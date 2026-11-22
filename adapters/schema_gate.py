"""Schema gate for the regression rungs (adapters/*/regression/*/verify.py).

A rung asserts that the adapter's output over a frozen input still matches its
freeze: trace_count, content_hash, unmapped_path_count. That is integrity only.
A trace can reproduce its content_hash exactly and still be schema-invalid, and
before this gate every rung was green over such traces (millisecond timestamps,
integer scope.target_id): the freeze had frozen the defect.

schema_errors() validates emitted traces against the base schema plus their
trace_type schema (spec §13) through schemas/validate.py, the same validator
verify_run.py's schema gate uses. A schema error is not drift and is not
re-freezable: the fix is in the adapter, and the re-freeze follows the fix.

A validator that cannot be loaded (typically jsonschema not installed) is
reported as an error rather than skipped, for the reason verify_run.py gives:
a skipped gate restores a green that does not mean what a reader takes it to
mean. Run rungs with:
    uv run --with pyyaml --with rfc8785 --with jsonschema python verify.py
"""
from __future__ import annotations

import importlib.util
import pathlib

_VALIDATE = pathlib.Path(__file__).resolve().parent.parent / "schemas" / "validate.py"
_mod = None


def _validator():
    global _mod
    if _mod is None:
        spec = importlib.util.spec_from_file_location("minitrace_schema_validate", _VALIDATE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mod = mod
    return _mod


def schema_errors(traces) -> list[str]:
    """Return schema errors over one trace or a list of traces; [] means valid."""
    try:
        v = _validator()
    except Exception as e:  # noqa: BLE001 — any load failure is a failed gate
        return [f"schema validator unavailable ({e.__class__.__name__}: {e})"]
    errors: list[str] = []
    for i, t in enumerate(traces if isinstance(traces, list) else [traces]):
        errs, _warnings = v.validate_doc(t)
        errors.extend(f"trace-{i}: {e}" for e in errs)
    return errors
