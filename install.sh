#!/usr/bin/env bash
# Register the OpenRouter second-opinion bridge with Claude Code and Codex.
# Safe to re-run, and re-running is how an existing install is brought up to
# date: every step converges on the current project rather than skipping when
# it finds an older registration. Changed client configs and interpreter pins
# are backed up first; existing launcher symlinks are updated in place.
# Nothing here is specific to one machine: paths come from $HOME and from where
# this file sits, so a clone installs the same way on any Linux or macOS box.
set -euo pipefail

PROJECT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PROJECT/bin/_python-env.sh"

: "${HOME:?HOME must be set}"
BIN_DIR="$HOME/.local/bin"
CLAUDE_JSON="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"
CODEX_TOML="${CODEX_HOME:-$HOME/.codex}/config.toml"
CONFIG_DIR="${ORASK_CONFIG_DIR:-$HOME/.config/openrouter}"
KEY_FILE="$CONFIG_DIR/env"
PIN_FILE="$PROJECT/.orask-python"
STAMP="$(date +%Y-%m-%d_%H%M%S).$$"
CHANGED=0
# From the shared helper, so the installer's floor and the launchers' floor cannot drift.
MIN_PYTHON="$ORASK_MIN_PYTHON_MAJOR.$ORASK_MIN_PYTHON_MINOR"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf '  %s\n' "$*" >&2; exit 1; }

# stat -c is GNU, stat -f is BSD; a machine has one or the other.
file_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null || true; }

# An interpreter is usable only if it is new enough AND can import the module
# the MCP server actually imports. `import mcp` alone passes on the 1.x SDK,
# which has no mcp.server.mcpserver and would fail at server start instead.
python_ok() {
    [[ -n ${1:-} && -x ${1:-} ]] || return 1
    "$1" - "$MIN_PYTHON" <<'PY' 2>/dev/null
import sys
want = tuple(int(p) for p in sys.argv[1].split("."))
if sys.version_info[:2] < want:
    sys.exit(1)
import mcp.server.mcpserver  # noqa: F401
PY
}

python_report() {
    "$1" -c 'import sys, importlib.metadata as m; print("Python %d.%d.%d, mcp %s" % (*sys.version_info[:3], m.version("mcp")))' 2>/dev/null \
        || echo "unknown build"
}

# Where a conda env would go. The project convention is ~/miniconda3, but any
# conda-style root already on the machine is used rather than forcing a second
# installation of the same thing.
conda_binary() {
    local root
    for root in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" \
                "$HOME/mambaforge" "/opt/conda"; do
        if [[ -x "$root/bin/conda" ]]; then
            printf '%s\n' "$root/bin/conda"
            return 0
        fi
    done
    command -v conda 2>/dev/null || true
}

# Build the env this project expects: its own conda env, Python 3.13, mcp.
bootstrap_conda() {
    local conda_bin="$1" prefix
    prefix="$(dirname "$(dirname "$conda_bin")")/envs/$ORASK_CONDA_ENV_NAME"
    if [[ -x "$prefix/bin/python" ]]; then
        say "reusing the existing env at $prefix"
    else
        say "creating $prefix (Python $ORASK_CONDA_PYTHON)"
        "$conda_bin" create -y -p "$prefix" "python=$ORASK_CONDA_PYTHON" >/dev/null \
            || return 1
    fi
    say "installing the mcp SDK into it"
    # Pinned to the 2.x line: mcp.server.mcpserver is a 2.0 API, and python_ok below checks
    # for exactly that. Unpinned, a future 3.0 would install cleanly and then fail the check
    # that just ran.
    "$prefix/bin/python" -m pip install --upgrade --quiet "mcp>=2,<3" || return 1
    PYTHON="$prefix/bin/python"
}

# Last resort for a machine with no conda at all, so the bridge still installs.
bootstrap_venv() {
    local base="$1"
    say "no conda found; creating $PROJECT/.venv instead"
    "$base" -m venv "$PROJECT/.venv" || return 1
    "$PROJECT/.venv/bin/python" -m pip install --upgrade --quiet pip "mcp>=2,<3" || return 1
    PYTHON="$PROJECT/.venv/bin/python"
}

step "Checking prerequisites"
if ! orask_find_python "$PROJECT" && [[ -n ${ORASK_PYTHON:-} ]]; then
    die "Nothing installed: fix ORASK_PYTHON or unset it to enable interpreter search."
fi
if python_ok "${PYTHON:-}"; then
    say "interpreter ready: $PYTHON ($(python_report "$PYTHON"))"
else
    if [[ -n ${ORASK_PYTHON:-} ]]; then
        die "Nothing installed: ORASK_PYTHON must have Python >= $MIN_PYTHON and mcp.server.mcpserver."
    fi
    # Anything on the machine that could at least build an env.
    BASE_PYTHON="${PYTHON:-}"
    [[ -n $BASE_PYTHON ]] || BASE_PYTHON="$(command -v python3 2>/dev/null || true)"
    CONDA_BIN="$(conda_binary)"

    say "no interpreter here has Python >= $MIN_PYTHON with the mcp SDK (>=2.0)."
    if [[ -n $CONDA_BIN ]]; then
        say "install.sh can create the conda env '$ORASK_CONDA_ENV_NAME'"
        say "(Python $ORASK_CONDA_PYTHON + mcp) under $(dirname "$(dirname "$CONDA_BIN")")/envs/."
    else
        say "install.sh can create a virtualenv at $PROJECT/.venv (Python + mcp)."
    fi

    PROCEED="${ORASK_BOOTSTRAP:-}"
    if [[ -z $PROCEED && -t 0 ]]; then
        printf '  Create it now? [Y/n] '
        read -r reply || reply=""
        if [[ -z $reply || $reply == [Yy]* ]]; then
            PROCEED=1
        fi
    fi
    if [[ $PROCEED != 1 ]]; then
        echo >&2
        if [[ -n $CONDA_BIN ]]; then
            die "Nothing installed. Build it yourself with:
    conda create -y -n $ORASK_CONDA_ENV_NAME python=$ORASK_CONDA_PYTHON
    conda run -n $ORASK_CONDA_ENV_NAME pip install mcp
  then re-run this script, or point ORASK_PYTHON at an interpreter that has mcp.
  Re-run with ORASK_BOOTSTRAP=1 to build it without asking."
        fi
        die "Nothing installed. This machine has no conda. Either install Miniconda
  (https://www.anaconda.com/docs/getting-started/miniconda/install) and re-run,
  or point ORASK_PYTHON at a Python >= $MIN_PYTHON that has the mcp SDK.
  Re-run with ORASK_BOOTSTRAP=1 to build a local virtualenv instead."
    fi

    if [[ -n $CONDA_BIN ]] && bootstrap_conda "$CONDA_BIN"; then
        :
    elif [[ -n $BASE_PYTHON ]] && bootstrap_venv "$BASE_PYTHON"; then
        :
    else
        die "Could not build an environment. Install Miniconda or python3, then re-run."
    fi

    python_ok "$PYTHON" || die "MISSING: $PYTHON is still not a Python >= $MIN_PYTHON with mcp.server.mcpserver"
    say "interpreter ready: $PYTHON ($(python_report "$PYTHON"))"
fi

# Write by atomic replacement: a pin symlink must never truncate another file,
# and an interrupted write must not leave the launchers reading a partial path.
# The .bak suffix keeps machine-specific backups under the existing gitignore.
PIN_BACKUP="$PIN_FILE.$STAMP.bak"
if ! PIN_STATE="$(PYTHONPATH="$PROJECT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m orask.install_config pin "$PIN_FILE" "$PYTHON" "$PIN_BACKUP")"; then
    die "FAILED: could not safely pin the interpreter; no registration attempted."
fi
say "pinned the interpreter for the launchers ($PIN_FILE)"
if [[ $PIN_STATE == updated ]]; then
    say "backed up the previous pin -> $PIN_BACKUP"
fi

# A tarball or zip download loses the executable bit that git tracks.
chmod +x "$PROJECT/bin/orask" "$PROJECT/bin/openrouter-mcp" 2>/dev/null || true

mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR" 2>/dev/null || true
if [[ -f $KEY_FILE ]]; then
    say "API key file present ($KEY_FILE)"
    mode="$(file_mode "$KEY_FILE")"
    if [[ -n $mode && $mode != 600 ]]; then
        chmod 600 "$KEY_FILE"
        say "tightened key file permissions from $mode to 600"
    fi
else
    say "WARNING: no key file at $KEY_FILE"
    say "create it with:  printf 'OPENROUTER_API_KEY=sk-or-...\\n' > $KEY_FILE && chmod 600 $KEY_FILE"
fi

step "Installing launchers into $BIN_DIR"
mkdir -p "$BIN_DIR"
for tool in orask openrouter-mcp; do
    if [[ -e $BIN_DIR/$tool && ! -L $BIN_DIR/$tool ]]; then
        die "Refusing to replace $BIN_DIR/$tool: move the existing file or directory first."
    fi
done
for tool in orask openrouter-mcp; do
    ln -sfn "$PROJECT/bin/$tool" "$BIN_DIR/$tool"
    say "$BIN_DIR/$tool -> $PROJECT/bin/$tool"
done
case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *)  say "WARNING: $BIN_DIR is not on PATH, so 'orask' will not be found."
        say "add this to ~/.bashrc or ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# Built with json.dumps so a project path containing a quote, a backslash or a
# space cannot produce a config file that the agent then fails to parse.
SERVER_JSON="$("$PYTHON" - "$PROJECT/bin/openrouter-mcp" "$PYTHON" <<'PY'
import json, sys
print(json.dumps({"type": "stdio", "command": sys.argv[1], "args": [],
                  "env": {"ORASK_PYTHON": sys.argv[2]}, "timeout": 600000}))
PY
)"

step "Registering the MCP server with Claude Code"
if ! command -v claude >/dev/null 2>&1; then
    say "SKIPPED: the 'claude' CLI is not on PATH"
else
    # An entry left by an older install points at whatever was true then: a
    # different project directory, a different interpreter, no env at all. It
    # has to be compared against what this run would write, not just counted.
    if ! CLAUDE_STATE="$(PYTHONPATH="$PROJECT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" - "$CLAUDE_JSON" "$SERVER_JSON" <<'PY'
import json, sys
from pathlib import Path
from orask.install_config import ConfigLimitError, read_config
try:
    contents = read_config(Path(sys.argv[1]))
    config = json.loads(contents.decode("utf-8")) if contents is not None else {}
    if not isinstance(config, dict):
        raise ValueError("Claude config must be an object")
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object")
    current = servers.get("openrouter")
    if current is not None and not isinstance(current, dict):
        raise ValueError("openrouter must be an object")
except ConfigLimitError as exc:
    print(f"Claude config update refused: {exc}.", file=sys.stderr)
    raise SystemExit(1) from None
except (OSError, ValueError):
    print("Claude config is unreadable or invalid; no registration attempted.", file=sys.stderr)
    raise SystemExit(1) from None
if not current:
    print("missing")
    raise SystemExit
want = json.loads(sys.argv[2])
# Only the keys this installer manages are compared. Anything the agent added
# on its own is not a reason to rewrite the entry.
drift = sorted(key for key, value in want.items() if current.get(key) != value)
print("current" if not drift else "stale:" + ",".join(drift))
PY
)"; then
        die "FAILED: could not read $CLAUDE_JSON; repair its access or JSON before retrying."
    fi
    case "$CLAUDE_STATE" in
        current)
            say "already registered and up to date" ;;
        *)
            if [[ -e $CLAUDE_JSON || -L $CLAUDE_JSON ]]; then
                PYTHONPATH="$PROJECT/src${PYTHONPATH:+:$PYTHONPATH}" \
                    "$PYTHON" -m orask.install_config backup "$CLAUDE_JSON" \
                    "$CLAUDE_JSON.bak.$STAMP" \
                    || die "FAILED: could not back up $CLAUDE_JSON; no registration attempted."
                say "backed up $CLAUDE_JSON -> $CLAUDE_JSON.bak.$STAMP"
            fi
            if [[ $CLAUDE_STATE == stale:* ]]; then
                say "registered, but out of date (${CLAUDE_STATE#stale:}); replacing it"
                claude mcp remove openrouter -s user >/dev/null \
                    || die "FAILED: Claude removal failed; no replacement attempted. Backup retained."
            fi
            # Written through the Claude CLI rather than by editing ~/.claude.json
            # directly, because a running session owns that file.
            if claude mcp add-json openrouter "$SERVER_JSON" --scope user >/dev/null; then
                CHANGED=1
                if [[ $CLAUDE_STATE == stale:* ]]; then
                    say "updated the user-scope MCP server 'openrouter'"
                else
                    say "registered as user-scope MCP server 'openrouter'"
                fi
            else
                say "FAILED: 'claude mcp add-json' did not accept the server; register it by hand with:"
                say "  claude mcp add-json openrouter '$SERVER_JSON' --scope user"
                die "Installation incomplete. Any existing Claude config backup was retained."
            fi ;;
    esac
fi

step "Registering the MCP server with Codex"
if [[ ! -e $CODEX_TOML && ! -L $CODEX_TOML ]] && ! command -v codex >/dev/null 2>&1; then
    say "SKIPPED: Codex is not installed ($CODEX_TOML does not exist)"
else
    # The editor reads and validates before making a private backup and replacing
    # the file atomically. Read errors must never be interpreted as an empty config.
    if ! CODEX_STATE="$(PYTHONPATH="$PROJECT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" -m orask.install_config "$CODEX_TOML" "$PROJECT/bin/openrouter-mcp" \
        "$PYTHON" "$CODEX_TOML.bak.$STAMP")"; then
        die "FAILED: could not safely update $CODEX_TOML. Installation incomplete."
    fi
    case "$CODEX_STATE" in
        unchanged)
            say "already registered and up to date" ;;
        added)
            CHANGED=1
            say "appended [mcp_servers.openrouter] to $CODEX_TOML" ;;
        updated)
            CHANGED=1
            say "rewrote the existing [mcp_servers.openrouter] block"
            say "backed up -> $CODEX_TOML.bak.$STAMP" ;;
        *)
            die "FAILED: unexpected Codex editor result. Installation incomplete." ;;
    esac
fi

step "Verifying"
"$PROJECT/bin/orask" doctor || true

printf '\n== Done\n\n'
if [[ $CHANGED == 1 ]]; then
    say "A registration changed, so restart Claude Code and Codex to pick it up"
    say "(an already running session keeps the tool list it started with)."
else
    say "Registrations were already current. The launchers are symlinks into"
    say "$PROJECT, so project code updates are live without re-registering;"
    say "restart running agents after updating server code or instructions."
fi

cat <<'DONE'

Then just ask, in either agent:
  "ask Kimi what it thinks about this"
  "get Grok's take on this approach"
  "ask them all whether this plan is sound"
  "use the coding LLMs to review this file"

Or from any shell:
  orask "why would this hang on shutdown?" -m glm -f server.py
  orask panel "is this migration plan safe?" -c "$(cat PLAN.md)"
  orask panel "is this design sound?" -C coding
  orask guide python            # local best-practice cheat sheets
  orask models --search grok
DONE
