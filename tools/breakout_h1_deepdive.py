"""
breakout_h1_deepdive.py — H1 ブレイクアウト・バスケットの深掘り。
核心: コスト感応度(スプレッド変動)。H1はSL細い×トレード多い → 実コストで崩れないか。
+ 年次一貫性・合成DD・プロップ(DD<10%)サイジング。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.bo_fast import cached_arrays, run_bo_fast  # noqa
from tools.breakout_lab import pip, comm, ALL  # noqa

P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
CONFIGS = {
    "H1 en30/ex25/SL2/SMA150": dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=2.0, sma_n=150, direction="long"),
    "H1 en30/ex25/SL2/SMA100": dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=2.0, sma_n=100, direction="long"),
    "H1 en30/ex25/SL3/SMA150": dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150, direction="long"),
}


def run_all(P):
    per = {}
    for sym in ALL:
        tr = run_bo_fast(cached_arrays(sym, "h1"), P)
        for t in tr:
            t["sym"] = sym; t["sl_pips"] = t["sld"] / pip(sym)
        per[sym] = tr
    return per


def net_basket(per, spread, viable):
    """指定 spread での viable バスケット trades(net付き)。"""
    out = []
    for s in viable:
        c = comm(s) + spread
        for t in per[s]:
            t2 = dict(t); t2["nR"] = t["R"] - c * t["units"] / max(t["sl_pips"], 1e-9)
            out.append(t2)
    return out


def split(tr):
    return sum(t["nR"] for t in tr if t["t"] < P1_END), sum(t["nR"] for t in tr if t["t"] >= P1_END)


def dd_of(tr, risk):
    ev = sorted(tr, key=lambda x: x["t"]); eq = peak = dd = 0.0
    for t in ev:
        eq += t["nR"] * risk; peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def main():
    for cname, P in CONFIGS.items():
        per = run_all(P)
        # viable は spread0.5 基準で確定
        v = []
        for s in ALL:
            c = comm(s) + 0.5
            n1 = sum(t["R"] - c*t["units"]/max(t["sld"]/pip(s),1e-9) for t in per[s] if t["t"] < P1_END)
            n2 = sum(t["R"] - c*t["units"]/max(t["sld"]/pip(s),1e-9) for t in per[s] if t["t"] >= P1_END)
            if n1 > 0 and n2 > 0: v.append(s)
        n = sum(len(per[s]) for s in v)
        print("=" * 84)
        print(f"■ {cname}  viable={v}  (basket {n}トレード)")
        print("=" * 84)
        # コスト感応度
        print(f"  {'spread':>7} | {'P1/月':>7} {'P2/月':>7} | {'合成DD(1%)':>10} | 判定")
        for sp in [0.5, 1.0, 1.5, 2.0, 3.0]:
            bt = net_basket(per, sp, v); p1, p2 = split(bt)
            _, dd = dd_of(bt, 1.0)
            ok = "両期間+" if (p1 > 0 and p2 > 0) else ("P2のみ+" if p2 > 0 else "崩壊")
            print(f"  +{sp:>4.1f}p | {p1/36:>+6.2f}% {p2/30:>+6.2f}% | {dd:>9.1f}% | {ok}")
        # 年次 + サイジング (spread1.0=やや保守的)
        bt = net_basket(per, 1.0, v)
        years = {}
        for t in bt:
            y = datetime.fromtimestamp(t["t"], tz=timezone.utc).year
            years[y] = years.get(y, 0.0) + t["nR"]
        print(f"  年別(spread1.0p,risk1%): " + " ".join(f"{y}:{years[y]:+.0f}" for y in sorted(years)) +
              f"  (マイナス年 {[y for y in years if years[y]<0] or 'なし'})")
        print(f"  {'risk/pair':>9} | {'年複利':>7} | {'合成DD':>7} | DD<10%  (spread1.0p)")
        for risk in [0.15, 0.25, 0.35, 0.5]:
            eq, dd = dd_of(bt, risk)
            yr = ((1 + (eq/66)/100) ** 12 - 1) * 100
            print(f"  {risk:>7.2f}% | {yr:>+6.1f}% | {dd:>6.1f}% | {'OK' if dd<10 else '--'}")
        print()


if __name__ == "__main__":
    main()
