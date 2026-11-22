#!/usr/bin/env python3
"""Preprocessor for the minimcp MCP-server adapter.

Reads one minimcp wire-level NDJSON capture (the server-side stderr event
stream from `minimcp-server`) and emits a flat JSONL intermediate that the
minitrace DSL (`minimcp.dsl.yaml`) maps 1:1 into a v0.3.0 bridge-session trace.

Why a preprocessor: the headline
divergence — a server-initiated request that the host never services — is
detectable only by **request-without-response correlation**, not by any single
event (real hosts kill the process before `lifecycle:warning:outbound_abandoned`
fires). That correlation is stateful and cannot be expressed in the 1:1 DSL, so
it lives here. This pass also computes the advertised-vs-serviced divergence and
pre-assigns stable annotation ids.

Vantage (design note §3): minimcp observes the MCP conversation from the SERVER
end. It is structurally blind to the host's caller-type, so client tool calls
carry invocation_source=unknown. Server-initiated flows, capability negotiation,
and abandonment ride ext:bridge:* / ext:mcp:* annotations.

Output records (one JSON object per line, scanned in order by the DSL):
  {"kind":"session_meta", ...}     exactly one, first line
  {"kind":"server_request", ...}   one per server-initiated request
  {"kind":"tool_call", ...}        one per client tools/call (paired with result)
  {"kind":"cap_divergence", ...}   one summary, only when init completed

Usage:
    preprocess_minimcp.py <capture.ndjson> [<out.jsonl>]
    preprocess_minimcp.py <capture.ndjson> [--output <out.jsonl>]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from input_tree import field  # noqa: E402
from source_reader import jsonl_text_lines  # noqa: E402
from timestamps import take_refused, to_spec_utc  # noqa: E402

# MCP capability  <->  server-initiated request family (event_kind prefix).
# The family is the event_kind with the trailing ":request"/":response" removed.
CAP_FOR_FAMILY = {
    "roots:list": "roots",
    "sampling:create": "sampling",
    "elicitation:create": "elicitation",
}


def _family(event_kind: str) -> str | None:
    """roots:list:request -> roots:list ; tool:call:response -> tool:call."""
    for suffix in (":request", ":response"):
        if event_kind.endswith(suffix):
            return event_kind[: -len(suffix)]
    return None


def _summarise(payload: dict | None) -> str:
    """Compact, PII-light one-line summary of a JSON-RPC payload's result/error."""
    if not isinstance(payload, dict):
        return ""
    if "error" in payload:
        err = payload["error"] or {}
        return f"error {err.get('code')}: {err.get('message')}"
    if "result" in payload:
        return json.dumps(payload["result"], sort_keys=True)[:200]
    return ""


def process(events: list[dict]) -> list[dict]:
    if not events:
        return []

    # Every emitted ts derives from an event's ts, so it is put in the §3.5
    # form once here, by timestamps.to_spec_utc as the other pre-processors
    # do; a value it cannot read (an invalid calendar date, malformed, not a
    # string) is null and counted for take_refused(), never copied through;
    # main() reports the count in one warning line for the input.
    for e in events:
        if isinstance(e, dict) and "ts" in e:
            e["ts"] = to_spec_utc(e["ts"])

    session_id = next((e.get("session_id") for e in events if e.get("session_id")), "unknown")
    transport = next((e.get("transport") for e in events if e.get("transport")), None)
    timestamps = [e["ts"] for e in events if e.get("ts")]
    first_ts = min(timestamps) if timestamps else None
    last_ts = max(timestamps) if timestamps else None

    # --- capability negotiation -------------------------------------------------
    init_req = next((e for e in events if e.get("event_kind") == "lifecycle:initialize:request"), None)
    init_resp = next((e for e in events if e.get("event_kind") == "lifecycle:initialize:response"), None)

    host = host_version = host_title = None
    protocol_offered = None
    advertised: list[str] = []
    if init_req:
        params = (init_req.get("payload") or {}).get("params") or {}
        ci = params.get("clientInfo") or {}
        host, host_version, host_title = ci.get("name"), ci.get("version"), ci.get("title")
        protocol_offered = params.get("protocolVersion")
        advertised = sorted((params.get("capabilities") or {}).keys())

    init_ok = False
    protocol_supported = None
    reject_code = None
    if init_resp:
        rp = init_resp.get("payload") or {}
        if "result" in rp and rp["result"] is not None:
            init_ok = True
            protocol_supported = (rp["result"] or {}).get("protocolVersion")
        elif "error" in rp:
            err = rp["error"] or {}
            reject_code = err.get("code")
            protocol_supported = (err.get("data") or {}).get("supported")

    out: list[dict] = []
    out.append({
        "kind": "session_meta",
        "session_id": session_id,
        "transport": transport,
        "host": host,
        "host_version": host_version,
        "host_title": host_title,
        "protocol_offered": protocol_offered,
        "protocol_supported": protocol_supported,
        "init_ok": init_ok,
        "reject_code": reject_code,
        "advertised": advertised,
        "first_ts": first_ts,
        "last_ts": last_ts,
    })

    # --- correlate request / response by (family, request_id) -------------------
    # Build a response index so each request can find its match. request_id may be
    # absent for some families; fall back to family-only matching in declaration order.
    responses: dict[tuple, list[dict]] = {}
    for e in events:
        ek = e.get("event_kind", "")
        if ek.endswith(":response"):
            fam = _family(ek)
            if fam is None:
                continue
            responses.setdefault((fam, e.get("request_id")), []).append(e)
            responses.setdefault((fam, None), []).append(e)

    def take_response(fam: str, rid) -> dict | None:
        for key in ((fam, rid), (fam, None)):
            bucket = responses.get(key)
            if bucket:
                return bucket[0]
        return None

    ann_n = 0

    def next_ann() -> str:
        nonlocal ann_n
        ann_n += 1
        return f"mcp-ann-{ann_n:04d}"

    serviced_caps: set[str] = set()

    for e in events:
        ek = e.get("event_kind", "")
        if not ek.endswith(":request"):
            continue
        fam = _family(ek)
        if fam is None:
            continue
        rid = e.get("request_id")
        direction = e.get("direction")

        # Server-initiated request (direction "out"): the divergence carrier.
        if direction == "out" and fam in CAP_FOR_FAMILY:
            resp = take_response(fam, rid)
            serviced = resp is not None
            cap = CAP_FOR_FAMILY[fam]
            if serviced:
                serviced_caps.add(cap)
            out.append({
                "kind": "server_request",
                "ann_id": next_ann(),
                "ts": e.get("ts"),
                "session_id": session_id,
                "method": (e.get("method") or fam),
                "capability": cap,
                "advertised": cap in advertised,
                "serviced": serviced,
                "response_summary": _summarise(resp.get("payload")) if resp else None,
            })

        # Client-initiated tool call (direction "in"): tool:call family.
        elif direction == "in" and fam == "tool:call":
            resp = take_response(fam, rid)
            payload = e.get("payload") or {}
            params = payload.get("params") or {}
            out.append({
                "kind": "tool_call",
                "id": f"mcp-tc-{rid}",
                "ts": e.get("ts"),
                "tool_name": params.get("name"),
                "arguments": params.get("arguments") or {},
                "output_summary": _summarise(resp.get("payload")) if resp else None,
                "serviced": resp is not None,
            })

    # --- advertised-vs-serviced divergence (only meaningful if init completed) ---
    if init_ok:
        server_initiated_caps = {
            cap
            for e in events
            if e.get("direction") == "out" and e.get("event_kind", "").endswith(":request")
            for cap in (CAP_FOR_FAMILY.get(_family(e.get("event_kind", "")) or ""),)
            if cap
        }
        advertised_set = set(advertised)
        # advertised-vs-serviced divergence has TWO independent directions, both
        # observable from the server vantage:
        #
        #   unserviced (advertised-but-unserviced): the host advertised a
        #     capability, the server fired the matching request, the host did NOT
        #     service it. (claude-code advertises roots, never answers roots/list;
        #     goose advertises elicitation, abandons elicitation/create.) Scoped to
        #     advertised ∩ fired so it matches the field's stated meaning — the DSL
        #     detail already calls it "advertised-and-fired unserviced". An earlier
        #     cut computed fired − serviced, which would also count an unadvertised
        #     fired-then-ignored capability; that is NOT a host divergence (see
        #     below) and is now excluded.
        #
        #   serviced_unadvertised (serviced-but-unadvertised): the host serviced a
        #     server-initiated request for a capability it did NOT advertise. (codex
        #     advertises elicitation only, yet answers roots/list with {roots:[]}.)
        #     Observable only because minimcp fires server-initiated requests
        #     regardless of the client's advertisement (divergence EM10); a strictly
        #     capability-gating server would never surface this direction.
        #
        # A request fired for an unadvertised capability that the host then ignores
        # is excluded from both sets: the host correctly declined a capability it
        # never offered, which is conformant, not a divergence.
        unserviced = sorted((advertised_set & server_initiated_caps) - serviced_caps)
        serviced_unadvertised = sorted(serviced_caps - advertised_set)
        lists = {"advertised": advertised, "serviced": sorted(serviced_caps), "unserviced": unserviced,
                 "serviced_unadvertised": serviced_unadvertised, "fired": sorted(server_initiated_caps)}
        out.append({
            "kind": "cap_divergence",
            "ann_id": next_ann(),
            "ts": first_ts,
            "session_id": session_id,
            **lists,
            # The DSL interpolates text, not lists: each list also as its
            # names joined with ", ", "none" when empty, for the title and
            # detail of the annotation; the lists stay as they are.
            **{f"{k}_text": ", ".join(v) if v else "none" for k, v in lists.items()},
            # Neutral outcome discriminator. The server-side bridge vantage cannot
            # establish FAULT for either divergence direction (it is blind to client
            # motive: workspace-trust gating, conditional capability, etc.), so the
            # annotation stays an observation; "divergence" is descriptive, not a
            # failure verdict. Fault interpretation is a downstream consumer's call.
            "outcome": "divergence" if (unserviced or serviced_unadvertised) else "conformant",
        })

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Preprocess a minimcp NDJSON capture into DSL-ready JSONL.")
    ap.add_argument("input", type=pathlib.Path, help="minimcp NDJSON capture")
    # The output as a second positional argument is the form every
    # pre-processor takes and verify_run.py passes; --output is kept.
    ap.add_argument("output_path", type=pathlib.Path, nargs="?", default=None, metavar="output",
                    help="output JSONL (default: stdout)")
    ap.add_argument("--output", type=pathlib.Path, default=None, help="output JSONL, as the positional form")
    args = ap.parse_args()
    if args.output_path is not None and args.output is not None:
        ap.error("give the output once: as the second argument or as --output")
    args.output = args.output or args.output_path
    if args.output is not None and args.output.exists() and args.input.exists() \
            and args.output.samefile(args.input):
        ap.error(f"the output {args.output} is the input; it would be overwritten")

    events: list[dict] = []
    # Framed as the readers frame jsonl (DSL rc3 §15): not at
    # U+2028/U+2029/U+0085 as splitlines(), which cut a record holding one
    # in a string; a line of VT, FF or Unicode spaces is malformed, not blank.
    for ln, raw in jsonl_text_lines(args.input.read_text(encoding="utf-8")):
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            print(f"warning: skipping malformed line {ln}: {exc}", file=sys.stderr)

    take_refused()
    records = process(events)
    if (n := take_refused()):
        print(f"warning: minimcp: {field(str(args.input))}: {n} timestamp(s) not convertible to the "
              f"§3.5 form, written as null", file=sys.stderr)
    if not records:
        print("warning: no records produced (empty capture)", file=sys.stderr)

    lines = "\n".join(json.dumps(r, sort_keys=True) for r in records) + ("\n" if records else "")
    if args.output:
        args.output.write_text(lines, encoding="utf-8")
    else:
        sys.stdout.write(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
