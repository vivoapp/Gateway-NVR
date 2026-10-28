"""S3-compatible storage access (AWS S3, MinIO, ...)."""
from __future__ import annotations

import re
from typing import NamedTuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import Config

_MD5_HEX = re.compile(r"^[0-9a-f]{32}$")


class IntegrityError(Exception):
    """The stored object does not match the local file."""


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
    def __init__(self, cfg: Config):
        self.bucket = cfg.bucket
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
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def count(self, prefix: str = "") -> int:
        """Number of objects under a prefix (used by the status command)."""
        total = 0
        for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix):
            total += len(page.get("Contents", []))
        return total
