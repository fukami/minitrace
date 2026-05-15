#!/usr/bin/env python3
"""
minitrace Codex adapter — converts Codex session data to minitrace v0.2.0 format.

Usage:
    # Convert a single exec JSONL (from `codex exec --json`)
    python3 minitrace-codex-adapter.py --source-jsonl output.jsonl

    # Convert persisted session JSONL (from ~/.codex/sessions/)
    python3 minitrace-codex-adapter.py --source-session ~/.codex/sessions/2026/03/15/rollout-*.jsonl

    # Batch convert all sessions in ~/.codex/
    python3 minitrace-codex-adapter.py --source-dir ~/.codex/

    # Discovery mode (inspect format)
    python3 minitrace-codex-adapter.py --discover --source-session path.jsonl

Codex native format (discovered 2026-03-15 against codex-cli 0.114.0):

  Two JSONL sources with different record structures:

  1. Exec JSONL (`codex exec --json` stdout):
     Record types: thread.started, turn.started, turn.completed,
     item.started, item.completed
     Item types: reasoning, command_execution, agent_message, error

  2. Session JSONL (`~/.codex/sessions/YYYY/MM/DD/*.jsonl`):
     Record types: session_meta, response_item, event_msg, turn_context
     response_item payload types: message, reasoning, function_call, function_call_output
     event_msg payload types: task_started, task_complete, user_message,
                              agent_reasoning, agent_message, token_count

  The session JSONL is strictly richer — it contains everything from exec JSONL
  plus system prompts, sandbox/approval policies, token usage, and framework
  metadata. This adapter supports both but prefers session JSONL when available.

  Primary tool: `exec_command` — Codex routes all tool use through shell execution.
  The function_call arguments contain {cmd, justification} where justification is
  a Codex-specific self-explanation field (not present in other frameworks).
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from minitrace_common import (
    SCHEMA_VERSION,
    build_annotation,
    build_session_skeleton,
    build_tool_call,
    build_turn,
    compute_metrics,
    compute_timing,
    compute_tool_call_context,
    deduplicate_tool_calls,
    detect_pii_in_paths,
    extract_title,
    format_timestamp,
    now_utc,
    parse_jsonl,
    parse_timestamp,
    safe_int,
    truncate_content,
    write_session,
    write_manifests,
    assign_quality_tier,
)

ADAPTER_VERSION = "minitrace-codex-adapter-0.2.0"
SOURCE_FORMAT_EXEC = "codex-exec-jsonl-v1"
SOURCE_FORMAT_SESSION = "codex-session-jsonl-v1"


# --- Operation Type Mapping ---
# Codex primarily uses exec_command (shell). The command string determines
# the actual operation type. We classify based on command patterns.

def classify_operation_from_command(cmd):
    """Classify minitrace operation_type from a shell command string.

    Codex routes all tool use through `exec_command` with a shell command.
    We infer the operation type from command patterns.
    """
    if not cmd:
        return "EXECUTE"

    # Truncate before regex to bound processing time on untrusted input
    cmd_lower = cmd[:1024].strip().lower()

    # Check for output redirection FIRST — if a command writes to a file,
    # the write is the primary operation regardless of the command name.
    # e.g., "find . -type f > files.txt" is NEW, not READ.
    if ">>" in cmd:
        return "MODIFY"
    if ">" in cmd:
        return "NEW"
    if re.search(r"^tee\s", cmd_lower):
        return "NEW"
    if re.search(r"^(touch|mkdir|cp)\s", cmd_lower):
        return "NEW"

    # Modify patterns (edit existing files)
    modify_patterns = [
        r"^sed\s+-i", r"^perl\s+-i",
        r"^patch\s", r"^git\s+apply\b",
        r"^chmod\s", r"^chown\s",
    ]
    for pat in modify_patterns:
        if re.search(pat, cmd_lower):
            return "MODIFY"

    # Read patterns (only if no output redirection)
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


def classify_function(func_name, arguments=None):
    """Classify operation_type from Codex function name and arguments.

    Primary function is exec_command. Others may appear in future Codex versions.
    """
    if func_name == "exec_command":
        cmd = ""
        if isinstance(arguments, dict):
            cmd = arguments.get("cmd", "")
        elif isinstance(arguments, str):
            try:
                args = json.loads(arguments)
                cmd = args.get("cmd", "")
            except json.JSONDecodeError:
                cmd = arguments
        return classify_operation_from_command(cmd)

    # Non-exec_command functions
    mapping = {
        "read_file": "READ",
        "write_file": "NEW",
        "edit_file": "MODIFY",
        "apply_patch": "MODIFY",
        "apply_diff": "MODIFY",
    }
    return mapping.get(func_name, "OTHER")


def extract_file_path_from_command(cmd):
    """Extract the primary file path from a shell command string.

    Best-effort extraction for common patterns. For multi-file commands
    (e.g., cat file1 file2), returns only the first file argument.
    Returns None when extraction is unreliable.
    """
    if not cmd:
        return None

    cmd_stripped = cmd.strip()
    if not cmd_stripped:
        return None

    # Output redirection: "... > file" or "... >> file"
    redir = re.search(r'>{1,2}\s*(\S+)\s*$', cmd_stripped)
    if redir:
        return redir.group(1)

    # tee target: "... | tee [-a] file"
    tee = re.search(r'\|\s*tee\s+(?:-a\s+)?(\S+)', cmd_stripped)
    if tee:
        return tee.group(1)

    # Single-target commands: cat, head, tail, less, more, wc, file, stat
    # Skip flags: -x (short), --long, -n 10 (short with space-separated value)
    m = re.match(
        r'^(?:cat|head|tail|less|more|wc|file|stat)\s+'
        r'(?:-\S+\s+(?:\d+\s+)?)*'   # skip flags (including -n 10 style)
        r'(\S+)',
        cmd_stripped,
    )
    if m and not m.group(1).startswith('-'):
        return m.group(1)

    # touch, mkdir: first non-flag argument
    m = re.match(r'^(?:touch|mkdir)\s+(?:-\S+\s+)*(\S+)', cmd_stripped)
    if m:
        return m.group(1)

    # cp/mv: destination (last argument)
    m = re.match(r'^(?:cp|mv)\s+(?:-\S+\s+)*(\S+)\s+(\S+)', cmd_stripped)
    if m:
        return m.group(2)

    # sed -i: target file (last argument)
    m = re.match(r'^sed\s+-i[^\s]*\s+.*\s+(\S+)\s*$', cmd_stripped)
    if m:
        return m.group(1)

    # chmod/chown: target file (last non-flag argument)
    m = re.match(r'^(?:chmod|chown)\s+(?:-\S+\s+)*\S+\s+(\S+)', cmd_stripped)
    if m:
        return m.group(1)

    return None


def classify_content_origin(func_name):
    """Classify ToolCall.output.content_origin for a Codex tool call.

    Codex routes all tool use through exec_command (shell execution).
    The output of every tool call is shell stdout/stderr, so the
    structural content_origin is always 'local_exec'.

    This is the spec's footnote [1] limitation: shell-mediated tool use
    collapses content_origin to a single value. A `curl` command returns
    'local_exec', not 'web'; a `cat` command returns 'local_exec', not
    'local_file'. Researchers querying content_origin='web' will miss
    Codex sessions where web content was fetched via shell.

    Future Codex versions may add non-shell tools (read_file, write_file,
    apply_patch appear in the function mapping). These would get distinct
    content_origin values if they appear.
    """
    if func_name == "exec_command":
        return "local_exec"

    # Non-exec_command functions (not yet observed in production data)
    mapping = {
        "read_file": "local_file",
        "write_file": "model_echo",
        "edit_file": "model_echo",
        "apply_patch": "model_echo",
        "apply_diff": "model_echo",
    }
    return mapping.get(func_name)


def classify_input_channel(record_type, payload):
    """Classify Turn.input_channel for a Codex JSONL record.

    Only classifies turns that the adapter actually emits:
    - event_msg/user_message turns → 'user_input'
    - event_msg/agent_message turns → None (assistant)

    Framework injection exists in the JSONL (response_item/message with
    role=developer or role=user containing <INSTRUCTIONS>/<environment_context>)
    but the adapter correctly discards these as duplicate/framework-internal
    content. tool_output is delivered via response_item/function_call_output,
    which is attached to tool calls, not emitted as turns.

    As a result, 'framework_inject' and 'tool_output' are not classifiable
    on emitted turns. This is a coverage gap inherent to Codex's message
    architecture, not a classifier limitation.
    """
    if record_type == "event_msg":
        evt_type = payload.get("type", "") if isinstance(payload, dict) else ""
        if evt_type == "user_message":
            return "user_input"
        if evt_type == "agent_message":
            return None  # assistant turns don't have an input channel
    return None


# --- Session JSONL Parser (rich format from ~/.codex/sessions/) ---

def parse_session_jsonl(records):
    """Parse Codex session JSONL records into minitrace structures.

    This is the rich format persisted in ~/.codex/sessions/YYYY/MM/DD/*.jsonl.
    Record types: session_meta, response_item, event_msg, turn_context.

    Returns: (turns, tool_calls, metadata, annotations, all_timestamps, token_totals)
    """
    turns = []
    tool_calls = []
    annotations = []
    all_timestamps = []
    token_totals = {
        "input": 0, "output": 0, "cache_read": 0, "reasoning": 0,
    }
    metadata = {
        "session_id": None,
        "model": None,
        "model_provider": None,
        "cwd": None,
        "cli_version": None,
        "originator": None,
        "system_prompt": None,
        "approval_policy": None,
        "sandbox_policy": None,
        "personality": None,
        "collaboration_mode": None,
        "reasoning_effort": None,
        "timezone": None,
        "context_window": None,
    }

    turn_index = 0
    tc_index = 0
    pending_function_calls = {}  # call_id -> tool_call dict
    pending_turn_tc_ids = set()  # tool call IDs awaiting turn assignment
    current_thinking = []  # accumulate reasoning blocks within a turn

    for rec in records:
        rtype = rec.get("type", "")
        ts_str = rec.get("timestamp")
        ts = parse_timestamp(ts_str)
        if ts:
            all_timestamps.append(ts)

        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue

        # --- session_meta: session-level metadata ---
        if rtype == "session_meta":
            metadata["session_id"] = payload.get("id")
            metadata["cwd"] = payload.get("cwd")
            metadata["cli_version"] = payload.get("cli_version")
            metadata["originator"] = payload.get("originator")
            metadata["model_provider"] = payload.get("model_provider")
            # System prompt
            base_inst = payload.get("base_instructions", {})
            if isinstance(base_inst, dict) and base_inst.get("text"):
                metadata["system_prompt"] = base_inst["text"]
            continue

        # --- turn_context: per-turn configuration state ---
        if rtype == "turn_context":
            metadata["approval_policy"] = payload.get("approval_policy")
            sp = payload.get("sandbox_policy", {})
            metadata["sandbox_policy"] = sp.get("type") if isinstance(sp, dict) else sp
            metadata["personality"] = payload.get("personality")
            metadata["timezone"] = payload.get("timezone")
            cm = payload.get("collaboration_mode", {})
            settings_model = None
            if isinstance(cm, dict):
                metadata["collaboration_mode"] = cm.get("mode")
                settings = cm.get("settings", {})
                if isinstance(settings, dict):
                    metadata["reasoning_effort"] = settings.get("reasoning_effort")
                    settings_model = settings.get("model")
            metadata["model"] = payload.get("model") or settings_model or metadata["model"]
            continue

        # --- event_msg: framework events ---
        if rtype == "event_msg":
            evt_type = payload.get("type", "")

            if evt_type == "task_started":
                metadata["context_window"] = payload.get("model_context_window")

            elif evt_type == "user_message":
                # User prompt — create a user turn
                content = payload.get("message", "")
                turns.append(build_turn(
                    index=turn_index,
                    timestamp=ts_str,
                    role="user",
                    source="human",
                    content=content,
                    input_channel=classify_input_channel(rtype, payload),
                ))
                turn_index += 1

            elif evt_type == "agent_reasoning":
                # Accumulate thinking for the current assistant turn
                current_thinking.append(payload.get("text", ""))

            elif evt_type == "agent_message":
                # Agent's final message — create an assistant turn
                content = payload.get("message", "")
                thinking = "\n".join(current_thinking) if current_thinking else None
                # Assign pending tool calls to this assistant turn
                tc_ids = []
                for tc in tool_calls:
                    if tc["id"] in pending_turn_tc_ids:
                        tc_ids.append(tc["id"])
                        tc["emitting_turn_index"] = turn_index
                pending_turn_tc_ids.clear()

                turns.append(build_turn(
                    index=turn_index,
                    timestamp=ts_str,
                    role="assistant",
                    source="model",
                    content=content,
                    tool_calls_in_turn=tc_ids,
                    thinking=thinking,
                    input_channel=classify_input_channel(rtype, payload),
                    model=metadata.get("model"),
                ))
                turn_index += 1
                current_thinking = []

            elif evt_type == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    last = info.get("last_token_usage", {})
                    if isinstance(last, dict):
                        token_totals["input"] += safe_int(last.get("input_tokens"))
                        token_totals["output"] += safe_int(last.get("output_tokens"))
                        token_totals["cache_read"] += safe_int(last.get("cached_input_tokens"))
                        token_totals["reasoning"] += safe_int(last.get("reasoning_output_tokens"))

                        # Attach usage to the most recent assistant turn
                        if turns and turns[-1]["role"] == "assistant":
                            turns[-1]["usage"] = {
                                "input_tokens": last.get("input_tokens"),
                                "output_tokens": last.get("output_tokens"),
                                "cache_read_tokens": last.get("cached_input_tokens"),
                                "cache_creation_tokens": None,
                                "reasoning_tokens": last.get("reasoning_output_tokens"),
                                "tool_tokens": None,
                            }

            continue

        # --- response_item: message/reasoning/function_call/function_call_output ---
        if rtype == "response_item":
            ptype = payload.get("type", "")

            if ptype == "message":
                role = payload.get("role", "")
                content_blocks = payload.get("content", [])

                # Extract text content
                text_parts = []
                for block in content_blocks:
                    if isinstance(block, dict):
                        if block.get("type") in ("input_text", "output_text", "text"):
                            text_parts.append(block.get("text", ""))

                content = "\n".join(text_parts) if text_parts else ""

                # Classify source
                if role in ("developer", "system"):
                    source = "framework"
                    mapped_role = "system"
                elif role == "user":
                    # In session JSONL, user messages appear both as
                    # response_item/message (role=user) and event_msg/user_message.
                    # The event_msg is the canonical user prompt. The response_item
                    # is Codex's internal message formatting (AGENTS.md, skills, etc.)
                    # which is framework-injected context, not human input.
                    source = "framework"
                    mapped_role = "system"
                else:
                    # Assistant messages appear both as response_item/message
                    # and event_msg/agent_message. The event_msg version is
                    # canonical (created with tool call linking and thinking).
                    # Skip the response_item duplicate.
                    source = "framework"
                    mapped_role = "assistant"

                # Skip all response_item/message records — they duplicate
                # content already captured from event_msg records (user_message,
                # agent_message) or are framework injections (developer, system).
                if source == "framework":
                    continue

                if content:
                    turns.append(build_turn(
                        index=turn_index,
                        timestamp=ts_str,
                        role=mapped_role,
                        source=source,
                        content=content,
                    ))
                    turn_index += 1

            elif ptype == "reasoning":
                # Reasoning blocks — accumulate for next assistant turn
                summary = payload.get("summary", [])
                for s in summary:
                    if isinstance(s, dict) and s.get("text"):
                        current_thinking.append(s["text"])

            elif ptype == "function_call":
                # Tool invocation
                func_name = payload.get("name", "unknown")
                call_id = payload.get("call_id", f"tc-codex-{tc_index:04d}")
                raw_args = payload.get("arguments", "{}")

                # Parse arguments
                args = {}
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args)
                        args = parsed if isinstance(parsed, dict) else {"raw": raw_args}
                    except json.JSONDecodeError:
                        args = {"raw": raw_args}
                elif isinstance(raw_args, dict):
                    args = raw_args

                cmd = args.get("cmd", "")
                justification = args.get("justification")

                operation_type = classify_function(func_name, args)

                # Build framework_metadata with Codex-specific fields
                fm = {"codex_function": func_name}
                if justification:
                    fm["justification"] = justification

                tc = build_tool_call(
                    tc_id=call_id,
                    turn_index=None,  # set when agent_message arrives
                    timestamp=ts_str,
                    tool_name=func_name,
                    operation_type=operation_type,
                    command=cmd if func_name == "exec_command" else None,
                    file_path=extract_file_path_from_command(cmd) if func_name == "exec_command" else None,
                    arguments=args,
                    framework_metadata=fm,
                    content_origin=classify_content_origin(func_name),
                )
                tool_calls.append(tc)
                pending_function_calls[call_id] = tc
                pending_turn_tc_ids.add(call_id)
                tc_index += 1

            elif ptype == "function_call_output":
                # Tool result
                call_id = payload.get("call_id", "")
                output = payload.get("output", "")

                tc = pending_function_calls.pop(call_id, None)
                if tc:
                    # Parse Codex output format:
                    # "Chunk ID: X\nWall time: X seconds\nProcess exited with code N\n..."
                    exit_code = None
                    wall_time_ms = None
                    actual_output = output

                    lines = output.split("\n") if output else []
                    output_started = False
                    output_lines = []
                    for line in lines:
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

                    actual_output = "\n".join(output_lines) if output_lines else output
                    truncated, full_bytes, full_hash = truncate_content(actual_output)

                    tc["output"]["result"] = truncated
                    tc["output"]["success"] = exit_code == 0 if exit_code is not None else True
                    tc["output"]["error"] = actual_output[:1024] if exit_code and exit_code != 0 else None
                    tc["output"]["duration_ms"] = wall_time_ms
                    tc["output"]["truncated"] = full_bytes is not None
                    tc["output"]["full_bytes"] = full_bytes
                    tc["output"]["full_hash"] = full_hash

    # Assign any remaining pending tool calls to the last turn
    if pending_turn_tc_ids:
        last_turn = len(turns) - 1 if turns else 0
        for tc in tool_calls:
            if tc["id"] in pending_turn_tc_ids:
                tc["emitting_turn_index"] = last_turn
        pending_turn_tc_ids.clear()

    return turns, tool_calls, metadata, annotations, all_timestamps, token_totals


# --- Exec JSONL Parser (streaming format from codex exec --json) ---

def parse_exec_jsonl(records):
    """Parse Codex exec JSONL records (from `codex exec --json` stdout).

    This is the simpler streaming format. Record types:
    thread.started, turn.started, turn.completed,
    item.started, item.completed (with item types: reasoning, command_execution,
    agent_message, error)

    Returns: (turns, tool_calls, metadata, annotations, all_timestamps, token_totals)
    """
    turns = []
    tool_calls = []
    annotations = []
    all_timestamps = []
    token_totals = {"input": 0, "output": 0, "reasoning": 0}
    metadata = {
        "session_id": None,
        "model": None,
        "model_provider": None,
        "cwd": None,
        "cli_version": None,
    }

    turn_index = 0
    tc_index = 0
    current_thinking = []

    for rec in records:
        rtype = rec.get("type", "")

        if rtype == "thread.started":
            metadata["session_id"] = rec.get("thread_id")
            continue

        if rtype in ("turn.started", "turn.completed"):
            continue

        if rtype not in ("item.started", "item.completed"):
            continue

        item = rec.get("item", {})
        if not isinstance(item, dict):
            continue

        itype = item.get("type", "")
        item_id = item.get("id", f"item-{tc_index}")

        if rtype == "item.completed":
            if itype == "error":
                msg = item.get("message", "")
                annotations.append(build_annotation(
                    ann_id=f"ann-error-{item_id}",
                    annotator="adapter",
                    scope_type="session",
                    target_id=metadata.get("session_id", "unknown"),
                    category="observation",
                    title=f"Codex error: {msg[:60]}",
                    detail=msg,
                    tags=["codex-error"],
                ))

            elif itype == "reasoning":
                text = item.get("text", "")
                if text:
                    current_thinking.append(text)

            elif itype == "command_execution":
                cmd = item.get("command", "")
                output = item.get("aggregated_output", "")
                exit_code = item.get("exit_code")
                status = item.get("status", "")

                operation_type = classify_operation_from_command(cmd)
                truncated, full_bytes, full_hash = truncate_content(output)

                tc = build_tool_call(
                    tc_id=item_id,
                    turn_index=turn_index,
                    timestamp=None,
                    tool_name="exec_command",
                    operation_type=operation_type,
                    command=cmd,
                    file_path=extract_file_path_from_command(cmd),
                    arguments={"cmd": cmd},
                    success=exit_code == 0 if exit_code is not None else status == "completed",
                    result=output,
                    duration_ms=None,
                    framework_metadata={
                        "codex_function": "exec_command",
                        "exit_code": exit_code,
                        "status": status,
                    },
                    content_origin=classify_content_origin("exec_command"),
                )
                tool_calls.append(tc)
                tc_index += 1

            elif itype == "agent_message":
                text = item.get("text", "")
                thinking = "\n".join(current_thinking) if current_thinking else None

                tc_ids = [tc["id"] for tc in tool_calls
                          if tc.get("emitting_turn_index") == turn_index]

                turns.append(build_turn(
                    index=turn_index,
                    timestamp=None,
                    role="assistant",
                    source="model",
                    content=text,
                    tool_calls_in_turn=tc_ids,
                    thinking=thinking,
                ))
                turn_index += 1
                current_thinking = []

    return turns, tool_calls, metadata, annotations, all_timestamps, token_totals


# --- Session Discovery ---

def find_session_files(source_dir):
    """Find all Codex session JSONL files under source_dir.

    Searches sessions/ subdirectory first (canonical ~/.codex/ layout).
    Falls back to searching the entire source_dir tree.

    Returns list of Path objects.
    """
    source = Path(source_dir)
    sessions_dir = source / "sessions"

    if sessions_dir.is_dir():
        files = list(sessions_dir.rglob("*.jsonl"))
        if files:
            return sorted(files)

    # Fallback: search entire source_dir tree
    return sorted(source.rglob("*.jsonl"))


def detect_format(records):
    """Detect whether records are exec JSONL or session JSONL format.

    Returns "session" or "exec".
    """
    for rec in records[:5]:
        if rec.get("type") == "session_meta":
            return "session"
        if rec.get("type") == "thread.started":
            return "exec"
    # Default to session if ambiguous
    return "session" if any(r.get("type") == "response_item" for r in records) else "exec"


# --- Session Conversion ---

def convert_session(records, session_id, source_path=None):
    """Convert Codex JSONL records into a minitrace session.

    Auto-detects format (exec vs session JSONL) and uses appropriate parser.
    """
    fmt = detect_format(records)

    if fmt == "session":
        turns, tool_calls, metadata, annotations, timestamps, token_totals = \
            parse_session_jsonl(records)
        source_format = SOURCE_FORMAT_SESSION
    else:
        turns, tool_calls, metadata, annotations, timestamps, token_totals = \
            parse_exec_jsonl(records)
        source_format = SOURCE_FORMAT_EXEC

    # Use metadata session_id if available
    if metadata.get("session_id"):
        session_id = metadata["session_id"]

    # Deduplicate
    tool_calls, dupe_count = deduplicate_tool_calls(tool_calls)
    if dupe_count > 0:
        annotations.append(build_annotation(
            ann_id=f"ann-dedup-{session_id[:8]}",
            annotator="adapter",
            scope_type="session",
            target_id=session_id,
            category="observation",
            title=f"Deduplicated {dupe_count} duplicate tool calls",
            detail=f"Removed {dupe_count} tool calls with duplicate IDs.",
            tags=["deduplication", "data-quality"],
        ))

    # Compute context
    compute_tool_call_context(tool_calls)

    # Build session
    timing = compute_timing(timestamps)
    quality = assign_quality_tier(turns, tool_calls)
    contains_pii = detect_pii_in_paths(tool_calls)

    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="codex",
        source_format=source_format,
        converter_version=ADAPTER_VERSION,
    )

    # Environment
    session["environment"]["model"] = metadata.get("model") or None
    session["environment"]["agent_version"] = metadata.get("cli_version")
    session["environment"]["platform_type"] = "agent"
    session["environment"]["system_prompt"] = metadata.get("system_prompt")
    session["environment"]["tools_enabled"] = list(set(
        tc["tool_name"] for tc in tool_calls
    ))

    # Provider hint
    mp = metadata.get("model_provider", "")
    if mp == "ollama":
        session["environment"]["provider_hint"] = "openai-compatible"
    elif mp in ("openai", ""):
        session["environment"]["provider_hint"] = "openai-compatible"
    else:
        session["environment"]["provider_hint"] = mp

    # Operational context
    session["operational_context"]["working_directory"] = metadata.get("cwd")

    # Map approval_policy to autonomy_level
    ap = metadata.get("approval_policy")
    if ap == "never":
        session["operational_context"]["autonomy_level"] = "full-auto"
    elif ap == "always":
        session["operational_context"]["autonomy_level"] = "suggest"
    elif ap:
        session["operational_context"]["autonomy_level"] = ap

    # Map sandbox_policy
    sp = metadata.get("sandbox_policy")
    if sp == "danger-full-access":
        session["operational_context"]["sandbox"] = False
    elif sp and "sandbox" in str(sp).lower():
        session["operational_context"]["sandbox"] = True

    # Framework config — Codex-specific settings not in core schema
    framework_config = {}
    if metadata.get("personality"):
        framework_config["personality"] = metadata["personality"]
    if metadata.get("collaboration_mode"):
        framework_config["collaboration_mode"] = metadata["collaboration_mode"]
    if metadata.get("reasoning_effort"):
        framework_config["reasoning_effort"] = metadata["reasoning_effort"]
    if metadata.get("originator"):
        framework_config["originator"] = metadata["originator"]
    if metadata.get("context_window"):
        framework_config["model_context_window"] = metadata["context_window"]
    if metadata.get("timezone"):
        framework_config["timezone"] = metadata["timezone"]
    if framework_config:
        session["operational_context"]["framework_config"] = framework_config

    # Provenance
    if source_path:
        session["provenance"]["source_path"] = str(source_path)

    # Title, timing, data
    session["quality"] = quality
    session["title"] = extract_title(turns)
    session["timing"] = timing
    session["turns"] = turns
    session["tool_calls"] = tool_calls
    session["annotations"] = annotations
    session["metrics"] = compute_metrics(
        turns, tool_calls, timing, token_totals=token_totals,
    )

    # Flags
    session["flags"]["contains_pii"] = contains_pii
    session["flags"]["for_research"] = quality in ("A",) and not contains_pii
    session["flags"]["needs_cleaning"] = quality not in ("A",) or contains_pii
    session["classification"] = "confidential" if contains_pii else "internal"

    return session, quality


# --- CLI ---

def parse_args():
    p = argparse.ArgumentParser(description="minitrace Codex adapter")
    p.add_argument("--source-dir", help="Codex data directory (e.g., ~/.codex/)")
    p.add_argument("--source-jsonl", help="Codex exec JSONL output file")
    p.add_argument("--source-session", help="Codex session JSONL file (from ~/.codex/sessions/)")
    p.add_argument("--output-dir", default="./data/sessions",
                   help="minitrace archive output directory")
    p.add_argument("--session-id", default=None,
                   help="Override session ID (default: derived from source)")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and report stats without writing output")
    p.add_argument("--verbose", action="store_true", help="Print progress")
    p.add_argument("--discover", action="store_true",
                   help="Discovery mode: inspect source format and print structure")
    return p.parse_args()


def discover_format_cli(args):
    """Discovery mode: inspect Codex output format and print structure."""
    source = args.source_jsonl or args.source_session
    if source:
        print(f"=== Codex JSONL Discovery: {source} ===")
        records = parse_jsonl(source)
        fmt = detect_format(records)
        print(f"Detected format: {fmt}")
        print(f"Total records: {len(records)}")
        print()

        # Record types
        types = defaultdict(int)
        for rec in records:
            rtype = rec.get("type", "<no type>")
            payload = rec.get("payload", {})
            ptype = payload.get("type", "") if isinstance(payload, dict) else ""
            key = f"{rtype}/{ptype}" if ptype else rtype
            types[key] += 1
        print("Record types:")
        for t, count in sorted(types.items()):
            print(f"  {t}: {count}")
        print()

        # One example per type
        seen = set()
        for rec in records:
            rtype = rec.get("type", "?")
            payload = rec.get("payload", {})
            ptype = payload.get("type", "") if isinstance(payload, dict) else ""
            key = f"{rtype}/{ptype}" if ptype else rtype
            if key not in seen:
                seen.add(key)
                print(f"--- {key} ---")
                print(json.dumps(rec, indent=2, ensure_ascii=False)[:1500])
                print()

    if args.source_dir:
        print(f"=== Codex Directory: {args.source_dir} ===")
        sessions = find_session_files(args.source_dir)
        print(f"Session files: {len(sessions)}")
        for s in sessions[:10]:
            size = s.stat().st_size
            print(f"  {s.name} ({size:,} bytes)")
        if len(sessions) > 10:
            print(f"  ... and {len(sessions) - 10} more")


def main():
    args = parse_args()

    if args.discover:
        discover_format_cli(args)
        return

    # Determine source
    source_file = args.source_jsonl or args.source_session
    if source_file:
        records = parse_jsonl(source_file)
        if not records:
            print(f"No records found in {source_file}", file=sys.stderr)
            sys.exit(1)

        session_id = args.session_id or Path(source_file).stem
        session, quality = convert_session(records, session_id, source_path=source_file)

        if args.dry_run:
            tc = session["metrics"]["tool_call_count"]
            tn = session["metrics"]["turn_count"]
            model = session["environment"]["model"]
            fmt = session["provenance"]["source_format"]
            print(f"{quality} {session['id'][:12]}... turns={tn} tools={tc} model={model} format={fmt}")
        else:
            file_path, file_size, period, _ = write_session(
                session, args.output_dir, quality,
            )
            print(f"{quality} {session['id'][:12]}... → {file_path} ({file_size:,} bytes)")

    elif args.source_dir:
        sessions_files = find_session_files(args.source_dir)
        if not sessions_files:
            print(f"No session files found in {args.source_dir}", file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(sessions_files)} session files")

        session_index = []
        quality_counts = defaultdict(int)
        errors = 0

        for sf in sessions_files:
            try:
                records = parse_jsonl(str(sf))
                if len(records) < 3:
                    quality_counts["D"] += 1
                    continue

                session_id = args.session_id or sf.stem
                session, quality = convert_session(records, session_id, source_path=sf)
                quality_counts[quality] += 1

                if args.dry_run:
                    tc = session["metrics"]["tool_call_count"]
                    tn = session["metrics"]["turn_count"]
                    model = session["environment"]["model"]
                    if args.verbose:
                        print(f"  {quality} {session['id'][:12]}... turns={tn} tools={tc} model={model}")
                else:
                    file_path, file_size, period, _ = write_session(
                        session, args.output_dir, quality,
                    )
                    if args.verbose:
                        print(f"  {quality} {session['id'][:12]}... → {file_path} ({file_size:,} bytes)")

                session_index.append({
                    "id": session["id"],
                    "profile": session["profile"],
                    "title": session.get("title"),
                    "classification": session["classification"],
                    "quality": quality,
                    "started_at": session["timing"].get("started_at"),
                    "duration_seconds": session["timing"].get("duration_seconds"),
                    "model": session["environment"]["model"],
                    "agent_framework": "codex",
                    "turn_count": session["metrics"]["turn_count"],
                    "tool_call_count": session["metrics"]["tool_call_count"],
                    "file_size_bytes": 0,
                    "period": (session["timing"].get("started_at") or "")[:7] or "unknown",
                    "source_format": session["provenance"]["source_format"],
                    "flags": session["flags"],
                })

            except Exception as e:
                errors += 1
                if args.verbose:
                    print(f"  ERROR {sf.name}: {type(e).__name__}", file=sys.stderr)

        # Write manifests
        if not args.dry_run and session_index:
            write_manifests(session_index, args.output_dir)

        # Summary
        total = sum(quality_counts.values())
        print(f"\n--- Summary ---")
        print(f"Total: {total}")
        print(f"Quality: A={quality_counts.get('A',0)} B={quality_counts.get('B',0)} "
              f"C={quality_counts.get('C',0)} D={quality_counts.get('D',0)}")
        print(f"Errors: {errors}")

    else:
        print("ERROR: --source-jsonl, --source-session, or --source-dir required",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
