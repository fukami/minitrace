# minitrace

A session trace format for capturing human-AI interactions across frameworks.

minitrace converts native session data from 11 coding agent and web AI frameworks into a common JSON format, enabling cross-framework behavioral comparison, failure pattern analysis, and reproducible experiments.

## Supported Frameworks

| Framework | Type | Tested Version | Storage Format | Adapter |
|-----------|------|----------------|---------------|---------|
| Claude Code | Agent | 2.1.76+ | JSONL | `adapters/claude-code/` |
| Codex | Agent | 0.114.0 | JSONL | `adapters/codex/` |
| Goose (Block) | Agent | 3.27.0 | SQLite | `adapters/goose/` |
| Pi | Agent | 0.58.1 | JSONL | `adapters/pi/` |
| OpenCode | Agent | 1.2.20 | SQLite | `adapters/opencode/` |
| Droid (Factory) | Agent | 0.74.0 | JSONL | `adapters/droid/` |
| Gemini CLI | Agent | 0.33.1 | JSON | `adapters/gemini/` |
| Vibe (Mistral) | Agent | 2.4.2 | meta.json + JSONL | `adapters/vibe/` |
| OpenClaw | Agent | 2026.3.13 | JSONL | `adapters/openclaw/` |
| ChatGPT | Web | Data export | ZIP (conversations.json) | `adapters/chatgpt/` |
| claude.ai | Web | Data export | ZIP (conversations.json) | `adapters/claude-ai/` |

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

# Convert ChatGPT data export
python3 adapters/chatgpt/minitrace-chatgpt-adapter.py \
    --source data-export.zip \
    --output-dir ./output/

# Convert claude.ai data export
python3 adapters/claude-ai/minitrace-claude-ai-adapter.py \
    --source data-export.zip \
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
       environment->>'platform_type' as type,
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

Each agent adapter includes a `--discover` mode that inspects the native format without converting:

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

The current specification is [`spec/minitrace-spec-v0.2.0.md`](spec/minitrace-spec-v0.2.0.md). It defines:

- **Session schema:** Full session structure with turns, tool calls, metrics
- **Two profiles:** Controlled (experiments) and Organic (natural sessions)
- **Input provenance:** `input_channel` and `content_origin` fields for prompt injection path analysis
- **Failure taxonomy:** Classification system for AI behavioral failures
- **Annotation layer:** Structured observations, patterns, and reviews
- **Adapter requirements:** What adapters must extract and how

v0.2.0 is backward-compatible with v0.1.0. The previous specification is at [`spec/minitrace-spec-v0.1.0.md`](spec/minitrace-spec-v0.1.0.md).

## Examples

The `examples/` directory contains reference traces organized by spec version:

- `examples/v0.1.0/` -- 44 traces from 9 agent frameworks (containerized scenario runs)
- `examples/v0.2.0/` -- traces demonstrating v0.2.0 fields (input provenance, quality tiers)

```bash
# Validate v0.2.0 examples
python3 adapters/validate-minitrace.py --dir ./examples/v0.2.0/ --recursive

# Validate v0.1.0 examples (backward-compatible validator)
python3 adapters/validate-minitrace.py --dir ./examples/v0.1.0/ --recursive

# Query examples with DuckDB
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
  "schema_version": "minitrace-v0.2.0",
  "profile": "controlled",
  "quality": "A",
  "environment": {
    "model": "claude-sonnet-4-5-20250514",
    "agent_framework": "claude-code",
    "agent_version": "2.1.76",
    "platform_type": "agent"
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

Web sessions without tool calls are capped at tier B regardless of conversational depth.

## Architecture

```
adapters/
├── minitrace_common.py          # Shared utilities (all adapters import this)
├── validate-minitrace.py        # Schema validator
├── test-format-stability.py     # Format change detector
├── claude-code/                 # Agent frameworks
├── codex/
├── goose/
├── pi/
├── opencode/
├── droid/
├── gemini/
├── vibe/
├── openclaw/
├── chatgpt/                     # Web platforms
└── claude-ai/
```

## Documentation

- [`spec/minitrace-spec-v0.2.0.md`](spec/minitrace-spec-v0.2.0.md) -- Format specification (schemas, types, constraints)
- [`spec/minitrace-spec-v0.1.0.md`](spec/minitrace-spec-v0.1.0.md) -- Previous specification
- [`docs/adapter-guide.md`](docs/adapter-guide.md) -- How to write an adapter (conversion process, tool mapping, quality tiers)
- [`docs/experiment-guide.md`](docs/experiment-guide.md) -- How to run reproducible experiments
- [`docs/format-discovery.md`](docs/format-discovery.md) -- Native format documentation for supported frameworks
- [`docs/related-work.md`](docs/related-work.md) -- Relationship to MAST, ToolEmu, OWASP
- [`docs/threat-model.md`](docs/threat-model.md) -- Threat model and security review
- [`docs/cross-framework-format-gaps.md`](docs/cross-framework-format-gaps.md) -- Known gaps and future candidates

## Security Considerations

These adapters read untrusted data from framework session stores. See
`docs/threat-model.md` for the threat model and review checklist.

Key properties:
- **Read-only:** Adapters only read native session data, never modify it
- **No code execution:** Parsed content is stored as strings, never evaluated
- **Content truncation:** Large outputs are truncated with hash references
- **PII detection:** Unsanitized user paths are flagged

## License

[MIT](LICENSE)
