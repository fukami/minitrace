#!/usr/bin/env python3
"""
minitrace Droid adapter — converts Droid (Factory) session data to minitrace v0.1.0 format.

Usage:
    python3 minitrace-droid-adapter.py --discover --source-dir ~/.factory/sessions/
    python3 minitrace-droid-adapter.py --source-dir ~/.factory/sessions/ --output-dir ./data/sessions/
    python3 minitrace-droid-adapter.py --source-session path/to/session.jsonl --output-dir ./data/sessions/

Droid native format (discovered 2026-03-15 against Droid 0.74.0):

  JSONL in ~/.factory/sessions/<project-dir>/
  File naming: <uuid>.jsonl (companion <uuid>.settings.json)

  Record types:
    session_start — {id, title, sessionTitle, ...}
    message       — {id, timestamp, message: {role, content: [blocks]}}

  Content block types:
    {type: "text", text: "..."}
    {type: "thinking", signature: "reasoning", thinking: "..."}
    {type: "tool_use", id, name, input: {...}}
    {type: "tool_result", tool_use_id, content: "..."}

  Droid uses OpenAI-compatible function calling via Factory's plugin system.
  Tools come from plugins (core, custom). Core tools: Grep, Read, Write, Edit, Bash, Glob.
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
    truncate_content,
    write_session,
    write_manifests,
    assign_quality_tier,
)

ADAPTER_VERSION = "minitrace-droid-adapter-0.1.0"
SOURCE_FORMAT = "droid-session-jsonl-v1"


def classify_operation(tool_name, arguments=None):
    """Map Droid tool names to minitrace operation_type."""
    mapping = {
        "Bash": "EXECUTE",
        "bash": "EXECUTE",
        "shell": "EXECUTE",
        "Read": "READ",
        "read": "READ",
        "read_file": "READ",
        "Write": "NEW",
        "write": "NEW",
        "write_file": "NEW",
        "Edit": "MODIFY",
        "edit": "MODIFY",
        "edit_file": "MODIFY",
        "Glob": "READ",
        "glob": "READ",
        "Grep": "READ",
        "grep": "READ",
        "search_files": "READ",
        "list_files": "READ",
        "Agent": "DELEGATE",
    }
    return mapping.get(tool_name, "OTHER")


def extract_file_path(tool_name, arguments):
    """Extract file path from tool arguments if present."""
    if not isinstance(arguments, dict):
        return None
    for key in ("file_path", "path", "file", "filename"):
        if key in arguments:
            return arguments[key]
    return None


def convert_session(records, source_path=None, verbose=False):
    """Convert Droid JSONL records to minitrace format."""
    session_meta = {}
    turns = []
    tool_calls = []
    all_timestamps = []
    token_totals = {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0}

    turn_index = 0
    pending_tool_calls = {}

    for rec in records:
        rtype = rec.get("type", "")

        if rtype == "session_start":
            session_meta = {
                "id": rec.get("id"),
                "title": rec.get("title") or rec.get("sessionTitle"),
            }
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

                elif btype == "tool_use":
                    tc_id = block.get("id", f"tc_{turn_index}_{len(tool_calls)}")
                    tc_name = block.get("name", "unknown")
                    tc_args = block.get("input", {})
                    file_path = extract_file_path(tc_name, tc_args)
                    command = tc_args.get("command") if tc_name.lower() in ("bash", "shell") else None

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
                        if full_bytes:
                            tc["output"]["truncated"] = True
                            tc["output"]["full_bytes"] = full_bytes
                            tc["output"]["full_hash"] = full_hash

        # Determine source
        if role == "user":
            source = "human"
            # Check if this is actually a framework-injected message
            content_text = "\n".join(text_parts).strip()
            if content_text and ("<system-reminder>" in content_text or "system info" in content_text[:100].lower()):
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
    session_id = session_meta.get("id", "unknown")
    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="droid",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    session["title"] = session_meta.get("title") or extract_title(turns)
    session["provenance"]["source_path"] = str(source_path) if source_path else None
    session["environment"]["platform_type"] = "agent"

    # Extract model info from first assistant message
    for rec in records:
        if rec.get("type") == "message":
            msg = rec.get("message", {})
            if msg.get("model"):
                session["environment"]["model"] = msg["model"]
            if msg.get("provider"):
                session["environment"]["provider_hint"] = msg["provider"]
            break

    session["environment"]["agent_framework"] = "droid"
    session["environment"]["tools_enabled"] = sorted(set(
        tc["tool_name"] for tc in tool_calls
    ))

    # Try to extract model from settings companion file
    if source_path:
        settings_path = Path(source_path).with_suffix(".settings.json")
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
                model = settings.get("model") or settings.get("sessionDefaultSettings", {}).get("model")
                if model:
                    session["environment"]["model"] = model
            except (json.JSONDecodeError, OSError):
                pass

    # Try to get CWD from first user message content
    for rec in records:
        if rec.get("type") == "message":
            msg = rec.get("message", {})
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    # Droid injects system context with CWD
                    if "Current working directory:" in text:
                        for line in text.split("\n"):
                            if "Current working directory:" in line:
                                session["operational_context"]["working_directory"] = line.split(":", 1)[1].strip()
                                break
                    break
            break

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
    """Discovery mode: inspect Droid session data."""
    source = Path(source_dir)
    if not source.exists():
        print(f"Directory not found: {source}")
        return

    print(f"=== Droid sessions in {source} ===")
    for sdir in sorted(source.iterdir()):
        if sdir.is_dir():
            jsonl_files = sorted(sdir.glob("*.jsonl"))
            if jsonl_files:
                print(f"\n  {sdir.name}/")
                for jf in jsonl_files:
                    records = parse_jsonl(jf)
                    types = defaultdict(int)
                    for r in records:
                        types[r.get("type", "?")] += 1
                    print(f"    {jf.name} ({len(records)} records: {dict(types)})")


def main():
    p = argparse.ArgumentParser(description="minitrace Droid adapter")
    p.add_argument("--source-dir", help="Droid sessions directory (e.g., ~/.factory/sessions/)")
    p.add_argument("--source-session", help="Single Droid session JSONL file")
    p.add_argument("--output-dir", default="./data/sessions")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.discover:
        discover_format(args.source_dir or str(Path.home() / ".factory" / "sessions"))
        return

    files = []
    if args.source_session:
        files.append(Path(args.source_session))
    elif args.source_dir:
        source = Path(args.source_dir)
        for jsonl in sorted(source.rglob("*.jsonl")):
            files.append(jsonl)
    else:
        default = Path.home() / ".factory" / "sessions"
        if default.exists():
            for jsonl in sorted(default.rglob("*.jsonl")):
                files.append(jsonl)
        else:
            print("No Droid sessions found.")
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
                    "agent_framework": "droid",
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
