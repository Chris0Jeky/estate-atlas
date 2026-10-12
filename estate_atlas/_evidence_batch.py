"""Internal bounded groups for read-only Git evidence, using literal paths and exact object IDs."""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Iterator
from typing import Any

MAX_PATHS = 64
MAX_PATH_BYTES = 8192
MAX_CONTENT_BYTES = 16 * 1024 * 1024
_OID = re.compile(rb'(?:[0-9a-f]{40}|[0-9a-f]{64})')
_SIZE = re.compile(rb'[0-9]+')
Result = tuple[bytes | None, str]


def _path_groups(names: list[str]) -> Iterator[list[str]]:
    group: list[str] = []
    size = 0
    for name in names:
        cost = len(name.encode('utf-8')) + 3  # allowance for quoting and the argument separator
        related = any(name.startswith(old + '/') or old.startswith(name + '/') for old in group)
        if group and (len(group) >= MAX_PATHS or size + cost > MAX_PATH_BYTES or related):
            yield group
            group, size = [], 0
        group.append(name)
        size += cost
    if group:
        yield group


def _metadata(raw: bytes, names: list[str], limit: int) -> tuple[dict[str, tuple[bytes, int]], dict[str, Result]]:
    """Reject malformed output as a whole; missing names and non-files retain explicit diagnoses."""
    if raw and not raw.endswith(b'\0'):
        raise ValueError('git ls-tree returned truncated metadata')
    expected = {name.encode('utf-8'): name for name in names}
    found: set[str] = set()
    admitted: dict[str, tuple[bytes, int]] = {}
    results: dict[str, Result] = {name: (None, 'file is absent at origin') for name in names}
    for record in raw.split(b'\0')[:-1]:
        fields, separator, raw_name = record.partition(b'\t')
        parts = fields.split()
        if not separator or raw_name not in expected or len(parts) != 4:
            raise ValueError('git ls-tree returned invalid metadata')
        name = expected[raw_name]
        mode, kind, oid, size = parts
        if name in found or not _OID.fullmatch(oid):
            raise ValueError('git ls-tree returned invalid metadata')
        found.add(name)
        if mode not in (b'100644', b'100755') or kind != b'blob':
            results[name] = (None, 'not a regular file at origin')
            continue
        if not _SIZE.fullmatch(size):
            raise ValueError('git ls-tree returned an invalid size')
        length = int(size)
        if length > limit:
            results[name] = (None, f'file is larger than {limit // (1024 * 1024)} MiB')
        else:
            admitted[name] = (oid, length)
    return admitted, results


def _content_groups(objects: dict[bytes, int]) -> Iterator[list[tuple[bytes, int]]]:
    group: list[tuple[bytes, int]] = []
    size = 0
    for oid, length in objects.items():
        if group and (len(group) >= MAX_PATHS or size + length > MAX_CONTENT_BYTES):
            yield group
            group, size = [], 0
        # A single object is already bounded by the caller's per-file evidence limit.
        group.append((oid, length))
        size += length
    if group:
        yield group


def _frames(raw: bytes, group: list[tuple[bytes, int]]) -> dict[bytes, bytes]:
    """All requested frames must match before any member of this response can count as evidence."""
    output: dict[bytes, bytes] = {}
    position = 0
    for oid, length in group:
        header = oid + b' blob ' + str(length).encode('ascii') + b'\n'
        if not raw.startswith(header, position):
            raise ValueError('git cat-file returned invalid batch framing')
        start = position + len(header)
        end = start + length
        if raw[end:end + 1] != b'\n':
            raise ValueError('git cat-file returned invalid batch framing')
        output[oid] = raw[start:end]
        position = end + 1
    if position != len(raw):
        raise ValueError('git cat-file returned invalid batch framing')
    return output


def read_many(path: str, commit: str, paths: Iterable[str], run: Callable[..., Any],
              limit: int) -> dict[str, Result]:
    """Read requested files only; never recursively enumerate a requested directory or follow a link.

    ``run`` is the existing sanitized, timeout-bounded Git broker. Metadata arguments and planned
    content responses are grouped independently. The per-call cache still retains the requested
    evidence, just as the single-file path did; these budgets are not a total process-memory limit.
    """
    names = sorted(set(paths))
    results: dict[str, Result] = {}
    admitted_names = []
    for name in names:
        if len(name.encode('utf-8')) + 3 > MAX_PATH_BYTES:
            results[name] = (None, 'evidence path exceeds Git argument budget')
        else:
            admitted_names.append(name)
    for group in _path_groups(admitted_names):
        try:
            listed = run(path, 'ls-tree', '-l', '-z', commit, '--', *group)
            if listed.returncode != 0:
                results.update((name, (None, 'git ls-tree failed')) for name in group)
                continue
            admitted, diagnosed = _metadata(listed.stdout, group, limit)
            results.update(diagnosed)
            objects: dict[bytes, int] = {}
            for oid, length in admitted.values():
                if oid in objects and objects[oid] != length:
                    raise ValueError('git ls-tree returned inconsistent object sizes')
                objects[oid] = length
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.update((name, (None, f'git failed: {type(exc).__name__}')) for name in group)
            continue
        except ValueError as exc:
            results.update((name, (None, str(exc))) for name in group)
            continue
        for content_group in _content_groups(objects):
            ids = {oid for oid, _ in content_group}
            members = [name for name, (oid, _) in admitted.items() if oid in ids]
            try:
                shown = run(path, 'cat-file', '--batch', input=b''.join(oid + b'\n' for oid, _ in content_group))
                if shown.returncode != 0:
                    results.update((name, (None, 'git cat-file failed')) for name in members)
                    continue
                parsed = _frames(shown.stdout, content_group)
            except (OSError, subprocess.TimeoutExpired) as exc:
                results.update((name, (None, f'git failed: {type(exc).__name__}')) for name in members)
                continue
            except ValueError as exc:
                results.update((name, (None, str(exc))) for name in members)
                continue
            results.update((name, (parsed[admitted[name][0]], '')) for name in members)
    return results
