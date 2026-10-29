"""M20 gap report: a gap is a segment that was never confirmed uploaded AND whose file is gone."""
import json
import os

from conftest import seg_key, seg_rel, step, write_segment
from segment_uploader import gaps
from segment_uploader.__main__ import main
from test_status_and_config import write_config


def result(cfg):
    return gaps.find_gaps(cfg, gaps.unfinished_rows(cfg.state_db))


def test_no_gaps_when_everything_is_uploaded(cfg, s3, new_service):
    for n in (1, 2, 3):
        write_segment(cfg, seg_rel("cam01", n), os.urandom(500))
    step(new_service())
    r = result(cfg)
    assert r["gap_segments"] == 0 and r["waiting"] == 0
    assert "No gaps" in gaps.format_text(r)


def test_an_uploaded_segment_whose_file_was_removed_is_not_a_gap(cfg, s3, new_service):
    p = write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    step(new_service())
    p.unlink()                                   # the recorder's retention (M9) removes it later
    assert result(cfg)["gap_segments"] == 0


def test_a_waiting_segment_with_its_file_is_not_a_gap_but_is_counted_as_waiting(cfg, s3, new_service):
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    svc = new_service()
    s3.stop()                                    # the storage is not reachable
    step(svc)
    r = result(cfg)
    assert r["gap_segments"] == 0 and r["waiting"] == 1
    assert r["oldest_waiting"] == "2026-10-20T10:01:00Z"
    assert "queue, not a gap" in gaps.format_text(r)


def test_a_waiting_segment_whose_file_was_removed_is_a_gap(cfg, s3, new_service):
    paths = {n: write_segment(cfg, seg_rel("cam01", n), os.urandom(500)) for n in (1, 2, 3, 4)}
    svc = new_service()
    s3.stop()
    step(svc)
    paths[2].unlink()                            # retention removes segments 2 and 3 during the outage
    paths[3].unlink()
    r = result(cfg)
    assert r["gap_segments"] == 2 and r["waiting"] == 2
    assert [g["key"] for g in r["gaps"]] == [seg_key("cam01", 2), seg_key("cam01", 3)]
    assert all("while it was waiting" in g["reason"] for g in r["gaps"])
    # the two neighbouring segments are reported as one stretch of 2 minutes
    assert len(r["ranges"]) == 1
    assert r["ranges"][0]["segments"] == 2 and r["ranges"][0]["seconds"] == 120
    assert r["ranges"][0]["start"] == "2026-10-20T10:02:00Z"


def test_separate_stretches_and_cameras_are_reported_separately(cfg, s3, new_service):
    paths = {(c, n): write_segment(cfg, seg_rel(c, n), os.urandom(300))
             for c in ("cam01", "cam02") for n in range(1, 6)}
    svc = new_service()
    s3.stop()
    step(svc)
    for key in [("cam01", 1), ("cam01", 4), ("cam02", 2), ("cam02", 3)]:
        paths[key].unlink()
    r = result(cfg)
    assert r["gap_segments"] == 4
    assert [(g["camera_id"], g["segments"]) for g in r["ranges"]] == [
        ("cam01", 1), ("cam01", 1), ("cam02", 2)]
    assert r["gap_seconds"] == 60 + 60 + 120


def test_a_segment_removed_before_the_first_upload_attempt_is_a_gap(cfg, s3, new_service):
    p = write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    svc = new_service()
    svc.scan_once()
    p.unlink()                                   # gone between the scan and the upload
    step(svc)
    r = result(cfg)
    assert r["gap_segments"] == 1
    assert r["gaps"][0]["state"] == "missing"
    assert "before the uploader could upload it" in r["gaps"][0]["reason"]


def test_command_exit_code_and_json(cfg, s3, new_service, tmp_path, capsys):
    conf = str(write_config(tmp_path, cfg))
    p1 = write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    write_segment(cfg, seg_rel("cam01", 2), os.urandom(500))
    svc = new_service()
    s3.stop()
    step(svc)

    assert main(["gaps", "--config", conf]) == 0                 # both files are still there
    assert "No gaps" in capsys.readouterr().out

    p1.unlink()
    assert main(["gaps", "--config", conf]) == 1                 # now one segment is lost
    assert "GAPS: 1 segment" in capsys.readouterr().out
    assert main(["gaps", "--config", conf, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["gap_segments"] == 1 and data["gaps"][0]["key"] == seg_key("cam01", 1)


def test_command_without_a_state_file_says_so(cfg, tmp_path, capsys):
    conf = str(write_config(tmp_path, cfg))
    assert main(["gaps", "--config", conf]) == 2
    assert "has not run yet" in capsys.readouterr().err
