"""Storage wrappers used to inject failures and to count real uploads."""
import os
import tempfile

from segment_uploader.storage import S3Storage


class Crash(BaseException):
    """Simulates the process being killed (not an ordinary error, so it is not caught)."""


class CountingStorage(S3Storage):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.puts = 0

    def put(self, *args, **kwargs):
        self.puts += 1
        return super().put(*args, **kwargs)


class CrashAfterPutStorage(CountingStorage):
    """The object reaches the server, then the process dies before saving its state."""

    def put(self, *args, **kwargs):
        super().put(*args, **kwargs)
        raise Crash("killed right after upload")


class TamperingStorage(CountingStorage):
    """Sends a damaged body while still announcing the checksum of the correct one."""

    def put(self, key, path, md5_b64, sha256, size):
        data = bytearray(open(path, "rb").read())
        data[0] ^= 0xFF
        fd, tmp = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return super().put(key, tmp, md5_b64, sha256, size)
        finally:
            os.unlink(tmp)


class LyingHeadStorage(CountingStorage):
    """Upload works, but the server's answer afterwards does not match the local file."""

    def head(self, key):
        remote = super().head(key)
        return remote._replace(etag="0" * 32) if remote else None


class ModifyLocalDuringPutStorage(CountingStorage):
    """The local file is changed while it is being uploaded."""

    def put(self, key, path, md5_b64, sha256, size):
        super().put(key, path, md5_b64, sha256, size)
        with open(path, "ab") as f:
            f.write(b"more data appended by the recorder")
