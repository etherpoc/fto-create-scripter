"""
axiory_usdopt.py — 「USD系はパラメータが違うだけでは?」を厳密に潰す WF パラメータ最適化。

各USDペアで Donchianブレイク のパラメータ(entry_n/sl_atr/sma_n/direction)を OOS(2015-20)で最適化し、
IS(2021-26)で評価。逆(ISで選びOOSで評価)も。**OOS最適パラメータがISでも+なら「USDにも頑健なパラメータが存在」**。
両方で剥落するなら「USDにトレンド機械エッジは(このコストでは)無い」が頑健な結論。net=コミ+spread。
"""
from __future__ import annotations
import sys, itertools
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
USD = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]
GRID = [dict(entry_n=en, exit_n=ex, atr_n=20, sl_atr=sl, sma_n=sm, direction=d)
        for en in (10, 20, 30, 40, 55) for ex in (10, 20)
        for sl in (2.0, 3.0) for sm in (0, 100, 200) for d in ("long", "short", "both")]


def split_net(pair, cfg):
    tr = run_bo_fast(ax.cached_arrays(pair, "h1"), cfg)
    c = bcomm(pair) + SPREAD; ps = bpip(pair)
    p1 = p2 = 0.0
    for t in tr:
        nr = t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)
        if t["t"] < SPLIT: p1 += nr
        else: p2 += nr
    return p1, p2, len(tr)


def main():
    print("=" * 96)
    print("USD系 WFパラメータ最適化 — OOSで最適化→ISで評価(と逆)。両方+なら頑健パラメータ存在")
    print("=" * 96)
    print(f"  グリッド {len(GRID)}構成 × {len(USD)}ペア")
    n_robust = 0
    for p in USD:
        res = [(cfg,) + split_net(p, cfg) for cfg in GRID]  # (cfg,p1,p2,n)
        res = [r for r in res if r[3] >= 20]                # 最低トレード数
        best_oos = max(res, key=lambda r: r[1])
        best_is = max(res, key=lambda r: r[2])
        # OOS最適のIS / IS最適のOOS
        c1, o1, i1, n1 = best_oos
        c2, o2, i2, n2 = best_is
        rob = "✅頑健あり" if (i1 > 0 and o2 > 0) else "✗剥落"
        if i1 > 0 and o2 > 0: n_robust += 1
        print(f"\n  {p}:")
        print(f"    OOS最適 [en{c1['entry_n']}/sl{c1['sl_atr']}/sma{c1['sma_n']}/{c1['direction']}] "
              f"OOS{o1:+.0f} → IS{i1:+.0f}  (ISで{'+' if i1>0 else '-'})")
        print(f"    IS最適  [en{c2['entry_n']}/sl{c2['sl_atr']}/sma{c2['sma_n']}/{c2['direction']}] "
              f"IS{i2:+.0f} → OOS{o2:+.0f}  (OOSで{'+' if o2>0 else '-'})  {rob}")
    print(f"\n  → 頑健パラメータが見つかったUSDペア: {n_robust}/{len(USD)}")
    print(f"  (OOS最適がISでも+ かつ IS最適がOOSでも+ を満たすペア。0なら『USDはパラメータでも救えない』が頑健)")


if __name__ == "__main__":
    main()
