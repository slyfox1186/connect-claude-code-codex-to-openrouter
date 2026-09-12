"""Core OpenRouter client for the Claude Code / Codex second-opinion bridge.

Standard library only, on purpose: the CLI must keep working even if no
third-party package is installed. The MCP front-end adds the one dependency
(the `mcp` SDK) and reuses everything here.
"""

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import fcntl
import fnmatch
import hashlib
import http.client
import json
import math
import os
import random
import re
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = [
    "MCP_TOOLS",
    "Config",
    "OpenRouterError",
    "account_usage",
    "as_list",
    "ask",
    "ask_panel",
    "attachment_summary",
    "category_models",
    "classify_attachment",
    "expand_paths",
    "get_api_key",
    "get_catalog",
    "guide_dirs",
    "guide_outline",
    "list_categories",
    "list_guides",
    "list_models",
    "load_config",
    "model_info",
    "override_allowed",
    "read_guide",
    "read_log",
    "resolve_category",
    "resolve_model",
    "search_guides",
    "sent_attachments",
    "split_embedded_question",
    "strip_call_syntax",
    "summarize_parts",
    "text_chars",
    "usable_turns",
    "verify_categories",
]

# The tools the MCP front-end exposes. Declared here, in the stdlib-only engine,
# because three things outside mcp_server.py need the list and none of them can
# import the mcp SDK to ask: install.sh writes it into Codex's enabled_tools,
# `orask doctor` compares it against what is registered, and the offline suite
# checks it against the decorators. A tool missing from a Codex registration
# fails silently, which is how list_llm_categories was dead there for a week.
MCP_TOOLS = (
    "ask_llm",
    "ask_panel",
    "list_llm_models",
    "list_llm_categories",
    "llm_model_info",
    "openrouter_usage",
    "read_guide",
)

API_BASE = "https://openrouter.ai/api/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_CONFIG = PROJECT_ROOT / "config" / "models.json"

CONFIG_DIR = Path(os.environ.get("ORASK_CONFIG_DIR", Path.home() / ".config" / "openrouter"))
ENV_FILE = CONFIG_DIR / "env"
USER_CONFIG = CONFIG_DIR / "config.json"
STATE_DIR = Path(os.environ.get("ORASK_STATE_DIR", Path.home() / ".local" / "state" / "orask"))
CACHE_DIR = Path(os.environ.get("ORASK_CACHE_DIR", Path.home() / ".cache" / "orask"))
CATALOG_CACHE = CACHE_DIR / "models.json"
CALL_LOG = STATE_DIR / "calls.jsonl"
THREAD_DIR = STATE_DIR / "threads"

# Ordered weakest -> strongest. Models advertise their own subset; a requested
# effort is snapped to the nearest value the target model actually accepts.
EFFORT_LADDER = ["minimal", "low", "medium", "high", "xhigh", "max"]

# Rough chars-per-token used only for the pre-flight cost estimate.
CHARS_PER_TOKEN = 3.6

# Floor on the room left for an answer inside the context window. Below this the prompt has
# eaten the window and the call would buy a truncated sentence, so it is refused instead.
MIN_ANSWER_TOKENS = 256

BINARY_HINT = re.compile(rb"[\x00-\x08\x0e-\x1f]")

# Read ceiling applied before the file is opened, so a huge file is never
# pulled into memory just to be truncated afterwards.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# Attachments ride along as base64, which inflates them by a third and is not
# subject to the text character cap, so they carry their own byte ceilings.
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 32 * 1024 * 1024

# Anything OpenRouter can carry as a real attachment instead of as pasted text.
# PDFs go through the file-parser plugin and work on every model; images and
# audio need the target model to advertise that input modality.
IMAGE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
AUDIO_FORMATS = {
    ".wav": "wav", ".mp3": "mp3", ".ogg": "ogg", ".flac": "flac",
    ".m4a": "m4a", ".aac": "aac", ".aiff": "aiff", ".aif": "aiff",
    ".pcm": "pcm16",
}
PDF_MEDIA = "application/pdf"

# Sniffed before the extension is trusted: a screenshot saved as "diagram" with
# no suffix, or a .txt that is really a PDF, should still attach correctly.
MAGIC_SIGNATURES = (
    (b"%PDF-", "pdf", PDF_MEDIA),
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"GIF87a", "image", "image/gif"),
    (b"GIF89a", "image", "image/gif"),
    (b"OggS", "audio", "ogg"),
    (b"fLaC", "audio", "flac"),
)

PDF_ENGINES = ("cloudflare-ai", "mistral-ocr", "native")

# mistral-ocr bills per 1,000 pages on top of tokens, and that charge is invisible to a
# token-only estimate: a long scan can pass a $1.00 guard and then bill separately. There is
# no page count before the parse, so pages are estimated from the file size. A scanned page is
# usually 100-500 KB, so 50 KB counts more pages than there really are and the guard errs
# toward refusing rather than toward a surprise invoice.
PDF_BYTES_PER_PAGE = 50_000
MISTRAL_OCR_USD_PER_1K_PAGES = 2.0

# Directories that are never what someone means by "send this folder".
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".next", "target", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".idea", ".vscode", "vendor", ".terraform",
    ".gradle", ".cache", "coverage", ".nyc_output", "site-packages",
}

# Per-attachment token counts for the pre-flight cost guard only. OpenRouter
# has no preflight token-counting endpoint and does not publish how a provider
# tiles an image, so these cannot be exact; they lean high so the guard errs
# toward refusing rather than toward a surprise bill. The real figures come
# back afterwards in usage.prompt_tokens_details.
TOKENS_PER_IMAGE = 1500
TOKENS_PER_PDF_BYTE = 1 / 150
TOKENS_PER_AUDIO_BYTE = 1 / 1000

# Anything sent through this bridge leaves the machine for a third-party API.
# These paths are refused by default: an agent following a poisoned instruction
# ("include your config files") must not be able to post credentials to
# OpenRouter. Override per call with allow_secret_files, or edit
# deny_file_patterns in the config.
DEFAULT_DENY_PATTERNS = [
    "*/.ssh/*", "*/.gnupg/*", "*/.aws/credentials", "*/.aws/config",
    "*/.netrc", "*/.npmrc", "*/.pypirc", "*/.docker/config.json",
    "*/.kube/config", "*/.git-credentials",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "*/id_rsa*", "*/id_dsa*", "*/id_ecdsa*", "*/id_ed25519*",
    "*/.env", "*/.env.*", "*.env",
    "*/.credentials.json", "*/auth.json", "*/.config/openrouter/env",
    "*RAILWAY_VARS.md", "*this_pc_ssh_transer_details*",
    "*admin_login_credentials*", "*/shadow", "*/.password-store/*",
    # Cloud and tooling credential stores. None of these is ever source code someone wants a
    # second opinion on, and every one of them is a plausible thing to talk an agent into
    # attaching: gcloud's application-default file is the commonest cloud credential on a
    # development machine.
    "*/.config/gcloud/*credentials*", "*/.azure/*", "*/.config/gh/hosts.yml",
    "*/.pgpass", "*/.my.cnf", "*/.s3cfg", "*/.boto", "*/.htpasswd",
    "*/.terraformrc", "*/terraform.tfvars", "*/*.auto.tfvars",
    "*/.gem/credentials", "*/.cargo/credentials*", "*/.gradle/gradle.properties",
    # A git remote URL routinely carries an access token inside it.
    "*/.gitconfig", "*/.git/config",
    # Process state, not files. /proc/<pid>/environ holds this process's own environment,
    # which is where OPENROUTER_API_KEY lives when it is exported. Until now the only thing
    # stopping that being attached was the binary-content heuristic noticing the NUL
    # separators. The informational parts of /proc (cpuinfo, meminfo) are deliberately left
    # readable, because asking a model about your own hardware is a real use.
    "/proc/*/environ", "/proc/*/cmdline", "/proc/*/mem", "/proc/*/maps",
    "/proc/*/fd/*", "/proc/*/task/*", "/proc/kcore", "/proc/keys", "/proc/key-users",
]


class OpenRouterError(RuntimeError):
    """Any failure worth showing to the calling agent verbatim."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

Config = dict[str, Any]

_DEFAULTS: Config = {
    "default_model": "kimi",
    "default_panel": ["kimi", "glm"],
    "default_effort": "high",
    "default_role": "advisor",
    "aliases": {"kimi": "moonshotai/kimi-k3", "glm": "z-ai/glm-5.3"},
    "allowed_models": [],
    "default_max_tokens": 32000,
    "max_context_tokens": 0,
    "context_compression": None,
    "request_timeout_s": 300,
    "max_input_chars": 600000,
    "max_file_chars": 200000,
    "max_attachment_bytes": MAX_ATTACHMENT_BYTES,
    "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
    "max_attachments": 20,
    "max_dir_files": 50,
    "thread_attachment_bytes": 4 * 1024 * 1024,
    "pdf_engine": "cloudflare-ai",
    "mistral_ocr_usd_per_1k_pages": MISTRAL_OCR_USD_PER_1K_PAGES,
    "max_cost_usd_per_call": 1.0,
    "catalog_ttl_s": 21600,
    "thread_max_messages": 20,
    "roles": {},
}

_config_cache: Config | None = None


def _validate_config(cfg: Config, path: Path) -> None:
    def reject(field: str, expected: str) -> None:
        raise OpenRouterError(f"config file {path}: {field} must be {expected}")

    for key in ("aliases", "roles"):
        if key in cfg and (not isinstance(cfg[key], dict) or any(
            not isinstance(v, str) or not v.strip() for v in cfg[key].values()
        )):
            reject(key, "an object of non-empty strings")
    for key in ("allowed_models", "default_panel", "deny_file_patterns",
                "category_exclude_vendors"):
        if key in cfg and (not isinstance(cfg[key], list) or any(
            not isinstance(v, str) or not v.strip() for v in cfg[key]
        )):
            reject(key, "a list of non-empty strings")
    for key in ("default_model", "default_role", "default_effort", "pdf_engine"):
        if key in cfg and cfg[key] is not None and not isinstance(cfg[key], str):
            reject(key, "a string or null")
    if cfg.get("cost_guard_on_unknown_pricing", "warn") not in ("warn", "block"):
        reject("cost_guard_on_unknown_pricing", "'warn' or 'block'")
    if "categories" in cfg:
        if not isinstance(cfg["categories"], dict):
            reject("categories", "an object")
        for name, spec in cfg["categories"].items():
            if not isinstance(spec, dict):
                reject(f"categories.{name}", "an object")
            for field in ("models", "aka"):
                value = spec.get(field, [])
                if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                    reject(f"categories.{name}.{field}", "a list of strings")
            for field in ("why", "measured"):
                if field in spec and not isinstance(spec[field], str):
                    reject(f"categories.{name}.{field}", "a string")


def load_config(refresh: bool = False) -> Config:
    """Packaged defaults, overlaid by ~/.config/openrouter/config.json."""
    global _config_cache
    if _config_cache is not None and not refresh:
        return _config_cache

    cfg: Config = dict(_DEFAULTS)
    for path in (PACKAGED_CONFIG, USER_CONFIG):
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise OpenRouterError(f"config file {path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise OpenRouterError(
                f"config file {path} must contain a JSON object, got {type(loaded).__name__}"
            )
        _validate_config(loaded, path)
        for key, value in loaded.items():
            if key.startswith("_"):
                continue
            # aliases/roles merge so a user file can add one entry without
            # having to restate the whole table.
            if key in ("aliases", "roles") and isinstance(value, dict):
                merged = dict(cfg.get(key) or {})
                merged.update(value)
                cfg[key] = merged
            else:
                cfg[key] = value

    _config_cache = cfg
    return cfg


def get_api_key() -> str:
    """Key from the environment, else from the 0600 key file."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key

    if ENV_FILE.is_file():
        try:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise OpenRouterError(f"cannot read the key file {ENV_FILE}: {exc}") from exc
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY":
                # An empty value must read as "no key", not produce a 401 later.
                found = value.strip().strip("'\"")
                if found:
                    return found

    raise OpenRouterError(
        "No OpenRouter API key. Set OPENROUTER_API_KEY, or put "
        f"OPENROUTER_API_KEY=sk-or-... in {ENV_FILE} (chmod 600)."
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

# GETs are free to retry. A POST to /chat/completions is not: a 5xx can arrive
# after the provider already generated (and billed) the tokens, so retrying it
# would pay twice. POSTs therefore retry only on statuses that mean the request
# never reached a model.
RETRY_STATUS_GET = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}
RETRY_STATUS_POST = {408, 429}


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 60.0,
    retries: int = 3,
) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
        # Current attribution header; X-Title is the legacy alias, so both are
        # sent. No HTTP-Referer, which is what would opt this key's usage into
        # OpenRouter's public app rankings.
        "X-OpenRouter-Title": "claude-codex-second-opinion",
        "X-Title": "claude-codex-second-opinion",
    }

    retryable = RETRY_STATUS_POST if method == "POST" else RETRY_STATUS_GET
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                received = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(received) > MAX_RESPONSE_BYTES:
                raise OpenRouterError("OpenRouter response exceeded the 64 MiB read limit")
            raw = received.decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise OpenRouterError(
                    f"OpenRouter returned a non-JSON response to {method} {path}; "
                    "the response body was omitted because it may contain private request data"
                ) from exc
            if not isinstance(parsed, dict):
                raise OpenRouterError(
                    f"OpenRouter returned {type(parsed).__name__}, expected a JSON object"
                )
            return parsed
        except urllib.error.HTTPError as exc:
            detail = ""
            # Any failure to read the body is fine: the status line alone is enough to
            # build a useful message, and the body is often already gone.
            try:
                with contextlib.suppress(Exception):
                    detail = exc.read(2000).decode("utf-8", "replace")
            finally:
                exc.close()
            message = _http_message(exc.code, detail)
            if exc.code in retryable and attempt < retries:
                last_error = OpenRouterError(message)
                time.sleep(min(2**attempt + random.random(), 20))
                continue
            raise OpenRouterError(message) from exc
        except urllib.error.URLError as exc:
            last_error = OpenRouterError(f"network error calling OpenRouter: {exc.reason}")
            # A POST may only be repeated when the failure provably happened
            # before the request reached a model. A reset or dropped connection
            # part-way through generation has already been billed, so repeating
            # it would pay twice.
            safe_to_repeat = method != "POST" or isinstance(
                exc.reason, (ConnectionRefusedError, socket.gaierror)
            )
            if attempt < retries and safe_to_repeat:
                time.sleep(min(2**attempt + random.random(), 20))
                continue
            raise last_error from exc
        except TimeoutError as exc:
            # Deliberately not retried: the provider may already be generating,
            # and a repeat would be billed a second time.
            last_error = OpenRouterError(
                f"OpenRouter request timed out after {timeout:.0f}s. "
                "Reasoning models on a large context can be slow; inspect the task size "
                "and raise request_timeout_s if appropriate. No automatic retry was made."
            )
            raise last_error from exc
        except (OSError, http.client.HTTPException) as exc:
            # A reset or incomplete read can happen after generation starts. It is not
            # evidence that repeating this POST is free, so return an actionable failure.
            raise OpenRouterError(
                f"OpenRouter connection failed during {method} {path} ({type(exc).__name__}); "
                "no automatic retry was made because the request may already be billed"
            ) from exc

    raise last_error or OpenRouterError("OpenRouter request failed")


# OpenRouter's stable machine-readable failure category. On /chat/completions
# it arrives at error.metadata.error_type. These are the ones an attachment can
# provoke, and each has a different fix, which a bare HTTP 400 does not convey.
ERROR_TYPE_HINTS = {
    "invalid_image": "the image is corrupt or unreadable; re-export it and try again",
    "image_too_large": "the image is over this provider's size or pixel limit; "
                       "scale it down and send it again",
    "image_too_small": "the image is under this provider's minimum pixel size",
    "unsupported_image_format": "this provider does not take that image format; "
                                "convert it to png or jpg",
    "image_not_found": "the referenced image could not be resolved",
    "image_download_failed": "OpenRouter could not fetch the image from that URL",
}


def _http_message(code: int, detail: str) -> str:
    hint = {
        401: "the API key was rejected - check the key in ~/.config/openrouter/env",
        402: "insufficient OpenRouter credits - top up at openrouter.ai/credits",
        403: "the key is not allowed to use this model (moderation or privacy setting)",
        404: "no such model slug - run 'orask models --search <name>' for exact slugs",
        429: "rate limited by OpenRouter or the upstream provider",
    }.get(code)
    parsed = ""
    try:
        obj = json.loads(detail)
        error = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(error, dict):
            message = error.get("message")
            parsed = message if isinstance(message, str) else ""
            metadata = error.get("metadata")
            typed = metadata.get("error_type") if isinstance(metadata, dict) else None
        else:
            typed = None
        if isinstance(typed, str) and typed:
            hint = ERROR_TYPE_HINTS.get(typed, f"error_type: {typed}")
    except (json.JSONDecodeError, AttributeError):
        pass
    text = f"OpenRouter HTTP {code}"
    if hint:
        text += f" ({hint})"
    if parsed:
        text += f": {parsed.strip()[:800]}"
    return text


# --------------------------------------------------------------------------
# model catalog
# --------------------------------------------------------------------------

_catalog_cache: list[dict[str, Any]] | None = None
_catalog_fetched_at: float = 0.0
_catalog_failed_at: float = 0.0
# One fetch at a time. A panel fans out into threads that all miss an empty cache at the same
# moment, and without this each of them issues its own /models request.
_catalog_lock = threading.Lock()
# A single ask() looks the catalogue up five or six times. With the network down and nothing
# cached, every one of those was a full retry cycle, so the call stalled for tens of seconds
# before failing. One attempt per cooldown is enough to notice the network came back.
CATALOG_FAILURE_COOLDOWN_S = 60.0


def _valid_catalog(data: Any) -> bool:
    """Validate the fields this client consumes, leaving unrelated provider fields alone."""
    if not isinstance(data, list) or not data:
        return False
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            return False
        for key in ("name", "description"):
            if entry.get(key) is not None and not isinstance(entry[key], str):
                return False
        for key in ("pricing", "top_provider", "reasoning", "architecture", "benchmarks"):
            if entry.get(key) is not None and not isinstance(entry[key], dict):
                return False
        top = entry.get("top_provider") or {}
        for value in (entry.get("created"), entry.get("context_length"),
                      top.get("context_length"), top.get("max_completion_tokens")):
            if value is not None and (not isinstance(value, (int, float))
                                      or _nonnegative_number(value) is None):
                return False
        reasoning = entry.get("reasoning") or {}
        architecture = entry.get("architecture") or {}
        for value in (reasoning.get("supported_efforts"), entry.get("supported_parameters"),
                      architecture.get("input_modalities")):
            if value is not None and (not isinstance(value, list)
                                      or any(not isinstance(v, str) for v in value)):
                return False
        bench = (entry.get("benchmarks") or {}).get("artificial_analysis")
        if bench is not None:
            if not isinstance(bench, dict):
                return False
            for key in ("intelligence_index", "coding_index", "agentic_index"):
                if bench.get(key) is not None and (
                    not isinstance(bench[key], (int, float))
                    or _nonnegative_number(bench[key]) is None
                ):
                    return False
    return True


def get_catalog(refresh: bool = False, allow_stale: bool = True) -> list[dict[str, Any]]:
    """Live model list, memoised in-process and cached on disk.

    Never raises on a network failure when a cached copy exists: model lookup
    degrading to a stale catalogue beats the whole tool going down.
    """
    ttl = _float_setting("catalog_ttl_s", 21600.0)

    # The MCP server is long-lived: without a TTL on the in-memory copy it would
    # serve the catalogue it started with for as long as the process lives, and
    # silently use stale prices, efforts and model lists.
    def _fresh() -> list[dict[str, Any]] | None:
        if (
            _catalog_cache is not None
            and not refresh
            and (time.time() - _catalog_fetched_at) < ttl
        ):
            return _catalog_cache
        return None

    hit = _fresh()
    if hit is not None:
        return hit

    with _catalog_lock:
        # Checked again inside the lock: another thread may have fetched while this one waited.
        hit = _fresh()
        if hit is not None:
            return hit
        return _fetch_catalog(refresh, allow_stale, ttl)


def _fetch_catalog(
    refresh: bool, allow_stale: bool, ttl: float
) -> list[dict[str, Any]]:
    """The slow half of get_catalog. Only ever called with _catalog_lock held."""
    global _catalog_cache, _catalog_fetched_at, _catalog_failed_at
    cached: list[dict[str, Any]] | None = None
    cache_age = float("inf")

    if CATALOG_CACHE.is_file():
        try:
            blob = json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))
            data = blob.get("data")
            # Validate the cache exactly as strictly as a fresh fetch: a corrupt
            # or hand-edited file would otherwise crash every lookup downstream.
            cached = data if _valid_catalog(data) else None
            cache_age = time.time() - float(blob.get("fetched_at") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
            cached = None

    if cached and not refresh and cache_age < ttl:
        _catalog_cache = cached
        _catalog_fetched_at = time.time() - cache_age
        return cached

    def _fall_back_to_stale() -> list[dict[str, Any]] | None:
        global _catalog_cache, _catalog_fetched_at
        if cached and allow_stale:
            _catalog_cache = cached
            _catalog_fetched_at = time.time() - min(cache_age, ttl)
            return cached
        return None

    if not refresh and (time.time() - _catalog_failed_at) < CATALOG_FAILURE_COOLDOWN_S:
        stale = _fall_back_to_stale()
        if stale is not None:
            return stale
        raise OpenRouterError(
            "the OpenRouter model catalogue is unreachable; the last attempt failed less than "
            f"{int(CATALOG_FAILURE_COOLDOWN_S)}s ago, so this one was not retried. Check the "
            "network, then try again."
        )

    try:
        data = _request("GET", "/models", timeout=45.0, retries=2).get("data") or []
        if not _valid_catalog(data):
            raise OpenRouterError("OpenRouter returned an unusable model catalogue")
        _write_json_atomic(CATALOG_CACHE, {"fetched_at": time.time(), "data": data})
        _catalog_cache = data
        _catalog_fetched_at = time.time()
        _catalog_failed_at = 0.0
        return data
    except OpenRouterError:
        _catalog_failed_at = time.time()
        stale = _fall_back_to_stale()
        if stale is not None:
            return stale
        raise


def _write_json_atomic(
    target: Path, payload: dict[str, Any], indent: int | None = None
) -> bool:
    """Write via a per-process temp file so concurrent writers cannot collide.

    A fixed ".tmp" name is not safe here: a panel fans out into threads and the
    CLI and MCP server can run at the same time.

    Returns False instead of raising on an OS error. Every caller is persisting
    an optimisation (the catalogue cache) or a record written after a paid API
    call (a thread transcript); a read-only or full ~/.cache must never take
    down a working fetch or discard an answer the user already paid for.
    """
    tmp: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.is_symlink():
            return False
        fd, filename = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        tmp = Path(filename)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)
        return True
    except OSError:
        return False
    finally:
        if tmp is not None and tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def _catalog_or_empty() -> list[dict[str, Any]]:
    try:
        return get_catalog()
    except OpenRouterError:
        return []


def _intelligence(model: dict[str, Any]) -> float:
    bench = (model.get("benchmarks") or {}).get("artificial_analysis") or {}
    value = bench.get("intelligence_index")
    return float(value) if isinstance(value, (int, float)) else -1.0


def _rank_key(model: dict[str, Any]) -> tuple:
    slug = model.get("id", "")
    return (
        _intelligence(model),
        0 if slug.startswith("~") else 1,  # prefer a concrete, auditable slug
        float(model.get("created") or 0),
    )


# --------------------------------------------------------------------------
# model resolution
# --------------------------------------------------------------------------


def resolve_model(spec: str) -> tuple[str, str | None]:
    """Turn 'kimi', 'glm-5.3' or a full slug into a real slug.

    Returns (slug, note) where note is a human-readable warning when the
    request was not satisfied exactly as written.
    """
    if not spec or not spec.strip():
        spec = load_config().get("default_model") or "kimi"
    spec = spec.strip()

    cfg = load_config()
    aliases = {k.lower(): v for k, v in (cfg.get("aliases") or {}).items()}
    catalog = _catalog_or_empty()
    known = {m.get("id") for m in catalog}
    allowed = [s for s in (cfg.get("allowed_models") or []) if s]

    key = spec.lower()
    note: str | None = None

    if key in aliases:
        pinned = aliases[key]
        if not known or pinned in known:
            return _enforce_allowed(pinned, allowed), None
        # Pinned slug retired upstream: fall back to the best current match
        # for the alias name rather than failing the call.
        slug, match_note = _fuzzy(key, catalog)
        if slug:
            return (
                _enforce_allowed(slug, allowed),
                f"alias '{spec}' is pinned to '{pinned}', which OpenRouter no longer lists; "
                f"used '{slug}' instead. Update config/models.json to make this permanent."
                + (f" ({match_note})" if match_note else ""),
            )
        raise OpenRouterError(
            f"alias '{spec}' points at '{pinned}', which OpenRouter no longer lists, "
            "and no similar model was found. Run 'orask models --search <name>'."
        )

    if "/" in spec:
        bare = spec.lstrip("~")
        if not known or spec in known or f"~{bare}" in known or bare in known:
            exact = spec if (not known or spec in known) else (
                bare if bare in known else f"~{bare}"
            )
            return _enforce_allowed(exact, allowed), None
        slug, match_note = _fuzzy(bare.split("/", 1)[1], catalog)
        if slug:
            return (
                _enforce_allowed(slug, allowed),
                f"'{spec}' is not in the OpenRouter catalogue; used the closest match "
                f"'{slug}'." + (f" ({match_note})" if match_note else ""),
            )
        raise OpenRouterError(
            f"'{spec}' is not an OpenRouter model slug and no close match was found. "
            "Run 'orask models --search <name>' to see real slugs."
        )

    slug, match_note = _fuzzy(key, catalog)
    if slug:
        alias_list = ", ".join(sorted(aliases)) or "none configured"
        note = (
            f"'{spec}' is not a configured alias ({alias_list}); matched the live "
            f"catalogue to '{slug}'." + (f" ({match_note})" if match_note else "")
        )
        return _enforce_allowed(slug, allowed), note

    raise OpenRouterError(
        f"cannot resolve model '{spec}'. Configured aliases: "
        f"{', '.join(sorted(aliases)) or 'none'}. "
        "Run 'orask models --search <name>' to find a slug, or pass a full slug."
    )


# --------------------------------------------------------------------------
# categories
#
# "ask an LLM that is good at coding" has to land on a real slug. Categories
# map a plain-English capability onto the two current benchmark leaders for
# it, defined in config/models.json with the evidence and the date attached.
#
# OpenAI and Anthropic are excluded from category picks by default. This
# bridge exists to fetch an opinion from outside the agent asking, and Claude
# Code is Anthropic while Codex is OpenAI: routing a category back to those
# vendors returns the house view the asker already holds. A full slug asked
# for by name is still honoured.
# --------------------------------------------------------------------------


def _norm_category(term: str) -> str:
    return re.sub(r"[\s\-]+", "_", (term or "").strip().lower())


def _vendor(slug: str) -> str:
    return slug.split("/", 1)[0].lstrip("~").lower() if "/" in slug else ""


def excluded_vendors() -> list[str]:
    cfg = load_config()
    listed = cfg.get("category_exclude_vendors") or []
    return [str(v).strip().lower() for v in listed if str(v).strip()]


def resolve_category(term: str) -> tuple[str, dict[str, Any]] | None:
    """Match plain English onto a configured category.

    Handles the name itself, the `aka` synonyms, and a phrase the agent lifted
    straight from the user ("something good at long context"). Returns None
    rather than guessing when nothing matches, so the caller can fall back to
    the default model instead of silently picking a category.
    """
    cats = load_config().get("categories") or {}
    if not term or not term.strip() or not cats:
        return None

    wanted = _norm_category(term)
    if wanted in cats:
        return wanted, cats[wanted]

    for name, spec in cats.items():
        if wanted in [_norm_category(a) for a in (spec.get("aka") or [])]:
            return name, spec

    # A phrase rather than a keyword: score each category on how many of its
    # labels appear in it. Longest label wins, so "long context" beats "context".
    best: tuple[int, str, dict[str, Any]] | None = None
    for name, spec in cats.items():
        for label in [name, *list(spec.get("aka") or [])]:
            token = _norm_category(label)
            if token and token in wanted and (best is None or len(token) > best[0]):
                best = (len(token), name, spec)
    if best:
        return best[1], best[2]
    return None


def _heal_slug(slug: str, catalog: list[dict[str, Any]], banned: list[str]) -> str | None:
    """Closest live model to a slug that has been retired upstream.

    Tries the full model name first, then drops trailing hyphen-separated
    segments, so 'kimi-k9-retired' falls back through 'kimi-k9' to 'kimi' and
    lands on the current Kimi rather than failing outright. A replacement from
    an excluded vendor is no replacement at all.
    """
    name = slug.split("/", 1)[-1]
    parts = name.split("-")
    for cut in range(len(parts), 0, -1):
        candidate, _ = _fuzzy("-".join(parts[:cut]), catalog)
        if candidate and (not banned or _vendor(candidate) not in banned):
            return candidate
    return None


def category_models(term: str) -> tuple[list[str], list[str]]:
    """The models configured for a category, validated against the catalogue.

    Returns (slugs, notes). A slug that has been retired upstream is replaced
    by the closest live match rather than failing the call, and one that
    violates the vendor exclusion is dropped, both with a note saying so.
    """
    match = resolve_category(term)
    if not match:
        known = ", ".join(sorted(load_config().get("categories") or {})) or "none configured"
        raise OpenRouterError(
            f"'{term}' is not a known category. Configured categories: {known}. "
            "Use list_llm_categories to see what each one is for, or pass `model` "
            "with an explicit slug."
        )

    name, spec = match
    catalog = _catalog_or_empty()
    listed = {m.get("id") for m in catalog}
    banned = excluded_vendors()
    slugs: list[str] = []
    notes: list[str] = []

    for slug in as_list(spec.get("models")):
        if banned and _vendor(slug) in banned:
            notes.append(
                f"category '{name}' lists {slug}, but vendor '{_vendor(slug)}' is excluded "
                "from category picks; skipped it."
            )
            continue
        if not listed or slug in listed:
            slugs.append(slug)
            continue
        replacement = _heal_slug(slug, catalog, banned)
        if replacement:
            slugs.append(replacement)
            notes.append(
                f"category '{name}' pins {slug}, which OpenRouter no longer lists; "
                f"used '{replacement}'. Update config/models.json to make this permanent."
            )
        else:
            notes.append(
                f"category '{name}' pins {slug}, which is no longer available; skipped it."
            )

    if not slugs:
        raise OpenRouterError(
            f"category '{name}' has no usable models left: none of "
            f"{', '.join(as_list(spec.get('models'))) or 'its entries'} are currently "
            "available. Edit the categories block in config/models.json."
        )
    return slugs, notes


def list_categories() -> list[dict[str, Any]]:
    """Every configured category, for display."""
    cats = load_config().get("categories") or {}
    rows = []
    for name, spec in cats.items():
        rows.append({
            "category": name,
            "models": as_list(spec.get("models")),
            "aka": as_list(spec.get("aka")),
            "why": spec.get("why") or "",
            "measured": spec.get("measured") or "",
        })
    return rows


def verify_categories() -> list[dict[str, Any]]:
    """Check every pinned category slug against the live catalogue.

    Benchmark leadership moves, so this is the maintenance check: it says
    which pins are still real, which have been retired, and what each one
    currently scores.
    """
    catalog = get_catalog(refresh=True, allow_stale=False)
    by_id = {m.get("id"): m for m in catalog}
    banned = excluded_vendors()
    rows = []
    for row in list_categories():
        for slug in row["models"]:
            model = by_id.get(slug)
            index = None
            if model:
                index = ((model.get("benchmarks") or {}).get("artificial_analysis") or {}).get(
                    "intelligence_index"
                )
            rows.append({
                "category": row["category"],
                "slug": slug,
                "available": bool(model),
                "excluded_vendor": bool(banned and _vendor(slug) in banned),
                "intelligence_index": index,
                "context": (model or {}).get("context_length"),
                "measured": row["measured"],
            })
    return rows


def _enforce_allowed(slug: str, allowed: list[str]) -> str:
    if allowed and slug not in allowed:
        raise OpenRouterError(
            f"model '{slug}' is not in allowed_models "
            f"({', '.join(allowed)}). Edit allowed_models in the config to permit it."
        )
    return slug


def _fuzzy(term: str, catalog: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Best current model whose slug or name matches `term`.

    Ranked by published intelligence index first, so 'kimi' lands on the
    vendor's flagship rather than a small or elderly variant.
    """
    if not catalog or not term:
        return None, None

    needle = re.sub(r"[\s_]+", "-", term.strip().lower())
    if not needle:
        return None, None

    scored: list[tuple[tuple, dict[str, Any]]] = []
    for model in catalog:
        slug = (model.get("id") or "").lower()
        if not slug:
            continue
        explicit = needle in slug
        # Batch endpoints answer in minutes and free tiers are rate limited:
        # never pick them by fuzzy match unless the user typed them.
        if (":batch" in slug and ":batch" not in needle) or (
            ":free" in slug and ":free" not in needle
        ):
            continue
        name = (model.get("name") or "").lower()
        bare = slug.lstrip("~")
        tail = bare.split("/", 1)[1] if "/" in bare else bare

        if tail == needle or bare == needle:
            quality = 4
        elif tail.startswith(needle):
            quality = 3
        elif explicit:
            quality = 2
        elif needle in name.replace(" ", "-"):
            quality = 1
        else:
            continue
        scored.append(((quality, *_rank_key(model)), model))

    if not scored:
        return None, None
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][1]
    others = [m.get("id") for _, m in scored[1:4]]
    note = f"other candidates: {', '.join(o for o in others if o)}" if others else None
    return best.get("id"), note


_index_source: list[dict[str, Any]] | None = None
_index: dict[str, dict[str, Any]] = {}


def _find(slug: str) -> dict[str, Any]:
    """The catalogue entry for a slug, or an empty dict.

    Indexed rather than scanned: ask() looks a model up four to six times per call. The index
    is keyed to the identity of the list it was built from, so replacing _catalog_cache - which
    the test suite does directly - rebuilds it instead of serving a stale answer.
    """
    global _index_source, _index
    catalog = _catalog_or_empty()
    if catalog is not _index_source:
        _index = {str(m.get("id")): m for m in catalog if m.get("id")}
        _index_source = catalog
    return _index.get(slug) or {}


def clamp_effort(slug: str, effort: str | None) -> tuple[str | None, str | None]:
    """Snap a requested reasoning effort onto what the model accepts.

    Kimi K3 and GLM 5.3, for instance, expose only max/high/low - sending
    'medium' to them is not valid, so it is snapped to the nearest rung
    (ties round upward, since a second opinion is worth more thinking).
    """
    if effort in (None, "", "none", "off"):
        return None, None

    model = _find(slug)
    reasoning = model.get("reasoning") or {}
    supported = [e for e in (reasoning.get("supported_efforts") or []) if e in EFFORT_LADDER]

    params = model.get("supported_parameters") or []
    if not supported:
        if model and not ({"reasoning", "reasoning_effort"} & set(params)):
            return None, f"{slug} does not take a reasoning effort; sent without one"
        if effort not in EFFORT_LADDER:
            return None, (
                f"effort '{effort}' is not a recognised level "
                f"({'/'.join(EFFORT_LADDER)}); sent without a reasoning effort"
            )
        return effort, None  # model unknown or efforts undeclared: pass through

    if effort in supported:
        return effort, None

    if effort not in EFFORT_LADDER:
        declared = reasoning.get("default_effort")
        if declared in supported:
            fallback = declared
        else:
            fallback = min(
                supported,
                key=lambda e: (
                    abs(EFFORT_LADDER.index(e) - EFFORT_LADDER.index("high")),
                    -EFFORT_LADDER.index(e),
                ),
            )
        return fallback, f"effort '{effort}' is not a known level; used '{fallback}'"

    want = EFFORT_LADDER.index(effort)
    best = min(
        supported,
        key=lambda e: (abs(EFFORT_LADDER.index(e) - want), -EFFORT_LADDER.index(e)),
    )
    return best, f"{slug} accepts only {'/'.join(supported)}; effort '{effort}' snapped to '{best}'"


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------


def denied_by_policy(path: Path, patterns: list[str]) -> str | None:
    """The deny pattern this path matches, if any.

    Matched against the path as given, its fully resolved form, and its bare
    name. Checking only the literal string would let `/tmp/notes.txt`, a symlink
    to `~/.ssh/id_rsa`, walk straight past the denylist.
    """
    forms = {path.as_posix(), path.name}
    try:
        forms.add(path.resolve().as_posix())
        forms.add(path.resolve().name)
    except (OSError, RuntimeError):
        pass
    # fnmatch is case-sensitive on POSIX, which would let ID_RSA or FOO.PEM
    # walk past lowercase patterns.
    lowered = {form.lower() for form in forms}
    for pattern in patterns:
        needle = pattern.lower()
        for text in lowered:
            if fnmatch.fnmatch(text, needle):
                return pattern
    return None


def _open_regular_fd(path: Path, flags: int = os.O_RDONLY) -> int:
    """Open a regular file without following a swapped leaf or ancestor symlink.

    Walk the already chosen path using directory descriptors without re-resolving it.
    Callers own the returned descriptor; every intermediate descriptor closes here.
    """
    parent = path.absolute().parent
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(parent.anchor, directory_flags)
    try:
        for part in parent.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, flags | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600,
                     dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file (fifo, device or socket); skipped")
        if flags & (os.O_WRONLY | os.O_RDWR):
            if info.st_nlink != 1:
                raise OSError(f"refusing to write a hardlinked state file: {path}")
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _slurp(path: Path, ceiling: int) -> tuple[bytes, str | None]:
    """Read a regular file up to `ceiling` bytes, or say why it was skipped.

    A FIFO, device or socket would block a plain read forever (or return
    endless data) and hang the bridge. The descriptor is opened first and
    checked with fstat, so nothing can swap a regular file for a FIFO between
    the check and the open. O_NOFOLLOW is safe because the caller passes an
    already-resolved path, and it closes the last symlink race.
    """
    try:
        fd = _open_regular_fd(path)
    except OSError as exc:
        return b"", f"could not open {path}: {exc.strerror or exc}"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return b"", f"{path} is not a regular file (fifo, device or socket); skipped"
        if info.st_size > ceiling:
            return b"", (
                f"{path} is {_human_bytes(info.st_size)}, over the "
                f"{_human_bytes(ceiling)} read ceiling; skipped"
            )
        chunks: list[bytes] = []
        remaining = ceiling + 1
        while remaining > 0:
            block = os.read(fd, min(1 << 20, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        if remaining == 0:
            return b"", f"{path} grew beyond the {_human_bytes(ceiling)} read ceiling; skipped"
        return b"".join(chunks), None
    except OSError as exc:
        return b"", f"could not read {path}: {exc.strerror or exc}"
    finally:
        os.close(fd)


def _peek(path: Path, count: int = 16) -> bytes:
    """First few bytes, for sniffing the real type. Never raises."""
    try:
        fd = _open_regular_fd(path)
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        return os.read(fd, count)
    except OSError:
        return b""
    finally:
        os.close(fd)


def classify_attachment(path: Path) -> tuple[str, str] | None:
    """Classify a file as ("pdf"|"image"|"audio", media type or audio format).

    None means "send this one as text", which is the right answer for source
    code and prose. The magic bytes win over the extension: a screenshot saved
    with no suffix still attaches as an image, and a .txt that is really a PDF
    is not pasted in as mojibake.
    """
    # 64 bytes rather than 16: the extension fallback below needs enough of the head to tell
    # text from binary, and 16 is not enough to be sure.
    head = _peek(path, 64)
    for signature, kind, media in MAGIC_SIGNATURES:
        if head.startswith(signature):
            return kind, media
    # ID3 is only three bytes, so a CSV whose first column is called ID3 looks like an MP3.
    # A real ID3v2 tag names its major version next, and 2, 3 and 4 are the only ones there
    # have ever been.
    if head[:3] == b"ID3" and len(head) >= 4 and head[3] in (2, 3, 4):
        return "audio", "mp3"
    # RIFF....WEBP and RIFF....WAVE share a container, so the tag at byte 8 is
    # what separates them.
    if head[:4] == b"RIFF" and len(head) >= 12:
        if head[8:12] == b"WEBP":
            return "image", "image/webp"
        if head[8:12] == b"WAVE":
            return "audio", "wav"

    suffix = path.suffix.lower()
    if suffix not in IMAGE_MEDIA and suffix not in AUDIO_FORMATS and suffix != ".pdf":
        return None
    # The extension still has to carry the untagged formats: an MP3 with no ID3 tag starts
    # with a frame sync, not a signature. But a text file someone named notes.mp3 would be
    # attached as corrupt audio and billed, so a head that reads as plain text is taken at
    # its word over the name.
    if head and not BINARY_HINT.search(head):
        return None
    if suffix == ".pdf":
        return "pdf", PDF_MEDIA
    if suffix in IMAGE_MEDIA:
        return "image", IMAGE_MEDIA[suffix]
    return "audio", AUDIO_FORMATS[suffix]


def _human_bytes(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1e6:.1f} MB"
    if size >= 1000:
        return f"{size / 1e3:.0f} KB"
    return f"{size} B"


def expand_paths(entries: Iterable[str], base: Path, limit: int) -> tuple[list[Path], list[str]]:
    """Resolve the `files` argument to real files, expanding any directory.

    Passing a directory is the shorthand worth having: "send it this folder"
    should not mean listing forty paths by hand. Build output, dependency trees
    and VCS metadata are pruned, because nobody means those.
    """
    return _expand_paths(entries, base, limit, [])


def _expand_paths(
    entries: Iterable[str], base: Path, limit: int, patterns: list[str]
) -> tuple[list[Path], list[str]]:
    out: list[Path] = []
    notes: list[str] = []
    seen: set[str] = set()

    def refused(candidate: Path) -> bool:
        matched = denied_by_policy(candidate, patterns)
        if matched:
            notes.append(
                f"REFUSED to send {candidate} to a third-party API: "
                f"it matches the deny pattern {matched!r}."
            )
        return matched is not None

    def take(candidate: Path) -> None:
        key = candidate.as_posix()
        if key not in seen:
            seen.add(key)
            out.append(candidate)

    for entry in entries:
        if not entry or not str(entry).strip():
            continue
        candidate = Path(str(entry).strip()).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        # Check the requested name before resolution discards a denied symlink alias.
        if refused(candidate):
            continue
        # Resolve absolute paths too, not just relative ones: an unresolved
        # absolute path lets a symlink (or a "..") slip past the denylist.
        try:
            candidate = candidate.resolve()
        except (OSError, RuntimeError):
            notes.append(f"could not resolve path, skipped: {candidate}")
            continue
        if not candidate.exists():
            notes.append(f"file not found, skipped: {candidate}")
            continue
        if not candidate.is_dir():
            take(candidate)
            continue

        if limit <= 0:
            notes.append(
                f"{candidate} is a directory and max_dir_files is 0, so it was not "
                "expanded; name the files you want instead"
            )
            continue
        found: list[Path] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(candidate):
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
            )
            for name in sorted(filenames):
                found.append(Path(dirpath) / name)
                if len(found) > limit:
                    truncated = True
                    break
            if truncated:
                break
        if not found:
            notes.append(f"{candidate} holds no files worth sending, skipped")
            continue
        if truncated:
            found = found[:limit]
            notes.append(
                f"{candidate} holds more than {limit} files; sent the first {limit}. "
                "Name the files you want, or raise max_dir_files in the config."
            )
        notes.append(f"expanded directory {candidate} to {len(found)} files")
        for item in found:
            if refused(item):
                continue
            try:
                take(item.resolve())
            except (OSError, RuntimeError):
                continue

    return out, notes


def _read_file(path: Path, limit: int) -> tuple[str, str | None]:
    raw, problem = _slurp(path, MAX_FILE_BYTES)
    if problem:
        return "", problem
    if not raw:
        return "", f"{path} is empty"
    if BINARY_HINT.search(raw[:8192]):
        return "", f"{path} looks binary; skipped"
    if limit <= 0:
        return "", f"file contents are disabled (max_file_chars={limit}); {path} skipped"
    text = raw.decode("utf-8", "replace")
    if len(text) > limit:
        head = int(limit * 0.7)
        tail = limit - head
        marker = f"\n\n... [{len(raw)} bytes total, middle elided by orask] ...\n\n"
        # text[-0:] would be the entire string, not an empty one.
        text = text[:head] + marker + (text[len(text) - tail:] if tail > 0 else "")
        return text, f"{path} truncated to {limit} chars"
    return text, None


# --------------------------------------------------------------------------
# argument shapes
#
# A calling agent assembles these arguments as JSON and sometimes gets the
# shape wrong: a list arrives as one comma-joined string, or the question ends
# up pasted inside `context` wrapped in <question> tags with a stray closing
# tag from the agent's own tool-call syntax trailing after it. The call is
# recoverable in every one of those cases, and recovering it beats billing a
# model for a prompt full of markup or making the agent burn a turn on a
# schema error.
# --------------------------------------------------------------------------

# Tags seen leaking out of tool-call syntax. Only these are ever stripped, and
# only when they wrap or terminate the whole value, so a genuine question about
# XML keeps its markup.
CALL_SYNTAX_TAGS = (
    "question", "context", "prompt", "query", "task", "instructions",
    "parameter", "parameters", "arg", "args", "argument", "arguments",
    "invoke", "function_calls", "antml:invoke", "antml:parameter",
    "antml:function_calls",
)

_OPEN_TAG = re.compile(r"\A<\s*([A-Za-z_:][\w:.-]*)(\s[^<>]*)?>\s*")
_CLOSE_TAG = re.compile(r"\s*</\s*([A-Za-z_:][\w:.-]*)\s*>\s*\Z")

# "## Question", "# Question", "Question:" or "QUESTION -" on its own line.
_QUESTION_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?question[ \t]*[:.\-]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def as_list(value: Any) -> list[str]:
    """Coerce a `files`/`models` argument into a list of strings.

    A bare string is the common mistake, and iterating it would treat every
    character as a separate entry. One path or slug per line if newlines are
    present, otherwise comma separated.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        pieces = text.splitlines() if "\n" in text else text.split(",")
        return [piece.strip() for piece in pieces if piece.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def strip_call_syntax(value: str | None) -> str | None:
    """Drop tool-call markup that has leaked into an argument value.

    Removes a wrapper pair that encloses the entire value, and an orphan
    closing tag left at the end by a truncated call. Markup anywhere else is
    left alone: it is far more likely to be content than a mistake.
    """
    if not value:
        return value
    text = value.strip()
    for _ in range(6):  # nesting this deep is already pathological
        before = text
        opening = _OPEN_TAG.match(text)
        if opening and opening.group(1).lower() in CALL_SYNTAX_TAGS:
            tail = re.search(
                rf"</\s*{re.escape(opening.group(1))}\s*>\s*\Z", text, re.IGNORECASE
            )
            if tail:
                text = text[opening.end():tail.start()].strip()
        # An orphan closer with no matching opener means the call was truncated.
        closing = _CLOSE_TAG.search(text)
        if (
            closing
            and closing.group(1).lower() in CALL_SYNTAX_TAGS
            and not re.search(
                rf"<\s*{re.escape(closing.group(1))}(\s[^<>]*)?>", text[: closing.start()],
                re.IGNORECASE,
            )
        ):
            text = text[: closing.start()].strip()
        if text == before:
            break
    return text


def _extract_tagged(text: str, tag: str) -> tuple[str, str] | None:
    """Pull one <tag>...</tag> block out of `text`, returning (inner, rest)."""
    match = re.search(
        rf"<\s*{tag}(?:\s[^<>]*)?>(.*?)</\s*{tag}\s*>", text, re.IGNORECASE | re.DOTALL
    )
    if not match or not match.group(1).strip():
        return None
    rest = (text[: match.start()] + "\n\n" + text[match.end():]).strip()
    return match.group(1).strip(), rest


def split_embedded_question(
    question: str | None, context: str | None
) -> tuple[str | None, str | None, str | None]:
    """Recover a question that was packed into `context` instead of sent on its own.

    Returns (question, context, note). The note is non-empty only when
    something was moved, and is meant to be shown to the caller so the next
    call is made correctly. Nothing is guessed: a context with no question
    marker in it comes back untouched with no question, and the caller reports
    the shape error rather than paying for a prompt assembled on a hunch.
    """
    question = strip_call_syntax(question)
    if question and question.strip():
        return question.strip(), strip_call_syntax(context), None
    if not context or not context.strip():
        return None, strip_call_syntax(context), None

    # Search the raw context, before any unwrapping: a context that is nothing
    # but "<question>...</question>" would otherwise have its one marker
    # stripped off as a wrapper and become unrecoverable.
    for tag in ("question", "query", "ask", "prompt"):
        found = _extract_tagged(context, tag)
        if found:
            inner, rest = found
            return (
                strip_call_syntax(inner),
                strip_call_syntax(rest) or None,
                (f"`question` was empty and a <{tag}> block was found inside `context`; "
                "used that as the question. Send `question` as its own argument next time."),
            )

    # The input was checked non-empty above, so this cannot come back as None.
    stripped = strip_call_syntax(context) or ""
    heading = None
    for match in _QUESTION_HEADING.finditer(stripped):
        heading = match  # the last heading wins; earlier ones are background
    if heading and stripped[heading.end():].strip():
        return (
            strip_call_syntax(stripped[heading.end():]),
            strip_call_syntax(stripped[: heading.start()]) or None,
            ("`question` was empty and a 'Question' heading was found inside `context`; "
            "used the text under it as the question. Send `question` as its own "
            "argument next time."),
        )

    return None, stripped, None


MISSING_QUESTION = (
    "no question was given. `question` is a required top-level argument holding a "
    "plain string, separate from `context`. Send flat JSON, one value per argument:\n"
    '  {"question": "what you want answered", "context": "background the other '
    'model needs", "files": ["/abs/path/one.py"]}\n'
    "Do not wrap a value in XML tags such as <question> or <context>, and do not put "
    "the question text inside `context`."
)


def _setting(key: str, default: int) -> int:
    """An integer config value where 0 means 0.

    `cfg.get(key) or default` would read a deliberate 0 as "unset", which is
    exactly how someone turns one of these ceilings off.
    """
    value = load_config().get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        number = int(value)
        return number if number >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _tristate(value: Any) -> bool | None:
    """A real true/false, or None for "leave the decision to OpenRouter".

    A missing key, a null and a string all read as None rather than as False: sending
    `enabled: false` is itself a decision, and it turns off the compression an 8k endpoint
    would otherwise apply for you.
    """
    return value if isinstance(value, bool) else None


def override_allowed(key: str, requested: bool) -> tuple[bool, str | None]:
    """Whether a caller-supplied safety override may be honoured, and why not.

    The denylist and the cost guard exist because the calling agent can be talked into things.
    An agent that can set the override in the same call it was talked into is no guard at all,
    so for tool calls both are refused unless the config turns them on. The CLI flags are a
    person typing them deliberately and never come through here.

    Lives in core rather than in the MCP layer so it can be tested without the mcp SDK, which
    the offline suite does not have and must not need.
    """
    if not requested:
        return False, None
    if load_config().get(key) is True:
        return True, None
    return False, (
        f"`{key.removeprefix('mcp_')}` was requested but is not honoured for tool calls. Set "
        f'"{key}": true in {USER_CONFIG} to permit it, or run the orask CLI with the matching '
        "flag. This is deliberate: an agent that can be talked into asking for a secret, or "
        "for an expensive call, can be talked into passing the override alongside it in the "
        "same call."
    )


def _float_setting(key: str, default: float) -> float:
    """A float config value that cannot take a call down.

    Same contract as _setting: a deliberate 0 means 0, and a value that is not a number falls
    back to the default rather than raising. These are read on the paid path, and one of them
    is the cost guard itself, so a typo in a config file must not become a traceback.
    """
    value = load_config().get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _model_modalities(slug: str | None) -> set[str]:
    """What the model accepts as input. Empty means unknown, never "nothing"."""
    if not slug:
        return set()
    entry = _find(slug) or {}
    listed = (entry.get("architecture") or {}).get("input_modalities") or []
    return {str(item).lower() for item in listed}


def _gather_files(
    files: Iterable[str] | str | None,
    base: Path,
    allow_secret_files: bool,
    model_slug: str | None,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Turn the `files` argument into inline text sections and attachment parts.

    Source and prose are pasted in as fenced text, because that is what a model
    reasons over best. A PDF, image or audio file is attached to the message
    instead: transcribing one into the prompt is impossible for the caller and
    lossy where it is not.
    """
    cfg = load_config()
    notes: list[str] = []

    file_limit = _setting("max_file_chars", 200000)
    # Union by default. Replace semantics on a safety list is a footgun: adding
    # one project pattern would silently drop every credential pattern.
    deny = cfg.get("deny_file_patterns") or []
    if cfg.get("deny_file_patterns_replace") is True:
        patterns = list(deny)
    else:
        patterns = sorted(set(DEFAULT_DENY_PATTERNS) | set(deny))

    if not allow_secret_files:
        patterns.append(ENV_FILE.resolve().as_posix())
    candidates, walk_notes = _expand_paths(
        as_list(files), base, _setting("max_dir_files", 50),
        [] if allow_secret_files else patterns,
    )
    notes.extend(walk_notes)

    per_file = _setting("max_attachment_bytes", MAX_ATTACHMENT_BYTES)
    total_cap = _setting("max_attachment_total_bytes", MAX_ATTACHMENT_TOTAL_BYTES)
    ceiling = _setting("max_attachments", 20)
    modalities = _model_modalities(model_slug)

    sections: list[str] = []
    attachments: list[dict[str, Any]] = []
    manifest: list[str] = []
    spent = 0

    for candidate in candidates:
        if candidate.is_dir():
            notes.append(f"{candidate} is a directory, skipped")
            continue
        if not allow_secret_files:
            matched = denied_by_policy(candidate, patterns)
            if matched:
                notes.append(
                    f"REFUSED to send {candidate} to a third-party API: it matches the "
                    f"deny pattern '{matched}'. Pass allow_secret_files if this file is "
                    "genuinely not a secret, or edit deny_file_patterns in the config."
                )
                continue

        classified = classify_attachment(candidate)
        if classified is None:
            text, warning = _read_file(candidate, file_limit)
            if warning:
                notes.append(warning)
            if not text:
                continue
            fence = "```"
            while fence in text:
                fence += "`"
            sections.append(f"## File: {candidate}\n\n{fence}\n{text}\n{fence}")
            continue

        family, media = classified
        # A PDF is parsed by OpenRouter before it reaches the model, so it works
        # everywhere. An image or a sound file has to be something the model
        # itself takes, and sending one blind is a billed request that fails.
        if family in ("image", "audio") and modalities and family not in modalities:
            notes.append(
                f"{candidate} is {family} input, which {model_slug} does not accept "
                f"(it takes {', '.join(sorted(modalities))}); not sent. "
                "Use llm_model_info or list_llm_models to pick a model that does."
            )
            continue
        if len(attachments) >= ceiling:
            notes.append(
                f"{candidate} not attached: already at the {ceiling} attachment limit"
            )
            continue

        raw, problem = _slurp(candidate, per_file)
        if problem:
            notes.append(problem)
            continue
        if not raw:
            notes.append(f"{candidate} is empty")
            continue
        if spent + len(raw) > total_cap:
            notes.append(
                f"{candidate} ({_human_bytes(len(raw))}) not attached: it would take this "
                f"call past the {_human_bytes(total_cap)} total attachment ceiling"
            )
            continue
        spent += len(raw)
        blob = base64.b64encode(raw).decode("ascii")
        if family == "image":
            attachments.append(
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{blob}"}}
            )
        elif family == "audio":
            # Audio wants bare base64 plus a format field, not a data URI.
            attachments.append(
                {"type": "input_audio", "input_audio": {"data": blob, "format": media}}
            )
        else:
            attachments.append(
                {
                    "type": "file",
                    "file": {
                        "filename": candidate.name,
                        "file_data": f"data:{media};base64,{blob}",
                    },
                }
            )
        manifest.append(f"- {candidate} ({family}, {_human_bytes(len(raw))})")

    if manifest:
        sections.append(
            "## Attached files\n\nAttached to this message directly, in this order:"
            "\n\n" + "\n".join(manifest)
        )
    return sections, attachments, notes


def build_messages(
    question: str | None,
    context: str | None = None,
    files: Iterable[str] | str | None = None,
    system: str | None = None,
    role: str | None = None,
    cwd: str | None = None,
    history: list[dict[str, str]] | None = None,
    allow_secret_files: bool = False,
    model_slug: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Assemble the message list plus any notes worth showing the caller.

    The last user message is a plain string when everything went in as text,
    and a list of content parts when something was attached. `model_slug` is
    what decides whether an image or a sound file can go at all; leave it unset
    and nothing is filtered on modality.
    """
    cfg = load_config()
    notes: list[str] = []

    question, context, shape_note = split_embedded_question(question, context)
    if shape_note:
        notes.append(shape_note)
    if not question or not question.strip():
        raise OpenRouterError(MISSING_QUESTION)

    if system and system.strip():
        system_prompt = system.strip()
    else:
        roles = cfg.get("roles") or {}
        wanted = (role or cfg.get("default_role") or "advisor").strip().lower()
        if wanted not in roles and roles:
            fallback = "advisor" if "advisor" in roles else next(iter(roles))
            notes.append(
                f"role '{wanted}' is not defined ({', '.join(sorted(roles))}); "
                f"used '{fallback}'"
            )
            wanted = fallback
        system_prompt = roles.get(wanted) or _DEFAULTS_ADVISOR

    parts: list[str] = []
    if context and context.strip():
        parts.append("## Background from the agent asking\n\n" + context.strip())

    base = Path(cwd).expanduser() if cwd else Path.cwd()
    sections, attachments, file_notes = _gather_files(
        files, base, allow_secret_files, model_slug
    )
    parts.extend(sections)
    notes.extend(file_notes)

    parts.append("## Question\n\n" + question.strip())
    user_content = "\n\n".join(parts)

    # Attachments are deliberately outside this cap: they are governed by the
    # byte ceilings instead, because base64 inflates a perfectly ordinary
    # screenshot past any sensible character limit.
    cap = _setting("max_input_chars", 600000)
    if len(user_content) > cap:
        raise OpenRouterError(
            f"assembled prompt is {len(user_content)} chars, over the "
            f"max_input_chars limit of {cap}. Send fewer files, or raise the limit "
            "in the config if you mean to pay for it."
        )

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    replayed = 0
    for message in history or []:
        if message.get("role") in ("user", "assistant") and message.get("content"):
            turn: dict[str, Any] = {
                "role": message["role"], "content": message["content"]
            }
            # Sending a past turn's file annotations back is what tells
            # OpenRouter it has already parsed that PDF, so a long thread about
            # one document parses it once rather than once per question.
            if message["role"] == "assistant" and message.get("annotations"):
                turn["annotations"] = message["annotations"]
            if message["role"] == "user" and isinstance(message.get("content"), list):
                replayed += sum(
                    1 for part in message["content"]
                    if isinstance(part, dict) and part.get("type") != "text"
                )
            messages.append(turn)
    if replayed:
        notes.append(
            f"carried {replayed} earlier attachment(s) forward with their parse "
            "annotations, so the document is still in view and is not parsed again"
        )
    # Text first, then the attachments: providers parse a trailing image more
    # reliably than one that arrives before the instruction about it.
    if attachments:
        messages.append(
            {"role": "user", "content": [{"type": "text", "text": user_content}, *attachments]}
        )
    else:
        messages.append({"role": "user", "content": user_content})
    return messages, notes


def text_chars(messages: list[dict[str, Any]]) -> int:
    """Characters of real text in a message list, ignoring base64 attachments."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(part.get("text") or "")
    return total


def sent_attachments(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The attachment parts of the final user message, in the order sent."""
    if not messages:
        return []
    content = messages[-1].get("content")
    if not isinstance(content, list):
        return []
    return [
        part for part in content
        if isinstance(part, dict) and part.get("type") != "text"
    ]


def summarize_parts(parts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Count attachment parts and estimate what they will cost in tokens.

    The estimate exists to keep the per-call cost guard meaningful, and it is
    rough on purpose: real usage depends on how a provider tiles an image and
    how many pages a PDF turns out to hold.
    """
    counts = {"image": 0, "pdf": 0, "audio": 0}
    tokens = 0.0
    pdf_bytes = 0.0
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "image_url":
            counts["image"] += 1
            tokens += TOKENS_PER_IMAGE
        elif kind == "input_audio":
            counts["audio"] += 1
            blob = ((part.get("input_audio") or {}).get("data")) or ""
            tokens += len(blob) * 0.75 * TOKENS_PER_AUDIO_BYTE
        elif kind == "file":
            counts["pdf"] += 1
            blob = ((part.get("file") or {}).get("file_data")) or ""
            # base64 carries three bytes in every four characters
            pdf_bytes += len(blob) * 0.75
            tokens += len(blob) * 0.75 * TOKENS_PER_PDF_BYTE
    counts["tokens"] = int(tokens)
    counts["pdf_bytes"] = int(pdf_bytes)
    counts["total"] = counts["image"] + counts["pdf"] + counts["audio"]
    return counts


def attachment_summary(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything this request carries, replayed thread attachments included.

    All of it is re-sent on the wire and priced again, so the cost guard has to
    see the replayed parts as well as the new ones.
    """
    parts: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts.extend(p for p in content if isinstance(p, dict))
    return summarize_parts(parts)


_DEFAULTS_ADVISOR = (
    "You are a senior engineer giving a blunt, high-signal second opinion to another AI "
    "coding agent. You cannot see the repository and have no tools: reason only from what "
    "you are given. Be specific and concrete, lead with the answer, and call out risks the "
    "asker has probably not considered."
)


# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------


def fmt_usd(value: float) -> str:
    """Small amounts need more than two decimals to be readable."""
    return f"${value:,.2f}" if abs(value) >= 0.01 else f"${value:.4f}"


def _price(model: dict[str, Any], field: str) -> float:
    return _known_price(model, field) or 0.0


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _known_price(model: dict[str, Any], field: str) -> float | None:
    pricing = model.get("pricing")
    return _nonnegative_number(pricing.get(field)) if isinstance(pricing, dict) else None


def context_window(slug: str) -> int:
    """Tokens the model takes for prompt and answer together, 0 when nothing is published.

    top_provider is the endpoint a request actually lands on, so its window wins over the
    model-wide number when both are there.
    """
    model = _find(slug)
    top = model.get("top_provider") or {}
    try:
        return int(top.get("context_length") or model.get("context_length") or 0)
    except (TypeError, ValueError):
        return 0


def fit_context(
    slug: str,
    prompt_chars: int,
    max_tokens: int,
    requested_window: int | None = None,
    compress: bool = False,
) -> tuple[int, int, list[str]]:
    """Fit prompt and answer inside the context window, and refuse when they do not.

    Returns the output cap to send, the window it was fitted to (0 when the catalogue
    publishes none) and the notes worth reporting. OpenRouter has no request parameter that
    sets a context window - a model's is fixed - so `requested_window` can only budget below
    it, and anything above is clamped back down to what the model actually accepts.
    """
    notes: list[str] = []
    published = context_window(slug)
    window = published
    if requested_window:
        window = int(requested_window)
        if published and window > published:
            notes.append(
                f"max_context_tokens of {window} is more than {slug} accepts; used its "
                f"published window of {published}. A context window is a property of the "
                "model, not a request parameter, so it cannot be raised from here."
            )
            window = published
        elif published:
            notes.append(
                f"context budgeted to {window} tokens of the {published} {slug} allows"
            )
    if window <= 0:
        return max_tokens, 0, notes

    prompt_tokens = int(prompt_chars / CHARS_PER_TOKEN) + 1
    room = window - prompt_tokens
    if room < MIN_ANSWER_TOKENS:
        if compress:
            # OpenRouter drops from the middle until the prompt fits, which it can only do
            # if the answer is not itself claiming the whole window.
            budget = min(max_tokens, max(MIN_ANSWER_TOKENS, window // 2))
            notes.append(
                f"the prompt is about {prompt_tokens} tokens against a {window} token "
                f"window, so OpenRouter's context-compression plugin will drop text from "
                f"the middle before the model reads it; the answer is capped at {budget} "
                "tokens to leave room for what survives"
            )
            return budget, window, notes
        raise OpenRouterError(
            f"refusing to send: the prompt is about {prompt_tokens} tokens and {slug} takes "
            f"{window} for prompt and answer together, so there is no room left to reply. "
            "Send fewer files, pick a model with a larger context window (list_llm_models "
            "shows it), raise max_context_tokens if you set it below the model's own, or "
            "pass context_compression=true to let OpenRouter drop text from the middle of "
            "the prompt until it fits."
        )
    if max_tokens > room:
        notes.append(
            f"lowered max_tokens from {max_tokens} to {room}: the prompt is about "
            f"{prompt_tokens} tokens of the {window} token context window and the answer "
            "has to fit in what is left"
        )
        max_tokens = room
    return max_tokens, window, notes


def estimate_call_cost(slug: str, chars: int, max_tokens: int | None) -> tuple[float, bool]:
    """Worst-case cost of a call: whole prompt in, max_tokens out.

    Returns (usd, priced). `priced` is False when the model is not in the
    catalogue, so the caller can say the guard could not be evaluated instead
    of treating an unknown price as free.
    """
    model = _find(slug)
    if not model:
        return 0.0, False
    prompt_price = _known_price(model, "prompt")
    completion_price = _known_price(model, "completion")
    if prompt_price is None or completion_price is None:
        return 0.0, False
    request_price = _known_price(model, "request")
    if "request" in (model.get("pricing") or {}) and request_price is None:
        return 0.0, False
    prompt = (chars / CHARS_PER_TOKEN) * prompt_price
    output = float(max_tokens or 0) * completion_price
    return prompt + output + (request_price or 0.0), True


def estimate_input_cost(slug: str, chars: int) -> float:
    """Prompt-side cost only (kept for callers that just want the input side)."""
    return estimate_call_cost(slug, chars, 0)[0]


def _clean_usage(raw: Any, notes: list[str]) -> dict[str, Any]:
    """Optional provider accounting cannot invalidate a useful paid answer."""
    out: dict[str, Any] = {}
    invalid = not isinstance(raw, dict) or not raw
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        if key in ("prompt_tokens", "completion_tokens", "cost", "total_cost"):
            number = _nonnegative_number(value)
            if number is None:
                invalid = True
            else:
                out[key] = int(number) if key.endswith("_tokens") else number
        elif key in ("prompt_tokens_details", "completion_tokens_details"):
            if not isinstance(value, dict):
                invalid = True
                continue
            out[key] = {}
            for field in ("reasoning_tokens", "audio_tokens", "video_tokens", "cached_tokens"):
                if field in value:
                    number = _nonnegative_number(value[field])
                    if number is None:
                        invalid = True
                    else:
                        out[key][field] = int(number)
    if invalid:
        notes.append("provider usage was missing or malformed; valid fields were kept, "
                     "but a zero fallback cost does not confirm a free call")
    return out


def actual_cost(slug: str, usage: dict[str, Any]) -> float:
    """Prefer OpenRouter's own cost; fall back to catalogue pricing."""
    for key in ("cost", "total_cost"):
        value = usage.get(key)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            return float(value)
    model = _find(slug)
    if not model:
        return 0.0
    prompt = _nonnegative_number(usage.get("prompt_tokens")) or 0
    completion = _nonnegative_number(usage.get("completion_tokens")) or 0
    return (prompt * _price(model, "prompt") + completion * _price(model, "completion")
            + _price(model, "request"))


# --------------------------------------------------------------------------
# threads
# --------------------------------------------------------------------------


def _thread_path(name: str) -> Path:
    """Filename for a thread.

    Sanitising alone collides: "plan a" and "plan-a!" flatten to the same stem,
    as do two names differing only past the length cut. A short digest of the
    raw name keeps distinct threads in distinct files.
    """
    raw = name.strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:60].strip("-") or "thread"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    path = THREAD_DIR / f"{safe}-{digest}.json"
    if not path.exists():
        # Keep using a transcript written before the digest was introduced.
        legacy = THREAD_DIR / f"{safe}.json"
        if legacy.is_file():
            return legacy
    return path


def usable_turns(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Turns fit to replay, with null content stripped out.

    A transcript written before ask() kept the recovered question can hold a null content
    string, or a text part whose text is null. Replaying either sends JSON null to the
    provider. Dropping them here means an existing thread repairs itself the first time it is
    read, with no migration and no change to the stored format.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                part for part in content
                if isinstance(part, dict)
                and (part.get("type") != "text" or isinstance(part.get("text"), str))
            ]
            if not parts:
                continue
            out.append(dict(message, content=parts))
        elif isinstance(content, str) and content:
            out.append(message)
    return out


def _read_thread_blob(path: Path) -> dict[str, Any] | None:
    raw, error = _slurp(path, MAX_FILE_BYTES)
    if error:
        return None
    try:
        blob = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(blob, dict) or not isinstance(blob.get("messages"), list):
        return None
    return blob


def load_thread(name: str | None) -> list[dict[str, Any]]:
    if not name:
        return []
    blob = _read_thread_blob(_thread_path(name))
    return usable_turns(blob["messages"]) if blob else []


def _lock_exclusive(fd: int) -> None:
    """Bound contention without proceeding unlocked or losing ordinary concurrent writes."""
    deadline = time.monotonic() + 10.0
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("state-file lock remained busy for 10 seconds") from None
            time.sleep(0.02)


def save_thread(
    name: str | None,
    question: str,
    answer: str,
    slug: str,
    annotations: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    """Persist a thread turn. Returns False if it could not be written.

    Read-modify-write under an exclusive lock: without it two concurrent turns
    on the same thread both load the same history and the second write silently
    discards the first exchange.
    """
    if not name:
        return True
    path = _thread_path(name)
    lock_fd = None
    try:
        THREAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = _open_regular_fd(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT)
        # A stalled writer must not hold a paid response indefinitely. Ordinary concurrent
        # turns wait briefly for the same stable lock inode, then merge the latest history.
        _lock_exclusive(lock_fd)
        return _save_thread_locked(path, name, question, answer, slug, annotations, attachments)
    except Exception:
        # Persistence follows billing. Return failure so ask() keeps the paid answer and
        # reports that it was not saved; never continue a read-modify-write without a lock.
        return False
    finally:
        if lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(lock_fd)  # closing releases the lock, including on failure


def _save_thread_locked(
    path: Path,
    name: str,
    question: str,
    answer: str,
    slug: str,
    annotations: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    blob = _read_thread_blob(path)
    if blob is None and (path.exists() or path.is_symlink()):
        return False  # preserve an unreadable/corrupt transcript for recovery
    history = usable_turns(blob["messages"]) if blob else []
    # The attachments ride on the user turn, which is where they were sent, so
    # a follow-up still has the document in front of it. Annotations alone do
    # not carry content: they only tell OpenRouter it has already parsed this
    # file, so it can skip the parse and its cost.
    if attachments:
        history.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": question}, *attachments],
            }
        )
    else:
        history.append({"role": "user", "content": question})
    turn: dict[str, Any] = {"role": "assistant", "content": answer, "model": slug}
    if annotations:
        turn["annotations"] = annotations
    history.append(turn)
    keep = _setting("thread_max_messages", 20)
    if keep > 0:
        history = history[-keep:]
    return _write_json_atomic(
        path,
        {"name": name, "updated_at": time.time(), "messages": history},
        indent=1,
    )


def list_threads() -> list[dict[str, Any]]:
    if not THREAD_DIR.is_dir():
        return []
    out = []
    for path in sorted(THREAD_DIR.glob("*.json")):
        blob = _read_thread_blob(path)
        if blob is None:
            continue
        out.append(
            {
                "name": path.stem,
                "messages": len(blob.get("messages") or []),
                "updated_at": blob.get("updated_at"),
            }
        )
    return out


# --------------------------------------------------------------------------
# call log
# --------------------------------------------------------------------------


def log_call(entry: dict[str, Any]) -> None:
    try:
        CALL_LOG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = _open_regular_fd(CALL_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            _lock_exclusive(handle.fileno())
            handle.write(json.dumps({"ts": time.time(), **entry}) + "\n")
    except (OSError, ValueError, TypeError):
        pass  # metadata logging must not destroy a billed answer


# The log is append-only and never rotated, so reads are bounded: `orask usage` asks for a
# hundred thousand entries, which at 512 bytes apiece would be a 51 MB read on every call.
MAX_LOG_WINDOW_BYTES = 4 * 1024 * 1024


def read_log(limit: int = 50) -> list[dict[str, Any]]:
    """Recent object records, bounded to the last 4 MiB even for a huge log."""
    if limit <= 0:
        return []
    try:
        fd = _open_regular_fd(CALL_LOG)
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            window = min(size, max(int(limit) * 512, 65536), MAX_LOG_WINDOW_BYTES)
            while True:
                handle.seek(size - window)
                raw = handle.read(window)
                if window < size:
                    raw = raw.partition(b"\n")[2]
                lines = raw.decode("utf-8", "replace").splitlines()
                out = []
                for line in lines:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            out.append(record)
                    except ValueError:
                        continue
                if len(out) >= limit or window >= min(size, MAX_LOG_WINDOW_BYTES):
                    return out[-limit:]
                window = min(size, window * 2, MAX_LOG_WINDOW_BYTES)
    except OSError:
        return []


# --------------------------------------------------------------------------
# the main event
# --------------------------------------------------------------------------


def ask(
    question: str | None,
    model: str | None = None,
    category: str | None = None,
    context: str | None = None,
    files: Iterable[str] | str | None = None,
    effort: str | None = None,
    role: str | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    max_context_tokens: int | None = None,
    context_compression: bool | None = None,
    temperature: float | None = None,
    thread: str | None = None,
    cwd: str | None = None,
    pdf_engine: str | None = None,
    allow_expensive: bool = False,
    allow_secret_files: bool = False,
    include_reasoning: bool = False,
    effort_reason: str | None = None,
    _mcp_call: bool = False,
) -> dict[str, Any]:
    """Ask one model and return a structured result."""
    cfg = load_config()
    started = time.monotonic()

    if _mcp_call:
        effort = (effort or "max").strip().lower()
        if effort not in {"max", "xhigh", "medium"}:
            raise OpenRouterError(
                "MCP calls require max or xhigh effort, or medium with effort_reason. "
                "Low, minimal and disabled reasoning are not permitted."
            )
        if effort == "medium" and not (effort_reason and effort_reason.strip()):
            raise OpenRouterError("MCP medium effort requires a non-empty effort_reason.")
    notes: list[str] = []
    # Recover a misplaced question here rather than only inside build_messages, so the question
    # that reaches the thread transcript is the real one. Storing the untouched argument wrote
    # a null user turn, and with an attachment on the same turn it put a null text part on the
    # wire. The split is idempotent, so build_messages re-running it changes nothing.
    question, context, shape_note = split_embedded_question(question, context)
    if shape_note:
        notes.append(shape_note)
    if not question or not question.strip():
        raise OpenRouterError(MISSING_QUESTION)
    question = question.strip()

    if model and category:
        # An explicit model is a deliberate choice; the category is the looser
        # of the two requests, so it loses rather than silently overriding.
        notes.append(
            f"both model='{model}' and category='{category}' were given; used the "
            "explicit model and ignored the category."
        )
        category = None

    if category:
        picks, cat_notes = category_models(category)
        notes.extend(cat_notes)
        # category_models has already refused an unknown category, so this always matches.
        matched = resolve_category(category)
        name = matched[0] if matched else category
        slug, resolve_note = resolve_model(picks[0])
        notes.append(f"category '{name}' -> {slug}")
    else:
        slug, resolve_note = resolve_model(model or cfg.get("default_model") or "kimi")
    if resolve_note:
        notes.append(resolve_note)

    history = load_thread(thread)
    messages, build_notes = build_messages(
        question, context=context, files=files, system=system, role=role,
        cwd=cwd, history=history, allow_secret_files=allow_secret_files,
        model_slug=slug,
    )
    notes.extend(build_notes)
    if history:
        notes.append(f"continuing thread '{thread}' with {len(history)} prior messages")

    chars = text_chars(messages)
    attached = attachment_summary(messages)
    fresh = summarize_parts(sent_attachments(messages))
    if fresh["total"]:
        notes.append(
            f"attached {fresh['total']} file(s) to the message rather than pasting "
            f"them in as text ({fresh['image']} image, {fresh['pdf']} pdf, "
            f"{fresh['audio']} audio); their share of the cost estimate is a "
            "heuristic, since OpenRouter has no preflight token count"
        )
    # Fold the attachments into the same character-based estimate the cost guard
    # reads, so a folder of screenshots cannot walk past it as "almost no text".
    billable = chars + int(attached["tokens"] * CHARS_PER_TOKEN)

    if max_tokens is not None and int(max_tokens) < 1:
        # 0 would read as "no cap": no max_tokens sent and zero output priced,
        # so the provider's own ceiling applies against a $0.00 estimate.
        raise OpenRouterError(
            "max_tokens must be 1 or more; omit it to use the configured default"
        )
    limit = int(max_tokens) if max_tokens is not None else _setting("default_max_tokens", 32000)
    ceiling = int(((_find(slug).get("top_provider") or {}).get("max_completion_tokens")) or 0)
    if limit and ceiling and limit > ceiling:
        notes.append(f"max_tokens={limit} exceeds {slug}'s output ceiling; using {ceiling}.")
        limit = ceiling
    if not limit:
        # Neither the config nor the catalogue gave a cap. Sending no max_tokens would price
        # zero output against the guard while the provider generates to its own ceiling, so
        # the packaged default stands in and is actually sent.
        limit = ceiling or int(_DEFAULTS["default_max_tokens"])
        notes.append(
            f"no output cap was configured and {slug} publishes none, so {limit} was used: "
            "without it the cost guard would price the answer at zero. Set default_max_tokens "
            "or pass max_tokens to choose your own."
        )

    wanted_window = (
        int(max_context_tokens) if max_context_tokens is not None
        else _setting("max_context_tokens", 0)
    )
    if wanted_window and wanted_window < MIN_ANSWER_TOKENS:
        raise OpenRouterError(
            f"max_context_tokens must be at least {MIN_ANSWER_TOKENS}; omit it to use the "
            "model's own context window"
        )
    compress = (
        context_compression if context_compression is not None
        else _tristate(cfg.get("context_compression"))
    )
    # Ahead of the cost guard, so the answer is priced at the cap that is actually sent.
    limit, window, fit_notes = fit_context(
        slug, billable, limit, wanted_window, compress is True
    )
    notes.extend(fit_notes)

    # Resolved before the guard runs, because which engine reads the PDF changes what the
    # call costs. cloudflare-ai is free, mistral-ocr is not.
    engine = str(pdf_engine or cfg.get("pdf_engine") or "cloudflare-ai").strip().lower()
    if engine not in PDF_ENGINES:
        notes.append(
            f"pdf_engine '{engine}' is not one of {', '.join(PDF_ENGINES)}; used cloudflare-ai"
        )
        engine = "cloudflare-ai"

    guard = _float_setting("max_cost_usd_per_call", 1.0)
    estimate, priced = estimate_call_cost(slug, billable, limit)

    ocr = 0.0
    pdf_bytes = int(attached.get("pdf_bytes") or 0)
    if engine == "mistral-ocr" and pdf_bytes:
        rate = _float_setting("mistral_ocr_usd_per_1k_pages", MISTRAL_OCR_USD_PER_1K_PAGES)
        pages = max(1, (pdf_bytes + PDF_BYTES_PER_PAGE - 1) // PDF_BYTES_PER_PAGE)
        ocr = (pages / 1000.0) * rate
        estimate += ocr
        notes.append(
            f"mistral-ocr bills per page on top of tokens. About {pages} page(s) estimated "
            f"from {_human_bytes(pdf_bytes)} of PDF, roughly {fmt_usd(ocr)}, which is included "
            "in the cost guard. That page count is inferred from the file size, not from "
            "parsing the document, so treat it as an upper bound rather than an invoice."
        )
    if guard and not allow_expensive:
        if not priced:
            policy = str(cfg.get("cost_guard_on_unknown_pricing") or "warn").lower()
            if policy == "block":
                raise OpenRouterError(
                    f"refusing to send: no catalogue pricing for {slug}, so the "
                    f"{fmt_usd(guard)} per-call cost guard cannot be checked, and "
                    "cost_guard_on_unknown_pricing is 'block'. Pass allow_expensive "
                    "to send anyway."
                )
            if ocr > guard:
                # The token side is unknowable, but the page charge is not, and it alone is
                # already over the line.
                raise OpenRouterError(
                    f"refusing to send: the mistral-ocr page charge alone is about "
                    f"{fmt_usd(ocr)}, over the {fmt_usd(guard)} per-call guard. Use "
                    "pdf_engine='cloudflare-ai' if the PDF has real text in it, send fewer "
                    "pages, or pass allow_expensive to override."
                )
            notes.append(
                f"no catalogue pricing for {slug}, so the {fmt_usd(guard)} per-call "
                "cost guard could not be checked (cost_guard_on_unknown_pricing="
                f"'{policy}')"
            )
        elif estimate > guard:
            raise OpenRouterError(
                f"refusing to send: worst-case cost for {slug} is about "
                f"{fmt_usd(estimate)} ({billable} chars in, up to {limit} tokens out"
                + (f", plus about {fmt_usd(ocr)} of mistral-ocr page charges" if ocr else "")
                + f"), over the {fmt_usd(guard)} per-call guard. Trim the context, lower "
                "max_tokens, "
                + ("use pdf_engine='cloudflare-ai' if the PDF has real text in it, "
                   if ocr else "")
                + "or pass allow_expensive to override."
            )

    payload: dict[str, Any] = {
        "model": slug,
        "messages": messages,
        "usage": {"include": True},
    }

    wanted_effort = effort if effort is not None else cfg.get("default_effort")
    final_effort, effort_note = clamp_effort(slug, wanted_effort)
    if _mcp_call:
        declared = ((_find(slug).get("reasoning") or {}).get("supported_efforts") or [])
        if (not declared or final_effort not in EFFORT_LADDER
                or EFFORT_LADDER.index(final_effort) < EFFORT_LADDER.index("medium")):
            raise OpenRouterError(
                f"{slug} cannot satisfy the MCP reasoning policy with its published efforts. "
                "Choose a model advertising medium or stronger reasoning; low is never used."
            )
        if effort == "medium":
            notes.append(f"medium effort requested because: {(effort_reason or '').strip()}")
    if effort_note:
        notes.append(effort_note)
    if final_effort:
        payload["reasoning"] = {"effort": final_effort}

    if limit:
        payload["max_tokens"] = int(limit)
    if temperature is not None:
        payload["temperature"] = float(temperature)

    plugins: list[dict[str, Any]] = []
    if fresh["pdf"] or attached["pdf"]:
        # OpenRouter parses the PDF before the model sees it, which is why a PDF
        # attaches to any model at all. The engine was resolved above, since it
        # changes what the call costs.
        plugins.append({"id": "file-parser", "pdf": {"engine": engine}})
        if fresh["pdf"]:
            notes.append(
                f"PDF parsed by '{engine}'"
                + (
                    " which bills separately per 1,000 pages"
                    if engine == "mistral-ocr"
                    else "; pass pdf_engine='mistral-ocr' if the PDF is a scan that "
                         "needs OCR"
                )
            )
    if compress is not None:
        # Only sent when someone actually decided: left out, an endpoint under 8k keeps the
        # compression OpenRouter applies to it by default, and a larger one keeps refusing an
        # overflow instead of quietly answering from a prompt with a hole in the middle.
        plugins.append({"id": "context-compression", "enabled": bool(compress)})
    if plugins:
        payload["plugins"] = plugins

    timeout = _float_setting("request_timeout_s", 300.0) or 300.0
    response: dict[str, Any] = {}
    try:
        response = _request("POST", "/chat/completions", payload, timeout=timeout)
        # OpenRouter can answer 200 and put the failure in the body. Route it through the same
        # translator as an HTTP error: otherwise the caller gets "returned no choices" plus a
        # raw dump, and none of the typed-error guidance that exists for exactly this.
        embedded = response.get("error")
        if embedded is not None:
            code = _nonnegative_number(embedded.get("code")) if isinstance(embedded, dict) else None
            raise OpenRouterError(_http_message(int(code or 200), json.dumps({"error": embedded})))
    except OpenRouterError as exc:
        log_call(
            {
                "model": slug, "requested": model, "ok": False,
                "error": str(exc)[:500], "chars_in": chars,
                "cost_usd": actual_cost(slug, _clean_usage(response.get("usage"), []))
                if response.get("usage") else 0.0,
                "latency_s": round(time.monotonic() - started, 2), "thread": thread,
            }
        )
        raise

    choices = response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    answer = content.strip() if isinstance(content, str) else ""
    annotations = message.get("annotations")
    if not isinstance(annotations, list) or any(not isinstance(a, dict) for a in annotations):
        annotations = None
    raw_reasoning = message.get("reasoning")
    reasoning = raw_reasoning.strip() if isinstance(raw_reasoning, str) else ""
    finish = choice.get("finish_reason") or choice.get("native_finish_reason")
    finish = finish if isinstance(finish, str) else None

    empty = not answer
    incomplete = empty or (finish is not None and finish != "stop")
    if incomplete and finish == "length":
        notes.append(
            f"incomplete response (finish_reason={finish}, max_tokens={limit}): "
            "max_tokens covers reasoning plus the final answer; a larger context window "
            "alone does not increase that output allowance. Inspect usage.reasoning_tokens "
            "and llm_model_info, then choose a larger max_tokens within the model and cost "
            "limits, or narrow the task. MCP calls require max/xhigh, or justified medium. "
            "Do not retry unchanged or treat partial analysis as a completed review."
        )
    elif incomplete:
        notes.append(
            f"incomplete response (finish_reason={finish}, max_tokens={limit}); "
            "inspect the response status before deciding how to recover. A missing, "
            "malformed or filtered answer is not evidence that more output tokens will help."
        )
    if empty:
        notes.append("the model returned no final answer; the call may still be billed")

    usage = _clean_usage(response.get("usage"), notes)
    prompt_detail = usage.get("prompt_tokens_details") or {}
    cost = actual_cost(slug, usage)
    elapsed = round(time.monotonic() - started, 2)

    carried: list[dict[str, Any]] | None = None
    if thread and fresh["total"]:
        parts = sent_attachments(messages)
        # Base64 in a transcript adds up fast, so only a thread's worth of it is
        # kept. Past that the caller is told to pass the files again rather than
        # left with a follow-up the model cannot see the document for.
        budget = _setting("thread_attachment_bytes", 4 * 1024 * 1024)
        weight = sum(len(json.dumps(part)) for part in parts)
        if weight <= budget:
            carried = parts
            notes.append(
                f"kept {len(parts)} attachment(s) on thread '{thread}', so a follow-up "
                "still sees them without you sending them again"
            )
        else:
            notes.append(
                f"the attachments are {_human_bytes(weight)}, over the "
                f"{_human_bytes(budget)} a thread will carry; pass the same files "
                "again on the next turn, or raise thread_attachment_bytes"
            )
    elif fresh["total"] and annotations and not thread:
        notes.append(
            "OpenRouter parsed the attached file for this call. Pass a `thread` name "
            "to keep following up on it without sending or parsing it again."
        )
    if thread and not incomplete and not save_thread(
        thread, question, answer, slug, annotations, carried
    ):
        notes.append(
            f"could not write the thread transcript to {THREAD_DIR}; this answer "
            "will not be part of the next follow-up"
        )

    log_call(
        {
            "model": slug, "requested": model, "ok": not incomplete, "empty": empty,
            "incomplete": incomplete, "finish_reason": finish,
            "effort": final_effort, "role": role or cfg.get("default_role"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
            "cost_usd": round(cost, 6), "latency_s": elapsed,
            "chars_in": chars, "attachments": attached["total"], "thread": thread,
        }
    )

    return {
        # Only a final answer that finished normally counts as a completed opinion.
        "ok": not incomplete,
        "incomplete": incomplete,
        "error": (
            f"{slug} returned an incomplete response (finish_reason={finish})"
            if incomplete else None
        ),
        "model": slug,
        "requested": model,
        "answer": answer,
        "reasoning": reasoning if include_reasoning else "",
        "effort": final_effort,
        "finish_reason": finish,
        "provider": response.get("provider"),
        # What the prompt and answer were actually fitted into, so a caller that set a
        # budget can see what it got, and one that did not can see the model's own.
        "context_window": window,
        "max_tokens": limit,
        "usage": {
            # prompt_tokens already includes images, audio and video; the detail
            # block is what says how much of it they were.
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
            "audio_tokens": prompt_detail.get("audio_tokens"),
            "video_tokens": prompt_detail.get("video_tokens"),
            "cached_tokens": prompt_detail.get("cached_tokens"),
            "cost_usd": round(cost, 6),
        },
        "latency_s": elapsed,
        "thread": thread,
        "notes": notes,
    }


def ask_panel(
    question: str | None,
    models: Iterable[str] | str | None = None,
    max_workers: int = 6,
    category: str | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Ask several models the same question in parallel.

    One model failing never takes the panel down: its slot comes back as an
    error entry so the caller still sees every other opinion.

    A category supplies the panel when no explicit models are given. Each
    category pairs two different vendors on purpose, so the panel is two
    independent houses rather than one lab asked twice.
    """
    cfg = load_config()
    panel_notes: list[str] = []
    wanted = as_list(models)
    if not wanted and category:
        wanted, panel_notes = category_models(category)
    wanted = wanted or as_list(cfg.get("default_panel"))
    if not wanted:
        wanted = [cfg.get("default_model") or "kimi"]

    # Deduplicated on the resolved slug, not on the spelling: "kimi" and its full slug are one
    # model and one opinion, but two bills. A spec that will not resolve keeps its own slot so
    # ask() can report it there, because one bad model must never take the panel down.
    seen: list[str] = []
    already: set[str] = set()
    for entry in wanted:
        try:
            slug, _ = resolve_model(entry)
        except OpenRouterError:
            slug = entry.strip().lower()
        if slug in already:
            panel_notes.append(
                f"'{entry}' resolves to {slug}, which is already on the panel; asked once"
            )
            continue
        already.add(slug)
        seen.append(entry)

    if kwargs.pop("thread", None):
        # Silently dropping it would leave the caller believing the panel was
        # continuing a conversation.
        raise OpenRouterError(
            "thread is not supported for a panel: several models writing one "
            "transcript would interleave. Use ask() per model with its own thread."
        )

    results: list[dict[str, Any]] = [{} for _ in seen]
    workers = max(1, min(int(max_workers), len(seen)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(ask, question, model=spec, **kwargs): index
            for index, spec in enumerate(seen)
        }
        # `category` chose the roster above; each member is now an explicit
        # model, so it must not be passed down again as a second request.
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            spec = seen[index]
            try:
                results[index] = future.result()
            except Exception as exc:
                # Reported in this model's own slot and never re-raised: one model failing
                # must not lose the answers the others paid for.
                results[index] = {
                    "ok": False,
                    "requested": spec,
                    "model": spec,
                    "error": str(exc),
                    "notes": [],
                }
    for note in panel_notes:
        if results:
            results[0].setdefault("notes", []).insert(0, note)
    return results


# --------------------------------------------------------------------------
# discovery helpers
# --------------------------------------------------------------------------


def list_models(
    search: str | None = None,
    vendor: str | None = None,
    limit: int = 25,
    include_batch: bool = False,
    sort: str = "intelligence",
) -> list[dict[str, Any]]:
    """Search the live catalogue so any of OpenRouter's models is reachable."""
    catalog = get_catalog()
    needle = (search or "").strip().lower()
    vendor_needle = (vendor or "").strip().lower().rstrip("/")

    rows: list[dict[str, Any]] = []
    for model in catalog:
        slug = model.get("id") or ""
        low = slug.lower()
        if not include_batch and ":batch" in low:
            continue
        if vendor_needle and not low.lstrip("~").startswith(vendor_needle + "/"):
            continue
        if needle and needle not in low and needle not in (model.get("name") or "").lower():
            continue
        reasoning = model.get("reasoning") or {}
        rows.append(
            {
                "slug": slug,
                "name": model.get("name"),
                "context": model.get("context_length"),
                "intelligence_index": _intelligence(model) if _intelligence(model) >= 0 else None,
                "usd_per_m_input": round(_price(model, "prompt") * 1_000_000, 3),
                "usd_per_m_output": round(_price(model, "completion") * 1_000_000, 3),
                "reasoning_efforts": reasoning.get("supported_efforts") or [],
                "modalities": (model.get("architecture") or {}).get("input_modalities") or [],
            }
        )

    if sort == "context":
        rows.sort(key=lambda r: r["context"] or 0, reverse=True)
    elif sort == "price":
        rows.sort(key=lambda r: r["usd_per_m_input"])
    elif sort == "name":
        rows.sort(key=lambda r: r["slug"])
    else:
        rows.sort(
            key=lambda r: (r["intelligence_index"] if r["intelligence_index"] is not None else -1),
            reverse=True,
        )
    return rows[: max(1, int(limit))]


def model_info(spec: str) -> dict[str, Any]:
    slug, note = resolve_model(spec)
    model = _find(slug)
    if not model:
        raise OpenRouterError(f"no catalogue entry for '{slug}'")
    reasoning = model.get("reasoning") or {}
    top = model.get("top_provider") or {}
    bench = (model.get("benchmarks") or {}).get("artificial_analysis") or {}
    return {
        "slug": slug,
        "resolved_from": spec,
        "note": note,
        "name": model.get("name"),
        "description": (model.get("description") or "")[:1200],
        "context_length": model.get("context_length"),
        "max_output_tokens": top.get("max_completion_tokens"),
        "bridge_limits": {
            "default_max_tokens": _setting("default_max_tokens", 32000),
            "default_effort": load_config().get("default_effort"),
            "mcp_default_effort": "max",
            "mcp_medium_requires_reason": True,
            "max_context_tokens": _setting("max_context_tokens", 0),
            "max_cost_usd_per_call": _float_setting("max_cost_usd_per_call", 1.0),
        },
        "moderated": top.get("is_moderated"),
        "usd_per_m_input": round(_price(model, "prompt") * 1_000_000, 3),
        "usd_per_m_output": round(_price(model, "completion") * 1_000_000, 3),
        "usd_per_m_cache_read": round(_price(model, "input_cache_read") * 1_000_000, 3),
        "reasoning_efforts": reasoning.get("supported_efforts") or [],
        "reasoning_default": reasoning.get("default_effort"),
        "reasoning_mandatory": reasoning.get("mandatory"),
        "modalities": (model.get("architecture") or {}).get("input_modalities") or [],
        "intelligence_index": bench.get("intelligence_index"),
        "coding_index": bench.get("coding_index"),
        "agentic_index": bench.get("agentic_index"),
    }


def account_usage() -> dict[str, Any]:
    data = _request("GET", "/key", timeout=30.0, retries=2).get("data") or {}
    entries = read_log(limit=100000)

    def _num(entry: dict[str, Any], key: str) -> float:
        try:
            return float(entry.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    # Summed over every entry, not only the ones that answered: an empty completion logs
    # ok: False and is still billed, so filtering on ok under-reports real spend.
    spend = sum(_num(e, "cost_usd") for e in entries)
    day_cutoff = time.time() - 86400
    spend_day = sum(
        _num(e, "cost_usd") for e in entries if _num(e, "ts") >= day_cutoff
    )
    return {
        "key_label": data.get("label"),
        "account_usage_usd": data.get("usage"),
        "credit_limit_usd": data.get("limit"),
        "credit_remaining_usd": data.get("limit_remaining"),
        "free_tier": data.get("is_free_tier"),
        "bridge_calls_logged": len([e for e in entries if e.get("ok")]),
        "bridge_calls_billed": len([e for e in entries if _num(e, "cost_usd") > 0]),
        "bridge_spend_usd": round(spend, 4),
        "bridge_spend_last_24h_usd": round(spend_day, 4),
        # Said out loud, because the read is bounded: a log past the window would otherwise
        # report a total that quietly stops being the whole story.
        "bridge_spend_covers": (
            f"the most recent {MAX_LOG_WINDOW_BYTES // (1024 * 1024)} MB of the call log"
            if CALL_LOG.is_file() and CALL_LOG.stat().st_size > MAX_LOG_WINDOW_BYTES
            else "every logged call"
        ),
        "log_file": str(CALL_LOG),
    }


# --------------------------------------------------------------------------
# Guides: local best-practice cheat sheets the calling agent can pull in
# --------------------------------------------------------------------------
# Plain markdown, read from disk and returned verbatim. No model is called and
# nothing is billed. They are served in widths - index, outline, section, whole -
# because a reference manual runs to thousands of lines and handing one over
# whole costs more context than the answer is worth.
#
# Every read goes through _guide_map(), which is the only place a path enters
# this subsystem. It resolves each candidate and confirms it sits inside the
# directory it was found in, so a symlink planted in a guide directory cannot
# read an unrelated file through list, search or read.

GUIDES_DIR = PROJECT_ROOT / "guides"
MAX_GUIDE_BYTES = 512 * 1024
MAX_GUIDE_SECTION_CHARS = 60_000
FRONT_MATTER_BYTES = 4096
MAX_INDEXED_GUIDES = 64
_GUIDE_TOPIC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GUIDE_HEADING = re.compile(r"^(#{2,4})\s+(.+?)\s*$")
_GUIDE_FENCE = re.compile(r"^\s*(```+|~~~+)")
_GUIDE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The closing delimiter must be a line of its own, so a document opening with a
# horizontal rule does not have everything up to its next rule eaten as metadata.
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---+[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def guide_dirs() -> list[Path]:
    """Extra directories from config, then the packaged guides.

    A directory named in `guide_dirs` wins over the packaged copy of the same
    topic: it is opt-in user configuration, so a local `python.md` is meant to
    replace the shipped one rather than be silently ignored. `orask guide
    <topic>` prints the winning path.

    The value is not comma-split the way `files` is, because a directory name
    may legitimately contain a comma. Give a JSON list, or one path.
    """
    raw = load_config().get("guide_dirs")
    entries = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) and raw else [])
    dirs: list[Path] = []
    for entry in entries:
        try:
            path = Path(str(entry)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if path.is_dir() and path not in dirs:
            dirs.append(path)
    packaged = GUIDES_DIR.resolve() if GUIDES_DIR.is_dir() else GUIDES_DIR
    if packaged not in dirs:
        dirs.append(packaged)
    return dirs


def _guide_slug(stem: str) -> str:
    """Filename stem to the name the tool is called with.

    The stem is canonical, never the front matter `topic`: a name that is
    displayed but cannot be looked up sends the agent to an error that lists the
    string it just refused.
    """
    slug = re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-.")
    return slug if _GUIDE_TOPIC.match(slug) else ""


def _guide_map() -> dict[str, Path]:
    """Every readable guide as slug -> resolved path. The one path gate."""
    found: dict[str, Path] = {}
    for directory in guide_dirs():
        try:
            root = directory.resolve()
            candidates = sorted(directory.glob("*.md"))
        except (OSError, RuntimeError):
            continue
        for path in candidates:
            slug = _guide_slug(path.stem)
            if not slug or slug in found:
                continue
            try:
                real = path.resolve()
                # Same rule the whole project uses on a path from outside: resolve
                # first, then confirm containment. A symlink out of the directory is
                # not a guide, and neither is a fifo or a device node.
                if real.is_file() and real.is_relative_to(root):
                    found[slug] = real
            except (OSError, RuntimeError):
                continue
    return found


def _guide_path(topic: str) -> Path | None:
    """Resolve a topic name to a file, or None. `topic` is hostile input."""
    name = (topic or "").strip().lower().removesuffix(".md")
    if not _GUIDE_TOPIC.match(name):
        return None
    return _guide_map().get(name)


def _guide_read(path: Path, limit: int = MAX_GUIDE_BYTES) -> str:
    """Bounded read. utf-8-sig so a BOM cannot hide the front matter."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise OpenRouterError(f"Cannot read guide {path.name}: {exc}") from exc
    text = raw[:limit].decode("utf-8-sig", "replace")
    if len(raw) > limit and limit == MAX_GUIDE_BYTES:
        text += f"\n\n[truncated at {limit // 1024} KB]\n"
    return text


def _guide_split(text: str) -> tuple[dict[str, str], str]:
    """Front matter and body, parsed once so the two can never disagree.

    Every line of the block has to be `key: value` or blank. A document that
    opens with a horizontal rule also starts with `---`, and without this check
    everything up to its next rule is swallowed as metadata: the prose vanishes
    from the body, the outline and search, and no error says so.
    """
    text = text.lstrip("\ufeff")
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or key.strip().startswith("#"):
            return {}, text
        meta[key.strip()] = value.strip()
    return meta, text[match.end() :].lstrip("\n")


def _guide_headings(body: str) -> list[tuple[int, re.Match[str]]]:
    """Heading positions, skipping fenced code blocks.

    Reference manuals are full of shell and markdown samples whose `##` lines
    look exactly like headings to a line scan. Treating one as real puts a
    section in the outline that does not exist and, worse, ends a section slice
    in the middle of an example.
    """
    starts: list[tuple[int, re.Match[str]]] = []
    fence = ""
    for index, line in enumerate(body.splitlines()):
        marker = _GUIDE_FENCE.match(line)
        if marker:
            token = marker.group(1)[:3]
            if not fence:
                fence = token
            elif line.lstrip().startswith(fence):
                fence = ""
            continue
        if fence:
            continue
        heading = _GUIDE_HEADING.match(line)
        if heading:
            starts.append((index, heading))
    return starts


def list_guides() -> list[dict]:
    """Every readable guide, with the front matter that says when to open it.

    Only the head of each file is read: the index needs about 40 bytes of
    metadata, and a configured directory can hold megabytes.
    """
    rows = []
    for slug, path in sorted(_guide_map().items()):
        head = _guide_read(path, FRONT_MATTER_BYTES)
        meta, body = _guide_split(head)
        rows.append({
            "topic": slug,
            "triggers": meta.get("triggers", "") or _guide_title(body),
            "verified": meta.get("verified", ""),
            "stale": not _GUIDE_DATE.match(meta.get("verified", "")),
            "path": str(path),
        })
    return rows


def _guide_title(body: str) -> str:
    """The h1 line, used when a file has no `triggers` front matter.

    A directory added through `guide_dirs` holds documents written for people,
    with no front matter at all. Without this their index line is a bare
    filename, and an agent has nothing to route on.
    """
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip() and not line.startswith("#"):
            break
    return ""


def _guide_load(topic: str) -> tuple[Path, dict[str, str], str]:
    path = _guide_path(topic)
    if path is None:
        known = ", ".join(sorted(_guide_map())) or "none"
        raise OpenRouterError(f"No guide named {topic!r}. Available guides: {known}")
    meta, body = _guide_split(_guide_read(path))
    return path, meta, body


def guide_outline(topic: str) -> dict:
    """Front matter plus the heading tree, so a long guide is navigable cheaply.

    Each entry carries its own length, because an agent choosing what to read
    needs to know that one section is 30 lines and another is 400.
    """
    path, meta, body = _guide_load(topic)
    lines = body.splitlines()
    starts = _guide_headings(body)
    sections = []
    for position, (index, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        sections.append({
            "level": len(match.group(1)),
            "title": match.group(2),
            "lines": end - index,
        })
    return {
        "topic": _guide_slug(path.stem),
        "triggers": meta.get("triggers", ""),
        "verified": meta.get("verified", ""),
        "path": str(path),
        "lines": len(lines),
        "sections": sections,
    }


def read_guide(topic: str, section: str | None = None) -> dict:
    """A whole guide, or one section of it.

    An empty or missing `section` returns the whole body. A section matches a
    heading case-insensitively, exactly first and then by substring, and returns
    that heading down to the next one at the same or a higher level. An
    ambiguous substring is refused rather than silently resolved to the first
    hit, because manuals repeat headings like "Examples" under every chapter.
    """
    path, meta, body = _guide_load(topic)
    result = {
        "topic": _guide_slug(path.stem),
        "verified": meta.get("verified", ""),
        "path": str(path),
        "section": None,
        "text": body,
    }
    if not section:
        return result

    lines = body.splitlines()
    wanted = section.strip().lower()
    starts = _guide_headings(body)
    hits = [p for p in starts if p[1].group(2).strip().lower() == wanted]
    if not hits and len(wanted) >= 3:
        hits = [p for p in starts if wanted in p[1].group(2).lower()]
    if not hits:
        titles = ", ".join(m.group(2) for _, m in starts) or "none"
        raise OpenRouterError(
            f"No section matching {section!r} in guide {result['topic']!r}. Sections: {titles}"
        )
    if len(hits) > 1:
        titles = ", ".join(m.group(2) for _, m in hits)
        raise OpenRouterError(
            f"{section!r} matches {len(hits)} headings in guide {result['topic']!r}: {titles}. "
            "Ask for the full heading."
        )

    start, match = hits[0]
    level = len(match.group(1))
    end = next(
        (index for index, other in starts if index > start and len(other.group(1)) <= level),
        len(lines),
    )
    text = "\n".join(lines[start:end]).rstrip()
    if len(text) > MAX_GUIDE_SECTION_CHARS:
        # The whole point of section mode is a bounded read. A chapter-sized
        # section would otherwise put the unbounded dump back.
        text = text[:MAX_GUIDE_SECTION_CHARS] + (
            f"\n\n[cut at {MAX_GUIDE_SECTION_CHARS} chars: read a sub-heading from the outline]"
        )
    result["section"] = match.group(2)
    result["text"] = text
    return result


def search_guides(query: str, limit: int = 20) -> dict:
    """Case-insensitive substring search across every guide.

    Returns `total` as well as the shown hits: a count that stops at the limit
    reads as "this is everywhere it appears", and the caller acts on the gap.
    Hits are taken round-robin so one large manual cannot use up the budget and
    hide every other guide.
    """
    needle = (query or "").strip().lower()
    if not needle:
        raise OpenRouterError("Search needs something to look for.")
    per_guide: list[list[dict]] = []
    total = 0
    for slug, path in sorted(_guide_map().items()):
        try:
            _, body = _guide_split(_guide_read(path))
        except OpenRouterError:
            continue
        heading = ""
        headings = dict(_guide_headings(body))
        found: list[dict] = []
        for index, line in enumerate(body.splitlines()):
            match = headings.get(index)
            if match:
                heading = match.group(2)
            # The heading line is searched too: it is the densest place the term
            # an agent is looking for actually appears.
            if needle in line.lower():
                total += 1
                found.append({
                    "topic": slug,
                    "section": heading,
                    "line": index + 1,
                    "snippet": line.strip()[:200],
                })
        if found:
            per_guide.append(found)

    hits: list[dict] = []
    while per_guide and len(hits) < limit:
        for found in list(per_guide):
            if len(hits) >= limit:
                break
            hits.append(found.pop(0))
            if not found:
                per_guide.remove(found)
    return {"hits": hits, "total": total, "truncated": total > len(hits)}
