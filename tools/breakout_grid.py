"""
breakout_grid.py — long-only ブレイクアウトの大規模グリッド探索(NumPy高速版)。

TF×entry_n×exit_n×sl_atr×sma_n を横断。各構成で全12ペアを net WF 評価し、
viable(両期間net+)ペアだけのバスケット net/DD を計算してランキング。

使い方:
    python tools/breakout_grid.py            # h1+h4 グリッド → top表示
    python tools/breakout_grid.py --tf h1
"""
from __future__ import annotations
import argparse, sys, itertools
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bo_fast import cached_arrays, run_bo_fast  # noqa
from tools.breakout_lab import pip, comm, SPREAD, ALL  # noqa

P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30


def basket_dd(trades, risk):
    ev = sorted(trades, key=lambda x: x["t"]); eq = peak = dd = 0.0
    for t in ev:
        eq += t["nR"] * risk; peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def eval_config(tf, P):
    per = {}; allt = []
    for sym in ALL:
        tr = run_bo_fast(cached_arrays(sym, tf), P)
        ps = pip(sym); c = comm(sym) + SPREAD
        for t in tr:
            t["nR"] = t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9); t["sym"] = sym
        if not tr: continue
        n1 = sum(t["nR"] for t in tr if t["t"] < P1_END)
        n2 = sum(t["nR"] for t in tr if t["t"] >= P1_END)
        per[sym] = (n1, n2, tr)
    viable = [s for s, (n1, n2, _) in per.items() if n1 > 0 and n2 > 0]
    bt = [t for s in viable for t in per[s][2]]
    if not bt:
        return None
    bp1 = sum(t["nR"] for t in bt if t["t"] < P1_END)
    bp2 = sum(t["nR"] for t in bt if t["t"] >= P1_END)
    _, dd = basket_dd(bt, 1.0)
    gold = sum(t["nR"] for t in bt if t["sym"] == "XAUUSD")
    tot = sum(t["nR"] for t in bt)
    return dict(nv=len(viable), viable=viable, p1=bp1, p2=bp2, dd=dd,
                golddep=100 * gold / tot if tot else 0, n=len(bt),
                rdd=(bp1 + bp2) / dd if dd > 0 else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="both", choices=["both", "h1", "h4"])
    args = ap.parse_args()

    grids = []
    tfs = ["h1", "h4"] if args.tf == "both" else [args.tf]
    for tf in tfs:
        if tf == "h1":
            ens = [30, 40, 50, 60, 80]; exs = [15, 25, 40]
        else:
            ens = [15, 20, 30, 40, 55]; exs = [10, 20, 30]
        for en, ex, sl, sm in itertools.product(ens, exs, [2.0, 3.0], [0, 100, 150, 200]):
            if ex >= en: continue
            grids.append((tf, dict(entry_n=en, exit_n=ex, atr_n=20, sl_atr=sl, sma_n=sm, direction="long")))

    print(f"探索 {len(grids)} 構成 (long-only)...", flush=True)
    res = []
    for tf, P in grids:
        r = eval_config(tf, P)
        if r: r.update(tf=tf, **P); res.append(r)

    # ランキング: viable>=4 かつ 両期間+ を、min(P1月,P2月) で評価
    good = [r for r in res if r["nv"] >= 4 and r["p1"] > 0 and r["p2"] > 0]
    good.sort(key=lambda r: min(r["p1"] / P1_M, r["p2"] / P2_M), reverse=True)

    print("\n" + "=" * 108)
    print(f"long-only ブレイクアウト グリッド top (viable≥4 & 両期間+)  {len(good)}/{len(res)} 構成が該当")
    print("=" * 108)
    print(f"  {'TF':>3} {'en':>3} {'ex':>3} {'SL':>3} {'SMA':>4} | {'viable':>6} | "
          f"{'P1/月':>6} {'P2/月':>6} | {'basketDD':>8} {'R/DD':>5} | {'金依存':>5} | viable銘柄")
    print("-" * 108)
    for r in good[:18]:
        print(f"  {r['tf']:>3} {r['entry_n']:>3} {r['exit_n']:>3} {r['sl_atr']:>3.0f} {r['sma_n']:>4} | "
              f"{r['nv']:>5}/12 | {r['p1']/P1_M:>+5.2f}% {r['p2']/P2_M:>+5.2f}% | "
              f"{r['dd']:>7.1f}% {r['rdd']:>4.1f} | {r['golddep']:>4.0f}% | {','.join(s[:6] for s in r['viable'])}")
    print("-" * 108)
    print("P1/P2月 = viableバスケットの月利(1%/pair)。basketDD = 合成DD(1%/pair)。R/DD=効率。")


if __name__ == "__main__":
    main()
