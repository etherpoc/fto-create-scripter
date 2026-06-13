"""
target_feasibility.py — 目標(月利6-8% / 最大DD<=10%)を満たすのに必要な
「勝率 × 月間トレード数」の関係を定量化する。

考え方:
  1トレード = リスク f(口座比), RR=b, 勝率 w, 片道コスト cost_R(R単位)。
  R_i = +b (勝, 確率 w) / -1 (負, 確率 1-w)。期待値 mu = w*b-(1-w)-cost_R。
  複利(fixed-fraction)では log 資産 ~= f * cumsum(R) なので、
  最大DD% ~= f * MaxDD_R   (MaxDD_R = R累積ウォークの最大DD, f に依存しない)。
  => DD を上限 DDcap に固定したときの最大リスク f* = DDcap / MaxDD_R。
  => そのときの月次リターン = N * mu * f*  = DDcap * (N*mu / MaxDD_R)。

MaxDD_R はモンテカルロで分布を出し、median(典型) と p95(安全側=プロップ失格回避) を使う。
独立トレード仮定 = DD は楽観値。実戦は相関/クラスタで悪化する点に注意(出力末尾に明記)。
"""
from __future__ import annotations
import numpy as np

rng = np.random.default_rng(12345)

DDCAP = 0.10          # 最大DD上限
T_MONTHS = 24         # DD評価ホライズン(2年=WF相当)
M = 2500              # モンテカルロ試行数
F_CAP = 0.05          # 1トレードriskの現実上限(5%)


def maxdd_R(w: float, b: float, cost_R: float, n_trades: int, m: int = M):
    """R累積ウォークの最大DD(R単位)の median と p95 を返す。"""
    if n_trades <= 0:
        return 0.0, 0.0
    # 各試行 n_trades 本の R を生成
    wins = rng.random((m, n_trades)) < w
    R = np.where(wins, b, -1.0) - cost_R
    eq = np.cumsum(R, axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    dd = peak - eq
    mdd = dd.max(axis=1)
    return float(np.median(mdd)), float(np.percentile(mdd, 95))


def monthly_return(w: float, b: float, cost_R: float, N: int, pct: str = "p95"):
    """DD=DDCAP に固定したときの月次リターン(%)。pct: 'median' or 'p95'."""
    mu = w * b - (1 - w) - cost_R
    if mu <= 0:
        return None  # エッジなし=不可能
    n_total = int(round(N * T_MONTHS))
    med, p95 = maxdd_R(w, b, cost_R, n_total)
    mdd = p95 if pct == "p95" else med
    if mdd <= 0:
        return None
    f = min(DDCAP / mdd, F_CAP)
    capped = f >= F_CAP
    ret = N * mu * f * 100.0
    return ret, f, mu, capped


def table(b: float, cost_R: float, pct: str):
    ws = [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    Ns = [4, 8, 15, 30, 60, 100, 200]
    print(f"\n{'='*86}")
    print(f"RR={b}  片道コスト={cost_R}R  DD上限={DDCAP*100:.0f}%({pct})  ホライズン={T_MONTHS}ヶ月")
    print(f"{'='*86}")
    print("  break-even WR = " + f"{1/(1+b)+cost_R/(1+b):.1%}" +
          f"  (RR{b}+コスト{cost_R}R を超えないとエッジ無し)")
    head = "  WR \\ N/月 |" + "".join(f"{n:>9}" for n in Ns)
    print(head)
    print("  " + "-" * (len(head)-2))
    for w in ws:
        row = f"   {w*100:>4.0f}%    |"
        for N in Ns:
            r = monthly_return(w, b, cost_R, N, pct)
            if r is None:
                cell = "   --"
            else:
                ret, f, mu, capped = r
                mark = "*" if capped else ("#" if ret >= 6.0 else " ")
                cell = f"{ret:>7.1f}{mark}"
            row += f"{cell:>9}"
        print(row)
    print("  (# = 月利>=6%達成,  * = risk上限5%に張り付き=DD制約が緩い領域,  -- = エッジ無し)")


_NGRID = [2, 4, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100,
          125, 150, 200, 250, 300, 400, 500, 750, 1000]


def frontier(b: float, cost_R: float, target: float, pct: str):
    """月利 target% を満たす (WR, 必要N/月) の最小Nフロンティアを出す(coarse grid)。"""
    print(f"\n  ▼ RR={b} cost={cost_R}R で月利{target:.0f}%に必要な最小N/月 ({pct}):", flush=True)
    for w in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        need = None
        for N in _NGRID:
            r = monthly_return(w, b, cost_R, N, pct)
            if r and r[0] >= target:
                need = N
                break
        s = f"~{need}本/月" if need else f">{_NGRID[-1]}本/月(非現実的)"
        mu = w * b - (1 - w) - cost_R
        edge = f"μ={mu:+.3f}R" if mu > 0 else "エッジ無し"
        print(f"     WR {w*100:>3.0f}%  ({edge}): {s}", flush=True)


if __name__ == "__main__":
    print("目標 月利6-8% / 最大DD<=10% の到達に必要な 勝率×トレード数")
    print("(独立トレード仮定 = DDは楽観。実戦は相関で悪化)")

    for pct in ["median", "p95"]:
        print(f"\n\n########## DD評価 = {pct} ##########")
        for b in [1.0, 1.5, 2.0]:
            table(b, cost_R=0.10, pct=pct)

    print(f"\n\n{'#'*86}")
    print("# 月利7%を満たす (WR, 必要トレード数/月) フロンティア")
    print(f"{'#'*86}")
    for pct in ["median", "p95"]:
        print(f"\n===== DD={pct} =====")
        for b in [1.0, 1.5, 2.0]:
            frontier(b, cost_R=0.10, target=7.0, pct=pct)

    # gross(コスト0)との比較で「コストがいかに高N戦略を殺すか」
    print(f"\n\n{'#'*86}")
    print("# コスト感応: 月利7% / DD10%(p95) / RR1.5 で 必要N がコストでどう変わるか")
    print(f"{'#'*86}")
    for cost in [0.0, 0.05, 0.10, 0.20]:
        frontier(1.5, cost_R=cost, target=7.0, pct="p95")
