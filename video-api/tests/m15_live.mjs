// M15 live-view measurement: drives the test player (web/player.html) in headless Chrome.
//   - first frame: N attempts, median, and the ICE path each attempt took
//   - revocation: a renewal with a grant that no longer covers the camera must cut the video
// Writes reports/m15-live-<label>[-turn]-<UTC>.md.
//
// usage: node tests/m15_live.mjs [--api https://127.0.0.1:8443] [--issuer http://127.0.0.1:8099]
//          [--cam cam01] [--stream sub] [--n 10] [--relay] [--label lab] [--target 2]
//          [--go2rtc http://127.0.0.1:1984]
// GO2RTC_AUTH=user:password lets it also check that go2rtc dropped the cut viewer.
import { args, median, openPlayer, sleep, writeReport } from "./lib/player.mjs";

const o = args({ api: "https://127.0.0.1:8443", issuer: "http://127.0.0.1:8099", cam: "cam01", stream: "sub", n: "10",
  relay: false, label: "lab", target: "", go2rtc: "http://127.0.0.1:1984" });
const target = Number(o.target || (o.relay ? 5 : 2)) * 1000;     // 2 s local, 3 s remote direct, 5 s TURN
const p = await openPlayer({ api: o.api, issuer: o.issuer, cam: o.cam, stream: o.stream, n: o.n, relay: o.relay ? "1" : "0" });

console.log(`first frame: ${o.n} attempts, ${o.cam} ${o.stream}${o.relay ? ", forced TURN" : ""}`);
await p.ev("window.vivo.runAttempts()");
const results = await p.ev("window.results");
for (const r of results) console.log(`  ${r.attempt}: ${r.first_frame_ms === null ? "no video" : (r.first_frame_ms / 1000).toFixed(2) + " s"}  ${r.path} ${r.note}`);

console.log("revocation test");
await p.ev("window.vivo.revocationTest()");
const rev = await p.ev("window.revoke");
console.log(`  ${JSON.stringify(rev)}`);
let consumerGone = null;
if (process.env.GO2RTC_AUTH && rev?.session_id) {
  const auth = "Basic " + Buffer.from(process.env.GO2RTC_AUTH).toString("base64");
  const t0 = Date.now();
  while (Date.now() - t0 < 60000) {
    const info = await (await fetch(`${o.go2rtc}/api/streams?src=${o.cam}_${o.stream}`, { headers: { Authorization: auth } })).json();
    if (!(info.consumers || []).some((c) => String(c.user_agent || "").includes(rev.session_id))) { consumerGone = Math.round((Date.now() - t0) / 1000); break; }
    await sleep(1000);
  }
  console.log(`  go2rtc dropped the viewer ${consumerGone === null ? "NOT within 60 s" : `within ${consumerGone} s of the check`}`);
}
await p.close();

const ok = results.filter((r) => r.first_frame_ms !== null).map((r) => r.first_frame_ms);
const med = median(ok);
const pass = med !== null && med <= target && ok.length === results.length;
const revPass = rev && rev.renew_status === 403 && rev.video_stopped && rev.stopped_after_ms !== null && rev.stopped_after_ms <= 2000;
const fmt = (ms) => (ms === null ? "none" : (ms / 1000).toFixed(2) + " s");
writeReport(`m15-live-${o.label}${o.relay ? "-turn" : ""}`, [
  `# M15 live view: ${o.label}${o.relay ? ", forced TURN" : ""}`, "",
  `Run ${new Date().toISOString().slice(0, 19)}Z. API ${o.api}, camera ${o.cam}, stream ${o.stream}, ${o.n} attempts.`,
  "First frame is measured from the live-session request to the first decoded video frame in the browser.", "",
  `Result: **${(pass ? 1 : 0) + (revPass ? 1 : 0)} passed, ${(pass ? 0 : 1) + (revPass ? 0 : 1)} failed**`, "",
  "| Check | Result | Target | |", "|---|---|---|---|",
  `| First frame, median of ${results.length} | ${fmt(med)} (${ok.length} of ${results.length} played) | ${target / 1000} s or less | ${pass ? "PASS" : "FAIL"} |`,
  `| Revocation: refused renewal cuts the video | renewal ${rev?.renew_status}, video stopped ${rev?.stopped_after_ms ?? "?"} ms after it${consumerGone !== null ? `, go2rtc dropped the viewer within ${consumerGone} s` : ""} | refused, video stops within 2 s | ${revPass ? "PASS" : "FAIL"} |`,
  "", "| Attempt | First frame | ICE path (local -> remote) |", "|---|---|---|",
  ...results.map((r) => `| ${r.attempt} | ${fmt(r.first_frame_ms)} | ${r.path} ${r.note} |`),
]);
process.exit(pass && revPass ? 0 : 1);
