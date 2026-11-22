"""Model changes inside a Claude Code session.

Raw assistant records carry `message.model` per message; the adapter kept
only the first as `environment.model`, so a mid-session /model switch was
invisible. Decisions (operator, 2026-10-01): a switch is an `x-model_shift`
segment marker (spec §4.5 x-* escape); `environment.model` is the first real
model; `<synthetic>` records never open a segment and are counted. The
ext block names the models only when a trace used more than one real model
or has synthetic records, so single-model traces are unchanged. Synthetic
sessions only.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from schema_gate import schema_errors  # noqa: E402
from test_content_loss import _cc, convert  # noqa: E402

SID = "3f0c1a2b-0000-4000-8000-00000000c0de"
A, B, SYN = "claude-model-a", "claude-model-b", "<synthetic>"


def _session(tmp_path: pathlib.Path, models: list[str]) -> dict:
    recs, parent = [], None
    for i, model in enumerate(models):
        u, a = f"u{i}", f"a{i}"
        recs.append(_cc(f"2026-09-01T10:00:{2 * i:02d}.000Z", u, parent, type="user",
                        message={"role": "user", "content": f"prompt {i}"}))
        recs.append(_cc(f"2026-09-01T10:00:{2 * i + 1:02d}.000Z", a, u, type="assistant",
                        message={"role": "assistant", "model": model,
                                 "content": [{"type": "text", "text": f"reply {i}"}]}))
        parent = a
    inp = tmp_path / f"{SID}.jsonl"
    inp.write_text("".join(json.dumps(r) + "\n" for r in recs))
    [trace] = convert(tmp_path, "claude-code", "claude-code.dsl.yaml",
                      "preprocess_claude_code.py", inp)
    return trace


def _model_markers(trace: dict) -> list[dict]:
    return [m for m in trace.get("segment_markers", []) if m["segment_type"] == "x-model_shift"]


def _ext(trace: dict) -> dict:
    return trace["environment"].get("ext:claude-code") or {}


def test_switches_become_markers_and_synthetic_does_not(tmp_path):
    trace = _session(tmp_path, [A, SYN, B, B, A])
    markers = _model_markers(trace)
    assert [m["segment_label"] for m in markers] == [B, A]
    assert [m["ext_claude_code_model_from"] for m in markers] == [A, B]
    assert all(m["ordering"] == 0 for m in markers)
    # the marker sorts before the turn it opens (same timestamp; the §4.3
    # ordering key compares `ordering` before the kind)
    asst = [t for t in trace["turns"] if t["role"] == "assistant"]
    assert [t.get("ordering") for t in asst] == [None, None, 1, None, 1]
    assert "ordering" not in asst[0]
    assert markers[0]["timestamp"] == asst[2]["timestamp"]
    assert trace["environment"]["model"] == A
    assert _ext(trace)["models_observed"] == [A, B]
    assert _ext(trace)["synthetic_assistant_records"] == 1


def test_leading_synthetic_is_not_the_model(tmp_path):
    trace = _session(tmp_path, [SYN, A, A])
    assert trace["environment"]["model"] == A
    assert _model_markers(trace) == []
    assert _ext(trace)["models_observed"] == [A]
    assert _ext(trace)["synthetic_assistant_records"] == 1


def test_only_synthetic_keeps_synthetic(tmp_path):
    trace = _session(tmp_path, [SYN])
    assert trace["environment"]["model"] == SYN
    assert _model_markers(trace) == []


def test_single_model_trace_is_unchanged(tmp_path):
    trace = _session(tmp_path, [A, A])
    assert _model_markers(trace) == []
    assert all("ordering" not in t for t in trace["turns"] if t["role"] == "assistant")
    assert "models_observed" not in _ext(trace)
    assert "synthetic_assistant_records" not in _ext(trace)


def test_model_shift_trace_is_schema_valid(tmp_path):
    assert schema_errors(_session(tmp_path, [A, SYN, B, A])) == []
