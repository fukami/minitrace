#!/usr/bin/env python3
"""
minitrace ChatGPT export adapter — converts ChatGPT data export to minitrace v0.2.0 format.

Usage:
    python3 minitrace-chatgpt-adapter.py --source data-export.zip
    python3 minitrace-chatgpt-adapter.py --source data-export.zip --id-filter 670c0928,abc123
    python3 minitrace-chatgpt-adapter.py --source data-export.zip --output-dir ./output/
    python3 minitrace-chatgpt-adapter.py --source data-export.zip --dry-run --verbose

ChatGPT data export format (Settings > Data controls > Export data):

  ZIP containing conversations.json, chat.html, user.json, user_settings.json,
  export_manifest.json, and optional image attachments.

  conversations.json is an array of conversation objects.

  Conversation structure:
    - Tree-based: `mapping` dict with node IDs as keys
    - Each node has: id, parent, children, message
    - `current_node` points to the last message in the active branch
    - Root node has parent=null and message with null role or empty content

  Message format:
    mapping[node_id].message = {
      author: {role: "system"|"user"|"assistant"|"tool"},
      content: {content_type: "text"|"multimodal_text"|"code"|"thoughts"|"reasoning_recap",
                parts: [str | {content_type: "image_asset_pointer", asset_pointer: "sediment://..."}]},
      create_time: float (epoch seconds),
      metadata: {model_slug: "gpt-4o"|"gpt-4"|"gpt-5-2"|..., finish_details: {...}, ...},
      status: "finished_successfully"|...,
      weight: 0.0|1.0
    }

  Content types:
    - "text": parts[] are strings
    - "multimodal_text": parts[] mix strings and image_asset_pointer objects
    - "code", "thoughts", "reasoning_recap": typically empty parts (canvas/reasoning artifacts)

  Tree linearization:
    Walk from current_node back to root via parent pointers, then reverse.
    This follows the active conversation branch, ignoring regenerated alternatives.

  No tool calls in web export. Model available per-message via metadata.model_slug.
  No token counts in export.
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
    build_turn,
    compute_metrics,
    compute_timing,
    assign_quality_tier,
    canary_check,
    format_timestamp,
    safe_fromtimestamp,
    write_session,
    write_manifests,
)

ADAPTER_VERSION = "minitrace-chatgpt-adapter-0.2.0"
SOURCE_FORMAT = "chatgpt-export-v1"


# --- Tree Linearization ---

def linearize_tree(mapping, current_node):
    """Walk from current_node to root via parent pointers, then reverse.

    Returns an ordered list of node dicts representing the active conversation path.
    Skips the synthetic root node (parent=null, no real message).
    """
    if not current_node or current_node not in mapping:
        return []

    path = []
    node_id = current_node
    visited = set()

    while node_id and node_id in mapping:
        if node_id in visited:
            break  # cycle guard
        visited.add(node_id)
        path.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")

    path.reverse()
    return path


def extract_content_text(message):
    """Extract text content from a ChatGPT message.

    Handles content_type: text, multimodal_text, code, thoughts, reasoning_recap.
    Returns (text, image_count, content_type).
    """
    if not message:
        return "", 0, None

    content = message.get("content", {})
    if not content:
        return "", 0, None

    content_type = content.get("content_type")
    parts = content.get("parts") or []
    text_parts = []
    image_count = 0

    for part in parts:
        if isinstance(part, str):
            text_parts.append(part)
        elif isinstance(part, dict):
            pt = part.get("content_type", "")
            if pt == "image_asset_pointer":
                image_count += 1
                asset = part.get("asset_pointer", "")
                text_parts.append(f"[image: {asset}]")
            elif "text" in part:
                text_parts.append(part["text"])
            # Other object types (fovea metadata etc.) are skipped

    return "\n".join(text_parts), image_count, content_type


def get_model_slug(message):
    """Extract model_slug from message metadata."""
    if not message:
        return None
    meta = message.get("metadata", {})
    return meta.get("model_slug")


# --- Conversation Conversion ---

def convert_conversation(conv):
    """Convert a single ChatGPT conversation to minitrace session.

    Returns: (session_dict, quality_tier)
    """
    conv_id = conv.get("conversation_id") or conv.get("id") or "unknown"
    title = conv.get("title")
    mapping = conv.get("mapping", {})
    current_node = conv.get("current_node")

    # Linearize tree to active path
    nodes = linearize_tree(mapping, current_node)
    if not nodes:
        # current_node missing or broken parent chain — cannot reconstruct conversation
        raise ValueError(
            f"Cannot linearize conversation {conv_id[:12]}: "
            f"current_node={'missing' if not current_node else 'broken chain'}, "
            f"{len(mapping)} nodes in mapping"
        )

    turns = []
    all_timestamps = []
    models_seen = set()
    turn_index = 0
    total_images = 0

    for node in nodes:
        message = node.get("message")
        if not message:
            continue

        author = message.get("author", {})
        role = author.get("role")

        # Skip synthetic/hidden nodes
        if role is None:
            continue
        meta = message.get("metadata", {})
        if meta.get("is_visually_hidden_from_conversation"):
            continue
        # Skip zero-weight system messages (empty system prompts)
        if role == "system" and message.get("weight", 1.0) == 0.0:
            continue

        # Timestamp
        create_time = message.get("create_time")
        ts = safe_fromtimestamp(create_time, auto_ms=False) if create_time else None
        if ts:
            all_timestamps.append(ts)

        # Content
        text, image_count, content_type = extract_content_text(message)
        total_images += image_count

        # Model tracking (assistant messages)
        model_slug = get_model_slug(message)
        if model_slug:
            models_seen.add(model_slug)

        # Map role
        if role == "system":
            mt_role = "system"
            source = "system"
            input_channel = "system_prompt"
        elif role == "user":
            mt_role = "user"
            source = "human"
            input_channel = "user_input"
        elif role == "assistant":
            mt_role = "assistant"
            source = "model"
            input_channel = None
        elif role == "tool":
            # ChatGPT tool messages (rare in web export, but handle gracefully)
            mt_role = "system"
            source = "framework"
            input_channel = "tool_output"
        else:
            mt_role = "system"
            source = "framework"
            input_channel = None

        # Skip empty content (code/reasoning artifacts with no parts)
        if not text and content_type in ("code", "thoughts", "reasoning_recap"):
            continue

        # Framework metadata for non-text content types
        fw_meta = None
        extras = {}
        if content_type and content_type not in ("text",):
            extras["content_type"] = content_type
        if model_slug:
            extras["model_slug"] = model_slug
        finish = meta.get("finish_details")
        if finish:
            extras["finish_details"] = finish
        if image_count > 0:
            extras["image_count"] = image_count
        if extras:
            fw_meta = extras

        # Map ChatGPT content_type to spec enum (19g)
        _CT_MAP = {
            "text": "text",
            "multimodal_text": "multimodal_text",
            "code": "code",
            "thoughts": "reasoning",
            "reasoning_recap": "reasoning",
        }
        mt_content_type = _CT_MAP.get(content_type) if content_type else None

        turn = build_turn(
            index=turn_index,
            timestamp=format_timestamp(ts) if ts else None,
            role=mt_role,
            source=source,
            content=text,
            input_channel=input_channel,
            framework_metadata=fw_meta,
            model=model_slug,
            content_type=mt_content_type,
        )
        # ChatGPT streams responses
        if role == "assistant":
            turn["streaming"] = {"was_streamed": True, "stream_log": None}

        turns.append(turn)
        turn_index += 1

    # Timing
    timing = compute_timing(all_timestamps)

    # Quality tier (no tool calls in ChatGPT web export)
    tool_calls = []
    quality = assign_quality_tier(turns, tool_calls)

    # Determine primary model (most common across assistant messages)
    primary_model = None
    if models_seen:
        # Use the model from the most recent assistant message
        for node in reversed(nodes):
            msg = node.get("message")
            if msg and msg.get("author", {}).get("role") == "assistant":
                slug = get_model_slug(msg)
                if slug:
                    primary_model = slug
                    break
        if not primary_model:
            primary_model = sorted(models_seen)[0]

    # Compute model switch metrics (#19f)
    # Collect per-turn model sequence from assistant turns
    turn_models = []
    for node in nodes:
        msg = node.get("message")
        if msg and msg.get("author", {}).get("role") == "assistant":
            slug = get_model_slug(msg)
            if slug:
                turn_models.append(slug)

    # model_switches and unique_models require at least 2 turns with model populated
    if len(turn_models) >= 2:
        model_switches = sum(
            1 for i in range(1, len(turn_models))
            if turn_models[i] != turn_models[i - 1]
        )
        unique_models = len(set(turn_models))
    else:
        model_switches = None
        unique_models = None

    # Build session
    session = build_session_skeleton(
        session_id=conv_id,
        agent_framework="chatgpt-web",
        source_format=SOURCE_FORMAT,
        converter_version=ADAPTER_VERSION,
    )

    # Environment
    session["environment"]["model"] = primary_model
    session["environment"]["model_version"] = None
    session["environment"]["agent_version"] = None
    session["environment"]["platform_type"] = "web"
    session["environment"]["provider_hint"] = "openai"
    session["environment"]["tools_enabled"] = []

    # Provenance
    session["provenance"]["original_session_id"] = conv_id

    # Fill session
    session["title"] = title
    session["quality"] = quality
    session["timing"] = timing
    session["turns"] = turns
    session["tool_calls"] = tool_calls
    session["annotations"] = []

    # Metrics
    session["metrics"] = compute_metrics(turns, tool_calls, timing)
    session["metrics"]["model_switches"] = model_switches
    session["metrics"]["unique_models"] = unique_models

    # Flags
    session["flags"]["contains_pii"] = True  # personal conversations
    session["flags"]["for_research"] = False  # requires manual PII review
    session["flags"]["needs_cleaning"] = True
    session["classification"] = "confidential"

    # Extra metadata: branch info, image count
    branch_count = sum(
        1 for n in mapping.values()
        if len(n.get("children", [])) > 1
    )
    if branch_count > 0 or total_images > 0 or len(models_seen) > 1:
        session["provenance"]["adapter_notes"] = {}
        if branch_count > 0:
            session["provenance"]["adapter_notes"]["branch_points"] = branch_count
        if total_images > 0:
            session["provenance"]["adapter_notes"]["image_attachments"] = total_images
        if len(models_seen) > 1:
            session["provenance"]["adapter_notes"]["models_used"] = sorted(models_seen)

    return session, quality


# --- ZIP Streaming ---

def stream_conversations(zip_path, id_filter=None):
    """Stream conversations from the ChatGPT export ZIP.

    Yields one conversation dict at a time.

    Args:
        zip_path: path to the data export ZIP
        id_filter: optional set of conversation ID prefixes to include
    """
    # Size limit for decompressed conversations.json (500 MB)
    MAX_DECOMPRESSED = 500 * 1024 * 1024

    zf = zipfile.ZipFile(zip_path, "r")
    info = zf.getinfo("conversations.json")
    if info.file_size > MAX_DECOMPRESSED:
        raise ValueError(
            f"conversations.json too large: {info.file_size:,} bytes "
            f"(limit {MAX_DECOMPRESSED:,})"
        )
    with zf.open("conversations.json") as f:
        raw = f.read(MAX_DECOMPRESSED + 1)
        if len(raw) > MAX_DECOMPRESSED:
            raise ValueError(
                f"conversations.json decompressed beyond limit: >{MAX_DECOMPRESSED:,} bytes"
            )
    zf.close()

    text = raw.decode("utf-8")
    del raw

    decoder = json.JSONDecoder()
    pos = 0

    # Skip opening bracket
    while pos < len(text) and text[pos] in " \n\r\t":
        pos += 1
    if pos < len(text) and text[pos] == "[":
        pos += 1

    while pos < len(text):
        while pos < len(text) and text[pos] in " \n\r\t,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            break

        conv, end_pos = decoder.raw_decode(text, pos)
        pos = end_pos

        if id_filter:
            conv_id = conv.get("conversation_id", conv.get("id", ""))
            if not any(conv_id.startswith(prefix) for prefix in id_filter):
                continue

        yield conv

    del text


# --- CLI ---

def parse_args():
    p = argparse.ArgumentParser(
        description="minitrace ChatGPT export adapter"
    )
    p.add_argument(
        "--source", required=True,
        help="Path to ChatGPT data export ZIP"
    )
    p.add_argument(
        "--output-dir", default="./data/sessions",
        help="Output directory for minitrace files"
    )
    p.add_argument(
        "--id-filter", default=None,
        help="Comma-separated conversation ID prefixes to convert (default: all)"
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

    id_filter = None
    if args.id_filter:
        id_filter = set(args.id_filter.split(","))

    print(f"Source: {source_path}")
    if id_filter:
        print(f"ID filter: {id_filter}")

    # Convert conversations
    session_index = []
    quality_counts = defaultdict(int)
    canary_warnings = []
    total_seen = 0
    converted = 0
    skipped_trivial = 0
    errors = 0

    for conv in stream_conversations(str(source_path), id_filter):
        total_seen += 1
        conv_id = conv.get("conversation_id", conv.get("id", "?"))
        mapping = conv.get("mapping", {})

        # Skip trivial conversations (fewer than 3 nodes = root + 1 exchange)
        real_messages = sum(
            1 for n in mapping.values()
            if n.get("message") and n["message"].get("author", {}).get("role") in ("user", "assistant")
        )
        if real_messages < 2:
            skipped_trivial += 1
            quality_counts["D"] += 1
            if args.verbose:
                print(f"  SKIP {conv_id[:12]} ({real_messages} real messages)")
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
                    tn = session["metrics"]["turn_count"]
                    model = session["environment"].get("model") or "?"
                    title_str = (session.get("title") or "")[:40]
                    print(f"  {quality} {conv_id[:12]} turns={tn} "
                          f"model={model} {title_str}")
            else:
                file_size = 0
                if args.verbose:
                    tn = session["metrics"]["turn_count"]
                    model = session["environment"].get("model") or "?"
                    dur = session["timing"].get("active_duration_seconds")
                    title_str = (session.get("title") or "")[:40]
                    dur_str = f"{dur:.0f}s" if dur else "?"
                    print(f"  {quality} {conv_id[:12]} turns={tn} "
                          f"model={model} active={dur_str} {title_str}")

            started = session["timing"].get("started_at")
            period = started[:7] if started else "unknown"

            session_index.append({
                "id": conv_id,
                "profile": "organic",
                "title": session.get("title"),
                "classification": session["classification"],
                "quality": quality,
                "started_at": started,
                "duration_seconds": session["timing"].get("duration_seconds"),
                "model": session["environment"].get("model"),
                "agent_framework": "chatgpt-web",
                "turn_count": session["metrics"]["turn_count"],
                "tool_call_count": 0,
                "file_size_bytes": file_size,
                "period": period,
                "source_format": SOURCE_FORMAT,
                "flags": session["flags"],
            })

        except Exception as e:
            errors += 1
            print(f"  ERROR {conv_id[:12]}: {type(e).__name__}: {e}",
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

    # Model distribution
    if session_index:
        model_counts = defaultdict(int)
        for entry in session_index:
            model_counts[entry.get("model") or "unknown"] += 1
        print(f"\n--- Models ---")
        for model, count in sorted(model_counts.items(), key=lambda x: -x[1]):
            print(f"  {model}: {count}")

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
