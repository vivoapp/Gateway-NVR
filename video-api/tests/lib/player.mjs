// Shared by the M15 and M16 measurements: serves web/player.html on 127.0.0.1 (a local origin, so
// Chrome lets it call local addresses), opens it in headless Chrome and evaluates code in it.
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function args(defaults) {
  const out = { ...defaults };
  for (const k of Object.keys(defaults)) {
    const i = process.argv.indexOf(`--${k}`);
    if (i > 0) out[k] = typeof defaults[k] === "boolean" ? true : process.argv[i + 1];
  }
  return out;
}

export async function openPlayer(params) {
  const page = readFileSync(join(ROOT, "web", "player.html"));
  const server = createServer((_, res) => { res.writeHead(200, { "Content-Type": "text/html" }); res.end(page); });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const port = 9600 + Math.floor(Math.random() * 300);
  const profile = mkdtempSync(join(tmpdir(), "vivo-chrome-"));
  const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    "--ignore-certificate-errors", "--autoplay-policy=no-user-gesture-required", "--no-first-run",
    // VIVO_TEST_NO_CORS=1: against a gateway whose CORS_ORIGINS is empty (no browser origin allowed), this
    // throwaway test browser skips the CORS check instead of the gateway being opened to the test page.
    ...(process.env.VIVO_TEST_NO_CORS === "1" ? ["--disable-web-security"] : []), "about:blank"],
  { stdio: "ignore" });
  let wsUrl;
  for (let i = 0; i < 100 && !wsUrl; i++) {
    try { wsUrl = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((x) => x.type === "page")?.webSocketDebuggerUrl; } catch {}
    await sleep(200);
  }
  const ws = new WebSocket(wsUrl);
  await new Promise((r) => ws.addEventListener("open", r, { once: true }));
  let id = 1; const pend = new Map();
  ws.addEventListener("message", ({ data }) => { const m = JSON.parse(data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } });
  const send = (method, p = {}) => new Promise((r) => { const i = id++; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params: p })); });
  const ev = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result?.result?.value;
  await send("Page.enable");
  await send("Page.navigate", { url: `http://127.0.0.1:${server.address().port}/player.html?${new URLSearchParams(params)}` });
  for (let i = 0; i < 100 && !(await ev("typeof window.vivo === 'object'")); i++) await sleep(200);   // hls.js loads first
  const close = async () => {
    await send("Browser.close").catch(() => {});
    chrome.kill(); server.close();
    // wait for Chrome to exit, or it writes into the profile again after it is removed
    if (chrome.exitCode === null) await new Promise((r) => { chrome.once("exit", r); setTimeout(r, 5000); });
    try { rmSync(profile, { recursive: true, force: true }); } catch {}
  };
  return { ev, close };
}

export const median = (a) => {
  const s = [...a].sort((x, y) => x - y);
  if (!s.length) return null;
  return s.length % 2 ? s[s.length >> 1] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
};

export function writeReport(name, lines) {
  mkdirSync(join(ROOT, "reports"), { recursive: true });
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
  const out = join(ROOT, "reports", `${name}-${stamp}.md`);
  writeFileSync(out, lines.join("\n") + "\n");
  console.log(`report: ${out}`);
  return out;
}
