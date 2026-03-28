# minitrace Security Model

**Scope:** 11 adapters + minitrace_common.py + validate-minitrace.py + test-format-stability.py

## System Context

minitrace adapters convert session data from AI frameworks into a
normalized JSON format. The adapters:

- **Read** session data from local framework stores (JSONL, JSON, SQLite, ZIP)
- **Parse** untrusted content (user prompts, model responses, tool I/O)
- **Write** normalized minitrace JSON to an output directory
- **Do not** execute parsed content, make network calls, or modify source data

## Trust Boundaries

```
┌─────────────────────────────────────────────────────┐
│ Framework Session Stores (UNTRUSTED)                │
│ ~/.claude/  ~/.codex/  ~/.goose/  ~/.pi/  etc.      │
│ Web exports: ChatGPT ZIP, claude.ai ZIP             │
│ Content: user prompts, model output, tool I/O       │
└────────────────────┬────────────────────────────────┘
                     │ read
                     ▼
┌─────────────────────────────────────────────────────┐
│ Adapter Code                                        │
│ Parse → Normalize → Compute Metrics → Write         │
└────────────────────┬────────────────────────────────┘
                     │ write
                     ▼
┌─────────────────────────────────────────────────────┐
│ Output Directory (SEMI-TRUSTED)                     │
│ ./<output>/active/YYYY-MM/*.minitrace.json          │
│ ./<output>/manifest.json                            │
└─────────────────────────────────────────────────────┘
```

The trust boundary is at the read stage. All content from framework
session stores is treated as potentially malicious.

## Architectural Security Properties

**No code execution.** No adapter uses `eval()`, `exec()`, `subprocess`, or any
mechanism that would execute parsed content. Tool commands and file paths are
stored as data, never executed.

**No network access.** No adapter makes network calls. All processing is local
file/DB I/O to local file output.

**Read-only on source data.** Adapters only read native session data, never
modify it. Source files and databases are opened read-only.

**Content truncation.** Large tool outputs are truncated to a configurable limit
(default 10 KB) with SHA-256 hash references to the full content.

**Input size limits.** Files exceeding 100 MB are rejected before parsing.
Web export adapters enforce decompressed size limits on ZIP contents.

**Encoding safety.** All file reads use `errors="replace"` to handle malformed
UTF-8 without crashing.

## Threat Scenarios

| Scenario | Vector | Impact | Mitigation |
|----------|--------|--------|------------|
| Malicious session ID in crafted session store | Path traversal chars in ID | File write outside output dir | Session ID sanitization |
| Crafted SQLite database (Goose, OpenCode) | Malicious DB file | Potential SQL injection in discovery mode | Table name allowlist validation |
| Oversized input file | Multi-GB crafted file | Memory exhaustion (local DoS) | File size check before parsing |
| Symlinks in session directories | Symlink to sensitive files | Information disclosure in output | Symlink detection on file open |
| Prompt injection in session content | Crafted model output | Social engineering via output | Content stored as data, not executed |
| ZIP bomb in web export | Oversized ZIP file | Disk/memory exhaustion | Decompressed size limits |

## Output Considerations

Converted `.minitrace.json` files may contain:

- User prompts (potentially sensitive)
- Model responses (may include generated credentials, PII)
- Tool I/O (file contents, command output, web fetch results)
- File paths from the source system
- System prompts (may contain proprietary instructions, API keys, or internal configuration)

Users should review output before sharing and use the `classification` field
(`internal`, `public`, `confidential`, `customer-confidential`) to control
distribution scope.

## Reporting Security Issues

Report security issues via [GitHub Issues](https://github.com/fukami/minitrace/issues).
