#!/usr/bin/env python3
"""
minitrace OpenCode adapter — converts OpenCode session data to minitrace v0.1.0 format.

Usage:
    python3 minitrace-opencode-adapter.py --discover
    python3 minitrace-opencode-adapter.py --output-dir ./data/sessions/
    python3 minitrace-opencode-adapter.py --session-id ses_xxx --output-dir ./data/sessions/

OpenCode native format (discovered 2026-03-15 against OpenCode 1.2.20):

  SQLite database at ~/.local/share/opencode/opencode.db
  Tables: session, message, part, project, workspace, permission, todo, control_account

  session — id, project_id, title, directory, version, time_created, time_updated,
            summary_additions, summary_deletions, summary_files, summary_diffs, permission
  message — id, session_id, time_created, time_updated, data (JSON)
  part    — id, message_id, session_id, time_created, time_updated, data (JSON)

  message.data JSON:
    {role: "user"|"assistant", time: {created}, agent, model: {providerID, modelID},
     summary: {diffs: [...]}}

  part.data JSON — typed content blocks:
    {type: "text", text: "..."}
    {type: "reasoning", text: "...", time: {start, end}}
    {type: "tool", callID, tool, state: {status, input: {...}, output: "..."}}
    {type: "step-start", snapshot}
    {type: "step-finish", reason, cost, tokens: {total, input, output, reasoning, cache: {read, write}}}

  Tool names: grep, bash, read, write, edit, glob, fetch (from OpenCode's built-in tools)
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
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
    parse_timestamp,
    safe_fromtimestamp,
    truncate_content,
    write_session,
    write_manifests,
    assign_quality_tier,
)

ADAPTER_VERSION = "minitrace-opencode-adapter-0.1.0"
SOURCE_FORMAT = "opencode-sqlite-v1"

DEFAULT_DB_PATHS = [
    Path.home() / ".local" / "share" / "opencode" / "opencode.db",
]


def classify_operation(tool_name, arguments=None):
    """Map OpenCode tool names to minitrace operation_type."""
    mapping = {
        "bash": "EXECUTE",
        "shell": "EXECUTE",
        "read": "READ",
        "read_file": "READ",
        "write": "NEW",
        "write_file": "NEW",
        "edit": "MODIFY",
        "edit_file": "MODIFY",
        "glob": "READ",
        "grep": "READ",
        "search": "READ",
        "fetch": "READ",
        "list_files": "READ",
    }
    return mapping.get(tool_name, "OTHER")


def extract_file_path(tool_name, arguments):
    """Extract file path from tool arguments."""
    if not isinstance(arguments, dict):
        return None
    for key in ("file_path", "path", "file", "filename", "pattern"):
        if key in arguments:
            return arguments[key]
    return None


def find_db():
    """Find the OpenCode database."""
    for path in DEFAULT_DB_PATHS:
        if path.exists():
            return path
    return None


def load_sessions(db_path, session_id=None):
    """Load session metadata from OpenCode database."""
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    if session_id:
        rows = db.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM session ORDER BY time_created").fetchall()

    sessions = [dict(row) for row in rows]
    db.close()
    return sessions


def load_messages_and_parts(db_path, session_id):
    """Load messages and their parts for a session.

    Returns list of (message_dict, [part_dicts]) tuples ordered by time.
    """
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    messages = db.execute(
        "SELECT * FROM message WHERE session_id = ? ORDER BY time_created",
        (session_id,)
    ).fetchall()

    result = []
    for msg in messages:
        msg_dict = dict(msg)
        msg_dict["_data"] = json.loads(msg_dict["data"]) if msg_dict.get("data") else {}

        parts = db.execute(
            "SELECT * FROM part WHERE message_id = ? ORDER BY time_created",
            (msg_dict["id"],)
        ).fetchall()

        part_dicts = []
        for p in parts:
            pd = dict(p)
            pd["_data"] = json.loads(pd["data"]) if pd.get("data") else {}
            part_dicts.append(pd)

        result.append((msg_dict, part_dicts))

    db.close()
    return result


def convert_session(session_row, db_path, verbose=False):
    """Convert an OpenCode session to minitrace format."""
    session_id = session_row["id"]
    msg_parts = load_messages_and_parts(db_path, session_id)

    turns = []
    tool_calls = []
    all_timestamps = []
    token_totals = {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0}

    turn_index = 0
    pending_tool_calls = {}
    model_info = {}

    for msg_dict, parts in msg_parts:
        msg_data = msg_dict.get("_data", {})
        role = msg_data.get("role", "user")
        msg_ts_ms = msg_dict.get("time_created", 0)
        msg_ts = safe_fromtimestamp(msg_ts_ms) if msg_ts_ms else None
        msg_ts_str = format_timestamp(msg_ts) if msg_ts else None
        if msg_ts:
            all_timestamps.append(msg_ts)

        # Extract model info
        model = msg_data.get("model", {})
        if isinstance(model, dict) and model.get("modelID"):
            model_info["model"] = model["modelID"]
            model_info["provider"] = model.get("providerID", "unknown")

        # Process parts into content
        text_parts = []
        thinking_parts = []
        tc_ids_in_turn = []
        turn_usage = None

        for pd in parts:
            pdata = pd.get("_data", {})
            ptype = pdata.get("type", "")

            if ptype == "text":
                text_parts.append(pdata.get("text", ""))

            elif ptype == "reasoning":
                thinking_parts.append(pdata.get("text", ""))

            elif ptype == "tool":
                tc_id = pdata.get("callID", f"tc_{turn_index}_{len(tool_calls)}")
                tc_name = pdata.get("tool", "unknown")
                state = pdata.get("state", {})
                tc_input = state.get("input", {})
                tc_output = state.get("output", "")
                tc_status = state.get("status", "completed")

                file_path = extract_file_path(tc_name, tc_input)
                command = tc_input.get("command") if tc_name in ("bash", "shell") else None

                tc = build_tool_call(
                    tc_id=tc_id,
                    turn_index=turn_index,
                    timestamp=msg_ts_str,
                    tool_name=tc_name,
                    operation_type=classify_operation(tc_name, tc_input),
                    file_path=file_path,
                    command=command,
                    arguments=tc_input,
                    success=tc_status != "error",
                    result=tc_output if tc_status == "completed" else None,
                    error=tc_output if tc_status == "error" else None,
                )
                tool_calls.append(tc)
                tc_ids_in_turn.append(tc_id)

            elif ptype == "step-finish":
                tokens = pdata.get("tokens", {})
                if isinstance(tokens, dict):
                    inp = tokens.get("input", 0)
                    out = tokens.get("output", 0)
                    reasoning = tokens.get("reasoning", 0)
                    cache = tokens.get("cache", {})
                    cache_read = cache.get("read", 0) if isinstance(cache, dict) else 0

                    token_totals["input"] += inp
                    token_totals["output"] += out
                    token_totals["reasoning"] += reasoning
                    token_totals["cache_read"] += cache_read

                    turn_usage = {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_read_tokens": cache_read,
                        "cache_creation_tokens": cache.get("write", 0) if isinstance(cache, dict) else None,
                        "reasoning_tokens": reasoning,
                        "tool_tokens": None,
                    }

        # Determine source
        if role == "user":
            source = "human"
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
            usage=turn_usage,
        ))
        turn_index += 1

    # Deduplicate
    tool_calls, dupe_count = deduplicate_tool_calls(tool_calls)

    # Build session
    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="opencode",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    session["title"] = session_row.get("title") or extract_title(turns)
    session["provenance"]["source_path"] = str(db_path)

    session["environment"]["model"] = model_info.get("model", "unknown")
    session["environment"]["provider_hint"] = model_info.get("provider", "unknown")
    session["environment"]["agent_framework"] = "opencode"
    session["environment"]["tools_enabled"] = sorted(set(
        tc["tool_name"] for tc in tool_calls
    ))

    session["operational_context"]["working_directory"] = session_row.get("directory")

    # Diffs summary from session metadata
    if session_row.get("summary_additions") is not None:
        session["environment"]["framework_config"] = {
            "summary_additions": session_row.get("summary_additions"),
            "summary_deletions": session_row.get("summary_deletions"),
            "summary_files": session_row.get("summary_files"),
        }

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


def discover_format(db_path):
    """Discovery mode: inspect OpenCode database structure."""
    if not db_path or not Path(db_path).exists():
        db_path = find_db()
    if not db_path:
        print("OpenCode database not found.")
        return

    print(f"=== OpenCode database: {db_path} ===")
    db = sqlite3.connect(str(db_path))

    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    print(f"Tables: {[t[0] for t in tables]}")

    sessions = db.execute("SELECT id, title, directory, time_created FROM session ORDER BY time_created DESC LIMIT 10").fetchall()
    print(f"\nRecent sessions ({len(sessions)}):")
    for s in sessions:
        ts_dt = safe_fromtimestamp(s[3]) if s[3] else None
        ts = ts_dt.isoformat() if ts_dt else "?"
        msg_count = db.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (s[0],)).fetchone()[0]
        part_count = db.execute("SELECT COUNT(*) FROM part WHERE session_id = ?", (s[0],)).fetchone()[0]
        print(f"  {s[0]} | {s[1][:60]} | {msg_count} msgs, {part_count} parts | {ts}")

    db.close()


def main():
    p = argparse.ArgumentParser(description="minitrace OpenCode adapter")
    p.add_argument("--source-db", help="Path to OpenCode database")
    p.add_argument("--session-id", help="Convert a single session")
    p.add_argument("--output-dir", default="./data/sessions")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    db_path = args.source_db or find_db()

    if args.discover:
        discover_format(db_path)
        return

    if not db_path or not Path(db_path).exists():
        print("OpenCode database not found. Specify --source-db.")
        sys.exit(1)

    sessions = load_sessions(db_path, args.session_id)
    if not sessions:
        print("No sessions found.")
        sys.exit(1)

    print(f"Found {len(sessions)} sessions in {db_path}")

    session_index = []
    total = 0
    errors = 0
    quality_counts = defaultdict(int)

    for session_row in sessions:
        try:
            session, quality = convert_session(session_row, db_path, verbose=args.verbose)
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
                    "agent_framework": "opencode",
                    "turn_count": len(session["turns"]),
                    "tool_call_count": len(session["tool_calls"]),
                    "file_size_bytes": file_size,
                    "source_format": SOURCE_FORMAT,
                    "title": session.get("title"),
                    "flags": session.get("flags", {}),
                })
                if args.verbose:
                    print(f"  {quality} {session['id'][:30]}... → {file_path} ({file_size:,} bytes)")

            total += 1

        except Exception as e:
            errors += 1
            print(f"  ERROR {session_row['id']}: {type(e).__name__}", file=sys.stderr)
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
