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
- The named prompt-policy file is missing; the owner supplied its full contents
  in the conversation, which governs runtime prompt work.
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

- [x] Send this plan and every tracked script, source file, test, configuration
  and guide to the OpenRouter coding panel. Require concrete reproductions and
  locations, no prompt rewriting, no dependency or public-interface changes.
  Save opinions locally and verify every proposed finding before adoption.
- [x] Safety/config/cost (`core.py`, offline tests): reproduce string booleans
  enabling overrides, non-finite numeric settings, missing/invalid pricing
  being treated as free, relocated key files and denied symlink names escaping
  policy. Validate at input boundaries; preserve true zero pricing and explicit
  CLI overrides. Test the actual ask path with a captured offline transport.
- [x] Persistence (`core.py`, offline tests): test permissive umask, lock failure,
  corrupt JSON shapes, thread-list names usable for follow-ups, atomic temporary
  cleanup and symlink handling. Keep paid answers even if persistence fails;
  never continue read-modify-write without its lock. Do not silently migrate or
  delete existing transcripts.
- [x] HTTP/provider boundaries (`core.py`, offline tests): exercise real urllib
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

## Priority update from the owner

The owner supplied the complete prompt-engineering policy in the conversation,
removing the prompt-review blocker. Budget exhaustion is now the first priority.
Call map: MCP initialize instructions plus ask tool descriptions -> Claude Code/
Codex choose parameters -> core.ask builds config role system + request data ->
OpenRouter model -> core status/usage -> CLI/MCP rendering and thread persistence.

Runtime baseline: first full-repository panel, high effort and 10,000 output
cap, produced no final answer in 2/2 calls ($0.5068). Second panel changed effort
to low and cap to 16,000; both completed ($0.5114). This is operational evidence,
not a controlled claim about prompt quality. Add caller-budget guidance,
discoverable bridge limits, and explicit incomplete status. Public behavior
change is intentional and flagged: reasoning-only and length-truncated results
no longer count as success or enter completed thread history. No automatic paid
retry, universal token cap, or context compression for exhaustive source review.

Evaluate baseline and candidate caller instructions against the same budget
scenarios/model/settings; keep held-out cases distinct. Record decisions, costs,
latency and limitations. A prompt cannot guarantee a model finishes within a cap.

Second panel completed: Kimi K3 and GLM 5.3, $0.5114. Accepted hypotheses:
attachment replay ceilings, non-object config validation, input environment
validation, live verification false positives, TOML decorated headers. Verify
before fixing. Reject GLM's claim that the Codex backup failure is ignored:
its cp command is under active set -e. Claude backup failure remains real.
Reject writing new unmerged transcript sidecars on lock failure: retain the
paid answer in the result and report save failure, without a new storage format.
Hardlinks/copies of arbitrary credentials exceed a path denylist's guarantee;
protect the known API-key inode and document that filenames cannot detect all
copied secrets, rather than adding a broad content regex that blocks source.

## Budget and effort verification

MCP now defaults to max, accepts max/xhigh or medium with a task-specific reason,
and refuses low/off before a request. This intentionally restricts MCP's public
effort argument per the owner; CLI effort behavior is unchanged. Reasoning-only
and truncated responses preserve usage/partial text but are incomplete, excluded
from answered counts and thread history. Caller instructions expose budget
selection/recovery criteria and model_info exposes current bridge limits.

Controlled surrogate evaluation: GLM 5.3, max effort, 16,000 output cap, four
synthetic scenarios repeated twice per instruction version (16 billed calls).
Baseline met 4/8 complete decision criteria; candidate met 8/8, including 4/4
held-out cases. Discovery effort is not scored because discovery is free and has
no effort argument. Baseline cost $0.202868, candidate $0.166539; mean latency
34.39s versus 44.88s. These small samples do not establish universal improvement
or Claude/Codex harness compliance. Saved decisions/usage: tests/fixtures/
budget_results.json; rerun tests/eval_budget.py --live with the pinned interpreter.
No critical regression observed in these cases. Total evaluation cost $0.369407.
Reviewer-role wording was inspected and covered by consultation paths, but has
no isolated controlled quality comparison; do not claim measured reviewer gains.

Gate: ./check.sh passes 339 engine assertions, real offline MCP protocol tests,
Ruff, mypy and Bash syntax. Earlier red runs reproduced 10 incomplete-response
and 8 effort-policy failures before fixes. Live protocol suite now uses max effort.

Persistence regression: 8 failures reproduced before fixes. New transcripts/logs
are private under umask 000; failed locks never permit writes, corrupt transcript
shapes and symlinks are handled safely, temporary replacement cleanup preserves
old data, and growth/ancestor-symlink reads are refused. Existing six-worker
concurrent transcript test still passes. Full gate passes after this change.
The initial short-name regression passed, but a later long-name reproduction
confirmed that truncation broke listed-name replay; that finding is now fixed.

Live protocol validation after budget changes passed every check: real max-effort
single calls, complete and partial panels, discovery, malformed-argument recovery,
and a PDF codeword returned from the attachment. No low-effort calls were used.

Boundary verification: 23 failed assertions and 10 errors reproduced with malformed
config/catalogue/response fixtures. All now pass, including bounded and closed
HTTP bodies, single-attempt reset failure, retained valid answers and billed
errors, and truthful live catalogue verification. New tests/test_boundaries.py
is in the full gate, alongside the CLI subprocess suite. No dependency or model
version changed. Public correction: malformed/absent choices return incomplete
with usage rather than a raw-payload exception. Collection config type errors
are refused early; negative/boolean numeric limits no longer disable guards.

Replay/path audit: ten failures reproduced for aggregate attachment ceilings,
cumulative thread budgets, known-key hardlinks, malformed saved parts, iterable
files, and guide symlink/fence handling. Date validation reproduced another failure.
All pass in tests/test_boundaries.py and the full gate. Historical attachment
overflow now refuses before billing; fresh-file skip behavior remains documented.
Known-key inode checks occur on the open descriptor. Arbitrary copied credential
content cannot be guaranteed safe by a filename denylist and is explicitly deferred.

Minimum-version execution: created an isolated Conda environment at
/home/jman/miniconda3/envs/orask-audit-py310, Python3.10.21/MCP2.0.0 with the same
Ruff/mypy versions. Initial run exposed a test-client constructor API difference
and five tests assuming stdlib tomllib. Explicit stdio_client transport fixes
SDK compatibility; tests verify the explicit unavailable doctor result on3.10.
Full gate passes on both3.10/MCP2.0 and3.13/MCP2.2. No dependency range changed;
full semantic doctor validation on3.10 is deliberately unavailable, documented.

Second persistence review reproduced five further failures, now passing: parsed
PDF annotations participate in text/image and storage accounting; malformed
base64 padding cannot evade byte limits; long displayed thread names resume;
serialized transcripts cannot exceed the reader's 32 MiB limit; and an incomplete
JSONL tail cannot consume the next billed record. OpenRouter's current PDF schema
confirms annotations contain parsed content, contradicting the old code comment.
Corrected that comment and report attachment retention only after a confirmed save.
Full gate passes: 354 core assertions, 93 CLI checks, 25 boundary tests and offline MCP.
