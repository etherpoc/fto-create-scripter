"""
verify_diversify_fx.py — 新データ無しで「分散レバー」を引けるか検証 (Axiory 15ペアのみ)。

現運用 BO7 = XAU+JPYクロス6 = 実質「円安/リスクオン」一点ベット。未使用の USD/EUR/GBPブロック
(AUDUSD,EURUSD,GBPUSD,NZDUSD,USDCAD,USDCHF,EURGBP,CADJPY) は駆動要因が違う=低相関の可能性。
単体で弱くても低相関ならポートのDDを下げ→DD10%でサイズ↑→月利↑(月利∝Sharpe×√N)。

実EAの約定モデル(成行・次足・終値確認 = verify_short_sleeve.ea_trades)で long-only net を生成し、
  - JPY7(現運用) / nonJPY8 / ALL15 のポート効率(sumR/DD)・DD10%換算月利
  - JPYスリーブ と nonJPYスリーブ の月次相関
を出す。overlay は合算後に適用。

  python tools/verify_diversify_fx.py
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.axiory_data import cached_arrays                       # noqa: E402
from tools.backtest_breakout import pip as bpip, comm as bcomm    # noqa: E402
from tools.verify_short_sleeve import ea_trades, eq_overlay       # noqa: E402

SPREAD = 0.5
SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
JPY7 = ["XAUUSD", "USDJPY", "EURJPY", "AUDJPY", "GBPJPY", "CHFJPY", "NZDJPY"]   # 現運用
NONJPY = ["AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "EURGBP", "CADJPY"]
ALL15 = JPY7 + NONJPY


def net_stream(pair):
    t, o, h, l, c = cached_arrays(pair, "h1")
    ps = bpip(pair); cost = bcomm(pair) + SPREAD
    return [(x["t"], x["R"] - cost / max(x["sld"] / ps, 1e-9)) for x in ea_trades(t, o, h, l, c, "long")]


def metrics(trades):
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    dd = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    mo = max(1.0, (t[-1] - t[0]) / (365.25 * 86400) * 12)
    p1 = float(r[t < SPLIT].sum()); p2 = float(r[t >= SPLIT].sum())
    mo1 = max(1.0, (SPLIT - t[0]) / (365.25 * 86400) * 12)
    mo2 = max(1.0, (t[-1] - SPLIT) / (365.25 * 86400) * 12)
    return dict(sumR=float(r.sum()), dd=dd, n=len(r), perMo=float(r.sum()) / mo,
                p1m=p1 / mo1, p2m=p2 / mo2)


def monthly(trades):
    """月次純R系列 {YYYY-MM: sumR}。"""
    m = {}
    for (t, r) in trades:
        k = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")
        m[k] = m.get(k, 0.0) + r
    return m


def corr(a, b):
    ks = sorted(set(a) | set(b))
    x = np.array([a.get(k, 0.0) for k in ks]); y = np.array([b.get(k, 0.0) for k in ks])
    if x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def port(pairs):
    s = []
    for p in pairs:
        s += net_stream(p)
    return eq_overlay(s)


def main():
    print("=" * 92)
    print("分散レバー検証: 現運用JPY7 に 未使用nonJPY8 を足すとポートは改善するか (Axiory15, 実EA約定)")
    print("=" * 92)

    # 単体の素性(overlay前)
    print("  [単体] long-only net (overlay前):")
    print(f"  {'pair':<8} {'N':>5} {'sumR':>8} {'DD':>7} {'効率':>6} {'OOS/月':>8} {'IS/月':>8}")
    raw = {}
    for p in ALL15:
        st = net_stream(p); raw[p] = st
        m = metrics(st)
        tag = "JPY" if p in JPY7 else "non"
        print(f"  {p:<8} {m['n']:>5} {m['sumR']:>+7.0f} {m['dd']:>7.0f} {m['sumR']/m['dd']:>6.1f} "
              f"{m['p1m']:>+7.1f} {m['p2m']:>+7.1f}  [{tag}]")

    print("-" * 92)
    print("  [ポート] overlay適用・DD10%換算:")
    print(f"  {'構成':<20} | {'N':>5} {'効率sumR/DD':>11} | {'OOS%/月':>8} {'IS%/月':>8} | {'risk%/pair':>10}")
    for label, pairs in [("JPY7 (現運用)", JPY7), ("nonJPY8 単独", NONJPY), ("ALL15", ALL15)]:
        comb = port(pairs)
        m = metrics(comb)
        s = 10.0 / m["dd"] if m["dd"] > 0 else 0
        npp = len(pairs)
        print(f"  {label:<20} | {m['n']:>5} {m['sumR']/m['dd']:>11.1f} | "
              f"{m['p1m']*s:>+7.2f} {m['p2m']*s:>+7.2f} | {s/1:>9.3f}")

    # 相関(スリーブ単位, 月次)
    jm = monthly([x for p in JPY7 for x in raw[p]])
    nm = monthly([x for p in NONJPY for x in raw[p]])
    print("-" * 92)
    print(f"  JPY7スリーブ × nonJPY8スリーブ 月次相関 = {corr(jm, nm):+.2f}  (低/負ほど分散効果大)")
    print("判定: ALL15 の効率(sumR/DD)が JPY7 を上回り、月次相関が低ければ『新データ無しで分散レバー有効』。")


if __name__ == "__main__":
    main()
