"""Private, detached consultation lifecycle tests. Provider fixture forbids network."""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCRATCH = tempfile.TemporaryDirectory(prefix="orask-consultations-")
for key, leaf in (
    ("ORASK_CONFIG_DIR", "config"),
    ("ORASK_STATE_DIR", "state"),
    ("ORASK_CACHE_DIR", "cache"),
):
    os.environ[key] = str(Path(SCRATCH.name) / leaf)
os.environ["OPENROUTER_API_KEY"] = ""

from orask import consultations, core


class Consultations(unittest.TestCase):
    def setUp(self):
        core._config_cache = None
        core.USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        core.USER_CONFIG.write_text("{}")
        self.worker = patch.object(
            consultations,
            "WORKER_COMMAND",
            [sys.executable, str(ROOT / "tests/test_mcp_offline.py"), "--worker"],
        )
        self.worker.start()
        self.addCleanup(self.worker.stop)

    def tearDown(self):
        for child in list(consultations._children):
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        consultations._reap()

    def start(self, **kwargs):
        return consultations.start(
            "ask_panel",
            {"question": "force-slow", "models": ["kimi", "glm"], "_mcp_call": True, **kwargs},
            [],
        )

    def wait(self, consultation_id, status=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = consultations.get(consultation_id)
            if record["status"] != "running" or (
                status == "partial" and any(r.get("ok") for r in record["results"])
            ):
                return record
            time.sleep(0.02)
        self.fail("worker did not reach expected state")

    def test_private_records_and_no_retained_request(self):
        old = os.umask(0)
        try:
            consultation_id = self.start(context="PRIVATE_INPUT_SENT_ONLY_TO_WORKER")
        finally:
            os.umask(old)
        record = self.wait(consultation_id)
        self.assertEqual(record["status"], "completed")
        directory = consultations._directory(consultation_id)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(b"PRIVATE_INPUT_SENT_ONLY_TO_WORKER", path.read_bytes())
        self.assertEqual(
            {p.name for p in directory.iterdir()},
            {"result.json", "worker.lock", "member-0.json", "member-1.json"},
        )

    def test_polling_keeps_exactly_one_request_per_member(self):
        consultation_id = self.start()
        record = self.wait(consultation_id)
        before = core.CALL_LOG.read_bytes()
        for _ in range(4):
            self.assertEqual(consultations.get(consultation_id), record)
        self.assertEqual(core.CALL_LOG.read_bytes(), before)

    def test_wall_deadline_keeps_completed_member(self):
        core.USER_CONFIG.write_text(json.dumps({"consultation_timeout_s": 0.3}))
        consultation_id = self.start()
        record = self.wait(consultation_id)
        self.assertEqual(record["status"], "timed_out", record)
        self.assertTrue(record["results"][0]["ok"])
        self.assertTrue(record["results"][1]["pending"])
        self.assertIn("may have been billed", record["error"])
        self.assertEqual(record["results"][0]["usage"]["cost_usd"], 0.02)

    def test_killed_worker_retains_partial_and_reports_interrupted(self):
        consultation_id = self.start()
        record = self.wait(consultation_id, "partial")
        self.assertEqual(record["status"], "running")
        child = consultations._children[-1]
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        record = consultations.get(consultation_id)
        self.assertEqual(record["status"], "interrupted")
        self.assertTrue(record["results"][0]["ok"])

    def test_active_limit_refuses_before_spawning(self):
        core.USER_CONFIG.write_text(json.dumps({"max_active_consultations": 1}))
        self.start()
        with patch.object(consultations.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(core.OpenRouterError, "slots are busy"):
                self.start()
            popen.assert_not_called()

    def test_storage_failure_refuses_before_spawning(self):
        with (
            patch.object(core, "_write_json_atomic", return_value=False),
            patch.object(consultations.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(core.OpenRouterError, "no model request sent"):
                self.start()
            popen.assert_not_called()

    def test_spawn_failure_leaves_recoverable_interrupted_receipt(self):
        with (
            patch.object(consultations.subprocess, "Popen", side_effect=OSError("cannot exec")),
            self.assertRaisesRegex(core.OpenRouterError, "no model request sent"),
        ):
            self.start()
        self.assertEqual(consultations.recent()[0]["status"], "interrupted")

    def test_post_spawn_cleanup_error_returns_existing_id(self):
        temporary_file = consultations.tempfile.TemporaryFile

        @contextmanager
        def close_fails(**kwargs):
            with temporary_file(**kwargs) as handle:
                yield handle
            raise OSError("close failed after spawn")

        with patch.object(consultations.tempfile, "TemporaryFile", close_fails):
            consultation_id = self.start(question="quick")
        self.assertEqual(self.wait(consultation_id)["status"], "completed")

    def test_panel_storage_has_no_combined_response_cap(self):
        consultation_id = self.start(question="force-wide")
        with patch.object(consultations, "MAX_RECORD_BYTES", 8000):
            record = self.wait(consultation_id)
        self.assertEqual(record["status"], "completed")
        self.assertEqual([len(r["answer"]) for r in record["results"]], [6000, 6000])

    def test_invalid_ids_and_symlinks_are_refused(self):
        for consultation_id in ("", "../env", "/tmp/result.json", "x" * 32):
            with self.assertRaises(core.OpenRouterError):
                consultations.get(consultation_id)
        directory = consultations._directory("a" * 32)
        target = Path(SCRATCH.name) / "elsewhere"
        target.mkdir(exist_ok=True)
        directory.symlink_to(target, target_is_directory=True)
        try:
            with self.assertRaises(core.OpenRouterError):
                consultations.get("a" * 32)
        finally:
            directory.unlink()

    def test_malformed_record_is_actionable(self):
        consultation_id = self.start(question="quick")
        self.wait(consultation_id)
        path = consultations._directory(consultation_id) / "result.json"
        for invalid in ([], {}, {"results": "bad"}, "bad json"):
            path.write_text(json.dumps(invalid))
            with self.assertRaises(core.OpenRouterError):
                consultations.get(consultation_id)

    def test_malformed_saved_output_cannot_crash_renderer(self):
        consultation_id = self.start(question="quick")
        record = self.wait(consultation_id)
        path = consultations._directory(consultation_id) / "result.json"
        for changes in (
            {"results": [{"pending": True}]},
            {"results": [{"model": "x", "usage": {"cost_usd": "bad"}}]},
            {"results": [{"model": "x", "usage": {"cost_usd": "0.02"}}]},
            {"results": [{"model": "x", "answer": []}]},
            {"error": {}},
            {"notes": [1]},
        ):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**record, **changes}))
                with self.assertRaises(core.OpenRouterError):
                    consultations.get(consultation_id)

    def test_second_read_after_exit_is_validated(self):
        consultation_id = self.start(question="quick")
        record = self.wait(consultation_id)
        record["status"] = "running"
        for final in ([], {**record, "status": "completed", "results": [{}]}):
            with (
                patch.object(
                    core,
                    "_slurp",
                    side_effect=[(json.dumps(record), None), (json.dumps(final), None)],
                ),
                patch.object(consultations, "_running", return_value=False),
                self.assertRaises(core.OpenRouterError),
            ):
                consultations.get(consultation_id)

    def test_queued_deadline_cannot_overwrite_completed_result(self):
        consultation_id = self.start(question="quick")
        record = self.wait(consultation_id)
        record["status"] = "running"
        directory = consultations._directory(consultation_id)
        (directory / "result.json").write_text(json.dumps(record))
        lock_fd = core._open_regular_fd(directory / "worker.lock", os.O_RDWR)
        core._lock_exclusive(lock_fd)

        class QueuedTimer:
            def __init__(self, _timeout, callback):
                self.callback = callback

            def start(self):
                pass

            def cancel(self):
                self.callback()  # cancellation can race with a callback already queued

        with (
            patch.object(consultations.threading, "Timer", QueuedTimer),
            patch.object(consultations.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))),
            patch.object(core, "ask_panel", return_value=record["results"]),
            patch.object(consultations.os, "_exit") as terminate,
        ):
            consultations.worker_main(consultation_id, lock_fd)
        self.assertEqual(consultations.get(consultation_id)["status"], "completed")
        terminate.assert_not_called()

    def test_oversized_arguments_refuse_before_spawn(self):
        with (
            patch.object(core, "MAX_FILE_BYTES", 64),
            patch.object(consultations.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(core.OpenRouterError, "arguments exceeded"):
                self.start(context="x" * 65)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
