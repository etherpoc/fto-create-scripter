"""
axiory_longshort.py — long-only basket に「小さなショート・スリーブ」を足すと改善するか。

short は long と月次相関 -0.18・両期間net+ → 静的に少量持つと long のDD(下落/リスクオフ局面)を
ヘッジしつつ自身も僅かに稼ぐ可能性。long + α·short の α を振り、DD10%換算の OOS/IS と効率で最適点を探す。

robust-7、overlay は合算後に適用。net=コミ+spread。WF: OOS=2015-20 / IS=2021-26。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bo_fast import run_bo_fast                            # noqa: E402
from tools.backtest_breakout import pip as bpip, comm as bcomm   # noqa: E402
import tools.axiory_data as ax                                   # noqa: E402

SPREAD = 0.5
SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
BO7 = ["XAUUSD", "USDJPY", "EURJPY", "AUDJPY", "GBPJPY", "CHFJPY", "NZDJPY"]
BO = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150)


def net_trades(pair, direction):
    arr = ax.cached_arrays(pair, "h1")
    tr = run_bo_fast(arr, dict(BO, direction=direction))
    c = bcomm(pair) + SPREAD; ps = bpip(pair)
    return [(t["t"], t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)) for t in tr]


def basket(direction):
    out = []
    for p in BO7:
        out += net_trades(p, direction)
    return out


def eq_overlay(trades, K=20, m=0.5):
    trades = sorted(trades, key=lambda x: x[0]); eq = 0.0; hist = []; out = []
    for (t, r) in trades:
        mult = 1.0
        if len(hist) >= K and eq < sum(hist[-K:]) / K:
            mult = m
        p = r * mult; eq += p; hist.append(eq); out.append((t, p))
    return out


def metrics(trades):
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    dd = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    mo1 = max(1.0, (SPLIT - t[0]) / (365.25 * 86400) * 12); mo2 = max(1.0, (t[-1] - SPLIT) / (365.25 * 86400) * 12)
    p1 = float(r[t < SPLIT].sum()); p2 = float(r[t >= SPLIT].sum())
    return dd, p1, p2, p1 / mo1, p2 / mo2


def main():
    print("=" * 96)
    print("long + α·short スリーブ最適化 @ 実Axiory 11年 (robust-7, overlay, DD10%スケール)")
    print("=" * 96)
    L = basket("long")
    S = basket("short")
    print(f"  {'α(short比)':<10} | {'OOS%/月':>8} {'IS%/月':>8} | {'効率sumR/DD':>11} | risk%/pair(DD10%)")
    best = None
    for a in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0):
        comb = L + [(t, a * r) for (t, r) in S]
        comb = eq_overlay(comb)
        dd, p1, p2, p1m, p2m = metrics(comb)
        eff = (p1 + p2) / dd if dd > 0 else 0
        s = 10.0 / dd if dd > 0 else 0
        mark = ""
        if best is None or eff > best[0]:
            best = (eff, a); mark = ""
        print(f"  {a:<10} | {p1m*s:>+7.2f} {p2m*s:>+7.2f} | {eff:>11.2f} | {s:>.3f}")
    print("-" * 96)
    print(f"  → 効率最大の short比 α = {best[1]}  (効率{best[0]:.2f})")
    # long-only(α=0) と比較
    comb0 = eq_overlay(L); dd0, _, _, m1_0, m2_0 = metrics(comb0); s0 = 10.0 / dd0
    print(f"  long-only(α=0): OOS{m1_0*s0:+.2f}%/月 IS{m2_0*s0:+.2f}%/月 効率{(metrics(comb0)[1]+metrics(comb0)[2])/dd0:.2f}")


if __name__ == "__main__":
    main()
