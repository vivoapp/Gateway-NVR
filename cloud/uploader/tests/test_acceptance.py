"""The M19 acceptance script must pass on a good run and fail on every kind of damage."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import m19_acceptance as acc  # noqa: E402

from conftest import seg_key, seg_rel, step, write_segment  # noqa: E402
from test_status_and_config import write_config  # noqa: E402


@pytest.fixture
def run(cfg, s3, new_service, tmp_path, caplog):
    """A finished run: 2 cameras x 3 segments uploaded, index delivered, config file and log written."""
    data = {}
    for cam in ("cam01", "cam02"):
        for n in (1, 2, 3):
            data[(cam, n)] = os.urandom(5000 + n)
            write_segment(cfg, seg_rel(cam, n), data[(cam, n)])
    svc = new_service()
    with caplog.at_level("INFO", logger="segment_uploader"):
        step(svc, 3)
        svc.log_stats()
    log = tmp_path / "uploader.log"
    log.write_text("\n".join(r.getMessage() for r in caplog.records))
    conf = write_config(tmp_path, cfg)
    manifest = tmp_path / "manifest.json"
    return {"cfg": cfg, "s3": s3, "conf": str(conf), "log": str(log), "manifest": manifest, "data": data,
            "args": ["--config", str(conf), "--log", str(log)]}


def go(run, command="verify", *extra):
    """Strict by default: the fixture's data is seconds old, so the default grace would skip all of it."""
    flags = [] if "--grace" in extra else ["--strict"]
    return acc.main([command, *run["args"], *flags, *extra])


def test_snapshot_then_verify_passes_on_a_good_run(run, capsys):
    assert acc.main(["snapshot", "--config", run["conf"], "--out", str(run["manifest"])]) == 0
    assert go(run, "verify", "--manifest", str(run["manifest"])) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "6 of 6 uploaded" in out and "6 of 6 have one" in out


def test_deep_check_downloads_and_still_passes(run):
    assert go(run, "verify", "--deep") == 0


def test_report_is_markdown_with_rate_and_cpu(run, capsys):
    assert go(run, "report") == 0
    out = capsys.readouterr().out
    assert "# M19 acceptance report" in out and "**Overall: PASS**" in out
    assert "CPU average" in out and "Upload rate" in out


def test_a_segment_that_is_not_uploaded_yet_fails(run, cfg, capsys):
    write_segment(cfg, seg_rel("cam01", 9), os.urandom(100))
    assert go(run) == 1
    assert "NOT uploaded" in capsys.readouterr().out


def test_a_different_object_in_the_bucket_fails(run, capsys):
    s3 = run["s3"]
    s3.client.put_object(Bucket=s3.bucket, Key=seg_key("cam01", 2), Body=b"not the same bytes")
    assert go(run) == 1
    assert "WRONG" in capsys.readouterr().out


def test_a_missing_object_fails(run, capsys):
    s3 = run["s3"]
    s3.client.delete_object(Bucket=s3.bucket, Key=seg_key("cam02", 1))
    assert go(run) == 1
    assert "missing" in capsys.readouterr().out


def test_an_extra_object_fails(run, capsys):
    s3 = run["s3"]
    s3.client.put_object(Bucket=s3.bucket, Key="site-1/cam09/2026-10-20/cam09_20261020T100000Z.mp4", Body=b"x")
    assert go(run) == 1
    assert "NOT marked uploaded" in capsys.readouterr().out


def test_a_missing_index_record_fails(run, tmp_path, capsys):
    f = next((tmp_path / "index").glob("index-*.jsonl"))
    lines = f.read_text().splitlines()
    f.write_text("\n".join(lines[1:]) + "\n")
    assert go(run) == 1
    assert "MISSING:" in capsys.readouterr().out


def test_a_wrong_index_value_fails(run, tmp_path, capsys):
    f = next((tmp_path / "index").glob("index-*.jsonl"))
    recs = [json.loads(l) for l in f.read_text().splitlines()]
    recs[0]["sha256"] = "0" * 64
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert go(run) == 1
    assert "wrong values" in capsys.readouterr().out


def test_a_repeated_index_record_is_allowed(run, tmp_path):
    f = next((tmp_path / "index").glob("index-*.jsonl"))
    lines = f.read_text().splitlines()
    f.write_text("\n".join(lines + lines[:1]) + "\n")
    assert go(run) == 0


def test_an_upload_logged_twice_fails(run, capsys):
    line = next(l for l in Path(run["log"]).read_text().splitlines() if "event=uploaded " in l)
    Path(run["log"]).write_text(Path(run["log"]).read_text() + "\n" + line)
    assert go(run) == 1
    assert "twice" in capsys.readouterr().out


def test_a_changed_local_file_fails(run, cfg, capsys):
    acc.main(["snapshot", "--config", run["conf"], "--out", str(run["manifest"])])
    with open(cfg.watch_dir / seg_rel("cam01", 1), "ab") as f:
        f.write(b"changed")
    assert go(run, "verify", "--manifest", str(run["manifest"])) == 1
    assert "CHANGED" in capsys.readouterr().out


def test_segments_still_in_flight_are_tolerated_while_running_but_not_in_strict_mode(run, cfg, s3, capsys):
    """Seen on a live run: the recorder and uploader keep going while the checker looks."""
    s3.client.put_object(Bucket=s3.bucket, Key="site-1/cam09/2026-10-20/cam09_20261020T100000Z.mp4", Body=b"x")
    write_segment(cfg, seg_rel("cam01", 8), os.urandom(100))
    assert go(run, "verify", "--grace", "30") == 0
    out = capsys.readouterr().out
    assert "still in flight" in out                      # said out loud, not hidden
    assert go(run, "verify") == 1                        # --strict


def test_old_damage_is_not_excused_by_the_grace_period(run, s3, cfg, capsys):
    s3.client.put_object(Bucket=s3.bucket, Key=seg_key("cam01", 2), Body=b"not the same bytes")
    # make the grace window start after everything above happened
    assert go(run, "verify", "--grace", "0.0001") == 1
    assert "WRONG" in capsys.readouterr().out


def test_a_segment_rewritten_after_upload_is_reported_as_a_conflict_not_hidden(run, cfg, new_service, capsys):
    """The recorder (or a simulator restart) wrote the same file name again with different content."""
    path = cfg.watch_dir / seg_rel("cam01", 2)
    path.write_bytes(os.urandom(7777))                       # same name, different bytes
    svc = new_service()
    svc.recover()                                            # start-up: a full scan notices the change
    step(svc, 3)
    assert svc.store.get(seg_key("cam01", 2))["state"] == "conflict"
    assert go(run) == 1
    out = capsys.readouterr().out
    assert "stuck" in out and "conflict" in out and "different object already exists" in out


def test_a_segment_waiting_too_long_is_reported_as_stuck(run, cfg, new_service, capsys):
    write_segment(cfg, seg_rel("cam01", 8), os.urandom(100))
    new_service().scan_once()                                # registered, never uploaded
    assert go(run) == 1
    assert "stuck" in capsys.readouterr().out
