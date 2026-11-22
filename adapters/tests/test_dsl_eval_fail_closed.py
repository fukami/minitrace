"""An expression that parses but cannot be evaluated must not be emitted as
its own source text.

Before this guard, evaluate() swallowed every EvalError into the string-
literal fallback meant for hyphenated and slash-separated literals. A DSL
typo such as `coalesce($block.id, 'x' + str(1))` (str is not a DSL builtin)
then wrote the expression text into the trace as tool ids, silently. An
expression that carries a `$` reference or a call is never a literal, so it
fails closed; plain literals keep the fallback.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dsl_runner import ExpressionEvalError, evaluate  # noqa: E402

CTX = {"block": {"id": None, "n": 2}, "record": {"a": 1, "s": "abc"}}

BROKEN = [
    "coalesce($block.id, 'x' + str(1))",   # the 2026-09-26 case: str is not a builtin
    "str($record.a)",
    "undefined_fn($record.a)",              # unknown function with a $ reference
    "$record.a * 2",                        # unsupported operator on a reference
    "$record.a ** 2",
    "$record.a <= 2",                       # unsupported comparator
    "tu-${record.a * 2}",                   # inside a template
    "${undefined_fn(record.a)}",
]


@pytest.mark.parametrize("expr", BROKEN)
def test_broken_expression_fails_closed(expr):
    with pytest.raises(ExpressionEvalError):
        evaluate(expr, CTX)


@pytest.mark.parametrize("expr", [
    "synthetic-test-judge",
    "atbench/codex/attack-vector/injected-description",
    "read-only",
    "EXECUTE",
    "a/b",
    "cost (USD)",        # a space before "(": no call marker (rc3 §8.3)
])
def test_plain_literals_keep_the_fallback(expr):
    assert evaluate(expr, CTX) == expr


@pytest.mark.parametrize("expr", ["undefined_fn(1)", "x.total_seconds()", "st_block_text($record.a)"])
def test_any_identifier_followed_by_a_parenthesis_is_an_expression(expr):
    # rc3 §8.3 call marker: such a value is an expression, and an unknown
    # function or a method is structural, never used as written.
    with pytest.raises(ExpressionEvalError):
        evaluate(expr, CTX)


@pytest.mark.parametrize("expr,expected", [
    ("coalesce($block.id, 'x')", "x"),
    ("$record.a + 1", 2),
    ("tu-${record.a}", "tu-1"),
    ("len($record.s)", 3),
])
def test_valid_expressions_unchanged(expr, expected):
    assert evaluate(expr, CTX) == expected


# The claude-ai finalize expression after its rc3 §9.1 rewrite.
DURATION = "duration_seconds($container.created_at, max($state.all_timestamps))"


def test_string_concatenation_is_an_input_failure():
    # rc3 §8.3 defines + for numbers only; claude-ai's one '...' + n site now
    # uses a template. Null, never the source text.
    assert evaluate("'tu-' + $record.s", CTX) is None


def test_duration_on_missing_data_is_none():
    # A failing duration expression once wrote its own source text as the
    # value; it must be None, never the text and never an abort.
    ctx = {"state": {"all_timestamps": []}, "container": {}}
    assert evaluate(DURATION, ctx) is None
    # A wrong-shaped timestamp is an input failure: null, not the text.
    bad = {"state": {"all_timestamps": ["garbage"]}, "container": {"created_at": "2026-01-01T00:00:00Z"}}
    assert evaluate(DURATION, bad) is None


def test_duration_with_data():
    ctx = {"state": {"all_timestamps": ["2026-01-01T00:01:00Z"]},
           "container": {"created_at": "2026-01-01T00:00:00Z"}}
    assert evaluate(DURATION, ctx) == 60.0


def _eval_counting_failures(expr, ctx):
    import dsl_runner as D
    sink = []
    tok = D._EVAL_DIAGNOSTICS.set(sink)
    try:
        return evaluate(expr, ctx), len(sink)
    finally:
        D._EVAL_DIAGNOSTICS.reset(tok)


@pytest.mark.parametrize("expr,record,expected,failures", [
    # rc3 §8.3 Evaluation order and reach: coalesce stops at its first
    # non-null argument; the arguments after it are not evaluated, so they
    # cannot fail (ruled 2026-10-03).
    ("coalesce($record.a, $record.b + 1)", {"a": 5, "b": "x"}, 5, 0),
    ("coalesce($record.a, $record.b + 1, $record.b + 2)", {"a": 5, "b": "x"}, 5, 0),
    ("coalesce($record.z, $record.a, $record.b + 1)", {"a": 5, "b": "x"}, 5, 0),
    ("coalesce($record.a, iso_normalise($record.b))", {"a": 0, "b": "x"}, 0, 0),
    ("coalesce($record.a, iso_normalise($record.b))", {"a": False, "b": "x"}, False, 0),
    ("coalesce($record.a, iso_normalise($record.b))", {"a": "", "b": "x"}, "", 0),
    # Arguments up to the first non-null one are evaluated, left to right,
    # and a failure among them nulls the whole value.
    ("coalesce($record.b + 1, $record.a)", {"a": 5, "b": "x"}, None, 1),
    ("coalesce($record.z, $record.b + 1, $record.a)", {"a": 5, "b": "x"}, None, 1),
    ("coalesce($record.z, $record.y)", {}, None, 0),
    # Wherever the call stands: nested, in a ternary branch, in a template.
    ("coalesce($record.z, coalesce($record.a, $record.b + 1))", {"a": 5, "b": "x"}, 5, 0),
    ("$record.a ? coalesce($record.a, $record.b + 1) : 0", {"a": 5, "b": "x"}, 5, 0),
    ("v-${coalesce(record.a, record.b + 1)}", {"a": 5, "b": "x"}, "v-5", 0),
    ("v-${record.a ? coalesce(record.a, record.b + 1) : 0}", {"a": 5, "b": "x"}, "v-5", 0),
])
def test_coalesce_stops_at_the_first_non_null_argument(expr, record, expected, failures):
    value, failed = _eval_counting_failures(expr, {"record": record})
    assert value == expected and type(value) is type(expected)
    assert failed == failures


def test_coalesce_laziness_does_not_hide_a_structural_error():
    # A later argument is not evaluated, but its text is still checked at
    # load (dsl_expression_problems applies expression_problem to every
    # evaluated value), so the DSL is refused before any record is read.
    from dsl_runner import dsl_load_problems

    def dsl(value):
        return {"adapter": {"dsl_version": "0.1.0"}, "input": {"type": "json"},
                "session": {"static": {}, "derived": {"id": "x", "t": value}}, "emit": []}

    assert dsl_load_problems(dsl("coalesce($record.a, $record.b + 1)")) is None
    for bad, value in (("coalesce($record.a, undefined_fn($record.b))", 5),
                       ("v-${coalesce(record.a, undefined_fn(record.b))}", "v-5")):
        # The message echoes the expression, so match the reason, not the name.
        assert "does not define: undefined_fn" in (dsl_load_problems(dsl(bad)) or ""), bad
        assert evaluate(bad, {"record": {"a": 5}}) == value


@pytest.mark.parametrize("d,codes", [
    # rc3 §9.1 and §4 Objects in the input: ascending order of the keys as
    # sequences of code points, whatever order the input wrote them in.
    ({"b": 1, "a": 2}, ["a", "b"]),
    ({"a": 2, "b": 1}, ["a", "b"]),
    ({"a": 0, "B": 0, "_": 0}, ["B", "_", "a"]),
    ({"1.2": 0, "1.10": 0, "1.1": 0}, ["1.1", "1.10", "1.2"]),
    ({"ab": 0, "a": 0, "": 0}, ["", "a", "ab"]),
    # Code points, not UTF-16 code units: U+FF61 sorts before U+1F600,
    # though its UTF-16 unit is above the surrogate D83D.
    ({"\U0001F600": 0, "｡": 0}, ["｡", "\U0001F600"]),
])
def test_dict_items_orders_by_key_code_points(d, codes):
    value, failed = _eval_counting_failures("dict_items($record.d)", {"record": {"d": d}})
    assert [e["code"] for e in value] == codes and failed == 0
    assert all(e["value"] == d[e["code"]] for e in value)


@pytest.mark.parametrize("d", [None, [], ["a"], "ab", 5, True])
def test_dict_items_of_a_non_object_is_empty(d):
    assert _eval_counting_failures("dict_items($record.d)", {"record": {"d": d}}) == ([], 0)


@pytest.mark.parametrize("m", [{1: "a", "b": 2}, {2: "a", 1: "b"}, {"a": 1, None: 2}])
def test_dict_items_with_a_non_string_key_is_an_input_failure(m):
    # Unreachable from adapter text, which §2 refuses at load
    # (load_adapter_yaml); this guards callers that pass Python mappings.
    # Keys of one non-string type sort without error, so the check is not
    # left to sorted().
    value, failed = _eval_counting_failures("dict_items($constants.m)", {"constants": {"m": m}})
    assert value is None and failed == 1


@pytest.mark.parametrize("fn,n,expected", [
    # §9.1 (ruled 2026-10-20): the number, read as the decimal of its
    # RFC 8785 form (the shortest that round-trips), is rounded down to a
    # whole microsecond, toward the earlier instant, never to nearest.
    ("iso_from_ms", 0.0006, "1970-01-01T00:00:00.000000Z"),
    ("iso_from_ms", 0.0015, "1970-01-01T00:00:00.000001Z"),
    ("iso_from_ms", -0.0006, "1969-12-31T23:59:59.999999Z"),
    # The decimal as written, not the binary64 value on either side of it.
    ("iso_from_ms", 1062833537436.475, "2003-09-06T07:32:17.436475Z"),
    ("iso_from_ms", 1552075003930.233, "2019-03-08T19:56:43.930233Z"),
    ("iso_from_ms", -0.001, "1969-12-31T23:59:59.999999Z"),
    ("iso_from_seconds", 1700000000.123457, "2023-11-14T22:13:20.123457Z"),
    ("iso_from_seconds", 1623041909.93674, "2021-06-07T04:58:29.936740Z"),
    # Not after a float multiplication, which rounds these up.
    ("iso_from_ms", 1689371262123.7969, "2023-07-14T21:47:42.123796Z"),
    ("iso_from_seconds", 1710034924.6615689, "2024-03-10T01:42:04.661568Z"),
    ("iso_from_ms", 1, "1970-01-01T00:00:00.001000Z"),
    ("iso_from_ms", -62135596800000, "0001-01-01T00:00:00.000000Z"),
    ("iso_from_ms", 253402300799999, "9999-12-31T23:59:59.999000Z"),
    ("iso_from_seconds", 0.04391, "1970-01-01T00:00:00.043910Z"),
    ("iso_from_seconds", 970092832.97161, "2000-09-27T22:13:52.971610Z"),
    ("iso_from_seconds", -0.0000005, "1969-12-31T23:59:59.999999Z"),
    ("iso_from_seconds", 1, "1970-01-01T00:00:01.000000Z"),
    # The largest binary64 below 10000-01-01T00:00:00Z.
    ("iso_from_seconds", 253402300799.99997, "9999-12-31T23:59:59.999970Z"),
    ("iso_from_seconds", -62135596800.0, "0001-01-01T00:00:00.000000Z"),
])
def test_epoch_conversion_rounds_the_written_decimal_down(fn, n, expected):
    assert _eval_counting_failures(f"{fn}($record.n)", {"record": {"n": n}}) == (expected, 0)


@pytest.mark.parametrize("fn,n", [
    ("iso_from_ms", -62135596800000.01),     # just before 0001-01-01
    ("iso_from_ms", 253402300800000),        # 10000-01-01
    ("iso_from_seconds", 253402300800),
    ("iso_from_seconds", -62135596800.00001),    # 10 us before 0001-01-01
    ("iso_from_seconds", float("nan")),
    ("iso_from_ms", float("inf")),
    ("iso_from_ms", True),
    ("iso_from_seconds", "5"),
])
def test_epoch_conversion_outside_its_domain_is_an_input_failure(fn, n):
    assert _eval_counting_failures(f"{fn}($record.n)", {"record": {"n": n}}) == (None, 1)


def test_epoch_conversion_matches_a_decimal_reference():
    # Seeded differential against Decimal over the shortest round-trip
    # decimal. repr is the source of rfc8785's digits too, so this checks
    # the exponent form, the parse and the multiply-and-floor, not the
    # digits themselves: floor(value * 10^6 /
    # unit) microseconds, then the §3.5 form. Raw floats and values written
    # to a few decimals, across the whole 0001-9999 range.
    import datetime as dt
    import decimal
    import random
    from dsl_runner import fn_iso_from_ms, fn_iso_from_seconds
    rng = random.Random(762)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    ctx = decimal.Context(prec=60, rounding=decimal.ROUND_FLOOR)
    for _ in range(20000):
        # Within the years 0001-9999: -62135596800 s to 253402300799 s.
        for fn, per_second in ((fn_iso_from_ms, 1000), (fn_iso_from_seconds, 1)):
            s = rng.uniform(-62135596800, 253402300799) / 10 ** rng.choice((0, 0, 0, 3, 6, 9))
            n = s * per_second
            if rng.random() < 0.5:
                n = float(f"{n:.{rng.randint(0, 9)}f}")
            v = ctx.multiply(decimal.Decimal(repr(n)), 1_000_000)
            us = int(ctx.divide(v, per_second).to_integral_value(decimal.ROUND_FLOOR))
            d = epoch + dt.timedelta(microseconds=us)
            want = f"{d.year:04d}-{d:%m-%dT%H:%M:%S}.{d.microsecond:06d}Z"
            assert fn(n) == want, (fn.__name__, n)


@pytest.mark.parametrize("c,expected,failures", [
    # rc3 §9.1 first_block_text: the .text of the first type "text" block.
    ([{"type": "x", "text": "a"}, {"type": "text", "text": "b"}, {"type": "text", "text": "c"}], "b", 0),
    ([{"type": "text", "text": ""}], "", 0),
    # A matched block whose .text is null or absent gives null ...
    ([{"type": "text", "text": None}, {"type": "text", "text": "later"}], None, 0),
    ([{"type": "text"}], None, 0),
    # ... and one whose .text is present but not a string is an input failure.
    ([{"type": "text", "text": 5}], None, 1),
    ([{"type": "text", "text": False}], None, 1),
    ([{"type": "text", "text": 0}], None, 1),
    ([{"type": "text", "text": ["a"]}], None, 1),
    # Null, or a value that is not a list, gives null.
    (None, None, 0),
    (5, None, 0),
    (True, None, 0),
    ("text", None, 0),
    ({"type": "text", "text": "x"}, None, 0),
    ([], None, 0),
    (["text", 5, None], None, 0),
])
def test_first_block_text_domain(c, expected, failures):
    assert _eval_counting_failures("first_block_text($record.c)", {"record": {"c": c}}) == (expected, failures)
