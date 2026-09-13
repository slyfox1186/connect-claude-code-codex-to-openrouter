"""Real stdio protocol tests with an isolated, network-disabled provider fixture."""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def provider_fixture():
    from orask import core

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
        for slug in (
            "moonshotai/kimi-k3",
            "z-ai/glm-5.3",
            "x-ai/grok-4.6",
            "google/gemini-3.8-flash",
        )
    ]
    core._catalog_fetched_at = time.time()

    def request(method, path, payload=None, **_kwargs):
        if method != "POST" or path != "/chat/completions":
            raise AssertionError("unexpected offline request")
        effort = payload["reasoning"]["effort"]
        if effort not in {"medium", "high", "xhigh", "max"}:
            raise AssertionError("weak effort reached provider")
        if (
            "force-google-failure" in json.dumps(payload["messages"])
            and payload["model"] == "google/gemini-3.8-flash"
        ):
            raise core.OpenRouterError("Google Flash fixture unavailable")
        if "force-cap-check" in json.dumps(payload["messages"]) and "max_tokens" in payload:
            raise AssertionError("a max_tokens tool argument reached the provider")
        partial = "force-incomplete" in json.dumps(payload["messages"])
        answer = "partial" if partial else "FINISHED"
        if "force-wide" in json.dumps(payload["messages"]):
            answer = "x" * 6000
        if "force-slow" in json.dumps(payload["messages"]):
            time.sleep(1.2 if payload["model"] == "z-ai/glm-5.3" else 0.01)
        if "force-worker-crash" in json.dumps(payload["messages"]):
            os._exit(7)
        if "force-storage-failure" in json.dumps(payload["messages"]):
            # Fail exactly one progress save, then allow final persistence to recover.
            original_write = core._write_json_atomic

            def fail_once(*args, **kwargs):
                core._write_json_atomic = original_write
                return False

            core._write_json_atomic = fail_once
        return {
            "choices": [
                {
                    "message": {
                        "content": answer,
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


def serve():
    from orask import mcp_server

    provider_fixture()
    mcp_server.CONSULTATION_WAIT_S = 0.3
    # The fixture is injected only by this test process, never through production config.
    try:
        from orask import consultations
    except ImportError:
        pass  # run the latency regression against the original adapter too
    else:
        consultations.WORKER_COMMAND = [sys.executable, str(Path(__file__).resolve()), "--worker"]
    mcp_server.main()


async def check_protocol():
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="orask-stdio-") as tmp:
        env = {
            "ORASK_CONFIG_DIR": tmp + "/config",
            "ORASK_STATE_DIR": tmp + "/state",
            "ORASK_CACHE_DIR": tmp + "/cache",
            "ORASK_DIAGNOSTIC_DIR": tmp + "/logs",
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
            assert "the bridge sends no output cap" in client.instructions
            assert not {"max_tokens", "max_context_tokens"} & set(ask.input_schema["properties"])
            panel = next(t for t in tools.tools if t.name == "ask_panel")
            categories = next(t for t in tools.tools if t.name == "list_llm_categories")
            for instructions in (
                client.instructions,
                ask.description,
                panel.description,
                categories.description,
            ):
                for model in ("Grok", "Google Flash", "GLM", "Kimi"):
                    assert model in instructions, (model, instructions)
                assert 'category="coding"' in instructions, instructions
            for question, category, expected in (
                ("Review this change", "coding", "4/4 answered"),
                (
                    "Explain a debugging workflow",
                    "Ask all of coding LLMs to explain a debugging workflow",
                    "4/4 answered",
                ),
                ("force-google-failure", "coding", "3/4 answered"),
            ):
                result = await client.call_tool(
                    "ask_panel", {"question": question, "category": category}
                )
                text = "".join(getattr(c, "text", "") for c in result.content)
                if "status: running" in text:
                    coding_id = re.search(r"consultation_id: ([0-9a-f]{32})", text)[1]
                    result = await client.call_tool(
                        "get_consultation", {"consultation_id": coding_id, "wait_seconds": 3}
                    )
                    text = "".join(getattr(c, "text", "") for c in result.content)
                assert expected in text, text
                assert "Grok, Google Flash, GLM, Kimi" in text, text
                if expected == "4/4 answered":
                    assert "$0.0800" in text, text
                else:
                    assert "Google Flash fixture unavailable" in text and "$0.0600" in text, text
            for effort in ("low", "minimal", "off", "none", "medium"):
                result = await client.call_tool("ask_llm", {"question": "q", "effort": effort})
                text = "".join(getattr(c, "text", "") for c in result.content)
                assert result.is_error and "\nFINISHED" not in text and "require" in text, text
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
            for needle in (
                "max_tokens: none sent",
                "completion_tokens: 100",
                "reasoning_tokens: 50",
            ):
                assert needle in text, text
            result = await client.call_tool("read_guide", {"topic": "bash", "section": "Quoting"})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "Quoting" in text, text
            started = time.monotonic()
            result = await client.call_tool(
                "ask_panel", {"question": "force-slow", "models": ["kimi", "glm"]}
            )
            text = "".join(getattr(c, "text", "") for c in result.content)
            elapsed = time.monotonic() - started
            assert elapsed < 0.9, f"slow provider blocked MCP response for {elapsed:.2f}s"
            assert "get_consultation" in text, text
            consultation_id = re.search(r"consultation_id: ([0-9a-f]{32})", text)[1]
            assert "1/2 answered" in text and "known cost so far $0.0200" in text, text
            assert "FINISHED" in text and "private reasoning" not in text, text
        # Closing the client must close its server's pipes promptly while the worker lives.
        async with Client(stdio_client(params), read_timeout_seconds=30) as client:
            result = await client.call_tool("get_consultation", {})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert consultation_id in text, text
            result = await client.call_tool(
                "get_consultation", {"consultation_id": consultation_id, "wait_seconds": 3}
            )
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "status: completed" in text and "2/2 answered" in text, text
            assert "$0.0400" in text and "private reasoning" not in text, text
            log_path = Path(tmp) / "state/calls.jsonl"
            before = log_path.read_bytes()
            for _ in range(3):
                result = await client.call_tool(
                    "get_consultation", {"consultation_id": consultation_id, "wait_seconds": 0}
                )
                assert "2/2 answered" in "".join(getattr(c, "text", "") for c in result.content)
            assert log_path.read_bytes() == before, "polling must not bill another call"
            for args in (
                {"consultation_id": "../../env"},
                {"consultation_id": consultation_id, "wait_seconds": 31},
                {"consultation_id": consultation_id, "wait_seconds": -1},
            ):
                result = await client.call_tool("get_consultation", args)
                assert result.is_error, result
            result = await client.call_tool("ask_llm", {"question": "force-worker-crash"})
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "status: interrupted" in text and "may have been billed" in text, text
            result = await client.call_tool(
                "ask_panel", {"question": "force-storage-failure", "models": ["kimi"]}
            )
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert "status: completed" in text and "FINISHED" in text and "1/1 answered" in text, (
                text
            )
            events = [
                json.loads(line)
                for line in (Path(tmp) / "logs/diagnostics.jsonl").read_text().splitlines()
            ]
            assert {
                "mcp.start",
                "worker.start",
                "call.prepared",
                "worker.completed",
                "consultation.receipt",
            } <= {e["event"] for e in events}
            correlated = [e for e in events if e.get("consultation_id") == consultation_id]
            assert len({e["pid"] for e in correlated}) >= 2, (
                "server and detached worker must correlate"
            )
            assert len({e["call_id"] for e in correlated if e.get("call_id")}) == 2
            assert "private reasoning" not in json.dumps(events)
            assert "force-slow" not in json.dumps(events)
            result = await client.call_tool(
                "ask_llm",
                {"question": "force-cap-check", "max_tokens": 2500, "max_context_tokens": 1000},
            )
            text = "".join(getattr(c, "text", "") for c in result.content)
            assert not result.is_error and "FINISHED" in text, text
    print("all offline MCP protocol checks passed")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        from orask import consultations

        provider_fixture()
        consultations.worker_main(sys.argv[-2], int(sys.argv[-1]))
    elif "--serve" in sys.argv:
        serve()
    else:
        os.environ.pop("OPENROUTER_API_KEY", None)
        asyncio.run(check_protocol())
