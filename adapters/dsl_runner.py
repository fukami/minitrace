"""minitrace adapter DSL interpreter v0.1.0 (reference implementation).

Reads a YAML adapter DSL file conforming to minitrace-adapter-dsl-spec v0.1.0
and applies it to a native-format input, producing a v0.3.0 minitrace trace.

Usage:
    python dsl_runner.py <adapter.dsl.yaml> <input.jsonl> [--output trace.minitrace.json]

This is a reference implementation for the pi pilot. It supports the subset
of the DSL needed to run pi/0.58.1.dsl.yaml end-to-end. It is intentionally
small: when the second pilot exposes new requirements, the interpreter grows
in step with the DSL spec.
"""
from __future__ import annotations

import argparse
import ast
import contextvars
import datetime as dt
import errno
import inspect
import hashlib
import json
import math
import os
import stat
import pathlib
import re
import secrets
import sys
import unicodedata
from fractions import Fraction
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Built-in functions exposed to DSL expressions

# The time builtins take values straight from session data. Per DSL spec rc3
# §9.1 a value of the wrong type or shape is an input failure: the builtin
# raises (TypeError/ValueError/OverflowError), evaluate() turns that into null
# plus a diagnostic (§8.3), and the conversion goes on.


def _epoch_number(n: Any, name: str) -> float:
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        raise TypeError(f"{name} takes a JSON number, got {type(n).__name__}")
    return float(n)


def _spec_ts(d: dt.datetime) -> str:
    """The minitrace-spec §3.5 form of a UTC datetime. The year is padded to
    four digits here, not by strftime: glibc's %Y writes year 999 as "999"
    and macOS's as "0999". One definition, timestamps.spec_ts,
    shared with every writer in adapters/."""
    from timestamps import spec_ts  # noqa: PLC0415
    return spec_ts(d)


def _from_epoch(n: Any, per_second: int, name: str) -> str:
    """§9.1: n, in units of 1/per_second s, read as the decimal of its
    RFC 8785 form (the shortest that round-trips, as written by a producer
    that wrote 1700000000.123457; not its binary64 value, which can lie on
    either side of it) and rounded down to a whole microsecond, toward the earlier instant
    (minitrace-spec §3.5 truncates); never to nearest, and never after a
    float multiplication (ruled 2026-10-20). Outside the years 0001-9999, a
    NaN, an infinity or an integer beyond +-(2^53-1) is an input failure
    (OverflowError or ValueError), as is a boolean or a string (TypeError)."""
    _epoch_number(n, name)
    import rfc8785  # noqa: PLC0415
    try:
        text = rfc8785.dumps(n).decode("ascii")
    except Exception as e:  # noqa: BLE001 - NaN, inf, out-of-range integer
        raise ValueError(f"{name}: number not representable: {e}") from None
    us = math.floor(Fraction(text) * 1_000_000 / per_second)
    return _spec_ts(dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=us))


def fn_iso_from_ms(ms: int | float | None) -> str | None:
    return None if ms is None else _from_epoch(ms, 1000, "iso_from_ms")


def fn_iso_from_seconds(s: int | float | None) -> str | None:
    return None if s is None else _from_epoch(s, 1, "iso_from_seconds")


def _iso_parse(s: Any) -> dt.datetime:
    """The one reading of a timestamp shared by iso_normalise and
    duration_seconds (§9.1): an offset-bearing ISO 8601 extended date-time
    with uppercase T and Z, a fraction of at least one digit truncated (not
    rounded) to six, a valid proleptic Gregorian date, hours 00-23, minutes
    and seconds 00-59, an offset below 24h with minutes 00-59 (-00:00 is UTC),
    and a UTC result in the years 0001-9999; a leap second (seconds 60 at
    23:59:60 UTC) reads as 23:59:59.999999 of that day (spec 62a1d701).
    Returns the UTC instant; raises TypeError or ValueError (input failures)
    for anything else. One parser, timestamps.parse_iso_utc, shared with the
    pre-processors' to_spec_utc (ruled 2026-10-29)."""
    from timestamps import parse_iso_utc  # noqa: PLC0415
    return parse_iso_utc(s)


def fn_iso_normalise(s: str | None) -> str | None:
    """§9.1: the minitrace-spec §3.5 form (YYYY-MM-DDTHH:MM:SS.ffffffZ, UTC)
    of a timestamp _iso_parse accepts; an input already in that form comes
    back unchanged (the §3.5 test of `<`/`>` and _spec_timestamp_problem rely
    on this); null gives null."""
    if s is None:
        return None
    return _spec_ts(_iso_parse(s))


def fn_duration_seconds(start: str | None, end: str | None) -> float | None:
    """Seconds from start to end (§9.1), each read exactly as iso_normalise
    reads it: the difference in whole microseconds divided by 10^6, rounded
    once to binary64 (int / int is correctly rounded in Python), so
    implementations agree to the last bit."""
    if start is None or end is None:
        return None
    delta = _iso_parse(end) - _iso_parse(start)
    return ((delta.days * 86400 + delta.seconds) * 10**6 + delta.microseconds) / 10**6


# DSL rc3 §12 (ruled 2026-10-22): one run's now_microseconds() can be fixed,
# by --now (a §3.5 timestamp) or SOURCE_DATE_EPOCH (whole seconds since the
# epoch, read by timestamps.source_date_epoch as source_reader reads it);
# --now wins. Set by run() for its own duration; module state, so two runs
# in one process at once (threads) would share it, which no caller does.
_NOW_PINNED: str | None = None


def pinned_now(now: str | None = None) -> str | None:
    """The fixed now_microseconds() value for a run: `now` when given, else
    SOURCE_DATE_EPOCH when set and not empty, else None (the clock). Raises
    ValueError for a `now` not exactly in the §3.5 form, or a
    SOURCE_DATE_EPOCH that is not a whole number of seconds a §3.5
    timestamp can hold: an unusable pin is refused, never replaced by the
    clock."""
    if now is not None:
        if (bad := _spec_timestamp_problem(now)):
            raise ValueError(f"--now {bad}")
        return now
    from timestamps import source_date_epoch  # noqa: PLC0415
    return source_date_epoch()


def fn_now_microseconds() -> str:
    if _NOW_PINNED is not None:
        return _NOW_PINNED
    d = dt.datetime.now(tz=dt.timezone.utc)
    return _spec_ts(d)


def fn_sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fn_text_blocks_join(content: list[dict] | None) -> str | None:
    """§9.1: the .text of every type "text" block, in order, joined by "\\n".
    Null, a value that is not a list (a number or true included), or no text
    block gives null; an absent .text contributes ""; a .text that is not a
    string, null included, is an input failure."""
    if not isinstance(content, list):
        return None
    parts = []
    for b in content:
        if not (isinstance(b, dict) and json_equal(b.get("type"), "text")):
            continue
        text = b.get("text", "")
        if not isinstance(text, str):
            raise TypeError(f"text_blocks_join: text is a {type(text).__name__}, not a string")
        parts.append(text)
    return "\n".join(parts) if parts else None


def fn_blocks_join_of_type(content: list[dict] | None, kind: str) -> str | None:
    """Join the payload of every block of `kind` (`text` or `thinking`), in order.

    Unlike first_block_text_of_type this keeps every block, for messages that
    carry several thinking or text blocks (claude.ai exports do)."""
    if not isinstance(content, list):
        return None
    payload_field = "thinking" if kind == "thinking" else "text"
    parts = []
    for b in content:
        if not (isinstance(b, dict) and json_equal(b.get("type"), kind)):
            continue
        payload = b.get(payload_field)
        if payload is None or payload == "":
            continue        # missing, null or empty: skipped, no separator (§9.1)
        if not isinstance(payload, str):
            # 0 and false included: an input failure, never skipped silently.
            raise TypeError(f"blocks_join_of_type: a {type(payload).__name__} payload is not a string")
        parts.append(payload)
    return "\n".join(parts) if parts else None


def fn_first_block_text(content: list[dict] | None) -> str | None:
    """§9.1: the .text of the first type "text" block. Null or a non-list
    gives null; a null or absent .text gives null; a .text that is not a
    string is an input failure."""
    if not isinstance(content, list):
        return None
    for b in content:
        if isinstance(b, dict) and json_equal(b.get("type"), "text"):
            text = b.get("text")
            if text is not None and not isinstance(text, str):
                raise TypeError(f"first_block_text: text is a {type(text).__name__}, not a string")
            return text
    return None


def fn_first_block_text_of_type(content: list[dict] | None, kind: str) -> str | None:
    """§9.1: the payload of the first block whose type matches kind (.thinking
    for thinking, else .text). Null or a non-list gives null; a null or absent
    payload gives null; a payload that is not a string is an input failure."""
    if not isinstance(content, list):
        return None
    payload_field = "thinking" if json_equal(kind, "thinking") else "text"
    for b in content:
        if isinstance(b, dict) and json_equal(b.get("type"), kind):
            payload = b.get(payload_field)
            if payload is not None and not isinstance(payload, str):
                raise TypeError(f"first_block_text_of_type: {payload_field} is a "
                                f"{type(payload).__name__}, not a string")
            return payload
    return None


def fn_len(x: Any) -> int:
    return 0 if x is None else len(x)


def _orderable(x: Any, fn: str) -> list:
    """§9.1 min/max domain: the non-null elements of a list, all numbers
    (never booleans) or all strings; anything else is an input failure
    (TypeError). Null gives [] (so the result is null)."""
    if x is None:
        return []
    if not isinstance(x, list):
        raise TypeError(f"{fn}() takes a list, got {type(x).__name__}")
    vals = [v for v in x if v is not None]
    if not (all(_numeric(v) for v in vals) or all(isinstance(v, str) for v in vals)):
        kinds = sorted({"boolean" if isinstance(v, bool) else type(v).__name__ for v in vals})
        raise TypeError(f"{fn}() orders only numbers or only strings, got {', '.join(kinds)}")
    return vals


def fn_min(x: list) -> Any:
    """§9.1: smallest number by value or string by code point, nulls ignored;
    null for null, an empty list or only nulls."""
    vals = _orderable(x, "min")
    return min(vals) if vals else None


def fn_max(x: list) -> Any:
    """§9.1: as min, the largest."""
    vals = _orderable(x, "max")
    return max(vals) if vals else None


def json_equal(a: Any, b: Any) -> bool:
    """DSL spec §8.3 equality, used by ==, !=, predicate value: matching
    (§8.1), count_where and the block type tests (§9.1): type-strict, so
    true != 1 and "1" != 1; numbers by value, so 1 == 1.0; objects equal
    with the same keys and equal values, lists with equal elements in order,
    by this same rule (Python's == made true == 1 and [true] == [1])."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _numeric(a) and _numeric(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def fn_count_where(items: list[dict], field: str, value: Any) -> int:
    """§9.1: elements whose `field` equals `value` by §8.3 equality; null
    gives 0, any other non-list is an input failure, elements that are not
    objects are not counted."""
    if items is None:
        return 0
    if not isinstance(items, list):
        raise TypeError(f"count_where() takes a list, got {type(items).__name__}")
    return sum(1 for it in items if isinstance(it, dict) and json_equal(it.get(field), value))


def fn_json_parse(s: Any) -> Any:
    """Parse a JSON-encoded string into a Python value.

    Idempotent: non-strings pass through unchanged (so a dict already-parsed
    upstream survives a json_parse call). Returns None on parse failure or
    None input — adapter authors can guard with truthiness. Parsed as the
    readers parse input (DSL spec §4): a number outside the numeric domain is
    kept as its source text, NaN and Infinity are not JSON, and an unpaired
    surrogate or noncharacter is replaced with U+FFFD and reported.
    """
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    from source_reader import nested_json_loads  # noqa: PLC0415
    refused, truncated, repaired, lost = object(), [], [], {}
    value = nested_json_loads(s, None, report_malformed=False, refused=refused,
                              truncated_out=truncated, repaired_out=repaired, lost_keys_out=lost)
    if value is refused:
        # §4: a value deeper than the read bound is an input failure (null,
        # reported), unlike malformed JSON; so is a NaN or Infinity token.
        raise ValueError("json_parse: value refused (nesting deeper than 512, or NaN/Infinity)")
    for pointer in truncated:
        # §4: within the read bound, each value too deep is replaced by null
        # and reported as an input failure; the rest of the value is kept.
        _report_truncation(f"json_parse value at {pointer}")
    for r in repaired:
        # §4 "Strings in the input": each repair, with its path.
        _report_replacement(f"json_parse value at {r['path'] or '/'}"
                            + (" (member name)" if r["member_name"] else ""),
                            f"held {r['count']} {r['reason']} character(s); replaced with U+FFFD")
    if lost.get("duplicates"):
        # §4 "Objects in the input": the last value wins, and the duplicate is
        # reported where it was found, as a depth replacement is.
        _report_replacement("json_parse value",
                            f"held {lost['duplicates']} duplicate key(s); the last value kept")
    for path in lost.get("collisions", ()):
        _report_replacement(f"json_parse value at {path or '/'}",
                            "collided with another member name after U+FFFD repair; the last value kept")
    return value


def fn_coalesce(*args: Any) -> Any:
    """Return the first non-None argument, or None if all are None.

    Useful when the same logical field appears under different names across
    record-shape variants (e.g. tool_used dicts with optional _server_url).
    """
    for a in args:
        if a is not None:
            return a
    return None


def fn_dict_items(d: Any) -> list:
    """Return a list of {code, value} dicts from a dict's items.

    Used by adapters that need to iterate over a dict's key-value pairs
    via foreach_block (e.g. MAST's mast_annotation flat block of 14 binary
    flags). Returns empty list on None or non-dict input.

    §9.1: entries in ascending order of the keys as sequences of code points
    (Python's str order), not in the order the input wrote them (§4). A key
    that is not a string cannot come from JSON input, and adapter text with
    one is refused at load (§2, load_adapter_yaml); a caller that passes
    Python mappings directly gets an input failure.
    """
    if not isinstance(d, dict):
        return []
    if not all(isinstance(k, str) for k in d):
        raise TypeError("dict_items() over a mapping with a key that is not a string")
    return [{"code": k, "value": d[k]} for k in sorted(d)]


def fn_prefix_token(s: Any) -> str | None:
    """Return the first whitespace-delimited token of a string, or None.

    Used to extract taxonomy codes from MAST human-dataset failure-mode
    strings of the form "1.4 Fail to elicit clarification\n\n...". Idempotent
    on None; non-strings pass through as None.
    """
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    return s.split(None, 1)[0]


BUILTINS = {
    "iso_from_ms": fn_iso_from_ms,
    "iso_from_seconds": fn_iso_from_seconds,
    "iso_normalise": fn_iso_normalise,
    "duration_seconds": fn_duration_seconds,
    "now_microseconds": fn_now_microseconds,
    "sha256": fn_sha256,
    "text_blocks_join": fn_text_blocks_join,
    "first_block_text": fn_first_block_text,
    "first_block_text_of_type": fn_first_block_text_of_type,
    "blocks_join_of_type": fn_blocks_join_of_type,
    "len": fn_len,
    "min": fn_min,
    "max": fn_max,
    "count_where": fn_count_where,
    "json_parse": fn_json_parse,
    "coalesce": fn_coalesce,
    "dict_items": fn_dict_items,
    "prefix_token": fn_prefix_token,
}


# ---------------------------------------------------------------------------
# Expression evaluator (whitelisted AST walker)

class EvalError(Exception):
    pass


_YAML_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


class _Core12Loader(yaml.SafeLoader):
    """YAML 1.2 core schema (DSL spec rc3 §2) on PyYAML's 1.1 parser.

    Plain scalars are typed by the core schema alone, so `no`/`on`/`off` stay
    strings and `1:30` and dates are strings; a scalar it reads as a number
    must be written as a JSON number (§2, so `0755`, `0o7`, `0x10` are
    refused), and `<<` is an
    ordinary key (1.2 has no merge). Duplicate and non-string keys are
    refused here; explicit tags, anchors and aliases in load_adapter_yaml."""

    yaml_implicit_resolvers: dict = {}

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, f"expected a mapping, found {node.id}", node.start_mark)
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"non-string key {key!r} (DSL spec §2)", key_node.start_mark)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"duplicate key {key!r} (DSL spec §2)", key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping

    def _json_number_form(self, node, v: str) -> None:
        """DSL rc3 §2 (spec 37b78f9d; ruled 2026-10-06): a plain
        scalar the core schema reads as a number is written as a JSON number
        with an optional leading `-` (0x10, 0o7, +1, .5, 1. and 007 are
        structural failures at load). .inf and .nan fail the §3.4 domain
        check instead, with the same outcome."""
        if not _YAML_JSON_NUMBER.match(v):
            raise yaml.constructor.ConstructorError(
                None, None, f"number {v!r} is not written as a JSON number (DSL spec §2)", node.start_mark)

    def construct_core_int(self, node):
        v = self.construct_scalar(node)
        self._json_number_form(node, v)   # so no 0o / 0x form reaches int()
        return int(v, 10)

    def construct_core_float(self, node):
        v = self.construct_scalar(node)
        if v.lstrip("+-").lower() == ".inf":
            return float("-inf") if v.startswith("-") else float("inf")
        if v.lower() == ".nan":
            return float("nan")
        self._json_number_form(node, v)
        return float(v)


for _tag, _rx, _first in (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    ("float", r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
              r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$", list("-+.0123456789")),
):
    _Core12Loader.add_implicit_resolver(f"tag:yaml.org,2002:{_tag}", re.compile(_rx), _first)
_Core12Loader.add_constructor("tag:yaml.org,2002:int", _Core12Loader.construct_core_int)
_Core12Loader.add_constructor("tag:yaml.org,2002:float", _Core12Loader.construct_core_float)
_Core12Loader.add_constructor("tag:yaml.org,2002:map", _Core12Loader.construct_mapping)


def load_adapter_yaml(text: str) -> Any:
    """Load an adapter DSL as YAML 1.2 core schema (DSL spec rc3 §2), refusing
    duplicate keys, non-string keys and explicit tags (structural failures),
    and anchors/aliases: aliases stay shared references after loading
    and are expanded later (state copies, walks), so a few hundred bytes of
    nested aliases cost exponential time and memory. No adapter needs them.
    A character YAML cannot hold (a raw U+FFFF, a control) is refused the
    same way, not raised as a reader traceback."""
    try:
        events = list(yaml.parse(text, Loader=_Core12Loader))
    except yaml.reader.ReaderError as e:
        raise ValueError(f"adapter YAML invalid: {e}") from None
    for event in events:
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise ValueError("adapter YAML must not use anchors or aliases")
        if getattr(event, "tag", None) is not None:
            raise ValueError(f"adapter YAML must not use explicit tags (DSL spec §2): {event.tag}")
    try:
        return yaml.load(text, Loader=_Core12Loader)  # noqa: S506 - SafeLoader subclass
    except yaml.constructor.ConstructorError as e:
        raise ValueError(f"adapter YAML invalid: {e.problem}") from None


class UnsafeExpressionError(Exception):
    """Structural failure (DSL spec rc3 §8.3): the expression reached outside
    the DSL's data model (a dunder attribute, a keyword argument). Aborts."""


class ExpressionEvalError(Exception):
    """Structural failure (DSL spec rc3 §8.3): an expression uses a function,
    operator or construct the spec does not define, does not parse, or names
    a function outside call position (`$len`). A defect in the adapter, so
    the conversion aborts; never a source-text fallback (§14.7)."""


class _InputFailure(Exception):
    """Input failure (§8.3): a well-formed expression met a value it cannot
    use. evaluate() turns it into null plus a diagnostic; never an abort."""


class InputUnreadable(ValueError):
    """DSL rc3 §15 Unreadable and empty input (spec 9c801194): the
    input cannot be read as a whole (not well-formed JSON, or in container
    mode a json root that is no array, or no file to read). The conversion
    ends with no trace; main() reports it in one line and exits with
    EXIT_INPUT_UNREADABLE. A ValueError, as before, for callers of run()."""


# Context variables (§8.4) and the expression test of §8.3, applied to the
# RAW text before any rewrite: a $-prefixed reference ($ then an identifier,
# so `$5` is not one), a §9.1 function name followed by "(", or a dotted path
# whose first segment is a context variable, with or without $.
CONTEXT_VARIABLES = ("record", "record_first", "block", "container", "container_first",
                     "container_index", "state", "adapter", "constants", "trace")
# DSL rc3 §8.3 (spec 281925d0; ruled 2026-10-06): a $-prefixed reference is
# a marker only where no Unicode letter or digit, `_` or `.` precedes the $,
# so `price a$b` and `a.$record` are literals.
_RX_DOLLAR_REF = re.compile(r"(?<![\w.])\$[A-Za-z_]")
# §8.3 call marker: any identifier directly followed by "(", unless a letter,
# digit or "_" precedes it (a preceding "." counts, so a method call is an
# expression too). Known function names only would leave `st_block_text(x)`
# or `x.total_seconds()` literals, used as written instead of rejected.
_RX_CALL = re.compile(r"(?<!\w)[A-Za-z_]\w*\(")
_RX_CTX_PATH = re.compile(r"(?<![\w.])\$?(?:" + "|".join(sorted(CONTEXT_VARIABLES, key=len, reverse=True))
                          + r")\.[A-Za-z_]")


def is_expression(text: str) -> bool:
    return bool(_RX_DOLLAR_REF.search(text) or _RX_CALL.search(text) or _RX_CTX_PATH.search(text))


# Diagnostics of the conversion in progress (set by _build_trace) and where
# evaluation currently is, so an input failure names record, field and
# expression (§8.3) without threading a parameter through every call.
_EVAL_DIAGNOSTICS: contextvars.ContextVar[list | None] = contextvars.ContextVar("_EVAL_DIAGNOSTICS", default=None)
_EVAL_SITE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("_EVAL_SITE", default=None)
# The expression or template being evaluated, for a §4 finding a function
# makes inside it (json_parse), as _report_input_failure names it.
_EVAL_EXPR: contextvars.ContextVar[str | None] = contextvars.ContextVar("_EVAL_EXPR", default=None)


# The input roots a session.derived expression can read, as whole
# names: `container_index` and `container_first` are not `container`.
_DERIVED_SOURCE = re.compile(r"(?<![\w.])(?:record_first|container_first|container_index|container)(?!\w)")


# The input position of the first kept container ($container_first), for
# reports (set by run(); 0 when no reader positions are known).
_FIRST_POSITION: contextvars.ContextVar[int] = contextvars.ContextVar("_FIRST_POSITION", default=0)


def _read_site(expr: Any, records: list, container: dict | None, container_index: int,
               bound: tuple = ("record_first", "container", "container_first", "container_index"),
               first_position: int = 0) -> dict:
    """The input a report from a session-level expression (session.derived,
    finalize.computed) names: each bound record or container root it reads
    (DSL rc3 §8.3, spec 5fc0192d; ruled 2026-10-06). $record_first is
    record 0, named with its container in container mode; $container and
    $container_index are the current container, $container_first container
    0 (container roots name nothing outside container mode); none read is
    "no input". One input sets record_index / container_index as in the
    record loop; several are listed in `inputs`. Only the roots in `bound`
    count: finalize.computed binds $container alone."""
    reads = {m.group(0) for m in _DERIVED_SOURCE.finditer(expr if isinstance(expr, str) else "")} & set(bound)
    cmode = container is not None
    found: list[tuple] = []
    if "record_first" in reads and records:
        found.append((first_position, container_index if cmode else None))   # its list position
    if cmode and reads & {"container", "container_index"}:
        found.append((None, container_index))
    if cmode and "container_first" in reads:
        found.append((None, _FIRST_POSITION.get()))
    found = list(dict.fromkeys(found))
    if len(found) == 1:
        return {"record_index": found[0][0], "container_index": found[0][1], "inputs": None}
    return {"record_index": None, "container_index": None,
            "inputs": [{"record_index": r, "container_index": c} for r, c in found] or None}


def _set_site(**kw: Any) -> None:
    site = _EVAL_SITE.get()
    if site is not None:
        site.update(kw)


def _site_place(site: dict) -> str:
    """Where an evaluated value came from, for a report (§4 names the
    record): "record N" (the trace's Nth record), "container N" for a
    container-level value in container mode, both when an expression
    read both, or "no input" for a value read from neither (§8.3). Several inputs
    (a session-level expression reading two roots) are joined with "; "."""
    def one(r: Any, c: Any) -> str:
        return ", ".join(p for p in (f"record {r}" if r is not None else "",
                                     f"container {c}" if c is not None else "") if p)
    if site.get("inputs"):
        return "; ".join(one(i["record_index"], i["container_index"]) for i in site["inputs"])
    return one(site.get("record_index"), site.get("container_index")) or "no input"


def _site_fields(site: dict) -> dict:
    out = {"record_index": site.get("record_index"), "field": site.get("field")}
    if site.get("container_index") is not None:
        out["container_index"] = site["container_index"]
    if site.get("inputs"):
        out["inputs"] = [dict(i) for i in site["inputs"]]
    return out


def _report_truncation(what: str) -> None:
    """One §4 depth replacement inside an evaluated value."""
    _report_replacement(what, "nested deeper than 64; replaced with null")


def _report_replacement(what: str, how: str) -> None:
    """One §4 replacement inside an evaluated value (a value too deep, a
    string repaired), reported like an input failure at the current record
    and field."""
    site = _EVAL_SITE.get() or {}
    expr = _EVAL_EXPR.get()
    entry = {"level": "warning", "kind": "input-failure", **_site_fields(site),
             **({"expression": expr[:200]} if expr is not None else {}),
             "message": f"{what[:240]} {how} ({_site_place(site)}, {site.get('field')!s})"}
    sink = _EVAL_DIAGNOSTICS.get()
    if sink is not None:
        sink.append(entry)
    else:
        sys.stderr.write(f"  [warning] {entry['message']}\n")


_NO_EXPRESSION = object()


def _input_failure(field: str, expr: Any, message: str, **extra: Any) -> dict:
    """An input-failure entry built where a runner rule (not the evaluator)
    finds the evaluated value unusable: like _report_input_failure it names
    the record (and container) from the current site, the field and the
    expression (DSL rc3 §8.3: record, field and expression).
    _NO_EXPRESSION when the adapter wrote none (an absent key), so the entry
    does not show one. The message ends with the place, as
    _report_input_failure's does, "(no input)" included (ruled
    2026-10-18: the place in every message)."""
    site = _EVAL_SITE.get() or {}
    shown = {} if expr is _NO_EXPRESSION else {
        "expression": (expr if isinstance(expr, str) else json.dumps(expr, default=str))[:200]}
    return {"level": "warning", "kind": "input-failure", **_site_fields(site), "field": field,
            **shown, **extra, "message": f"{message} ({_site_place(site)})"}


def _report_input_failure(expr: str, cause: BaseException) -> None:
    site = _EVAL_SITE.get() or {}
    entry = {"level": "warning", "kind": "input-failure", **_site_fields(site),
             "expression": expr[:200],
             "message": f"input failure in {site.get('field')!s} "
                        f"({_site_place(site)}): {type(cause).__name__}: {str(cause)[:120]}"}
    sink = _EVAL_DIAGNOSTICS.get()
    if sink is not None:
        sink.append(entry)
    else:
        sys.stderr.write(f"  [warning] {entry['message']}\n")


# Errors a builtin or an operator raises when the INPUT has the wrong type or
# shape. Anything else propagates.
_INPUT_ERRORS = (TypeError, ValueError, AttributeError, OverflowError, KeyError,
                 IndexError, ZeroDivisionError, OSError)


def _render_interpolated(value: Any) -> str:
    """§8.3 "Interpolated values": string as is; number in RFC 8785 form;
    boolean true/false; null as ""; anything else is an input failure."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        import rfc8785  # noqa: PLC0415
        try:
            return rfc8785.dumps(value).decode("ascii")
        except Exception as e:  # noqa: BLE001 - NaN, inf, out-of-range integer
            raise _InputFailure(f"number not representable: {e}") from None
    raise _InputFailure(f"cannot interpolate a {type(value).__name__}")


def _unusable_part(value: Any) -> bool:
    """§13.2 R4 and R5: a part of a trace id, or of a placeholder's trace-id
    portion, that is null, "", a boolean, an object or a list identifies no
    trace, whatever it would render as. A number is usable: container_index
    builds the ids of shipped shapes."""
    return value is None or value == "" or isinstance(value, (bool, dict, list))


def _last_top_level_colon(expr: str) -> int:
    """Index of the last ':' of a `trace:...` body outside any inner ${...},
    which separates the trace-id portion from the field (§13.1); the body's
    length when there is none, so the whole body is the trace-id portion."""
    depth, cut = 0, len(expr)
    for k, ch in enumerate(expr):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ":" and depth == 0 and k >= len("trace:"):
            cut = k
    return cut


_TEMPLATE_BODY_KEYWORDS = frozenset({"not", "true", "false", "null", "True", "False", "None"})
_RX_LEADING_IDENT = re.compile(r"[A-Za-z_]\w*")


def _template_body_expr(body: str) -> str:
    """The expression a ${...} body evaluates (§8.3): the optional `$` is
    supplied only when the body begins with an identifier that is not a
    keyword, so `${record.id}` is `$record.id`; a body that begins with
    not/true/false/null/True/False/None, a quote, a digit or anything else
    is evaluated as written (`${not record.ok}`, `${true}`). Leading spaces
    do not change the decision."""
    text = body.lstrip(_EXPR_END_WS)   # ruling B
    if text.startswith("$"):
        return text
    m = _RX_LEADING_IDENT.match(text)
    if m and m.group() not in _TEMPLATE_BODY_KEYWORDS:
        return "$" + text
    return text


def _interpolate_template(template: str, ctx: dict, id_parts: bool = False) -> str | None:
    """Replace ${expr} substitutions in `template` with their rendered values.

    An inner expression that fails as input makes the whole string null
    (§8.3); a structural failure propagates. Substituted text is never
    evaluated again. With id_parts (the trace `id` field, §13.2 R5), an
    unusable part (_unusable_part) makes the whole string null, so a missing
    or empty `record.id` does not turn `agent-${record.id}` into `agent-`.

    Cross-trace placeholder special form (§13.1): ${trace:<trace-id>:<field>}
    is not evaluated; inner ${...} in the trace-id portion are resolved under
    the same rule as an id (R4: an unusable part makes the placeholder null,
    so its edge is omitted), and the outer wrap is kept for adapters.cross_trace.
    """
    out: list[str] = []
    i = 0
    while i < len(template):
        if template[i:i+2] == "${":
            depth = 1
            j = i + 2
            while j < len(template) and depth > 0:
                if template[j] == "{":
                    depth += 1
                elif template[j] == "}":
                    depth -= 1
                if depth == 0:
                    break
                j += 1
            if depth == 0:
                expr = template[i + 2:j]
                if expr.startswith("trace:"):
                    # The unusable-part rule covers the trace-id portion only
                    # (R4): everything before the last ':' outside inner ${...}.
                    cut = _last_top_level_colon(expr)
                    id_part = _interpolate_template(expr[:cut], ctx, id_parts=True)
                    field_part = _interpolate_template(expr[cut:], ctx)
                    if id_part is None or field_part is None:
                        return None
                    out.append("${" + id_part + field_part + "}")
                    i = j + 1
                    continue
                inner = _template_body_expr(expr)
                expr_reset = _EVAL_EXPR.set(template)
                try:
                    value = _evaluate_expression(inner, ctx)
                    if id_parts and _unusable_part(value):
                        return None
                    out.append(_render_interpolated(value))
                except _InputFailure as e:
                    _report_input_failure(template, e)
                    return None
                finally:
                    _EVAL_EXPR.reset(expr_reset)
                i = j + 1
                continue
        out.append(template[i])
        i += 1
    return "".join(out)


def _quoted_spans(expr: str) -> list[tuple[int, int]]:
    """(start, end) of every single- or double-quoted string literal, with
    backslash escapes; an unterminated quote runs to the end."""
    spans, i, n = [], 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n and expr[j] != ch:
                j += 2 if expr[j] == "\\" else 1
            spans.append((i, min(j + 1, n)))
            i = j + 1
            continue
        i += 1
    return spans


def _outside_quotes(expr: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for a, b in _quoted_spans(expr):
        out.append((pos, a))
        pos = b
    out.append((pos, len(expr)))
    return out


def _rewrite_outside_quotes(expr: str, fn) -> str:
    parts, pos = [], 0
    for a, b in _quoted_spans(expr):
        parts.append(fn(expr[pos:a]))
        parts.append(expr[a:b])
        pos = b
    parts.append(fn(expr[pos:]))
    return "".join(parts)


# §8.3 (spec badea515, ruling B; ruled 2026-10-10): only these are removed at
# either end of an expression or a ${...} body. str.strip() also removed the
# characters below and U+001C-U+001F (str.isspace, not White_Space).
_EXPR_END_WS = " \t\n\r\f"
# Unicode White_Space (PropList) other than space, tab, line breaks and form
# feed: a structural failure wherever it stands outside a string constant.
_OTHER_WHITE_SPACE = frozenset(map(chr, (0x0B, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
                                         0x2028, 0x2029, 0x202F, 0x205F, 0x3000)))


def _tokenizer_syntax_problem(expr: str) -> str | None:
    """Ruled 2026-10-09: §8.3 defines neither a line
    continuation nor a comment, but Python's tokenizer joins lines at a
    backslash and drops a # to the end of the line. Either, outside a
    string constant, is a structural failure. So is a line break or a form
    feed inside the expression (ruled 2026-10-09: space and tab only):
    Python joined lines inside brackets and took \f as whitespace. At the
    ends they are stripped, as before parsing, so a YAML block's trailing
    newline is harmless. Under ruling B only those are stripped, and any
    other White_Space character outside a string constant is a structural
    failure: the ends were str.strip()ped, and the ternary rewrite stripped
    its branches, so `$record.a` followed by U+00A0, and U+00A0 after the `?`
    of `a ? b : c`, loaded."""
    expr = expr.strip(_EXPR_END_WS)
    for a, b in _outside_quotes(expr):
        for ch, what in (("\\", "a backslash"), ("#", "a #"), ("\n", "a line break"),
                         ("\r", "a line break"), ("\f", "a form feed")):
            if ch in expr[a:b]:
                return f"{what} outside a string constant (§8.3)"
        if (ws := next((c for c in expr[a:b] if c in _OTHER_WHITE_SPACE), None)):
            return f"whitespace U+{ord(ws):04X} outside a string constant (§8.3: space and tab only)"
    return None


def _dollar_names(expr: str) -> set[str]:
    """Identifiers written with a $ prefix, outside string literals."""
    names: set[str] = set()
    for a, b in _outside_quotes(expr):
        names.update(re.findall(r"\$([A-Za-z_]\w*)", expr[a:b]))
    return names


_SAFE_NAV = re.compile(r"\?\.(?=[A-Za-z_])")
# Not after an identifier character or `.` either, so x$record
# stays two operands (no parse) instead of becoming the identifier xrecord.
_DOLLAR_REF = re.compile(r"(?<![\w.])\$(?=[A-Za-z_])")


def _normalise(expr: str) -> str:
    """Strip cosmetic '$', rewrite safe-nav '?.' to '.', and rewrite C-style
    ternary 'a ? b : c' to Python's '(b) if (a) else (c)'. Applied only to
    values already classed as expressions (§8.3), never to literals.

    The legacy ternary is single-level (§8.3): its condition and branches
    hold no other legacy ternary. It may stand in any expression position
    (ruled 2026-10-22): each one spans its innermost enclosing bracket
    group, bounded by that group or a comma at the same depth, so
    `($a ? 1 : 2) + 1` and `f(x, $a ? 1 : 2)` read as written. When one span
    holds another `?`, or a `?` has no `:` in its span, nothing is
    rewritten and the expression fails to parse; nested conditionals use
    Python's native `if/else` syntax.
    """
    # Rewrites act only outside string literals: `'price $5'` and
    # `'what?.'` inside quotes are data, not syntax.
    # `?.` and `$` are rewritten only before an
    # identifier (§8.3: `?.name`; `$5` is no reference), so a stray `1?.5` or
    # `1$5` fails to parse instead of becoming the number 1.5 or 15.
    expr = _rewrite_outside_quotes(expr, lambda t: _SAFE_NAV.sub(".", t))
    spans = _ternary_spans(expr)
    for seg_start, q_idx, col_idx, end in reversed(spans or []):   # disjoint: right to left
        cond = expr[seg_start:q_idx].strip(_EXPR_END_WS)   # not str.strip (U+001C-U+001F)
        then_branch = expr[q_idx + 1:col_idx].strip(_EXPR_END_WS)
        else_branch = expr[col_idx + 1:end].strip(_EXPR_END_WS)
        expr = expr[:seg_start] + f"(({then_branch}) if ({cond}) else ({else_branch}))" + expr[end:]
    return _rewrite_outside_quotes(expr, lambda t: _DOLLAR_REF.sub("", t))


def _ternary_spans(expr: str) -> list[tuple[int, int, int, int]] | None:
    """(segment start, `?`, `:`, end) for each legacy ternary, in order: the
    segment starts after the innermost enclosing open bracket or a comma at
    the `?`'s depth, and ends at that group's close or the next such comma
    after the `:`. None when a `?` has no `:` in its span or a span holds
    another `?` (a nested legacy ternary, §8.3); [] when there is none."""
    outside = [False] * len(expr)
    for a, b in _outside_quotes(expr):
        outside[a:b] = [True] * (b - a)
    starts: list[int] = [0]       # start of the current segment, per depth
    found: list[tuple[int, int]] = []
    for i, ch in enumerate(expr):
        if not outside[i]:
            continue
        if ch in "([{":
            starts.append(i + 1)
        elif ch in ")]}":
            if len(starts) > 1:
                starts.pop()
        elif ch == ",":
            starts[-1] = i + 1
        elif ch == "?":
            found.append((starts[-1], i))
    spans = []
    for seg_start, q_idx in found:
        depth = 0
        col_idx = end = -1
        for i in range(q_idx + 1, len(expr)):
            if not outside[i]:
                continue
            ch = expr[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif depth == 0 and ch == ":" and col_idx < 0:
                col_idx = i
            elif depth == 0 and ch == ",":
                # The comma ends the span; one before the `:` leaves the
                # `?` without its `:`.
                end = i
                break
        if col_idx < 0:
            return None
        spans.append((seg_start, q_idx, col_idx, len(expr) if end < 0 else end))
    questions = [q for _, q in found]
    if any(s <= q < e and q != own for s, own, _, e in spans for q in questions):
        return None
    return spans


def _strip_templates(text: str) -> str | None:
    """`text` with every top-level ${...} span removed; None if a ${ is
    never closed."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            if depth:
                return None
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def template_problem(text: str) -> str | None:
    """DSL spec rc3 §8.3: a value that is an
    expression outside its ${...} parts is a structural failure, and so is
    an unclosed ${. None if the template is well formed."""
    rest = _strip_templates(text)
    if rest is None:
        return f"unclosed ${{ in {text[:80]!r}"
    if is_expression(rest):
        return f"expression outside its ${{...}} parts: {text[:80]!r}"
    return None


def evaluated_values(dsl: dict) -> list[tuple[str, str]]:
    """(position, value) for every string the runner evaluates, following
    _build_trace and _apply_actions: session(s).derived, finalize.computed,
    foreach_block, and the values of every action (emit_* recursively)."""
    out: list[tuple[str, str]] = []

    def leaves(pos: str, v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                leaves(f"{pos}.{k}", x)
        elif isinstance(v, list):
            for x in v:
                leaves(pos, x)
        elif isinstance(v, str):
            out.append((pos, v))

    sessions = [("session", dsl.get("session"))] + [
        (f"sessions.{n}", c) for n, c in (dsl.get("sessions") or {}).items()]
    for name, sess in sessions:
        if isinstance(sess, dict):
            leaves(f"{name}.derived", sess.get("derived") or {})
    leaves("finalize.computed", (dsl.get("finalize") or {}).get("computed") or {})

    def rules(rs: Any, where: str) -> None:
        for r in rs or []:
            if not isinstance(r, dict):
                continue
            if isinstance(r.get("foreach_block"), str):
                out.append((f"{where}.{r.get('name', '?')}.foreach_block", r["foreach_block"]))
            for a in r.get("do") or []:
                if isinstance(a, dict):
                    for kind, body in a.items():
                        if kind == "omit_if_none":
                            continue
                        leaves(f"{where}.{r.get('name', '?')}.{kind}", body)
            rules(r.get("on_block"), f"{where}.{r.get('name', '?')}.on_block")
    rules(dsl.get("emit"), "emit")
    return out


def _index_problem(index: ast.AST) -> str | None:
    """§8.3 path access: `[n]` takes a non-negative integer constant and
    `['name']` a string constant; any other index form (computed, negative,
    boolean, a slice) is structural, whatever the data, and so is an integer
    constant outside the §3.4 domain (DSL spec §2)."""
    if isinstance(index, ast.Constant) and isinstance(index.value, str):
        return None
    if isinstance(index, ast.Constant) and isinstance(index.value, int) \
            and not isinstance(index.value, bool) and index.value >= 0:
        if _constant_out_of_domain(index.value):
            return f"number constant {index.value!r} outside the §3.4 domain"
        return None
    return f"index form [{ast.unparse(index)}] is not part of the language ([n] or ['name'] only)"


def _static_problem(node: ast.AST) -> str | None:
    """The structural failure `_eval_node` would raise on `node` whatever the
    data, or None. Mirrors _eval_node node by node; it decides only what the
    text decides (§8.3), so data-dependent failures stay at evaluation."""
    if isinstance(node, ast.Constant):
        if _constant_out_of_domain(node.value):
            return f"number constant {node.value!r} outside the §3.4 domain"
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return None
        return f"constant of type {type(node.value).__name__} is not a JSON value"
    if isinstance(node, ast.Name):
        if node.id in BUILTINS:
            return f"function {node.id!r} outside call position"
        return None
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("__") and node.attr.endswith("__"):
            return f"attribute {node.attr!r} is not accessible"
        return _static_problem(node.value)
    if isinstance(node, ast.Subscript):
        return _index_problem(node.slice) or _static_problem(node.value)
    if isinstance(node, ast.Call):
        if node.keywords:
            return "keyword arguments are not supported"
        if not (isinstance(node.func, ast.Name) and node.func.id in BUILTINS):
            return f"call to a function §9.1 does not define: {ast.unparse(node.func)}"
        try:
            inspect.signature(BUILTINS[node.func.id]).bind(*node.args)
        except TypeError:
            return f"{node.func.id}() called with {len(node.args)} argument(s)"
        return next((p for a in node.args if (p := _static_problem(a))), None)
    if isinstance(node, ast.IfExp):
        return _static_problem(node.test) or _static_problem(node.body) or _static_problem(node.orelse)
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.Not, ast.USub)):
            return f"unsupported unary op: {ast.dump(node.op)}"
        return _static_problem(node.operand)
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub)):
            return f"unsupported binop: {ast.dump(node.op)}"
        return _static_problem(node.left) or _static_problem(node.right)
    if isinstance(node, ast.Compare):
        if len(node.ops) > 1:
            return "chained comparison is not part of the language"
        for op in node.ops:
            if not isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.Gt)):
                return f"unsupported comparator: {ast.dump(op)}"
        if (problem := _ordering_operand_problem(node)):
            return problem
        return _static_problem(node.left) or next(
            (p for c in node.comparators if (p := _static_problem(c))), None)
    return f"unsupported AST node: {type(node).__name__}"


# DSL rc3 §8.3: a number constant is an unsigned JSON number (RFC 8259 §6).
_JSON_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


# DSL rc3 §8.3: a string constant is one single- or double-quoted run with
# neither its own quote character nor a backslash (no escapes, no prefixes).
_DSL_STRING = re.compile(r"(?:'[^'\\]*'|\"[^\"\\]*\")\Z")


def _constant_form_problem(src: str, tree: ast.AST) -> str | None:
    """A constant written in a form §8.3 leaves out of the language,
    which Python's parser accepts as an ordinary constant: a number not in
    the JSON form (0x10, 0o7, 0b1, 1_000, .5, 1., 01.5), or a string with
    an escape, a prefix (r'', u''), triple quotes, or two adjacent strings
    that Python joins ('a' 'b'). A structural failure the text decides.
    The source text of a number is the adapter's own: _normalise removes `$`
    and rewrites `?.` only before an identifier, and the ternary rewrite
    moves whole branches, so no rewrite joins or alters digits."""
    # An f-string is no construct of the language (_static_problem says so);
    # its parts are not string constants to check here.
    stack = [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.JoinedStr):
            continue
        stack.extend(ast.iter_child_nodes(n))
        if isinstance(n, ast.Constant) and _numeric(n.value):
            seg = ast.get_source_segment(src, n)
            if seg is None or not _JSON_NUMBER.match(seg):
                return f"number constant {seg!r} is not in the JSON number form (§8.3)"
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            seg = ast.get_source_segment(src, n)
            if seg is None or not _DSL_STRING.match(seg):
                return (f"string constant {str(seg)[:60]!r} is not one quoted run without a backslash "
                        f"(§8.3: no escapes, prefixes, triple quotes or adjacent strings)")
    return None


# b, br, rb are left out: a bytes constant is refused at evaluation as no
# JSON value, as before (test_non_json_values_are_structural).
_PY_STRING_PREFIX = re.compile(r"(?i)(?<![\w.$])(?:r|u|f|fr|rf)\Z")   # Not $u, .f


def _dsl_constants(expr: str) -> list[tuple[int, int]]:
    """(start, end) of every string constant by §8.3's rule (a quote runs to
    the next same quote), up to an unterminated quote, which is left to the
    parser; the constants before it are still read (a triple-quoted
    constant with a quote inside makes an odd count)."""
    out, i = [], 0
    while i < len(expr):
        if expr[i] in ("'", '"'):
            j = expr.find(expr[i], i + 1)
            if j < 0:
                break
            out.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return out


def _string_text_problem(expr: str) -> str | None:
    """The string constants of the text as written, read by
    §8.3's own rule (a quote runs to the next same quote; there are no
    escapes), before anything that reads the text with Python's quoting
    (_quoted_spans, the ternary split, the word scan) can misread it.
    A constant holding a backslash, one written with a Python string prefix
    (an f-string named as the parsed tree names it), or a run of constants
    with only spaces or tabs between them (triple quotes included; a line
    break between them is the line-break failure) is the string-constant
    failure _constant_form_problem names after parsing, named here first.
    An unterminated quote is left to the parser."""
    spans = _dsl_constants(expr)
    message = ("string constant {!r} is not one quoted run without a backslash "
               "(§8.3: no escapes, prefixes, triple quotes or adjacent strings)")
    k = 0
    while k < len(spans):
        a, b = spans[k]
        last = k   # the run of constants with only spaces or tabs between them
        while last + 1 < len(spans) and expr[spans[last][1]:spans[last + 1][0]].strip(" \t") == "":
            last += 1
        if (m := _PY_STRING_PREFIX.search(expr[:a])):
            if "f" in m.group().lower():
                # An f-string is no construct of the language, named as the
                # parsed tree names it (_static_problem), even where it does
                # not parse (f'{Ｔｒｕｅ}').
                return "unsupported AST node: JoinedStr"
            return message.format(expr[m.start():spans[last][1]][:60])
        if last > k or "\\" in expr[a + 1:b - 1]:
            return message.format(expr[a:spans[last][1]][:60])
        k = last + 1
    return None


def _identifier_ok(name: str) -> bool:
    """DSL rc3 §8.3: an ASCII letter or _, then Unicode letters or digits
    (general categories L and N) or _."""
    return bool(name) and (name[0] == "_" or (name[0].isascii() and name[0].isalpha())) and all(
        c == "_" or unicodedata.category(c)[0] in "LN" for c in name[1:])


def _identifier_form_problem(src: str, tree: ast.AST) -> str | None:
    """Ruled 2026-10-09, §8.3's full form: every name and path
    segment, as written, is a §8.3 identifier and is read as written. Python
    also takes combining marks and connector punctuation (so x́$record, its
    $ stripped after a mark, parsed as one name), a non-ASCII first letter,
    and NFKC-normalises (ﬁeld read as "field")."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            written, parsed = ast.get_source_segment(src, n) or n.id, n.id
        elif isinstance(n, ast.Attribute):
            # Whitespace around the segment is _path_form_problem's to report.
            written, parsed = (ast.get_source_segment(src, n) or n.attr).rsplit(".", 1)[-1].strip(), n.attr
        else:
            continue
        if written != parsed or not _identifier_ok(written):
            return f"identifier {written!r} is not in the §8.3 form"
    return None


def _path_form_problem(src: str, tree: ast.AST) -> str | None:
    """DSL rc3 §8.3 (spec 3edc6b97; ruled 2026-10-06): a path is
    one lexical unit, so no whitespace between its root and first segment,
    between segments, or inside [n] / ['name'] outside a string constant
    ($record . a, $record [0], $record[ 0 ]). The text after each attribute
    or subscript's base must be exactly .name or [<index text>]."""
    # Lines split as ast counts them (\r\n, \r or \n; a bare \r ran past
    # this list), as UTF-8 since offsets are bytes.
    raw = [line.encode() for line in re.split(r"\r\n|\r|\n", src)]

    def between(a: ast.AST, b: ast.AST) -> str:
        """From a's end to b's end, after any whitespace and `)` closing a
        grouped base, across lines too: ($record).a and ($record\n).a are a
        grouping, not whitespace in a path."""
        if a.end_lineno == b.end_lineno:
            tail = raw[a.end_lineno - 1][a.end_col_offset:b.end_col_offset].decode()
        else:
            tail = "\n".join([raw[a.end_lineno - 1][a.end_col_offset:].decode()]
                             + [x.decode() for x in raw[a.end_lineno:b.end_lineno - 1]]
                             + [raw[b.end_lineno - 1][:b.end_col_offset].decode()])
        return re.sub(r"\A(?:\s*\))+", "", tail)
    stack = [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.JoinedStr):
            continue   # no construct of the language; _static_problem says so
        stack.extend(ast.iter_child_nodes(n))
        if isinstance(n, ast.Attribute) and between(n.value, n) != "." + n.attr:
            return f"path segment {between(n.value, n).strip()!r} is not one lexical unit (§8.3: no whitespace in a path)"
        if isinstance(n, ast.Subscript):
            idx = ast.get_source_segment(src, n.slice)
            if between(n.value, n) != f"[{idx}]":
                return f"path segment {between(n.value, n).strip()!r} is not one lexical unit (§8.3: no whitespace in a path)"
    return None


def expression_problem(expr: str) -> str | None:
    """The structural failure §8.3 lets the text of an expression decide:
    an unknown $ root, syntax, a construct, operator or comparator outside
    the language, a function name outside call position, an unknown function,
    keyword arguments or a wrong argument count (parse_iso included: §9.1
    defines no such function), a dunder attribute, or a number constant
    outside the §3.4 domain. None if the text is well formed."""
    if (problem := _string_text_problem(expr) or _tokenizer_syntax_problem(expr)):   # strings first
        return problem
    for name in _dollar_names(expr):
        if name not in CONTEXT_VARIABLES and name not in BUILTINS:
            return f"${name} is not a context variable (§8.4)"
    try:
        src = _normalise(expr).strip(_EXPR_END_WS)
        node = ast.parse(src, mode="eval")
        return (_constant_form_problem(src, node) or _identifier_form_problem(src, node)
                or _path_form_problem(src, node) or _static_problem(node.body))
    except SyntaxError:
        return "does not parse as an expression"
    except ValueError:
        return _parse_value_error_problem(expr)
    except RecursionError:
        return "expression nested too deeply"


_PYTHON_CONSTANT_NAMES = frozenset({"True", "False", "None"})


def _parse_value_error_problem(expr: str) -> str:
    """ast.parse raises ValueError, not SyntaxError, for a name that
    NFKC-normalises to True, False or None (Ｔｒｕｅ), since an ast.Name cannot
    hold a constant. NFKC changes it, so it is not a §8.3 identifier and is
    named as any non-identifier is, the first as written (the text before
    _normalise, whose ternary rewrite moves the condition after the
    then-branch). With no such word, the ValueError has another cause (a
    lone surrogate's UnicodeEncodeError) and the text does not parse; with
    one, that word is named even if another cause is present too."""
    for a, b in _outside_quotes(expr):
        for word in re.findall(r"\w+", expr[a:b]):
            if word not in _PYTHON_CONSTANT_NAMES and unicodedata.normalize("NFKC", word) in _PYTHON_CONSTANT_NAMES:
                return f"identifier {word!r} is not in the §8.3 form"
    return "does not parse as an expression"


def _template_bodies(text: str) -> list[str]:
    """The expressions a template evaluates, as _interpolate_template forms
    them: each top-level ${...} body with the optional $ supplied, and for a
    ${trace:...} placeholder the bodies inside its trace-id portion."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            body = text[i + 2:j - 1]
            if body.startswith("trace:"):
                out += _template_bodies(body[:_last_top_level_colon(body)])
            else:
                out.append(_template_body_expr(body))
            i = j
            continue
        i += 1
    return out


def dsl_expression_problems(dsl: dict) -> list[str]:
    """§8.3 at load: every text-decided structural failure in an evaluated
    position, in every rule whether or not it fires, before input is read."""
    out: list[str] = []
    for pos, v in evaluated_values(dsl):
        if "${" in v:
            exprs = _template_bodies(v)
        elif is_expression(v):
            exprs = [v]
        else:
            continue
        for e in exprs:
            if (problem := expression_problem(e)):
                out.append(f"{pos}: {e[:120]!r}: {problem}")
    return out


def dsl_template_problems(dsl: dict) -> list[str]:
    """Load-time form of the §8.3 template rule: every template in an evaluated
    position is checked before any record is read."""
    return [f"{pos}: {problem}" for pos, v in evaluated_values(dsl)
            if "${" in v and (problem := template_problem(v))]


def evaluate(expr: Any, ctx: dict, id_field: bool = False) -> Any:
    """Evaluate a DSL value (DSL spec rc3 §8.3).

    Non-strings are YAML-typed literals, returned as they are (a map or list
    as a copy). A string
    with ${...} is a template. Any other string is classified on its raw
    text: a literal is returned exactly as written; an expression is
    evaluated, a structural failure raises, an input failure yields None and
    a diagnostic. id_field marks the trace `id` (§13.2 R5): an unusable
    template part (null, "", a boolean, an object or a list) makes the id
    null, so R5 drops the trace.
    """
    if not isinstance(expr, str):
        if isinstance(expr, float) and (expr != expr or expr in (float("inf"), float("-inf"))):
            raise ExpressionEvalError(f"{expr!r}: a non-finite number is not a JSON value")
        # A map or list literal is copied: returned as the adapter's
        # own object, a write beneath it in one trace changed the adapter
        # text and every trace sharing it.
        return _json_copy(expr)
    if "${" in expr:
        problem = template_problem(expr)
        if problem:
            raise ExpressionEvalError(problem)
        return _interpolate_template(expr, ctx, id_parts=id_field)
    if not is_expression(expr):
        return expr
    expr_reset = _EVAL_EXPR.set(expr)
    try:
        value = _evaluate_expression(expr, ctx)
    except _InputFailure as e:
        _report_input_failure(expr, e)
        return None
    finally:
        _EVAL_EXPR.reset(expr_reset)
    if isinstance(value, (dict, list)):
        # A whole-value reference ($record, $trace, $constants.x) must not
        # alias the context into the trace: $trace would make a cycle and
        # a shared constant would carry one container's data into another.
        return _json_copy(value)
    return value


def _json_copy(value: Any) -> Any:
    """Copy of a JSON-shaped value without recursion: copy.deepcopy recursed
    and raised RecursionError on data nested a few hundred levels deep, which
    the readers admit."""
    if not isinstance(value, (dict, list)):
        return value
    root = {} if isinstance(value, dict) else []
    stack = [(value, root)]
    while stack:
        src, dst = stack.pop()
        items = src.items() if isinstance(src, dict) else enumerate(src)
        for k, v in items:
            if isinstance(v, (dict, list)):
                child = {} if isinstance(v, dict) else []
                stack.append((v, child))
            else:
                child = v
            if isinstance(dst, dict):
                dst[k] = child
            else:
                dst.append(child)
    return root


def _evaluate_expression(expr: str, ctx: dict) -> Any:
    if (problem := _string_text_problem(expr) or _tokenizer_syntax_problem(expr)):   # as at load
        raise ExpressionEvalError(f"{expr[:200]!r}: {problem}")
    for name in _dollar_names(expr):
        if name not in ctx and name not in CONTEXT_VARIABLES and name not in BUILTINS:
            # §8.3: a reference to a root the spec does not define is a
            # structural failure; returning the bare name was source text
            # with its $ removed (§14.7).
            raise ExpressionEvalError(f"{expr[:80]!r}: ${name} is not a context variable (§8.4)")
    src = _normalise(expr).strip(_EXPR_END_WS)
    try:
        node = ast.parse(src, mode="eval")
    except SyntaxError:
        raise ExpressionEvalError(f"{expr!r}: does not parse as an expression") from None
    except ValueError:   # as at load
        raise ExpressionEvalError(f"{expr[:200]!r}: {_parse_value_error_problem(expr)}") from None
    if (problem := _constant_form_problem(src, node) or _identifier_form_problem(src, node)
            or _path_form_problem(src, node)):   # as at load
        raise ExpressionEvalError(f"{expr[:200]!r}: {problem}")
    try:
        return _eval_node(node.body, ctx)
    except EvalError as exc:
        raise ExpressionEvalError(f"{expr[:200]!r}: {exc}") from None
    except RecursionError:
        raise ExpressionEvalError(f"{expr[:80]!r}: expression nested too deeply") from None
    except _INPUT_ERRORS as exc:
        raise _InputFailure(f"{type(exc).__name__}: {str(exc)[:200]}") from None


def _numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _ordering_operand_problem(node: ast.Compare) -> str | None:
    """§8.3 (ruled 2026-10-05, spec 49647777): an operand of `<` or `>`
    that is itself a comparison or a `not` expression, parenthesised or not,
    can only be a boolean, so the text decides the failure: structural at
    load ((1 < x) < 9, (not x) > 0). Under == and != it stays valid, and a
    boolean constant operand (true > 0) stays an input failure."""
    if not any(isinstance(op, (ast.Lt, ast.Gt)) for op in node.ops):
        return None
    for operand in (node.left, *node.comparators):
        if isinstance(operand, ast.Compare) or (
                isinstance(operand, ast.UnaryOp) and isinstance(operand.op, ast.Not)):
            return f"a comparison or `not` as an operand of < or > is not part of the language: " \
                   f"{ast.unparse(node)[:120]}"
    return None


def _check_ordering_operands(left: Any, right: Any) -> None:
    """§8.3: `<` and `>` take two numbers (never booleans) or two timestamp
    strings iso_normalise returns unchanged (the minitrace-spec §3.5 form, in
    which string order is chronological). Any other pair, null included, is
    an input failure (null was read as 0, so `$missing < 1` was true)."""
    if _numeric(left) and _numeric(right):
        return
    if isinstance(left, str) and isinstance(right, str) and \
            _spec_timestamp_problem(left) is None and _spec_timestamp_problem(right) is None:
        return
    raise TypeError(f"< and > need two numbers or two §3.5 timestamps, "
                    f"got {type(left).__name__} and {type(right).__name__}")


_MAX_SAFE_INT = 2**53 - 1


def _in_number_domain(v: Any) -> Any:
    """Return v if a trace can hold it (RFC 8785: integers within +-(2^53-1),
    finite floats), else raise OverflowError, an input failure (§8.3). A sum
    past the domain used to surface only at sealing and lose the whole set."""
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) > _MAX_SAFE_INT:
        raise OverflowError(f"integer result outside the RFC 8785 domain ({len(str(v))} digits)")
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        raise OverflowError("non-finite number")
    if isinstance(v, float) and abs(v) > _MAX_SAFE_INT:
        # §3.4 decides by value: 1e16 is outside whatever its spelling.
        raise OverflowError(f"number result {v!r} outside the domain of ±(2^53-1)")
    return v


def _constant_out_of_domain(v: Any) -> bool:
    """A DSL number (literal or expression constant) outside the §3.4 domain
    (DSL spec §2): structural at load."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return True
    return abs(v) > _MAX_SAFE_INT


def _json_shape_problem(v: Any) -> tuple[str, str] | None:
    """A Python value that is not a JSON value as it stands (DSL spec §7.1:
    one an I-JSON message can hold): a tuple, set, bytes or any other type,
    or an object with a member name that is not a string. json.dumps would
    rewrite some of these ((1,) as [1], {1: 'a', '1': 'b'} as a duplicate
    name) and the hashing raises on them, so the walks that look inside
    only dicts and lists refuse them first."""
    if isinstance(v, dict):
        bad = [k for k in v if not isinstance(k, str)]
        if not bad:
            return None
        return ("not-a-json-value", f"member name {bad[0]!r} ({type(bad[0]).__name__}), not a string")
    if v is None or isinstance(v, (str, int, float, list)):   # bool is an int
        return None
    return ("not-a-json-value", f"a {type(v).__name__} value, not a JSON value")


def _eval_node(node: ast.AST, ctx: dict) -> Any:
    if isinstance(node, ast.Constant):
        if _constant_out_of_domain(node.value):
            raise EvalError(f"number constant {node.value!r} outside the §3.4 domain")
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return node.value
        raise EvalError(f"constant of type {type(node.value).__name__} is not a JSON value")
    if isinstance(node, ast.Name):
        name = node.id
        if name in ctx:
            return ctx[name]
        if name in CONTEXT_VARIABLES:
            return None   # defined but not bound in this scope: safe navigation
        if name in BUILTINS:
            # A function name outside call position (`$len`).
            raise EvalError(f"function {name!r} outside call position")
        if name in ("True", "true"):
            return True
        if name in ("False", "false"):
            return False
        if name in ("None", "null"):
            return None
        # A bare word inside an expression is a string (e.g. `EXECUTE`).
        return name
    if isinstance(node, ast.Attribute):
        target = _eval_node(node.value, ctx)
        return _attr(target, node.attr)
    if isinstance(node, ast.Subscript):
        if (problem := _index_problem(node.slice)):
            raise EvalError(problem)
        target = _eval_node(node.value, ctx)
        idx = node.slice.value
        # [n] reads a list element and ['name'] an object key; anything else,
        # [n] on a string included, is null (safe navigation, §8.3).
        if isinstance(idx, str):
            return target.get(idx) if isinstance(target, dict) else None
        return target[idx] if isinstance(target, list) and idx < len(target) else None
    if isinstance(node, ast.Call):
        if node.keywords:
            raise UnsafeExpressionError("keyword arguments are not supported")
        if not (isinstance(node.func, ast.Name) and node.func.id in BUILTINS):
            # Methods (`.total_seconds()`), unknown names, computed callees:
            # not part of the language (§9.1).
            if isinstance(node.func, ast.Attribute) and node.func.attr.startswith("__"):
                raise UnsafeExpressionError(f"attribute {node.func.attr!r} is not accessible")
            raise EvalError(f"call to a function §9.1 does not define: {ast.unparse(node.func)}")
        fn = BUILTINS[node.func.id]
        try:
            inspect.signature(fn).bind(*node.args)
        except TypeError:
            raise EvalError(f"{node.func.id}() called with {len(node.args)} argument(s)") from None
        if fn is fn_coalesce:
            # §8.3 Evaluation order and reach: coalesce stops at its first
            # non-null argument; the ones after it are not evaluated and
            # cannot fail (their text was checked at load).
            for a in node.args:
                if (value := _eval_node(a, ctx)) is not None:
                    return value
            return None
        args = [_eval_node(a, ctx) for a in node.args]
        return fn(*args)
    if isinstance(node, ast.IfExp):
        cond = _eval_node(node.test, ctx)
        return _eval_node(node.body, ctx) if cond else _eval_node(node.orelse, ctx)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not bool(operand)
        if isinstance(node.op, ast.USub):
            # Kept for the sign of a number literal (`coalesce($x, -1)`).
            if not _numeric(operand):
                raise TypeError(f"unary - on a {type(operand).__name__}")
            return _in_number_domain(-operand)
        raise EvalError(f"unsupported unary op: {ast.dump(node.op)}")
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub)):
            raise EvalError(f"unsupported binop: {ast.dump(node.op)}")
        a = _eval_node(node.left, ctx)
        b = _eval_node(node.right, ctx)
        if not (_numeric(a) and _numeric(b)):
            raise TypeError(f"+/- needs two numbers, got {type(a).__name__} and {type(b).__name__}")
        return _in_number_domain(a + b if isinstance(node.op, ast.Add) else a - b)
    if isinstance(node, ast.Compare):
        if len(node.ops) > 1:
            raise EvalError("chained comparison is not part of the language")
        if (problem := _ordering_operand_problem(node)):
            raise EvalError(problem)
        left = _eval_node(node.left, ctx)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, ctx)
            if isinstance(op, ast.Eq):
                ok = json_equal(left, right)
            elif isinstance(op, ast.NotEq):
                ok = not json_equal(left, right)
            elif isinstance(op, (ast.Lt, ast.Gt)):
                _check_ordering_operands(left, right)
                ok = left < right if isinstance(op, ast.Lt) else left > right
            else:
                raise EvalError(f"unsupported comparator: {ast.dump(op)}")
            if not ok:
                return False
            left = right
        return True
    raise EvalError(f"unsupported AST node: {ast.dump(node)}")


def _attr(target: Any, attr: str) -> Any:
    """Key access on dicts; on anything else None (safe navigation, §8.3: a
    non-object intermediate is not a failure). Input data decides a value's
    type, so this must not raise. Dunder names raise: no data-access
    expression uses them, and a DSL that does is an escape attempt."""
    if attr.startswith("__") and attr.endswith("__"):
        raise UnsafeExpressionError(f"attribute {attr!r} is not accessible")
    if isinstance(target, dict):
        return target.get(attr)
    return None


# Allowlist, not blocklist: a blocklist misses platform specifics such as
# Windows drive-relative names ("C:x") and reserved device names.
_SAFE_TRACE_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}")
_WINDOWS_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?")


def trace_output_filename(trace: dict) -> str:
    """File name for one trace in directory output. The trace id usually
    derives from a session id in the input, so it is untrusted: it must be
    1-200 characters of [A-Za-z0-9._-], not starting with a dot and not a
    Windows device name, otherwise it raises rather than writing elsewhere.
    run() drops such a trace before this is reached (_drop_unwritable_names),
    so the raise is a backstop."""
    tid = trace.get("id") or "trace"
    if (not isinstance(tid, str) or not _SAFE_TRACE_ID.fullmatch(tid)
            or _WINDOWS_RESERVED.fullmatch(tid)):
        raise ValueError(f"trace id is not usable as a file name: {tid!r}")
    return tid + ".minitrace.json"


def output_filenames(traces: list[dict]) -> list[str]:
    """All file names for directory output, checked before anything is
    written: an unusable id or two ids that collide (also case-insensitively,
    as on APFS/NTFS) fail the run instead of leaving partial or overwritten
    output."""
    names = [trace_output_filename(t) for t in traces]
    seen: dict[str, str] = {}
    for n in names:
        if n.casefold() in seen:
            raise ValueError(f"two traces map to the same file name: {seen[n.casefold()]!r}, {n!r}")
        seen[n.casefold()] = n
    return names


def _drop_unwritable_names(traces: list[dict], diagnostics: list[dict]) -> list[dict]:
    """DSL rc3 §15 Writing output: in directory output a trace
    whose id is not usable as a file name under trace_output_filename's
    rule, or whose file name collides with another's (letter case ignored,
    as on APFS/NTFS), is not written: a set-level loss, reported, never an
    abort. None of the colliding traces is written, as under R5. Runs after
    R5 and before cross-trace resolution (ruled 2026-10-19), so a
    placeholder naming a dropped trace degrades under R4 and output_filenames
    no longer raises on what is left."""
    named = []
    for t in traces:
        try:
            named.append((t, trace_output_filename(t)))
        except ValueError:
            tid = str(t.get("id")).encode("unicode_escape").decode()[:80]
            diagnostics.append({"level": "warning", "kind": "trace-dropped", "reason": "file-name-unusable",
                                "message": f"trace id {tid!r} is not usable as a file name; "
                                           f"trace not written (DSL §15)"})
    groups: dict[str, list[str]] = {}
    for t, n in named:
        groups.setdefault(n.casefold(), []).append(t["id"])
    for ids in groups.values():
        if len(ids) > 1:
            diagnostics.append({"level": "warning", "kind": "trace-dropped", "reason": "file-name-collision",
                                "message": f"trace ids {', '.join(map(repr, ids))} map to the same file name "
                                           f"(letter case ignored); none written (DSL §15)"})
    return [t for t, n in named if len(groups[n.casefold()]) == 1]


# ---------------------------------------------------------------------------
# Path-based set: write 'a.b.c' into a nested dict, creating dicts on the way.

def deep_set(target: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    node = target
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


def _json_type(v: Any) -> str:
    """The JSON name of a value's type, with its article, for messages."""
    return ("a boolean" if isinstance(v, bool) else "a number" if isinstance(v, (int, float))
            else "a string" if isinstance(v, str) else "an array" if isinstance(v, list)
            else "an object" if isinstance(v, dict) else "null")


def deep_set_blocked(target: dict, path: str, value: Any) -> tuple[int, Any] | None:
    """deep_set for set and accumulate (ruled 2026-10-09): a parent
    on the path that holds a string, array, number or boolean is not
    replaced; nothing is written and (segments to it, its value) is
    returned for the caller to report as an input failure, the prior value
    kept. A null or absent parent is created, as deep_set does. Only parents
    that already exist can block, and they come before any created one, so
    a blocked write changes nothing."""
    keys = path.split(".")
    node = target
    for i, k in enumerate(keys[:-1]):
        nxt = node.get(k)
        if nxt is None:
            node[k] = nxt = {}
        elif not isinstance(nxt, dict):
            return i + 1, nxt
        node = nxt
    node[keys[-1]] = value
    return None


def _blocking_parent(target: dict, path: str) -> tuple[int, Any] | None:
    """What deep_set_blocked would refuse, without writing: (segments to it,
    its value) for the first existing parent that is not an object, or None.
    A null or absent parent ends the walk: everything below it is created."""
    node = target
    for i, k in enumerate(path.split(".")[:-1]):
        nxt = node.get(k)
        if nxt is None:
            return None
        if not isinstance(nxt, dict):
            return i + 1, nxt
        node = nxt
    return None


def _blocked_write(verb: str, key: str, expr: Any, prefix: int, blocked: tuple[int, Any]) -> dict:
    """The input failure for a set or accumulate key, or a
    session.derived or finalize.computed key, whose path runs through
    a non-object: names the key's path up to that value, and its type.
    `prefix` is 1 when the key starts with state. or session."""
    n, value = blocked
    in_the_way = ".".join(key.split(".")[:prefix + n])
    return _input_failure(f"{verb}.{key}", expr, f"{verb}.{key}: {in_the_way} holds "
                          f"{_json_type(value)}, not an object; prior value kept")


def deep_get(target: dict, path: str) -> Any:
    keys = path.split(".")
    node = target
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
        if node is None:
            return None
    return node


_LIST_INDEX = re.compile(r"0|[1-9][0-9]*")


def record_path_get(target: Any, path: str) -> Any:
    """Look up a container's dotted record_path (DSL rc3 §4.1.1): at
    an object a segment is a key; at a list a segment in the canonical form
    of a non-negative integer (ASCII digits, no sign, no leading zero, as a
    JSON Pointer index) is an index. Anything else, an index out of range
    included, is a missing key: null, which the caller reads as an empty
    record set. deep_get, which predicates and state paths use, does not
    index lists."""
    node = target
    for k in path.split("."):
        if isinstance(node, dict):
            node = node.get(k)
        # A canonical index with more digits than len(node) is out of range;
        # checking that first keeps int() off a segment past its digit limit.
        elif (isinstance(node, list) and _LIST_INDEX.fullmatch(k)
              and len(k) <= len(str(len(node))) and int(k) < len(node)):
            node = node[int(k)]
        else:
            return None
        if node is None:
            return None
    return node


# ---------------------------------------------------------------------------
# Predicate evaluation

# Predicate shapes predicate_matches() evaluates. Anything else reaches its
# final `return False`: the rule silently never fires. That is how vibe's four
# `equals:` rules and opencode's two `length_gt` rules sat dead (f533ad69,
# 9499d90a). predicate_problems() is the single definition of "recognised";
# run() warns with it once per DSL, and lint_dispatch fails on it.
_PRED_LEAF_SHAPES = ({"has_any"}, {"has"}, {"block_type"})
_PRED_SCOPES = ("field", "container_field", "block_field")
_PRED_TESTS = ("value", "present")


def predicate_problems(pred: Any, where: str = "when", in_block: bool = False) -> list[str]:
    """Return why `pred` is not a §8.1 predicate form ([] if it is one
    predicate_matches() evaluates as written): a shape that would silently
    evaluate False regardless of input, keys ignored beside a recognised
    shape, and an operand of the wrong type, which crashed when the
    rule fired or was read by truthiness. run() refuses every one at load
    through dsl_shape_problems, a rule with no when too since the 2026-10-17 ruling."""
    if not isinstance(pred, dict) or not pred:
        return [f"{where}: empty or non-mapping predicate never matches"]
    keys = set(pred)
    for combinator in ("all_of", "any_of"):
        if combinator in keys:
            out = [f"{where}: keys {sorted(keys - {combinator})} ignored beside {combinator}"] \
                if keys != {combinator} else []
            children = pred[combinator]
            if not isinstance(children, list) or not children:
                return out + [f"{where}.{combinator}: must be a non-empty list"]
            for i, c in enumerate(children):
                out += predicate_problems(c, f"{where}.{combinator}[{i}]", in_block)
            return out
    # Ruled 2026-10-10, match the schema: operand types too. A path
    # or key that is not a string crashed when the rule fired (has: [x],
    # field: 5), and has_any or present were read by truthiness, so
    # `present: "false"` meant present and `has_any: "false"` matched all.
    if keys in _PRED_LEAF_SHAPES:
        if keys == {"has_any"} and not pred["has_any"]:
            return [f"{where}: has_any is falsy, never matches"]
        if keys == {"has_any"} and pred["has_any"] is not True:
            return [f"{where}: has_any takes only the value true (§8.1)"]
        if keys == {"has"} and not isinstance(pred["has"], str):
            return [f"{where}: has takes a field name, a string (§8.1)"]
        if keys == {"block_type"} and not in_block:
            return [f"{where}: block_type outside on_block never matches"]
        return []
    scopes = [s for s in _PRED_SCOPES if s in keys]
    tests = [t for t in _PRED_TESTS if t in keys]
    if len(scopes) == 1 and len(tests) == 1 and keys == {scopes[0], tests[0]}:
        if scopes[0] == "block_field" and not in_block:
            return [f"{where}: block_field outside on_block never matches"]
        out = []
        if not isinstance(pred[scopes[0]], str):
            out.append(f"{where}: {scopes[0]} takes a path, a string (§8.1)")
        if tests[0] == "present" and not isinstance(pred["present"], bool):
            out.append(f"{where}: present takes true or false (§8.1)")
        return out
    if len(scopes) == 1 and not tests:
        return [f"{where}: {scopes[0]} without value: or present: never matches "
                f"(unrecognised {sorted(keys - {scopes[0]})})"]
    return [f"{where}: unrecognised predicate {sorted(keys)} "
            f"(recognised: all_of, any_of, has_any, has, block_type, "
            f"field|container_field|block_field + value|present)"]


def dsl_predicate_problems(dsl: dict) -> list[str]:
    """predicate_problems over every rule's when:, including on_block sub-rules."""
    out: list[str] = []
    for i, rule in enumerate(dsl.get("emit") or []):
        name = rule.get("name", f"emit[{i}]")
        out += predicate_problems(rule.get("when"), f"{name}.when")
        for j, sub in enumerate(rule.get("on_block") or []):
            sub_name = sub.get("name", f"on_block[{j}]")
            out += predicate_problems(sub.get("when"), f"{name}/{sub_name}.when", in_block=True)
    return out


def _is_whole_template(s: str) -> bool:
    """True if `s` is exactly one `${...}` whose closing brace ends the string."""
    if not s.startswith("${"):
        return False
    depth = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i == len(s) - 1
    return False


def _is_lineage_bearing_path(path: tuple) -> bool:
    """True for the `target_content_hash` of an `emit_lineage` action, in a
    top-level rule or in an on_block sub-rule:
        emit / <i> / do / <j> / emit_lineage / target_content_hash
        emit / <i> / on_block / <k> / do / <j> / emit_lineage / target_content_hash
    Lineage set through session.static / derived is not a supported home and is
    rejected: those paths are literal or expression values, not an emitted edge,
    and no adapter uses them (stricter than §8.4's wording, deliberately)."""
    def is_idx(x: Any) -> bool:
        return isinstance(x, int) and not isinstance(x, bool)
    tail = ("emit_lineage", "target_content_hash")
    if len(path) == 6:
        return path[0] == "emit" and is_idx(path[1]) and path[2] == "do" and is_idx(path[3]) \
            and path[4:] == tail
    if len(path) == 8:
        return path[0] == "emit" and is_idx(path[1]) and path[2] == "on_block" and is_idx(path[3]) \
            and path[4] == "do" and is_idx(path[5]) and path[6:] == tail
    return False


def dsl_placeholder_problems(dsl: Any) -> list[str]:
    """DSL spec RC 3 §8.4 / §13.1: the `trace:` special form is valid only as the
    whole template of `target_content_hash` in an `emit_lineage` action.

    Rejected at load rather than warned about (unlike dsl_predicate_problems):
    a misplaced placeholder produces wrong output, a reference nobody resolves
    carried as content, while a dead rule only produces no output. Keys are
    checked as well as values. YAML comments are not part of the loaded document
    and are not inspected."""
    out: list[str] = []

    def where(path: tuple) -> str:
        return ".".join(str(p) for p in path) or "<root>"

    def walk(node: Any, path: tuple) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and "${trace:" in k:
                    out.append(f"{where(path)}: `trace:` form in a key: {k!r}")
                walk(v, path + (k,))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + (i,))
        elif isinstance(node, str) and "${trace:" in node:
            if not _is_lineage_bearing_path(path):
                out.append(f"{where(path)}: `trace:` form outside an emit_lineage target_content_hash")
            elif not _is_whole_template(node):
                out.append(f"{where(path)}: `trace:` form must be the whole template, got {node!r}")

    walk(dsl, ())
    return out


def dsl_number_problems(dsl: Any) -> list[str]:
    """DSL spec rc3 §2: a number anywhere in the DSL file must lie in the
    minitrace-spec §3.4 domain; one outside it (beyond ±(2^53-1) by value in
    any notation, .inf, .nan, an overflow such as 1e400) is structural at
    load. The whole document is walked, not only evaluated positions, since a
    static or passthrough value reaches the trace as written. Number constants
    inside expressions are checked by the expression pass."""
    out: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif _constant_out_of_domain(node):
            out.append(f"{path or '<root>'}: {node!r}")

    walk(dsl, "")
    return out


def dsl_string_problems(dsl: Any) -> list[str]:
    """An emitted trace is I-JSON (minitrace-spec §3.4, DSL spec §4): a
    string or key anywhere in the DSL file holding an unpaired surrogate or a
    noncharacter (written as a YAML escape such as "\\uFFFF") is structural
    at load, since a static value or a template reaches the trace as written."""
    from source_reader import _ijson_string_problem  # noqa: PLC0415
    out: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                where = f"{path}.{k}" if path else str(k)
                if isinstance(k, str) and (bad := _ijson_string_problem(k)):
                    out.append(f"{where.encode('unicode_escape').decode()}: key holds {bad[1]}")
                walk(v, where)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and (bad := _ijson_string_problem(node)):
            out.append(f"{(path or '<root>').encode('unicode_escape').decode()}: {bad[1]}")

    walk(dsl, "")
    return out


def _refuse_non_ijson(traces: list[dict], diagnostics: list[dict],
                      dropped_ids: list | None = None) -> list[dict]:
    """Last check before writing: an emitted trace is I-JSON (minitrace-spec
    §3.4). Input strings are repaired when read and the adapter text is
    checked at load, so nothing should reach here; a trace that still holds
    an unpaired surrogate or a noncharacter (a member name included) is not
    written: a set-level loss, reported, never a write that fails halfway.
    It runs before cross-trace resolution (whose hashing cannot take such a
    string), and a dropped trace's id goes to `dropped_ids`, which R5 counts
    as it counts a trace dropped for its turn index."""
    from source_reader import _ijson_string_problem  # noqa: PLC0415
    kept = []
    for t in traces:
        stack, bad = [t], None
        while stack and bad is None:
            v = stack.pop()
            if (bad := _json_shape_problem(v)) is not None:
                break
            if isinstance(v, str):
                bad = _ijson_string_problem(v)
            elif _constant_out_of_domain(v):   # the hashing raises on these too
                bad = ("number-outside-safe-range", f"number {v!r} outside the §3.4 domain")
            elif isinstance(v, dict):
                bad = next((p for k in v if isinstance(k, str) and (p := _ijson_string_problem(k))), None)
                stack.extend(v.values())
            elif isinstance(v, list):
                stack.extend(v)
        if bad is None:
            kept.append(t)
            continue
        if dropped_ids is not None and isinstance(t, dict):
            dropped_ids.append(t.get("id"))
        tid = str(t.get("id")).encode("unicode_escape").decode()[:80] if isinstance(t, dict) else "?"
        diagnostics.append({"level": "warning", "kind": "trace-dropped", "reason": bad[0],
                            "message": f"trace {tid!r} holds {bad[1]}; not I-JSON "
                                       f"(minitrace-spec §3.4), trace not written (DSL §15)"})
    return kept


def _writes_field(key: Any, field: str) -> bool:
    """True if a trace path (dotted, as deep_set takes it) is `field` or below."""
    return isinstance(key, str) and (key == field or key.startswith(field + "."))


def _direct_write_sites(dsl: Any, field: str) -> list[str]:
    """Every place outside the emit actions that writes the trace field
    `field`: session static, derived or passthrough, finalize.computed, and
    a rule's set to it (session.<field> or <field>)."""
    out: list[str] = []
    if not isinstance(dsl, dict):
        return out
    sessions = [("session", dsl.get("session"))] + [
        (f"sessions.{n}", s) for n, s in (dsl.get("sessions") or {}).items()]
    for name, sess in sessions:
        if not isinstance(sess, dict):
            continue
        for block in ("static", "derived", "passthrough"):
            for k in (sess.get(block) or {}):
                if _writes_field(k, field):
                    out.append(f"{name}.{block}.{k}")
    for k in ((dsl.get("finalize") or {}).get("computed") or {}):
        if _writes_field(k, field):
            out.append(f"finalize.computed.{k}")

    def rules(rs: Any, where: str) -> None:
        for r in rs or []:
            if not isinstance(r, dict):
                continue
            here = f"{where}.{r.get('name', '?')}"
            for a in r.get("do") or []:
                if not isinstance(a, dict):
                    continue
                for k in (a.get("set") or {}) if isinstance(a.get("set"), dict) else ():
                    target = k[len("session."):] if isinstance(k, str) and k.startswith("session.") else k
                    if _writes_field(target, field):
                        out.append(f"{here}.set.{k}")
            rules(r.get("on_block"), f"{here}.on_block")
    rules(dsl.get("emit"), "emit")
    return out


def dsl_tool_calls_problems(dsl: Any) -> list[str]:
    """Ruled 2026-10-05: rejected at load as §13.1 rejects lineage:
    tool_calls is written only by emit_tool_call and attach_tool_result, so
    every call carries the §8.2.3 id and tool_name rules. A write through
    set, session static, derived or passthrough, or finalize.computed would
    put calls past them ([{"id": 1, "tool_name": true}] was written).
    $state.tool_calls_by_id holds the emitted calls themselves (§8.2.3), so
    a write through it writes the trace: it is read, never written (ruled
    2026-10-05, option 1): no set or accumulate key at or below
    state.tool_calls_by_id, and no such key in the initial state block."""
    out = [f"{w}: tool_calls is written only by emit_tool_call and attach_tool_result"
           for w in _direct_write_sites(dsl, "tool_calls")]
    if not isinstance(dsl, dict):
        return out
    alias = "is the emitted calls themselves (§8.2.3); it is read, never written"
    # A dotted key too (tool_calls_by_id.5), as the DSL schema's state-block
    # propertyNames refuse it, so runner and schema reject the same adapters.
    for k in (dsl["state"] if isinstance(dsl.get("state"), dict) else ()):
        if _writes_field(k, "tool_calls_by_id"):
            out.append(f"state.{k}: state.tool_calls_by_id {alias}")

    def rules(rs: Any, where: str) -> None:
        for r in rs or []:
            if not isinstance(r, dict):
                continue
            here = f"{where}.{r.get('name', '?')}"
            for a in r.get("do") or []:
                if not isinstance(a, dict):
                    continue
                for verb in ("set", "accumulate"):
                    for k in (a.get(verb) or {}) if isinstance(a.get(verb), dict) else ():
                        if isinstance(k, str) and k.startswith("state.") \
                                and _writes_field(k[len("state."):], "tool_calls_by_id"):
                            out.append(f"{here}.{verb}.{k}: state.tool_calls_by_id {alias}")
            rules(r.get("on_block"), f"{here}.on_block")
    rules(dsl.get("emit"), "emit")
    return out


def dsl_empty_segment_problems(dsl: Any) -> list[str]:
    """DSL rc3 §15, spec f2430aed; ruled 2026-10-09: a key with an
    empty segment (state.a..b, state.c., state., flags..x) in a set or
    accumulate (on_block's do included), in finalize.computed, or in a
    static, derived or passthrough map of session or of a sessions shape is
    a structural failure at load. The runner wrote keys named "" instead."""
    out: list[str] = []
    if not isinstance(dsl, dict):
        return out

    def check(where: str, keys: Any) -> None:
        for k in keys if isinstance(keys, dict) else ():
            if isinstance(k, str) and "" in k.split("."):
                out.append(f"{where}.{k}: a key with an empty segment (§15)")

    def rules(rs: Any, where: str) -> None:
        for r in rs or []:
            if not isinstance(r, dict):
                continue
            here = f"{where}.{r.get('name', '?')}"
            for a in r.get("do") or []:
                for verb in ("set", "accumulate"):
                    if isinstance(a, dict):
                        check(f"{here}.{verb}", a.get(verb))
            rules(r.get("on_block"), f"{here}.on_block")
    rules(dsl.get("emit"), "emit")
    check("finalize.computed", (dsl.get("finalize") or {}).get("computed") if isinstance(dsl.get("finalize"), dict) else None)
    shapes = [("session", dsl["session"])] if isinstance(dsl.get("session"), dict) else []
    if isinstance(dsl.get("sessions"), dict):
        shapes += [(f"sessions.{n}", s) for n, s in dsl["sessions"].items() if isinstance(s, dict)]
    for where, s in shapes:
        for part in ("static", "derived", "passthrough"):
            check(f"{where}.{part}", s.get(part))
    return out


def dsl_accumulate_problems(dsl: Any) -> list[str]:
    """DSL rc3 §15 (ruled 2026-10-09): "an accumulate key that is not
    a state. path" is a structural failure at load (§8.2.5: the LHS is a
    state. path; the DSL schema's AccumulateBody says the same). Such a key
    loaded and was then dropped, with no write and no report."""
    out: list[str] = []
    if not isinstance(dsl, dict):
        return out

    def rules(rs: Any, where: str) -> None:
        for r in rs or []:
            if not isinstance(r, dict):
                continue
            here = f"{where}.{r.get('name', '?')}"
            for a in r.get("do") or []:
                body = a.get("accumulate") if isinstance(a, dict) else None
                for k in body if isinstance(body, dict) else ():
                    if not (isinstance(k, str) and k.startswith("state.")):
                        out.append(f"{here}.accumulate.{k}: an accumulate key is a state. path (§8.2.5)")
            rules(r.get("on_block"), f"{here}.on_block")
    rules(dsl.get("emit"), "emit")
    return out


_RULE_KEYS = frozenset({"name", "when", "do", "foreach_block", "on_block", "target"})
_ADAPTER_KEYS = frozenset({"name", "harness_version", "dsl_version", "source_format", "converter_version",
                           "produces_schema"})   # §3, as the schema
_BLOCK_RULE_KEYS = frozenset({"name", "when", "do"})   # block iteration is one level deep
_ACTIONS = frozenset({"set", "emit_turn", "emit_tool_call", "attach_tool_result", "accumulate", "note",
                      "emit_lineage", "emit_annotation", "emit_redaction", "emit_policy_trigger",
                      "emit_segment_marker"})
_OPERATION_TYPES = ("READ", "MODIFY", "NEW", "EXECUTE", "DELEGATE", "OTHER")
_BODY_KEYS = {"attach_tool_result": frozenset({"target_id", "output"}),   # §8.2.4
              "note": frozenset({"level", "message"})}                     # §8.2.6


def dsl_shape_problems(dsl: Any) -> list[str]:
    """DSL rc3 §15 (ruled 2026-10-10): shape errors are
    structural failures at load: an unknown key in a rule (or in an on_block
    sub-rule, which takes name, when and do only), an unknown action, a second
    key beside an action (omit_if_none beside emit_turn only), an action body
    that is not a mapping, a do that is not a list, an unknown predicate form
    (§8.1: every form predicate_problems reports, for a when that is present)
    or a block_type/block_field predicate outside on_block, and an
    operation_types value outside §10's enumeration. They loaded; a wrong body
    crashed only when its rule fired, an unknown key or action was ignored or
    failed at run time, and a dead predicate was a warning. A rule with no
    when is refused too since the 2026-10-17 ruling. Runs before the other load checks, which
    assume these shapes."""
    out: list[str] = []
    if not isinstance(dsl, dict):
        # Ruled 2026-10-18: refused at load, not a crash. A list, a scalar,
        # null or an empty file crashed in run() reading the adapter block.
        return [f"the adapter text: a mapping of top-level keys (§2), not {_json_type(dsl)}"]
    # Ruled 2026-10-17, refusal at load: exactly one of
    # session and sessions (§2) and an input type of json or jsonl (§4). Both
    # keys or neither failed after the input was read, and another input type
    # crashed reading it (unsupported input type, a ValueError).
    if ("session" in dsl) == ("sessions" in dsl):
        out.append("session, sessions: exactly one of the two is present (§2)")
    # Ruled 2026-10-18, refuse at load, as the schema: a session
    # or sessions of another type, a shape of another type, or an empty
    # sessions crashed or was refused for a rule's target; an absent or
    # non-mapping adapter block, or one without dsl_version, crashed.
    if "session" in dsl and not isinstance(dsl["session"], dict):
        out.append(f"session: a mapping (§7), not {_json_type(dsl['session'])}")
    if "sessions" in dsl:
        shapes = dsl["sessions"]
        if not isinstance(shapes, dict):
            out.append(f"sessions: a mapping of shape name to shape (§14), not {_json_type(shapes)}")
        elif not shapes:
            out.append("sessions: no shape declared (§14)")
        for k, v in shapes.items() if isinstance(shapes, dict) else ():
            if not isinstance(v, dict):
                out.append(f"sessions.{k}: a mapping (§14), not {_json_type(v)}")
    if "adapter" not in dsl:
        out.append("adapter: absent; it is required (§3)")
    elif not isinstance(dsl["adapter"], dict):
        out.append(f"adapter: a mapping (§3), not {_json_type(dsl['adapter'])}")
    elif "dsl_version" not in dsl["adapter"]:
        out.append("adapter.dsl_version: absent; it is required (§3)")
    inp = dsl.get("input")
    if not isinstance(inp, dict) or inp.get("type") not in ("json", "jsonl"):
        got = repr(inp.get("type")) if isinstance(inp, dict) else f"input {inp!r}"
        out.append(f"input.type: json or jsonl, not {got} (§4)")
    # DSL rc3 §4 and §15 (ruled 2026-10-18: UTF-8 only): an input is read as
    # UTF-8, so encoding, where present, is exactly utf-8. Another name
    # loaded and then read the input as that codec, skipped valid utf-16
    # lines, crashed (hex) or made the input unreadable (an unknown name).
    if isinstance(inp, dict) and "encoding" in inp and inp["encoding"] != "utf-8":
        out.append(f"input.encoding: utf-8 only, not {inp['encoding']!r} (§4)")
    # Ruled 2026-10-19, refuse at load, as the schema: these are mappings
    # wherever present. A list or another non-mapping passed load and lint,
    # and then crashed the build on any input (TypeError, AttributeError) or
    # crashed a load check (a list finalize).
    for key, where in (("state", "§5"), ("constants", "§6"), ("finalize", "§9")):
        if key in dsl and not isinstance(dsl[key], dict):
            out.append(f"{key}: a mapping ({where}), not {_json_type(dsl[key])}")
    fin = dsl.get("finalize")
    if isinstance(fin, dict) and "computed" in fin and not isinstance(fin["computed"], dict):
        out.append(f"finalize.computed: a mapping (§9), not {_json_type(fin['computed'])}")
    if isinstance(inp, dict) and "container" in inp and not isinstance(inp["container"], dict):
        out.append(f"input.container: a mapping (§4.1), not {_json_type(inp['container'])}")
    elif isinstance(inp, dict) and "container" in inp:
        # Ruled 2026-10-19, as the schema: record_path
        # is required and a string, and it is the container's only key. A
        # record_path of another type, or none, crashed run() on any input;
        # an empty container converted nothing, unreported.
        cont = inp["container"]
        if "record_path" not in cont:
            out.append("input.container.record_path: absent; it is required (§4.1)")
        elif not isinstance(cont["record_path"], str):
            out.append(f"input.container.record_path: a string (§4.1), not {_json_type(cont['record_path'])}")
        for k in sorted(set(cont) - {"record_path"}, key=str):
            out.append(f"input.container: unknown key {k!r} (§4.1)")
    shapes = [("session", dsl["session"])] if isinstance(dsl.get("session"), dict) else []
    if isinstance(dsl.get("sessions"), dict):
        shapes += [(f"sessions.{k}", v) for k, v in dsl["sessions"].items() if isinstance(v, dict)]
    for where, shape in shapes:
        for m in ("static", "derived", "passthrough"):
            if m in shape and not isinstance(shape[m], dict):
                out.append(f"{where}.{m}: a mapping (§7), not {_json_type(shape[m])}")
    # Ruled 2026-10-17, refuse, as the schema: the adapter
    # block takes the six keys of §3 only; an unknown one (a misspelt
    # harness_version) was ignored.
    ad = dsl.get("adapter")
    for k in sorted(set(ad) - _ADAPTER_KEYS, key=str) if isinstance(ad, dict) else ():
        out.append(f"adapter: unknown key {k!r} (§3)")
    # A key that is present is checked whatever its value: null (YAML's bare
    # `do:`) is not a list or a mapping either.
    ops = dsl.get("operation_types")
    if "operation_types" in dsl and not isinstance(ops, dict):
        out.append("operation_types: a mapping of tool name to operation type (§10)")
    for k, v in ops.items() if isinstance(ops, dict) else ():
        if v not in _OPERATION_TYPES:   # a tuple: == per item, so a map or list is simply not in it
            out.append(f"operation_types.{k}: {v!r} is not one of {', '.join(_OPERATION_TYPES)} (§10)")

    def rules(rs: Any, where: str, keys: frozenset, in_block: bool) -> None:
        if not isinstance(rs, list):
            out.append(f"{where}: a list of rules (§8)")
            return
        for i, r in enumerate(rs):
            if not isinstance(r, dict):
                out.append(f"{where}[{i}]: a rule is a mapping (§8)")
                continue
            name = r.get("name")
            here = f"{where}.{name}" if isinstance(name, str) else f"{where}[{i}]"
            for k in sorted(set(r) - keys, key=str):
                out.append(f"{here}: unknown key {k!r} in a rule (§15)")
            for k in ("name", "target"):   # as the schema
                if k in r and not isinstance(r[k], str):
                    out.append(f"{here}.{k}: a string (§8)")
            if "when" in r:
                out.extend(predicate_problems(r["when"], f"{here}.when", in_block))
            else:   # ruled 2026-10-17: it never fired, a warning only
                out.append(f"{here}: no when; a rule without one never fires (§8)")
            fb = r.get("foreach_block")
            if not in_block and "foreach_block" in r and not (fb is None or isinstance(fb, str)):
                # DSL rc3 §8 and §15 (D3, ruled 2026-10-18): a
                # literal that is not an expression; it loaded and failed on
                # every record at run time.
                out.append(f"{here}.foreach_block: {_json_type(fb)}, not an expression or null (§8)")
            if not in_block and ("foreach_block" in r) != ("on_block" in r):
                # Ruled 2026-10-17: one without the other
                # iterated for nothing or was ignored. A literal null
                # foreach_block beside an on_block stays valid (§8: null
                # iterates nothing).
                have, lack = ("foreach_block", "on_block") if "foreach_block" in r else ("on_block", "foreach_block")
                out.append(f"{here}: {have} without {lack}; the two go together (§8)")
            # Ruled 2026-10-18, refuse at load, as the
            # schema: a rule with when only did nothing; a sub-rule without
            # do swallowed its block, since the first match wins.
            # An on_block without foreach_block is reported above; its fix is
            # the foreach_block, not a do.
            if not in_block and "do" not in r and "foreach_block" not in r and "on_block" not in r:
                out.append(f"{here}: neither do nor foreach_block; the rule does nothing (§8)")
            if in_block and "do" not in r:
                out.append(f"{here}: no do; an on_block sub-rule needs one, do: [] to skip its blocks (§8)")
            do = r.get("do")
            if "do" in r and not isinstance(do, list):
                out.append(f"{here}.do: a list of actions (§8.2)")
            for j, a in enumerate(do if isinstance(do, list) else ()):
                at = f"{here}.do[{j}]"
                if not isinstance(a, dict):
                    out.append(f"{at}: an action is a mapping (§8.2)")
                    continue
                named = [k for k in a if k in _ACTIONS]
                if not named:
                    out.append(f"{at}: unknown action {sorted(map(str, a))} (§15)")
                    continue
                allowed = {named[0], "omit_if_none"} if named == ["emit_turn"] else {named[0]}
                for k in sorted(set(a) - allowed, key=str):
                    out.append(f"{at}: unknown key {k!r} beside the action {named[0]} (§15)")
                body = a[named[0]]
                if named[0] == "set" and isinstance(body, dict):
                    # Ruled 2026-10-17: a set key under
                    # neither state. nor session. was written into the trace.
                    for k in body:
                        if not (isinstance(k, str) and k.startswith(("state.", "session."))):
                            out.append(f"{at}.set: key {k!r} is under neither state. nor session. (§8.2.1)")
                if not isinstance(body, dict):
                    out.append(f"{at}.{named[0]}: the action body is a mapping (§8.2)")
                elif named[0] in _BODY_KEYS:   # the two bodies with fixed keys, as the schema
                    for k in sorted(set(body) - _BODY_KEYS[named[0]], key=str):
                        out.append(f"{at}.{named[0]}: unknown key {k!r} (§8.2)")
                    if named[0] == "attach_tool_result":
                        if "target_id" not in body:
                            out.append(f"{at}.attach_tool_result: target_id is required (§8.2.4)")
                        if "output" in body and not isinstance(body["output"], dict):
                            out.append(f"{at}.attach_tool_result.output: a mapping (§8.2.4)")
                    elif "level" in body and not isinstance(body["level"], str):
                        out.append(f"{at}.note.level: a string (§8.2.6)")
                omit = a.get("omit_if_none")
                if "omit_if_none" in a and "omit_if_none" in allowed and not (
                        isinstance(omit, list) and all(isinstance(x, str) for x in omit)):
                    out.append(f"{at}.omit_if_none: a list of field names (§8.2.2)")
            if not in_block and "on_block" in r:
                rules(r.get("on_block"), f"{here}.on_block", _BLOCK_RULE_KEYS, True)
    rules(dsl.get("emit"), "emit", _RULE_KEYS, False)   # absent too: run() reads dsl["emit"]
    # DSL rc3 §14.3, ruled 2026-10-17: in multi-trace mode
    # every emit rule names a declared shape in target; a rule without one,
    # or naming a shape not under sessions, was dropped and never fired. A
    # target that is not a string is reported above.
    shapes = dsl.get("sessions")
    for i, r in enumerate(dsl.get("emit") or ()) if isinstance(shapes, dict) and isinstance(dsl.get("emit"), list) else ():
        if not isinstance(r, dict):
            continue
        name = r.get("name")
        here = f"emit.{name}" if isinstance(name, str) else f"emit[{i}]"
        if "target" not in r:
            out.append(f"{here}: no target; in multi-trace mode every rule names its trace shape (§14.3)")
        elif isinstance(r["target"], str) and r["target"] not in shapes:
            out.append(f"{here}.target: {r['target']!r} is not a shape declared under sessions "
                       f"({', '.join(sorted(map(str, shapes)))}) (§14.3)")
    out.extend(_literal_path_problems(dsl))
    return out


def _literal_path_problems(dsl: dict) -> list[str]:
    """Ruled 2026-10-17, refuse at load: a static or passthrough
    value is literal, so a key of the same shape's static, derived or
    passthrough map, or of finalize.computed, that lies beneath one that is
    not an object would run through it, whichever is written first. A
    literal object counts by its own keys; null is a parent that is
    created, as an absent one is."""
    out: list[str] = []
    if isinstance(dsl.get("sessions"), dict):
        shapes = [(f"sessions.{n}", s) for n, s in dsl["sessions"].items()]
    else:   # a sessions key that is not a mapping is reported elsewhere
        shapes = [] if "sessions" in dsl else [("session", dsl.get("session"))]
    computed = (dsl.get("finalize") or {}).get("computed") if isinstance(dsl.get("finalize"), dict) else None
    for name, s in shapes:
        if not isinstance(s, dict):
            continue
        maps = [(f"{name}.{m}", s.get(m)) for m in ("static", "derived", "passthrough")] \
            + [("finalize.computed", computed)]
        keys = [(where, k) for where, kv in maps if isinstance(kv, dict) for k in kv if isinstance(k, str)]
        # Paths as segments: a map key is split on its dots, as deep_set
        # splits it, but a key inside a literal object is one segment as
        # written ({"a": {"b.c": 1}} holds no a.b, and
        # an int key 2 is not the segment "2").
        blocking: list[tuple[str, tuple, Any]] = []   # (where, literal path, value)

        def walk(where: str, segs: tuple, v: Any) -> None:
            if isinstance(v, dict):
                for k, sub in v.items():
                    walk(where, segs + (k,), sub)
            elif v is not None:
                blocking.append((where, segs, v))
        for where, kv in maps[0:1] + maps[2:3]:   # static, passthrough
            for k, v in (kv.items() if isinstance(kv, dict) else ()):
                if isinstance(k, str):
                    walk(where, tuple(k.split(".")), v)
        for bw, bp, bv in blocking:
            for where, k in keys:
                ks = tuple(k.split("."))
                if len(ks) > len(bp) and ks[:len(bp)] == bp:
                    out.append(f"{where}.{k}: runs through {bw}.{'.'.join(map(str, bp))}, which holds "
                               f"{_json_type(bv)}, not an object (§7)")
    return out


def dsl_lineage_problems(dsl: Any) -> list[str]:
    """DSL spec rc3 §13.1, rejected at load: lineage comes only from
    emit_lineage, and its target_content_hash is absent, the null literal, or
    one whole `${trace:<trace-id template>:content_hash}` template. Any other
    value (an expression, a literal hash, a template with text outside the
    form, another field name) would let source data decide whether a field
    holds a placeholder or which hash it binds; any other write to lineage
    (session static, derived or passthrough, finalize.computed, a set to
    session.lineage) would carry such a value past emit_lineage."""
    out = [f"{w}: lineage is written only by emit_lineage" for w in _direct_write_sites(dsl, "lineage")]
    if not isinstance(dsl, dict):
        return out

    def rules(rs: Any, where: str) -> None:
        for r in rs or []:
            if not isinstance(r, dict):
                continue
            here = f"{where}.{r.get('name', '?')}"
            for a in r.get("do") or []:
                if not isinstance(a, dict):
                    continue
                body = a.get("emit_lineage")
                if isinstance(body, dict) and "target_content_hash" in body:
                    v = body["target_content_hash"]
                    if v is None:
                        continue
                    if not (isinstance(v, str) and v.startswith("${trace:") and _is_whole_template(v)
                            and v[2:-1][_last_top_level_colon(v[2:-1]):] == ":content_hash"):
                        out.append(f"{here}.emit_lineage.target_content_hash: must be absent, null, or "
                                   f"a whole ${{trace:<trace-id>:content_hash}} template, got {str(v)[:120]!r}")
            rules(r.get("on_block"), f"{here}.on_block")
    rules(dsl.get("emit"), "emit")
    return out


def predicate_matches(pred: dict, record: dict, block: dict | None = None,
                      container: dict | None = None) -> bool:
    if "all_of" in pred:
        return all(predicate_matches(p, record, block, container) for p in pred["all_of"])
    if "any_of" in pred:
        return any(predicate_matches(p, record, block, container) for p in pred["any_of"])
    if "has_any" in pred and pred["has_any"]:
        return True
    if "has" in pred:
        # Input decides a record's type; a non-object never "has" a key.
        return isinstance(record, dict) and pred["has"] in record
    if "block_type" in pred and block is not None:
        return isinstance(block, dict) and json_equal(block.get("type"), pred["block_type"])
    if "field" in pred and "value" in pred:
        return json_equal(deep_get(record, pred["field"]), pred["value"])
    if "field" in pred and "present" in pred:
        actual = deep_get(record, pred["field"])
        return (actual is not None) == bool(pred["present"])
    if "container_field" in pred:
        # A predicate scoped to the
        # parent container, used for conditional emission based on labels
        # or other top-level fields (e.g. defense_type non-null).
        if container is None:
            return False
        actual = deep_get(container, pred["container_field"])
        if "present" in pred:
            return (actual is not None) == bool(pred["present"])
        if "value" in pred:
            return json_equal(actual, pred["value"])
        return False
    if "block_field" in pred:
        # Predicate scoped to the current block (inside foreach_block /
        # on_block). Symmetric to `field:` for records and `container_field:`
        # for containers. Required when foreach iterates over synthesised
        # blocks (e.g. dict_items output) where the discriminator is in the
        # block payload, not in record.type.
        if block is None:
            return False
        actual = deep_get(block, pred["block_field"])
        if "present" in pred:
            return (actual is not None) == bool(pred["present"])
        if "value" in pred:
            return json_equal(actual, pred["value"])
        return False
    return False


# ---------------------------------------------------------------------------
# Top-level run

def _record_in_container(rp: Any, path: str) -> int | None:
    """The record_path list position a reader finding's path lies in
    ($self: the container itself, record 0), or None for a value outside
    every record."""
    if rp == "$self":
        return 0
    if not isinstance(rp, str):
        return None
    prefix = "/" + "/".join(x.replace("~", "~0").replace("/", "~1") for x in rp.split(".")) + "/"
    head = path[len(prefix):].split("/", 1)[0] if path.startswith(prefix) else ""
    # As record_path_get reads an index; no list has 19 digits of items,
    # and the bound keeps int() off a key past its digit limit.
    return int(head) if _LIST_INDEX.fullmatch(head) and len(head) <= 18 else None


def _reader_order_key(container_cfg: dict | None):
    """DSL rc3 §4: the input order of a
    reader finding, for the summary's first: the line or element, then (in
    container mode) the record_path list position. A finding with neither
    (the input as a whole, a container apart from its records) sorts before
    the positioned ones of its line, as _report_item_skips compares; the
    spec fixes no order for them."""
    def key(f: dict) -> tuple:
        ri = f.get("record_index")
        rec = _record_in_container(container_cfg.get("record_path"), str(f.get("path") or "")) \
            if container_cfg else None
        return (-1 if ri is None else ri, -1 if rec is None else rec)
    return key


def _input_place(d: dict, container_cfg: dict | None, containers: dict | None = None) -> str:
    """Name the input of a reader or guard finding (DSL rc3 §4, spec
    5fc0192d; ruled 2026-10-06): in container mode the reader's
    record_index is the container (a top-level element), so it becomes
    container_index, and record_index becomes the record under the
    container's record_path the path lies in ($self: the container itself,
    record 0), or None for a value outside every record. A finding with no
    record_index is a whole-document one (a decode error or duplicate key
    in a json input): it lies in no record and no container, so it is
    "the input file" (container_index None in container mode, as every
    container-mode finding carries the key). Returns the place for the
    message; outside container mode d is otherwise unchanged."""
    if d.get("record_index") is None:
        if container_cfg:
            d["container_index"] = None
        return "the input file"
    if not container_cfg:
        return f"input record {d.get('record_index')}"
    ci = d["record_index"]
    rp = container_cfg.get("record_path")
    rec = _record_in_container(rp, str(d.get("path") or ""))
    # A container whose record_path value is no list yields no
    # records, so a finding under key "0" of an object there lies in
    # no record; the path alone could not tell.
    if rec is not None and rp != "$self" and ci in (containers or {}) \
            and not isinstance(record_path_get(containers[ci], rp), list):
        rec = None
    d["container_index"], d["record_index"] = ci, rec
    return (f"input record {rec}, container {ci}" if rec is not None else f"input container {ci}")


def _report_item_skips(skips: list[tuple], diags: list[dict], n_reader: int) -> None:
    """DSL rc3 §4.1.1, §15; ruled 2026-10-10: report each item
    of a record_path list skipped for not being an object, as the reader
    reports a top-level element it skips (input-parse-error, not-an-object,
    set-level), naming the record by its list position and its container.
    The skips count toward the §4 reporting bound together with the
    reader's findings, which are the first `n_reader` entries of `diags`:
    reported one by one while the source's 1,000 are not used up, the rest
    in the reader's summary (its count raised, now set-level) or, when the
    reader had none, in a new one naming their number and the first."""
    if not skips:
        return
    from source_reader import DEFAULT_LIMITS  # noqa: PLC0415
    reader = diags[:n_reader]
    summary = next((d for d in reader if "suppressed_count" in d), None)
    listed = sum(1 for d in reader if str(d.get("kind")).startswith("input-") and "suppressed_count" not in d
                 and d.get("kind") not in ("input-failure", "input-empty"))
    budget = 0 if summary is not None else max(0, DEFAULT_LIMITS.max_findings - listed)
    out = [{"level": "warning", "kind": "input-parse-error", "loss": "set", "record_index": i,
            "container_index": ci, "reason": "not-an-object", "path": None,
            "message": f"input record {i}, container {ci}: parse-error (not-an-object): "
                       f"{_json_type(item)} in the record_path list, skipped as unreadable"}
           for ci, i, item in skips[:budget]]
    rest = skips[budget:]
    if rest and summary is not None:
        summary["suppressed_count"] += len(rest)
        summary["loss"] = "set"
        # DSL rc3 §4, spec e03c3454; ruled 2026-10-10: the summary
        # names the earliest input in input order among all it stands for,
        # not the reader's first unreported finding. A folded skip earlier
        # than that (container, then list position) becomes the first; an
        # input with no position ("the input file") is left as it is (the
        # spec fixes no order for it).
        ci, i, _ = rest[0]
        rci, rrec = summary.get("container_index"), summary.get("record_index")
        if rci is not None and (ci, i) < (rci, -1 if rrec is None else rrec):
            summary.update(record_index=i, container_index=ci, path=None)
            summary["message"] = (f"{summary['suppressed_count']} further input finding(s) not listed, "
                                  f"the first at record {i}, container {ci}; at least one skipped a record")
        else:
            head, _, tail = summary["message"].partition(" ")
            summary["message"] = (f"{summary['suppressed_count']} {tail}"
                                  + ("" if tail.endswith("; at least one skipped a record")
                                     else "; at least one skipped a record"))
    elif rest:
        ci, i, _ = rest[0]
        out.append({"level": "warning", "kind": "input-findings-truncated", "loss": "set",
                    "record_index": i, "container_index": ci, "reason": None, "path": None,
                    "suppressed_count": len(rest),
                    "message": f"{len(rest)} further input finding(s) not listed, the first at "
                               f"record {i}, container {ci}; at least one skipped a record"})
    diags[n_reader:n_reader] = out


def load_input(input_cfg: dict, path: pathlib.Path,
               diagnostics: list | None = None,
               positions: list | None = None) -> list[list[dict]] | list[dict]:
    """Read input file. Returns:
       - list[dict] of records (single-trace mode, no container).
       - list[list[dict]] where each inner list is one container's records (multi-trace mode).
    Reader findings (a record skipped for depth, a damaged line, a repaired
    encoding) are appended to `diagnostics` when given: a skipped record is
    an input failure (§4, §8.3) and belongs in the conversion's diagnostics,
    not only on stderr.
    """
    # Through the shared reader: LF/CR framing (not str.splitlines(), which
    # also splits inside strings at U+2028/U+2029/U+0085), damaged lines
    # skipped and reported, encoding damage repaired, duplicate keys recorded.
    from source_reader import (  # noqa: PLC0415
        SourceUnreadable, read_json, read_jsonl_records, repair_strings)
    encoding = input_cfg.get("encoding", "utf-8")
    findings: list[tuple[dict, str]] = []
    # In container mode the input position of each kept record,
    # also when the caller passes no list, so a finding can be matched to
    # its container.
    if positions is None and input_cfg.get("container"):
        positions = []
    if input_cfg["type"] == "jsonl":
        try:
            records = read_jsonl_records(path, encoding=encoding, report_out=findings, indices_out=positions,
                                         order_key=_reader_order_key(input_cfg.get("container")))
        except SourceUnreadable as e:   # no file to read
            raise InputUnreadable(f"input unreadable: {e}") from None
    elif input_cfg["type"] == "json":
        try:
            # §15, spec 9c801194: an element of the root array, or a
            # root that is no array, that is not an object is skipped as
            # unreadable (set-level), in both modes, as a jsonl line is.
            src = read_json(path, encoding=encoding, require_object=True,
                            order_key=_reader_order_key(input_cfg.get("container")))
        except SourceUnreadable as e:
            raise InputUnreadable(f"input unreadable: {e}") from None
        if input_cfg.get("container") and src.root_kind != "array":
            # Ruled 2026-10-09: §4.1 container input MUST be an array;
            # any other root is the input unreadable, as malformed JSON is.
            article = "" if src.root_kind == "null" else "an " if src.root_kind[0] in "ao" else "a "
            raise InputUnreadable(f"input unreadable: {path}: container input is {article}{src.root_kind}, "
                                  f"not an array (§4.1)")
        findings = src.findings_with_loss()
        if findings:
            print(f"warning: {str(path)!r}: {len(findings)} input finding(s) "
                  f"({', '.join(sorted({str(f.get('reason')) for f, _ in findings}))})",
                  file=sys.stderr)
        records = src.records
        if positions is not None:
            positions.extend(src.record_indices)   # input position of each kept record
    else:
        raise ValueError(f"unsupported input type: {input_cfg['type']}")
    if diagnostics is not None:
        # The reader caps its findings per source (with a truncation entry) and
        # gives each its loss class: "set" for a skipped record, and for the
        # cap entry only when a suppressed finding was a skip (§15).
        container_cfg = input_cfg.get("container")
        containers = dict(zip(positions, records)) if container_cfg else {}
        for f, loss in findings:
            d = {"level": "warning", "kind": f"input-{f.get('kind')}",
                 "loss": loss,
                 "record_index": f.get("record_index"), "reason": f.get("reason"),
                 "path": f.get("path")}
            # A decode error the reader found in the bytes carries the real
            # file offset (ruled 2026-10-10: keep real offsets).
            # A string repair carries a path and only the offset of its record
            # or line (read_json passes 0), so it never shows one;
            # a duplicate key is only counted, so has none.
            at_byte = f.get("kind") == "decode-error" and "path" not in f \
                and isinstance(f.get("byte_offset"), int)
            place = _input_place(d, container_cfg, containers)
            if at_byte:
                d["byte_offset"] = f["byte_offset"]
            d["message"] = (f"{place}: {f.get('kind')} ({f.get('reason')})"
                            + (f" at {str(f.get('path'))[:200]}" if f.get("path") else "")
                            + (f" at byte {f['byte_offset']}" if at_byte else "")
                            + _repair_note(f))
            for k in ("count", "member_name"):
                # §4 "Strings in the input": which part was repaired, how often.
                if k in f:
                    d[k] = f[k]
            if "suppressed_count" in f:
                # §4 summary: it names the count and the first affected record.
                d["suppressed_count"] = f["suppressed_count"]
                d["message"] = (f"{f['suppressed_count']} further input finding(s) not listed, "
                                f"the first at {place.removeprefix('input ')}"
                                + ("; at least one skipped a record" if loss == "set" else ""))
            diagnostics.append(d)
    # The readers repair strings per record and report each repair (§4
    # "Strings in the input"); this pass is a guard for any record that
    # reached here another way, and reports what it repairs the same way.
    # Pointers here start at the records list; its index i is the reader's
    # input position positions[i] when known (skipped records counted), so
    # the report names the record (for container input, the container) as
    # the reader does.
    records, repairs, collision_paths = repair_strings(records)
    if diagnostics is not None:
        for r in repairs + [{"path": c, "reason": "repair-key-collision"} for c in collision_paths]:
            head, _, rest = r["path"].lstrip("/").partition("/")
            i = int(head) if head.isdigit() else None
            d = {"level": "warning", "kind": "input-failure", "loss": "value",
                 "record_index": positions[i] if i is not None and positions and i < len(positions) else i,
                 "reason": r["reason"], "path": "/" + rest,
                 **{k: r[k] for k in ("count", "member_name") if k in r}}
            d["message"] = (f"{_input_place(d, input_cfg.get('container'), containers)}: {r['reason']} at /{rest[:200]}"
                            + _repair_note(d))
            diagnostics.append(d)
    return records


def _repair_note(f: dict) -> str:
    """The §4 "Strings in the input" detail of a repair report: whether the
    member name or its value was repaired, and how many characters."""
    if "count" not in f:
        return ""
    return (f"; {'member name' if f.get('member_name') else 'value'}: "
            f"{f['count']} character(s) replaced with U+FFFD")


def _remove_shared_tool_call_ids(trace: dict, diagnostics: list[dict]) -> None:
    """DSL rc3 §8.2.3 (ruled 2026-10-20): once a trace is built, before
    finalize, each id held by two or more emitted tool calls is reported once
    and none of those calls is kept, nor the results attached to them (a
    value-level loss, §15), as R5 does for traces. Ids are compared as
    emitted, after rendering, so a source 1 and "1" share one. $state is not
    changed, and a reference to a removed call elsewhere is not rewritten.
    Every tool_calls entry is an emitted call (others are refused at load)."""
    counts: dict[str, int] = {}
    for tc in trace["tool_calls"]:
        counts[tc["id"]] = counts.get(tc["id"], 0) + 1
    shared = [i for i, n in counts.items() if n > 1]
    if not shared:
        return
    # Named by trace, as the other trace-level reports are: two traces of
    # one container can each hold the id.
    tid = str(trace.get("id"))[:80]
    for i in shared:
        diagnostics.append(_input_failure(
            "emit_tool_call.id", _NO_EXPRESSION,
            f"emit_tool_call.id: in trace {tid!r}, tool-call id {i[:60]!r} is held by {counts[i]} emitted tool calls; "
            f"none of them is kept, nor any result attached to them (§8.2.3)", loss="value"))
    trace["tool_calls"] = [tc for tc in trace["tool_calls"] if counts[tc["id"]] == 1]


def _build_trace(dsl: dict, records: list[dict], container: dict | None,
                 container_first: dict | None,
                 operator_override: dict | None = None,
                 container_index: int = 0,
                 container_position: int | None = None,
                 record_positions: list[int] | None = None) -> tuple[dict, list[dict]]:
    """Build one trace from one container's records.

    operator_override (run() always provides it) replaces trace.operator AFTER all DSL
    processing, enforcing DSL spec §7.1: consent-claim fields are runtime-
    supplied, not adapter-declared.

    container_index is the 0-based index of the container in the input array;
    available to DSL expressions as `container_index` for synthesising unique
    ids when the input format does not carry a per-record id.
    """
    state = json.loads(json.dumps(dsl.get("state", {})))  # deep copy
    constants = dsl.get("constants", {})
    op_types = dsl.get("operation_types", {})
    diagnostics: list[dict] = []
    trace: dict = {}
    # Input failures (§8.3) are reported here, with record and field.
    diag_reset = _EVAL_DIAGNOSTICS.set(diagnostics)
    site_reset = _EVAL_SITE.set({"record_index": None, "field": None})
    try:
        # Reports name a container by its input position (as the reader does,
        # skipped containers counted); $container_index stays the kept-only
        # index.
        report_ci = container_index if container_position is None else container_position
        return _build_trace_body(dsl, records, container, container_first, operator_override,
                                 container_index, state, constants, op_types, diagnostics, trace,
                                 report_ci, record_positions)
    finally:
        _EVAL_DIAGNOSTICS.reset(diag_reset)
        _EVAL_SITE.reset(site_reset)


def _build_trace_body(dsl, records, container, container_first, operator_override,
                      container_index, state, constants, op_types, diagnostics, trace, report_ci,
                      record_positions=None):
    # A record is named by its record_path list position (skipped
    # items counted), not by its place among the kept records.
    rpos = record_positions if record_positions is not None else range(len(records))
    # Apply session.static and session.passthrough verbatim.
    # Ruled 2026-10-18: passthrough is written before static, so a static
    # key beneath a passthrough object wins inside it (the object replaced
    # it, unreported); derived is written last, below. Static is written
    # once more before passthrough only so the trace keeps its key order
    # (static keys first); the values are those of passthrough-then-static.
    # Copied, as evaluate() copies a map or list literal: the
    # adapter's own objects were shared by every trace built from it.
    static = dsl["session"].get("static") or {}
    for path_, value in static.items():
        deep_set(trace, path_, _json_copy(value))
    for key, value in (dsl["session"].get("passthrough") or {}).items():
        deep_set(trace, key, _json_copy(value))
    for path_, value in static.items():
        deep_set(trace, path_, _json_copy(value))

    # Default arrays/objects required by the schema.
    for k in ("turns", "tool_calls", "segment_markers", "policy_triggers",
              "annotations", "lineage", "redactions"):
        trace.setdefault(k, [])
    for k in ("metrics", "flags", "environment", "operational_context",
              "timing", "provenance", "operator"):
        trace.setdefault(k, {})

    base_ctx = {
        "record_first": records[0] if records else {},
        "container": container or {},
        "container_first": container_first or {},
        "container_index": container_index,
        "adapter": dsl["adapter"],
        "constants": constants,
        "state": state,
        "trace": trace,
    }
    for path_, expr in (dsl["session"].get("derived") or {}).items():
        # Ruled 2026-10-05: a report names what the field read, the
        # trace's first record ($record_first) and, in container mode, the
        # container ($container; $container_first is container 0).
        _set_site(field=f"session.derived.{path_}", **_read_site(expr, records, container, report_ci, first_position=rpos[0] if records else 0))
        # Ruled 2026-10-17, as for set: a path through a
        # non-object is an input failure, the path first; it was
        # replaced by an object, unreported.
        if blocked := _blocking_parent(trace, path_):
            diagnostics.append(_blocked_write("session.derived", path_, expr, 0, blocked))
            continue
        deep_set_blocked(trace, path_, evaluate(expr, base_ctx, id_field=(path_ == "id")))
    _set_site(container_index=None, inputs=None)

    for rec_index, rec in enumerate(records):
        # In container mode a record-level report also names its container
        # (ruled 2026-10-06): the trace's record N alone did not tell two
        # containers apart.
        _set_site(record_index=rpos[rec_index], container_index=report_ci if container is not None else None,
                  inputs=None)
        ctx = {
            "record": rec,
            "record_first": records[0] if records else {},
            "container": container or {},
            "container_first": container_first or {},
            "container_index": container_index,
            "state": state,
            "adapter": dsl["adapter"],
            "constants": constants,
            "trace": trace,
        }
        for rule in dsl["emit"]:
            if not predicate_matches(rule.get("when", {}), rec, container=container):
                continue
            _apply_actions(rule.get("do") or [], ctx, trace, state, op_types, diagnostics)
            if "foreach_block" in rule:
                _set_site(field=f"{rule.get('name', '?')}.foreach_block")
                blocks = evaluate(rule["foreach_block"], ctx)
                if blocks is not None and not isinstance(blocks, list):
                    # A string or object where a list was expected is an input
                    # failure (§8.3): iterating it walked characters or keys,
                    # and an int aborted the whole run.
                    diagnostics.append(_input_failure(
                        f"{rule.get('name', '?')}.foreach_block", rule["foreach_block"],
                        f"foreach_block source is a {type(blocks).__name__}, not a list; "
                        f"the record is skipped for this rule"))
                    blocks = None
                # Iterate a snapshot: an on_block action that appends to the
                # same state list would otherwise extend the loop forever.
                for blk in list(blocks or []):
                    block_ctx = {**ctx, "block": blk}
                    for sub_rule in rule.get("on_block") or []:
                        if predicate_matches(sub_rule.get("when", {}), rec, blk, container=container):
                            _apply_actions(sub_rule.get("do") or [], block_ctx, trace, state, op_types, diagnostics)
                            break
            # No break: rules are ordered actions, not pattern-match dispatch.
            # Multiple rules can match the same record (e.g. an unconditional
            # judge_emit_per_record rule + a conditional defense_type rule both
            # matching session_meta). Existing adapters use mutually-exclusive
            # when:-clauses, so this is non-regressive for them.

    _set_site(record_index=None, container_index=report_ci if container is not None else None, inputs=None)
    _remove_shared_tool_call_ids(trace, diagnostics)

    final_ctx = {
        "state": state,
        "adapter": dsl["adapter"],
        "constants": constants,
        "trace": trace,
        "container": container or {},
    }
    _set_site(record_index=None)
    for path_, expr in ((dsl.get("finalize") or {}).get("computed") or {}).items():
        # finalize binds $container too (no record is bound).
        _set_site(field=f"finalize.computed.{path_}", **_read_site(expr, [], container, report_ci, bound=("container",)))
        if blocked := _blocking_parent(trace, path_):   # as session.derived above
            diagnostics.append(_blocked_write("finalize.computed", path_, expr, 0, blocked))
            continue
        deep_set_blocked(trace, path_, evaluate(expr, final_ctx, id_field=(path_ == "id")))
    _set_site(container_index=None, inputs=None)

    # DSL spec §7.1: the runtime-supplied operator replaces the whole object.
    # An adapter that writes at or beneath operator is refused at load
    # and run() refuses a call without an operator, so the end-of-build
    # check on four fields that stood here was unreachable (ruled 2026-10-17).
    if operator_override is not None:
        trace["operator"] = operator_override

    return trace, diagnostics


def _shape_views(dsl: dict) -> list[tuple[str, dict]]:
    """Return list of (shape_name, dsl_view) for each declared trace shape.

    Single-trace mode: dsl["session"] (singular). Returns one view named "_default".
    Multi-trace mode: dsl["sessions"] (plural). Returns one view per shape with
    emit rules filtered by `target:` field per Y-α-q1 (explicit-target required
    when plural).
    """
    if "session" in dsl and "sessions" in dsl:
        raise SystemExit(
            "ERROR: adapter has both `session:` (singular) and `sessions:` (plural). "
            "Use one or the other; per DSL spec §Y, `session:` is single-trace and "
            "`sessions:` is multi-trace."
        )
    if "sessions" in dsl:
        shapes = dsl["sessions"]
        if not isinstance(shapes, dict) or not shapes:
            raise SystemExit(
                "ERROR: `sessions:` must be a non-empty mapping of shape-name to shape-config."
            )
        views = []
        for shape_name, shape_cfg in shapes.items():
            filtered_emit = [
                r for r in dsl.get("emit") or []
                if r.get("target") == shape_name
            ]
            view = {**dsl, "session": shape_cfg, "emit": filtered_emit}
            view.pop("sessions", None)
            views.append((shape_name, view))
        return views
    # Single-trace path
    return [("_default", dsl)]


def _truncate_too_deep(traces: list[dict], diagnostics: list[dict],
                       containers: list | None = None) -> list[dict]:
    """§4: in every built trace, before cross-trace resolution, each array or
    object at level 64 is replaced by null and reported as an input failure
    naming the trace and the path. No trace is dropped for its depth, so
    every emitted trace has depth at most 64 (minitrace-spec §3.4)."""
    from source_reader import MAX_DEPTH, truncate_deep  # noqa: PLC0415
    for i, t in enumerate(traces):
        # In container mode the trace's container is named too (spec 5fc0192d).
        ci = containers[i] if containers is not None else None
        for pointer in truncate_deep(t, MAX_DEPTH):
            diagnostics.append({"level": "warning", "kind": "input-failure", "field": pointer,
                                **({"container_index": ci} if ci is not None else {}),
                                "message": f"trace {str(t.get('id'))[:80]!r}"
                                           + (f" (container {ci})" if ci is not None else "")
                                           + f": value at {pointer[:200]} "
                                           f"nested deeper than {MAX_DEPTH}; replaced with null"})
    return traces


# The numeric fields of the v0.3.0 base schema (the agent-session and
# judge-session schemas add none), by type, whether each admits null, and its
# inclusive bounds (None: unbounded, ruled 2026-10-05, option i).
# test_fuzz_regressions pins this table to the schema file.
_METRIC_NUMBERS = {
    **{k: ("integer", False, 0, None) for k in (
        "turn_count", "tool_call_count", "read_count", "modify_count", "create_count",
        "execute_count", "delegate_count", "subagent_count", "subagent_tool_calls")},
    **{k: ("number", True, None, None) for k in (
        "read_ratio", "time_to_first_action", "idle_ratio", "session_cost")},
    **{k: ("integer", True, 0, None) for k in (
        "total_input_tokens", "total_output_tokens", "total_cache_read_tokens",
        "total_cache_creation_tokens", "total_reasoning_tokens", "total_tool_tokens",
        "model_switches", "unique_models", "median_response_tokens", "max_response_tokens")},
}
_ITEM_NUMBERS = {   # every one admits null
    # Ruled 2026-10-10: the base schema types ordering on turns
    # and tool calls too, so DSL §4 (l.113-116) reaches them.
    "turns": {"ordering": ("integer", None, None)},
    "tool_calls": {"emitting_turn_index": ("integer", 0, None), "ordering": ("integer", None, None)},
    "segment_markers": {"ordering": ("integer", None, None)},
    "policy_triggers": {"threshold_proximity": ("number", 0, 1), "ordering": ("integer", None, None)},
    "annotations": {"near_miss_proximity": ("number", 0, 1)},
}


def _admits(v: Any, typ: str, lo: int | None = None, hi: int | None = None) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if not (v == v and -_MAX_SAFE_INT <= v <= _MAX_SAFE_INT):
        return False
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return False
    return typ == "number" or v == int(v)


def _is_whole_number(v: Any) -> bool:
    """An integer by value (minitrace-spec rc3 §3.4, T1, ruled 2026-10-05):
    2, 2.0 and 2e0 are all the integer 2; a boolean is never a number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return isinstance(v, int) or (v == v and abs(v) != float("inf") and v == int(v))


def _enforce_schema_numbers(traces: list[dict], diagnostics: list[dict],
                            index_dropped: list | None = None) -> list[dict]:
    """DSL spec §4 (F2, ruled 2026-10-04; bounds ruled 2026-10-05): a value a numeric
    schema field cannot hold (not a number of its type, outside the §3.4
    domain or the schema's bounds), such as a number the reader kept as its
    source text or a negative count, never reaches the trace. Where the field
    admits null it becomes null; in the nine metrics fields that do not admit
    null the key is omitted, null included; each is a value-level loss,
    reported with the trace and field. A turn whose index is not its position
    in turns[] (minitrace-spec rc3 §4.1), whatever the value, means the trace
    is not emitted: a set-level loss. Its id goes to `index_dropped`, since
    it still counts for R5's unique-id rule."""
    kept = []
    for t in traces:
        tid = str(t.get("id"))[:80]

        def lost(field: str, v: Any, typ: str, action: str) -> None:
            diagnostics.append({"level": "warning", "kind": "input-failure", "field": field,
                                "message": f"trace {tid!r}: {field} holds {str(v)[:40]!r}, "
                                           f"not a {typ} in the §3.4 domain and the "
                                           f"schema's bounds; {action}"})
        turns = t.get("turns")
        bad = next(((i, x.get("index") if isinstance(x, dict) else x)
                    for i, x in enumerate(turns if isinstance(turns, list) else [])
                    if not (isinstance(x, dict) and _is_whole_number(x.get("index")) and x["index"] == i)),
                   None)
        if bad is not None:
            diagnostics.append({"level": "warning", "kind": "trace-dropped", "field": f"/turns/{bad[0]}/index",
                                "message": f"trace {tid!r}: turn at position {bad[0]} has index "
                                           f"{str(bad[1])[:40]!r}; a turn's index is its position "
                                           f"(minitrace-spec §4.1); trace not emitted"})
            if index_dropped is not None:
                index_dropped.append(t.get("id"))
            continue
        m = t.get("metrics")
        if isinstance(m, dict):
            for k, (typ, nullable, lo, hi) in _METRIC_NUMBERS.items():
                if k not in m or _admits(m[k], typ, lo, hi) or (nullable and m[k] is None):
                    continue
                if nullable:
                    lost(f"/metrics/{k}", m[k], typ, "null")
                    m[k] = None
                else:
                    lost(f"/metrics/{k}", m[k], typ, "key omitted")
                    del m[k]
        for arr, fields in _ITEM_NUMBERS.items():
            items = t.get(arr)
            for i, item in enumerate(items if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                for k, (typ, lo, hi) in fields.items():
                    if k in item and item[k] is not None and not _admits(item[k], typ, lo, hi):
                        lost(f"/{arr}/{i}/{k}", item[k], typ, "null")
                        item[k] = None
        kept.append(t)
    return kept


_CONSOLE_DIAGNOSTICS = 20

# §15 Losses. Set-level: a trace not emitted, a record skipped as unreadable.
# Value-level: a lineage edge omitted, a value nulled or truncated, a tool
# result lost. Anything else (a DSL note, an informational line) is no loss.
_LOSS_BY_KIND = {"trace-dropped": "set", "lineage-edge-omitted": "value",
                 "input-failure": "value", "tool-result-unattached": "value",
                 "tool-result-replaced": "value"}


def _classify_loss(d: dict) -> None:
    """Give a diagnostic its §15 loss class ("set", "value" or None) unless it
    carries one, and the level "error" for a set-level loss, so a caller can
    tell a lossy set apart without reading message text."""
    d.setdefault("loss", _LOSS_BY_KIND.get(d.get("kind")))
    if d["loss"] == "set":
        d["level"] = "error"


def set_level_loss(diags: list[dict]) -> bool:
    """True when any diagnostic records a set-level loss (§15)."""
    for d in diags:
        _classify_loss(d)
    return any(d.get("loss") == "set" for d in diags)
_FILE_MESSAGE_CHARS = 2000


def _bounded_message(d: dict) -> dict:
    """A diagnostic for the file: every one is kept (§8.3), but a message
    that quotes a long data string is cut, and says how long it was."""
    msg = d.get("message")
    if isinstance(msg, str) and len(msg) > _FILE_MESSAGE_CHARS:
        return {**d, "message": msg[:_FILE_MESSAGE_CHARS] + f"...({len(msg)} chars)"}
    return d


def _console_safe(text: str) -> str:
    """Control and format characters from source data are shown escaped, so
    a diagnostic cannot drive the terminal (escape sequences, bidi overrides)."""
    import unicodedata  # noqa: PLC0415
    return "".join(c if unicodedata.category(c) not in ("Cc", "Cf") else f"\\x{{{ord(c):x}}}"
                   for c in text)


class OutputFailed(Exception):
    """Writing the output failed part-way (an I/O error: a full disk, a
    file-size limit); what this run wrote is removed (ruled 2026-10-29).
    main() exits 1 with one error line."""


# Errors from os.link that mean the file system has no hard links, where a
# rename after an existence check stands in for it.
_NO_HARD_LINKS = {getattr(errno, n) for n in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS") if hasattr(errno, n)}


def _write_output_files(files: list[tuple[pathlib.Path, str]], replace: bool, label: pathlib.Path) -> None:
    """Ruled 2026-10-29: each (path, text) is written under a temporary
    name beside it (hidden, never `*.minitrace.json`, and short, so a final
    name that fits the file system's limit never fails for the temporary
    one), then all are put in place in order, the last one (the report)
    last, so a report never stands beside a partial set of traces.
    Directory output never overwrites (a hard link, which fails on an
    existing name); a single output file and its report replace (an
    earlier run's were removed before the run). On any exception the
    temporary files and the files this call put in place are removed,
    never another file; an OSError becomes OutputFailed naming `label`,
    anything else (Ctrl-C) propagates."""
    temps: list[str] = []
    placed: list[pathlib.Path] = []
    try:
        for path, text in files:
            tmp = str(path.with_name(f".{secrets.token_hex(8)}.partial"))
            # O_EXCL: a new file only; 0o666 under the umask, as write_text.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            temps.append(tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        for (path, _), tmp in zip(files, temps):
            if replace:
                os.replace(tmp, path)
                placed.append(path)
                continue
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise
            except OSError as e:
                if e.errno not in _NO_HARD_LINKS or os.path.lexists(path):
                    raise
                os.rename(tmp, path)
                placed.append(path)
            else:
                placed.append(path)   # before the unlink, which can fail too
                os.unlink(tmp)
    except BaseException as e:
        for p in [*temps, *placed]:
            try:
                os.unlink(p)
            except OSError:
                pass
        if not isinstance(e, OSError):
            raise
        raise OutputFailed(f"output {label} could not be written ({e.strerror or e}); "
                           f"nothing of this run's output is left") from None


def _report_diagnostics(diags: list[dict], out_path: pathlib.Path | None, directory: bool,
                        diagnostics_out: list | None,
                        files: list[tuple[pathlib.Path, str]] = ()) -> None:
    """§15 and §8.3: every diagnostic is reported. With an output path the
    full list goes to a sibling file (`<output>.diagnostics.json`, or
    `conversion.diagnostics.json` inside an output directory, a name no
    `<id>.minitrace.json` can take), written even when empty, and the console
    shows the first lines and where the rest are. Without one, the console is
    the only channel and shows every diagnostic. Each console line is bounded,
    since messages can quote source data. `files` are this run's trace
    files, written with the report and put in place before it (ruled
    2026-10-29)."""
    for d in diags:
        _classify_loss(d)
    if diagnostics_out is not None:
        diagnostics_out.extend(diags)
    sink = None
    if out_path is not None:
        sink = (out_path / "conversion.diagnostics.json" if directory
                else out_path.with_name(out_path.name + ".diagnostics.json"))
        text = json.dumps({"set_level_loss": any(d.get("loss") == "set" for d in diags),
                           "diagnostics": [_bounded_message(d) for d in diags]},
                          indent=2, ensure_ascii=False, default=str) + "\n"
        # Directory output never overwrites, like the trace files; a single
        # output file is replaced, and so is its sibling.
        _write_output_files([*files, (sink, text)], replace=not directory, label=out_path)
    if not diags:
        return
    shown = diags if sink is None else diags[:_CONSOLE_DIAGNOSTICS]
    sys.stderr.write(f"-- diagnostics ({len(diags)}) --\n")
    for d in shown:
        sys.stderr.write(f"  [{d.get('level')}] {_console_safe(str(d.get('message'))[:300])}\n")
    if len(diags) > len(shown):
        sys.stderr.write(f"  ... {len(diags) - len(shown)} more in {sink}\n")


class OutputRefused(Exception):
    """An output directory that already holds a trace or a report is
    refused before anything is written (DSL rc3 §15 lets a runner refuse
    instead of clearing it); main() exits EXIT_INPUT_UNREADABLE, the
    failure a caller tells apart from success and from set-level losses."""


def _clear_or_refuse_output(out_path: pathlib.Path | None, protect: tuple = (),
                            coverage: pathlib.Path | None = None) -> None:
    """(DSL rc3 §15, spec 9c801194; ruled 2026-10-10: replace a file,
    refuse a directory, never delete in a directory). Single-file output:
    the file and its `.diagnostics.json` sibling from an earlier run are
    removed before this run does anything, so an unreadable input, a
    refused adapter or an abort leaves no earlier trace or report to be
    read as this run's (they were left in place). Directory output: one
    that already holds an `<id>.minitrace.json` or the report is refused;
    the report counts too, since it is opened exclusively and a second run
    failed on it with a traceback, or part-way through the trace files.

    An output (or its sibling) that is the same file
    as one in `protect` (the input, the adapter) is refused before anything
    is removed: the removal deleted the input, and before that the trace
    overwrote it. In a directory every entry counts, whatever its type and
    letter case (a subdirectory, a dangling link or `X.MINITRACE.JSON` on a
    case-insensitive file system got past the check, and the run crashed
    after writing the report). A sibling that is not a file or a link is
    refused, not removed.

    The CLI's --coverage path is checked in the same step, so every
    path is checked before any is removed (_coverage_to_clear)."""
    if out_path is None and coverage is None:
        return
    present = []
    if out_path is not None:
        try:
            present = _output_to_clear(out_path, protect)
        except OSError as e:   # No traceback for a permission error
            raise OutputRefused(f"output {out_path} cannot be cleared ({e.strerror or e}); refused before writing") \
                from None
    if coverage is not None:
        try:
            present += _coverage_to_clear(coverage, out_path, protect)
        except OSError as e:
            raise OutputRefused(f"coverage {coverage} cannot be cleared ({e.strerror or e}); refused before writing") \
                from None
    for n, c in enumerate(present):   # the trace first: it is what §15 is about
        try:
            c.unlink()   # a link is removed, never its target
        except OSError as e:
            raise OutputRefused(
                f"{c} cannot be removed ({e.strerror or e}); refused before writing"
                + (f"; removed: {', '.join(map(str, present[:n]))}" if n else "")
                + f"; left in place from an earlier run: {', '.join(map(str, present[n:]))}") from None


def _output_to_clear(out_path: pathlib.Path, protect: tuple) -> list[pathlib.Path]:
    """Checks only; returns the earlier single-file trace and report to remove."""
    directory = os.path.isdir(out_path)
    # A directory output never writes the file sibling.
    sibling = out_path.with_name(out_path.name + ".diagnostics.json")
    for q in protect:
        for c in ((out_path,) if directory else (out_path, sibling)):
            if q is not None and os.path.exists(c) and os.path.exists(q) and os.path.samefile(c, q):
                raise OutputRefused(f"output {c} is the same file as {q}; refused before writing")
    if directory:
        held = sorted(n for n in os.listdir(out_path) if n.casefold().endswith(".minitrace.json")
                      or n.casefold() == "conversion.diagnostics.json")
        if held:
            raise OutputRefused(f"output directory {out_path} already holds {len(held)} trace or report "
                                f"file(s) ({', '.join(held[:3])}{', ...' if len(held) > 3 else ''}); "
                                f"refused before writing (§15: an earlier run's traces must not pass for this run's)")
        return []
    # Both paths are checked before either is removed (a sibling that was
    # a directory, or could not be removed, was
    # refused after the earlier trace was already gone, as if nothing had
    # been touched). lstat also refuses a name the file system cannot hold
    # (the trace was written, then the report failed with a traceback).
    return [c for c in (out_path, sibling) if _earlier_file(c, "output")]


def _earlier_file(c: pathlib.Path, what: str) -> bool:
    """True when an earlier file or link is at `c` (to be removed); anything
    else there is refused, not removed."""
    try:
        mode = os.lstat(c).st_mode
    except FileNotFoundError:
        return False
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        raise OutputRefused(f"{what} {c} exists and is not a file; refused before writing, nothing removed")
    return True


def _same_location(a: pathlib.Path, b: pathlib.Path) -> bool:
    """The same file where both exist (a link or a hard link included);
    otherwise the same path once links are resolved, so a path not written
    yet, or a dangling link to it, counts too. Two names differing only in
    letter case on a case-insensitive file system, neither there yet, are
    not caught here (see _coverage_clash)."""
    if os.path.exists(a) and os.path.exists(b):
        return os.path.samefile(a, b)
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _coverage_to_clear(cov: pathlib.Path, out_path: pathlib.Path | None,
                       protect: tuple) -> list[pathlib.Path]:
    """Ruled 2026-10-10: --coverage overwrote the input, the adapter,
    the trace just written or its report (exit 0), and a directory there
    failed after the trace was written. Checks only, before anything is
    removed or run; returns an earlier coverage file to remove, as the output check
    removes an earlier single-file trace (a link is removed, never its
    target)."""
    if os.path.isdir(cov):   # a link to a directory too
        raise OutputRefused(f"coverage {cov} is a directory; refused before writing, nothing removed")
    # Ruled 2026-10-10: a missing parent failed with a
    # traceback after the trace was written.
    if not os.path.isdir(cov.parent):
        raise OutputRefused(f"coverage {cov}: {cov.parent} is not an existing directory; "
                            f"refused before writing, nothing removed")
    others = [pathlib.Path(q) for q in protect if q is not None]
    if out_path is not None and os.path.isdir(out_path):
        name = cov.name.casefold()
        inside = _same_location(cov.parent, out_path)
        if (name.endswith(".minitrace.json") or name == "conversion.diagnostics.json") and inside:
            raise OutputRefused(f"coverage {cov} is inside the output directory under a trace's or the "
                                f"report's name; refused before writing, nothing removed")
        # Ruled 2026-10-10: output clearing never deletes in a
        # directory output, so an earlier file there is refused, not removed.
        if inside and os.path.lexists(cov):
            raise OutputRefused(f"coverage {cov} already exists inside the output directory, where nothing "
                                f"is removed; refused before writing, nothing removed")
    elif out_path is not None:
        others += [out_path, out_path.with_name(out_path.name + ".diagnostics.json")]
    for q in others:
        if _same_location(cov, q):
            raise OutputRefused(f"coverage {cov} is the same file as {q}; refused before writing, nothing removed")
    return [cov] if _earlier_file(cov, "coverage") else []


def _coverage_clash(cov: pathlib.Path, paths: tuple) -> pathlib.Path | None:
    """Right before the coverage is written, the file among `paths` it
    would overwrite, if any; this catches what _coverage_to_clear cannot see
    before the run (letter case on a case-insensitive file system)."""
    for q in paths:
        if q is not None and os.path.exists(cov) and os.path.exists(q) and os.path.samefile(cov, q):
            return pathlib.Path(q)
    return None


def dsl_load_problems(dsl: Any) -> str | None:
    """Every check run() refuses an adapter text on at load, in run()'s
    order: the message run() exits with for the first one that fails, or
    None when the adapter loads. lint_dispatch reports the same message, so
    lint is clean exactly when run() would load the adapter (ruled
    2026-10-19). The shape check comes first, since the others walk rules
    assuming those shapes."""
    shape_problems = dsl_shape_problems(dsl)
    if shape_problems:
        return ("shape error in the adapter text (DSL spec v0.1.0 §15), rejected at load:\n  "
                + "\n  ".join(shape_problems))
    # After the shape check, which refuses an adapter block that is
    # absent, not a mapping or without dsl_version; read first, they crashed.
    if dsl["adapter"]["dsl_version"] != "0.1.0":
        return f"unsupported dsl_version: {dsl['adapter']['dsl_version']}"
    template_problems = dsl_template_problems(dsl)
    if template_problems:
        return "invalid template (DSL spec v0.1.0 §8.3):\n  " + "\n  ".join(template_problems)
    expression_problems = dsl_expression_problems(dsl)
    if expression_problems:
        return ("structural failure in the adapter text (DSL spec v0.1.0 §8.3), rejected at load:\n  "
                + "\n  ".join(expression_problems))
    placeholder_problems = dsl_placeholder_problems(dsl)
    if placeholder_problems:
        return ("misplaced cross-trace placeholder (DSL spec v0.1.0 §8.4):\n  "
                + "\n  ".join(placeholder_problems))
    lineage_problems = dsl_lineage_problems(dsl)
    if lineage_problems:
        return ("lineage outside its one form (DSL spec v0.1.0 §13.1):\n  "
                + "\n  ".join(lineage_problems))
    # DSL rc3 §7.1, §15, spec 15321f73; ruled 2026-10-10: the
    # operator object is runtime-supplied and replaced whole, so an adapter
    # writes nothing at or beneath operator: a key operator or operator.* in
    # a session map, finalize.computed or a set to session.operator*,
    # whatever the value (null and expressions included). Before, such an
    # adapter ran (the CLI's operator replaced it) or, called as a library,
    # stopped only after reading the input.
    operator_writes = _direct_write_sites(dsl, "operator")
    if operator_writes:
        return ("an adapter writes nothing at or beneath operator (DSL spec v0.1.0 §7.1), rejected at load:\n  "
                + "\n  ".join(operator_writes))
    tool_calls_problems = dsl_tool_calls_problems(dsl)
    if tool_calls_problems:
        return ("tool_calls outside the emit actions (DSL spec v0.1.0 §8.2.3), rejected at load:\n  "
                + "\n  ".join(tool_calls_problems))
    empty_segment_problems = dsl_empty_segment_problems(dsl)
    if empty_segment_problems:
        return ("structural failure in the adapter text (DSL spec v0.1.0 §15), rejected at load:\n  "
                + "\n  ".join(empty_segment_problems))
    accumulate_problems = dsl_accumulate_problems(dsl)
    if accumulate_problems:
        return ("structural failure in the adapter text (DSL spec v0.1.0 §15), rejected at load:\n  "
                + "\n  ".join(accumulate_problems))
    number_problems = dsl_number_problems(dsl)
    if number_problems:
        return ("number outside the minitrace-spec §3.4 domain (DSL spec v0.1.0 §2), rejected at load:\n  "
                + "\n  ".join(number_problems))
    string_problems = dsl_string_problems(dsl)
    if string_problems:
        return ("string the trace cannot hold (not I-JSON, minitrace-spec §3.4), rejected at load:\n  "
                + "\n  ".join(string_problems))
    return None


def run(adapter_path: pathlib.Path, input_path: pathlib.Path, out_path: pathlib.Path | None,
        operator_override: dict | None = None,
        diagnostics_out: list | None = None, now: str | None = None) -> dict | list[dict] | None:
    """`now` (a §3.5 timestamp), else SOURCE_DATE_EPOCH, fixes what
    now_microseconds() returns for this run (DSL rc3 §12, ruled
    2026-10-22). An unusable pin raises ValueError before anything else,
    the output not cleared."""
    global _NOW_PINNED
    pin = pinned_now(now)
    saved, _NOW_PINNED = _NOW_PINNED, pin
    try:
        return _run_with_clock(adapter_path, input_path, out_path, operator_override, diagnostics_out)
    finally:
        _NOW_PINNED = saved


def _run_with_clock(adapter_path: pathlib.Path, input_path: pathlib.Path, out_path: pathlib.Path | None,
                    operator_override: dict | None = None,
                    diagnostics_out: list | None = None) -> dict | list[dict] | None:
    # §15, spec 9c801194: no earlier run's output may pass for this
    # one's, whatever the outcome; first of all, before anything can fail.
    _clear_or_refuse_output(out_path, (adapter_path, input_path))
    # DSL rc3 §7.1, ruled 2026-10-17: operator.type is required at
    # invocation, by a library call as by the command line; refused here,
    # before the adapter or the input is read.
    if operator_override is None:
        raise SystemExit("operator not supplied: operator.type is required at invocation "
                         "(DSL spec §7.1); pass operator_override")
    if problems := _operator_problems(operator_override):
        raise SystemExit("operator not conformant (minitrace-spec §5, DSL spec §7.1):\n  "
                         + "\n  ".join(problems))
    try:
        # Ruled 2026-10-10: a FIFO blocked the read for
        # ever; anything not a regular file is refused before reading.
        if not stat.S_ISREG(os.stat(adapter_path).st_mode):
            raise SystemExit(f"error: adapter {adapter_path} cannot be read (not a regular file)")
        adapter_text = adapter_path.read_text()
    except OSError as e:   # ruled 2026-10-10: exit 1 in one line, as an adapter refused at load
        raise SystemExit(f"error: adapter {adapter_path} cannot be read ({e.strerror or e})") from None
    dsl = load_adapter_yaml(adapter_text)
    refusal = dsl_load_problems(dsl)
    if refusal is not None:
        raise SystemExit(refusal)
    # Once per DSL, not per container: these rules can never fire on any input.
    # Since the 2026-10-17 ruling dsl_shape_problems refuses a rule with no when, the
    # last thing that reached this warning; kept as a backstop (lint uses it).
    for problem in dsl_predicate_problems(dsl):
        sys.stderr.write(f"  [warning] rule never fires: {problem}\n")
    # Ruled 2026-10-19: directory output is a container-mode form. Without
    # input.container it crashed (IsADirectoryError) for any trace id; it is
    # a refused output location (§15), before the input is read. The
    # directory is left as it is; an earlier --coverage file outside it was
    # already cleared, as on any refusal (ruled 2026-10-10).
    if (out_path and (os.path.isdir(out_path) or str(out_path).endswith("/"))
            and not dsl["input"].get("container")):
        raise OutputRefused(f"output {out_path} is a directory; directory output needs input.container "
                            f"(container mode, §4.1); refused before writing")

    reader_diag: list[dict] = []
    positions: list[int] = []
    try:
        raw = load_input(dsl["input"], input_path, reader_diag, positions)
    except InputUnreadable as e:
        # The report says why, where an earlier run's report was.
        directory = bool(out_path) and (os.path.isdir(out_path) or str(out_path).endswith("/"))
        if directory:
            out_path.mkdir(parents=True, exist_ok=True)
        _report_diagnostics([{"level": "error", "kind": "input-unreadable", "loss": None, "message": str(e)}],
                            out_path, directory, diagnostics_out)
        raise
    if not raw and not set_level_loss(reader_diag):
        # §15, spec 9c801194: nothing to convert (an empty root
        # array, an empty or all-blank jsonl, no top-level elements) is a
        # complete conversion that writes no trace: a warning, no set-level
        # signal. It exited 1 ("no records").
        what = "no top-level elements" if dsl["input"].get("container") else "no records"
        reader_diag.append({"level": "warning", "kind": "input-empty", "loss": None,
                            "message": f"{input_path}: nothing to convert ({what})"})
    if not raw:
        # Every record was skipped (§15 set-level), or there was nothing to
        # convert: no trace, reported, never an abort that leaves an earlier
        # run's output and report in place.
        directory = bool(out_path) and (os.path.isdir(out_path) or str(out_path).endswith("/"))
        if directory:
            out_path.mkdir(parents=True, exist_ok=True)
        elif out_path and os.path.isfile(out_path):
            out_path.unlink()
        _report_diagnostics(reader_diag, out_path, directory, diagnostics_out)
        return [] if ("sessions" in dsl or dsl["input"].get("container")) else None

    shape_views = _shape_views(dsl)
    is_multi_trace = "sessions" in dsl
    container_cfg = dsl["input"].get("container")
    all_diag: list[dict] = list(reader_diag)

    if container_cfg:
        record_path = container_cfg["record_path"]
        containers = raw  # already a list
        first = containers[0] if containers else None
        traces: list[dict] = []
        trace_containers: list[int] = []
        # The reader's input position of each kept container (skipped ones
        # counted), the number every report uses for it.
        pos = positions
        assert len(pos) == len(containers), "reader positions out of step with containers"
        first_reset = _FIRST_POSITION.set(pos[0] if pos else 0)
        item_skips: list[tuple] = []   # (container position, list position, item)
        try:
            for c_idx, c in enumerate(containers):
                # record_path may be a dotted path (e.g. "trajectory.events"),
                # or the special sentinel "$self" meaning "iterate once with the
                # container itself as the record." Used by adapters where each
                # container is a unit (no inner events list), e.g. MAST records
                # whose payload is a single trajectory string + flat annotation
                # block rather than a list of events.
                if record_path == "$self":
                    recs = [c]
                else:
                    recs = record_path_get(c, record_path)
                    if recs is not None and not isinstance(recs, list):
                        # Ruled 2026-10-07: an object, string, number
                        # or boolean here aborted the conversion (KeyError,
                        # TypeError) or was walked character by character;
                        # it is an input failure for this container, which
                        # yields no records, as a null does (§4.1.1, §8.3);
                        # value-level: §15 lists such a container, whose
                        # traces are still built from no records (spec
                        # 2c360b2d, ruled 2026-10-09).
                        all_diag.append({
                            "level": "warning", "kind": "input-failure", "loss": "value",
                            "record_index": None, "container_index": pos[c_idx],
                            "field": "input.container.record_path",
                            "message": f"input container {pos[c_idx]}: record_path {record_path!r} holds "
                                       f"{_json_type(recs)}, not a list; no records read from it"})
                        recs = None
                    recs = recs or []
                # DSL rc3 §4.1.1, §15 (spec f405b67e; ruled
                # 2026-10-10): an item of the record_path list that is not an
                # object is skipped as unreadable (set-level); the container's
                # traces are built from the other items, from none if every
                # item was skipped. A record keeps its list position in every
                # report. ($self is one record, the container, an object.)
                rec_pos = None
                if record_path != "$self" and any(not isinstance(r, dict) for r in recs):
                    rec_pos = [i for i, r in enumerate(recs) if isinstance(r, dict)]
                    item_skips.extend((pos[c_idx], i, r) for i, r in enumerate(recs) if not isinstance(r, dict))
                    recs = [recs[i] for i in rec_pos]
                for shape_name, view in shape_views:
                    t, diag = _build_trace(view, recs, container=c, container_first=first,
                                           operator_override=operator_override,
                                           container_index=c_idx, container_position=pos[c_idx],
                                           record_positions=rec_pos)
                    traces.append(t)
                    trace_containers.append(pos[c_idx])
                    all_diag.extend(diag)
        finally:
            _FIRST_POSITION.reset(first_reset)   # also when a container aborts the run
        _report_item_skips(item_skips, all_diag, len(reader_diag))
        from cross_trace import (  # noqa: PLC0415
            back_fill_cross_trace_references, drop_unusable_and_duplicate_ids)
        index_dropped: list = []
        traces = _enforce_schema_numbers(_truncate_too_deep(traces, all_diag, trace_containers),
                                         all_diag, index_dropped)
        traces = _refuse_non_ijson(traces, all_diag, index_dropped)
        directory = bool(out_path) and (os.path.isdir(out_path) or str(out_path).endswith("/"))
        if directory:
            # §15 Writing output: R5, then the file-name check, both before
            # resolution; back_fill's own R5 pass below then drops nothing.
            traces = _drop_unwritable_names(
                drop_unusable_and_duplicate_ids(traces, all_diag, index_dropped), all_diag)
        if is_multi_trace:
            # Resolve cross-trace placeholders across all emitted traces
            # (R1-R4); back_fill runs R5 first, which drops unusable and
            # shared ids.
            traces = back_fill_cross_trace_references(traces, all_diag, index_dropped)
        else:
            traces = drop_unusable_and_duplicate_ids(traces, all_diag, index_dropped)  # R5
        if directory:
            out_path.mkdir(parents=True, exist_ok=True)
            # Every trace file and the report are written, then put in
            # place, the report last; a failure leaves none of them
            # (ruled 2026-10-29).
            files = [(out_path / fname, json.dumps(t, indent=2, ensure_ascii=False) + "\n")
                     for t, fname in zip(traces, output_filenames(traces))]
            _report_diagnostics(all_diag, out_path, directory, diagnostics_out, files)
            return traces
        files = [(out_path, json.dumps(traces, indent=2, ensure_ascii=False) + "\n")] if out_path else []
        _report_diagnostics(all_diag, out_path, directory, diagnostics_out, files)
        return traces

    # Non-container path. Ruled 2026-10-18: a record keeps its
    # input position (skipped records counted) in every report, as the
    # reader's findings and container mode name it.
    assert len(positions) == len(raw), "reader positions out of step with records"
    if is_multi_trace:
        traces = []
        for shape_name, view in shape_views:
            t, diag = _build_trace(view, raw, container=None, container_first=None,
                                   operator_override=operator_override, record_positions=positions)
            traces.append(t)
            all_diag.extend(diag)
        from cross_trace import back_fill_cross_trace_references  # noqa: PLC0415
        index_dropped: list = []
        traces = _enforce_schema_numbers(_truncate_too_deep(traces, all_diag), all_diag, index_dropped)
        traces = _refuse_non_ijson(traces, all_diag, index_dropped)
        traces = back_fill_cross_trace_references(traces, all_diag, index_dropped)  # R3-R5 report here
        files = [(out_path, json.dumps(traces, indent=2, ensure_ascii=False) + "\n")] if out_path else []
        _report_diagnostics(all_diag, out_path, False, diagnostics_out, files)
        return traces

    # Single-trace, non-container — original path
    _, view = shape_views[0]
    trace, diagnostics = _build_trace(view, raw, container=None, container_first=None,
                                       operator_override=operator_override, record_positions=positions)
    diagnostics = reader_diag + diagnostics
    from cross_trace import drop_unusable_and_duplicate_ids  # noqa: PLC0415
    if not drop_unusable_and_duplicate_ids(
            _enforce_schema_numbers(_truncate_too_deep([trace], diagnostics), diagnostics), diagnostics):  # §4, R5
        trace = None  # not emitted
    elif not _refuse_non_ijson([trace], diagnostics):
        trace = None  # not I-JSON: not written, reported (DSL §15)
    files = []
    if out_path and trace is not None:
        files = [(out_path, json.dumps(trace, indent=2, ensure_ascii=False) + "\n")]
    elif out_path and trace is None and os.path.isfile(out_path):
        # A file from an earlier run must not pass for this run's output.
        out_path.unlink()
    # Written even when the trace was dropped: that is where the drop is reported.
    _report_diagnostics(diagnostics, out_path, False, diagnostics_out, files)
    return trace


def _apply_actions(actions: list[dict], ctx: dict, trace: dict, state: dict, op_types: dict, diagnostics: list[dict]) -> None:
    for action in actions:
        if "set" in action:
            for k, v in action["set"].items():
                _set_site(field=f"set.{k}")
                target, path_, prefix = ((state, k[len("state."):], 1) if k.startswith("state.") else
                                         (trace, k[len("session."):], 1) if k.startswith("session.") else
                                         (trace, k, 0))
                # Ruled 2026-10-09, path first, set too: a blocked
                # path is the one report; the RHS is not evaluated, so a
                # failing RHS does not add a second.
                if blocked := _blocking_parent(target, path_):
                    diagnostics.append(_blocked_write("set", k, v, prefix, blocked))
                    continue
                deep_set_blocked(target, path_, _materialise_value(v, ctx))
        elif "emit_turn" in action:
            turn = {}
            for key, val in action["emit_turn"].items():
                _set_site(field=f"emit_turn.{key}")
                turn[key] = _materialise_value(val, ctx)
            # Optional keys listed beside the action are left out when they
            # evaluate to None, so a conditional field (e.g. `ordering` only on
            # the turn a segment marker opens) does not appear as null on
            # every other turn.
            for key in action.get("omit_if_none") or []:
                if turn.get(key) is None:
                    turn.pop(key, None)
            trace.setdefault("turns", []).append(turn)
            state["last_turn_index"] = turn.get("index")
        elif "emit_tool_call" in action:
            # DSL rc3 §8.2.3 (ruled 2026-10-05: R5 throughout; spec
            # 146cdeb3): the id is classified as R5 classifies a trace-id part,
            # evaluated in id mode so an unusable ${...} part makes it null.
            spec_tc = action["emit_tool_call"]
            tc = {}
            for key, val in spec_tc.items():
                _set_site(field=f"emit_tool_call.{key}")
                tc[key] = evaluate(val, ctx, id_field=True) if key == "id" else _materialise_value(val, ctx)
            raw_id = tc.get("id")
            if _numeric(raw_id):
                tc["id"] = _render_interpolated(raw_id)   # 1 and 1.0 -> "1"
            if not isinstance(tc.get("id"), str) or _unusable_part(tc.get("id")):
                # Null, "", a boolean, an object or a list identifies no call:
                # not emitted (a null id is schema-invalid), a value-level loss;
                # last_tool_call_id is reset so a later attach cannot write the
                # result onto the previous call.
                id_spec = spec_tc.get("id")
                if raw_id is None and isinstance(id_spec, str) and "${" in id_spec:
                    why = f"template {id_spec[:60]!r} has an unusable ${{...}} part (null, \"\", a boolean, " \
                          f"an object or a list)"
                else:
                    why = f"{str(raw_id)[:60]!r} ({type(raw_id).__name__}) is not a usable id"
                diagnostics.append(_input_failure("emit_tool_call.id", spec_tc.get("id", _NO_EXPRESSION),
                                                  f"emit_tool_call.id: {why}; tool call not emitted", loss="value"))
                state["last_tool_call_id"] = None
                continue
            if tc.get("tool_name") is not None and not isinstance(tc.get("tool_name"), str):
                diagnostics.append(_input_failure("emit_tool_call.tool_name", spec_tc.get("tool_name"),
                                                  f"emit_tool_call.tool_name is a {type(tc['tool_name']).__name__}, "
                                                  f"not a string; null"))
                tc["tool_name"] = None
            tool_name = tc.get("tool_name")
            if op_types and "operation_type" not in tc:
                # §8.3 equality (spec 24a1ab2c): table keys are strings.
                default = op_types.get("default", "OTHER")
                # Copied: a map or list here was shared by every trace.
                tc["operation_type"] = _json_copy(op_types.get(tool_name, default) if isinstance(tool_name, str)
                                                  else default)
            trace.setdefault("tool_calls", []).append(tc)
            state["last_tool_call_id"] = tc["id"]
            state.setdefault("tool_calls_by_id", {})[tc["id"]] = tc
        elif "attach_tool_result" in action:
            spec = action["attach_tool_result"]
            _set_site(field="attach_tool_result.target_id")
            target_id = evaluate(spec["target_id"], ctx)
            if not _is_key_scalar(target_id):
                diagnostics.append(_input_failure("attach_tool_result.target_id", spec["target_id"],
                                                  f"attach_tool_result: target_id is a {type(target_id).__name__}, "
                                                  f"not a scalar"))
                continue
            # Ruled 2026-10-05: a numeric target_id is rendered per
            # §8.3 as emit_tool_call renders a numeric id, so the source
            # number 5 finds the call emitted as "5". Matching is then §8.3
            # equality on strings; anything else is reported unattached.
            raw_target = target_id
            if _numeric(target_id):
                target_id = _render_interpolated(target_id)
            target = state.get("tool_calls_by_id", {}).get(target_id) if isinstance(target_id, str) else None
            if target is None:
                shown = repr(raw_target)[:80] + ("" if isinstance(raw_target, str) else
                                                 f" ({type(raw_target).__name__})")
                diagnostics.append({"level": "warning", "kind": "tool-result-unattached",
                                    "message": f"attach_tool_result: unknown target_id {shown}"})
                continue
            if target.get("output"):
                # A second result for the same call replaces the first; say so
                # rather than overwrite an outcome silently.
                diagnostics.append({"level": "warning", "kind": "tool-result-replaced", "message":
                                    f"attach_tool_result: second result for {repr(target_id)[:80]} replaces the first"})
            output = target.setdefault("output", {})
            for key, val in (spec.get("output") or {}).items():
                _set_site(field=f"attach_tool_result.output.{key}")
                output[key] = _materialise_value(val, ctx)
        elif "accumulate" in action:
            for k, v in action["accumulate"].items():
                _set_site(field=f"accumulate.{k}")
                if k.startswith("state."):
                    path_ = k[len("state."):]
                    # §8.2.1 (spec 8dbbc465, ruled 2026-10-09: path
                    # first): a path through a non-object is the one report,
                    # nothing written, whatever the RHS; the RHS is not
                    # evaluated, so its own failure cannot add a second.
                    if blocked := _blocking_parent(state, path_):
                        diagnostics.append(_blocked_write("accumulate", k, v, 1, blocked))
                        continue
                    value = _materialise_value(v, ctx)
                    cur = deep_get(state, path_)
                    if isinstance(cur, list):
                        if value is not None:
                            cur.append(value)
                    else:
                        if value is None:
                            if cur is None:
                                # §8.2.5 (spec ed6d769b, ruled 2026-10-10:
                                # option i): a null RHS into a null or absent
                                # counter writes nothing and creates no parent.
                                continue
                            value = 0
                        # §8.2.5: a RHS that is neither null nor a number, or a
                        # counter holding a non-number, is an input failure;
                        # the state keeps its prior value (it was skipped
                        # silently before; booleans are not numbers).
                        if isinstance(value, bool) or not isinstance(value, (int, float)):
                            diagnostics.append(_input_failure(
                                f"accumulate.{k}", v, f"accumulate.{k}: the value is {_json_type(value)}, "
                                f"not a number; prior value kept"))
                            continue
                        if cur is not None and (isinstance(cur, bool) or not isinstance(cur, (int, float))):
                            diagnostics.append(_input_failure(
                                f"accumulate.{k}", v, f"accumulate.{k}: the counter holds {_json_type(cur)}, "
                                f"not a number; prior value kept"))
                            continue
                        try:
                            total = _in_number_domain((cur or 0) + value)
                        except OverflowError as e:
                            # §8.3: an input failure keeps the prior value and
                            # is reported; it never reaches sealing.
                            diagnostics.append(_input_failure(f"accumulate.{k}", v,
                                                              f"accumulate.{k}: {e}; prior value kept"))
                            continue
                        if blocked := deep_set_blocked(state, path_, total):
                            diagnostics.append(_blocked_write("accumulate", k, v, 1, blocked))
        elif "note" in action:
            _set_site(field="note.message")
            before = len(diagnostics)
            message = evaluate(action["note"].get("message", ""), ctx)
            # Ruled 2026-10-21: a message that failed to render is reported
            # once, by the input failure naming note.message; the note itself
            # is dropped rather than added with a null message. A message
            # that renders null without a failure is still a note.
            if message is None and any(d.get("kind") == "input-failure"
                                       for d in diagnostics[before:]):
                continue
            diagnostics.append({
                "kind": "note",
                "level": action["note"].get("level", "info"),
                "message": message,
            })
        elif "emit_annotation" in action:
            _set_site(field="emit_annotation")
            ann = _materialise(action["emit_annotation"], ctx)
            trace.setdefault("annotations", []).append(ann)
        elif "emit_lineage" in action:
            _set_site(field="emit_lineage")
            edge = _materialise(action["emit_lineage"], ctx)
            declared = action["emit_lineage"].get("target_content_hash")
            if isinstance(declared, str) and "${trace:" in declared and edge.get("target_content_hash") is None:
                # DSL spec rc3 §13.2 R4: an edge requiring integrity is not
                # created without the referenced hash; omit it and say so.
                diagnostics.append({"level": "warning", "kind": "lineage-edge-omitted",
                                    "message": "emit_lineage: target_content_hash evaluated to null; "
                                               f"edge to {edge.get('target_trace_id')!r} omitted (R4)"})
                continue
            trace.setdefault("lineage", []).append(edge)
        elif "emit_redaction" in action:
            _set_site(field="emit_redaction")
            red = _materialise(action["emit_redaction"], ctx)
            trace.setdefault("redactions", []).append(red)
        elif "emit_policy_trigger" in action:
            _set_site(field="emit_policy_trigger")
            pt = _materialise(action["emit_policy_trigger"], ctx)
            trace.setdefault("policy_triggers", []).append(pt)
        elif "emit_segment_marker" in action:
            _set_site(field="emit_segment_marker")
            sm = _materialise(action["emit_segment_marker"], ctx)
            trace.setdefault("segment_markers", []).append(sm)
        else:
            raise EvalError(f"unknown action: {list(action.keys())}")


def _is_key_scalar(v: Any) -> bool:
    """A value usable as an id or lookup key: data decides its type, and a
    list or object used as a dict key aborted the run (TypeError)."""
    return v is None or isinstance(v, (str, int, float, bool))


def _materialise(spec: dict, ctx: dict) -> dict:
    site = (_EVAL_SITE.get() or {}).get("field") or "?"
    base = site.split(".")[0]
    out: dict = {}
    for key, val in spec.items():
        _set_site(field=f"{base}.{key}")
        out[key] = _materialise_value(val, ctx)
    return out


def _materialise_value(val: Any, ctx: dict) -> Any:
    """Recursively materialise a DSL value: evaluate strings, recurse into
    dicts and lists. Without list recursion, list-of-string fields like
    `taxonomy: ["atbench/claw/risk-source/${container.labels.risk_source}"]`
    pass through unevaluated, leaking placeholder syntax into the trace.
    """
    if isinstance(val, dict):
        return {k: _materialise_value(v, ctx) for k, v in val.items()}
    if isinstance(val, list):
        return [_materialise_value(v, ctx) for v in val]
    return evaluate(val, ctx)


# minitrace-spec §5.3: the legal bases a well-formed capture_purpose names
# (or an `x-` extension). test_fuzz_regressions pins this to the base schema.
_LEGAL_BASES = ("gdpr_art6_consent", "gdpr_art6_contract", "gdpr_art6_legitimate_interest",
                "gdpr_art88_employment_safeguards", "works_council_agreement",
                "research_ethics_approval")


def _capture_purpose_problems(legal_basis: str, reference: str, authorised_at: str) -> list[str]:
    """Why a capture_purpose is not well-formed (minitrace-spec §5.3, DSL
    spec §7.1), or []: legal_basis from the enumeration or x-*, reference a
    non-empty string, authorised_at exactly in the §3.5 form."""
    from source_reader import _ijson_string_problem  # noqa: PLC0415
    problems = []
    for name, v in (("legal basis", legal_basis), ("reference", reference), ("authorised_at", authorised_at)):
        # An emitted trace is I-JSON (minitrace-spec §3.4); a non-UTF-8 argv
        # byte arrives as an unpaired surrogate (surrogateescape).
        if (bad := _ijson_string_problem(v)):
            problems.append(f"{name} holds {bad[1]} (not I-JSON)")
    if problems:
        return problems
    if not (legal_basis in _LEGAL_BASES or legal_basis.startswith("x-")):
        problems.append(f"legal basis {legal_basis!r} is not one of {', '.join(_LEGAL_BASES)} "
                        f"and not an x-* extension")
    if not reference:
        problems.append("reference is empty")
    if (bad := _spec_timestamp_problem(authorised_at)):
        problems.append(f"authorised_at {bad}")
    return problems


def _spec_timestamp_problem(ts: str) -> str | None:
    """None if `ts` is exactly in the minitrace-spec §3.5 form (a valid
    instant written YYYY-MM-DDTHH:MM:SS.ffffffZ), else why not."""
    try:
        normal = fn_iso_normalise(ts)
    except Exception:  # noqa: BLE001 - any refusal of _iso_parse
        normal = None
    if normal == ts:
        return None
    return (f"{ts!r} is not in the §3.5 form YYYY-MM-DDTHH:MM:SS.ffffffZ"
            + (f" (did you mean {normal}?)" if normal else ""))


# Capture modes that promise a transformation of the operator's content
# (minitrace-spec §5.2, §8.4). The runner performs none of them, so a trace
# declaring one would carry the content unchanged: refused for every operator
# type until the transformation exists (ruled 2026-10-27).
_UNPERFORMED_MODES = ("pseudonymised", "scrubbed", "reference_only")


def _unperformed_mode_problem(op_type: Any, mode: Any, cli: bool) -> str | None:
    """Why `mode` is refused for an operator of `op_type`, or None. `cli`
    names the remedy as command-line options rather than object fields."""
    if mode not in _UNPERFORMED_MODES:
        return None
    what = "--operator-capture-mode" if cli else "capture_mode"
    if op_type == "human":
        remedy = ("Use full with --operator-capture-purpose-legal-basis, -reference and -authorised-at."
                  if cli else "Use full with a capture_purpose.")
    elif op_type == "delegated":
        remedy = "Use full."
    else:
        remedy = "Use full, or omit the mode." if cli else "Use full, or leave capture_mode out."
    return (f"{what} {mode} is refused: this runner does not perform the {mode} transformation, so the "
            f"trace would declare {mode} while carrying the content unchanged (minitrace-spec §5.2, "
            f"§8.4). {remedy}")


def _operator_problems(op: Any) -> list[str]:
    """Why an operator object passed to run() is not one the runner may
    write (minitrace-spec §5.2-§5.4 and the base schema's Operator, DSL spec
    §7.1), or []: the rules _build_operator_from_args applies to the CLI,
    plus the schema's typing for every operator type, so the library path
    cannot emit a trace the CLI could not."""
    from source_reader import _ijson_string_problem  # noqa: PLC0415
    if not isinstance(op, dict):
        return ["operator is not an object"]
    stack, bad = [op], None
    while stack and bad is None:   # every member name and string is I-JSON,
        v = stack.pop()            # every number in the §3.4 domain
        if (bad := _json_shape_problem(v)) is not None:
            break
        if isinstance(v, str):
            bad = _ijson_string_problem(v)
        elif _constant_out_of_domain(v):
            bad = ("number-outside-safe-range", f"number {v!r} outside the §3.4 domain")
        elif isinstance(v, dict):
            bad = next((p for k in v if isinstance(k, str) and (p := _ijson_string_problem(k))), None)
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    if bad is not None:
        return [f"operator holds {bad[1]}"]
    t = op.get("type")
    if t not in ("human", "ai_overseer", "scripted", "delegated"):
        return [f"operator type {t!r} is not human, ai_overseer, scripted or delegated"]
    modes = ("full", "pseudonymised", "scrubbed", "reference_only")
    problems = []
    if op.get("capture_mode") is not None and op.get("capture_mode") not in modes:
        problems.append(f"capture_mode {op.get('capture_mode')!r} is not one of {', '.join(modes)}")
    if t in ("human", "delegated") and op.get("capture_mode") not in modes:
        problems.append(f"capture_mode is required for a {t} operator")
    if (refused := _unperformed_mode_problem(t, op.get("capture_mode"), cli=False)):
        problems.append(refused)
    aware = op.get("operator_aware")
    if t == "human" and not isinstance(aware, bool):
        problems.append("operator_aware (true or false) is required for a human operator")
    elif aware is not None and not isinstance(aware, bool):
        problems.append(f"operator_aware {aware!r} is not a boolean")
    cp = op.get("capture_purpose")
    if cp is not None and not isinstance(cp, dict):
        problems.append(f"capture_purpose {cp!r} is not an object or null")
    if t == "human" and op.get("capture_mode") == "full" and not isinstance(cp, dict):
        problems.append("capture_purpose is required for a human operator in full mode")
    if isinstance(cp, dict):
        fields = {k: cp.get(k) for k in ("legal_basis", "reference", "authorised_at")}
        typed = [k for k, v in fields.items() if k in cp and not isinstance(v, str)]
        if typed:
            problems.append(f"capture_purpose {', '.join(typed)} not a string")
        elif t == "human":
            # §5.3 well-formedness binds a human operator's capture_purpose.
            if not all(isinstance(v, str) for v in fields.values()):
                problems.append("capture_purpose needs legal_basis, reference and authorised_at")
            else:
                problems += _capture_purpose_problems(*fields.values())
        elif isinstance(fields["authorised_at"], str) and \
                (bad := _spec_timestamp_problem(fields["authorised_at"])):
            # Any operator type: the schema types authorised_at as a §3.5 timestamp.
            problems.append(f"capture_purpose authorised_at {bad}")
    return problems


def _build_operator_from_args(args) -> dict:
    """Construct the operator object from CLI args, enforcing minitrace-spec
    §5 + adapter DSL spec §7.1.

    The trace producer (= the entity invoking the runner) supplies these.
    The adapter DSL is forbidden from declaring them. A `human` or
    `delegated` operator needs a capture mode (§5.2, T10, ruled 2026-10-05);
    a human operator's capture_purpose is well-formed (§5.3), or nothing is
    emitted; another operator's is held to the schema's typing only (§5.3:
    not rejected for being ill-formed). Every field the base schema allows is kept where the producer
    asserted it, for every operator type (ruled 2026-10-20), and the object
    is then held to _operator_problems, the rules run() applies, so the CLI
    and the library path emit the same operators: a human operator's
    capture_purpose in any capture mode, a delegated operator's
    operator_aware, any asserted field of a scripted or ai_overseer one.
    A capture mode the runner does not perform (pseudonymised, scrubbed,
    reference_only) is refused for every operator type (ruled 2026-10-27).
    """
    if not args.operator_type:
        raise SystemExit(
            "ERROR: --operator-type is REQUIRED (per minitrace-spec §5 and DSL spec §7.1).\n"
            "  Allowed values: human, ai_overseer, scripted, delegated"
        )
    op: dict = {"type": args.operator_type}

    if args.operator_type in ("human", "delegated") and not args.operator_capture_mode:
        raise SystemExit(
            f"ERROR: --operator-capture-mode is REQUIRED when operator-type is "
            f"'{args.operator_type}' (minitrace-spec §5.2, DSL spec §7.1).\n"
            "  Accepted value: full (pseudonymised, scrubbed and reference_only are refused: this\n"
            "  runner does not perform those transformations)"
        )
    if (refused := _unperformed_mode_problem(args.operator_type, args.operator_capture_mode, cli=True)):
        raise SystemExit(f"ERROR: {refused}")
    purpose = {f: getattr(args, f"operator_capture_purpose_{f}")
               for f in ("legal_basis", "reference", "authorised_at")}
    purpose = {f: v for f, v in purpose.items() if v is not None}
    if args.operator_type != "human":
        if args.operator_aware is not None:
            op["operator_aware"] = args.operator_aware
        if args.operator_capture_mode is not None:
            op["capture_mode"] = args.operator_capture_mode
        if purpose:
            op["capture_purpose"] = purpose
    if args.operator_type == "human":
        if args.operator_aware is None:
            raise SystemExit(
                "ERROR: --operator-aware is REQUIRED when operator-type is 'human'.\n"
                "  Per minitrace-spec §5.4 the value cannot be inferred from harness output;\n"
                "  it MUST come from the trace producer who knows the operator's actual state."
            )
        op["operator_aware"] = args.operator_aware
        op["capture_mode"] = args.operator_capture_mode
        if args.operator_capture_mode == "full":
            missing = [
                f for f in ("legal_basis", "reference", "authorised_at")
                if not getattr(args, f"operator_capture_purpose_{f.replace('-', '_')}")
            ]
            if missing:
                raise SystemExit(
                    f"ERROR: capture-mode 'full' with operator-type 'human' requires "
                    f"--operator-capture-purpose-legal-basis, --operator-capture-purpose-reference, "
                    f"and --operator-capture-purpose-authorised-at. Missing: {missing}"
                )
            problems = _capture_purpose_problems(args.operator_capture_purpose_legal_basis,
                                                 args.operator_capture_purpose_reference,
                                                 args.operator_capture_purpose_authorised_at)
            if problems:
                raise SystemExit("ERROR: capture_purpose is not well-formed (minitrace-spec §5.3):\n  "
                                 + "\n  ".join(problems))
            op["capture_purpose"] = {
                "legal_basis": args.operator_capture_purpose_legal_basis,
                "reference": args.operator_capture_purpose_reference,
                "authorised_at": args.operator_capture_purpose_authorised_at,
            }
        else:
            # Not reached through main() while the runner refuses every
            # mode but full (argparse admits only the four modes, and a
            # human operator needs one); a direct call with another value
            # lands here and _operator_problems refuses it below. Kept for
            # when a transformation is performed. §5.3: a non-null
            # capture_purpose is held to well-formedness whatever the mode;
            # none asserted stays null.
            if purpose and len(purpose) < 3:
                missing = [f"--operator-capture-purpose-{f.replace('_', '-')}"
                           for f in ("legal_basis", "reference", "authorised_at") if f not in purpose]
                raise SystemExit(
                    f"ERROR: a capture purpose for operator-type 'human' needs all three "
                    f"--operator-capture-purpose-* options in any capture mode (minitrace-spec §5.3), "
                    f"or none of them. Missing: {missing}")
            op["capture_purpose"] = purpose or None
    if (problems := _operator_problems(op)):
        raise SystemExit("ERROR: operator is not one the runner may write (minitrace-spec §5.2-§5.4):\n  "
                         + "\n  ".join(problems))
    return op


def _strtobool(v: str | None) -> bool | None:
    if v is None:
        return None
    if v.lower() in ("true", "1", "yes", "y"):
        return True
    if v.lower() in ("false", "0", "no", "n"):
        return False
    raise SystemExit(f"ERROR: --operator-aware must be 'true' or 'false', got {v!r}")


EXIT_SET_LEVEL_LOSS = 3
# §15 (spec 9c801194; ruled 2026-10-10: 4): an input unreadable as a whole,
# told apart from success (0), set-level losses (3), an adapter refused at
# load or a crash (1) and argparse usage (2).
EXIT_INPUT_UNREADABLE = 4


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Run a minitrace adapter DSL against a native-format input. "
            "Per DSL spec §7.1, consent-claim fields on the operator object are "
            "supplied at runtime, NOT baked into the adapter DSL file."
        ),
        epilog=(
            "Exit status: 0 converted; 3 converted with a set-level loss (a trace not "
            "emitted or a record skipped; see the diagnostics, whose file also carries "
            "set_level_loss); 4 input unreadable as a whole, or an output or --coverage location "
            "refused before writing (DSL spec §15), or a --coverage path found to be the same file "
            "as the input, adapter, trace or report only after the run (letter case on a "
            "case-insensitive file system: the trace is written and kept, the coverage not "
            "written); 1 fatal error (an adapter refused at load included, and output that could "
            "not be written, of which nothing is left); "
            "2 usage error. "
            "Reporting bound (DSL spec §4): input findings are reported one by one "
            "up to 1,000 per source; the rest as one input-findings-truncated summary "
            "naming their count and the first affected input, set-level if any of them was a "
            "skipped record or element."
        ),
    )
    p.add_argument("adapter", type=pathlib.Path)
    p.add_argument("input", type=pathlib.Path)
    p.add_argument("--output", type=pathlib.Path, default=None)
    p.add_argument("--operator-type", required=True,
                   choices=["human", "ai_overseer", "scripted", "delegated"],
                   help="Operator type for the produced trace(s). REQUIRED.")
    p.add_argument("--operator-aware", type=_strtobool, default=None,
                   help="REQUIRED when --operator-type is 'human'. true|false.")
    p.add_argument("--operator-capture-mode", default=None,
                   choices=["full", "pseudonymised", "scrubbed", "reference_only"],
                   help="REQUIRED when --operator-type is 'human' or 'delegated'. Only 'full' is accepted: "
                        "this runner performs no pseudonymisation, scrubbing or reference-only "
                        "transformation, so the other three are refused for every operator type.")
    p.add_argument("--operator-capture-purpose-legal-basis", default=None,
                   help="REQUIRED when capture-mode is 'full' and operator-type is 'human'; kept for any "
                        "operator when given (a human operator's purpose needs all three, §5.3).")
    p.add_argument("--operator-capture-purpose-reference", default=None,
                   help="REQUIRED when capture-mode is 'full' and operator-type is 'human'; kept for any "
                        "operator when given (a human operator's purpose needs all three, §5.3).")
    p.add_argument("--operator-capture-purpose-authorised-at", default=None,
                   help="Timestamp in the §3.5 form YYYY-MM-DDTHH:MM:SS.ffffffZ; REQUIRED when capture-mode is "
                        "'full' and operator-type is 'human'; kept for any operator when given.")
    p.add_argument("--coverage", type=pathlib.Path, default=None,
                   help="Write a coverage report sidecar (JSON) listing seen vs consumed input fields. "
                        "Useful for detecting silent source-format drift across producer versions.")
    p.add_argument("--coverage-type-field", default="type",
                   help="Field name on input records that discriminates record-type (default: 'type'). "
                        "Adapters whose source format uses a different discriminator (e.g. 'event_kind') "
                        "should override this to bucket the coverage report correctly.")
    p.add_argument("--now", default=None, metavar="TIMESTAMP",
                   help="Fix what now_microseconds() returns for this run (DSL spec §12) to a timestamp "
                        "in the §3.5 form YYYY-MM-DDTHH:MM:SS.ffffffZ. Without it, SOURCE_DATE_EPOCH "
                        "(whole seconds since the epoch) fixes it when set; an unusable value of "
                        "either is a usage error.")
    args = p.parse_args()

    try:   # ruled 2026-10-22: an unusable pin is refused before anything runs
        pinned_now(args.now)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:   # Before the CLI's own operator refusal too
        _clear_or_refuse_output(args.output, (args.adapter, args.input), args.coverage)
    except OutputRefused as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INPUT_UNREADABLE
    operator = _build_operator_from_args(args)
    diagnostics: list[dict] = []
    try:
        trace = run(args.adapter, args.input, args.output, operator_override=operator,
                    diagnostics_out=diagnostics, now=args.now)
    except (InputUnreadable, OutputRefused) as e:   # its own status, no traceback
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INPUT_UNREADABLE
    except OutputFailed as e:   # ruled 2026-10-29: the fatal status, no traceback
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.output is None:
        print(json.dumps(trace, indent=2, ensure_ascii=False))

    if args.coverage:
        from coverage import build_coverage_report, write_coverage_report  # noqa: PLC0415
        dsl = load_adapter_yaml(args.adapter.read_text())
        raw = load_input(dsl["input"], args.input)
        output_count = len(trace) if isinstance(trace, list) else int(trace is not None)
        report = build_coverage_report(
            raw=raw, dsl=dsl,
            input_path=str(args.input), adapter_path=str(args.adapter),
            output_count=output_count, type_field=args.coverage_type_field,
        )
        written = (args.output, None if args.output is None or os.path.isdir(args.output)
                   else args.output.with_name(args.output.name + ".diagnostics.json"))
        if (q := _coverage_clash(args.coverage, (args.input, args.adapter) + written)) is not None:
            # ruled 2026-10-10: still 4, though the conversion completed;
            # set_level_loss stays in the diagnostics file.
            print(f"error: coverage {args.coverage} is the same file as {q}, found only after the run; "
                  f"the trace was written and kept, the coverage not written", file=sys.stderr)
            return EXIT_INPUT_UNREADABLE
        write_coverage_report(report, str(args.coverage))
        sys.stderr.write(f"-- coverage report written to {args.coverage} --\n")
        sys.stderr.write(
            f"   input: {report['input_summary']['container_count']} container(s), "
            f"{report['input_summary']['record_count_total']} record(s); "
            f"output: {report['output_summary']['trace_count']} trace(s)\n"
        )
        unmapped = report.get("unmapped_paths_by_type", {})
        if unmapped:
            sys.stderr.write(f"   unmapped surface: {sum(len(v) for v in unmapped.values())} "
                             f"path(s) across {len(unmapped)} record-type(s)\n")
        else:
            sys.stderr.write("   unmapped surface: 0 paths (full coverage)\n")
        # A2(iii): the reverse direction — fields the DSL reads that the corpus
        # never provides (the direct detector for field-name drift / A1-class bugs).
        absent = report.get("consumed_but_absent_in_record", [])
        if absent:
            sys.stderr.write(
                f"   ⚠ read-but-never-present: {len(absent)} field(s) the DSL "
                f"references but the corpus lacks: {', '.join(absent)}\n"
            )

    # §15: a set-level loss is visible without reading message text.
    return EXIT_SET_LEVEL_LOSS if set_level_loss(diagnostics) else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    from input_tree import install_output_sanitiser
    install_output_sanitiser()
    sys.exit(main())
