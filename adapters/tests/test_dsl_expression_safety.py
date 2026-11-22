"""The DSL expression evaluator must not reach Python's object model.

Before this guard, `''.__class__.__base__.__subclasses__()` evaluated to the
list of every loaded class, from which arbitrary code execution is a textbook
step: an adapter .dsl.yaml was code, not data. These tests pin the boundary:
dict key access, the BUILTINS functions and SAFE_ATTRIBUTES, nothing else.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import dsl_runner  # noqa: E402
from dsl_runner import UnsafeExpressionError, evaluate  # noqa: E402

# Expressions (DSL spec rc3 §8.3 raw-text classification): they are
# evaluated, so the boundary must hold.
ESCAPES = [
    "$x.__class__",
    "$x.__class__.__base__.__subclasses__()",
    "${''.__class__.__base__.__subclasses__()}",
    "tu-${x.__class__}",
    "$record.__class__",
    "len($record.__class__)",
    # An identifier followed by "(" marks an expression (rc3 §8.3 call
    # marker), so these are evaluated and refused, not used as written.
    "''.__class__.__base__.__subclasses__()",
    "x.__class__.__base__.__subclasses__()",
]

# Look-alikes with no $ reference, no call marker and no context-variable path
# are literals under rc3 §8.3: never evaluated, emitted exactly as written.
# Stronger than refusing them: no code path reaches the object model.
LITERAL_LOOKALIKES = [
    "''.__class__",
    "len.__globals__",
    "sha256.__globals__['hashlib']",
]


@pytest.mark.parametrize("text", LITERAL_LOOKALIKES)
def test_escape_lookalikes_are_literals(text):
    assert evaluate(text, {"x": "abc", "record": {"a": 1}}) == text


@pytest.mark.parametrize("expr", ESCAPES)
def test_escape_expressions_fail_closed(expr):
    # Must raise, not fall back to returning the expression as a string.
    with pytest.raises(UnsafeExpressionError):
        evaluate(expr, {"x": "abc", "record": {"a": 1}})


@pytest.mark.parametrize("expr", [
    "$x.upper()",          # bound method of str
    "$x.encode()",
    "$y.keys()",           # bound method of dict is never reachable: key lookup
])
def test_methods_on_data_are_not_callable(expr):
    ctx = {"x": "abc", "y": {"keys": 1}}
    # Never the method's result, and no longer the source text
    # either: a call to something that is not callable fails closed.
    with pytest.raises((UnsafeExpressionError, dsl_runner.ExpressionEvalError)):
        evaluate(expr, ctx)


def test_non_dict_attribute_yields_none_not_error():
    # Input decides a value's type: a field that is normally an object may
    # arrive as a string. That must not abort the conversion.
    assert evaluate("record.message.role", {"record": {"message": "text"}}) is None
    assert evaluate("record.message.count", {"record": {"message": "text"}}) is None


def test_underscore_dict_keys_still_resolve():
    # inspect-swe reads `$record._retry_attempt_index`: a key, not an attribute.
    assert evaluate("$record._retry_attempt_index", {"record": {"_retry_attempt_index": 2}}) == 2


def test_datetime_arithmetic_and_methods_are_structural():
    # rc3 §9.1: instants support no operator and no method; durations are
    # duration_seconds (claude-ai and pi were rewritten to it).
    ctx = {"record": {"a": "2026-01-01T00:00:00Z", "b": "2026-01-01T00:00:02.5Z"}}
    with pytest.raises(dsl_runner.ExpressionEvalError):
        evaluate("(parse_iso($record.b) - parse_iso($record.a)).total_seconds()", ctx)
    assert evaluate("duration_seconds($record.a, $record.b)", ctx) == 2.5


def test_builtins_are_callable():
    assert evaluate("len(x)", {"x": [1, 2, 3]}) == 3
    assert evaluate("coalesce(a, b)", {"a": None, "b": 7}) == 7


def test_keyword_arguments_rejected():
    with pytest.raises(UnsafeExpressionError):
        evaluate("len(obj=x)", {"x": [1]})


def test_input_text_is_not_evaluated():
    # A value from the input that looks like an expression or a template is
    # data: interpolation is single-pass and never evaluates substituted text.
    payload = "${''.__class__.__base__.__subclasses__()}"
    assert evaluate("pre-${record.text}", {"record": {"text": payload}}) == "pre-" + payload
    assert evaluate("$record.text", {"record": {"text": payload}}) == payload


def test_no_attribute_access_on_non_dicts():
    # rc3: the attribute whitelist is gone; a non-object intermediate is null
    # under safe navigation, whatever Python attribute the name would match.
    assert not hasattr(dsl_runner, "SAFE_ATTRIBUTES")
    ctx = {"record": {"n": 5, "s": "abc", "d": dt.timedelta(seconds=1)}}
    for expr in ("$record.n.real", "$record.s.upper", "$record.d.total_seconds"):
        assert evaluate(expr, ctx) is None, expr


@pytest.mark.parametrize("tid", [
    "../escaped", "/abs/path", "a/b", "a\\b", ".hidden", "x\x00y", "x\ny", "\x1b[2Jx",
])
def test_trace_filename_rejects_unsafe_ids(tid):
    # The trace id usually carries a session id from the input.
    with pytest.raises(ValueError):
        dsl_runner.trace_output_filename({"id": tid})


def test_trace_filename_accepts_shipped_id_shapes():
    for tid in ["claude-code-agent-3f2a-b1", "goose-20260925_1", "petri-target-ab12"]:
        assert dsl_runner.trace_output_filename({"id": tid}) == tid + ".minitrace.json"
    assert dsl_runner.trace_output_filename({}) == "trace.minitrace.json"


@pytest.mark.parametrize("expr,ctx", [
    ("iso_from_ms(t)", {"t": 1e30}),
    ("iso_from_seconds(t)", {"t": -1e300}),
    ("iso_normalise(t)", {"t": "garbage"}),
    ("iso_normalise(t)", {"t": 17}),
    ("duration_seconds(t, t)", {"t": "2026-10-04"}),
])
def test_time_builtins_yield_none_on_hostile_values(expr, ctx):
    # Session data reaches these directly; a bad value must not abort the run.
    assert evaluate(expr, ctx) is None


def test_adapter_yaml_with_aliases_is_rejected():
    bomb = "a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n"
    with pytest.raises(ValueError):
        dsl_runner.load_adapter_yaml(bomb)
    assert dsl_runner.load_adapter_yaml("a: [x, y]\n") == {"a": ["x", "y"]}


@pytest.mark.parametrize("tid", ["C:x", "CON", "nul.txt", "a" * 201, "x y", "é"])
def test_trace_filename_allowlist(tid):
    with pytest.raises(ValueError):
        dsl_runner.trace_output_filename({"id": tid})


def test_colliding_trace_ids_fail_before_writing():
    with pytest.raises(ValueError):
        dsl_runner.output_filenames([{"id": "same"}, {"id": "same"}])
    with pytest.raises(ValueError):
        dsl_runner.output_filenames([{"id": "Case"}, {"id": "case"}])
    assert dsl_runner.output_filenames([{"id": "a"}, {"id": "b"}]) == [
        "a.minitrace.json", "b.minitrace.json"]
