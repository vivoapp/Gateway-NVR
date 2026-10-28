"""S3-compatible storage access (AWS S3, MinIO, ...)."""
from __future__ import annotations

import re
import threading
import time
from typing import NamedTuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, HTTPClientError

from .config import Config

_MD5_HEX = re.compile(r"^[0-9a-f]{32}$")


class IntegrityError(Exception):
    """The stored object does not match the local file."""


# ---- what kind of failure is this? --------------------------------------------------------
# "outage"  the storage cannot be reached or is unhealthy   -> pause the whole service, back off
# "auth"    wrong or expired credentials, missing bucket    -> pause the whole service, say so loudly
# "full"    the account or bucket has no room / quota hit   -> pause the whole service, say so loudly
# "file"    something is wrong with this one file           -> only this file waits, others go on
PAUSING_KINDS = ("outage", "auth", "full")
_AUTH_CODES = {"InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "ExpiredToken",
               "InvalidToken", "NoSuchBucket", "InvalidBucketName", "AllAccessDisabled",
               "bad_auth_token", "expired_auth_token", "unauthorized"}
_FULL_MARKERS = ("storagefull", "quota", "cap_exceeded", "capexceeded", "insufficientstorage",
                 "storagelimit")


def classify(exc: BaseException) -> str:
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return "outage"
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if status == 507 or any(m in code.lower() for m in _FULL_MARKERS):
            return "full"            # checked before auth: some stores answer "full" with a 403
        if code in _AUTH_CODES or status in (401, 403):
            return "auth"
        if status >= 500 or code in ("SlowDown", "RequestTimeout", "ServiceUnavailable"):
            return "outage"
    return "file"


class RateLimiter:
    """Shared upload speed limit for all workers (megabits per second; 0 = unlimited)."""

    def __init__(self, mbps: float, clock=time.monotonic, sleep=time.sleep):
        self.bytes_per_s = mbps * 1_000_000 / 8
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0                     # time at which the "wire" is free again

    def wait(self, nbytes: int) -> None:
        if self.bytes_per_s <= 0 or nbytes <= 0:
            return
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + nbytes / self.bytes_per_s
            delay = start - now
        if delay > 0:
            self._sleep(delay)


class ThrottledFile:
    """A file object whose reads are slowed to the shared limit (used as the upload body)."""

    def __init__(self, f, limiter: RateLimiter, chunk: int = 64 * 1024):
        self._f, self._limiter, self._chunk = f, limiter, chunk

    def read(self, size: int = -1) -> bytes:
        out = bytearray()
        want = size
        while want != 0:
            data = self._f.read(self._chunk if want < 0 else min(self._chunk, want))
            if not data:
                break
            self._limiter.wait(len(data))
            out += data
            if want > 0:
                want -= len(data)
        return bytes(out)

    def seek(self, pos, whence=0):
        return self._f.seek(pos, whence)

    def tell(self):
        return self._f.tell()

    def __getattr__(self, name):
        return getattr(self._f, name)


class RemoteObject(NamedTuple):
    size: int
    etag: str
    sha256: str | None


def remote_matches(remote: RemoteObject, size: int, md5_hex: str, sha256: str) -> bool:
    """True if the remote object is byte-for-byte what we have locally.

    For a normal single-part upload the S3 ETag is the MD5 of the content, and the server
    computed it itself, so it is an independent check. If the ETag is not a plain MD5
    (e.g. a multipart upload) we fall back to the sha256 stored in the object metadata.
    """
    if remote.size != size:
        return False
    if _MD5_HEX.match(remote.etag.lower()):
        return remote.etag.lower() == md5_hex
    return remote.sha256 == sha256


class S3Storage:
    def __init__(self, cfg: Config, limiter: RateLimiter | None = None):
        self.bucket = cfg.bucket
        self.prefix = cfg.key_prefix.strip("/")
        self.limiter = limiter or RateLimiter(cfg.upload_limit_mbps)
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            region_name=cfg.region,
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                # Retries are handled by the service (persisted, with backoff), not by boto.
                retries={"max_attempts": 1, "mode": "standard"},
                connect_timeout=cfg.connect_timeout,
                read_timeout=cfg.read_timeout,
                # Some S3-compatible servers do not support the newer default checksums.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        self.client.meta.events.register("before-send.s3.PutObject", self._throttle_body)

    def _throttle_body(self, request, **_):
        """Slow down the bytes that really go on the wire.

        This runs just before the request is sent, after botocore has finished signing. Over plain
        http (a local MinIO) some botocore versions read the whole body once more to sign it;
        wrapping the file earlier would slow that extra pass down too and halve the speed.
        """
        body = request.body
        if self.limiter.bytes_per_s > 0 and hasattr(body, "read"):
            request.body = ThrottledFile(body, self.limiter)

    def head(self, key: str) -> RemoteObject | None:
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return RemoteObject(r["ContentLength"], r["ETag"].strip('"'),
                            (r.get("Metadata") or {}).get("sha256"))

    def put(self, key: str, path, md5_b64: str, sha256: str, size: int) -> None:
        """Upload one file. The server checks Content-MD5 and rejects a damaged body."""
        with open(path, "rb") as f:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=f, ContentLength=size,
                                   ContentMD5=md5_b64, Metadata={"sha256": sha256})

    def delete(self, key: str) -> None:
        """Remove an object this service has just written (a copy that failed the read-back check).

        It refuses any key outside our own prefix, so a bug can never delete someone else's data.
        """
        if self.prefix and not key.startswith(self.prefix + "/"):
            raise ValueError(f"refusing to delete {key!r}: outside the prefix {self.prefix!r}")
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def count(self, prefix: str = "") -> int:
        """Number of objects under a prefix (used by the status command)."""
        total = 0
        for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix):
            total += len(page.get("Contents", []))
        return total
