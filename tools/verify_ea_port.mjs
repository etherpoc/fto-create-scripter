// verify_ea_port.mjs — スタンドアロン EA の純粋ロジック (ZigZag/Dow/集計) が
// Python 実装と完全一致するか実データで検証する。
//
// EA ファイルから export default class より前 (= 純関数群 + SDK インライン) を
// 切り出して named export を足し、Node に動的 import する。EA 本体 (this.api 依存) は
// 読み込まないので Node でも動く。同じ M5 スライスを JS と Python (tools/_ea_port_ref.py)
// に流して、M5/M15 ピボットと M5 トレンド系列を突き合わせる。
//
// 使い方:  node tools/verify_ea_port.mjs <recorded_m5.jsonl> <N>

import fs from "fs";
import path from "path";
import os from "os";
import { execFileSync } from "child_process";
import { pathToFileURL } from "url";

const FILE = process.argv[2];
const N = parseInt(process.argv[3] || "30000", 10);
if (!FILE) { console.error("usage: node tools/verify_ea_port.mjs <file.jsonl> <N>"); process.exit(2); }

// --- EA から純関数を抽出して import ---
const eaPath = path.resolve("strategies/standalone/mtf_pullback_v2.js");
let txt = fs.readFileSync(eaPath, "utf8");
txt = txt.split("export default class MtfPullbackV2")[0] +
  "\nexport { ZigZag, dowTrend, opposite, trendlineDist };\n";
const tmp = path.join(os.tmpdir(), "ea_pure_" + Date.now() + ".mjs");
fs.writeFileSync(tmp, txt);
const mod = await import(pathToFileURL(tmp).href);
const { ZigZag, dowTrend } = mod;

// --- M5 バーを読む (Python ref と同じ dedup/slice) ---
const seen = new Map();
for (const line of fs.readFileSync(FILE, "utf8").split("\n")) {
  if (line.indexOf('"tick"') < 0) continue;
  let r; try { r = JSON.parse(line); } catch (e) { continue; }
  if (r._type !== "tick") continue;
  const m = r.m15; if (!m) continue;
  const t = parseInt(m.time, 10);
  if (seen.has(t)) continue;
  seen.set(t, m);
}
const times = [...seen.keys()].sort((a, b) => a - b).slice(0, N);
const bars = times.map(t => seen.get(t));

// --- JS 側で同じパイプライン ---
const dev = 3.0 * 0.0001;
const zz5 = new ZigZag(5, dev);
const zz15 = new ZigZag(8, dev);
let lastM15 = -1;
let m5buf = [];
const m5trend = [];
function agg(c) {
  let hi = c[0].high, lo = c[0].low;
  for (const b of c) { if (b.high > hi) hi = b.high; if (b.low < lo) lo = b.low; }
  return { time: c[0].time, open: c[0].open, high: hi, low: lo, close: c[c.length - 1].close, volume: 0 };
}
for (const m of bars) {
  const bar = { time: parseInt(m.time, 10), open: m.open, high: m.high, low: m.low, close: m.close, volume: m.volume || 0 };
  m5buf.push(bar);
  if (m5buf.length > 30) m5buf.shift();
  zz5.update(bar);
  if ((bar.time + 300) % 900 === 0 && m5buf.length >= 3) {
    const c = m5buf.slice(-3);
    const m15 = agg(c);
    if (m15.time > lastM15) { zz15.update(m15); lastM15 = m15.time; }
  }
  m5trend.push(dowTrend(zz5.pivots));
}
function tail(pivs, k = 30) {
  return pivs.slice(-k).map(p => ({ index: p.index, kind: p.kind, price: Math.round(p.price * 1e6) / 1e6 }));
}
const jsOut = { n: bars.length, zz5: tail(zz5.pivots), zz15: tail(zz15.pivots), m5trend_tail: m5trend.slice(-50) };

// --- Python ref ---
const pyRaw = execFileSync("python", ["tools/_ea_port_ref.py", FILE, String(N)], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
const pyOut = JSON.parse(pyRaw.trim().split("\n").pop());

// --- 比較 ---
let fails = 0;
function cmpPivots(name, a, b) {
  if (a.length !== b.length) { console.log(`  MISMATCH ${name}: len js=${a.length} py=${b.length}`); fails++; return; }
  for (let i = 0; i < a.length; i++) {
    if (a[i].index !== b[i].index || a[i].kind !== b[i].kind || Math.abs(a[i].price - b[i].price) > 1e-6) {
      console.log(`  MISMATCH ${name}[${i}]: js=${JSON.stringify(a[i])} py=${JSON.stringify(b[i])}`); fails++;
      if (fails > 10) return;
    }
  }
}
function cmpSeq(name, a, b) {
  if (a.length !== b.length) { console.log(`  MISMATCH ${name}: len js=${a.length} py=${b.length}`); fails++; return; }
  for (let i = 0; i < a.length; i++) {
    const av = a[i] === null ? null : a[i], bv = b[i] === null ? null : b[i];
    if (av !== bv) { console.log(`  MISMATCH ${name}[${i}]: js=${av} py=${bv}`); fails++; if (fails > 10) return; }
  }
}
console.log(`bars: js=${jsOut.n} py=${pyOut.n}`);
cmpPivots("zz5", jsOut.zz5, pyOut.zz5);
cmpPivots("zz15", jsOut.zz15, pyOut.zz15);
cmpSeq("m5trend_tail", jsOut.m5trend_tail, pyOut.m5trend_tail);

try { fs.unlinkSync(tmp); } catch (e) {}
if (fails === 0) { console.log("OK: JS == Python (zz5/zz15 pivots + m5 trend sequence match exactly)"); process.exit(0); }
console.log(`FAIL: ${fails} mismatch(es)`); process.exit(1);
