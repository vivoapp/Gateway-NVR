"""The index record: one note per uploaded segment, handed to a replaceable sink."""
import json
import os

import pytest

from conftest import BASE, seg_key, seg_rel, sha, step, write_segment
from segment_uploader.config import load_config
from segment_uploader.index import IndexSink, make_sink, JsonLinesSink


class ListSink(IndexSink):
    def __init__(self):
        self.records, self.fail = [], False

    def emit(self, record):
        if self.fail:
            raise OSError("disk full")
        self.records.append(record)


def make(new_service, sink):
    svc = new_service()
    svc.sink = sink
    return svc


def test_record_has_what_the_backend_needs(cfg, s3, new_service):
    data = os.urandom(4000)
    write_segment(cfg, seg_rel("cam07", 3), data)
    sink = ListSink()
    step(make(new_service, sink))

    assert len(sink.records) == 1
    r = sink.records[0]
    assert r["camera_id"] == "cam07"
    assert r["start_utc"] == "2026-10-20T10:03:00Z" and r["end_utc"] == "2026-10-20T10:04:00Z"
    assert r["key"] == seg_key("cam07", 3) and r["bucket"] == cfg.bucket
    assert r["size"] == 4000 and r["sha256"] == sha(data) and r["etag"]
    assert r["schema"] == 1 and r["uploaded_at"].endswith("Z")


def test_each_record_is_sent_once_even_across_restarts(cfg, s3, new_service):
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(1000))
    sink = ListSink()
    svc = make(new_service, sink)
    step(svc, 3)
    svc.store.close()
    svc2 = make(new_service, sink)
    svc2.recover()
    step(svc2, 3)
    assert len(sink.records) == 1


def test_a_failing_sink_never_blocks_uploads_and_records_arrive_later(cfg, s3, new_service, clock):
    for n in (1, 2, 3):
        write_segment(cfg, seg_rel("cam01", n), os.urandom(1000))
    sink = ListSink()
    sink.fail = True
    svc = make(new_service, sink)
    step(svc)
    assert svc.store.counts() == {"uploaded": 3} and sink.records == []

    sink.fail = False
    clock.advance(60)
    step(svc)
    assert [r["key"] for r in sink.records] == [seg_key("cam01", n) for n in (1, 2, 3)]   # oldest first
    step(svc)
    assert len(sink.records) == 3


def test_default_sink_writes_one_json_line_per_segment_per_day(cfg, s3, new_service, tmp_path):
    for n in (1, 2):
        write_segment(cfg, seg_rel("cam01", n), os.urandom(1000))
    step(new_service())                                     # default sink: <state dir>/index
    lines = (tmp_path / "index" / "index-2026-10-20.jsonl").read_text().splitlines()
    assert [json.loads(l)["key"] for l in lines] == [seg_key("cam01", 1), seg_key("cam01", 2)]


def test_sink_is_chosen_in_the_config_and_unknown_names_are_rejected(cfg, tmp_path):
    cfg.index_sink = "jsonl"
    assert isinstance(make_sink(cfg), JsonLinesSink)
    cfg.index_sink = "carrier-pigeon"
    with pytest.raises(ValueError, match="carrier-pigeon"):
        make_sink(cfg)

    f = tmp_path / "c.toml"
    f.write_text('[service]\nwatch_dir = "x"\n[s3]\nbucket = "b"\n[index]\nsink = "none"\ndir = "out"\n')
    loaded = load_config(f)
    assert loaded.index_sink == "none" and loaded.index_dir == tmp_path / "out"
