---
topic: python
triggers: writing or reviewing Python, async code, subprocess calls, file and path handling, config parsing, retries against an API, anything handling money or credentials
source: written from scratch, with defects verified in this repository
verified: 2026-09-12
---

# Python

Defect classes, ordered by how much damage they do. Several of these were live
bugs in this repository and are named as such.

## Interpreter

On Jeff's machines Python is Miniconda only, rooted at `/home/jman/miniconda3`.
Never use system Python, never take one off `PATH`, never create a venv beside a
conda env. Invoke by absolute path:
`/home/jman/miniconda3/envs/<env>/bin/python`. Check `conda env list` and any
launch script before choosing; do not infer the env from its name.

## Silent data and money loss

**Never let a config value crash a path that has already spent something.**
This repo billed a model call, then raised `ValueError` from `int(setting)`
while saving the transcript, losing the answer it had just paid for.

```python
def _setting(key, default):
    try:
        return int(load_config().get(key, default))
    except (TypeError, ValueError, OverflowError):
        return default
```

Integer settings need a coercer like this; floating-point settings also need
`math.isfinite` and domain checks. Parsing a number does not establish that it
is a valid timeout or price. Work after an irreversible action must preserve
the result even when optional bookkeeping fails.

**Persistence after a paid or irreversible operation is best effort.**

```python
try:
    save_thread(...)
except Exception:
    # A transcript problem must never destroy an answer already billed for.
    return False
```

**Do not retry a non-idempotent POST after an ambiguous failure.** A 502 from a
completions endpoint can arrive after generation and billing. This bridge's
POST retry policy permits only `408` and `429`, and excludes read timeouts.
For another API, establish its idempotency and retry contract before choosing
statuses; this bridge's policy is not a universal HTTP guarantee.

## Exceptions

`except Exception: pass` is how a bug becomes unfindable. When you genuinely
want a catch-all, write the reason beside it:

```python
except Exception as exc:
    # A panel slot swallows its own failure so one bad model cannot drop the rest.
    return {"ok": False, "error": str(exc)}
```

Catch the narrowest type that can actually occur. `except (TypeError, ValueError)`
beats `except Exception` every time you can name the failures.

Do not suppress `BaseException`: it includes `KeyboardInterrupt`, `SystemExit`
and `asyncio.CancelledError`. Cleanup code that catches it must re-raise.

Raise the exception type the caller's framework forwards. This repository's
MCP 2.x server uses `ToolError` for actionable failures; unexpected exceptions
are hidden behind the SDK's generic error message.

## Mutable defaults and late binding

```python
# A default of [] would be shared across calls.
def f(items=None):
    if items is None:
        items = []
    return items
```

```python
fs = [lambda: i for i in range(3)]      # all three return 2
fs = [lambda i=i: i for i in range(3)]  # correct
```

Same trap in a dataclass: use `field(default_factory=list)`.

## Subprocess

```python
subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
```

Always pass `check` explicitly — without it a failing command looks like a
success. Always a timeout. Never `shell=True` with anything a user or another
program can influence; pass a list. Never build a command by interpolating a
path into a string.

## Files and paths

Use `pathlib`. Use context managers so a descriptor cannot leak.

Check a file's size with `fstat` on the open descriptor, not `stat` on the path
before opening. The path can be swapped between the two calls.

Resolve before you compare. A denylist, an allowlist or a "must live under this
directory" check that matches on the unresolved path is walked past with a
symlink:

```python
p = Path(user_input).resolve()
if not p.is_relative_to(root.resolve()):
    raise ValueError(...)
```

Never trust an extension to tell you what a file is, and never trust a short
magic number on its own. This repo classified any CSV whose first column was
`ID3` as an MP3.

For a read-modify-write that another process might also be doing, take an
exclusive `flock` around the whole cycle, not around each half.

Use `tempfile.mkstemp` / `mkdtemp`, never `mktemp`.

## Async

A blocking call inside an async handler stalls the entire event loop, including
every other request:

```python
result = await asyncio.to_thread(blocking_call, arg)
```

Never call `time.sleep`, `requests`, or a synchronous DB driver in a coroutine.

A bare `asyncio.create_task(...)` whose result nobody awaits can leave an
exception unhandled, and the task can be garbage collected mid-flight. Keep a
reference and retrieve its result or exception, or await it.

`asyncio.gather` normally propagates the first exception immediately; the other
awaitables keep running. `return_exceptions=True` waits for all outcomes and
returns exceptions alongside successful values. `TaskGroup` (Python 3.11+)
cancels remaining tasks when a task raises a non-cancellation exception.
See the [asyncio task documentation](https://docs.python.org/3/library/asyncio-task.html#running-tasks-concurrently).

## Typing

Run mypy and fix the code rather than adding `type: ignore`. If a suppression
must stay, write why beside it.

`Optional[X]` is not implicit. A parameter defaulting to `None` must say
`X | None`.

Narrow before use. mypy flagging `Item "None" of "X | None" has no attribute` is
usually pointing at a real crash, not being pedantic.

## Correctness details that bite

- `is` compares identity. Use it only for `None`, `True`, `False`.
- `datetime.now()` is naive. Use `datetime.now(timezone.utc)` and keep
  everything aware until the moment you format for display.
- Floating point is wrong for money. Use `Decimal`, or integer minor units.
- A generator is consumed once. Iterating it twice silently gives nothing the
  second time.
- `dict` preserves insertion order; `set` does not preserve anything. Never let
  output ordering depend on a set.
- Mutating a list while iterating it skips elements. Iterate a copy.
- `logging` takes lazy args: `log.info("got %s", x)`, not an f-string, so the
  formatting cost is skipped when the level is off.
- `functools.lru_cache` on a method keeps `self` alive until its entries are
  evicted or the cache is cleared; see the [cache lifetime documentation](https://docs.python.org/3/library/functools.html#functools.lru_cache).
- Module-level state in a long-lived server does not refresh itself. If a cache needs to
  notice a changed file, give it a TTL or an mtime check, or document that a
  restart is required.

## Standard library only, when that is the design

If a module is deliberately dependency-free, adding a third-party import
silently removes the property that made it safe. In this repo `core.py` is
stdlib-only so a broken `mcp` SDK cannot take the CLI down with it. Check for a
rule like that before reaching for a convenience library.

## Output channels

If stdout is a protocol channel — an MCP stdio server, a filter in a pipe —
nothing in that path may `print`. Send diagnostics to stderr or a log file. One
stray print corrupts the stream and the failure looks like a protocol bug.
