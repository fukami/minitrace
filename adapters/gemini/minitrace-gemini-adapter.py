#!/usr/bin/env python3
"""
minitrace Gemini CLI adapter — converts Gemini CLI session data to minitrace v0.1.0 format.

Usage:
    python3 minitrace-gemini-adapter.py --discover
    python3 minitrace-gemini-adapter.py --source-dir ~/.gemini/tmp/ --output-dir ./data/sessions/
    python3 minitrace-gemini-adapter.py --source-session path/to/session.json --output-dir ./data/sessions/

Gemini CLI native format (discovered 2026-03-15 against Gemini CLI 0.33.1):

  JSON files at ~/.gemini/tmp/<project-hash>/chats/session-YYYY-MM-DDTHH-MM-<hash>.json

  Session JSON keys: kind, lastUpdated, messages, projectHash, sessionId, startTime, summary

  Message format:
    {type: "user"|"gemini", id, timestamp, content, model?, thoughts?, tokens?, toolCalls?}

  content: string (gemini) or list of {text: "..."} blocks (user)

  toolCalls: [{id, name, args: {...}, result: [{functionResponse: {id, name, response: {output: "..."}}}]}]

  tokens: {inputTokens, outputTokens, totalTokens, thoughtsTokens?}

  Gemini tools: grep_search, run_shell_command, read_file, write_file, edit_file
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
    parse_timestamp,
    safe_int,
    truncate_content,
    write_session,
    write_manifests,
    assign_quality_tier,
)

ADAPTER_VERSION = "minitrace-gemini-adapter-0.1.0"
SOURCE_FORMAT = "gemini-cli-session-v1"


def classify_operation(tool_name, arguments=None):
    """Map Gemini CLI tool names to minitrace operation_type."""
    mapping = {
        "grep_search": "READ",
        "read_file": "READ",
        "read_many_files": "READ",
        "list_directory": "READ",
        "write_file": "NEW",
        "write_new_file": "NEW",
        "edit_file": "MODIFY",
        "replace_in_file": "MODIFY",
        "run_shell_command": "EXECUTE",
        "shell": "EXECUTE",
        "web_search": "READ",
        "search_web": "READ",
    }
    return mapping.get(tool_name, "OTHER")


def extract_file_path(tool_name, arguments):
    """Extract file path from tool arguments."""
    if not isinstance(arguments, dict):
        return None
    for key in ("file_path", "path", "file", "filename", "filepath"):
        if key in arguments:
            return arguments[key]
    return None


def convert_session(session_data, source_path=None, verbose=False):
    """Convert a Gemini CLI session JSON to minitrace format."""
    session_id = session_data.get("sessionId", "unknown")
    messages = session_data.get("messages", [])

    turns = []
    tool_calls = []
    all_timestamps = []
    token_totals = {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0}

    turn_index = 0

    # Session-level timestamps
    start_ts = parse_timestamp(session_data.get("startTime"))
    end_ts = parse_timestamp(session_data.get("lastUpdated"))
    if start_ts:
        all_timestamps.append(start_ts)
    if end_ts:
        all_timestamps.append(end_ts)

    model_name = None

    for msg in messages:
        msg_type = msg.get("type", "")
        msg_ts_str = msg.get("timestamp")
        msg_ts = parse_timestamp(msg_ts_str)
        if msg_ts:
            all_timestamps.append(msg_ts)

        # Extract model
        if msg.get("model") and not model_name:
            model_name = msg["model"]

        # Extract tokens
        tokens = msg.get("tokens", {})
        if isinstance(tokens, dict):
            token_totals["input"] += safe_int(tokens.get("inputTokens"))
            token_totals["output"] += safe_int(tokens.get("outputTokens"))
            token_totals["reasoning"] += safe_int(tokens.get("thoughtsTokens"))

        usage = None
        if tokens:
            usage = {
                "input_tokens": tokens.get("inputTokens"),
                "output_tokens": tokens.get("outputTokens"),
                "cache_read_tokens": None,
                "cache_creation_tokens": None,
                "reasoning_tokens": tokens.get("thoughtsTokens"),
                "tool_tokens": None,
            }

        # Content
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and "text" in b]
            content = "\n".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)

        # Thinking
        thinking = msg.get("thoughts")

        # Tool calls
        tc_ids_in_turn = []
        raw_tool_calls = msg.get("toolCalls", [])

        for tc_raw in raw_tool_calls:
            tc_id = tc_raw.get("id", f"tc_{turn_index}_{len(tool_calls)}")
            tc_name = tc_raw.get("name", "unknown")
            tc_args = tc_raw.get("args", {})

            file_path = extract_file_path(tc_name, tc_args)
            command = tc_args.get("command") if tc_name in ("run_shell_command", "shell") else None

            # Extract result from embedded functionResponse
            result_text = None
            success = True
            error = None
            results = tc_raw.get("result", [])
            for r in results:
                if isinstance(r, dict):
                    fr = r.get("functionResponse", {})
                    resp = fr.get("response", {})
                    if isinstance(resp, dict):
                        result_text = resp.get("output", "")
                        if resp.get("error"):
                            success = False
                            error = resp["error"]

            tc = build_tool_call(
                tc_id=tc_id,
                turn_index=turn_index,
                timestamp=msg_ts_str,
                tool_name=tc_name,
                operation_type=classify_operation(tc_name, tc_args),
                file_path=file_path,
                command=command,
                arguments=tc_args,
                success=success,
                result=result_text,
                error=error,
            )
            tool_calls.append(tc)
            tc_ids_in_turn.append(tc_id)

        # Build turn
        if msg_type == "user":
            source = "human"
            role = "user"
        elif msg_type == "gemini":
            source = "model"
            role = "assistant"
        else:
            source = "framework"
            role = "system"

        turns.append(build_turn(
            index=turn_index,
            timestamp=msg_ts_str,
            role=role,
            source=source,
            content=content if content else None,
            tool_calls_in_turn=tc_ids_in_turn,
            thinking=thinking,
            usage=usage,
        ))
        turn_index += 1

    # Deduplicate
    tool_calls, dupe_count = deduplicate_tool_calls(tool_calls)

    # Build session
    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="gemini-cli",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    session["title"] = session_data.get("summary") or extract_title(turns)
    session["provenance"]["source_path"] = str(source_path) if source_path else None

    session["environment"]["model"] = model_name or "gemini-3-flash-preview"
    session["environment"]["provider_hint"] = "google"
    session["environment"]["agent_framework"] = "gemini-cli"
    session["environment"]["tools_enabled"] = sorted(set(
        tc["tool_name"] for tc in tool_calls
    ))

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

    print(f"=== Gemini CLI sessions in {source} ===")
    for json_file in sorted(source.rglob("session-*.json")):
        try:
            d = json.loads(json_file.read_text())
            msgs = len(d.get("messages", []))
            tcs = sum(len(m.get("toolCalls", [])) for m in d.get("messages", []))
            print(f"  {json_file.relative_to(source)}")
            print(f"    id: {d.get('sessionId','?')[:20]} msgs: {msgs} tool_calls: {tcs}")
            print(f"    summary: {d.get('summary','?')[:60]}")
        except Exception as e:
            print(f"  {json_file}: ERROR {e}")


def main():
    p = argparse.ArgumentParser(description="minitrace Gemini CLI adapter")
    p.add_argument("--source-dir", help="Gemini tmp directory (e.g., ~/.gemini/tmp/)")
    p.add_argument("--source-session", help="Single Gemini session JSON file")
    p.add_argument("--output-dir", default="./data/sessions")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.discover:
        discover_format(args.source_dir or str(Path.home() / ".gemini" / "tmp"))
        return

    files = []
    if args.source_session:
        files.append(Path(args.source_session))
    elif args.source_dir:
        source = Path(args.source_dir)
        files = sorted(source.rglob("session-*.json"))
    else:
        default = Path.home() / ".gemini" / "tmp"
        if default.exists():
            files = sorted(default.rglob("session-*.json"))

    if not files:
        print("No Gemini sessions found.")
        sys.exit(1)

    session_index = []
    total = 0
    errors = 0
    quality_counts = defaultdict(int)

    for json_path in files:
        try:
            with safe_open(json_path) as f:
                data = json.load(f)
            session, quality = convert_session(data, source_path=json_path, verbose=args.verbose)
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
                    "agent_framework": "gemini-cli",
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
            print(f"  ERROR {json_path.name}: {type(e).__name__}", file=sys.stderr)
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
