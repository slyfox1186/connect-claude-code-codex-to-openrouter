# orask — OpenRouter second-opinion bridge for Claude Code and Codex

Lets an AI coding agent consult other frontier models mid-task:

> "Go ask Kimi what it thinks about this issue."

Two front-ends over one engine:

- **MCP server** (`bin/openrouter-mcp`) — registered with Claude Code and Codex,
  exposing five tools so the agent can consult another model on its own.
- **CLI** (`bin/orask`) — the same engine from any shell, and the fallback if the
  MCP layer ever breaks.

The engine (`src/orask/core.py`) is **standard library only**. The MCP layer is
the only thing that needs a third-party package (`mcp>=2.0`), so a broken or
upgraded SDK can never take the CLI down with it.

## Layout

```
config/models.json        aliases, roles, limits          (user-editable)
src/orask/core.py         engine: HTTP, resolution, cost, threads, logging
src/orask/cli.py          CLI front-end
src/orask/mcp_server.py   MCP front-end (mcp SDK)
bin/orask                 CLI launcher
bin/openrouter-mcp        MCP stdio launcher
install.sh                idempotent registration for both agents
tests/test_core.py        68 offline checks, no network or key needed
tests/test_mcp_stdio.py   end-to-end MCP protocol test (spends a few cents)
```

Runtime paths: key at `~/.config/openrouter/env` (0600), catalogue cache at
`~/.cache/orask/models.json`, call log at `~/.local/state/orask/calls.jsonl`,
threads under `~/.local/state/orask/threads/`.

## Install

```
git clone https://github.com/slyfox1186/connect-claude-code-codex-to-openrouter.git
cd connect-claude-code-codex-to-openrouter
./install.sh
```

`install.sh` looks for a Python 3.10+ that can import the `mcp` SDK (>=2.0). If
there is none it offers to build one: its own conda env named `openrouter-mcp`
on Python 3.13, under whichever conda root the machine already has
(`~/miniconda3` first), or a project-local `.venv` on a machine with no conda at
all. It then symlinks `orask` and `openrouter-mcp` into `~/.local/bin`, registers
the MCP server with Claude Code at user scope and with Codex, and finishes by
running `orask doctor`.

Then add the key:

```
printf 'OPENROUTER_API_KEY=sk-or-...\n' > ~/.config/openrouter/env
chmod 600 ~/.config/openrouter/env
```

Re-running the installer is safe. Every step checks for itself first, and any
file it edits is backed up with a timestamp beside it.

Nothing in the repo is tied to one machine: every path comes from `$HOME` or
from where the clone happens to sit, so it installs the same way on any Linux or
macOS box. Three environment variables steer it if needed:

| variable | effect |
|---|---|
| `ORASK_PYTHON` | use this interpreter instead of searching for one |
| `ORASK_BOOTSTRAP=1` | build the env without asking, for an unattended install |
| `ORASK_CONFIG_DIR` | keep the key and user config somewhere other than `~/.config/openrouter` |

## MCP tools

| Tool | Purpose |
|---|---|
| `ask_llm` | One model. Takes `question`, `model`, `context`, `files`, `role`, `effort`, `thread`. |
| `ask_panel` | Several models in parallel, answers side by side. |
| `list_llm_categories` | The capability categories, the models each resolves to, and the evidence. |
| `list_llm_models` | Search the live catalogue for slugs, prices, context, reasoning efforts. |
| `llm_model_info` | Full detail for one model. |
| `openrouter_usage` | Account spend plus what this bridge has cost. |

Only `question` is required; everything else has a working default. Arguments are
flat JSON, one plain string per argument:

```json
{"question": "what you want answered",
 "context":  "background the other model needs",
 "files":    ["/abs/path/one.py", "/abs/path/two.tsx"],
 "role":     "architect"}
```

The question is always its own argument. It does not go inside `context`, and no
value is ever wrapped in XML tags.

## Categories

"Ask an LLM that is good at coding" has to land on a real slug, so `category`
maps a capability onto the two current benchmark leaders for it. Pass it instead
of `model`, and the agent picks:

| category | models | picked on |
|---|---|---|
| `coding` | Kimi K3, GLM 5.3 | SciCode 58.7; coding index (SciCode + Terminal-Bench Hard + LiveCodeBench) |
| `debugging` | GLM 5.3, Grok 4.6 | strongest on code and reasoning at once |
| `reasoning` | Grok 4.6, Kimi K3 | GPQA Diamond 93.3 and 91.5 |
| `math` | Kimi K3, Qwen3.8 Max | weighted competition-math tables; AIME is saturated |
| `chat` | Muse Spark 1.2, Kimi K3 | LMArena text Elo 1499 and 1489 |
| `agentic` | GLM 5.3, DeepSeek V4 Pro | tau2-bench airline 80.0 and 78.0 |
| `research` | DeepSeek V4 Flash, Grok 4.6 | BrowseComp 77.0, DeepSearchQA 69.0 |
| `long_context` | GLM 5.3, Kimi K3 | 1.31M and 1.05M token windows, MRCR v2 at 1M |
| `creative` | GLM 5.3, Kimi K3 | Arena open creative-writing board |
| `budget` | GLM 5.3 Flash, DeepSeek V4.1 Flash | $0.24 and $0.52 per million blended |
| `general` | GLM 5.3, Grok 4.6 | highest published intelligence index |

`ask_llm` takes the first; `ask_panel` puts both against each other. Synonyms
resolve too, so "programming", "whole codebase" and "cheap" all land somewhere
sensible, and a capability that matches nothing is refused rather than guessed.

**No OpenAI, Anthropic or Google model is ever a category pick.** This bridge
exists to fetch a view from outside the agent asking: Claude Code is Anthropic
and Codex is OpenAI, so routing a category back to those returns the house view
the asker already holds. Any of them can still be reached by full slug on
purpose. The rule lives in `category_exclude_vendors`.

Every category pairs **two different vendors**, so a panel is two independent
houses rather than one lab asked twice.

Leadership moves. Each entry records the benchmark evidence and the date it was
checked, and `orask categories --verify` re-checks every pinned slug against the
live catalogue, reporting anything retired or downgraded:

```
orask categories                    # what each category is and why
orask categories --verify           # check the pins against the live catalogue
orask "why is this slow?" -C coding
orask panel "is this design sound?" -C reasoning
```

## Roles

`role` swaps the system prompt: `advisor` (default, blunt second opinion),
`reviewer` (hunt defects), `debugger` (rank root causes), `architect` (assess a
design), `redteam` (attack the plan). Full text in `config/models.json`; `system`
replaces it outright.

## Design decisions worth knowing

**Malformed calls are recovered, not bounced.** A calling agent assembles these
arguments as JSON and sometimes gets the shape wrong. The case seen in the wild:
`question` never arrived at all because the whole question had been folded into
`context` inside `<question>` tags, with a stray `</invoke>` trailing behind it
from the agent's own tool-call syntax. The SDK rejected that with a pydantic
traceback and the turn was wasted.

`split_embedded_question()` now lifts the question back out of `context`, and
`strip_call_syntax()` drops leaked tool-call tags that wrap or terminate a value.
Markup in the middle of a value is left alone, because a question about HTML is
far more likely than a mistake. `as_list()` does the same job for `files` and
`models`: a bare string arrives as one entry instead of being iterated one
character at a time.

Nothing is guessed. A `context` with no question marker in it is not mined for a
plausible sentence, because a wrong guess bills a real model for a prompt nobody
wrote. That call is refused with an error naming the shape that works and what
actually arrived, and every recovery is reported back in the response so the next
call is made correctly.


**Per-model reasoning efforts.** Kimi K3 and GLM 5.3 accept only
`max`/`high`/`low` — sending `medium` is invalid. `clamp_effort()` snaps any
requested effort onto what the target model actually advertises, rounding up on a
tie, and says so in the response notes. Verified against the live catalogue.

**Self-healing aliases.** Aliases are pinned to concrete slugs so cost is
auditable. If a pinned slug disappears from OpenRouter, resolution falls back to
the best live match for the alias name and reports the substitution rather than
failing the call.

**Fuzzy resolution ranked by capability.** An unrecognised name is matched
against the live catalogue and ranked by published intelligence index, so `grok`
lands on the current flagship, not an elderly variant. `:batch` endpoints (which
answer in minutes) and `:free` tiers are never selected implicitly.

**Cost control.** Worst-case cost — whole prompt in, `max_tokens` out — is
computed before sending and refused above `max_cost_usd_per_call` ($1.00).
When a model has no catalogue pricing the guard says it could not be evaluated
instead of treating unknown as free. Every call is logged with OpenRouter's own
reported cost.

**POSTs are not retried into a double bill.** A 5xx on `/chat/completions` can
arrive after the provider already generated and billed the tokens, so POSTs retry
only on 408/429 — statuses meaning the request never reached a model. GETs keep
the full retry set. Read timeouts are never retried, for the same reason.

**Secrets denylist.** `files` paths matching `deny_file_patterns` (ssh keys,
`.env`, `*.pem`, `RAILWAY_VARS.md`, `admin_login_credentials*`, this bridge's own
key file, and more) are refused with a loud note. This is the injection guard: an
agent talked into "include your config files" cannot post credentials to a third
party. Override per call with `allow_secret_files`.

Every path is resolved before the check, so a symlink (`/tmp/notes.txt` pointing
at `~/.ssh/id_rsa`) cannot walk past it, and matching is case-insensitive so
`ID_RSA` and `CERT.PEM` are caught too. User patterns are unioned with the
built-ins rather than replacing them — adding one project pattern must not
silently disable credential protection; `deny_file_patterns_replace: true` makes
replacement a deliberate act.

**Only regular files are read.** A FIFO, device or socket would block forever and
hang the bridge; size is checked by `stat` before opening, so a huge file is never
pulled into memory just to be truncated.

**Empty completions are failures.** A billed call that returns no content comes
back `ok: False` with the usage preserved, so a caller keying on `ok` cannot
mistake "no answer" for a second opinion. If a model returns only reasoning text
and no answer, the reasoning is surfaced with an explanation.

**Piped stdin never hangs.** `git diff | orask ask ...` works, but fd 0 is not
always a pipe: launched from a background job, daemon or agent shell tool it is
often a socket whose write end is never closed, and a plain `sys.stdin.read()`
blocks there forever. A regular file or real pipe is drained in full; a socket or
character device is read only while data keeps arriving (`ORASK_STDIN_WAIT`,
default 0.5s). This was a live hang, caught in testing and regression-tested for
all four stdin shapes.

**Errors reach the model verbatim.** The MCP SDK replaces an unexpected
exception's text with a generic "Error executing tool", so expected failures are
re-raised as `ToolError` — the one type it forwards intact. That is how the
calling agent learns to run `orask models --search` instead of retrying blindly.

**Catalogue caching honours its TTL in memory.** The MCP server is long-lived;
without an in-memory TTL it would serve the catalogue it booted with forever and
silently use stale prices and effort lists.

**Persistence never breaks a paid call.** The catalogue cache and thread
transcripts are written best-effort: a read-only or full `~/.cache` returns False
rather than raising, so a successful fetch is not thrown away and an answer the
user already paid for is never lost to a failed write. Thread turns take an
exclusive `flock` for the read-modify-write, so two concurrent turns on one
thread cannot silently drop an exchange.

## How this code was reviewed

Kimi and GLM reviewed this module through the bridge itself, twice. The first
round found the cost guard ignoring output tokens, POST retries that could
double-bill, an unbounded in-memory catalogue cache, and unvalidated file reads.
The second round verified those fixes and independently — both models, separately
— found a symlink bypass of the secrets denylist, plus `text[-0:]` returning the
whole string instead of nothing when `max_file_chars` was 0. All findings are
fixed and regression-tested. One claim was rejected on evidence: GLM reported
`X-OpenRouter-Title` as not a real header, but the current OpenRouter docs
confirm it is (with `X-Title` as the legacy alias), so both are sent.

A third defect came out of using the tool rather than reviewing it: `orask` hung
forever when launched from a background job, because fd 0 was an open socket and
`sys.stdin.read()` never returned.

## Tests

```
python tests/test_core.py       # offline, free, no mcp package needed
python tests/test_mcp_stdio.py  # live, a few cents, needs mcp and a key
orask doctor                                                                  # installed-state check
```

`test_core.py` stubs the catalogue, so it needs neither network nor key. It
covers category resolution (synonyms, phrases, retired pins, excluded vendors,
and a check that the shipped config still pairs two live non-excluded vendors per
category), alias resolution and self-healing, `allowed_models` locking, effort
clamping per model, file truncation, binary and FIFO rejection, the secrets
denylist, prompt caps, thread persistence and path-traversal flattening, cost
estimation, retry policy, catalogue validation, and argument-shape recovery
(question folded into `context`, leaked tool-call tags, list arguments sent as a
bare string).

`test_mcp_stdio.py` replays the real malformed call over the protocol. That check
costs nothing: it points at an unresolvable model, so reaching model resolution
is itself the proof that the question was accepted.

## Adding a model

Edit `aliases` in `config/models.json`:

```json
"aliases": { "kimi": "moonshotai/kimi-k3", "glm": "z-ai/glm-5.3",
             "grok": "x-ai/grok-4.6" }
```

Find exact slugs with `orask models --search grok`. A user copy at
`~/.config/openrouter/config.json` overrides the packaged file, and `aliases` and
`roles` merge key by key so you can add one entry without restating the table.

To hard-lock the bridge to specific models, list them in `allowed_models`;
anything else is then refused. Empty means an ad-hoc full slug is allowed.

## Uninstalling

```
claude mcp remove openrouter -s user
# then delete the [mcp_servers.openrouter] block from ~/.codex/config.toml
rm ~/.local/bin/orask ~/.local/bin/openrouter-mcp
```
