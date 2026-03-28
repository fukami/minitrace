#!/usr/bin/env python3
"""
minitrace claude.ai export adapter — converts claude.ai data export to minitrace v0.2.0 format.

Usage:
    python3 minitrace-claude-ai-adapter.py --source data-export.zip
    python3 minitrace-claude-ai-adapter.py --source data-export.zip --uuid-filter e9b76845,49e157c3
    python3 minitrace-claude-ai-adapter.py --source data-export.zip --output-dir ./output/
    python3 minitrace-claude-ai-adapter.py --source data-export.zip --dry-run --verbose

claude.ai data export format (Settings > Privacy > Export data):

  ZIP containing conversations.json (can be 470MB+), users.json, projects.json,
  memories.json. conversations.json is an array of conversation objects.

  Content blocks: text, tool_use, tool_result.
  Tool IDs are always null — pairing is positional (TU at i, TR at i+1).
  Model identifier not included in export.
  Token counts not available.

See the adapter docstring and format-discovery.md for format details.
"""

import argparse
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from minitrace_common import (
    build_session_skeleton,
    build_tool_call,
    build_turn,
    build_annotation,
    compute_metrics,
    compute_timing,
    compute_tool_call_context,
    assign_quality_tier,
    canary_check,
    format_timestamp,
    parse_timestamp,
    write_session,
    write_manifests,
)

ADAPTER_VERSION = "minitrace-claude-ai-adapter-0.2.0"
SOURCE_FORMAT = "claude-ai-export-v1"

# Redaction marker in tool results
REDACTED_MARKER = "This block is not supported on your current device yet."


# --- Operation Type Mapping ---

_OPERATION_TYPE = {
    # READ
    "view": "READ",
    "web_search": "READ",
    "web_fetch": "READ",
    "project_knowledge_search": "READ",
    "conversation_search": "READ",
    "recent_chats": "READ",
    "present_files": "READ",
    # MCP filesystem reads
    "Filesystem:read_file": "READ",
    "Filesystem:read_text_file": "READ",
    "Filesystem:read_multiple_files": "READ",
    "Filesystem:get_file_info": "READ",
    "Filesystem:list_directory": "READ",
    "Filesystem:directory_tree": "READ",
    "Filesystem:search_files": "READ",
    "Filesystem:list_allowed_directories": "READ",
    # MODIFY
    "str_replace": "MODIFY",
    "memory_user_edits": "MODIFY",
    "Filesystem:edit_file": "MODIFY",
    "Filesystem:move_file": "MODIFY",
    # NEW
    "create_file": "NEW",
    "artifacts": "NEW",
    "Filesystem:write_file": "NEW",
    "Filesystem:create_directory": "NEW",
    "Filesystem:copy_file_user_to_claude": "NEW",
    # EXECUTE
    "bash_tool": "EXECUTE",
    "repl": "EXECUTE",
    # DELEGATE
    "launch_extended_search_task": "DELEGATE",
}


def classify_operation(tool_name):
    """Map claude.ai tool name to minitrace operation_type."""
    if tool_name in _OPERATION_TYPE:
        return _OPERATION_TYPE[tool_name]
    # Unknown MCP tools default to OTHER
    return "OTHER"


# --- Content Origin Mapping ---

_CONTENT_ORIGIN = {
    "bash_tool": "local_exec",
    "repl": "local_exec",
    "view": "local_file",
    "Filesystem:read_file": "local_file",
    "Filesystem:read_text_file": "local_file",
    "Filesystem:read_multiple_files": "local_file",
    "web_search": "web",
    "web_fetch": "web",
    "project_knowledge_search": "database",
    "conversation_search": "database",
    "recent_chats": "database",
    "artifacts": "model_echo",
    "create_file": "model_echo",
    "str_replace": "model_echo",
    "Filesystem:write_file": "model_echo",
    "Filesystem:edit_file": "model_echo",
    "Filesystem:copy_file_user_to_claude": "user_provided",
    "launch_extended_search_task": "sub_agent",
    "memory_user_edits": "model_echo",
}


def classify_content_origin(tool_name):
    """Map claude.ai tool name to ToolCall.output.content_origin."""
    if tool_name in _CONTENT_ORIGIN:
        return _CONTENT_ORIGIN[tool_name]
    # MCP tools (integration_name present) would be mcp_server,
    # but we classify by name here; caller can override if integration_name is set
    return None


def is_mcp_tool(tool_use_block):
    """Check if a tool_use block is an MCP tool."""
    return tool_use_block.get("integration_name") is not None


# --- Streaming JSON Parser ---

def stream_conversations(zip_path, uuid_filter=None):
    """Iterate conversations from the ZIP using incremental JSON decoding.

    Loads conversations.json into memory as text, then uses raw_decode to
    yield one conversation dict at a time (avoids constructing all Python
    objects simultaneously). For 470MB+ files this needs ~1.5GB RAM.

    Args:
        zip_path: path to the data export ZIP
        uuid_filter: optional set of UUID prefixes to include (prefix match)
    """
    # Size limit for decompressed conversations.json (2 GB — claude.ai exports
    # can legitimately reach 470MB+, so the limit is generous)
    MAX_DECOMPRESSED = 2 * 1024 * 1024 * 1024

    zf = zipfile.ZipFile(zip_path, "r")
    try:
        info = zf.getinfo("conversations.json")
    except KeyError:
        zf.close()
        raise ValueError("ZIP does not contain conversations.json")
    if info.file_size > MAX_DECOMPRESSED:
        zf.close()
        raise ValueError(
            f"conversations.json too large: {info.file_size:,} bytes "
            f"(limit {MAX_DECOMPRESSED:,})"
        )
    with zf.open("conversations.json") as f:
        raw = f.read(MAX_DECOMPRESSED + 1)
        if len(raw) > MAX_DECOMPRESSED:
            zf.close()
            raise ValueError(
                f"conversations.json decompressed beyond limit: "
                f">{MAX_DECOMPRESSED:,} bytes"
            )
    zf.close()

    text = raw.decode("utf-8", errors="replace")
    del raw  # free the bytes copy

    decoder = json.JSONDecoder()
    pos = 0

    # Skip opening bracket
    while pos < len(text) and text[pos] in " \n\r\t":
        pos += 1
    if pos < len(text) and text[pos] == "[":
        pos += 1

    while pos < len(text):
        # Skip whitespace and commas
        while pos < len(text) and text[pos] in " \n\r\t,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            break

        conv, end_pos = decoder.raw_decode(text, pos)
        pos = end_pos

        # UUID filter: prefix match
        if uuid_filter:
            conv_uuid = conv.get("uuid", "")
            if not any(conv_uuid.startswith(prefix) for prefix in uuid_filter):
                continue

        yield conv

    del text  # free the string


# --- Conversation Conversion ---

def convert_conversation(conv):
    """Convert a single claude.ai conversation to minitrace session.

    Returns: (session_dict, quality_tier)
    """
    conv_uuid = conv.get("uuid", "unknown")
    messages = conv.get("chat_messages", [])

    turns = []
    tool_calls = []
    annotations = []
    all_timestamps = []
    turn_index = 0
    tc_index = 0

    for msg in messages:
        sender = msg.get("sender", "")
        ts_str = msg.get("created_at")
        ts = parse_timestamp(ts_str)
        if ts:
            all_timestamps.append(ts)

        content_blocks = msg.get("content", [])
        text_field = msg.get("text", "")

        if sender == "human":
            # Human turn — use text field for content, include attachments
            content_parts = [text_field or ""]
            attachments = msg.get("attachments") or []
            fw_meta = None

            for att in attachments:
                extracted = att.get("extracted_content", "")
                if extracted:
                    content_parts.append(extracted)

            if attachments:
                fw_meta = {
                    "attachments": [
                        {
                            "file_name": att.get("file_name"),
                            "file_size": att.get("file_size"),
                            "file_type": att.get("file_type"),
                        }
                        for att in attachments
                    ]
                }

            turns.append(build_turn(
                index=turn_index,
                timestamp=format_timestamp(ts) if ts else ts_str,
                role="user",
                source="human",
                content="\n\n".join(p for p in content_parts if p),
                input_channel="user_input",
                framework_metadata=fw_meta,
            ))
            turn_index += 1
            continue

        if sender == "assistant":
            # Assistant turn — process content blocks for text and tool calls
            text_parts = []
            turn_tool_ids = []

            if isinstance(content_blocks, list):
                i = 0
                while i < len(content_blocks):
                    block = content_blocks[i]
                    if not isinstance(block, dict):
                        i += 1
                        continue

                    btype = block.get("type", "")

                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                        i += 1

                    elif btype == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_input = block.get("input", {})
                        integration_name = block.get("integration_name")
                        message = block.get("message")

                        # Timestamps from tool_use block
                        tu_start = parse_timestamp(block.get("start_timestamp"))
                        tu_stop = parse_timestamp(block.get("stop_timestamp"))
                        duration_ms = None
                        if tu_start and tu_stop:
                            duration_ms = int(
                                (tu_stop - tu_start).total_seconds() * 1000
                            )

                        # Determine content_origin
                        origin = classify_content_origin(tool_name)
                        if origin is None and integration_name:
                            origin = "mcp_server"

                        # Extract file_path and command from input
                        file_path = (
                            tool_input.get("file_path")
                            or tool_input.get("path")
                            or tool_input.get("file")
                        )
                        command = tool_input.get("command")

                        # Generate synthetic ID
                        tc_id = f"tc-{conv_uuid[:8]}-{tc_index:04d}"

                        # Pair with tool_result at i+1
                        result_text = None
                        is_error = False
                        is_redacted = False

                        if (i + 1 < len(content_blocks)
                                and isinstance(content_blocks[i + 1], dict)
                                and content_blocks[i + 1].get("type") == "tool_result"):
                            tr_block = content_blocks[i + 1]

                            # Sanity check: name match
                            tr_name = tr_block.get("name", "")
                            if tr_name and tr_name != tool_name:
                                annotations.append(build_annotation(
                                    ann_id=f"ann-mismatch-{tc_id}",
                                    annotator="adapter",
                                    scope_type="tool_call",
                                    target_id=tc_id,
                                    category="observation",
                                    title=f"Tool name mismatch: TU={tool_name} TR={tr_name}",
                                    detail=(
                                        f"Positional pairing produced name mismatch "
                                        f"at content index {i}/{i+1}."
                                    ),
                                    tags=["data-quality", "pairing-mismatch"],
                                ))

                            is_error = tr_block.get("is_error", False)

                            # Extract result text from tool_result content
                            tr_content = tr_block.get("content", [])
                            if isinstance(tr_content, list):
                                parts = []
                                for rc in tr_content:
                                    if isinstance(rc, dict):
                                        parts.append(rc.get("text", ""))
                                    elif isinstance(rc, str):
                                        parts.append(rc)
                                result_text = "\n".join(parts)
                            elif isinstance(tr_content, str):
                                result_text = tr_content

                            # Check for redacted content
                            if result_text and REDACTED_MARKER in result_text:
                                is_redacted = True

                            i += 2  # skip both tool_use and tool_result
                        else:
                            # Orphaned tool_use — no tool_result follows
                            annotations.append(build_annotation(
                                ann_id=f"ann-orphan-{tc_id}",
                                annotator="adapter",
                                scope_type="tool_call",
                                target_id=tc_id,
                                category="observation",
                                title=f"Tool call {tool_name} has no tool_result",
                                detail=(
                                    f"tool_use at content index {i} not followed "
                                    f"by tool_result. Export may be truncated."
                                ),
                                tags=["data-quality", "orphan-tool-call"],
                            ))
                            i += 1

                        # Build framework_metadata
                        fw_meta = None
                        if integration_name or message:
                            fw_meta = {}
                            if integration_name:
                                fw_meta["integration_name"] = integration_name
                            if message:
                                fw_meta["message"] = message

                        tc = build_tool_call(
                            tc_id=tc_id,
                            turn_index=turn_index,
                            timestamp=(
                                format_timestamp(tu_start) if tu_start
                                else (format_timestamp(ts) if ts else ts_str)
                            ),
                            tool_name=tool_name,
                            operation_type=classify_operation(tool_name),
                            file_path=file_path,
                            command=command,
                            arguments=tool_input,
                            success=not is_error,
                            result=result_text,
                            error=result_text[:500] if is_error and result_text else None,
                            duration_ms=duration_ms,
                            framework_metadata=fw_meta,
                            content_origin=origin,
                            redacted=True if is_redacted else None,
                        )

                        # Handle delegation
                        if tool_name == "launch_extended_search_task":
                            tc["spawned_agent"] = {
                                "agent_type": "extended_search",
                                "task_scope": json.dumps(tool_input, ensure_ascii=False)[:200],
                                "sub_session_id": None,
                                "outcome_summary": None,
                            }

                        tool_calls.append(tc)
                        turn_tool_ids.append(tc_id)
                        tc_index += 1

                    else:
                        # Unknown block type — skip
                        i += 1

            content_text = "\n".join(text_parts) if text_parts else (text_field or "")

            turns.append(build_turn(
                index=turn_index,
                timestamp=format_timestamp(ts) if ts else ts_str,
                role="assistant",
                source="model",
                content=content_text,
                tool_calls_in_turn=turn_tool_ids,
                input_channel=None,
            ))
            # claude.ai streams responses
            turns[-1]["streaming"] = {"was_streamed": True, "stream_log": None}
            turn_index += 1
            continue

    # Compute context fields on tool calls
    if tool_calls:
        compute_tool_call_context(tool_calls)

    # Compute timing
    timing = compute_timing(all_timestamps)

    # Quality tier
    quality = assign_quality_tier(turns, tool_calls)

    # PII: blanket True for all claude.ai conversations (personal context).
    # detect_pii_in_paths() available but not used — it only checks file paths
    # in tool calls, not conversation content.
    contains_pii = True

    # Build session
    session = build_session_skeleton(
        session_id=conv_uuid,
        agent_framework="claude.ai",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    # Environment — model is unknown in export (spec footnote [3])
    session["environment"]["model"] = None
    session["environment"]["model_version"] = None
    session["environment"]["agent_version"] = None
    session["environment"]["platform_type"] = "web"
    session["environment"]["provider_hint"] = "anthropic"
    session["environment"]["tools_enabled"] = sorted(set(
        tc["tool_name"] for tc in tool_calls
    ))

    # Provenance
    session["provenance"]["original_session_id"] = conv_uuid

    # Fill session
    # Branch metadata (#19e): claude.ai export uses flat chat_messages array,
    # not a tree structure. No branching data available to populate
    # framework_metadata branch conventions (branch_parent_turn, etc.).

    session["title"] = conv.get("name") or None
    session["summary"] = conv.get("summary") or None
    session["timing"] = timing
    session["turns"] = turns
    session["tool_calls"] = tool_calls
    session["annotations"] = annotations
    session["quality"] = quality

    # Metrics — no token data available
    session["metrics"] = compute_metrics(
        turns, tool_calls, timing,
        subagent_count=sum(1 for tc in tool_calls if tc.get("spawned_agent")),
    )

    # Flags
    session["flags"]["contains_pii"] = contains_pii
    session["flags"]["for_research"] = False  # requires manual PII review
    session["flags"]["needs_cleaning"] = True
    session["classification"] = "confidential" if contains_pii else "internal"

    return session, quality


# --- CLI ---

def parse_args():
    p = argparse.ArgumentParser(
        description="minitrace claude.ai export adapter"
    )
    p.add_argument(
        "--source", required=True,
        help="Path to claude.ai data export ZIP"
    )
    p.add_argument(
        "--output-dir", default="./data/sessions",
        help="Output directory for minitrace files"
    )
    p.add_argument(
        "--uuid-filter", default=None,
        help="Comma-separated UUID prefixes to convert (default: all)"
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Parse and report stats without writing output"
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Print progress per conversation"
    )
    return p.parse_args()


def main():
    args = parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        print(f"Source not found: {source_path}", file=sys.stderr)
        sys.exit(1)

    if not zipfile.is_zipfile(str(source_path)):
        print(f"Not a ZIP file: {source_path}", file=sys.stderr)
        sys.exit(1)

    uuid_filter = None
    if args.uuid_filter:
        uuid_filter = set(args.uuid_filter.split(","))

    print(f"Source: {source_path}")
    if uuid_filter:
        print(f"UUID filter: {uuid_filter}")

    # Convert conversations
    session_index = []
    quality_counts = defaultdict(int)
    canary_warnings = []
    total_seen = 0
    converted = 0
    skipped_trivial = 0
    errors = 0

    for conv in stream_conversations(str(source_path), uuid_filter):
        total_seen += 1
        conv_uuid = conv.get("uuid", "?")
        messages = conv.get("chat_messages", [])

        # Skip trivial conversations (0-1 messages)
        if len(messages) < 2:
            skipped_trivial += 1
            quality_counts["D"] += 1
            if args.verbose:
                print(f"  SKIP {conv_uuid[:12]} ({len(messages)} messages)")
            continue

        try:
            session, quality = convert_conversation(conv)
            quality_counts[quality] += 1
            converted += 1

            # Post-conversion canary
            ws = canary_check(session, verbose=args.verbose)
            canary_warnings.extend(ws)

            if not args.dry_run:
                _, file_size, period, _ = write_session(
                    session, args.output_dir, quality,
                )
                if args.verbose:
                    tc = session["metrics"]["tool_call_count"]
                    tn = session["metrics"]["turn_count"]
                    title = (session.get("title") or "")[:40]
                    print(f"  {quality} {conv_uuid[:12]} turns={tn} "
                          f"tools={tc} {title}")
            else:
                file_size = 0
                if args.verbose:
                    tc = session["metrics"]["tool_call_count"]
                    tn = session["metrics"]["turn_count"]
                    dur = session["timing"].get("active_duration_seconds")
                    title = (session.get("title") or "")[:40]
                    dur_str = f"{dur:.0f}s" if dur else "?"
                    print(f"  {quality} {conv_uuid[:12]} turns={tn} "
                          f"tools={tc} active={dur_str} {title}")

            started = session["timing"].get("started_at")
            period = started[:7] if started else "unknown"

            session_index.append({
                "id": conv_uuid,
                "profile": "organic",
                "title": session.get("title"),
                "classification": session["classification"],
                "quality": quality,
                "started_at": started,
                "duration_seconds": session["timing"].get("duration_seconds"),
                "model": session["environment"].get("model"),
                "agent_framework": "claude.ai",
                "turn_count": session["metrics"]["turn_count"],
                "tool_call_count": session["metrics"]["tool_call_count"],
                "file_size_bytes": file_size,
                "period": period,
                "source_format": SOURCE_FORMAT,
                "flags": session["flags"],
            })

        except Exception as e:
            errors += 1
            print(f"  ERROR {conv_uuid[:12]}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            if args.verbose:
                import traceback
                traceback.print_exc()

    # Write manifests
    if not args.dry_run and session_index:
        write_manifests(session_index, args.output_dir)

    # Summary
    print(f"\n--- Summary ---")
    print(f"Conversations seen: {total_seen}")
    print(f"Converted: {converted}")
    print(f"Skipped (trivial): {skipped_trivial}")
    print(f"Errors: {errors}")
    print(f"Quality: A={quality_counts.get('A', 0)} B={quality_counts.get('B', 0)} "
          f"C={quality_counts.get('C', 0)} D={quality_counts.get('D', 0)}")
    if not args.dry_run:
        print(f"Output: {args.output_dir}")

    # Canary summary
    if canary_warnings:
        by_code = defaultdict(int)
        for w in canary_warnings:
            code = w.split("]")[0].lstrip("[") if "]" in w else "?"
            by_code[code] += 1
        print(f"\n--- Canary ---")
        print(f"Warnings: {len(canary_warnings)} across {len(by_code)} check(s)")
        for code, count in sorted(by_code.items()):
            print(f"  {code}: {count}")


if __name__ == "__main__":
    main()
