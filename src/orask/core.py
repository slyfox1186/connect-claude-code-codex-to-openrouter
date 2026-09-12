"""Core OpenRouter client for the Claude Code / Codex second-opinion bridge.

Standard library only, on purpose: the CLI must keep working even if no
third-party package is installed. The MCP front-end adds the one dependency
(the `mcp` SDK) and reuses everything here.
"""

from __future__ import annotations

import base64
import concurrent.futures
import fcntl
import fnmatch
import hashlib
import json
import os
import socket
import stat
import threading
import uuid
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "OpenRouterError",
    "Config",
    "load_config",
    "get_api_key",
    "get_catalog",
    "resolve_model",
    "resolve_category",
    "category_models",
    "list_categories",
    "verify_categories",
    "as_list",
    "strip_call_syntax",
    "split_embedded_question",
    "classify_attachment",
    "expand_paths",
    "text_chars",
    "attachment_summary",
    "summarize_parts",
    "sent_attachments",
    "usable_turns",
    "ask",
    "ask_panel",
    "list_models",
    "model_info",
    "account_usage",
    "read_log",
]

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

BINARY_HINT = re.compile(rb"[\x00-\x08\x0e-\x1f]")

# Read ceiling applied before the file is opened, so a huge file is never
# pulled into memory just to be truncated afterwards.
MAX_FILE_BYTES = 32 * 1024 * 1024

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
    "request_timeout_s": 300,
    "max_input_chars": 600000,
    "max_file_chars": 200000,
    "max_attachment_bytes": MAX_ATTACHMENT_BYTES,
    "max_attachment_total_bytes": MAX_ATTACHMENT_TOTAL_BYTES,
    "max_attachments": 20,
    "max_dir_files": 50,
    "thread_attachment_bytes": 4 * 1024 * 1024,
    "pdf_engine": "cloudflare-ai",
    "max_cost_usd_per_call": 1.0,
    "catalog_ttl_s": 21600,
    "thread_max_messages": 20,
    "roles": {},
}

_config_cache: Config | None = None


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
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenRouterError(f"config file {path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise OpenRouterError(
                f"config file {path} must contain a JSON object, got {type(loaded).__name__}"
            )
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
                raw = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise OpenRouterError(
                    f"OpenRouter returned a non-JSON response to {method} {path}: "
                    f"{raw.strip()[:400] or '(empty body)'}"
                ) from exc
            if not isinstance(parsed, dict):
                raise OpenRouterError(
                    f"OpenRouter returned {type(parsed).__name__}, expected a JSON object"
                )
            return parsed
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001 - body already gone, status is enough
                pass
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
                "Reasoning models on a large context can be slow; retry with a "
                "lower effort, or raise request_timeout_s in the config."
            )
            raise last_error from exc

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
    parsed = detail
    try:
        obj = json.loads(detail)
        error = obj.get("error") or {}
        parsed = error.get("message") or detail
        typed = (error.get("metadata") or {}).get("error_type")
        if typed:
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
    return (
        isinstance(data, list)
        and bool(data)
        and all(isinstance(entry, dict) and entry.get("id") for entry in data)
    )


def get_catalog(refresh: bool = False, allow_stale: bool = True) -> list[dict[str, Any]]:
    """Live model list, memoised in-process and cached on disk.

    Never raises on a network failure when a cached copy exists: model lookup
    degrading to a stale catalogue beats the whole tool going down.
    """
    global _catalog_cache, _catalog_fetched_at, _catalog_failed_at
    ttl = _float_setting("catalog_ttl_s", 21600.0)

    # The MCP server is long-lived: without a TTL on the in-memory copy it would
    # serve the catalogue it started with for as long as the process lives, and
    # silently use stale prices, efforts and model lists.
    def _fresh() -> list[dict[str, Any]] | None:
        if _catalog_cache is not None and not refresh:
            if (time.time() - _catalog_fetched_at) < ttl:
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
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload, indent=indent), encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError:
        return False
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


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
            exact = spec if (not known or spec in known) else (bare if bare in known else f"~{bare}")
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
    return [str(v).strip().lower() for v in (cfg.get("category_exclude_vendors") or []) if str(v).strip()]


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
        for label in [name] + list(spec.get("aka") or []):
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
    known = {m.get("id") for m in catalog}
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
        if not known or slug in known:
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
            notes.append(f"category '{name}' pins {slug}, which is no longer available; skipped it.")

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
    catalog = _catalog_or_empty()
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
                "available": bool(model) or not by_id,
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
        scored.append(((quality,) + _rank_key(model), model))

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
    best = min(supported, key=lambda e: (abs(EFFORT_LADDER.index(e) - want), -EFFORT_LADDER.index(e)))
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


def _slurp(path: Path, ceiling: int) -> tuple[bytes, str | None]:
    """Read a regular file up to `ceiling` bytes, or say why it was skipped.

    A FIFO, device or socket would block a plain read forever (or return
    endless data) and hang the bridge. The descriptor is opened first and
    checked with fstat, so nothing can swap a regular file for a FIFO between
    the check and the open. O_NOFOLLOW is safe because the caller passes an
    already-resolved path, and it closes the last symlink race.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
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
        return b"".join(chunks), None
    except OSError as exc:
        return b"", f"could not read {path}: {exc.strerror or exc}"
    finally:
        os.close(fd)


def _peek(path: Path, count: int = 16) -> bytes:
    """First few bytes, for sniffing the real type. Never raises."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
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
    out: list[Path] = []
    notes: list[str] = []
    seen: set[str] = set()

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
        closing = _CLOSE_TAG.search(text)
        if closing and closing.group(1).lower() in CALL_SYNTAX_TAGS:
            # An orphan closer with no matching opener: the call was truncated.
            if not re.search(
                rf"<\s*{re.escape(closing.group(1))}(\s[^<>]*)?>", text[: closing.start()],
                re.IGNORECASE,
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
                f"`question` was empty and a <{tag}> block was found inside `context`; "
                "used that as the question. Send `question` as its own argument next time.",
            )

    context = strip_call_syntax(context)
    heading = None
    for match in _QUESTION_HEADING.finditer(context):
        heading = match  # the last heading wins; earlier ones are background
    if heading and context[heading.end():].strip():
        return (
            strip_call_syntax(context[heading.end():]),
            strip_call_syntax(context[: heading.start()]) or None,
            "`question` was empty and a 'Question' heading was found inside `context`; "
            "used the text under it as the question. Send `question` as its own "
            "argument next time.",
        )

    return None, context, None


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
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _float_setting(key: str, default: float) -> float:
    """A float config value that cannot take a call down.

    Same contract as _setting: a deliberate 0 means 0, and a value that is not a number falls
    back to the default rather than raising. These are read on the paid path, and one of them
    is the cost guard itself, so a typo in a config file must not become a traceback.
    """
    value = load_config().get(key)
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
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
    if cfg.get("deny_file_patterns_replace"):
        patterns = list(deny)
    else:
        patterns = sorted(set(DEFAULT_DENY_PATTERNS) | set(deny))

    candidates, walk_notes = expand_paths(as_list(files), base, _setting("max_dir_files", 50))
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
            {"role": "user", "content": [{"type": "text", "text": user_content}] + attachments}
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
            tokens += len(blob) * 0.75 * TOKENS_PER_PDF_BYTE
    counts["tokens"] = int(tokens)
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
    try:
        return float((model.get("pricing") or {}).get(field) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def estimate_call_cost(slug: str, chars: int, max_tokens: int | None) -> tuple[float, bool]:
    """Worst-case cost of a call: whole prompt in, max_tokens out.

    Returns (usd, priced). `priced` is False when the model is not in the
    catalogue, so the caller can say the guard could not be evaluated instead
    of treating an unknown price as free.
    """
    model = _find(slug)
    if not model:
        return 0.0, False
    prompt = (chars / CHARS_PER_TOKEN) * _price(model, "prompt")
    output = float(max_tokens or 0) * _price(model, "completion")
    return prompt + output, True


def estimate_input_cost(slug: str, chars: int) -> float:
    """Prompt-side cost only (kept for callers that just want the input side)."""
    return estimate_call_cost(slug, chars, 0)[0]


def actual_cost(slug: str, usage: dict[str, Any]) -> float:
    """Prefer OpenRouter's own cost; fall back to catalogue pricing."""
    for key in ("cost", "total_cost"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    model = _find(slug)
    if not model:
        return 0.0
    prompt = float(usage.get("prompt_tokens") or 0)
    completion = float(usage.get("completion_tokens") or 0)
    return prompt * _price(model, "prompt") + completion * _price(model, "completion")


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
        if not isinstance(message, dict):
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


def load_thread(name: str | None) -> list[dict[str, Any]]:
    if not name:
        return []
    path = _thread_path(name)
    if not path.is_file():
        return []
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return usable_turns(blob.get("messages") or [])
    except (OSError, json.JSONDecodeError):
        return []


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
    lock_handle = None
    try:
        THREAD_DIR.mkdir(parents=True, exist_ok=True)
        lock_handle = open(path.with_suffix(".lock"), "a+")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        if lock_handle is not None:
            lock_handle.close()
        lock_handle = None  # proceed unlocked rather than lose the answer
    try:
        return _save_thread_locked(
            path, name, question, answer, slug, annotations, attachments
        )
    except Exception:  # noqa: BLE001 - this runs after the call has been billed, so no
        return False   # transcript problem may be allowed to destroy the answer
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                lock_handle.close()


def _save_thread_locked(
    path: Path,
    name: str,
    question: str,
    answer: str,
    slug: str,
    annotations: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    history = load_thread(name)
    # The attachments ride on the user turn, which is where they were sent, so
    # a follow-up still has the document in front of it. Annotations alone do
    # not carry content: they only tell OpenRouter it has already parsed this
    # file, so it can skip the parse and its cost.
    if attachments:
        history.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": question}] + attachments,
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
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with CALL_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.time(), **entry}) + "\n")
    except OSError:
        pass  # never fail a call because the log is unwritable


# The log is append-only and never rotated, so reads are bounded: `orask usage` asks for a
# hundred thousand entries, which at 512 bytes apiece would be a 51 MB read on every call.
MAX_LOG_WINDOW_BYTES = 4 * 1024 * 1024


def read_log(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent `limit` entries, read from the tail rather than the whole file."""
    if not CALL_LOG.is_file() or limit <= 0:
        return []
    try:
        size = CALL_LOG.stat().st_size
        window = min(size, max(int(limit) * 512, 65536), MAX_LOG_WINDOW_BYTES)
        with CALL_LOG.open("rb") as handle:
            handle.seek(size - window)
            blob = handle.read(window)
        text = blob.decode("utf-8", "replace")
        if window < size:
            # The first line is probably a fragment of an earlier record.
            text = text.partition("\n")[2]
        lines = text.splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


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
    temperature: float | None = None,
    thread: str | None = None,
    cwd: str | None = None,
    pdf_engine: str | None = None,
    allow_expensive: bool = False,
    allow_secret_files: bool = False,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    """Ask one model and return a structured result."""
    cfg = load_config()
    started = time.monotonic()

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
        name, _ = resolve_category(category)
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

    guard = _float_setting("max_cost_usd_per_call", 1.0)
    estimate, priced = estimate_call_cost(slug, billable, limit)
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
            notes.append(
                f"no catalogue pricing for {slug}, so the {fmt_usd(guard)} per-call "
                "cost guard could not be checked (cost_guard_on_unknown_pricing="
                f"'{policy}')"
            )
        elif estimate > guard:
            raise OpenRouterError(
                f"refusing to send: worst-case cost for {slug} is about "
                f"{fmt_usd(estimate)} ({billable} chars in, up to {limit} tokens out), over "
                f"the {fmt_usd(guard)} per-call guard. Trim the context, lower "
                "max_tokens, or pass allow_expensive to override."
            )

    payload: dict[str, Any] = {
        "model": slug,
        "messages": messages,
        "usage": {"include": True},
    }

    wanted_effort = effort if effort is not None else cfg.get("default_effort")
    final_effort, effort_note = clamp_effort(slug, wanted_effort)
    if effort_note:
        notes.append(effort_note)
    if final_effort:
        payload["reasoning"] = {"effort": final_effort}

    if limit:
        payload["max_tokens"] = int(limit)
    if temperature is not None:
        payload["temperature"] = float(temperature)

    if fresh["pdf"] or attached["pdf"]:
        # OpenRouter parses the PDF before the model sees it, which is why a PDF
        # attaches to any model at all. The engine decides what that costs:
        # cloudflare-ai is free and fine for a text PDF, mistral-ocr bills per
        # 1,000 pages and is the one that can read a scan.
        engine = str(pdf_engine or cfg.get("pdf_engine") or "cloudflare-ai").strip().lower()
        if engine not in PDF_ENGINES:
            notes.append(
                f"pdf_engine '{engine}' is not one of {', '.join(PDF_ENGINES)}; "
                "used cloudflare-ai"
            )
            engine = "cloudflare-ai"
        payload["plugins"] = [{"id": "file-parser", "pdf": {"engine": engine}}]
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

    timeout = _float_setting("request_timeout_s", 300.0) or 300.0
    try:
        response = _request("POST", "/chat/completions", payload, timeout=timeout)
        # OpenRouter can answer 200 and put the failure in the body. Route it through the same
        # translator as an HTTP error: otherwise the caller gets "returned no choices" plus a
        # raw dump, and none of the typed-error guidance that exists for exactly this.
        embedded = response.get("error")
        if isinstance(embedded, dict):
            raise OpenRouterError(
                _http_message(int(embedded.get("code") or 200), json.dumps(response))
            )
    except OpenRouterError as exc:
        log_call(
            {
                "model": slug, "requested": model, "ok": False,
                "error": str(exc)[:500], "chars_in": chars,
                "latency_s": round(time.monotonic() - started, 2), "thread": thread,
            }
        )
        raise

    choices = response.get("choices") or []
    if not choices:
        raise OpenRouterError(
            f"{slug} returned no choices. Raw response: {json.dumps(response)[:600]}"
        )
    message = choices[0].get("message") or {}
    answer = (message.get("content") or "").strip()
    # Present when OpenRouter parsed an attached file for this call. Sent back
    # on the next turn, it stands in for re-parsing the same document.
    annotations = message.get("annotations") or None
    reasoning = (message.get("reasoning") or "").strip()
    finish = choices[0].get("finish_reason") or choices[0].get("native_finish_reason")

    if not answer and reasoning:
        answer = reasoning
        notes.append(
            "the model returned only reasoning text and no final answer "
            "(often means max_tokens was consumed while thinking); showing the reasoning"
        )
        reasoning = ""
    empty = not answer
    if empty:
        notes.append(
            f"{slug} returned no answer at all (finish_reason={finish}); "
            "the call was still billed"
        )

    if finish == "length":
        cap = payload.get("max_tokens")
        notes.append(
            f"answer was cut off at the {cap} token limit; ask for a shorter answer "
            "or raise max_tokens"
            if cap
            else "answer was cut off by the provider's own output limit"
        )

    usage = response.get("usage") or {}
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
    if thread and answer and not save_thread(
        thread, question, answer, slug, annotations, carried
    ):
        notes.append(
            f"could not write the thread transcript to {THREAD_DIR}; this answer "
            "will not be part of the next follow-up"
        )

    log_call(
        {
            "model": slug, "requested": model, "ok": not empty, "empty": empty,
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
        # An empty completion is reported as a failure: a caller keying on `ok`
        # must not treat "no answer" as a second opinion.
        "ok": not empty,
        "error": (
            f"{slug} returned an empty answer (finish_reason={finish})" if empty else None
        ),
        "model": slug,
        "requested": model,
        "answer": answer,
        "reasoning": reasoning if include_reasoning else "",
        "effort": final_effort,
        "finish_reason": finish,
        "provider": response.get("provider"),
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
            except Exception as exc:  # noqa: BLE001 - report, never propagate
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

    rows = []
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
