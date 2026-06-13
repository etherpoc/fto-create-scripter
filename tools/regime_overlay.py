"""
regime_overlay.py — BO エンジン(H1 long basket)の DD をレジーム/エクイティカーブ・デリスクで
削れるか検証。削れれば再レバで全体リターンが上がる(feasibility数学)。

オーバーレイは「そのトレード時点までの実現エクイティ」だけを使う(ルックアヘッド無し)。
WF: P1/P2 両方で DD が減り MAR が上がるか(=過剰適合でないか)を必ず確認する。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bo_fast import cached_arrays, run_bo_fast              # noqa: E402
from tools.backtest_breakout import pip as bpip, comm as bcomm    # noqa: E402

ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
MONTHS, P1_M, P2_M = 66.0, 36.0, 30.0
SPREAD = 0.5
BO_LONG = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150, direction="long")


def basket_trades():
    out = []
    for s in ALL:
        tr = run_bo_fast(cached_arrays(s, "h1"), BO_LONG)
        if not tr:
            continue
        c = bcomm(s) + SPREAD; ps = bpip(s)
        nrs = [(t["t"], t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)) for t in tr]
        n1 = sum(r for (tt, r) in nrs if tt < P1_END)
        n2 = sum(r for (tt, r) in nrs if tt >= P1_END)
        if n1 > 0 and n2 > 0:
            out += nrs
    out.sort(key=lambda x: x[0])
    return out


def eval_overlay(trades, overlay):
    """overlay(state)->mult を各トレード前に呼ぶ。state は実現エクイティ履歴のみ。"""
    eq = 0.0; peak = 0.0; hist = []  # hist: list of (t, eq)
    times = []; pnls = []
    for (t, r) in trades:
        mult = overlay(eq, peak, hist, t)
        p = r * mult
        eq += p; peak = max(peak, eq); hist.append((t, eq))
        times.append(t); pnls.append(p)
    times = np.array(times); pnls = np.array(pnls)
    return metrics(times, pnls)


def metrics(times, pnl):
    eq = np.cumsum(pnl); peak = np.maximum.accumulate(eq)
    dd = float((peak - eq).max()) if len(eq) else 0.0
    total = float(eq[-1]) if len(eq) else 0.0
    # WF DD: 各期間内の DD も
    def seg(mask):
        e = np.cumsum(pnl[mask]); pk = np.maximum.accumulate(e) if len(e) else np.array([0])
        d = float((pk - e).max()) if len(e) else 0.0
        return float(e[-1]) if len(e) else 0.0, d
    p1, dd1 = seg(times < P1_END); p2, dd2 = seg(times >= P1_END)
    monthly = total / MONTHS
    annual = ((1 + monthly / 100) ** 12 - 1) * 100 if monthly > -100 else -100
    mar = annual / dd if dd > 0 else 0
    return dict(ann=annual, dd=dd, mar=mar, total=total, p1=p1, p2=p2, dd1=dd1, dd2=dd2)


def eq_ma(eq, peak, hist, t, K=40, m=0.5):
    """直近K件のエクイティMAを下回ったら mult=m。"""
    if len(hist) < K:
        return 1.0
    ma = sum(e for (_, e) in hist[-K:]) / K
    return m if eq < ma else 1.0


def dd_throttle(eq, peak, hist, t, D=4.0, m=0.5):
    """現在DDが D% 超なら mult=m。"""
    return m if (peak - eq) > D else 1.0


def main():
    tr = basket_trades()
    print("=" * 96)
    print(f"BO H1-long basket レジーム/エクイティ・オーバーレイ検証 (N={len(tr)}, net, 1R=1%/pair)")
    print("DD を削って再レバ可能か。WF: P1/P2 両方で DD↓ & MAR↑ が必要(過剰適合排除)。")
    print("=" * 96)

    configs = [
        ("baseline", lambda e, p, h, t: 1.0),
        ("eqMA K20 m0.5", lambda e, p, h, t: eq_ma(e, p, h, t, 20, 0.5)),
        ("eqMA K40 m0.5", lambda e, p, h, t: eq_ma(e, p, h, t, 40, 0.5)),
        ("eqMA K40 m0.0", lambda e, p, h, t: eq_ma(e, p, h, t, 40, 0.0)),
        ("eqMA K80 m0.5", lambda e, p, h, t: eq_ma(e, p, h, t, 80, 0.5)),
        ("DDthr 4% m0.5", lambda e, p, h, t: dd_throttle(e, p, h, t, 4.0, 0.5)),
        ("DDthr 6% m0.5", lambda e, p, h, t: dd_throttle(e, p, h, t, 6.0, 0.5)),
        ("DDthr 4% m0.0", lambda e, p, h, t: dd_throttle(e, p, h, t, 4.0, 0.0)),
    ]
    print(f"  {'overlay':<16} | {'年率':>7} {'maxDD':>6} {'MAR':>5} | {'sumR':>6} | "
          f"{'P1':>6}({'dd':>4}) {'P2':>6}({'dd':>4})")
    for name, ov in configs:
        # baseline は 1% スケール; DD で正規化するため maxDD と sumR をそのまま見る
        m = eval_overlay(tr, ov)
        print(f"  {name:<16} | {m['ann']:>+6.1f}% {m['dd']:>5.1f}% {m['mar']:>5.1f} | {m['total']:>+6.1f} | "
              f"{m['p1']:>+6.1f}({m['dd1']:>4.1f}) {m['p2']:>+6.1f}({m['dd2']:>4.1f})", flush=True)
    print("\n注: MAR が baseline より明確に高く、かつ P1/P2 両方で DD が減れば採用候補。")


if __name__ == "__main__":
    main()
