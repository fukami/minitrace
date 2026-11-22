"""A claude-code session file with records but no conversation fails closed.

Found in the organic archive: files holding only `file-history-snapshot`
records. Every record is discarded by adapter policy, so there is nothing to
convert; before this the pre-processor emitted a container with no session_id
and the pipeline crashed later with a traceback. Converter input contract §2:
a zero-record result terminates with a structured error that declares a reason.

Loaded via importlib because the adapter lives under a hyphenated directory.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADAPTERS))
import source_reader as sr  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "preprocess_claude_code", _ADAPTERS / "claude-code" / "preprocess_claude_code.py"
)
assert _spec is not None and _spec.loader is not None
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _write(tmp_path: pathlib.Path, records: list[dict]) -> pathlib.Path:
    p = tmp_path / "3f0c1a2b-0000-4000-8000-000000000000.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


def test_only_discarded_records_fails_closed(tmp_path):
    path = _write(tmp_path, [{"type": "file-history-snapshot", "messageId": f"m{i}"} for i in range(3)])
    with pytest.raises(sr.SourceUnreadable) as exc:
        pp.build_containers_with_fidelity(path)
    assert exc.value.reason == "no-emittable-records"
    assert exc.value.detail == "file-history-snapshot=3"


def test_no_session_id_fails_closed(tmp_path):
    # older Claude Code versions wrote files holding only `summary` records
    path = _write(tmp_path, [{"type": "summary", "summary": "s", "leafUuid": f"l{i}"} for i in range(2)])
    with pytest.raises(sr.SourceUnreadable) as exc:
        pp.build_containers_with_fidelity(path)
    assert exc.value.reason == "no-session-id"
    assert exc.value.detail == "summary=2"


def test_one_conversation_record_still_converts(tmp_path):
    path = _write(tmp_path, [
        {"type": "file-history-snapshot", "messageId": "m0"},
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "3f0c1a2b-0000-4000-8000-000000000000",
         "timestamp": "2026-01-01T00:00:00.000Z", "message": {"role": "user", "content": "hello"}},
    ])
    containers, _fidelity = pp.build_containers_with_fidelity(path)
    assert containers[0]["session_id"] == "3f0c1a2b-0000-4000-8000-000000000000"
