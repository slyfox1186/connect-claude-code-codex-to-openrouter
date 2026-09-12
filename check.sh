#!/usr/bin/env bash
# The gate. One command, one exit code: lint, types, shell syntax, offline tests.
#
# The tests run against a scratch config/state/cache so a run can never read the real API
# key, never append to the real call log, and never reach the network.
set -uo pipefail

PROJECT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT/bin/_python-env.sh"

if ! orask_find_python "$PROJECT"; then
    echo "check: no usable Python interpreter found; run ./install.sh" >&2
    exit 1
fi

FAILED=()
run() {
    local label="$1"; shift
    printf '\n== %s\n' "$label"
    if "$@"; then
        printf '   ok\n'
    else
        printf '   FAILED\n'
        FAILED+=("$label")
    fi
}

run "ruff (lint)"  "$PYTHON" -m ruff check "$PROJECT/src" "$PROJECT/tests"
run "mypy (types)" "$PYTHON" -m mypy --config-file "$PROJECT/pyproject.toml"
run "bash -n (shell syntax)" bash -c '
    for f in "$1"/install.sh "$1"/check.sh "$1"/bin/orask "$1"/bin/openrouter-mcp \
             "$1"/bin/_python-env.sh; do
        bash -n "$f" || exit 1
    done' _ "$PROJECT"

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
run "offline tests" env \
    ORASK_CONFIG_DIR="$SCRATCH/config" \
    ORASK_STATE_DIR="$SCRATCH/state" \
    ORASK_CACHE_DIR="$SCRATCH/cache" \
    OPENROUTER_API_KEY="" \
    "$PYTHON" "$PROJECT/tests/test_core.py"

printf '\n'
if ((${#FAILED[@]})); then
    printf 'check: %d stage(s) failed: %s\n' "${#FAILED[@]}" "${FAILED[*]}" >&2
    exit 1
fi
echo "check: all stages passed"
