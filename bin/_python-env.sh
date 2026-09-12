# Shared interpreter resolution, sourced by bin/orask, bin/openrouter-mcp and
# install.sh so the installer and the runtime can never disagree about which
# Python runs this project. Nothing here writes to stdout: the MCP launcher
# reserves stdout for the protocol.

ORASK_CONDA_ENV_NAME="openrouter-mcp"
ORASK_CONDA_PYTHON="3.13"
ORASK_MIN_PYTHON_MAJOR=3
ORASK_MIN_PYTHON_MINOR=10

# A version floor and nothing else. Deliberately NOT an "import mcp" check: the CLI is
# standard library only, and it has to keep working on a machine that has never installed the
# SDK. Whether mcp is importable is an install-time question, answered in install.sh.
orask_python_new_enough() {
    [[ -n ${1:-} && -x ${1:-} ]] || return 1
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($ORASK_MIN_PYTHON_MAJOR, $ORASK_MIN_PYTHON_MINOR) else 1)" >/dev/null 2>&1
}

# readlink -f is GNU; BSD and older macOS do not have it, so walk the symlinks.
orask_project_root() {
    local src="$1" dir
    while [[ -L $src ]]; do
        dir="$(cd -P "$(dirname "$src")" && pwd)"
        src="$(readlink "$src")"
        [[ $src == /* ]] || src="$dir/$src"
    done
    cd -P "$(dirname "$src")/.." && pwd
}

# Every conda-style root a machine might plausibly have, most likely first.
orask_conda_roots() {
    local user_home="${HOME:-}" root base
    if [[ -n $user_home ]]; then
        for base in miniconda3 anaconda3 miniforge3 mambaforge micromamba; do
            printf '%s\n' "$user_home/$base"
        done
    fi
    printf '%s\n' "/opt/conda"
    root="$(command -v conda 2>/dev/null || true)"
    if [[ -n $root ]]; then
        printf '%s\n' "$(dirname "$(dirname "$root")")"
    fi
    root="${CONDA_PREFIX_1:-${CONDA_PREFIX:-}}"
    if [[ -n $root ]]; then
        printf '%s\n' "$root"
    fi
    return 0
}

# Sets PYTHON to the first interpreter new enough to run this project, or empties it and
# returns 1. Priority: explicit override, the pin install.sh wrote, this project's conda env
# under any conda root, a project venv, then whatever python3 is on PATH so a fresh clone
# still runs on a machine that has never seen conda. The PATH fallback is why the version
# floor matters: a system python can easily be older than this code needs.
orask_find_python() {
    local root="$1" candidate conda_root
    local -a candidates=()

    # An explicit override is a requirement, not a search hint. A typo or old
    # interpreter must never silently select another environment.
    PYTHON="${ORASK_PYTHON:-}"
    if [[ -n $PYTHON ]]; then
        if [[ $PYTHON == /* ]] && orask_python_new_enough "$PYTHON"; then
            return 0
        fi
        printf 'orask: ORASK_PYTHON must be an absolute executable Python >= %s.%s: %s\n' \
            "$ORASK_MIN_PYTHON_MAJOR" "$ORASK_MIN_PYTHON_MINOR" "$PYTHON" >&2
        PYTHON=""
        return 1
    fi

    if [[ -f $root/.orask-python ]]; then
        candidates+=("$(<"$root/.orask-python")")
    fi
    while read -r conda_root; do
        if [[ -n $conda_root ]]; then
            candidates+=("$conda_root/envs/$ORASK_CONDA_ENV_NAME/bin/python")
        fi
    done < <(orask_conda_roots)
    candidates+=("$root/.venv/bin/python")
    candidates+=("$(command -v python3 2>/dev/null || true)")

    for candidate in "${candidates[@]}"; do
        if orask_python_new_enough "$candidate"; then
            PYTHON="$candidate"
            return 0
        fi
    done

    PYTHON=""
    return 1
}
