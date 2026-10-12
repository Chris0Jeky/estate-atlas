"""Bounded binary reads for local record files, before UTF-8 decoding or JSON parsing."""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

_READ_CHUNK = 64 * 1024
_NEWLINE = re.compile(rb'\r\n?|\n')


def bounded_lines(path: Path, line_limit: int, file_limit: int) -> Iterator[tuple[int, bytes]]:
    """Yield physical lines without terminators; budgets count bytes, not decoded characters.

    LF, CRLF and CR are accepted, including a CRLF split between chunks. The line budget
    excludes the terminator but includes any BOM. The file budget counts every input byte.
    Neither a file stat nor an unbounded line read is used to establish the limit.
    """
    number, total = 1, 0
    pending = bytearray()
    skip_lf = False

    def append(part: bytes) -> None:
        if len(pending) + len(part) > line_limit:
            raise ValueError(f'{path}:{number}: line byte limit exceeded ({line_limit})')
        pending.extend(part)

    try:
        with path.open('rb') as stream:
            while True:
                chunk = stream.read(min(_READ_CHUNK, line_limit + 1, file_limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > file_limit:
                    raise ValueError(f'{path}: file byte limit exceeded ({file_limit})')
                if skip_lf:
                    if chunk.startswith(b'\n'):
                        chunk = chunk[1:]
                    skip_lf = False
                position = 0
                for end in _NEWLINE.finditer(chunk):
                    append(chunk[position:end.start()])
                    yield number, bytes(pending)
                    pending.clear()
                    number += 1
                    skip_lf = end.group() == b'\r' and end.end() == len(chunk)
                    position = end.end()
                append(chunk[position:])
            if pending:
                yield number, bytes(pending)
    except OSError as exc:
        raise ValueError(f'cannot read {path}: {exc.strerror or type(exc).__name__}') from None


def bounded_document(path: Path, limit: int) -> bytes:
    """Read at most the links budget plus one sentinel byte, then refuse any overflow."""
    try:
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise ValueError(f'cannot read {path}: {exc.strerror or type(exc).__name__}') from None
    if len(raw) > limit:
        raise ValueError(f'{path}: links file byte limit exceeded ({limit})')
    return raw
