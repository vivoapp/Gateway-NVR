"""MP4 helpers for clips the API prepares.

- problem(): a whole-file check: the top-level boxes add up to the file size, with a movie header
  and media data. The same rule as the recorder's tools/mp4box.py.
- decode_times(), kept_size(), rebase_fragment(): join stored fragmented-MP4 pieces into one clip
  that starts at time 0 (the mock recorder, M18).
"""
from __future__ import annotations

import struct
from pathlib import Path


def problem(path: Path) -> str | None:
    """None when the file is a whole MP4, else a short reason."""
    try:
        size = path.stat().st_size
        seen, pos = set(), 0
        with open(path, "rb") as f:
            while pos < size:
                f.seek(pos)
                head = f.read(16)
                if len(head) < 8:
                    return "truncated box header"
                box_size, kind = struct.unpack(">I4s", head[:8])
                if box_size == 1:
                    if len(head) < 16:
                        return "truncated box header"
                    box_size = struct.unpack(">Q", head[8:16])[0]
                elif box_size == 0:
                    box_size = size - pos
                if box_size < 8 or pos + box_size > size:
                    return f"box {kind!r} runs past the end"
                seen.add(kind)
                pos += box_size
    except OSError as exc:
        return f"cannot read: {exc.strerror}"
    if b"moov" not in seen:
        return "no movie header (moov)"
    if b"mdat" not in seen:
        return "no media data (mdat)"
    return None


# ---------------------------------------------------------------- fragments for a clip

_DROP = (b"styp", b"sidx")          # per-piece boxes that do not belong inside one joined file


def _boxes(data: bytes, pos: int = 0, end: int | None = None):
    """(kind, start, size, header size) of each box in data[pos:end]."""
    end = len(data) if end is None else end
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise ValueError(f"box {kind!r} runs past the end")
        yield kind, pos, size, hdr
        pos += size


def _tfdts(data: bytes, pos: int, end: int):
    """(track, decode time, where the time is, struct format) for each traf in one moof."""
    for kind, p, size, hdr in _boxes(data, pos, end):
        if kind != b"traf":
            continue
        track = at = fmt = None
        for k, q, s, h in _boxes(data, p + hdr, p + size):
            if k == b"tfhd":
                flags = int.from_bytes(data[q + h + 1:q + h + 4], "big")
                if flags & 0x000001:
                    raise ValueError("fragment uses absolute data offsets")
                track = struct.unpack(">I", data[q + h + 4:q + h + 8])[0]
            elif k == b"tfdt":
                at, fmt = q + h + 4, (">Q" if data[q + h] == 1 else ">I")
        if track is not None and at is not None:
            yield track, struct.unpack(fmt, data[at:at + struct.calcsize(fmt)])[0], at, fmt


def decode_times(piece: bytes) -> dict[int, int]:
    """Each track's decode time at the start of one stored piece (its first moof)."""
    out: dict[int, int] = {}
    for kind, pos, size, hdr in _boxes(piece):
        if kind == b"moof":
            for track, base, _, _ in _tfdts(piece, pos + hdr, pos + size):
                out.setdefault(track, base)
    return out


def kept_size(path: Path) -> int:
    """Bytes a stored piece adds to a clip: its boxes without styp and sidx. Reads box headers only."""
    total = size_left = Path(path).stat().st_size
    pos = 0
    with open(path, "rb") as f:
        while pos < size_left:
            f.seek(pos)
            head = f.read(16)
            if len(head) < 8:
                raise ValueError("truncated box header")
            box, kind = struct.unpack(">I4s", head[:8])
            if box == 1:
                box = struct.unpack(">Q", head[8:16])[0]
            elif box == 0:
                box = size_left - pos
            if box < 8 or pos + box > size_left:
                raise ValueError(f"box {kind!r} runs past the end")
            if kind in _DROP:
                total -= box
            pos += box
    return total


def rebase_fragment(piece: bytes, shift: dict[int, int]) -> bytes:
    """One stored piece as part of a clip: without styp and sidx, and with each track's decode time
    moved back by shift[track], so the clip starts at 0. The data offsets count from each moof, so
    dropping the boxes before it changes nothing else."""
    out = bytearray()
    for kind, pos, size, hdr in _boxes(piece):
        if kind in _DROP:
            continue
        box = bytearray(piece[pos:pos + size])
        if kind == b"moof":
            for track, base, at, fmt in _tfdts(piece, pos + hdr, pos + size):
                struct.pack_into(fmt, box, at - pos, max(0, base - shift.get(track, 0)))
        out += box
    return bytes(out)
