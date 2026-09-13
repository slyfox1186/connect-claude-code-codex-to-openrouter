"""Command-line front-end: orask.

orask "why is this leaking?" -m glm -f server.py
orask "what does this diagram show?" -f ~/shots/arch.png -m glm
orask "does this spec contradict itself?" -f ~/docs/spec.pdf
git diff | orask ask "review this diff" --role reviewer
orask panel "is this plan sound?" -c "$(cat PLAN.md)"
orask models --search kimi
orask doctor
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import select
import stat
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, core, diagnostics

SUBCOMMANDS = {
    "ask",
    "panel",
    "models",
    "info",
    "usage",
    "threads",
    "log",
    "doctor",
    "categories",
    "guide",
}


def _fmt_money(value: Any) -> str:
    number = core._nonnegative_number(value)
    return f"${number:.4f}" if number is not None else "$?"


def _fmt_timestamp(value: Any, pattern: str) -> str:
    try:
        stamp = float(value)
        if isinstance(value, bool) or not math.isfinite(stamp):
            return "?"
        return time.strftime(pattern, time.localtime(stamp))
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _print_result(result: dict[str, Any], show_reasoning: bool) -> None:
    if not result.get("ok"):
        label = "INCOMPLETE" if result.get("incomplete") else "NO ANSWER"
        print(f"── {result.get('model') or result.get('requested')}  {label}")
        print(f"   {result['error']}")
        for note in result.get("notes") or []:
            print(f"   note: {note}")
        if result.get("usage"):
            print(f"   billed {_fmt_money(result['usage'].get('cost_usd'))}")
            usage = result["usage"]
            print(
                f"   max_tokens={result.get('max_tokens') or 'none sent'} "
                f"context_window={result.get('context_window')} "
                f"effort={result.get('effort')} finish_reason={result.get('finish_reason')} "
                f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} "
                f"reasoning={usage.get('reasoning_tokens')} latency_s={result.get('latency_s')}"
            )
        if result.get("diagnostics"):
            print("   diagnostics: " + json.dumps(result["diagnostics"]))
        if result.get("answer"):
            print("\nPartial answer:\n" + result["answer"])
        if show_reasoning and result.get("reasoning"):
            print("\nReasoning (not a final answer):\n" + result["reasoning"])
        return
    usage = result.get("usage") or {}
    head = f"── {result['model']}"
    if result.get("effort"):
        head += f"  effort={result['effort']}"
    head += (
        f"  {result.get('latency_s', '?')}s"
        f"  in={usage.get('prompt_tokens', '?')}"
        f" out={usage.get('completion_tokens', '?')}"
    )
    if usage.get("reasoning_tokens"):
        head += f" (reasoning {usage['reasoning_tokens']})"
    head += f"  {_fmt_money(usage.get('cost_usd'))}"
    print(head)
    if result.get("diagnostics"):
        print("   diagnostics: " + json.dumps(result["diagnostics"]))
    for note in result.get("notes") or []:
        print(f"   note: {note}")
    print()
    if show_reasoning and result.get("reasoning"):
        print("[reasoning]")
        print(result["reasoning"])
        print("\n[answer]")
    print(result.get("answer") or "(empty answer)")


# How long to wait for data on a stdin we cannot trust to ever close.
STDIN_WAIT_S = 0.5
# Hard ceilings for every shape of stdin. Generous enough that a real `git diff | orask` goes
# through untouched, small enough that a producer which never stops - `yes | orask ask ...` -
# cannot hold the CLI open or grow the buffer without bound.
STDIN_MAX_BYTES = 32 * 1024 * 1024
STDIN_DEADLINE_S = 30.0


def _stdin_seconds(name: str, default: float) -> float:
    """Validate only when stdin is read, so bad settings cannot break --help/import."""
    try:
        value = float(os.environ.get(name, default))
        if math.isfinite(value) and value > 0:
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    raise core.OpenRouterError(f"{name} must be a finite positive number of seconds")


def read_stdin_safely(wait: float | None = None) -> str:
    """Read piped stdin without ever hanging.

    A plain `sys.stdin.read()` here is a trap: when orask is launched by a
    background job, daemon or agent shell tool, fd 0 is often a socket whose
    write end is never closed, and the read blocks forever.

    Every shape goes through select, so nothing can block indefinitely. A regular file or a
    real pipe is waited on until the deadline, because a slow producer is normal and a
    redirect has a genuine EOF. Anything else - a socket, a character device - is read only
    while data keeps arriving. Both are bounded by the byte ceiling and the deadline;
    reaching a hard limit refuses the call rather than billing for a partial prompt.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        fd = sys.stdin.fileno()
        mode = os.fstat(fd).st_mode
    except (OSError, ValueError, AttributeError):
        return ""

    wait = _stdin_seconds("ORASK_STDIN_WAIT", STDIN_WAIT_S) if wait is None else wait
    if not math.isfinite(wait) or wait <= 0:
        raise core.OpenRouterError("stdin wait must be a finite positive number of seconds")
    duration = _stdin_seconds("ORASK_STDIN_DEADLINE", STDIN_DEADLINE_S)
    drainable = stat.S_ISREG(mode) or stat.S_ISFIFO(mode)
    deadline = time.monotonic() + duration
    chunks: list[bytes] = []
    total = 0

    def timed_out() -> None:
        raise core.OpenRouterError(
            f"stdin did not finish within {duration:g}s; no call was sent. "
            "Finish the producer or save its output to a file before calling orask; "
            "increase ORASK_STDIN_DEADLINE if a slower producer is intentional."
        )

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out()
        try:
            ready, _, _ = select.select(
                [fd], [], [], remaining if drainable else min(wait, remaining)
            )
        except (OSError, ValueError, OverflowError) as exc:
            raise core.OpenRouterError(
                "cannot read stdin safely; no call was sent. Check ORASK_STDIN_WAIT "
                f"and ORASK_STDIN_DEADLINE: {exc}"
            ) from exc
        if not ready:
            # An idle socket is normal for agent shells, including one carrying no input.
            if drainable or (total and remaining <= wait):
                timed_out()
            break
        try:
            # Read one byte beyond the ceiling to distinguish exact-sized input from
            # truncation, while never allocating an unbounded chunk.
            data = os.read(fd, min(65536, STDIN_MAX_BYTES - total + 1))
        except OSError as exc:
            raise core.OpenRouterError(f"cannot read stdin; no call was sent: {exc}") from exc
        if not data:  # EOF
            break
        total += len(data)
        if total > STDIN_MAX_BYTES:
            raise core.OpenRouterError(
                f"stdin exceeds the {STDIN_MAX_BYTES:,}-byte limit; no call was sent. "
                "Narrow the input or pass selected files with --file."
            )
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", "replace")


def _gather_context(args: argparse.Namespace) -> str | None:
    parts = []
    if args.context:
        parts.append(args.context)
    # Piped input becomes context, so `git diff | orask ask ...` just works.
    piped = read_stdin_safely().strip()
    if piped:
        parts.append(piped)
    return "\n\n".join(parts) if parts else None


def _shared_ask_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", nargs="+", help="the question to ask")
    parser.add_argument("-c", "--context", help="background text to include")
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="send a file, or a directory of files (repeatable). Source goes in as "
        "text; a PDF, image or audio file is attached to the message directly",
    )
    parser.add_argument(
        "--pdf-engine",
        choices=list(core.PDF_ENGINES),
        help="how an attached PDF is read (default: cloudflare-ai, free)",
    )
    parser.add_argument("-e", "--effort", help="reasoning effort: low|medium|high|xhigh|max|none")
    parser.add_argument(
        "-r",
        "--role",
        help="advisor (default) | reviewer | debugger | architect | redteam",
    )
    parser.add_argument("-s", "--system", help="override the system prompt entirely")
    parser.add_argument(
        "--max-tokens",
        type=int,
        help=(
            "send an output cap covering reasoning and answer; unset sends none and the "
            "provider's own limit applies"
        ),
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        metavar="N",
        help="budget prompt plus answer into N tokens; capped at the model's own window",
    )
    parser.add_argument(
        "--compress",
        dest="compress",
        action="store_true",
        default=None,
        help="let OpenRouter drop text from the middle of an oversized prompt",
    )
    parser.add_argument(
        "--no-compress",
        dest="compress",
        action="store_false",
        help="refuse an oversized prompt instead, even on an endpoint that compresses by default",
    )
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--show-reasoning", action="store_true", help="print reasoning too")
    parser.add_argument(
        "--allow-expensive", action="store_true", help="bypass the per-call cost guard"
    )
    parser.add_argument(
        "--allow-secret-files",
        action="store_true",
        help="permit sending files that match the secrets denylist",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orask",
        description="Ask another state-of-the-art LLM on OpenRouter for a second opinion.",
    )
    parser.add_argument("--version", action="version", version=f"orask {__version__}")
    subs = parser.add_subparsers(dest="command")

    ask = subs.add_parser("ask", help="ask one model")
    _shared_ask_args(ask)
    ask.add_argument("-m", "--model", help="alias (kimi, glm) or full OpenRouter slug")
    ask.add_argument(
        "-C",
        "--category",
        help="pick by capability instead of naming a model: coding, debugging, reasoning, "
        "math, chat, agentic, research, long_context, creative, budget, general",
    )
    ask.add_argument("-t", "--thread", help="keep a named conversation for follow-ups")

    panel = subs.add_parser("panel", help="ask several models in parallel and compare")
    _shared_ask_args(panel)
    panel.add_argument(
        "-M",
        "--models",
        help="comma-separated list (default: kimi,glm)",
    )
    panel.add_argument(
        "-C",
        "--category",
        help="put the two current leaders for a capability against each other",
    )

    models = subs.add_parser("models", help="search the live OpenRouter catalogue")
    models.add_argument("--search", help="substring to match in slug or name")
    models.add_argument("--vendor", help="restrict to one vendor, e.g. moonshotai")
    models.add_argument("--limit", type=int, default=25)
    models.add_argument(
        "--sort",
        default="intelligence",
        choices=["intelligence", "context", "price", "name"],
    )
    models.add_argument("--include-batch", action="store_true")
    models.add_argument("--refresh", action="store_true", help="bypass the catalogue cache")
    models.add_argument("--json", action="store_true")

    info = subs.add_parser("info", help="details for one model")
    info.add_argument("model")
    info.add_argument("--json", action="store_true")

    usage = subs.add_parser("usage", help="OpenRouter account usage and bridge spend")
    usage.add_argument("--json", action="store_true")

    threads = subs.add_parser("threads", help="list saved conversation threads")
    threads.add_argument("--json", action="store_true")

    log = subs.add_parser("log", help="recent calls made through the bridge")
    log.add_argument("-n", "--limit", type=int, default=20)
    log.add_argument("--json", action="store_true")

    cats = subs.add_parser("categories", help="capabilities you can ask for by name")
    cats.add_argument(
        "--verify", action="store_true", help="check each pinned model against the live catalogue"
    )
    cats.add_argument("--json", action="store_true")

    guide = subs.add_parser("guide", help="local best-practice guides (free, no model call)")
    guide.add_argument("topic", nargs="?", help="guide name; omit for the index")
    guide.add_argument("section", nargs="?", help="one heading within that guide")
    guide.add_argument("--search", metavar="TEXT", help="search across every guide")
    guide.add_argument("--all", action="store_true", help="print the whole guide")
    guide.add_argument(
        "--stale",
        action="store_true",
        help="list guides whose verified date is over six months old",
    )

    subs.add_parser("doctor", help="check key, catalogue, aliases and registrations")
    return parser


def _cmd_categories(args: argparse.Namespace) -> int:
    rows = core.list_categories()
    status = 0
    if args.verify:
        checks = {(r["category"], r["slug"]): r for r in core.verify_categories()}
        status = int(
            any(not r.get("available") or r.get("excluded_vendor") for r in checks.values())
        )
    if args.json:
        payload = (
            rows if not args.verify else {"categories": rows, "verified": list(checks.values())}
        )
        print(json.dumps(payload, indent=2))
        return status

    for row in rows:
        print(f"\n{row['category']}")
        if row["panel_models"] != row["models"]:
            print(f"    panel: {', '.join(row['panel_models'])}")
            print("    benchmark pins (single consultation uses first):")
        for slug in row["models"]:
            line = f"    {slug}"
            if args.verify:
                check = checks.get((row["category"], slug)) or {}
                iq = check.get("intelligence_index")
                state = (
                    "EXCLUDED VENDOR"
                    if check.get("excluded_vendor")
                    else "ok"
                    if check.get("available")
                    else "NO LONGER LISTED"
                )
                line += f"    [{state}" + (f", index {iq:.1f}" if iq is not None else "") + "]"
            print(line)
        if row["aka"]:
            print(f"    also matches: {', '.join(row['aka'])}")
        print(f"    why: {row['why']}")
        print(f"    checked: {row['measured']}")

    banned = core.excluded_vendors()
    if banned:
        print(f"\nBenchmark picks exclude {' or '.join(banned)} models.")
        print("The named coding group and explicitly requested models do not use this filter.")
    return status


def _cmd_ask(args: argparse.Namespace) -> int:
    result = core.ask(
        " ".join(args.question),
        model=args.model,
        category=getattr(args, "category", None),
        context=_gather_context(args),
        files=args.file,
        effort=args.effort,
        role=args.role,
        system=args.system,
        max_tokens=args.max_tokens,
        max_context_tokens=args.max_context_tokens,
        context_compression=args.compress,
        temperature=args.temperature,
        thread=args.thread,
        pdf_engine=args.pdf_engine,
        allow_expensive=args.allow_expensive,
        allow_secret_files=args.allow_secret_files,
        include_reasoning=args.show_reasoning,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_result(result, args.show_reasoning)
    return 0 if result.get("ok") else 1


def _cmd_panel(args: argparse.Namespace) -> int:
    models = [m.strip() for m in (args.models or "").split(",") if m.strip()] or None
    started = time.monotonic()
    if getattr(args, "category", None) and not getattr(args, "models", None) and not args.json:
        match = core.resolve_category(args.category)
        if match:
            name, spec = match
            why = ", ".join(core.CODING_PANEL.values()) if name == "coding" else spec.get("why")
            print(f"category: {name}" + (f" - {why}" if why else ""))
            print()

    results = core.ask_panel(
        " ".join(args.question),
        models=models,
        category=getattr(args, "category", None),
        context=_gather_context(args),
        files=args.file,
        effort=args.effort,
        role=args.role,
        system=args.system,
        max_tokens=args.max_tokens,
        max_context_tokens=args.max_context_tokens,
        context_compression=args.compress,
        temperature=args.temperature,
        pdf_engine=args.pdf_engine,
        allow_expensive=args.allow_expensive,
        allow_secret_files=args.allow_secret_files,
        include_reasoning=args.show_reasoning,
    )
    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if any(r.get("ok") for r in results) else 1

    total = 0.0
    for result in results:
        # Counted whether or not it answered: a model that returns nothing is ok: False and
        # was still billed for it.
        total += float((result.get("usage") or {}).get("cost_usd") or 0)
        _print_result(result, args.show_reasoning)
        print()
    print(
        f"── panel of {len(results)} in {time.monotonic() - started:.1f}s, "
        f"total {_fmt_money(total)}"
    )
    return 0 if any(r.get("ok") for r in results) else 1


def _cmd_models(args: argparse.Namespace) -> int:
    if args.refresh:
        core.get_catalog(refresh=True)
    rows = core.list_models(
        search=args.search,
        vendor=args.vendor,
        limit=args.limit,
        include_batch=args.include_batch,
        sort=args.sort,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no models matched")
        return 1
    print(f"{'slug':46} {'ctx':>9} {'in$/M':>8} {'out$/M':>8} {'IQ':>5}  efforts")
    print("-" * 100)
    for row in rows:
        iq = row["intelligence_index"]
        print(
            f"{row['slug'][:46]:46} {row['context'] or 0:>9,} "
            f"{row['usd_per_m_input']:>8.2f} {row['usd_per_m_output']:>8.2f} "
            f"{(f'{iq:.1f}' if iq is not None else '-'):>5}  "
            f"{'/'.join(row['reasoning_efforts']) or '-'}"
        )
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    data = core.model_info(args.model)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    for key, value in data.items():
        if value in (None, "", []):
            continue
        print(f"{key:22} {value if not isinstance(value, list) else '/'.join(map(str, value))}")
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    data = core.account_usage()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    for key, value in data.items():
        print(f"{key:28} {value}")
    return 0


def _cmd_threads(args: argparse.Namespace) -> int:
    rows = core.list_threads()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no threads yet (pass --thread NAME to orask ask to start one)")
        return 0
    for row in rows:
        stamp = _fmt_timestamp(row.get("updated_at"), "%Y-%m-%d %H:%M")
        print(f"{row['name']!s:30} {row['messages']!s:>3} messages   last {stamp}")
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    rows = core.read_log(limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no calls logged yet")
        return 0
    for row in rows:
        stamp = _fmt_timestamp(row.get("ts"), "%m-%d %H:%M:%S")
        if row.get("ok"):
            print(
                f"{stamp}  {str(row.get('model'))[:34]:34} "
                f"{row.get('effort') or '-'!s:6} "
                f"in={row.get('prompt_tokens') or 0!s:>7} "
                f"out={row.get('completion_tokens') or 0!s:>6} "
                f"{_fmt_money(row.get('cost_usd')):>9} {row.get('latency_s')}s"
            )
        else:
            print(f"{stamp}  {str(row.get('model'))[:34]:34} FAILED  {str(row.get('error'))[:70]}")
    costs = [core._nonnegative_number(row.get("cost_usd")) for row in rows]
    total = sum(cost for cost in costs if cost is not None)
    unknown = sum(cost is None for cost in costs)
    suffix = f" known cost; {unknown} unknown" if unknown else " total"
    print(f"\n{len(rows)} shown, {_fmt_money(total)}{suffix}")
    return 0


def _registration_issues(server: Any, *, codex: bool = False) -> list[str]:
    """Check the managed local server without launching commands from a config file."""
    if not isinstance(server, dict):
        return ["openrouter registration is missing or is not an object"]
    issues = []
    command = server.get("command")
    launcher = core.PROJECT_ROOT / "bin" / "openrouter-mcp"
    try:
        current = (
            isinstance(command, str)
            and Path(command).is_absolute()
            and Path(command).resolve() == launcher.resolve()
            and launcher.is_file()
            and os.access(launcher, os.X_OK)
        )
    except (OSError, ValueError):
        current = False
    if not current:
        issues.append("launch command does not point to this checkout's executable server")
    if server.get("args", []) != []:
        issues.append("launcher args must be empty")
    if server.get("type", "stdio") != "stdio" or "url" in server:
        issues.append("registration must use local stdio transport")
    env = server.get("env", {})
    if not isinstance(env, dict) or any(not isinstance(v, str) for v in env.values()):
        issues.append("server env must be an object of strings")
    elif "ORASK_PYTHON" in env:
        interpreter = env["ORASK_PYTHON"]
        try:
            matches = (
                Path(interpreter).is_absolute()
                and Path(interpreter).resolve() == Path(sys.executable).resolve()
            )
        except (OSError, ValueError):
            matches = False
        if not matches:
            issues.append("ORASK_PYTHON differs from the interpreter running this doctor")
    if codex:
        if server.get("enabled", True) is not True:
            issues.append("server is disabled or enabled is not a boolean")
        enabled = server.get("enabled_tools", list(core.MCP_TOOLS))
        disabled = server.get("disabled_tools", [])
        if (
            not isinstance(enabled, list)
            or not isinstance(disabled, list)
            or any(not isinstance(t, str) for t in [*enabled, *disabled])
        ):
            issues.append("enabled_tools and disabled_tools must be arrays of strings")
        else:
            available = set(enabled) - set(disabled)
            missing = [tool for tool in core.MCP_TOOLS if tool not in available]
            if missing:
                issues.append("tools unavailable: " + ", ".join(missing))
    return issues


def _codex_registration(path: Path) -> Any:
    # Keep the CLI stdlib-only and Python 3.10-compatible. Without a real TOML parser,
    # doctor cannot distinguish a table from text in a comment or multiline string.
    try:
        import tomllib
    except ImportError as exc:
        raise core.OpenRouterError(
            "Codex TOML validation is unavailable on this Python; run doctor with "
            "Python 3.11+ to validate it (other CLI commands still support Python 3.10)"
        ) from exc
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    servers = data.get("mcp_servers", {})
    return servers.get("openrouter") if isinstance(servers, dict) else None


def _cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"[{'PASS' if good else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
        ok = ok and good

    print(f"orask {__version__}\n")

    try:
        key = core.get_api_key()
        # Enough to tell one key from another, not enough to be worth shoulder-surfing:
        # doctor output gets pasted into issues and chat windows.
        check("API key found", bool(key), f"{key[:8]}... ({len(key)} chars)")
    except core.OpenRouterError as exc:
        check("API key found", False, str(exc))
        return 1

    mode = ""
    if core.ENV_FILE.is_file():
        mode = oct(core.ENV_FILE.stat().st_mode & 0o777)[2:]
        check(f"key file permissions ({core.ENV_FILE})", mode == "600", f"mode {mode}")

    try:
        catalog = core.get_catalog(refresh=True, allow_stale=False)
        check("OpenRouter catalogue reachable", True, f"{len(catalog)} models")
    except core.OpenRouterError as exc:
        check("OpenRouter catalogue reachable", False, str(exc))
        return 1

    cfg = core.load_config()
    for alias, pinned in (cfg.get("aliases") or {}).items():
        try:
            slug, note = core.resolve_model(alias)
            check(f"alias '{alias}' -> {slug}", slug == pinned or not note, note or "")
        except core.OpenRouterError as exc:
            check(f"alias '{alias}'", False, str(exc))

    try:
        usage = core.account_usage()
        check(
            "account reachable",
            True,
            f"spent ${usage.get('account_usage_usd')} lifetime on this key; "
            f"bridge has logged {usage.get('bridge_calls_logged')} calls "
            f"(${usage.get('bridge_spend_usd')})",
        )
    except core.OpenRouterError as exc:
        check("account reachable", False, str(exc))

    # These are the installer's user-scope registrations. Project overrides, trust
    # policy and a running client's MCP handshake need checking in that client.
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    claude_json = (
        Path(claude_dir).expanduser() / ".claude.json"
        if claude_dir
        else Path.home() / ".claude.json"
    )
    try:
        with claude_json.open(encoding="utf-8") as handle:
            data = json.load(handle)
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
        server = servers.get("openrouter") if isinstance(servers, dict) else None
        issues = _registration_issues(server)
        check(
            "Claude Code user-scope registration",
            not issues,
            "; ".join(issues) + "; run install.sh, then restart Claude Code"
            if issues
            else "configured for this checkout",
        )
    except (OSError, ValueError) as exc:
        check("Claude Code user-scope registration", False, f"{claude_json}: {exc}")

    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    codex_toml = codex_dir / "config.toml"
    try:
        server = _codex_registration(codex_toml)
        issues = _registration_issues(server, codex=True)
        check(
            "Codex user-scope registration",
            not issues,
            "; ".join(issues) + "; run install.sh, then restart Codex"
            if issues
            else f"configured for this checkout; {len(core.MCP_TOOLS)} tools enabled",
        )
    except (OSError, ValueError, core.OpenRouterError) as exc:
        check("Codex user-scope registration", False, f"{codex_toml}: {exc}")

    print("\n" + ("all checks passed" if ok else "some checks failed - see above"))
    return 0 if ok else 1


def _cmd_guide(args: argparse.Namespace) -> int:
    if args.search:
        found = core.search_guides(args.search)
        if not found["hits"]:
            print(f"nothing matches {args.search!r}")
            return 1
        for hit in found["hits"]:
            print(f"{hit['topic']:<14} {hit['section'] or '(top)':<30} {hit['snippet']}")
        if found["truncated"]:
            print(f"\n{len(found['hits'])} of {found['total']} shown - narrow the query")
        return 0

    if args.stale:
        cutoff = (dt.date.today() - dt.timedelta(days=182)).isoformat()
        # An unparseable date counts as stale: that is the safe direction, and it
        # also surfaces a guide whose front matter was written by hand and wrong.
        rows = [r for r in core.list_guides() if r["stale"] or r["verified"] < cutoff]
        if not rows:
            print("every guide has been verified in the last six months")
            return 0
        for row in rows:
            print(f"{row['topic']:<14} verified {row['verified'] or 'never'}")
        return 1

    if not args.topic:
        rows = core.list_guides()
        if not rows:
            print("no guides installed")
            return 1
        width = max(len(r["topic"]) for r in rows)
        for row in rows:
            print(f"{row['topic']:<{width}}  {row['triggers']}")
        return 0

    if args.section or args.all:
        print(core.read_guide(args.topic, section=args.section)["text"])
        return 0

    data = core.guide_outline(args.topic)
    print(f"{data['topic']}  ({data['lines']} lines, verified {data['verified'] or 'undated'})")
    print(data["path"])
    if data["triggers"]:
        print(f"read when: {data['triggers']}")
    print()
    for entry in data["sections"]:
        print(f"{'  ' * (entry['level'] - 2)}- {entry['title']}  ({entry['lines']} lines)")
    print(f"\norask guide {data['topic']} <section>, or --all for the whole file")
    return 0


HANDLERS = {
    "ask": _cmd_ask,
    "panel": _cmd_panel,
    "models": _cmd_models,
    "info": _cmd_info,
    "usage": _cmd_usage,
    "threads": _cmd_threads,
    "log": _cmd_log,
    "doctor": _cmd_doctor,
    "categories": _cmd_categories,
    "guide": _cmd_guide,
}


def main(argv: list[str] | None = None) -> int:
    diagnostics.emit("cli.start", runtime=__version__, python_version=sys.version.split()[0])
    argv = list(sys.argv[1:] if argv is None else argv)
    # `orask "question"` is shorthand for `orask ask "question"`.
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "ask")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        diagnostics.emit("cli.command", operation=args.command)
        result = HANDLERS[args.command](args)
        diagnostics.emit("cli.end", operation=args.command, ok=result == 0)
        return result
    except core.OpenRouterError as exc:
        diagnostics.emit("cli.error", error_type=type(exc).__name__)
        print(f"orask: {exc}", file=sys.stderr)
        details = getattr(exc, "orask_diagnostics", None)
        if details:
            print("diagnostics: " + json.dumps(details), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\norask: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
