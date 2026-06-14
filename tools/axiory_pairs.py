"""
axiory_pairs.py — クロスペア統計裁定(ペアトレード)を実Axiory 11年で検証。

狙い: 方向に賭けない市場ニュートラル戦略 → トレンドbreakoutと**構造的に無相関**の新エッジを探す。
高相関ペア(A,B)の log スプレッド = logA - β·logB を z-score 化し、|z|>entry で平均回帰を逆張り、z→0 で利確。

R 正規化: stop=stop_z, entry=entry_z → 1R = (stop_z-entry_z)·std。target(z=0)で +entry_z/(stop_z-entry_z) R。
net: 2レグぶんのコスト(往復コミ+spread)を log 換算で控除。WF: OOS=2015-20/IS=2021-26。breakout との月次相関も。

  python tools/axiory_pairs.py
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
from tools.axiory_validate import pair_net as bo_net, BREAKOUT5  # noqa: E402

SPREAD = 0.5
SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
# 高相関ペア候補(同系統=平均回帰しやすい)
CANDIDATES = [
    ("AUDUSD", "NZDUSD"), ("EURUSD", "GBPUSD"), ("EURJPY", "GBPJPY"),
    ("AUDJPY", "NZDJPY"), ("USDCHF", "EURUSD"), ("EURUSD", "USDCHF"),
    ("CADJPY", "AUDJPY"), ("EURJPY", "CHFJPY"), ("GBPUSD", "EURGBP"),
]
W = 120          # z-score 窓(H1本)
ENTRY_Z = 2.0
STOP_Z = 3.5
MAX_HOLD = 200   # 最大保有(H1本)。タイムアウト


def aligned(A, B):
    ta, _, _, _, ca = ax.cached_arrays(A, "h1")
    tb, _, _, _, cb = ax.cached_arrays(B, "h1")
    common = np.intersect1d(ta, tb)
    ia = np.searchsorted(ta, common); ib = np.searchsorted(tb, common)
    return common, ca[ia], cb[ib]


def leg_cost_log(sym, price):
    # 片道コスト(pips)→log換算 ≈ cost_pips*pip / price
    return (bcomm(sym) + SPREAD) * bpip(sym) / price


def pairs_trade(A, B):
    t, a, b = aligned(A, B)
    if len(t) < W + 10:
        return []
    la = np.log(a); lb = np.log(b)
    spread = la - lb                     # β=1(log比)
    s = pd.Series(spread)
    mean = s.rolling(W).mean().values
    std = s.rolling(W).std().values
    z = (spread - mean) / std
    trades = []
    pos = 0; ent_i = 0; ent_spread = 0.0; ent_std = 0.0
    for i in range(W + 1, len(t)):
        if not (std[i] > 0):
            continue
        if pos == 0:
            if z[i] > ENTRY_Z and z[i - 1] <= ENTRY_Z:
                pos = -1; ent_i = i; ent_spread = spread[i]; ent_std = std[i]   # スプレッド売り(A売B買)
            elif z[i] < -ENTRY_Z and z[i - 1] >= -ENTRY_Z:
                pos = 1; ent_i = i; ent_spread = spread[i]; ent_std = std[i]    # スプレッド買い(A買B売)
        else:
            exit_now = (z[i] * pos <= 0) or (abs(z[i]) > STOP_Z) or (i - ent_i >= MAX_HOLD)
            if exit_now:
                move = (spread[i] - ent_spread) * pos          # 平均回帰で得た log 幅
                risk = (STOP_Z - ENTRY_Z) * ent_std            # 1R(log)
                cost = leg_cost_log(A, a[i]) + leg_cost_log(B, b[i])  # 2レグ
                nR = (move - cost) / max(risk, 1e-12)
                trades.append((int(t[i]), nR))
                pos = 0
    return trades


def monthly(trades):
    m = {}
    for (t, r) in trades:
        k = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m"); m[k] = m.get(k, 0.0) + r
    return m


def corr(a, b):
    ks = sorted(set(a) | set(b)); x = np.array([a.get(k, 0.0) for k in ks]); y = np.array([b.get(k, 0.0) for k in ks])
    return float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else 0.0


def stat(trades):
    if not trades:
        return None
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    w = int((r > 0).sum())
    p1 = float(r[t < SPLIT].sum()); p2 = float(r[t >= SPLIT].sum())
    dd = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    return len(r), 100 * w / len(r), p1, p2, dd


def main():
    print("=" * 104)
    print("クロスペア統計裁定(ペアトレード) @ 実Axiory 11年 — トレンドと無相関の新エッジ探索")
    print(f"窓{W} entry|z|>{ENTRY_Z} stop{STOP_Z} 最大保有{MAX_HOLD}本, net(2レグ), WF OOS/IS")
    print("=" * 104)
    allt = []
    for (A, B) in CANDIDATES:
        tr = pairs_trade(A, B)
        s = stat(tr)
        if not s:
            print(f"  {A}-{B:<8} | 0"); continue
        n, wr, p1, p2, dd = s
        rob = "✅両+" if (p1 > 0 and p2 > 0) else ("△IS+" if p2 > 0 else ("△OOS+" if p1 > 0 else "✗"))
        print(f"  {A}-{B:<8} | N{n:>4} WR{wr:>4.1f}% | OOS{p1:>+6.1f} IS{p2:>+6.1f} DD{dd:>5.1f} | {rob}")
        allt += tr
    s = stat(allt)
    if s:
        n, wr, p1, p2, dd = s
        print("-" * 104)
        print(f"  ★合算       | N{n:>4} WR{wr:>4.1f}% | OOS{p1:>+6.1f} IS{p2:>+6.1f} DD{dd:>5.1f}")
        # breakout との相関
        bo = []
        for p in BREAKOUT5:
            bo += bo_net(p)
        print(f"  ★ breakout との月次相関: {corr(monthly(allt), monthly(bo)):+.2f} (0付近なら無相関=価値あり)")
        print(f"  判定: 両期間net+ かつ 無相関(|corr|<0.3) なら新エッジ候補。net負け/相関ありなら不採用。")


if __name__ == "__main__":
    main()
