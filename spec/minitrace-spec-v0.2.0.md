---
title: "minitrace v0.2.0 -- Format Specification"
author: "fukami"
date: 2026-03-28
lang: en-GB
description: "Format specification for minitrace v0.2.0. Schema definitions, input provenance fields, failure taxonomy, and annotation layer for structured human-AI session capture."
---

---

## Purpose

minitrace is a session trace format for capturing human-AI coding interactions. It enables reproducible behavioral experiments, operational practice documentation, cross-model comparison, and failure pattern analysis. Sessions are stored as structured JSON with schemas for turns, tool calls, and annotations.

### Session profiles

minitrace supports two capture profiles:

| Profile | Use case | scenario_id | condition | outcome annotation |
|---------|----------|-------------|-----------|-------------------|
| **Controlled** | Designed experiments with reproducible conditions | Required | Required | Required |
| **Organic** | Natural operational sessions captured as-is | Optional | Optional | Optional |

Both profiles use the same schemas. The distinction is which fields are populated.

### Format conventions

- **Timestamps:** ISO 8601 with mandatory UTC, e.g., `2026-03-08T22:00:00Z`. All timestamps in a session record MUST use this format. No local time, no offset notation.
- **Hashes:** `sha256:<hex-digest>`. SHA-256 only.
- **IDs:** Opaque strings. No format enforced, but UUIDs recommended for new sessions.

### Backward compatibility

v0.2.0 is backward-compatible with v0.1.0. All new v0.2.0 fields (`Turn.input_channel`, `Turn.model`, `Turn.content_type`, `ToolCall.output.content_origin`, `ToolCall.output.redacted`, `environment.platform_type`, and the four cross-turn metrics) are nullable and SHOULD-level. A valid v0.1.0 trace (where these fields are absent) is also a valid v0.2.0 trace with all new fields implicitly null. No migration is required.

Validators MUST accept both `minitrace-v0.1.0` and `minitrace-v0.2.0` schema versions. When validating v0.1.0 traces, the new fields are ignored. When validating v0.2.0 traces, the new fields are validated against their enum sets if present. Values outside the defined enum sets are validation errors.

Readers SHOULD treat absent fields as equivalent to `null`. A v0.1.0 trace where `input_channel` is absent is semantically identical to a v0.2.0 trace where `input_channel` is explicitly `null`. DuckDB's `read_json` handles this natively (missing keys become null). Converters are not required to backfill v0.1.0 traces with explicit null fields.

---

## 1. Scenario Definition Schema

A scenario is a reproducible test condition. Required for controlled profile, optional for organic.

```yaml
scenario:
  id: string                    # unique identifier (e.g., "deceptive-location-v1")
  version: string               # this scenario's own version (not minitrace schema version). Since scenarios are immutable (changes produce a new id), this is typically "1".
  category: string              # what it tests (guidance-effectiveness, verification-mismatch, error-propagation, coordination)

  setup:
    description: string         # human-readable purpose
    task: string                # what the model is asked to do
    context: object             # files, codebase state, environment
    deception: object | null    # if scenario includes lies/traps, what and where
    expected_behavior: string   # what correct behavior looks like
    failure_modes: string[]     # anticipated ways to fail

  conditions:                   # what can be varied
    guidance: string[]          # instruction variants (careful, fast, verbose)
    tools_available: string[]   # what tools the model can use
    permissions: object         # explicit grants/restrictions

  coordination:
    type: "single" | "multi-session"
    tests_handover: boolean     # true if scenario focuses on handover failures
    sessions_expected: int      # 1 for single, N for multi-session
    coordination_risks: string[] # failure modes tested (stale-handover, duplicate-work, context-loss, state-divergence)

  metadata:
    author: string
    created: datetime
    tags: string[]
    mast_mapping: string[]      # corresponding MAST categories (e.g., "1.1", "2.3", "2.5", "3.2")
    toolemu_mapping: string[]   # corresponding ToolEmu categories if applicable
```

Scenario specs are immutable once published. To modify, create a new version with a new `id` (e.g., `deceptive-location-v2`). Never edit a published scenario.

---

## 2. Session Schema

A session is one run of a scenario (controlled) or one captured operational session (organic).

```yaml
session:
  id: string                    # unique session identifier
  schema_version: string        # "minitrace-v0.2.0". Format version for reader compatibility
  profile: "controlled" | "organic"
  scenario_id: string | null    # links to scenario definition (required for controlled, optional for organic)

  title: string | null          # short description (organic: auto-generated from first substantive user message)
  summary: string | null        # topic keywords or brief description (organic: auto-generated by converter)

  classification: "public" | "internal" | "confidential" | "customer-confidential"
    #   public         = publishable, open research
    #   internal       = operational, not for external sharing
    #   confidential   = credentials, sensitive config
    #   customer-confidential = engagement data, never in research corpus

  provenance:
    source_format: string       # identifier for the native format (e.g., "claude-code-jsonl-v2", "vibe-session-v1", "codex-session-jsonl-v1", "gemini-checkpoint-v1", "manual")
    source_path: string | null  # original file path before migration (for traceability)
    converted_at: datetime      # when this minitrace record was created
    converter_version: string   # adapter tool + version that produced this record (e.g., "minitrace-claude-adapter-0.2", "minitrace-vibe-adapter-0.2")
    original_session_id: string | null  # source system's session ID if different from minitrace id

  quality: "A" | "B" | "C" | "D"  # data completeness tier, computed by converter:
    #   A = full conversation + tool I/O, >10 tool calls, >5 turns
    #   B = conversation present but limited tool I/O or few tool calls
    #   C = no conversation content (metadata only)
    #   D = empty or trivial (aborted, single-turn, no useful data)
    # Note: these thresholds are calibrated for agent sessions with tool use. Web sessions without tool calls are capped at tier B regardless of conversational depth. Revision of quality tiers for web profiles is tracked as a future work item.

  flags:
    for_research: boolean       # suitable for inclusion in research datasets
    needs_cleaning: boolean     # contains artifacts that need manual review before research use
    contains_error: boolean     # known errors in capture (truncation, corruption, missing data)
    contains_pii: boolean       # personal identifiable information present
    category: string[]          # freeform tags for research categorization (optional, defaults to [])

  # --- environment ---

  environment:
    model: string | null        # model identifier (e.g., "claude-opus-4-6"). Null when source format does not provide model identification (see footnote [3])
    model_version: string       # specific version/checkpoint if known
    temperature: float | null
    tools_enabled: string[]     # actual tools available this run (framework-specific names, e.g., "Read", "Bash", "shell", "edit_file")
    system_prompt: string | null  # full system prompt used (null if not captured)
    agent_framework: string | null  # framework identifier
    agent_version: string | null    # framework version (e.g., "2.1.71")
    platform_type: "agent" | "web" | "api" | null  # v0.2.0. Interaction surface classifier. "agent" = autonomous agent with tool use (Claude Code, Codex CLI, Goose, OpenCode). "web" = conversational web UI (claude.ai, ChatGPT, Gemini web). "api" = direct API call, no UI framework. null = unknown or not classified (legacy traces). Determined by interaction surface, not model capabilities: ChatGPT with code interpreter = "web"; Claude Code in CI = "agent". IDE extensions (Cline, Cursor, Copilot Chat) classified as "agent" pending a future non-breaking "ide" value.
    provider_hint: string | null    # inferred API provider, derived from tool_use_id format, model name patterns, or other signals (not self-reported). Values: "anthropic", "google", "openai-compatible", "meta", "mistral", "local", "unknown". "local" covers locally-hosted models (Ollama, llama.cpp, vLLM). May be inaccurate, treat as a hint.

  operational_context:           # null for controlled profile. Adapters populate what's available.
    working_directory: string | null
    git_branch: string | null
    git_ref: string | null        # HEAD commit hash at session start (branch name alone is insufficient for reproducibility)
    autonomy_level: string | null   # freeform descriptor of agent autonomy (e.g., "suggest", "auto-edit", "full-auto", "restricted", "yolo"). Semantics vary by framework.
    sandbox: boolean | null         # was execution sandboxed/containerized? (null if unknown)
    framework_config: object | null  # framework-specific configuration state not covered above (e.g., loaded rules, persona, approval policy details)

  timing:
    # privacy options
    privacy_level: "full" | "anonymous" | "minimal"

    # always present
    duration_seconds: float     # wall-clock elapsed time (includes idle periods)
    active_duration_seconds: float | null  # estimated active time (excludes idle gaps > threshold). Default threshold: 300 seconds (5 minutes).

    # full mode only (null if anonymous/minimal)
    started_at: datetime | null
    ended_at: datetime | null

    # anonymous mode provides (null if minimal)
    hour_of_day: int | null     # 0-23
    day_of_week: int | null     # 0-6, Monday=0 (Python convention; differs from ISO 8601 Monday=1 and JavaScript Sunday=0)

  condition:                    # which variant was run (required for controlled, optional for organic)
    guidance_variant: string | null  # e.g., "careful", "fast", "baseline"
    permission_level: string | null  # e.g., "explicit", "implicit", "restricted"
    custom: object | null       # any other varied parameters

  # Coordination (all optional)
  coordination:
    project_id: string | null           # groups related sessions
    predecessor_session: string | null  # explicit chain to previous session
    concurrent_sessions: int | null     # how many other sessions active (if known)
    human_attention: "focused" | "divided" | "unknown"

  handover:
    received:                           # null if no explicit handover received
      from_session: string | null
      document: string                  # the handover content
      state_description: string | null  # what state was described
    produced:                           # null if no handover produced
      to_session: string | null         # null if unknown/terminal
      document: string
      state_description: string | null

  turns: Turn[]                 # the interaction sequence
  tool_calls: ToolCall[]        # all tool invocations (separate for analysis)

  outcome:                      # required for controlled, optional for organic
    success: boolean | null     # null if not assessed
    partial: boolean            # task completed but with issues
    failure_codes: string[]     # taxonomy labels if failed
    outcome_notes: string       # free-form description

  annotations: Annotation[]     # observer notes, post-hoc analysis

  metrics:                      # computed from turns/tool_calls
    turn_count: int
    tool_call_count: int
    read_count: int             # tool calls with operation_type READ
    modify_count: int           # tool calls with operation_type MODIFY
    create_count: int           # tool calls with operation_type NEW
    execute_count: int          # tool calls with operation_type EXECUTE
    delegate_count: int         # tool calls with operation_type DELEGATE
    read_ratio: float | null    # read_count / tool_call_count. Null if tool_call_count == 0
    time_to_first_action: float | null  # seconds from start to first tool call. Null if no tool calls in session
    idle_ratio: float | null    # 1 - (active_duration / duration). Null if active_duration not computed. Comparable only across sessions using the same idle threshold.

    # token economics
    total_input_tokens: int | null
    total_output_tokens: int | null
    total_cache_read_tokens: int | null
    total_cache_creation_tokens: int | null
    total_reasoning_tokens: int | null
    total_tool_tokens: int | null
    session_cost: float | null          # total session cost in USD if tracked by framework

    # subagent metrics
    subagent_count: int
    subagent_tool_calls: int

    # v0.2.0: cross-turn analysis
    model_switches: int | null      # times the model changed between turns. Computed from Turn.model deltas. Null when fewer than 2 turns have Turn.model populated.
    unique_models: int | null       # distinct model identifiers across turns. Null when fewer than 2 turns have Turn.model populated.
    median_response_tokens: int | null  # median output tokens per assistant turn. API-reported token counts only. Null when source format does not provide per-turn token counts. No character-based heuristics, no tiktoken estimates.
    max_response_tokens: int | null     # longest assistant turn by output tokens. Same source constraint as median_response_tokens.
```

`read_ratio` and `time_to_first_action` MUST be `null` for sessions with zero tool calls (ghost sessions, pure conversation). Converters MUST NOT set these to 0. Zero is a valid value (a session that read nothing, or a tool call at t=0) and is semantically distinct from "not applicable."

### Session-level model heuristic (v0.2.0)

`environment.model` is the primary model for a session: the model that handled the majority of turns, or the model the operator selected/configured. It is a summary field for filtering and manifest display.

When a session uses multiple models, `environment.model` SHOULD be set to the model that produced the plurality of assistant turns. If no model produced a plurality (equal split), adapters MAY use the most recently used model or the operator-configured model. Adapters MUST document their selection heuristic.

`Turn.model` is the authoritative per-turn record. `environment.model` is a convenience summary. When both are available, `Turn.model` takes precedence for any per-turn analysis. `environment.model` is appropriate for session-level filtering, manifest generation, and display.

### Minimum required fields by profile

A valid minitrace session record MUST contain at least:

| Field | Controlled | Organic |
|-------|-----------|---------|
| `id` | Required | Required |
| `schema_version` | Required | Required |
| `profile` | Required | Required |
| `scenario_id` | Required | Optional |
| `provenance.source_format` | Required | Required |
| `provenance.converted_at` | Required | Required |
| `provenance.converter_version` | Required | Required |
| `flags.for_research` | Required | Required |
| `flags.needs_cleaning` | Required | Required |
| `flags.contains_error` | Required | Required |
| `flags.contains_pii` | Required | Required |
| `classification` | Required | Required |
| `environment.model` | Required | Optional [3] |
| `timing.duration_seconds` | Required | Required |
| `turns` (non-empty) | Required | Required |
| `metrics.turn_count` | Required | Required |
| `metrics.tool_call_count` | Required | Required |
| `condition` | Required | Optional |
| `outcome` | Required | Optional |

[3] Organic-profile traces MAY set `environment.model` to null when the source format does not include model identification (e.g., platform data exports that omit the model used per response). Controlled-profile traces MUST always identify the model. The same applies to `environment.model_version`, `environment.agent_version`, and all token-count metrics (`total_input_tokens`, `total_output_tokens`, cache/reasoning token fields): these are Required for controlled profile but Optional for organic profile when the source format does not provide them. Adapters SHOULD document which fields are unavailable and why in the adapter specification.

All other scalar fields default to `null` if not available. Array fields (`turns`, `tool_calls`, `annotations`, `flags.category`, `tools_enabled`, `failure_codes`) default to empty arrays `[]`. Object fields (`condition.custom`, `input.arguments`, `framework_metadata`) default to `null`.

---

## 3. Turn Schema

```yaml
Turn:
  index: int                    # position in sequence
  timestamp: datetime
  role: "user" | "assistant" | "system"
  source: "human" | "framework" | "model" | "system" | null  # distinguishes authorship from API message position. "system" denotes platform-level system prompt content, distinct from "framework" (runtime injections by the agent framework). Non-obvious case: a framework-injected session-end template has role="user" (the message slot it occupies) but source="framework" (it was not typed by a human). role describes API message position; source describes authorship. Adapters SHOULD populate this field but MAY use null when the source cannot be reliably determined. "human" and "model" are reliably derivable from role in most frameworks. "framework" requires framework-specific heuristics (e.g., detecting system reminders, command expansions, injected templates). "system" is for content set via the platform's system prompt mechanism before the session begins.

  model: string | null          # v0.2.0. Per-turn model identifier when the source format provides per-message model identification (e.g., ChatGPT metadata.model_slug). Populate when known, regardless of whether it matches session-level environment.model. null means unknown for this turn — consumers should fall back to environment.model. This field does NOT replace environment.model, which remains the session-level summary.

  content_type: string | null   # v0.2.0. Content modality of this turn. Values: "text", "multimodal_text", "code", "reasoning", null. "text" is plain text (default assumption when null). "multimodal_text" contains mixed text and image/file references. "code" is executable code or code artifact. "reasoning" is model reasoning output — chain-of-thought summaries, thought recaps (distinct from the Turn.thinking field, which captures raw reasoning tokens). null means unknown or not classified.

  input_channel: string | null  # v0.2.0. Through what channel did this turn's content arrive? Orthogonal to source (which answers "who authored this?"). input_channel answers "through what delivery mechanism?" This distinction matters for prompt injection analysis: a turn with source="framework" could arrive via system_prompt (operator trust) or retrieval (attacker-influenceable). Adapters SHOULD populate this field but MAY use null when the channel cannot be reliably determined. See Input Provenance section below.
    # Values:
    #   "user_input"       — typed/pasted by human operator
    #   "system_prompt"    — initial system prompt, instructions file (e.g., CLAUDE.md, .cursorrules)
    #   "framework_control" — v0.2.0. Framework-generated behavioral steering (system-reminders, policy hooks, command expansions, skill loads). Operator trust level.
    #   "framework_content" — v0.2.0. Framework-inserted substantive data from operator-controlled sources (CLAUDE.md loaded into turn, memory file contents, local config). Operator trust level. Boundary: content is framework_content if it exists in the deployment's filesystem or configuration at container/session start time.
    #   "framework_inject"  — DEPRECATED. v0.1.0 value, accepted for backward compatibility. New adapters MUST NOT use this value. Use framework_control or framework_content instead.
    #   "tool_output"      — tool result delivered as a conversation turn (the tool call itself is in tool_calls[])
    #   "retrieval"        — RAG result, memory lookup, or fetched content injected by framework
    #   null               — unknown or not classified

  content: string               # full message content
  framework_metadata: object | null  # framework-specific fields not covered by the core schema

  # for assistant turns only
  tool_calls_in_turn: string[]  # IDs of tool calls made in this turn
  thinking: string | null       # model thinking/reasoning if captured (stripped of signatures)

  intent_markers:               # optional, can be inferred post-hoc
    requested: boolean          # user explicitly asked for this
    inferred: boolean           # logical next step, not explicitly requested
    proactive: boolean          # helpful but not asked for

  streaming:
    was_streamed: boolean
    stream_log: string | null   # path to raw stream capture if stored

  # token accounting per turn
  usage:
    input_tokens: int | null
    output_tokens: int | null
    cache_read_tokens: int | null   # tokens served from cache (Anthropic, Gemini)
    cache_creation_tokens: int | null # tokens written to cache (Anthropic)
    reasoning_tokens: int | null    # thinking/reasoning tokens (OpenAI, Gemini)
    tool_tokens: int | null         # tokens consumed by tool definitions/results (Gemini)
```

### Conversation linearization (v0.2.0)

Turns are a linear sequence. `turns[]` is an ordered array representing the conversation as experienced by the participant. When the source platform supports branching (e.g., ChatGPT regeneration), adapters MUST linearize to the selected/final branch. Discarded branches are not included in `turns[]`.

`turn_count` in metrics counts the linearized sequence only.

Branch metadata MAY be preserved on surviving turns via advisory `framework_metadata` conventions:

```yaml
Turn.framework_metadata: {
  "branch_parent_turn": 12,       # which turn this was regenerated from
  "branch_index": 2,              # 0-indexed: this was the 3rd generation
  "branch_siblings": 3,           # total alternatives generated
  "branch_selected": true         # was this the one the user continued with
}
```

These keys are advisory conventions, not validated by the schema. If discarded branch research becomes needed, it belongs in a separate structure, not interleaved in `turns[]`.

---

## 4. ToolCall Schema

```yaml
ToolCall:
  id: string                    # unique identifier
  emitting_turn_index: int | null  # which turn emitted this tool call, if the framework models tool calls as subordinate to turns. Null for shell-driven agents where tool events are the primitive and assistant messages are secondary summaries. Adapters using Anthropic-style tool_use blocks or OpenAI function calling SHOULD populate this; adapters for shell-first frameworks MAY use null.
  timestamp: datetime

  tool_name: string             # framework-specific tool name (e.g., "Read", "edit_file", "shell", "ReadFile")
  operation_type: "READ" | "MODIFY" | "NEW" | "EXECUTE" | "DELEGATE" | "OTHER"  # universal classifier. Use this for cross-framework comparison

  input:
    file_path: string | null
    command: string | null      # for Bash
    arguments: object           # tool-specific args

  output:
    success: boolean
    result: string | null       # content, truncated if over limit
    error: string | null
    duration_ms: int | null     # null if not tracked (PostToolUse-only hooks cannot compute this)

    # truncation handling
    truncated: boolean
    full_bytes: int | null      # original size if truncated
    full_hash: string | null    # format: "sha256:<hex-digest>" (SHA-256 only)
    full_reference: string | null # path/URI to full output if stored

    redacted: boolean | null    # v0.2.0. true if tool result was policy-redacted by the platform before export (e.g., "not supported on your device" blocks in claude.ai exports). Semantically distinct from truncated (size-based removal by the adapter). null means unknown or not checked. false means checked, no redaction found.

    # v0.2.0: content provenance
    content_origin: string | null  # Where did this tool's output content originate? Complements Turn.input_channel by providing finer-grained provenance for tool results. When a tool result is delivered as a turn (input_channel="tool_output"), content_origin specifies what the tool actually accessed. Adapters SHOULD populate this field but MAY use null when the origin cannot be determined. See Input Provenance section below.
      # Values:
      #   "local_file"     — read from local filesystem
      #   "local_exec"     — output of local command execution
      #   "web"            — fetched from the internet
      #   "mcp_server"     — result from an MCP (Model Context Protocol) tool server
      #   "database"       — database query result
      #   "sub_agent"      — generated by a delegated model (sub-agent, spawned task)
      #   "model_echo"     — framework echo of model-authored content (e.g., write-tool confirmation, memory recall of model-generated text)
      #   "user_provided"  — content provided directly by the user (e.g., AskUserQuestion)
      #   null             — unknown or not classified

  context:
    position_in_session: float  # tool_call_index / total_tool_calls (0.0 to 1.0)
    tools_before: string[]      # recommended: last 3-5 tool names used (for sequence analysis). EXPERIMENTAL. Optimal window size is an open research question. Hypothesis: tool sequences predict failures (e.g., "write without preceding read" correlates with over-autonomy). Promotion to stable requires: empirical evidence that window size >= N has predictive validity for at least one failure code. Demotion criteria: no predictive value after sufficient data collection.
    time_since_last_user: float # seconds since last user turn

  # framework-specific metadata
  framework_metadata: object | null  # framework-specific fields not covered by the core schema (e.g., behavioral audit tags, approval decisions, sandbox policy)

  # for spawned/delegated agents
  spawned_agent:                # null if not a delegation
    agent_type: string          # "explorer", "reviewer", "drafter", etc.
    task_scope: string          # what it was asked to do
    sub_session_id: string | null # links to child minitrace if captured
    outcome_summary: string     # what it returned (brief)
```

---

## 5. Input Provenance (v0.2.0)

v0.2.0 adds two fields for tracking where content enters the model's context. These support prompt injection path analysis by distinguishing trusted from untrusted input channels.

### Design rationale

The existing `Turn.source` field answers "who authored this?" with four string values (`human`, `framework`, `model`, `system`) plus `null`. This is too coarse for injection research. `framework` lumps system prompts, RAG results, MCP outputs, and injected templates into one bucket. For injection path analysis, researchers need to know *which channel* carried untrusted content.

Rather than expanding the `source` enum (which would break the clean authorship semantic), v0.2.0 adds two orthogonal fields:

- **`Turn.input_channel`** answers "through what delivery mechanism did this content arrive?"
- **`ToolCall.output.content_origin`** answers "what did this tool actually access?"

### Trust levels

`input_channel` values carry implicit trust levels for injection analysis:

| input_channel | Trust level | Rationale |
|--------------|-------------|-----------|
| `user_input` | Operator trust | Direct human input |
| `system_prompt` | Operator trust | Set by operator before session |
| `framework_control` | Operator trust | Framework-generated behavioral steering, operator-configured |
| `framework_content` | Operator trust | Framework-inserted data from operator-controlled sources (on-disk at session start) |
| `framework_inject` | *(deprecated)* | v0.1.0 value. Accepted for backward compatibility. Trust equivalent to `framework_control` |
| `tool_output` | Variable | Depends on what the tool accessed (see `content_origin`) |
| `retrieval` | Low | Content may be attacker-influenceable (web pages, shared documents, user-submitted data) |

`content_origin` further refines the trust picture for `tool_output` turns:

| content_origin | Trust level | Rationale |
|---------------|-------------|-----------|
| `local_file` | Medium-high | Local filesystem, but files could contain injections |
| `local_exec` | Medium | Command output, depends on what was executed |
| `web` | Low | Internet content, attacker-influenceable |
| `mcp_server` | Variable | Depends on the MCP server and its data sources |
| `database` | Medium | Local data store |
| `sub_agent` | Medium | Another model's output (delegation chain) |
| `model_echo` | Medium-high | Framework echo of model-authored content (the model wrote this content in a prior step) |
| `user_provided` | Operator trust | Direct human response |

Trust levels are default risk heuristics for researchers, not enforced constraints. Actual trust depends on deployment context (e.g., a local trusted memory store may warrant higher trust than the default for `retrieval`; a hostile hook configuration may warrant lower trust than the default for `framework_control`).

**Known limitation:** `user_input` assumes the operator is not an unwitting conduit for adversarial content. Scenarios where users paste attacker-controlled content (e.g., copying a suspicious email into the chat) are indistinguishable from genuine human input at the trace level. Distinguishing these would require UI-level instrumentation beyond what trace formats can capture.

### Relationship between fields

`input_channel` and `content_origin` are complementary:

- `input_channel` is set on **turns**. Each turn SHOULD have at most one input channel. In practice, some turns combine content from multiple channels (e.g., framework wrapper text around a user quote, or a turn mixing retrieved content with framework instructions). When a turn has mixed provenance, adapters SHOULD classify by the dominant or highest-risk channel. A future version may support an array of channels per turn.
- `content_origin` is set on **tool call outputs**. It refines `input_channel="tool_output"` turns.
- A turn with `input_channel="tool_output"` typically corresponds to tool calls where `content_origin` is also populated.
- For non-tool turns, `content_origin` does not apply (it lives on the ToolCall, not the Turn).
- `input_channel="retrieval"` applies when the framework injects retrieved content directly into the conversation without going through the tool call mechanism. If retrieval goes through a tool call (e.g., a RAG tool), use `input_channel="tool_output"` with appropriate `content_origin` instead.

### OWASP LLM Top 10 mapping

These fields directly support analysis of **LLM01: Prompt Injection** from the OWASP LLM Top 10:

- **Direct injection:** `input_channel="user_input"` with malicious content
- **Indirect injection via RAG:** `input_channel="retrieval"` carrying attacker-planted content
- **Indirect injection via tools:** `input_channel="tool_output"` with `content_origin="web"` carrying attacker-controlled responses
- **Indirect injection via MCP:** `input_channel="tool_output"` with `content_origin="mcp_server"` from untrusted MCP servers

### Adapter coverage

Per-adapter implementation status is documented in adapter-level documentation. Adapters SHOULD use `null` when uncertain rather than guessing.

[1] Codex routes all tool use through `exec_command`. The `local_exec` classification reflects the tool name, not the command's actual behaviour. A command that fetches web content (e.g., `curl`) is classified as `local_exec`, not `web`. Researchers querying `content_origin="web"` to find injection surfaces will miss Codex sessions where web content was fetched via shell commands.

---

## 6. Annotation Schema

```yaml
Annotation:
  id: string
  timestamp: datetime           # when annotation was made
  annotator: string             # who made it (user, model, automated)

  scope:
    type: "session" | "turn" | "tool_call" | "handover"
    target_id: string           # what it refers to

  content:
    category: string            # observation, pattern, ai-failure, recommendation
    tags: string[]              # taxonomy labels
    title: string               # short description
    detail: string              # full annotation

  taxonomy_mappings:
    minitrace: string[]         # local failure codes
    mast: string[]              # MAST category codes
    toolemu: string[]           # ToolEmu codes if applicable

  # classification override
  classification: "public" | "internal" | "confidential" | "customer-confidential" | null  # if annotation changes session classification (e.g., annotator discovers PII). Values MUST use the session-level classification enum. Overrides MUST be toward more restrictive only.
```

---

## 7. Failure Taxonomy

### Primary failure codes

| Code | Name | Description | MAST Mapping |
|------|------|-------------|--------------|
| F-AUT | Over-autonomy | Acted beyond instruction scope | FC2 2.2 (Fail to ask for clarification) |
| F-INS | Disobey-instruction | Failed to follow explicit instruction | FC1 1.1 (Disobey task specification) |
| F-ROG | Going-rogue | Pursued goals misaligned with user intent | FC2 2.3 (Task derailment) |
| F-VER | Verification-mismatch | Used wrong method to verify | FC2 2.6 + FC3 3.2/3.3 |
| F-HAL | Hallucination | Stated false information confidently | |
| F-STA | Knowledge-stale | Used outdated information | |
| F-PRO | Error-propagation | Compounded earlier error | |
| F-SCO | Scope-creep | Expanded task beyond request | FC1 1.2 (Disobey role specification) |
| F-DEF | Excessive-deference | Over-cautious, refused to act, excessive confirmation seeking | |
| F-ASM | Unverified-assumption | Acted on assumption without checking available data | |
| F-CMP | Completion-bias | Prioritized finishing over actual user need | FC3 3.1 (Premature termination) |
| F-LUP | Tool-loop | Repeated same or equivalent tool call without progress (common in ReAct-style agents) | |
| F-MIS | Misreported-completion | Reported task status (completion, test results, file state, deployment state) inconsistent with actual state. Requires: (a) explicit status assertion by agent, (b) observable evidence contradicting it. Distinguished from F-HAL (false factual claims not about task status) and F-CMP (rushing to finish, which may or may not include false status claims) | |
| F-OBS | Observation-failure | Misread or failed to correctly process available evidence from tool output or context. Requires: (a) evidence was present in agent's context, (b) agent's subsequent action contradicts what evidence shows. Distinguished from F-ASM (evidence not consulted) and F-HAL (confident false claims, which may occur with or without evidence present) | |

MAST reference: arXiv:2503.13657. MAST categories FC1 (1.1-1.5), FC2 (2.1-2.6), FC3 (3.1-3.3). Codes without MAST mapping are derived from operational practice, observed in real human-AI interaction sessions but outside MAST's multi-agent focus.

### Security failure codes

| Code | Name | Description | OWASP LLM Mapping |
|------|------|-------------|-------------------|
| F-INJ | Injection-susceptibility | Treated untrusted input as instructions (followed adversarial content from files, web pages, or injected prompts) | LLM01 (Prompt Injection) |
| F-EXF | Data-exfiltration | Sent sensitive data to unauthorized destination (leaked credentials, PII, or confidential content in output) | LLM06 (Sensitive Information Disclosure) |
| F-SEC | Security-boundary-violation | Crossed trust, data, or network boundary without authorization (accessed resources outside permitted scope) | LLM07/LLM08 (approximate) [2] |

These codes complement the primary failure codes for security-focused analysis. They map to OWASP LLM Top 10 categories rather than MAST (which focuses on multi-agent coordination, not security boundaries). F-INJ is the primary code for prompt injection scenarios (INJ-1 through INJ-4). F-EXF covers data leakage scenarios (INJ-5). F-SEC covers boundary violations not captured by the other two.

Security codes and primary codes can co-occur on the same session. F-INJ typically co-occurs with one of F-AUT (agent acted beyond scope by following injected instructions), F-INS (agent disobeyed real instructions in favour of injected ones), or F-ROG (agent pursued attacker-aligned goals). The annotation schema supports multiple failure codes per session via the `tags` array. Use the security code for the attack vector and the primary code for the resulting behavioural failure.

Injection test scenarios are defined in `scenarios/definitions/` and are not reproduced in this specification.

[2] F-SEC's OWASP mapping is approximate. LLM02 (Insecure Output Handling) covers downstream systems acting on unvalidated LLM output, which is narrower than F-SEC's scope. F-SEC also overlaps with LLM07 (Insecure Plugin Design) for tool/plugin boundary violations and LLM08 (Excessive Agency) for unauthorized actions. No single OWASP category cleanly maps to trust boundary crossing.

### Coordination failure codes

| Code | Name | Description | MAST Mapping |
|------|------|-------------|--------------|
| F-HND | Stale-handover | Acted on outdated handover information | FC2 2.5 (Ignored other agent's input) |
| F-DUP | Duplicate-work | Repeated work done in another session | |
| F-CTX | Context-loss | Lost relevant context across sessions | FC1 1.4 + FC2 2.5 |
| F-DIV | State-divergence | Sessions produced conflicting state | |
| F-MSG | Message-failure | Failed to pass critical information | FC2 2.4 (Information withholding) |

### Context codes (contributing factors)

| Code | Name | Description |
|------|------|-------------|
| C-PHA | Late-session | Occurred in final 20% of session |
| C-TIM | Time-pressure | Occurred during rushed period |
| C-DOM | Domain-risk | Occurred in high-risk domain context |
| C-SEQ | Sequence-risk | Preceded by risk pattern (e.g., no recent read) |
| C-DIV | Divided-attention | Human attention was divided across sessions |
| C-HND | Handover-absent | No explicit handover where one was needed |

### Taxonomy design principles

The failure taxonomy is intentionally compact. Operational practice produces finer-grained categories (e.g., `unauthorized-action` as a subtype of F-AUT, `knowledge-ignored` as a subtype of F-STA). These are valid as annotation tags but not promoted to F-codes unless they represent a distinct failure mechanism rather than a more specific instance.

When to add a new F-code vs. use annotation tags: a new F-code requires a distinct failure mechanism, generalizable across teams and frameworks, with high enough frequency to warrant structured analysis. Annotation tags cover specific instances of an existing code, team-specific categories, or patterns too rare to justify schema extension.

---

## 8. File Format

Sessions stored as JSON with extension `.minitrace.json`.

Recommended truncation: 10 KB per tool call output. Store full output separately if needed, referenced via `full_reference` and `full_hash`.

Compression: cold tier uses Zstandard (`.zst`). Active and archive tiers are uncompressed for fast access.

---

## 9. Classification and Access Control

minitrace sessions carry a classification level that determines handling:

| minitrace | Can send to external AI? | Can publish? |
|-----------|--------------------------|--------------|
| `public` | Yes | Yes |
| `internal` | Requires review | No |
| `confidential` | No | No |
| `customer-confidential` | No | No, destroy after retention |

Classification is set at conversion time and can be overridden by annotations (always toward more restrictive, never less).

Derived research observations from `customer-confidential` sessions use classification `internal` with provenance noting `derived-from-customer`.

### Classification constraints

Classification levels are ordered: `public < internal < confidential < customer-confidential`. Constraints use `>=` and `<=` against this ordering.

- `contains_pii = true` -> `classification >= confidential`
- `customer-confidential` -> `for_research = false` (always)
- `for_research = true` -> `classification <= internal`
- `public + contains_pii` = **INVALID** (converter MUST reject)

Converters MUST enforce these constraints at write time and reject or auto-escalate sessions that violate them. A session with `contains_pii: true` and `classification: "internal"` MUST be escalated to `confidential` or rejected with an error. It MUST NOT be written as-is.

### Security considerations for consumers

All string fields in minitrace traces are untrusted data. Consumers MUST sanitize field values for their rendering context before display or processing: HTML-escape for web dashboards, parameterized queries for SQL (including DuckDB), and safe escaping for shell contexts. Fields such as `ToolCall.input.command`, `Turn.content`, `ToolCall.output.result`, `outcome_notes`, and annotation text may contain shell commands, code, markdown, or terminal control sequences that could trigger injection or UI spoofing if rendered unsafely. In particular, `ToolCall.input.command` MUST NOT be executed by consumers without explicit user consent and sandboxing. Path-like fields (`source_path`, `file_path`, `full_reference`, `stream_log`) are inert strings and MUST NOT be dereferenced automatically. Open-ended object fields (`framework_metadata`, `condition.custom`, `input.arguments`, `framework_config`) may contain arbitrary nested data; consumers processing traces from untrusted sources SHOULD validate or strip these fields.

---

## 10. Manifest Format

The manifest is split by year-month. Each period has its own manifest file. A root manifest indexes the period manifests.

**Root manifest:** `manifest.json`

```json
{
  "version": "minitrace-manifest-v2",
  "generated_at": "2026-03-13T22:00:00Z",
  "periods": [
    { "period": "2026-03", "path": "active/2026-03/manifest.json", "session_count": 15 },
    { "period": "2026-02", "path": "archive/2026-02/manifest.json", "session_count": 40 },
    { "period": "2026-01", "path": "archive/2026-01/manifest.json", "session_count": 45 }
  ],
  "statistics": {
    "total_sessions": 100,
    "by_profile": { "organic": 95, "controlled": 5 },
    "by_quality": { "A": 30, "B": 20, "C": 35, "D": 15 },
    "by_classification": { "public": 5, "internal": 90, "confidential": 5 },
    "date_range": { "earliest": "2025-12-01", "latest": "2026-03-13" }
  }
}
```

**Period manifest:** `<tier>/<YYYY-MM>/manifest.json`

```json
{
  "version": "minitrace-manifest-v2",
  "period": "2026-03",
  "generated_at": "2026-03-13T22:00:00Z",
  "sessions": [
    {
      "id": "session-uuid",
      "schema_version": "minitrace-v0.2.0",
      "profile": "organic",
      "title": "minitrace spec review for external sharing",
      "classification": "internal",
      "quality": "A",
      "started_at": "2026-03-08T10:00:00Z",
      "duration_seconds": 3600,
      "model": "claude-opus-4-6",
      "agent_framework": "claude-code",
      "turn_count": 25,
      "tool_call_count": 47,
      "file_path": "session-uuid.minitrace.json",
      "file_size_bytes": 524000,
      "source_format": "claude-code-jsonl-v2",
      "flags": { "for_research": false, "needs_cleaning": false }
    }
  ]
}
```

---

## 11. Open Questions (for future versions)

1. **Integrity verification**: How to verify state consistency across sessions? Hash algorithm specified (SHA-256), but cross-session state verification protocol still open.

2. **Human reflection capture**: How to capture human's post-session assessment? Separate schema or annotation type?

3. **Conflict detection**: Automated detection of concurrent edits to same files across parallel sessions.

4. **Sub-agent traces**: When spawned agent produces full trace, how to inline vs reference?

5. **Configuration file references**: `operational_context` should **reference** agent configuration files (e.g., CLAUDE.md, .cursorrules, codex instructions) by git commit hash + path, not inline them. These files evolve over time and are reconstructable from git history using the session timestamp.

6. **Multi-hop content provenance**: When a shell tool call runs a command that fetches web content (e.g., `curl`), `content_origin="local_exec"` reflects the tool but not the actual content source. The real provenance is `web`, but determining this requires parsing command strings, which is fragile. This is the Codex `exec_command` problem (see footnote [1]) generalised to every framework with a shell tool. A future version might add a `content_origin_chain` field or support adapter-level heuristics for common patterns (`curl`, `wget`, `fetch`).

Deferred design questions from external review are tracked in [DESIGN-NOTES.md](../docs/DESIGN-NOTES.md).

---

## Changelog

- v0.2.0: Input provenance fields, security failure taxonomy, and external review revisions. Adds `Turn.input_channel` (delivery channel classification) and `ToolCall.output.content_origin` (tool output source classification) for prompt injection path analysis. Both fields are nullable, backward-compatible with v0.1.0. New Section 5 (Input Provenance) documents design rationale, default risk heuristics, OWASP LLM01 mapping, and per-adapter coverage. Motivated by external feedback that `Turn.source` is too coarse for injection research. Adds three security failure codes (F-INJ, F-EXF, F-SEC) with OWASP LLM Top 10 mappings plus F-LUP (tool-loop). Injection test scenarios added to scenarios/definitions/. Post-external-review changes: `ToolCall.turn_index` renamed to `emitting_turn_index: int | null` to support shell-first frameworks; `content_origin="model"` split into `sub_agent` and `model_echo`; `quality` field added to session schema; `provider_hint` expanded; trust table reframed as default risk heuristics; mixed-channel turns acknowledged. Deferred design questions from external review tracked in DESIGN-NOTES.md. Design session 15 additions: `environment.platform_type` enum (agent/web/api) for interaction surface classification; `Turn.model` (per-turn model identification); `Turn.content_type` (content modality classification); `ToolCall.output.redacted` (platform redaction flag); `input_channel` split from `framework_inject` into `framework_control` (behavioral steering) and `framework_content` (operator data), with `framework_inject` deprecated but accepted; four cross-turn metrics (`model_switches`, `unique_models`, `median_response_tokens`, `max_response_tokens`); session-level model heuristic codified; conversation linearization statement with advisory branch metadata conventions in `framework_metadata`; failure codes F-MIS (misreported completion) and F-OBS (observation failure) promoted to primary taxonomy.

- v0.1.0: Initial public release. 9 framework adapters (Claude Code, Codex, Goose, Pi, OpenCode, Droid, Gemini CLI, Vibe, OpenClaw). Session schema with turns, tool calls, metrics, annotations. Two profiles (controlled, organic). Failure taxonomy with MAST and OWASP mappings. DuckDB-queryable JSON output.

---

*minitrace is an open format. Contributions and feedback welcome.*
