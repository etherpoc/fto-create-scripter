"""
analyze_h4_agree.py — 現行 H1+M15 構成で「H4 トレンド一致が勝率に効くか」を厳密検証。

各エントリー時の H4 トレンド(self._dbg_h4)を記録し、major方向(=side)と
  一致 / 不一致 / 中立(None) に分けて WR・平均R・sumR を P1/P2 別に集計。

baseline = 現ベスト (H1+M15 + room_R<2.0 + 6-10時除外, RR1.5)。

使い方:
    python tools/analyze_h4_agree.py            # robust4
    python tools/analyze_h4_agree.py --all
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks         # noqa: E402

ROBUST4 = ["EURUSD", "USDJPY", "GBPUSD", "EURJPY"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
PARAMS = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15",
              room_R_max=2.0, block_hour_start=6, block_hour_end=10)
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()


def capture(symbol, ticks):
    strat = MtfPullbackStrategy(Params(**PARAMS))
    strat.symbol = symbol
    strat._dbg_h4 = None
    ctx = SimContext()
    last_h1 = last_h4 = -1
    ot = None
    trades = []
    for (m5, h1, h4) in ticks:
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60:
            ctx.bars_seq.pop(0)
        if h1 is not None and h1.time > last_h1:
            ctx.mtf[3600].append(h1); last_h1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        if ot is not None:
            hit = exitp = None
            if ot["side"] == "long":
                if m5.low <= ot["sl"]: hit, exitp = "sl", ot["sl"]
                elif m5.high >= ot["tp"]: hit, exitp = "tp", ot["tp"]
            else:
                if m5.high >= ot["sl"]: hit, exitp = "sl", ot["sl"]
                elif m5.low <= ot["tp"]: hit, exitp = "tp", ot["tp"]
            if hit:
                pnl = (exitp - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - exitp)
                trades.append({"entry_t": ot["entry_t"], "R": pnl / ot["sld"],
                               "side": ot["side"], "h4": ot["h4"]})
                ot = None; ctx._position = None
        ctx.pending_commands = []
        try:
            strat.on_bar(ctx)
        except Exception:
            pass
        if ot is None:
            for cmd in ctx.pending_commands:
                if cmd["type"] in ("buy", "sell"):
                    side = "long" if cmd["type"] == "buy" else "short"
                    entry = cmd.get("entry_price") or m5.close
                    sl, tp = cmd.get("sl"), cmd.get("tp")
                    sld = abs(entry - sl) if sl else 0
                    if not sl or not tp or sld <= 0:
                        continue
                    ot = {"side": side, "entry": entry, "sl": sl, "tp": tp, "sld": sld,
                          "entry_t": int(m5.time), "h4": getattr(strat, "_dbg_h4", None)}
                    ctx._position = side
                    break
    return trades


def h4_status(t):
    """エントリーの major 方向(side)と H4 トレンドの関係。"""
    major = "up" if t["side"] == "long" else "down"
    if t["h4"] is None:
        return "中立(None)"
    return "一致" if t["h4"] == major else "不一致"


def stats(rows):
    n = len(rows)
    if n == 0:
        return (0, 0.0, 0.0, 0.0, 0.0)
    w = sum(1 for r in rows if r["R"] > 0)
    sumR = sum(r["R"] for r in rows)
    p1 = sum(r["R"] for r in rows if r["entry_t"] < P1_END)
    p2 = sum(r["R"] for r in rows if r["entry_t"] >= P1_END)
    return (n, 100 * w / n, sumR / n, p1, p2)


def report(title, trades):
    print(f"\n{'='*72}\n{title}  (全 {len(trades)} トレード)\n{'='*72}")
    # 全体
    n, wr, mr, p1, p2 = stats(trades)
    print(f"  {'区分':<12} {'N':>5} {'WR':>6} {'平均R':>7} {'sumR_P1':>8} {'sumR_P2':>8}")
    print(f"  {'全体':<12} {n:>5} {wr:>5.1f}% {mr:>+7.2f} {p1:>+8.1f} {p2:>+8.1f}")
    print("  " + "-" * 60)
    for key in ["一致", "不一致", "中立(None)"]:
        rows = [t for t in trades if h4_status(t) == key]
        n, wr, mr, p1, p2 = stats(rows)
        print(f"  {key:<12} {n:>5} {wr:>5.1f}% {mr:>+7.2f} {p1:>+8.1f} {p2:>+8.1f}")
    # 一致 vs (不一致のみ) の差を明示
    agr = [t for t in trades if h4_status(t) == "一致"]
    dis = [t for t in trades if h4_status(t) == "不一致"]
    _, wa, ma, _, _ = stats(agr)
    _, wd, md, _, _ = stats(dis)
    print("  " + "-" * 60)
    print(f"  一致 - 不一致 : WR差 {wa-wd:+.1f}pt  平均R差 {ma-md:+.2f}")


def per_pair_robust(per_pair):
    """{pair:[trades]} から base vs 不一致除外 の P1/P2 と robust 数を出す。"""
    print(f"\n{'='*78}\nペア別 robust 検証 (base vs H4不一致除外)  ※robust=P1>0 かつ P2>0\n{'='*78}")
    print(f"  {'pair':<8} | {'base P1/P2':>16} {'rb':>3} | {'不一致除外 P1/P2':>18} {'rb':>3} | 判定")
    print("  " + "-" * 72)
    b_rob = f_rob = 0
    bP1 = bP2 = fP1 = fP2 = 0.0
    for pair, ts in per_pair.items():
        _, _, _, p1, p2 = stats(ts)
        keep = [t for t in ts if h4_status(t) != "不一致"]
        _, _, _, q1, q2 = stats(keep)
        br = (p1 > 0 and p2 > 0); fr = (q1 > 0 and q2 > 0)
        b_rob += br; f_rob += fr
        bP1 += p1; bP2 += p2; fP1 += q1; fP2 += q2
        verdict = "改善" if (q1 > p1 and q2 > p2) else ("P2改善" if q2 > p2 else "悪化/同等")
        print(f"  {pair:<8} | {p1:>+7.1f}/{p2:>+7.1f} {('Y' if br else '-'):>3} | "
              f"{q1:>+8.1f}/{q2:>+8.1f} {('Y' if fr else '-'):>3} | {verdict}")
    print("  " + "-" * 72)
    print(f"  {'合計':<8} | {bP1:>+7.1f}/{bP2:>+7.1f} {b_rob:>3} | {fP1:>+8.1f}/{fP2:>+8.1f} {f_rob:>3} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else ROBUST4
    per_pair = {}
    allt = []
    for p in pairs:
        t = capture(p, load_ticks(p))
        per_pair[p] = t
        allt += t
        print(f"  {p}: {len(t)} trades", flush=True)
    report(f"H4 トレンド一致と勝率の関係 ({'全12' if args.all else 'robust4'})", allt)
    keep = [t for t in allt if h4_status(t) != "不一致"]
    print(f"\n[集計] 『H4 不一致を除外』: {len(allt)}→{len(keep)} トレード")
    report("  H4 不一致を除外後", keep)
    per_pair_robust(per_pair)


if __name__ == "__main__":
    main()
