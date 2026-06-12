"""
bo_fast.py — ブレイクアウト・エンジンの NumPy 高速版。

ボトルネックは毎バーの O(window) 窓計算 (max/min/sum over bars[i-w:i])。
指標(Donchian高安・トレール窓・SMA・ATR)を sliding_window_view で一括前計算し、
逐次の位置管理ループは O(1) 参照だけにする。ロジックは breakout_lab.run_bo と完全一致。
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_arrcache = {}


def to_arrays(bars):
    """list[Bar] → (time,open,high,low,close) numpy 配列。"""
    n = len(bars)
    t = np.empty(n, dtype=np.int64); o = np.empty(n); h = np.empty(n); l = np.empty(n); c = np.empty(n)
    for k, b in enumerate(bars):
        t[k] = b.time; o[k] = b.open; h[k] = b.high; l[k] = b.low; c[k] = b.close
    return (t, o, h, l, c)


def precompute(arr, en, ex, an, sma_n):
    t, o, h, l, c = arr
    N = len(c)
    # TR
    tr = np.zeros(N)
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])

    def rmax(x, w):
        out = np.full(N, np.nan);
        if w <= N: out[w:] = swv(x, w).max(axis=1)[:N - w]
        return out  # out[i] = max(x[i-w:i])

    def rmin(x, w):
        out = np.full(N, np.nan)
        if w <= N: out[w:] = swv(x, w).min(axis=1)[:N - w]
        return out

    def rmean(x, w):
        out = np.full(N, np.nan)
        if w <= N: out[w:] = swv(x, w).mean(axis=1)[:N - w]
        return out

    donch_hi = rmax(h, en); donch_lo = rmin(l, en)
    trail_lo = rmin(l, ex); trail_hi = rmax(h, ex)
    sma_a = rmean(c, sma_n) if sma_n > 0 else np.full(N, np.nan)
    atr_a = rmean(tr, an)   # atr_a[i] = mean(tr[i-an:i]) = atr_at(bars,i-1,an)
    return donch_hi, donch_lo, trail_lo, trail_hi, sma_a, atr_a


def run_bo_fast(arr, P):
    t, o, h, l, c = arr
    N = len(c)
    en, ex, an = P["entry_n"], P["exit_n"], P["atr_n"]
    sl_atr, sma_n = P["sl_atr"], P["sma_n"]
    direction = P.get("direction", "both"); max_adds = P.get("max_adds", 0); step_atr = P.get("step_atr", 1.0)
    trail_mode = P.get("trail_mode", "donchian"); trail_atr = P.get("trail_atr", 3.0); confirm = P.get("confirm", "wick")
    partial_R = P.get("partial_R", 0.0); partial_frac = P.get("partial_frac", 0.5)
    dhi_a, dlo_a, tlo_a, thi_a, sma_a, atr_a = precompute(arr, en, ex, an, sma_n)
    warm = max(en, an, sma_n, ex) + 2
    trades = []; pos = None
    for i in range(warm, N):
        atr = atr_a[i]
        if not (atr > 0): continue
        dhi = dhi_a[i]; dlo = dlo_a[i]; bh = h[i]; bl = l[i]; bo = o[i]; bc = c[i]
        if pos is None:
            blk = (bc > dhi) if confirm == "close" else (bh > dhi)
            bsk = (bc < dlo) if confirm == "close" else (bl < dlo)
            long_ok = blk and (sma_n == 0 or bc > sma_a[i]) and direction in ("both", "long")
            short_ok = bsk and (sma_n == 0 or bc < sma_a[i]) and direction in ("both", "short")
            if long_ok:
                e = bc if confirm == "close" else dhi
                pos = dict(side="long", units=[e], sl=e - sl_atr * atr, sld=sl_atr * atr, i=i, best=e, atr0=atr, last_add=e, realized=0.0, rem=1.0)
            elif short_ok:
                e = bc if confirm == "close" else dlo
                pos = dict(side="short", units=[e], sl=e + sl_atr * atr, sld=sl_atr * atr, i=i, best=e, atr0=atr, last_add=e, realized=0.0, rem=1.0)
            continue
        sld = pos["sld"]; a0 = pos["atr0"]
        if pos["side"] == "long":
            pos["best"] = max(pos["best"], bh)
            while max_adds > 0 and len(pos["units"]) <= max_adds and bh >= pos["last_add"] + step_atr * a0:
                addp = pos["last_add"] + step_atr * a0; pos["units"].append(addp); pos["last_add"] = addp
            if partial_R > 0 and pos["rem"] >= 1.0:
                tp = pos["units"][0] + partial_R * sld
                if bh >= tp:
                    pos["realized"] += partial_frac * sum(tp - u for u in pos["units"]) / sld; pos["rem"] = 1.0 - partial_frac
            trail = (pos["best"] - trail_atr * atr) if trail_mode == "chandelier" else tlo_a[i]
            exitp = None
            if bl <= pos["sl"]: exitp = pos["sl"]
            elif bl <= trail: exitp = min(trail, bo)
            if exitp is not None:
                R = pos["realized"] + pos["rem"] * sum(exitp - u for u in pos["units"]) / sld
                trades.append(dict(t=int(t[pos["i"]]), R=R, sld=sld, units=len(pos["units"]))); pos = None
        else:
            pos["best"] = min(pos["best"], bl)
            while max_adds > 0 and len(pos["units"]) <= max_adds and bl <= pos["last_add"] - step_atr * a0:
                addp = pos["last_add"] - step_atr * a0; pos["units"].append(addp); pos["last_add"] = addp
            if partial_R > 0 and pos["rem"] >= 1.0:
                tp = pos["units"][0] - partial_R * sld
                if bl <= tp:
                    pos["realized"] += partial_frac * sum(u - tp for u in pos["units"]) / sld; pos["rem"] = 1.0 - partial_frac
            trail = (pos["best"] + trail_atr * atr) if trail_mode == "chandelier" else thi_a[i]
            exitp = None
            if bh >= pos["sl"]: exitp = pos["sl"]
            elif bh >= trail: exitp = max(trail, bo)
            if exitp is not None:
                R = pos["realized"] + pos["rem"] * sum(u - exitp for u in pos["units"]) / sld
                trades.append(dict(t=int(t[pos["i"]]), R=R, sld=sld, units=len(pos["units"]))); pos = None
    return trades


CACHE = ROOT / "data" / "cache"


def _extract_tf(sym, tf):
    """load_ticks ストリームから TF バー配列を抽出 (キャッシュ無い時のみ)。"""
    from tools.backtest_breakout import load_ticks
    from datetime import datetime, timezone
    idx = {"h1": 1, "h4": 2, "d1": 2}[tf]
    seen = set(); raw = []
    for tup in load_ticks(sym):
        b = tup[idx]
        if b is None or b.time in seen: continue
        seen.add(b.time); raw.append((b.time, b.open, b.high, b.low, b.close))
    raw.sort(key=lambda x: x[0])
    if tf in ("h1", "h4"):
        rows = raw
    else:  # d1: UTC日付集約
        days = {}; order = []
        for (tt, o, h, l, c) in raw:
            d = datetime.fromtimestamp(tt, tz=timezone.utc).strftime("%Y-%m-%d")
            if d not in days: days[d] = [tt, o, h, l, c]; order.append(d)
            else:
                r = days[d]; r[2] = max(r[2], h); r[3] = min(r[3], l); r[4] = c
        rows = [tuple(days[d]) for d in order]
    n = len(rows)
    t = np.empty(n, dtype=np.int64); o = np.empty(n); h = np.empty(n); l = np.empty(n); c = np.empty(n)
    for k, r in enumerate(rows):
        t[k], o[k], h[k], l[k], c[k] = r
    return (t, o, h, l, c)


def cached_arrays(sym, tf="h4"):
    """TF配列をディスクキャッシュ(data/cache/bo_<sym>_<tf>.npz)。初回のみ抽出。"""
    key = (sym, tf)
    if key in _arrcache: return _arrcache[key]
    f = CACHE / f"bo_{sym}_{tf}.npz"
    if f.exists():
        d = np.load(f); arr = (d["t"], d["o"], d["h"], d["l"], d["c"])
    else:
        arr = _extract_tf(sym, tf)
        CACHE.mkdir(parents=True, exist_ok=True)
        np.savez(f, t=arr[0], o=arr[1], h=arr[2], l=arr[3], c=arr[4])
    _arrcache[key] = arr
    return arr


def get_arrays(sym):
    return cached_arrays(sym, "h4")


def run_macross_fast(arr, P):
    """MA交差(golden/death cross)NumPy高速版。momentum_lab.run_macross と完全一致。"""
    t, o, h, l, c = arr
    N = len(c)
    fn, sn, an = P["fast_n"], P["slow_n"], P.get("atr_n", 20)
    sl_atr = P["sl_atr"]; direction = P.get("direction", "both")
    # 指標前計算
    tr = np.zeros(N)
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    def rmean(x, w):
        out = np.full(N, np.nan)
        if w <= N: out[w:] = swv(x, w).mean(axis=1)[:N - w]
        return out
    fa = rmean(c, fn); sa = rmean(c, sn); atr_a = rmean(tr, an)
    warm = sn + 2
    pos = None; trades = []
    for i in range(warm, N):
        atr = atr_a[i]
        if not (atr > 0): continue
        golden = fa[i - 1] <= sa[i - 1] and fa[i] > sa[i]
        death = fa[i - 1] >= sa[i - 1] and fa[i] < sa[i]
        bh = h[i]; bl = l[i]; bo = o[i]
        if pos is None:
            if golden and direction in ("both", "long"):
                pos = dict(side="long", e=bo, sl=bo - sl_atr * atr, sld=sl_atr * atr, i=i)
            elif death and direction in ("both", "short"):
                pos = dict(side="short", e=bo, sl=bo + sl_atr * atr, sld=sl_atr * atr, i=i)
            continue
        exitp = None
        if pos["side"] == "long":
            if bl <= pos["sl"]: exitp = pos["sl"]
            elif death: exitp = bo
            if exitp is not None:
                trades.append(dict(t=int(t[pos["i"]]), R=(exitp - pos["e"]) / pos["sld"], sld=pos["sld"], units=1)); pos = None
                if death and direction == "both":
                    pos = dict(side="short", e=bo, sl=bo + sl_atr * atr, sld=sl_atr * atr, i=i)
        else:
            if bh >= pos["sl"]: exitp = pos["sl"]
            elif golden: exitp = bo
            if exitp is not None:
                trades.append(dict(t=int(t[pos["i"]]), R=(pos["e"] - exitp) / pos["sld"], sld=pos["sld"], units=1)); pos = None
                if golden and direction == "both":
                    pos = dict(side="long", e=bo, sl=bo - sl_atr * atr, sld=sl_atr * atr, i=i)
    return trades


def verify():
    """breakout_lab.run_bo と完全一致するか検証。"""
    from tools.breakout_lab import extract_h4, run_bo, ALL
    configs = [
        dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, direction="long"),
        dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, max_adds=2, step_atr=0.5),
        dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, direction="long", partial_R=2.0),
        dict(entry_n=20, exit_n=10, atr_n=20, sl_atr=2.0, sma_n=0, trail_mode="chandelier", trail_atr=3.0),
    ]
    allok = True
    for P in configs:
        for sym in ALL:
            bars = extract_h4(sym)
            slow = run_bo(bars, P)
            fast = run_bo_fast(to_arrays(bars), P)
            if len(slow) != len(fast):
                print(f"  ✗ {sym} {P.get('label','')}: N {len(slow)} vs {len(fast)}"); allok = False; continue
            for a, bb in zip(slow, fast):
                if a["t"] != bb["t"] or abs(a["R"] - bb["R"]) > 1e-9 or a["units"] != bb["units"]:
                    print(f"  ✗ {sym}: t{a['t']} R {a['R']:.6f} vs {bb['R']:.6f}"); allok = False; break
        print(f"  {'✓' if allok else '✗'} config {P} 一致" if allok else "", end="")
    print("\n" + ("✅ 全構成・全12ペアで完全一致" if allok else "❌ 不一致あり"))
    return allok


if __name__ == "__main__":
    verify()
