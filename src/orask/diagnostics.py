"""Private, bounded lifecycle metadata. Never send diagnostics to MCP stdout.

Only explicitly allowed scalar fields are recorded. Request/response bodies,
headers, source paths, exception messages and reasoning text are never accepted.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import functools
import json
import math
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
LOCK_WAIT_S = 0.25
FIELDS = frozenset(
    [
        "call_id",
        "consultation_id",
        "operation",
        "model",
        "requested_max_tokens",
        "max_tokens",
        "context_window",
        "estimated_prompt_tokens",
        "estimated_cost_usd",
        "pricing_known",
        "cost_guard_usd",
        "effort",
        "requested_effort",
        "question_chars",
        "context_chars",
        "input_chars",
        "files_count",
        "file_index",
        "file_kind",
        "warning",
        "attachments_count",
        "notes_count",
        "method",
        "endpoint",
        "attempt",
        "timeout_s",
        "http_status",
        "response_bytes",
        "request_bytes",
        "delay_s",
        "retryable",
        "error_type",
        "elapsed_s",
        "ok",
        "incomplete",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cost_usd",
        "cost_known",
        "answer_chars",
        "reasoning_chars",
        "provider",
        "generation_id",
        "worker_pid",
        "status",
        "members",
        "answered",
        "pending",
        "bytes_written",
        "storage",
        "reason",
        "phase",
        "cache_source",
        "cache_age_s",
        "models_count",
        "runtime",
        "python_version",
        "write_errors",
        "error_location",
    ]
)
_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("orask_call_id", default=None)
_consultation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "orask_consultation_id", default=None
)
_write_errors: contextvars.ContextVar[int] = contextvars.ContextVar("orask_log_errors", default=0)
_warned = False
P = ParamSpec("P")
T = TypeVar("T")


def log_path() -> Path:
    directory = Path(os.environ.get("ORASK_DIAGNOSTIC_DIR") or f"/tmp/orask-{os.getuid()}")
    # Metadata must remain available even if a relative path's cwd disappeared
    # during generation. emit() will count the failed write; never lose the answer.
    with contextlib.suppress(OSError):
        directory = directory.absolute()
    return directory / "diagnostics.jsonl"


def set_consultation(consultation_id: str) -> None:
    # The detached worker owns its process; each panel thread gets a copied context.
    _consultation_id.set(consultation_id)


def current() -> dict[str, Any]:
    return {
        "call_id": _call_id.get(),
        "consultation_id": _consultation_id.get(),
        "path": str(log_path()),
        "write_errors": _write_errors.get(),
    }


def _open_private(name: str, flags: int, directory: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError("unsafe diagnostic file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _append(encoded: bytes) -> None:
    path = log_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("unsafe diagnostic directory")
        lock = _open_private(".lock", os.O_RDWR | os.O_CREAT, directory)
        try:
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError("diagnostic lock busy") from None
                    time.sleep(0.005)
            fd = _open_private(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT, directory)
            try:
                if os.fstat(fd).st_size + len(encoded) > MAX_BYTES:
                    os.close(fd)
                    fd = -1
                    for index in range(BACKUPS, 0, -1):
                        source = path.name if index == 1 else f"{path.name}.{index - 1}"
                        with contextlib.suppress(FileNotFoundError):
                            os.replace(
                                source,
                                f"{path.name}.{index}",
                                src_dir_fd=directory,
                                dst_dir_fd=directory,
                            )
                    fd = _open_private(path.name, os.O_RDWR | os.O_APPEND | os.O_CREAT, directory)
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    os.write(fd, b"\n")
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("diagnostic write stalled")
                    view = view[written:]
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(lock)
    finally:
        os.close(directory)


def emit(event: str, **fields: Any) -> bool:
    """Best effort and bounded. A disk failure must not lose an already paid answer."""
    global _warned
    try:
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event[:100],
            "pid": os.getpid(),
            "call_id": _call_id.get(),
            "consultation_id": _consultation_id.get(),
        }
        for key, value in fields.items():
            if key not in FIELDS:
                continue
            if (
                value is None
                or isinstance(value, (bool, int))
                or (isinstance(value, float) and math.isfinite(value))
            ):
                row[key] = value
            elif isinstance(value, str):
                row[key] = value[:256]
        _append((json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n").encode())
        return True
    except Exception as exc:
        _write_errors.set(_write_errors.get() + 1)
        if not _warned:
            _warned = True
            with contextlib.suppress(Exception):
                print(
                    f"orask: diagnostic logging unavailable ({type(exc).__name__}); "
                    "check ORASK_DIAGNOSTIC_DIR permissions/storage",
                    file=sys.stderr,
                )
        return False


def traced(operation: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Record start/end/error without capturing function arguments or exceptions."""

    def decorate(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def run(*args: P.args, **kwargs: P.kwargs) -> T:
            token = _call_id.set(uuid.uuid4().hex) if operation == "call" else None
            errors = _write_errors.set(0) if operation == "call" else None
            started = time.monotonic()
            emit(f"{operation}.start")
            try:
                result = func(*args, **kwargs)
                emit(f"{operation}.end", elapsed_s=round(time.monotonic() - started, 3))
                if operation == "call" and isinstance(result, dict):
                    result["diagnostics"] = current()
                return result
            except BaseException as exc:
                trace = exc.__traceback__
                while trace is not None and trace.tb_next is not None:
                    trace = trace.tb_next
                location = (
                    f"{Path(trace.tb_frame.f_code.co_filename).name}:{trace.tb_lineno}"
                    if trace is not None
                    else None
                )
                emit(
                    f"{operation}.error",
                    error_type=type(exc).__name__,
                    error_location=location,
                    elapsed_s=round(time.monotonic() - started, 3),
                )
                if operation == "call":
                    with contextlib.suppress(Exception):
                        exc.__dict__["orask_diagnostics"] = current()
                raise
            finally:
                if token is not None:
                    _call_id.reset(token)
                if errors is not None:
                    _write_errors.reset(errors)

        return run

    return decorate
