"""End-to-end MCP protocol test: spawn the real launcher over stdio.

Run:  python tests/test_mcp_stdio.py   (needs the mcp package and a live API key)
Makes five billed completions at max effort; needs a live API key.
"""

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

LAUNCHER = str(Path(__file__).resolve().parents[1] / "bin" / "openrouter-mcp")

# A one-page PDF whose only content is a codeword, so "the model saw the file"
# cannot be faked by a lucky guess.
PROOF_PDF_B64 = (
    "JVBERi0xLjQKMSAwIG9iajw8L1R5cGUvQ2F0YWxvZy9QYWdlcyAyIDAgUj4+ZW5kb2JqCjIgMCBvYmo8PC9UeXBl"
    "L1BhZ2VzL0tpZHNbMyAwIFJdL0NvdW50IDE+PmVuZG9iagozIDAgb2JqPDwvVHlwZS9QYWdlL1BhcmVudCAyIDAg"
    "Ui9NZWRpYUJveFswIDAgNjEyIDc5Ml0vQ29udGVudHMgNCAwIFIvUmVzb3VyY2VzPDwvRm9udDw8L0YxIDUgMCBS"
    "Pj4+Pj4+ZW5kb2JqCjQgMCBvYmo8PC9MZW5ndGggMTI3Pj5zdHJlYW0KQlQgL0YxIDIyIFRmIDcyIDcwMCBUZCAo"
    "Q29kZXdvcmQ6IFBFTElDQU4tOTkzMSkgVGogRVQKQlQgL0YxIDE2IFRmIDcyIDY2MCBUZCAoVGhpcyBwYWdlIHBy"
    "b3ZlcyBhIFBERiByZWFjaGVkIHRoZSBtb2RlbC4pIFRqIEVUCmVuZHN0cmVhbWVuZG9iago1IDAgb2JqPDwvVHlw"
    "ZS9Gb250L1N1YnR5cGUvVHlwZTEvQmFzZUZvbnQvSGVsdmV0aWNhPj5lbmRvYmoKeHJlZgowIDYKMDAwMDAwMDAw"
    "MCA2NTUzNSBmIAowMDAwMDAwMDA5IDAwMDAwIG4gCjAwMDAwMDAwNTIgMDAwMDAgbiAKMDAwMDAwMDEwMSAwMDAw"
    "MCBuIAowMDAwMDAwMjExIDAwMDAwIG4gCjAwMDAwMDAzODMgMDAwMDAgbiAKdHJhaWxlcjw8L1NpemUgNi9Sb290"
    "IDEgMCBSPj4Kc3RhcnR4cmVmCjQ0NAolJUVPRgo="
)

EXPECTED_TOOLS = {
    "ask_llm",
    "ask_panel",
    "list_llm_models",
    "llm_model_info",
    "openrouter_usage",
    "list_llm_categories",
    "read_guide",
}


async def main() -> int:
    failures = []

    def check(label, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    params = StdioServerParameters(command=LAUNCHER, args=[])
    async with Client(stdio_client(params), read_timeout_seconds=420) as client:
        info = client.server_info
        check("handshake", info is not None, f"{info.name} v{info.version}" if info else "")
        check("instructions advertised", bool(client.instructions))

        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        check("all tools listed", names >= EXPECTED_TOOLS, ", ".join(sorted(names)))

        ask = next((t for t in tools.tools if t.name == "ask_llm"), None)
        if ask:
            props = set((ask.input_schema or {}).get("properties") or {})
            check(
                "ask_llm schema has question/model/context/files",
                {"question", "model", "context", "files"} <= props,
            )
            check("ask_llm exposes the pdf engine choice", "pdf_engine" in props)
            check(
                "ask_llm description tells the caller not to paste files in",
                "Never paste a file" in (ask.description or ""),
            )
            # The call shape has to travel with the tool, not just live in the
            # server instructions: a model reads the description at call time.
            desc = ask.description or ""
            check(
                "ask_llm description spells out the call shape",
                '"question":' in desc and "XML" in desc,
                desc[-90:],
            )
            check(
                "ask_llm description says the question is separate from context",
                "separate from `context`" in desc,
            )

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
        check(
            "a question folded into context is recovered, not bounced",
            "cannot resolve model" in text.lower(),
            text.strip()[:140],
        )

        # A call with nothing usable must explain the shape, not hand back a
        # pydantic traceback for the model to decode.
        res = await client.call_tool("ask_llm", {"context": "background only, no question"})
        text = "".join(getattr(c, "text", "") for c in res.content)
        check(
            "an unusable call gets an actionable shape error",
            '"question":' in text and "Received:" in text and "validation error" not in text,
            text.strip()[:140],
        )

        # categories: the "ask one that's good at coding" path. Free, no model call.
        cat = next((t for t in tools.tools if t.name == "ask_llm"), None)
        if cat:
            props = set((cat.input_schema or {}).get("properties") or {})
            check("ask_llm accepts a category", "category" in props)
        res = await client.call_tool("list_llm_categories", {"verify": True})
        text = "".join(getattr(c, "text", "") for c in res.content)
        check(
            "categories cover the capabilities a user would ask for",
            all(
                c in text
                for c in (
                    "coding",
                    "chat",
                    "reasoning",
                    "math",
                    "budget",
                    "long_context",
                    "creative",
                    "agentic",
                )
            ),
        )
        check(
            "every pinned category model is still listed by OpenRouter",
            "NO LONGER LISTED" not in text,
            next((line for line in text.splitlines() if "NO LONGER" in line), "")[:90],
        )
        check(
            "category picks exclude the asking agent's own vendors",
            "openai/" not in text and "anthropic/" not in text and "google/" not in text,
            next(
                (
                    line
                    for line in text.splitlines()
                    if any(v in line for v in ("openai/", "anthropic/", "google/"))
                ),
                "",
            )[:90],
        )
        check("each category shows the evidence behind it", "Why each pick:" in text)

        # an unknown capability must not silently pick something
        res = await client.call_tool(
            "ask_llm",
            {"question": "hi", "category": "underwater basket weaving"},
        )
        text = "".join(getattr(c, "text", "") for c in res.content)
        check(
            "an unknown category is refused rather than guessed",
            "not a known category" in text.lower(),
            text.strip()[:120],
        )

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
                "model": "kimi",
                "effort": "max",
                "max_tokens": 16000,
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
                "models": ["kimi", "glm"],
                "effort": "max",
                "max_tokens": 16000,
            },
        )
        text = "".join(getattr(c, "text", "") for c in res.content)
        check(
            "ask_panel returns both models", "moonshotai/kimi-k3" in text and "z-ai/glm-5.3" in text
        )
        check("ask_panel reports a combined cost", "total cost $" in text)
        check(
            "ask_panel answered from both",
            "2/2 answered" in text,
            next((line for line in text.splitlines() if "answered" in line), "")[:90],
        )

        # a panel with one bad model must still return the good one
        res = await client.call_tool(
            "ask_panel",
            {
                "question": "Reply with exactly one word: PARTIAL",
                "models": ["kimi", "no-such-model-xyz"],
                "effort": "max",
                "max_tokens": 16000,
            },
        )
        text = "".join(getattr(c, "text", "") for c in res.content)
        check(
            "one bad model does not lose the others",
            "moonshotai/kimi-k3" in text and "1/2 answered" in text,
            next((line for line in text.splitlines() if "answered" in line), "")[:90],
        )
        check(
            "an unreachable model is labelled FAILED, not NO ANSWER",
            "no-such-model-xyz - FAILED" in text,
            next((line for line in text.splitlines() if "no-such-model" in line), "")[:90],
        )

        # An attachment has to survive the whole path: tool argument, base64,
        # the wire, and the model actually seeing it. A codeword the model can
        # only produce by decoding the file is the proof.
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "proof.pdf"
            pdf.write_bytes(base64.b64decode(PROOF_PDF_B64))
            res = await client.call_tool(
                "ask_llm",
                {
                    "question": "What codeword is in the attached PDF? Reply with just it.",
                    "model": "kimi",
                    "files": [str(pdf)],
                    "effort": "max",
                    "max_tokens": 16000,
                },
            )
            text = "".join(getattr(c, "text", "") for c in res.content)
            check("a pdf attachment reaches the model", "PELICAN-9931" in text, text.strip()[-160:])
            check("the answer says the file was attached, not pasted", "attached 1 file" in text)

        # error path must come back as a readable message, not a crash
        res = await client.call_tool("ask_llm", {"question": "hi", "model": "no-such-model-xyz"})
        text = "".join(getattr(c, "text", "") for c in res.content)
        check(
            "unknown model gives a usable error",
            "cannot resolve model" in text.lower(),
            text.strip()[:160],
        )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all MCP protocol checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
