"""The M20 test proxy: passes requests on unchanged, and fails them in the ways the recovery tests need."""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from fault_proxy import FaultProxy  # noqa: E402

from conftest import seg_key, seg_rel, step, write_segment  # noqa: E402
from segment_uploader.config import Config  # noqa: E402
from segment_uploader.service import Service  # noqa: E402
from segment_uploader.storage import S3Storage, classify  # noqa: E402


@pytest.fixture
def proxy(s3):
    p = FaultProxy(s3.url).start()
    yield p
    p.stop()


@pytest.fixture
def pcfg(cfg, proxy):
    cfg.endpoint_url = proxy.url
    return cfg


def head_error(storage, key="site-1/x.mp4"):
    try:
        storage.head(key)
    except Exception as exc:          # noqa: BLE001
        return exc
    return None


def test_everything_passes_through_when_ok(pcfg, s3, proxy, new_service):
    data = os.urandom(300_000)
    write_segment(pcfg, seg_rel("cam01", 1), data)
    step(new_service())
    assert s3.objects()[seg_key("cam01", 1)] == data              # arrived intact through the proxy
    assert proxy.puts_stored == 1 and proxy.requests["ok"] >= 3   # head, put, head


@pytest.mark.parametrize("mode,kind", [("down", "outage"), ("auth", "auth"), ("full", "full"),
                                       ("error", "outage")])
def test_each_failure_mode_is_classified_as_the_service_expects(pcfg, proxy, mode, kind):
    storage = S3Storage(pcfg)
    proxy.set_mode(mode)
    exc = head_error(storage)
    assert exc is not None and classify(exc) == kind


def test_put_in_down_mode_is_an_outage_and_stores_nothing(pcfg, s3, proxy, tmp_path):
    storage = S3Storage(pcfg)
    f = tmp_path / "f.mp4"
    f.write_bytes(os.urandom(200_000))
    proxy.set_mode("down")
    with pytest.raises(Exception) as err:
        storage.put("site-1/f.mp4", f, "AAAAAAAAAAAAAAAAAAAAAA==", "0" * 64, 200_000)
    assert classify(err.value) == "outage"
    assert s3.objects() == {}


def test_flaky_fails_every_second_request(pcfg, proxy):
    storage = S3Storage(pcfg)
    proxy.set_mode("flaky")
    results = [head_error(storage) is None or True for _ in range(1)]    # warm up
    outcomes = []
    for _ in range(6):
        exc = head_error(storage)
        outcomes.append("server error" if exc is not None and classify(exc) == "outage" else "answered")
    assert outcomes.count("server error") == 3 and results


def test_answer_is_held_after_the_object_is_stored(pcfg, s3, proxy, tmp_path):
    storage = S3Storage(pcfg)
    f = tmp_path / "f.mp4"
    data = os.urandom(50_000)
    f.write_bytes(data)
    import base64, hashlib
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    proxy.delay_after_put = 1.5
    t = threading.Thread(target=storage.put, args=("site-1/f.mp4", f, md5, "0" * 64, len(data)))
    t.start()
    assert proxy.held.wait(5)                        # the proxy now holds the answer ...
    assert "site-1/f.mp4" in s3.objects()            # ... but the object is already in the bucket
    t.join()


def test_going_down_cuts_an_upload_that_is_waiting_in_the_proxy(pcfg, s3, proxy, tmp_path):
    storage = S3Storage(pcfg)
    f = tmp_path / "f.mp4"
    f.write_bytes(os.urandom(50_000))
    proxy.delay_before_put = 5.0
    errors = []

    def put():
        try:
            storage.put("site-1/f.mp4", f, "AAAAAAAAAAAAAAAAAAAAAA==", "0" * 64, 50_000)
        except Exception as exc:                     # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=put)
    t.start()
    time.sleep(0.8)
    proxy.set_mode("down")
    t.join(5)
    assert errors and classify(errors[0]) == "outage"
    assert s3.objects() == {}


# ---- the proxy must not break the request signature (MinIO and B2 check it; moto does not) -------
import hashlib  # noqa: E402
import hmac  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


def sigv4_matches(method, path, headers, body, secret):
    """Re-compute the AWS Signature V4 of a received request, the way a strict S3 server does."""
    auth = headers["Authorization"]
    _, date, region, service, _ = auth.split("Credential=")[1].split(",")[0].split("/")
    signed = auth.split("SignedHeaders=")[1].split(",")[0]
    given = auth.split("Signature=")[1].strip()
    uri, _, query = path.partition("?")
    canon_headers = "".join(f"{h}:{' '.join(headers[h].split())}\n" for h in signed.split(";"))
    payload = headers["x-amz-content-sha256"]
    if payload != "UNSIGNED-PAYLOAD" and payload != hashlib.sha256(body).hexdigest():
        return False
    canon = "\n".join([method, uri, query, canon_headers, signed, payload])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", headers["x-amz-date"], scope, hashlib.sha256(canon.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest() == given


class StrictS3:
    """A tiny S3 stand-in that refuses every request whose signature or Content-MD5 is wrong."""

    def __init__(self):
        self.objects, self.refused = {}, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                ok = sigv4_matches(self.command, self.path, self.headers, body, "test")
                if ok and self.command == "PUT" and "Content-MD5" in self.headers:
                    import base64
                    ok = base64.b64decode(self.headers["Content-MD5"]) == hashlib.md5(body).digest()
                if not ok:
                    outer.refused += 1
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.command == "PUT":
                    outer.objects[self.path] = body
                    self.send_response(200)
                    self.send_header("ETag", '"' + hashlib.md5(body).hexdigest() + '"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path in outer.objects:
                    data = outer.objects[self.path]
                    self.send_response(200)
                    self.send_header("ETag", '"' + hashlib.md5(data).hexdigest() + '"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command == "GET":
                        self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            do_PUT = do_HEAD = do_GET = _go

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_the_proxy_keeps_signature_and_checksum_valid_for_a_strict_server(cfg, tmp_path):
    strict = StrictS3()
    proxy = FaultProxy(strict.url).start()
    try:
        cfg.endpoint_url = proxy.url
        storage = S3Storage(cfg)
        data = os.urandom(400_000)
        f = tmp_path / "f.mp4"
        f.write_bytes(data)
        import base64
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        assert storage.head("site-1/a.mp4") is None                        # a signed HEAD passes
        storage.put("site-1/a.mp4", f, md5, hashlib.sha256(data).hexdigest(), len(data))
        remote = storage.head("site-1/a.mp4")
        assert remote.size == len(data) and remote.etag == hashlib.md5(data).hexdigest()
        assert strict.refused == 0
        assert strict.objects[f"/{cfg.bucket}/site-1/a.mp4"] == data
    finally:
        proxy.stop()
        strict.stop()


def test_the_strict_server_really_refuses_a_changed_host_header():
    """Control: proves the check above has teeth (a proxy that rewrote Host would fail)."""
    import http.client
    import botocore.auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    strict = StrictS3()
    try:
        req = AWSRequest(method="HEAD", url=f"{strict.url}/bucket/key", headers={})
        req.headers["x-amz-content-sha256"] = hashlib.sha256(b"").hexdigest()
        botocore.auth.S3SigV4Auth(Credentials("test", "test"), "s3", "us-east-1").add_auth(req)
        good = dict(req.headers.items())
        c = http.client.HTTPConnection("127.0.0.1", strict.srv.server_address[1])
        c.request("HEAD", "/bucket/key", headers=good)
        assert c.getresponse().status == 404                               # signature accepted
        bad = dict(good, Host="other-host:9000")
        c = http.client.HTTPConnection("127.0.0.1", strict.srv.server_address[1])
        c.putrequest("HEAD", "/bucket/key", skip_host=True)
        for k, v in bad.items():
            c.putheader(k, v)
        c.endheaders()
        assert c.getresponse().status == 403
    finally:
        strict.stop()
