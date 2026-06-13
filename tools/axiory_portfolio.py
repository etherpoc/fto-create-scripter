"""
axiory_portfolio.py — long+short スリーブ採用ポートフォリオの (1)αのWF頑健性 (2)最終診断。

(1) アンカーWF: αを OOS(2015-20)だけで選び IS(2021-26) で評価 / 逆も。固定αが両期間で良ければ過剰適合でない。
(2) 採用構成(long-7 full + short-7 ×0.4, overlay)を DD10%スケールで年別・OOS/IS別DD・最悪月・負け月% 診断。
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


def _net(pair, direction):
    tr = run_bo_fast(ax.cached_arrays(pair, "h1"), dict(BO, direction=direction))
    c = bcomm(pair) + SPREAD; ps = bpip(pair)
    return [(t["t"], t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)) for t in tr]


def basket(direction):
    out = []
    for p in BO7:
        out += _net(p, direction)
    return out


def overlay(trades, K=20, m=0.5):
    trades = sorted(trades, key=lambda x: x[0]); eq = 0.0; hist = []; out = []
    for (t, r) in trades:
        mult = 0.5 if (len(hist) >= K and eq < sum(hist[-K:]) / K) else 1.0
        p = r * mult; eq += p; hist.append(eq); out.append((t, p))
    return out


def seg_metrics(trades, mask_fn):
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    m = mask_fn(t); e = np.cumsum(r[m])
    dd = float((np.maximum.accumulate(e) - e).max()) if len(e) else 0.0
    return float(r[m].sum()), dd


def full_dd(trades):
    r = np.array([x[1] for x in sorted(trades, key=lambda x: x[0])])
    e = np.cumsum(r); return float((np.maximum.accumulate(e) - e).max())


def combo(a):
    return overlay(basket("long") + [(t, a * r) for (t, r) in basket("short")])


def main():
    L = basket("long"); S = basket("short")

    print("=" * 92)
    print("(1) α(short比) アンカーWF — 片期間で選び他期間で評価(固定αが両方で良ければ頑健)")
    print("=" * 92)
    grid = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0]
    def eff(a, mask):
        c = overlay(L + [(t, a * r) for (t, r) in S])
        p, dd = seg_metrics(c, mask)
        return p / dd if dd > 0 else 0
    oos = lambda t: t < SPLIT; iss = lambda t: t >= SPLIT
    a_oos = max(grid, key=lambda a: eff(a, oos))
    a_is = max(grid, key=lambda a: eff(a, iss))
    print(f"  OOSで最適なα = {a_oos}  → そのαのIS効率 = {eff(a_oos, iss):.2f}")
    print(f"  ISで最適なα  = {a_is}  → そのαのOOS効率 = {eff(a_is, oos):.2f}")
    print(f"  → 両期間で選ばれるαが近い({a_oos}/{a_is})なら過剰適合でない")

    print("\n" + "=" * 92)
    print("(2) 採用構成 long-7 + short-7×0.4 + overlay の最終診断 (DD10%スケール)")
    print("=" * 92)
    c = combo(0.4)
    dd = full_dd(c); s = 10.0 / dd
    cs = [(t, r * s) for (t, r) in c]
    t = np.array([x[0] for x in cs]); r = np.array([x[1] for x in cs]); o = np.argsort(t); t = t[o]; r = r[o]
    # 年別
    yr = {}
    for tt, pp in zip(t, r):
        y = datetime.fromtimestamp(tt, tz=timezone.utc).year; yr[y] = yr.get(y, 0.0) + pp
    # 月別
    mo = {}
    for tt, pp in zip(t, r):
        k = datetime.fromtimestamp(tt, tz=timezone.utc).strftime("%Y-%m"); mo[k] = mo.get(k, 0.0) + pp
    mvals = np.array(list(mo.values()))
    p_oos, dd_oos = seg_metrics(cs, oos); p_is, dd_is = seg_metrics(cs, iss)
    mo1 = 72; mo2 = (t[-1] - SPLIT) / (365.25 * 86400) * 12
    print(f"  risk/pair(long) ≈ {s:.3f}% (short=その0.4倍 ≈ {s*0.4:.3f}%)  全DD={10.0:.0f}%(スケール先)")
    print(f"  OOS(2015-20): 月{p_oos/mo1:+.2f}%/月  期間内DD{dd_oos:.1f}%")
    print(f"  IS (2021-26): 月{p_is/mo2:+.2f}%/月  期間内DD{dd_is:.1f}%")
    print(f"  月次: 平均{mvals.mean():+.2f}%  Sharpe(年){mvals.mean()/mvals.std()*np.sqrt(12):+.2f}  "
          f"最悪月{mvals.min():+.2f}%  負け月{100*(mvals<0).mean():.0f}%")
    print(f"  年別: " + " ".join(f"{y}:{yr[y]:+.0f}" for y in sorted(yr)))
    print(f"\n  目標6-8%/月との距離: IS{p_is/mo2/7*100:.0f}% (月7%基準)。long-onlyの約2倍だが安全DDでは未達。")


if __name__ == "__main__":
    main()
