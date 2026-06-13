"""
axiory_basket.py — breakout の採用ペアセットを実Axiory 11年で比較(5 vs 7 vs ...)。
overlay入り、DD10%にスケールしたときの OOS/IS 月利・効率で「ペア追加が改善か」を判定。

CHFJPY/NZDJPY は全15スキャンで OOS/IS 両期間net+ → basket追加候補。実際に効率が上がるか確認。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.axiory_validate import pair_net, eq_overlay   # noqa: E402

SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
BO5 = ["XAUUSD", "USDJPY", "EURJPY", "AUDJPY", "GBPJPY"]
BO7 = BO5 + ["CHFJPY", "NZDJPY"]


def metrics(trades):
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    eq = np.cumsum(r); dd = float((np.maximum.accumulate(eq) - eq).max())
    mo1 = max(1.0, (SPLIT - t[0]) / (365.25 * 86400) * 12)
    mo2 = max(1.0, (t[-1] - SPLIT) / (365.25 * 86400) * 12)
    p1 = float(r[t < SPLIT].sum()); p2 = float(r[t >= SPLIT].sum())
    return dd, p1, p2, p1 / mo1, p2 / mo2, len(r)


def report(name, pairs):
    tr = []
    for p in pairs:
        tr += pair_net(p)
    tr = eq_overlay(tr)   # overlay on
    dd, p1, p2, p1m, p2m, n = metrics(tr)
    s = 10.0 / dd if dd > 0 else 0   # DD=10%にスケール(risk%/pair = s, 1R=1%基準)
    print(f"  {name:<10}(n={len(pairs)}) | N{n:>4} | 全DD(1%){dd:>5.1f} → risk{s:>5.3f}%/pair でDD10%")
    print(f"       OOS(15-20){p1m*s:>+5.2f}%/月  IS(21-26){p2m*s:>+5.2f}%/月  効率(sumR/DD){(p1+p2)/dd:>5.2f}")
    return (p1 + p2) / dd


def main():
    print("=" * 90)
    print("breakout 採用ペア比較 @ 実Axiory 11年 (overlay on, DD10%スケール, OOS/IS)")
    print("=" * 90)
    e5 = report("BO5(現行)", BO5)
    e7 = report("BO7(+CHFJPY/NZDJPY)", BO7)
    print("-" * 90)
    print(f"  → 効率 BO5={e5:.2f} / BO7={e7:.2f}  : {'BO7採用(改善)' if e7 > e5 else 'BO5維持(改善せず)'}")


if __name__ == "__main__":
    main()
