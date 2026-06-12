"""
breakout_lab.py — ブレイクアウト改善の体系検証エンジン。

base(Donchian) に対し、ピラミッディング/方向バイアス/トレーリング各種/エントリー確度
などを切替可能。各 variant を **全12ペアで横断検証**(P1/P2 net + robust数 + 金単体)し、
改善効果と他ペアへの汎化を同時に見る。

net = 往復コミ + spread(ピラミッドは unit 毎にコスト)。1R=初期ユニットの口座1%。
WF: P1=2021-23 / P2=2024-26。robust = 両期間 net+。

使い方:
    python tools/breakout_lab.py --set pyramid       # ①ピラミッディング
    python tools/breakout_lab.py --set direction     # ②方向バイアス
    python tools/breakout_lab.py --set trailing      # ③トレーリング
    python tools/breakout_lab.py --set entry         # ④エントリー確度
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.backtest_breakout import pip, comm, load_ticks  # noqa: E402

ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30
SPREAD = 0.5
_cache = {}


class Bar:
    __slots__ = ("time", "open", "high", "low", "close")
    def __init__(s, t, o, h, l, c): s.time=t; s.open=o; s.high=h; s.low=l; s.close=c


def extract_h4(sym):
    if sym in _cache: return _cache[sym]
    seen = set(); bars = []
    for tup in load_ticks(sym):
        b = tup[2]
        if b is None or b.time in seen: continue
        seen.add(b.time); bars.append(Bar(b.time, b.open, b.high, b.low, b.close))
    bars.sort(key=lambda x: x.time)
    _cache[sym] = bars
    return bars


def atr_at(bars, i, n):
    if i < n: return None
    s = 0.0
    for k in range(i - n + 1, i + 1):
        h, l, pc = bars[k].high, bars[k].low, bars[k - 1].close
        s += max(h - l, abs(h - pc), abs(l - pc))
    return s / n


def run_bo(bars, P):
    en, ex, an = P["entry_n"], P["exit_n"], P["atr_n"]
    sl_atr, sma_n = P["sl_atr"], P["sma_n"]
    direction = P.get("direction", "both")
    max_adds = P.get("max_adds", 0)
    step_atr = P.get("step_atr", 1.0)
    trail_mode = P.get("trail_mode", "donchian")
    trail_atr = P.get("trail_atr", 3.0)
    confirm = P.get("confirm", "wick")
    partial_R = P.get("partial_R", 0.0)      # ⑥ 部分利確: +partial_R×SL 到達で partial_frac を利確
    partial_frac = P.get("partial_frac", 0.5)
    closes = [b.close for b in bars]
    warm = max(en, an, sma_n, ex) + 2
    trades = []; pos = None
    for i in range(warm, len(bars)):
        b = bars[i]
        atr = atr_at(bars, i - 1, an)
        if not atr or atr <= 0: continue
        dhi = max(bb.high for bb in bars[i - en:i])
        dlo = min(bb.low for bb in bars[i - en:i])
        sma = sum(closes[i - sma_n:i]) / sma_n if sma_n > 0 else None
        if pos is None:
            bl = (b.close > dhi) if confirm == "close" else (b.high > dhi)
            bs = (b.close < dlo) if confirm == "close" else (b.low < dlo)
            long_ok = bl and (sma is None or b.close > sma) and direction in ("both", "long")
            short_ok = bs and (sma is None or b.close < sma) and direction in ("both", "short")
            if long_ok:
                e = b.close if confirm == "close" else dhi
                pos = dict(side="long", units=[e], sl=e - sl_atr * atr, sld=sl_atr * atr,
                           i=i, best=e, atr0=atr, last_add=e, realized=0.0, rem=1.0)
            elif short_ok:
                e = b.close if confirm == "close" else dlo
                pos = dict(side="short", units=[e], sl=e + sl_atr * atr, sld=sl_atr * atr,
                           i=i, best=e, atr0=atr, last_add=e, realized=0.0, rem=1.0)
            continue
        sld = pos["sld"]; a0 = pos["atr0"]
        if pos["side"] == "long":
            pos["best"] = max(pos["best"], b.high)
            while max_adds > 0 and len(pos["units"]) <= max_adds and b.high >= pos["last_add"] + step_atr * a0:
                addp = pos["last_add"] + step_atr * a0
                pos["units"].append(addp); pos["last_add"] = addp
            if partial_R > 0 and pos["rem"] >= 1.0:
                tp = pos["units"][0] + partial_R * sld
                if b.high >= tp:
                    pos["realized"] += partial_frac * sum(tp - u for u in pos["units"]) / sld
                    pos["rem"] = 1.0 - partial_frac
            trail = (pos["best"] - trail_atr * atr) if trail_mode == "chandelier" else min(bb.low for bb in bars[i - ex:i])
            exitp = None
            if b.low <= pos["sl"]: exitp = pos["sl"]
            elif b.low <= trail: exitp = min(trail, b.open)
            if exitp is not None:
                R = pos["realized"] + pos["rem"] * sum(exitp - u for u in pos["units"]) / sld
                trades.append(dict(t=bars[pos["i"]].time, R=R, sld=sld, units=len(pos["units"])))
                pos = None
        else:
            pos["best"] = min(pos["best"], b.low)
            while max_adds > 0 and len(pos["units"]) <= max_adds and b.low <= pos["last_add"] - step_atr * a0:
                addp = pos["last_add"] - step_atr * a0
                pos["units"].append(addp); pos["last_add"] = addp
            if partial_R > 0 and pos["rem"] >= 1.0:
                tp = pos["units"][0] - partial_R * sld
                if b.low <= tp:
                    pos["realized"] += partial_frac * sum(u - tp for u in pos["units"]) / sld
                    pos["rem"] = 1.0 - partial_frac
            trail = (pos["best"] + trail_atr * atr) if trail_mode == "chandelier" else max(bb.high for bb in bars[i - ex:i])
            exitp = None
            if b.high >= pos["sl"]: exitp = pos["sl"]
            elif b.high >= trail: exitp = max(trail, b.open)
            if exitp is not None:
                R = pos["realized"] + pos["rem"] * sum(u - exitp for u in pos["units"]) / sld
                trades.append(dict(t=bars[pos["i"]].time, R=R, sld=sld, units=len(pos["units"])))
                pos = None
    return trades


def eval_pairs(P, pairs=ALL):
    from tools.bo_fast import cached_arrays, run_bo_fast  # NumPy高速版(完全一致検証済)
    agg = dict(n=0, p1=0.0, p2=0.0, w=0)
    rob = 0; gold = None; viable = []
    for sym in pairs:
        tr = run_bo_fast(cached_arrays(sym, "h4"), P)
        ps = pip(sym); c = comm(sym) + SPREAD
        for t in tr:
            t["nR"] = t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)  # コストは unit 毎
        if not tr: continue
        n1 = sum(t["nR"] for t in tr if t["t"] < P1_END)
        n2 = sum(t["nR"] for t in tr if t["t"] >= P1_END)
        w = sum(1 for t in tr if t["nR"] > 0)
        agg["n"] += len(tr); agg["p1"] += n1; agg["p2"] += n2; agg["w"] += w
        r = (n1 > 0 and n2 > 0); rob += r
        if r: viable.append(sym)
        if sym == "XAUUSD": gold = (len(tr), 100*w/len(tr), n1, n2)
    return agg, rob, gold, viable


def variant_sets(name):
    base = dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, label="base(en20/ex20/SL2/SMA100)")
    if name == "pyramid":
        return [base,
                dict(base, max_adds=1, step_atr=1.0, label="+pyramid x1 (step1N)"),
                dict(base, max_adds=2, step_atr=1.0, label="+pyramid x2 (step1N)"),
                dict(base, max_adds=3, step_atr=0.5, label="+pyramid x3 (step0.5N)"),
                dict(base, max_adds=2, step_atr=0.5, label="+pyramid x2 (step0.5N)")]
    if name == "direction":
        return [base,
                dict(base, direction="long", label="long-only"),
                dict(base, direction="short", label="short-only")]
    if name == "trailing":
        return [base,
                dict(base, exit_n=10, label="exit_n=10 (狭)"),
                dict(base, exit_n=30, label="exit_n=30 (広)"),
                dict(base, exit_n=40, label="exit_n=40 (より広)"),
                dict(base, trail_mode="chandelier", trail_atr=3.0, label="chandelier 3ATR"),
                dict(base, trail_mode="chandelier", trail_atr=5.0, label="chandelier 5ATR")]
    if name == "entry":
        return [base,
                dict(base, confirm="close", label="終値ブレイク確定"),
                dict(base, sma_n=200, label="SMA200(強トレンドのみ)"),
                dict(base, sma_n=0, label="SMAフィルタなし")]
    if name == "combo":
        return [base,
                dict(base, direction="long", label="long-only/SMA100"),
                dict(base, direction="long", sma_n=200, label="long-only/SMA200"),
                dict(base, direction="long", sma_n=200, exit_n=10, label="long/SMA200/exit10"),
                dict(base, direction="long", sma_n=200, sl_atr=3.0, label="long/SMA200/SL3"),
                dict(base, direction="long", sma_n=150, label="long-only/SMA150")]
    if name == "partial":
        lo = dict(base, direction="long", label="long-only (基準)")
        return [lo,
                dict(lo, partial_R=1.0, partial_frac=0.5, label="+partial 50%@+1R"),
                dict(lo, partial_R=2.0, partial_frac=0.5, label="+partial 50%@+2R"),
                dict(lo, partial_R=3.0, partial_frac=0.5, label="+partial 50%@+3R")]
    return [base]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="pyramid",
                    choices=["pyramid","direction","trailing","entry","combo","partial"])
    args = ap.parse_args()
    variants = variant_sets(args.set)
    print("=" * 104)
    print(f"ブレイクアウト改善検証 [{args.set}] — 全12ペア横断 (net, WF)")
    print("robust=両期間net+ のペア数。金=XAUUSD単体。採用=金↑かつ robust数維持/増。")
    print("=" * 104)
    print(f"  {'variant':<28} | {'全N':>5} | {'agg P1/月':>9} {'agg P2/月':>9} | {'robust':>6} | "
          f"{'金 P1/P2':>14} {'金WR':>5}")
    print("-" * 104)
    for P in variants:
        agg, rob, gold, viable = eval_pairs(P)
        g = f"{gold[2]:>+6.1f}/{gold[3]:>+6.1f}" if gold else "   -  "
        gwr = f"{gold[1]:.0f}%" if gold else "-"
        print(f"  {P['label']:<28} | {agg['n']:>5} | {agg['p1']/P1_M:>+8.2f}% {agg['p2']/P2_M:>+8.2f}% | "
              f"{rob:>2}/12 | {g:>14} {gwr:>5}")
        print(f"      viable: {','.join(viable)}", flush=True)
    print("-" * 104)


if __name__ == "__main__":
    main()
