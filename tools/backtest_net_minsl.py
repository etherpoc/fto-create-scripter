"""
backtest_net_minsl.py — 絶対最小SL(min_sl_dist_pips)を「エントリー時フィルタ」として
正しく適用した net 評価。後付け除外と違い、タイトSLスキップで空いた機会を戦略が拾う分も反映。

net_R = gross_R - cost_pips/sl_dist_pips。block ON, RR1.5。spread +0.5p 固定。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks         # noqa: E402

ROBUST3 = ["EURUSD", "USDJPY", "EURJPY"]
ROBUST4 = ["EURUSD", "USDJPY", "GBPUSD", "EURJPY"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30
SPREAD = 0.5
_ticks = {}


def pip_size(p): return 0.01 if "JPY" in p else (0.1 if "XAU" in p else 0.0001)
def comm_pips(p):
    if p.endswith("JPY"): return 1.8
    if p.startswith("USD"): return 1.5
    if "XAU" in p: return 0.5
    return 1.2


def capture(symbol, min_pips):
    if symbol not in _ticks:
        _ticks[symbol] = load_ticks(symbol)
    pr = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15", room_R_max=2.0,
              block_hour_start=6, block_hour_end=10, min_sl_dist_pips=min_pips)
    strat = MtfPullbackStrategy(Params(**pr)); strat.symbol = symbol
    ctx = SimContext(); lh1 = lh4 = -1; ot = None; tr = []
    for (m5, h1, h4) in _ticks[symbol]:
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60: ctx.bars_seq.pop(0)
        if h1 and h1.time > lh1: ctx.mtf[3600].append(h1); lh1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 and h4.time > lh4: ctx.mtf[14400].append(h4); lh4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        if ot:
            hit = ep = None
            if ot["side"] == "long":
                if m5.low <= ot["sl"]: hit, ep = 1, ot["sl"]
                elif m5.high >= ot["tp"]: hit, ep = 1, ot["tp"]
            else:
                if m5.high >= ot["sl"]: hit, ep = 1, ot["sl"]
                elif m5.low <= ot["tp"]: hit, ep = 1, ot["tp"]
            if hit:
                pnl = (ep - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - ep)
                tr.append({"t": ot["t"], "R": pnl / ot["sld"], "sld_pips": ot["sld"] / pip_size(symbol), "pair": symbol})
                ot = None; ctx._position = None
        ctx.pending_commands = []
        try: strat.on_bar(ctx)
        except Exception: pass
        if ot is None:
            for c in ctx.pending_commands:
                if c["type"] in ("buy", "sell"):
                    side = "long" if c["type"] == "buy" else "short"
                    e = c.get("entry_price") or m5.close; sl, tp = c.get("sl"), c.get("tp")
                    sld = abs(e - sl) if sl else 0
                    if not sl or not tp or sld <= 0: continue
                    ot = {"side": side, "entry": e, "sl": sl, "tp": tp, "sld": sld, "t": int(m5.time)}
                    ctx._position = side; break
    return tr


def net_R(t):
    return t["R"] - (comm_pips(t["pair"]) + SPREAD) / max(t["sld_pips"], 1e-9)


def main():
    print("=" * 92)
    print(f"絶対最小SL(エントリー時フィルタ) × net評価  block ON, RR1.5, spread +{SPREAD}p + コミ往復$12/lot")
    print("採用基準 = 両期間(P1/P2) net プラス。1R=口座1%。")
    print("=" * 92)
    for name, pairs in [("robust3", ROBUST3), ("robust4", ROBUST4), ("全12", ALL)]:
        print(f"\n■ {name}")
        print(f"  {'minSL':>6} | {'N':>5} {'nWR':>5} | {'net P1/月':>10} {'net P2/月':>10} | {'年複利':>7} | 判定")
        for mn in [0, 15, 20, 25]:
            trades = []
            for p in pairs:
                trades += capture(p, mn)
            n1 = sum(net_R(t) for t in trades if t["t"] < P1_END)
            n2 = sum(net_R(t) for t in trades if t["t"] >= P1_END)
            nw = sum(1 for t in trades if net_R(t) > 0)
            m2 = n2 / P2_M
            yr = ((1 + m2/100) ** 12 - 1) * 100 if m2 > -100 else 0
            ok = (n1 > 0 and n2 > 0)
            print(f"  {mn:>4}p | {len(trades):>5} {100*nw/max(1,len(trades)):>4.0f}% | "
                  f"{n1/P1_M:>+9.2f}% {n2/P2_M:>+9.2f}% | {yr:>+6.1f}% | {'✅両期間+' if ok else '—'}", flush=True)


if __name__ == "__main__":
    main()
