"""Conservative, dependency-free edits of the installer-owned Codex table.

The scanner finds statements, not lines: bracket-looking text in strings and
arrays cannot end a table. Python 3.11+ also validates TOML and the complete
semantic change. Python 3.10 retains the same structural checks and refuses
inline/dotted definitions of the managed server instead of guessing ownership.
"""

import copy
import fcntl
import importlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .core import MCP_TOOLS

try:
    _toml: Any = importlib.import_module("tomllib")
except ModuleNotFoundError:
    _toml = None

TARGET = ("mcp_servers", "openrouter")


def _statements(text: str) -> list[tuple[int, int, str]]:
    """Locate complete statements while tracking strings, arrays and comments."""
    result = []
    stack: list[str] = []
    quote = ""
    start = index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote[0] == '"':
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                if len(quote) == 3:
                    # TOML permits one or two quotes just inside a closing delimiter.
                    for _ in range(2):
                        if index < len(text) and text[index] == quote[0]:
                            index += 1
                quote = ""
                continue
            if char == "\n" and len(quote) == 1:
                raise ValueError("unterminated single-line TOML string")
        elif char in "\"'":
            quote = char * (3 if text.startswith(char * 3, index) else 1)
            index += len(quote)
            continue
        elif char == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack or stack.pop() != {"}": "{", "]": "["}[char]:
                raise ValueError("unbalanced TOML delimiters")
        if char == "\n" and not quote and not stack:
            result.append((start, index + 1, text[start:index + 1]))
            start = index + 1
        index += 1
    if quote or stack:
        raise ValueError("unterminated TOML string or array")
    if start < len(text):
        result.append((start, len(text), text[start:]))
    return result


def _key_path(text: str, end: str) -> tuple[tuple[str, ...], int]:
    """Read TOML bare/basic/literal keys without a third-party parser."""
    keys = []
    index = 0
    while True:
        while index < len(text) and text[index] in " \t":
            index += 1
        if index >= len(text):
            raise ValueError("incomplete TOML key")
        key = ""
        char = text[index]
        if char in "\"'":
            quote = char
            index += 1
            while index < len(text) and text[index] != quote:
                char = text[index]
                if char == "\\" and quote == '"':
                    index += 1
                    if index >= len(text):
                        raise ValueError("incomplete TOML escape")
                    escape = text[index]
                    if escape in "uU":
                        count = 4 if escape == "u" else 8
                        digits = text[index + 1:index + 1 + count]
                        if not re.fullmatch(rf"[0-9a-fA-F]{{{count}}}", digits):
                            raise ValueError("invalid TOML Unicode escape")
                        value = int(digits, 16)
                        if 0xD800 <= value <= 0xDFFF or value > 0x10FFFF:
                            raise ValueError("invalid TOML Unicode scalar")
                        char = chr(value)
                        index += count
                    else:
                        escapes = {"b": "\b", "t": "\t", "n": "\n", "f": "\f",
                                   "r": "\r", '"': '"', "\\": "\\"}
                        if escape not in escapes:
                            raise ValueError("unsupported TOML key escape")
                        char = escapes[escape]
                elif (ord(char) < 32 and char != "\t") or ord(char) == 127:
                    raise ValueError("invalid control character in TOML key")
                key += char
                index += 1
            if index >= len(text):
                raise ValueError("unterminated TOML key")
            index += 1
        else:
            match = re.match(r"[A-Za-z0-9_-]+", text[index:])
            if match is None:
                raise ValueError("unsupported TOML key")
            key = match.group()
            index += len(key)
        keys.append(key)
        while index < len(text) and text[index] in " \t":
            index += 1
        if text.startswith(end, index):
            return tuple(keys), index + len(end)
        if index >= len(text) or text[index] != ".":
            raise ValueError("unsupported TOML key syntax")
        index += 1


def _table(statement: str) -> tuple[tuple[str, ...], bool]:
    array = statement.startswith("[[")
    width = 2 if array else 1
    path, end = _key_path(statement[width:], "]" * width)
    tail = statement[width + end:].strip()
    if tail and not tail.startswith("#"):
        raise ValueError("unsupported TOML table syntax")
    return path, array


def _managed_spans(original: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    table: tuple[str, ...] = ()
    start = end = None
    for left, right, raw in _statements(original):
        statement = raw.strip()
        if not statement or statement.startswith("#"):
            continue
        if statement.startswith("["):
            if start is not None and end is not None:
                spans.append((start, end))
            start = end = None
            table, array = _table(statement)
            if array and (table[:2] == TARGET or TARGET[:len(table)] == table):
                raise ValueError("managed server cannot be an array of tables")
            if table[:2] == TARGET:
                start, end = left, right
        else:
            key, _ = _key_path(statement, "=")
            absolute = table + key
            if TARGET[:len(absolute)] == absolute or (
                absolute[:2] == TARGET and table[:2] != TARGET
            ):
                raise ValueError(
                    "inline/dotted openrouter definitions need manual migration "
                    "to [mcp_servers.openrouter]"
                )
            if start is not None:
                end = right
    if start is not None and end is not None:
        spans.append((start, end))
    return spans


def codex_block(command: str, interpreter: str) -> str:
    def quoted(value: str) -> str:
        # JSON's surrogate-pair escapes are invalid TOML; UTF-8 scalar values are valid.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")

    return (
        "[mcp_servers.openrouter]\n"
        f"command = {quoted(command)}\n"
        "args = []\n"
        f"env = {{ ORASK_PYTHON = {quoted(interpreter)} }}\n"
        "startup_timeout_sec = 30\n"
        "# Reasoning models on a large context can take minutes; a panel runs in parallel.\n"
        "tool_timeout_sec = 600\n"
        f"enabled_tools = {json.dumps(list(MCP_TOOLS))}\n"
    )


def update_codex_text(original: str, command: str, interpreter: str) -> str:
    parsed: dict[str, Any] = _toml.loads(original) if _toml is not None else {}
    spans = _managed_spans(original)
    block = codex_block(command, interpreter)
    if spans:
        updated = original[:spans[0][0]] + block
        for index, (_, end) in enumerate(spans):
            next_start = spans[index + 1][0] if index + 1 < len(spans) else len(original)
            updated += original[end:next_start]
    else:
        updated = original + ("\n" if original and not original.endswith("\n") else "")
        updated += "\n" + block
    if _toml is not None:
        expected = copy.deepcopy(parsed)
        servers = expected.setdefault("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp_servers must be a TOML table")
        servers["openrouter"] = _toml.loads(block)["mcp_servers"]["openrouter"]
        if _toml.loads(updated) != expected:
            raise ValueError("refused TOML edit that changes unrelated configuration")
    return updated


def read_config(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"refusing non-regular configuration file: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev, info.st_ino
        ):
            raise ValueError("configuration changed while opening it; retry installation")
        return handle.read()


def _write_backup(path: Path, contents: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        complete = True
    finally:
        if not complete:
            path.unlink(missing_ok=True)


def backup_config(source: Path, destination: Path) -> None:
    contents = read_config(source)
    if contents is None:
        raise FileNotFoundError("configuration disappeared before backup")
    _write_backup(destination, contents)


def _register_codex(path: Path, command: str, interpreter: str, backup: Path) -> str:
    original = read_config(path)
    updated = update_codex_text((original or b"").decode("utf-8"), command, interpreter)
    encoded = updated.encode("utf-8")
    if original == encoded:
        return "unchanged"
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".orask-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if original is not None:
            # Exclusive creation prevents a second run or a planted symlink from
            # replacing an earlier backup. Backups can contain private settings.
            _write_backup(backup, original)
        if read_config(path) != original:
            raise ValueError("configuration changed during installation; retry")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return "updated" if original is not None else "added"


def register_codex(path: Path, command: str, interpreter: str, backup: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A stable sidecar serializes installers across atomic replacement of the
    # config inode. Other editors do not share this lock; check their changes too.
    descriptor = os.open(path.with_name(path.name + ".orask.lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("configuration lock must be a regular file")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _register_codex(path, command, interpreter, backup)


def main() -> int:
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "backup":
            backup_config(Path(sys.argv[2]), Path(sys.argv[3]))
            return 0
        path, command, interpreter, backup = sys.argv[1:]
        print(register_codex(Path(path), command, interpreter, Path(backup)))
    except (OSError, ValueError) as exc:
        # Parser errors may include private config values. Keep diagnostics structural.
        print(f"Configuration update refused ({type(exc).__name__}); "
              "check file access and TOML syntax; inline/dotted managed definitions "
              "must be migrated manually. Original config was not replaced.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
