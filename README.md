# minitrace

A session trace format for capturing human-AI coding interactions across frameworks.

minitrace converts native session data from 9 coding agent frameworks into a common JSON format,
enabling cross-framework behavioral comparison, failure pattern analysis, and reproducible experiments.

## Supported Frameworks

| Framework | Version | Storage Format | Adapter |
|-----------|---------|---------------|---------|
| Claude Code | 2.1.76+ | JSONL | `adapters/claude-code/` |
| Codex | 0.114.0 | JSONL | `adapters/codex/` |
| Goose (Block) | 3.27.0 | SQLite | `adapters/goose/` |
| Pi | 0.58.1 | JSONL | `adapters/pi/` |
| OpenCode | 1.2.20 | SQLite | `adapters/opencode/` |
| Droid (Factory) | 0.74.0 | JSONL | `adapters/droid/` |
| Gemini CLI | 0.33.1 | JSON | `adapters/gemini/` |
| Vibe (Mistral) | 2.4.2 | meta.json + JSONL | `adapters/vibe/` |
| OpenClaw | 2026.3.13 | JSONL | `adapters/openclaw/` |

## Quick Start

```bash
# Clone (Python 3.9+, no external dependencies)
git clone https://github.com/fukami/minitrace.git
cd minitrace

# Convert Claude Code sessions
python3 adapters/claude-code/minitrace-claude-adapter.py \
    --source-dir ~/.claude/projects/ \
    --output-dir ./output/

# Convert Goose sessions
python3 adapters/goose/minitrace-goose-adapter.py \
    --source-db ~/.local/share/goose/sessions/sessions.db \
    --output-dir ./output/

# Validate output
python3 adapters/validate-minitrace.py --dir ./output/ --recursive
```

## Querying with DuckDB

Query minitrace JSON files directly with SQL. No loading step, no database setup.

```bash
# Framework comparison (aggregate stats)
duckdb -f queries/framework-summary.sql

# One-liner: list all sessions
duckdb -c "
SELECT id, environment->>'agent_framework' as framework,
       CAST(metrics->>'tool_call_count' AS INT) as tools
FROM read_json('output/**/*.minitrace.json',
  columns={id: 'VARCHAR', environment: 'JSON', metrics: 'JSON'},
  ignore_errors=true)
ORDER BY tools DESC
"
```

See [`queries/`](queries/) for ready-made SQL files covering framework summaries,
tool operation breakdowns, read-ratio analysis, timing, and annotations.

## Format Discovery

Each adapter includes a `--discover` mode that inspects the native format without converting:

```bash
python3 adapters/codex/minitrace-codex-adapter.py --discover --source-dir ~/.codex/
python3 adapters/pi/minitrace-pi-adapter.py --discover
python3 adapters/gemini/minitrace-gemini-adapter.py --discover
```

## Format Stability Testing

The `test-format-stability.py` tool detects when a framework updates its native format:

```bash
# Test all adapters against reference schemas
python3 adapters/test-format-stability.py --all

# Extract schema from a session file
python3 adapters/test-format-stability.py --extract session.jsonl

# Check a new file against a reference schema
python3 adapters/test-format-stability.py --check new.jsonl --reference ref.schema.json
```

## Specification

The minitrace v0.1.0 specification is at [`spec/minitrace-spec-v0.1.0.md`](spec/minitrace-spec-v0.1.0.md). It defines:

- **Session schema:** Full session structure with turns, tool calls, metrics
- **Two profiles:** Controlled (experiments) and Organic (natural sessions)
- **Failure taxonomy:** Classification system for AI behavioral failures
- **Annotation layer:** Structured observations, patterns, and reviews
- **Adapter requirements:** What adapters must extract and how

## Examples

The `examples/` directory contains 44 pre-converted traces from all 9 frameworks,
produced from containerized scenario runs. Use them to explore the format or test queries.

```bash
# Validate the examples
python3 adapters/validate-minitrace.py --dir ./examples/ --recursive

# Query the examples with DuckDB
duckdb -c "SELECT environment->>'agent_framework' as fw, count(*) as n
FROM read_json('examples/**/*.minitrace.json',
  columns={environment: 'JSON'}, ignore_errors=true)
GROUP BY fw ORDER BY n DESC"
```

## Output Structure

Adapters write converted traces to a directory with this structure:

```
<output-dir>/
├── manifest.json                    # Root manifest
└── active/
    └── 2026-03/
        ├── manifest.json            # Period manifest
        ├── <session-id>.minitrace.json
        └── ...
```

Each `.minitrace.json` file contains a complete session:

```json
{
  "id": "session-uuid",
  "schema_version": "minitrace-v0.1.0",
  "profile": "controlled",
  "environment": {
    "model": "qwen3.5:cloud",
    "agent_framework": "claude-code",
    "agent_version": "2.1.76"
  },
  "timing": {
    "duration_seconds": 45.2,
    "started_at": "2026-03-15T14:30:00Z"
  },
  "turns": [...],
  "tool_calls": [...],
  "metrics": {
    "turn_count": 12,
    "tool_call_count": 8,
    "read_ratio": 0.375
  },
  "annotations": [...]
}
```

## Quality Tiers

Sessions are assigned a quality tier during conversion:

| Tier | Criteria |
|------|----------|
| **A** | Full conversation + tool I/O, >10 tool calls, >5 turns |
| **B** | Conversation but limited tool I/O or few tool calls |
| **C** | No conversation (metadata only) |
| **D** | Empty/trivial |

## Architecture

```
adapters/
├── minitrace_common.py          # Shared utilities (all adapters import this)
├── validate-minitrace.py        # Schema validator
├── test-format-stability.py     # Format change detector
├── claude-code/                 # One directory per framework
├── codex/
├── goose/
├── pi/
├── opencode/
├── droid/
├── gemini/
├── vibe/
└── openclaw/
```

## Documentation

- [`spec/minitrace-spec-v0.1.0.md`](spec/minitrace-spec-v0.1.0.md) -- Format specification (schemas, types, constraints)
- [`docs/adapter-guide.md`](docs/adapter-guide.md) -- How to write an adapter (conversion process, tool mapping, quality tiers)
- [`docs/experiment-guide.md`](docs/experiment-guide.md) -- How to run reproducible experiments
- [`docs/format-discovery.md`](docs/format-discovery.md) -- Native format documentation for all 9 frameworks
- [`docs/related-work.md`](docs/related-work.md) -- Relationship to MAST, ToolEmu, OWASP
- [`docs/security-review.md`](docs/security-review.md) -- Security review checklist
- [`docs/cross-framework-format-gaps.md`](docs/cross-framework-format-gaps.md) -- Known gaps and future candidates

## Security Considerations

These adapters read untrusted data from framework session stores. See
`docs/security-review.md` for the threat model and review checklist.

Key properties:
- **Read-only:** Adapters only read native session data, never modify it
- **No code execution:** Parsed content is stored as strings, never evaluated
- **Content truncation:** Large outputs are truncated with hash references
- **PII detection:** Unsanitized user paths are flagged

## License

[MIT](LICENSE)
