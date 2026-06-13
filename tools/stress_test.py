"""
stress_test.py — overlay再レバ portfolio(月+4.4%/DD10%)の尾部/レジーム反転リスクを定量化。

再レバ判断の核心: 2021-26 には金高/円安の大反転が無い=盲点。overlay は『半減・反応lag』なので
急反転の第一撃は full size で食らう。以下を測る:
  1) 採用サイズでの maxDD と 最悪 1/2/3/6ヶ月窓(実測の尾部)
  2) 悪月クラスタ境界: 最悪K月が連続したら DD いくつ(レジーム反転クラスタの素朴上限)
  3) overlay の反応lag: 最大DD局面で『overlay作動前(full size)』に食らった損失割合
  4) 2026 canary(レジーム減衰の実測) + 反転シミュ(直近トレンドを反転させた block を接ぐ)

再レバ採用サイズ(portfolio_lab MAR最大→DD10%): BO_H1_long 0.44% / BO_H4_long 0.33% / short 0.05% /pair。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.portfolio_lab import _bo_stream, _eq_overlay, BO_LONG, BO_LONG_H4, BO_SHORT  # noqa

SIZING = {"BO_H1_long": 0.44, "BO_H4_long": 0.33, "BO_H1_short": 0.05}


def build_portfolio():
    streams = {
        "BO_H1_long": _bo_stream(BO_LONG, "h1", overlay=True),
        "BO_H4_long": _bo_stream(BO_LONG_H4, "h4", overlay=True),
        "BO_H1_short": _bo_stream(BO_SHORT, "h1", viable_only=False),
    }
    trades = []
    for name, w in SIZING.items():
        for (t, r) in streams[name]:
            trades.append((t, r * w))   # pnl in %(1R=1%/pair × w)
    trades.sort(key=lambda x: x[0])
    return trades, streams


def maxdd(pnls):
    eq = np.cumsum(pnls); peak = np.maximum.accumulate(eq)
    return float((peak - eq).max())


def monthly(trades):
    m = {}
    for (t, p) in trades:
        k = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")
        m[k] = m.get(k, 0.0) + p
    return m


def rolling_window_worst(mvals_sorted_keys, m, w):
    keys = mvals_sorted_keys
    worst = 0.0
    for i in range(len(keys) - w + 1):
        s = sum(m[keys[j]] for j in range(i, i + w))
        worst = min(worst, s)
    return worst


def main():
    trades, streams = build_portfolio()
    pnls = np.array([p for (_, p) in trades])
    times = np.array([t for (t, _) in trades])
    dd = maxdd(pnls)
    total = pnls.sum(); months = 66
    print("=" * 92)
    print("再レバ portfolio ストレステスト (BO_H1 0.44% + BO_H4 0.33% + short 0.05%/pair, overlay入り)")
    print("=" * 92)
    print(f"  実測: 累積 {total:+.1f}% / {months}ヶ月 / 月平均 {total/months:+.2f}% / maxDD {dd:.1f}%")

    m = monthly(trades); mk = sorted(m)
    print(f"\n■ 最悪窓(実測の尾部)")
    for w in (1, 2, 3, 6):
        print(f"    最悪{w}ヶ月窓: {rolling_window_worst(mk, m, w):+.1f}%")

    print(f"\n■ 悪月クラスタ境界(レジーム反転で最悪月が連続したらの素朴上限)")
    vals = sorted(m.values())
    for k in (3, 6, 12):
        print(f"    最悪{k}月の合計(=連続したらのDD): {sum(vals[:k]):+.1f}%")

    # overlay 反応lag: BO_H1 basket で full-size 期間の損失割合
    print(f"\n■ overlay 反応lag (BO_H1-long basket, 急反転の第一撃は full size)")
    bk = _bo_stream(BO_LONG, "h1", overlay=False)   # overlay前の生
    bk.sort(key=lambda x: x[0])
    # overlay mult 系列を再現
    eq = 0.0; hist = []; full_loss = 0.0; halved_loss = 0.0
    for (t, r) in bk:
        mult = 1.0
        if len(hist) >= 20 and eq < sum(hist[-20:]) / 20:
            mult = 0.5
        if r < 0:
            if mult == 1.0: full_loss += r
            else: halved_loss += r * mult
        p = r * mult; eq += p; hist.append(eq)
    print(f"    full-size中の負け合計 {full_loss:+.1f}R / overlay作動中の負け合計 {halved_loss:+.1f}R")
    print(f"    → 損失の {100*full_loss/(full_loss+halved_loss):.0f}% は overlay 作動前(full size)に発生 = lag の代償")

    # 年別 + 2026 canary
    print(f"\n■ 年別(レジーム減衰の canary)")
    yr = {}
    for (t, p) in trades:
        y = datetime.fromtimestamp(t, tz=timezone.utc).year; yr[y] = yr.get(y, 0.0) + p
    for y in sorted(yr):
        bar = "█" * max(0, int(yr[y] / 5))
        print(f"    {y}: {yr[y]:+6.1f}%  {bar}")
    print(f"    → 2026 は最弱。trend 減衰が継続/反転すれば overlay は『半減』止まりで赤字月クラスタの可能性。")

    print(f"\n■ 結論(再レバ判断への含意)")
    print(f"    ・実測DD 10% は『悪月が散らばる benign な 2021-26』が前提。最悪3月が連続(=レジーム反転の姿)なら")
    print(f"      DD -13.5%、6月連続なら -22% で 10%枠を突破。in-sample DD は反転耐性を保証しない。")
    print(f"    ・overlay は反応lag(損失の63%が full size 中)=急反転の第一撃は防げない『漸進的減衰向け』の保護。")
    print(f"    ・→ DD枠を埋め切る 0.44/0.33% への一括再レバは尾部過大。**段階再レバ**を推奨:")
    print(f"        まず採用5ペア・risk0.5% のまま overlay ON で DD を 9.4→~6% に下げ(安全を銀行)、")
    print(f"        数か月フォワードで DD 低下を実機確認 → その後 0.6-0.7% へ部分的に上げる(一気に枠上限へ行かない)。")


if __name__ == "__main__":
    main()
