"""
breakout_deepdive.py — 金(XAUUSD)+USDJPY ブレイクアウトの深掘り。

backtest_breakout の Donchian ロジックを使い、TF(H4/D1)×パラメータをグリッド探索。
頑健性(P1もP2もnet+)とDD・年複利を実測。金ブル依存度(P1の弱さ)も可視化。

net = 往復コミ + spread。1R=口座1%。WF: P1=2021-23 / P2=2024-26。

使い方:
    python tools/breakout_deepdive.py            # XAUUSD グリッド探索 → top表示
    python tools/breakout_deepdive.py --sym USDJPY
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.backtest_breakout import run_breakout, pip, comm, load_ticks  # noqa: E402

P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30
SPREAD = 0.5


class Bar:
    __slots__ = ("time", "open", "high", "low", "close")
    def __init__(s, t, o, h, l, c): s.time=t; s.open=o; s.high=h; s.low=l; s.close=c


def extract(sym, tf):
    """h4/d1 のバー列。d1 は h4 を UTC 日付で集約。"""
    idx = 2  # h4
    seen = set(); h4 = []
    for tup in load_ticks(sym):
        b = tup[idx]
        if b is None or b.time in seen: continue
        seen.add(b.time); h4.append(b)
    h4.sort(key=lambda x: x.time)
    if tf == "h4":
        return [Bar(b.time, b.open, b.high, b.low, b.close) for b in h4]
    # d1: UTC日付で集約
    days = {}
    order = []
    for b in h4:
        d = datetime.fromtimestamp(b.time, tz=timezone.utc).strftime("%Y-%m-%d")
        if d not in days:
            days[d] = [b.time, b.open, b.high, b.low, b.close]; order.append(d)
        else:
            r = days[d]
            r[2] = max(r[2], b.high); r[3] = min(r[3], b.low); r[4] = b.close
    return [Bar(*days[d]) for d in order]


def equity_dd(trades):
    ev = sorted(trades, key=lambda x: x["t"])
    eq = peak = dd = 0.0
    for t in ev:
        eq += t["nR"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def evalconf(bars, sym, P):
    tr = run_breakout(bars, P)
    ps = pip(sym); cost = comm(sym) + SPREAD
    for t in tr:
        t["nR"] = t["R"] - cost / max(t["sl_dist"] / ps, 1e-9)
    n = len(tr)
    if n == 0: return None
    n1 = sum(t["nR"] for t in tr if t["t"] < P1_END)
    n2 = sum(t["nR"] for t in tr if t["t"] >= P1_END)
    w = sum(1 for t in tr if t["nR"] > 0)
    eq, dd = equity_dd(tr)
    return dict(n=n, wr=100*w/n, p1=n1, p2=n2, m1=n1/P1_M, m2=n2/P2_M, eq=eq, dd=dd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="XAUUSD")
    args = ap.parse_args()
    sym = args.sym

    grid = []
    for tf in ["h4", "d1"]:
        for en in ([20, 30, 40, 55] if tf == "h4" else [10, 20, 30]):
            for ex in ([10, 20] if tf == "h4" else [5, 10]):
                for sl in [2.0, 3.0]:
                    for sm in [0, 50, 100]:
                        grid.append((tf, dict(entry_n=en, exit_n=ex, atr_n=20, sl_atr=sl, sma_n=sm)))

    barcache = {tf: extract(sym, tf) for tf in ["h4", "d1"]}
    print(f"  {sym}: H4={len(barcache['h4'])}本 / D1={len(barcache['d1'])}本", flush=True)

    res = []
    for tf, P in grid:
        r = evalconf(barcache[tf], sym, P)
        if r is None: continue
        r.update(tf=tf, **P)
        res.append(r)

    # 頑健(両期間net+)を、min(月利)で評価しつつ DD効率も見る
    robust = [r for r in res if r["p1"] > 0 and r["p2"] > 0]
    robust.sort(key=lambda r: min(r["m1"], r["m2"]), reverse=True)

    print("\n" + "=" * 104)
    print(f"{sym} ブレイクアウト グリッド探索 (net, WF)  ★頑健=P1もP2もnet+  {len(robust)}/{len(res)}構成が頑健")
    print("=" * 104)
    print(f"  {'TF':>3} {'entry':>5} {'exit':>4} {'SL':>3} {'SMA':>4} | {'N':>4} {'WR':>4} | "
          f"{'P1/月':>7} {'P2/月':>7} | {'累積R':>7} {'DD':>6} | {'R/DD':>5} | {'年複利(P2)':>9}")
    print("-" * 104)
    for r in robust[:12]:
        m2 = r["m2"]; yr = ((1+m2/100)**12 - 1)*100
        rdd = r["eq"]/r["dd"] if r["dd"] > 0 else 0
        print(f"  {r['tf']:>3} {r['entry_n']:>5} {r['exit_n']:>4} {r['sl_atr']:>3.0f} {r['sma_n']:>4} | "
              f"{r['n']:>4} {r['wr']:>3.0f}% | {r['m1']:>+6.2f}% {r['m2']:>+6.2f}% | "
              f"{r['eq']:>+6.1f} {r['dd']:>5.1f} | {rdd:>4.1f} | {yr:>+8.1f}%")
    if not robust:
        print("  頑健な構成なし(P1かP2が負け)。金ブル依存=P2のみの構成しかない可能性。")
        res.sort(key=lambda r: r["m2"], reverse=True)
        print("\n  [参考] P2月利トップ5 (P1問わず):")
        for r in res[:5]:
            print(f"  {r['tf']:>3} en{r['entry_n']} ex{r['exit_n']} SL{r['sl_atr']:.0f} SMA{r['sma_n']} | "
                  f"P1/月{r['m1']:>+6.2f}% P2/月{r['m2']:>+6.2f}% DD{r['dd']:.1f}")
    print("-" * 104)
    print("R/DD = 累積R÷最大DD(高いほどサイズUP余地=リターン効率)。年複利は P2月利ベース。")


if __name__ == "__main__":
    main()
