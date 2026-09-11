#!/usr/bin/env bash
# Register the OpenRouter second-opinion bridge with Claude Code and Codex.
# Safe to re-run: every step is idempotent and every edited file is backed up.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Interpreter, in priority order: an explicit override, the conda env this
# project is built against, then whatever python3 is on PATH.
PYTHON="${ORASK_PYTHON:-}"
if [[ -z $PYTHON || ! -x $PYTHON ]]; then
    for candidate in \
        "$HOME/miniconda3/envs/openrouter-mcp/bin/python" \
        "$HOME/anaconda3/envs/openrouter-mcp/bin/python" \
        "$(command -v python3 2>/dev/null || true)"
    do
        if [[ -n $candidate && -x $candidate ]]; then
            PYTHON="$candidate"
            break
        fi
    done
fi
BIN_DIR="$HOME/.local/bin"
CLAUDE_JSON="$HOME/.claude.json"
CODEX_TOML="$HOME/.codex/config.toml"
KEY_FILE="$HOME/.config/openrouter/env"
STAMP="$(date +%Y-%m-%d_%H%M%S)"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

step "Checking prerequisites"
[[ -n $PYTHON && -x $PYTHON ]] || {
    echo "  MISSING: no usable Python interpreter" >&2
    echo "  Create the env with:" >&2
    echo "    conda create -y -n openrouter-mcp python=3.13" >&2
    echo "    conda run -n openrouter-mcp pip install mcp" >&2
    echo "  Or point ORASK_PYTHON at an interpreter that has the mcp package." >&2
    exit 1
}
"$PYTHON" -c "import mcp" 2>/dev/null || {
    echo "  MISSING: the mcp package in $PYTHON" >&2
    echo "  Install it with: $PYTHON -m pip install mcp" >&2
    exit 1
}
say "interpreter and mcp package present ($PYTHON)"

if [[ -f $KEY_FILE ]]; then
    say "API key file present ($KEY_FILE)"
    mode="$(stat -c '%a' "$KEY_FILE")"
    if [[ $mode != 600 ]]; then
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
    ln -sfn "$PROJECT/bin/$tool" "$BIN_DIR/$tool"
    say "$BIN_DIR/$tool -> $PROJECT/bin/$tool"
done

step "Registering the MCP server with Claude Code"
if ! command -v claude >/dev/null 2>&1; then
    say "SKIPPED: the 'claude' CLI is not on PATH"
elif "$PYTHON" - "$CLAUDE_JSON" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        sys.exit(0 if "openrouter" in (json.load(fh).get("mcpServers") or {}) else 1)
except (OSError, ValueError):
    sys.exit(1)
PY
then
    say "already registered (remove with: claude mcp remove openrouter -s user)"
else
    cp -p "$CLAUDE_JSON" "$HOME/.claude.json.bak.$STAMP" 2>/dev/null \
        && say "backed up ~/.claude.json -> ~/.claude.json.bak.$STAMP"
    # Written through the Claude CLI rather than by editing ~/.claude.json
    # directly, because a running session owns that file.
    claude mcp add-json openrouter "$(cat <<JSON
{
  "type": "stdio",
  "command": "$PROJECT/bin/openrouter-mcp",
  "args": [],
  "env": {},
  "timeout": 600000
}
JSON
)" --scope user >/dev/null
    say "registered as user-scope MCP server 'openrouter'"
fi

step "Registering the MCP server with Codex"
if [[ ! -f $CODEX_TOML ]]; then
    say "SKIPPED: $CODEX_TOML does not exist"
elif grep -q '^\[mcp_servers\.openrouter\]' "$CODEX_TOML"; then
    say "already registered (delete the [mcp_servers.openrouter] block to undo)"
else
    cp -p "$CODEX_TOML" "$CODEX_TOML.bak.$STAMP"
    say "backed up -> $CODEX_TOML.bak.$STAMP"
    cat >> "$CODEX_TOML" <<TOML

[mcp_servers.openrouter]
command = "$PROJECT/bin/openrouter-mcp"
args = []
startup_timeout_sec = 30
# Reasoning models on a large context can take minutes; a panel runs in parallel.
tool_timeout_sec = 600
enabled_tools = ["ask_llm", "ask_panel", "list_llm_models", "llm_model_info", "openrouter_usage"]
TOML
    say "appended [mcp_servers.openrouter]"
fi

step "Verifying"
"$PROJECT/bin/orask" doctor || true

cat <<'DONE'

== Done

Restart Claude Code and Codex to pick up the new MCP server (an already
running session keeps the tool list it started with).

Then just ask, in either agent:
  "ask Kimi what it thinks about this"
  "get GLM's take on this approach"
  "ask both Kimi and GLM whether this plan is sound"

Or from any shell:
  orask "why would this hang on shutdown?" -m glm -f server.py
  orask panel "is this migration plan safe?" -c "$(cat PLAN.md)"
  orask models --search grok
DONE
