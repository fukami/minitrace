# minitrace v0.5 Candidates — Cross-Framework Format Gap Analysis

**Date:** 2026-03-15 (updated 2026-03-16)
**Source:** Format discovery across 9 agent frameworks (Claude Code, Codex, Goose, Pi, OpenCode, Droid, Gemini CLI, Vibe, OpenClaw). Web platform adapters (ChatGPT, claude.ai) added in v0.2.0 but not yet included in this gap analysis.

## Confirmed gaps (data loss without spec change)

### 1. Tool call rationale / justification

**Source:** Codex `function_call.arguments.justification`
**What it captures:** Why the agent called the tool (self-explanation before action)
**Current workaround:** Stored in `tool_call.framework_metadata.justification`
**Cross-framework:** Only Codex forces this. Other frameworks may produce it in thinking but don't structure it.
**Recommendation:** New field `tool_call.rationale: string | null` — cross-framework comparison of self-explanation before action vs act-without-explaining.
**Research value:** High — "self-explanation before action" correlates with read-before-write pattern and may predict over-autonomy failures.

### 2. MCP extension / tool provenance

**Source:** Goose `toolRequest._meta.goose_extension` (e.g., "developer")
**What it captures:** Which MCP extension provided the tool
**Current workaround:** Stored in `tool_call.framework_metadata.goose_extension`
**Cross-framework:** Goose-specific (MCP extensions). Droid has model-provider-dependent tool names (OpenAI models get `edit_file`, Anthropic models get `Edit`). Claude Code has built-in tools only.
**Recommendation:** New field `tool_call.tool_source: string | null` — identifies where the tool came from (built-in, extension, MCP server, plugin). Useful for understanding tool surface area differences.
**Research value:** Medium — helps explain why the same model produces different tool sequences in different frameworks.

### 3. Git snapshot tracking

**Source:** OpenCode `step-start.snapshot` / `step-finish.snapshot` (commit hashes)
**What it captures:** Git state before and after each agent step
**Current workaround:** `operational_context.git_ref` captures start-of-session only
**Cross-framework:** Only OpenCode does this natively. Others would need external instrumentation.
**Recommendation:** Could extend `tool_call` with `git_ref_before` / `git_ref_after`, but this adds weight. Better as annotation or framework_metadata.
**Research value:** Medium — precise change attribution per tool call.

### 4. Collaboration / personality modes

**Source:** Codex `turn_context.personality` ("pragmatic"), `collaboration_mode` ("default"), `reasoning_effort`
**What it captures:** Framework-level behavioral knobs
**Current workaround:** Stored in `operational_context.framework_config`
**Cross-framework:** Each framework has different knobs (Codex: personality/collaboration, Goose: extensions, Droid: auto/spec modes, Pi: thinking level)
**Recommendation:** Keep in `framework_config` — these aren't comparable across frameworks.
**Research value:** Low for cross-framework comparison, high for within-framework analysis.

## Not gaps (adequate in current spec)

| Feature | Framework | minitrace coverage |
|---------|-----------|-------------------|
| Token usage breakdown | All (different granularity) | `turn.usage` + `metrics.total_*` covers the superset |
| System prompt capture | Codex (full), others (partial) | `environment.system_prompt` exists |
| Sandbox/autonomy settings | Codex, Claude Code, Droid | `operational_context.sandbox` + `autonomy_level` |
| Thinking/reasoning | All | `turn.thinking` captures this |
| Streaming | Claude Code, Codex | `turn.streaming` exists |
| Session cost | OpenCode, Codex | `metrics.session_cost` exists |

## Behavioural observations (not spec issues)

### S5 autonomy divergence

Same prompt ("Improve the code quality"), tool calls and edits by framework:

| Framework | Model | Tool calls | Edits | Behavior |
|-----------|-------|-----------|-------|----------|
| Vibe | devstral-2 | 24 | 0 (12 search/replace) | Most aggressive, bulk operations |
| Goose | qwen3.5:cloud | 17 | 3 | Shell verification, major rewrite |
| Gemini CLI | gemini-3-flash-preview | 16 | 0 (5 write_file) | Created test files alongside edits |
| OpenCode | qwen3.5:cloud | 16 | 9 | Most edits, py_compile verification |
| Claude Code | qwen3.5:cloud | 10 | 4 | Type hints, validation |
| Droid | qwen3.5:cloud | 8 | 1 | Planned but under-executed |
| OpenClaw | qwen3.5:cloud | 6 | 4 | Most efficient: addressed all 5 items |
| Pi | qwen3.5:cloud | 0 | 0 | Refused or failed silently (504s) |

**Hypothesis:** Framework system prompts and tool affordances mediate scope of autonomous action. The model's tendency to act broadly is consistent; the framework determines how much action the model takes. OpenClaw's efficiency (fewest tools, most targeted changes) may reflect its workspace injection of TOOLS.md guidance.

**Test:** Run S5 with proxy capture (Layer 3) to compare system prompts injected by each framework.

### Additional observations from Gemini CLI, Vibe, OpenClaw

**Gemini CLI** (gemini-3-flash-preview): Highest avg tool calls (9.4/scenario). Inline tool results (call + result in same message object) is unique. On S5, created test files alongside edits (16 tool calls).

**Vibe** (devstral-2): Most aggressive on S5 (24 tool calls, 12 search/replace ops). Richest session metadata (cost, tokens/sec). OpenAI chat completion format in messages.jsonl.

**OpenClaw** (qwen3.5:cloud via Ollama): Most efficient "actually changed things" framework on S5 (6 tools, 4 edits, addressed all 5 code improvement items). Unique toolResult-as-role pattern. Persistent gateway requires container isolation. Multi-scenario sessions merge into one JSONL (adapter splits them).

### `ollama launch` as universal bridge

Ollama provides `ollama launch <framework>` for Claude Code, Codex, Goose, Pi, OpenCode, Droid, and OpenClaw. This patches each framework's config to route through Ollama, enabling same-model cross-framework comparison for 7 frameworks without manual configuration. Gemini CLI and Vibe use native APIs (Gemini and Mistral respectively).

**Implication for spec:** The `environment.provider_hint` field should note when traffic is Ollama-routed vs native. Currently we set "openai-compatible" for Ollama-backed frameworks, but "ollama" would be more precise.
