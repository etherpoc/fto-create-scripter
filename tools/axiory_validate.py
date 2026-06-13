"""
axiory_validate.py — 既存 breakout を実Axioryデータ(2015-2026, 11年)で再検証。
真のWF: OOS=2015-2020(既存パラメータ未学習) / IS=2021-2026(従来の最適化期間と重複)。

「2021-22集中・以降停滞」のレジーム依存が長期実データでどう出るか、OOSでエッジが保つかを直視する。
net=往復コミ+spread(従来と同一規約)。run_bo_fast(同一エンジン)に Axiory 配列を流すだけ。

  python tools/axiory_validate.py            # breakout 5ペア + overlay
  python tools/axiory_validate.py all        # 全15ペア long-only スキャン
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
BO_LONG = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150, direction="long")
BREAKOUT5 = ["XAUUSD", "USDJPY", "EURJPY", "AUDJPY", "GBPJPY"]


def eq_overlay(trades, K=20, m=0.5):
    trades = sorted(trades, key=lambda x: x[0]); eq = 0.0; hist = []; out = []
    for (t, r) in trades:
        mult = 1.0
        if len(hist) >= K and eq < sum(hist[-K:]) / K:
            mult = m
        p = r * mult; eq += p; hist.append(eq); out.append((t, p))
    return out


def pair_net(pair, cfg=BO_LONG):
    arr = ax.cached_arrays(pair, "h1")
    tr = run_bo_fast(arr, cfg)
    c = bcomm(pair) + SPREAD; ps = bpip(pair)
    return [(t["t"], t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)) for t in tr]


def months_between(t0, t1):
    return max(1.0, (t1 - t0) / (365.25 * 86400) * 12)


def stats(nets):
    if not nets:
        return None
    t = np.array([x[0] for x in nets]); r = np.array([x[1] for x in nets])
    o = np.argsort(t); t = t[o]; r = r[o]
    eq = np.cumsum(r); dd = float((np.maximum.accumulate(eq) - eq).max())
    m1 = months_between(t[0], SPLIT) if t[0] < SPLIT else 1
    m2 = months_between(SPLIT, t[-1]) if t[-1] >= SPLIT else 1
    p1 = float(r[t < SPLIT].sum()); p2 = float(r[t >= SPLIT].sum())
    w = int((r > 0).sum())
    return dict(n=len(r), wr=100 * w / len(r), p1=p1, p2=p2, dd=dd,
                p1m=p1 / m1, p2m=p2 / m2, sumR=float(r.sum()))


def show(label, nets):
    s = stats(nets)
    if not s:
        print(f"  {label:<26} | 0"); return None
    rob = "✅OOS+IS両+" if (s["p1"] > 0 and s["p2"] > 0) else ("△IS+のみ" if s["p2"] > 0 else ("△OOS+のみ" if s["p1"] > 0 else "✗"))
    print(f"  {label:<26} | N{s['n']:>4} WR{s['wr']:>4.1f}% | "
          f"OOS(15-20){s['p1m']:>+5.2f}%/月 IS(21-26){s['p2m']:>+5.2f}%/月 | DD{s['dd']:>5.1f} sumR{s['sumR']:>+6.1f} | {rob}")
    return s


def main():
    pairs = ax.PAIRS if (len(sys.argv) > 1 and sys.argv[1] == "all") else BREAKOUT5
    print("=" * 112)
    print("breakout long-only @ 実Axiory 2015-2026 (真のWF: OOS=2015-20未学習 / IS=2021-26)")
    print("=" * 112)
    agg = []
    for p in pairs:
        nets = pair_net(p)
        show(p, nets)
        agg += nets
    print("-" * 112)
    show("★合算(等加重)", agg)
    ov = eq_overlay(agg)
    show("★合算 +overlay", ov)


if __name__ == "__main__":
    main()
