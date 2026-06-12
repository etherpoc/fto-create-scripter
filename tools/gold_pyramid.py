"""gold_pyramid.py — 金単体 ピラミッディングの DD/年次/リスク%別 詳細。"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.breakout_lab import pip, comm, SPREAD  # noqa
from tools.bo_fast import cached_arrays, run_bo_fast  # noqa

SYM = "XAUUSD"
B = dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100)
CONFIGS = {
    "base (pyramidなし)":        dict(B),
    "pyramid x1 step1N":         dict(B, max_adds=1, step_atr=1.0),
    "pyramid x2 step0.5N":       dict(B, max_adds=2, step_atr=0.5),
    "pyramid x3 step0.5N":       dict(B, max_adds=3, step_atr=0.5),
}


def net(bars, P):
    tr = run_bo_fast(bars, P); ps = pip(SYM); c = comm(SYM) + SPREAD
    for t in tr:
        t["nR"] = t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)
    return tr


def dd_of(tr, risk):
    ev = sorted(tr, key=lambda x: x["t"]); eq = peak = dd = 0.0
    for t in ev:
        eq += t["nR"] * risk; peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def main():
    bars = cached_arrays(SYM, "h4")
    for cname, P in CONFIGS.items():
        tr = net(bars, P)
        n = len(tr); w = sum(1 for t in tr if t["nR"] > 0)
        maxu = max((t["units"] for t in tr), default=1)
        years = {}
        for t in tr:
            y = datetime.fromtimestamp(t["t"], tz=timezone.utc).year
            years[y] = years.get(y, 0.0) + t["nR"]
        print("=" * 86)
        print(f"■ {cname}   N={n} WR={100*w/n:.0f}% 最大同時ユニット={maxu}")
        print("  年別 net(risk1%/初期unit): " + " ".join(f"{y}:{years[y]:+.0f}" for y in sorted(years)))
        neg = [y for y in years if years[y] < 0]
        print(f"  マイナス年: {neg if neg else 'なし'}")
        print(f"  {'risk/unit':>9} | {'累積5.5y':>9} {'年複利':>7} | {'最大DD':>7} | DD<10%")
        for risk in [0.25, 0.5, 0.75, 1.0]:
            eq, dd = dd_of(tr, risk)
            yr = ((1 + (eq/66)/100) ** 12 - 1) * 100
            print(f"  {risk:>7.2f}% | {eq:>+8.1f}% {yr:>+6.1f}% | {dd:>6.1f}% | {'OK' if dd<10 else '--'}")
        print()


if __name__ == "__main__":
    main()
