"""
analyze_entry_quality.py — 「伸び切った位置でのエントリーは負けやすい」仮説を定量検証。

各トレードで以下を測り、勝ち(TP)/負け(SL)で差が出るか集計する:
  - room_R     : 直近 M15 スイング高安(=ターゲット)までの余地 ÷ sl_dist
                 (long: (M15高値-entry)/sl_dist、short: (entry-M15安値)/sl_dist)
                 TP は tp_rr=1.5R 先なので room_R<1.5 = TP が前回高安の外 = 伸び切り
  - room_h1_R  : 同じく H1 スイングまでの余地 ÷ sl_dist
  - pb_frac    : M15 スイング内での押し戻し深さ (0=高安に張り付き/浅い, 1=逆端/深い)

使い方:
    python tools/analyze_entry_quality.py            # 主要4ペア
    python tools/analyze_entry_quality.py --all      # 全12ペア
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime, timezone  # noqa: E402
from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy, _dow_trend  # noqa: E402
from src.core.indicators import atr as _atr  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks  # noqa: E402

MAJORS = ["EURUSD", "USDJPY", "GBPUSD", "XAUUSD"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]


def run_pair(symbol, ticks):
    strat = MtfPullbackStrategy(Params(skip_on_trendline_break=True, tp_rr=1.5, room_R_max=2.0))
    strat.symbol = symbol
    ctx = SimContext()
    last_h1 = last_h4 = -1
    open_trade = None
    trades = []
    for (m5, h1, h4) in ticks:
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60:
            ctx.bars_seq.pop(0)
        if h1 is not None and h1.time > last_h1:
            ctx.mtf[3600].append(h1); last_h1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        # SL/TP 決済
        if open_trade is not None:
            hit = None
            if open_trade["side"] == "long":
                if m5.low <= open_trade["sl"]: hit = 0
                elif m5.high >= open_trade["tp"]: hit = 1
            else:
                if m5.high >= open_trade["sl"]: hit = 0
                elif m5.low <= open_trade["tp"]: hit = 1
            if hit is not None:
                open_trade["win"] = hit
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
                    sl, tp = cmd.get("sl"), cmd.get("tp")
                    sld = abs(entry - sl) if sl else 0
                    if not sl or not tp or sld <= 0:
                        continue
                    m15h = [p.price for p in strat.zz_m15.pivots if p.kind == "high"]
                    m15l = [p.price for p in strat.zz_m15.pivots if p.kind == "low"]
                    h1h = [p.price for p in strat.zz_h1.pivots if p.kind == "high"]
                    h1l = [p.price for p in strat.zz_h1.pivots if p.kind == "low"]
                    # room: ターゲット方向の直近スイングまでの余地
                    if side == "long":
                        room = (m15h[-1] - entry) if m15h else None
                        room_h1 = (h1h[-1] - entry) if h1h else None
                        rng = (m15h[-1] - m15l[-1]) if (m15h and m15l) else None
                        pb = ((m15h[-1] - entry) / rng) if (rng and rng > 0) else None
                    else:
                        room = (entry - m15l[-1]) if m15l else None
                        room_h1 = (entry - h1l[-1]) if h1l else None
                        rng = (m15h[-1] - m15l[-1]) if (m15h and m15l) else None
                        pb = ((entry - m15l[-1]) / rng) if (rng and rng > 0) else None
                    # --- 追加特徴量 ---
                    bars = ctx.bars(30)
                    hs = [b.high for b in bars]; ls = [b.low for b in bars]; cs = [b.close for b in bars]
                    av = _atr(hs, ls, cs, 14)
                    atrv = av[-1] if av and av[-1] else None
                    hour = datetime.fromtimestamp(int(m5.time), tz=timezone.utc).hour
                    # 押し目の鮮度: M5 トレンド履歴で「最後に opposite だった」のが何本前か
                    hist = strat._m5_trend_history
                    opp = "down" if side == "long" else "up"
                    pb_age = None
                    for k in range(1, min(len(hist), 30) + 1):
                        if hist[-k] == opp:
                            pb_age = k; break
                    m30t = _dow_trend(strat.zz_m30.pivots)
                    open_trade = {
                        "side": side, "entry": entry, "sl": sl, "tp": tp, "sld": sld,
                        "room_R": (room / sld) if room is not None else None,
                        "room_h1_R": (room_h1 / sld) if room_h1 is not None else None,
                        "pb_frac": pb,
                        "sl_atr": (sld / atrv) if atrv else None,        # SL のタイトさ (ATR 単位)
                        "atr_pct": (atrv / entry * 100) if atrv else None,  # ボラ regime
                        "hour": float(hour),                              # エントリー時刻 (UTC)
                        "pb_age": float(pb_age) if pb_age else None,      # 押し目転換からの鮮度(本)
                        "m30_align": 1.0 if m30t == ("up" if side == "long" else "down") else 0.0,
                    }
                    ctx._position = side
                    break
    return trades


def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return (0, 0.0, 0.0)
    n = len(vals)
    m = sum(vals) / n
    sd = (sum((v - m) ** 2 for v in vals) / n) ** 0.5
    return (n, m, sd)


def wr_by_quantile(trades, key, nq=4):
    vals = sorted(t[key] for t in trades if t[key] is not None)
    if len(vals) < nq * 2:
        return
    edges = [vals[int(len(vals) * i / nq)] for i in range(1, nq)]
    groups = [[] for _ in range(nq)]
    for t in trades:
        v = t[key]
        if v is None:
            continue
        g = 0
        while g < nq - 1 and v >= edges[g]:
            g += 1
        groups[g].append(t)
    parts = []
    for g, sub in enumerate(groups):
        if not sub:
            continue
        w = sum(t["win"] for t in sub)
        rng = f"<{edges[0]:.2f}" if g == 0 else (f">={edges[-1]:.2f}" if g == nq - 1 else f"{edges[g-1]:.2f}-{edges[g]:.2f}")
        parts.append(f"[{rng}] n={len(sub)} WR={100*w/len(sub):.0f}%")
    print("     " + "  |  ".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else MAJORS

    all_trades = []
    for p in pairs:
        ticks = load_ticks(p)
        all_trades += run_pair(p, ticks)
        print(f"  loaded {p}")

    wins = [t for t in all_trades if t["win"] == 1]
    loss = [t for t in all_trades if t["win"] == 0]
    print("\n" + "=" * 84)
    print(f"エントリー多特徴 vs 勝敗  ({'全12' if args.all else '主要4'}ペア, v2+room_R<2.0, RR1.5)  "
          f"全{len(all_trades)} (勝{len(wins)}/負{len(loss)}, WR={100*len(wins)/len(all_trades):.1f}%)")
    print("=" * 84)
    print("各特徴を4分位に分けた勝率。勝ち/負けで平均差が大きい & 分位で勝率が単調な特徴が有望。\n")

    feats = [
        ("room_R", "room_R (M15余地/SL, 小=タイトSL)"),
        ("room_h1_R", "room_h1_R (H1余地/SL)"),
        ("pb_frac", "pb_frac (押し戻し深さ)"),
        ("sl_atr", "sl_atr (SLのタイトさ ATR単位)"),
        ("atr_pct", "atr_pct (ボラ regime %)"),
        ("hour", "hour (UTC時刻)"),
        ("pb_age", "pb_age (押し目転換からの本数)"),
        ("m30_align", "m30_align (M30も同方向か 0/1)"),
    ]
    for key, name in feats:
        nw, mw, sw = stats([t[key] for t in wins])
        nl, ml, sl_ = stats([t[key] for t in loss])
        flag = " ★差大" if (sw + sl_) > 0 and abs(mw - ml) > 0.4 * (sw + sl_) / 2 else ""
        print(f"■ {name}")
        print(f"   勝ち平均={mw:+.2f}  負け平均={ml:+.2f}  差={mw-ml:+.2f}{flag}")
        wr_by_quantile(all_trades, key, nq=4)
        print()


if __name__ == "__main__":
    main()
