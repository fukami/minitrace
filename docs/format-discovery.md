# Native Session Format Reference

How each supported framework stores session data natively.
All formats discovered empirically through black-box inspection of framework outputs.

---

## 1. Claude Code

**Versions tested:** 2.1.76+
**Storage:** `~/.claude/projects/<project>/<session-id>.jsonl`
**Format:** JSONL (one JSON object per line)

Two source variants:
- **JSONL v2** (current): Full transcript with system, user, assistant records
- **Dir v1** (pre-Feb 2026): `<session-id>/tool-results/*.txt` only, no transcript

### Record types (v2)

| Type | Purpose |
|------|---------|
| `system` | System prompt injection |
| `user` | Human input or tool results |
| `assistant` | Model response with content blocks |
| `progress` | Framework progress events |
| `file-history-snapshot` | File state tracking |
| `last-prompt` | Cached prompt |

### Content blocks (assistant)

| Block type | Fields |
|-----------|--------|
| `text` | `{type, text}` |
| `thinking` | `{type, thinking, signature?}` |
| `tool_use` | `{type, id, name, input}` |

Tool results arrive as user messages with `tool_result` content blocks:
`{type: "tool_result", tool_use_id, content, is_error?}`

### Subagents

Subagent transcripts stored at `<session-id>/subagents/<agent-id>.jsonl`.
Same JSONL format as parent sessions. First record contains `agentId` and `slug` fields.

### Tools

Read, Glob, Grep, Edit, Write, Bash, Agent, Task, TaskCreate, TaskUpdate,
TaskGet, TaskList, TaskOutput, TaskStop, Skill, AskUserQuestion, NotebookEdit,
WebFetch, WebSearch, ToolSearch

### Token usage

Per-turn usage in `message.usage`: `{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}`

---

## 2. Codex

**Versions tested:** 0.114.0
**Storage:** Two sources with different record structures

### Exec JSONL (`codex exec --json` stdout)

Record types: `thread.started`, `turn.started`, `turn.completed`, `item.started`, `item.completed`

Item types: `reasoning`, `command_execution`, `agent_message`, `error`

### Session JSONL (`~/.codex/sessions/YYYY/MM/DD/*.jsonl`)

Strictly richer than exec JSONL.

| Record type | Payload types |
|------------|--------------|
| `session_meta` | Session-level metadata (id, cwd, cli_version, model_provider) |
| `response_item` | `message`, `reasoning`, `function_call`, `function_call_output` |
| `event_msg` | `task_started`, `task_complete`, `user_message`, `agent_reasoning`, `agent_message`, `token_count` |
| `turn_context` | Per-turn config (model, approval_policy, sandbox_policy, personality) |

### Unique features

- **Justification field:** `function_call.arguments.justification` -- self-explanation before every tool call
- **Personality/collaboration modes:** Configurable behavioral presets
- **Single tool:** `exec_command` routes all actions through shell

### Token usage

Via `event_msg/token_count`: `{input_tokens, output_tokens, cached_input_tokens, reasoning_output_tokens}`

---

## 3. Goose (Block)

**Versions tested:** 3.27.0
**Storage:** SQLite at `~/.local/share/goose/sessions/sessions.db`
**Schema version:** 7

### Tables

| Table | Key columns |
|-------|------------|
| `sessions` | id, name, working_dir, provider_name, tokens, created_at, model_config_json |
| `messages` | id, message_id, session_id, role, content_json, created_timestamp, tokens |

### Content blocks (in content_json)

| Block type | Structure |
|-----------|-----------|
| `text` | `{type, text}` |
| `reasoning` | `{type, text}` (streamed as multiple chunks) |
| `toolRequest` | `{type, id, toolCall: {status, value: {name, arguments}}, _meta: {goose_extension}}` |
| `toolResponse` | `{type, id, toolResult: {status, value: {content: [...], isError}}}` |

### Unique features

- **MCP extension tracking:** `_meta.goose_extension` identifies which extension provided each tool
- **Built-in developer extension:** tree, read, write, edit, shell, text_editor, list_directory, search_files
- **Computercontroller extension:** screenshot, click, type_text, scroll

---

## 4. Pi

**Versions tested:** 0.58.1
**Storage:** JSONL v3 at `~/.pi/agent/sessions/<project-dir>/`
**File naming:** `YYYY-MM-DDThh-mm-ss-mmmZ_<uuid>.jsonl`

### Record types

| Type | Purpose |
|------|---------|
| `session` | `{version: 3, id, timestamp, cwd}` |
| `model_change` | `{provider, modelId}` |
| `thinking_level_change` | `{thinkingLevel}` |
| `message` | `{message: {role, content: [blocks], usage?, api?, provider?, model?}}` |

### Content blocks

| Block type | Structure |
|-----------|-----------|
| `text` | `{type, text}` |
| `thinking` | `{type, thinking, signature?}` |
| `toolCall` | `{type, id, name, arguments}` |
| `toolResult` | `{type, toolUseId?, content}` |

### Unique features

- **Tree-structured records:** `parentId` field links records
- **Thinking level tracking:** Explicit record for thinking level changes
- **Settings companion:** `~/.pi/agent/settings.json` persists provider config

### Token usage

Per-message usage: `{input, output, cacheRead, cacheWrite}`

---

## 5. OpenCode

**Versions tested:** 1.2.20
**Storage:** SQLite at `~/.local/share/opencode/opencode.db`

### Tables

| Table | Key columns |
|-------|------------|
| `session` | id, project_id, title, directory, version, time_created, summary_* |
| `message` | id, session_id, time_created, data (JSON) |
| `part` | id, message_id, session_id, time_created, data (JSON) |

### message.data JSON

`{role, time: {created}, agent, model: {providerID, modelID}, summary: {diffs: [...]}}`

### part.data JSON (typed content blocks)

| Part type | Structure |
|----------|-----------|
| `text` | `{type, text}` |
| `reasoning` | `{type, text, time: {start, end}}` |
| `tool` | `{type, callID, tool, state: {status, input, output}}` |
| `step-start` | `{type, snapshot}` |
| `step-finish` | `{type, reason, cost, tokens: {total, input, output, reasoning, cache: {read, write}}}` |

### Unique features

- **3-table model:** session/message/part hierarchy
- **Git snapshots:** `step-start.snapshot` / `step-finish.snapshot` track commit hashes per step
- **Per-step cost tracking:** `step-finish.cost` and detailed token breakdown

### Tools

grep, bash, read, write, edit, glob, fetch

---

## 6. Droid (Factory)

**Versions tested:** 0.74.0
**Storage:** JSONL at `~/.factory/sessions/<project-dir>/`
**File naming:** `<uuid>.jsonl` with companion `<uuid>.settings.json`

### Record types

| Type | Purpose |
|------|---------|
| `session_start` | `{id, title, sessionTitle}` |
| `message` | `{id, timestamp, message: {role, content: [blocks]}}` |

### Content blocks

| Block type | Structure |
|-----------|-----------|
| `text` | `{type, text}` |
| `thinking` | `{type, signature: "reasoning", thinking}` |
| `tool_use` | `{type, id, name, input}` |
| `tool_result` | `{type, tool_use_id, content, is_error?}` |

### Unique features

- **OpenAI-compatible format:** Uses `tool_use`/`tool_result` block naming (Anthropic-style with OpenAI naming)
- **Settings companion file:** `<uuid>.settings.json` contains model config, session defaults
- **CWD injection:** Droid injects "Current working directory:" in first user message content
- **Case-sensitive tool names:** OpenAI models get lowercase (`edit_file`), Anthropic models get Pascal case (`Edit`)

### Tools

Grep, Read, Write, Edit, Bash, Glob, Agent

---

## 7. Gemini CLI

**Versions tested:** 0.33.1
**Storage:** JSON at `~/.gemini/tmp/<project-hash>/chats/session-*.json`

### Session JSON structure

```json
{
  "kind": "session",
  "sessionId": "...",
  "startTime": "...",
  "lastUpdated": "...",
  "summary": "...",
  "projectHash": "...",
  "messages": [...]
}
```

### Message format

```json
{
  "type": "user" | "gemini",
  "id": "...",
  "timestamp": "...",
  "content": "..." | [{text: "..."}],
  "model": "...",
  "thoughts": "...",
  "tokens": {inputTokens, outputTokens, totalTokens, thoughtsTokens?},
  "toolCalls": [...]
}
```

### Tool call format

Tool calls and results are embedded in the same message:

```json
{
  "id": "...",
  "name": "grep_search",
  "args": {...},
  "result": [{
    "functionResponse": {
      "id": "...",
      "name": "...",
      "response": {"output": "..."}
    }
  }]
}
```

### Unique features

- **Inline results:** Tool call and result in the same message object (no separate result messages)
- **Per-project directories:** Session files organized by project hash
- **Native model:** Uses Gemini API directly (not through Ollama bridge)
- **Thoughts field:** Separate from content, not a content block

### Tools

grep_search, run_shell_command, read_file, write_file, edit_file, read_many_files, list_directory, web_search

---

## 8. Vibe (Mistral)

**Versions tested:** 2.4.2
**Storage:** `~/.vibe/logs/session/session_YYYYMMDD_HHMMSS_<hash>/`
**Format:** Directory with `meta.json` + `messages.jsonl`

### meta.json

```json
{
  "session_id": "...",
  "start_time": "...",
  "end_time": "...",
  "environment": {"working_directory": "..."},
  "username": "...",
  "title": "...",
  "total_messages": 42,
  "tools_available": ["bash", "grep", ...],
  "stats": {
    "session_prompt_tokens": 12345,
    "session_completion_tokens": 6789,
    "session_cost": 0.042,
    "steps": 5
  }
}
```

### messages.jsonl (OpenAI chat completion format)

| Role | Structure |
|------|-----------|
| `user` | `{role: "user", content: "..."}` |
| `assistant` | `{role: "assistant", content: "...", tool_calls: [{id, type: "function", function: {name, arguments}}]}` |
| `tool` | `{role: "tool", tool_call_id: "...", content: "..."}` |

### Unique features

- **Rich stats in meta:** Cost, tokens/sec, tool counts, step counts
- **OpenAI-compatible messages:** Standard chat completion format
- **Native Mistral model:** Uses Mistral API directly (devstral-2)
- **Structured JSON output:** `--output json` flag for automation

### Tools

grep, read_file, write_file, edit_file, bash, list_directory, task, exit_plan_mode

---

## 9. OpenClaw

**Versions tested:** 2026.3.13
**Storage:** JSONL v3 at `~/.openclaw/agents/<agent-id>/sessions/`
**File naming:** `<session-id>.jsonl` (session-id can be UUID or custom string)
**Index:** `sessions.json` maps session keys to metadata

### Record types

| Type | Purpose |
|------|---------|
| `session` | `{version: 3, id, timestamp, cwd}` |
| `model_change` | `{provider, modelId}` |
| `thinking_level_change` | `{thinkingLevel}` |
| `custom` | `{customType: "model-snapshot"\|"openclaw:prompt-error", data}` |
| `message` | `{message: {role, content: [blocks], usage?, api?, provider?, model?}}` |

### Message roles

| Role | Purpose |
|------|---------|
| `user` | Human input, content: `[{type: "text", text}]` |
| `assistant` | Model response with `text` or `toolCall` content blocks, plus usage stats |
| `toolResult` | **Top-level role** (not content block): `{toolCallId, toolName, content, isError}` |

### Unique features

- **toolResult as message role:** Unlike all other frameworks where tool results are content blocks or separate records, OpenClaw promotes toolResult to a top-level message role
- **Tree-structured records:** `parentId` field links records (similar to Pi)
- **Persistent gateway:** Runs ws:// gateway server; requires container isolation for automated runs
- **Workspace injection:** AGENTS.md, SOUL.md, TOOLS.md, IDENTITY.md injected as system context
- **Multi-session merging:** Multiple `openclaw agent` invocations with same session key append to the same JSONL file

### Token usage

Per-message usage: `{input, output, cacheRead, cacheWrite, totalTokens, cost: {input, output, total}}`

### Tools (coding profile)

read, edit, write, exec, process, cron, sessions_list, sessions_history, sessions_send, sessions_yield, sessions_spawn, subagents, session_status, web_fetch, memory_search, memory_get

---

## Cross-Framework Comparison

| Aspect | Claude Code | Codex | Goose | Pi | OpenCode | Droid | Gemini CLI | Vibe | OpenClaw |
|--------|------------|-------|-------|-----|----------|-------|-----------|------|---------|
| Storage | JSONL | JSONL | SQLite | JSONL | SQLite | JSONL | JSON | meta.json + JSONL | JSONL |
| Tool results | Separate message | Separate record | Content block | Content block | Part record | Content block | Inline | Separate message | Top-level role |
| Thinking | Content block | Separate records | Content block | Content block | Part type | Content block | Top-level field | N/A | N/A (thinking_level record) |
| Token tracking | Per-turn | Per-event | Per-session | Per-message | Per-step | N/A | Per-message | In meta stats | Per-message |
| Subagents | Yes (separate files) | No | No | No | No | No | No | Yes (task tool) | Yes (sessions_spawn) |

---

## 10. ChatGPT (Web Export)

**Source:** Data export ZIP (Settings > Data controls > Export data)
**Format:** ZIP containing `conversations.json` (JSON array of conversation objects)

Tree-based conversation structure with branching support. Each node in `mapping` has an `id`, `parent`, `children`, and `message`. The `current_node` field identifies the active branch endpoint. The adapter linearizes to the selected branch.

Model identification available via `metadata.model_slug`. Token counts not available in exports.

Full format documentation is in the adapter source: `adapters/chatgpt/minitrace-chatgpt-adapter.py`.

---

## 11. claude.ai (Web Export)

**Source:** Data export ZIP (Settings > Privacy > Export data)
**Format:** ZIP containing `conversations.json`, `users.json`, `projects.json`, `memories.json`

Linear conversation structure. Content blocks: `text`, `tool_use`, `tool_result`. Tool IDs are always null in exports -- pairing is positional (tool_use at position i, tool_result at i+1).

Model identifier and token counts are not included in exports.

Full format documentation is in the adapter source: `adapters/claude-ai/minitrace-claude-ai-adapter.py`.
