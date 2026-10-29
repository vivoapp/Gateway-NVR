"""M20 tools: the retention calculation and a short end-to-end run of the recovery script."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import m20_recovery  # noqa: E402
import m20_retention as ret  # noqa: E402


# ---- retention / catch-up calculation -------------------------------------------------------
def test_live_rate_of_ten_cameras_at_one_mb_per_minute():
    assert round(ret.live_mbps(10, 1.0), 2) == 1.33


def test_a_two_day_outage_piles_up_28_8_gb():
    assert ret.pile_gb(2, 10, 1.0) == 28.8


def test_catch_up_uses_only_the_speed_left_over_by_the_live_stream():
    # 28.8 GB = 230.4 Gb; (20 - 1.333) Mbps spare -> 12,343 s = 3.43 h, plus 5 minutes before the first retry
    assert round(ret.catchup_hours(2, 20), 2) == round(230_400 / 18.6667 / 3600 + 5 / 60, 2)
    assert ret.catchup_hours(2, 50) < ret.catchup_hours(2, 20) < ret.catchup_hours(2, 10)


def test_catch_up_never_ends_if_the_speed_is_not_above_the_live_rate():
    assert ret.catchup_hours(2, 1.33) is None
    assert "never" in ret.fmt_hours(None)


def test_retention_is_outage_plus_margin_in_whole_days():
    assert ret.retention_days(2) == 3 and ret.retention_days(3) == 4 and ret.retention_days(2.5) == 4
    assert ret.retention_days(2, margin_days=2) == 4


def test_recommendation_names_days_and_disk():
    text = ret.recommendation(2, 20)
    assert "at least 3 days" in text and "43 GB" in text and "20 Mbps" in text


# ---- the recovery script, end to end, short -------------------------------------------------
def test_recovery_script_passes_a_short_run_against_a_real_s3_server(s3, tmp_path, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", s3.url)
    monkeypatch.setenv("S3_BUCKET", s3.bucket)
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_KEY_PREFIX", "placeholder")      # the script sets its own prefix; this restores the variable afterwards
    rc = m20_recovery.main(["--quick", "--only", "K2,C1", "--work", str(tmp_path / "w"), "--out-dir", str(tmp_path / "r")])
    results = json.loads((tmp_path / "r" / "m20-recovery-results.json").read_text())
    failed = [(r["id"], [c for c in r["checks"] if not c[1]]) for r in results if not r["pass"]]
    assert rc == 0 and not failed, failed
    assert [r["id"] for r in results] == ["K2", "C1"]
    report = (tmp_path / "r" / "m20-recovery-report.md").read_text()
    assert "2 of 2 tests passed" in report and "Retention recommendation" in report


def test_the_recovery_script_lists_exactly_the_18_agreed_tests(capsys):
    assert m20_recovery.main(["--list"]) == 0
    ids = [line.split()[0] for line in capsys.readouterr().out.strip().splitlines()]
    assert len(ids) == 18
    assert [i[0] for i in ids].count("C") == 5 and [i[0] for i in ids].count("R") == 5
    assert [i[0] for i in ids].count("K") == 5 and [i[0] for i in ids].count("S") == 3


def test_the_outage_pile_never_rewrites_an_existing_segment(tmp_path):
    """A segment of an earlier test with the same name must stay exactly as it is (found in a long run)."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    rig = SimpleNamespace(rec=tmp_path, prefix="m20-x")
    end = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=90)
    cid = m20_recovery.NAME_FORMAT.format(1)
    existing = tmp_path / cid / end.strftime("%Y-%m-%d") / f"{cid}_{end.strftime('%Y%m%dT%H%M%S')}Z.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"earlier test")
    keys = m20_recovery.write_pile(rig, 3, 1)
    assert existing.read_bytes() == b"earlier test"
    assert len(keys) == 3 * m20_recovery.CAMERAS - 1 and f"m20-x/{cid}/{existing.parent.name}/{existing.name}" not in keys
