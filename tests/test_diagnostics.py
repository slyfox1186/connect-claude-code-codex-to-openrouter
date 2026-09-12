"""Offline diagnostics and paid-call prevention regressions."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCRATCH = tempfile.TemporaryDirectory(prefix="orask-diagnostics-")
os.environ.update(
    ORASK_CONFIG_DIR=SCRATCH.name + "/config",
    ORASK_STATE_DIR=SCRATCH.name + "/state",
    ORASK_CACHE_DIR=SCRATCH.name + "/cache",
    ORASK_DIAGNOSTIC_DIR=SCRATCH.name + "/logs",
    OPENROUTER_API_KEY="",
)
from orask import core

CATALOG = [
    {
        "id": "test/model",
        "context_length": 100000,
        "top_provider": {"max_completion_tokens": 64000},
        "reasoning": {"supported_efforts": ["medium", "high", "max"]},
        "pricing": {"prompt": "0.000001", "completion": "0.000001"},
    }
]


def deny_network(event, _args):
    if event == "socket.connect":
        raise AssertionError("offline diagnostics tests cannot connect")


sys.addaudithook(deny_network)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=SCRATCH.name)
        self.addCleanup(self.directory.cleanup)
        self.logs = Path(self.directory.name) / "logs"
        self.env = patch.dict(os.environ, ORASK_DIAGNOSTIC_DIR=str(self.logs))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.catalog = patch.object(core, "get_catalog", return_value=CATALOG)
        self.catalog.start()
        self.addCleanup(self.catalog.stop)
        core._config_cache = None
        self.response = {
            "choices": [{"message": {"content": "PRIVATE_ANSWER"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1500,
                "completion_tokens": 800,
                "completion_tokens_details": {"reasoning_tokens": 600},
                "cost": 0.004,
            },
        }

    def events(self):
        path = self.logs / "diagnostics.jsonl"
        self.assertTrue(path.exists(), "the normal call path must write diagnostic events")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def ask(self, **kwargs):
        with patch.object(core, "_request", return_value=self.response):
            return core.ask("PRIVATE_QUESTION", model="test/model", **kwargs)

    def test_success_has_correlated_preflight_and_completion_without_content(self):
        result = self.ask(context="PRIVATE_CONTEXT")
        events = self.events()
        self.assertTrue(
            {"call.start", "call.prepared", "call.response", "call.end"}
            <= {e["event"] for e in events}
        )
        prepared = next(e for e in events if e["event"] == "call.prepared")
        self.assertEqual(prepared["max_tokens"], 32000)
        self.assertGreater(prepared["estimated_prompt_tokens"], 0)
        self.assertGreater(prepared["estimated_cost_usd"], 0)
        call_id = result["diagnostics"]["call_id"]
        self.assertTrue(call_id)
        self.assertTrue(all(e["call_id"] == call_id for e in events))
        self.assertNotIn("PRIVATE_", json.dumps(events))
        self.assertEqual((self.logs.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.logs / "diagnostics.jsonl").stat().st_mode & 0o777), 0o600)

    def test_length_failure_logs_actual_budget_and_all_usage(self):
        self.response["choices"][0] = {
            "finish_reason": "length",
            "message": {"content": "", "reasoning": "PRIVATE_TRACE"},
        }
        result = self.ask(max_tokens=16000)
        self.assertFalse(result["ok"])
        event = next(e for e in self.events() if e["event"] == "call.response")
        for key, expected in [
            ("finish_reason", "length"),
            ("max_tokens", 16000),
            ("reasoning_tokens", 600),
            ("completion_tokens", 800),
            ("cost_usd", 0.004),
            ("answer_chars", 0),
        ]:
            self.assertEqual(event[key], expected)

    def test_mcp_preserves_output_room_instead_of_paying_with_a_reduced_cap(self):
        with patch.object(core, "_request", return_value=self.response) as request:
            with self.assertRaisesRegex(core.OpenRouterError, "output budget"):
                core.ask(
                    "q",
                    model="test/model",
                    max_tokens=32000,
                    max_context_tokens=1000,
                    _mcp_call=True,
                )
            request.assert_not_called()
        self.assertIn("call.error", {e["event"] for e in self.events()})

    def test_incomplete_renderers_show_usage_and_budget(self):
        from orask import cli, mcp_server

        self.response["choices"][0]["finish_reason"] = "length"
        result = self.ask(max_tokens=16000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_result(result, False)
        for rendered in [output.getvalue(), mcp_server._render(result)]:
            for needle in ["16000", "1500", "800", "600", "length", "diagnostics.jsonl"]:
                self.assertIn(needle, rendered)

    def test_transport_success_and_retry_are_logged_without_headers_or_bodies(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 429, "PRIVATE_ERROR", {}, io.BytesIO(b"PRIVATE_RESPONSE")
        )
        response = io.BytesIO(json.dumps(self.response).encode())
        with (
            patch.object(core, "get_api_key", return_value="PRIVATE_KEY"),
            patch.object(core.urllib.request, "urlopen", side_effect=[error, response]),
            patch.object(core.time, "sleep"),
        ):
            result = core.ask("PRIVATE_QUESTION", model="test/model")
        self.assertTrue(result["ok"])
        events = self.events()
        self.assertEqual(len([e for e in events if e["event"] == "http.attempt"]), 2)
        self.assertTrue(
            {"http.retry", "http.received", "http.decoded"} <= {e["event"] for e in events}
        )
        self.assertNotIn("PRIVATE_", json.dumps(events))

    def test_timeout_is_not_retried_and_billing_is_unknown(self):
        with (
            patch.object(core, "get_api_key", return_value="PRIVATE_KEY"),
            patch.object(
                core.urllib.request, "urlopen", side_effect=TimeoutError("PRIVATE_ERROR")
            ) as request,
            self.assertRaises(core.OpenRouterError) as failure,
        ):
            core.ask("PRIVATE_QUESTION", model="test/model")
        self.assertEqual(request.call_count, 1)
        self.assertTrue(failure.exception.orask_diagnostics["call_id"])
        events = self.events()
        self.assertIn("http.timeout", {e["event"] for e in events})
        event = next(e for e in events if e["event"] == "call.provider_error")
        self.assertIsNone(event["cost_usd"])
        self.assertFalse(event["cost_known"])
        self.assertNotIn("PRIVATE_", json.dumps(events))

    def test_malformed_error_usage_is_unknown_and_total_cost_is_reported(self):
        self.response = {"error": {"message": "failed"}, "usage": {"cost": "malformed"}}
        with self.assertRaises(core.OpenRouterError):
            self.ask()
        event = next(e for e in self.events() if e["event"] == "call.provider_error")
        self.assertFalse(event["cost_known"])
        self.assertIsNone(event["cost_usd"])
        self.assertIsNone(core.read_log()[-1]["cost_usd"])
        self.response = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"total_cost": 0.02},
        }
        self.ask()
        event = next(e for e in self.events() if e["event"] == "call.response")
        self.assertTrue(event["cost_known"])
        self.assertEqual(event["cost_usd"], 0.02)

    def test_metadata_cannot_lose_paid_answer_when_cwd_disappears(self):
        previous = Path.cwd()
        gone = Path(self.directory.name) / "gone"
        gone.mkdir()
        os.chdir(gone)

        def response(*_args, **_kwargs):
            gone.rmdir()
            return self.response

        try:
            with (
                patch.dict(os.environ, ORASK_DIAGNOSTIC_DIR="../relative_logs"),
                patch.object(core, "_request", side_effect=response),
            ):
                result = core.ask("q", model="test/model")
                self.assertTrue(result["ok"])
                self.assertEqual(result["answer"], "PRIVATE_ANSWER")
        finally:
            os.chdir(previous)

    def test_file_inputs_and_transcript_saves_are_logged_by_size_only(self):
        source = Path(self.directory.name) / "private_source.py"
        source.write_text("PRIVATE_SOURCE")
        result = self.ask(files=[str(source)], thread="private-thread")
        self.assertTrue(result["ok"])
        events = self.events()
        self.assertIn("storage.saved", {e["event"] for e in events})
        self.assertNotIn("private_source", json.dumps(events))
        self.assertNotIn("PRIVATE_SOURCE", json.dumps(events))

    def test_logging_failure_preserves_paid_answer_and_exposes_write_errors(self):
        from orask import diagnostics

        with (
            patch.object(diagnostics, "_append", side_effect=OSError("PRIVATE_FAILURE")),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            result = self.ask()
        self.assertTrue(result["ok"])
        self.assertGreater(result["diagnostics"]["write_errors"], 0)
        self.assertEqual(output.getvalue(), "")

    def test_rotation_is_bounded_and_unknown_fields_are_discarded(self):
        from orask import diagnostics

        with patch.object(diagnostics, "MAX_BYTES", 1000):
            for i in range(40):
                self.assertTrue(
                    diagnostics.emit(
                        "test.event",
                        attempt=i,
                        payload="PRIVATE_PAYLOAD",
                        headers={"key": "PRIVATE_KEY"},
                    )
                )
        files = list(self.logs.glob("diagnostics.jsonl*"))
        self.assertEqual(len(files), 4)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 1000)
            self.assertNotIn("PRIVATE_", path.read_text())
            for line in path.read_text().splitlines():
                self.assertEqual(json.loads(line)["event"], "test.event")

    def test_unsafe_files_are_refused_without_mutating_the_target(self):
        from orask import diagnostics

        self.logs.mkdir(mode=0o700)
        victim = Path(self.directory.name) / "victim"
        victim.write_text("PRIVATE_FILE")
        victim.chmod(0o600)
        target = self.logs / "diagnostics.jsonl"
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target.symlink_to(victim)
                elif kind == "hardlink":
                    os.link(victim, target)
                else:
                    os.mkfifo(target, 0o600)
                self.assertFalse(diagnostics.emit("test.event"))
                self.assertEqual(victim.read_text(), "PRIVATE_FILE")
                target.unlink()

    def test_busy_log_does_not_hang_the_call(self):
        from orask import diagnostics

        self.assertTrue(diagnostics.emit("test.event"))
        with (self.logs / ".lock").open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            with patch.object(diagnostics, "LOCK_WAIT_S", 0.01):
                self.assertFalse(diagnostics.emit("test.busy"))
            self.assertLess(time.monotonic() - started, 0.5)

    def test_multiple_processes_write_complete_json_records(self):
        script = (
            "from orask import diagnostics; "
            "[diagnostics.emit('test.concurrent', attempt=i) for i in range(25)]"
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual((process.returncode, stdout, stderr), (0, b"", b""))
        self.assertEqual(len(self.events()), 100)

    def test_cli_can_still_fit_output_and_mcp_never_increases_a_billed_budget(self):
        result = self.ask(max_tokens=32000, max_context_tokens=1000)
        self.assertLess(result["max_tokens"], 1000)
        result = self.ask(max_tokens=16000, _mcp_call=True)
        self.assertEqual(result["max_tokens"], 16000)


if __name__ == "__main__":
    unittest.main()
