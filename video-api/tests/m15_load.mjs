// M15 load acceptance: 10 cameras recording plus 10 live viewers (5 on one camera) for --minutes.
// Pass when: every viewer plays (at least 10 fps on the 15 fps substream), each go2rtc stream keeps
// exactly one camera connection, no recording gap over 3 s on the watched cameras, CPU peak under
// 85%, and a 6th viewer of the busy camera is refused with 429 camera_viewers.
// Writes reports/m15-load-<label>-<UTC>.md.
//
// usage: GO2RTC_AUTH=user:password node tests/m15_load.mjs --cams cam01,cam02,...,cam06 [--busy cam01]
//          [--minutes 10] [--api https://192.168.30.200:8443] [--go2rtc http://192.168.30.200:1984]
//          [--health http://192.168.30.200:9101/health] [--issuer http://127.0.0.1:8099] [--label gateway]
// The plan: 5 viewers on --busy, one on each other camera in --cams, 10 in all.
import { args, median, openPlayer, sleep, writeReport } from "./lib/player.mjs";

const o = args({ api: "https://127.0.0.1:8443", issuer: "http://127.0.0.1:8099", go2rtc: "http://127.0.0.1:1984",
  health: "http://127.0.0.1:9101/health", cams: "cam01,cam02,cam03,cam04,cam05,cam06", busy: "cam01",
  minutes: "10", label: "lab" });
const cams = o.cams.split(",");
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";     // the API's self-signed certificate (until Haris's M23), for the timeline query
const plan = Object.fromEntries(cams.map((c) => [c, c === o.busy ? 5 : 1]));
const auth = process.env.GO2RTC_AUTH ? { Authorization: "Basic " + Buffer.from(process.env.GO2RTC_AUTH).toString("base64") } : {};
const seconds = Number(o.minutes) * 60;
const started = new Date();

// samples every 10 s while the viewers watch: camera connections per stream, CPU from the health endpoint
const samples = [];
let sampling = true;
const sampler = (async () => {
  while (sampling) {
    const s = { t: Date.now(), producers: {}, cpu: null };
    try {
      const streams = await (await fetch(`${o.go2rtc}/api/streams`, { headers: auth })).json();
      for (const c of cams) for (const role of ["main", "sub"]) s.producers[`${c}_${role}`] = (streams[`${c}_${role}`]?.producers || []).length;
    } catch {}
    try { s.cpu = (await (await fetch(o.health)).json())?.gateway?.cpu_percent ?? null; } catch {}
    samples.push(s);
    await sleep(10000);
  }
})();

const p = await openPlayer({ api: o.api, issuer: o.issuer, cam: o.busy });
const viewers = await p.ev(`window.vivo.loadTest(${JSON.stringify(plan)}, ${seconds})`);
// a 6th viewer of the busy camera must be refused while the five are watching: ask again with them open
const extra = await p.ev(`(async () => { const v = await window.vivo.loadTest({"${o.busy}": 6}, 5); return v[5]; })()`);
sampling = false;
await sampler;
await p.close();

// recording gaps on the watched cameras during the run (the timeline route, 3 s rule)
const iso = (d) => new Date(d).toISOString().replace(/\.\d+Z$/, "Z");
const gaps = {};
for (const c of cams) {
  try {
    const g = await (await fetch(`${o.issuer}/grant?cams=${c}&perms=playback`)).text();
    const t = await (await fetch(`${o.api}/v1/cameras/${c}/timeline?start=${iso(started)}&end=${iso(Date.now() - 15000)}`,
      { headers: { Authorization: `Bearer ${g.trim()}` } })).json();
    gaps[c] = t.gaps || [];
  } catch (e) { gaps[c] = [{ error: String(e) }]; }
}

const playing = viewers.filter((v) => v.ok && v.fps >= 10);
const oneConn = samples.length && samples.every((s) => Object.values(s.producers).every((n) => n === 1));
const cpus = samples.map((s) => s.cpu).filter((x) => x !== null);
const cpuPeak = cpus.length ? Math.max(...cpus) : null;
const anyGap = Object.values(gaps).some((g) => g.length);
const refused = extra && extra.status === 429 && extra.limit === "camera_viewers";
const checks = [
  ["10 viewers play (at least 10 fps)", playing.length === 10, `${playing.length} of ${viewers.length}; fps median ${median(viewers.map((v) => v.fps))}`],
  ["one camera connection per stream", oneConn, `${samples.length} samples of ${Object.keys(samples[0]?.producers || {}).length} streams`],
  ["no recording gap on the watched cameras", !anyGap, anyGap ? JSON.stringify(gaps) : "none"],
  ["CPU peak under 85%", cpuPeak !== null && cpuPeak < 85, cpuPeak === null ? "no CPU samples (health endpoint not reached)" : `${cpuPeak}%`],
  ["a 6th viewer of the busy camera is refused", refused, extra ? `HTTP ${extra.status} ${extra.limit || extra.error || ""}` : "no answer"],
];
for (const [n, ok, d] of checks) console.log(`${ok ? "PASS" : "FAIL"}  ${n}  ${d}`);
writeReport(`m15-load-${o.label}`, [
  `# M15 load: ${o.label}`, "",
  `Run ${started.toISOString().slice(0, 19)}Z for ${o.minutes} minutes. API ${o.api}. Viewers: ${JSON.stringify(plan)} (substream).`, "",
  `Result: **${checks.filter((c) => c[1]).length} passed, ${checks.filter((c) => !c[1]).length} failed**`, "",
  "| Check | Result | Detail |", "|---|---|---|",
  ...checks.map(([n, ok, d]) => `| ${n} | ${ok ? "PASS" : "FAIL"} | ${d} |`), "",
  "| Viewer | Camera | First frame | fps |", "|---|---|---|---|",
  ...viewers.map((v, i) => `| ${i + 1} | ${v.cam} | ${v.first_ms ?? "none"} ms | ${v.fps} |`),
]);
process.exit(checks.every((c) => c[1]) ? 0 : 1);
