"""
classify_trades.py — 各トレードを多軸で分類して勝率/期待値を統計分析。

分類軸 (エントリー時点):
  - trend_dir   : H4/H1/M15 のトレンド方向 (up/down/range)
  - regime      : H4/H1/M15 の加速/減速/レンジ (推進波が拡大=accel / 縮小=decel / トレンド無し=range)
  - h1_line     : H1 トレンドラインが 近方(near)/遠方(far)  (near = 直近 M15 レッグ幅以内 = 次の波で到達しそう)
  - pattern     : 直近 M15 プライスアクション (none / double_top / double_bottom / wedge) ※簡易ヒューリスティック
  - dur         : トレード期間 (M5 本数) を四分位
  - hour        : エントリー UTC 時刻帯
  - pair        : 通貨ペア

エントリー = 設定可能 (既定: H1+M15 一致 + room_R<2.0 + 6-10時除外, RR1.5)。

使い方:
    python tools/classify_trades.py            # 主要4ペア
    python tools/classify_trades.py --all      # 全12ペア
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import (  # noqa: E402
    Params, MtfPullbackStrategy, _dow_trend, _trendline_dist_atr)
from src.core.indicators import atr as _atr  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks  # noqa: E402

MAJORS = ["EURUSD", "USDJPY", "GBPUSD", "XAUUSD"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]

# 分析対象のエントリー設定 (H1+M15 一致 + フィルタ)
PARAMS = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15",
              room_R_max=2.0, block_hour_start=6, block_hour_end=10)


def _legs(pivots, trend):
    out = []
    if trend == "up":
        for i in range(1, len(pivots)):
            if pivots[i - 1].kind == "low" and pivots[i].kind == "high":
                out.append(pivots[i].price - pivots[i - 1].price)
    elif trend == "down":
        for i in range(1, len(pivots)):
            if pivots[i - 1].kind == "high" and pivots[i].kind == "low":
                out.append(pivots[i - 1].price - pivots[i].price)
    return out


def regime(pivots, trend):
    if trend is None:
        return "range"
    lg = _legs(pivots, trend)
    if len(lg) < 2:
        return "range"
    return "accel" if lg[-1] > lg[-2] else "decel"


def pattern(pivots):
    """直近 M15 ピボットから簡易パターン検出 (ヒューリスティック)。"""
    if len(pivots) < 4:
        return "none"
    highs = [p.price for p in pivots if p.kind == "high"]
    lows = [p.price for p in pivots if p.kind == "low"]
    last = pivots[-1].price
    # double top/bottom: 直近 2 高値(安値) が ~0.1% 以内で近接
    if len(highs) >= 2 and abs(highs[-1] - highs[-2]) / max(1e-9, highs[-1]) < 0.0012:
        return "double_top"
    if len(lows) >= 2 and abs(lows[-1] - lows[-2]) / max(1e-9, lows[-1]) < 0.0012:
        return "double_bottom"
    # wedge: 直近 up-leg と down-leg が両方縮小 (収束)
    up = _legs(pivots, "up")
    dn = _legs(pivots, "down")
    if len(up) >= 2 and len(dn) >= 2 and up[-1] < up[-2] and dn[-1] < dn[-2]:
        return "wedge"
    return "none"


def run_pair(symbol, ticks):
    strat = MtfPullbackStrategy(Params(**PARAMS))
    strat.symbol = symbol
    ctx = SimContext()
    last_h1 = last_h4 = -1
    open_trade = None
    trades = []
    m5buf = []
    for i, (m5, h1, h4) in enumerate(ticks):
        m5buf.append(m5)
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60:
            ctx.bars_seq.pop(0)
        if h1 is not None and h1.time > last_h1:
            ctx.mtf[3600].append(h1); last_h1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        # 既存ポジの決済
        if open_trade is not None:
            hit = None; ip = i
            if open_trade["side"] == "long":
                if m5.low <= open_trade["sl"]: hit = 0
                elif m5.high >= open_trade["tp"]: hit = 1
            else:
                if m5.high >= open_trade["sl"]: hit = 0
                elif m5.low <= open_trade["tp"]: hit = 1
            if hit is not None:
                open_trade["win"] = hit
                open_trade["dur"] = ip - open_trade["i"]
                trades.append(open_trade)
                open_trade = None
                ctx._position = None
        ctx.pending_commands = []
        try:
            strat.on_bar(ctx)
        except Exception:
            pass
        if open_trade is None:
            for cmd in ctx.pending_commands:
                if cmd["type"] in ("buy", "sell"):
                    side = "long" if cmd["type"] == "buy" else "short"
                    entry = cmd.get("entry_price") or m5.close
                    sl = cmd.get("sl")
                    sld = abs(entry - sl) if sl else 0
                    if not sl or sld <= 0:
                        continue
                    h4t = _dow_trend(strat.zz_h4.pivots)
                    h1t = _dow_trend(strat.zz_h1.pivots)
                    m15t = _dow_trend(strat.zz_m15.pivots)
                    # H1 トレンドライン距離 (near/far)
                    hs = [b.high for b in ctx.bars_seq[-20:]]; ls = [b.low for b in ctx.bars_seq[-20:]]; cs = [b.close for b in ctx.bars_seq[-20:]]
                    av = _atr(hs, ls, cs, 14); atrv = av[-1] if av and av[-1] else None
                    tl = _trendline_dist_atr(strat.zz_h1.pivots, len(strat.zz_h1.bars) - 1, entry, atrv or 1, h1t)
                    leg = _legs(strat.zz_m15.pivots, m15t)
                    legsz = leg[-1] if leg else None
                    if tl is None or atrv is None or legsz is None:
                        h1_line = "na"
                    else:
                        dist_price = abs(tl) * atrv
                        h1_line = "near" if dist_price < legsz else "far"
                    # 追加特徴
                    m30t = _dow_trend(strat.zz_m30.pivots)
                    m15h = [q.price for q in strat.zz_m15.pivots if q.kind == "high"]
                    m15l = [q.price for q in strat.zz_m15.pivots if q.kind == "low"]
                    if side == "long":
                        room = (m15h[-1] - entry) if m15h else None
                        rng = (m15h[-1] - m15l[-1]) if (m15h and m15l) else None
                        pb = ((m15h[-1] - entry) / rng) if (rng and rng > 0) else None
                    else:
                        room = (entry - m15l[-1]) if m15l else None
                        rng = (m15h[-1] - m15l[-1]) if (m15h and m15l) else None
                        pb = ((entry - m15l[-1]) / rng) if (rng and rng > 0) else None
                    dt = datetime.fromtimestamp(int(m5.time), tz=timezone.utc)
                    want = "up" if side == "long" else "down"
                    open_trade = {
                        "side": side, "entry": entry, "sl": sl, "tp": cmd.get("tp"), "sld": sld, "i": i,
                        "pair": symbol,
                        "h4_dir": h4t or "range", "h1_dir": h1t or "range", "m15_dir": m15t or "range",
                        "h4_reg": regime(strat.zz_h4.pivots, h4t),
                        "h1_reg": regime(strat.zz_h1.pivots, h1t),
                        "m15_reg": regime(strat.zz_m15.pivots, m15t),
                        "h1_line": h1_line,
                        "pattern": pattern(strat.zz_m15.pivots),
                        "hour": dt.hour,
                        # --- 追加分析項目 ---
                        "sl_atr": (sld / atrv) if atrv else None,             # SL のタイトさ
                        "atr_pct": (atrv / entry * 100) if atrv else None,    # ボラ regime
                        "room_R": (room / sld) if room is not None else None,
                        "pb_frac": pb,                                        # 押し目深さ
                        "h4_agree": ("agree" if h4t == want else ("range" if h4t is None else "disagree")),
                        "m30_agree": ("agree" if m30t == want else ("range" if m30t is None else "disagree")),
                        "dow": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()],
                    }
                    ctx._position = side
                    break
    return trades


def grp_wr(trades, keyfn):
    g = defaultdict(lambda: [0, 0])  # key -> [n, wins]
    for t in trades:
        k = keyfn(t)
        g[k][0] += 1; g[k][1] += t["win"]
    return g


def show(title, trades, keyfn, sort_by_wr=False):
    g = grp_wr(trades, keyfn)
    items = sorted(g.items(), key=(lambda kv: -kv[1][1] / kv[1][0]) if sort_by_wr else (lambda kv: str(kv[0])))
    print(f"■ {title}")
    for k, (n, w) in items:
        if n < 3:
            continue
        print(f"    {str(k):22s} n={n:4d}  WR={100*w/n:5.1f}%")
    print()


def show_q(title, trades, key, nq=4):
    """連続値特徴を四分位で。"""
    vals = sorted(t[key] for t in trades if t.get(key) is not None)
    if len(vals) < nq * 2:
        return
    edges = [vals[int(len(vals) * i / nq)] for i in range(1, nq)]

    def bucket(t):
        v = t.get(key)
        if v is None:
            return "na"
        g = 0
        while g < nq - 1 and v >= edges[g]:
            g += 1
        return f"Q{g+1} " + (f"<{edges[0]:.2f}" if g == 0 else (f">={edges[-1]:.2f}" if g == nq - 1 else f"{edges[g-1]:.2f}-{edges[g]:.2f}"))
    show(title, trades, bucket)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else MAJORS

    trades = []
    for p in pairs:
        trades += run_pair(p, load_ticks(p))
        print(f"  {p} done")
    durs = sorted(t["dur"] for t in trades)
    q = [durs[int(len(durs) * x)] for x in (0.25, 0.5, 0.75)] if durs else [0, 0, 0]

    def durbucket(t):
        d = t["dur"]
        return "Q1短" if d < q[0] else ("Q2" if d < q[1] else ("Q3" if d < q[2] else "Q4長"))

    def hourband(t):
        h = t["hour"]
        return "0-6" if h < 6 else ("6-10" if h < 10 else ("10-14" if h < 14 else ("14-18" if h < 18 else "18-24")))

    n = len(trades); w = sum(t["win"] for t in trades)
    print("\n" + "=" * 70)
    print(f"トレード分類統計  ({'全12' if args.all else '主要4'}ペア, H1+M15+room_R<2.0+6-10h除外, RR1.5)")
    print(f"全 {n} トレード, WR={100*w/n:.1f}% (損益分岐40%)")
    print("=" * 70 + "\n")
    show("H1 トレンド方向", trades, lambda t: t["h1_dir"])
    show("M15 トレンド方向", trades, lambda t: t["m15_dir"])
    show("H1 regime (加速/減速/レンジ)", trades, lambda t: t["h1_reg"], sort_by_wr=True)
    show("M15 regime", trades, lambda t: t["m15_reg"], sort_by_wr=True)
    show("H4×H1 regime", trades, lambda t: f"H4:{t['h4_reg']}/H1:{t['h1_reg']}", sort_by_wr=True)
    show("H1 トレンドライン 遠近", trades, lambda t: t["h1_line"], sort_by_wr=True)
    show("プライスアクション", trades, lambda t: t["pattern"], sort_by_wr=True)
    show("トレード期間(四分位)", trades, durbucket, sort_by_wr=True)
    show("時間帯(UTC)", trades, hourband, sort_by_wr=True)
    show("曜日", trades, lambda t: t["dow"], sort_by_wr=True)
    show("通貨ペア", trades, lambda t: t["pair"], sort_by_wr=True)
    # --- 追加分析項目 ---
    print("---- 追加項目 ----\n")
    show("H4 が H1 と一致/不一致 (H4外しの妥当性)", trades, lambda t: t["h4_agree"], sort_by_wr=True)
    show("M30 が一致/不一致", trades, lambda t: t["m30_agree"], sort_by_wr=True)
    show_q("sl_atr (SL幅のATR比, 小=タイト)", trades, "sl_atr")
    show_q("pb_frac (押し目深さ)", trades, "pb_frac")
    show_q("atr_pct (ボラregime %)", trades, "atr_pct")
    show_q("room_R (M15余地/SL)", trades, "room_R")


if __name__ == "__main__":
    main()
