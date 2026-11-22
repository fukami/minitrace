#!/usr/bin/env python3
"""HALO/OpenInference span tree → minitrace-DSL container pre-processor.

Reads a HALO traces.jsonl file (one OpenInference-shape OTel span per line)
and emits a JSON array of containers, one per trace_id, with the
conversation reconstructed from the last LLM span's input_messages and
TOOL spans collected for tool_calls[] enrichment.

Design per the otel-openinference adapter scoping note (2026-05-06):
  O-1: pre-processor route (Path B). Runner stays domain-agnostic; this
       script handles OTel-shape conversion.
  O-3: redaction-by-omission. When llm.output_messages is absent across
       all LLM spans, emit a redaction_summary flag for the DSL to
       materialise as a Redaction entry.
  O-5: multi-inference-per-turn collapse. The reconstructed
       conversation_messages already represents the collapsed view.
  O-6: agent.Assistant root as session source-of-truth. The mcp_tools
       root contributes mcp.tools.listed to provenance.

Usage:
    python preprocess_halo.py <input.jsonl> <output.json>
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_reader import jsonl_text_lines  # noqa: E402

# Match keys like "llm.input_messages.{N}.message.role" or
# "llm.input_messages.{N}.message.tool_calls.{M}.tool_call.function.name"
_INPUT_MSG_RE = re.compile(
    r"^llm\.input_messages\.(\d+)\.message\.(.+)$"
)
_TOOL_CALL_SUBKEY_RE = re.compile(
    r"^tool_calls\.(\d+)\.tool_call\.(.+)$"
)


def reconstruct_messages(attrs: dict) -> list[dict]:
    """Parse dot-flattened llm.input_messages.{N}.message.{path} attributes
    back into a structured list of message dicts.

    Each message has shape:
        {role, content?, tool_call_id?, tool_calls?: [{id, function: {name, arguments}}, ...]}
    """
    by_index: dict[int, dict] = collections.defaultdict(dict)
    tool_calls_by_index: dict[int, dict[int, dict]] = collections.defaultdict(
        lambda: collections.defaultdict(dict)
    )

    for k, v in attrs.items():
        m = _INPUT_MSG_RE.match(k)
        if not m:
            continue
        msg_idx = int(m.group(1))
        sub = m.group(2)
        tc = _TOOL_CALL_SUBKEY_RE.match(sub)
        if tc:
            tc_idx = int(tc.group(1))
            tc_path = tc.group(2)  # e.g. "function.name", "function.arguments", "id"
            cur = tool_calls_by_index[msg_idx][tc_idx]
            # Reconstruct nested dict from dotted path
            parts = tc_path.split(".")
            node = cur
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = v
        else:
            # Plain field on the message (role, content, tool_call_id, etc.)
            by_index[msg_idx][sub] = v

    # Stitch tool_calls into messages, then sort by index
    messages = []
    for idx in sorted(by_index.keys()):
        msg = dict(by_index[idx])
        if idx in tool_calls_by_index:
            tc_list = []
            for tc_idx in sorted(tool_calls_by_index[idx].keys()):
                tc_list.append(tool_calls_by_index[idx][tc_idx])
            msg["tool_calls"] = tc_list
        messages.append(msg)

    return messages


def parse_iso(s: str | None) -> str | None:
    """Pass through ISO 8601 strings; HALO uses microsecond-precision UTC."""
    return s


def build_container(spans: list[dict]) -> dict:
    """Convert one trace_id's worth of spans into a minitrace-DSL container.

    Returns a dict with: trace_id, agent_root_span_id, agent_name, project_id,
    model_name, model_provider, service_name, sdk_name, sdk_version,
    trace_start_time, trace_end_time, mcp_tools_listed, conversation_messages,
    tool_spans, redaction_summary.
    """
    if not spans:
        return {}
    trace_id = spans[0]["trace_id"]

    # Sort by start_time for stable ordering
    spans_sorted = sorted(spans, key=lambda s: s.get("start_time", ""))

    # Find roots
    roots = [s for s in spans_sorted if not s.get("parent_span_id")]
    agent_root = next(
        (s for s in roots if (s.get("attributes") or {}).get("openinference.span.kind") == "AGENT"),
        None,
    )
    mcp_root = next(
        (s for s in roots if (s.get("attributes") or {}).get("openinference.span.kind") == "TOOL"
         and s.get("name") == "mcp_tools"),
        None,
    )

    # All LLM spans, sorted by start_time
    llm_spans = [
        s for s in spans_sorted
        if (s.get("attributes") or {}).get("openinference.span.kind") == "LLM"
    ]
    last_llm = llm_spans[-1] if llm_spans else None

    # All TOOL spans (excluding mcp_tools root, which is initialization)
    tool_spans = [
        s for s in spans_sorted
        if (s.get("attributes") or {}).get("openinference.span.kind") == "TOOL"
        and s is not mcp_root
    ]

    # Collect tool span metadata (including span_id linkage to tool_call.id when discoverable)
    tool_spans_meta = []
    for s in tool_spans:
        attrs = s.get("attributes") or {}
        tool_spans_meta.append({
            "span_id": s.get("span_id"),
            "parent_span_id": s.get("parent_span_id"),
            "name": s.get("name"),
            "start_time": s.get("start_time"),
            "end_time": s.get("end_time"),
            "mcp_server": attrs.get("mcp.server"),
        })

    # Reconstruct conversation from last LLM span's input_messages
    conversation_messages = []
    if last_llm:
        attrs = last_llm.get("attributes") or {}
        conversation_messages = reconstruct_messages(attrs)

    # Container metadata
    agent_attrs = (agent_root or {}).get("attributes") or {}
    llm_attrs = (last_llm or {}).get("attributes") or {}

    # mcp_tools.listed is a JSON-encoded string; pass it through as-is
    mcp_tools_listed = None
    if mcp_root:
        mcp_attrs = mcp_root.get("attributes") or {}
        mcp_tools_listed = mcp_attrs.get("mcp.tools.listed")

    # Redaction summary
    has_any_output = any(
        any(k.startswith("llm.output_messages.") for k in (s.get("attributes") or {}).keys())
        for s in llm_spans
    )
    output_messages_absent = not has_any_output

    tokens_absent = all(
        (s.get("attributes") or {}).get("inference.llm.input_tokens") is None
        and (s.get("attributes") or {}).get("inference.llm.output_tokens") is None
        for s in llm_spans
    )

    # Trace-level start/end from agent_root if available
    if agent_root:
        trace_start = agent_root.get("start_time")
        trace_end = agent_root.get("end_time")
    elif spans_sorted:
        trace_start = spans_sorted[0].get("start_time")
        trace_end = spans_sorted[-1].get("end_time")
    else:
        trace_start = trace_end = None

    # Pull resource and scope info (uniform across spans in a trace)
    sample = spans_sorted[0]
    resource_attrs = (sample.get("resource") or {}).get("attributes") or {}
    scope_info = sample.get("scope") or {}

    return {
        "trace_id": trace_id,
        "agent_root_span_id": (agent_root or {}).get("span_id"),
        "agent_name": agent_attrs.get("agent.workflow.name") or llm_attrs.get("agent.workflow.name"),
        "project_id": llm_attrs.get("inference.project_id") or agent_attrs.get("inference.project_id"),
        "model_name": llm_attrs.get("inference.llm.model_name") or llm_attrs.get("llm.model_name"),
        "model_provider": llm_attrs.get("inference.llm.provider") or llm_attrs.get("llm.provider"),
        "service_name": resource_attrs.get("service.name"),
        "sdk_name": scope_info.get("name"),
        "sdk_version": scope_info.get("version"),
        "trace_start_time": trace_start,
        "trace_end_time": trace_end,
        "mcp_tools_listed": mcp_tools_listed,
        "conversation_messages": conversation_messages,
        "tool_spans": tool_spans_meta,
        "n_llm_spans": len(llm_spans),
        "n_tool_spans": len(tool_spans),
        "redaction_summary": {
            "output_messages_absent": output_messages_absent,
            "tokens_absent": tokens_absent,
            "n_llm_spans": len(llm_spans),
            "final_assistant_response_lost": output_messages_absent,
        },
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <input.jsonl> <output.json>", file=sys.stderr)
        return 1
    inp = Path(sys.argv[1])
    outp = Path(sys.argv[2])

    spans_by_trace: dict[str, list[dict]] = collections.defaultdict(list)
    # Framed as the readers frame jsonl (DSL rc3 §15): a line of
    # VT, FF or Unicode spaces is not blank, so it fails json.loads as any
    # other malformed line does here.
    with inp.open() as f:
        text = f.read()
    for _, line in jsonl_text_lines(text):
        span = json.loads(line)
        spans_by_trace[span["trace_id"]].append(span)

    containers = []
    for tid in spans_by_trace:
        c = build_container(spans_by_trace[tid])
        if c:
            containers.append(c)

    outp.write_text(json.dumps(containers, indent=2, ensure_ascii=False))
    print(f"wrote {len(containers)} containers to {outp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
