#!/usr/bin/env python3
"""M7: save each camera's substream as 60-second MP4 segments for the cloud uploader.

Runs in the `substream` container (the pinned go2rtc image, for its ffmpeg and Python).
One ffmpeg per camera reads `<camera>_sub` from go2rtc and copies the video, without
transcoding, into the layout of docs/SEGMENT_FORMAT.md:

  <ARCHIVE_DIR>/<camera_id>/<YYYY-MM-DD>/<camera_id>_<start>Z_<end>Z.mp4

ffmpeg writes `<camera_id>_<start>Z.mp4.part` and reports each finished segment on its
segment list; this supervisor then renames the file to the full name. `.part` files that
ffmpeg could not finish (killed, crashed) are deleted, never renamed. Segments are cut at
the first keyframe after each full minute (UTC). Retention is in tools/storage_guard.py.

Cameras and segment length come from services/config.json (generated from cameras.yaml).
Settings (environment):
  ARCHIVE_DIR       default /recordings/substream
  GO2RTC_RTSP       default rtsp://go2rtc:8554, login from FRIGATE_GO2RTC_RTSP_USER/PASSWORD
"""
import calendar
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vivo  # noqa: E402

ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/recordings/substream"))
GO2RTC_RTSP = os.environ.get("GO2RTC_RTSP", "rtsp://go2rtc:8554")
SEGMENT_SECONDS = 60                  # docs/SEGMENT_FORMAT.md; replaced from services/config.json
RTSP_TIMEOUT_US = 10_000_000          # ffmpeg gives up on a silent connection after 10 s
STAMP = "%Y%m%dT%H%M%S"
# A segment cannot end later than when ffmpeg reported it, plus this much slack for rounding.
CLOCK_SLACK = 2.0
# Restart ffmpeg when no segment has finished for this many segment lengths (a stream that stalls
# but keeps the connection open; a closed or silent connection is caught by RTSP_TIMEOUT_US).
STALL_SEGMENTS = 3.5
SUMMARY_SECONDS = 600

PART_RE = re.compile(r"^(?P<cam>[a-z][a-z0-9_]*)_(?P<start>\d{8}T\d{6})Z\.mp4\.part$")
SEGMENT_RE = re.compile(r"^(?P<cam>[a-z][a-z0-9_]*)_(?P<start>\d{8}T\d{6})Z_(?P<end>\d{8}T\d{6})Z\.mp4$")

stopping = threading.Event()


def log(cam: str, msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {cam}: {vivo.redact(msg)}", flush=True)


def parse_stamp(s: str) -> int:
    """'20261023T051710' -> UTC epoch seconds."""
    return calendar.timegm(time.strptime(s, STAMP))


def stamp(t: float) -> str:
    return time.strftime(STAMP, time.gmtime(int(t)))


def day_dir(root: Path, cam: str, t: float) -> Path:
    return root / cam / time.strftime("%Y-%m-%d", time.gmtime(int(t)))


def segment_name(cam: str, start: int, end: int) -> str:
    return f"{cam}_{stamp(start)}Z_{stamp(end)}Z.mp4"


def segment_times(opened: int, duration: float, next_start: int | None, now: float | None = None) -> tuple[int, int]:
    """(start, end) for the file name of a finished segment.

    `opened` is when ffmpeg opened the file (its .part name). While ffmpeg runs, it cuts a segment
    exactly where it opens the next one, so with a next segment the end is that segment's start,
    and the start is end - duration: the first segment after ffmpeg starts is opened on connect,
    but its video only begins at the first keyframe, up to one keyframe interval later.
    Without a next segment (ffmpeg stopped) the end is start + duration, never later than `now`.
    """
    if next_start is not None:
        start = opened
        if next_start - duration - opened > 1.0:   # names are to the second: more than 1 s is real
            start = int(round(next_start - duration))
        return start, max(start, next_start)
    end = opened + duration if now is None else min(opened + duration, now)
    return opened, max(opened, int(round(end)))


def mp4_duration(path: Path) -> float | None:
    """Duration from the MP4 header (moov/mvhd), which faststart puts at the front of the file."""
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 20)
    except OSError:
        return None

    def boxes(buf: bytes, pos: int, end: int):
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", buf[pos:pos + 8])
            header = 8
            if size == 1 and pos + 16 <= end:
                size, header = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
            if size < header:
                return
            yield kind, pos + header, min(pos + size, end)
            pos += size

    for kind, start, end in boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for sub, s, e in boxes(data, start, end):
            if sub == b"mvhd" and e - s >= 32:
                if data[s] == 1:   # version 1: 64-bit times
                    timescale, duration = struct.unpack(">IQ", data[s + 20:s + 32])
                else:
                    timescale, duration = struct.unpack(">II", data[s + 12:s + 20])
                return duration / timescale if timescale else None
    return None


def parse_list_line(line: str) -> tuple[str, float] | None:
    """One line of ffmpeg's csv segment list, 'name,start,end' -> (name, duration in seconds)."""
    parts = line.strip().rsplit(",", 2)
    if len(parts) != 3:
        return None
    try:
        return parts[0].strip('"'), max(0.0, float(parts[2]) - float(parts[1]))
    except ValueError:
        return None


def part_files(root: Path, cam: str) -> list[Path]:
    """This camera's .part files in yesterday's, today's and tomorrow's folders, oldest first."""
    now = time.time()
    found = []
    for d in {day_dir(root, cam, now + off) for off in (-86400, 0, 86400)}:
        if d.is_dir():
            found += [p for p in d.iterdir() if (m := PART_RE.match(p.name)) and m["cam"] == cam]
    return sorted(found, key=lambda p: p.name)


def ensure_day_dirs(root: Path, cam: str) -> None:
    # ffmpeg does not create folders, so tomorrow's must exist before midnight UTC.
    now = time.time()
    for t in (now, now + 86400):
        day_dir(root, cam, t).mkdir(parents=True, exist_ok=True)


def source_url(cam: str) -> str:
    u = os.environ.get("FRIGATE_GO2RTC_RTSP_USER", "")
    p = os.environ.get("FRIGATE_GO2RTC_RTSP_PASSWORD", "")
    cred = f"{u}:{p}@" if u else ""
    return GO2RTC_RTSP.replace("rtsp://", f"rtsp://{cred}", 1) + f"/{cam}_sub"


def probe_video(url: str) -> tuple[str, bool] | None:
    """(codec, has B-frames) of the substream, or None if go2rtc does not deliver it."""
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(RTSP_TIMEOUT_US),
           "-select_streams", "v:0", "-show_entries", "stream=codec_name,has_b_frames", "-of", "csv=p=0", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    fields = out.stdout.strip().splitlines()[0].split(",") if out.stdout.strip() else []
    if out.returncode != 0 or not fields or not fields[0]:
        return None
    return fields[0], len(fields) > 1 and fields[1].strip() not in ("", "0")


def ffmpeg_cmd(root: Path, cam: str, url: str, codec: str, b_frames: bool = False) -> list[str]:
    pattern = root / cam / "%Y-%m-%d" / f"{cam}_%Y%m%dT%H%M%SZ.mp4.part"
    # MP4 players and browsers expect the hvc1 tag for H.265 (ffmpeg writes hev1 by default).
    tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
    # Frame times from the arrival clock, as in Frigate's preset-rtsp-generic: when go2rtc reconnects
    # to a camera, the camera's own timestamps restart from a new base, and a copied segment would
    # claim hours of video. Only safe without B-frames (the reference cameras send none, F11).
    clock = [] if b_frames else ["-use_wallclock_as_timestamps", "1"]
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt",
        "-rtsp_transport", "tcp", "-timeout", str(RTSP_TIMEOUT_US), *clock, "-i", url,
        "-map", "0:v:0", "-c", "copy", *tag,
        # Start each run's timestamps at its first keyframe; otherwise the first segment begins with
        # an empty edit as long as the wait for that keyframe (up to one keyframe interval).
        "-bsf:v", "setts=ts=TS-STARTPTS",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_atclocktime", "1",
        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
        "-reset_timestamps", "1", "-strftime", "1",
        "-segment_list", "pipe:1", "-segment_list_type", "csv",
        str(pattern),
    ]


class Camera(threading.Thread):
    """Keeps one ffmpeg running for a camera and turns its finished segments into final names."""

    def __init__(self, root: Path, cam: str):
        super().__init__(name=cam, daemon=True)
        self.root, self.cam = root, cam
        self.proc: subprocess.Popen | None = None
        self.last_done = time.time()
        self.done = 0

    def run(self) -> None:
        backoff = 2
        while not stopping.is_set():
            ensure_day_dirs(self.root, self.cam)
            self.drop_parts("left by an earlier ffmpeg")
            started = time.time()
            reason = self.run_ffmpeg()
            self.drop_parts("not finished by ffmpeg")
            if stopping.is_set():
                break
            ran = time.time() - started
            backoff = 2 if ran > 60 else min(backoff * 2, 30)
            log(self.cam, f"{reason} after {ran:.0f} s; starting again in {backoff} s")
            stopping.wait(backoff)

    def run_ffmpeg(self) -> str:
        url = source_url(self.cam)
        video = probe_video(url)
        if video is None:
            return "substream not available from go2rtc"
        codec, b_frames = video
        if b_frames:
            log(self.cam, "the substream has B-frames: keeping the camera's own timestamps (a camera "
                          "reconnect can then leave one segment with a wrong length)")
        self.proc = subprocess.Popen(ffmpeg_cmd(self.root, self.cam, url, codec, b_frames), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        log(self.cam, f"recording {codec} substream (ffmpeg pid {self.proc.pid})")
        self.last_done = time.time()
        threading.Thread(target=self.read_stderr, args=(self.proc,), daemon=True).start()
        watchdog = threading.Thread(target=self.watch, args=(self.proc,), daemon=True)
        watchdog.start()
        for line in self.proc.stdout:
            entry = parse_list_line(line)
            if entry:
                self.finish(*entry)
        rc = self.proc.wait()
        return "ffmpeg was stopped by the watchdog" if getattr(self.proc, "stalled", False) else f"ffmpeg exited ({rc})"

    def read_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            if line.strip():
                log(self.cam, f"ffmpeg: {line.strip()}")

    def watch(self, proc: subprocess.Popen) -> None:
        """Make tomorrow's folder ahead of time, log a summary, and restart a stalled ffmpeg."""
        last_summary, done_then = time.time(), self.done
        while proc.poll() is None:
            time.sleep(5)
            ensure_day_dirs(self.root, self.cam)
            now = time.time()
            if now - self.last_done > STALL_SEGMENTS * SEGMENT_SECONDS and proc.poll() is None:
                log(self.cam, f"no segment finished for {now - self.last_done:.0f} s; restarting ffmpeg")
                proc.stalled = True
                proc.kill()
            if now - last_summary >= SUMMARY_SECONDS:
                log(self.cam, f"{self.done - done_then} segments in the last {SUMMARY_SECONDS // 60} min")
                last_summary, done_then = now, self.done

    def next_start(self, after: int, wait: float = 2.0) -> int | None:
        """Start of the segment ffmpeg opened after this one; ffmpeg opens it right after reporting."""
        deadline = time.time() + wait
        while True:
            later = [parse_stamp(m["start"]) for p in part_files(self.root, self.cam)
                     if (m := PART_RE.match(p.name)) and parse_stamp(m["start"]) > after]
            if later:
                return min(later)
            if time.time() >= deadline or (self.proc and self.proc.poll() is not None):
                return None
            time.sleep(0.1)

    def finish(self, name: str, duration: float) -> None:
        m = PART_RE.match(name)
        if not m or m["cam"] != self.cam:
            log(self.cam, f"unexpected segment name {name!r} from ffmpeg")
            return
        start = parse_stamp(m["start"])
        finished = time.time()
        part = day_dir(self.root, self.cam, start) / name
        if not part.exists():
            log(self.cam, f"{name} reported finished but missing")
            return
        # The MP4's own duration, the length players and the uploader see; ffmpeg's segment list
        # counts from when it connected, which can be up to one keyframe interval longer, never shorter.
        header = mp4_duration(part)
        if header and 0 < header <= duration + 1.0:
            duration = header
        start, end = segment_times(start, duration, self.next_start(start), now=finished + CLOCK_SLACK)
        final = day_dir(self.root, self.cam, start) / segment_name(self.cam, start, end)
        if final.exists():
            log(self.cam, f"{final.name} already exists; deleting the duplicate {name}")
            part.unlink()
            return
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(part, final)  # same disk (and nearly always the same folder), so the rename is atomic
        self.done += 1
        self.last_done = time.time()

    def drop_parts(self, why: str) -> None:
        for p in part_files(self.root, self.cam):
            log(self.cam, f"deleting {p.name}: {why}")
            p.unlink(missing_ok=True)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # ffmpeg finishes the current segment, then exits


def main() -> int:
    global SEGMENT_SECONDS
    if not ARCHIVE_DIR.is_dir():
        print(f"ERROR: {ARCHIVE_DIR} does not exist (recording disk not mounted?)", file=sys.stderr)
        return 1
    config = vivo.services_config()
    SEGMENT_SECONDS = int(config["segment_seconds"])
    cams = [Camera(ARCHIVE_DIR, cam) for cam in config["cameras"]]

    def on_signal(signum, _frame):
        if not stopping.is_set():
            print(f"stopping on signal {signum}: finishing the current segments", flush=True)
            stopping.set()
            for c in cams:
                c.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print(f"substream archiver: {len(cams)} cameras -> {ARCHIVE_DIR}, {SEGMENT_SECONDS} s segments", flush=True)
    for c in cams:
        c.start()
    while any(c.is_alive() for c in cams):
        for c in cams:
            c.join(timeout=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
