"""Validate v0.3.0 worked-example traces against the JSON Schema files.

Per spec §13, a trace MUST validate against the base schema PLUS the
appropriate type-specific schema (selected by `trace_type`).
"""
from __future__ import annotations

import json
import pathlib
import stat
import sys

import jsonschema

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "adapters"))
from source_reader import trace_json_verdict  # noqa: E402

SCHEMAS_DIR = pathlib.Path(__file__).parent
# Try the version-pinned layout (source-of-truth) first; fall back to flat layout
# (review package). Both ship with this validator.
_EXAMPLES_CANDIDATES = [
    SCHEMAS_DIR.parent / "examples" / "v0.3.0",
    SCHEMAS_DIR.parent / "examples",
]
EXAMPLES_DIR = next((p for p in _EXAMPLES_CANDIDATES if p.is_dir()), _EXAMPLES_CANDIDATES[-1])

BASE_SCHEMA = json.loads((SCHEMAS_DIR / "minitrace-v0.3.0.base.schema.json").read_text())
TYPE_SCHEMAS = {
    "agent-session": json.loads(
        (SCHEMAS_DIR / "minitrace-v0.3.0.agent-session.schema.json").read_text()
    ),
    "judge-session": json.loads(
        (SCHEMAS_DIR / "minitrace-v0.3.0.judge-session.schema.json").read_text()
    ),
}


def validate_doc(trace: dict, max_errors: int | None = None) -> tuple[list[str], list[str]]:
    """Validate one already-parsed trace. Returns (errors, warnings).

    Split out from `validate` so a caller holding a trace in memory — an
    adapter driver checking what it just emitted — does not have to write it to
    a file first. `validate` is the file-reading wrapper over this.

    Per spec §3.2, an unknown trace_type with a schema_uri is valid-but-uninterpreted
    (warning); without a schema_uri it is invalid (error).

    max_errors stops the validators once that many errors are collected, so a
    caller that reports only a few does not pay for all of them.
    """
    if max_errors is not None and max_errors < 1:
        raise ValueError(f"max_errors must be at least 1, got {max_errors}")
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(trace, dict):
        # The base schema's top level is an object; any other is an error by
        # name, not a crash on .get (ruled 2026-10-21).
        kind = {list: "array", str: "string", bool: "boolean", int: "number",
                float: "number", type(None): "null"}.get(type(trace), type(trace).__name__)
        return [f"top level is not an object (a JSON {kind})"], []

    def full() -> bool:
        return max_errors is not None and len(errors) >= max_errors

    base_validator = jsonschema.Draft202012Validator(BASE_SCHEMA)
    for err in base_validator.iter_errors(trace):
        errors.append(f"BASE: {err.message} at {list(err.absolute_path)}")
        if full():
            return errors, warnings

    trace_type = trace.get("trace_type")
    # A list or object here is not a key of TYPE_SCHEMAS (TypeError stopped
    # a batch); like a number or null it is an unknown type, after the base
    # error for its type (ruled 2026-10-29).
    if isinstance(trace_type, str) and trace_type in TYPE_SCHEMAS:
        type_validator = jsonschema.Draft202012Validator(TYPE_SCHEMAS[trace_type])
        for err in type_validator.iter_errors(trace):
            errors.append(f"{trace_type.upper()}: {err.message} at {list(err.absolute_path)}")
            if full():
                return errors, warnings
    else:
        schema_uri = trace.get("schema_uri")
        if not schema_uri:
            errors.append(
                f"unknown trace_type {trace_type!r} requires schema_uri (spec §3.2)"
            )
        else:
            warnings.append(
                f"unknown trace_type {trace_type!r}; base-validated only; "
                f"schema_uri={schema_uri!r} not fetched in this implementation"
            )

    return errors, warnings


def validate(trace_path: pathlib.Path) -> tuple[list[str], list[str]]:
    """Validate one trace file. Thin wrapper over `validate_doc`."""
    # The text is read under the §3.4 rules the schema cannot express: I-JSON
    # (UTF-8, no duplicate member names, no unpaired surrogate or
    # noncharacter), the numeric domain and the depth limit. One reader,
    # shared with verify_envelope.py and the canonical vectors.
    # A FIFO or device would block the read; a path that cannot be examined
    # says why (ruled 2026-10-21).
    try:
        st = trace_path.stat()
    except OSError as e:
        return [f"{trace_path.name}: unreadable ({e.strerror or e.__class__.__name__})"], []
    if not stat.S_ISREG(st.st_mode):
        return [f"{trace_path.name}: not a regular file"], []
    doc, why = trace_json_verdict(trace_path.read_bytes())
    if why is not None:
        return [f"{trace_path.name}: not conformant (§3.4): {why}"], []
    return validate_doc(doc)


def collect(targets: list[str]) -> list[pathlib.Path]:
    """Resolve CLI targets to trace files.

    A directory contributes its *.minitrace.json recursively; a file is taken
    as given, whatever it is called, so a caller can validate a trace it just
    produced under a temp name. Dotfiles are skipped in both cases.
    """
    out: list[pathlib.Path] = []
    for t in targets:
        path = pathlib.Path(t)
        if path.is_dir():
            out.extend(
                q for q in path.rglob("*.minitrace.json")
                if not q.name.startswith(".")
            )
        else:
            out.append(path)
    return sorted(set(out))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # No arguments keeps the historical behaviour (the shipped worked-example
    # corpus), so existing callers and the docstring above stay true. With
    # arguments this validates ANY trace file or directory, which is what makes
    # the validator wirable into a gate — it previously had no way to be
    # pointed at a trace an adapter had just emitted, and that is a large part
    # of why it went ungated for so long.
    if argv:
        paths = collect(argv)
        if not paths:
            print(f"no traces found in {argv}", file=sys.stderr)
            return 1
    else:
        paths = sorted(
            p for p in EXAMPLES_DIR.glob("*.minitrace.json")
            if not p.name.startswith(".")
        )
        if not paths:
            print(f"no examples found in {EXAMPLES_DIR}", file=sys.stderr)
            return 1

    failed = 0
    warned = 0
    for p in paths:
        errs, warns = validate(p)
        if errs:
            failed += 1
            print(f"FAIL {p.name}")
            for e in errs:
                print(f"  - {e}")
        elif warns:
            warned += 1
            print(f"WARN {p.name}")
            for w in warns:
                print(f"  - {w}")
        else:
            print(f"OK   {p.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
