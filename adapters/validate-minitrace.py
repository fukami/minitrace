#!/usr/bin/env python3
"""
minitrace session validator — checks minitrace JSON files against v0.4 schema requirements.

Usage:
    python3 validate-minitrace.py session.minitrace.json
    python3 validate-minitrace.py --dir ./data/sessions/active/2026-03/
    python3 validate-minitrace.py --dir ./data/sessions/ --recursive
    python3 validate-minitrace.py --strict session.minitrace.json  # fail on warnings too

Validates:
  - Required fields present (per profile: controlled vs organic)
  - Field types correct
  - Enum values valid
  - Metrics internally consistent (tool_call_count matches len(tool_calls))
  - Null-handling rules (read_ratio null when tool_call_count == 0)
  - Schema version matches expected
"""

import argparse
import json
import sys
from pathlib import Path


EXPECTED_SCHEMA = "minitrace-v0.1.0"

VALID_PROFILES = {"controlled", "organic"}
VALID_CLASSIFICATIONS = {"public", "internal", "confidential", "customer-confidential"}
VALID_OPERATION_TYPES = {"READ", "MODIFY", "NEW", "EXECUTE", "DELEGATE", "OTHER"}
VALID_ROLES = {"user", "assistant", "system"}
VALID_SOURCES = {"human", "framework", "model", None}
VALID_PRIVACY_LEVELS = {"full", "anonymous", "minimal"}
VALID_HUMAN_ATTENTION = {"focused", "divided", "unknown"}
VALID_SCOPE_TYPES = {"session", "turn", "tool_call", "handover"}
VALID_ANNOTATION_CATEGORIES = {"observation", "pattern", "ai-failure", "recommendation",
                                "external-review"}  # external-review for egress annotations


class ValidationResult:
    def __init__(self, file_path):
        self.file_path = file_path
        self.errors = []
        self.warnings = []

    def error(self, path, msg):
        self.errors.append(f"  ERROR {path}: {msg}")

    def warn(self, path, msg):
        self.warnings.append(f"  WARN  {path}: {msg}")

    @property
    def valid(self):
        return len(self.errors) == 0

    def summary(self):
        status = "PASS" if self.valid else "FAIL"
        return f"{status} {self.file_path} ({len(self.errors)} errors, {len(self.warnings)} warnings)"


def check_type(result, path, value, expected_type, nullable=True):
    """Check value is of expected type (or null if nullable)."""
    if value is None:
        if not nullable:
            result.error(path, f"required field is null")
        return
    if not isinstance(value, expected_type):
        result.error(path, f"expected {expected_type.__name__}, got {type(value).__name__}")


def check_enum(result, path, value, valid_values, nullable=True):
    """Check value is in set of valid values."""
    if value is None:
        if not nullable:
            result.error(path, f"required field is null")
        return
    if value not in valid_values:
        result.error(path, f"invalid value '{value}', expected one of {valid_values}")


def check_required(result, path, obj, field):
    """Check that a required field exists in object."""
    if not isinstance(obj, dict):
        result.error(path, f"expected object, got {type(obj).__name__}")
        return False
    if field not in obj:
        result.error(f"{path}.{field}", "required field missing")
        return False
    return True


def validate_tool_call(result, tc, idx, session_profile):
    """Validate a single tool call."""
    prefix = f"tool_calls[{idx}]"

    check_type(result, f"{prefix}.id", tc.get("id"), str, nullable=False)
    check_type(result, f"{prefix}.turn_index", tc.get("turn_index"), int)
    check_type(result, f"{prefix}.timestamp", tc.get("timestamp"), str)
    check_type(result, f"{prefix}.tool_name", tc.get("tool_name"), str, nullable=False)
    check_enum(result, f"{prefix}.operation_type", tc.get("operation_type"),
               VALID_OPERATION_TYPES, nullable=False)

    # Input block
    inp = tc.get("input")
    if inp is not None:
        check_type(result, f"{prefix}.input", inp, dict)

    # Output block
    out = tc.get("output")
    if out is not None:
        check_type(result, f"{prefix}.output", out, dict)
        if isinstance(out, dict):
            check_type(result, f"{prefix}.output.success", out.get("success"), bool)
            check_type(result, f"{prefix}.output.truncated", out.get("truncated"), bool)
            # full_hash format check
            fh = out.get("full_hash")
            if fh is not None and isinstance(fh, str):
                if not fh.startswith("sha256:"):
                    result.warn(f"{prefix}.output.full_hash",
                                f"expected 'sha256:<hex>', got '{fh[:30]}'")

    # Context block
    ctx = tc.get("context")
    if ctx is not None:
        check_type(result, f"{prefix}.context", ctx, dict)
        if isinstance(ctx, dict):
            pos = ctx.get("position_in_session")
            if pos is not None:
                if not isinstance(pos, (int, float)):
                    result.error(f"{prefix}.context.position_in_session",
                                 f"expected float, got {type(pos).__name__}")
                elif not (0.0 <= pos <= 1.0):
                    result.warn(f"{prefix}.context.position_in_session",
                                f"value {pos} outside expected range [0.0, 1.0]")

    # Spawned agent
    sa = tc.get("spawned_agent")
    if sa is not None:
        check_type(result, f"{prefix}.spawned_agent", sa, dict)
        if isinstance(sa, dict):
            check_type(result, f"{prefix}.spawned_agent.agent_type",
                       sa.get("agent_type"), str, nullable=False)


def validate_turn(result, turn, idx):
    """Validate a single turn."""
    prefix = f"turns[{idx}]"

    check_type(result, f"{prefix}.index", turn.get("index"), int, nullable=False)
    check_type(result, f"{prefix}.timestamp", turn.get("timestamp"), str)
    check_enum(result, f"{prefix}.role", turn.get("role"), VALID_ROLES, nullable=False)
    check_enum(result, f"{prefix}.source", turn.get("source"), VALID_SOURCES)
    check_type(result, f"{prefix}.content", turn.get("content"), str)

    tcit = turn.get("tool_calls_in_turn")
    if tcit is not None:
        check_type(result, f"{prefix}.tool_calls_in_turn", tcit, list)


def validate_annotation(result, ann, idx):
    """Validate a single annotation."""
    prefix = f"annotations[{idx}]"

    check_type(result, f"{prefix}.id", ann.get("id"), str, nullable=False)
    check_type(result, f"{prefix}.annotator", ann.get("annotator"), str, nullable=False)

    scope = ann.get("scope")
    if scope is not None:
        check_type(result, f"{prefix}.scope", scope, dict)
        if isinstance(scope, dict):
            check_enum(result, f"{prefix}.scope.type", scope.get("type"),
                       VALID_SCOPE_TYPES, nullable=False)

    content = ann.get("content")
    if content is not None:
        check_type(result, f"{prefix}.content", content, dict)


def validate_session(data, file_path):
    """Validate a minitrace session dict. Returns ValidationResult."""
    result = ValidationResult(file_path)

    # Top-level required fields
    for field in ["id", "schema_version", "profile"]:
        check_required(result, "session", data, field)

    # Schema version
    sv = data.get("schema_version")
    if sv and sv != EXPECTED_SCHEMA:
        result.error("schema_version", f"expected '{EXPECTED_SCHEMA}', got '{sv}'")

    # Profile
    profile = data.get("profile")
    check_enum(result, "profile", profile, VALID_PROFILES, nullable=False)

    # Classification
    check_enum(result, "classification", data.get("classification"),
               VALID_CLASSIFICATIONS)

    # Provenance
    prov = data.get("provenance")
    if prov is None:
        result.error("provenance", "required block missing")
    elif isinstance(prov, dict):
        for f in ["source_format", "converted_at", "converter_version"]:
            check_required(result, "provenance", prov, f)

    # Flags
    flags = data.get("flags")
    if flags is None:
        result.error("flags", "required block missing")
    elif isinstance(flags, dict):
        for f in ["for_research", "needs_cleaning", "contains_error", "contains_pii"]:
            if check_required(result, "flags", flags, f):
                check_type(result, f"flags.{f}", flags[f], bool, nullable=False)

    # Environment
    env = data.get("environment")
    if env is None:
        result.error("environment", "required block missing")
    elif isinstance(env, dict):
        check_required(result, "environment", env, "model")

    # Timing
    timing = data.get("timing")
    if timing is None:
        result.error("timing", "required block missing")
    elif isinstance(timing, dict):
        check_enum(result, "timing.privacy_level", timing.get("privacy_level"),
                   VALID_PRIVACY_LEVELS)
        # duration_seconds required
        ds = timing.get("duration_seconds")
        if ds is None:
            result.warn("timing.duration_seconds", "null (required by spec)")
        elif not isinstance(ds, (int, float)):
            result.error("timing.duration_seconds",
                         f"expected number, got {type(ds).__name__}")

    # Coordination
    coord = data.get("coordination")
    if coord is not None and isinstance(coord, dict):
        check_enum(result, "coordination.human_attention",
                   coord.get("human_attention"), VALID_HUMAN_ATTENTION)

    # Turns
    turns = data.get("turns")
    if turns is None:
        result.error("turns", "required field missing")
    elif not isinstance(turns, list):
        result.error("turns", f"expected list, got {type(turns).__name__}")
    else:
        for i, turn in enumerate(turns):
            validate_turn(result, turn, i)

    # Tool calls
    tool_calls = data.get("tool_calls")
    if tool_calls is None:
        result.error("tool_calls", "required field missing")
    elif not isinstance(tool_calls, list):
        result.error("tool_calls", f"expected list, got {type(tool_calls).__name__}")
    else:
        for i, tc in enumerate(tool_calls):
            validate_tool_call(result, tc, i, profile)

    # Annotations
    annotations = data.get("annotations")
    if annotations is not None and isinstance(annotations, list):
        for i, ann in enumerate(annotations):
            validate_annotation(result, ann, i)

    # Metrics
    metrics = data.get("metrics")
    if metrics is None:
        result.error("metrics", "required block missing")
    elif isinstance(metrics, dict):
        # Required metrics
        for f in ["turn_count", "tool_call_count"]:
            check_required(result, "metrics", metrics, f)

        # Internal consistency checks
        if isinstance(turns, list) and metrics.get("turn_count") is not None:
            if metrics["turn_count"] != len(turns):
                result.error("metrics.turn_count",
                             f"value {metrics['turn_count']} != len(turns) {len(turns)}")

        if isinstance(tool_calls, list) and metrics.get("tool_call_count") is not None:
            if metrics["tool_call_count"] != len(tool_calls):
                result.error("metrics.tool_call_count",
                             f"value {metrics['tool_call_count']} != len(tool_calls) {len(tool_calls)}")

        # read_ratio must be null when tool_call_count == 0
        tc_count = metrics.get("tool_call_count", 0)
        rr = metrics.get("read_ratio")
        if tc_count == 0 and rr is not None:
            result.error("metrics.read_ratio",
                         "must be null when tool_call_count == 0")

        # time_to_first_action must be null when tool_call_count == 0
        ttfa = metrics.get("time_to_first_action")
        if tc_count == 0 and ttfa is not None:
            result.error("metrics.time_to_first_action",
                         "must be null when tool_call_count == 0")

        # Operation count consistency
        op_sum = sum(metrics.get(f, 0) or 0 for f in [
            "read_count", "modify_count", "create_count",
            "execute_count", "delegate_count"
        ])
        other_count = tc_count - op_sum
        if other_count < 0:
            result.error("metrics",
                         f"operation type counts ({op_sum}) exceed tool_call_count ({tc_count})")

    # Controlled profile extra requirements
    if profile == "controlled":
        if data.get("scenario_id") is None:
            result.error("scenario_id", "required for controlled profile")
        if data.get("condition") is None:
            result.error("condition", "required for controlled profile")
        if data.get("outcome") is None:
            result.error("outcome", "required for controlled profile")

    return result


def validate_file(file_path):
    """Load and validate a single minitrace JSON file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        result = ValidationResult(str(file_path))
        result.error("file", f"invalid JSON: {e}")
        return result
    except FileNotFoundError:
        result = ValidationResult(str(file_path))
        result.error("file", "file not found")
        return result

    return validate_session(data, str(file_path))


def main():
    p = argparse.ArgumentParser(description="minitrace v0.4 session validator")
    p.add_argument("files", nargs="*", help="minitrace JSON files to validate")
    p.add_argument("--dir", help="Directory containing minitrace files")
    p.add_argument("--recursive", action="store_true",
                   help="Search directory recursively")
    p.add_argument("--strict", action="store_true",
                   help="Treat warnings as errors")
    p.add_argument("--quiet", action="store_true",
                   help="Only show summary, not individual errors")
    args = p.parse_args()

    files = []
    if args.files:
        files.extend(args.files)
    if args.dir:
        d = Path(args.dir)
        pattern = "**/*.minitrace.json" if args.recursive else "*.minitrace.json"
        files.extend(str(f) for f in d.glob(pattern))

    if not files:
        print("No files to validate. Use positional args or --dir.", file=sys.stderr)
        sys.exit(1)

    total = len(files)
    passed = 0
    failed = 0

    for file_path in sorted(files):
        result = validate_file(file_path)

        if args.strict and result.warnings:
            # In strict mode, warnings are treated as errors
            result.errors.extend(result.warnings)
            result.warnings = []

        if result.valid:
            passed += 1
        else:
            failed += 1

        if not args.quiet or not result.valid:
            print(result.summary())
            if not args.quiet:
                for err in result.errors:
                    print(err)
                for warn in result.warnings:
                    print(warn)

    print(f"\n--- Validation Summary ---")
    print(f"Files: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
