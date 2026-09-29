"""Inspect local movie structure without resolving external media references."""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

VIDEO_INSPECTION_VERSION = 1
MAX_COMPRESSED_METADATA = 16 * 1024 * 1024
MOVIE_EXTENSIONS = {".mov", ".mp4", ".m4v"}


def has_external_references(path: Path) -> bool:
    if path.suffix.lower() not in MOVIE_EXTENSIONS:
        return False
    with path.open("rb") as handle:
        if handle.read(4) == b"\x00\x05\x16\x07":
            raise ValueError("AppleDouble metadata sidecar, not a video; left unchanged")
        end = path.stat().st_size
        if end == 0:
            raise ValueError("Empty file, not a playable movie; left unchanged")
        count = 0
        expanded_bytes = 0
        external = False
        movie = False

        def read(offset: int, size: int) -> bytes:
            handle.seek(offset)
            data = handle.read(size)
            if len(data) != size:
                raise ValueError("Truncated movie atom")
            return data

        def atoms(start: int, limit: int, depth: int = 0):
            nonlocal count
            if depth > 16:
                raise ValueError("Movie atom nesting exceeds limit")
            while start < limit:
                count += 1
                if count > 100000 or limit - start < 8:
                    raise ValueError("Invalid movie atom structure")
                size, kind = struct.unpack(">I4s", read(start, 8))
                header = 8
                if size == 1:
                    if limit - start < 16:
                        raise ValueError("Truncated extended movie atom")
                    size = struct.unpack(">Q", read(start + 8, 8))[0]
                    header = 16
                if size == 0:
                    size = limit - start
                if size < header or start + size > limit:
                    raise ValueError("Movie atom exceeds container bounds")
                yield kind, start + header, start + size
                start += size

        def walk(start: int, limit: int, depth: int = 0):
            nonlocal external, movie, handle, expanded_bytes
            for kind, body, stop in atoms(start, limit, depth):
                if kind == b"cmov":
                    fields = {}
                    for field, field_start, field_end in atoms(body, stop, depth + 1):
                        if field in fields:
                            raise ValueError("Duplicate compressed movie metadata field")
                        fields[field] = (field_start, field_end)
                    if set(fields) != {b"dcom", b"cmvd"}:
                        raise ValueError("Incomplete compressed movie metadata")
                    begin, finish = fields[b"dcom"]
                    if finish - begin != 4 or read(begin, 4) != b"zlib":
                        raise ValueError("Unsupported movie metadata compression")
                    begin, finish = fields[b"cmvd"]
                    if finish - begin < 4 or finish - begin > MAX_COMPRESSED_METADATA:
                        raise ValueError("Invalid compressed movie metadata size")
                    expected = struct.unpack(">I", read(begin, 4))[0]
                    expanded_bytes += expected
                    if expected < 8 or expanded_bytes > MAX_COMPRESSED_METADATA:
                        raise ValueError("Expanded movie metadata exceeds size limit")
                    decoder = zlib.decompressobj()
                    try:
                        data = decoder.decompress(read(begin + 4, finish - begin - 4), expected + 1)
                    except zlib.error as exc:
                        raise ValueError("Invalid compressed movie metadata") from exc
                    if (
                        len(data) != expected
                        or not decoder.eof
                        or decoder.unused_data
                        or data[4:8] != b"moov"
                    ):
                        raise ValueError("Compressed movie metadata length or structure mismatch")
                    outer = handle
                    try:
                        handle = io.BytesIO(data)
                        walk(0, len(data), depth + 1)
                    finally:
                        handle.close()
                        handle = outer
                if kind == b"moov":
                    movie = True
                if kind in {b"moov", b"trak", b"mdia", b"minf", b"dinf", b"rmra", b"rmda"}:
                    walk(body, stop, depth + 1)
                elif kind == b"dref":
                    if stop - body < 8:
                        raise ValueError("Truncated data reference table")
                    version, entries = struct.unpack(">II", read(body, 8))
                    if version >> 24:
                        raise ValueError("Unsupported data reference version")
                    actual = 0
                    for _, entry, entry_end in atoms(body + 8, stop, depth + 1):
                        if entry_end - entry < 4:
                            raise ValueError("Truncated data reference flags")
                        flags = int.from_bytes(read(entry, 4), "big")
                        if flags >> 24:
                            raise ValueError("Unsupported data reference entry version")
                        external |= not bool(flags & 1)
                        actual += 1
                    if actual != entries:
                        raise ValueError("Invalid data reference count")
                elif kind == b"rdrf":
                    if stop - body < 12:
                        raise ValueError("Truncated reference movie")
                    flags, _, length = struct.unpack(">I4sI", read(body, 12))
                    if length > stop - body - 12:
                        raise ValueError("Truncated reference movie target")
                    external |= not bool(flags & 1)

        walk(0, end)
        if not movie:
            raise ValueError("No movie atom found")
        return external
