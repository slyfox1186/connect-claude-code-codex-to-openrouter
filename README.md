# orask: OpenRouter second-opinion bridge for Claude Code and Codex

Lets an AI coding agent consult other frontier models mid-task:

> "Go ask Kimi what it thinks about this issue."

Two front-ends over one engine:

- **MCP server** (`bin/openrouter-mcp`), registered with Claude Code and Codex,
  exposing seven tools so the agent can consult another model on its own.
- **CLI** (`bin/orask`), the same engine from any shell, and the fallback if the
  MCP layer ever breaks.

The engine (`src/orask/core.py`) is **standard library only**. The MCP layer is
the only thing that needs a third-party package (`mcp>=2,<3`), so a broken or
upgraded SDK can never take the CLI down with it.

## Layout

```
config/models.json        aliases, roles, limits          (user-editable)
src/orask/core.py         engine: HTTP, resolution, cost, threads, logging
src/orask/cli.py          CLI front-end
src/orask/mcp_server.py   MCP front-end (mcp SDK)
src/orask/install_config.py safe configuration and interpreter-pin updates
bin/orask                 CLI launcher
bin/openrouter-mcp        MCP stdio launcher
install.sh                idempotent registration for both agents
check.sh                  the gate: lint, format, types, shell syntax, offline tests
pyproject.toml            ruff and mypy config (no [project] table, on purpose)
guides/                   local best-practice cheat sheets, served by read_guide
tests/test_core.py        offline engine checks, no network or key needed
tests/test_mcp_offline.py offline MCP protocol and safety checks
tests/test_cli.py         CLI subprocess and doctor checks
tests/test_boundaries.py provider/configuration boundary checks
tests/test_install.py    isolated installer and launcher checks
tests/test_mcp_stdio.py   live MCP protocol test (five billed completions)
tests/eval_budget.py      opt-in paid baseline/candidate caller-prompt evaluation
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

`install.sh` looks for a Python 3.10+ that can import the `mcp` SDK, which it
installs pinned to `mcp>=2,<3` because `mcp.server.mcpserver` is a 2.0 API. If
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

Re-running the installer checks existing registrations before updating them.
Changed client configurations and the interpreter pin receive private timestamped
backups. Codex edits preserve unrelated sections and use a lock plus atomic
replacement. Malformed, unreadable, symbolic-link or unsupported configuration files
are refused; they are never treated as empty. Common quoted TOML headers and
multiline values are supported, including on Python 3.10. Ambiguous inline/dotted
managed-server definitions require a manual edit. Installer reads are capped at
32 MiB. Existing real launcher files or directories are refused; symlinks can be
updated.

A Claude registration update stops if backup, removal or addition fails. If
addition fails after removal, the backup remains for recovery; rerun the
installer after resolving the error. Restart both clients after server code,
instruction or registration changes. These checks do not replace the clients' own trust settings.

| variable | effect |
|---|---|
| `ORASK_PYTHON` | explicit absolute executable Python 3.10+; invalid values fail without fallback; the installer also requires its MCP SDK import |
| `ORASK_BOOTSTRAP=1` | build the env without asking, for an unattended install |
| `ORASK_CONFIG_DIR` | relocate the key and user config from `~/.config/openrouter` |
| `CLAUDE_CONFIG_DIR` | register in this directory's `.claude.json`, otherwise `~/.claude.json` |
| `CODEX_HOME` | register in this directory's `config.toml`, otherwise `~/.codex/config.toml` |

The selected interpreter is stored in `.orask-python`. Pin changes use a private
atomic replacement and backup; a symlink pin is refused. Launchers otherwise
search the saved pin, known environments and available Python installations.

## MCP tools

| Tool | Purpose |
|---|---|
| `ask_llm` | One model. Takes `question`, `model`, `context`, `files`, `role`, `effort`, `thread`. |
| `ask_panel` | Several models in parallel, answers side by side. |
| `list_llm_categories` | The capability categories, the models each resolves to, and the evidence. |
| `list_llm_models` | Search the live catalogue for slugs, prices, context, reasoning efforts. |
| `llm_model_info` | Full detail for one model. |
| `openrouter_usage` | Account spend plus what this bridge has cost. |
| `read_guide` | Local best-practice guides. Free, no model call. |

For `ask_llm` and `ask_panel`, provide `question` as its own argument.
Arguments use their declared JSON types: text strings, file/model arrays, numeric
budgets and boolean switches. For example:

```json
{"question": "what you want answered",
 "context":  "background the other model needs",
 "files":    ["/abs/path/one.py", "/abs/path/two.tsx"],
 "role":     "architect"}
```

The question is always its own argument. It does not go inside `context`, and no
value is ever wrapped in XML tags.

## Context window

Prompt and answer share one window, and how big it is belongs to the model: no
OpenRouter request parameter raises it. What a caller sets is a budget inside it.

`max_context_tokens` (tool argument, `--max-context-tokens` on the CLI, or
`max_context_tokens` in the config for a standing default) budgets prompt plus
answer into that many tokens. A number above what the model takes is clamped back
down to the model's own window and the answer says so. Left unset, the published window is the fitting limit when available;
file limits and the output cap still apply.

Whatever the budget, the prompt is estimated against it before the call goes out:

- room left over, and `max_tokens` is lowered to fit it, with a note saying so
- no room left, and the call is refused before it is billed, naming the estimate
  and the window

`context_compression` decides that second case instead. `true` sends OpenRouter's
context-compression plugin, which drops text from the middle of the prompt until
it fits and caps the answer at half the window to leave room for what survives.
`false` refuses even on the endpoints of 8k or less that OpenRouter compresses by
default. Unset leaves that default alone.

The window in force, and the estimated prompt size, comes back on every
answer: `context: 41231/200000` in the header line, and `context_window` in the
JSON.

## Guides

`guides/` holds best-practice cheat sheets as plain markdown, one per topic.
They are local files: `read_guide` opens one, no model is called and nothing is
billed.

The point is that an agent should read the house rules for a language *before*
writing it, rather than being corrected afterwards. The index is generated from
the directory at startup and appended to the server instructions, so the calling
agent knows what exists without a tool call, and adding a file to `guides/` is
the whole change.

A long guide is served in widths, because dropping 2,000 lines into a context
window to answer one question costs more than it saves. A guide under 400 lines
comes back whole, since two round trips cost more than the file; a longer one
comes back as a heading tree with each section's length, so the agent can budget
a read before making it:

```bash
orask guide                      # the index: topic and when to read it
orask guide python               # whole guide if short; heading tree if long
orask guide python Subprocess    # one section
orask guide python --all         # the whole file
orask guide --search flock       # every guide at once, with the section named
orask guide --stale              # anything not verified in six months
```

Each file carries front matter with `topic`, `triggers` and `verified`. A guide
with wrong advice is worse than no guide, because it overrides the model's own
judgement, so the date is part of the format and `--stale` is how it gets
audited.

`guide_dirs` in the user config adds machine-local collections without putting a
personal path in this repository. A directory listed there wins over the
packaged copy of the same topic, because it is opt-in configuration; `orask
guide <topic>` prints the winning path. A file with no front matter is indexed
by its h1, so a directory of ordinary documents is still routable.

Front matter reaches the calling agent's system prompt, so it is collapsed to
one bounded line per guide and the list is capped. A directory named in
`guide_dirs` is trusted content by that route. The guide *body* is only ever a
tool result, which is data.

The index in the server instructions is built once at startup, because MCP sends
instructions during initialize and cannot change them afterwards. A guide added
mid-session appears in `read_guide` immediately and in the instructions after a
restart; the instructions say so.

Every path enters through one function, `_guide_map()`, which resolves each
candidate and confirms it sits inside the directory it was found in. Listing,
searching and reading all go through it, so a symlink planted in a guide
directory cannot be read by any of the three. The `topic` argument is
pattern-checked before it is joined to a path as well: the pattern stops `../`,
the resolve-and-contain check stops the symlink, and both are needed.

Headings are found with fenced code blocks excluded. A reference manual is full
of samples whose `##` lines look like headings to a line scan, and treating one
as real ends a section slice in the middle of an example.

## Sending files

Never paste a file into the question. Put its path in `files` and the bridge
sends the file itself.

| What you point at | What goes over the wire |
|---|---|
| Source, config, prose, a diff | Pasted in as fenced text, labelled with its full path |
| PDF | Attached as a file part, parsed by OpenRouter before the model reads it |
| PNG, JPG, WEBP, GIF | Attached as an image part |
| WAV, MP3, OGG, FLAC, M4A, AAC | Attached as an audio part |
| A directory | The files inside it, pruning `.git`, `node_modules` and build output |

The type is decided by the file's magic bytes first and its extension second, so
a screenshot saved with no suffix still attaches as an image.

```bash
orask "what does this diagram show?" -f ~/shots/arch.png -m kimi
orask "does this spec contradict the code?" -f ~/docs/spec.pdf -f src/api.py
orask "review this package" -f src/orask/ --role reviewer
```

OpenRouter can parse PDFs for models without native file input. `pdf_engine` picks how: `cloudflare-ai` (the default, free, right for a text
PDF), `mistral-ocr` (reads scanned pages, billed per 1,000 pages) or `native`
(models that take a file directly). Images and audio need a model that accepts
that modality; ask one that does not and the file is held back with a note
naming what it does accept, rather than spending a call on a request that fails.
`llm_model_info` reports the modalities of any model.

Attachments are bounded by bytes rather than by characters, since base64 inflates
anything by a third: `max_attachment_bytes` per file, `max_attachment_total_bytes`
per call, `max_attachments` per message, `max_dir_files` per expanded directory.
The secrets denylist applies to attachments exactly as it does to text, so a
private key does not become sendable by having binary contents.

### Following up on a document

Name a `thread` and the attachment stays with it, so the next question can reuse its saved document context:

```bash
orask "what does clause 4 say?" -f contract.pdf -t contract
orask "does clause 9 contradict it?"            -t contract
```

The bridge replays the original file parts and any saved assistant annotations.
PDF annotations contain parsed text and sometimes images; they are document data.
Reusing them can avoid another parsing charge, but the follow-up still sends
content and incurs model input/output charges. See [OpenRouter's PDF annotation
format](https://openrouter.ai/docs/guides/overview/multimodal/pdfs).

New and replayed attachments share the current byte/count limits, and stored
image/audio inputs must be compatible with the follow-up model. Parsed annotation
text contributes to context estimates; embedded images contribute to attachment
limits and cost estimates. The cumulative serialized attachment and annotation
budget is `thread_attachment_bytes` (4 MiB). If saving the turn would exceed it,
the existing transcript is preserved and the answer reports that the turn was
not saved. Pass the needed files again or choose a new thread.

## Categories

"Ask an LLM that is good at coding" has to land on a real slug, so `category`
maps a capability onto two configured model pins. Pass it instead
of `model`, and the agent picks:

| category | configured models |
|---|---|
| `coding` | Kimi K3, GLM 5.3 |
| `debugging` | GLM 5.3, Grok 4.6 |
| `reasoning` | Grok 4.6, Kimi K3 |
| `math` | Kimi K3, Qwen3.8 Max |
| `chat` | Muse Spark 1.2, Kimi K3 |
| `agentic` | GLM 5.3, DeepSeek V4 Pro |
| `research` | DeepSeek V4 Flash, Grok 4.6 |
| `long_context` | GLM 5.3, Kimi K3 |
| `creative` | GLM 5.3, Kimi K3 |
| `budget` | GLM 5.3 Flash, DeepSeek V4.1 Flash |
| `general` | GLM 5.3, Grok 4.6 |

These are the packaged selections recorded on 2026-09-10, not a live benchmark
ranking. Exact slugs, selection rationale and dates live in `config/models.json`.

`ask_llm` takes the first; `ask_panel` puts both against each other, which is
what a plural request means. "Use the coding LLMs to review this" is passed
through as `category` verbatim: `resolve_category()` matches the name, the
synonyms, or any phrase containing one, and refuses rather than guesses when
nothing matches. Synonyms
resolve too, so "programming", "whole codebase" and "cheap" all land somewhere
sensible, and a capability that matches nothing is refused rather than guessed.

**No OpenAI, Anthropic or Google model is ever a category pick.** This bridge
exists to fetch a view from outside the agent asking: Claude Code is Anthropic
and Codex is OpenAI, so routing a category back to those returns the house view
the asker already holds. Any of them can still be reached by full slug on
purpose. The rule lives in `category_exclude_vendors`.

Every category pairs **two different vendors**, so a panel is two independent
houses rather than one lab asked twice.

Each entry records its selection rationale and date. `orask categories --verify`
requires a fresh catalogue and exits nonzero for missing or excluded pins. It
checks availability and policy, not benchmark leadership:

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


**Per-model reasoning efforts.** `clamp_effort()` maps the requested level to
published model levels, rounding upward on a tie and reporting substitutions.
Use `llm_model_info` for the current advertised levels rather than relying on a
fixed per-model effort table.

MCP calls default to `max` independently of the CLI's configurable default.
Claude Code and Codex must request `max` or `xhigh`; `medium` requires a concrete
task-specific `effort_reason`. Low, minimal and disabled reasoning are rejected
before billing. A preferred effort maps to the model's published levels; a model
whose strongest level is `high` can use that level, and mapping never drops below
medium. Models without published compatible reasoning levels are refused by MCP.
MCP also sends `provider.require_parameters=true` to restrict routing to providers
that support the request parameters, as described in [OpenRouter provider
routing](https://openrouter.ai/docs/guides/routing/provider-selection). This may
refuse a request when no compatible provider is available. The CLI retains its
existing effort choices and packaged `high` default.

**Choosing a reply budget.** `max_tokens` covers reasoning and the final answer
together. A large context window does not prevent a small output cap from being
used entirely for reasoning. Before a substantial consultation, the caller uses
`llm_model_info` (including `bridge_limits`) to choose output and context budgets
for the actual task. The configured default is a starting point. If a call ends
with `length`, inspect its usage, increase the output allowance within the model
and cost limits, or split the work. Retry only the incomplete model in a panel.
There is no automatic paid retry, and exhaustive reviews must not use compression.

**Self-healing aliases.** Aliases are pinned to concrete slugs so cost is
auditable. If a pinned slug disappears from OpenRouter, resolution falls back to
the best live match for the alias name and reports the substitution rather than
failing the call.

**Fuzzy resolution ranked by capability.** An unrecognised name is matched
against the live catalogue and ranked by published intelligence index, so `grok`
lands on the current flagship, not an elderly variant. `:batch` endpoints (which
answer in minutes) and `:free` tiers are never selected implicitly.

**Cost control.** The preflight estimate includes the estimated prompt, the full
chosen output cap and known request fees. It refuses an estimate above
`max_cost_usd_per_call` ($1.00 by default, per model). This is a heuristic guard,
not a spending ceiling: token estimates, attachment processing, routing prices
and OCR page estimates can differ from the eventual bill. Unknown pricing warns
by default; `cost_guard_on_unknown_pricing: "block"` refuses it instead. Explicit
zero prices and provider-reported zero cost are preserved. Results prefer
provider-reported cost and otherwise fall back to available usage/catalogue data;
missing accounting does not prove a free call. Call logging is best effort.

**Retry policy.** Completion POSTs retry only HTTP 408/429. They do not retry 5xx
or read timeouts, to reduce duplicate-billing risk after an ambiguous failure.
GETs use a broader retry set. This policy cannot guarantee that every failed
request was unbilled.

**Typed failures are translated.** OpenRouter returns a stable `error_type` at
`error.metadata.error_type` on `/chat/completions`. The image ones each have a
different fix, so `image_too_large`, `unsupported_image_format`, `invalid_image`
and the rest are turned into the sentence that says what to do instead of a bare
HTTP 400. An unrecognised type is still printed rather than swallowed.

**The OCR page charge is inside the guard, not outside it.** `mistral-ocr` bills per
1,000 pages on top of tokens, which a token-only estimate cannot see: a long scan
could pass the $1.00 guard and then bill separately. There is no way to count pages
before the parse, so they are inferred from the file size using a
bytes-per-page heuristic, priced from `mistral_ocr_usd_per_1k_pages`, and folded into
the estimate. The answer says the count is inferred rather than parsed.

**The cost guard estimates attachments, and says that it is estimating.**
OpenRouter has no preflight token-counting endpoint and does not publish how a
provider tiles an image, so an attachment's share of the pre-flight estimate is a
heuristic, and the answer says so. The real numbers come back
afterwards in `usage.prompt_tokens_details` (`audio_tokens`, `video_tokens`,
`cached_tokens`), which the result now reports.

**Secrets denylist.** `files` paths matching `deny_file_patterns` are refused with a
loud note: ssh keys, `.env`, `*.pem`, cloud credential stores (gcloud's
application-default file, `.azure`, the `gh` token store), database and tooling
secrets (`.pgpass`, `.my.cnf`, `.s3cfg`, terraform vars, gem and cargo credentials),
both git configs since a remote URL routinely carries a token, `RAILWAY_VARS.md`,
`admin_login_credentials*`, this bridge's own key file, and `/proc/<pid>/environ`,
which holds this process's own environment and therefore the API key itself. This reduces accidental disclosure through known file paths. Filename rules
cannot detect arbitrary copied secrets or credentials pasted into context;
callers must inspect what they send. Override per call with `allow_secret_files` from the
CLI, or from a tool call only when the config permits it (see below).

Every path is resolved before the check, so a symlink (`/tmp/notes.txt` pointing
at `~/.ssh/id_rsa`) cannot walk past it, and matching is case-insensitive so
`ID_RSA` and `CERT.PEM` are caught too. User patterns are unioned with the
built-ins rather than replacing them, because adding one project pattern must
not silently disable credential protection; `deny_file_patterns_replace: true` makes
replacement a deliberate act. Replacement and MCP override permissions require
JSON `true`; strings such as `"false"` never grant permission. Policy checks both
the requested path and resolved target, and detects hardlinks to the known API
key file, including when `ORASK_CONFIG_DIR` relocates it.

**Only regular files are read.** A FIFO, device or socket would block forever and
hang the bridge. The descriptor is opened first and then checked with `fstat`, which
also closes the race where a regular file is swapped for a FIFO between the check
and the open, and the size is read from that same descriptor so a huge file is never
pulled into memory just to be truncated.

**The type is decided by the content, not only by the name.** Magic bytes win, but
`ID3` is three bytes, so a CSV whose first column is called `ID3` used to attach as
an MP3; a real ID3v2 tag names its major version next. The extension still carries
the formats that have no signature (an untagged MP3 starts with a frame sync), but a
file whose first 64 bytes read as plain text is taken at its word over its name, so
`notes.mp3` full of text is not sent as corrupt audio and billed.

**The safety overrides are not the calling agent's to set.** `allow_secret_files`
and `allow_expensive` are tool arguments, which means an agent that can be talked
into asking for a credential can be talked into passing the override alongside it in
the same call. For tool calls both are refused unless the config opts in with
`mcp_allow_secret_files` or `mcp_allow_expensive`, and the refusal names the key that
would permit it. The CLI flags are a person typing them and are unchanged.

**A context window is fitted, never set.** OpenRouter has no parameter that changes a
model's context length, so `max_context_tokens` can only budget below it and is clamped
to the published window otherwise. The fit runs before the cost guard, so the guard
prices the cap that is really sent, and a prompt with no room left for a reply is refused
without being billed rather than answered in half a sentence. `context_compression` is
tri-state on purpose: unset sends no plugin at all, because `enabled: false` is itself a
decision and it turns off the compression an endpoint of 8k or less applies for you.

**An output cap is always chosen and always sent.** With no `max_tokens`, no
`default_max_tokens` and no published provider ceiling, the old code sent no cap and
priced zero output tokens, so any prompt passed the guard while the provider
generated to its own limit. There is no state now in which the guard prices the
answer at nothing.

**A failed catalogue fetch is remembered.** A single `ask()` looks the catalogue up
five or six times; with the network down each of those was a full retry cycle. One
failure suppresses the next attempt for a minute. The fetch also takes a lock, so a
cold panel makes one `/models` request rather than one per worker.

**An error in a 200 body is still an error.** OpenRouter can answer 200 and put the
failure in the body, which used to surface as "returned no choices" plus a raw dump.
It goes through the same typed-error translation as any other failure.

**Incomplete completions are failures.** Empty answers, reasoning-only responses,
`finish_reason: length`, and filtered/error finishes return `ok: false` and `incomplete: true`, preserving
usage and any partial answer. They do not count as answered panel members or enter
completed thread history. Reasoning is visible only with `include_reasoning`.
This intentionally corrects the earlier behavior that counted unfinished reasoning
or truncated text as a successful consultation.

**Piped stdin is bounded.** `git diff | orask ask ...` works, but fd 0 is not
always a pipe: launched from a background job, daemon or agent shell tool it is
often a socket whose write end is never closed, and a plain `sys.stdin.read()`
blocks there forever. Every shape goes through `select`. A regular file or real pipe
is waited on until the deadline, because a slow producer is normal; a socket or
character device is read only while data keeps arriving (`ORASK_STDIN_WAIT`, default
0.5s). Both are bounded by a 32 MB ceiling and a 30s deadline (`ORASK_STDIN_DEADLINE`),
Reaching either hard limit refuses the call instead of sending a partial prompt.
Both timing settings must be finite positive seconds. This was a live hang, caught in testing and
regression-tested for all four stdin shapes.

**Expected failures reach the caller.** The MCP SDK replaces an unexpected
exception's text with a generic "Error executing tool", so expected failures are
re-raised as `ToolError`, the one type it forwards intact. That is how the
calling agent learns to run `orask models --search` instead of retrying blindly.

**Catalogue caching honours its TTL in memory.** The MCP server is long-lived;
without an in-memory TTL it would serve the catalogue it booted with forever and
silently use stale prices and effort lists.

**Persistence is best effort.** A failed cache or thread write does not discard
the provider answer. Thread save failures are reported with the result. Private
state files reject unsafe links and nonregular files; transcript updates are
atomic and preserve malformed existing files for recovery. A stable exclusive
lock merges concurrent saves, waiting at most 10 seconds before refusing the
write. This protects saved exchanges; simultaneous questions can still have read
the same earlier history. Call-log readers skip malformed records and read a
bounded tail, so local totals are not an authoritative billing ledger.

`usage` scans at most the newest 4 MiB of the log and reports its coverage.
`bridge_costs_unknown`, `bridge_timestamps_unknown`, `bridge_log_records_skipped`
and `notes` disclose incomplete accounting. Totals include known costs only;
invalid/future timestamps are excluded from the last-24h total. Unavailable totals
are `null`. The existing `bridge_calls_logged` field still counts successful answers.
Transcript writes also enforce the reader's 32 MiB serialized-file limit.

**Configuration and provider boundaries.** Collection fields are validated before
use: aliases/roles are objects of strings and model/deny lists are arrays of
strings. Invalid numeric configuration falls back to safe defaults; invalid
per-call budgets are refused. Optional malformed usage metadata does not discard
a usable paid answer. Error bodies are bounded and closed, and successful HTTP
response bodies are limited to 64 MiB.

**Strict CLI and doctor checks.** Unknown commands/options and invalid arguments
exit nonzero. A single incomplete answer exits nonzero; a panel exits zero if
at least one model succeeded, so inspect each result for partial panel failures.
`doctor` requires fresh catalogue/account access and checks user-scope command,
interpreter and tool settings for this checkout. It cannot prove a running
client's handshake, project overrides or trust policy. Python 3.10 lacks stdlib
TOML parsing, so doctor reports Codex validation unavailable; run that check with
Python 3.11+. Installer editing and ordinary consultations still support 3.10.

## Tests

```bash
./check.sh                         # lint, format, types, shell syntax, offline tests
PY=$(cat .orask-python)            # absolute interpreter selected by install.sh
"$PY" tests/test_core.py           # offline engine checks
"$PY" tests/test_mcp_stdio.py       # live: five billed completions, needs SDK and key
"$PY" tests/eval_budget.py --live   # paid prompt comparison, 16 calls by default
orask doctor                      # installed-state check; network, no completion
```

`check.sh` is the required gate. It runs Ruff lint and format checks, mypy, Bash
syntax checks, and the core, MCP protocol, CLI, provider/configuration boundary,
and installer suites. Offline fixtures use scratch configuration/state/cache and
fake client commands; they do not require the real key or paid model calls.
Coverage includes malformed provider data, effort/budget policy, incomplete
answers, attachment replay, private persistence, stdin limits, client registration
and installer failure recovery. Live tests are separate opt-in checks.

The supported minimum is Python 3.10 with MCP 2.x. Linux offline checks cover the
minimum and current project environments; macOS execution remains unverified.
Formatting changes are kept in a separate mechanical commit from behavior fixes.

## Adding a model

Edit `aliases` in `config/models.json`:

```json
"aliases": { "kimi": "moonshotai/kimi-k3", "glm": "z-ai/glm-5.3",
             "grok": "x-ai/grok-4.6" }
```

The packaged aliases are `kimi` → `moonshotai/kimi-k3`, `glm` → `z-ai/glm-5.3`,
`grok` → `x-ai/grok-4.6` and `gemini` → `google/gemini-3.8-flash`.
`default_model` is `kimi`; `default_panel` includes all four aliases. Each distinct
resolved model is a separate billed consultation. The category vendor exclusion
applies to category selection; it does not remove Gemini from this explicit
panel or prevent a deliberately named model.

Find exact slugs with `orask models --search grok`. A user copy at
`~/.config/openrouter/config.json` overrides the packaged file, and `aliases` and
`roles` merge key by key so you can add one entry without restating the table.

To hard-lock the bridge to specific models, list them in `allowed_models`;
anything else is then refused. Empty means an ad-hoc full slug is allowed.

## Uninstalling

```
claude mcp remove openrouter -s user
# then delete the [mcp_servers.openrouter] block (and its subtables)
# from ${CODEX_HOME:-$HOME/.codex}/config.toml
rm ~/.local/bin/orask ~/.local/bin/openrouter-mcp
```
