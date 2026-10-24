#!/usr/bin/env node
// M5 live view, checked in a real browser: opens go2rtc's player page in Chrome (headless),
// lets it play for a while, and records whether the video really plays (resolution, playback
// time moving forward, frames decoded) plus a screenshot. Runs on an operator workstation that
// can reach the gateway (LAN or VPN), not on the gateway.
//
// The go2rtc login comes from the environment and is answered to the browser's auth challenge
// through the DevTools protocol; it is never printed or written to the report.
//
// usage: GO2RTC_USER=vivo GO2RTC_PASSWORD=... node tests/m5_browser.mjs \
//          [--gateway 192.168.30.200] [--streams cam01_main,cam01_sub] [--modes mse,webrtc] [--seconds 12]
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) =>
  a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc, []));
const gateway = args.gateway || "192.168.30.200";
const streams = (args.streams || "cam01_main").split(",");
const modes = (args.modes || "mse").split(",");
const seconds = Number(args.seconds || 12);
const user = process.env.GO2RTC_USER || "vivo";
const password = process.env.GO2RTC_PASSWORD;
if (!password) { console.error("set GO2RTC_PASSWORD"); process.exit(2); }

const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9300 + Math.floor(Math.random() * 500);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
const outDir = join(root, "reports", `m5-browser-${stamp}`);
mkdirSync(outDir, { recursive: true });

const profile = mkdtempSync(join(tmpdir(), "m5-chrome-"));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--autoplay-policy=no-user-gesture-required", "--window-size=1280,760", "--no-first-run", "about:blank"], { stdio: "ignore" });

async function cdpPage() {
  for (let i = 0; i < 50; i++) {
    try {
      const t = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find((x) => x.type === "page");
      if (t) return t.webSocketDebuggerUrl;
    } catch { /* Chrome still starting */ }
    await sleep(200);
  }
  throw new Error("Chrome did not start");
}

const ws = new WebSocket(await cdpPage());
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let nextId = 1;
const pending = new Map();
ws.addEventListener("message", ({ data }) => {
  const m = JSON.parse(data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Fetch.authRequired") {
    send("Fetch.continueWithAuth", { requestId: m.params.requestId,
      authChallengeResponse: { response: "ProvideCredentials", username: user, password } });
  } else if (m.method === "Fetch.requestPaused") {
    send("Fetch.continueRequest", { requestId: m.params.requestId });
  }
});
function send(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((r) => pending.set(id, r));
}
const evaluate = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result?.result?.value;

await send("Fetch.enable", { handleAuthRequests: true, patterns: [{ urlPattern: "*" }] });
await send("Page.enable");

const probe = `(() => {
  const v = document.querySelector("video");
  const q = v && v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : {};
  return {
    hevc_mse: !!(window.MediaSource && (MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L153.B0"') ||
                                        MediaSource.isTypeSupported('video/mp4; codecs="hev1.1.6.L153.B0"'))),
    h264_mse: !!(window.MediaSource && MediaSource.isTypeSupported('video/mp4; codecs="avc1.640029"')),
    found: !!v, width: v ? v.videoWidth : 0, height: v ? v.videoHeight : 0,
    time: v ? Number(v.currentTime.toFixed(2)) : 0, paused: v ? v.paused : null, ready: v ? v.readyState : 0,
    frames: q.totalVideoFrames || 0, dropped: q.droppedVideoFrames || 0,
    status: (document.querySelector("video-stream, video-rtc")?.innerText || "").trim().slice(0, 80),
  };
})()`;

const rows = [];
for (const stream of streams) {
  for (const mode of modes) {
    const url = `http://${gateway}:1984/stream.html?src=${encodeURIComponent(stream)}&mode=${mode}`;
    await send("Page.navigate", { url });
    await sleep(4000);
    const a = await evaluate(probe);
    await sleep(seconds * 1000);
    const b = await evaluate(probe);
    const shot = (await send("Page.captureScreenshot", { format: "png" })).result?.data;
    const png = `${stream}-${mode}.png`;
    if (shot) writeFileSync(join(outDir, png), Buffer.from(shot, "base64"));
    const advanced = b && a ? b.time - a.time : 0;
    const ok = !!b && b.width > 0 && advanced > seconds * 0.6 && b.frames > a.frames;
    rows.push({ stream, mode, ok, advanced: Number(advanced.toFixed(1)), ...b, png });
    console.log(`${ok ? "PASS" : "FAIL"}  ${stream} ${mode}: ${b?.width}x${b?.height}, playback +${advanced.toFixed(1)} s in ${seconds} s, ` +
      `frames ${b?.frames} (dropped ${b?.dropped}), HEVC in MSE: ${b?.hevc_mse}`);
  }
}

const md = [`# M5 live view in a browser`, "",
  `Run ${new Date().toISOString()} from an operator workstation (headless Chrome) against ${gateway}:1984.`,
  `Each stream played ${seconds} s after a 4 s start. PASS = video has a size, playback time moved forward by at least 60% of the wait, and new frames were decoded.`, "",
  "| Result | Stream | Mode | Video | Playback moved | Frames (dropped) | Browser HEVC (MSE) | Screenshot |",
  "|---|---|---|---|---|---|---|---|",
  ...rows.map((r) => `| ${r.ok ? "PASS" : "**FAIL**"} | ${r.stream} | ${r.mode} | ${r.width}x${r.height} | ${r.advanced} s | ${r.frames} (${r.dropped}) | ${r.hevc_mse} | ${r.png} |`),
  ""].join("\n");
writeFileSync(join(outDir, "report.md"), md);
console.log(`report: ${join(outDir, "report.md")}`);

await send("Browser.close").catch(() => {});
chrome.kill();
await new Promise((r) => (chrome.exitCode !== null ? r() : chrome.once("exit", r)));
try { rmSync(profile, { recursive: true, force: true }); } catch { /* Chrome may still flush its profile */ }
process.exit(rows.every((r) => r.ok) ? 0 : 1);
