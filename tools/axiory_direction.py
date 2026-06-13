"""
axiory_direction.py — 実Axiory 11年で「long-only basket と無相関の補完」を価格データ内で探す。

第一候補=ショート側。long-only は上昇相場が必要 → ショートは下落/リスクオフで稼ぐ=潜在的に無相関ヘッジ。
FTO 2021-26(全部上昇)ではショート負けだったが、2015-2020(リスクオフ局面含む)で効くか実測。
併せて別機序(MA交差)も direction 別に見る。

評価: 各方向の OOS/IS net + 「long-basket と short-basket の月次相関」(負なら真のヘッジ)。
robust-7 を母集団に。net=コミ+spread。run_bo_fast/run_macross_fast(同一エンジン)に Axiory 配列を流す。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bo_fast import run_bo_fast, run_macross_fast          # noqa: E402
from tools.backtest_breakout import pip as bpip, comm as bcomm   # noqa: E402
import tools.axiory_data as ax                                   # noqa: E402

SPREAD = 0.5
SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
BO7 = ["XAUUSD", "USDJPY", "EURJPY", "AUDJPY", "GBPJPY", "CHFJPY", "NZDJPY"]
ALL15 = ax.PAIRS


def net_trades(pair, cfg, engine=run_bo_fast):
    arr = ax.cached_arrays(pair, "h1")
    tr = engine(arr, cfg)
    c = bcomm(pair) + SPREAD; ps = bpip(pair)
    return [(t["t"], t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)) for t in tr]


def basket(pairs, cfg, engine=run_bo_fast):
    out = []
    for p in pairs:
        out += net_trades(p, cfg, engine)
    return out


def monthly(trades):
    m = {}
    for (t, r) in trades:
        k = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")
        m[k] = m.get(k, 0.0) + r
    return m


def corr(a, b):
    ks = sorted(set(a) | set(b)); x = np.array([a.get(k, 0.0) for k in ks]); y = np.array([b.get(k, 0.0) for k in ks])
    return float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else 0.0


def stat(trades):
    if not trades:
        return "0", 0, 0, 0
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    dd = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    mo1 = max(1.0, (SPLIT - t[0]) / (365.25 * 86400) * 12); mo2 = max(1.0, (t[-1] - SPLIT) / (365.25 * 86400) * 12)
    return (float(r[t < SPLIT].sum()) / mo1, float(r[t >= SPLIT].sum()) / mo2, dd, len(r))


def show(label, trades):
    p1, p2, dd, n = stat(trades)
    rob = "✅両+" if (p1 > 0 and p2 > 0) else ("△IS+" if p2 > 0 else ("△OOS+" if p1 > 0 else "✗"))
    print(f"  {label:<30} | N{n:>4} | OOS{p1:>+5.2f}%/月 IS{p2:>+5.2f}%/月 DD{dd:>5.1f} | {rob}")


BO = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150)
MA = dict(fast_n=20, slow_n=100, atr_n=20, sl_atr=3.0)


def main():
    print("=" * 100)
    print("実Axiory 11年: long-only と無相関の補完を価格内で探す (robust-7, OOS/IS)")
    print("=" * 100)
    print("\n--- Donchianブレイク 方向別 ---")
    lo = basket(BO7, dict(BO, direction="long"))
    so = basket(BO7, dict(BO, direction="short"))
    bo = basket(BO7, dict(BO, direction="both"))
    show("long-only(現行)", lo)
    show("short-only", so)
    show("both", bo)
    print(f"\n  ★ long-basket vs short-basket 月次相関: {corr(monthly(lo), monthly(so)):+.2f}  "
          f"(負=真のヘッジ / 正=同時に動く)")

    print("\n--- MA交差(別機序) 方向別 ---")
    mlo = basket(BO7, dict(MA, direction="long"), run_macross_fast)
    mso = basket(BO7, dict(MA, direction="short"), run_macross_fast)
    show("MA long-only", mlo)
    show("MA short-only", mso)
    print(f"  MA-long vs Donchian-long 相関: {corr(monthly(mlo), monthly(lo)):+.2f} (高=冗長)")

    # 全15でショートが効く局面があるか(OOS別)
    print("\n--- short-only を全15ペアで(2015-20リスクオフで稼ぐペアは?) ---")
    so15 = []
    for p in ALL15:
        tr = net_trades(p, dict(BO, direction="short"))
        p1, p2, dd, n = stat(tr)
        if p1 > 0 or p2 > 0:
            so15 += [(p, p1, p2)]
    so15.sort(key=lambda x: -(x[1] + x[2]))
    for (p, p1, p2) in so15[:8]:
        print(f"    {p:<7} OOS{p1:>+5.2f} IS{p2:>+5.2f}")


if __name__ == "__main__":
    main()
