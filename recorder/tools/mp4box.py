"""Is an MP4 file whole? (standard library only)

ffprobe reads only a file's index (the moov box), so a file cut short still looks whole when its
index sits at the start, as in Frigate's segments. Every MP4 is a row of top-level boxes, each
starting with its own size; in a whole file they add up to the file's size exactly.
"""
from pathlib import Path


def cut_short(path: Path) -> str | None:
    """None if the file's top-level boxes add up to its size and hold its index (moov) and video
    (mdat), else what is wrong (M11 power cuts and corrupted-file test, M12 integrity scan)."""
    size = path.stat().st_size
    kinds, pos = set(), 0
    with path.open("rb") as f:
        while pos < size:
            f.seek(pos)
            head = f.read(16)
            box = int.from_bytes(head[:4], "big") if len(head) >= 8 else -1
            if box == 1:
                box = int.from_bytes(head[8:16], "big") if len(head) == 16 else -1
            elif box == 0:
                box = size - pos                          # the last box runs to the end of the file
            if box < 8 or not head[4:8].isalnum():
                return f"damaged box header at byte {pos} of {size}"
            kinds.add(head[4:8])
            pos += box
    if pos > size:
        return f"cut short: {size} of {pos} bytes"
    missing = [k.decode() for k in (b"moov", b"mdat") if k not in kinds]
    return f"no {' or '.join(missing)} box" if missing else None
