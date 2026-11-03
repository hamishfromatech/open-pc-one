"""Framing is LF-only. These are the cases a Node ``readline`` implementation fails."""

from __future__ import annotations

import json

from openpc_engine.jsonl import MAX_RECORD_BYTES, JsonlSplitter, encode_record


def test_splits_only_on_lf_and_preserves_unicode_separators():
    payload = json.dumps({"text": "line\u2028with\u2029separators"}, ensure_ascii=False)
    splitter = JsonlSplitter()
    records = list(splitter.feed((payload + "\n").encode()))
    assert len(records) == 1
    assert json.loads(records[0])["text"] == "line\u2028with\u2029separators"


def test_multiple_records_in_one_chunk():
    splitter = JsonlSplitter()
    records = list(splitter.feed(b'{"a":1}\n{"a":2}\n{"a":3}\n'))
    assert [json.loads(r)["a"] for r in records] == [1, 2, 3]


def test_partial_record_is_buffered_until_terminated():
    splitter = JsonlSplitter()
    assert list(splitter.feed(b'{"a":')) == []
    assert splitter.pending_bytes == 5
    assert list(splitter.feed(b'1}\n')) == ['{"a":1}']


def test_crlf_is_accepted_and_stripped():
    splitter = JsonlSplitter()
    assert list(splitter.feed(b'{"a":1}\r\n')) == ['{"a":1}']


def test_multibyte_character_split_across_chunks():
    raw = '{"t":"é"}\n'.encode()
    splitter = JsonlSplitter()
    # Split inside the two-byte é sequence.
    first = raw[:7]
    second = raw[7:]
    records = list(splitter.feed(first))
    records += list(splitter.feed(second))
    assert len(records) == 1
    assert json.loads(records[0])["t"] == "é"


def test_blank_line_yields_empty_record_that_is_ignored_upstream():
    splitter = JsonlSplitter()
    assert list(splitter.feed(b"\n")) == [""]


def test_oversized_tail_is_discarded_until_its_terminator():
    splitter = JsonlSplitter(max_record_bytes=16)
    list(splitter.feed(b"x" * 32))
    assert splitter.pending_bytes == 0
    assert splitter.oversized_lines == 1
    # Memory stays bounded: the rest of the runaway record is skipped up to its
    # LF, because those bytes cannot be part of a valid exchange either.
    assert list(splitter.feed(b"still-the-same-record\n")) == []
    # Framing recovers on the next record.
    assert list(splitter.feed(b'{"ok":1}\n')) == ['{"ok":1}']


def test_finish_flushes_unterminated_tail():
    splitter = JsonlSplitter()
    assert list(splitter.feed(b'{"a":1}')) == []
    assert list(splitter.finish()) == ['{"a":1}']
    assert list(splitter.finish()) == []


def test_encode_record_is_single_line_and_ascii_escaped():
    encoded = encode_record({"message": "a\u2028b"})
    assert encoded.count(b"\n") == 1
    assert encoded.endswith(b"\n")
    # Escaped, so even a non-compliant splitter cannot break the record.
    assert b"\xe2\x80\xa8" not in encoded
    assert json.loads(encoded)["message"] == "a\u2028b"


def test_frame_cap_matches_websocket_cap():
    assert MAX_RECORD_BYTES == 16 * 1024 * 1024
