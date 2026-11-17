"""Step 10: fixes found in the code review (CR-2, CR-4, CR-5, CR-6, CR-7)."""
import json
import logging
import os
import sqlite3

import pytest
from botocore.exceptions import ClientError

from conftest import seg_key, seg_rel, step, write_segment
from segment_uploader import gaps, service as service_mod
from segment_uploader.__main__ import main
from segment_uploader.config import load_config
from segment_uploader.index import IndexSink, JsonLinesSink
from segment_uploader.storage import S3Storage
from test_status_and_config import write_config


# ---- CR-2: one index record that cannot be emitted must not block the ones behind it ---------------

class PickySink(IndexSink):
    """Refuses records for chosen keys (forever), or everything while `down` is set."""

    def __init__(self, refuse=()):
        self.records, self.refuse, self.down = [], set(refuse), False

    def emit(self, record):
        if self.down or record["key"] in self.refuse:
            raise OSError("refused")
        self.records.append(record)


def served(cfg, n):
    return [write_segment(cfg, seg_rel("cam01", i), os.urandom(500)) for i in range(1, n + 1)]


def test_a_record_the_sink_always_refuses_is_given_up_and_the_others_still_arrive(cfg, s3, new_service, clock, caplog):
    served(cfg, 6)
    sink = PickySink(refuse={seg_key("cam01", 1)})           # the OLDEST record is the bad one
    svc = new_service()
    svc.sink = sink
    with caplog.at_level(logging.INFO, logger="segment_uploader"):
        for _ in range(service_mod.INDEX_GIVE_UP_AFTER + 2):
            step(svc)
            clock.advance(10)                                  # past the short pause after a failed round
    assert [r["key"] for r in sink.records] == [seg_key("cam01", n) for n in range(2, 7)]
    assert svc.store.get(seg_key("cam01", 1))["index_sent"] == 2          # given up, marked
    assert sum("event=index_skipped" in r.message for r in caplog.records) == 1     # one ERROR, not a stream
    assert svc.store.counts() == {"uploaded": 6}                           # uploads were never affected


def test_a_sink_that_is_down_loses_nothing_and_does_not_skip_records(cfg, s3, new_service, clock):
    served(cfg, 6)
    sink = PickySink()
    sink.down = True
    svc = new_service()
    svc.sink = sink
    for _ in range(service_mod.INDEX_GIVE_UP_AFTER + 5):
        step(svc)
        clock.advance(30)
    assert sink.records == []
    assert all(svc.store.get(seg_key("cam01", n))["index_sent"] == 0 for n in range(1, 7))
    sink.down = False
    step(svc)
    assert len(sink.records) == 6                                          # all arrive, oldest first


def test_a_record_that_cannot_be_built_is_skipped_at_once(cfg, s3, new_service, caplog):
    served(cfg, 3)
    svc = new_service()
    svc.sink = PickySink()
    svc.scan_once()
    svc.upload_due()
    svc.store._exec("UPDATE segments SET uploaded_at=NULL WHERE key=?", (seg_key("cam01", 1),))
    with caplog.at_level(logging.INFO, logger="segment_uploader"):
        svc.deliver_index()
    assert svc.store.get(seg_key("cam01", 1))["index_sent"] == 2
    assert len(svc.sink.records) == 2
    assert any("cannot_build_record" in r.message for r in caplog.records)


# ---- CR-4: the delete guard must not switch itself off with an empty prefix ------------------------

def test_an_empty_key_prefix_is_refused_at_start_up(cfg, tmp_path, monkeypatch):
    conf = write_config(tmp_path, cfg)
    monkeypatch.setenv("S3_KEY_PREFIX", "")                  # deliberately empty
    with pytest.raises(ValueError, match="no key prefix"):
        load_config(conf)
    monkeypatch.setenv("S3_KEY_PREFIX", "/")                 # only slashes: also empty
    with pytest.raises(ValueError, match="no key prefix"):
        load_config(conf)
    monkeypatch.setenv("S3_KEY_PREFIX", "site-9")
    assert load_config(conf).key_prefix == "site-9"


def test_delete_refuses_everything_when_no_prefix_is_set(cfg, s3):
    cfg.key_prefix = ""
    storage = S3Storage(cfg)
    s3.client.put_object(Bucket=s3.bucket, Key="someone-elses/object.mp4", Body=b"x")
    with pytest.raises(ValueError, match="no key prefix"):
        storage.delete("someone-elses/object.mp4")
    assert "someone-elses/object.mp4" in s3.objects()


# ---- CR-5: status must not take a write lock ------------------------------------------------------

def test_status_works_while_another_process_holds_the_write_lock(cfg, s3, new_service, tmp_path, capsys):
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    step(new_service())
    locker = sqlite3.connect(str(cfg.state_db), timeout=0)
    locker.execute("BEGIN IMMEDIATE")                        # like a long bulk scan: the database is write-locked
    try:
        assert main(["status", "--remote", "--config", str(write_config(tmp_path, cfg))]) == 0
        out = capsys.readouterr().out
        assert "'uploaded': 1" in out and "objects in bucket" in out
        assert main(["status", "--brief", "--config", str(write_config(tmp_path, cfg))]) == 0
    finally:
        locker.rollback()
        locker.close()


def test_status_without_a_state_file_does_not_create_one(cfg, s3, tmp_path, capsys):
    assert main(["status", "--brief", "--config", str(write_config(tmp_path, cfg))]) == 0
    assert not cfg.state_db.exists()


def test_status_lists_only_the_oldest_waiting_segments(cfg, s3, new_service, tmp_path, capsys):
    for i in range(1, 31):
        write_segment(cfg, seg_rel("cam01", i), os.urandom(200))
    svc = new_service()
    s3.stop()
    step(svc)
    s3.start()
    main(["status", "--config", str(write_config(tmp_path, cfg))])
    out = capsys.readouterr().out
    assert out.count("attempts=") == 20 and "and 10 more" in out


# ---- CR-6: a file that keeps failing is escalated once and then retried slowly -----------------------

class AlwaysBadDigest(S3Storage):
    def put(self, *a, **k):
        raise ClientError({"Error": {"Code": "BadDigest", "Message": "checksum mismatch"},
                           "ResponseMetadata": {"HTTPStatusCode": 400}}, "PutObject")


def test_a_file_that_keeps_failing_gets_one_error_and_then_slow_retries(cfg, s3, new_service, clock, caplog):
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    svc = new_service(AlwaysBadDigest(cfg))
    key = seg_key("cam01", 1)
    with caplog.at_level(logging.INFO, logger="segment_uploader"):
        for _ in range(service_mod.STUCK_AFTER + 4):
            step(svc)
            clock.advance(400)                                # longer than backoff_max: always due again
            row = svc.store.get(key)
            if row["last_error"].startswith(gaps.STUCK_PREFIX):
                break
    row = svc.store.get(key)
    assert row["last_error"].startswith(gaps.STUCK_PREFIX) and "BadDigest" in row["last_error"]
    assert row["next_attempt_at"] - clock() > 1500            # now about an hour away, not minutes
    errors = [r for r in caplog.records if "event=segment_stuck" in r.message]
    assert len(errors) == 1 and errors[0].levelno == logging.ERROR
    # the gap report names it
    r = gaps.find_gaps(cfg, gaps.unfinished_rows(cfg.state_db))
    assert r["stuck_segments"] == 1 and r["stuck"][0]["key"] == key


def test_a_stuck_file_recovers_when_the_problem_goes_away(cfg, s3, new_service, clock):
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    svc = new_service(AlwaysBadDigest(cfg))
    for _ in range(service_mod.STUCK_AFTER + 1):
        step(svc)
        clock.advance(400)
    key = seg_key("cam01", 1)
    assert svc.store.get(key)["last_error"].startswith(gaps.STUCK_PREFIX)
    svc.storage = S3Storage(cfg)
    clock.advance(4000)                                       # the slow retry comes round
    step(svc)
    row = svc.store.get(key)
    assert row["state"] == "uploaded" and row["last_error"] is None
    assert key not in svc._file_failures


def test_outage_errors_do_not_count_towards_stuck(cfg, s3, new_service, clock):
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(500))
    svc = new_service()
    s3.stop()
    for _ in range(service_mod.STUCK_AFTER + 5):
        step(svc)
        clock.advance(400)
    row = svc.store.get(seg_key("cam01", 1))
    assert not (row["last_error"] or "").startswith(gaps.STUCK_PREFIX)
    assert svc._file_failures == {}


# ---- CR-7: the index folder is created once ---------------------------------------------------------

def test_the_index_folder_is_created_once(tmp_path, monkeypatch):
    sink = JsonLinesSink(tmp_path / "idx")
    calls = []
    real = type(sink.dir).mkdir
    monkeypatch.setattr(type(sink.dir), "mkdir", lambda self, *a, **k: (calls.append(1), real(self, *a, **k))[1])
    rec = {"start_utc": "2026-10-20T10:00:00Z", "key": "k"}
    for _ in range(5):
        sink.emit(rec)
    assert len(calls) == 1
    assert len((tmp_path / "idx" / "index-2026-10-20.jsonl").read_text().splitlines()) == 5


# ---- CR-1 and CR-8: the state file and the memory must not grow for ever ----------------------------

DAY = 86400


def upload_n(cfg, new_service, clock, n=5):
    for i in range(1, n + 1):
        write_segment(cfg, seg_rel("cam01", i), os.urandom(300))
    svc = new_service()
    step(svc, 2)
    assert svc.store.counts() == {"uploaded": n}
    return svc


def test_old_uploaded_segments_are_forgotten_and_recent_ones_kept(cfg, s3, new_service, clock):
    cfg.state_keep_days = 45
    svc = upload_n(cfg, new_service, clock)
    clock.advance(30 * DAY)
    assert svc.prune_old() == 0                               # 30 days: still within 45
    clock.advance(20 * DAY)                                   # now 50 days after the upload
    write_segment(cfg, seg_rel("cam01", 99), os.urandom(300))
    step(svc, 2)                                              # a fresh segment, uploaded now
    assert svc.prune_old() == 5
    assert svc.store.counts() == {"uploaded": 1}
    assert len(svc._known) == 1                               # forgotten keys also leave memory
    assert seg_key("cam01", 1) not in svc._known


def test_pruning_keeps_everything_that_is_not_finished(cfg, s3, new_service, clock):
    cfg.state_keep_days = 1
    svc = upload_n(cfg, new_service, clock, 3)
    svc.store._exec("UPDATE segments SET index_sent=0 WHERE key=?", (seg_key("cam01", 1),))     # record not sent yet
    svc.store._exec("UPDATE segments SET state='conflict' WHERE key=?", (seg_key("cam01", 2),))
    clock.advance(10 * DAY)
    assert svc.prune_old() == 1                               # only cam01 #3: uploaded and its record handed over
    assert svc.store.get(seg_key("cam01", 1)) and svc.store.get(seg_key("cam01", 2))
    assert svc.store.get(seg_key("cam01", 3)) is None


def test_state_keep_days_zero_never_forgets(cfg, s3, new_service, clock):
    cfg.state_keep_days = 0
    svc = upload_n(cfg, new_service, clock, 2)
    clock.advance(1000 * DAY)
    assert svc.prune_old() == 0 and svc.store.counts() == {"uploaded": 2}


def test_a_forgotten_segment_whose_file_reappears_is_not_uploaded_twice(cfg, s3, new_service, clock):
    from helpers import CountingStorage
    cfg.state_keep_days = 1
    p = write_segment(cfg, seg_rel("cam01", 1), os.urandom(400))
    svc = new_service(CountingStorage(cfg))
    step(svc, 2)
    assert svc.storage.puts == 1
    clock.advance(5 * DAY)
    assert svc.prune_old() == 1
    svc.scan_once(full=True)                                  # the file is (still) on the disk: seen as new
    step(svc, 2)
    assert svc.storage.puts == 1                              # found in the bucket, not sent again
    assert svc.store.get(seg_key("cam01", 1))["state"] == "uploaded"
    assert len(s3.objects()) == 1 and p.exists()


def test_recover_prunes_before_loading_the_known_keys(cfg, s3, new_service, clock):
    cfg.state_keep_days = 1
    svc = upload_n(cfg, new_service, clock, 4)
    clock.advance(5 * DAY)
    svc2 = new_service()
    svc2.recover()                                            # a restart: old rows go before they are loaded
    assert svc2._known == set() and svc2.store.counts() == {}


def test_gap_report_is_not_affected_by_pruning(cfg, s3, new_service, clock):
    cfg.state_keep_days = 1
    p = write_segment(cfg, seg_rel("cam01", 1), os.urandom(400))
    svc = new_service()
    step(svc, 2)
    p.unlink()
    clock.advance(5 * DAY)
    svc.prune_old()
    r = gaps.find_gaps(cfg, gaps.unfinished_rows(cfg.state_db))
    assert r["gap_segments"] == 0 and r["stuck_segments"] == 0


def test_state_keep_days_is_a_setting_and_negative_is_refused(cfg, tmp_path):
    conf = write_config(tmp_path, cfg, "state_keep_days = 60")
    assert load_config(conf).state_keep_days == 60
    bad = write_config(tmp_path, cfg, "state_keep_days = -1")
    with pytest.raises(ValueError, match="state_keep_days"):
        load_config(bad)
    assert load_config(write_config(tmp_path, cfg)).state_keep_days == 45          # default


def test_the_helper_sets_are_bounded(cfg, s3, new_service):
    svc = new_service()
    svc._warned_names.update(f"x{i}" for i in range(10_001))
    svc._dir_state["some/old/folder"] = 5
    svc.scan_once(full=True)
    assert svc._warned_names == set() and svc._dir_state == {}
