#!/usr/bin/env python3
"""
minitrace Pi adapter — converts Pi (Mario Zechner) session data to minitrace v0.1.0 format.

Usage:
    python3 minitrace-pi-adapter.py --discover --source-dir ~/.pi/agent/sessions/
    python3 minitrace-pi-adapter.py --source-dir ~/.pi/agent/sessions/ --output-dir ./data/sessions/
    python3 minitrace-pi-adapter.py --source-session path/to/session.jsonl --output-dir ./data/sessions/

Pi native format (discovered 2026-03-15 against Pi 0.58.1):

  JSONL v3 in ~/.pi/agent/sessions/<project-dir>/
  File naming: YYYY-MM-DDThh-mm-ss-mmmZ_<uuid>.jsonl

  Record types:
    session           — {version: 3, id, timestamp, cwd}
    model_change      — {provider, modelId}
    thinking_level_change — {thinkingLevel}
    message           — {message: {role, content: [blocks], usage?, api?, provider?, model?}}

  Content block types:
    {type: "text", text: "..."}
    {type: "thinking", thinking: "...", signature?: "..."}
    {type: "toolCall", id, name, arguments: {...}}
    {type: "toolResult", toolUseId?, content: "..."}

  Pi tools: bash, edit, write, read (inferred from runs)
"""

import argparse
import json
import os
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

ADAPTER_VERSION = "minitrace-pi-adapter-0.1.0"
SOURCE_FORMAT = "pi-agent-jsonl-v3"


def classify_operation(tool_name, arguments=None):
    """Map Pi tool names to minitrace operation_type."""
    mapping = {
        "bash": "EXECUTE",
        "read": "READ",
        "read_file": "READ",
        "write": "NEW",
        "write_file": "NEW",
        "edit": "MODIFY",
        "edit_file": "MODIFY",
        "search": "READ",
        "list_files": "READ",
        "glob": "READ",
        "grep": "READ",
    }
    op = mapping.get(tool_name, "OTHER")

    # Refine bash commands by content
    if tool_name == "bash" and isinstance(arguments, dict):
        cmd = arguments.get("command", "").strip().lower()
        if cmd:
            # Read patterns
            for pat in ("cat ", "head ", "tail ", "less ", "ls ", "find ", "grep ", "rg "):
                if cmd.startswith(pat):
                    return "READ"
            # Write patterns
            if " > " in cmd or cmd.startswith("touch ") or cmd.startswith("mkdir "):
                return "NEW"
            if " >> " in cmd or cmd.startswith("sed -i"):
                return "MODIFY"
    return op


def extract_file_path(tool_name, arguments):
    """Extract file path from tool arguments if present."""
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "file", "filename"):
        if key in arguments:
            return arguments[key]
    return None


def convert_session(records, source_path=None, verbose=False):
    """Convert Pi JSONL records to minitrace format.

    Returns a minitrace session dict.
    """
    session_meta = {}
    model_info = {}
    turns = []
    tool_calls = []
    all_timestamps = []
    token_totals = {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0}

    turn_index = 0
    pending_tool_calls = {}  # id -> tool_call dict (awaiting result)

    for rec in records:
        rtype = rec.get("type", "")

        if rtype == "session":
            session_meta = {
                "id": rec.get("id"),
                "version": rec.get("version"),
                "cwd": rec.get("cwd"),
                "timestamp": rec.get("timestamp"),
            }
            ts = parse_timestamp(rec.get("timestamp"))
            if ts:
                all_timestamps.append(ts)
            continue

        if rtype == "model_change":
            model_info = {
                "provider": rec.get("provider"),
                "model": rec.get("modelId"),
            }
            continue

        if rtype == "thinking_level_change":
            model_info["thinking_level"] = rec.get("thinkingLevel")
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

        # Extract usage
        usage_raw = msg.get("usage")
        usage = None
        if usage_raw and isinstance(usage_raw, dict):
            token_totals["input"] += safe_int(usage_raw.get("input"))
            token_totals["output"] += safe_int(usage_raw.get("output"))
            token_totals["cache_read"] += safe_int(usage_raw.get("cacheRead"))
            usage = {
                "input_tokens": usage_raw.get("input"),
                "output_tokens": usage_raw.get("output"),
                "cache_read_tokens": usage_raw.get("cacheRead"),
                "cache_creation_tokens": usage_raw.get("cacheWrite"),
                "reasoning_tokens": None,
                "tool_tokens": None,
            }

        # Process content blocks
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
                    command = tc_args.get("command") if tc_name == "bash" else None

                    tc = build_tool_call(
                        tc_id=tc_id,
                        turn_index=turn_index,
                        timestamp=msg_ts_str,
                        tool_name=tc_name,
                        operation_type=classify_operation(tc_name, tc_args),
                        file_path=file_path,
                        command=command,
                        arguments=tc_args,
                        success=True,  # updated when result arrives
                    )
                    tool_calls.append(tc)
                    tc_ids_in_turn.append(tc_id)
                    pending_tool_calls[tc_id] = tc

                elif btype == "toolResult":
                    # Match to pending tool call
                    result_id = block.get("toolUseId") or block.get("tool_use_id")
                    result_content = block.get("content", "")
                    is_error = block.get("isError", False)

                    if result_id and result_id in pending_tool_calls:
                        tc = pending_tool_calls.pop(result_id)
                        truncated, full_bytes, full_hash = truncate_content(result_content)
                        tc["output"]["result"] = truncated
                        tc["output"]["success"] = not is_error
                        if is_error:
                            tc["output"]["error"] = str(result_content)[:500]
                        if full_bytes:
                            tc["output"]["truncated"] = True
                            tc["output"]["full_bytes"] = full_bytes
                            tc["output"]["full_hash"] = full_hash

                elif btype == "tool_use":
                    # Alternative format (OpenAI-style)
                    tc_id = block.get("id", f"tc_{turn_index}_{len(tool_calls)}")
                    tc_name = block.get("name", "unknown")
                    tc_args = block.get("input", {})
                    file_path = extract_file_path(tc_name, tc_args)
                    command = tc_args.get("command") if tc_name in ("bash", "shell") else None

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

                elif btype == "tool_result":
                    result_id = block.get("tool_use_id")
                    result_content = block.get("content", "")
                    is_error = block.get("is_error", False)
                    if result_id and result_id in pending_tool_calls:
                        tc = pending_tool_calls.pop(result_id)
                        truncated, full_bytes, full_hash = truncate_content(result_content)
                        tc["output"]["result"] = truncated
                        tc["output"]["success"] = not is_error
                        if is_error:
                            tc["output"]["error"] = str(result_content)[:500]

        # Determine source
        if role == "user":
            source = "human"
        elif role == "assistant":
            source = "model"
        elif role == "toolResult":
            source = "framework"
            role = "user"  # tool results are "user" role in conversation
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
            usage=usage,
        ))
        turn_index += 1

    # Deduplicate tool calls
    tool_calls, dupe_count = deduplicate_tool_calls(tool_calls)

    # Build session
    session_id = session_meta.get("id", "unknown")
    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="pi",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    session["title"] = extract_title(turns)
    session["provenance"]["source_path"] = str(source_path) if source_path else None
    session["provenance"]["original_session_id"] = session_id

    session["environment"]["model"] = model_info.get("model", "unknown")
    session["environment"]["platform_type"] = "agent"
    session["environment"]["provider_hint"] = model_info.get("provider", "unknown")
    session["environment"]["agent_framework"] = "pi"
    session["environment"]["tools_enabled"] = sorted(set(
        tc["tool_name"] for tc in tool_calls
    ))
    session["environment"]["framework_config"] = {
        "thinking_level": model_info.get("thinking_level"),
        "api": model_info.get("api"),
    }

    session["operational_context"]["working_directory"] = session_meta.get("cwd")

    # Timing
    timing = compute_timing(all_timestamps)
    session["timing"] = timing

    # Turns and tool calls
    session["turns"] = turns
    session["tool_calls"] = tool_calls

    # Context computation
    compute_tool_call_context(tool_calls)

    # Metrics
    session["metrics"] = compute_metrics(turns, tool_calls, timing, token_totals=token_totals)

    # Quality
    quality = assign_quality_tier(turns, tool_calls)
    session["flags"]["needs_cleaning"] = False
    session["flags"]["contains_pii"] = detect_pii_in_paths(tool_calls)

    return session, quality


def discover_format(source_dir):
    """Discovery mode: inspect Pi session data and print structure."""
    source = Path(source_dir)
    if not source.exists():
        print(f"Directory not found: {source}")
        return

    print(f"=== Pi sessions in {source} ===")
    session_dirs = sorted(source.iterdir()) if source.is_dir() else []
    for sdir in session_dirs:
        if sdir.is_dir():
            jsonl_files = sorted(sdir.glob("*.jsonl"))
            print(f"\n  {sdir.name}/")
            for jf in jsonl_files:
                records = parse_jsonl(jf)
                types = defaultdict(int)
                for r in records:
                    types[r.get("type", "?")] += 1
                print(f"    {jf.name} ({len(records)} records)")
                for t, c in sorted(types.items()):
                    print(f"      {t}: {c}")


def main():
    p = argparse.ArgumentParser(description="minitrace Pi adapter")
    p.add_argument("--source-dir", help="Pi sessions directory (e.g., ~/.pi/agent/sessions/)")
    p.add_argument("--source-session", help="Single Pi session JSONL file")
    p.add_argument("--output-dir", default="./data/sessions")
    p.add_argument("--discover", action="store_true", help="Discovery mode: inspect native format")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.discover:
        discover_format(args.source_dir or str(Path.home() / ".pi" / "agent" / "sessions"))
        return

    # Collect JSONL files to convert
    files = []
    if args.source_session:
        files.append(Path(args.source_session))
    elif args.source_dir:
        source = Path(args.source_dir)
        for jsonl in sorted(source.rglob("*.jsonl")):
            files.append(jsonl)
    else:
        # Default location
        default = Path.home() / ".pi" / "agent" / "sessions"
        if default.exists():
            for jsonl in sorted(default.rglob("*.jsonl")):
                files.append(jsonl)
        else:
            print("No Pi sessions found. Specify --source-dir or --source-session.")
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

            session, quality = convert_session(records, source_path=jsonl_path, verbose=args.verbose)
            quality_counts[quality] += 1

            if args.dry_run:
                print(f"  {quality} {session['id'][:20]}... ({len(session['turns'])} turns, {len(session['tool_calls'])} tool_calls)")
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
                    "agent_framework": "pi",
                    "turn_count": len(session["turns"]),
                    "tool_call_count": len(session["tool_calls"]),
                    "file_size_bytes": file_size,
                    "source_format": SOURCE_FORMAT,
                    "title": session.get("title"),
                    "flags": session.get("flags", {}),
                })

                if args.verbose:
                    print(f"  {quality} {session['id'][:20]}... → {file_path} ({file_size:,} bytes)")

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
