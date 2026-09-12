# orask: OpenRouter second-opinion bridge for Claude Code and Codex

Lets an AI coding agent consult other frontier models mid-task:

> "Go ask Kimi what it thinks about this issue."

Two front-ends over one engine:

- **MCP server** (`bin/openrouter-mcp`), registered with Claude Code and Codex,
  exposing seven tools so the agent can consult another model on its own.
- **CLI** (`bin/orask`), the same engine from any shell, and the fallback if the
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
check.sh                  the gate: lint, types, shell syntax, offline tests
pyproject.toml            ruff and mypy config (no [project] table, on purpose)
guides/                   local best-practice cheat sheets, served by read_guide
tests/test_core.py        273 offline checks, no network or key needed
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
| `read_guide` | Local best-practice guides. Free, no model call. |

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
orask guide python               # heading tree only
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

A PDF works on every model, because OpenRouter parses it before the model sees
it. `pdf_engine` picks how: `cloudflare-ai` (the default, free, right for a text
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

Name a `thread` and the attachment stays with it, so the next question about the
same PDF does not mean sending it again:

```bash
orask "what does clause 4 say?" -f contract.pdf -t contract
orask "does clause 9 contradict it?"            -t contract
```

The turn is stored with its attachment parts, and OpenRouter's file annotations
from the answer are stored alongside and replayed on the assistant turn. The
annotations are what let OpenRouter recognise a document it has already parsed
and skip the parse, which is where the `mistral-ocr` per-page charge would land.

They are only a parse receipt, not the document: annotations replayed without the
file leave the model with nothing to read. That is why the file part is carried
too, which is also how OpenRouter's own example does it. A live probe caught the
difference, and `test_core.py` now pins it.

Carrying base64 in a transcript has a budget of its own, `thread_attachment_bytes`
(4 MB). Over that the attachment is not kept and the answer says to pass the file
again on the next turn, rather than leaving a follow-up that quietly cannot see
the document.

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


**Per-model reasoning efforts.** They genuinely differ: Kimi K3 and GLM 5.3
accept `max`/`high`/`low` and reject `medium`, while Grok 4.6 accepts
`xhigh`/`high`/`medium`/`low` and has no `max`. `clamp_effort()` snaps any
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

**Cost control.** Worst-case cost, meaning the whole prompt in and `max_tokens`
out, is computed before sending and refused above `max_cost_usd_per_call` ($1.00).
When a model has no catalogue pricing the guard says it could not be evaluated
instead of treating unknown as free. Every call is logged with OpenRouter's own
reported cost.

**POSTs are not retried into a double bill.** A 5xx on `/chat/completions` can
arrive after the provider already generated and billed the tokens, so POSTs retry
only on 408/429, the statuses that mean the request never reached a model. GETs keep
the full retry set. Read timeouts are never retried, for the same reason.

**Typed failures are translated.** OpenRouter returns a stable `error_type` at
`error.metadata.error_type` on `/chat/completions`. The image ones each have a
different fix, so `image_too_large`, `unsupported_image_format`, `invalid_image`
and the rest are turned into the sentence that says what to do instead of a bare
HTTP 400. An unrecognised type is still printed rather than swallowed.

**The OCR page charge is inside the guard, not outside it.** `mistral-ocr` bills per
1,000 pages on top of tokens, which a token-only estimate cannot see: a long scan
could pass the $1.00 guard and then bill separately. There is no way to count pages
before the parse, so they are inferred from the file size at a deliberately small
bytes-per-page figure, priced from `mistral_ocr_usd_per_1k_pages`, and folded into
the estimate. The answer says the count is inferred rather than parsed.

**The cost guard estimates attachments, and says that it is estimating.**
OpenRouter has no preflight token-counting endpoint and does not publish how a
provider tiles an image, so an attachment's share of the pre-flight estimate is a
deliberately high heuristic, and the answer says so. The real numbers come back
afterwards in `usage.prompt_tokens_details` (`audio_tokens`, `video_tokens`,
`cached_tokens`), which the result now reports.

**Secrets denylist.** `files` paths matching `deny_file_patterns` are refused with a
loud note: ssh keys, `.env`, `*.pem`, cloud credential stores (gcloud's
application-default file, `.azure`, the `gh` token store), database and tooling
secrets (`.pgpass`, `.my.cnf`, `.s3cfg`, terraform vars, gem and cargo credentials),
both git configs since a remote URL routinely carries a token, `RAILWAY_VARS.md`,
`admin_login_credentials*`, this bridge's own key file, and `/proc/<pid>/environ`,
which holds this process's own environment and therefore the API key itself. This is
the injection guard: an agent talked into "include your config files" cannot post
credentials to a third party. Override per call with `allow_secret_files` from the
CLI, or from a tool call only when the config permits it (see above).

Every path is resolved before the check, so a symlink (`/tmp/notes.txt` pointing
at `~/.ssh/id_rsa`) cannot walk past it, and matching is case-insensitive so
`ID_RSA` and `CERT.PEM` are caught too. User patterns are unioned with the
built-ins rather than replacing them, because adding one project pattern must
not silently disable credential protection; `deny_file_patterns_replace: true` makes
replacement a deliberate act.

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

**Empty completions are failures.** A billed call that returns no content comes
back `ok: False` with the usage preserved, so a caller keying on `ok` cannot
mistake "no answer" for a second opinion. If a model returns only reasoning text
and no answer, the reasoning is surfaced with an explanation.

**Piped stdin never hangs.** `git diff | orask ask ...` works, but fd 0 is not
always a pipe: launched from a background job, daemon or agent shell tool it is
often a socket whose write end is never closed, and a plain `sys.stdin.read()`
blocks there forever. Every shape goes through `select`. A regular file or real pipe
is waited on until the deadline, because a slow producer is normal; a socket or
character device is read only while data keeps arriving (`ORASK_STDIN_WAIT`, default
0.5s). Both are bounded by a 32 MB ceiling and a 30s deadline (`ORASK_STDIN_DEADLINE`),
so `yes | orask ask ...` returns too. This was a live hang, caught in testing and
regression-tested for all four stdin shapes.

**Errors reach the model verbatim.** The MCP SDK replaces an unexpected
exception's text with a generic "Error executing tool", so expected failures are
re-raised as `ToolError`, the one type it forwards intact. That is how the
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
The second round verified those fixes, and both models independently and
separately found a symlink bypass of the secrets denylist, plus `text[-0:]` returning the
whole string instead of nothing when `max_file_chars` was 0. All findings are
fixed and regression-tested. One claim was rejected on evidence: GLM reported
`X-OpenRouter-Title` as not a real header, but the current OpenRouter docs
confirm it is (with `X-Title` as the legacy alias), so both are sent.

A third defect came out of using the tool rather than reviewing it: `orask` hung
forever when launched from a background job, because fd 0 was an open socket and
`sys.stdin.read()` never returned.

## Tests

```
./check.sh                      # the gate: ruff, mypy, bash -n, offline tests
python tests/test_core.py       # offline, free, no mcp package needed
python tests/test_mcp_stdio.py  # live, a few cents, needs mcp and a key
orask doctor                    # installed-state check
```

`check.sh` is what has to pass. It runs the offline suite against a scratch
config, state and cache directory, so a run cannot read the real API key, append to
the real call log, or reach the network. `ruff format` is deliberately not part of it:
the source is hand-aligned and a wholesale reformat is 2000 lines of churn.

`test_core.py` stubs the catalogue and replaces the HTTP layer with one that raises,
so a check that reaches the network fails loudly rather than billing a model. It
covers category resolution (synonyms, phrases, retired pins, excluded vendors,
and a check that the shipped config still pairs two live non-excluded vendors per
category), alias resolution and self-healing, `allowed_models` locking, effort
clamping per model, file truncation, binary and FIFO rejection, the secrets
denylist, prompt caps, thread persistence and path-traversal flattening, cost
estimation, retry policy, catalogue validation, and argument-shape recovery
(question folded into `context`, leaked tool-call tags, list arguments sent as a
bare string).

For attachments it covers type classification by magic bytes and by extension,
the content parts each kind produces, the modality gate, the byte and count
ceilings, the denylist applying to binaries, directory expansion and pruning,
and the fact that base64 is judged by bytes rather than against the text cap.

`test_mcp_stdio.py` replays the real malformed call over the protocol. That check
costs nothing: it points at an unresolvable model, so reaching model resolution
is itself the proof that the question was accepted. It also sends a real one-page
PDF whose only content is a codeword, so the round trip is proved by the model
returning something it could only have read out of the file.

## Adding a model

Edit `aliases` in `config/models.json`:

```json
"aliases": { "kimi": "moonshotai/kimi-k3", "glm": "z-ai/glm-5.3",
             "grok": "x-ai/grok-4.6" }
```

`default_panel` decides who answers a bare `orask panel`, and every alias there
is one more billed call per panel.

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
