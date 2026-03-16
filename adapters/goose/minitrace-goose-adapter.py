#!/usr/bin/env python3
"""
minitrace Goose adapter — converts Goose (Block) session data to minitrace v0.1.0 format.

Usage:
    # Convert all sessions from default location
    python3 minitrace-goose-adapter.py --source-db ~/.local/share/goose/sessions/sessions.db

    # Convert a single session
    python3 minitrace-goose-adapter.py --source-db ~/.local/share/goose/sessions/sessions.db --session-id 20260315_3

    # Auto-detect default DB location
    python3 minitrace-goose-adapter.py

    # Discovery mode
    python3 minitrace-goose-adapter.py --discover

Goose native format (discovered 2026-03-15 against goose v3.27.0):

  SQLite database at ~/.local/share/goose/sessions/sessions.db
  Schema version 7 (tracked in schema_version table).

  Tables:
    sessions — id, name, working_dir, provider_name, tokens, created_at, model_config_json
    messages — id, message_id, session_id, role, content_json, created_timestamp, tokens

  content_json is a JSON array of typed content blocks:
    - {type: "text", text: "..."}
    - {type: "reasoning", text: "..."}  (streamed as multiple chunks)
    - {type: "toolRequest", id: "call_xxx", toolCall: {status, value: {name, arguments}}, _meta: {goose_extension}}
    - {type: "toolResponse", id: "call_xxx", toolResult: {status, value: {content: [...], isError}}}

  Tool names come from MCP extensions. Built-in "developer" extension provides:
    tree, read, write, edit, shell, text_editor, list_directory, search_files

  The _meta.goose_extension field identifies which extension provided each tool.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
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
    normalize_path,
    now_utc,
    parse_timestamp,
    safe_fromtimestamp,
    truncate_content,
    write_session,
    write_manifests,
    assign_quality_tier,
)

ADAPTER_VERSION = "minitrace-goose-adapter-0.1.0"
SOURCE_FORMAT = "goose-sqlite-v7"


# --- Operation Type Mapping ---

def classify_operation(tool_name):
    """Map Goose tool names to minitrace operation_type.

    Goose tools come from MCP extensions. The developer extension is built-in
    and provides the core file/shell tools. Other extensions add domain-specific
    tools (computercontroller, memory, etc.).
    """
    mapping = {
        # Developer extension (built-in)
        "read": "READ",
        "read_file": "READ",
        "tree": "READ",
        "list_directory": "READ",
        "search_files": "READ",
        "text_editor": "MODIFY",  # view/insert/replace subcommands
        "write": "NEW",
        "write_file": "NEW",
        "edit": "MODIFY",
        "edit_file": "MODIFY",
        "shell": "EXECUTE",
        "run_command": "EXECUTE",
        # Computercontroller extension
        "screenshot": "READ",
        "click": "EXECUTE",
        "type_text": "EXECUTE",
        "scroll": "EXECUTE",
        # Memory extension
        "remember": "OTHER",
        "recall": "READ",
    }
    return mapping.get(tool_name, "OTHER")


# --- Database Access ---

DEFAULT_DB_PATHS = [
    Path.home() / ".local" / "share" / "goose" / "sessions" / "sessions.db",
    Path.home() / "Library" / "Application Support" / "Block" / "goose" / "sessions" / "sessions.db",
]


def find_db():
    """Find the Goose sessions database."""
    for path in DEFAULT_DB_PATHS:
        if path.exists():
            return path
    return None


def load_sessions(db_path, session_id=None):
    """Load session metadata from the database.

    Returns list of session dicts.
    """
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    if session_id:
        rows = db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()

    sessions = [dict(row) for row in rows]
    db.close()
    return sessions


def load_messages(db_path, session_id):
    """Load messages for a session, ordered by id.

    Returns list of message dicts with parsed content_json.
    """
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    rows = db.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,)
    ).fetchall()

    messages = []
    for row in rows:
        msg = dict(row)
        # Parse content_json
        try:
            msg["content"] = json.loads(msg.get("content_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            msg["content"] = []
        # Parse metadata_json
        try:
            msg["metadata"] = json.loads(msg.get("metadata_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            msg["metadata"] = {}
        messages.append(msg)

    db.close()
    return messages


# --- Session Conversion ---

def convert_session(session_meta, messages):
    """Convert a Goose session (metadata + messages) into a minitrace session.

    Args:
        session_meta: dict from sessions table
        messages: list of message dicts from messages table (with parsed content)

    Returns: (session, quality)
    """
    session_id = session_meta["id"]
    turns = []
    tool_calls = []
    annotations = []
    all_timestamps = []
    token_totals = {"input": 0, "output": 0}
    current_thinking = []

    turn_index = 0
    tc_index = 0
    pending_tool_calls = {}  # call_id -> tool_call dict

    for msg in messages:
        role = msg["role"]
        content_blocks = msg.get("content", [])
        ts_epoch = msg.get("created_timestamp")
        ts = None
        ts_str = None
        if ts_epoch:
            ts = safe_fromtimestamp(ts_epoch)
            if ts:
                ts_str = format_timestamp(ts)
                all_timestamps.append(ts)

        # Accumulate tokens
        msg_tokens = msg.get("tokens")
        if msg_tokens and role == "assistant":
            token_totals["output"] += msg_tokens
        elif msg_tokens and role == "user":
            token_totals["input"] += msg_tokens

        # Process content blocks
        text_parts = []
        thinking_parts = []
        tc_ids_in_turn = []

        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")

            if btype == "text":
                text_parts.append(block.get("text", ""))

            elif btype == "reasoning":
                thinking_parts.append(block.get("text", ""))

            elif btype == "toolRequest":
                call_id = block.get("id", f"tc-goose-{tc_index:04d}")
                tool_call_data = block.get("toolCall", {})
                value = tool_call_data.get("value", {}) if isinstance(tool_call_data, dict) else {}
                tool_name = value.get("name", "unknown")
                arguments = value.get("arguments", {})

                # Extract file_path and command from arguments
                file_path = arguments.get("path") or arguments.get("file_path")
                command = arguments.get("command") or arguments.get("cmd")

                # Framework metadata: which extension provided this tool
                meta = block.get("_meta", {})
                fm = {}
                if meta.get("goose_extension"):
                    fm["goose_extension"] = meta["goose_extension"]

                tc = build_tool_call(
                    tc_id=call_id,
                    turn_index=turn_index,
                    timestamp=ts_str,
                    tool_name=tool_name,
                    operation_type=classify_operation(tool_name),
                    file_path=file_path,
                    command=command,
                    arguments=arguments,
                    framework_metadata=fm if fm else None,
                )
                tool_calls.append(tc)
                pending_tool_calls[call_id] = tc
                tc_ids_in_turn.append(call_id)
                tc_index += 1

            elif btype == "toolResponse":
                call_id = block.get("id", "")
                result_data = block.get("toolResult", {})
                value = result_data.get("value", {}) if isinstance(result_data, dict) else {}
                is_error = value.get("isError", False)

                # Extract text content from result
                result_content = value.get("content", [])
                result_text_parts = []
                for rc in result_content:
                    if isinstance(rc, dict) and rc.get("type") == "text":
                        result_text_parts.append(rc.get("text", ""))
                result_text = "\n".join(result_text_parts)

                tc = pending_tool_calls.pop(call_id, None)
                if tc:
                    truncated, full_bytes, full_hash = truncate_content(result_text)
                    tc["output"]["result"] = truncated
                    tc["output"]["success"] = not is_error
                    tc["output"]["error"] = result_text[:1024] if is_error else None
                    tc["output"]["truncated"] = full_bytes is not None
                    tc["output"]["full_bytes"] = full_bytes
                    tc["output"]["full_hash"] = full_hash

        # Build turn from accumulated content
        content = "\n".join(text_parts) if text_parts else None
        thinking = "\n".join(thinking_parts) if thinking_parts else None

        # Determine source and role mapping
        if role == "user":
            # User messages with toolResponse blocks are framework-generated
            has_tool_response = any(
                b.get("type") == "toolResponse" for b in content_blocks
                if isinstance(b, dict)
            )
            if has_tool_response:
                source = "framework"
            else:
                source = "human"
            mapped_role = "user"
        elif role == "assistant":
            source = "model"
            mapped_role = "assistant"
        elif role == "system":
            source = "framework"
            mapped_role = "system"
        else:
            source = None
            mapped_role = role

        # Skip empty assistant turns (reasoning-only with no text and no tool calls)
        if mapped_role == "assistant" and not content and not tc_ids_in_turn:
            # Still accumulate thinking for the next turn
            if thinking:
                current_thinking.extend(thinking_parts)
            continue

        # Merge accumulated thinking from previous empty turns
        if current_thinking:
            if thinking:
                thinking = "\n".join(current_thinking) + "\n" + thinking
            else:
                thinking = "\n".join(current_thinking)
            current_thinking = []

        # Build usage for this turn
        usage = None
        if msg_tokens:
            if role == "assistant":
                usage = {
                    "input_tokens": None,
                    "output_tokens": msg_tokens,
                    "cache_read_tokens": None,
                    "cache_creation_tokens": None,
                    "reasoning_tokens": None,
                    "tool_tokens": None,
                }

        turn = build_turn(
            index=turn_index,
            timestamp=ts_str,
            role=mapped_role,
            source=source,
            content=content or "",
            tool_calls_in_turn=tc_ids_in_turn,
            thinking=thinking,
            usage=usage,
        )
        turns.append(turn)
        turn_index += 1

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

    # Timing
    timing = compute_timing(all_timestamps)
    # Supplement from session metadata if timestamps sparse
    if not timing["started_at"] and session_meta.get("created_at"):
        sa = parse_timestamp(session_meta["created_at"])
        if sa:
            timing["started_at"] = format_timestamp(sa)
            timing["hour_of_day"] = sa.hour
            timing["day_of_week"] = sa.weekday()

    # Quality
    quality = assign_quality_tier(turns, tool_calls)
    contains_pii = detect_pii_in_paths(tool_calls)

    # Build session
    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="goose",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    # Environment
    provider = session_meta.get("provider_name") or "unknown"
    session["environment"]["model"] = "unknown"
    session["environment"]["provider_hint"] = (
        "openai-compatible" if provider == "ollama" else provider
    )

    # Parse model_config_json if available
    model_config = None
    if session_meta.get("model_config_json"):
        try:
            model_config = json.loads(session_meta["model_config_json"])
            if isinstance(model_config, dict):
                session["environment"]["model"] = (
                    model_config.get("model_name")
                    or model_config.get("model")
                    or "unknown"
                )
                session["environment"]["temperature"] = model_config.get("temperature")
        except (json.JSONDecodeError, TypeError):
            pass

    session["environment"]["tools_enabled"] = list(set(
        tc["tool_name"] for tc in tool_calls
    ))

    # Operational context
    session["operational_context"]["working_directory"] = session_meta.get("working_dir")

    # Framework config — Goose-specific
    framework_config = {}
    if session_meta.get("session_type") and session_meta["session_type"] != "user":
        framework_config["session_type"] = session_meta["session_type"]
    if session_meta.get("extension_data") and session_meta["extension_data"] != "{}":
        try:
            ext_data = json.loads(session_meta["extension_data"])
            if ext_data:
                framework_config["extension_data"] = ext_data
        except (json.JSONDecodeError, TypeError):
            pass
    # Collect which extensions were used (from tool call metadata)
    extensions_used = set()
    for tc in tool_calls:
        fm = tc.get("framework_metadata") or {}
        ext = fm.get("goose_extension")
        if ext:
            extensions_used.add(ext)
    if extensions_used:
        framework_config["extensions_used"] = sorted(extensions_used)
    if framework_config:
        session["operational_context"]["framework_config"] = framework_config

    # Token data from session metadata (more reliable than per-message)
    if session_meta.get("total_tokens"):
        token_totals["input"] = session_meta.get("input_tokens") or 0
        token_totals["output"] = session_meta.get("output_tokens") or 0

    # Fill session
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
    p = argparse.ArgumentParser(description="minitrace Goose adapter")
    p.add_argument("--source-db", help="Path to Goose sessions.db")
    p.add_argument("--session-id", help="Convert a single session")
    p.add_argument("--output-dir", default="./data/sessions")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--discover", action="store_true",
                   help="Discovery mode: inspect database structure")
    return p.parse_args()


def discover_format_cli(db_path):
    """Discovery mode: inspect Goose database and print structure."""
    print(f"=== Goose SQLite Discovery: {db_path} ===")
    db = sqlite3.connect(str(db_path))

    # Schema version
    try:
        ver = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        print(f"Schema version: {ver}")
    except sqlite3.OperationalError:
        print("No schema_version table")

    # Tables
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"Tables: {tables}\n")

    _safe_table_re = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    for table in tables:
        if table.startswith("sqlite_") or table == "schema_version":
            continue
        if not _safe_table_re.match(table):
            print(f"  {table}: SKIPPED (unsafe table name)")
            continue
        cols = [(row[1], row[2]) for row in db.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()]
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table} ({count} rows):")
        for name, dtype in cols:
            print(f"    {name}: {dtype}")
        print()

    # Sessions summary
    print("Sessions:")
    for row in db.execute(
        "SELECT id, name, provider_name, total_tokens, created_at FROM sessions ORDER BY created_at"
    ).fetchall():
        print(f"  {row[0]:20} provider={row[2]:10} tokens={row[3]} created={row[4]}")

    # Message content types
    print("\nContent block types across all messages:")
    type_counts = defaultdict(int)
    for row in db.execute("SELECT content_json FROM messages").fetchall():
        try:
            content = json.loads(row[0])
            for block in content:
                if isinstance(block, dict):
                    type_counts[block.get("type", "?")] += 1
        except (json.JSONDecodeError, TypeError):
            pass
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c}")

    db.close()


def main():
    args = parse_args()

    # Find database
    db_path = Path(args.source_db) if args.source_db else find_db()
    if not db_path or not db_path.exists():
        print("Goose sessions database not found.", file=sys.stderr)
        print("Try: --source-db ~/.local/share/goose/sessions/sessions.db", file=sys.stderr)
        sys.exit(1)

    if args.discover:
        discover_format_cli(db_path)
        return

    # Load sessions
    session_metas = load_sessions(db_path, args.session_id)
    if not session_metas:
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(session_metas)} sessions in {db_path}")

    session_index = []
    quality_counts = defaultdict(int)
    errors = 0

    for meta in session_metas:
        sid = meta["id"]
        try:
            messages = load_messages(db_path, sid)
            if not messages:
                quality_counts["D"] += 1
                continue

            session, quality = convert_session(meta, messages)
            quality_counts[quality] += 1

            if args.dry_run:
                tc = session["metrics"]["tool_call_count"]
                tn = session["metrics"]["turn_count"]
                model = session["environment"]["model"]
                provider = session["environment"]["provider_hint"]
                if args.verbose or args.session_id:
                    print(f"  {quality} {sid} turns={tn} tools={tc} model={model} provider={provider}")
            else:
                file_path, file_size, period, _ = write_session(
                    session, args.output_dir, quality,
                )
                if args.verbose or args.session_id:
                    print(f"  {quality} {sid} → {file_path} ({file_size:,} bytes)")

            session_index.append({
                "id": session["id"],
                "profile": session["profile"],
                "title": session.get("title"),
                "classification": session["classification"],
                "quality": quality,
                "started_at": session["timing"].get("started_at"),
                "duration_seconds": session["timing"].get("duration_seconds"),
                "model": session["environment"]["model"],
                "agent_framework": "goose",
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
                print(f"  ERROR {sid}: {type(e).__name__}", file=sys.stderr)
                import traceback
                traceback.print_exc()

    # Manifests
    if not args.dry_run and session_index:
        write_manifests(session_index, args.output_dir)

    # Summary
    total = sum(quality_counts.values())
    print(f"\n--- Summary ---")
    print(f"Total: {total}")
    print(f"Quality: A={quality_counts.get('A',0)} B={quality_counts.get('B',0)} "
          f"C={quality_counts.get('C',0)} D={quality_counts.get('D',0)}")
    print(f"Errors: {errors}")


if __name__ == "__main__":
    main()
