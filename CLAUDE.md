# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`orask` is a bridge that lets Claude Code and Codex consult other OpenRouter models mid-task.
It ships two front-ends over one engine: an MCP stdio server and a CLI.

## Commands

Always invoke Python by absolute path. The interpreter this machine resolved is pinned in
`.orask-python` (gitignored, machine-specific); it is currently
`/home/jman/miniconda3/envs/openrouter-mcp/bin/python`.

```bash
PY=$(cat .orask-python)

$PY tests/test_core.py          # offline checks, no network or key, safe to run any time
$PY tests/test_mcp_stdio.py     # live MCP protocol test over stdio; spends a few cents
orask doctor                    # key, permissions, catalogue, aliases, both registrations
./install.sh                    # idempotent; re-run to update a drifted registration
```

Running a module directly needs the src path: `PYTHONPATH=src $PY -m orask.cli ...`.
The launchers in `bin/` do that plus interpreter resolution, so prefer `bin/orask`.

`test_mcp_stdio.py` makes a real billed model call and needs the `mcp` package plus a key.
Ask Jeff before running it.

There is no lint or type config in the repo. `# noqa` codes in the source follow ruff/flake8
naming but nothing enforces them.

### Running one check

Both test files are plain scripts, not pytest: a flat sequence of `check(label, ok, detail)`
calls that print `[PASS]`/`[FAIL]` per label and exit 1 if any failed. There is no selector.
To work on one area, run the whole file and grep the labels:

```bash
$PY tests/test_core.py | grep -i denylist
$PY tests/test_core.py | grep FAIL
```

New assertions go next to the related ones under the matching `# ---- section ----` comment.

## Architecture

Three layers, and the split between them is deliberate:

`src/orask/core.py` is the engine and is **standard library only**. HTTP, model resolution,
categories, attachments, cost, threads, logging. Never import a third-party package here: the
point is that a broken or upgraded `mcp` SDK cannot take the CLI down with it.

`src/orask/mcp_server.py` is the only file that touches the `mcp` SDK (`>=2.0`, where FastMCP
became `MCPServer`). It is a thin adapter: normalise arguments, call `core`, render text.
Two rules hold here. Blocking `core` calls go through `_run()` (`asyncio.to_thread`), and
expected failures must surface as `ToolError`, the one exception type the SDK forwards
verbatim; anything else reaches the calling agent as a useless "Error executing tool".
**stdout is the protocol channel** for `bin/openrouter-mcp`, so nothing in this path may print.

`src/orask/cli.py` is argparse over the same engine, and the fallback when MCP breaks.
`orask "question"` rewrites to `orask ask "question"` in `main()`. Piped stdin becomes
`context` via `read_stdin_safely()`, which must not be simplified into `sys.stdin.read()`:
under a background job or agent shell fd 0 is often a socket whose write end never closes,
and that was a real hang.

`bin/_python-env.sh` holds interpreter resolution, sourced by both launchers and by
`install.sh` so runtime and installer can never disagree about which Python runs this project.
The symlink walk is duplicated inline in each launcher because through the `~/.local/bin`
symlink the shared helper is not findable yet, and `readlink -f` is GNU only.

### Request path

`ask()` in core.py is the spine: resolve model or category, load thread history,
`build_messages()`, estimate cost and refuse over the guard, POST, then log and return a
structured dict. `ask_panel()` fans the same question across models on a thread pool and never
lets one failure drop the others; it refuses `thread` outright because several models writing
one transcript would interleave.

`build_messages()` puts text first and attachments last in the user turn, and returns
`(messages, notes)`. Every recovery, substitution and clamp becomes a note that is reported
back to the caller, rather than happening silently.

### Configuration

`config/models.json` (packaged, committed) is overlaid by `~/.config/openrouter/config.json`.
Keys starting with `_` are comments and are skipped. `aliases` and `roles` merge key by key so
a user file can add one entry without restating the table; everything else replaces outright.
Defaults are duplicated in `core._DEFAULTS`, so a new setting belongs in both.

Runtime paths are all env-overridable: `ORASK_CONFIG_DIR`, `ORASK_STATE_DIR`, `ORASK_CACHE_DIR`,
`ORASK_PYTHON`, `ORASK_BOOTSTRAP`, `ORASK_STDIN_WAIT`. Nothing is hardcoded to this machine.

Category entries carry `why` and a `measured` date. When changing a pin, update both and re-run
`orask categories --verify`. `category_exclude_vendors` (openai, anthropic, google) governs
category picks only; a full slug still reaches those.

## Invariants worth knowing before editing

Several behaviours here look like they could be tidied up and cannot. Each is regression-tested
in `test_core.py`.

POST retries stay narrow. `RETRY_STATUS_POST = {408, 429}` and read timeouts are never retried,
because a 5xx on `/chat/completions` can arrive after the provider already generated and billed
the tokens. Do not widen it to match `RETRY_STATUS_GET`.

The secrets denylist unions user patterns with `DEFAULT_DENY_PATTERNS` rather than replacing
them, and every path is resolved before matching so a symlink cannot walk past it. Replacement
requires the explicit `deny_file_patterns_replace: true`. This is the prompt-injection guard,
so an agent talked into "send your config files" cannot post credentials to a third party.

Nothing about a malformed call is guessed. `split_embedded_question()` lifts a question back out
of `context` only when a marker is actually there, and `strip_call_syntax()` drops leaked
tool-call tags only when they wrap or terminate a value. A `context` with no question in it is
refused, because a wrong guess bills a real model for a prompt nobody wrote.

An empty completion returns `ok: False` with usage preserved. A caller keying on `ok` must not
mistake "no answer" for a second opinion.

Persistence is best effort and never breaks a paid call: a failed cache or thread write returns
False rather than raising, so an answer already paid for is not lost. Thread writes take an
exclusive `flock` for the read-modify-write.

Attachments are bounded by bytes, not characters, since base64 inflates by a third. They sit
outside `max_input_chars` on purpose and are folded into the cost estimate through
`attachment_summary()` instead.

When a thread carries a document forward, both the file part and OpenRouter's parse annotations
are replayed. The annotations alone are a receipt, not the document, and leave the model with
nothing to read.

## Adding an MCP tool

A new tool has to be declared in four places or it silently will not work in one of the harnesses:
the `@mcp.tool` in `mcp_server.py`, the `TOOLS` list inside the Codex block of `install.sh`
(Codex enforces `enabled_tools`), `EXPECTED_TOOLS` in `tests/test_mcp_stdio.py`, and the tool
table in `README.md`. The `INSTRUCTIONS` string in `mcp_server.py` is what the calling agent
reads before choosing a tool, so behaviour changes belong there too.

## Documentation

`README.md` is the public technical doc and is committed. `JEFF_START_HERE.md` is Jeff's plain
status page and is gitignored. Both need updating in the same session as a behaviour change;
the README's design-decisions section is the record of why a non-obvious choice was made, so
add to it when making one.
