"""
backtest_net_perpair.py — 全ペアの net(実コスト込み)を個別評価。
「どのペアが勝てるか」を一覧化。block ON + minSL20 + コミ往復$12/lot + spread。

実機はPython netの約半分(データフィード乖離)なので、Python net に十分マージンのある
強ペアだけが実機で生き残る (USDJPY +10.9→+9.8○ / EURUSD +1.6→-3.5✗)。
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

ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
SPREAD = 0.5


def pip(p): return 0.01 if "JPY" in p else (0.1 if "XAU" in p else 0.0001)
def comm(p):
    if p.endswith("JPY"): return 1.8
    if p.startswith("USD"): return 1.5
    if "XAU" in p: return 0.5
    return 1.2


def cap(sym):
    pr = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15", room_R_max=2.0,
              block_hour_start=6, block_hour_end=10, min_sl_dist_pips=20)
    s = MtfPullbackStrategy(Params(**pr)); s.symbol = sym
    ctx = SimContext(); lh1 = lh4 = -1; ot = None; tr = []
    for (m5, h1, h4) in load_ticks(sym):
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
                pn = (ep - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - ep)
                tr.append({"t": ot["t"], "R": pn / ot["sld"], "sp": ot["sld"]/pip(sym), "p": sym})
                ot = None; ctx._position = None
        ctx.pending_commands = []
        try: s.on_bar(ctx)
        except Exception: pass
        if ot is None:
            for c in ctx.pending_commands:
                if c["type"] in ("buy", "sell"):
                    sd = "long" if c["type"] == "buy" else "short"; sl, tp = c.get("sl"), c.get("tp")
                    e = c.get("entry_price") or m5.close
                    if not sl or not tp or abs(e-sl) <= 0: continue
                    ot = {"side": sd, "entry": e, "sl": sl, "tp": tp, "sld": abs(e-sl), "t": int(m5.time)}
                    ctx._position = sd; break
    return tr


def netR(t): return t["R"] - (comm(t["p"]) + SPREAD) / max(t["sp"], 1e-9)


def main():
    print("=" * 80)
    print("全ペア net 評価 (block ON + minSL20 + コミ往復$12 + spread0.5p)  5.5年 + 2024+")
    print("実機 ≈ Python net の約半分。マージン厚いペアだけ実機生存 (USDJPY実証済)。")
    print("=" * 80)
    print(f"{'pair':8} | {'N':>4} {'WR':>5} | {'net全期間':>9} {'年換算':>7} | {'net 2024+':>9} | 実機ヒント")
    print("-" * 80)
    rows = []
    for p in ALL:
        t = cap(p)
        n = len(t); w = sum(1 for x in t if x["R"] > 0)
        nn = sum(netR(x) for x in t)
        n24 = sum(netR(x) for x in t if x["t"] >= P1_END)
        yr = nn / 5.5
        rows.append((nn, p, n, w, n24, yr))
    for nn, p, n, w, n24, yr in sorted(rows, reverse=True):
        hint = "◎強(実機生存濃厚)" if yr >= 1.5 else ("○可(要確認)" if yr >= 0.5 else "✗薄/負(実機で負ける)")
        print(f"{p:8} | {n:>4} {100*w/max(1,n):>4.0f}% | {nn:>+8.1f}% {yr:>+6.1f}% | {n24:>+8.1f}% | {hint}")
    print("-" * 80)
    print("net全期間 = 5.5年累積%(口座1%/trade)。年換算 = ÷5.5。◎=年+1.5%超(実機でも+のはず)。")


if __name__ == "__main__":
    main()
