#!/usr/bin/env python3
"""The 72-hour test (Block C, contract section 13). Runs on the gateway, after funding.

While 10 cameras record, every minute it opens a live view (the MSE fallback, so no browser is
needed), plays back 5 minutes from the last 24 hours, and every 10 minutes prepares and downloads a
1-minute clip. It restarts the API every 6 hours and go2rtc plus Frigate every 12 hours, and logs
each restart; the one reboot is scheduled on the host (below) and noticed by the gap in its own log.
At the end it writes reports/soak72-<label>-<UTC>.md with the agreed pass/fail criteria:

  - no recording gap over 10 s outside the planned restarts and the reboot
  - after each restart: recording back within 60 s, live view and playback back within 2 minutes
  - at least 99% of the scripted actions succeed (actions during a restart window do not count)
  - each container's memory in the last 12 hours within 10% of hours 12 to 24
  - CPU under 85% as a 5-minute average

Run it in the API's test image with host networking (it reaches the API on the LAN address, so its
requests count as the building network) and the Docker socket (for the planned restarts and memory):

  docker run -d --name vivo-soak72 --restart unless-stopped --network host \\
    -v /var/run/docker.sock:/var/run/docker.sock -v /srv/vivo/video-api-soak:/out \\
    -v /etc/vivo/video-api-test:/testkey:ro vivo-video-api:test \\
    python tests/soak72.py --key /testkey/test-1.private.pem --aud vivo-gw:<GATEWAY_ID> \\
      --api https://<GATEWAY_LAN_IP>:8443 --cams cam01,...,cam10 --out /out --label gateway
  sudo systemd-run --on-active=36h --unit vivo-soak-reboot systemctl reboot      # the one reboot

  tests/soak72.py --report-only --out /srv/vivo/video-api-soak ...               # report from the logs
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import random
import ssl
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
import grant_issuer  # noqa: E402
from vivo_api import mp4  # noqa: E402

CYCLE = 60
API_RESTART_EVERY = 6 * 3600
STACK_RESTART_EVERY = 12 * 3600
STACK_OFFSET = 1800                    # go2rtc and Frigate 30 minutes after an API restart, to tell them apart
CONTAINERS = ("video-api", "go2rtc", "frigate", "substream", "storage")
GAP_LIMIT, RECORD_BACK, FEATURES_BACK = 10.0, 60.0, 120.0


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ------------------------------------------------------------------ pure evaluation (unit tested)
def in_window(t: float, events: list[dict], after: float = FEATURES_BACK) -> bool:
    """True when time t falls inside a planned event or the recovery time after it."""
    return any(e["start"] - 5 <= t <= e["end"] + after for e in events)


def gaps_outside(gaps: list[tuple[float, float]], events: list[dict]) -> list[tuple[float, float]]:
    """Recording gaps over GAP_LIMIT that do not touch a planned event (with its recovery time)."""
    out = []
    for a, b in gaps:
        if b - a <= GAP_LIMIT:
            continue
        if any(a <= e["end"] + RECORD_BACK and b >= e["start"] - 5 for e in events):
            continue
        out.append((a, b))
    return out


def success_rate(actions: list[dict], events: list[dict]) -> tuple[int, int]:
    counted = [a for a in actions if not in_window(a["t"], events) and not a.get("skipped")]
    return sum(1 for a in counted if a["ok"]), len(counted)


def memory_growth(samples: list[dict], start: float, end: float) -> dict[str, tuple[float, float, float]]:
    """Per container: (mean MB in hours 12 to 24, mean MB in the last 12 hours, ratio)."""
    out = {}
    for name in CONTAINERS:
        early = [s["mem"][name] for s in samples if name in s.get("mem", {}) and start + 12 * 3600 <= s["t"] < start + 24 * 3600]
        late = [s["mem"][name] for s in samples if name in s.get("mem", {}) and s["t"] >= end - 12 * 3600]
        if early and late:
            e, l_ = sum(early) / len(early), sum(late) / len(late)
            out[name] = (round(e, 1), round(l_, 1), round(l_ / e, 3) if e else 0.0)
    return out


def cpu_peak_5min(samples: list[dict]) -> float | None:
    cpu = [(s["t"], s["cpu"]) for s in samples if s.get("cpu") is not None]
    best = None
    for i, (t, _) in enumerate(cpu):
        window = [c for tt, c in cpu[i:] if tt < t + 300]
        if len(window) >= 3:
            avg = sum(window) / len(window)
            best = avg if best is None else max(best, avg)
    return round(best, 1) if best is not None else None


def evaluate(events, actions, samples, gaps_by_cam, start, end) -> list[tuple[str, bool, str]]:
    checks = []
    bad = {c: gaps_outside(g, events) for c, g in gaps_by_cam.items()}
    n_bad = sum(len(v) for v in bad.values())
    checks.append((f"no recording gap over {GAP_LIMIT:.0f} s outside planned restarts", n_bad == 0,
                   "none" if not n_bad else "; ".join(f"{c} {utc(a)} {b - a:.0f} s" for c, v in bad.items() for a, b in v[:3])))
    slow_rec = [e for e in events if e.get("recording_back_s") is None or e["recording_back_s"] > RECORD_BACK]
    slow_feat = [e for e in events if e.get("features_back_s") is None or e["features_back_s"] > FEATURES_BACK]
    checks.append((f"recording back within {RECORD_BACK:.0f} s after each restart", bool(events) and not slow_rec,
                   f"{len(events)} events; slowest {max((e.get('recording_back_s') or 1e9) for e in events) if events else '-'} s"))
    checks.append((f"live view and playback back within {FEATURES_BACK:.0f} s", bool(events) and not slow_feat,
                   f"slowest {max((e.get('features_back_s') or 1e9) for e in events) if events else '-'} s"))
    ok, n = success_rate(actions, events)
    checks.append(("at least 99% of scripted actions succeed", n > 0 and ok / n >= 0.99, f"{ok} of {n} ({100 * ok / n:.2f}%)" if n else "none"))
    mem = memory_growth(samples, start, end)
    grew = {k: v for k, v in mem.items() if v[2] > 1.10}
    checks.append(("memory: last 12 h within 10% of hours 12 to 24", bool(mem) and not grew,
                   ", ".join(f"{k} {a} to {b} MB ({c:.2f})" for k, (a, b, c) in mem.items()) or "no samples"))
    peak = cpu_peak_5min(samples)
    checks.append(("CPU under 85% (5-minute average)", peak is not None and peak < 85, f"peak {peak}%"))
    return checks


# ------------------------------------------------------------------ the run
class Soak:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "state.json"
        self.key, self.kid = grant_issuer.load_key(a.key), grant_issuer.kid_of(a.key)
        self.cams = a.cams.split(",")
        self.http = httpx.AsyncClient(base_url=a.api, verify=False, timeout=60)        # self-signed until M23
        self.docker = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
                                        base_url="http://docker", timeout=120)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def grant(self, cam: str, perms: list[str]) -> dict:
        return {"Authorization": "Bearer " + grant_issuer.sign(self.key, self.kid, self.a.aud, [cam], perms, sub="soak72", ttl=90)}

    def log(self, name: str, rec: dict) -> None:
        with open(self.out / f"{name}.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.state_path)

    async def live(self, cam: str) -> None:
        """Live view through the MSE fallback: a session, the WebSocket, video bytes within 8 s."""
        from websockets.asyncio.client import connect
        t0 = time.time()
        r = await self.http.post(f"/v1/cameras/{cam}/live", json={"stream": "sub"}, headers=self.grant(cam, ["live"]))
        r.raise_for_status()
        sid = r.json()["session_id"]
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        url = self.a.api.replace("https://", "wss://") + f"/v1/live/{sid}/mse"
        got = 0
        try:
            async with connect(url, ssl=ctx, open_timeout=10, max_size=None) as ws:
                await ws.send(json.dumps({"type": "mse", "value": "avc1.640029,avc1.64002A,avc1.4D401F,hvc1.1.6.L153.B0,mp4a.40.2"}))
                while got < 20000 and time.time() - t0 < 8:
                    msg = await asyncio.wait_for(ws.recv(), timeout=8)
                    if isinstance(msg, bytes):
                        got += len(msg)
        finally:
            await self.http.delete(f"/v1/live/{sid}", headers=self.grant(cam, ["live"]))
        if got < 20000:
            raise RuntimeError(f"only {got} bytes of video in 8 s")

    async def playback(self, cam: str) -> bool:
        """5 minutes from a random time in the last 24 hours: playlist and its first piece. False when
        that time has no recording (not counted)."""
        start = time.time() - random.uniform(600, 86400)
        r = await self.http.post(f"/v1/cameras/{cam}/playback", json={"start": utc(start), "end": utc(start + 300)},
                                 headers=self.grant(cam, ["playback"]))
        if r.status_code == 404 and r.json().get("error", {}).get("code") == "no_recording":
            return False
        r.raise_for_status()
        url = r.json()["url"]
        base = url.rsplit("/", 1)[0]
        pl = (await self.http.get(url)).text
        names = [x for x in pl.splitlines() if x and not x.startswith("#")]
        if names and names[0].endswith(".m3u8"):
            pl = (await self.http.get(f"{base}/{names[0]}")).text
            names = [x for x in pl.splitlines() if x and not x.startswith("#")]
        piece = await self.http.get(f"{base}/{names[0]}")
        if piece.status_code != 200 or len(piece.content) < 100:
            raise RuntimeError(f"first piece: HTTP {piece.status_code}, {len(piece.content)} bytes")
        return True

    async def clip(self, cam: str) -> None:
        h = self.grant(cam, ["export"])
        now = time.time()
        r = await self.http.post(f"/v1/cameras/{cam}/clips", json={"start": utc(now - 660), "end": utc(now - 600)}, headers=h)
        r.raise_for_status()
        cid = r.json()["clip_id"]
        for _ in range(60):
            b = (await self.http.get(f"/v1/clips/{cid}", headers=self.grant(cam, ["export"]))).json()
            if b["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(1)
        if b["status"] != "ready":
            raise RuntimeError(f"clip {b['status']}: {b.get('error')}")
        d = await self.http.get(f"/v1/clips/{cid}/download", headers=self.grant(cam, ["export"]))
        path = self.out / "last-clip.mp4"
        path.write_bytes(d.content)
        bad = mp4.problem(path)
        path.unlink(missing_ok=True)
        await self.http.delete(f"/v1/clips/{cid}", headers=self.grant(cam, ["export"]))
        if bad:
            raise RuntimeError(f"clip not a whole MP4: {bad}")

    async def act(self, kind: str, cam: str) -> bool:
        t0 = time.time()
        rec = {"t": t0, "kind": kind, "cam": cam, "ok": False}
        try:
            fn = {"live": self.live, "playback": self.playback, "clip": self.clip}[kind]
            res = await fn(cam)
            rec["ok"] = True
            if res is False:
                rec["skipped"] = True
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rec["ms"] = round((time.time() - t0) * 1000)
        self.log("actions", rec)
        return rec["ok"] and not rec.get("skipped")

    async def sample(self) -> None:
        s = {"t": time.time(), "cpu": None, "mem": {}}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                s["cpu"] = (await c.get(self.a.health)).json().get("gateway", {}).get("cpu_percent")
        except Exception:
            pass
        for name in CONTAINERS:
            try:
                st = (await self.docker.get(f"/containers/{name}/stats", params={"stream": "false"})).json()
                used = st["memory_stats"]["usage"] - st["memory_stats"].get("stats", {}).get("inactive_file", 0)
                s["mem"][name] = round(used / 2 ** 20, 1)
            except Exception:
                pass
        self.log("samples", s)

    async def restart(self, kind: str, names: list[str]) -> None:
        ev = {"kind": kind, "start": time.time()}
        for n in names:
            await self.docker.post(f"/containers/{n}/restart", params={"t": 10})
        ev["end"] = time.time()
        await self.recover(ev)

    async def recover(self, ev: dict) -> None:
        """Times until every camera records again and live view plus playback work again."""
        rec_at = feat_at = None
        deadline = ev["end"] + 600
        while time.time() < deadline and (rec_at is None or feat_at is None):
            if rec_at is None:
                fresh = 0
                for cam in self.cams:
                    try:
                        r = await self.http.get(f"/v1/cameras/{cam}/status", headers=self.grant(cam, ["live"]))
                        end = r.json()["recording"]["newest_segment_end"]
                        if end and calendar.timegm(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")) > ev["end"]:
                            fresh += 1
                    except Exception:
                        pass
                if fresh == len(self.cams):
                    rec_at = time.time()
            if feat_at is None and await self.act("live", self.cams[0]) and await self.act("playback", self.cams[0]):
                feat_at = time.time()
            await asyncio.sleep(5)
        ev["recording_back_s"] = round(rec_at - ev["end"], 1) if rec_at else None
        ev["features_back_s"] = round(feat_at - ev["end"], 1) if feat_at else None
        self.log("events", ev)

    async def run(self) -> None:
        st = self.state
        now = time.time()
        if "start" not in st:
            st.update(start=now, next_api=now + API_RESTART_EVERY, next_stack=now + STACK_RESTART_EVERY + STACK_OFFSET,
                      cycle=0, last=now)
        elif now - st["last"] > 3 * CYCLE:                 # this run was away: the reboot (or a crash)
            await self.recover({"kind": "reboot", "start": st["last"], "end": now})
        end = st["start"] + self.a.hours * 3600
        while time.time() < end:
            t0 = time.time()
            cam = self.cams[st["cycle"] % len(self.cams)]
            await self.act("live", cam)
            await self.act("playback", cam)
            if st["cycle"] % 10 == 0:
                await self.act("clip", cam)
            await self.sample()
            if time.time() >= st["next_api"]:
                await self.restart("api_restart", ["video-api"])
                st["next_api"] += API_RESTART_EVERY
            if time.time() >= st["next_stack"]:
                await self.restart("stack_restart", ["go2rtc", "frigate"])
                st["next_stack"] += STACK_RESTART_EVERY
            st["cycle"] += 1
            st["last"] = time.time()
            self.save()
            await asyncio.sleep(max(1.0, CYCLE - (time.time() - t0)))
        st["done"] = time.time()
        self.save()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


async def gaps_for(a, cams, start, end) -> dict:
    key, kid = grant_issuer.load_key(a.key), grant_issuer.kid_of(a.key)
    out = {}
    async with httpx.AsyncClient(base_url=a.api, verify=False, timeout=60) as c:
        for cam in cams:
            out[cam] = []
            t = start
            while t < end:
                h = {"Authorization": "Bearer " + grant_issuer.sign(key, kid, a.aud, [cam], ["playback"], sub="soak72")}
                r = await c.get(f"/v1/cameras/{cam}/timeline", params={"start": utc(t), "end": utc(min(t + 86400, end))}, headers=h)
                from datetime import datetime
                ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()  # noqa: E731
                out[cam] += [(ts(g["start"]), ts(g["end"])) for g in r.json().get("gaps", [])]
                t += 86400
    return out


def report(a) -> int:
    out = Path(a.out)
    st = json.loads((out / "state.json").read_text())
    start, end = st["start"], st.get("done") or st["last"]
    events, actions, samples = read_jsonl(out / "events.jsonl"), read_jsonl(out / "actions.jsonl"), read_jsonl(out / "samples.jsonl")
    gaps = asyncio.run(gaps_for(a, a.cams.split(","), start, end))
    checks = evaluate(events, actions, samples, gaps, start, end)
    lines = [f"# 72-hour test: {a.label}", "", f"From {utc(start)} to {utc(end)} ({(end - start) / 3600:.1f} h), cameras {a.cams}.", "",
             f"Result: **{sum(c[1] for c in checks)} passed, {sum(not c[1] for c in checks)} failed**", "",
             "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {n} | {'PASS' if ok else 'FAIL'} | {d} |" for n, ok, d in checks]
    lines += ["", "| Planned event | Start | Recording back | Live view and playback back |", "|---|---|---|---|"]
    lines += [f"| {e['kind']} | {utc(e['start'])} | {e.get('recording_back_s')} s | {e.get('features_back_s')} s |" for e in events]
    path = ROOT / "reports" / f"soak72-{a.label}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"report: {path}")
    return 0 if all(c[1] for c in checks) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--aud", required=True)
    ap.add_argument("--api", required=True, help="the API on the gateway's LAN address, e.g. https://192.168.30.200:8443")
    ap.add_argument("--health", default="http://127.0.0.1:9101/health")
    ap.add_argument("--cams", required=True)
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="gateway")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    if not a.report_only:
        asyncio.run(Soak(a).run())
    return report(a)


if __name__ == "__main__":
    sys.exit(main())
