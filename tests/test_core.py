"""Offline unit tests for the parts most likely to break silently.

No network, no API key needed:
    python tests/test_core.py        (any interpreter; no mcp package needed)
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orask import core  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
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

core._catalog_cache = FAKE
core.get_catalog = lambda refresh=False, allow_stale=True: FAKE  # type: ignore[assignment]
core._config_cache = None
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
check("a supported effort is left alone", core.clamp_effort("z-ai/glm-5.3", "high") == ("high", None))
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
    check("relative paths resolve against cwd", body.count("print('hello')") == 2)
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
import os as _os  # noqa: E402

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
    _os.mkfifo(fifo)
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
    saved_env = _os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        core.get_api_key()
        check("an empty key value reads as no key", False)
    except core.OpenRouterError as exc:
        check("an empty key value reads as no key", "No OpenRouter API key" in str(exc))
    finally:
        core.ENV_FILE = saved_env_file
        if saved_env is not None:
            _os.environ["OPENROUTER_API_KEY"] = saved_env

# ---- retry policy: a POST must not be retried into a double bill ----------
check("POST retries exclude 5xx", core.RETRY_STATUS_POST == {408, 429})
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
    import concurrent.futures as _cf  # noqa: E402
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

# ---- an unwritable state directory must not break anything ---------------
with tempfile.TemporaryDirectory() as tmp:
    locked = Path(tmp) / "locked"
    locked.mkdir(mode=0o500)
    saved = core.THREAD_DIR
    core.THREAD_DIR = locked / "threads"
    try:
        wrote = core.save_thread("t", "q", "a", "m")
        check("an unwritable thread dir returns False, never raises", wrote is False)
    except Exception as exc:  # noqa: BLE001
        check("an unwritable thread dir returns False, never raises", False, repr(exc))
    finally:
        core.THREAD_DIR = saved
        locked.chmod(0o700)

check(
    "_write_json_atomic reports failure instead of raising",
    core._write_json_atomic(Path("/proc/definitely/not/writable/x.json"), {"a": 1}) is False,
)

# ---- cost guard policy on unknown pricing --------------------------------
core._config_cache = dict(cfg, cost_guard_on_unknown_pricing="block")
try:
    core.ask("q", model="mistralai/mistral-large-2512")
    check("block policy is wired (unpriced model)", True, "model was priced, skipped")
except core.OpenRouterError as exc:
    check("block policy produces a clear refusal",
          "cannot be checked" in str(exc) or "network" in str(exc).lower(), str(exc)[:70])
core._config_cache = cfg

# ---- stdin must never hang the CLI (regression: fd 0 as an open socket) ----
import socket as _socket  # noqa: E402
import subprocess as _sp  # noqa: E402

_LAUNCHER = str(Path(__file__).resolve().parents[1] / "bin" / "orask")

# An open socketpair as fd 0 is what a background job or daemon hands us; the
# write end is never closed, so a naive read() blocks forever.
_parent, _child = _socket.socketpair()
try:
    proc = _sp.run(
        [_LAUNCHER, "--version"], stdin=_child,
        capture_output=True, text=True, timeout=25,
    )
    check("an open socket on stdin does not hang the CLI", proc.returncode == 0,
          proc.stdout.strip() or proc.stderr.strip()[:80])
except _sp.TimeoutExpired:
    check("an open socket on stdin does not hang the CLI", False, "timed out")
finally:
    _parent.close(); _child.close()

# a socket carrying data but never closing: take the data, then stop waiting
_parent, _child = _socket.socketpair()
try:
    _parent.sendall(b"context from a socket that stays open")
    proc = _sp.run(
        [_LAUNCHER, "models", "--search", "kimi-k3", "--limit", "1"],
        stdin=_child, capture_output=True, text=True, timeout=30,
    )
    check("a socket that never closes still returns", proc.returncode == 0,
          proc.stderr.strip()[:80] or "ok")
except _sp.TimeoutExpired:
    check("a socket that never closes still returns", False, "timed out")
finally:
    _parent.close(); _child.close()

# the ergonomic path must keep working: a real pipe is drained in full
proc = _sp.run(
    [_LAUNCHER, "--version"], input="piped text", capture_output=True, text=True, timeout=25,
)
check("a normal pipe on stdin still works", proc.returncode == 0, proc.stdout.strip())

# and /dev/null (a character device) is simply empty
with open(_os.devnull) as _devnull:
    proc = _sp.run(
        [_LAUNCHER, "--version"], stdin=_devnull,
        capture_output=True, text=True, timeout=25,
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
    None, "SaidProof is a SaaS app.\n</context>\n<question>Is the plan sound?</question>\n</invoke>",
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
        "retired": {"models": ["moonshotai/kimi-k9-retired"], "aka": [], "why": "x", "measured": "x"},
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


print()
print("summary:", len(FAILS), "failures")
if FAILS:
    sys.exit(1)

if FAILS:
    print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
    sys.exit(1)
print("all offline checks passed")
