"""
overlay_validate.py — エクイティカーブ・オーバーレイの頑健性/forward検証。
MT5 実装前の最終チェック。以下を確認する:

  1) 年次ごとに DD 削減 / MAR 改善が一貫するか (一度の幸運でないか)
  2) ペア単体でも効くか (バスケット集計の偶然でないか)
  3) シャッフル対照: トレード順をシャッフルして DD クラスタを壊すと効果が消えるか
     (消える=本物=連続DDを捉えている / 残る=単なる機械的デレバ=再レバで無意味)
  4) MT5 実装可能な版(口座エクイティの暦日MA throttle)が proven 版(trade-MA)と同等か

overlay は全て causal (過去の実現益のみ)。1R=口座1%/pair。net WF: P1=21-23 / P2=24-26。
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
SPREAD = 0.5
BO_LONG = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150, direction="long")
DAY = 86400.0


def pair_trades(s, cfg=BO_LONG, tf="h1"):
    tr = run_bo_fast(cached_arrays(s, tf), cfg)
    if not tr:
        return []
    c = bcomm(s) + SPREAD; ps = bpip(s)
    return [(t["t"], t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)) for t in tr]


def viable_basket(cfg=BO_LONG, tf="h1"):
    out = []
    for s in ALL:
        nrs = pair_trades(s, cfg, tf)
        if not nrs:
            continue
        n1 = sum(r for (tt, r) in nrs if tt < P1_END)
        n2 = sum(r for (tt, r) in nrs if tt >= P1_END)
        if n1 > 0 and n2 > 0:
            out += nrs
    out.sort(key=lambda x: x[0])
    return out


# ---------- overlay 各種 (causal) ----------
def ov_none(trades):
    return list(trades)


def ov_trade_ma(trades, K=20, m=0.5):
    """trade列の実現エクイティが直近K件MAを割ったら mult=m。"""
    trades = sorted(trades, key=lambda x: x[0])
    eq = 0.0; hist = []; out = []
    for (t, r) in trades:
        mult = 1.0
        if len(hist) >= K:
            if eq < sum(hist[-K:]) / K:
                mult = m
        p = r * mult; eq += p; hist.append(eq); out.append((t, p))
    return out


def ov_cal_ma(trades, days=60, m=0.5):
    """口座エクイティの『暦日MA』を割ったら mult=m。MT5で各EAが口座エクイティから同一計算可能な版。
    実現エクイティを暦日でステップ化し、各トレード直前時点で過去 days 日平均と比較。"""
    trades = sorted(trades, key=lambda x: x[0])
    eq = 0.0
    daily = []  # (day, eq_at_end_of_that_day) を時系列で
    out = []
    for (t, r) in trades:
        day = t // DAY
        # トレード直前の過去 days 日平均(その日より前の確定エクイティ)
        mult = 1.0
        if daily:
            cutoff = day - days
            window = [e for (d, e) in daily if d >= cutoff]
            if len(window) >= max(5, days // 3):
                ma = sum(window) / len(window)
                if eq < ma:
                    mult = m
        p = r * mult; eq += p
        out.append((t, p))
        # その日の終端エクイティを記録(同日複数なら上書き)
        if daily and daily[-1][0] == day:
            daily[-1] = (day, eq)
        else:
            daily.append((day, eq))
    return out


def ov_dd_throttle(trades, D=4.0, m=0.5):
    """ピークからの DD が D(=口座%, 1R=1%基準) を超えたら mult=m。最も単純な口座DD版。"""
    trades = sorted(trades, key=lambda x: x[0])
    eq = 0.0; peak = 0.0; out = []
    for (t, r) in trades:
        mult = m if (peak - eq) > D else 1.0
        p = r * mult; eq += p; peak = max(peak, eq); out.append((t, p))
    return out


def stats(trades):
    if not trades:
        return dict(ann=0, dd=0, mar=0, total=0)
    t = np.array([x[0] for x in trades]); p = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; p = p[o]
    eq = np.cumsum(p); peak = np.maximum.accumulate(eq)
    dd = float((peak - eq).max()); total = float(eq[-1])
    months = (t[-1] - t[0]) / DAY / 30.44
    monthly = total / months if months > 0 else 0
    ann = ((1 + monthly / 100) ** 12 - 1) * 100 if monthly > -100 else -100
    return dict(ann=ann, dd=dd, mar=(ann / dd if dd > 0 else 0), total=total)


def year_dd(trades):
    by = {}
    for (t, r) in trades:
        y = datetime.fromtimestamp(t, tz=timezone.utc).year
        by.setdefault(y, []).append((t, r))
    res = {}
    for y, tr in by.items():
        tr.sort(); p = np.array([r for (_, r) in tr]); eq = np.cumsum(p)
        pk = np.maximum.accumulate(eq); res[y] = (float(eq[-1]), float((pk - eq).max()))
    return res


def main():
    print("=" * 100)
    print("オーバーレイ頑健性/forward検証 (BO H1-long basket, net, 1R=1%/pair)")
    print("=" * 100)
    bk = viable_basket()
    print(f"basket trades N={len(bk)}")

    ovs = [("baseline", ov_none),
           ("trade-MA K20 m0.5", ov_trade_ma),
           ("cal-MA 60d m0.5 (MT5実装版)", ov_cal_ma),
           ("DDthr 4% m0.5 (口座DD単純)", ov_dd_throttle)]

    print("\n■ 全体 (年率/maxDD/MAR/sumR)")
    base_mar = None
    for name, ov in ovs:
        s = stats(ov(bk))
        if name == "baseline": base_mar = s["mar"]
        print(f"  {name:<30} ann{s['ann']:>+7.1f}% DD{s['dd']:>5.1f}% MAR{s['mar']:>5.1f} sumR{s['total']:>+7.1f}")

    print("\n■ 年別 sumR(DD) — DD削減が毎年効くか")
    print(f"  {'overlay':<30}" + "".join(f"{y:>13}" for y in range(2021, 2027)))
    for name, ov in ovs:
        yd = year_dd(ov(bk))
        row = f"  {name:<30}"
        for y in range(2021, 2027):
            if y in yd:
                row += f" {yd[y][0]:>+5.0f}({yd[y][1]:>4.1f})"
            else:
                row += f"{'—':>13}"
        print(row)

    print("\n■ ペア単体: baseline MAR → trade-MA overlay MAR (各ペアで改善するか)")
    imp = 0; tot = 0
    for s in ALL:
        nrs = pair_trades(s)
        if not nrs:
            continue
        n1 = sum(r for (tt, r) in nrs if tt < P1_END); n2 = sum(r for (tt, r) in nrs if tt >= P1_END)
        if not (n1 > 0 and n2 > 0):
            continue
        tot += 1
        b = stats(ov_none(nrs))["mar"]; a = stats(ov_trade_ma(nrs))["mar"]
        if a > b: imp += 1
        print(f"  {s:<8} MAR {b:>5.1f} → {a:>5.1f}  {'✅' if a > b else '✗'}")
    print(f"  → {imp}/{tot} ペアで MAR 改善")

    print("\n■ シャッフル対照: トレード順をシャッフルしDDクラスタを壊すと overlay 効果は消えるか")
    print("  (消える=本物=連続DDを捉えている / 残る=機械的デレバ=再レバ無意味)")
    rng = np.random.default_rng(42)
    rs = [r for (_, r) in bk]
    base_real = stats(ov_trade_ma(bk))["mar"]
    shuf_mars = []
    for _ in range(30):
        perm = rng.permutation(len(rs))
        # 時刻は元の昇順を維持し、R だけ並べ替え(順序効果のみ壊す)
        fake = [(bk[i][0], rs[perm[i]]) for i in range(len(rs))]
        shuf_mars.append(stats(ov_trade_ma(fake))["mar"])
    base_shuf = []
    for _ in range(30):
        perm = rng.permutation(len(rs))
        fake = [(bk[i][0], rs[perm[i]]) for i in range(len(rs))]
        base_shuf.append(stats(ov_none(fake))["mar"])
    print(f"  実データ: baseline MAR {base_mar:.2f} → overlay MAR {base_real:.2f}  (改善 {base_real-base_mar:+.2f})")
    print(f"  シャッフル平均: baseline MAR {np.mean(base_shuf):.2f} → overlay MAR {np.mean(shuf_mars):.2f}  "
          f"(改善 {np.mean(shuf_mars)-np.mean(base_shuf):+.2f})")
    print(f"  → 実データの改善がシャッフルより明確に大きければ『連続DDを捉える本物のエッジ』")


if __name__ == "__main__":
    main()
