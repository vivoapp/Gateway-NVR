#!/usr/bin/env python3
"""A small HTTP proxy between the uploader and the S3 server, used by the M20 recovery tests.

It passes every request on unchanged (so MinIO or any S3 server still checks the signature), and it can
be told to misbehave like a real network or storage service:

    ok      pass everything on
    down    the internet is cut: connections are closed at once; a body that is being sent is cut half way
    auth    the storage refuses the credentials (HTTP 403, InvalidAccessKeyId)
    full    the storage has no room (HTTP 507, StorageFull)
    error   the storage answers with server errors (HTTP 500, InternalError)
    flaky   every second request gets a server error, the others pass

Two delays make it possible to kill the uploader at an exact moment:

    delay_before_put   seconds the proxy waits before it sends an upload on (the file is "in flight")
    delay_after_put    seconds the proxy holds the answer after the server has stored the object
                       (the object IS in the bucket, the uploader has not been told yet)

Used from tools/m20_recovery.py; it can also run alone:  python tools/fault_proxy.py --upstream URL --port 9100
"""
from __future__ import annotations

import argparse
import http.client
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

MODES = ("ok", "down", "auth", "full", "error", "flaky")

_XML = ('<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code><Message>{msg}</Message>'
        '<RequestId>fault-proxy</RequestId></Error>')
_ERRORS = {
    "auth": (403, "InvalidAccessKeyId", "The access key you provided does not exist (test proxy)."),
    "full": (507, "StorageFull", "The storage has no room left (test proxy)."),
    "error": (500, "InternalError", "We encountered an internal error (test proxy)."),
}
_HOP = {"connection", "keep-alive", "transfer-encoding", "proxy-connection", "upgrade", "te", "trailer"}


class FaultProxy:
    def __init__(self, upstream: str, port: int = 0, host: str = "127.0.0.1"):
        u = urlsplit(upstream)
        self.up_host, self.up_port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        if u.scheme == "https":
            raise ValueError("the test proxy talks plain http to the test storage")
        self.mode = "ok"
        self.delay_before_put = 0.0
        self.delay_after_put = 0.0
        self.requests: dict[str, int] = {}          # how many requests arrived in each mode
        self.puts_stored = 0                          # uploads the real storage accepted
        self.held = threading.Event()                 # set while an answer is held (delay_after_put)
        self._lock = threading.Lock()
        self._n = 0
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            upstream: http.client.HTTPConnection | None = None

            def log_message(self, *a):               # silence the per-request log lines
                pass

            def _drop(self):
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def _body(self, cut: bool = False) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                if cut:                               # take only part of the body, then the line dies
                    n //= 2
                data = b""
                while len(data) < n:
                    chunk = self.rfile.read(min(n - len(data), 1 << 20))
                    if not chunk:
                        break
                    data += chunk
                return data

            def _reply_error(self, mode: str):
                status, code, msg = _ERRORS[mode]
                self._body()
                payload = _XML.format(code=code, msg=msg).encode()
                if self.command != "HEAD":
                    self.send_response(status)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def _wait(self, seconds: float) -> bool:
                """Sleep, but give up (and return False) if the line is cut meanwhile."""
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if proxy.mode == "down":
                        return False
                    time.sleep(0.05)
                return True

            def _handle(self):
                with proxy._lock:
                    proxy._n += 1
                    mode = proxy.mode
                    if mode == "flaky":
                        mode = "error" if proxy._n % 2 else "ok"
                    proxy.requests[mode] = proxy.requests.get(mode, 0) + 1
                is_put = self.command == "PUT"
                try:
                    if mode == "down":
                        if is_put:
                            self._body(cut=True)
                        return self._drop()
                    if mode in _ERRORS:
                        return self._reply_error(mode)
                    body = self._body()
                    if is_put and proxy.delay_before_put and not self._wait(proxy.delay_before_put):
                        return self._drop()
                    status, headers, data = self._forward(body)
                    if is_put and 200 <= status < 300:
                        proxy.puts_stored += 1
                        if proxy.delay_after_put:
                            proxy.held.set()
                            try:
                                if not self._wait(proxy.delay_after_put):
                                    return self._drop()     # cut after the object was stored
                            finally:
                                proxy.held.clear()
                    self.send_response(status)
                    for k, v in headers:
                        if k.lower() not in _HOP and k.lower() not in ("server", "date", "content-length"):
                            self.send_header(k, v)
                    if self.command == "HEAD":              # no body, but the object's length is the answer
                        length = next((v for k, v in headers if k.lower() == "content-length"), None)
                        if length is not None:
                            self.send_header("Content-Length", length)
                    else:
                        self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self._drop()
                except Exception:                      # the real storage is not answering
                    self._drop()

            def _forward(self, body: bytes):
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in _HOP and k.lower() not in ("expect", "content-length")}
                if body or self.command in ("PUT", "POST"):
                    headers["Content-Length"] = str(len(body))
                for attempt in (1, 2):                 # a kept-open connection may have been closed
                    try:
                        if self.upstream is None:
                            self.upstream = http.client.HTTPConnection(proxy.up_host, proxy.up_port, timeout=60)
                        # skip_host: the Host header is part of the signature, so it must stay as the client sent it
                        self.upstream.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                        for k, v in headers.items():
                            self.upstream.putheader(k, v)
                        self.upstream.endheaders(body if body else None)
                        resp = self.upstream.getresponse()
                        data = b"" if self.command == "HEAD" else resp.read()
                        return resp.status, resp.getheaders(), data
                    except (http.client.HTTPException, OSError):
                        try:
                            if self.upstream:
                                self.upstream.close()
                        finally:
                            self.upstream = None
                        if attempt == 2:
                            raise

            do_GET = do_PUT = do_HEAD = do_POST = do_DELETE = _handle

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 128

        self._srv = Server((host, port), Handler)
        self.host, self.port = host, self._srv.server_address[1]
        self.url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, name="fault-proxy", daemon=True)

    def start(self) -> "FaultProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode

    def total_requests(self, *modes: str) -> int:
        return sum(self.requests.get(m, 0) for m in (modes or self.requests))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mode", default="ok", choices=MODES)
    args = ap.parse_args()
    p = FaultProxy(args.upstream, args.port, args.host).start()
    p.set_mode(args.mode)
    print(f"proxy {p.url} -> {args.upstream}, mode {p.mode}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        p.stop()


if __name__ == "__main__":
    main()
