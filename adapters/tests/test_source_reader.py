"""Input-contract tests for `source_reader`.

These are the committed form of the throwaway probes recorded in the
converter input contract (design note of 2026-07-30, §1): the five ordinary
ways a session log goes wrong, four of which aborted the entire conversion with an
unhandled exception in both pre-existing readers.

The property under test (contract §5) is a single assertion: for any input the
reader either produces records whose fidelity counts account for every skipped
record, or fails closed with a declared reason. It never raises an unhandled
exception and never drops a record silently.

Several tests carry a kill assertion — a check that the fixture genuinely is
damaged — because a corruption fixture that stopped being corrupt would make
every downstream assertion pass vacuously.
"""
from __future__ import annotations

import codecs
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import source_reader as sr  # noqa: E402


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def good(n: int) -> bytes:
    return b"\n".join(
        json.dumps({"type": "user", "i": i}).encode() for i in range(n)
    )


def findings_of(read: sr.SourceRead, kind: str) -> list[dict]:
    return [f for f in read.findings if f["kind"] == kind]


# --- the five conditions from the contract note §1 --------------------------


def test_truncated_final_line_recovers(tmp_path):
    """What a killed harness leaves behind, and the case both readers aborted on."""
    data = good(3) + b'\n{"type": "assistant", "message": {"con'
    path = write(tmp_path, "s.jsonl", data)

    with pytest.raises(json.JSONDecodeError):     # kill assertion
        json.loads('{"type": "assistant", "message": {"con')

    read = sr.read_jsonl(path)
    assert len(read.records) == 3
    assert read.record_count_read == 4
    assert read.record_count_skipped == 1
    errs = findings_of(read, "parse-error")
    assert len(errs) == 1
    assert errs[0]["record_index"] == 3
    assert errs[0]["byte_offset"] == len(good(3)) + 1
    assert errs[0]["reason"] in {"unterminated-string", "expecting-value",
                                 "expecting-delimiter", "expecting-property-name"}


def test_corrupt_middle_line_recovers(tmp_path):
    lines = [json.dumps({"i": 0}).encode(), b"{not json at all", json.dumps({"i": 2}).encode()]
    path = write(tmp_path, "s.jsonl", b"\n".join(lines) + b"\n")

    read = sr.read_jsonl(path)
    assert [r["i"] for r in read.records] == [0, 2]
    assert read.record_count_read == 3
    assert read.record_count_skipped == 1
    assert read.record_indices == [0, 2]
    assert findings_of(read, "parse-error")[0]["record_index"] == 1


def test_invalid_utf8_is_replaced_and_recorded(tmp_path):
    payload = b'{"type": "user", "text": "caf\xe9 latte"}'   # latin-1 byte in UTF-8 stream
    path = write(tmp_path, "s.jsonl", good(1) + b"\n" + payload + b"\n")

    with pytest.raises(UnicodeDecodeError):       # kill assertion
        payload.decode("utf-8")

    read = sr.read_jsonl(path)
    decode = findings_of(read, "decode-error")
    assert len(decode) == 1
    # A valid lead byte followed by a non-continuation byte; the lone-0xff case
    # below is the invalid-start-byte one.
    assert decode[0]["reason"] == "invalid-continuation-byte"
    assert decode[0]["byte_length"] == 1
    # The offset is into the original bytes, before decoding.
    assert decode[0]["byte_offset"] == len(good(1)) + 1 + payload.index(b"\xe9")
    # Replacement leaves the record parseable, so it is read, not skipped.
    assert len(read.records) == 2
    assert read.records[1]["text"] == "caf� latte"
    assert read.record_count_skipped == 0


def test_every_bad_byte_run_is_recorded(tmp_path):
    """Two damaged runs in one line produce two findings, at their own offsets."""
    payload = b'{"a": "\xff", "b": "\xfe"}'
    path = write(tmp_path, "s.jsonl", payload + b"\n")

    read = sr.read_jsonl(path)
    decode = findings_of(read, "decode-error")
    assert [f["reason"] for f in decode] == ["invalid-start-byte", "invalid-start-byte"]
    assert [f["byte_offset"] for f in decode] == [payload.index(b"\xff"),
                                                  payload.index(b"\xfe")]
    assert read.records[0] == {"a": "�", "b": "�"}


def test_nul_byte_inside_line_recovers(tmp_path):
    payload = b'{"type": "user", "text": "a\x00b"}'
    path = write(tmp_path, "s.jsonl", payload + b"\n" + good(1) + b"\n")

    read = sr.read_jsonl(path)
    assert read.record_count_read == 2
    assert read.record_count_skipped == 1
    assert findings_of(read, "parse-error")[0]["reason"] == "invalid-control-character"
    assert len(read.records) == 1


def test_oversized_line_is_skipped_not_fatal(tmp_path):
    limits = sr.ReadLimits(max_line_bytes=1024)
    big = json.dumps({"blob": "x" * 4096}).encode()
    path = write(tmp_path, "s.jsonl", good(1) + b"\n" + big + b"\n" + good(1) + b"\n")

    read = sr.read_jsonl(path, limits=limits)
    assert read.record_count_read == 3
    assert read.record_count_skipped == 1
    assert len(read.records) == 2               # the good records survive
    bounds = findings_of(read, "bounds-exceeded")
    assert len(bounds) == 1
    assert bounds[0]["reason"] == "line-too-long"
    assert bounds[0]["byte_length"] == len(big)
    assert bounds[0]["byte_offset"] == len(good(1)) + 1


def test_oversized_unterminated_line_at_eof(tmp_path):
    """The over-long line never gets a delimiter; it must still be reported."""
    limits = sr.ReadLimits(max_line_bytes=64)
    path = write(tmp_path, "s.jsonl", good(1) + b"\n" + b"x" * 5000)

    read = sr.read_jsonl(path, limits=limits)
    assert read.record_count_skipped == 1
    assert findings_of(read, "bounds-exceeded")[0]["byte_length"] == 5000


def _line(n: int) -> bytes:
    """A JSON object of exactly n bytes."""
    return b'{"a":"' + b"x" * (n - 8) + b'"}'


@pytest.mark.parametrize("chunk", [1, 7, 1 << 16])
@pytest.mark.parametrize("tail", [b"\n", b""])
def test_a_leading_mark_does_not_count_toward_the_line_bound(tmp_path, chunk, tail):
    """DSL rc3 §4: a UTF-8 mark at the very start of the input is ignored, so
    the first line at the bound is kept with or without one (ruled
    2026-10-19); one byte over is skipped either way. Only the input's start
    is exempt: a later line beginning with the mark bytes counts them.
    Chunk size 1 drives the bound check for a line still growing."""
    bom, cap = codecs.BOM_UTF8, 64
    for mark in (b"", bom):
        lines = list(sr._iter_lines(io.BytesIO(mark + _line(cap) + tail), cap, chunk_size=chunk))
        assert [raw for _, raw, _ in lines] == [mark + _line(cap)], (mark, lines)
        lines = list(sr._iter_lines(io.BytesIO(mark + _line(cap + 1) + tail), cap, chunk_size=chunk))
        assert [raw for _, raw, _ in lines] == [None], (mark, lines)
    later = _line(10) + b"\n" + bom + _line(cap - 2) + tail   # cap + 1 bytes with the mark
    assert [raw for _, raw, _ in sr._iter_lines(io.BytesIO(later), cap, chunk_size=chunk)] == [_line(10), None]
    # A bound below the mark's length: a partial mark at a chunk boundary is
    # decided as the whole mark is, so every chunk size agrees with a whole read.
    for tiny in (0, 1, 2):
        data = bom + b"x" * tiny + tail
        whole = list(sr._iter_lines(io.BytesIO(data), tiny, chunk_size=1 << 16))
        assert whole[0][1] == bom + b"x" * tiny, (tiny, whole)
        assert list(sr._iter_lines(io.BytesIO(data), tiny, chunk_size=chunk)) == whole, (tiny, chunk)
    for part in (bom[:1], bom[:2]):   # an input that is only a partial mark: no allowance at its end
        for tiny in range(len(part)):
            got = list(sr._iter_lines(io.BytesIO(part + tail), tiny, chunk_size=chunk))
            assert [raw for _, raw, _ in got] == [None], (part, tiny, got)
    later = b"x\n" + bom[:2] + tail   # a later line that is a partial mark: no allowance
    whole = list(sr._iter_lines(io.BytesIO(later), 1, chunk_size=1 << 16))
    assert [raw for _, raw, _ in whole] == [b"x", None], whole
    assert list(sr._iter_lines(io.BytesIO(later), 1, chunk_size=chunk)) == whole, chunk
    path = write(tmp_path, "s.jsonl", bom + _line(cap) + b"\n" + _line(cap) + tail)
    read = sr.read_jsonl(path, limits=sr.ReadLimits(max_line_bytes=cap))
    assert read.record_count_read == 2 and read.record_count_skipped == 0, read


def _reference_lines(data: bytes, bound: int) -> list[tuple[int, bytes | None, int]]:
    """The line rule written out: LF, CR and CRLF end a line, a final line
    without a delimiter counts if not empty, and a line longer than the
    bound (a UTF-8 mark at offset 0 not counted) is dropped."""
    out, i, start = [], 0, 0
    while i <= len(data):
        if i == len(data) or data[i] in b"\r\n":
            if i == len(data) and i == start:
                break
            line = data[start:i]
            allow = len(codecs.BOM_UTF8) if start == 0 and line.startswith(codecs.BOM_UTF8) else 0
            out.append((start, None if len(line) - allow > bound else line, len(line)))
            if i == len(data):
                break
            i += 2 if data[i:i + 2] == b"\r\n" else 1
            start = i
        else:
            i += 1
    return out


def test_a_pending_cr_does_not_count_toward_the_bound_nor_vanish_while_dropping():
    """A CR at the end of the read buffer waits for the next byte (CR or
    CRLF). It is not part of the line, so a line exactly at the bound before
    it is kept; and while a long line is being dropped, it is not discarded
    with the line's bytes, so the next line still starts after it. Checked
    against the line rule written out, over every input of up to 6 bytes
    from {x, CR, LF}, bounds 0 to 3, and every input of up to 5 bytes from
    those and the mark's bytes, bounds 0 to 4; at chunk sizes 1, 2, 3 and
    65536, and with reads of random size (the mark and
    short reads were checked only outside the suite)."""
    import itertools  # noqa: PLC0415
    import random  # noqa: PLC0415
    rng = random.Random(20261019)

    class ShortReads(io.BytesIO):
        def read(self, n=-1):
            return super().read(rng.randint(1, max(1, n)))

    for alphabet, longest, bounds in (((b"x", b"\r", b"\n"), 6, range(4)),
                                      ((b"x", b"\r", b"\n", b"\xef", b"\xbb", b"\xbf"), 5, range(5))):
        for n in range(longest + 1):
            for t in itertools.product(alphabet, repeat=n):
                data = b"".join(t)
                for bound in bounds:
                    want = _reference_lines(data, bound)
                    for chunk in (1, 2, 3, 1 << 16):
                        got = list(sr._iter_lines(io.BytesIO(data), bound, chunk_size=chunk))
                        assert got == want, (data, bound, chunk, got, want)
                    got = list(sr._iter_lines(ShortReads(data), bound, chunk_size=4))
                    assert got == want, (data, bound, "short reads", got, want)
    line = b"x" * 64   # the reported cases, at the default chunk size
    assert [r for _, r, _ in sr._iter_lines(io.BytesIO(line + b"\r"), 64)] == [line]
    assert [r for _, r, _ in sr._iter_lines(io.BytesIO(line + b"\r\n"), 64, chunk_size=65)] == [line]


# --- the delimiter set is pinned --------------------------------------------


@pytest.mark.parametrize("char", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85",
                                  " ", " "])
def test_splitlines_characters_do_not_end_a_record(tmp_path, char):
    """`str.splitlines()` splits on all of these; the contract does not.

    A record carrying one of them raw becomes several parse errors for a reader
    that calls `splitlines()` and one record for a reader that iterates the file
    handle, which is why `record_count_read` was incomparable across adapters.
    """
    assert len(f"a{char}b".splitlines()) == 2          # kill assertion
    payload = json.dumps({"text": f"a{char}b"}, ensure_ascii=False).encode()
    path = write(tmp_path, "s.jsonl", payload + b"\n")

    read = sr.read_jsonl(path)
    assert read.record_count_read == 1
    assert read.record_count_skipped == 0
    assert read.records[0]["text"] == f"a{char}b"


@pytest.mark.parametrize("delim", [b"\n", b"\r", b"\r\n"])
def test_all_three_delimiters_end_a_record(tmp_path, delim):
    path = write(tmp_path, "s.jsonl", delim.join(
        [json.dumps({"i": i}).encode() for i in range(3)]) + delim)
    read = sr.read_jsonl(path)
    assert [r["i"] for r in read.records] == [0, 1, 2]
    assert read.record_count_read == 3


def test_cr_at_chunk_boundary_is_not_a_double_delimiter():
    """CRLF split across two reads must stay one delimiter.

    Driven at chunk_size=1 so every byte lands in its own read, which is the
    worst case for the CR-then-wait branch.
    """
    data = b'{"i": 0}\r\n{"i": 1}\r\n'
    lines = list(sr._iter_lines(io.BytesIO(data), max_line_bytes=1 << 20, chunk_size=1))
    assert [raw for _, raw, _ in lines] == [b'{"i": 0}', b'{"i": 1}']
    assert [offset for offset, _, _ in lines] == [0, 10]


def test_blank_lines_are_not_records(tmp_path):
    path = write(tmp_path, "s.jsonl", b'\n\n{"i": 0}\n\n   \n{"i": 1}\n\n')
    read = sr.read_jsonl(path)
    assert read.record_count_read == 2
    assert read.record_indices == [0, 1]


def test_only_json_whitespace_makes_a_line_blank(tmp_path):
    # DSL rc3 §15: a blank jsonl line is empty or JSON whitespace
    # only (space, tab, LF, CR). bytes.strip() also took VT and FF, so a line
    # of only U+000B or U+000C was ignored unreported and a record followed
    # by one was accepted; both are lines that are not well-formed JSON,
    # skipped as unreadable. Space, tab and CR around a record stay allowed.
    path = write(tmp_path, "s.jsonl", b' \t\r\n{"i": 0}\n\x0c\n\x0b\n{"i": 1}\x0c\n\x0b{"i": 2}\n \t{"i": 3}\t \r\n')
    read = sr.read_jsonl(path)
    assert [r["i"] for r in read.records] == [0, 3]
    assert read.record_count_read == 6 and read.record_count_skipped == 4
    assert [f["record_index"] for f in findings_of(read, "parse-error")] == [1, 2, 3, 4]
    # The lead for a decode error's offset is cut with the same set as the
    # line: a leading FF is neither lead nor stripped, so the byte is placed
    # right (17 on the parent too, where FF was both; this pins the two
    # staying together, not the change above).
    path = write(tmp_path, "d.jsonl", b'{"i": 0}\n\x0c{"s": "\xff"}\n')
    read = sr.read_jsonl(path)
    assert [f["byte_offset"] for f in findings_of(read, "decode-error")] == [9 + 8], read.findings


# --- fail-closed conditions -------------------------------------------------


def test_empty_source_fails_closed(tmp_path):
    path = write(tmp_path, "s.jsonl", b"\n\n")
    with pytest.raises(sr.SourceUnreadable) as exc:
        sr.read_jsonl(path)
    assert exc.value.reason == "no-records"
    # ...unless the caller declares an empty source legitimate.
    assert sr.read_jsonl(path, require_records=False).record_count_read == 0


def test_file_too_large_fails_closed(tmp_path):
    path = write(tmp_path, "s.jsonl", good(10))
    with pytest.raises(sr.SourceUnreadable) as exc:
        sr.read_jsonl(path, limits=sr.ReadLimits(max_file_bytes=8))
    assert exc.value.reason == "file-too-large"


def test_record_count_exceeded_fails_closed(tmp_path):
    path = write(tmp_path, "s.jsonl", good(10))
    with pytest.raises(sr.SourceUnreadable) as exc:
        sr.read_jsonl(path, limits=sr.ReadLimits(max_records=3))
    assert exc.value.reason == "record-count-exceeded"


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(sr.SourceUnreadable) as exc:
        sr.read_jsonl(tmp_path / "absent.jsonl")
    assert exc.value.reason == "not-a-file"


def test_fatal_reason_vocabulary_is_closed():
    with pytest.raises(ValueError):
        sr.SourceUnreadable("something-went-wrong", "/x")


def test_json_reader_treats_parse_failure_as_fatal(tmp_path):
    path = write(tmp_path, "pre.json", b"[{}, {")
    with pytest.raises(sr.SourceUnreadable) as exc:
        sr.read_json(path)
    assert exc.value.reason == "malformed-json"


def test_json_reader_records_decode_damage_without_failing(tmp_path):
    path = write(tmp_path, "pre.json", b'[{"t": "caf\xe9"}]')
    read = sr.read_json(path)
    assert read.record_count_read == 1
    assert findings_of(read, "decode-error")[0]["reason"] == "invalid-continuation-byte"
    assert read.records[0]["t"] == "caf�"


# --- counts, classes and the findings cap -----------------------------------


def test_discard_class_from_untrusted_string_lands_in_other(tmp_path):
    read = sr.read_jsonl(write(tmp_path, "s.jsonl", good(3) + b"\n"))
    read.note_discard("file-history-snapshot")
    read.note_discard("type with spaces")
    read.note_discard("\x1b[31mred")
    read.note_discard("x" * 65)
    read.note_discard(None)
    entry = read.to_source_entry()
    assert entry["discarded_by_class"] == {"file-history-snapshot": 1, "other": 4}
    assert entry["record_count_discarded"] == 5


def test_count_invariant_is_checked(tmp_path):
    read = sr.read_jsonl(write(tmp_path, "s.jsonl", good(2) + b"\n"))
    read.note_discard("a", count=3)               # 3 discarded of 2 read
    with pytest.raises(ValueError):
        sr.check_count_invariant(read.to_source_entry())


def test_emitted_may_exceed_read(tmp_path):
    """Preprocessors legitimately merge and split, so this is not constrained."""
    read = sr.read_jsonl(write(tmp_path, "s.jsonl", good(2) + b"\n"))
    read.note_emitted(9)
    sr.check_count_invariant(read.to_source_entry())


def test_findings_are_capped_and_truncation_is_recorded(tmp_path):
    body = b"\n".join(b"{broken" for _ in range(50))
    path = write(tmp_path, "s.jsonl", body + b"\n")

    read = sr.read_jsonl(path, limits=sr.ReadLimits(max_findings=10))
    assert read.record_count_read == 50
    assert read.record_count_skipped == 50        # every skip is still counted
    truncated = findings_of(read, "findings-truncated")
    assert len(truncated) == 1
    assert truncated[0]["suppressed_count"] == 40
    assert truncated[0]["record_index"] == 10     # locator, per proposal §5
    assert len(findings_of(read, "parse-error")) == 10


def test_every_finding_carries_a_locator(tmp_path):
    payload = b'{"a": "\xff"}\n{broken\n' + b"x" * 200 + b"\n"
    path = write(tmp_path, "s.jsonl", payload)
    read = sr.read_jsonl(path, limits=sr.ReadLimits(max_line_bytes=100))
    assert read.findings                          # non-vacuous
    for finding in read.findings:
        assert "byte_offset" in finding or "record_index" in finding


# --- assertion assembly -----------------------------------------------------


def test_assertion_shape_and_ordering(tmp_path):
    primary = sr.read_jsonl(write(tmp_path, "p.jsonl", good(4) + b"\n"))
    contrib_b = sr.read_jsonl(write(tmp_path, "b.jsonl", b'{"i": 9}\n'),
                              role="contributing")
    contrib_a = sr.read_jsonl(write(tmp_path, "a.jsonl", b'{"i": 8}\n'),
                              role="contributing")
    primary.note_emitted(4)

    assertion = sr.build_assertion(
        [contrib_b, primary, contrib_a],
        asserted_by="minitrace-adapter/test",
        converter_version="0.4.0",
    )

    assert assertion["sources"][0]["role"] == "primary"
    contrib_hashes = [s["content_hash"] for s in assertion["sources"][1:]]
    assert contrib_hashes == sorted(contrib_hashes)
    assert [c["check"] for c in assertion["checks"]] == ["bounds", "jsonl-parse", "utf8-decode"]
    assert all(c["status"] == "ran" for c in assertion["checks"])
    assert assertion["read_findings"] == []       # meaningful only with the roster
    assert assertion["chain_findings"] == []
    assert "asserted_at" not in assertion


def test_source_index_follows_reordering(tmp_path):
    primary = sr.read_jsonl(write(tmp_path, "p.jsonl", good(2) + b"\n"))
    damaged = sr.read_jsonl(write(tmp_path, "d.jsonl", b"{broken\n" + good(1) + b"\n"),
                            role="contributing")
    assertion = sr.build_assertion(
        [damaged, primary],                       # damaged passed first
        asserted_by="t", converter_version="0.4.0",
    )
    assert assertion["sources"][0]["content_hash"] == primary.content_hash
    assert len(assertion["read_findings"]) == 1
    assert assertion["read_findings"][0]["source_index"] == 1   # remapped, not 0


def test_assertion_is_byte_identical_across_runs(tmp_path):
    """Determinism: the assertion must not carry run-varying content.

    `asserted_at` is the only field permitted to vary (proposal §7.1) and is
    omitted here, so two runs over the same bytes must serialise identically.
    """
    path = write(tmp_path, "s.jsonl", good(5) + b"\n{broken\n" + b'{"a": "\xff"}\n')
    runs = []
    for _ in range(2):
        read = sr.read_jsonl(path)
        read.note_discard("last-prompt")
        read.note_emitted(4)
        runs.append(json.dumps(sr.build_assertion(
            [read], asserted_by="t", converter_version="0.4.0"), sort_keys=True))
    assert runs[0] == runs[1]
    assert "parse-error" in runs[0]               # non-vacuous: damage was seen


def test_roster_disagreement_across_sources_becomes_incomplete(tmp_path):
    a = sr.read_jsonl(write(tmp_path, "a.jsonl", good(1) + b"\n"))
    b = sr.read_jsonl(write(tmp_path, "b.jsonl", good(1) + b"\n"), role="contributing")
    b.checks = [{"check": "jsonl-parse", "status": "not-implemented"},
                {"check": "utf8-decode", "status": "ran"},
                {"check": "bounds", "status": "ran"}]
    assertion = sr.build_assertion([a, b], asserted_by="t", converter_version="0.4.0")
    status = {c["check"]: c["status"] for c in assertion["checks"]}
    assert status["jsonl-parse"] == "incomplete"
    assert status["utf8-decode"] == "ran"
