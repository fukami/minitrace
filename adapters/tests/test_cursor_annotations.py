"""cursor's interaction and synthetic-turn annotations carry a recorded time
and a schema-valid source; before, both format-reference samples with such
annotations failed the schema gate (null timestamp, source.type 'derived')."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ADAPTERS))
from schema_gate import schema_errors  # noqa: E402

REF = ADAPTERS / "cursor" / "format-reference"


def _convert(tmp: pathlib.Path, name: str) -> dict:
    events, traces = tmp / "events.json", tmp / "traces.json"
    subprocess.run([sys.executable, str(ADAPTERS / "cursor" / "preprocess_cursor.py"),
                    str(REF / name), str(events)], check=True, capture_output=True, text=True, timeout=300)
    subprocess.run([sys.executable, str(ADAPTERS / "dsl_runner.py"),
                    str(ADAPTERS / "cursor" / "cursor.dsl.yaml"), str(events),
                    "--output", str(traces), "--operator-type", "scripted"],
                   check=True, capture_output=True, text=True, timeout=300)
    out = json.loads(traces.read_text())
    return out[0] if isinstance(out, list) else out


def test_modeswitch_annotations_have_recorded_times(tmp_path):
    t = _convert(tmp_path, "session-v2026.07.23-e383d2b.modeswitch.stream.jsonl")
    assert schema_errors(t) == []
    stamps = {a["timestamp"] for a in t["annotations"] if a["annotator"] == "cursor-adapter"}
    # interaction request, and the synthetic turn's first tool call
    assert {"2026-07-25T11:32:20.249000Z", "2026-07-25T11:32:20.274000Z"} <= stamps
    assert all((a.get("source") or {}).get("type") == "runtime" for a in t["annotations"])


def test_planmode_is_schema_valid(tmp_path):
    t = _convert(tmp_path, "session-v2026.07.23-e383d2b.planmode.stream.jsonl")
    assert schema_errors(t) == []
