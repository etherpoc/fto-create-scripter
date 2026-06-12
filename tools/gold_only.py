"""
gold_only.py — XAUUSD 単体ブレイクアウトの詳細(年別一貫性・DD・リスク%別リターン)。

金はトレンド依存なので「金がレンジの年=悪い年」がどれだけ痛いかを見る。
net = 往復コミ + spread。1R=口座1%。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.backtest_breakout import run_breakout, pip, comm  # noqa: E402
from tools.breakout_deepdive import extract                  # noqa: E402

SYM = "XAUUSD"
SPREAD = 0.5

CONFIGS = {
    "A: en20/ex20/SL2/SMA100 (バランス)": dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100),
    "B: en30/ex20/SL3/SMA100 (低DD効率)": dict(entry_n=30, exit_n=20, atr_n=20, sl_atr=3.0, sma_n=100),
    "C: en20/ex20/SL2/SMA50 (高リターン)": dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=50),
}


def nettrades(bars):
    ps = pip(SYM); cost = comm(SYM) + SPREAD
    out = []
    for cname, P in CONFIGS.items():
        tr = run_breakout(bars, P)
        for t in tr:
            t["nR"] = t["R"] - cost / max(t["sl_dist"] / ps, 1e-9)
        out.append((cname, tr))
    return out


def maxdd(trades, risk):
    ev = sorted(trades, key=lambda x: x["t"])
    eq = peak = dd = 0.0
    for t in ev:
        eq += t["nR"] * risk; peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def worst_day(trades, risk):
    by = {}
    for t in trades:
        d = datetime.fromtimestamp(t["t"], tz=timezone.utc).strftime("%Y-%m-%d")
        by[d] = by.get(d, 0.0) + t["nR"] * risk
    return min(by.values()) if by else 0.0


def main():
    bars = extract(SYM, "h4")
    print(f"{SYM} H4 {len(bars)}本\n")
    allres = nettrades(bars)

    for cname, tr in allres:
        n = len(tr); w = sum(1 for t in tr if t["nR"] > 0)
        print("=" * 88)
        print(f"■ {cname}   N={n}  WR={100*w/n:.0f}%")
        print("=" * 88)
        # 年別 net R (risk1%)
        years = {}
        for t in tr:
            y = datetime.fromtimestamp(t["t"], tz=timezone.utc).year
            years[y] = years.get(y, 0.0) + t["nR"]
        print("  年別 net (risk1%):  " + "  ".join(f"{y}:{years[y]:+.0f}%" for y in sorted(years)))
        neg = [y for y in years if years[y] < 0]
        print(f"  マイナスの年: {neg if neg else 'なし'}  / 最良年 {max(years.values()):+.0f}% / 最悪年 {min(years.values()):+.0f}%")
        # リスク%別 リターン/DD
        print(f"\n  {'risk/trade':>10} | {'累積(5.5y)':>10} {'年複利':>7} | {'最大DD':>7} {'最悪日':>7} | プロップ(DD<10%)")
        for risk in [0.5, 1.0, 1.5, 2.0]:
            eq, dd = maxdd(tr, risk)
            wd = worst_day(tr, risk)
            yr = ((1 + (eq/66)/100) ** 12 - 1) * 100   # 平均月利→年複利 (66ヶ月)
            ok = "✅" if dd < 10 else "❌"
            print(f"  {risk:>8.1f}% | {eq:>+9.1f}% {yr:>+6.1f}% | {dd:>6.1f}% {wd:>+6.1f}% | {ok}")
        print()


if __name__ == "__main__":
    main()
