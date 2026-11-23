"""Joining stored fragmented-MP4 pieces into one clip that starts at 0 (vivo_api/mp4.py)."""
from __future__ import annotations

import struct

import pytest

from vivo_api import mp4


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def piece(base: int, version: int = 1, tfhd_flags: int = 0x020000, track: int = 1) -> bytes:
    """One stored piece as ffmpeg writes it for HLS: styp, sidx, moof (mfhd, traf), mdat."""
    when = struct.pack(">Q", base) if version == 1 else struct.pack(">I", base)
    traf = box(b"traf", full(b"tfhd", 0, tfhd_flags, struct.pack(">I", track))
               + full(b"tfdt", version, 0, when) + full(b"trun", 0, 0, b"\0" * 4))
    return (box(b"styp", b"msdh\0\0\0\0msdh") + box(b"sidx", b"\0" * 44)
            + box(b"moof", box(b"mfhd", b"\0" * 8) + traf) + box(b"mdat", b"x" * 100))


def kinds(data: bytes) -> list[bytes]:
    return [k for k, _, _, _ in mp4._boxes(data)]


@pytest.mark.parametrize("version", [0, 1])
def test_rebase_moves_the_decode_time_and_drops_styp_and_sidx(tmp_path, version):
    p = piece(153600, version)
    assert mp4.decode_times(p) == {1: 153600}
    out = mp4.rebase_fragment(p, {1: 153600})
    assert kinds(out) == [b"moof", b"mdat"]
    assert mp4.decode_times(out) == {1: 0}
    assert out.endswith(b"x" * 100)
    f = tmp_path / "p.m4s"
    f.write_bytes(p)
    assert mp4.kept_size(f) == len(out)


def test_later_pieces_keep_their_distance_from_the_first():
    first, later = piece(1_000_000), piece(1_153_600)
    shift = mp4.decode_times(first)
    assert mp4.decode_times(mp4.rebase_fragment(later, shift)) == {1: 153600}


def test_other_tracks_and_times_before_the_first_are_left_safe():
    assert mp4.decode_times(mp4.rebase_fragment(piece(500, track=2), {1: 900})) == {2: 500}
    assert mp4.decode_times(mp4.rebase_fragment(piece(500), {1: 900})) == {1: 0}


def test_absolute_data_offsets_are_refused():
    with pytest.raises(ValueError):
        mp4.rebase_fragment(piece(10, tfhd_flags=0x000001), {1: 10})


def test_a_cut_piece_is_refused(tmp_path):
    f = tmp_path / "cut.m4s"
    f.write_bytes(piece(10)[:-50])
    with pytest.raises(ValueError):
        mp4.kept_size(f)
    with pytest.raises(ValueError):
        mp4.rebase_fragment(f.read_bytes(), {1: 10})
