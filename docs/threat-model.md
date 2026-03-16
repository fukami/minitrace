# minitrace Adapter Threat Model

**Scope:** 9 adapters + minitrace_common.py + validate-minitrace.py

## 1. System Context

minitrace adapters convert session data from AI coding assistants into a
normalized JSON format. The adapters:

- **Read** session data from local framework stores (JSONL, JSON, SQLite)
- **Parse** untrusted content (user prompts, model responses, tool I/O)
- **Write** normalized minitrace JSON to an output directory
- **Do not** execute parsed content, make network calls, or modify source data

Threat actors: Anyone who can place files in the framework session stores that
the adapters read (e.g., `~/.claude/projects/`, `~/.codex/sessions/`,
`~/.local/share/goose/sessions/sessions.db`).

## 2. Attack Surface Per Adapter

### 2.1 minitrace_common.py (shared by all)

| Surface | Function | Risk |
|---------|----------|------|
| JSONL parsing | `parse_jsonl()` | Reads line-by-line, `json.loads()` per line. Silent skip on bad lines. |
| Content truncation | `truncate_content()` | Calls `str(content)` on arbitrary input, then `len(.encode("utf-8"))`. |
| Path normalization | `normalize_path()` | Home directory prefix stripping only. No path validation. |
| **File write** | **`write_session()`** | **Session ID used directly in output filename. No sanitization.** |
| Manifest write | `write_manifests()` | Period string (YYYY-MM) from timestamps used in directory name. |
| Hash computation | `truncate_content()` | SHA-256 on content. No algorithmic complexity concern. |

### 2.2 Claude Code Adapter

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| File discovery | `~/.claude/projects/` | `rglob("*.jsonl")` follows symlinks |
| JSONL parsing | Session JSONL files | Via `parse_jsonl()` |
| Content block parsing | `message.content[]` blocks | Dict access, type checking |
| Tool input extraction | `block.input.file_path` | Stored as-is in output |
| Subagent file discovery | `*/subagents/*.jsonl` | Path component matching |

### 2.3 Codex Adapter

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| JSONL parsing | `~/.codex/sessions/` | Via `parse_jsonl()` |
| Command classification | `arguments.cmd` string | Regex matching (read-only) |
| Argument parsing | `payload.arguments` string | `json.loads()` on string field |
| Output parsing | `function_call_output.output` | String splitting for exit code/wall time |

### 2.4 Goose Adapter (SQLite)

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| **Database open** | `sessions.db` path | `sqlite3.connect()` on user-specified path |
| **Query execution** | Session/message tables | Parameterized queries (safe) |
| **Discovery mode** | Table enumeration | **f-string interpolation for table names** |
| JSON parsing | `content_json` column | `json.loads()` on DB column value |
| Model config | `model_config_json` column | `json.loads()` on DB column value |

### 2.5 OpenCode Adapter (SQLite)

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| Database open | `opencode.db` path | `sqlite3.connect()` on user-specified path |
| Query execution | session/message/part tables | Parameterized queries (safe) |
| **Discovery mode** | Table enumeration | **f-string for table names (not present, safer than Goose)** |
| JSON parsing | `message.data`, `part.data` columns | `json.loads()` on DB column value |

### 2.6 Pi Adapter

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| File discovery | `~/.pi/agent/sessions/` | `rglob("*.jsonl")` follows symlinks |
| JSONL parsing | Session JSONL files | Via `parse_jsonl()` |
| Content block parsing | Multiple block type handlers | Dict access with type dispatch |

### 2.7 Droid Adapter

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| File discovery | `~/.factory/sessions/` | `rglob("*.jsonl")` follows symlinks |
| JSONL parsing | Session JSONL files | Via `parse_jsonl()` |
| **Settings file read** | `.settings.json` companion | **Path derived from JSONL path via string replace** |
| CWD extraction | User message content | String parsing for "Current working directory:" |

### 2.8 Gemini CLI Adapter

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| File discovery | `~/.gemini/tmp/` | `rglob("session-*.json")` |
| **Full JSON parse** | Session JSON files | **`json_path.read_text()` loads entire file** |
| Embedded tool results | `toolCalls[].result[].functionResponse` | Nested dict traversal |

### 2.9 Vibe Adapter

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| Directory discovery | `~/.vibe/logs/session/` | Directory iteration |
| **Meta JSON parse** | `meta.json` per session | **`meta_path.read_text()` loads entire file** |
| JSONL parsing | `messages.jsonl` per session | Via `parse_jsonl()` |
| Tool argument parsing | `function.arguments` string | `json.loads()` on string |

### 2.10 OpenClaw Adapter

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| File discovery | `~/.openclaw/agents/` | `glob("*.jsonl")` |
| JSONL parsing | Session JSONL files | Via `parse_jsonl()` |
| Multi-scenario splitting | User message boundaries | Record grouping logic |
| Session index | `sessions.json` | `json.loads()` (discovery mode only) |

### 2.11 validate-minitrace.py

| Surface | Input Source | Mechanism |
|---------|-------------|-----------|
| File parsing | Any `.minitrace.json` | `json.load()` on file |
| Directory glob | `--dir` argument | `Path.glob()` for file discovery |
| Validation logic | Parsed JSON data | Dict access, type checking (read-only) |

## 3. Findings

### F1: Path Traversal in Output File Write [HIGH]

**Location:** `minitrace_common.py:378`

```python
file_path = out_path / f"{session['id']}.minitrace.json"
```

`session['id']` is sourced from untrusted input (framework session stores).
A crafted session ID containing `../` sequences (e.g., `../../etc/cron.d/evil`)
would write files outside the intended output directory.

**Impact:** Arbitrary file write on the filesystem, limited to JSON content.

**Remediation:** Sanitize session ID before use in file paths:
```python
safe_id = session['id'].replace('/', '_').replace('\\', '_').replace('..', '_')
# or: assert '/' not in session['id'] and '..' not in session['id']
```

### F2: SQL Injection in Goose Discovery Mode [MEDIUM]

**Location:** `goose/minitrace-goose-adapter.py:512-513`

```python
f"PRAGMA table_info({table})"
f"SELECT COUNT(*) FROM {table}"
```

Table names from `sqlite_master` are interpolated into SQL without
parameterization. A malicious Goose database with crafted table names could
inject SQL.

**Impact:** Read/write to the Goose database via the discovery mode.
Exploitation requires the attacker to control the Goose database file.

**Remediation:** Quote table names or use allowlist:
```python
db.execute(f'PRAGMA table_info("{table}")')
# or validate table name against [a-zA-Z0-9_] pattern
```

### F3: Unbounded Memory on Large Input [MEDIUM]

**Location:** All adapters.

- `parse_jsonl()` accumulates all records in a list
- Gemini adapter: `json_path.read_text()` loads entire file
- Vibe adapter: `meta_path.read_text()` loads entire file
- All adapters accumulate turns and tool_calls without limit

A multi-GB crafted input file would cause out-of-memory.

**Impact:** Denial of service (local).

**Remediation:** Add file size checks before reading, or use streaming parsers.
For the current use case (local tool), this is acceptable risk.

### F4: Symlink Following in File Discovery [LOW]

**Location:** All JSONL-based adapters using `rglob()`.

`rglob("*.jsonl")` follows symlinks by default. A symlink in the session
directory could cause the adapter to read files outside the expected scope.

**Impact:** Information disclosure (session data from unexpected locations
included in output).

**Remediation:** Add `followlinks=False` or verify resolved paths stay within
the source directory.

### F5: Silent Data Corruption on Malformed Input [LOW]

**Location:** `minitrace_common.py:468-480`

`parse_jsonl()` silently skips lines that fail `json.loads()`. A carefully
crafted file where key records are malformed could cause the adapter to produce
output that appears valid but is missing critical data.

**Impact:** Data integrity. Mitigated by quality tier assignment and validation.

### F6: Droid Settings File Path Derivation [LOW]

**Location:** `droid/minitrace-droid-adapter.py:249`

```python
settings_path = Path(str(source_path).replace(".jsonl", ".settings.json"))
```

If `source_path` contains `.jsonl` elsewhere in the path (not just the
extension), the replacement could target an unintended file.

**Impact:** Reading wrong settings file. Low severity since settings are
metadata-only.

### F7: No Input Encoding Validation [INFO]

All adapters use `encoding="utf-8", errors="replace"` which silently replaces
invalid bytes. This is the correct defensive choice but means content integrity
is not guaranteed for files with encoding issues.

### F8: No Code Execution from Parsed Content [POSITIVE]

None of the adapters use `eval()`, `exec()`, `subprocess`, or any mechanism
that would execute parsed content. Tool commands and file paths are stored
as data, never executed.

### F9: No Network Access [POSITIVE]

None of the adapters make network calls. All processing is local file/DB I/O
to local file output. No exfiltration vector exists in the adapter code.

### F10: str() Materialization Before Truncation [MEDIUM]

**Location:** `minitrace_common.py:61`

```python
text = str(content)
full_bytes = len(text.encode("utf-8"))
```

`truncate_content()` calls `str(content)` which materializes the full string
representation before measuring or truncating. If `content` is a large nested
dict/list from `json.loads()`, `str()` produces the full `repr()` first.

**Impact:** Memory exhaustion. A 100 MB JSON array becomes a multi-hundred MB
string before truncation kicks in.

**Remediation:** Check `isinstance(content, str)` first; for non-strings, use
`json.dumps()` with a size guard or truncate the input before `str()`.

### F11: Error Messages Leak Internal Paths [LOW]

**Location:** All adapters, exception handlers (e.g., `main()` functions).

```python
print(f"  ERROR {sid}: {e}", file=sys.stderr)
```

Exception messages from `json.JSONDecodeError`, `sqlite3.OperationalError`,
etc. may contain full file paths, SQL fragments, or partial content from
untrusted input.

**Impact:** Information disclosure via stderr. Low severity for a local CLI
tool, but relevant if output is captured or logged.

**Remediation:** Sanitize exception messages before logging, or log only
exception type and a generic message.

### F12: normalize_path() Missing normpath [LOW]

**Location:** `minitrace_common.py:68-75`

```python
def normalize_path(file_path, cwd=None):
    home = os.path.expanduser("~")
    if file_path.startswith(home):
        file_path = "~" + file_path[len(home):]
    return file_path
```

Does not call `os.path.normpath()`, so paths like `/Users/foo/../etc/passwd`
survive normalization and appear in output JSON with traversal sequences.

**Impact:** Information disclosure (path structure) in output. The function
is used for display purposes in output JSON, not for file operations.

### F13: Codex Regex Patterns - ReDoS Consideration [INFO]

**Location:** `codex/minitrace-codex-adapter.py:86-128`

The `classify_operation_from_command()` regex patterns are simple anchored
patterns (`^cmd\s`) without problematic backtracking. ReDoS is not a practical
concern with the current patterns, but the function operates on untrusted
input strings of unbounded length.

**Remediation:** Truncate command strings before regex matching (e.g., first
1024 chars) as a defense-in-depth measure.

### F14: Path Traversal in Manifest Period Directory [MEDIUM]

**Location:** `minitrace_common.py:373-376`, Claude adapter line 685

```python
period = started[:7] if started else "unknown"  # YYYY-MM
out_path = Path(output_dir) / "active" / period
out_path.mkdir(parents=True, exist_ok=True)
```

The `period` variable is derived from `started_at[:7]`. While
`compute_timing` -> `format_timestamp` produces safe `YYYY-MM` values,
some adapters construct period strings from raw session metadata
(e.g., `(session["timing"].get("started_at") or "")[:7]`). If `started_at`
is set to an unformatted string, `period` could contain path traversal chars.

**Impact:** Arbitrary directory creation via `mkdir(parents=True)`.

**Remediation:** Validate period matches `^\d{4}-\d{2}$` or `"unknown"`.

### F15: Token Accumulation Type Confusion [LOW]

**Location:** Multiple adapters (codex:293, goose:213, openclaw:225, etc.)

```python
token_totals["input"] += usage.get("input_tokens", 0)
```

If `input_tokens` in source data is a string (e.g., `"100"`) instead of int,
`+=` changes the accumulator from int to string concatenation. Subsequent
operations silently produce corrupted metrics.

**Remediation:** Cast: `token_totals["input"] += int(last.get("input_tokens", 0) or 0)`

### F16: Integer Overflow in Timestamp Conversion [LOW]

**Location:** `goose/minitrace-goose-adapter.py:207`, `opencode/minitrace-opencode-adapter.py:178`

Extreme epoch values (e.g., `99999999999999999`) cause `datetime.fromtimestamp()`
to raise `OverflowError` or `OSError`, crashing the adapter.

**Remediation:** Wrap `datetime.fromtimestamp()` in try/except for
`(OverflowError, OSError, ValueError)`.

### F17: Codex Double-Parse Type Confusion [LOW]

**Location:** `codex/minitrace-codex-adapter.py:382-386`

```python
args = json.loads(raw_args)  # could produce list, int, string -- not just dict
cmd = args.get("cmd", "")    # AttributeError if args is not a dict
```

**Remediation:** After `json.loads`, check `if not isinstance(args, dict)`.

### F18: _pending_turn Marker Leaks to Output [LOW]

**Location:** `codex/minitrace-codex-adapter.py:411,464-467`

The `_pending_turn` key added to tool call dicts is not removed if an exception
occurs between insertion and cleanup, leaking internal adapter state to output.

**Remediation:** Use a separate set to track pending tool calls.

### F19: Provenance source_path Discloses User Paths [INFO]

**Location:** All adapters store `str(source_path)` in provenance.

Absolute paths like `/home/username/.claude/projects/...` appear in output JSON.
`normalize_path()` exists but is only applied to tool call file paths, not provenance.

**Remediation:** Apply `normalize_path()` to provenance paths before output.

### F20: OpenCode Discovery Mode Bug [INFO]

**Location:** `opencode/minitrace-opencode-adapter.py:342`

Discovery mode SQL selects `id, title, directory, time_created` but formats
`s[2]` (directory) as a timestamp. Will crash on any OpenCode database.

**Remediation:** Fix column index to `s[3]`.

### F21: Goose SQL Injection Remediation Note [INFO]

The suggested F2 remediation of double-quoting table names is insufficient.
SQLite double-quoted identifiers can be escaped with `""`. A table name
containing `"` characters would break out. The only safe approach is to
validate table names against `^[a-zA-Z_][a-zA-Z0-9_]*$` before interpolation.

## 4. Trust Boundaries

```
┌─────────────────────────────────────────────────────┐
│ Framework Session Stores (UNTRUSTED)                │
│ ~/.claude/  ~/.codex/  ~/.goose/  ~/.pi/  etc.      │
│ Content: user prompts, model output, tool I/O       │
└────────────────────┬────────────────────────────────┘
                     │ read
                     ▼
┌─────────────────────────────────────────────────────┐
│ Adapter Code (THIS REVIEW)                          │
│ Parse → Normalize → Compute Metrics → Write         │
└────────────────────┬────────────────────────────────┘
                     │ write
                     ▼
┌─────────────────────────────────────────────────────┐
│ Output Directory (SEMI-TRUSTED)                     │
│ ./data/sessions/active/YYYY-MM/*.minitrace.json     │
│ ./data/sessions/manifest.json                       │
└─────────────────────────────────────────────────────┘
```

Key: The trust boundary is at the read stage. All content from framework
session stores must be treated as potentially malicious.

## 5. Threat Scenarios

| # | Scenario | Vector | Impact | Likelihood |
|---|----------|--------|--------|------------|
| T1 | Malicious session ID causes file write outside output dir | Crafted session store data | Arbitrary file write (JSON) | Medium |
| T2 | Crafted Goose DB with SQL injection table names | Malicious `.db` file | DB read/write in discovery mode | Low |
| T3 | Multi-GB input file causes OOM | Large crafted file | Local DoS | Low |
| T4 | Symlink in session dir reads sensitive files | Symlink in `~/.claude/` | Info disclosure in output | Low |
| T5 | Prompt injection in session content propagates | Crafted model output | Social engineering via output | Low |

## 6. Remediation Status

All 21 findings have been addressed.

| Finding | Severity | Fix | Status |
|---------|----------|-----|--------|
| F1 | HIGH | `sanitize_id()` strips path chars from session IDs | FIXED |
| F2/F21 | MEDIUM | Allowlist regex `^[a-zA-Z_][a-zA-Z0-9_]*$` for table names | FIXED |
| F3 | MEDIUM | `check_file_size()` rejects files > 100 MB | FIXED |
| F4 | LOW | `check_symlink()` rejects symlinks in `parse_jsonl` + Gemini/Vibe | FIXED |
| F5 | LOW | `parse_jsonl()` logs skipped line count to stderr | FIXED |
| F6 | LOW | Droid settings: `Path.with_suffix()` replaces string replace | FIXED |
| F7 | INFO | Accepted: `errors="replace"` is the correct defensive choice | ACCEPTED |
| F8 | POSITIVE | No code execution from parsed content | N/A |
| F9 | POSITIVE | No network access | N/A |
| F10 | MEDIUM | `truncate_content()` checks `isinstance(str)` + pre-truncate guard | FIXED |
| F11 | LOW | Error handlers log `type(e).__name__` not `str(e)` | FIXED |
| F12 | LOW | `normalize_path()` calls `os.path.normpath()` | FIXED |
| F13 | INFO | Codex regex: `cmd[:1024]` truncation before matching | FIXED |
| F14 | MEDIUM | `sanitize_period()` validates `^\d{4}-\d{2}$` | FIXED |
| F15 | LOW | `safe_int()` casts token values before accumulation | FIXED |
| F16 | LOW | `safe_fromtimestamp()` wraps overflow/error cases | FIXED |
| F17 | LOW | Codex: `isinstance(parsed, dict)` check after `json.loads` | FIXED |
| F18 | LOW | Codex: `pending_turn_tc_ids` set replaces dict marker | FIXED |
| F19 | INFO | `write_session()` normalizes provenance source_path | FIXED |
| F20 | INFO | OpenCode discovery: column index `s[3]` for time_created | FIXED |
| F21 | INFO | Goose SQL: allowlist regex (supersedes quoting suggestion) | FIXED |

