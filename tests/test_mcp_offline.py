"""Real stdio protocol tests with an isolated, network-disabled provider fixture."""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def serve():
    from orask import core, mcp_server

    def deny_network(event, _args):
        if event == "socket.connect":
            raise AssertionError("offline MCP test attempted a network connection")

    sys.addaudithook(deny_network)
    core._catalog_cache = [
        {
            "id": slug,
            "name": slug,
            "context_length": 1000000,
            "top_provider": {"max_completion_tokens": 128000},
            "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            "supported_parameters": ["reasoning"],
            "reasoning": {"supported_efforts": ["max", "high", "medium", "low"]},
        }
        for slug in ("moonshotai/kimi-k3", "z-ai/glm-5.3")
    ]
    core._catalog_fetched_at = time.time()

    def request(method, path, payload=None, **_kwargs):
        if method != "POST" or path != "/chat/completions":
            raise AssertionError("unexpected offline request")
        effort = payload["reasoning"]["effort"]
        if effort not in {"medium", "high", "xhigh", "max"}:
            raise AssertionError("weak effort reached provider")
        partial = "force-incomplete" in json.dumps(payload["messages"])
        return {
            "choices": [
                {
                    "message": {
                        "content": "partial" if partial else "FINISHED",
                        "reasoning": "private reasoning",
                    },
                    "finish_reason": "length" if partial else "stop",
                }
            ],
            "usage": {
                "cost": 0.02,
                "completion_tokens": 100,
                "completion_tokens_details": {"reasoning_tokens": 50},
            },
        }

    core._request = request
    mcp_server.main()


async def check_protocol():
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="orask-stdio-") as tmp:
        env = {
            "ORASK_CONFIG_DIR": tmp + "/config",
            "ORASK_STATE_DIR": tmp + "/state",
            "ORASK_CACHE_DIR": tmp + "/cache",
            "OPENROUTER_API_KEY": "",
            "ORASK_PYTHON": sys.executable,
        }
        params = StdioServerParameters(
            command=sys.executable, args=[str(Path(__file__).resolve()), "--serve"], env=env
        )
        async with Client(stdio_client(params), read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            ask = next(t for t in tools.tools if t.name == "ask_llm")
            assert "effort_reason" in ask.input_schema["properties"]
            assert "max_tokens includes BOTH" in client.instructions
            for effort in ("low", "minimal", "off", "none", "medium"):
                result = await client.call_tool("ask_llm", {"question": "q", "effort": effort})
                text = "".join(getattr(c, "text", "") for c in result.content)
                assert "FINISHED" not in text and ("require" in text or "requires" in text), text
            for args in ({}, {"effort": "medium", "effort_reason": "Bounded syntax check"}):
                result = await client.call_tool("ask_llm", {"question": "q", **args})
                text = "".join(getattr(c, "text", "") for c in result.content)
                assert "FINISHED" in text, text
            result = await client.call_tool(
                "ask_panel", {"question": "force-incomplete", "models": ["kimi", "glm"]}
            )
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "0/2 answered" in text and "INCOMPLETE" in text and "partial" in text, text
            assert "private reasoning" not in text and "$0.0400" in text, text
            result = await client.call_tool("read_guide", {"topic": "bash", "section": "Quoting"})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "Quoting" in text, text
    print("all offline MCP protocol checks passed")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        os.environ.pop("OPENROUTER_API_KEY", None)
        asyncio.run(check_protocol())
