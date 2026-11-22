#!/usr/bin/env python3
"""gemini-cli session JSON → minitrace-DSL container pre-processor.

Reads a single gemini-cli session file (one JSON document at
~/.gemini/tmp/<project>/chats/session-*.json) and emits a single-element
JSON array containing one container ready for the DSL.

Each container becomes one minitrace agent-session trace. The DSL iterates
container.messages as the record stream.

Design per the gemini-cli adapter scoping note (2026-05-08):

  S2 (DSL constraint): the runner's predicates can't combine multiple
                       conditions on a record (e.g. type=gemini AND
                       toolCalls != null). Synthesise event_kind here.

  S3 (content normalisation): user messages have content as a list of
                              {text} blocks; gemini messages have content
                              as a string. Normalise to a single
                              content_text field for uniform DSL access.

Usage:
    python preprocess_gemini_cli.py <input.json> <output.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from timestamps import take_refused, to_spec_utc  # noqa: E402


# The session file decides every value's type. A `messages` that is not a
# list, or a message that is not an object, must not abort the export; these
# read such a value as absent (as the claude-code pre-processor does).
def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _list(v) -> list:
    return v if isinstance(v, list) else []


def derive_trace_id(session_id: str) -> str:
    return f"gemini-cli-{session_id}"


def derive_trace_title(summary: str | None, session_id: str) -> str:
    if isinstance(summary, str) and summary:
        return summary
    return f"gemini-cli session {session_id[:8]}"


def normalise_timestamp(ts: str | None) -> str | None:
    """Spec §3.5 form (UTC, six fractional digits) via the shared
    offset-aware normaliser. gemini-cli writes `2026-04-12T01:24:24.366Z`;
    an offset such as `+02:00` is converted to the UTC instant, not dropped
    (the earlier slicing kept the local clock time and appended Z). A value
    it cannot read, a non-string too, is null and counted for the warning
    line (ruled 2026-10-29); null and the empty string are null.
    """
    return to_spec_utc(ts)


def normalise_content(content) -> str:
    """User content is list-of-{text} blocks; gemini content is a string.

    Both cases yield a single string. Multi-block user input is joined
    with newline separators (preserving block boundaries lossy-text-wise).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def annotate_messages(messages: list[dict]) -> list[dict]:
    """Attach event_kind discriminator + content_text per message.

    event_kind values:
      - "user"           : type=user
      - "gemini_text"    : type=gemini AND no toolCalls (or empty list)
      - "gemini_tools"   : type=gemini AND toolCalls is a non-empty list
      - "info"           : type=info (CLI system info messages,
                           e.g. "Last output copied to the clipboard";
                           emit as runtime observation annotation,
                           not as a turn)
      - "unknown"        : everything else (catch-all; surfaces in
                           coverage report if a new type appears)
    """
    out: list[dict] = []
    for m in messages:
        m = _obj(m)  # kept in the container as an unknown event; no DSL rule emits it
        msg_type = m.get("type")
        tool_calls = m.get("toolCalls")
        if not isinstance(tool_calls, list):
            tool_calls = []

        if msg_type == "user":
            event_kind = "user"
        elif msg_type == "gemini":
            event_kind = "gemini_tools" if tool_calls else "gemini_text"
        elif msg_type == "info":
            event_kind = "info"
        else:
            event_kind = "unknown"

        annotated = dict(m)
        annotated["event_kind"] = event_kind
        annotated["tool_calls"] = tool_calls
        annotated["content_text"] = normalise_content(m.get("content"))
        if "timestamp" in annotated:
            annotated["timestamp"] = normalise_timestamp(annotated.get("timestamp"))
        out.append(annotated)
    return out


def detect_model_observed(messages: list[dict]) -> str | None:
    """Pick a session-level model from the message stream.

    Per V2 (scoping verification): model is constant within a session in
    the local 11-session corpus. We pick the first non-null model value
    we encounter. If a future session model-switches, both the trace's
    `environment.model` and per-message `model` are passthrough fields,
    so the producer-side variation is recoverable.
    """
    for m in messages:
        model = _obj(m).get("model")
        if isinstance(model, str) and model:
            return model
    return None


def build_container(session: dict, source_path: Path | None = None) -> dict:
    # A session id counts only as a string (ruled 2026-10-27); anything else,
    # null included, reads as absent rather than being formatted by str().
    session_id = session.get("sessionId")
    if not isinstance(session_id, str):
        session_id = "unknown"
    summary = session.get("summary")
    raw_messages = _list(session.get("messages"))

    annotated = annotate_messages(raw_messages)
    model_observed = detect_model_observed(raw_messages)

    return {
        "kind": session.get("kind"),
        "sessionId": session_id,
        "projectHash": session.get("projectHash"),
        "startTime": normalise_timestamp(session.get("startTime")),
        "lastUpdated": normalise_timestamp(session.get("lastUpdated")),
        "summary": summary,
        "messages": annotated,
        "model_observed": model_observed,
        "trace_id": derive_trace_id(session_id),
        "trace_title": derive_trace_title(summary, session_id),
        "source_format": "gemini-cli-session-v1",
        "source_path": str(source_path) if source_path is not None else None,
        # Pinned per scoping note: stable schema observed across 11 sessions
        # spanning gemini-cli ~0.33-0.40 (2026-03-13 → 2026-04-12).
        "harness_version": "v0.33.1+",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: preprocess_gemini_cli.py <input.json> <output.json>",
            file=sys.stderr,
        )
        return 2
    in_path = Path(argv[1])
    out_path = Path(argv[2])

    # Through the shared reader: invalid UTF-8 is repaired and recorded, an
    # unparsable file ends with a message rather than a traceback.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from source_reader import SourceUnreadable, read_json  # noqa: PLC0415
    try:
        src = read_json(in_path)
    except SourceUnreadable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if src.findings:
        reasons = sorted({f"{f.get('kind')}/{f.get('reason')}" for f in src.findings})
        print(f"warning: {str(in_path)!r}: {len(src.findings)} input finding(s): "
              f"{', '.join(reasons)}", file=sys.stderr)
    if not src.records:
        # Ruled 2026-10-28: the one record was skipped (nested deeper than
        # 512) or the file holds an empty array; nothing to convert,
        # refused as an unparsable file is.
        print(f"error: {in_path}: no readable record", file=sys.stderr)
        return 2
    session = src.records[0]
    take_refused()
    container = build_container(session, source_path=in_path)
    # A timestamp written as null is lost input (ruled 2026-10-29).
    if nulled := take_refused():
        print(f"warning: gemini-cli: {in_path}: {nulled} timestamp(s) not convertible to the §3.5 form, "
              f"written as null", file=sys.stderr)
    out_path.write_text(json.dumps([container], indent=2, sort_keys=True))

    n_msg = len(container["messages"])
    n_tools = sum(len(m["tool_calls"]) for m in container["messages"])
    model = container["model_observed"] or "?"
    print(
        f"gemini-cli preprocess: 1 container "
        f"(session={container['sessionId'][:8]}, model={model}, "
        f"messages={n_msg}, tool_calls={n_tools})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    raise SystemExit(main(sys.argv))
