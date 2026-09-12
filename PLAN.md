# Repository audit implementation plan

Date: 2026-09-12

**Goal:** Audit the documented CLI/MCP bridge, reproduce defects, fix them in
independently revertible commits, and verify the release.

**Architecture:** Keep the standard-library engine and thin CLI/MCP adapters.
Preserve public arguments, model pins, dependency ranges, and deliberate retry
policy. Add regression coverage at the boundary where each failure originates.

**Stack:** Python 3.10+, Bash on Linux/macOS, MCP SDK 2.x. This machine uses
`/home/jman/miniconda3/envs/openrouter-mcp/bin/python` (Python environment verified;
installed MCP 2.2.0, Ruff 0.16.7, mypy 2.3.1).

## Evidence and constraints

- Initial worktree clean on main. Existing `./check.sh` passes Ruff, mypy,
  shell syntax and 296 offline assertions. No build/package target exists.
- README and CLAUDE.md define local stdio/CLI operation. Live Railway status
  resolves to unrelated `slyfox1186/coastaltechgroup`; never deploy there.
  Ask for the intended target before any deployment work.
- The required LLM_SYSTEM_PROMPT_INSTRUCTIONS.md is missing. Prompt-content
  review/change remains blocked until the user supplies its current location.
- The local Git remote contained a credential. Removed credentials from the
  remote URL; credential rotation is a user action. Never include .git/config,
  runtime state, keys, private docs or unrelated files in external review.
- Format policy conflict: local notes exclude Ruff formatting. The current
  request explicitly asks for a format gate. Flag this, make formatting a
  separate mechanical commit, and add `ruff format --check` to the gate.
- Use Context7 and official online docs for MCP, urllib/OS APIs and tooling.
  MCP v2 docs confirm ToolError is the expected actionable-failure mechanism.

## Execution and pass/fail rules

For each task below: add a focused regression, run it against the old code and
record the observed failure, implement the smallest causal fix, run the complete
`./check.sh`, inspect `git diff --check`, commit only that task, then run the full
gate again. Use the executing-plans workflow inline. No deployment or live
registration changes while developing; installer tests use isolated home/config
trees and fake external commands. Offline tests must never reach OpenRouter.

- [ ] Send this plan and every tracked script, source file, test, configuration
  and guide to the OpenRouter coding panel. Require concrete reproductions and
  locations, no prompt rewriting, no dependency or public-interface changes.
  Save opinions locally and verify every proposed finding before adoption.
- [ ] Safety/config/cost (`core.py`, offline tests): reproduce string booleans
  enabling overrides, non-finite numeric settings, missing/invalid pricing
  being treated as free, relocated key files and denied symlink names escaping
  policy. Validate at input boundaries; preserve true zero pricing and explicit
  CLI overrides. Test the actual ask path with a captured offline transport.
- [ ] Persistence (`core.py`, offline tests): test permissive umask, lock failure,
  corrupt JSON shapes, thread-list names usable for follow-ups, atomic temporary
  cleanup and symlink handling. Keep paid answers even if persistence fails;
  never continue read-modify-write without its lock. Do not silently migrate or
  delete existing transcripts.
- [ ] HTTP/provider boundaries (`core.py`, offline tests): exercise real urllib
  handling with fake responses, including HTTPError cleanup, connection resets,
  malformed error/choice/usage structures and explicit zero cost. Preserve
  useful answers when optional metadata is malformed. Keep POST retry policy.
- [ ] Installer/launchers (`install.sh`, `bin/_python-env.sh`, new installer
  tests): reproduce config loss on read errors, TOML quoted/commented headers,
  multiline arrays/strings, drift/idempotence, backup failure and launcher
  collisions. Refuse ambiguous edits and preserve unrelated config. Check the
  explicit interpreter version and avoid silently selecting another interpreter
  when the override is invalid. Verify with fixtures, never real agent configs.
- [ ] CLI/discovery/guides (`cli.py`, `core.py`, tests): reproduce category prose
  corrupting JSON; malformed stdin environment settings and silent truncation;
  category verification falsely succeeding offline; nested Markdown fences and
  invalid/stale guide dates. Fix only demonstrated errors and document behavior.
- [ ] Gate and maintainability (`check.sh`, `pyproject.toml`, tests): make the gate
  independent of caller cwd; fail safely if scratch allocation fails; clean test
  scratch state; add offline real MCP stdio coverage separately from the stdlib
  unit suite. Add the format gate in its own commit. Remove obsolete suppressions
  only after checking the relevant types; explain any retained suppression.
- [ ] Docs: update README and local JEFF_START_HERE.md for affected behavior and
  verification commands. Correct overclaims (cost estimates are heuristics, not
  absolute spending guarantees; test count; default panel; actual thread replay).
  Read all remaining guides and validate material technical claims against
  primary sources. Record any uncertain benchmark claims rather than change pins.
- [ ] Final audit: inspect complete affected success/error paths; run full gate,
  real offline stdio, CLI subprocess and installer fixture tests, supported Python
  syntax/version checks where available. Report macOS/runtime versions not run.
- [ ] Release: fetch and safely rebase, run gate again if upstream changed, push
  without force, verify HEAD equals upstream and clean worktree. Inspect GitHub
  checks. Deploy/monitor only a confirmed service belonging to this repository;
  report deployment as blocked if the target remains unknown.

## Review disposition

External opinions are proposals, not evidence. Track accepted, rejected and
deferred suggestions here with test results and reasons as review proceeds.

First panel: Kimi K3 and GLM 5.3, $0.5068 total. Both exhausted their reply
budgets and returned unfinished reviews. A second pass requests concise final
findings at low effort, with every file attached again.

- Confirmed locally: 18 failing safety assertions; follow-up coverage also
  reproduces omitted per-request fees and ignored explicit zero usage cost.
- Add strict structural validation for collection config (aliases, categories,
  deny patterns): the panel correctly identified unsafe truthiness/iteration.
- Reject the SDK-import concern: installed MCP 2.2.0 import succeeds and current
  primary v2 docs confirm MCPServer. Do not change the dependency range.
- Do not accept the suggestion that failed locks are harmless: unlocked
  read-modify-write contradicts the documented no-lost-exchanges invariant.
- Treat unset compression wording as a docs issue: the existing local refusal
  is safer than silently dropping review context. Retain it and clarify docs.
