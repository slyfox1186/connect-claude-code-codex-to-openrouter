"""End-to-end MCP protocol test: spawn the real launcher over stdio.

Run:  python tests/test_mcp_stdio.py   (needs the mcp package and a live API key)
Costs a few cents - it makes one real model call.
"""

import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

LAUNCHER = str(Path(__file__).resolve().parents[1] / "bin" / "openrouter-mcp")
EXPECTED_TOOLS = {
    "ask_llm", "ask_panel", "list_llm_models", "llm_model_info", "openrouter_usage",
}


async def main() -> int:
    failures = []

    def check(label, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    params = StdioServerParameters(command=LAUNCHER, args=[])
    async with Client(params, read_timeout_seconds=420) as client:
        info = client.server_info
        check("handshake", info is not None, f"{info.name} v{info.version}" if info else "")
        check("instructions advertised", bool(client.instructions))

        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        check("all five tools listed", EXPECTED_TOOLS <= names, ", ".join(sorted(names)))

        ask = next((t for t in tools.tools if t.name == "ask_llm"), None)
        if ask:
            props = set((ask.input_schema or {}).get("properties") or {})
            check("ask_llm schema has question/model/context/files",
                  {"question", "model", "context", "files"} <= props)
            # The call shape has to travel with the tool, not just live in the
            # server instructions: a model reads the description at call time.
            desc = ask.description or ""
            check("ask_llm description spells out the call shape",
                  '"question":' in desc and "XML" in desc, desc[-90:])
            check("ask_llm description says the question is separate from context",
                  "separate from `context`" in desc)

        # The exact malformed call that failed in the wild: `question` never
        # arrived because it had been folded into `context` in <question> tags,
        # with a stray </invoke> trailing behind it. It must now be recovered.
        # Pointing at an unresolvable model proves recovery without paying for
        # a model call: getting as far as model resolution means the question
        # was accepted.
        res = await client.call_tool(
            "ask_llm",
            {
                "model": "no-such-model-xyz",
                "context": (
                    "Background about the app.\n</context>\n"
                    "<question>Is this plan sound?</question>\n</invoke>"
                ),
            },
        )
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("a question folded into context is recovered, not bounced",
              "cannot resolve model" in text.lower(), text.strip()[:140])

        # A call with nothing usable must explain the shape, not hand back a
        # pydantic traceback for the model to decode.
        res = await client.call_tool("ask_llm", {"context": "background only, no question"})
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("an unusable call gets an actionable shape error",
              '"question":' in text and "Received:" in text and "validation error" not in text,
              text.strip()[:140])

        # cheap catalogue call, no model tokens spent
        res = await client.call_tool("list_llm_models", {"search": "kimi-k3", "limit": 3})
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("list_llm_models returns the real slug", "moonshotai/kimi-k3" in text)

        res = await client.call_tool("llm_model_info", {"model": "glm"})
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("llm_model_info resolves the glm alias", "z-ai/glm-5.3" in text)

        # a real (small) model call through the full protocol path
        res = await client.call_tool(
            "ask_llm",
            {
                "question": "Reply with exactly the word: ACKNOWLEDGED",
                "model": "kimi", "effort": "low", "max_tokens": 4000,
            },
        )
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("ask_llm round-trip", "ACKNOWLEDGED" in text.upper(), text.strip()[:160])
        check("answer carries cost metadata", "cost: $" in text)

        # the headline "ask both" feature, over the real protocol
        res = await client.call_tool(
            "ask_panel",
            {
                "question": "Reply with exactly one word: PANEL",
                "models": ["kimi", "glm"], "effort": "low", "max_tokens": 6000,
            },
        )
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("ask_panel returns both models", 
              "moonshotai/kimi-k3" in text and "z-ai/glm-5.3" in text)
        check("ask_panel reports a combined cost", "total cost $" in text)
        check("ask_panel answered from both", "2/2 answered" in text,
              next((l for l in text.splitlines() if "answered" in l), "")[:90])

        # a panel with one bad model must still return the good one
        res = await client.call_tool(
            "ask_panel",
            {
                "question": "Reply with exactly one word: PARTIAL",
                "models": ["kimi", "no-such-model-xyz"],
                "effort": "low", "max_tokens": 6000,
            },
        )
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("one bad model does not lose the others",
              "moonshotai/kimi-k3" in text and "1/2 answered" in text,
              next((l for l in text.splitlines() if "answered" in l), "")[:90])
        check("an unreachable model is labelled FAILED, not NO ANSWER",
              "no-such-model-xyz - FAILED" in text,
              next((l for l in text.splitlines() if "no-such-model" in l), "")[:90])

        # error path must come back as a readable message, not a crash
        res = await client.call_tool("ask_llm", {"question": "hi", "model": "no-such-model-xyz"})
        text = "".join(getattr(c, "text", "") for c in res.content)
        check("unknown model gives a usable error", "cannot resolve model" in text.lower(),
              text.strip()[:160])

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all MCP protocol checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
