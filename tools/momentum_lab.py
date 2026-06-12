"""
momentum_lab.py — ①複数時間足ブレイクアウト ②MA交差(ゴールデンクロス)型 を検証。

同じ net(往復コミ+spread)・WF(P1/P2)・全12ペア枠で、
  - tf       : long-only Donchian を H1/H4/D1 で比較
  - macross  : 移動平均クロス(golden/death cross)を各種 fast/slow で比較

1R=口座1%。robust=両期間net+。

使い方:
    python tools/momentum_lab.py --mode tf
    python tools/momentum_lab.py --mode macross
    python tools/momentum_lab.py --mode macross_tf   # 良かったMAクロスをTF比較
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.breakout_lab import run_bo, atr_at, pip, comm, Bar, ALL, SPREAD, load_ticks  # noqa

P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30
_tfcache = {}


def extract_tf(sym, tf):
    key = (sym, tf)
    if key in _tfcache: return _tfcache[key]
    idx = {"h1": 1, "h4": 2, "d1": 2}[tf]
    seen = set(); raw = []
    for tup in load_ticks(sym):
        b = tup[idx]
        if b is None or b.time in seen: continue
        seen.add(b.time); raw.append(b)
    raw.sort(key=lambda x: x.time)
    if tf in ("h1", "h4"):
        out = [Bar(b.time, b.open, b.high, b.low, b.close) for b in raw]
    else:  # d1: UTC日付集約
        days = {}; order = []
        for b in raw:
            d = datetime.fromtimestamp(b.time, tz=timezone.utc).strftime("%Y-%m-%d")
            if d not in days:
                days[d] = [b.time, b.open, b.high, b.low, b.close]; order.append(d)
            else:
                r = days[d]; r[2] = max(r[2], b.high); r[3] = min(r[3], b.low); r[4] = b.close
        out = [Bar(*days[d]) for d in order]
    _tfcache[key] = out
    return out


def run_macross(bars, P):
    """移動平均クロス。golden cross=long, death cross=exit/short。SL=sl_atr×ATR。"""
    fn, sn, an = P["fast_n"], P["slow_n"], P.get("atr_n", 20)
    sl_atr = P["sl_atr"]; direction = P.get("direction", "both")
    closes = [b.close for b in bars]
    warm = sn + 2
    pos = None; trades = []

    def f(i): return sum(closes[i - fn:i]) / fn
    def s(i): return sum(closes[i - sn:i]) / sn

    for i in range(warm, len(bars)):
        b = bars[i]
        atr = atr_at(bars, i - 1, an)
        if not atr or atr <= 0: continue
        golden = f(i - 1) <= s(i - 1) and f(i) > s(i)
        death = f(i - 1) >= s(i - 1) and f(i) < s(i)
        if pos is None:
            if golden and direction in ("both", "long"):
                e = b.open; pos = dict(side="long", e=e, sl=e - sl_atr * atr, sld=sl_atr * atr, i=i)
            elif death and direction in ("both", "short"):
                e = b.open; pos = dict(side="short", e=e, sl=e + sl_atr * atr, sld=sl_atr * atr, i=i)
            continue
        # 保有中: 逆クロス or SL
        exitp = None
        if pos["side"] == "long":
            if b.low <= pos["sl"]: exitp = pos["sl"]
            elif death: exitp = b.open
            if exitp is not None:
                trades.append(dict(t=bars[pos["i"]].time, R=(exitp - pos["e"]) / pos["sld"], sld=pos["sld"], units=1))
                pos = None
                if death and direction == "both":
                    e = b.open; pos = dict(side="short", e=e, sl=e + sl_atr * atr, sld=sl_atr * atr, i=i)
        else:
            if b.high >= pos["sl"]: exitp = pos["sl"]
            elif golden: exitp = b.open
            if exitp is not None:
                trades.append(dict(t=bars[pos["i"]].time, R=(pos["e"] - exitp) / pos["sld"], sld=pos["sld"], units=1))
                pos = None
                if golden and direction == "both":
                    e = b.open; pos = dict(side="long", e=e, sl=e - sl_atr * atr, sld=sl_atr * atr, i=i)
    return trades


def evalp(runner, tf, P, label):
    from tools.bo_fast import cached_arrays  # 高速・ディスクキャッシュ
    agg = dict(n=0, p1=0.0, p2=0.0, w=0); rob = 0; gold = None; viable = []
    for sym in ALL:
        tr = runner(cached_arrays(sym, tf), P)
        ps = pip(sym); c = comm(sym) + SPREAD
        for t in tr:
            t["nR"] = t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)
        if not tr: continue
        n1 = sum(t["nR"] for t in tr if t["t"] < P1_END)
        n2 = sum(t["nR"] for t in tr if t["t"] >= P1_END)
        w = sum(1 for t in tr if t["nR"] > 0)
        agg["n"] += len(tr); agg["p1"] += n1; agg["p2"] += n2; agg["w"] += w
        r = n1 > 0 and n2 > 0; rob += r
        if r: viable.append(sym)
        if sym == "XAUUSD": gold = (n1, n2, 100*w/len(tr))
    g = f"{gold[0]:>+6.1f}/{gold[1]:>+6.1f} {gold[2]:.0f}%" if gold else "-"
    print(f"  {label:<30} | {agg['n']:>5} | {agg['p1']/P1_M:>+7.2f}% {agg['p2']/P2_M:>+7.2f}% | "
          f"{rob:>2}/12 | {g}")
    print(f"      viable: {','.join(viable)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="tf", choices=["tf", "macross", "macross_tf"])
    args = ap.parse_args()
    from tools.bo_fast import run_bo_fast, run_macross_fast
    LO = dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, direction="long")

    print("=" * 92)
    print(f"momentum_lab [{args.mode}] — 全12ペア net WF  (P1=21-23 / P2=24-26)")
    print("=" * 92)
    print(f"  {'構成':<30} | {'全N':>5} | {'P1/月':>7} {'P2/月':>7} | {'robust':>6} | 金 P1/P2 WR")
    print("-" * 92)

    if args.mode == "tf":
        for tf in ["h1", "h4", "d1"]:
            # TF に応じて Donchian 長を調整(おおむね同等の実時間)
            ext = lambda s, tf=tf: extract_tf(s, tf)
            if tf == "h1":   en, ex = 50, 25
            elif tf == "h4": en, ex = 20, 20
            else:            en, ex = 20, 10   # d1
            P = dict(LO, entry_n=en, exit_n=ex)
            evalp(run_bo_fast, tf, P, f"breakout {tf.upper()} (D{en}/E{ex})")

    elif args.mode == "macross":
        combos = [(50, 200), (20, 50), (10, 30), (9, 21), (5, 20)]
        for d in ["long", "both"]:
            print(f"  --- direction={d} (H4) ---")
            for fn, sn in combos:
                P = dict(fast_n=fn, slow_n=sn, atr_n=20, sl_atr=2.0, direction=d)
                evalp(run_macross_fast, "h4", P, f"MAcross {fn}/{sn}")

    elif args.mode == "macross_tf":
        for tf in ["h1", "h4", "d1"]:
            ext = lambda s, tf=tf: extract_tf(s, tf)
            P = dict(fast_n=20, slow_n=50, atr_n=20, sl_atr=2.0, direction="long")
            evalp(run_macross_fast, tf, P, f"MAcross 20/50 long {tf.upper()}")
    print("-" * 92)


if __name__ == "__main__":
    main()
