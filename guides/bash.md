---
topic: bash
triggers: writing or reviewing a shell script, an installer, a CI step, a launcher, anything that deletes or moves files, any command built from a variable
source: written from scratch, with defects verified in this repository
verified: 2026-09-11
---

# Bash

Shell fails quietly and destructively. Most of this guide is about making it
fail loudly instead.

## The header

```bash
#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
```

Know what it does not cover, or you will trust it too far:

- `set -e` does **not** fire inside a condition, a `&&` chain, or any command
  whose result is tested. `if cmd; then` never aborts.
- `set -e` inside a function called in a condition is disabled for that whole
  call tree.
- `local x=$(cmd)` always succeeds, because `local` is the command whose status
  is reported. Declare first, assign second:

```bash
local x
x=$(cmd) || return 1
```

- `(( count++ ))` returns 1 when the result is 0, which kills the script under
  `set -e`. Use `(( count++ )) || true`, or `count=$(( count + 1 ))`.
- `pipefail` plus a short-circuiting reader gives you SIGPIPE failures:
  `cmd | head -1` can now fail.

## Quoting

Quote every expansion. The unquoted ones are the bugs.

```bash
"$var"  "$@"  "${arr[@]}"  "$(cmd)"
```

`$@` unquoted splits arguments on whitespace and glob-expands them. `"$@"` is
the only correct way to forward arguments. `"$*"` joins them into one string.

Use `[[ ]]`, which does not word-split, over `[ ]`. Inside `[[ ]]` the right
side of `==` and `!=` is a pattern unless quoted, so `[[ $f == "$pat" ]]`
compares literally and `[[ $f == $pat ]]` globs.

## Destruction

The empty-variable disaster:

```bash
rm -rf "$dir/"        # if dir is unset or empty, this is rm -rf /
rm -rf "${dir:?dir is unset}"/   # aborts instead
```

`set -u` catches unset, but not set-and-empty. `${var:?message}` catches both.

Before any `rm -rf`, `mv`, or truncating redirect, confirm the target resolves
where you think. Prefer deleting a directory you created yourself in the same
script:

```bash
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
```

`trap ... EXIT` runs on normal exit and on error. Add `INT TERM` when cleanup
must survive a Ctrl-C.

## Heredocs

Unquoted heredocs expand variables, command substitution and backticks:

```bash
cat <<'EOF'    # literal: nothing expands
cat <<EOF      # expands $var, $(cmd) and `cmd`
```

Default to the quoted form. This matters most for text that contains shell
metacharacters by nature. Backticks inside a double-quoted `git commit -m`
string execute, and in this repo that once injected live command output,
including account spend figures, into a commit message headed for a public
repository. Write the message to a file and use `git commit -F`.

## Portability

`readlink -f`, `sed -i` without a suffix, `date -d`, `mktemp -t` without a
template, `stat -c`, `grep -P`, `sort -V`, `echo -e`, `head -c` and long options
generally are GNU-isms. On macOS and BSD they are absent or behave differently.

If a script claims macOS support, either restrict yourself to POSIX flags or
walk symlinks manually rather than calling `readlink -f`.

`#!/usr/bin/env bash`, never `#!/bin/bash` — on macOS `/bin/bash` is 3.2, which
has no associative arrays, no `${var,,}`, and no `mapfile`.

`command -v cmd`, never `which`.

## Constructing commands

Never build a command as a string and eval it. Use an array:

```bash
args=(--flag "$value")
[[ -n ${opt:-} ]] && args+=(--opt "$opt")
cmd "${args[@]}"
```

Every path from outside the script is hostile. A filename can contain spaces,
newlines, quotes and leading dashes. Use `--` to end option parsing:
`rm -- "$file"`.

## Iterating files

Never parse `ls`. Never iterate `$(find ...)`.

```bash
while IFS= read -r -d '' f; do
  printf '%s\n' "$f"
done < <(find . -name '*.txt' -print0)
```

`IFS=` keeps leading and trailing whitespace, `-r` keeps backslashes, `-d ''`
pairs with `-print0`.

A plain glob is fine and safer than either, but set `shopt -s nullglob` or the
loop runs once with the literal pattern when nothing matches.

## Subshells eat variables

```bash
cmd | while read -r l; do n=$((n+1)); done   # n is 0 afterwards
while read -r l; do n=$((n+1)); done < <(cmd)   # n survives
```

Anything in a pipeline runs in a subshell. Use process substitution when the
loop must set variables the rest of the script reads.

## Output and exit

Diagnostics go to stderr: `printf '%s\n' "msg" >&2`. If stdout is a protocol
channel or the script's output is consumed by another program, a stray `echo`
corrupts it.

`printf` over `echo`: `echo` behaviour with `-n`, `-e` and leading dashes varies
between shells and builds.

Exit non-zero on failure, and make the message say which stage failed. A gate
script that collects failures and names them all at the end is more useful than
one that dies on the first.

## Checking

`bash -n script.sh` catches syntax errors without executing, and belongs in any
gate. `shellcheck` catches most of the above and is worth installing; ask before
installing it on a machine that does not have it.

Run destructive scripts with `bash -x` the first time, on a copy.
