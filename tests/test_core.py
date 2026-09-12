"""Offline unit tests for the parts most likely to break silently.

No network, no API key needed:
    python tests/test_core.py        (any interpreter; no mcp package needed)

"Offline" is enforced here rather than merely intended: every runtime path is redirected to a
scratch directory before core is imported, and the HTTP layer is replaced with one that raises.
A test that reaches the network is a bug in the test, and it fails loudly instead of billing a
real model call.
"""

import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# core reads these into module-level constants at import time, so they have to be set first.
# Without this a run reads the real API key and appends fabricated entries to the real call
# log, which then feeds `orask log` and `orask usage`.
SCRATCH = Path(tempfile.mkdtemp(prefix="orask-tests-"))
os.environ["ORASK_CONFIG_DIR"] = str(SCRATCH / "config")
os.environ["ORASK_STATE_DIR"] = str(SCRATCH / "state")
os.environ["ORASK_CACHE_DIR"] = str(SCRATCH / "cache")
os.environ.pop("OPENROUTER_API_KEY", None)

from orask import core

FAILS = []
CHECKS = 0


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


# A stand-in catalogue that mirrors the real shapes, including the fact that
# Kimi and GLM accept only max/high/low.
FAKE = [
    {
        "id": "moonshotai/kimi-k3", "name": "MoonshotAI: Kimi K3", "created": 100,
        "context_length": 1048576, "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "top_provider": {"max_completion_tokens": 900000},
        "supported_parameters": ["reasoning", "reasoning_effort", "max_tokens"],
        "reasoning": {"supported_efforts": ["max", "high", "low"], "default_effort": "max"},
        "benchmarks": {"artificial_analysis": {"intelligence_index": 43.8}},
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "moonshotai/kimi-k2.5", "name": "MoonshotAI: Kimi K2.5", "created": 50,
        "context_length": 262144, "pricing": {"prompt": "0.00000045", "completion": "0.00000225"},
        "top_provider": {"max_completion_tokens": 100000},
        "supported_parameters": ["reasoning"],
        "reasoning": {"supported_efforts": ["high", "low"]},
        "benchmarks": {"artificial_analysis": {"intelligence_index": 30.0}},
        "architecture": {"input_modalities": ["text"]},
    },
    {
        "id": "moonshotai/kimi-k3:batch", "name": "Kimi K3 batch", "created": 100,
        "context_length": 1048576, "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "top_provider": {}, "supported_parameters": [],
        "benchmarks": {"artificial_analysis": {"intelligence_index": 43.8}},
        "architecture": {},
    },
    {
        "id": "z-ai/glm-5.3", "name": "Z.AI: GLM 5.3", "created": 90,
        "context_length": 1310720, "pricing": {"prompt": "0.0000014", "completion": "0.0000044"},
        "top_provider": {"max_completion_tokens": 900000},
        "supported_parameters": ["reasoning", "reasoning_effort"],
        "reasoning": {"supported_efforts": ["max", "high", "low"], "mandatory": True},
        "benchmarks": {"artificial_analysis": {"intelligence_index": 44.9}},
        "architecture": {"input_modalities": ["text"]},
    },
    {
        "id": "mistralai/mistral-large-2512", "name": "Mistral Large", "created": 80,
        "context_length": 262144, "pricing": {"prompt": "0.000002", "completion": "0.000006"},
        "top_provider": {"max_completion_tokens": 8000},
        "supported_parameters": ["max_tokens", "temperature"],
        "benchmarks": {}, "architecture": {"input_modalities": ["text"]},
    },
]

REAL_GET_CATALOG = core.get_catalog
core._catalog_cache = FAKE
core.get_catalog = lambda refresh=False, allow_stale=True: FAKE  # type: ignore[assignment]
core._config_cache = None


def _no_network(method, path, payload=None, timeout=60.0, retries=3):
    raise AssertionError(
        f"a test reached the HTTP layer: {method} {path}. That is a billable call, so it is an "
        "error in the test rather than something to tolerate."
    )


core._request = _no_network  # type: ignore[assignment]
cfg = core.load_config()

# ---- alias + slug resolution ----------------------------------------------
check("alias resolves to pinned slug", core.resolve_model("kimi")[0] == "moonshotai/kimi-k3")
check("alias is case-insensitive", core.resolve_model("GLM")[0] == "z-ai/glm-5.3")
check("full slug passes through", core.resolve_model("z-ai/glm-5.3")[0] == "z-ai/glm-5.3")
check(
    "fuzzy match prefers the smartest variant, not the newest name",
    core.resolve_model("kimi-k2.5")[0] == "moonshotai/kimi-k2.5",
)
check(
    "fuzzy match never silently picks a :batch endpoint",
    core.resolve_model("kimi")[0] != "moonshotai/kimi-k3:batch",
)
try:
    core.resolve_model("totally-unknown-thing")
    check("unknown model raises", False)
except core.OpenRouterError as exc:
    check("unknown model raises with guidance", "models --search" in str(exc))

# self-heal when a pinned alias disappears upstream
core._config_cache = dict(cfg, aliases={"kimi": "moonshotai/kimi-k9-retired"})
slug, note = core.resolve_model("kimi")
check(
    "retired pin self-heals to a live model",
    slug == "moonshotai/kimi-k3" and note and "no longer lists" in note,
    f"{slug} / {note}",
)

# allowed_models hard lock
core._config_cache = dict(cfg, allowed_models=["z-ai/glm-5.3"])
try:
    core.resolve_model("kimi")
    check("allowed_models blocks other models", False)
except core.OpenRouterError as exc:
    check("allowed_models blocks other models", "not in allowed_models" in str(exc))
check("allowed_models permits a listed model", core.resolve_model("glm")[0] == "z-ai/glm-5.3")
core._config_cache = cfg

# ---- effort clamping -------------------------------------------------------
check(
    "medium snaps up to high on a max/high/low model",
    core.clamp_effort("moonshotai/kimi-k3", "medium")[0] == "high",
)
check(
    "xhigh snaps up to max",
    core.clamp_effort("z-ai/glm-5.3", "xhigh")[0] == "max",
)
check("a supported effort is left alone",
      core.clamp_effort("z-ai/glm-5.3", "high") == ("high", None))
check("minimal snaps to low", core.clamp_effort("moonshotai/kimi-k3", "minimal")[0] == "low")
check(
    "a model without reasoning gets no effort",
    core.clamp_effort("mistralai/mistral-large-2512", "high")[0] is None,
)
check("effort None stays None", core.clamp_effort("z-ai/glm-5.3", None)[0] is None)
check("effort 'none' disables it", core.clamp_effort("z-ai/glm-5.3", "none")[0] is None)
check(
    "a bogus effort falls back to the model default",
    core.clamp_effort("moonshotai/kimi-k3", "wildly-wrong")[0] == "max",
)

# ---- prompt assembly -------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "a.py").write_text("print('hello')\n")
    (root / "big.txt").write_text("x" * 5000)
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00")

    messages, notes = core.build_messages(
        "What breaks here?", context="It crashes on start.",
        files=[str(root / "a.py"), "a.py", str(root / "missing.py"), str(root / "blob.bin")],
        cwd=str(root),
    )
    body = messages[-1]["content"]
    check("system prompt comes first", messages[0]["role"] == "system")
    check("advisor role text is used", "second opinion" in messages[0]["content"])
    check("context is included", "It crashes on start." in body)
    check("question is included", "What breaks here?" in body)
    check("file content is inlined", "print('hello')" in body)
    check("relative paths resolve against cwd",
          f"## File: {root.resolve() / 'a.py'}" in body)
    check("the same file named twice is only sent once",
          body.count("print('hello')") == 1)
    check("a missing file is reported, not fatal", any("not found" in n for n in notes))
    check("a binary file is skipped", any("binary" in n for n in notes))

    core._config_cache = dict(cfg, max_file_chars=1000)
    _, notes = core.build_messages("q", files=[str(root / "big.txt")])
    check("oversized files are truncated with a note", any("truncated" in n for n in notes))

    core._config_cache = dict(cfg, max_input_chars=100)
    try:
        core.build_messages("q", files=[str(root / "big.txt")])
        check("max_input_chars is enforced", False)
    except core.OpenRouterError as exc:
        check("max_input_chars is enforced", "max_input_chars" in str(exc))
    core._config_cache = cfg

try:
    core.build_messages("   ")
    check("empty question rejected", False)
except core.OpenRouterError:
    check("empty question rejected", True)

messages, notes = core.build_messages("q", role="does-not-exist")
check("an unknown role falls back to advisor with a note", any("not defined" in n for n in notes))

messages, _ = core.build_messages("q", system="BE TERSE")
check("explicit system prompt overrides the role", messages[0]["content"] == "BE TERSE")

messages, _ = core.build_messages(
    "follow up", history=[{"role": "user", "content": "first"},
                          {"role": "assistant", "content": "reply"}],
)
check("thread history is replayed in order",
      [m["role"] for m in messages] == ["system", "user", "assistant", "user"])

# a fence inside a file must not break out of the code block
with tempfile.TemporaryDirectory() as tmp:
    tricky = Path(tmp) / "readme.md"
    tricky.write_text("```\nfenced\n```\n")
    messages, _ = core.build_messages("q", files=[str(tricky)])
    check("nested code fences are escaped", "````" in messages[-1]["content"])

# ---- cost ------------------------------------------------------------------
check(
    "input cost estimate is in the right ballpark",
    0.7 < core.estimate_input_cost("moonshotai/kimi-k3", 1_200_000) < 1.2,
    f"${core.estimate_input_cost('moonshotai/kimi-k3', 1_200_000):.3f} for 1.2M chars",
)
check(
    "OpenRouter's own cost figure wins",
    core.actual_cost("moonshotai/kimi-k3", {"cost": 0.5, "prompt_tokens": 10}) == 0.5,
)
check(
    "cost falls back to catalogue pricing",
    abs(core.actual_cost("z-ai/glm-5.3", {"prompt_tokens": 1000, "completion_tokens": 1000})
        - (0.0000014 + 0.0000044) * 1000) < 1e-9,
)

# ---- threads ---------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    core.THREAD_DIR = Path(tmp) / "threads"
    core.save_thread("my-thread", "q1", "a1", "moonshotai/kimi-k3")
    core.save_thread("my-thread", "q2", "a2", "moonshotai/kimi-k3")
    history = core.load_thread("my-thread")
    check("thread round-trips", len(history) == 4 and history[0]["content"] == "q1")
    check("unknown thread is empty, not an error", core.load_thread("nope") == [])
    core.save_thread("../../escape", "q", "a", "m")
    written = core._thread_path("../../escape").resolve()
    check(
        "thread names cannot escape the directory",
        written.parent == core.THREAD_DIR.resolve() and written.is_file(),
        str(written),
    )
    check(
        "a traversal name is flattened, not honoured",
        "/" not in written.stem and ".." not in written.parts[:-1],
        written.name,
    )

    core._config_cache = dict(cfg, thread_max_messages=2)
    core.save_thread("capped", "q1", "a1", "m")
    core.save_thread("capped", "q2", "a2", "m")
    check("thread history is capped", len(core.load_thread("capped")) == 2)
    core._config_cache = cfg

# ---- file safety and the secrets denylist ---------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_ed25519").write_text("PRIVATE KEY MATERIAL")
    (root / ".env").write_text("SECRET=abc123")
    (root / "key.pem").write_text("cert")
    (root / "ok.py").write_text("value = 1")
    (root / "tokenizer.py").write_text("# legitimate source")

    _, notes = core.build_messages("q", files=[str(root / ".ssh" / "id_ed25519")])
    check(
        "an ssh private key is refused, not uploaded",
        any("REFUSED" in n for n in notes),
        next((n[:60] for n in notes), ""),
    )
    messages, _ = core.build_messages("q", files=[str(root / ".ssh" / "id_ed25519")])
    check("refused file content never reaches the prompt",
          "PRIVATE KEY MATERIAL" not in messages[-1]["content"])

    for secret in (".env", "key.pem"):
        _, notes = core.build_messages("q", files=[str(root / secret)])
        check(f"{secret} is refused", any("REFUSED" in n for n in notes))

    messages, notes = core.build_messages("q", files=[str(root / "ok.py")])
    check("an ordinary source file still goes through",
          "value = 1" in messages[-1]["content"] and not any("REFUSED" in n for n in notes))
    messages, _ = core.build_messages("q", files=[str(root / "tokenizer.py")])
    check("the denylist does not catch innocent names like tokenizer.py",
          "legitimate source" in messages[-1]["content"])

    messages, _ = core.build_messages(
        "q", files=[str(root / ".env")], allow_secret_files=True,
    )
    check("allow_secret_files overrides the denylist",
          "SECRET=abc123" in messages[-1]["content"])

    core._config_cache = dict(cfg, deny_file_patterns=["*.py"])
    _, notes = core.build_messages("q", files=[str(root / "ok.py")])
    check("deny_file_patterns is configurable", any("REFUSED" in n for n in notes))
    core._config_cache = cfg

    fifo = root / "pipe"
    os.mkfifo(fifo)
    _, notes = core.build_messages("q", files=[str(fifo)])
    check(
        "a FIFO is skipped instead of hanging the bridge",
        any("not a regular file" in n for n in notes),
        next((n[:60] for n in notes), ""),
    )

# ---- cost guard ------------------------------------------------------------
check(
    "worst-case estimate includes the output side",
    core.estimate_call_cost("moonshotai/kimi-k3", 1000, 32000)[0]
    > core.estimate_call_cost("moonshotai/kimi-k3", 1000, 0)[0],
)
check(
    "an unpriced model reports that the guard cannot be judged",
    core.estimate_call_cost("who/knows", 1000, 100) == (0.0, False),
)
check(
    "32k output on Kimi is correctly seen as costing about $0.48",
    0.45 < core.estimate_call_cost("moonshotai/kimi-k3", 100, 32000)[0] < 0.52,
    f"${core.estimate_call_cost('moonshotai/kimi-k3', 100, 32000)[0]:.3f}",
)

# ---- config and key validation --------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    bad = Path(tmp) / "config.json"
    bad.write_text("[1, 2, 3]")
    saved_user, core.USER_CONFIG = core.USER_CONFIG, bad
    try:
        core.load_config(refresh=True)
        check("a non-object config is rejected clearly", False)
    except core.OpenRouterError as exc:
        check("a non-object config is rejected clearly", "must contain a JSON object" in str(exc))
    core.USER_CONFIG = saved_user
    core._config_cache = cfg

    empty_key = Path(tmp) / "env"
    empty_key.write_text("OPENROUTER_API_KEY=\n")
    saved_env_file, core.ENV_FILE = core.ENV_FILE, empty_key
    saved_env = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        core.get_api_key()
        check("an empty key value reads as no key", False)
    except core.OpenRouterError as exc:
        check("an empty key value reads as no key", "No OpenRouter API key" in str(exc))
    finally:
        core.ENV_FILE = saved_env_file
        if saved_env is not None:
            os.environ["OPENROUTER_API_KEY"] = saved_env

# ---- retry policy: a POST must not be retried into a double bill ----------
check("POST retries exclude 5xx", {408, 429} == core.RETRY_STATUS_POST)
check("GET retries still cover 5xx", 502 in core.RETRY_STATUS_GET)

# ---- panel refuses a thread instead of dropping it ------------------------
try:
    core.ask_panel("q", models=["kimi"], thread="oops")
    check("a panel rejects a thread argument", False)
except core.OpenRouterError as exc:
    check("a panel rejects a thread argument", "not supported for a panel" in str(exc))

# ---- catalogue cache validation -------------------------------------------
check("a corrupt catalogue shape is rejected", not core._valid_catalog([{"no_id": 1}]))
check("an empty catalogue is rejected", not core._valid_catalog([]))
check("a dict masquerading as a catalogue is rejected", not core._valid_catalog({"a": 1}))
check("a real catalogue validates", core._valid_catalog(FAKE))


# ---- the -0 slice trap and case-dodging -----------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    big = root / "big.txt"
    big.write_text("A" * 4000 + "B" * 4000)

    core._config_cache = dict(cfg, max_file_chars=0)
    messages, notes = core.build_messages("q", files=[str(big)])
    check(
        "max_file_chars=0 does not leak the whole file (text[-0:] trap)",
        "A" * 100 not in messages[-1]["content"],
        next((n[:60] for n in notes), ""),
    )
    core._config_cache = dict(cfg, max_file_chars=100)
    messages, _ = core.build_messages("q", files=[str(big)])
    body = messages[-1]["content"]
    check("a truncated file really is truncated", len(body) < 1500, f"{len(body)} chars")
    check("truncation keeps a tail", "B" in body)
    core._config_cache = cfg

    upper = root / "ID_RSA"
    upper.write_text("KEY")
    _, notes = core.build_messages("q", files=[str(upper)])
    check("an uppercase secret filename is still refused",
          any("REFUSED" in n for n in notes))
    upper_pem = root / "CERT.PEM"
    upper_pem.write_text("cert")
    _, notes = core.build_messages("q", files=[str(upper_pem)])
    check("an uppercase .PEM is still refused", any("REFUSED" in n for n in notes))

check("read_log with limit 0 returns nothing, not everything", core.read_log(0) == [])

# ---- threads: distinct names, distinct files, concurrent-safe -------------
with tempfile.TemporaryDirectory() as tmp:
    core.THREAD_DIR = Path(tmp) / "threads"
    a, b = core._thread_path("plan a"), core._thread_path("plan-a!")
    check("similar thread names get distinct files", a != b, f"{a.name} vs {b.name}")
    check("the same name is stable across calls",
          core._thread_path("plan a") == core._thread_path("plan a"))

    core.save_thread("t1", "q", "a", "m")
    core.save_thread("t2", "q", "a", "m")
    check("two threads stay separate", len(core.list_threads()) == 2)

    # concurrent turns on one thread must not lose an exchange
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(
            lambda i: core.save_thread("shared", f"q{i}", f"a{i}", "m"), range(6)
        ))
    kept = core.load_thread("shared")
    check("concurrent writes to one thread lose nothing",
          len(kept) == 12, f"{len(kept)} of 12 messages kept")

# ---- denylist must not be bypassable by a symlink or a .. path -----------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / ".ssh").mkdir()
    secret = root / ".ssh" / "id_rsa"
    secret.write_text("PRIVATE KEY MATERIAL")

    innocent = root / "notes.txt"
    innocent.symlink_to(secret)
    _, notes = core.build_messages("q", files=[str(innocent)])
    check(
        "a symlink pointing at a secret is refused",
        any("REFUSED" in n for n in notes),
        next((n[:70] for n in notes), ""),
    )
    messages, _ = core.build_messages("q", files=[str(innocent)])
    check("symlinked secret content never reaches the prompt",
          "PRIVATE KEY MATERIAL" not in messages[-1]["content"])

    sneaky = root / "sub" / ".." / ".ssh" / "id_rsa"
    (root / "sub").mkdir()
    _, notes = core.build_messages("q", files=[str(sneaky)])
    check("a '..' path to a secret is refused", any("REFUSED" in n for n in notes))

    # a symlink to an ordinary file must still work
    plain = root / "real.py"
    plain.write_text("x = 1")
    link = root / "link.py"
    link.symlink_to(plain)
    messages, notes = core.build_messages("q", files=[str(link)])
    check("a symlink to an ordinary file still works",
          "x = 1" in messages[-1]["content"], str(notes))

    # user patterns must ADD to the built-ins, not replace them
    core._config_cache = dict(cfg, deny_file_patterns=["*/proprietary/*"])
    _, notes = core.build_messages("q", files=[str(secret)])
    check("a custom deny pattern does not disable the built-ins",
          any("REFUSED" in n for n in notes))
    core._config_cache = dict(
        cfg, deny_file_patterns=["*/nothing/*"], deny_file_patterns_replace=True,
    )
    _, notes = core.build_messages("q", files=[str(secret)])
    check("replace mode is available but must be explicit",
          not any("REFUSED" in n for n in notes))
    core._config_cache = cfg

# ---- max_tokens must be a real cap ---------------------------------------
try:
    core.ask("q", model="kimi", max_tokens=0)
    check("max_tokens=0 is rejected", False)
except core.OpenRouterError as exc:
    check("max_tokens=0 is rejected", "must be 1 or more" in str(exc))

# ---- context window ------------------------------------------------------
# kimi-k3 is 1048576 in FAKE; mistral-large is 262144 with an 8000 output ceiling.
check(
    "the published context window is read from the catalogue",
    core.context_window("moonshotai/kimi-k3") == 1048576,
    str(core.context_window("moonshotai/kimi-k3")),
)
check("an unknown model has no window", core.context_window("who/knows") == 0)

_fit, _win, _notes = core.fit_context("moonshotai/kimi-k3", 1000, 32000)
check("a prompt that fits leaves max_tokens alone", (_fit, _win, _notes) == (32000, 1048576, []))

# 900k chars is about 250k tokens, so a 262144 window has ~11k left for the answer.
_fit, _win, _notes = core.fit_context("mistralai/mistral-large-2512", 900000, 32000)
check(
    "max_tokens is lowered to the room left in the window",
    _fit < 32000 and _fit >= core.MIN_ANSWER_TOKENS and _win == 262144,
    f"{_fit} of {_win}",
)
check("lowering max_tokens is reported",
      any("lowered max_tokens" in n for n in _notes), str(_notes))

_fit, _win, _notes = core.fit_context("moonshotai/kimi-k3", 1000, 32000, requested_window=99999999)
check(
    "a requested window above the model's is clamped to the model's",
    _win == 1048576 and any("cannot be raised" in n for n in _notes),
    f"{_win} {_notes}",
)

_fit, _win, _notes = core.fit_context("moonshotai/kimi-k3", 1000, 32000, requested_window=4000)
check(
    "a smaller requested window budgets the answer down",
    _win == 4000 and _fit < 32000,
    f"{_fit} of {_win}",
)

try:
    core.fit_context("mistralai/mistral-large-2512", 4000000, 32000)
    check("a prompt that fills the window is refused", False, "no error raised")
except core.OpenRouterError as exc:
    check(
        "a prompt that fills the window is refused",
        "no room left to reply" in str(exc),
        str(exc)[:80],
    )

_fit, _win, _notes = core.fit_context(
    "mistralai/mistral-large-2512", 4000000, 32000, compress=True
)
check(
    "context_compression turns that refusal into a capped answer",
    _fit <= _win // 2 and _fit >= core.MIN_ANSWER_TOKENS
    and any("compression" in n for n in _notes),
    f"{_fit} {_notes}",
)

try:
    core.ask("q", model="kimi", max_context_tokens=10)
    check("an unusably small max_context_tokens is rejected", False, "no error raised")
except core.OpenRouterError as exc:
    check(
        "an unusably small max_context_tokens is rejected",
        "at least" in str(exc),
        str(exc)[:80],
    )

check(
    "context_compression only sends a plugin when someone decided",
    core._tristate(None) is None and core._tristate("yes") is None
    and core._tristate(False) is False and core._tristate(True) is True,
)

# ---- an unwritable state directory must not break anything ---------------
with tempfile.TemporaryDirectory() as tmp:
    locked = Path(tmp) / "locked"
    locked.mkdir(mode=0o500)
    saved = core.THREAD_DIR
    core.THREAD_DIR = locked / "threads"
    try:
        wrote = core.save_thread("t", "q", "a", "m")
        check("an unwritable thread dir returns False, never raises", wrote is False)
    except Exception as exc:
        check("an unwritable thread dir returns False, never raises", False, repr(exc))
    finally:
        core.THREAD_DIR = saved
        locked.chmod(0o700)

check(
    "_write_json_atomic reports failure instead of raising",
    core._write_json_atomic(Path("/proc/definitely/not/writable/x.json"), {"a": 1}) is False,
)

# ---- cost guard policy on unknown pricing --------------------------------
# The model has to be genuinely unpriced for this branch to exist, which means absent from the
# catalogue. mistral-large IS priced in FAKE, so the earlier version of this check sailed past
# the guard, sent a real billed POST, and then asserted on a refusal that could never happen.
_saved_catalog = core.get_catalog
core.get_catalog = lambda refresh=False, allow_stale=True: []  # type: ignore[assignment]
core._config_cache = dict(cfg, cost_guard_on_unknown_pricing="block")
try:
    core.ask("q", model="unpriced/model-x")
    check("block policy refuses a model with no catalogue pricing", False, "no error raised")
except core.OpenRouterError as exc:
    check("block policy refuses a model with no catalogue pricing",
          "cannot be checked" in str(exc), str(exc)[:80])
core._config_cache = cfg
core.get_catalog = _saved_catalog

# ---- stdin must never hang the CLI (regression: fd 0 as an open socket) ----
import socket as _socket
import subprocess as _sp

_LAUNCHER = str(Path(__file__).resolve().parents[1] / "bin" / "orask")

# An open socketpair as fd 0 is what a background job or daemon hands us; the
# write end is never closed, so a naive read() blocks forever.
_parent, _child = _socket.socketpair()
try:
    proc = _sp.run(
        [_LAUNCHER, "--version"], stdin=_child,
        capture_output=True, text=True, timeout=25, check=False,
    )
    check("an open socket on stdin does not hang the CLI", proc.returncode == 0,
          proc.stdout.strip() or proc.stderr.strip()[:80])
except _sp.TimeoutExpired:
    check("an open socket on stdin does not hang the CLI", False, "timed out")
finally:
    _parent.close()
    _child.close()

# a socket carrying data but never closing: take the data, then stop waiting
_parent, _child = _socket.socketpair()
try:
    _parent.sendall(b"context from a socket that stays open")
    proc = _sp.run(
        # `categories` reads the packaged config and nothing else: no key, no network.
        [_LAUNCHER, "categories"],
        stdin=_child, capture_output=True, text=True, timeout=30, check=False,
    )
    check("a socket that never closes still returns", proc.returncode == 0,
          proc.stderr.strip()[:80] or "ok")
except _sp.TimeoutExpired:
    check("a socket that never closes still returns", False, "timed out")
finally:
    _parent.close()
    _child.close()

# the ergonomic path must keep working: a real pipe is drained in full
proc = _sp.run(
    [_LAUNCHER, "--version"], input="piped text", capture_output=True, text=True,
    timeout=25, check=False,
)
check("a normal pipe on stdin still works", proc.returncode == 0, proc.stdout.strip())

# and /dev/null (a character device) is simply empty
with open(os.devnull) as _devnull:
    proc = _sp.run(
        [_LAUNCHER, "--version"], stdin=_devnull,
        capture_output=True, text=True, timeout=25, check=False,
    )
    check("/dev/null on stdin is treated as empty", proc.returncode == 0)

# --------------------------------------------------------------------------
# argument shapes: a malformed call from the calling agent
#
# The failure this guards against, seen in the wild: Claude Code emitted a
# tool call whose `question` never arrived because the whole question had been
# folded into `context` inside <question> tags, with a stray </invoke> left
# trailing from its own tool-call syntax. The SDK rejected it with a pydantic
# traceback and the turn was wasted.
# --------------------------------------------------------------------------

check("a list argument is left alone", core.as_list(["a", "b"]) == ["a", "b"])
check("a bare string does not iterate as characters",
      core.as_list("kimi") == ["kimi"], str(core.as_list("kimi")))
check("a comma joined string splits", core.as_list("kimi, glm") == ["kimi", "glm"])
check("a newline joined string splits",
      core.as_list("/a/one.py\n/b/two.py") == ["/a/one.py", "/b/two.py"])
check("a JSON array string parses", core.as_list('["kimi","glm"]') == ["kimi", "glm"])
check("None and empty give an empty list",
      core.as_list(None) == [] and core.as_list("") == [] and core.as_list("  ,  ") == [])

check("a clean value is untouched",
      core.strip_call_syntax("why does this hang?") == "why does this hang?")
check("an orphan closing tag is dropped",
      core.strip_call_syntax("the real context\n</invoke>\n") == "the real context",
      repr(core.strip_call_syntax("the real context\n</invoke>\n")))
check("a wrapping tag pair is unwrapped",
      core.strip_call_syntax("<question>what broke?</question>") == "what broke?")
check("a nested wrap is unwrapped",
      core.strip_call_syntax("<parameter><question>what broke?</question></parameter>")
      == "what broke?")
check("markup in the middle is content, not syntax",
      core.strip_call_syntax("does <div> need a closing tag here?")
      == "does <div> need a closing tag here?")
check("an unlisted tag is left alone",
      core.strip_call_syntax("<svg>circle</svg>") == "<svg>circle</svg>")

q, c, note = core.split_embedded_question("what broke?", "some background")
check("a well formed call passes through with no note",
      (q, c, note) == ("what broke?", "some background", None))

q, c, note = core.split_embedded_question(
    None,
    "SaidProof is a SaaS app.\n</context>\n"
    "<question>Is the plan sound?</question>\n</invoke>",
)
check("a question buried in context is recovered", q == "Is the plan sound?", repr(q))
check("the recovered context keeps the background", c == "SaidProof is a SaaS app.", repr(c))
check("the recovery is reported back to the caller", bool(note) and "question" in note)

q, c, note = core.split_embedded_question("", "background here\n\n## Question\n\nWhat should I do?")
check("a 'Question' heading in context is recovered", q == "What should I do?", repr(q))
check("the text above the heading stays as context", c == "background here", repr(c))

q, c, note = core.split_embedded_question(None, "just background, no question anywhere")
check("context with no question is never guessed at",
      q is None and c == "just background, no question anywhere" and note is None)

q, c, note = core.split_embedded_question(None, None)
check("an empty call is not recoverable", q is None and note is None)

try:
    core.build_messages(None, context="background with no question in it")
    check("an unrecoverable call is refused", False, "no error raised")
except core.OpenRouterError as exc:
    text = str(exc)
    check("an unrecoverable call is refused", True)
    check("the refusal names the argument and shows the shape",
          "question" in text and '"question":' in text and "XML" in text,
          text.splitlines()[0][:80])

msgs, notes = core.build_messages(
    None, context="<question>Recovered question here</question>", files=None,
)
check("a recovered question reaches the prompt",
      "Recovered question here" in msgs[-1]["content"])
check("the prompt carries the shape warning for the caller",
      any("own argument" in n for n in notes), str(notes)[:90])

# a bare string in `files` must not be read as one path per character
msgs, notes = core.build_messages("q", files="/definitely/not/a/real/path.py")
check("a bare string in files is treated as one path",
      sum(1 for n in notes if "not found" in n) == 1, str(notes)[:100])


# --------------------------------------------------------------------------
# categories: "ask an LLM that is good at coding" has to reach a real slug
# --------------------------------------------------------------------------

_cat_cfg = dict(
    core.load_config(),
    category_exclude_vendors=["openai", "anthropic", "google"],
    categories={
        "coding": {"models": ["moonshotai/kimi-k3", "z-ai/glm-5.3"],
                   "aka": ["code", "programming"], "why": "x", "measured": "2026-09-10"},
        "long_context": {"models": ["z-ai/glm-5.3"], "aka": ["long context", "whole codebase"],
                         "why": "x", "measured": "2026-09-10"},
        "retired": {"models": ["moonshotai/kimi-k9-retired"], "aka": [],
                    "why": "x", "measured": "x"},
        "banned": {"models": ["openai/gpt-6-astra", "z-ai/glm-5.3"], "aka": [],
                   "why": "x", "measured": "x"},
        "gone": {"models": ["openai/gpt-6-astra"], "aka": [], "why": "x", "measured": "x"},
    },
)
core._config_cache = _cat_cfg

check("a category name resolves", core.resolve_category("coding")[0] == "coding")
check("a category name is case and space insensitive",
      core.resolve_category("Long Context")[0] == "long_context")
check("an aka synonym resolves", core.resolve_category("programming")[0] == "coding")
check("a phrase lifted from the user resolves",
      core.resolve_category("something good at whole codebase work")[0] == "long_context",
      str(core.resolve_category("something good at whole codebase work")))
check("the longest matching label wins, not the first",
      core.resolve_category("long context")[0] == "long_context")
check("an unknown capability is not guessed at", core.resolve_category("underwater basket") is None)
check("an empty term is not a category", core.resolve_category("") is None)

slugs, notes = core.category_models("coding")
check("a category returns its models in order",
      slugs == ["moonshotai/kimi-k3", "z-ai/glm-5.3"], str(slugs))
check("a healthy category reports no notes", notes == [], str(notes))

slugs, notes = core.category_models("retired")
check("a retired pin self-heals to the closest live model",
      slugs == ["moonshotai/kimi-k3"], str(slugs))
check("the substitution is reported", any("no longer lists" in n for n in notes), str(notes))

slugs, notes = core.category_models("banned")
check("an excluded vendor is dropped from a category", slugs == ["z-ai/glm-5.3"], str(slugs))
check("dropping an excluded vendor is reported",
      any("excluded" in n for n in notes), str(notes))

try:
    core.category_models("gone")
    check("a category with nothing usable left is an error", False, "no error raised")
except core.OpenRouterError as exc:
    check("a category with nothing usable left is an error", True)
    check("that error names the category and the config file",
          "gone" in str(exc) and "models.json" in str(exc), str(exc)[:80])

try:
    core.category_models("underwater basket")
    check("an unknown category is refused", False, "no error raised")
except core.OpenRouterError as exc:
    check("an unknown category is refused", True)
    check("the refusal lists the real categories",
          "coding" in str(exc) and "list_llm_categories" in str(exc), str(exc)[:90])

check("every configured category is listed",
      {r["category"] for r in core.list_categories()} == set(_cat_cfg["categories"]))

core._config_cache = None  # back to the real packaged config

# The shipped config has to survive the same checks, since it is what runs.
_shipped = core.load_config()
_cats = _shipped.get("categories") or {}
check("the shipped config defines categories", len(_cats) >= 10, f"{len(_cats)} categories")
check("every shipped category names two models",
      all(len(core.as_list(c.get("models"))) == 2 for c in _cats.values()),
      str({k: len(core.as_list(v.get("models"))) for k, v in _cats.items()
           if len(core.as_list(v.get("models"))) != 2}))
_banned = set(core.excluded_vendors())
_offenders = [m for c in _cats.values() for m in core.as_list(c.get("models"))
              if m.split("/")[0].lower() in _banned]
check("no shipped category pins an excluded vendor", not _offenders, str(_offenders))
_same = [k for k, v in _cats.items()
         if len({m.split("/")[0] for m in core.as_list(v.get("models"))}) < 2]
check("every shipped category pairs two different vendors", not _same, str(_same))
check("every shipped category records its evidence and date",
      all(c.get("why") and c.get("measured") for c in _cats.values()))


# --------------------------------------------------------------------------
# attachments: a PDF, an image or a sound file rides along as an attachment
# rather than being transcribed into the prompt
# --------------------------------------------------------------------------

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)
PDF_TINY = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
WAV_TINY = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00" + b"\x00" * 20

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "shot.png").write_bytes(PNG_1PX)
    (root / "spec.pdf").write_bytes(PDF_TINY)
    (root / "clip.wav").write_bytes(WAV_TINY)
    (root / "app.py").write_text("print('hi')\n")
    # no extension at all: the magic bytes have to carry it
    (root / "screenshot").write_bytes(PNG_1PX)

    check("a png is classified as an image",
          core.classify_attachment(root / "shot.png") == ("image", "image/png"))
    check("a pdf is classified as a pdf",
          core.classify_attachment(root / "spec.pdf") == ("pdf", "application/pdf"))
    check("a wav is classified as audio",
          core.classify_attachment(root / "clip.wav") == ("audio", "wav"))
    check("source code is not an attachment",
          core.classify_attachment(root / "app.py") is None)
    check("magic bytes beat a missing extension",
          core.classify_attachment(root / "screenshot") == ("image", "image/png"))

    messages, notes = core.build_messages(
        "what is in these?",
        files=[str(root / "shot.png"), str(root / "spec.pdf"), str(root / "app.py")],
        model_slug="moonshotai/kimi-k3",
    )
    content = messages[-1]["content"]
    check("attachments turn the user message into content parts",
          isinstance(content, list), type(content).__name__)
    kinds = [part["type"] for part in content]
    check("the text part comes first", kinds[0] == "text")
    check("an image becomes an image_url part", "image_url" in kinds)
    check("a pdf becomes a file part", "file" in kinds)
    image_part = next(p for p in content if p["type"] == "image_url")
    check("the image is a base64 data url",
          image_part["image_url"]["url"].startswith("data:image/png;base64,"))
    file_part = next(p for p in content if p["type"] == "file")
    check("the pdf carries its filename", file_part["file"]["filename"] == "spec.pdf")
    check("the pdf is a base64 data url",
          file_part["file"]["file_data"].startswith("data:application/pdf;base64,"))
    check("source alongside an attachment is still inlined as text",
          "print('hi')" in content[0]["text"])
    check("the text part lists what was attached",
          "## Attached files" in content[0]["text"] and "shot.png" in content[0]["text"])

    summary = core.attachment_summary(messages)
    check("the attachment summary counts each kind",
          summary["image"] == 1 and summary["pdf"] == 1 and summary["total"] == 2,
          str(summary))
    check("text_chars ignores the base64 payload",
          core.text_chars(messages) < 2000, str(core.text_chars(messages)))

    # kimi's fake catalogue entry takes text and image but not audio
    _, notes = core.build_messages(
        "transcribe", files=[str(root / "clip.wav")], model_slug="moonshotai/kimi-k3",
    )
    check("audio is refused for a model that cannot take it",
          any("does not accept" in n for n in notes), str(notes)[:100])
    messages, _ = core.build_messages("transcribe", files=[str(root / "clip.wav")])
    audio = [p for p in messages[-1]["content"] if p["type"] == "input_audio"]
    check("with no model named, audio is attached anyway", len(audio) == 1)
    check("audio sends bare base64 and a format, not a data uri",
          audio and audio[0]["input_audio"]["format"] == "wav"
          and not audio[0]["input_audio"]["data"].startswith("data:"))

    # base64 is far bigger than the text cap, and must not be judged against it
    core._config_cache = dict(cfg, max_input_chars=2000)
    messages, _ = core.build_messages("q", files=[str(root / "shot.png")])
    check("an attachment is not counted against max_input_chars",
          isinstance(messages[-1]["content"], list))
    core._config_cache = cfg

    # byte ceilings, not character ceilings, are what bound an attachment
    core._config_cache = dict(cfg, max_attachment_bytes=10)
    _, notes = core.build_messages("q", files=[str(root / "shot.png")])
    check("an oversized attachment is skipped with a note",
          any("read ceiling" in n for n in notes), str(notes)[:100])
    core._config_cache = dict(cfg, max_attachments=1)
    messages, notes = core.build_messages(
        "q", files=[str(root / "shot.png"), str(root / "spec.pdf")],
    )
    check("the attachment count ceiling holds",
          any("attachment limit" in n for n in notes), str(notes)[:100])
    core._config_cache = cfg

    # a secret must not become sendable just by being binary
    (root / "id_rsa").write_bytes(PNG_1PX)
    _, notes = core.build_messages("q", files=[str(root / "id_rsa")])
    check("the denylist applies to attachments too",
          any("REFUSED" in n for n in notes), str(notes)[:100])


# ---- directories expand instead of being skipped ---------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "src").mkdir()
    (root / "src" / "one.py").write_text("one = 1")
    (root / "src" / "two.py").write_text("two = 2")
    (root / "src" / "node_modules").mkdir()
    (root / "src" / "node_modules" / "junk.js").write_text("junk")
    (root / "src" / ".git").mkdir()
    (root / "src" / ".git" / "HEAD").write_text("ref: refs/heads/main")

    messages, notes = core.build_messages("review this", files=[str(root / "src")])
    body = messages[-1]["content"]
    check("a directory is expanded, not skipped", "one = 1" in body and "two = 2" in body)
    check("node_modules is pruned", "junk" not in body)
    check(".git is pruned", "refs/heads/main" not in body)
    check("the expansion is reported", any("expanded directory" in n for n in notes))

    core._config_cache = dict(cfg, max_dir_files=1)
    _, notes = core.build_messages("review this", files=[str(root / "src")])
    check("max_dir_files bounds an expansion",
          any("more than 1 files" in n for n in notes), str(notes)[:120])
    core._config_cache = cfg



# --------------------------------------------------------------------------
# threads carry the document, not just a note that there was one
#
# Annotations alone do NOT put a file back in front of the model: OpenRouter's
# own example re-sends the file part and uses the annotations only to skip the
# parse. A live probe caught that the hard way, so it is pinned here.
# --------------------------------------------------------------------------

_FILE_PART = {
    "type": "file",
    "file": {"filename": "spec.pdf", "file_data": "data:application/pdf;base64,JVBERi0="},
}
_ANNOT = [{"type": "file", "file": {"hash": "abc", "name": "spec.pdf", "content": []}}]

core.save_thread("carry", "what is in it?", "a codeword", "m",
                 annotations=_ANNOT, attachments=[_FILE_PART])
_hist = core.load_thread("carry")
check("a carried attachment rides on the user turn",
      isinstance(_hist[0]["content"], list)
      and any(p.get("type") == "file" for p in _hist[0]["content"]))
check("the question text is still the first part of that turn",
      _hist[0]["content"][0] == {"type": "text", "text": "what is in it?"})
check("annotations ride on the assistant turn", _hist[1].get("annotations") == _ANNOT)

_msgs, _notes = core.build_messages("follow up question", history=_hist)
_user = [m for m in _msgs if m["role"] == "user"]
check("the replayed turn still carries the file part",
      any(p.get("type") == "file"
          for m in _user if isinstance(m["content"], list) for p in m["content"]))
check("the replayed assistant turn carries the annotations",
      any(m.get("annotations") == _ANNOT for m in _msgs if m["role"] == "assistant"))
check("carrying a document forward is reported",
      any("carried 1 earlier attachment" in n for n in _notes), str(_notes)[:110])

# a turn with nothing attached must stay a plain string, as before
core.save_thread("carry-none", "plain question", "plain answer", "m")
check("a turn with no attachment stays a plain string",
      core.load_thread("carry-none")[0]["content"] == "plain question")

# the cost guard has to see replayed parts too: they are re-sent and re-priced
check("attachment_summary counts replayed parts, not just new ones",
      core.attachment_summary(_msgs)["pdf"] == 1)
check("sent_attachments reports only what this call added",
      core.sent_attachments(_msgs) == [])


# ---- typed error codes become something the caller can act on -------------
_img_err = json.dumps({
    "error": {"code": 400, "message": "bad image",
              "metadata": {"error_type": "image_too_large"}},
})
check("an image_too_large error explains itself",
      "scale it down" in core._http_message(400, _img_err),
      core._http_message(400, _img_err)[:100])
_unknown = json.dumps({"error": {"code": 400, "message": "nope",
                                 "metadata": {"error_type": "something_new"}}})
check("an unrecognised error_type is still surfaced",
      "something_new" in core._http_message(400, _unknown))
check("a plain error still reports its status and message",
      "402" in core._http_message(402, '{"error":{"message":"no credits"}}'))



# --------------------------------------------------------------------------
# the cost guard, and config values that must never take a paid call down
# --------------------------------------------------------------------------

# With no cap from the config and none published by the model, the old code sent no
# max_tokens and priced zero output, so any prompt passed the guard and the provider billed
# to its own ceiling.
core._config_cache = dict(cfg, default_max_tokens=None, max_cost_usd_per_call=0.10)
try:
    core.ask("q", model="moonshotai/kimi-k3:batch")
    check("a missing output cap cannot price the answer at zero", False, "guard did not fire")
except core.OpenRouterError as exc:
    check("a missing output cap cannot price the answer at zero",
          "over the" in str(exc), str(exc)[:80])
except AssertionError:
    check("a missing output cap cannot price the answer at zero", False,
          "reached the network, so the guard was skipped")
core._config_cache = cfg

for _bad_key in ("max_file_chars", "max_input_chars", "default_max_tokens",
                 "catalog_ttl_s", "request_timeout_s", "max_cost_usd_per_call"):
    core._config_cache = dict(cfg, **{_bad_key: "not-a-number"})
    try:
        core.build_messages("q")
        core.estimate_call_cost("moonshotai/kimi-k3", 100, 100)
        check(f"a non-numeric {_bad_key} falls back instead of raising", True)
    except Exception as exc:
        check(f"a non-numeric {_bad_key} falls back instead of raising", False, repr(exc))
core._config_cache = cfg

# thread_max_messages is read AFTER the response has been billed, so a bad value there used
# to destroy an answer the user had already paid for.
with tempfile.TemporaryDirectory() as tmp:
    core.THREAD_DIR = Path(tmp) / "threads"
    core._config_cache = dict(cfg, thread_max_messages="lots")
    try:
        wrote = core.save_thread("paid", "q", "an answer already paid for", "m")
        check("a bad thread_max_messages cannot lose a paid answer", wrote is True,
              f"save_thread returned {wrote}")
        check("and the answer is actually there",
              any(m.get("content") == "an answer already paid for"
                  for m in core.load_thread("paid")))
    except Exception as exc:
        check("a bad thread_max_messages cannot lose a paid answer", False, repr(exc))
    core._config_cache = cfg

# an error delivered in a 200 body is still an error, and gets the typed explanation
core._request = lambda method, path, payload=None, timeout=60.0, retries=3: {
    "error": {"code": 429, "message": "upstream rate limited",
              "metadata": {"error_type": "rate_limited"}},
}
try:
    core.ask("q", model="kimi")
    check("an error inside a 200 body is raised, not read as an answer", False)
except core.OpenRouterError as exc:
    check("an error inside a 200 body is raised, not read as an answer",
          "429" in str(exc) and "rate limited" in str(exc), str(exc)[:90])
    check("and it carries the typed hint rather than a raw dump",
          "rate_limited" in str(exc) or "rate limited by OpenRouter" in str(exc), str(exc)[:90])

# spend is what was billed, not what answered: an empty completion logs ok: False with a cost
core._request = lambda method, path, payload=None, timeout=60.0, retries=3: {
    "data": {"label": "test-key", "usage": 1.0},
}
core.log_call({"model": "m", "ok": True, "cost_usd": 0.01})
core.log_call({"model": "m", "ok": False, "empty": True, "cost_usd": 0.02})
_usage = core.account_usage()
check("bridge spend counts a billed non-answer",
      abs(_usage["bridge_spend_usd"] - 0.03) < 1e-9, str(_usage["bridge_spend_usd"]))
check("and reports how many calls were billed", _usage["bridge_calls_billed"] == 2,
      str(_usage.get("bridge_calls_billed")))
core._request = _no_network


# --------------------------------------------------------------------------
# a recovered question must reach the transcript, and old damage must heal
# --------------------------------------------------------------------------

_answered = {
    "choices": [{"message": {"content": "the answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
}
with tempfile.TemporaryDirectory() as tmp:
    core.THREAD_DIR = Path(tmp) / "threads"
    core._request = lambda method, path, payload=None, timeout=60.0, retries=3: _answered
    core.ask(None, model="kimi", thread="recovered",
             context="Background.\n<question>Is the plan sound?</question>")
    _kept = core.load_thread("recovered")
    check("a question recovered from context is stored, not a null turn",
          [m["role"] for m in _kept] == ["user", "assistant"], str([m.get("role") for m in _kept]))
    check("and the stored question is the recovered text",
          _kept[0]["content"] == "Is the plan sound?", repr(_kept[0]["content"]))

    _png = Path(tmp) / "shot.png"
    _png.write_bytes(PNG_1PX)
    core.ask(None, model="kimi", thread="recovered-file", files=[str(_png)],
             context="<question>What is in this image?</question>")
    _first = core.load_thread("recovered-file")[0]["content"]
    check("an attached turn carries real question text, never null",
          isinstance(_first, list) and _first[0] == {"type": "text",
                                                     "text": "What is in this image?"},
          str(_first[0])[:80])

    # a transcript already damaged by the old bug repairs itself on load
    _damaged = core.THREAD_DIR / "damaged.json"
    core.THREAD_DIR.mkdir(parents=True, exist_ok=True)
    _damaged.write_text(json.dumps({"name": "damaged", "messages": [
        {"role": "user", "content": None},
        {"role": "assistant", "content": "orphaned reply"},
        {"role": "user", "content": [{"type": "text", "text": None}, _FILE_PART]},
        {"role": "assistant", "content": "reply about the file"},
    ]}))
    _healed = core.load_thread("damaged")
    check("a null content turn is dropped on load", len(_healed) == 3, str(len(_healed)))
    check("a null text part is dropped but the file part survives",
          _healed[1]["content"] == [_FILE_PART], str(_healed[1]["content"])[:80])
    core._request = _no_network

# ---- classification must not be fooled either way -------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "ids.csv").write_text("ID3,name,score\n1,alpha,10\n2,beta,20\n")
    check("a csv whose first column is ID3 is not an mp3",
          core.classify_attachment(root / "ids.csv") is None,
          str(core.classify_attachment(root / "ids.csv")))
    (root / "tagged.mp3").write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x21" + b"\x00" * 40)
    check("a real ID3v2 tag is still audio",
          core.classify_attachment(root / "tagged.mp3") == ("audio", "mp3"))
    (root / "untagged.mp3").write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 60)
    check("an mp3 with no tag is still audio, by extension",
          core.classify_attachment(root / "untagged.mp3") == ("audio", "mp3"))
    (root / "notes.mp3").write_text("these are meeting notes, not a sound file\n" * 3)
    check("a text file named .mp3 is not attached as corrupt audio",
          core.classify_attachment(root / "notes.mp3") is None)
    (root / "shot.png").write_bytes(PNG_1PX)
    check("a real png is unaffected",
          core.classify_attachment(root / "shot.png") == ("image", "image/png"))

# ---- the role fallback has to name the role it actually used --------------
core._config_cache = dict(cfg, roles={"reviewer": "REVIEWER PROMPT"})
_msgs, _notes = core.build_messages("q", role="nope")
check("the role fallback note names the role actually used",
      any("used 'reviewer'" in n for n in _notes), str(_notes)[:90])
check("and that is the prompt that was sent", _msgs[0]["content"] == "REVIEWER PROMPT")
core._config_cache = cfg

# ---- a panel asks one model once ------------------------------------------
_panel = core.ask_panel("q", models=["kimi", "moonshotai/kimi-k3"])
check("a panel does not bill the same model twice", len(_panel) == 1, str(len(_panel)))
check("and says why the duplicate was dropped",
      any("already on the panel" in n for n in _panel[0].get("notes") or []),
      str(_panel[0].get("notes"))[:90])
_mixed = core.ask_panel("q", models=["kimi", "no-such-model-xyz"])
check("an unresolvable model keeps its own slot", len(_mixed) == 2, str(len(_mixed)))
check("and is reported there rather than killing the panel",
      any("cannot resolve model" in str(r.get("error") or "").lower() for r in _mixed),
      str([r.get("error") for r in _mixed])[:90])


# --------------------------------------------------------------------------
# an outage must not cost a retry cycle per lookup, and stdin must stay bounded
# --------------------------------------------------------------------------

_attempts = {"n": 0}


def _failing_request(method, path, payload=None, timeout=60.0, retries=3):
    _attempts["n"] += 1
    raise core.OpenRouterError("network error calling OpenRouter: unreachable")


core.get_catalog = REAL_GET_CATALOG
core._catalog_cache = None
core._catalog_fetched_at = 0.0
core._catalog_failed_at = 0.0
core._request = _failing_request
_refusals = []
for _ in range(4):
    try:
        core.get_catalog()
    except core.OpenRouterError as exc:
        _refusals.append(str(exc))
check("a catalogue outage is attempted once per cooldown, not once per lookup",
      _attempts["n"] == 1, f"{_attempts['n']} network attempts for 4 lookups")
check("and the later refusals say the retry was skipped on purpose",
      len(_refusals) == 4 and "not retried" in _refusals[-1], _refusals[-1][:80])
core._request = _no_network
core._catalog_failed_at = 0.0
core._catalog_cache = FAKE
core._catalog_fetched_at = time.time()
core.get_catalog = lambda refresh=False, allow_stale=True: FAKE
check("the catalogue index follows a replaced cache rather than going stale",
      core._find("z-ai/glm-5.3").get("name") == "Z.AI: GLM 5.3")

from orask import cli as _cli


class _FakeStdin:
    def __init__(self, fd):
        self._fd = fd

    def isatty(self):
        return False

    def fileno(self):
        return self._fd


_saved_stdin = sys.stdin
_saved_deadline = _cli.STDIN_DEADLINE_S
_saved_max = _cli.STDIN_MAX_BYTES
_cli.STDIN_DEADLINE_S = 1.0

# a pipe whose writer never closes is what `yes | orask ask ...` is: the old code called
# sys.stdin.read() on it and never came back
_read_fd, _write_fd = os.pipe()
os.write(_write_fd, b"y\n" * 2000)
sys.stdin = _FakeStdin(_read_fd)
_started = time.monotonic()
try:
    _cli.read_stdin_safely(wait=0.05)
    check("a pipe that never closes refuses an incomplete prompt", False)
except core.OpenRouterError as exc:
    _elapsed = time.monotonic() - _started
    check("a pipe that never closes refuses an incomplete prompt",
          "ORASK_STDIN_DEADLINE" in str(exc) and _elapsed < 5, str(exc)[:100])
os.close(_read_fd)
os.close(_write_fd)

# and the byte ceiling holds
_read_fd, _write_fd = os.pipe()
os.write(_write_fd, b"z" * 4000)
sys.stdin = _FakeStdin(_read_fd)
_cli.STDIN_MAX_BYTES = 100
try:
    _cli.read_stdin_safely(wait=0.05)
    check("the stdin byte ceiling refuses truncation", False)
except core.OpenRouterError as exc:
    check("the stdin byte ceiling refuses truncation", "100-byte limit" in str(exc), str(exc)[:100])
os.close(_read_fd)
os.close(_write_fd)

sys.stdin = _saved_stdin
_cli.STDIN_DEADLINE_S = _saved_deadline
_cli.STDIN_MAX_BYTES = _saved_max


# --------------------------------------------------------------------------
# the safety overrides are not the calling agent's to set
# --------------------------------------------------------------------------

core._config_cache = cfg
for _key in ("mcp_allow_secret_files", "mcp_allow_expensive"):
    _allowed, _why = core.override_allowed(_key, True)
    check(f"{_key} is refused by default", _allowed is False)
    check(f"and {_key} says how to permit it",
          bool(_why) and _key in (_why or ""), (_why or "")[:70])
    core._config_cache = dict(cfg, **{_key: True})
    _allowed, _why = core.override_allowed(_key, True)
    check(f"{_key} is honoured once the config opts in", _allowed is True and _why is None)
    core._config_cache = cfg
    check(f"{_key} is silent when nothing was requested",
          core.override_allowed(_key, False) == (False, None))

# the paths a poisoned instruction would name
for _path, _label in (
    ("/proc/1234/environ", "a process environment"),
    ("/proc/1234/cmdline", "a process command line"),
    ("/home/u/.config/gcloud/application_default_credentials.json", "gcloud credentials"),
    ("/home/u/.gitconfig", "a git config"),
    ("/home/u/project/.git/config", "a repository git config"),
    ("/home/u/.pgpass", "a postgres password file"),
    ("/home/u/.config/gh/hosts.yml", "a github cli token store"),
    ("/home/u/infra/terraform.tfvars", "terraform variables"),
):
    check(f"{_label} is on the denylist",
          bool(core.denied_by_policy(Path(_path), core.DEFAULT_DENY_PATTERNS)), _path)

for _path in ("/proc/cpuinfo", "/proc/meminfo", "/home/u/project/config.py"):
    check(f"{_path} is still sendable",
          core.denied_by_policy(Path(_path), core.DEFAULT_DENY_PATTERNS) is None)


# ---- the OCR page charge is part of the estimate, not a surprise afterwards ----
_big_pdf = {"type": "file", "file": {"file_data": "data:application/pdf;base64,"
                                     + "A" * 8_000_000, "filename": "scan.pdf"}}
_summary = core.summarize_parts([_big_pdf])
check("the attachment summary reports pdf bytes", _summary["pdf_bytes"] > 5_000_000,
      str(_summary["pdf_bytes"]))

with tempfile.TemporaryDirectory() as tmp:
    _scan = Path(tmp) / "scan.pdf"
    _scan.write_bytes(PDF_TINY + b"\x00" * 3_000_000)
    core._config_cache = dict(cfg, max_cost_usd_per_call=1.0,
                              mistral_ocr_usd_per_1k_pages=200.0)
    try:
        core.ask("q", model="kimi", files=[str(_scan)], pdf_engine="mistral-ocr",
                 max_tokens=100)
        check("an ocr page charge is counted by the cost guard", False, "guard did not fire")
    except core.OpenRouterError as exc:
        check("an ocr page charge is counted by the cost guard",
              "over the" in str(exc), str(exc)[:80])
        check("and the refusal says the page charge is why",
              "mistral-ocr page charges" in str(exc), str(exc)[:110])
    except AssertionError:
        check("an ocr page charge is counted by the cost guard", False,
              "reached the network, so the charge was not priced")
    # the same file on the free engine must not be charged for pages
    core._request = lambda method, path, payload=None, timeout=60.0, retries=3: _answered
    _res = core.ask("q", model="kimi", files=[str(_scan)], pdf_engine="cloudflare-ai",
                    max_tokens=100)
    check("the free engine adds no page charge",
          not any("bills per page" in n for n in _res["notes"]), str(_res["notes"])[:90])
    core._config_cache = cfg  # back to the real per-page rate
    _res = core.ask("q", model="kimi", files=[str(_scan)], pdf_engine="mistral-ocr",
                    max_tokens=100)
    check("and the ocr estimate says it is inferred from the file size",
          any("upper bound" in n for n in _res["notes"]), str(_res["notes"])[:90])
    core._request = _no_network
    core._config_cache = cfg


# ---- the packaged roster has to be internally consistent ------------------
# A default_panel entry that is not a real alias costs a failed billed call to
# discover, and only for whoever runs a bare `orask panel` first.
_packaged = json.loads(core.PACKAGED_CONFIG.read_text())
_aliases = _packaged["aliases"]
check("every default_panel entry is a configured alias or a full slug",
      all(m in _aliases or "/" in m for m in _packaged["default_panel"]),
      str([m for m in _packaged["default_panel"] if m not in _aliases and "/" not in m]))
check("the default model is one of them",
      _packaged["default_model"] in _aliases or "/" in _packaged["default_model"])
check("every alias points at a full slug, not another alias",
      all("/" in v for v in _aliases.values()), str(_aliases))
check("the packaged aliases are the four in service",
      set(_aliases) == {"kimi", "glm", "grok", "gemini"}, str(sorted(_aliases)))
# category_exclude_vendors governs automatic category picks only. An alias is a
# deliberate choice and is not filtered by it, or the README's "a full slug still
# reaches those" would be false. The two lists are meant to overlap.
_excluded = tuple(f"{v}/" for v in core.excluded_vendors())
check("an alias may name a vendor the categories exclude",
      any(v.startswith(_excluded) for v in _aliases.values()),
      f"aliases {sorted(_aliases.values())} vs excluded {_excluded}")
check("and no category pins a model from an excluded vendor",
      not [m for row in core.list_categories() for m in row["models"]
           if m.startswith(_excluded)],
      str([m for row in core.list_categories() for m in row["models"]
           if m.startswith(_excluded)]))


# ---- plain English has to reach a category -------------------------------
# "Use the coding LLMs to ..." is how this is actually asked for. The whole
# phrase is passed through, so it has to resolve without the agent parsing it.
for _phrase, _want in [
    ("coding", "coding"),
    ("the coding LLMs", "coding"),
    ("Use the coding LLMs to refactor this module", "coding"),
    ("use the debugging llms", "debugging"),
    ("ask the reasoning models", "reasoning"),
    ("the math ones", "math"),
    ("something strong at long context", "long_context"),
    ("use the budget llms", "budget"),
]:
    _got = core.resolve_category(_phrase)
    check(f"'{_phrase}' resolves to {_want}", _got is not None and _got[0] == _want,
          str(_got[0] if _got else None))
check("a phrase matching no category is refused, not guessed",
      core.resolve_category("use the underwater basket weaving llms") is None)
check("every category names the evidence behind its pick",
      all(row["why"] and row["measured"] for row in core.list_categories()),
      str([r["category"] for r in core.list_categories() if not r["why"]]))


# ---- the tool list cannot drift from the tools that exist -----------------
# mcp_server cannot be imported here (that would need the mcp SDK, and the point
# of this suite is that it does not). The decorators are read as text instead.
_server_src = (ROOT / "src" / "orask" / "mcp_server.py").read_text()
_declared = re.findall(r'@mcp\.tool\(\s*\n\s*name="([a-z_]+)"', _server_src)
check("every @mcp.tool is in core.MCP_TOOLS",
      sorted(_declared) == sorted(core.MCP_TOOLS),
      f"decorators {sorted(_declared)} vs constant {sorted(core.MCP_TOOLS)}")
_install_src = (ROOT / "install.sh").read_text()
check("the installer derives the Codex tool list instead of repeating it",
      "from orask.core import MCP_TOOLS" in _install_src)
check("the live MCP protocol test expects the same set",
      sorted(re.findall(r'"([a-z_]+)"',
             (ROOT / "tests" / "test_mcp_stdio.py").read_text()
             .split("EXPECTED_TOOLS = {")[1].split("}")[0])) == sorted(core.MCP_TOOLS))


# ---- guides are local files, and the topic name is not to be trusted ------
_guides = core.list_guides()
check("the packaged guides are found", len(_guides) >= 5,
      ", ".join(g["topic"] for g in _guides))
check("every guide declares when to read it and a parseable verified date",
      all(g["triggers"] and not g["stale"] for g in _guides),
      ", ".join(g["topic"] for g in _guides if not (g["triggers"] and not g["stale"])))

_outline = core.guide_outline("bash")
check("an outline lists headings without returning the body",
      len(_outline["sections"]) > 3, f"{len(_outline['sections'])} sections")
check("every outline entry carries its own length, for budgeting a read",
      all(s["lines"] > 0 for s in _outline["sections"]))
check("the line count is the body, not the front matter",
      _outline["lines"] < len(Path(_outline["path"]).read_text().splitlines()))

_whole = core.read_guide("bash")["text"]
_part = core.read_guide("bash", section="Quoting")
check("a section read returns that heading only",
      _part["text"].startswith("## Quoting") and len(_part["text"]) < len(_whole),
      f"{len(_part['text'])} of {len(_whole)} chars")
check("a section match is case-insensitive and partial",
      core.read_guide("bash", section="quot")["section"] == "Quoting")
check("an empty section is the whole guide, not an outline",
      core.read_guide("bash", section="")["text"] == _whole)

# The topic comes from a tool argument, so it is hostile input.
for _bad in ("../../../etc/passwd", "/etc/passwd", "../README", "..", "a/b", "%2e%2e/x"):
    check(f"a guide path cannot escape the guides directory ({_bad})",
          core._guide_path(_bad) is None)

# _guide_path is not the only reader: list and search open files too, and a
# symlink planted in a guide directory must not be readable through any of them.
_gdir = SCRATCH / "guides-extra"
_gdir.mkdir(exist_ok=True)
(_gdir / "real.md").write_text("---\ntriggers: t\nverified: 2026-09-11\n---\n\n## A\nsecretword\n")
_secret = SCRATCH / "outside.md"
_secret.write_text("## Leaked\nsecretword-outside\n")
with contextlib.suppress(OSError, NotImplementedError):
    (_gdir / "escape.md").symlink_to(_secret)
_cfg_guides = dict(core.load_config(), guide_dirs=[str(_gdir)])
core._config_cache = _cfg_guides
_names = {r["topic"] for r in core.list_guides()}
check("a guide directory from config is indexed", "real" in _names, str(sorted(_names))[:80])
check("a symlink out of a guide directory is not listed", "escape" not in _names)
check("and it is not readable", core._guide_path("escape") is None)
check("and its contents cannot be reached through search",
      core.search_guides("secretword-outside")["total"] == 0)
(_gdir / "python.md").write_text(
    "---\ntriggers: local override\nverified: 2026-09-11\n---\n\n## Local\nmine\n")
check("a configured directory wins over the packaged copy of the same topic",
      core.read_guide("python")["path"].startswith(str(_gdir)),
      core.read_guide("python")["path"])
(_gdir / "python.md").unlink()
core._config_cache = cfg

try:
    core.read_guide("no-such-guide")
    check("an unknown guide is refused with the list of real ones", False)
except core.OpenRouterError as _exc:
    check("an unknown guide is refused with the list of real ones",
          "Available guides" in str(_exc), str(_exc)[:80])
try:
    core.read_guide("bash", section="no-such-section")
    check("an unknown section names the real sections", False)
except core.OpenRouterError as _exc:
    check("an unknown section names the real sections", "Sections:" in str(_exc),
          str(_exc)[:80])

# A heading inside a fenced code block is a sample, not a section. Getting this
# wrong puts phantom entries in the outline and ends a slice inside an example.
_fenced = SCRATCH / "fenced.md"
_fenced.write_text(
    "---\ntriggers: t\nverified: 2026-09-11\n---\n\n"
    "## Real one\nbody\n\n```md\n## Not a heading\n```\n\nmore body\n\n## Real two\ntail\n"
)
_body = core._guide_split(_fenced.read_text())[1]
_heads = [m.group(2) for _, m in core._guide_headings(_body)]
check("a heading inside a code fence is not a section",
      _heads == ["Real one", "Real two"], str(_heads))
_starts = core._guide_headings(_body)
check("and a fenced heading does not cut a section short",
      "more body" in "\n".join(_body.splitlines()[_starts[0][0]:_starts[1][0]]))

# A document that opens with a horizontal rule has no front matter, and its
# content must not be eaten as metadata.
_meta, _rule_body = core._guide_split("---\n\nIntro para\n\n---\n\nRest\n")
check("a horizontal rule is not mistaken for front matter",
      _meta == {} and "Intro para" in _rule_body, str(_meta))
_meta2, _ = core._guide_split("\ufeff---\ntriggers: t\nverified: 2026-01-01\n---\n\nbody\n")
check("a byte order mark does not hide the front matter",
      _meta2.get("verified") == "2026-01-01", str(_meta2))

_found = core.search_guides("pipefail")
check("search finds a rule and names the section holding it",
      any(h["topic"] == "bash" and h["section"] for h in _found["hits"]),
      str(_found["hits"][:1])[:100])
check("search matches heading text, not only body lines",
      core.search_guides("Heredocs")["total"] > 0)
_small = core.search_guides("the", limit=3)
check("a truncated search says so and still counts every match",
      _small["truncated"] and _small["total"] > len(_small["hits"]),
      f"{len(_small['hits'])} of {_small['total']}")
check("a truncated search is not filled from one guide alone",
      len({h["topic"] for h in core.search_guides("the", limit=6)["hits"]}) > 1)

# Front matter must never be handed to the model as if it were content.
check("front matter is stripped from the returned text",
      not _whole.lstrip().startswith("---"), _whole[:40])
check("the packaged guides directory is always searched",
      core.GUIDES_DIR.resolve() in core.guide_dirs())
check("a guide_dirs path is not comma-split the way files are",
      core.guide_dirs()[0] != Path("/nope"))

# A directory added through guide_dirs holds documents written for people, with
# no front matter. A bare filename gives an agent nothing to route on.
(_gdir / "plain.md").write_text("# Redis How-To and Best Practices\n\n## A\nbody\n")
core._config_cache = _cfg_guides
_plain = next(r for r in core.list_guides() if r["topic"] == "plain")
check("a file with no front matter is indexed by its title",
      _plain["triggers"] == "Redis How-To and Best Practices", _plain["triggers"])
check("and it is flagged as undated rather than silently trusted", _plain["stale"])
core._config_cache = cfg


# ---- audit: safety switches and numeric bounds fail closed -----------------
for _key in ("mcp_allow_secret_files", "mcp_allow_expensive"):
    for _value in ("false", "true", 1, [True]):
        core._config_cache = dict(cfg, **{_key: _value})
        check(f"{_key} requires JSON true ({_value!r})",
              core.override_allowed(_key, True)[0] is False)
for _value in (float("nan"), float("inf"), float("-inf")):
    core._config_cache = dict(cfg, max_cost_usd_per_call=_value, max_file_chars=_value)
    check(f"non-finite cost setting uses safe default ({_value})",
          core._float_setting("max_cost_usd_per_call", 1.0) == 1.0)
    try:
        check(f"non-finite integer setting uses safe default ({_value})",
              core._setting("max_file_chars", 200000) == 200000)
    except (ValueError, OverflowError):
        check(f"non-finite integer setting uses safe default ({_value})", False)
core._config_cache = cfg
for _pricing in ({}, {"prompt": "0", "completion": "nan"},
                 {"prompt": "0", "completion": "-1"}):
    _entry = dict(FAKE[0], pricing=_pricing)
    core.get_catalog = lambda refresh=False, allow_stale=True, entry=_entry: [entry]
    check(f"incomplete or invalid pricing is unknown ({_pricing})",
          core.estimate_call_cost(_entry["id"], 100, 100)[1] is False)
core.get_catalog = lambda refresh=False, allow_stale=True: FAKE
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    plain = root / "ordinary.txt"
    plain.write_text("DENIED_ALIAS_CONTENT")
    alias = root / ".env"
    alias.symlink_to(plain)
    messages, notes = core.build_messages("q", files=[str(alias)])
    check("denied original symlink name stays denied after resolving",
          "DENIED_ALIAS_CONTENT" not in str(messages) and any("REFUSED" in n for n in notes))
    relocated = root / "custom-config" / "env"
    relocated.parent.mkdir()
    relocated.write_text("RELOCATED_KEY_CONTENT")
    saved_env_file, core.ENV_FILE = core.ENV_FILE, relocated
    messages, notes = core.build_messages("q", files=[str(relocated)])
    check("relocated API key file is always denied",
          "RELOCATED_KEY_CONTENT" not in str(messages) and any("REFUSED" in n for n in notes))
    core.ENV_FILE = saved_env_file


# ---- audit: complete prices and explicit zero usage ------------------------
_entry = dict(FAKE[0], pricing={"prompt": "0", "completion": "0", "request": "2"})
core.get_catalog = lambda refresh=False, allow_stale=True, entry=_entry: [entry]
check("per-request price contributes to the cost guard",
      core.estimate_call_cost(_entry["id"], 100, 100) == (2.0, True))
core.get_catalog = lambda refresh=False, allow_stale=True: FAKE
check("reported zero cost is authoritative",
      core.actual_cost(FAKE[0]["id"], {"cost": 0, "prompt_tokens": 1000}) == 0)


# ---- audit: output exhaustion must not masquerade as a completed review ----
from unittest.mock import patch

for _content, _reasoning, _finish in (("", "unfinished analysis", "length"),
                                      ("partial findings", "analysis", "length"),
                                      ("", "reasoning only", "stop")):
    _response = {
        "choices": [{"message": {"content": _content, "reasoning": _reasoning},
                     "finish_reason": _finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 1000, "cost": 0.02,
                  "completion_tokens_details": {"reasoning_tokens": 900}},
    }
    with patch.object(core, "_request", return_value=_response), \
            patch.object(core, "save_thread") as _save:
        _result = core.ask("review", model="kimi", max_tokens=1000, thread="unfinished")
    check(f"unfinished reply is not success ({_content!r}, {_finish})",
          _result["ok"] is False and _result.get("incomplete") is True)
    check("reasoning never replaces the final answer", _result["answer"] == _content)
    check("unfinished reply retains usage and diagnostic recovery",
          _result["usage"]["cost_usd"] == 0.02
          and any("max_tokens" in n for n in _result["notes"]))
    check("unfinished reply is not saved as a completed thread turn", not _save.called)
with patch.object(core, "_request", return_value=_answered):
    _result = core.ask("review", model="kimi", max_tokens=1000)
check("a complete final answer remains successful",
      _result["ok"] and _result.get("incomplete") is False)
_info = core.model_info("kimi")
check("model discovery exposes bridge budget defaults and cost guard",
      _info.get("bridge_limits", {}).get("default_max_tokens") == cfg["default_max_tokens"]
      and _info.get("bridge_limits", {}).get("max_cost_usd_per_call")
      == cfg["max_cost_usd_per_call"])


# ---- MCP effort is enforced, not entrusted to caller prose -----------------
for _effort, _reason in (("low", None), ("minimal", None), ("none", None),
                         ("off", None), ("medium", None), ("medium", "   ")):
    with patch.object(core, "_request", return_value=_answered) as _transport:
        try:
            core.ask("q", model="kimi", effort=_effort, effort_reason=_reason, _mcp_call=True)
            check(f"MCP refuses disallowed effort {_effort!r}", False)
        except core.OpenRouterError:
            check(f"MCP refuses disallowed effort {_effort!r}", not _transport.called)
        except TypeError:
            check(f"MCP refuses disallowed effort {_effort!r}", False)
try:
    with patch.object(core, "_request", return_value=_answered) as _transport:
        _result = core.ask("q", model="kimi", _mcp_call=True)
    check("MCP defaults to max regardless of lower configured defaults",
          _transport.call_args.args[2]["reasoning"]["effort"] == "max")
except TypeError:
    check("MCP defaults to max regardless of lower configured defaults", False)
try:
    with patch.object(core, "_request", return_value=_answered) as _transport:
        core.ask("q", model="kimi", effort="medium", effort_reason="Bounded syntax check",
                 _mcp_call=True)
    check("justified medium never maps down to low",
          _transport.call_args.args[2]["reasoning"]["effort"] == "high")
except TypeError:
    check("justified medium never maps down to low", False)

# ---- audit: private, recoverable thread persistence ------------------------

with tempfile.TemporaryDirectory() as tmp:
    saved_thread_dir, core.THREAD_DIR = core.THREAD_DIR, Path(tmp) / "threads"
    old_umask = os.umask(0)
    try:
        core.save_thread("private name", "q", "a", "m")
    finally:
        os.umask(old_umask)
    path = core._thread_path("private name")
    check("new transcripts are private regardless of umask", path.stat().st_mode & 0o777 == 0o600)
    listed_name = core.list_threads()[0]["name"]
    check("listed thread names resume the same conversation",
          core.load_thread(listed_name) == core.load_thread("private name"))
    before = path.read_bytes()
    with patch.object(core.fcntl, "flock", side_effect=OSError("lock unavailable")):
        wrote = core.save_thread("private name", "q2", "a2", "m")
    check("failed lock cannot cause an unlocked write",
          wrote is False and path.read_bytes() == before)
    for blob in ([], {"messages": 3}, {"messages": [None, {}, {"content": "x"}]}):
        path.write_text(json.dumps(blob))
        try:
            check(f"corrupt thread shape is safely handled ({blob})",
                  core.load_thread("private name") == [])
        except (AttributeError, TypeError):
            check(f"corrupt thread shape is safely handled ({blob})", False)
    outside = Path(tmp) / "outside.json"
    outside.write_text(json.dumps({"messages": [{"role": "user", "content": "PRIVATE"}]}))
    path.unlink()
    path.symlink_to(outside)
    check("thread reads do not follow symlinks", core.load_thread("private name") == [])
    core.THREAD_DIR = saved_thread_dir
with tempfile.TemporaryDirectory() as tmp:
    saved_log, core.CALL_LOG = core.CALL_LOG, Path(tmp) / "calls.jsonl"
    old_umask = os.umask(0)
    try:
        core.log_call({"ok": True})
    finally:
        os.umask(old_umask)
    check("call logs are private regardless of umask",
          core.CALL_LOG.stat().st_mode & 0o777 == 0o600)
    core.CALL_LOG.write_text('[]\n42\n{"ok": true}\n')
    check("non-object log records are ignored", core.read_log() == [{"ok": True}])
    core.CALL_LOG = saved_log


# Persistence failure must leave the old bytes intact and remove temporary output.
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "record.json"
    target.write_text('{"original": true}')
    with patch.object(Path, "replace", side_effect=OSError("disk unavailable")):
        check("atomic replacement failure is reported",
              not core._write_json_atomic(target, {"new": True}))
    check("atomic failure preserves old data and removes temp files",
          target.read_text() == '{"original": true}' and list(Path(tmp).iterdir()) == [target])
    symlink = Path(tmp) / "link.json"
    symlink.symlink_to(target)
    check("atomic writes refuse symlink destinations",
          not core._write_json_atomic(symlink, {}) and target.read_text() == '{"original": true}')
    core.CALL_LOG = symlink
    core.log_call({"ok": True})
    check("logging cannot follow a planted symlink", target.read_text() == '{"original": true}')
    core.CALL_LOG = saved_log
    small = Path(tmp) / "small"
    small.write_bytes(b"a")
    with patch.object(core.os, "read", return_value=b"abc"):
        check("a growing file cannot exceed its read ceiling", core._slurp(small, 2)[1] is not None)
    directory = Path(tmp) / "alias"
    directory.symlink_to(Path(tmp), target_is_directory=True)
    check("reads refuse symlink ancestors after path selection",
          core._slurp(directory / "small", 20)[1] is not None)


print()
if FAILS:
    print(f"{len(FAILS)} of {CHECKS} checks failed: {', '.join(FAILS)}")
    sys.exit(1)
print(f"all {CHECKS} offline checks passed")
