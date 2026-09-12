"""MCP server exposing the OpenRouter bridge to Claude Code and Codex.

Tool descriptions name the models out loud (Kimi, GLM) because that is how
the request usually arrives: "go ask Kimi what it thinks about this".

Requires the `mcp` SDK (>=2.0, where FastMCP became MCPServer). The CLI in
cli.py has no third-party dependency and is the fallback if this ever breaks.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, core

INSTRUCTIONS = """\
Second-opinion bridge to other frontier LLMs through OpenRouter.

Use ask_llm when the user asks you to consult another model ("ask Kimi",
"see what GLM thinks", "get a second opinion"), or when you are stuck and an
independent take would help. Use ask_panel to ask several models at once and
compare. The other model cannot see the repository, so pass the relevant code
with `files` and the situation with `context` - a question with no context
gets a generic answer.

Never paste a file's contents into `question` or `context`. Put its path in
`files` and the bridge sends the file itself: source and prose go in as text,
while a PDF, screenshot, diagram or sound file is attached to the message as
a real attachment. A directory path in `files` sends the files inside it.

When the user names a capability instead of a model, pass it as `category` and
leave `model` and `models` unset. Which tool depends on how they said it:

  singular - "ask an LLM that is good at coding", "something strong at math"
             -> ask_llm with category
  plural   - "use the coding LLMs", "ask the reasoning models", "what do the
             debugging ones say" -> ask_panel with category

Pass the user's own words as `category`. It is matched against the category
names, their synonyms, and any phrase containing one, so "use the coding LLMs
to review this" resolves on its own. A phrase matching nothing is refused
rather than guessed at, so you do not need to map it yourself first.

Each category resolves to the current benchmark leaders for it, two different
vendors on purpose; list_llm_categories shows the evidence behind each pick.

Category picks never return an OpenAI, Anthropic or Google model: this bridge
exists to fetch a view from outside the agent asking. Ask for one of those by
full slug if you specifically want it.

Any OpenRouter model can be reached by passing its full slug; use
list_llm_models to find one. Short aliases are configured for these:

Arguments are flat JSON, one plain string per argument, and `question` is
always its own argument:

    {"question": "...", "context": "...", "files": ["/abs/path.py"],
     "role": "architect"}

Never wrap a value in XML tags, and never fold the question into `context`.
A long `context` is fine and expected; length is not what breaks a call.

Prompt and answer share the model's context window, and no argument makes it
bigger. `max_context_tokens` budgets below it (anything above is clamped down),
`max_tokens` is lowered automatically to whatever room the prompt leaves, and a
prompt with no room left for a reply is refused before it is billed. Pass
`context_compression: true` on that refusal only when a partial view of the
material is acceptable: OpenRouter then drops text from the middle of the prompt
until it fits, so the model answers without having read all of it.

Two safety overrides exist but are off for tool calls: `allow_secret_files`
(send a file matching the credential denylist) and `allow_expensive` (bypass
the per-call cost guard). Passing either is refused with a note unless the
user has turned it on in their config. Relay that note rather than retrying:
the user has to make that decision, not you.\
"""


def _alias_index() -> str:
    """The configured aliases, generated rather than written out.

    This list used to be prose inside INSTRUCTIONS, so adding an alias to
    config/models.json left the calling agent being told the old pair. Reading
    it from config is the only way the two cannot disagree.
    """
    try:
        aliases = core.load_config().get("aliases") or {}
    except Exception:
        # A bad config must never stop the server starting.
        return ""
    if not aliases:
        return ""
    return "\n" + "\n".join(f"  {k:<10} {v}" for k, v in sorted(aliases.items()))


def _clean(value: str, limit: int) -> str:
    """Collapse to one bounded line.

    Front matter reaches the agent's system prompt verbatim, and a directory
    named in `guide_dirs` is content this project did not write. One line,
    bounded length, so a guide file cannot author the instructions.
    """
    return " ".join(str(value).split())[:limit]


def _guide_index() -> str:
    """One line per local guide, appended to the instructions the agent reads.

    Generated rather than written out, so adding a file to guides/ is the whole
    change. It is read once at startup: MCP instructions are sent during
    initialize and cannot change afterwards, so a guide added mid-session shows
    up in read_guide but not here until the server restarts. That is said out
    loud below rather than left for someone to discover.
    """
    try:
        rows = core.list_guides()[:core.MAX_INDEXED_GUIDES]
    except Exception:
        # A guides directory problem must never stop the server starting.
        return ""
    if not rows:
        return ""
    lines = [
        "",
        "",
        "This machine also carries local best-practice guides. Read the matching",
        "one with read_guide BEFORE writing or reviewing code in that area. They",
        "are local files: free, instant, no model call. This list is fixed when",
        "the server starts; read_guide with no arguments is always current.",
        "",
    ]
    lines += [
        f"  {_clean(r['topic'], 24):<24} {_clean(r['triggers'], 110)}" for r in rows
    ]
    return "\n".join(lines)


def _category_index() -> str:
    """The configured categories, generated rather than written out.

    The list used to be prose, so a category added to config left the agent
    being told an older set and never asking for the new one.
    """
    try:
        cats = core.load_config().get("categories") or {}
    except Exception:
        # A bad config must never stop the server starting.
        return ""
    return f"\n\nCategories: {', '.join(sorted(cats))}." if cats else ""


INSTRUCTIONS += _category_index()
INSTRUCTIONS += _alias_index()
INSTRUCTIONS += _guide_index()

mcp = MCPServer(
    name="openrouter",
    title="OpenRouter second opinion",
    instructions=INSTRUCTIONS,
    version=__version__,
)


def _render(result: dict[str, Any], include_reasoning: bool = False) -> str:
    """One model's answer as text, with the metadata the caller should relay."""
    usage = result.get("usage") or {}
    if not result.get("ok"):
        # Distinguish "could not reach this model" from "the model was billed
        # but produced nothing": the caller should act differently on each.
        billed_call = bool(usage)
        lines = [
            f"### {result.get('model') or result.get('requested')} - "
            + ("NO ANSWER" if billed_call else "FAILED"),
            "",
            str(result.get("error")),
        ]
        # A billed non-answer still has usage worth reporting.
        billed = usage.get("cost_usd") or 0.0
        if billed:
            lines += ["", f"`billed ${billed:.4f} anyway`"]
        for note in result.get("notes") or []:
            lines.append(f"> note: {note}")
        return "\n".join(lines)

    bits = [f"model: {result['model']}"]
    if result.get("effort"):
        bits.append(f"effort: {result['effort']}")
    if result.get("latency_s") is not None:
        bits.append(f"{result['latency_s']}s")
    if usage.get("prompt_tokens") is not None:
        bits.append(
            f"tokens in/out: {usage.get('prompt_tokens')}/{usage.get('completion_tokens')}"
        )
    if usage.get("reasoning_tokens"):
        bits.append(f"reasoning: {usage['reasoning_tokens']}")
    window = result.get("context_window") or 0
    if window:
        # How much of the window this call used, so the next one can be budgeted
        # rather than guessed at.
        used = usage.get("prompt_tokens")
        bits.append(f"context: {used}/{window}" if used is not None else f"context: {window}")
    # A missing or null figure must not crash the formatter and lose the answer.
    bits.append(f"cost: ${usage.get('cost_usd') or 0.0:.4f}")

    lines = [f"### {result['model']}", "", "`" + " | ".join(bits) + "`", ""]
    for note in result.get("notes") or []:
        lines.append(f"> note: {note}")
    if result.get("notes"):
        lines.append("")
    if include_reasoning and result.get("reasoning"):
        lines += ["<reasoning>", result["reasoning"], "</reasoning>", ""]
    lines.append(result.get("answer") or "(the model returned an empty answer)")
    return "\n".join(lines)


CALL_SHAPE = (
    "Arguments are flat JSON, one plain string per argument:\n"
    '  {"question": "what you want answered", "context": "background the other '
    'model needs", "files": ["/abs/path/one.py", "/abs/path/two.tsx"], '
    '"role": "architect"}\n'
    "`question` is required and separate from `context`. Never wrap a value in XML "
    "tags such as <question> or <context>, and never put the question inside `context`."
)


def _shape(tool: str, question: str | None, context: str | None, files: Any) -> str:
    """The error a model can act on: what arrived, and the shape that works."""
    received = [
        "question="
        + (f"{len(question.strip())} chars" if question and question.strip() else "MISSING"),
        "context=" + (f"{len(context)} chars" if context else "none"),
        f"files={len(core.as_list(files))}",
    ]
    return (
        f"{tool}: no question was given, and none could be recovered from `context`.\n"
        f"{CALL_SHAPE}\nReceived: " + ", ".join(received) + "."
    )


def _question(tool: str, question: str | None, context: str | None, files: Any):
    """Normalise the arguments, recovering a misplaced question where possible.

    A question packed into `context` is the failure seen in the wild, usually
    with the agent's own tool-call tags still around it. That is recoverable,
    so it is recovered and reported rather than bounced. Anything genuinely
    unusable gets an error naming the shape that works, because the SDK's own
    schema error is a pydantic traceback the model has to decode first.
    """
    question, context, note = core.split_embedded_question(question, context)
    if not question or not question.strip():
        raise ToolError(_shape(tool, question, context, files))
    return question, context, core.as_list(files) or None, note


def _note(text: str, *notes: str | None) -> str:
    """Put warnings above the answer, where they will be read."""
    lines = [f"> note: {note}" for note in notes if note]
    return "\n".join(lines) + "\n\n" + text if lines else text


# The policy itself lives in core.override_allowed, so the offline suite can test it without
# needing the mcp SDK. This layer only decides which keys the tool arguments map onto.
_gate = core.override_allowed


async def _run(func, /, **kwargs):
    """Run the blocking stdlib HTTP call off the event loop.

    Expected failures are re-raised as ToolError, which is the one exception
    type the SDK forwards verbatim to the calling model. Anything else would
    reach the agent as a bare "Error executing tool", hiding the message that
    tells it how to fix the call.
    """
    try:
        return await asyncio.to_thread(lambda: func(**kwargs))
    except core.OpenRouterError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(
    name="ask_llm",
    title="Ask another LLM for a second opinion",
    description=(
        "Ask a different frontier model for its independent take on the problem at hand. "
        "Use for 'ask Kimi', 'what does GLM think', 'get a second opinion', or when you are stuck. "
        "When the user asks for a model good at something ('one that's good at coding', "
        "'strong at math'), pass their words as `category` and leave `model` unset: coding, "
        "debugging, reasoning, math, chat, agentic, research, long_context, creative, budget, "
        "general. If they said it in the plural ('the coding LLMs', 'the reasoning models') "
        "use ask_panel with the same `category` instead, so they get every leader rather than "
        "the first. Otherwise pass `model` ('kimi', 'glm', 'grok', 'gemini', or any slug). "
        "The other model has no access to this machine or repo: pass the relevant source "
        "with `files` and the situation with `context`, or the answer will be generic. "
        "Never paste a file's contents into the question: put its path in `files` and the "
        "bridge sends the file itself. Source goes in as text; a PDF, image or audio file "
        "is attached to the message directly; a directory sends the files inside it.\n\n"
        + CALL_SHAPE
    ),
)
async def ask_llm(
    question: str | None = None,
    model: str | None = None,
    category: str | None = None,
    context: str | None = None,
    files: list[str] | str | None = None,
    role: str | None = None,
    effort: str | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    max_context_tokens: int | None = None,
    context_compression: bool | None = None,
    temperature: float | None = None,
    thread: str | None = None,
    cwd: str | None = None,
    pdf_engine: str | None = None,
    show_reasoning: bool = False,
    allow_expensive: bool = False,
    allow_secret_files: bool = False,
) -> str:
    """Ask one model.

    Args:
        question: What to ask, as a plain string, and required. It is always its
            own argument: do not fold it into `context` and do not wrap it in
            <question> tags. Be specific; state what you want back.
        model: 'kimi', 'glm', or a full OpenRouter slug like 'x-ai/grok-4.6'.
            Leave unset when using `category`. Defaults to 'kimi' if neither
            is given.
        category: A capability to pick the model by, used when the user asks
            for a model good at something rather than naming one: coding,
            debugging, reasoning, math, chat, agentic, research, long_context,
            creative, budget, general. Resolves to the current benchmark
            leader for that category. An explicit `model` wins over this.
        context: Background the other model needs - the problem, what you tried,
            error output, constraints. One plain string, as long as you like;
            it sees nothing else. The question does not go in here.
        files: A JSON array of paths to send. Never paste a file into `question`
            or `context` instead: put the path here and the bridge sends the
            file itself. Source and prose go in as text (large ones truncated
            in the middle); a PDF, image (png/jpg/webp/gif) or audio file is
            attached to the message as an attachment, so it never has to be
            described or transcribed. A directory path sends the files inside
            it, skipping build output and .git. Relative paths resolve against
            `cwd`. Images and audio need a model that accepts them; PDFs work
            on every model.
        role: advisor (blunt second opinion), reviewer (hunt for defects),
            debugger (rank root causes), architect (assess a design), redteam
            (attack the plan). Left unset it follows default_role in the config,
            which is advisor unless it has been changed.
        effort: Reasoning effort - low, medium, high (default), xhigh, max, or
            'none'. Automatically snapped to what the target model supports.
        system: Replace the role prompt entirely with your own system prompt.
        max_tokens: Cap the answer. Leave unset unless you need a short reply;
            reasoning models spend this budget thinking before answering.
        max_context_tokens: Budget prompt and answer together into this many
            tokens. A model's context window is fixed and cannot be raised from
            here, so a number above it is clamped back down to what the model
            takes; below it, max_tokens is lowered to leave room for the reply.
            Leave unset to use the whole window the model publishes.
        context_compression: What to do when the prompt does not fit the window.
            true lets OpenRouter drop text from the middle until it does, false
            refuses instead. Left unset the prompt is refused with the numbers,
            except on the small endpoints OpenRouter already compresses for you.
        temperature: Sampling temperature. Leave unset for the model default.
        thread: Name a conversation to keep, so a later call with the same name
            is a follow-up the model remembers.
        cwd: Directory that relative `files` paths resolve against.
        pdf_engine: How an attached PDF is read: 'cloudflare-ai' (default, free,
            right for a text PDF), 'mistral-ocr' (reads scans, billed per
            1,000 pages) or 'native' (only for models that take files directly).
        show_reasoning: Also return the model's reasoning trace.
        allow_expensive: Ask to bypass the per-call cost guard for a large
            prompt. Refused unless mcp_allow_expensive is true in the config,
            and the answer says so.
        allow_secret_files: Ask to send a file that matches the secrets denylist
            (ssh keys, .env, credentials). Refused unless mcp_allow_secret_files
            is true in the config, and the answer says so. Leave false unless
            the user has explicitly asked for that specific file to be sent.
    """
    question, context, files, shape_note = _question("ask_llm", question, context, files)
    allow_expensive, expensive_note = _gate("mcp_allow_expensive", allow_expensive)
    allow_secret_files, secret_note = _gate("mcp_allow_secret_files", allow_secret_files)
    result = await _run(
        core.ask,
        question=question, model=model, category=category, context=context, files=files,
        role=role, effort=effort, system=system, max_tokens=max_tokens,
        max_context_tokens=max_context_tokens, context_compression=context_compression,
        temperature=temperature,
        thread=thread, cwd=cwd, pdf_engine=pdf_engine, allow_expensive=allow_expensive,
        allow_secret_files=allow_secret_files, include_reasoning=show_reasoning,
    )
    return _note(_render(result, show_reasoning), shape_note, expensive_note, secret_note)


@mcp.tool(
    name="ask_panel",
    title="Ask several LLMs at once and compare",
    description=(
        "Ask the same question of several models in parallel and get every answer back "
        "side by side. Use when the user wants more than one outside view, when a decision "
        "is contested, or to see whether independent models agree. This is the tool for a "
        "plural request: 'use the coding LLMs', 'ask the reasoning models', 'what do the "
        "debugging ones think' - pass the user's own words as `category` instead of "
        "`models`, and the current leaders for that capability answer side by side. Each "
        "category pairs different vendors, so the panel is independent houses rather than "
        "one lab asked twice. "
        "Costs one call per model; one model failing does not lose the others.\n\n"
        + CALL_SHAPE + " `models` is a JSON array of aliases or slugs."
    ),
)
async def ask_panel(
    question: str | None = None,
    models: list[str] | str | None = None,
    category: str | None = None,
    context: str | None = None,
    files: list[str] | str | None = None,
    role: str | None = None,
    effort: str | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    max_context_tokens: int | None = None,
    context_compression: bool | None = None,
    temperature: float | None = None,
    cwd: str | None = None,
    pdf_engine: str | None = None,
    show_reasoning: bool = False,
    allow_expensive: bool = False,
    allow_secret_files: bool = False,
) -> str:
    """Ask several models the same question at once.

    Args:
        question: What to ask all of them, as a plain string, and required. It is
            always its own argument: do not fold it into `context` and do not
            wrap it in <question> tags.
        models: A JSON array of aliases or slugs, e.g. ["kimi", "glm"]. Defaults
            to both configured models. Leave unset when using `category`.
        category: Put the two current leaders for a capability against each
            other instead of naming models: coding, debugging, reasoning, math,
            chat, agentic, research, long_context, creative, budget, general.
        context: Background every model should see. One plain string, as long as
            you like. The question does not go in here.
        files: A JSON array of paths, sent to every model. Source goes in as
            text, a PDF or image is attached directly, and a directory sends
            the files inside it. Never paste a file into `question` instead.
        role: advisor, reviewer, debugger, architect or redteam. Left unset it
            follows default_role in the config.
        effort: Reasoning effort, snapped per model to what each supports.
        system: Replace the role prompt with your own.
        max_tokens: Cap each answer.
        max_context_tokens: Budget prompt and answer together into this many
            tokens per model, clamped down to each model's own window.
        context_compression: true lets OpenRouter drop text from the middle of a
            prompt that does not fit; false refuses it. Unset refuses with the
            numbers, except where OpenRouter compresses by default.
        temperature: Sampling temperature. Leave unset for the model default.
        cwd: Directory that relative `files` paths resolve against.
        pdf_engine: How an attached PDF is read: 'cloudflare-ai' (default, free),
            'mistral-ocr' (reads scans, billed per 1,000 pages) or 'native'.
        show_reasoning: Also return each model's reasoning trace.
        allow_expensive: Ask to bypass the per-call cost guard. Refused unless
            mcp_allow_expensive is true in the config.
        allow_secret_files: Ask to send a file that matches the secrets denylist.
            Refused unless mcp_allow_secret_files is true in the config. Leave
            false unless the user has explicitly asked for that file.
    """
    question, context, files, shape_note = _question("ask_panel", question, context, files)
    allow_expensive, expensive_note = _gate("mcp_allow_expensive", allow_expensive)
    allow_secret_files, secret_note = _gate("mcp_allow_secret_files", allow_secret_files)
    results = await _run(
        core.ask_panel,
        question=question, models=models, category=category, context=context, files=files,
        role=role, effort=effort, system=system, max_tokens=max_tokens,
        max_context_tokens=max_context_tokens, context_compression=context_compression,
        temperature=temperature, cwd=cwd, pdf_engine=pdf_engine,
        allow_expensive=allow_expensive, allow_secret_files=allow_secret_files,
        include_reasoning=show_reasoning,
    )
    # Every result, not just the ones that answered: an empty completion is ok: False and is
    # still billed, so filtering on ok reports a total lower than the invoice.
    total = sum(float((r.get("usage") or {}).get("cost_usd") or 0) for r in results)
    # When a capability chose the panel rather than the caller, say which category
    # that resolved to and why. Otherwise "use the coding LLMs" returns answers
    # from two models with nothing saying they were the ones meant.
    header = ""
    if category and not models:
        match = await _run(core.resolve_category, term=category)
        if match:
            name, spec = match
            header = f"`category: {name}`"
            if spec.get("why"):
                header += f" - {spec['why']}"
            header += "\n\n"
    body = header + "\n\n".join(_render(r, show_reasoning) for r in results)
    agreed = [r["model"] for r in results if r.get("ok")]
    footer = (
        f"\n\n---\n`panel: {len(agreed)}/{len(results)} answered "
        f"({', '.join(agreed) or 'none'}) | total cost ${total:.4f}`"
    )
    if len(agreed) > 1:
        footer += (
            "\n\nCompare the answers above before acting: where they agree you have "
            "corroboration, where they disagree say so rather than silently picking one."
        )
    return _note(body + footer, shape_note, expensive_note, secret_note)


@mcp.tool(
    name="list_llm_models",
    title="Search available OpenRouter models",
    description=(
        "Search the live OpenRouter catalogue (hundreds of models) for exact slugs, context "
        "sizes, price per million tokens, published intelligence index, and which reasoning "
        "efforts each accepts. Use to find a model that is not one of the configured "
        "aliases, then pass its slug to ask_llm."
    ),
)
async def list_llm_models(
    search: str | None = None,
    vendor: str | None = None,
    limit: int = 20,
    sort: str = "intelligence",
) -> str:
    """Search the OpenRouter model catalogue.

    Args:
        search: Substring to match against slug or name, e.g. 'kimi', 'grok'.
        vendor: Restrict to one vendor prefix, e.g. 'moonshotai', 'z-ai'.
        limit: Maximum rows to return.
        sort: intelligence (default), context, price, or name.
    """
    rows = await _run(
        core.list_models, search=search, vendor=vendor, limit=limit, sort=sort,
    )
    if not rows:
        return f"No models matched search={search!r} vendor={vendor!r}."
    lines = [
        "| slug | ctx | $/M in | $/M out | IQ | reasoning efforts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        iq = row["intelligence_index"]
        lines.append(
            f"| `{row['slug']}` | {row['context'] or 0:,} | {row['usd_per_m_input']:.2f} | "
            f"{row['usd_per_m_output']:.2f} | {f'{iq:.1f}' if iq is not None else '-'} | "
            f"{'/'.join(row['reasoning_efforts']) or '-'} |"
        )
    lines.append("")
    lines.append("Pass any slug above as `model` to ask_llm.")
    return "\n".join(lines)


@mcp.tool(
    name="list_llm_categories",
    title="Capabilities you can ask for by name",
    description=(
        "Show every capability category ask_llm and ask_panel accept, the two models each one "
        "resolves to, and the benchmark evidence behind the pick. Use when the user asks which "
        "model is best at something, or to check what a category would actually call before "
        "spending money on it."
    ),
)
async def list_llm_categories(verify: bool = False) -> str:
    """List the capability categories.

    Args:
        verify: Also check each pinned model against the live OpenRouter
            catalogue and report its current intelligence index, to see
            whether a category has gone stale.
    """
    rows = await _run(core.list_categories)
    lines = [
        "| category | models | also matches | measured |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        aka = ", ".join(row["aka"][:4])
        models = "<br>".join(f"`{m}`" for m in row["models"])
        lines.append(f"| **{row['category']}** | {models} | {aka} | {row['measured']} |")
    lines.append("")
    lines.append("Why each pick:")
    for row in rows:
        lines.append(f"- **{row['category']}**: {row['why']}")

    if verify:
        checks = await _run(core.verify_categories)
        lines += ["", "Live check against the OpenRouter catalogue:", ""]
        lines.append("| category | slug | available | intelligence | context |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in checks:
            iq = row["intelligence_index"]
            lines.append(
                f"| {row['category']} | `{row['slug']}` | "
                f"{'yes' if row['available'] else 'NO LONGER LISTED'} | "
                f"{f'{iq:.1f}' if iq is not None else '-'} | "
                f"{row['context'] or 0:,} |"
            )

    excluded = await _run(core.excluded_vendors)
    if excluded:
        lines += [
            "",
            (f"Category picks never return {' or '.join(excluded)} models: this bridge is for "
            "an opinion from outside the agent asking. Ask for one of those by full slug if "
            "you specifically want it."),
        ]
    return "\n".join(lines)


@mcp.tool(
    name="llm_model_info",
    title="Details for one OpenRouter model",
    description=(
        "Full detail for one model: context window, max output, price, supported reasoning "
        "efforts, input modalities and benchmark indices. Use before sending a very large "
        "prompt, or to check whether a model accepts a given effort level."
    ),
)
async def llm_model_info(model: str) -> str:
    """Look up one model.

    Args:
        model: An alias ('kimi', 'glm') or a full OpenRouter slug.
    """
    data = await _run(core.model_info, spec=model)
    return json.dumps(data, indent=2)


@mcp.tool(
    name="openrouter_usage",
    title="OpenRouter spend and bridge call log",
    description=(
        "Report OpenRouter account usage for this key plus what this bridge has spent, "
        "lifetime and in the last 24 hours. Use when asked what these consultations cost."
    ),
)
async def openrouter_usage() -> str:
    """Account usage and bridge spend."""
    data = await _run(core.account_usage)
    return json.dumps(data, indent=2)


FULL_GUIDE_MAX_LINES = 400


def _guide_topics() -> str:
    """The topic list, carried on the tool description as well as the instructions.

    The description travels with the tool schema, which every harness delivers;
    whether a harness surfaces server instructions is its own choice.
    """
    try:
        topics = ", ".join(r["topic"] for r in core.list_guides()[:core.MAX_INDEXED_GUIDES])
    except Exception:
        return ""
    return f" Guides on this machine: {topics}." if topics else ""


@mcp.tool(
    name="read_guide",
    title="Local best-practice guide for a topic",
    description=(
        "Read the project's local best-practice guides. These are plain files on this "
        "machine: free, instant, and no model is called. Call with no arguments for the "
        "index, with `topic` for that guide (short guides come back whole, long ones as a "
        "heading tree), with `topic` and `section` for one section, `full=true` to force "
        "the whole file, or `search` to grep every guide at once. Read the guide for a "
        "topic BEFORE writing or reviewing code in it, whenever one exists." + _guide_topics()
    ),
)
async def read_guide(
    topic: str | None = None,
    section: str | None = None,
    search: str | None = None,
    full: bool = False,
) -> str:
    """Read a local best-practice guide.

    Args:
        topic: Guide name, e.g. 'python', 'bash', 'css'. Omit for the index.
        section: A heading within that guide. Omit for the whole guide if it is
            short, or its heading tree if it is long.
        search: Substring to look for across every guide. Overrides `topic`.
        full: Return the whole file even when it is long.
    """
    if search:
        found = await _run(core.search_guides, query=search)
        hits, total = found["hits"], found["total"]
        if not hits:
            return f"Nothing in the guides matches {search!r}."
        shown = (
            f"{len(hits)} of {total} matches for {search!r} "
            "(narrow the query, or search one topic):"
            if found["truncated"]
            else f"{total} match(es) for {search!r}:"
        )
        lines = [shown, ""]
        lines += [
            f"- **{h['topic']}** / {h['section'] or '(top)'} (line {h['line']}): {h['snippet']}"
            for h in hits
        ]
        lines += ["", "Read one with read_guide(topic, section)."]
        return "\n".join(lines)

    if not topic:
        rows = await _run(core.list_guides)
        if not rows:
            return "No guides are installed."
        lines = ["| guide | read it when | verified |", "| --- | --- | --- |"]
        lines += [
            f"| `{r['topic']}` | {r['triggers']} | "
            f"{r['verified'] or 'undated'}{' (stale)' if r['stale'] else ''} |"
            for r in rows
        ]
        lines += ["", "read_guide(topic) for a guide, read_guide(topic, section) for one part."]
        return "\n".join(lines)

    if section:
        data = await _run(core.read_guide, topic=topic, section=section)
        stamp = data["verified"] or "undated"
        return f"[{data['topic']} / {data['section']}, verified {stamp}]\n\n{data['text']}"

    outline = await _run(core.guide_outline, topic=topic)
    stamp = outline["verified"] or "undated"
    # A short guide is cheaper read whole than navigated in two round trips, and
    # the agent should not have to know which width applies before it has looked.
    if full or outline["lines"] <= FULL_GUIDE_MAX_LINES:
        data = await _run(core.read_guide, topic=topic)
        return f"[{data['topic']}, verified {stamp}]\n\n{data['text']}"

    lines = [f"# {outline['topic']} ({outline['lines']} lines, verified {stamp})", ""]
    if outline["triggers"]:
        lines += [f"Read when: {outline['triggers']}", ""]
    lines.append("Sections:")
    lines += [
        f"{'  ' * (s['level'] - 2)}- {s['title']}  ({s['lines']} lines)"
        for s in outline["sections"]
    ]
    lines += ["", "read_guide(topic, section) for one of these, or full=true for all of it."]
    return "\n".join(lines)


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
