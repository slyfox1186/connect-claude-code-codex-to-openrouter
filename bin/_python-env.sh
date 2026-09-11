# Shared interpreter resolution, sourced by bin/orask, bin/openrouter-mcp and
# install.sh so the installer and the runtime can never disagree about which
# Python runs this project. Nothing here writes to stdout: the MCP launcher
# reserves stdout for the protocol.

ORASK_CONDA_ENV_NAME="openrouter-mcp"
ORASK_CONDA_PYTHON="3.13"

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
    local home="${HOME:-}" root base
    if [[ -n $home ]]; then
        for base in miniconda3 anaconda3 miniforge3 mambaforge micromamba; do
            printf '%s\n' "$home/$base"
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

# Sets PYTHON to the first usable interpreter, or empties it and returns 1.
# Priority: explicit override, the pin install.sh wrote, this project's conda
# env under any conda root, a project venv, then whatever python3 is on PATH
# so a fresh clone still runs on a machine that has never seen conda.
orask_find_python() {
    local root="$1" candidate conda_root
    local -a candidates=()

    PYTHON="${ORASK_PYTHON:-}"
    if [[ -n $PYTHON && -x $PYTHON ]]; then
        return 0
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
        if [[ -n $candidate && -x $candidate ]]; then
            PYTHON="$candidate"
            return 0
        fi
    done

    PYTHON=""
    return 1
}
