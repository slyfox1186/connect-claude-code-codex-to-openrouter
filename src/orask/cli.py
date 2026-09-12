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
import json
import os
import select
import stat
import sys
import time
from typing import Any

from . import __version__
from . import core

SUBCOMMANDS = {"ask", "panel", "models", "info", "usage", "threads", "log", "doctor",
               "categories"}


def _fmt_money(value: Any) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "$?"


def _print_result(result: dict[str, Any], show_reasoning: bool) -> None:
    if result.get("error") and not result.get("answer"):
        print(f"── {result.get('model') or result.get('requested')}  NO ANSWER")
        print(f"   {result['error']}")
        for note in result.get("notes") or []:
            print(f"   note: {note}")
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
    for note in result.get("notes") or []:
        print(f"   note: {note}")
    print()
    if show_reasoning and result.get("reasoning"):
        print("[reasoning]")
        print(result["reasoning"])
        print("\n[answer]")
    print(result.get("answer") or "(empty answer)")


# How long to wait for data on a stdin we cannot trust to ever close.
STDIN_WAIT_S = float(os.environ.get("ORASK_STDIN_WAIT", "0.5"))


def read_stdin_safely(wait: float = STDIN_WAIT_S) -> str:
    """Read piped stdin without ever hanging.

    A plain `sys.stdin.read()` here is a trap: when orask is launched by a
    background job, daemon or agent shell tool, fd 0 is often a socket whose
    write end is never closed, and the read blocks forever.

    A regular file or a real pipe is safe to drain (a redirect has an EOF, and a
    pipe's writer closes it on exit). Anything else - a socket, a character
    device - is read only while data keeps arriving, then abandoned.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        fd = sys.stdin.fileno()
        mode = os.fstat(fd).st_mode
    except (OSError, ValueError, AttributeError):
        return ""

    if stat.S_ISREG(mode) or stat.S_ISFIFO(mode):
        try:
            return sys.stdin.read()
        except (OSError, UnicodeDecodeError):
            return ""

    chunks: list[bytes] = []
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], wait)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:  # EOF
            break
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
        "-f", "--file", action="append", default=[], metavar="PATH",
        help="send a file, or a directory of files (repeatable). Source goes in as "
             "text; a PDF, image or audio file is attached to the message directly",
    )
    parser.add_argument(
        "--pdf-engine", choices=list(core.PDF_ENGINES),
        help="how an attached PDF is read (default: cloudflare-ai, free)",
    )
    parser.add_argument("-e", "--effort", help="reasoning effort: low|medium|high|xhigh|max|none")
    parser.add_argument(
        "-r", "--role",
        help="advisor (default) | reviewer | debugger | architect | redteam",
    )
    parser.add_argument("-s", "--system", help="override the system prompt entirely")
    parser.add_argument("--max-tokens", type=int, help="cap the answer length")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--show-reasoning", action="store_true", help="print reasoning too")
    parser.add_argument("--allow-expensive", action="store_true",
                        help="bypass the per-call cost guard")
    parser.add_argument(
        "--allow-secret-files", action="store_true",
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
        "-C", "--category",
        help="pick by capability instead of naming a model: coding, debugging, reasoning, "
             "math, chat, agentic, research, long_context, creative, budget, general",
    )
    ask.add_argument("-t", "--thread", help="keep a named conversation for follow-ups")

    panel = subs.add_parser("panel", help="ask several models in parallel and compare")
    _shared_ask_args(panel)
    panel.add_argument(
        "-M", "--models", help="comma-separated list (default: kimi,glm)",
    )
    panel.add_argument(
        "-C", "--category",
        help="put the two current leaders for a capability against each other",
    )

    models = subs.add_parser("models", help="search the live OpenRouter catalogue")
    models.add_argument("--search", help="substring to match in slug or name")
    models.add_argument("--vendor", help="restrict to one vendor, e.g. moonshotai")
    models.add_argument("--limit", type=int, default=25)
    models.add_argument(
        "--sort", default="intelligence",
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
    cats.add_argument("--verify", action="store_true",
                      help="check each pinned model against the live catalogue")
    cats.add_argument("--json", action="store_true")

    subs.add_parser("doctor", help="check key, catalogue, aliases and registrations")
    return parser


def _cmd_categories(args: argparse.Namespace) -> int:
    rows = core.list_categories()
    if args.verify:
        checks = {(r["category"], r["slug"]): r for r in core.verify_categories()}
    if args.json:
        payload = rows if not args.verify else {"categories": rows,
                                                "verified": list(checks.values())}
        print(json.dumps(payload, indent=2))
        return 0

    for row in rows:
        print(f"\n{row['category']}")
        for slug in row["models"]:
            line = f"    {slug}"
            if args.verify:
                check = checks.get((row["category"], slug)) or {}
                iq = check.get("intelligence_index")
                state = "ok" if check.get("available") else "NO LONGER LISTED"
                line += f"    [{state}" + (f", index {iq:.1f}" if iq is not None else "") + "]"
            print(line)
        if row["aka"]:
            print(f"    also matches: {', '.join(row['aka'])}")
        print(f"    why: {row['why']}")
        print(f"    checked: {row['measured']}")

    banned = core.excluded_vendors()
    if banned:
        print(f"\nCategory picks never return {' or '.join(banned)} models: this bridge is for")
        print("an opinion from outside the agent asking. Ask by full slug to override.")
    return 0


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
        if result.get("ok"):
            _print_result(result, args.show_reasoning)
        else:
            print(f"── {result.get('requested')}  FAILED")
            print(f"   {result.get('error')}")
            for note in result.get("notes") or []:
                print(f"   note: {note}")
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
        search=args.search, vendor=args.vendor, limit=args.limit,
        include_batch=args.include_batch, sort=args.sort,
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
        stamp = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated_at"]))
            if row.get("updated_at") else "?"
        )
        print(f"{row['name']:30} {row['messages']:>3} messages   last {stamp}")
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
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(row.get("ts", 0)))
        if row.get("ok"):
            print(
                f"{stamp}  {str(row.get('model'))[:34]:34} "
                f"{str(row.get('effort') or '-'):6} "
                f"in={row.get('prompt_tokens') or 0:>7} out={row.get('completion_tokens') or 0:>6} "
                f"{_fmt_money(row.get('cost_usd')):>9} {row.get('latency_s')}s"
            )
        else:
            print(f"{stamp}  {str(row.get('model'))[:34]:34} FAILED  {str(row.get('error'))[:70]}")
    total = sum(float(r.get("cost_usd") or 0) for r in rows)
    print(f"\n{len(rows)} shown, {_fmt_money(total)} total")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"[{'PASS' if good else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
        ok = ok and good

    print(f"orask {__version__}\n")

    try:
        key = core.get_api_key()
        check("API key found", bool(key), f"{key[:11]}...{key[-4:]} ({len(key)} chars)")
    except core.OpenRouterError as exc:
        check("API key found", False, str(exc))
        return 1

    mode = ""
    if core.ENV_FILE.is_file():
        mode = oct(core.ENV_FILE.stat().st_mode & 0o777)[2:]
        check(f"key file permissions ({core.ENV_FILE})", mode == "600", f"mode {mode}")

    try:
        catalog = core.get_catalog(refresh=True)
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
            "account reachable", True,
            f"spent ${usage.get('account_usage_usd')} lifetime on this key; "
            f"bridge has logged {usage.get('bridge_calls_logged')} calls "
            f"(${usage.get('bridge_spend_usd')})",
        )
    except core.OpenRouterError as exc:
        check("account reachable", False, str(exc))

    # registration in both harnesses
    claude_json = os.path.expanduser("~/.claude.json")
    if os.path.isfile(claude_json):
        try:
            with open(claude_json, encoding="utf-8") as handle:
                servers = json.load(handle).get("mcpServers") or {}
            check(
                "registered as an MCP server in Claude Code",
                "openrouter" in servers,
                "run install.sh if missing",
            )
        except (OSError, json.JSONDecodeError) as exc:
            check("Claude Code config readable", False, str(exc))

    codex_toml = os.path.expanduser("~/.codex/config.toml")
    if os.path.isfile(codex_toml):
        with open(codex_toml, encoding="utf-8") as handle:
            body = handle.read()
        check(
            "registered as an MCP server in Codex",
            "[mcp_servers.openrouter]" in body,
            "run install.sh if missing",
        )

    print("\n" + ("all checks passed" if ok else "some checks failed - see above"))
    return 0 if ok else 1


HANDLERS = {
    "ask": _cmd_ask, "panel": _cmd_panel, "models": _cmd_models, "info": _cmd_info,
    "usage": _cmd_usage, "threads": _cmd_threads, "log": _cmd_log, "doctor": _cmd_doctor,
    "categories": _cmd_categories,
}


def main(argv: list[str] | None = None) -> int:
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
        return HANDLERS[args.command](args)
    except core.OpenRouterError as exc:
        print(f"orask: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\norask: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
