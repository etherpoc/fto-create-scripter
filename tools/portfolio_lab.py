"""
portfolio_lab.py — 複数ストリームの相関を測り、DD10%枠で到達できる上限リターンを出す。

各ストリーム(BO-H1long / BO-H4long / BO-short / PB-JPY ...)を net で構築し、
時系列マージ→相対ウェイト探索で MAR(年率/maxDD) 最大の配合を求め、最後に DD=10% へスケール。
これにより「現状の手持ちエッジで、安全DD内に届く現実的天井」を厳密化する。

net 規約は breakout_lab / backtest_net_minsl と同一。1R=口座1%(=risk%/pair)。WF: P1=21-23/P2=24-26。
"""
from __future__ import annotations
import sys, itertools
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bo_fast import cached_arrays, run_bo_fast              # noqa: E402
from tools.backtest_breakout import pip as bpip, comm as bcomm    # noqa: E402
from tools.backtest_net_minsl import capture as pb_capture, net_R as pb_net  # noqa: E402

ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
MONTHS = 66.0
P1_M, P2_M = 36.0, 30.0
SPREAD = 0.5

BO_LONG = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150, direction="long")
BO_LONG_H4 = dict(entry_n=20, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, direction="long")
BO_SHORT = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150, direction="short")
PB_JPY = ["USDJPY", "GBPJPY", "EURJPY"]


def _eq_overlay(trades, K=20, m=0.5):
    """エクイティカーブ・デリスク: basket 実現エクイティが直近K件MAを割ったら mult=m。
    mult は scale 不変(eq も MA も同率)なので unit risk で precompute して良い。"""
    trades = sorted(trades, key=lambda x: x[0])
    eq = 0.0; hist = []; out = []
    for (t, r) in trades:
        mult = 1.0
        if len(hist) >= K:
            ma = sum(hist[-K:]) / K
            if eq < ma:
                mult = m
        p = r * mult
        eq += p; hist.append(eq)
        out.append((t, p))
    return out


def _bo_stream(cfg, tf, pairs=ALL, viable_only=True, overlay=False):
    """BO ストリーム: viable(両期間net+)ペアのみ採用。返り値 list[(t, nR)]."""
    out = []
    for s in pairs:
        tr = run_bo_fast(cached_arrays(s, tf), cfg)
        if not tr:
            continue
        c = bcomm(s) + SPREAD; ps = bpip(s)
        nrs = [(t["t"], t["R"] - c * t["units"] / max(t["sld"] / ps, 1e-9)) for t in tr]
        if viable_only:
            n1 = sum(r for (tt, r) in nrs if tt < P1_END)
            n2 = sum(r for (tt, r) in nrs if tt >= P1_END)
            if not (n1 > 0 and n2 > 0):
                continue
        out += nrs
    if overlay:
        out = _eq_overlay(out)
    return out


def _pb_stream(pairs=PB_JPY, minsl=20):
    out = []
    for s in pairs:
        for t in pb_capture(s, minsl):
            out.append((t["t"], pb_net(t)))
    return out


def monthly_series(trades):
    m = {}
    for (t, r) in trades:
        k = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")
        m[k] = m.get(k, 0.0) + r
    return m


def corr(a, b):
    keys = sorted(set(a) | set(b))
    xs = np.array([a.get(k, 0.0) for k in keys]); ys = np.array([b.get(k, 0.0) for k in keys])
    if xs.std() == 0 or ys.std() == 0:
        return 0.0
    return float(np.corrcoef(xs, ys)[0, 1])


def metrics(times, pnl):
    """time-sorted pnl(%) 配列 → (annual%, maxDD%, p1sum, p2sum)."""
    order = np.argsort(times)
    t = times[order]; p = pnl[order]
    eq = np.cumsum(p)
    peak = np.maximum.accumulate(eq)
    dd = float((peak - eq).max()) if len(eq) else 0.0
    total = float(eq[-1]) if len(eq) else 0.0
    monthly = total / MONTHS
    annual = ((1 + monthly / 100) ** 12 - 1) * 100 if monthly > -100 else -100
    p1 = float(p[t < P1_END].sum()); p2 = float(p[t >= P1_END].sum())
    return annual, dd, p1, p2


def main():
    print("=" * 100)
    print("ポートフォリオ最適化: 複数ストリームを相関考慮で配合 → DD10%枠の到達上限リターン")
    print("=" * 100)

    # --- ストリーム構築 ---
    streams = {}
    streams["BO_H1_long"] = _bo_stream(BO_LONG, "h1", overlay=True)
    streams["BO_H4_long"] = _bo_stream(BO_LONG_H4, "h4", overlay=True)
    streams["BO_H1_short"] = _bo_stream(BO_SHORT, "h1", viable_only=False)  # tail hedge
    streams["PB_JPY"] = _pb_stream()

    print("\n■ 各ストリーム単体 (1%/pair 相当, net WF)")
    print(f"  {'stream':<14} | {'N':>5} | {'年率':>7} {'maxDD':>6} {'MAR':>5} | {'P1':>6} {'P2':>7}")
    msers = {}
    for name, tr in streams.items():
        if not tr:
            print(f"  {name:<14} | 0"); continue
        times = np.array([t for (t, r) in tr], dtype=np.float64)
        pnl = np.array([r for (t, r) in tr], dtype=np.float64)  # 1R=1% 相当
        ann, dd, p1, p2 = metrics(times, pnl)
        mar = ann / dd if dd > 0 else 0
        msers[name] = monthly_series(tr)
        print(f"  {name:<14} | {len(tr):>5} | {ann:>+6.1f}% {dd:>5.1f}% {mar:>5.1f} | {p1:>+6.1f} {p2:>+7.1f}")

    print("\n■ ストリーム間 月次相関")
    names = [n for n in streams if streams[n]]
    print("  " + " " * 14 + "".join(f"{n[:10]:>11}" for n in names))
    for a in names:
        row = f"  {a:<14}"
        for b in names:
            row += f"{corr(msers[a], msers[b]):>11.2f}"
        print(row)

    # --- マージ用配列 (stream index 付き) ---
    sidx = {n: i for i, n in enumerate(names)}
    allt = []; allr = []; alls = []
    for n in names:
        for (t, r) in streams[n]:
            allt.append(t); allr.append(r); alls.append(sidx[n])
    allt = np.array(allt, dtype=np.float64); allr = np.array(allr); alls = np.array(alls)

    # --- 相対ウェイト grid 探索: MAR 最大 → DD10% スケール ---
    grid = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
    best = None
    for combo in itertools.product(grid, repeat=len(names)):
        if sum(combo) == 0:
            continue
        w = np.array(combo)
        pnl = allr * w[alls]
        ann, dd, p1, p2 = metrics(allt, pnl)
        if dd <= 0 or p1 <= 0 or p2 <= 0:
            continue
        mar = (p1 + p2) / dd   # scale不変(sumR/maxDD)。compounding に歪まされない
        if best is None or mar > best[0]:
            best = (mar, combo, ann, dd, p1, p2)

    print("\n■ MAR 最大の配合 (相対ウェイト)")
    if best is None:
        print("  (両期間+の配合なし)"); return
    mar, combo, ann, dd, p1, p2 = best
    for n, wv in zip(names, combo):
        print(f"    {n:<14}: x{wv}")
    print(f"  → 配合の MAR={mar:.1f} (年{ann:+.1f}% / DD{dd:.1f}%)")

    # DD=10% にスケール
    w = np.array(combo)
    pnl = allr * w[alls]
    _, dd0, _, _ = metrics(allt, pnl)
    s = 10.0 / dd0
    pnl2 = pnl * s
    ann2, dd2, p1b, p2b = metrics(allt, pnl2)
    monthly2 = (np.cumsum(pnl2[np.argsort(allt)])[-1]) / MONTHS
    print(f"\n■ DD=10% にスケール (各ストリーム risk% = 相対ウェイト × {s:.2f})")
    for n, wv in zip(names, combo):
        print(f"    {n:<14}: {wv * s:.2f}%/pair")
    print(f"  → 年複利 {ann2:+.1f}% / 月利 {monthly2:+.2f}% / maxDD {dd2:.1f}% / MAR {ann2/dd2:.1f}")
    print(f"  目標(月6-8%)との比: {monthly2/7*100:.0f}% (月7%基準)")
    _verify("MAR最大(集中)", allt, pnl2)

    # --- 総額レベルのオーバーレイを更にかけて DD 圧縮 → 再レバ ---
    base = list(zip(allt.tolist(), pnl.tolist()))   # スケール前の混合
    ov2 = _eq_overlay(base, K=20, m=0.5)
    t2 = np.array([x[0] for x in ov2]); p2arr = np.array([x[1] for x in ov2])
    _, ddx, q1, q2 = metrics(t2, p2arr)
    if ddx > 0 and q1 > 0 and q2 > 0:
        s2 = 10.0 / ddx; p2s = p2arr * s2
        annx, ddx2, _, _ = metrics(t2, p2s)
        mx = np.cumsum(p2s[np.argsort(t2)])[-1] / MONTHS
        print(f"\n■ +総額オーバーレイ(eqMA K20 m0.5) DD=10%スケール:")
        print(f"  → 年複利 {annx:+.1f}% / 月利 {mx:+.2f}% / maxDD {ddx2:.1f}% / MAR {annx/ddx2:.1f}")
        _verify("二重オーバーレイ", t2, p2s)

    # --- 過剰適合診断: 総額オーバーレイの K,m 感応度 (バラつけば overfit) ---
    print(f"\n■ 二重オーバーレイの頑健性スキャン (K,m を振って DD10%月利。安定なら信頼/バラつけば過剰適合)")
    print(f"  {'':>6}" + "".join(f"  m={mm}" for mm in (0.3, 0.5, 0.7)))
    for K in (10, 20, 40, 80):
        row = f"  K={K:>3}"
        for mm in (0.3, 0.5, 0.7):
            ov = _eq_overlay(base, K=K, m=mm)
            tt = np.array([x[0] for x in ov]); pp = np.array([x[1] for x in ov])
            _, dz, r1, r2 = metrics(tt, pp)
            if dz > 0 and r1 > 0 and r2 > 0:
                ps = pp * (10.0 / dz); mz = np.cumsum(ps[np.argsort(tt)])[-1] / MONTHS
                row += f" {mz:>+5.2f}%"
            else:
                row += "   --- "
        print(row)

    # --- 頑健版: PB を必ず含める制約付き探索 ---
    pb_i = names.index("PB_JPY") if "PB_JPY" in names else None
    if pb_i is not None:
        best_r = None
        for combo in itertools.product(grid, repeat=len(names)):
            if combo[pb_i] < 0.5:      # PB を必ず一定量入れる(レジーム保険)
                continue
            w = np.array(combo); pnl = allr * w[alls]
            ann, dd, p1, p2 = metrics(allt, pnl)
            if dd <= 0 or p1 <= 0 or p2 <= 0:
                continue
            mar = (p1 + p2) / dd
            if best_r is None or mar > best_r[0]:
                best_r = (mar, combo)
        if best_r:
            combo = best_r[1]; w = np.array(combo); pnl = allr * w[alls]
            _, dd0, _, _ = metrics(allt, pnl); s = 10.0 / dd0; pnl2 = pnl * s
            ann2, dd2, _, _ = metrics(allt, pnl2)
            monthly2 = np.cumsum(pnl2[np.argsort(allt)])[-1] / MONTHS
            print(f"\n■ 頑健版(PB>=0.5強制) DD=10%スケール:")
            for n, wv in zip(names, combo):
                print(f"    {n:<14}: {wv * s:.2f}%/pair")
            print(f"  → 年複利 {ann2:+.1f}% / 月利 {monthly2:+.2f}% / maxDD {dd2:.1f}% / MAR {ann2/dd2:.1f}")
            _verify("頑健版", allt, pnl2)


def _verify(label, times, pnl):
    order = np.argsort(times); t = times[order]; p = pnl[order]
    eq = np.cumsum(p); peak = np.maximum.accumulate(eq)
    # 年別
    years = {}
    for tt, pp in zip(t, p):
        y = datetime.fromtimestamp(tt, tz=timezone.utc).year
        years[y] = years.get(y, 0.0) + pp
    # 月別(最悪月)
    mons = {}
    for tt, pp in zip(t, p):
        k = datetime.fromtimestamp(tt, tz=timezone.utc).strftime("%Y-%m")
        mons[k] = mons.get(k, 0.0) + pp
    worst = min(mons.values()); neg = sum(1 for v in mons.values() if v < 0)
    p1 = p[t < P1_END]; p2 = p[t >= P1_END]
    def segdd(x):
        e = np.cumsum(x); pk = np.maximum.accumulate(e); return float((pk - e).max())
    print(f"    [{label}] 年別: " + " ".join(f"{y}:{years[y]:+.0f}" for y in sorted(years)))
    print(f"    [{label}] P1 DD {segdd(p1):.1f}% / P2 DD {segdd(p2):.1f}% / 最悪月 {worst:+.1f}% / "
          f"マイナス月 {neg}/{len(mons)} ({100*neg/len(mons):.0f}%)")


if __name__ == "__main__":
    main()
