// M16 playback measurement in headless Chrome with hls.js (the test player):
//   - first picture: --starts playbacks of the last --minutes (default 24 h, or what is recorded), median
//   - --seeks random seeks across the range: time from the seek to the next frame shown
// Writes reports/m16-playback-<label>-<UTC>.md.
//
// usage: node tests/m16_playback.mjs [--api https://127.0.0.1:8443] [--issuer http://127.0.0.1:8099]
//          [--cam cam01] [--minutes 1440] [--starts 5] [--seeks 20] [--label lab]
import { args, median, openPlayer, writeReport } from "./lib/player.mjs";

const o = args({ api: "https://127.0.0.1:8443", issuer: "http://127.0.0.1:8099", cam: "cam01", minutes: "1440",
  starts: "5", seeks: "20", label: "lab" });
const p = await openPlayer({ api: o.api, issuer: o.issuer, cam: o.cam });
const r = await p.ev(`window.vivo.playbackTest(${Number(o.minutes)}, ${Number(o.seeks)}, ${Number(o.starts)})`);
await p.close();
if (!r || r.error) { console.log(`playback failed: ${r?.error}`); process.exit(1); }
const firstMs = r.firsts.map((f) => f.ms).filter((x) => x !== null);
const medFirst = median(firstMs);
console.log(`first picture: median ${medFirst} ms of ${r.firsts.length} (${firstMs.join(", ")} ms)`);
console.log(`range ${r.start} to ${r.end} (${Math.round(r.duration_s / 60)} min in the playlist)`);
const okSeeks = r.seeks.filter((s) => s.ms !== null);
const worst = okSeeks.length ? Math.max(...okSeeks.map((s) => s.ms)) : null;
console.log(`seeks: median ${median(okSeeks.map((s) => s.ms))} ms, slowest ${worst} ms, ${okSeeks.length} of ${r.seeks.length}`);
const firstPass = firstMs.length === r.firsts.length && medFirst !== null && medFirst <= 3000;
const seekPass = okSeeks.length === r.seeks.length && worst !== null && worst <= 2000;
writeReport(`m16-playback-${o.label}`, [
  `# M16 playback and seeking: ${o.label}`, "",
  `Run ${new Date().toISOString().slice(0, 19)}Z. API ${o.api}, camera ${o.cam}. Playback of ${r.start} to ${r.end}`,
  `(${Math.round(r.duration_s / 60)} minutes in the playlist; gaps are skipped). hls.js in headless Chrome.`,
  "First picture is measured from the playback request to the first frame shown.", "",
  `Result: **${(firstPass ? 1 : 0) + (seekPass ? 1 : 0)} passed, ${(firstPass ? 0 : 1) + (seekPass ? 0 : 1)} failed**`, "",
  "| Check | Result | Target | |", "|---|---|---|---|",
  `| First picture, median of ${r.firsts.length} starts | ${medFirst} ms (slowest ${Math.max(...firstMs)} ms) | 3 s or less | ${firstPass ? "PASS" : "FAIL"} |`,
  `| ${r.seeks.length} random seeks | median ${median(okSeeks.map((s) => s.ms))} ms, slowest ${worst} ms | each 2 s or less | ${seekPass ? "PASS" : "FAIL"} |`,
  "", "| Start | First picture | Request | Playlist | First piece | Buffered |", "|---|---|---|---|---|---|",
  ...r.firsts.map((f, i) => `| ${i + 1} | ${f.ms} ms | ${f.request_ms} ms | ${f.playlist_ms} ms | ${f.first_piece_ms} ms | ${f.buffered_ms} ms |`),
  "", "| Seek to (s into the playlist) | Next frame after |", "|---|---|",
  ...r.seeks.map((s) => `| ${s.at_s.toFixed(0)} | ${s.ms === null ? "none" : s.ms + " ms"} |`),
]);
process.exit(firstPass && seekPass ? 0 : 1);
