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

When the user asks for a model that is good at something ("ask an LLM that is
good at coding", "get advice from one that's good at chatting", "something
strong at math"), pass that capability as `category` and leave `model` unset.
Categories: coding, debugging, reasoning, math, chat, agentic, research,
long_context, creative, budget, general. Each one resolves to the two current
benchmark leaders for it; list_llm_categories shows the evidence behind each.

Category picks never return an OpenAI, Anthropic or Google model: this bridge
exists to fetch a view from outside the agent asking. Ask for one of those by
full slug if you specifically want it.

Configured aliases: kimi (Moonshot Kimi K3), glm (Z.ai GLM 5.3). Any other
OpenRouter model can be reached by passing its full slug; use list_llm_models
to find one.

Arguments are flat JSON, one plain string per argument, and `question` is
always its own argument:

    {"question": "...", "context": "...", "files": ["/abs/path.py"],
     "role": "architect"}

Never wrap a value in XML tags, and never fold the question into `context`.
A long `context` is fine and expected; length is not what breaks a call.\
"""

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
        "question=" + (f"{len(question.strip())} chars" if question and question.strip() else "MISSING"),
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


def _note(text: str, note: str | None) -> str:
    """Put a recovered-shape warning above the answer, where it will be read."""
    return f"> note: {note}\n\n{text}" if note else text


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
        "'strong at math'), pass that as `category` and leave `model` unset: coding, debugging, "
        "reasoning, math, chat, agentic, research, long_context, creative, budget, general. "
        "Otherwise pass `model` ('kimi', 'glm', or any OpenRouter slug). "
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
    role: str = "advisor",
    effort: str | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
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
        role: advisor (blunt second opinion, default), reviewer (hunt for
            defects), debugger (rank root causes), architect (assess a design),
            redteam (attack the plan).
        effort: Reasoning effort - low, medium, high (default), xhigh, max, or
            'none'. Automatically snapped to what the target model supports.
        system: Replace the role prompt entirely with your own system prompt.
        max_tokens: Cap the answer. Leave unset unless you need a short reply;
            reasoning models spend this budget thinking before answering.
        temperature: Sampling temperature. Leave unset for the model default.
        thread: Name a conversation to keep, so a later call with the same name
            is a follow-up the model remembers.
        cwd: Directory that relative `files` paths resolve against.
        pdf_engine: How an attached PDF is read: 'cloudflare-ai' (default, free,
            right for a text PDF), 'mistral-ocr' (reads scans, billed per
            1,000 pages) or 'native' (only for models that take files directly).
        show_reasoning: Also return the model's reasoning trace.
        allow_expensive: Bypass the per-call cost guard for a large prompt.
        allow_secret_files: Permit a file that matches the secrets denylist
            (ssh keys, .env, credentials). Leave false unless the user has
            explicitly asked for that specific file to be sent.
    """
    question, context, files, shape_note = _question("ask_llm", question, context, files)
    result = await _run(
        core.ask,
        question=question, model=model, category=category, context=context, files=files,
        role=role, effort=effort, system=system, max_tokens=max_tokens, temperature=temperature,
        thread=thread, cwd=cwd, pdf_engine=pdf_engine, allow_expensive=allow_expensive,
        allow_secret_files=allow_secret_files, include_reasoning=show_reasoning,
    )
    return _note(_render(result, show_reasoning), shape_note)


@mcp.tool(
    name="ask_panel",
    title="Ask several LLMs at once and compare",
    description=(
        "Ask the same question of several models in parallel (default: Kimi K3 and GLM 5.3) "
        "and get every answer back side by side. Use when the user wants more than one "
        "outside view, when a decision is contested, or to see whether independent models "
        "agree. Pass `category` instead of `models` to put the two current leaders for a "
        "capability against each other (coding, debugging, reasoning, math, chat, agentic, "
        "research, long_context, creative, budget, general); each category pairs two "
        "different vendors, so the panel is two independent houses. "
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
    role: str = "advisor",
    effort: str | None = None,
    system: str | None = None,
    max_tokens: int | None = None,
    cwd: str | None = None,
    pdf_engine: str | None = None,
    allow_expensive: bool = False,
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
        role: advisor, reviewer, debugger, architect or redteam.
        effort: Reasoning effort, snapped per model to what each supports.
        system: Replace the role prompt with your own.
        max_tokens: Cap each answer.
        cwd: Directory that relative `files` paths resolve against.
        pdf_engine: How an attached PDF is read: 'cloudflare-ai' (default, free),
            'mistral-ocr' (reads scans, billed per 1,000 pages) or 'native'.
        allow_expensive: Bypass the per-call cost guard.
    """
    question, context, files, shape_note = _question("ask_panel", question, context, files)
    results = await _run(
        core.ask_panel,
        question=question, models=models, category=category, context=context, files=files,
        role=role, effort=effort, system=system, max_tokens=max_tokens, cwd=cwd,
        pdf_engine=pdf_engine, allow_expensive=allow_expensive,
    )
    total = sum(
        float((r.get("usage") or {}).get("cost_usd") or 0) for r in results if r.get("ok")
    )
    body = "\n\n".join(_render(r) for r in results)
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
    return _note(body + footer, shape_note)


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
            f"Category picks never return {' or '.join(excluded)} models: this bridge is for "
            "an opinion from outside the agent asking. Ask for one of those by full slug if "
            "you specifically want it.",
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


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
