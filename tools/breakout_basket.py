"""breakout_basket.py — long-only ブレイクアウト・バスケットの合成 net/DD/年次。"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.breakout_lab import pip, comm, SPREAD  # noqa
from tools.bo_fast import cached_arrays, run_bo_fast  # noqa

P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
CONFIGS = {
    "long-only/SMA100": dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, direction="long"),
    "long-only/SMA150": dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=150, direction="long"),
}
# SMA100/150 で viable だった金+JPY+α
PAIRS = ["XAUUSD", "USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "USDCHF"]


def trades_of(sym, P):
    tr = run_bo_fast(cached_arrays(sym, "h4"), P); ps = pip(sym); c = comm(sym) + SPREAD
    for t in tr:
        t["nR"] = t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9); t["sym"] = sym
    return tr


def combine_dd(trades, risk):
    ev = sorted(trades, key=lambda x: x["t"]); eq = peak = dd = 0.0
    for t in ev:
        eq += t["nR"] * risk; peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def main():
    for cname, P in CONFIGS.items():
        print("=" * 90)
        print(f"■ {cname}  バスケット {PAIRS}")
        print("=" * 90)
        allt = []
        print(f"  {'pair':8} | {'N':>4} {'WR':>4} | {'net P1':>7} {'net P2':>7} | robust")
        for sym in PAIRS:
            tr = trades_of(sym, P); allt += tr
            n = len(tr)
            if n == 0:
                print(f"  {sym:8} | 0"); continue
            w = sum(1 for t in tr if t["nR"] > 0)
            n1 = sum(t["nR"] for t in tr if t["t"] < P1_END)
            n2 = sum(t["nR"] for t in tr if t["t"] >= P1_END)
            print(f"  {sym:8} | {n:>4} {100*w/n:>3.0f}% | {n1:>+6.1f} {n2:>+6.1f} | {'✓' if n1>0 and n2>0 else ' '}")
        # 合成
        p1 = sum(t["nR"] for t in allt if t["t"] < P1_END)
        p2 = sum(t["nR"] for t in allt if t["t"] >= P1_END)
        years = {}
        for t in allt:
            y = datetime.fromtimestamp(t["t"], tz=timezone.utc).year
            years[y] = years.get(y, 0.0) + t["nR"]
        gold = sum(t["nR"] for t in allt if t["sym"] == "XAUUSD")
        tot = sum(t["nR"] for t in allt)
        print(f"\n  合成 net(risk1%/pair): P1 {p1:+.1f}% ({p1/36:+.2f}%/月) / P2 {p2:+.1f}% ({p2/30:+.2f}%/月)")
        print(f"  年別: " + " ".join(f"{y}:{years[y]:+.0f}" for y in sorted(years)) +
              f"  (マイナス年: {[y for y in years if years[y]<0] or 'なし'})")
        print(f"  金の寄与: {gold:+.0f}% / 全体 {tot:+.0f}%  (金依存度 {100*gold/tot:.0f}%)")
        print(f"\n  {'risk/pair':>9} | {'合成net5.5y':>11} {'年複利':>7} | {'合成DD':>7} | DD<10%")
        for risk in [0.25, 0.5, 0.75, 1.0]:
            eq, dd = combine_dd(allt, risk)
            yr = ((1 + (eq/66)/100) ** 12 - 1) * 100
            print(f"  {risk:>7.2f}% | {eq:>+10.1f}% {yr:>+6.1f}% | {dd:>6.1f}% | {'OK' if dd<10 else '--'}")
        print()


if __name__ == "__main__":
    main()
