#!/usr/bin/env python3
"""Hermetic CLI regressions; every child blocks network and uses scratch runtime paths."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, good: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if good else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not good:
        FAILURES.append(label)


# The subprocess exercises argparse, main(), context collection and output rendering. A
# successful call echoes its arguments at the provider boundary rather than buying an answer.
BOOTSTRAP = """
import json, socket, sys
from orask import cli, core

def no_network(*args, **kwargs):
    raise AssertionError('network is forbidden in CLI tests')

socket.create_connection = no_network
core._request = no_network

def answer(question, **kwargs):
    print('PROVIDER_CALLED', file=sys.stderr)
    return {'ok': True, 'model': 'test/model', 'answer': question,
            'context': kwargs.get('context'), 'usage': {'cost_usd': 0}}

core.ask = answer
core.ask_panel = lambda question, **kwargs: [answer(question, **kwargs)]
"""


def run_cli(
    argv: list[str],
    *,
    setup: str = "",
    data: str | None = None,
    stdin=None,
    extra_env: dict[str, str] | None = None,
    scratch: str | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(scratch or temporary)
        env = dict(
            os.environ,
            PYTHONPATH=str(ROOT / "src"),
            HOME=str(base),
            ORASK_CONFIG_DIR=str(base / "config"),
            ORASK_STATE_DIR=str(base / "state"),
            ORASK_CACHE_DIR=str(base / "cache"),
            OPENROUTER_API_KEY="",
        )
        env.pop("CODEX_HOME", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ORASK_STDIN_WAIT", None)
        env.pop("ORASK_STDIN_DEADLINE", None)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, "-c", BOOTSTRAP + setup + "\nraise SystemExit(cli.main())", *argv],
            input=data,
            stdin=stdin,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            check=False,
        )


def payload(proc: subprocess.CompletedProcess[str]):
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


# ---- machine-readable results and argument routing ------------------------
proc = run_cli(["panel", "question", "-C", "coding", "--json"], data="context")
check(
    "category panel JSON is one parseable JSON value",
    proc.returncode == 0 and isinstance(payload(proc), list),
    proc.stdout[:80],
)

proc = run_cli(["question", "--json", "-c", "explicit"], data="piped")
check(
    "shorthand ask combines explicit and piped context",
    proc.returncode == 0 and (payload(proc) or {}).get("context") == "explicit\n\npiped",
)

# ---- every descriptor shape exercises ask, which actually reads stdin ----
with tempfile.TemporaryFile() as source:
    source.write("file context π".encode())
    source.seek(0)
    proc = run_cli(["ask", "question", "--json"], stdin=source)
check(
    "regular redirected stdin reaches the question intact",
    proc.returncode == 0 and (payload(proc) or {}).get("context") == "file context π",
)

with open(os.devnull) as source:
    proc = run_cli(["ask", "question", "--json"], stdin=source)
check(
    "character device /dev/null is empty context",
    proc.returncode == 0 and (payload(proc) or {}).get("context") is None,
)

for body in (b"", b"socket context"):
    writer, reader = socket.socketpair()
    try:
        if body:
            writer.sendall(body)
        started = time.monotonic()
        proc = run_cli(
            ["ask", "question", "--json"], stdin=reader, extra_env={"ORASK_STDIN_WAIT": "0.05"}
        )
        check(
            f"open socket with {len(body)} bytes returns after its idle wait",
            proc.returncode == 0
            and time.monotonic() - started < 2
            and (payload(proc) or {}).get("context") == (body.decode() or None),
        )
    finally:
        writer.close()
        reader.close()

writer, reader = socket.socketpair()
try:
    started = time.monotonic()
    proc = run_cli(
        ["ask", "question", "--json"],
        stdin=reader,
        extra_env={"ORASK_STDIN_WAIT": "2", "ORASK_STDIN_DEADLINE": "0.05"},
    )
    check(
        "socket wait cannot exceed the total deadline",
        proc.returncode == 0 and time.monotonic() - started < 1,
    )
finally:
    writer.close()
    reader.close()

# ---- hard limits fail before the provider sees an incomplete prompt -------
for command in ("ask", "panel"):
    for body, succeeds in (
        ("x" * 64, True),
        ("x" * 65, False),
        ("π" * 32, True),
        ("π" * 33, False),
    ):
        proc = run_cli(
            [command, "question", "--json"], data=body, setup="\ncli.STDIN_MAX_BYTES = 64\n"
        )
        check(
            f"{command} {'accepts exact' if succeeds else 'refuses over'} stdin byte limit "
            f"({len(body)} characters, {len(body.encode())} bytes)",
            (proc.returncode == 0 and "PROVIDER_CALLED" in proc.stderr)
            if succeeds
            else (
                proc.returncode == 1
                and "PROVIDER_CALLED" not in proc.stderr
                and "stdin" in proc.stderr
                and "64" in proc.stderr
            ),
        )

read_fd, write_fd = os.pipe()


def slow_producer() -> None:
    try:
        os.write(write_fd, b"first ")
        time.sleep(0.15)
        os.write(write_fd, b"second")
    finally:
        os.close(write_fd)


producer = threading.Thread(target=slow_producer)
producer.start()
try:
    proc = run_cli(
        ["ask", "question", "--json"],
        stdin=read_fd,
        extra_env={"ORASK_STDIN_WAIT": "0.01", "ORASK_STDIN_DEADLINE": "1"},
    )
    check(
        "slow pipe producer is fully drained beyond the socket idle wait",
        proc.returncode == 0 and (payload(proc) or {}).get("context") == "first second",
    )
finally:
    producer.join(timeout=2)
    os.close(read_fd)

for body in (b"", b"incomplete context"):
    read_fd, write_fd = os.pipe()
    try:
        if body:
            os.write(write_fd, body)
        proc = run_cli(
            ["ask", "question", "--json"], stdin=read_fd, extra_env={"ORASK_STDIN_DEADLINE": "0.05"}
        )
        check(
            f"unclosed pipe with {len(body)} bytes refuses at deadline before calling provider",
            proc.returncode == 1
            and "PROVIDER_CALLED" not in proc.stderr
            and "ORASK_STDIN_DEADLINE" in proc.stderr,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

for name in ("ORASK_STDIN_WAIT", "ORASK_STDIN_DEADLINE"):
    for value in ("invalid", "nan", "inf", "-1", "0"):
        proc = run_cli(["--version"], data="", extra_env={name: value})
        check(f"{name}={value} cannot crash CLI import", proc.returncode == 0)
        proc = run_cli(["ask", "question", "--json"], data="context", extra_env={name: value})
        check(
            f"{name}={value} yields actionable refusal before provider call",
            proc.returncode == 1
            and name in proc.stderr
            and "Traceback" not in proc.stderr
            and "PROVIDER_CALLED" not in proc.stderr,
        )

proc = run_cli(
    ["ask", "question", "--json"], data="context", extra_env={"ORASK_STDIN_DEADLINE": "1e308"}
)
check(
    "platform timeout overflow is actionable and cannot reach provider",
    proc.returncode == 1
    and "ORASK_STDIN_DEADLINE" in proc.stderr
    and "Traceback" not in proc.stderr
    and "PROVIDER_CALLED" not in proc.stderr,
)

# ---- maintenance must prove live availability, not cached availability ----
proc = run_cli(
    ["doctor"],
    data="",
    setup="""
core.get_api_key = lambda: 'test-key'
def catalog(refresh=False, allow_stale=True):
    if allow_stale:
        return [{'id': 'test/cached'}]
    raise core.OpenRouterError('live catalogue offline')
core.get_catalog = catalog
core.account_usage = lambda: {}
""",
)
check(
    "doctor cannot report reachable using a stale fallback",
    proc.returncode == 1
    and "[FAIL] OpenRouter catalogue reachable" in proc.stdout
    and "live catalogue offline" in proc.stdout,
)

for args in (["categories", "--verify"], ["categories", "--verify", "--json"]):
    for available, excluded in ((False, False), (True, True), (True, False)):
        proc = run_cli(
            args,
            data="",
            setup=f"""
core.list_categories = lambda: [{{'category': 'test', 'models': ['test/model'],
    'aka': [], 'why': 'fixture', 'measured': '2026-09-12'}}]
core.verify_categories = lambda: [{{'category': 'test', 'slug': 'test/model',
    'available': {available}, 'excluded_vendor': {excluded}}}]
""",
        )
        expected = 0 if available and not excluded else 1
        check(
            f"category verification exit status reflects availability={available}, "
            f"excluded={excluded}, json={'--json' in args}",
            proc.returncode == expected,
        )

# ---- malformed historical records must not take down text rendering -------
for timestamp in (None, "bad", "nan", "inf", 1e308, {}, [], True):
    for command in ("log", "threads"):
        proc = run_cli(
            [command],
            data="",
            setup=f"""
core.read_log = lambda **kwargs: [{{'ts': {timestamp!r}, 'ok': True,
    'model': 'fixture/model', 'prompt_tokens': 'bad', 'completion_tokens': [], 'cost_usd': 0}}]
core.list_threads = lambda: [{{'name': 'fixture', 'messages': 2, 'updated_at': {timestamp!r}}}]
""",
        )
        check(
            f"{command} renders invalid timestamp {timestamp!r} as unknown",
            proc.returncode == 0 and "?" in proc.stdout and "Traceback" not in proc.stderr,
        )

for cost in ("bad", "nan", "inf", None, True, -1, {}, []):
    proc = run_cli(
        ["log"],
        data="",
        setup=f"""
core.read_log = lambda **kwargs: [{{'ts': 0, 'model': 'fixture/model', 'ok': True,
    'cost_usd': {cost!r}}}, {{'ts': 0, 'model': 'fixture/paid', 'ok': False, 'cost_usd': 0.25}}]
""",
    )
    check(
        f"log preserves known billed cost and labels invalid cost {cost!r}",
        proc.returncode == 0
        and "$?" in proc.stdout
        and "$0.2500" in proc.stdout
        and "unknown" in proc.stdout
        and "Traceback" not in proc.stderr,
    )

# ---- registration checks inspect parsed config, never similarly named text ----
DOCTOR_SETUP = """
core.get_api_key = lambda: 'test-key'
core.get_catalog = lambda **kwargs: [{'id': 'test/model'}]
core._config_cache = {'aliases': {}}
core.account_usage = lambda: {}
"""


def doctor_fixture(claude, codex, *, extra_env=None, setup=""):
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch)
        if claude is not None:
            (base / ".claude.json").write_text(claude)
        (base / ".codex").mkdir()
        if codex is not None:
            (base / ".codex/config.toml").write_text(codex)
        return run_cli(
            ["doctor"], data="", setup=DOCTOR_SETUP + setup, scratch=scratch, extra_env=extra_env
        )


SERVER = {
    "command": str(ROOT / "bin/openrouter-mcp"),
    "args": [],
    "env": {"ORASK_PYTHON": sys.executable},
}
CLAUDE_CONFIG = json.dumps({"mcpServers": {"openrouter": SERVER}})
CODEX_CONFIG = (
    "[mcp_servers.openrouter]\n"
    f"command = {json.dumps(SERVER['command'])}\nargs = []\n"
    f"env = {{ ORASK_PYTHON = {json.dumps(sys.executable)} }}\n"
)


def doctor_ready(proc):
    # Full TOML parsing is stdlib-only on 3.11+. The 3.10 contract is an explicit
    # unavailable result, never a false claim that its registration was verified.
    if sys.version_info < (3, 11):
        return proc.returncode == 1 and "TOML validation is unavailable" in proc.stdout
    return proc.returncode == 0


proc = doctor_fixture(CLAUDE_CONFIG, CODEX_CONFIG)
check(
    "Codex allowlist check succeeds or explicitly requires a TOML parser",
    doctor_ready(proc),
    proc.stdout[-100:],
)

for name, body in (
    ("quoted header", CODEX_CONFIG.replace("mcp_servers.openrouter", '"mcp_servers"."openrouter"')),
    ("commented header", CODEX_CONFIG.replace("]\n", "] # managed\n", 1)),
    (
        "multiline allowlist",
        CODEX_CONFIG
        + "enabled_tools = [\n"
        + "\n".join(
            f"{json.dumps(tool)},"
            for tool in (
                "ask_llm",
                "ask_panel",
                "get_consultation",
                "list_llm_categories",
                "list_llm_models",
                "llm_model_info",
                "openrouter_usage",
                "read_guide",
            )
        )
        + "\n]\n",
    ),
):
    proc = doctor_fixture(CLAUDE_CONFIG, body)
    check(
        f"doctor validates Codex {name} or reports unavailable parser",
        doctor_ready(proc),
        proc.stdout[-100:],
    )

CODEX_WITH_TOOLS = (
    CODEX_CONFIG
    + "enabled_tools = "
    + json.dumps(
        [
            "ask_llm",
            "ask_panel",
            "get_consultation",
            "list_llm_categories",
            "list_llm_models",
            "llm_model_info",
            "openrouter_usage",
            "read_guide",
        ]
    )
    + "\n"
)
for name, claude, codex in (
    ("missing files", None, None),
    ("missing Claude server", "{}", CODEX_WITH_TOOLS),
    ("non-object Claude config", "[]", CODEX_WITH_TOOLS),
    ("non-object Claude servers", '{"mcpServers": ["openrouter"]}', CODEX_WITH_TOOLS),
    ("non-object Claude server", '{"mcpServers": {"openrouter": []}}', CODEX_WITH_TOOLS),
    (
        "stale Claude command",
        CLAUDE_CONFIG.replace("bin/openrouter-mcp", "bin/old-server"),
        CODEX_WITH_TOOLS,
    ),
    (
        "stale Codex command",
        CLAUDE_CONFIG,
        CODEX_WITH_TOOLS.replace("bin/openrouter-mcp", "bin/old-server"),
    ),
    (
        "stale Codex interpreter",
        CLAUDE_CONFIG,
        CODEX_WITH_TOOLS.replace(sys.executable, "/missing/python"),
    ),
    (
        "nonempty launcher arguments",
        CLAUDE_CONFIG,
        CODEX_WITH_TOOLS.replace("args = []", 'args = ["x"]'),
    ),
    ("missing Codex server", CLAUDE_CONFIG, "[mcp_servers.other]\ncommand = 'other'\n"),
    ("comment-only Codex server", CLAUDE_CONFIG, "# [mcp_servers.openrouter]\n"),
    ("malformed Codex config", CLAUDE_CONFIG, "[mcp_servers.openrouter]\ncommand = [\n"),
    ("disabled Codex server", CLAUDE_CONFIG, CODEX_WITH_TOOLS + "enabled = false\n"),
    ("empty allowlist", CLAUDE_CONFIG, CODEX_CONFIG + "enabled_tools = []\n"),
    ("disabled bridge tool", CLAUDE_CONFIG, CODEX_WITH_TOOLS + 'disabled_tools = ["ask_llm"]\n'),
    ("invalid allowlist type", CLAUDE_CONFIG, CODEX_CONFIG + 'enabled_tools = "ask_llm"\n'),
    ("fake table inside multiline text", CLAUDE_CONFIG, "text = '''" + CODEX_WITH_TOOLS + "'''\n"),
):
    proc = doctor_fixture(claude, codex)
    check(
        f"doctor reports {name} as failed without a traceback",
        proc.returncode == 1 and "[FAIL]" in proc.stdout and "Traceback" not in proc.stderr,
        proc.stdout[-100:] or proc.stderr[:100],
    )

proc = doctor_fixture(CLAUDE_CONFIG, CODEX_CONFIG, setup="\nsys.modules['tomllib'] = None\n")
check(
    "doctor reports unavailable TOML parser without breaking Python 3.10 CLI",
    proc.returncode == 1 and "TOML" in proc.stdout and "Traceback" not in proc.stderr,
)

with tempfile.TemporaryDirectory() as scratch:
    base = Path(scratch)
    claude_dir = base / "claude-custom"
    codex_dir = base / "codex-custom"
    claude_dir.mkdir()
    codex_dir.mkdir()
    (claude_dir / ".claude.json").write_text(CLAUDE_CONFIG)
    (codex_dir / "config.toml").write_text(CODEX_WITH_TOOLS)
    proc = run_cli(
        ["doctor"],
        data="",
        scratch=scratch,
        setup=DOCTOR_SETUP,
        extra_env={"CLAUDE_CONFIG_DIR": str(claude_dir), "CODEX_HOME": str(codex_dir)},
    )
check("doctor inspects relocated paths or reports unavailable TOML parser", doctor_ready(proc))

# ---- usage accounting consumes real scratch logs and a fake free /key response ----
USAGE_SETUP = """
core._request = lambda *args, **kwargs: {'data': {
    'label': 'fixture', 'usage': 0, 'limit': None, 'limit_remaining': None, 'is_free_tier': False}}
core.CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
"""

for bad in (None, [], 42, "PRIVATE_PROVIDER_DATA"):
    proc = run_cli(
        ["usage", "--json"],
        data="",
        setup=USAGE_SETUP
        + f"""
core._request = lambda *args, **kwargs: {{'data': {bad!r}}}
""",
    )
    check(
        f"usage refuses non-object provider data {type(bad).__name__}",
        proc.returncode == 1
        and "key" in proc.stderr
        and "Traceback" not in proc.stderr
        and "PRIVATE_PROVIDER_DATA" not in proc.stderr,
    )

proc = run_cli(["usage", "--json"], data="", setup=USAGE_SETUP)
usage = payload(proc) or {}
check(
    "usage preserves explicit zero, unlimited credits, and false free-tier status",
    proc.returncode == 0
    and usage.get("account_usage_usd") == 0
    and usage.get("credit_limit_usd") is None
    and usage.get("credit_remaining_usd") is None
    and usage.get("free_tier") is False,
)

for bad in ("bad", "nan", "inf", True, -1, {}, []):
    proc = run_cli(
        ["usage", "--json"],
        data="",
        setup=USAGE_SETUP
        + f"""
core._request = lambda *args, **kwargs: {{'data': {{'usage': {bad!r}, 'limit': {bad!r},
    'limit_remaining': 'nan', 'is_free_tier': 'false', 'label': {{'private': 'payload'}}}}}}
""",
    )
    usage = payload(proc) or {}
    check(
        f"usage labels invalid account amounts {bad!r} instead of forwarding them",
        proc.returncode == 0
        and usage.get("account_usage_usd") is None
        and usage.get("credit_limit_usd") is None
        and usage.get("credit_remaining_usd") is None
        and usage.get("free_tier") is None
        and usage.get("key_label") is None
        and bool(usage.get("notes")),
    )

proc = run_cli(
    ["usage", "--json"],
    data="",
    setup=USAGE_SETUP
    + """
core._request = lambda *args, **kwargs: {'data': {'usage': 2.5, 'limit': 1,
    'limit_remaining': -1.5, 'is_free_tier': False}}
""",
)
check(
    "usage retains a finite negative credit remainder as debt",
    proc.returncode == 0 and (payload(proc) or {}).get("credit_remaining_usd") == -1.5,
)

for bad in ("bad", "nan", "inf", True, -1, None, {}, []):
    proc = run_cli(
        ["usage", "--json"],
        data="",
        setup=USAGE_SETUP
        + f"""
rows = [{{'ts': core.time.time(), 'ok': True, 'cost_usd': {bad!r}}},
        {{'ts': core.time.time(), 'ok': False, 'cost_usd': 0.25}}]
core.CALL_LOG.write_text(''.join(json.dumps(row) + '\\n' for row in rows))
""",
    )
    usage = payload(proc) or {}
    check(
        f"usage totals known billed failures and labels unknown cost {bad!r}",
        proc.returncode == 0
        and usage.get("bridge_spend_usd") == 0.25
        and usage.get("bridge_spend_last_24h_usd") == 0.25
        and usage.get("bridge_calls_billed") == 1
        and usage.get("bridge_costs_unknown") == 1,
    )

for bad in ("bad", "nan", "inf", True, -1, None, 1e308):
    proc = run_cli(
        ["usage", "--json"],
        data="",
        setup=USAGE_SETUP
        + f"""
core.CALL_LOG.write_text(json.dumps({{'ts': {bad!r}, 'ok': True, 'cost_usd': 0.25}}) + '\\n')
""",
    )
    usage = payload(proc) or {}
    check(
        f"usage cannot put invalid timestamp {bad!r} into the last day",
        proc.returncode == 0
        and usage.get("bridge_spend_usd") == 0.25
        and usage.get("bridge_spend_last_24h_usd") == 0
        and usage.get("bridge_timestamps_unknown") == 1,
    )

proc = run_cli(
    ["usage", "--json"],
    data="",
    setup=USAGE_SETUP
    + """
core.CALL_LOG.write_text(json.dumps({'ok': True, 'cost_usd': 0.25}) + '\\n'
    + (json.dumps({'ok': True, 'cost_usd': 0}) + '\\n') * 100000)
""",
)
usage = payload(proc) or {}
check(
    "usage reads more than 100000 compact records when all fit the byte window",
    proc.returncode == 0
    and usage.get("bridge_calls_logged") == 100001
    and usage.get("bridge_spend_usd") == 0.25
    and usage.get("bridge_spend_covers") == "every logged call",
)

proc = run_cli(
    ["usage", "--json"],
    data="",
    setup=USAGE_SETUP
    + """
line = json.dumps({'cost_usd': 1}) + '\\n'
core.MAX_LOG_WINDOW_BYTES = len(line.encode()) * 2
core.CALL_LOG.write_text(line * 3)
""",
)
usage = payload(proc) or {}
check(
    "usage includes a complete record exactly at its truncated window boundary",
    proc.returncode == 0
    and usage.get("bridge_spend_usd") == 2
    and "most recent" in usage.get("bridge_spend_covers", ""),
)

proc = run_cli(
    ["usage", "--json"],
    data="",
    setup=USAGE_SETUP
    + """
core.CALL_LOG.write_text('broken\\n[]\\n' + json.dumps({'ok': True, 'cost_usd': 0.25}) + '\\n')
""",
)
usage = payload(proc) or {}
check(
    "usage discloses skipped malformed log records",
    proc.returncode == 0
    and usage.get("bridge_spend_usd") == 0.25
    and usage.get("bridge_log_records_skipped") == 2
    and bool(usage.get("notes")),
)

proc = run_cli(
    ["usage", "--json"],
    data="",
    setup=USAGE_SETUP
    + """
core.CALL_LOG.symlink_to(core.PROJECT_ROOT / 'README.md')
""",
)
usage = payload(proc) or {}
check(
    "usage does not claim full accounting when the log cannot be safely read",
    proc.returncode == 0
    and usage.get("bridge_spend_covers") != "every logged call"
    and bool(usage.get("notes")),
)

proc = run_cli(
    ["usage", "--json"],
    data="",
    setup=USAGE_SETUP
    + """
core.CALL_LOG.write_text((json.dumps({'cost_usd': 1e308}) + '\\n') * 2)
""",
)
usage = payload(proc) or {}
check(
    "usage reports an overflowing total as unknown instead of Infinity",
    proc.returncode == 0
    and usage.get("bridge_spend_usd") is None
    and bool(usage.get("notes"))
    and "Infinity" not in proc.stdout,
)

print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} CLI checks passed")
raise SystemExit(bool(FAILURES))
