#!/usr/bin/env python3
"""
minitrace Claude Code adapter — converts Claude Code JSONL transcripts to minitrace v0.1.0 format.

Usage:
    python3 minitrace-claude-adapter.py --source-dir ~/.claude/projects/
    python3 minitrace-claude-adapter.py --source-dir ~/.claude/projects/ --output-dir ./output/
    python3 minitrace-claude-adapter.py --source-dir ~/.claude/projects/ --session-id <uuid>
    python3 minitrace-claude-adapter.py --discover --source-dir ~/.claude/projects/

Claude Code native format (discovered against Claude Code 2.1.76+):

  Two source types:

  1. JSONL v2 (~/.claude/projects/<project>/<session-id>.jsonl):
     Record types: system, user, assistant, progress, file-history-snapshot, last-prompt
     Content blocks: text, thinking, tool_use, tool_result
     Tool result delivery: user message with tool_result content blocks
     Subagents: <session-id>/subagents/<agent-id>.jsonl

  2. Dir v1 (~/.claude/projects/<project>/<session-id>/tool-results/):
     Older format (pre-Feb 2026) with tool output files only, no full transcript.
     Files named by tool_use_id with .txt extension.

  Claude Code tools: Read, Glob, Grep, Edit, Write, Bash, Agent, Task, TaskCreate,
  TaskUpdate, TaskGet, TaskList, TaskOutput, TaskStop, Skill, AskUserQuestion,
  NotebookEdit, WebFetch, WebSearch, ToolSearch
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
    normalize_path,
    parse_jsonl,
    parse_timestamp,
    truncate_content,
    write_session,
    write_manifests,
    assign_quality_tier,
)

ADAPTER_VERSION = "minitrace-claude-adapter-0.1.0"
SOURCE_FORMAT_V2 = "claude-code-jsonl-v2"
SOURCE_FORMAT_V1 = "claude-code-dir-v1"

# Record types to discard (framework machinery, not session content)
DISCARD_TYPES = {"file-history-snapshot", "last-prompt"}


# --- Operation Type Mapping ---

def classify_operation(tool_name):
    """Map Claude Code tool names to minitrace operation_type."""
    mapping = {
        "Read": "READ",
        "Glob": "READ",
        "Grep": "READ",
        "Edit": "MODIFY",
        "Write": "NEW",
        "Bash": "EXECUTE",
        "Agent": "DELEGATE",
        "Task": "DELEGATE",
        "TaskCreate": "DELEGATE",
        "TaskUpdate": "DELEGATE",
        "TaskGet": "READ",
        "TaskList": "READ",
        "TaskOutput": "READ",
        "TaskStop": "EXECUTE",
        "Skill": "OTHER",
        "AskUserQuestion": "OTHER",
        "NotebookEdit": "MODIFY",
        "WebFetch": "READ",
        "WebSearch": "READ",
        "ToolSearch": "READ",
    }
    return mapping.get(tool_name, "OTHER")


def classify_source(record):
    """Classify Turn.source from a JSONL record."""
    rtype = record.get("type", "")
    if rtype == "system":
        return "framework"
    if rtype == "user":
        content = record.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return "framework"
        if isinstance(content, str):
            if "<system-reminder>" in content or "<command-name>" in content:
                return "framework"
        return "human"
    if rtype == "assistant":
        return "model"
    return None


# --- Session Discovery ---

def find_session_files(source_dir):
    """Find all session sources under source_dir.

    Returns dict of session_id -> {"type": "jsonl"|"dir", "path": Path}
    """
    source = Path(source_dir)
    files = {}

    # Find JSONL transcripts (v2 format)
    for jsonl in source.rglob("*.jsonl"):
        sid = jsonl.stem
        if len(sid) < 32:
            continue
        if "/subagents/" in str(jsonl):
            continue
        files[sid] = {"type": "jsonl", "path": jsonl}

    # Find dir-only sessions (v1 format)
    for project_dir in source.iterdir():
        if not project_dir.is_dir():
            continue
        for session_dir in project_dir.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if len(sid) < 32:
                continue
            if sid in files:
                continue
            tool_results = session_dir / "tool-results"
            if tool_results.is_dir():
                files[sid] = {"type": "dir", "path": session_dir}

    return files


def find_subagent_files(source_dir):
    """Find all subagent JSONL files under source_dir.

    Returns list of {"agent_id": str, "parent_session_id": str, "path": Path}
    """
    source = Path(source_dir)
    subagents = []
    for jsonl in source.rglob("*.jsonl"):
        if "/subagents/" not in str(jsonl):
            continue
        parts = jsonl.parts
        try:
            sa_idx = parts.index("subagents")
            parent_sid = parts[sa_idx - 1]
        except (ValueError, IndexError):
            continue
        subagents.append({
            "agent_id": jsonl.stem,
            "parent_session_id": parent_sid,
            "path": jsonl,
        })
    return subagents


# --- Session Conversion ---

def convert_session(records, session_id, source_path=None):
    """Convert parsed JSONL records into a minitrace session dict.

    Args:
        records: list of parsed JSONL records
        session_id: session identifier (UUID)
        source_path: optional path to source file (for provenance)

    Returns: (session_dict, quality_tier)
    """
    turns = []
    tool_calls = []
    tool_use_pending = {}  # id -> tool_call dict (waiting for result)
    annotations = []
    all_timestamps = []
    token_totals = {
        "input": 0, "output": 0,
        "cache_read": 0, "cache_creation": 0,
    }

    # Extract session-level metadata from first record
    first = records[0] if records else {}
    session_meta = {
        "cwd": first.get("cwd"),
        "version": first.get("version"),
        "git_branch": first.get("gitBranch"),
        "model": None,
    }

    turn_index = 0
    tc_index = 0

    for rec in records:
        rtype = rec.get("type", "")
        ts_str = rec.get("timestamp")
        ts = parse_timestamp(ts_str)
        if ts:
            all_timestamps.append(ts)

        if rtype in DISCARD_TYPES or rtype == "progress":
            continue

        # System messages
        if rtype == "system":
            content = rec.get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            turns.append(build_turn(
                index=turn_index,
                timestamp=ts_str,
                role="system",
                source="framework",
                content=content,
            ))
            turn_index += 1
            continue

        # User messages
        if rtype == "user":
            msg = rec.get("message", {})
            content = msg.get("content", "")
            source = classify_source(rec)

            # Handle tool results (returned as user messages with tool_result content)
            if isinstance(content, list):
                has_tool_results = False
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        has_tool_results = True
                        tuid = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        is_error = block.get("is_error", False)

                        if tuid in tool_use_pending:
                            tc = tool_use_pending.pop(tuid)
                            truncated, full_bytes, full_hash = truncate_content(result_content)
                            tc["output"]["success"] = not is_error
                            tc["output"]["result"] = truncated
                            tc["output"]["error"] = str(result_content)[:500] if is_error else None
                            tc["output"]["truncated"] = full_bytes is not None
                            tc["output"]["full_bytes"] = full_bytes
                            tc["output"]["full_hash"] = full_hash
                            tc["timestamp"] = ts_str
                            tool_calls.append(tc)

                if has_tool_results:
                    continue

                content = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_result")
                )

            turns.append(build_turn(
                index=turn_index,
                timestamp=ts_str,
                role="user",
                source=source,
                content=content if isinstance(content, str) else str(content),
            ))
            turn_index += 1
            continue

        # Assistant messages
        if rtype == "assistant":
            msg = rec.get("message", {})
            content_blocks = msg.get("content", [])
            model = msg.get("model")
            if model:
                session_meta["model"] = model

            usage = msg.get("usage", {})

            # Accumulate token totals
            turn_usage = None
            if usage:
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                cr_tok = usage.get("cache_read_input_tokens", 0)
                cc_tok = usage.get("cache_creation_input_tokens", 0)
                token_totals["input"] += in_tok
                token_totals["output"] += out_tok
                token_totals["cache_read"] += cr_tok
                token_totals["cache_creation"] += cc_tok
                turn_usage = {
                    "input_tokens": in_tok or None,
                    "output_tokens": out_tok or None,
                    "cache_read_tokens": cr_tok or None,
                    "cache_creation_tokens": cc_tok or None,
                    "reasoning_tokens": None,
                    "tool_tokens": None,
                }

            text_parts = []
            thinking_text = None
            turn_tool_ids = []

            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "thinking":
                        thinking_text = block.get("thinking", "")
                    elif btype == "tool_use":
                        tc_id = block.get("id", f"tc-{tc_index:04d}")
                        tool_name = block.get("name", "unknown")
                        tool_input = block.get("input", {})

                        file_path = tool_input.get("file_path") or tool_input.get("path")
                        command = tool_input.get("command")

                        tc = build_tool_call(
                            tc_id=tc_id,
                            turn_index=turn_index,
                            timestamp=ts_str,
                            tool_name=tool_name,
                            operation_type=classify_operation(tool_name),
                            file_path=file_path,
                            command=command,
                            arguments=tool_input,
                        )

                        # Handle Agent/Task delegation
                        if tool_name in ("Agent", "Task", "TaskCreate"):
                            tc["spawned_agent"] = {
                                "agent_type": tool_input.get("subagent_type",
                                              tool_input.get("type", "general")),
                                "task_scope": tool_input.get("prompt",
                                              tool_input.get("description", ""))[:200],
                                "sub_session_id": None,
                                "outcome_summary": None,
                            }

                        tool_use_pending[tc_id] = tc
                        turn_tool_ids.append(tc_id)
                        tc_index += 1

            content_text = "\n".join(text_parts)

            turns.append(build_turn(
                index=turn_index,
                timestamp=ts_str,
                role="assistant",
                source="model",
                content=content_text,
                tool_calls_in_turn=turn_tool_ids,
                thinking=thinking_text,
                usage=turn_usage,
            ))
            # Override streaming default for Claude Code (always streams)
            turns[-1]["streaming"] = {"was_streamed": True, "stream_log": None}
            turn_index += 1
            continue

    # Flush pending tool_uses that never got results
    for tc_id, tc in tool_use_pending.items():
        tc["output"]["success"] = False
        tc["output"]["error"] = "no tool_result received"
        tool_calls.append(tc)
        annotations.append(build_annotation(
            ann_id=f"ann-orphan-{tc_id[:8]}",
            annotator="adapter",
            scope_type="tool_call",
            target_id=tc_id,
            category="observation",
            title=f"Tool call {tc['tool_name']} never received result",
            detail=f"tool_use id={tc_id} has no matching tool_result. "
                   "Model may have crashed or timed out.",
            tags=["data-quality", "orphan-tool-call"],
        ))

    # Sort tool_calls by timestamp
    tool_calls.sort(key=lambda tc: tc.get("timestamp") or "")

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
    timing = compute_timing(all_timestamps)
    quality = assign_quality_tier(turns, tool_calls)
    contains_pii = detect_pii_in_paths(tool_calls)

    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="claude-code",
        source_format=SOURCE_FORMAT_V2,
        converter_version=ADAPTER_VERSION,
    )

    # Environment
    session["environment"]["model"] = session_meta["model"] or "unknown"
    session["environment"]["agent_version"] = session_meta.get("version")
    session["environment"]["provider_hint"] = "anthropic"
    session["environment"]["tools_enabled"] = list(set(
        tc["tool_name"] for tc in tool_calls
    ))

    # Operational context
    session["operational_context"]["working_directory"] = normalize_path(
        session_meta.get("cwd")
    )
    session["operational_context"]["git_branch"] = session_meta.get("git_branch")

    # Provenance
    if source_path:
        session["provenance"]["source_path"] = str(source_path)

    # Fill session
    session["title"] = extract_title(turns)
    session["timing"] = timing
    session["turns"] = turns
    session["tool_calls"] = tool_calls
    session["annotations"] = annotations
    session["metrics"] = compute_metrics(
        turns, tool_calls, timing,
        subagent_count=sum(1 for tc in tool_calls if tc.get("spawned_agent")),
        token_totals=token_totals,
    )

    # Flags
    session["flags"]["contains_pii"] = contains_pii
    session["flags"]["for_research"] = quality in ("A",) and not contains_pii
    session["flags"]["needs_cleaning"] = quality not in ("A",) or contains_pii
    session["classification"] = "confidential" if contains_pii else "internal"

    return session, quality


# --- Dir v1 Session Conversion ---

def convert_dir_session(session_dir, session_id):
    """Convert a claude-code-dir-v1 session (directory with tool-results/, no JSONL).

    These are older sessions (pre-Feb 2026) where Claude Code stored only tool
    outputs, not full transcripts. Reconstructed from tool-results/*.txt files.

    Returns: (session_dict, quality_tier)
    """
    tool_results_dir = session_dir / "tool-results"
    tool_calls = []
    annotations = []

    # Load tool result files
    if tool_results_dir.is_dir():
        for f in sorted(tool_results_dir.iterdir()):
            if f.suffix != ".txt":
                continue
            tuid = f.stem
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            tc = build_tool_call(
                tc_id=tuid,
                turn_index=None,
                timestamp=None,
                tool_name="unknown",
                operation_type="OTHER",
                result=content,
            )
            tool_calls.append(tc)

    # Deduplicate
    tool_calls, _ = deduplicate_tool_calls(tool_calls)
    compute_tool_call_context(tool_calls)

    # Quality: C (metadata only) since we have no conversation content
    has_tool_io = any(tc["output"]["result"] is not None for tc in tool_calls)
    tc_count = len(tool_calls)
    if has_tool_io and tc_count > 10:
        quality = "B"
    elif tc_count > 0:
        quality = "C"
    else:
        quality = "D"

    session = build_session_skeleton(
        session_id=session_id,
        agent_framework="claude-code",
        source_format=SOURCE_FORMAT_V1,
        converter_version=ADAPTER_VERSION,
    )

    session["environment"]["provider_hint"] = "anthropic"
    session["provenance"]["source_path"] = str(session_dir)
    session["flags"]["needs_cleaning"] = True
    session["flags"]["category"] = ["dir-v1", "no-transcript"]

    session["turns"] = []
    session["tool_calls"] = tool_calls
    session["annotations"] = annotations
    session["metrics"] = compute_metrics([], tool_calls, session["timing"])

    return session, quality


# --- Subagent Post-Processing ---

def make_subagent_session(session, agent_id, parent_session_id, slug=None):
    """Adjust a converted session for subagent context."""
    session["id"] = agent_id
    session["provenance"]["original_session_id"] = agent_id
    session["provenance"]["source_format"] = SOURCE_FORMAT_V2 + "+subagent"
    session["coordination"]["parent_session"] = parent_session_id
    session["flags"]["category"].append("subagent")

    if slug:
        session["title"] = f"[subagent] {slug}"
    elif session["title"]:
        session["title"] = f"[subagent] {session['title']}"
    else:
        session["title"] = f"[subagent] {agent_id}"

    return session


def link_parent_subagents(parent_session, subagent_ids):
    """Update spawned_agent.sub_session_id on parent's tool calls.

    Matches Agent/Task tool calls to subagent IDs by order of appearance.
    """
    agent_tool_calls = [
        tc for tc in parent_session["tool_calls"]
        if tc.get("spawned_agent") is not None
    ]
    for tc, sa_id in zip(agent_tool_calls, subagent_ids):
        tc["spawned_agent"]["sub_session_id"] = sa_id


# --- Discovery ---

def discover_format(source_dir):
    """Discovery mode: inspect Claude Code session data and print structure."""
    source = Path(source_dir)
    if not source.exists():
        print(f"Directory not found: {source}")
        return

    session_files = find_session_files(source_dir)
    subagent_files = find_subagent_files(source_dir)

    v2_count = sum(1 for v in session_files.values() if v["type"] == "jsonl")
    v1_count = sum(1 for v in session_files.values() if v["type"] == "dir")

    print(f"=== Claude Code sessions in {source} ===")
    print(f"Total: {len(session_files)} sessions ({v2_count} JSONL v2, {v1_count} dir v1)")
    print(f"Subagents: {len(subagent_files)}")

    # Sample a few v2 sessions
    v2_sessions = [(sid, s) for sid, s in session_files.items() if s["type"] == "jsonl"]
    for sid, s in v2_sessions[:5]:
        records = parse_jsonl(s["path"])
        types = defaultdict(int)
        for r in records:
            types[r.get("type", "?")] += 1
        print(f"\n  {sid[:20]}... ({len(records)} records)")
        for t, c in sorted(types.items()):
            print(f"    {t}: {c}")


# --- CLI ---

def parse_args():
    p = argparse.ArgumentParser(description="minitrace Claude Code adapter")
    p.add_argument("--source-dir", required=True,
                   help="Claude Code projects directory (e.g., ~/.claude/projects/)")
    p.add_argument("--output-dir", default="./data/sessions",
                   help="minitrace archive output directory")
    p.add_argument("--session-id", default=None,
                   help="Convert a single session (for testing)")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and report stats without writing output")
    p.add_argument("--verbose", action="store_true",
                   help="Print progress per session")
    p.add_argument("--discover", action="store_true",
                   help="Discovery mode: inspect native format")
    return p.parse_args()


def main():
    args = parse_args()

    if args.discover:
        discover_format(args.source_dir)
        return

    # Find sessions
    print(f"Scanning {args.source_dir}...")
    session_files = find_session_files(args.source_dir)
    if args.session_id:
        if args.session_id in session_files:
            session_files = {args.session_id: session_files[args.session_id]}
        else:
            print(f"Session {args.session_id} not found")
            sys.exit(1)

    v2_count = sum(1 for v in session_files.values() if v["type"] == "jsonl")
    v1_count = sum(1 for v in session_files.values() if v["type"] == "dir")
    print(f"Found {len(session_files)} sessions ({v2_count} JSONL v2, {v1_count} dir v1)")

    # Convert sessions
    session_index = []
    quality_counts = defaultdict(int)
    skipped = 0
    errors = 0

    for sid, source in sorted(session_files.items()):
        try:
            stype = source["type"]
            path = source["path"]

            if stype == "jsonl":
                records = parse_jsonl(path)
                if len(records) < 3:
                    quality_counts["D"] += 1
                    skipped += 1
                    if args.verbose:
                        print(f"  SKIP {sid} ({len(records)} records)")
                    continue

                session, quality = convert_session(records, sid, source_path=path)

            elif stype == "dir":
                session, quality = convert_dir_session(path, sid)
                if session["metrics"]["tool_call_count"] == 0:
                    quality_counts["D"] += 1
                    skipped += 1
                    if args.verbose:
                        print(f"  SKIP {sid} (dir-v1, no tool data)")
                    continue

            quality_counts[quality] += 1

            if not args.dry_run:
                file_path, file_size, period, _ = write_session(
                    session, args.output_dir, quality,
                )
                if args.verbose:
                    print(f"  {quality} {sid} -> {file_path} ({file_size:,} bytes)")
            else:
                file_size = 0
                period = (session["timing"].get("started_at") or "")[:7] or "unknown"
                if args.verbose:
                    tc = session["metrics"]["tool_call_count"]
                    tn = session["metrics"]["turn_count"]
                    model = session["environment"]["model"]
                    print(f"  {quality} {sid} turns={tn} tools={tc} model={model}")

            session_index.append({
                "id": sid,
                "profile": "organic",
                "title": session.get("title"),
                "classification": session["classification"],
                "quality": quality,
                "started_at": session["timing"].get("started_at"),
                "duration_seconds": session["timing"].get("duration_seconds"),
                "model": session["environment"]["model"],
                "agent_framework": "claude-code",
                "turn_count": session["metrics"]["turn_count"],
                "tool_call_count": session["metrics"]["tool_call_count"],
                "file_size_bytes": file_size,
                "period": period,
                "source_format": session["provenance"]["source_format"],
                "flags": session["flags"],
            })

        except Exception as e:
            errors += 1
            print(f"  ERROR {sid}: {type(e).__name__}", file=sys.stderr)

    # --- Subagent processing ---
    if not args.session_id:
        subagent_files = find_subagent_files(args.source_dir)
        sa_converted = 0
        sa_skipped = 0
        sa_errors = 0

        if subagent_files:
            print(f"\nProcessing {len(subagent_files)} subagents...")

            by_parent = defaultdict(list)
            for sa in subagent_files:
                by_parent[sa["parent_session_id"]].append(sa)

            for sa in subagent_files:
                try:
                    records = parse_jsonl(sa["path"])
                    if len(records) < 2:
                        sa_skipped += 1
                        continue

                    first = records[0] if records else {}
                    agent_id = first.get("agentId", sa["agent_id"])
                    slug = first.get("slug")

                    session, quality = convert_session(records, agent_id,
                                                       source_path=sa["path"])
                    make_subagent_session(session, agent_id,
                                         sa["parent_session_id"], slug)

                    quality_counts[quality] += 1
                    sa_converted += 1

                    if not args.dry_run:
                        file_path, file_size, period, _ = write_session(
                            session, args.output_dir, quality,
                        )
                        if args.verbose:
                            print(f"  {quality} {agent_id} (sub of "
                                  f"{sa['parent_session_id'][:8]}...) -> {period}")
                    else:
                        file_size = 0
                        period = (session["timing"].get("started_at") or "")[:7] or "unknown"
                        if args.verbose:
                            tc = session["metrics"]["tool_call_count"]
                            tn = session["metrics"]["turn_count"]
                            print(f"  {quality} {agent_id} (sub) turns={tn} tools={tc}")

                    session_index.append({
                        "id": agent_id,
                        "profile": "organic",
                        "title": session.get("title"),
                        "classification": session["classification"],
                        "quality": quality,
                        "started_at": session["timing"].get("started_at"),
                        "duration_seconds": session["timing"].get("duration_seconds"),
                        "model": session["environment"]["model"],
                        "agent_framework": "claude-code",
                        "turn_count": session["metrics"]["turn_count"],
                        "tool_call_count": session["metrics"]["tool_call_count"],
                        "file_size_bytes": file_size,
                        "period": period,
                        "source_format": session["provenance"]["source_format"],
                        "flags": session["flags"],
                    })

                except Exception as e:
                    sa_errors += 1
                    if args.verbose:
                        print(f"  ERROR subagent {sa['agent_id']}: {type(e).__name__}",
                              file=sys.stderr)

            print(f"Subagents: {sa_converted} converted, "
                  f"{sa_skipped} skipped, {sa_errors} errors")

            # Backfill parent spawned_agent.sub_session_id
            if not args.dry_run:
                backlinked = 0
                for parent_sid, sa_list in by_parent.items():
                    converted_ids = []
                    for sa in sa_list:
                        first_rec = None
                        try:
                            recs = parse_jsonl(sa["path"])
                            if recs:
                                first_rec = recs[0]
                        except Exception:
                            pass
                        agent_id = (first_rec.get("agentId", sa["agent_id"])
                                    if first_rec else sa["agent_id"])
                        converted_ids.append(agent_id)

                    if not converted_ids:
                        continue

                    parent_entry = next(
                        (e for e in session_index if e["id"] == parent_sid), None
                    )
                    if not parent_entry:
                        continue

                    period = parent_entry.get("period", "unknown")
                    parent_path = (Path(args.output_dir) / "active" / period
                                   / f"{parent_sid}.minitrace.json")
                    if not parent_path.exists():
                        continue

                    try:
                        with open(parent_path) as f:
                            parent_session = json.load(f)
                        link_parent_subagents(parent_session, converted_ids)
                        with open(parent_path, "w", encoding="utf-8") as f:
                            json.dump(parent_session, f, indent=2,
                                      ensure_ascii=False)
                        backlinked += 1
                    except Exception as e:
                        if args.verbose:
                            print(f"  WARN: Could not backlink parent "
                                  f"{parent_sid[:8]}: {e}")

                if backlinked:
                    print(f"Parent backlinks: {backlinked} sessions updated")

    # Write manifests
    if not args.dry_run and session_index:
        write_manifests(session_index, args.output_dir)

    # Summary
    total = len(session_index) + skipped
    print(f"\n--- Summary ---")
    print(f"Total sessions: {total}")
    print(f"Converted: {len(session_index)}")
    print(f"Skipped (D): {skipped}")
    print(f"Errors: {errors}")
    print(f"Quality: A={quality_counts.get('A',0)} B={quality_counts.get('B',0)} "
          f"C={quality_counts.get('C',0)} D={quality_counts.get('D',0)}")
    if not args.dry_run:
        print(f"Output: {args.output_dir}")


if __name__ == "__main__":
    main()
