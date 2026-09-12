"""Recoverable MCP calls over the stdlib engine, independent of client lifetime.

Only the worker receives request data, through an anonymous private file descriptor.
Saved records contain results and status. Reading a record never starts another call.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import core, diagnostics

WORKER_COMMAND = [sys.executable, "-m", "orask.consultations"]
MAX_RECORD_BYTES = 128 * 1024 * 1024
_children: list[subprocess.Popen] = []
_children_lock = threading.Lock()


def _root() -> Path:
    return core.STATE_DIR.absolute() / "consultations"


def _directory(consultation_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", consultation_id):
        raise core.OpenRouterError(
            "Invalid consultation_id; use the ID returned by ask_llm/ask_panel"
        )
    return _root() / consultation_id


def _reap() -> None:
    with _children_lock:
        _children[:] = [child for child in _children if child.poll() is None]


def _running(directory: Path) -> bool:
    # The parent takes this lock before spawning and the child inherits the descriptor.
    # It therefore has no startup race, stale-PID ambiguity, or dependency on the MCP server.
    with os.fdopen(core._open_regular_fd(directory / "worker.lock", os.O_RDWR), "rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def _read_record(directory: Path, consultation_id: str) -> dict[str, Any]:
    raw, problem = core._slurp(directory / "result.json", core.MAX_FILE_BYTES)
    if problem:
        raise core.OpenRouterError(f"Cannot recover consultation {consultation_id}: {problem}")
    record = json.loads(raw)
    if (
        not isinstance(record, dict)
        or record.get("consultation_id") != consultation_id
        or record.get("kind") not in {"ask_llm", "ask_panel"}
        or record.get("status") not in {"running", "completed", "failed", "timed_out"}
        or not isinstance(record.get("results"), list)
        or core._nonnegative_number(record.get("created_at")) is None
        or not isinstance(record.get("notes"), list)
        or any(not isinstance(note, str) for note in record["notes"])
        or (record.get("error") is not None and not isinstance(record["error"], str))
        or (record.get("category") is not None and not isinstance(record["category"], str))
        or not isinstance(record.get("show_reasoning"), bool)
        or core._nonnegative_number(record.get("timeout_s")) is None
        or not 0 < record["timeout_s"] <= 86400
    ):
        raise ValueError("invalid record structure")
    for index in range(len(record["results"])):
        item = record["results"][index]
        filename = f"member-{index}.json"
        if (
            isinstance(item, dict)
            and item.get("pending")
            and not isinstance(item.get("model"), str)
        ):
            raise ValueError("invalid pending model result")
        # A member may have been durably saved just before a manifest write failed.
        # Recover that file even when the older manifest still calls the slot pending.
        if isinstance(item, dict) and item.get("pending") and (directory / filename).exists():
            item = {"result_file": filename}
        if isinstance(item, dict) and "result_file" in item:
            if item["result_file"] != filename:
                raise ValueError("invalid saved result filename")
            raw, problem = core._slurp(directory / item["result_file"], MAX_RECORD_BYTES)
            if problem:
                raise core.OpenRouterError(
                    f"Cannot recover consultation {consultation_id}: {problem}"
                )
            item = json.loads(raw)
            record["results"][index] = item
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("model"), str)
            or any(
                key in item and not isinstance(item[key], bool)
                for key in ("pending", "ok", "incomplete")
            )
            or any(
                item.get(key) is not None and not isinstance(item[key], str)
                for key in ("answer", "reasoning", "error")
            )
            or not isinstance(item.get("notes", []), list)
            or any(not isinstance(note, str) for note in item.get("notes", []))
        ):
            raise ValueError("invalid saved model result")
        usage = item.get("usage")
        if usage is not None and (
            not isinstance(usage, dict)
            or (
                usage.get("cost_usd") is not None
                and (
                    not isinstance(usage["cost_usd"], (int, float))
                    or core._nonnegative_number(usage["cost_usd"]) is None
                )
            )
        ):
            raise ValueError("invalid saved model usage")
    return record


def get(consultation_id: str) -> dict[str, Any]:
    _reap()
    directory = _directory(consultation_id)
    try:
        record = _read_record(directory, consultation_id)
        if record["status"] == "running" and not _running(directory):
            # Re-read after checking the lock: a worker can finish between the first
            # read and exit. Never label its newly committed final answer interrupted.
            record = _read_record(directory, consultation_id)
            if record["status"] in {"completed", "failed", "timed_out"}:
                return record
            record["status"] = "interrupted"
            record["error"] = (
                "The worker exited before saving a final result. Completed members are below. "
                "Unfinished requests may have been billed; no automatic retry was made."
            )
        return record
    except (OSError, ValueError, TypeError) as exc:
        raise core.OpenRouterError(f"Cannot recover consultation {consultation_id}: {exc}") from exc


def recent() -> list[dict[str, Any]]:
    """Recover IDs when the client lost even the initial receipt. No prompt text."""
    _reap()
    rows = []
    try:
        candidates = sorted(
            (p for p in _root().iterdir() if re.fullmatch(r"[0-9a-f]{32}", p.name)),
            key=lambda p: p.lstat().st_mtime,
            reverse=True,
        )[:20]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise core.OpenRouterError(f"Cannot list consultations: {exc}") from exc
    for path in candidates:
        try:
            record = get(path.name)
            rows.append({k: record[k] for k in ("consultation_id", "kind", "status", "created_at")})
        except (core.OpenRouterError, KeyError):
            rows.append({"consultation_id": path.name, "status": "unreadable"})
    return rows


def start(kind: str, kwargs: dict[str, Any], notes: list[str]) -> str:
    diagnostics.emit("consultation.admission", operation=kind)
    if kind not in {"ask_llm", "ask_panel"}:
        raise core.OpenRouterError("Unknown consultation kind")
    encoded = json.dumps(kwargs).encode("utf-8")
    if len(encoded) > core.MAX_FILE_BYTES:
        raise core.OpenRouterError(
            "Consultation arguments exceeded the 32 MiB limit; no model request sent"
        )
    timeout = core._float_setting("consultation_timeout_s", 3600.0) or 3600.0
    if timeout > 86400:
        raise core.OpenRouterError("consultation_timeout_s must be at most 86400 seconds")
    _reap()
    spawned_id: str | None = None
    # Nonblocking admission across MCP instances; refuse before billing if saturated.
    try:
        _root().mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(
            core._open_regular_fd(_root() / ".admission.lock", os.O_RDWR | os.O_CREAT), "rb"
        ) as admission:
            try:
                fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise core.OpenRouterError(
                    "Another consultation is starting; try again shortly"
                ) from exc
            active = sum(
                _running(p)
                for p in _root().iterdir()
                if re.fullmatch(r"[0-9a-f]{32}", p.name) and (p / "worker.lock").exists()
            )
            limit = core._setting("max_active_consultations", 8) or 8
            if active >= limit:
                raise core.OpenRouterError(
                    f"All {limit} consultation slots are busy. Use get_consultation to recover "
                    "existing work; no new model request was sent."
                )
            consultation_id = uuid.uuid4().hex
            directory = _directory(consultation_id)
            directory.mkdir(mode=0o700)
            lock = core._open_regular_fd(directory / "worker.lock", os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                record = {
                    "consultation_id": consultation_id,
                    "kind": kind,
                    "status": "running",
                    "created_at": time.time(),
                    "results": [],
                    "notes": notes,
                    "show_reasoning": bool(kwargs.get("include_reasoning")),
                    "category": kwargs.get("category") if not kwargs.get("models") else None,
                    "timeout_s": timeout,
                }
                if not core._write_json_atomic(directory / "result.json", record):
                    raise core.OpenRouterError(
                        "Cannot save consultation receipt; no model request sent"
                    )
                # No request data in argv, named request files, stdout, or stderr. A child
                # can finish after client shutdown without holding its protocol pipes open.
                with tempfile.TemporaryFile(mode="w+b", dir=directory) as request:
                    request.write(encoded)
                    request.seek(0)
                    env = dict(os.environ)
                    env["PYTHONPATH"] = (
                        str(core.PROJECT_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
                    )
                    child = subprocess.Popen(
                        [*WORKER_COMMAND, consultation_id, str(lock)],
                        stdin=request,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        pass_fds=(lock,),
                        start_new_session=True,
                        env=env,
                    )
                    spawned_id = consultation_id
                    with _children_lock:
                        _children.append(child)
                    diagnostics.emit(
                        "consultation.started",
                        consultation_id=consultation_id,
                        operation=kind,
                        worker_pid=child.pid,
                        timeout_s=timeout,
                    )
            finally:
                os.close(lock)
            return consultation_id
    except OSError as exc:
        diagnostics.emit("consultation.start_error", error_type=type(exc).__name__)
        if spawned_id is not None:
            # The worker can already be billing. Losing its ID during local handle
            # cleanup would invite a duplicate consultation instead of recovery.
            return spawned_id
        raise core.OpenRouterError(
            f"Cannot start recoverable consultation: {exc}; no model request sent"
        ) from exc


def worker_main(consultation_id: str, lock_fd: int) -> None:
    diagnostics.set_consultation(consultation_id)
    diagnostics.emit("worker.start")
    directory = _directory(consultation_id)
    record = get(consultation_id)
    state_lock = threading.RLock()
    saved: dict[int, dict[str, Any]] = {}

    def save() -> None:
        manifest = {**record, "results": []}
        for index, result in enumerate(record["results"]):
            if result.get("pending"):
                manifest["results"].append(result)
                continue
            filename = f"member-{index}.json"
            if saved.get(index) != result:
                if not core._write_json_atomic(
                    directory / filename, result, max_bytes=MAX_RECORD_BYTES
                ):
                    raise core.OpenRouterError("Could not save a model result; check local storage")
                # core.ask_panel adds roster notes to its first result at the end.
                saved[index] = {**result, "notes": list(result.get("notes", []))}
            manifest["results"].append({"result_file": filename})
        if not core._write_json_atomic(
            directory / "result.json", manifest, max_bytes=core.MAX_FILE_BYTES
        ):
            raise core.OpenRouterError(
                "Could not save consultation results. Earlier snapshots remain; "
                "check local storage before any paid retry."
            )

    def progress(results: list[dict[str, Any]]) -> None:
        with state_lock:
            record["results"] = list(results)
            try:
                save()
                diagnostics.emit(
                    "worker.progress",
                    members=len(results),
                    answered=sum(bool(r.get("ok")) for r in results),
                    pending=sum(bool(r.get("pending")) for r in results),
                )
            except core.OpenRouterError:
                if all(r.get("pending") for r in results):
                    raise  # establish the recoverable roster before submitting paid requests
                note = "An intermediate result save failed; final persistence was attempted again."
                if note not in record["notes"]:
                    record["notes"].append(note)

    def expire() -> None:
        with state_lock:
            if record["status"] != "running":
                return
            try:
                diagnostics.emit(
                    "worker.timeout", consultation_id=consultation_id, timeout_s=timeout
                )
                record["status"] = "timed_out"
                record["error"] = (
                    f"Consultation exceeded its {timeout:g}s wall-clock limit. Completed members "
                    "are preserved. Unfinished requests may have been billed; no automatic retry "
                    "was made. Adjust consultation_timeout_s only for a subsequent consultation."
                )
                save()
            finally:
                # ThreadPoolExecutor shutdown would wait for blocked sockets. Exit this isolated
                # worker, releasing its lock and sockets without affecting the MCP server.
                os._exit(1)

    timeout = record["timeout_s"]
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        raw = sys.stdin.buffer.read(core.MAX_FILE_BYTES + 1)
        if len(raw) > core.MAX_FILE_BYTES:
            raise core.OpenRouterError(
                "Consultation arguments exceeded the 32 MiB limit; no model request sent"
            )
        kwargs = json.loads(raw)
        if not isinstance(kwargs, dict):
            raise core.OpenRouterError("Consultation arguments must be an object")
        if record["kind"] == "ask_panel":
            results = core.ask_panel(**kwargs, on_result=progress)
        else:
            progress(
                [
                    {
                        "model": kwargs.get("model") or kwargs.get("category") or "default model",
                        "pending": True,
                    }
                ]
            )
            results = [core.ask(**kwargs)]
        with state_lock:
            record["results"] = results
            record["status"] = "completed"
            save()
            diagnostics.emit(
                "worker.completed",
                members=len(results),
                answered=sum(bool(r.get("ok")) for r in results),
            )
    except Exception as exc:
        diagnostics.emit("worker.error", error_type=type(exc).__name__)
        with state_lock:
            record["status"] = "failed"
            record["error"] = (
                str(exc)
                if isinstance(exc, core.OpenRouterError)
                else (
                    f"Consultation worker failed ({type(exc).__name__}); "
                    "private request data omitted. "
                    "No automatic retry was made."
                )
            )
            details = getattr(exc, "orask_diagnostics", None)
            if details:
                record["notes"].append("diagnostics: " + json.dumps(details))
            with contextlib.suppress(core.OpenRouterError):
                save()
    finally:
        timer.cancel()
        diagnostics.emit("worker.end", status=record["status"])
        os.close(lock_fd)


if __name__ == "__main__":
    worker_main(sys.argv[1], int(sys.argv[2]))
