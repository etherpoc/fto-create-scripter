"""
verify_eriot3.py — YouTube手法「エリオット第3波 EMA戦略」(highlevefx-eriot.md)の再現性検証。

機械化したコアルール (4H足):
  S1: 20EMA × 200EMA クロス。GC→ロング目線 / DC→ショート目線。クロス後「最初の1発のみ」。
  S2: クロス確定後、価格が 20EMA にタッチ(ヒゲ可)した足 = 「基準足」。
  S3: 基準足の高値(L)/安値(S)を、後続足の【実体(終値)】が抜けた瞬間にエントリー。
  SL: 2波の押し安値/戻り高値(クロス〜エントリーの逆方向extreme)の少し外側。
  TP: 固定RR (1.0/1.5/2.0/3.0) を sweep。+ 1波=3波の値幅投影(W1проекция)も別途。

裁量フィルタ(レンジ回避/RR選別/日足同期/分割利確)はコード化不能 → コアの素の期待値を測る。
net = 往復コミ + spread0.5p (breakout.py と同一コスト)。先読み無し(確定足のみ)。

  python tools/verify_eriot3.py
  python tools/verify_eriot3.py --all --detail
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.axiory_data import cached_arrays  # noqa: E402

MAJORS = ["XAUUSD", "EURUSD", "USDJPY", "GBPJPY"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()  # IS 2015-20 / OOS 2021-26
SPREAD = 0.5


def pip(p): return 0.01 if "JPY" in p else (0.1 if "XAU" in p else 0.0001)
def comm(p):
    if p.endswith("JPY"): return 1.8
    if p.startswith("USD"): return 1.5
    if "XAU" in p: return 0.5
    return 1.2


def ema(arr, n):
    a = 2.0 / (n + 1)
    out = np.empty(len(arr))
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = a * arr[i] + (1 - a) * out[i - 1]
    return out


def atr(h, l, c, n=14):
    tr = np.empty(len(c)); tr[0] = h[0] - l[0]
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = np.full(len(c), np.nan)
    out[n] = tr[1:n + 1].mean()
    for i in range(n + 1, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def find_setups(t, o, h, l, c, sl_buf_atr=0.1):
    """ルール通りに setup → entry を抽出。各 trade: dict(side, i_entry, entry, sl, sl_dist)。"""
    e20, e200 = ema(c, 20), ema(c, 200)
    a = atr(h, l, c, 14)
    n = len(c)
    trades = []
    warm = 205
    i = warm
    while i < n - 1:
        # クロス検出(確定足 i-1 vs i)
        gc = e20[i - 1] <= e200[i - 1] and e20[i] > e200[i]
        dc = e20[i - 1] >= e200[i - 1] and e20[i] < e200[i]
        if not (gc or dc):
            i += 1
            continue
        side = "long" if gc else "short"
        cross_i = i
        # S2: クロス後、最初に 20EMA タッチした足を探す(基準足)
        j = i + 1
        ref = None
        while j < n - 1:
            # 反対クロスが先に起きたら setup 破棄(レンジ/転換)
            if (side == "long" and e20[j] < e200[j]) or (side == "short" and e20[j] > e200[j]):
                break
            if l[j] <= e20[j] <= h[j]:   # 20EMA タッチ(ヒゲ可)
                ref = j
                break
            j += 1
        if ref is None:
            i = cross_i + 1
            continue
        ref_hi, ref_lo = h[ref], l[ref]
        # S3: 基準足を実体(終値)で抜けたらエントリー
        m = ref + 1
        entry = None
        while m < n:
            # 反対クロスで破棄
            if (side == "long" and e20[m] < e200[m]) or (side == "short" and e20[m] > e200[m]):
                break
            if side == "long" and c[m] > ref_hi:
                entry = ("long", m, c[m]); break
            if side == "short" and c[m] < ref_lo:
                entry = ("short", m, c[m]); break
            m += 1
        if entry is None:
            i = cross_i + 1
            continue
        sd, ei, ep = entry
        # SL: クロス〜エントリーの押し安値/戻り高値の少し外側
        seg_lo = l[cross_i:ei + 1].min()
        seg_hi = h[cross_i:ei + 1].max()
        buf = sl_buf_atr * (a[ei] if not np.isnan(a[ei]) else (h[ei] - l[ei]))
        if sd == "long":
            sl = seg_lo - buf
        else:
            sl = seg_hi + buf
        sl_dist = abs(ep - sl)
        if sl_dist <= 0:
            i = cross_i + 1
            continue
        trades.append(dict(side=sd, i=ei, entry=ep, sl=sl, sl_dist=sl_dist, t=t[ei]))
        # 「最初の1発のみ」→ 次のクロスまでスキップ。エントリー足以降から再走査
        i = ei + 1
    return trades, e20, e200


def simulate_rr(t, o, h, l, c, trades, rr):
    """各 trade を固定RRで決済。SL/TP同足はSL優先(保守)。R(±)を返す。"""
    n = len(c)
    out = []
    for tr in trades:
        side, ei, ep, sl, sd = tr["side"], tr["i"], tr["entry"], tr["sl"], tr["sl_dist"]
        tp = ep + rr * sd if side == "long" else ep - rr * sd
        res = None
        for k in range(ei + 1, n):
            if side == "long":
                if l[k] <= sl: res = -1.0; break
                if h[k] >= tp: res = rr; break
            else:
                if h[k] >= sl: res = -1.0; break
                if l[k] <= tp: res = rr; break
        if res is None:
            # 未決済 → 最終足の含損益をRで
            last = c[-1]
            res = ((last - ep) if side == "long" else (ep - last)) / sd
        out.append(dict(R=res, sl_dist=sd, t=t[ei], side=side))
    return out


def evaluate(pairs, rr, detail=False):
    grand = {"n": 0, "g": 0.0, "net": 0.0, "w": 0, "n1": 0.0, "n2": 0.0,
             "c1": 0, "c2": 0, "w1": 0, "w2": 0}
    rows = []
    for sym in pairs:
        t, o, h, l, c = cached_arrays(sym, "h4")
        trades, _, _ = find_setups(t, o, h, l, c)
        sims = simulate_rr(t, o, h, l, c, trades, rr)
        ps = pip(sym); cost = comm(sym) + SPREAD
        n = len(sims)
        if n == 0:
            rows.append((sym, 0, 0, 0, 0)); continue
        net = 0.0; w = 0; n1 = n2 = 0.0; c1 = c2 = w1 = w2 = 0
        for s in sims:
            nr = s["R"] - cost / max(s["sl_dist"] / ps, 1e-9)
            net += nr
            w += s["R"] > 0
            if s["t"] < P1_END:
                n1 += nr; c1 += 1; w1 += s["R"] > 0
            else:
                n2 += nr; c2 += 1; w2 += s["R"] > 0
        rows.append((sym, n, 100 * w / n, net, (n1, n2, c1, c2)))
        grand["n"] += n; grand["net"] += net; grand["w"] += w
        grand["n1"] += n1; grand["n2"] += n2
        grand["c1"] += c1; grand["c2"] += c2; grand["w1"] += w1; grand["w2"] += w2
    return rows, grand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else MAJORS
    tag = "全12" if args.all else "主要4(XAU/EUR/USDJPY/GBPJPY)"

    print("=" * 92)
    print(f"エリオット第3波 EMA戦略 検証 ({tag}, H4, 2015-2026)  net=往復コミ+spread0.5p")
    print("IS=2015-20 / OOS=2021-26。SL=2波extreme±0.1ATR。「クロス後1発のみ」厳守。")
    print("=" * 92)
    print(f"  {'RR':>4} | {'N':>4} {'WR':>5} {'期待値':>8} | {'net合計R':>9} | {'IS net':>8} {'OOS net':>8} | {'年間本数':>7}")
    print("-" * 92)
    YEARS = 11.33
    best = None
    for rr in [1.0, 1.5, 2.0, 3.0]:
        rows, g = evaluate(pairs, rr)
        if g["n"] == 0:
            print(f"  {rr:>4} | 0 trades"); continue
        wr = 100 * g["w"] / g["n"]
        ev = g["net"] / g["n"]  # 1トレード平均net R
        print(f"  1:{rr:>2.0f} | {g['n']:>4} {wr:>4.0f}% {ev:>+7.3f}R | {g['net']:>+8.1f}R | "
              f"{g['n1']:>+7.1f} {g['n2']:>+7.1f} | {g['n']/YEARS:>6.1f}/年")
        if best is None or g["net"] > best[1]:
            best = (rr, g["net"], rows, g)
    print("-" * 92)
    print("net合計R: 1R=口座1%риスク想定の合計損益。OOS net が安定プラスなら再現性あり。")

    if args.detail and best:
        rr, _, rows, g = best
        print(f"\n--- ペア別内訳 (best RR=1:{rr:.0f}) ---")
        print(f"  {'sym':<8} {'N':>4} {'WR':>5} {'net R':>8} | {'IS(n)':>10} {'OOS(n)':>10}")
        for (sym, n, wr, net, sub) in rows:
            if n == 0:
                print(f"  {sym:<8} 0"); continue
            n1, n2, c1, c2 = sub
            print(f"  {sym:<8} {n:>4} {wr:>4.0f}% {net:>+7.1f}R | "
                  f"{n1:>+6.1f}({c1:>3}) {n2:>+6.1f}({c2:>3})")
        print(f"\n  IS WR={100*g['w1']/max(g['c1'],1):.0f}% ({g['c1']}本)  "
              f"OOS WR={100*g['w2']/max(g['c2'],1):.0f}% ({g['c2']}本)")


if __name__ == "__main__":
    main()
