#!/usr/bin/env python3
"""
minitrace Vibe adapter — converts Mistral Vibe session data to minitrace v0.1.0 format.

Usage:
    python3 minitrace-vibe-adapter.py --discover
    python3 minitrace-vibe-adapter.py --source-dir ~/.vibe/logs/session/ --output-dir ./data/sessions/
    python3 minitrace-vibe-adapter.py --source-session ~/.vibe/logs/session/session_xxx/ --output-dir ./data/sessions/

Vibe native format (discovered 2026-03-15 against Vibe 2.4.2):

  Session directory: ~/.vibe/logs/session/session_YYYYMMDD_HHMMSS_<hash>/
  Contains:
    meta.json      — session metadata, stats, tools_available, cost
    messages.jsonl — OpenAI-compatible message format

  meta.json keys: session_id, start_time, end_time, environment, username, stats,
                  title, total_messages, tools_available

  messages.jsonl record format (OpenAI chat completion):
    {role: "user", content: "..."}
    {role: "assistant", content: "...", tool_calls: [{id, type: "function", function: {name, arguments}}]}
    {role: "tool", tool_call_id: "...", content: "..."}

  Vibe tools: grep, read_file, write_file, edit_file, bash, list_directory, task, exit_plan_mode
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
    safe_open,
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

ADAPTER_VERSION = "minitrace-vibe-adapter-0.1.0"
SOURCE_FORMAT = "vibe-session-v1"

DEFAULT_SESSION_DIR = Path.home() / ".vibe" / "logs" / "session"


def classify_operation(tool_name, arguments=None):
    """Map Vibe tool names to minitrace operation_type."""
    mapping = {
        "bash": "EXECUTE",
        "grep": "READ",
        "read_file": "READ",
        "write_file": "NEW",
        "edit_file": "MODIFY",
        "list_directory": "READ",
        "task": "DELEGATE",
        "exit_plan_mode": "OTHER",
        "search": "READ",
    }
    return mapping.get(tool_name, "OTHER")


def extract_file_path(tool_name, arguments):
    """Extract file path from tool arguments."""
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "file", "filename"):
        if key in arguments:
            return arguments[key]
    return None


def convert_session(session_dir, verbose=False):
    """Convert a Vibe session directory to minitrace format."""
    session_dir = Path(session_dir)
    meta_path = session_dir / "meta.json"
    messages_path = session_dir / "messages.jsonl"

    if not meta_path.exists() or not messages_path.exists():
        raise FileNotFoundError(f"Missing meta.json or messages.jsonl in {session_dir}")

    with safe_open(meta_path) as f:
        meta = json.load(f)
    messages = parse_jsonl(messages_path)

    session_id = meta.get("session_id", session_dir.name)
    stats = meta.get("stats", {})

    turns = []
    tool_calls = []
    all_timestamps = []
    pending_tool_calls = {}

    turn_index = 0

    # Parse timestamps from meta
    start_ts = parse_timestamp(meta.get("start_time"))
    end_ts = parse_timestamp(meta.get("end_time"))
    if start_ts:
        all_timestamps.append(start_ts)
    if end_ts:
        all_timestamps.append(end_ts)

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        msg_tool_calls = msg.get("tool_calls", [])
        tool_call_id = msg.get("tool_call_id")

        # Tool result message
        if role == "tool" and tool_call_id:
            if tool_call_id in pending_tool_calls:
                tc = pending_tool_calls.pop(tool_call_id)
                result_text = content if isinstance(content, str) else json.dumps(content)
                truncated, full_bytes, full_hash = truncate_content(result_text)
                tc["output"]["result"] = truncated
                if full_bytes:
                    tc["output"]["truncated"] = True
                    tc["output"]["full_bytes"] = full_bytes
                    tc["output"]["full_hash"] = full_hash

            turns.append(build_turn(
                index=turn_index,
                timestamp=None,
                role="user",
                source="framework",
                content=str(content)[:500] if content else None,
            ))
            turn_index += 1
            continue

        # Build turn
        tc_ids_in_turn = []

        for tc_raw in msg_tool_calls:
            func = tc_raw.get("function", {})
            tc_id = tc_raw.get("id", f"tc_{turn_index}_{len(tool_calls)}")
            tc_name = func.get("name", "unknown")

            try:
                tc_args = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                tc_args = {}

            file_path = extract_file_path(tc_name, tc_args)
            command = tc_args.get("command") if tc_name == "bash" else None

            tc = build_tool_call(
                tc_id=tc_id,
                turn_index=turn_index,
                timestamp=None,
                tool_name=tc_name,
                operation_type=classify_operation(tc_name, tc_args),
                file_path=file_path,
                command=command,
                arguments=tc_args,
            )
            tool_calls.append(tc)
            tc_ids_in_turn.append(tc_id)
            pending_tool_calls[tc_id] = tc

        source = "human" if role == "user" else ("model" if role == "assistant" else "framework")
        text_content = content if isinstance(content, str) else json.dumps(content) if content else None

        turns.append(build_turn(
            index=turn_index,
            timestamp=None,
            role=role if role in ("user", "assistant", "system") else "user",
            source=source,
            content=text_content,
            tool_calls_in_turn=tc_ids_in_turn,
        ))
        turn_index += 1

    # Deduplicate
    tool_calls, dupe_count = deduplicate_tool_calls(tool_calls)

    # Token totals from meta.stats
    token_totals = {
        "input": stats.get("session_prompt_tokens", 0),
        "output": stats.get("session_completion_tokens", 0),
        "cache_read": 0,
        "reasoning": 0,
        "cost": stats.get("session_cost"),
    }

    # Build session
    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="vibe",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    session["title"] = meta.get("title") or extract_title(turns)
    session["provenance"]["source_path"] = str(session_dir)

    # Extract model from meta environment or stats
    env = meta.get("environment", {})
    model_name = env.get("model") or meta.get("model") or None
    session["environment"]["model"] = model_name
    session["environment"]["platform_type"] = "agent"
    session["environment"]["provider_hint"] = "mistral"
    session["environment"]["agent_framework"] = "vibe"
    session["environment"]["agent_version"] = env.get("version") or meta.get("version")
    session["environment"]["tools_enabled"] = sorted(set(
        tc["tool_name"] for tc in tool_calls
    ))

    env = meta.get("environment", {})
    session["operational_context"]["working_directory"] = env.get("working_directory")
    session["operational_context"]["git_branch"] = meta.get("git_branch")

    # Timing from meta
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
    """Discovery mode."""
    source = Path(source_dir)
    if not source.exists():
        print(f"Not found: {source}")
        return

    print(f"=== Vibe sessions in {source} ===")
    for sdir in sorted(source.iterdir()):
        if sdir.is_dir() and (sdir / "meta.json").exists():
            meta = json.loads((sdir / "meta.json").read_text())
            msgs = sum(1 for _ in open(sdir / "messages.jsonl")) if (sdir / "messages.jsonl").exists() else 0
            print(f"  {sdir.name}")
            print(f"    title: {meta.get('title','?')[:60]}")
            print(f"    msgs: {msgs}, steps: {meta.get('stats',{}).get('steps',0)}")


def main():
    p = argparse.ArgumentParser(description="minitrace Vibe adapter")
    p.add_argument("--source-dir", help="Vibe session logs directory")
    p.add_argument("--source-session", help="Single Vibe session directory")
    p.add_argument("--output-dir", default="./data/sessions")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.discover:
        discover_format(args.source_dir or str(DEFAULT_SESSION_DIR))
        return

    session_dirs = []
    if args.source_session:
        session_dirs.append(Path(args.source_session))
    elif args.source_dir:
        source = Path(args.source_dir)
        for d in sorted(source.iterdir()):
            if d.is_dir() and (d / "meta.json").exists():
                session_dirs.append(d)
    else:
        if DEFAULT_SESSION_DIR.exists():
            for d in sorted(DEFAULT_SESSION_DIR.iterdir()):
                if d.is_dir() and (d / "meta.json").exists():
                    session_dirs.append(d)

    if not session_dirs:
        print("No Vibe sessions found.")
        sys.exit(1)

    session_index = []
    total = 0
    errors = 0
    quality_counts = defaultdict(int)

    for sdir in session_dirs:
        try:
            session, quality = convert_session(sdir, verbose=args.verbose)
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
                    "agent_framework": "vibe",
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
            print(f"  ERROR {sdir.name}: {type(e).__name__}", file=sys.stderr)
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
