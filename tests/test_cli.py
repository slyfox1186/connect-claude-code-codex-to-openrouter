#!/usr/bin/env python3
"""Hermetic CLI regressions; every child blocks network and uses scratch runtime paths."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
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


def run_cli(argv: list[str], *, setup: str = "", data: str | None = None,
            stdin=None, extra_env: dict[str, str] | None = None,
            scratch: str | None = None) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(scratch or temporary)
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), HOME=str(base),
                   ORASK_CONFIG_DIR=str(base / "config"),
                   ORASK_STATE_DIR=str(base / "state"),
                   ORASK_CACHE_DIR=str(base / "cache"), OPENROUTER_API_KEY="")
        env.pop("ORASK_STDIN_WAIT", None)
        env.pop("ORASK_STDIN_DEADLINE", None)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, "-c", BOOTSTRAP + setup + "\nraise SystemExit(cli.main())", *argv],
            input=data, stdin=stdin, capture_output=True, text=True, timeout=5,
            env=env, check=False,
        )


def payload(proc: subprocess.CompletedProcess[str]):
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


# ---- machine-readable results and argument routing ------------------------
proc = run_cli(["panel", "question", "-C", "coding", "--json"], data="context")
check("category panel JSON is one parseable JSON value",
      proc.returncode == 0 and isinstance(payload(proc), list), proc.stdout[:80])

proc = run_cli(["question", "--json", "-c", "explicit"], data="piped")
check("shorthand ask combines explicit and piped context",
      proc.returncode == 0 and (payload(proc) or {}).get("context") == "explicit\n\npiped")

# ---- every descriptor shape exercises ask, which actually reads stdin ----
with tempfile.TemporaryFile() as source:
    source.write("file context π".encode())
    source.seek(0)
    proc = run_cli(["ask", "question", "--json"], stdin=source)
check("regular redirected stdin reaches the question intact",
      proc.returncode == 0 and (payload(proc) or {}).get("context") == "file context π")

with open(os.devnull) as source:
    proc = run_cli(["ask", "question", "--json"], stdin=source)
check("character device /dev/null is empty context",
      proc.returncode == 0 and (payload(proc) or {}).get("context") is None)

for body in (b"", b"socket context"):
    writer, reader = socket.socketpair()
    try:
        if body:
            writer.sendall(body)
        started = time.monotonic()
        proc = run_cli(["ask", "question", "--json"], stdin=reader,
                       extra_env={"ORASK_STDIN_WAIT": "0.05"})
        check(f"open socket with {len(body)} bytes returns after its idle wait",
              proc.returncode == 0 and time.monotonic() - started < 2
              and (payload(proc) or {}).get("context") == (body.decode() or None))
    finally:
        writer.close()
        reader.close()

writer, reader = socket.socketpair()
try:
    started = time.monotonic()
    proc = run_cli(["ask", "question", "--json"], stdin=reader,
                   extra_env={"ORASK_STDIN_WAIT": "2", "ORASK_STDIN_DEADLINE": "0.05"})
    check("socket wait cannot exceed the total deadline",
          proc.returncode == 0 and time.monotonic() - started < 1)
finally:
    writer.close()
    reader.close()

# ---- hard limits fail before the provider sees an incomplete prompt -------
for command in ("ask", "panel"):
    for body, succeeds in (("x" * 64, True), ("x" * 65, False)):
        proc = run_cli([command, "question", "--json"], data=body,
                       setup="\ncli.STDIN_MAX_BYTES = 64\n")
        check(f"{command} {'accepts exact' if succeeds else 'refuses over'} stdin byte limit",
              (proc.returncode == 0 and "PROVIDER_CALLED" in proc.stderr) if succeeds else
              (proc.returncode == 1 and "PROVIDER_CALLED" not in proc.stderr
               and "stdin" in proc.stderr and "64" in proc.stderr))

for body in (b"", b"incomplete context"):
    read_fd, write_fd = os.pipe()
    try:
        if body:
            os.write(write_fd, body)
        proc = run_cli(["ask", "question", "--json"], stdin=read_fd,
                       extra_env={"ORASK_STDIN_DEADLINE": "0.05"})
        check(f"unclosed pipe with {len(body)} bytes refuses at deadline before calling provider",
              proc.returncode == 1 and "PROVIDER_CALLED" not in proc.stderr
              and "ORASK_STDIN_DEADLINE" in proc.stderr)
    finally:
        os.close(read_fd)
        os.close(write_fd)

for name in ("ORASK_STDIN_WAIT", "ORASK_STDIN_DEADLINE"):
    for value in ("invalid", "nan", "inf", "-1", "0"):
        proc = run_cli(["--version"], data="", extra_env={name: value})
        check(f"{name}={value} cannot crash CLI import", proc.returncode == 0)
        proc = run_cli(["ask", "question", "--json"], data="context",
                       extra_env={name: value})
        check(f"{name}={value} yields actionable refusal before provider call",
              proc.returncode == 1 and name in proc.stderr
              and "Traceback" not in proc.stderr and "PROVIDER_CALLED" not in proc.stderr)

# ---- maintenance must prove live availability, not cached availability ----
proc = run_cli(["doctor"], data="", setup="""
core.get_api_key = lambda: 'test-key'
def catalog(refresh=False, allow_stale=True):
    if allow_stale:
        return [{'id': 'test/cached'}]
    raise core.OpenRouterError('live catalogue offline')
core.get_catalog = catalog
core.account_usage = lambda: {}
""")
check("doctor cannot report reachable using a stale fallback",
      proc.returncode == 1 and "[FAIL] OpenRouter catalogue reachable" in proc.stdout
      and "live catalogue offline" in proc.stdout)

for args in (["categories", "--verify"], ["categories", "--verify", "--json"]):
    for available, excluded in ((False, False), (True, True), (True, False)):
        proc = run_cli(args, data="", setup=f"""
core.list_categories = lambda: [{{'category': 'test', 'models': ['test/model'],
    'aka': [], 'why': 'fixture', 'measured': '2026-09-12'}}]
core.verify_categories = lambda: [{{'category': 'test', 'slug': 'test/model',
    'available': {available}, 'excluded_vendor': {excluded}}}]
""")
        expected = 0 if available and not excluded else 1
        check(f"category verification exit status reflects availability={available}, "
              f"excluded={excluded}, json={'--json' in args}", proc.returncode == expected)

print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} CLI checks passed")
raise SystemExit(bool(FAILURES))
