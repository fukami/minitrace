"""Claude Code token metrics count each API response once.

Claude Code writes one API response as several assistant records, one per
content block, each repeating message.usage. The DSL sums usage per
assistant_turn, so the adapter counted a response once per record: 1.00x to
3.58x over the 21 frozen specimens. The pre-processor now keeps usage on the
last record of each message.id. Synthetic sessions pin the rule; the corpus
test holds every frozen claude-code rung input to the invariant
(usage-bearing events equal unique message ids) and to token sums taken
directly from the raw JSONL.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from test_content_loss import _cc, convert  # noqa: E402

ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
CC = ADAPTERS / "claude-code"
SID = "3f0c1a2b-0000-4000-8000-00000000c0de"
METRICS = {
    "total_input_tokens": "input_tokens",
    "total_output_tokens": "output_tokens",
    "total_cache_creation_tokens": "cache_creation_input_tokens",
    "total_cache_read_tokens": "cache_read_input_tokens",
}


def _usage(i: int, o: int, cc: int, cr: int) -> dict:
    return {"input_tokens": i, "output_tokens": o,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}


def _asst(t: str, uuid: str, parent: str, mid: str | None, usage: dict, text: str) -> dict:
    msg = {"role": "assistant", "model": "m", "usage": usage,
           "content": [{"type": "text", "text": text}]}
    if mid is not None:
        msg["id"] = mid
    return _cc(t, uuid, parent, type="assistant", message=msg)


def _metrics(tmp_path: pathlib.Path, assistants: list[dict]) -> dict:
    recs = [_cc("2026-09-01T10:00:00.000Z", "u0", None, type="user",
                message={"role": "user", "content": "go"}), *assistants]
    inp = tmp_path / f"{SID}.jsonl"
    inp.write_text("".join(json.dumps(r) + "\n" for r in recs))
    [trace] = convert(tmp_path, "claude-code", "claude-code.dsl.yaml",
                      "preprocess_claude_code.py", inp)
    return {k: trace["metrics"].get(k) for k in METRICS}


def test_one_response_over_three_records_counts_once(tmp_path):
    u1, u2 = _usage(3, 50, 1000, 200), _usage(1, 7, 0, 1200)
    got = _metrics(tmp_path, [
        _asst("2026-09-01T10:00:01.000Z", "a1", "u0", "msg_1", u1, "x"),
        _asst("2026-09-01T10:00:02.000Z", "a2", "a1", "msg_1", u1, "y"),
        _asst("2026-09-01T10:00:03.000Z", "a3", "a2", "msg_1", u1, "z"),
        _asst("2026-09-01T10:00:04.000Z", "a4", "a3", "msg_2", u2, "w"),
    ])
    assert got == {"total_input_tokens": 4, "total_output_tokens": 57,
                   "total_cache_creation_tokens": 1000, "total_cache_read_tokens": 1400}


def test_differing_usage_within_one_message_takes_the_last_record(tmp_path):
    got = _metrics(tmp_path, [
        _asst("2026-09-01T10:00:01.000Z", "a1", "u0", "msg_1", _usage(3, 1, 10, 20), "x"),
        _asst("2026-09-01T10:00:02.000Z", "a2", "a1", "msg_1", _usage(3, 9, 10, 20), "y"),
    ])
    assert got["total_output_tokens"] == 9
    assert got["total_input_tokens"] == 3


def test_records_without_message_id_each_keep_their_usage(tmp_path):
    got = _metrics(tmp_path, [
        _asst("2026-09-01T10:00:01.000Z", "a1", "u0", None, _usage(2, 5, 0, 0), "x"),
        _asst("2026-09-01T10:00:02.000Z", "a2", "a1", None, _usage(2, 5, 0, 0), "y"),
    ])
    assert got["total_input_tokens"] == 4
    assert got["total_output_tokens"] == 10


def _rung_inputs() -> list[tuple[pathlib.Path, str]]:
    out = []
    for manifest in sorted((CC / "regression").glob("*/manifest.json")):
        for sc in json.loads(manifest.read_text())["scenarios"].values():
            out.append((manifest.parent, sc["input"]))
    return out


RUNG_INPUTS = _rung_inputs()


def test_rung_corpus_is_present():
    # 3 rungs x 7 scenarios when frozen (2026-07-25); an empty list would pass
    # the parametrized test below vacuously.
    assert len(RUNG_INPUTS) >= 21


@pytest.mark.parametrize("rung,inp", RUNG_INPUTS,
                         ids=[f"{r.name}/{i}" for r, i in RUNG_INPUTS])
def test_frozen_inputs_count_each_message_once(rung, inp, tmp_path):
    raw: dict[str, dict] = {}
    for line in (rung / inp).read_text().splitlines():
        rec = json.loads(line)
        msg = rec.get("message") or {}
        if rec.get("type") == "assistant" and isinstance(msg.get("usage"), dict):
            assert msg.get("id"), "a frozen record without message.id: extend this test"
            raw[msg["id"]] = msg["usage"]

    events = tmp_path / "events.json"
    traces = tmp_path / "traces.json"
    subprocess.run([sys.executable, str(CC / "preprocess_claude_code.py"), inp, str(events)],
                   cwd=rung, check=True, capture_output=True, text=True, timeout=300)
    containers = json.loads(events.read_text())
    main = containers[0] if isinstance(containers, list) else containers
    bearing = [e for e in main["events"]
               if e.get("event_kind") == "assistant_turn" and isinstance(e.get("usage"), dict)]
    # Usage-bearing events equal unique message ids
    assert len(bearing) == len(raw)

    subprocess.run([sys.executable, str(ADAPTERS / "dsl_runner.py"),
                    str(CC / "claude-code.dsl.yaml"), str(events), "--output", str(traces),
                    "--operator-type", "scripted"],
                   cwd=rung, check=True, capture_output=True, text=True, timeout=300)
    metrics = json.loads(traces.read_text())[0]["metrics"]
    for metric, key in METRICS.items():
        assert metrics.get(metric) == sum(u.get(key) or 0 for u in raw.values()), metric
