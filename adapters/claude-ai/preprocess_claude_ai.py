#!/usr/bin/env python3
"""Claude.ai export → chain-walked conversations pre-processor.

Reads a conversations.json from a claude.ai data export (one or more conversation
objects, each with a `chat_messages` array) and re-orders each conversation's
`chat_messages` so that `parent_message_uuid` chain walk is the ordering primary
with timestamp fallback for orphans. Matches the convention
established by claude-code's `preprocess_claude_code.py:order_events`:

  > Pre-processor walks the chain via iterative DFS (recursion overflowed on
  > 3 of 10 stratified-sample sessions before the fix); ordering primary,
  > timestamp fallback.

Output is the same JSON shape as the input — the `chat_messages` array is the
only field rewritten, and only its element order changes. Per-message fields
are preserved verbatim so the existing DSL `record_path: chat_messages`
indexing is unaffected.

The cheap reading is "this is a no-op when the export already comes
linearised" (which the 2026-05-03 format observation suggests). The
disposition value is convention-tracking: chain walk is the source-of-truth
for ordering, the export's array order is treated as an opaque transport
detail. The pre-processor materialises that distinction.

Usage:
    python preprocess_claude_ai.py <input.json> <output.json>

If the input top-level value is a single conversation object (has `uuid` +
`chat_messages`), it is processed in place. If it is an array of conversation
objects (the standard data-export shape), each conversation is processed
independently.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _key(v):
    """A uuid usable as a dict key and comparable: data decides its type, and
    a list or object here aborted the whole export (unhashable)."""
    return v if isinstance(v, str) else None


def _order_key(m: dict) -> tuple[str, str]:
    """Total order for sorting: mixed-type created_at values (5 beside "x")
    raised TypeError and aborted the whole export."""
    c, u = m.get("created_at"), m.get("uuid")
    return (c if isinstance(c, str) else "", u if isinstance(u, str) else "")


def order_messages(messages: list[dict]) -> list[dict]:
    """Order messages by parent_message_uuid linked-list, timestamp fallback.

    Mirrors the claude-code preprocessor's `order_events` (preprocess_claude_code.py
    lines 201-249) with field-name parameterisation:
        parent_uuid → parent_message_uuid
        uuid        → uuid
        timestamp   → created_at

    Roots are messages whose `parent_message_uuid` does not match any sibling's
    `uuid` (typically null or a sentinel like `00000000-...`). Orphans (parent
    points at a record not present in this conversation) are appended in
    timestamp order after the main chain walk completes — same orphan-pickup
    rule as the claude-code precedent.

    Iterative-DFS pre-order; recursion would overflow on long linear chains
    (the claude-code precedent flagged stack overflow on 3 of 10 sessions
    before its iterative rewrite).
    """
    uuid_set = {_key(m.get("uuid")) for m in messages if _key(m.get("uuid"))}

    children: dict[str | None, list[dict]] = {}
    for m in messages:
        parent = _key(m.get("parent_message_uuid"))
        # A parent pointer that doesn't resolve within this conversation is
        # bucketed as a root (so chain walk still emits it via the None
        # bucket). Orphan-by-timestamp recovery below handles any remainder.
        key = parent if parent in uuid_set else None
        children.setdefault(key, []).append(m)

    # Stable timestamp sort within each child bucket to make sibling order
    # deterministic when the chain branches.
    for bucket in children.values():
        bucket.sort(key=_order_key)

    ordered: list[dict] = []
    emitted: set[int] = set()

    # Iterative DFS pre-order. Stack entries are (uuid_key, next_child_index):
    # we re-push the parent at index+1 before descending into the current
    # child's subtree, so peak stack depth is O(branching factor) not
    # O(chain length).
    stack: list[tuple[str | None, int]] = [(None, 0)]
    while stack:
        uuid_key, idx = stack.pop()
        bucket = children.get(uuid_key, [])
        if idx >= len(bucket):
            continue
        stack.append((uuid_key, idx + 1))
        m = bucket[idx]
        if id(m) in emitted:
            continue
        emitted.add(id(m))
        ordered.append(m)
        m_uuid = _key(m.get("uuid"))
        if m_uuid:
            stack.append((m_uuid, 0))

    # Pick up any record the DFS missed (orphans whose parent_message_uuid
    # was not None but also not present in the conversation's uuid set —
    # already bucketed under None above, so this catches DFS bugs more than
    # real cases).
    orphans = [m for m in messages if id(m) not in emitted]
    orphans.sort(key=_order_key)
    ordered.extend(orphans)

    if len(ordered) != len(messages):
        raise RuntimeError(
            f"chain walk emitted {len(ordered)} of {len(messages)} messages; "
            "this indicates a bug in order_messages, not data."
        )
    return ordered


# Timestamp fields normalised to minitrace-spec §3.5 (UTC, Z, six fractional
# digits) before anything orders them: the chain walk sorts messages by
# created_at, and the DSL takes min/max over it, which DSL spec rc3 §9.1 makes
# chronological only for normalised strings.
_CONV_TIME_FIELDS = ("created_at", "updated_at")
_MESSAGE_TIME_FIELDS = ("created_at", "updated_at")
_BLOCK_TIME_FIELDS = ("start_timestamp", "stop_timestamp")


def normalise_timestamps(conv: dict, counts: dict) -> dict:
    """Copy of `conv` with every known timestamp field in §3.5 form. A value
    timestamps.to_spec_utc cannot read (a non-string too) is null and
    counted in counts["nulled"] for the warning line (ruled 2026-10-29);
    null and the empty string are kept as they are."""
    from timestamps import to_spec_utc  # noqa: PLC0415

    def norm(d: dict, fields: tuple) -> dict:
        out = dict(d)
        for f in fields:
            v = out.get(f)
            if v is None or v == "":
                continue
            n = to_spec_utc(v)
            if n is None:
                counts["nulled"] = counts.get("nulled", 0) + 1
            elif n != v:
                counts["normalised"] = counts.get("normalised", 0) + 1
            out[f] = n
        return out

    conv = norm(conv, _CONV_TIME_FIELDS)
    msgs = []
    for m in conv.get("chat_messages") or []:
        if not isinstance(m, dict):
            msgs.append(m)
            continue
        m = norm(m, _MESSAGE_TIME_FIELDS)
        if isinstance(m.get("content"), list):
            m["content"] = [norm(b, _BLOCK_TIME_FIELDS) if isinstance(b, dict) else b
                            for b in m["content"]]
        msgs.append(m)
    if "chat_messages" in conv:
        conv["chat_messages"] = msgs
    return conv


def preprocess_conversation(conv: dict, counts: dict | None = None) -> dict:
    """Normalise timestamps (§3.5), then re-order chat_messages in chain-walk
    order. Other fields (uuid, name, summary, ...) are preserved verbatim."""
    counts = {} if counts is None else counts
    msgs = conv.get("chat_messages")
    if "chat_messages" in conv:
        # Input decides the shape (§8.3): a non-list message array or a
        # non-object message is skipped and counted, never an abort.
        if not isinstance(msgs, list):
            counts["skipped_messages"] = counts.get("skipped_messages", 0) + 1
            msgs = []
        kept = [m for m in msgs if isinstance(m, dict)]
        if len(kept) != len(msgs):
            counts["skipped_messages"] = counts.get("skipped_messages", 0) + len(msgs) - len(kept)
        conv = {**conv, "chat_messages": kept}
    conv = normalise_timestamps(conv, counts)
    if "chat_messages" not in conv:
        return conv  # nothing to order
    return {**conv, "chat_messages": order_messages(conv["chat_messages"])}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: preprocess_claude_ai.py <input.json> <output.json>", file=sys.stderr)
        return 2
    src = Path(argv[1])
    dst = Path(argv[2])
    # Invalid bytes replaced and reported, a malformed export ends with a
    # message rather than a traceback.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from source_reader import (  # noqa: PLC0415
        SourceUnreadable, decode_reporting, input_json_loads, repair_strings)
    try:
        raw = input_json_loads(decode_reporting(src))
    except SourceUnreadable as e:   # not a regular file, or over the shared bound
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (ValueError, RecursionError) as e:
        print(f"error: {str(src)!r} is not readable JSON: {e}", file=sys.stderr)
        return 2
    # A "\ud83d" escape parses to an unpaired surrogate, which the UTF-8
    # write below refuses; a noncharacter would reach the trace (not I-JSON).
    # Both are replaced with U+FFFD, as the shared readers do (DSL §4).
    try:
        raw, repairs, collisions = repair_strings(raw)
    except RecursionError:   # parsed, but deeper than the repair reaches: unreadable, as the readers rule
        print(f"error: {str(src)!r} is not readable JSON: nested too deep to check its strings",
              file=sys.stderr)
        return 2
    if repairs:
        print(f"warning: claude-ai: {src}: {len(repairs)} string(s) held an unpaired surrogate or a "
              f"noncharacter, replaced with U+FFFD at "
              + ", ".join(r["path"][:80] or "/" for r in repairs[:5])
              + (" ..." if len(repairs) > 5 else ""), file=sys.stderr)
    if collisions:
        print(f"warning: claude-ai: {src}: {len(collisions)} member(s) lost: a repaired name equals "
              f"another in its object (the later value kept)", file=sys.stderr)

    counts: dict = {}
    if isinstance(raw, list):
        convs = [c for c in raw if isinstance(c, dict)]
        if len(convs) != len(raw):
            counts["skipped_conversations"] = len(raw) - len(convs)
        out: list[dict] | dict = [preprocess_conversation(c, counts) for c in convs]
    elif isinstance(raw, dict) and "chat_messages" in raw:
        out = preprocess_conversation(raw, counts)
    else:
        print(
            f"unexpected input shape at {src}: top-level must be an array of "
            "conversation objects or a single conversation with chat_messages",
            file=sys.stderr,
        )
        return 1

    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if counts.get("normalised", 0):
        print(f"timestamps: {counts['normalised']} normalised to §3.5", file=sys.stderr)
    # A value written as null is lost input (ruled 2026-10-29).
    if counts.get("nulled", 0):
        print(f"warning: claude-ai: {src}: {counts['nulled']} timestamp(s) not convertible to the §3.5 form, "
              f"written as null", file=sys.stderr)
    # A skip is lost input, so it is a "warning:" line, which verify_run
    # counts as a loss (ruled 2026-10-21); printed only when something was.
    if counts.get("skipped_conversations", 0) or counts.get("skipped_messages", 0):
        print(f"warning: skipped {counts.get('skipped_conversations', 0)} non-object conversation(s), "
              f"{counts.get('skipped_messages', 0)} non-object message(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    sys.exit(main(sys.argv))
