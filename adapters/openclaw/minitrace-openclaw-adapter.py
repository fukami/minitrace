#!/usr/bin/env python3
"""
minitrace OpenClaw adapter — converts OpenClaw session data to minitrace v0.1.0 format.

Usage:
    python3 minitrace-openclaw-adapter.py --discover --source-dir ~/.openclaw/agents/main/sessions/
    python3 minitrace-openclaw-adapter.py --source-dir ~/.openclaw/agents/main/sessions/ --output-dir ./data/sessions/
    python3 minitrace-openclaw-adapter.py --source-session path/to/session.jsonl --output-dir ./data/sessions/

OpenClaw native format (discovered 2026-03-16 against OpenClaw 2026.3.13):

  JSONL v3 in ~/.openclaw/agents/<agent-id>/sessions/
  File naming: <session-id>.jsonl (session-id can be UUID or custom string)
  Index: sessions.json (maps session keys to metadata)

  Record types:
    session                — {version: 3, id, timestamp, cwd}
    model_change           — {provider, modelId, parentId}
    thinking_level_change  — {thinkingLevel, parentId}
    custom                 — {customType: "model-snapshot"|"openclaw:prompt-error", data}
    message                — {message: {role, content: [blocks], ...}}

  Message roles:
    user       — content: [{type: "text", text: "..."}]
    assistant  — content: [{type: "toolCall", id, name, arguments}, {type: "text", text: "..."}]
                 Includes: stopReason, api, provider, model, usage stats
    toolResult — toolCallId, toolName, content: [{type: "text", text: "..."}], isError

  Parent chain: each record has parentId linking to previous record (tree structure).
  Timestamps: ISO 8601 at record level + Unix ms at message.timestamp.

  Tools (coding profile):
    read, edit, write, exec, process, cron,
    sessions_list, sessions_history, sessions_send, sessions_yield, sessions_spawn,
    subagents, session_status, web_fetch, memory_search, memory_get

  Gateway model: runs persistent ws:// gateway on configurable port.
  Agent command: `openclaw agent --message "..." --json` routes through gateway.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from minitrace_common import (
    SCHEMA_VERSION,
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

ADAPTER_VERSION = "minitrace-openclaw-adapter-0.1.0"
SOURCE_FORMAT = "openclaw-session-jsonl-v3"


def split_multi_scenario_session(records):
    """Split a combined JSONL (multiple user prompts in one session) into sub-sessions.

    OpenClaw may merge multiple agent invocations into a single session file
    when using the same session key. Each user message marks a new scenario.

    Returns list of (sub_session_id, records) tuples.
    """
    # Separate header records from message records
    headers = []
    message_groups = []
    current_group = []

    for rec in records:
        rtype = rec.get("type", "")
        if rtype in ("session", "model_change", "thinking_level_change", "custom"):
            if rec.get("type") == "custom" and rec.get("customType") == "openclaw:prompt-error":
                # Error events belong to current group
                if current_group:
                    current_group.append(rec)
                continue
            headers.append(rec)
            continue

        if rtype == "message":
            role = rec.get("message", {}).get("role", "")
            if role == "user":
                # New scenario boundary
                if current_group:
                    message_groups.append(current_group)
                current_group = [rec]
            else:
                current_group.append(rec)

    if current_group:
        message_groups.append(current_group)

    if len(message_groups) <= 1:
        return [(None, records)]  # Single scenario, no split needed

    # Build sub-sessions: headers + each message group
    base_id = None
    for h in headers:
        if h.get("type") == "session":
            base_id = h.get("id", "unknown")
            break

    result = []
    for i, group in enumerate(message_groups):
        sub_id = f"{base_id}-sub{i+1}" if base_id else f"sub{i+1}"
        sub_records = headers + group
        result.append((sub_id, sub_records))

    return result


def classify_operation(tool_name, arguments=None):
    """Map OpenClaw tool names to minitrace operation_type."""
    mapping = {
        "read": "READ",
        "edit": "MODIFY",
        "write": "NEW",
        "exec": "EXECUTE",
        "process": "EXECUTE",
        "sessions_spawn": "DELEGATE",
        "subagents": "DELEGATE",
        "sessions_send": "DELEGATE",
        "sessions_list": "OTHER",
        "sessions_history": "OTHER",
        "sessions_yield": "OTHER",
        "session_status": "OTHER",
        "web_fetch": "READ",
        "memory_search": "READ",
        "memory_get": "READ",
        "cron": "OTHER",
    }
    return mapping.get(tool_name, "OTHER")


def extract_file_path(tool_name, arguments):
    """Extract file path from tool arguments if present."""
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "file", "filename"):
        if key in arguments:
            return arguments[key]
    return None


def convert_session(records, source_path=None, verbose=False, override_id=None):
    """Convert OpenClaw JSONL records to minitrace format."""
    session_meta = {}
    model_info = {}
    turns = []
    tool_calls = []
    all_timestamps = []
    token_totals = {"input": 0, "output": 0, "cache_read": 0}

    turn_index = 0
    pending_tool_calls = {}

    for rec in records:
        rtype = rec.get("type", "")

        # Session header
        if rtype == "session":
            session_meta = {
                "id": rec.get("id"),
                "cwd": rec.get("cwd"),
                "version": rec.get("version"),
            }
            ts = parse_timestamp(rec.get("timestamp"))
            if ts:
                all_timestamps.append(ts)
            continue

        # Model configuration changes
        if rtype == "model_change":
            model_info = {
                "provider": rec.get("provider"),
                "model": rec.get("modelId"),
            }
            continue

        if rtype == "thinking_level_change":
            model_info["thinking_level"] = rec.get("thinkingLevel")
            continue

        # Custom events (errors, snapshots)
        if rtype == "custom":
            custom_type = rec.get("customType", "")
            if custom_type == "openclaw:prompt-error":
                data = rec.get("data", {})
                ts = parse_timestamp(rec.get("timestamp"))
                if ts:
                    all_timestamps.append(ts)
            continue

        if rtype != "message":
            continue

        msg = rec.get("message", {})
        role = msg.get("role", "")
        content_blocks = msg.get("content", [])
        msg_ts_str = rec.get("timestamp")
        msg_ts = parse_timestamp(msg_ts_str)
        if msg_ts:
            all_timestamps.append(msg_ts)

        # Track usage from assistant messages
        usage = msg.get("usage", {})
        if usage:
            token_totals["input"] += safe_int(usage.get("input"))
            token_totals["output"] += safe_int(usage.get("output"))
            token_totals["cache_read"] += safe_int(usage.get("cacheRead"))

        # toolResult is a top-level message role in OpenClaw (not a content block)
        if role == "toolResult":
            result_id = msg.get("toolCallId")
            result_content_blocks = content_blocks
            is_error = msg.get("isError", False)

            # Extract text from content blocks
            result_text = ""
            if isinstance(result_content_blocks, list):
                parts = []
                for block in result_content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                result_text = "\n".join(parts)
            elif isinstance(result_content_blocks, str):
                result_text = result_content_blocks

            if result_id and result_id in pending_tool_calls:
                tc = pending_tool_calls.pop(result_id)
                truncated, full_bytes, full_hash = truncate_content(result_text)
                tc["output"]["result"] = truncated
                tc["output"]["success"] = not is_error
                if is_error:
                    tc["output"]["error"] = str(result_text)[:500]
                if full_bytes:
                    tc["output"]["truncated"] = True
                    tc["output"]["full_bytes"] = full_bytes
                    tc["output"]["full_hash"] = full_hash

                # Calculate duration if we have timestamps
                tc_ts = parse_timestamp(tc.get("timestamp"))
                result_ts = msg_ts
                if tc_ts and result_ts:
                    duration_ms = int((result_ts - tc_ts).total_seconds() * 1000)
                    if duration_ms >= 0:
                        tc["output"]["duration_ms"] = duration_ms

            continue  # toolResult doesn't create a turn

        # user or assistant messages
        text_parts = []
        thinking_parts = []
        tc_ids_in_turn = []

        if isinstance(content_blocks, str):
            text_parts.append(content_blocks)
        elif isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                    continue

                btype = block.get("type", "")

                if btype == "text":
                    text_parts.append(block.get("text", ""))

                elif btype == "thinking":
                    thinking_parts.append(block.get("thinking", ""))

                elif btype == "toolCall":
                    tc_id = block.get("id", f"tc_{turn_index}_{len(tool_calls)}")
                    tc_name = block.get("name", "unknown")
                    tc_args = block.get("arguments", {})
                    file_path = extract_file_path(tc_name, tc_args)
                    command = tc_args.get("command") if tc_name in ("exec", "process") else None

                    tc = build_tool_call(
                        tc_id=tc_id,
                        turn_index=turn_index,
                        timestamp=msg_ts_str,
                        tool_name=tc_name,
                        operation_type=classify_operation(tc_name, tc_args),
                        file_path=file_path,
                        command=command,
                        arguments=tc_args,
                    )
                    tool_calls.append(tc)
                    tc_ids_in_turn.append(tc_id)
                    pending_tool_calls[tc_id] = tc

        # Determine source
        if role == "user":
            source = "human"
            content_text = "\n".join(text_parts).strip()
            # OpenClaw injects sender metadata as untrusted
            if content_text and ("Sender (untrusted metadata)" in content_text):
                source = "framework"
        elif role == "assistant":
            source = "model"
        else:
            source = "framework"

        content = "\n".join(text_parts).strip() if text_parts else None
        thinking = "\n".join(thinking_parts) if thinking_parts else None

        turns.append(build_turn(
            index=turn_index,
            timestamp=msg_ts_str,
            role=role if role in ("user", "assistant", "system") else "user",
            source=source,
            content=content,
            tool_calls_in_turn=tc_ids_in_turn,
            thinking=thinking,
        ))
        turn_index += 1

    # Deduplicate
    tool_calls, dupe_count = deduplicate_tool_calls(tool_calls)

    # Build session
    session_id = override_id or session_meta.get("id", "unknown")
    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="openclaw",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    session["title"] = extract_title(turns)
    session["provenance"]["source_path"] = str(source_path) if source_path else None
    session["provenance"]["original_session_id"] = session_meta.get("id")

    # Environment
    session["environment"]["model"] = model_info.get("model", "unknown")
    session["environment"]["agent_framework"] = "openclaw"
    session["environment"]["agent_version"] = "2026.3.13"
    session["environment"]["provider_hint"] = (
        "openai-compatible" if model_info.get("provider") == "ollama"
        else model_info.get("provider", "unknown")
    )
    session["environment"]["tools_enabled"] = sorted(set(
        tc["tool_name"] for tc in tool_calls
    ))

    # Operational context
    if session_meta.get("cwd"):
        session["operational_context"]["working_directory"] = session_meta["cwd"]
    session["operational_context"]["framework_config"] = {
        "thinking_level": model_info.get("thinking_level"),
        "session_format_version": session_meta.get("version"),
    }

    # Timing
    timing = compute_timing(all_timestamps)
    session["timing"] = timing
    session["turns"] = turns
    session["tool_calls"] = tool_calls

    compute_tool_call_context(tool_calls)

    session["metrics"] = compute_metrics(turns, tool_calls, timing, token_totals=token_totals)

    quality = assign_quality_tier(turns, tool_calls)
    session["flags"]["needs_cleaning"] = False
    session["flags"]["contains_pii"] = detect_pii_in_paths(tool_calls)

    return session, quality


def discover_format(source_dir):
    """Discovery mode: inspect OpenClaw session data."""
    source = Path(source_dir)
    if not source.exists():
        print(f"Directory not found: {source}")
        return

    print(f"=== OpenClaw sessions in {source} ===")

    # Check for sessions index
    index_path = source / "sessions.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text())
            print(f"\nSession index: {len(index)} entries")
            for key, meta in index.items():
                sid = meta.get("sessionId", "?")
                model = meta.get("model", "?")
                provider = meta.get("modelProvider", "?")
                print(f"  {key}: {sid} ({provider}/{model})")
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Error reading index: {e}")

    # List JSONL files
    jsonl_files = sorted(source.glob("*.jsonl"))
    print(f"\nSession files: {len(jsonl_files)}")
    for jf in jsonl_files:
        records = parse_jsonl(jf)
        types = defaultdict(int)
        roles = defaultdict(int)
        for r in records:
            types[r.get("type", "?")] += 1
            if r.get("type") == "message":
                roles[r.get("message", {}).get("role", "?")] += 1
        print(f"  {jf.name}")
        print(f"    Records: {len(records)} ({dict(types)})")
        print(f"    Message roles: {dict(roles)}")

        # Show model info
        for r in records:
            if r.get("type") == "model_change":
                print(f"    Model: {r.get('provider')}/{r.get('modelId')}")
                break


def main():
    p = argparse.ArgumentParser(description="minitrace OpenClaw adapter")
    p.add_argument("--source-dir", help="OpenClaw sessions directory")
    p.add_argument("--source-session", help="Single OpenClaw session JSONL file")
    p.add_argument("--output-dir", default="./data/sessions")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--split-scenarios", action="store_true",
                   help="Split multi-scenario session files into separate minitrace sessions")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    default_dir = Path.home() / ".openclaw" / "agents" / "main" / "sessions"

    if args.discover:
        discover_format(args.source_dir or str(default_dir))
        return

    files = []
    if args.source_session:
        files.append(Path(args.source_session))
    elif args.source_dir:
        source = Path(args.source_dir)
        for jsonl in sorted(source.glob("*.jsonl")):
            files.append(jsonl)
    else:
        if default_dir.exists():
            for jsonl in sorted(default_dir.glob("*.jsonl")):
                files.append(jsonl)
        else:
            print(f"No OpenClaw sessions found at {default_dir}")
            sys.exit(1)

    if not files:
        print("No JSONL files found.")
        sys.exit(1)

    session_index = []
    total = 0
    errors = 0
    quality_counts = defaultdict(int)

    for jsonl_path in files:
        try:
            records = parse_jsonl(jsonl_path)
            if not records:
                continue

            # Split multi-scenario sessions if requested
            if args.split_scenarios:
                sub_sessions = split_multi_scenario_session(records)
            else:
                sub_sessions = [(None, records)]

            for override_id, sub_records in sub_sessions:
                session, quality = convert_session(
                    sub_records, source_path=jsonl_path,
                    verbose=args.verbose, override_id=override_id,
                )
                quality_counts[quality] += 1

                if args.dry_run:
                    print(f"  {quality} {session['id'][:30]}... ({len(session['turns'])} turns, {len(session['tool_calls'])} tool_calls)")
                else:
                    file_path, file_size, period, _ = write_session(session, args.output_dir, quality)
                    session_index.append({
                        "id": session["id"],
                        "profile": session["profile"],
                        "classification": session["classification"],
                        "quality": quality,
                        "period": period,
                        "started_at": session["timing"].get("started_at"),
                        "duration_seconds": session["timing"].get("duration_seconds"),
                        "model": session["environment"].get("model"),
                        "agent_framework": "openclaw",
                        "turn_count": len(session["turns"]),
                        "tool_call_count": len(session["tool_calls"]),
                        "file_size_bytes": file_size,
                        "source_format": SOURCE_FORMAT,
                        "title": session.get("title"),
                        "flags": session.get("flags", {}),
                    })
                    if args.verbose:
                        print(f"  {quality} {session['id'][:30]}... -> {file_path} ({file_size:,} bytes)")

                total += 1

        except Exception as e:
            errors += 1
            print(f"  ERROR {jsonl_path.name}: {type(e).__name__}", file=sys.stderr)
            if args.verbose:
                import traceback
                traceback.print_exc()

    if not args.dry_run and session_index:
        write_manifests(session_index, args.output_dir)

    print(f"\n--- Summary ---")
    print(f"Total: {total}")
    print(f"Quality: A={quality_counts['A']} B={quality_counts['B']} C={quality_counts['C']} D={quality_counts['D']}")
    print(f"Errors: {errors}")


if __name__ == "__main__":
    main()
