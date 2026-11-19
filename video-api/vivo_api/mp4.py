"""A whole-file check for MP4: the top-level boxes add up to the file size, with a movie header
and media data. The same rule as the recorder's tools/mp4box.py, for clips the API prepares."""
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
