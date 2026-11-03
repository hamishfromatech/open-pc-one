"""Strict JSONL framing for the a-coder-cli RPC wire.

The engine speaks JSONL to a child process over a pipe. Framing is LF (0x0A)
only. JavaScript's ``readline`` is **not** protocol compliant: it also splits on
U+2028/U+2029, which are legal unescaped characters inside a JSON string. A
prompt or tool result containing either would be torn in half mid-record.

This module therefore splits on raw bytes. Byte 0x0A can never appear inside a
multi-byte UTF-8 sequence, so decoding each completed record on its own is safe
and needs no incremental decoder.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

__all__ = [
    "MAX_RECORD_BYTES",
    "JsonlSplitter",
    "encode_record",
    "iter_records",
]

#: Upper bound for a single unterminated record. Matches the 16 MiB WebSocket
#: frame cap; anything longer cannot be part of a valid exchange and is dropped
#: rather than buffered forever.
MAX_RECORD_BYTES = 16 * 1024 * 1024

_LF = b"\n"
_CR = b"\r"


class JsonlSplitter:
    """Incremental byte-level LF-only JSONL splitter.

    ``feed()`` accepts arbitrarily chunked bytes and yields complete records as
    ``str`` (without the trailing LF, and with a trailing CR stripped so CRLF
    input is accepted). An unterminated tail is retained until the next chunk.

    Oversized unterminated tails are discarded (with ``oversized_lines``
    incremented) because they can never be accepted by the engine.
    """

    def __init__(self, max_record_bytes: int = MAX_RECORD_BYTES) -> None:
        self._buffer = bytearray()
        self._max = max_record_bytes
        self.oversized_lines = 0
        # True while we are discarding the remainder of an oversized record.
        self._discarding = False

    def feed(self, chunk: bytes | bytearray | memoryview) -> Iterable[str]:
        if self._discarding:
            # Drop everything up to and including the next LF, then resume.
            view = bytes(chunk)
            idx = view.find(_LF)
            if idx == -1:
                return ()
            self._discarding = False
            chunk = view[idx + 1 :]
            if not chunk:
                return ()

        self._buffer.extend(chunk)

        out: list[str] = []
        while True:
            idx = self._buffer.find(_LF)
            if idx == -1:
                if len(self._buffer) > self._max:
                    self._buffer.clear()
                    self._discarding = True
                    self.oversized_lines += 1
                break
            raw = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            if len(raw) > self._max:
                self.oversized_lines += 1
                continue
            if raw.endswith(_CR):
                raw = raw[:-1]
            out.append(raw.decode("utf-8", errors="replace"))
        return out

    def finish(self) -> Iterable[str]:
        """Flush a trailing record that was not LF-terminated."""
        if not self._buffer:
            return ()
        raw = bytes(self._buffer)
        self._buffer.clear()
        if self._discarding:
            self._discarding = False
            return ()
        if raw.endswith(_CR):
            raw = raw[:-1]
        if not raw:
            return ()
        return (raw.decode("utf-8", errors="replace"),)

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)


def encode_record(value: Any) -> bytes:
    """Serialise one RPC command as a single LF-terminated JSONL record.

    ``ensure_ascii=True`` keeps U+2028/U+2029 escaped on the wire, so even a
    client that (incorrectly) splits on them cannot break the stream.
    """
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + _LF


def iter_records(stream: Any) -> Iterable[str]:
    """Yield records from a blocking binary stream (test/CLI convenience).

    Uses ``read1`` when available: ``BufferedReader.read(n)`` blocks until *n*
    bytes or EOF, which deadlocks a command/reply protocol.
    """
    splitter = JsonlSplitter()
    read = getattr(stream, "read1", None) or stream.read
    while True:
        chunk = read(65536)
        if not chunk:
            break
        yield from splitter.feed(chunk)
    yield from splitter.finish()
