#!/usr/bin/env bash
#
# minitrace scenario runner — execute a scenario against a framework + model
#
# Usage:
#   ./run-scenario.sh --framework claude-code --model qwen3.5:cloud --scenario S1-file-analysis
#   ./run-scenario.sh --framework codex --model qwen3.5:cloud --scenario S1-file-analysis --proxy
#
# Environment:
#   OLLAMA_HOST          — Ollama endpoint (default: http://localhost:11434)
#   MINITRACE_PROXY_HOST — Proxy endpoint (default: http://localhost:11435)
#   OLLAMA_PORT          — Ollama port (default: 11434)
#   MINITRACE_PROXY_PORT — Proxy port (default: 11435)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPEC_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE_BASE="$SCRIPT_DIR/workspaces"
DEFINITIONS_DIR="$SCRIPT_DIR/definitions"
OUTPUT_BASE="$SPEC_DIR/data/scenario-runs"

# Defaults
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
MINITRACE_PROXY_PORT="${MINITRACE_PROXY_PORT:-11435}"
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:$OLLAMA_PORT}"
MINITRACE_PROXY_HOST="${MINITRACE_PROXY_HOST:-http://localhost:$MINITRACE_PROXY_PORT}"

# Parse arguments
FRAMEWORK=""
MODEL=""
SCENARIO=""
USE_PROXY=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --framework) FRAMEWORK="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --scenario) SCENARIO="$2"; shift 2 ;;
        --proxy) USE_PROXY=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help)
            echo "Usage: $0 --framework <name> --model <model> --scenario <id>"
            echo ""
            echo "Frameworks: claude-code, codex, goose, pi, opencode, droid, gemini, vibe, openclaw"
            echo "Scenarios:  S1-file-analysis, S2-search-synthesize, S3-edit-existing,"
            echo "            S4-multi-step-verify, S5-ambiguous-instruction"
            echo ""
            echo "Options:"
            echo "  --proxy     Route through mitmproxy (Layer 3 capture)"
            echo "  --dry-run   Show commands without executing"
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# Validate
if [[ -z "$FRAMEWORK" || -z "$MODEL" || -z "$SCENARIO" ]]; then
    echo "ERROR: --framework, --model, and --scenario are required"
    exit 1
fi

SCENARIO_FILE="$DEFINITIONS_DIR/${SCENARIO}.scenario.yaml"
if [[ ! -f "$SCENARIO_FILE" ]]; then
    echo "ERROR: Scenario file not found: $SCENARIO_FILE"
    exit 1
fi

# Extract workspace name and prompt from scenario YAML (simple parsing)
WORKSPACE=$(grep '^workspace:' "$SCENARIO_FILE" | awk '{print $2}')
PROMPT=$(sed -n '/^prompt: |/,/^[a-z]/{ /^prompt: |/d; /^[a-z]/d; p; }' "$SCENARIO_FILE" | sed 's/^  //')

if [[ -z "$WORKSPACE" || -z "$PROMPT" ]]; then
    echo "ERROR: Could not parse workspace or prompt from $SCENARIO_FILE"
    exit 1
fi

WORKSPACE_DIR="$WORKSPACE_BASE/$WORKSPACE"
if [[ ! -d "$WORKSPACE_DIR" ]]; then
    echo "ERROR: Workspace not found: $WORKSPACE_DIR"
    exit 1
fi

# Determine Ollama host (proxy or direct)
if $USE_PROXY; then
    EFFECTIVE_HOST="$MINITRACE_PROXY_HOST"
    echo "Proxy mode: routing through $EFFECTIVE_HOST"
else
    EFFECTIVE_HOST="$OLLAMA_HOST"
fi

# Create run output directory
RUN_ID="$(date +%Y%m%dT%H%M%S)-${FRAMEWORK}-${SCENARIO}"
RUN_DIR="$OUTPUT_BASE/$RUN_ID"
mkdir -p "$RUN_DIR"

# Prepare workspace copy (reset to known state)
WORK_DIR="$RUN_DIR/workspace"
cp -r "$WORKSPACE_DIR" "$WORK_DIR"

# Initialize as git repo if not already
if [[ ! -d "$WORK_DIR/.git" ]]; then
    (cd "$WORK_DIR" && git init -q && git add -A && git commit -q -m "initial state")
fi

# Ensure /tmp/claude exists for scenario outputs
mkdir -p /tmp/claude

echo "=== minitrace scenario run ==="
echo "Framework:  $FRAMEWORK"
echo "Model:      $MODEL"
echo "Scenario:   $SCENARIO"
echo "Workspace:  $WORK_DIR"
echo "Output:     $RUN_DIR"
echo "Ollama:     $EFFECTIVE_HOST"
echo ""

# Save run metadata
cat > "$RUN_DIR/run-metadata.json" << METAEOF
{
  "run_id": "$RUN_ID",
  "framework": "$FRAMEWORK",
  "model": "$MODEL",
  "scenario_id": "$SCENARIO",
  "workspace": "$WORKSPACE",
  "ollama_host": "$EFFECTIVE_HOST",
  "proxy_enabled": $USE_PROXY,
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "$(hostname)"
}
METAEOF

if $DRY_RUN; then
    echo "[DRY RUN] Would execute $FRAMEWORK with prompt:"
    echo "$PROMPT"
    exit 0
fi

# Execute framework-specific command
case $FRAMEWORK in
    claude-code)
        OLLAMA_HOST="$EFFECTIVE_HOST" claude -p "$PROMPT" \
            --model "ollama:$MODEL" \
            --dangerously-skip-permissions \
            --output-format json \
            -C "$WORK_DIR" \
            > "$RUN_DIR/output.json" 2> "$RUN_DIR/stderr.log"
        ;;
    codex)
        OLLAMA_HOST="$EFFECTIVE_HOST" codex exec "$PROMPT" \
            --json --oss \
            --local-provider ollama \
            -m "$MODEL" \
            --dangerously-bypass-approvals-and-sandbox \
            -C "$WORK_DIR" \
            > "$RUN_DIR/output.jsonl" 2> "$RUN_DIR/stderr.log"
        ;;
    goose)
        OLLAMA_HOST="$EFFECTIVE_HOST" goose run \
            --text "$PROMPT" \
            --provider ollama \
            --model "$MODEL" \
            > "$RUN_DIR/output.log" 2> "$RUN_DIR/stderr.log"
        # Native session data in ~/.local/share/goose/sessions/sessions.db
        ;;
    pi)
        OLLAMA_HOST="$EFFECTIVE_HOST" pi --print "$PROMPT" \
            > "$RUN_DIR/output.log" 2> "$RUN_DIR/stderr.log"
        # Native session data in ~/.pi/agent/sessions/
        ;;
    droid)
        OLLAMA_HOST="$EFFECTIVE_HOST" droid exec "$PROMPT" \
            > "$RUN_DIR/output.log" 2> "$RUN_DIR/stderr.log"
        # Native session data in ~/.factory/sessions/
        ;;
    vibe)
        OLLAMA_HOST="$EFFECTIVE_HOST" vibe "$PROMPT" \
            > "$RUN_DIR/output.log" 2> "$RUN_DIR/stderr.log"
        ;;
    opencode)
        OLLAMA_HOST="$EFFECTIVE_HOST" opencode "$PROMPT" \
            > "$RUN_DIR/output.log" 2> "$RUN_DIR/stderr.log"
        ;;
    gemini)
        gemini "$PROMPT" \
            > "$RUN_DIR/output.log" 2> "$RUN_DIR/stderr.log"
        ;;
    openclaw)
        # OpenClaw runs a persistent gateway server -- use container isolation
        echo "OpenClaw requires containerized execution."
        echo "See: https://github.com/fukami/minitrace/tree/main/containers/openclaw"
        exit 1
        ;;
    *)
        echo "ERROR: Unknown framework: $FRAMEWORK"
        echo "Supported: claude-code, codex, goose, pi, opencode, droid, gemini, vibe, openclaw"
        exit 1
        ;;
esac

EXIT_CODE=$?
ENDED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Update metadata with completion
python3 -c "
import json
with open('$RUN_DIR/run-metadata.json') as f:
    meta = json.load(f)
meta['ended_at'] = '$ENDED_AT'
meta['exit_code'] = $EXIT_CODE
with open('$RUN_DIR/run-metadata.json', 'w') as f:
    json.dump(meta, f, indent=2)
"

# Capture workspace diff (for S3/S4 ground truth comparison)
(cd "$WORK_DIR" && git diff) > "$RUN_DIR/workspace-diff.patch" 2>/dev/null || true
(cd "$WORK_DIR" && git diff --stat) > "$RUN_DIR/workspace-diff-stat.txt" 2>/dev/null || true

echo ""
echo "=== Run complete ==="
echo "Exit code:  $EXIT_CODE"
echo "Output dir: $RUN_DIR"
echo ""
echo "Next steps:"
echo "  1. Check output: cat $RUN_DIR/output.*"
echo "  2. Check workspace changes: cat $RUN_DIR/workspace-diff.patch"
echo "  3. Run adapter: python3 adapters/<framework>/minitrace-<framework>-adapter.py ..."
echo "  4. Validate: python3 adapters/validate-minitrace.py <output>.minitrace.json"
