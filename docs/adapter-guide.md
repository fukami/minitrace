# Adapter Guide

How to write a minitrace adapter for a new framework.

A minitrace adapter reads a framework's native session output and produces `.minitrace.json` files. Each framework has its own adapter. This guide covers what your adapter needs to do, how to handle organic session conversion, and how to classify tool operations.

## Requirements

Your adapter **must**:

1. Identify sessions by the framework's native session ID.
2. Extract turns with role, content, and timestamps (where available).
3. Extract tool calls with name, arguments, and success/failure status (where available).
4. Map framework-specific tool names to `operation_type` values.
5. Populate `provenance` with the source format identifier and adapter version.
6. Set `schema_version` to the minitrace version it targets.
7. Produce valid minitrace JSON conforming to the minimum required fields for the session profile.

Your adapter **should**:

1. Merge multiple data sources for the same session (e.g., transcript + database + telemetry).
2. Detect and convert ghost sessions (sessions where the model produced no tool calls).
3. Compute derived metrics (turn_count, tool_call_count, read_ratio, etc.).
4. Normalize file paths relative to project root.
5. Normalize timestamps to ISO 8601 UTC.
6. Deduplicate tool calls when sources produce duplicates (keep the last occurrence for a given tool call ID).
7. Populate `Turn.source` when the framework distinguishes human input from injected content.

Your adapter **may**:

1. Populate `framework_metadata` on tool calls with framework-specific fields.
2. Populate `operational_context.framework_config` with framework state.
3. Apply quality tier classification (see Quality tiers below).
4. Auto-classify sessions.

## Source format identifiers

Source format identifiers are freeform strings chosen by the adapter author. The convention is `<framework>-<format>-v<version>`. Examples: `claude-code-jsonl-v2`, `vibe-session-v1`, `codex-sqlite-v5`. See the spec's Appendix A for known frameworks and their native formats.

## Conversion process

For each source session:

1. **Identify the session** by the framework's native session identifier.
2. **Determine the best source** for each field. Frameworks may store data across multiple locations (transcript files, databases, telemetry logs). Document your per-field source precedence.
3. **Merge sources** where the same session appears in multiple locations.
4. **Map tool names to operation_type** using the framework's tool vocabulary (see the mapping table below).
5. **Compute derived fields**: metrics, read_ratio, active_duration_seconds.
6. **Set defaults for organic profile**:
   - `classification: "internal"`
   - `flags.for_research: false`
   - `outcome: null`
   - `scenario_id: null`
   - `condition: null`

## Data reduction

Organic sessions can be large. Keep minitrace records practical to work with.

**Tool call output.** Truncate at 10 KB in the minitrace record. Always store the full output in `full-outputs/` before truncating. You cannot un-truncate.

**Thinking/reasoning blocks.** Strip cryptographic signatures or internal markers. Retain reasoning text.

**Streaming duplicates.** Collapse to final response only. Preserve timing metadata.

**Framework internals.** Discard progress indicators, heartbeat messages, and other framework machinery not relevant to the conversation.

**File paths.** Absolute paths expose local usernames and directory structure. Normalize paths relative to the project root (e.g., `/Users/alice/project/src/main.rs` becomes `src/main.rs`). If the project root is unknown, strip to the last N path components rather than publishing full absolute paths. Document the normalization method in `provenance.converter_version`.

**Turn content.** Framework-injected turns (session templates, command expansions, system reminders) can be large and dominate word counts. Tag these with `source: "framework"` rather than discarding, so downstream analysis can filter them.

**Timestamps.** Source formats may store timestamps without timezone or in local time. Normalize to ISO 8601 UTC and document the assumption in annotations if the source timezone is ambiguous.

## Quality tiers

Not all organic sessions are equally useful for research. Assign a quality tier during conversion based on data completeness:

| Tier | Criteria | Action |
|------|----------|--------|
| **A: Research-ready** | `has_conversation_content AND has_tool_call_io AND tool_call_count > 10 AND turn_count > 5` | `for_research: true` |
| **B: Usable with cleaning** | `has_conversation_content AND (NOT has_tool_call_io OR tool_call_count <= 10)` | `needs_cleaning: true` |
| **C: Metadata only** | `NOT has_conversation_content` (only metadata/summary available) | Extract what's available, flag |
| **D: Discard** | `turn_count < 3 OR corrupted OR empty` | Don't convert, log in manifest |

Criteria definitions:

- `has_conversation_content`: the adapter can extract turn-level messages with role and content.
- `has_tool_call_io`: the adapter can extract tool call arguments and results (not just names).

Quality tier is a session-level field (`quality: "A" | "B" | "C" | "D"`) computed by the adapter during conversion. Adapters SHOULD populate this field based on the criteria above. The manifest also records quality for per-period summaries.

Quality tiers are defined relative to tool-use behavioral research (the primary minitrace use case). Other research questions may have different quality requirements. For example, conversational pattern analysis needs turn content but not tool I/O, making a "B" session fully usable. Your adapter can define additional quality dimensions in annotations.

## Source conflict resolution

When the same session appears in multiple sources (e.g., a transcript file and a database), define per-field precedence in your adapter. General principle: prefer the source with richer, more granular data.

Document your precedence table. When sources conflict on the same field, record the conflict in `flags.needs_cleaning` with detail in annotations.

## Tool operation type mapping

Frameworks use different names for equivalent operations. The `operation_type` field provides a universal classifier for cross-framework comparison. The `tool_name` field preserves the framework-specific name.

| operation_type | Claude Code | Codex | Goose | Pi | OpenCode | Droid | Gemini CLI | Vibe | OpenClaw |
|---------------|-------------|-------|-------|-----|----------|-------|-----------|------|----------|
| `READ` | Read, Glob, Grep | exec_command (cat, ls, grep...) | read, tree, list_directory, search_files | read, bash (cat...) | read, grep, glob, fetch | Read, Glob, Grep | grep_search, read_file, list_directory | grep, read_file, list_directory | read, web_fetch, memory_search, memory_get, sessions_list, sessions_history, session_status |
| `MODIFY` | Edit | exec_command (sed -i, patch...) | edit, text_editor | edit | edit | Edit | edit_file | edit_file | edit |
| `NEW` | Write | exec_command (touch, cp, >...) | write | write | write | Write | write_file | write_file | write |
| `EXECUTE` | Bash | exec_command (default) | shell | bash | bash | Bash | run_shell_command | bash | exec, process, cron |
| `DELEGATE` | Agent, Task | - | - | - | - | Agent | - | task | sessions_spawn, subagents |
| `OTHER` | AskUserQuestion, Skill | - | remember | - | - | - | web_search | exit_plan_mode | sessions_send, sessions_yield |

**Mapping guidance:**

- When in doubt, use `OTHER`. Incorrect classification is worse than unclassified.
- Search operations (grep, glob, find) are `READ`. They read content without modifying state.
- File creation where the file may or may not exist: use `NEW` if you can determine the file did not exist, `MODIFY` otherwise.
- MCP tool calls: classify by what the tool does, not by the MCP protocol layer.
