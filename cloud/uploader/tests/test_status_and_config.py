"""The status command (used by run.sh) and the environment overrides used in Docker."""
import os

from conftest import seg_rel, step, write_segment
from segment_uploader.__main__ import main
from segment_uploader.config import load_config


def write_config(tmp_path, cfg, extra=""):
    path = tmp_path / "config.toml"
    path.write_text(f"""
[service]
watch_dir = "{cfg.watch_dir.as_posix()}"
state_db = "{cfg.state_db.as_posix()}"
{extra}
[s3]
endpoint_url = "{cfg.endpoint_url}"
bucket = "{cfg.bucket}"
key_prefix = "site-1"
access_key = "test"
secret_key = "test"
""")
    return path


def test_status_brief_reports_counts_local_files_and_bucket_objects(cfg, s3, new_service, tmp_path, capsys):
    write_segment(cfg, seg_rel("cam01", 1), os.urandom(1000))
    write_segment(cfg, seg_rel("cam01", 2) + ".part", os.urandom(1000))      # not a finished file
    step(new_service())

    assert main(["status", "--brief", "--config", str(write_config(tmp_path, cfg))]) == 0

    line = capsys.readouterr().out.strip()
    assert line == ("uploaded=1 pending=0 uploading=0 conflict=0 missing=0 "
                    "local_files=1 bucket_objects=1")


def test_status_brief_says_unreachable_when_storage_is_down(cfg, s3, tmp_path, capsys):
    config_file = write_config(tmp_path, cfg)
    s3.stop()
    main(["status", "--brief", "--config", str(config_file)])
    assert "bucket_objects=unreachable" in capsys.readouterr().out


def test_environment_overrides_win_over_the_file(cfg, tmp_path, monkeypatch):
    config_file = write_config(tmp_path, cfg)

    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_ACCESS_KEY", "from-env")
    loaded = load_config(config_file)
    assert loaded.endpoint_url == "http://minio:9000"
    assert loaded.access_key == "from-env"


def test_unknown_setting_in_the_file_is_an_error(cfg, tmp_path):
    import pytest
    config_file = write_config(tmp_path, cfg, extra="settle_seconds = 2.0\n")     # removed setting
    with pytest.raises(ValueError, match="service.settle_seconds"):
        load_config(config_file)


def test_site_settings_can_come_from_the_environment_only(tmp_path, monkeypatch):
    """On the gateway one config file serves every site; bucket, region and prefix are in cloud.env."""
    f = tmp_path / "c.toml"
    f.write_text('[service]\nwatch_dir = "/data/recordings"\n')
    for k in ("S3_BUCKET", "S3_REGION", "S3_KEY_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    import pytest
    with pytest.raises(ValueError, match="no bucket"):
        load_config(f)
    monkeypatch.setenv("S3_BUCKET", "vivo-prod")
    monkeypatch.setenv("S3_REGION", "eu-central-003")
    monkeypatch.setenv("S3_KEY_PREFIX", "site-042")
    cfg = load_config(f)
    assert (cfg.bucket, cfg.region, cfg.key_prefix) == ("vivo-prod", "eu-central-003", "site-042")
