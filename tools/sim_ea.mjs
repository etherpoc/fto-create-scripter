// sim_ea.mjs — スタンドアロン EA を「ミニ FTO」で丸ごとオフライン実行する。
//
// EA の default class をそのまま import し、FTO API をモックして OnTick を回す。
// 録音 M5 を 1 本ずつ「確定足」として流し込み、PlaceOrder / ポジション照会 /
// SL/TP 決済をシミュレートする。EA._log を差し替えて diag/entry/outcome を全収集。
//
// iXxx 依存を排除し H1/H4 を M5 集計にしたので、戦略全体が M5 データだけで再現でき、
// 「実機に貼る前にエントリーが本当に出るか」をここで確認できる。
//
// 使い方:  node tools/sim_ea.mjs <recorded_m5.jsonl> <N> [SYMBOL]

import fs from "fs";
import path from "path";
import { pathToFileURL } from "url";

const FILE = process.argv[2];
const N = parseInt(process.argv[3] || "100000", 10);
const SYMBOL = (process.argv[4] || path.basename(path.dirname(FILE)) || "EURUSD").toUpperCase();
if (!FILE) { console.error("usage: node tools/sim_ea.mjs <file.jsonl> <N> [SYMBOL]"); process.exit(2); }

const mod = await import(pathToFileURL(path.resolve("strategies/standalone/mtf_pullback_v2.js")).href);
const Strategy = mod.default;

// --- M5 バーを読む ---
const seen = new Map();
for (const line of fs.readFileSync(FILE, "utf8").split("\n")) {
  if (line.indexOf('"tick"') < 0) continue;
  let r; try { r = JSON.parse(line); } catch (e) { continue; }
  if (r._type !== "tick") continue;
  const m = r.m15; if (!m) continue;
  const t = parseInt(m.time, 10);
  if (seen.has(t)) continue;
  seen.set(t, { time: t, open: m.open, high: m.high, low: m.low, close: m.close, volume: m.volume || 0 });
}
const bars = [...seen.keys()].sort((a, b) => a - b).slice(0, N).map(t => seen.get(t));
console.log(`bars=${bars.length} symbol=${SYMBOL}`);

// --- ミニ FTO 状態 ---
let cur = 0;                 // index 0 (forming) のバー位置
let pos = null;             // {side:'long'|'short', sl, tp, entry, vol}
let balance = 10000.0;
let ticketSeq = 0;
const MAGIC = 220611;

function fdate(sec) { return { valueOf: () => sec * 1000 }; }

const api = {
  Time: (i) => fdate(bars[cur - i].time),
  Open: (i) => bars[cur - i].open,
  High: (i) => bars[cur - i].high,
  Low: (i) => bars[cur - i].low,
  Close: (i) => bars[cur - i].close,
  Volume: (i) => bars[cur - i].volume,
  Symbol: () => SYMBOL,
  GetAccountCurrency: () => "USD",
  GetAccountBalance: () => balance,
  GetActiveOrderCount: () => (pos ? 1 : 0),
  SelectOrder: () => true,
  GetOrderSymbol: () => SYMBOL,
  GetOrderMagicNumber: () => MAGIC,
  GetOrderType: () => (pos && pos.side === "long" ? 0 : 1),  // BUY=0 / SELL=1
  GetOrderTicket: () => 1,
  iClose: (sym) => (sym === "USDJPY" ? 150.0 : null),   // JPYクロスのサイズ計算用
  iTime: () => null, iOpen: () => null, iHigh: () => null, iLow: () => null,
  PlaceOrder: (sym, side, mode, vol, sl, tp) => {
    pos = { side: side === 0 ? "long" : "short", sl, tp, entry: bars[cur - 1].close, vol };
    return ++ticketSeq;
  },
  CloseOrder: () => { pos = null; return true; },
  createTOptValue_number: (d) => ({ value: d }),
  createTOptValue_bool: (d) => ({ value: d }),
  RegOption: () => {}, SetOptionRange: () => {}, SetOptionDigits: () => {}, SetOptionStep: () => {},
  setStrategyShortName: () => {}, setStrategyDescription: () => {},
};

// --- EA インスタンス ---
const s = new Strategy();
s.OnAttach(api);
s.Init();
s.logPort.value = 0;        // ネットワーク送信を無効化
const records = [];
s._log = (rec) => { rec.symbol = SYMBOL; records.push(rec); };
// console.log を黙らせる (entry/exit の大量出力抑制)
const realLog = console.log; console.log = () => {};

// FTO の SL/TP 決済を模す: 確定したバー bars[k] が pos の sl/tp に触れたら閉じる。
function maybeClose(bar) {
  if (!pos) return;
  if (pos.side === "long") {
    if (bar.low <= pos.sl) { balance += (pos.sl - pos.entry) * pos.vol; pos = null; }
    else if (bar.high >= pos.tp) { balance += (pos.tp - pos.entry) * pos.vol; pos = null; }
  } else {
    if (bar.high >= pos.sl) { balance += (pos.entry - pos.sl) * pos.vol; pos = null; }
    else if (bar.low <= pos.tp) { balance += (pos.entry - pos.tp) * pos.vol; pos = null; }
  }
}

// --- 駆動: cur=k で forming=bars[k]、OnTick が確定足 bars[k-1] を処理 ---
for (let k = 1; k < bars.length; k++) {
  cur = k;
  maybeClose(bars[k - 1]);   // 直近確定足で SL/TP 決済 (FTO 相当、EA が照会する前に)
  s.OnTick();
}
console.log = realLog;

// --- 集計 ---
const entries = records.filter(r => r.type === "entry");
const outcomes = records.filter(r => r.type === "outcome");
const skips = records.filter(r => r.type === "skip_size");
const diags = records.filter(r => r.type === "diag");
const lastDiag = diags[diags.length - 1];

console.log(`\n=== RESULT ${SYMBOL} ===`);
console.log(`entries=${entries.length}  outcomes=${outcomes.length}  skip_size=${skips.length}  diag_beats=${diags.length}`);
if (lastDiag) {
  console.log(`last diag @bar ${lastDiag.bar_idx}:`);
  console.log(`  trends=${JSON.stringify(lastDiag.trends)}`);
  console.log(`  pivots=${JSON.stringify(lastDiag.pivots)}  mtf_bars=${JSON.stringify(lastDiag.mtf_bars)}`);
  console.log(`  funnel=${JSON.stringify(lastDiag.funnel)}`);
}
if (entries.length > 0) {
  console.log(`\nfirst 3 entries:`);
  for (const e of entries.slice(0, 3)) {
    console.log(`  ${e.side} price=${(+e.entry_price).toFixed(5)} sl=${(+e.sl).toFixed(5)} tp=${(+e.tp).toFixed(5)} ` +
      `lot=${(+e.lot).toFixed(2)} risk$=${(+e.risk_amount).toFixed(2)} path=${e.conv_path}`);
  }
  // risk% チェック (entry 時残高は概算 10000 起点なので参考値)
  const r = entries.map(e => +e.risk_amount);
  console.log(`risk$ range: min=${Math.min(...r).toFixed(2)} max=${Math.max(...r).toFixed(2)} (口座1%=~100付近が正常)`);
  console.log(`conv_path set: ${JSON.stringify([...new Set(entries.map(e => e.conv_path))])}`);
}
console.log(entries.length > 0 ? "\nOK: エントリー発生" : "\nWARN: エントリー 0 件");
