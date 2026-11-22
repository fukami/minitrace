#!/usr/bin/env python3
"""Codex session/exec JSONL → minitrace-DSL container preprocessor.

Reads a single Codex capture and emits a JSON array of exactly one
container (one agent-session trace) consumed by codex.dsl.yaml.

Two native source formats are supported via field-presence dispatch:

  1. session (rollout) — ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
     Record types: session_meta, turn_context, response_item, event_msg.
     Rich: system prompt, model, sandbox/approval policy, token usage,
     the canonical user prompt. This is what a fresh capture grabs and
     the preferred source when available.

  2. exec — stdout of `codex exec --json` (the first-run S1..S5 corpus
     stores this as output.jsonl). Record types: thread.started,
     turn.started, turn.completed, item.started, item.completed
     (item types: reasoning, command_execution, agent_message, error).
     Thinner: no user prompt, no system prompt, no token usage. Some
     runs end with no agent_message at all (e.g. S2); such runs still
     produce a synthetic closing assistant turn so the trace is valid.

Codex routes all tool use through `exec_command` (a shell command). The
operation_type is inferred from the command string, and the primary
file path is best-effort extracted from it. These classifiers are ported
verbatim from the pre-v0.3.0 hand-written adapter (proven against the
2026-03 corpus); the DSL emits the pre-computed values directly.

Usage:
    python preprocess_codex.py <input.jsonl> <output.json> \
        [--framework-meta framework-meta.json] [--user-prompt TEXT]

--framework-meta supplies env overrides (model, cli_version, cwd) for
captures whose native stream omits them (the thin exec format). It is
never used to fabricate conversation content, only session metadata the
capturing harness recorded out-of-band.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timestamps import take_refused, to_spec_utc, uuid7_time  # noqa: E402
from source_reader import (  # noqa: E402
    count_sum, input_count, load_json_file_reporting, nested_json_loads, read_jsonl_records,
    result_as_text, take_nested_refused)

ADAPTER_SOURCE_SESSION = "codex-session-jsonl-v1"
ADAPTER_SOURCE_EXEC = "codex-exec-jsonl-v1"


# The rollout decides every value's type. A value of the wrong type must not
# abort the export; these read it as absent, the same as a missing key (as the
# claude-code pre-processor does; ruled 2026-10-27).
def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _list(v) -> list:
    return v if isinstance(v, list) else []


# ---------------------------------------------------------------------------
# Classification (ported verbatim from the pre-v0.3.0 codex adapter)

# A command separator (; & && | ||), a line break, command substitution ($(
# or a backtick) or an input redirect or process substitution (<): any of
# them means more than one verb may run.
SHELL_COMPOUND = re.compile(r"[;&|<\n\r`]|\$\(")


def classify_operation_from_command(cmd: str | None) -> str:
    """Classify minitrace operation_type from a shell command string.

    Codex routes all tool use through `exec_command`; the command string
    determines the real operation. Output redirection is checked first: a
    command that writes to a file is a write regardless of its verb. A
    command that is not a string is EXECUTE, not a crash.
    """
    if not cmd or not isinstance(cmd, str):
        return "EXECUTE"

    cmd_lower = cmd[:1024].strip().lower()

    if ">>" in cmd:
        return "MODIFY"
    if ">" in cmd:
        return "NEW"
    if re.search(r"^tee\s", cmd_lower):
        return "NEW"
    if re.search(r"^(touch|mkdir|cp)\s", cmd_lower):
        return "NEW"

    modify_patterns = [
        r"^sed\s+-i", r"^perl\s+-i",
        r"^patch\s", r"^git\s+apply\b",
        r"^chmod\s", r"^chown\s",
    ]
    for pat in modify_patterns:
        if re.search(pat, cmd_lower):
            return "MODIFY"

    # Ruled 2026-10-21: a command that may run more than one verb is not READ
    # (cat foo; rm bar). Checked on the whole string, quoted text included:
    # there is no shell parser here.
    if SHELL_COMPOUND.search(cmd):
        return "EXECUTE"

    read_patterns = [
        r"^cat\s", r"^head\s", r"^tail\s", r"^less\s", r"^more\s",
        r"^find\s", r"^ls\s", r"^tree\s", r"^wc\s",
        r"^grep\s", r"^rg\s", r"^ag\s", r"^ack\s",
        r"^file\s", r"^stat\s", r"^du\s", r"^df\s",
        r"^git\s+(log|show|diff|status|branch|blame)\b",
        r"^python3?\s+-c\s+.*open.*read",
    ]
    for pat in read_patterns:
        if re.search(pat, cmd_lower):
            return "READ"

    return "EXECUTE"


def classify_function(func_name: str, arguments: Any = None) -> str:
    """Classify operation_type from a Codex function name and arguments."""
    if func_name == "exec_command":
        cmd = ""
        if isinstance(arguments, dict):
            cmd = arguments.get("cmd", "")
        elif isinstance(arguments, str):
            # Counted where the record is built (build_tool_call_record).
            parsed = nested_json_loads(arguments, None, count=False)
            cmd = parsed.get("cmd", "") if isinstance(parsed, dict) else arguments
        return classify_operation_from_command(cmd)

    mapping = {
        "read_file": "READ",
        "write_file": "NEW",
        "edit_file": "MODIFY",
        "apply_patch": "MODIFY",
        "apply_diff": "MODIFY",
    }
    return mapping.get(func_name, "OTHER")


def extract_file_path_from_command(cmd: str | None) -> str | None:
    """Best-effort primary file path from a shell command string.

    Returns None when extraction is unreliable. For multi-file commands
    returns only the first (or, for cp/mv, the destination) argument; None
    for a command that is not a string.
    """
    if not cmd or not isinstance(cmd, str):
        return None
    cmd_stripped = cmd.strip()
    if not cmd_stripped:
        return None

    redir = re.search(r'>{1,2}\s*(\S+)\s*$', cmd_stripped)
    if redir:
        return redir.group(1)

    tee = re.search(r'\|\s*tee\s+(?:-a\s+)?(\S+)', cmd_stripped)
    if tee:
        return tee.group(1)

    m = re.match(
        r'^(?:cat|head|tail|less|more|wc|file|stat)\s+'
        r'(?:-\S+\s+(?:\d+\s+)?)*'
        r'(\S+)',
        cmd_stripped,
    )
    if m and not m.group(1).startswith('-'):
        return m.group(1)

    m = re.match(r'^(?:touch|mkdir)\s+(?:-\S+\s+)*(\S+)', cmd_stripped)
    if m:
        return m.group(1)

    m = re.match(r'^(?:cp|mv)\s+(?:-\S+\s+)*(\S+)\s+(\S+)', cmd_stripped)
    if m:
        return m.group(2)

    target = sed_inplace_target(cmd_stripped)
    if target:
        return target

    m = re.match(r'^(?:chmod|chown)\s+(?:-\S+\s+)*\S+\s+(\S+)', cmd_stripped)
    if m:
        return m.group(1)

    return None


_SED_INPLACE_HEAD = re.compile(r'^sed\s+-i\S*')


def sed_inplace_target(cmd: str) -> str | None:
    r"""Last token of a `sed -i...` command: the file it edits.

    Same result on single-line commands as the regex
    r'^sed\s+-i[^\s]*\s+.*\s+(\S+)\s*$' (checked by fuzzing against it),
    without its backtracking, which was cubic in a run of spaces: one tool
    call in a crafted log hung the conversion for minutes.
    """
    m = _SED_INPLACE_HEAD.match(cmd)
    if not m:
        return None
    rest = cmd[m.end():]
    body = rest.rstrip()
    if not rest or not rest[0].isspace() or not body:
        return None
    i = len(body)
    while i > 0 and not body[i - 1].isspace():
        i -= 1
    gap = body[:i]
    if len(gap) < 2 or not gap[0].isspace() or not gap[-1].isspace():
        return None
    return body[i:]


def classify_content_origin(func_name: str) -> str | None:
    """ToolCall.output.content_origin for a Codex tool call.

    Shell-mediated tool use collapses content_origin to 'local_exec' for
    exec_command (spec footnote [1]): a `curl` returns local_exec, not web.
    Non-shell functions (not yet observed) map to their structural origin.
    """
    if func_name == "exec_command":
        return "local_exec"
    mapping = {
        "read_file": "local_file",
        "write_file": "model_echo",
        "edit_file": "model_echo",
        "apply_patch": "model_echo",
        "apply_diff": "model_echo",
    }
    return mapping.get(func_name)


# ---------------------------------------------------------------------------
# Helpers

def add_token(tokens: dict, key: str, v: Any) -> None:
    """Add a token count from input to tokens[key]. An unusable count (e.g. a
    number the reader kept as its source text, DSL spec §4) or a sum outside
    the §3.4 domain makes the total null, never 0, and is reported."""
    tokens[key] = count_sum(tokens[key], input_count(v, f"codex {key} tokens"), f"codex {key} tokens")


def parse_jsonl(path: Path) -> list[dict]:
    # Through the shared reader: a damaged line is skipped, counted and
    # reported, not dropped silently; encoding damage is repaired.
    return read_jsonl_records(path)


def parse_exec_command_output(output: str) -> tuple[str, int | None, int | None]:
    """Split a Codex function_call_output.output blob.

    The blob is "Chunk ID: X\\nWall time: N seconds\\nProcess exited with
    code N\\nOriginal token count: N\\nOutput:\\n<actual>". Returns
    (actual_output, exit_code, wall_time_ms).
    """
    if not output:
        return "", None, None
    exit_code: int | None = None
    wall_time_ms: int | None = None
    output_lines: list[str] = []
    output_started = False
    for line in output.split("\n"):
        if output_started:
            output_lines.append(line)
        elif line.startswith("Output:"):
            output_started = True
            rest = line[len("Output:"):].strip()
            if rest:
                output_lines.append(rest)
        elif line.startswith("Process exited with code "):
            try:
                exit_code = int(line.split("code ")[-1].strip())
            except ValueError:
                pass
        elif line.startswith("Wall time: "):
            try:
                secs = float(line.split(": ")[1].split(" ")[0])
                wall_time_ms = int(secs * 1000)
            except (ValueError, IndexError):
                pass
    actual = "\n".join(output_lines) if output_started else output
    return actual, exit_code, wall_time_ms


def build_tool_call_record(call_id: str, func_name: str, raw_args: Any) -> dict:
    """Create a tool_call record (result attached later by call_id)."""
    args: dict = {}
    if isinstance(raw_args, str):
        # Text that is not a JSON object is kept as {"raw": text} (and in
        # raw_arguments), so nothing is lost and nothing is reported.
        parsed = nested_json_loads(raw_args, None, report_malformed=False)
        args = parsed if isinstance(parsed, dict) else {"raw": raw_args}
    elif isinstance(raw_args, dict):
        args = raw_args

    cmd = args.get("cmd", "") if isinstance(args, dict) else ""
    justification = args.get("justification") if isinstance(args, dict) else None

    # 0.132.0+ --oss rollouts record function_call.name as an EMPTY string
    # (March-era rollouts carried "exec_command"). The call is still
    # unambiguously the shell when its arguments carry a cmd string, so
    # classification dispatches on argument shape; codex_function below keeps
    # the raw recorded name (empty stays observable).
    effective_name = func_name
    if not func_name and cmd:
        effective_name = "exec_command"

    return {
        "id": call_id,
        "tool_name": effective_name,
        "operation_type": classify_function(effective_name, args),
        "command": cmd if effective_name == "exec_command" else None,
        "file_path": (extract_file_path_from_command(cmd)
                      if effective_name == "exec_command" else None),
        "arguments": args if isinstance(args, dict) else {},
        "raw_arguments": raw_args if isinstance(raw_args, str) else json.dumps(args or {}),
        "content_origin": classify_content_origin(effective_name),
        "codex_function": func_name,
        "justification": justification,
        # result fields, filled on function_call_output
        "success": None,
        "result": "",
        "error": None,
        "duration_ms": None,
        "has_result": False,
    }


def attach_output(tc: dict, output: str) -> None:
    actual, exit_code, wall_time_ms = parse_exec_command_output(output)
    tc["result"] = actual
    tc["success"] = (exit_code == 0) if exit_code is not None else True
    tc["error"] = actual[:1024] if (exit_code is not None and exit_code != 0) else None
    tc["duration_ms"] = wall_time_ms
    tc["has_result"] = True


# ---------------------------------------------------------------------------
# Session (rollout) parser

def parse_session_jsonl(records: list[dict]) -> dict:
    """Parse the rich rollout format into container fields + messages."""
    meta: dict[str, Any] = {}
    messages: list[dict] = []
    all_ts: list[str] = []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}

    pending_tools: list[dict] = []      # tool_calls awaiting their agent_message
    tools_by_id: dict[str, dict] = {}   # call_id -> tool_call (for output attach)
    outputs_seen: set[str] = set()
    thinking: list[str] = []            # reasoning accumulated for next assistant turn
    last_assistant_msg_ref: dict | None = None  # for token_count attribution

    def flush_assistant(content: str, ts: str | None) -> dict:
        nonlocal pending_tools, thinking
        rec = {
            "event_kind": "assistant_turn",
            "content_text": content or "",
            "thinking": "\n".join(thinking) if thinking else None,
            "tool_calls": pending_tools,
            "timestamp": ts,
            "usage": None,
        }
        messages.append(rec)
        pending_tools = []
        thinking = []
        return rec

    for rec in records:
        rtype = rec.get("type", "")
        # Codex writes milliseconds (...:06.816Z); spec §3.5 requires six digits.
        # This is the only native timestamp read, so every emitted timestamp
        # (turns, annotations, timing via created_at) inherits the form.
        ts = to_spec_utc(rec.get("timestamp"))
        if isinstance(ts, str):
            all_ts.append(ts)
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue

        if rtype == "session_meta":
            meta["session_id"] = payload.get("id")
            meta["cwd"] = payload.get("cwd")
            meta["cli_version"] = payload.get("cli_version")
            meta["originator"] = payload.get("originator")
            meta["model_provider"] = payload.get("model_provider")
            base_inst = payload.get("base_instructions")
            if isinstance(base_inst, dict) and base_inst.get("text"):
                meta["system_prompt"] = base_inst["text"]
            elif isinstance(base_inst, str):
                meta["system_prompt"] = base_inst
            continue

        if rtype == "turn_context":
            meta["model"] = payload.get("model") or meta.get("model")
            meta["approval_policy"] = payload.get("approval_policy")
            sp = payload.get("sandbox_policy")
            meta["sandbox_policy"] = sp.get("type") if isinstance(sp, dict) else sp
            meta["personality"] = payload.get("personality")
            meta["timezone"] = payload.get("timezone")
            cm = payload.get("collaboration_mode")
            if isinstance(cm, dict):
                meta["collaboration_mode"] = cm.get("mode")
                settings = cm.get("settings")
                if isinstance(settings, dict):
                    meta["reasoning_effort"] = settings.get("reasoning_effort")
            continue

        if rtype == "event_msg":
            evt = payload.get("type", "")
            if evt == "task_started":
                meta["context_window"] = payload.get("model_context_window")
            elif evt == "user_message":
                messages.append({
                    "event_kind": "user_text",
                    "content_text": _str(payload.get("message")),
                    "timestamp": ts,
                })
            elif evt == "agent_reasoning":
                thinking.append(_str(payload.get("text")))
            elif evt == "agent_message":
                last_assistant_msg_ref = flush_assistant(_str(payload.get("message")), ts)
            elif evt == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    last = info.get("last_token_usage")
                    if isinstance(last, dict):
                        add_token(tokens, "input", last.get("input_tokens"))
                        add_token(tokens, "output", last.get("output_tokens"))
                        add_token(tokens, "cache_read", last.get("cached_input_tokens"))
                        add_token(tokens, "reasoning", last.get("reasoning_output_tokens"))
                        if last_assistant_msg_ref is not None and last_assistant_msg_ref.get("usage") is None:
                            last_assistant_msg_ref["usage"] = {
                                "input_tokens": last.get("input_tokens"),
                                "output_tokens": last.get("output_tokens"),
                                "cache_read_tokens": last.get("cached_input_tokens"),
                                "reasoning_tokens": last.get("reasoning_output_tokens"),
                            }
            continue

        if rtype == "response_item":
            ptype = payload.get("type", "")
            if ptype == "message":
                # response_item/message duplicates event_msg content
                # (user_message/agent_message) or is framework injection
                # (developer/system). The event_msg channel is canonical.
                continue
            elif ptype == "reasoning":
                for s in _list(payload.get("summary")):
                    if isinstance(s, dict) and _str(s.get("text")):
                        thinking.append(s["text"])
            elif ptype == "function_call":
                call_id = _str(payload.get("call_id")) or f"tc-codex-{len(tools_by_id):04d}"
                name = payload.get("name", "unknown")
                tc = build_tool_call_record(
                    # A falsy name (None, 0, [], ...) passes as before: a call
                    # with a command is then read as exec_command. Only a
                    # truthy non-string reads as the absent key's "unknown".
                    call_id, name if isinstance(name, str) or not name else "unknown",
                    payload.get("arguments", "{}"),
                )
                pending_tools.append(tc)
                tools_by_id[call_id] = tc
            elif ptype == "function_call_output":
                call_id = _str(payload.get("call_id"))
                tc = tools_by_id.get(call_id)
                if tc is None:
                    print(f"warning: function_call_output for unknown call_id {call_id!r} "
                          f"is not in the trace", file=sys.stderr)
                else:
                    if call_id in outputs_seen:
                        print(f"warning: second function_call_output for call_id {call_id!r}",
                              file=sys.stderr)
                    outputs_seen.add(call_id)
                    attach_output(tc, _str(payload.get("output")))
            continue

    # Trailing tools/thinking with no closing agent_message → synthetic turn.
    if pending_tools or thinking:
        flush_assistant("", all_ts[-1] if all_ts else None)

    return _assemble_container(
        meta, messages, all_ts, tokens, ADAPTER_SOURCE_SESSION,
    )


# ---------------------------------------------------------------------------
# Exec parser (codex exec --json stdout)

def parse_exec_jsonl(records: list[dict]) -> dict:
    """Parse the thin exec streaming format into container fields + messages."""
    meta: dict[str, Any] = {}
    messages: list[dict] = []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}

    pending_tools: list[dict] = []
    thinking: list[str] = []
    results_as_json = 0

    def flush_assistant(content: str) -> None:
        nonlocal pending_tools, thinking
        messages.append({
            "event_kind": "assistant_turn",
            "content_text": content or "",
            "thinking": "\n".join(thinking) if thinking else None,
            "tool_calls": pending_tools,
            "timestamp": None,
            "usage": None,
        })
        pending_tools = []
        thinking = []

    for rec in records:
        rtype = rec.get("type", "")
        if rtype == "thread.started":
            meta["session_id"] = rec.get("thread_id")
            continue
        if rtype == "turn.completed":
            usage = rec.get("usage")
            if isinstance(usage, dict):
                add_token(tokens, "input", usage.get("input_tokens"))
                add_token(tokens, "output", usage.get("output_tokens"))
                add_token(tokens, "cache_read", usage.get("cached_input_tokens"))
            continue
        if rtype not in ("item.started", "item.completed"):
            continue
        item = rec.get("item")
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        item_id = item.get("id", f"item-{len(pending_tools)}")

        if rtype != "item.completed":
            continue

        if itype == "error":
            messages.append({
                "event_kind": "error",
                "message": item.get("message", "") or "",
                "timestamp": None,
            })
        elif itype == "reasoning":
            text = _str(item.get("text"))
            if text:
                thinking.append(text)
        elif itype == "command_execution":
            cmd = item.get("command", "") or ""
            # output.result is a string or null (v0.2.0): a non-string output
            # is written as JSON text and counted (ruled 2026-11-08); absent
            # or null stays the empty string, as before.
            output, structured = result_as_text(item.get("aggregated_output"))
            results_as_json += structured
            output = "" if output is None else output
            exit_code = item.get("exit_code")
            status = item.get("status", "")
            tc = build_tool_call_record(item_id, "exec_command", {"cmd": cmd})
            tc["result"] = output
            tc["success"] = (exit_code == 0) if exit_code is not None else (status == "completed")
            tc["error"] = None
            tc["duration_ms"] = None
            tc["has_result"] = True
            tc["exec_exit_code"] = exit_code
            tc["exec_status"] = status
            pending_tools.append(tc)
        elif itype == "agent_message":
            flush_assistant(item.get("text", "") or "")

    # Exec runs frequently end without an agent_message (e.g. S2): emit a
    # synthetic closing assistant turn so trailing tools/reasoning are not
    # dropped and the trace has >= 1 turn.
    if pending_tools or thinking:
        flush_assistant("")

    if results_as_json:
        print(f"warning: codex: {results_as_json} command output(s) not a string, written as JSON text "
              f"(output.result is a string or null)", file=sys.stderr)
    return _assemble_container(
        meta, messages, [], tokens, ADAPTER_SOURCE_EXEC,
    )


# ---------------------------------------------------------------------------
# Container assembly (shared)

def _assemble_container(meta: dict, messages: list[dict], all_ts: list[str],
                        tokens: dict, source_format: str) -> dict:
    # A session id counts only as a non-empty string; anything else is absent.
    source_session_id = _str(meta.get("session_id")) or None
    session_id = source_session_id or "codex-unknown"
    cli_version = meta.get("cli_version")
    version_label = cli_version or "unknown"

    mp = meta.get("model_provider") or ""
    if mp in ("ollama", "openai", ""):
        provider_hint = "openai-compatible"
    else:
        provider_hint = mp

    ap = meta.get("approval_policy")
    if not isinstance(ap, str):
        ap = None
    autonomy = {"never": "full-auto", "always": "suggest"}.get(ap, ap) if ap else None

    sp = meta.get("sandbox_policy")
    sandbox: bool | None = None
    if sp == "danger-full-access":
        sandbox = False
    elif sp and "sandbox" in str(sp).lower():
        sandbox = True

    tools_enabled = sorted({
        tc["tool_name"]
        for m in messages if m.get("event_kind") == "assistant_turn"
        for tc in m.get("tool_calls", [])
    })

    started = all_ts[0] if all_ts else None
    ended = all_ts[-1] if all_ts else None
    # Spec §3.5.1 fallback. The exec format records no wall-clock time at all,
    # but its thread_id is a UUIDv7, whose first 48 bits are the thread's
    # creation time: the session start, derived from the id rather than read
    # from a timestamp field. Checked on 0.114.0 s1, which has both: the
    # UUIDv7 time precedes the first recorded event by 35 ms. Used only when
    # nothing is recorded; the basis is carried into provenance so a reader
    # never takes it for a recorded time. No end time is derived.
    created_at_basis = "source_timestamp" if started else "absent"
    if started is None:
        started = uuid7_time(source_session_id)
        if started is not None:
            created_at_basis = "uuidv7:thread_id"

    # session_init anchor (messages[0]) for once-per-trace emissions.
    session_init = {"event_kind": "session_init", "timestamp": started}

    title = None
    for m in messages:
        if m.get("event_kind") == "user_text" and m.get("content_text"):
            title = m["content_text"].strip().split("\n", 1)[0][:120]
            break
    if not title:
        title = f"codex session {session_id[:12]}"

    total_tokens = count_sum(tokens["input"], tokens["output"], "codex total tokens")

    return {
        "session_id": session_id,
        "trace_id": f"codex-{session_id}",
        "trace_title": title,
        "source_format": source_format,
        "source_format_versioned": f"{source_format}/{version_label}",
        "cli_version": cli_version,
        "version_label": version_label,
        "model": meta.get("model"),
        "model_provider": meta.get("model_provider"),
        "provider_hint": provider_hint,
        "cwd": meta.get("cwd"),
        "originator": meta.get("originator"),
        "system_prompt": meta.get("system_prompt"),
        "approval_policy": ap,
        "autonomy_level": autonomy,
        "sandbox": sandbox,
        "sandbox_policy": sp,
        "personality": meta.get("personality"),
        "collaboration_mode": meta.get("collaboration_mode"),
        "reasoning_effort": meta.get("reasoning_effort"),
        "timezone": meta.get("timezone"),
        "context_window": meta.get("context_window"),
        "tools_enabled": tools_enabled,
        "created_at": started,
        "created_at_basis": created_at_basis,
        "updated_at": ended,
        "total_input_tokens": tokens["input"],
        "total_output_tokens": tokens["output"],
        "total_reasoning_tokens": tokens["reasoning"],
        "total_cache_read_tokens": tokens["cache_read"],
        "total_tokens": total_tokens,
        "messages": [session_init] + messages,
    }


# ---------------------------------------------------------------------------
# Format detection + framework-meta overrides

def detect_format(records: list[dict]) -> str:
    for rec in records[:5]:
        if rec.get("type") == "session_meta":
            return "session"
        if rec.get("type") == "thread.started":
            return "exec"
    return "session" if any(r.get("type") == "response_item" for r in records) else "exec"


def apply_framework_meta(container: dict, fm_path: Path) -> None:
    """Fill session-metadata gaps from a capture-side framework-meta.json.

    Only fills fields the native stream left None; never overwrites content.
    """
    fm = load_json_file_reporting(fm_path)
    if not isinstance(fm, dict):
        return
    aliases = {
        "model": ("model", "model_id"),
        "cli_version": ("cli_version", "agent_version", "version"),
        "cwd": ("cwd", "working_directory"),
        "model_provider": ("model_provider", "provider"),
    }
    for field, keys in aliases.items():
        if container.get(field):
            continue
        for k in keys:
            if fm.get(k):
                container[field] = fm[k]
                break
    if container.get("cli_version") and container.get("version_label") in (None, "unknown"):
        container["version_label"] = container["cli_version"]
        container["source_format_versioned"] = f"{container['source_format']}/{container['cli_version']}"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Codex session/exec JSONL → minitrace DSL container preprocessor",
    )
    p.add_argument("input_path", type=Path, help="Codex rollout or exec JSONL file")
    p.add_argument("output_json", type=Path, help="Output JSON array path")
    p.add_argument("--framework-meta", type=Path, default=None,
                   help="framework-meta.json for env overrides (thin exec captures)")
    p.add_argument("--user-prompt", default=None,
                   help="Operator-supplied user prompt for exec captures that omit "
                        "it (recorded as a user turn; NOT fabricated conversation).")
    args = p.parse_args(argv[1:])

    if not args.input_path.exists():
        print(f"error: input not found: {args.input_path}", file=sys.stderr)
        return 2

    records = parse_jsonl(args.input_path)
    if not records:
        print(f"error: no records in {args.input_path}", file=sys.stderr)
        return 3

    fmt = detect_format(records)
    take_refused()
    container = (parse_session_jsonl(records) if fmt == "session"
                 else parse_exec_jsonl(records))
    # A timestamp written as null is lost input (ruled 2026-10-29).
    if nulled := take_refused():
        print(f"warning: codex: {args.input_path}: {nulled} timestamp(s) not convertible to the §3.5 form, "
              f"written as null", file=sys.stderr)

    if args.user_prompt and not any(
        m.get("event_kind") == "user_text" for m in container["messages"]
    ):
        # Insert right after session_init so it precedes assistant turns.
        container["messages"].insert(1, {
            "event_kind": "user_text",
            "content_text": args.user_prompt,
            "timestamp": container.get("created_at"),
        })
        if container["trace_title"].startswith("codex session "):
            container["trace_title"] = args.user_prompt.strip().split("\n", 1)[0][:120]

    if args.framework_meta:
        apply_framework_meta(container, args.framework_meta)
    refused = take_nested_refused()
    if refused:
        container["nested_json_refused"] = refused

    args.output_json.write_text(
        json.dumps([container], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    n_turns = sum(1 for m in container["messages"]
                  if m.get("event_kind") in ("user_text", "assistant_turn"))
    n_tools = sum(len(m.get("tool_calls", [])) for m in container["messages"]
                  if m.get("event_kind") == "assistant_turn")
    print(
        f"codex preprocess [{fmt}]: 1 container "
        f"(session={container['session_id'][:16]}, cli={container['version_label']}, "
        f"model={container['model']}, turns={n_turns}, tool_calls={n_tools}, "
        f"tokens={container['total_tokens']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main(sys.argv))
