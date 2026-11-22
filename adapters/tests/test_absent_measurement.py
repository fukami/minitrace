"""An absent measurement must be emitted as null, never as 0.

`derive_token_totals()` in the droid pre-processor returns `(None, None)` when
the session sidecar carries no `tokenUsage`, and its docstring argues that
choice as a fleet precedent: a measurement that was never taken must not read
as a measurement of zero. Nothing committed asserted it. The content-hash
freeze cannot: every frozen droid specimen carries `tokenUsage`, so the corpus
only ever exercises the present path, and a later `or 0` would regress the
branch with all four rung hashes still green.

Neither can the coverage cell. `drift_watch.py` reads cell 26 as
`any(metrics.get(k, 0) ...)`, which collapses null and 0 to the same dark
result — so cell darkness is not a witness for the distinction. This test
therefore inspects the **emitted value**, end to end through preprocess + DSL,
not the cell and not only the function.

Three arms, all four pipeline runs on real frozen inputs:

  - present   — the sidecar's integers reach `metrics.total_*_tokens`
                unchanged (the control; without it the absent arm would pass
                just as well on a pipeline that emitted nothing at all)
  - absent    — `tokenUsage` removed: both keys still present, both null
  - partial   — one of the two counters removed: the survivor stays an int,
                the missing one goes null on its own

The fleet-wide arm is deliberately out of scope. Other adapters do not yet
agree on these key names or on their absent-value convention, and goose
actively emits 0 for counters it never read; settling that is a fleet decision,
not this test's.

Run:
    python -m pytest adapters/tests/test_absent_measurement.py -v
or:
    python adapters/tests/test_absent_measurement.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
DROID = ADAPTERS / "droid"
PREPROCESS = DROID / "preprocess_droid.py"
DSL = DROID / "droid.dsl.yaml"
DSL_RUNNER = ADAPTERS / "dsl_runner.py"

# Loaded via importlib: adapter directories are not importable packages.
_spec = importlib.util.spec_from_file_location("preprocess_droid", PREPROCESS)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
derive_token_totals = _mod.derive_token_totals


# ---------------------------------------------------------------------------
# Specimen discovery — dynamic, and empty is an error, not a pass

def _specimens() -> list[tuple[pathlib.Path, pathlib.Path]]:
    """Frozen droid (jsonl, settings) pairs whose sidecar reports tokenUsage.

    Discovered rather than hard-coded so a new droid rung joins by existing,
    matching run_fleet_sweep.py's discovery rule.
    """
    found = []
    for settings in sorted(DROID.glob("regression/*/inputs/*.settings.json")):
        jsonl = settings.with_name(settings.name.removesuffix(".settings.json") + ".jsonl")
        if not jsonl.is_file():
            continue
        doc = json.loads(settings.read_text())
        if isinstance(doc.get("tokenUsage"), dict):
            found.append((jsonl, settings))
    return found


def _one_per_rung() -> list[tuple[pathlib.Path, pathlib.Path]]:
    """First specimen of each rung, so both rungs are exercised without paying
    for all four (the present path is already frozen per scenario by verify.py)."""
    by_rung: dict[str, tuple[pathlib.Path, pathlib.Path]] = {}
    for jsonl, settings in _specimens():
        by_rung.setdefault(jsonl.parents[1].name, (jsonl, settings))
    return [by_rung[k] for k in sorted(by_rung)]


def _emit_metrics(jsonl: pathlib.Path, settings_doc: dict) -> dict:
    """Run preprocess + DSL over one input with a substituted sidecar.

    The pair is copied into a temp dir because the pre-processor resolves the
    sidecar by suffix swap next to the input; the frozen inputs are never
    written to.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        shutil.copy(jsonl, tmp / jsonl.name)
        sidecar = tmp / (jsonl.name.removesuffix(".jsonl") + ".settings.json")
        sidecar.write_text(json.dumps(settings_doc))
        events, traces = tmp / "events.json", tmp / "traces.json"
        subprocess.run(
            [sys.executable, str(PREPROCESS), jsonl.name, str(events)],
            cwd=tmp, check=True, capture_output=True, text=True, timeout=300,
        )
        subprocess.run(
            [sys.executable, str(DSL_RUNNER), str(DSL), str(events),
             "--output", str(traces), "--operator-type", "scripted"],
            cwd=tmp, check=True, capture_output=True, text=True, timeout=300,
        )
        out = json.loads(traces.read_text())
        out_list = out if isinstance(out, list) else [out]
        assert len(out_list) == 1, f"expected one droid trace, got {len(out_list)}"
        return out_list[0]["metrics"]


# ---------------------------------------------------------------------------
# Emitted-value arms

def test_specimens_exist() -> None:
    specimens = _specimens()
    assert specimens, (
        "no frozen droid specimen carries a tokenUsage sidecar — the arms below "
        "would pass vacuously")


def test_present_measurement_reaches_metrics_unchanged() -> None:
    """Control. Establishes that this pipeline does emit the counters at all,
    so a null in the absent arm means 'derived null' and not 'nothing ran'."""
    jsonl, settings = _one_per_rung()[0]
    doc = json.loads(settings.read_text())
    metrics = _emit_metrics(jsonl, doc)
    for key in ("total_input_tokens", "total_output_tokens"):
        assert key in metrics, f"{key} not emitted at all; got {sorted(metrics)}"
    assert metrics["total_input_tokens"] == doc["tokenUsage"]["inputTokens"]
    assert metrics["total_output_tokens"] == doc["tokenUsage"]["outputTokens"]
    assert isinstance(metrics["total_input_tokens"], int)
    assert metrics["total_input_tokens"] > 0, (
        "control specimen reports zero input tokens; it cannot distinguish a "
        "measured 0 from an absent measurement")


def test_absent_measurement_emits_null_not_zero() -> None:
    for jsonl, settings in _one_per_rung():
        doc = json.loads(settings.read_text())
        doc.pop("tokenUsage")
        metrics = _emit_metrics(jsonl, doc)
        rung = jsonl.parents[1].name
        for key in ("total_input_tokens", "total_output_tokens"):
            assert key in metrics, (
                f"{rung}: {key} missing entirely — the mapping was dropped, not "
                f"nulled; a consumer cannot tell the field from an unsupported one")
            assert metrics[key] is None, (
                f"{rung}: {key} == {metrics[key]!r} for a session whose sidecar "
                f"reports no tokenUsage. An absent measurement must be null; 0 "
                f"reads as a measured zero and silently under-counts any "
                f"cross-adapter sum.")


def test_partial_measurement_nulls_only_the_missing_counter() -> None:
    jsonl, settings = _one_per_rung()[0]
    doc = json.loads(settings.read_text())
    doc["tokenUsage"] = {"inputTokens": doc["tokenUsage"]["inputTokens"]}
    metrics = _emit_metrics(jsonl, doc)
    for key in ("total_input_tokens", "total_output_tokens"):
        assert key in metrics, f"{key} not emitted at all; got {sorted(metrics)}"
    assert metrics["total_input_tokens"] == doc["tokenUsage"]["inputTokens"]
    assert metrics["total_output_tokens"] is None, (
        f"outputTokens absent from tokenUsage but emitted as "
        f"{metrics['total_output_tokens']!r}")


# ---------------------------------------------------------------------------
# Derivation arm — the shapes no frozen sidecar happens to contain

def test_derive_returns_none_pair_when_token_usage_absent_or_malformed() -> None:
    for settings in ({}, {"tokenUsage": None}, {"tokenUsage": []},
                     {"tokenUsage": "12"}, {"tokenUsage": 0}):
        assert derive_token_totals(settings) == (None, None), (
            f"expected (None, None) for {settings!r}")


def test_derive_nulls_each_counter_independently() -> None:
    assert derive_token_totals({"tokenUsage": {"inputTokens": 7}}) == (7, None)
    assert derive_token_totals({"tokenUsage": {"outputTokens": 9}}) == (None, 9)
    assert derive_token_totals({"tokenUsage": {}}) == (None, None)


def test_derive_preserves_a_genuine_zero() -> None:
    """The converse of the branch under test: 0 that WAS measured stays 0."""
    assert derive_token_totals(
        {"tokenUsage": {"inputTokens": 0, "outputTokens": 0}}) == (0, 0)


def test_derive_rejects_bool_as_a_count() -> None:
    # bool is an int subclass; True must not become a token count of 1.
    assert derive_token_totals(
        {"tokenUsage": {"inputTokens": True, "outputTokens": False}}) == (None, None)


def test_derive_rejects_non_integer_counts() -> None:
    assert derive_token_totals(
        {"tokenUsage": {"inputTokens": 1.5, "outputTokens": "240"}}) == (None, None)


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    tests = [obj for name, obj in inspect.getmembers(mod)
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
