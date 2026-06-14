"""
axiory_meanrev.py — 平均回帰(ボリンジャー/zスコア・フェード)を実Axioryで検証。

仮説: JPY系=トレンド(breakout向き)、USD系=レンジ寄り→平均回帰が合うのでは。トレンドで「USD負け」と
結論したのは片手落ち。USD majors を中心に、price の z-score 逆張りを net・WF で測る。効けば M5 スキャへ。

z = (close - SMA_W)/std_W。|z|>z_entry でフェード(zが高→売/低→買)、SL=entry±sl_atr·ATR、
target=平均(z→0)で利確 or SL or timeout。1R=SL距離。net=コミ+spread。WF OOS/IS。

  python tools/axiory_meanrev.py            # USD majors H1
  python tools/axiory_meanrev.py all h1
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.backtest_breakout import pip as bpip, comm as bcomm   # noqa: E402
import tools.axiory_data as ax                                   # noqa: E402

SPREAD = 0.5
SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
USD = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]


def atr_arr(h, l, c, n):
    tr = np.zeros(len(c)); tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    return pd.Series(tr).rolling(n).mean().values


def meanrev(arr, W=50, z_entry=2.0, sl_atr=2.0, atr_n=20, direction="both", max_hold=100):
    t, o, h, l, c = arr
    sma = pd.Series(c).rolling(W).mean().values
    std = pd.Series(c).rolling(W).std().values
    atr = atr_arr(h, l, c, atr_n)
    z = (c - sma) / std
    trades = []; pos = 0; ent = 0.0; sl = 0.0; ent_i = 0
    warm = max(W, atr_n) + 2
    for i in range(warm, len(c)):
        if not (std[i] > 0 and atr[i] > 0):
            continue
        if pos == 0:
            if z[i] > z_entry and z[i - 1] <= z_entry and direction in ("both", "short"):
                pos = -1; ent = c[i]; sl = c[i] + sl_atr * atr[i]; ent_i = i
            elif z[i] < -z_entry and z[i - 1] >= -z_entry and direction in ("both", "long"):
                pos = 1; ent = c[i]; sl = c[i] - sl_atr * atr[i]; ent_i = i
        else:
            sld = abs(ent - sl); ex = None
            if pos == 1:
                if l[i] <= sl: ex = sl
                elif c[i] >= sma[i]: ex = c[i]      # 平均回帰達成
            else:
                if h[i] >= sl: ex = sl
                elif c[i] <= sma[i]: ex = c[i]
            if ex is None and i - ent_i >= max_hold: ex = c[i]
            if ex is not None:
                pnl = (ex - ent) * pos
                trades.append((int(t[i]), pnl / sld, sld))
                pos = 0
    return trades


def evalp(pair, tf, **kw):
    arr = ax.cached_arrays(pair, tf)
    tr = meanrev(arr, **kw)
    c = bcomm(pair) + SPREAD; ps = bpip(pair)
    return [(t, R - c / max(sld / ps, 1e-9)) for (t, R, sld) in tr]


def st(trades):
    if not trades:
        return None
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t, r = t[o], r[o]
    dd = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    return len(r), 100 * (r > 0).mean(), float(r[t < SPLIT].sum()), float(r[t >= SPLIT].sum()), dd


def show(label, trades):
    s = st(trades)
    if not s:
        print(f"  {label:<22}| 0"); return
    n, wr, p1, p2, dd = s
    rob = "✅両+" if (p1 > 0 and p2 > 0) else ("△IS+" if p2 > 0 else ("△OOS+" if p1 > 0 else "✗"))
    print(f"  {label:<22}| N{n:>5} WR{wr:>4.1f}% | OOS{p1:>+6.1f} IS{p2:>+6.1f} DD{dd:>5.1f} | {rob}")


def main():
    pairs = (ax.PAIRS if (len(sys.argv) > 1 and sys.argv[1] == "all") else USD)
    tf = sys.argv[2] if len(sys.argv) > 2 else "h1"
    print("=" * 96)
    print(f"平均回帰(zフェード) @ 実Axiory {tf} — USD系はレンジ向き?を検証 (net, WF OOS/IS)")
    print("=" * 96)
    for cfgname, kw in [("z2.0/sl2/W50", {}), ("z2.5/sl2/W50", dict(z_entry=2.5)),
                        ("z2/sl1.5/W30", dict(sl_atr=1.5, W=30)), ("z2/sl3/W100", dict(sl_atr=3.0, W=100))]:
        print(f"\n--- {cfgname} ---")
        agg = []
        for p in pairs:
            tr = evalp(p, tf, **kw)
            show(p, tr); agg += tr
        show("★合算", agg)


if __name__ == "__main__":
    main()
