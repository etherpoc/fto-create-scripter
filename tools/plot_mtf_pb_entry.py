"""
plot_mtf_pb_entry.py — エントリーごとに H4 / H1 / M15 / M5 を縦に並べて描画。

各 TF パネルに、その TF のローソク足 + ZigZag(エントリー時点で確定済みのピボットのみ) +
トレンド方向を表示。H4/H1 には v2 が判定するトレンドライン(直近2つの同種ピボット)を、
M15/M5 にはエントリー・SL・TP・リスク/リワード箱を描く。SL アンカー(直近M15安値/高値)は★で明示。

ピボットは「エントリー時点のスナップショット」を使うので、戦略が実際に見ていた構造を再現する
(確定ラグで後から出来る安値/高値は含めない)。

使い方:
    python tools/plot_mtf_pb_entry.py --symbol CADJPY --start 0 --ntrades 3
出力: data/charts/mtf_pb_<SYM>_entry<N>.png  (エントリーごとに 1 枚)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy, _dow_trend  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks  # noqa: E402


def snap(pivots):
    return [(p.time, p.price, p.kind) for p in pivots]


def run_capture(symbol, ticks, sl_buffer=0.0, structure=False,
                structure_close=False, recent_low_sl=False, tp_rr=1.0, dow_sl=False,
                room_r_max=0.0, min_sl_atr=0.3, block_hours=None, m15_stale=False):
    bh_start, bh_end = (block_hours if block_hours else (-1, -1))
    params = Params(skip_on_trendline_break=True, sl_buffer_atr=sl_buffer,
                    require_structure_intact=structure,
                    structure_use_close=structure_close,
                    sl_anchor_recent_low=recent_low_sl,
                    tp_rr=tp_rr, sl_dow_confirmed_swing=dow_sl,
                    room_R_max=room_r_max, min_sl_dist_atr=min_sl_atr,
                    block_hour_start=bh_start, block_hour_end=bh_end,
                    require_m15_unupdated_extreme=m15_stale)
    strat = MtfPullbackStrategy(params)
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
            ctx.mtf[3600].append(h1); last_h1 = h1.time
            if len(ctx.mtf[3600]) > 300: ctx.mtf[3600].pop(0)
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time
            if len(ctx.mtf[14400]) > 300: ctx.mtf[14400].pop(0)

        if open_trade is not None:
            hit = exitp = None
            if open_trade["side"] == "long":
                if m5.low <= open_trade["sl"]: hit, exitp = "SL", open_trade["sl"]
                elif m5.high >= open_trade["tp"]: hit, exitp = "TP", open_trade["tp"]
            else:
                if m5.high >= open_trade["sl"]: hit, exitp = "SL", open_trade["sl"]
                elif m5.low <= open_trade["tp"]: hit, exitp = "TP", open_trade["tp"]
            if hit:
                open_trade["exit_time"] = m5.time
                open_trade["exit_price"] = exitp
                open_trade["exit_reason"] = hit
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
                    sld = cmd.get("sl_dist") or (abs(entry - sl) if sl else 0)
                    if not sl or not tp or sld <= 0:
                        continue
                    open_trade = {
                        "side": side, "entry": entry, "sl": sl, "tp": tp, "sl_dist": sld,
                        "entry_time": m5.time,
                        "trends": {"h4": _dow_trend(strat.zz_h4.pivots), "h1": _dow_trend(strat.zz_h1.pivots),
                                   "m15": _dow_trend(strat.zz_m15.pivots), "m5": _dow_trend(strat.zz_m5.pivots)},
                        # エントリー時点で確定済みピボットのスナップショット
                        "piv": {"h4": snap(strat.zz_h4.pivots), "h1": snap(strat.zz_h1.pivots),
                                "m15": snap(strat.zz_m15.pivots), "m5": snap(strat.zz_m5.pivots)},
                    }
                    ctx._position = side
                    break

    # 各 TF の全バー (トラッカが保持)
    tf_bars = {"h4": list(strat.zz_h4.bars), "h1": list(strat.zz_h1.bars),
               "m15": list(strat.zz_m15.bars), "m5": list(strat.zz_m5.bars)}
    return trades, tf_bars


def dt(t):
    return datetime.fromtimestamp(t, tz=timezone.utc)


def bar_idx_at(bars, t):
    """time t を含むバー(= bars[i].time <= t < 次) の index。なければ最近傍。"""
    lo, hi = 0, len(bars) - 1
    if t <= bars[0].time:
        return 0
    if t >= bars[-1].time:
        return len(bars) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid].time <= t:
            lo = mid + 1
        else:
            hi = mid - 1
    return hi


def candles(ax, win):
    n = len(win)
    bw = max(0.7, min(2.6, 260.0 / max(1, n)))   # 本数が多いと細く
    for i, b in enumerate(win):
        c = "tab:green" if b.close >= b.open else "tab:red"
        ax.plot([i, i], [b.low, b.high], color=c, lw=bw * 0.35, zorder=1)
        ax.plot([i, i], [b.open, b.close], color=c, lw=bw, zorder=1, solid_capstyle="butt")


def trend_origin_time(pivots, trend):
    """現在トレンドの起点ピボットの time を返す。
    up: 末尾から遡って『higher low』が続く最古の安値 (= 上昇の起点)。
    down: 末尾から遡って『lower high』が続く最古の高値。
    """
    if trend == "up":
        lows = [(t, p) for (t, p, k) in pivots if k == "low"]
        if not lows:
            return None
        origin = lows[-1]
        for i in range(len(lows) - 2, -1, -1):
            if lows[i][1] < lows[i + 1][1]:   # より過去の安値がより低い = 上昇の一部
                origin = lows[i]
            else:
                break
        return origin[0]
    if trend == "down":
        highs = [(t, p) for (t, p, k) in pivots if k == "high"]
        if not highs:
            return None
        origin = highs[-1]
        for i in range(len(highs) - 2, -1, -1):
            if highs[i][1] > highs[i + 1][1]:
                origin = highs[i]
            else:
                break
        return origin[0]
    return None


def x_in_win(win, t):
    """time t に対応する窓内 x。範囲外なら None。"""
    if t < win[0].time or t > win[-1].time:
        # 範囲少し外でも端にクランプ (entry 後すぐ等)
        if win[0].time <= t <= win[-1].time + 1:
            return len(win) - 1
        return None
    return bar_idx_at(win, t)


def draw_zigzag(ax, win, pivots, label="ZigZag"):
    pts = []
    for (pt, pr, k) in pivots:
        x = x_in_win(win, pt)
        if x is not None:
            pts.append((x, pr, k))
    if len(pts) >= 2:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color="tab:blue",
                lw=1.1, marker="o", ms=4, alpha=0.7, zorder=3, label=label)
    return pts


def draw_trendline(ax, win, pivots, trend, xmap, origin_t=None):
    """トレンドラインを窓全域に延長して描く。
    起点アンカー = トレンド起点ピボット (origin_t)、終点 = 直近同種ピボット。
    (起点から引くことで『トレンドの開始地点』が正しく反映される)"""
    kind = "low" if trend == "up" else ("high" if trend == "down" else None)
    if kind is None:
        return
    same = [(pt, pr) for (pt, pr, k) in pivots if k == kind]
    if len(same) < 2:
        return
    # 起点 = origin_t に一致するピボット (無ければ最古の同種)
    p_origin = None
    if origin_t is not None:
        for (pt, pr) in same:
            if pt == origin_t:
                p_origin = (pt, pr)
                break
    (t1, p1) = p_origin if p_origin is not None else same[0]
    (t2, p2) = same[-1]
    if t1 == t2:
        (t1, p1) = same[-2]
    x1, x2 = xmap(t1), xmap(t2)
    if x2 == x1:
        return
    slope = (p2 - p1) / (x2 - x1)
    xs = [0, len(win) - 1]
    ys = [p2 + slope * (x - x2) for x in xs]
    ax.plot(xs, ys, ls="--", color="purple", lw=1.5, zorder=2,
            label=f"{'support' if trend=='up' else 'resistance'} trendline (v2)")
    # 窓内に入るアンカーだけ点を打つ
    for xa, pa in ((x1, p1), (x2, p2)):
        if 0 <= xa <= len(win) - 1:
            ax.scatter([xa], [pa], color="purple", s=45, zorder=4)


def set_xticks(ax, win):
    n = len(win)
    idxs = [int(i) for i in (0, n * 0.25, n * 0.5, n * 0.75, n - 1)]
    ax.set_xticks(idxs)
    ax.set_xticklabels([dt(win[i].time).strftime("%m-%d\n%H:%M") for i in idxs], fontsize=7)


def plot_entry(symbol, trade, tf_bars, out_path):
    et = trade["entry_time"]
    xt_time = trade.get("exit_time", et)
    tr = trade["trends"]
    after_map = {"h4": 14, "h1": 18, "m15": 25, "m5": 50}   # 決済が近い時の最低 after
    tail_map = {"h4": 14, "h1": 30, "m15": 70, "m5": 220}    # 決済地点より後ろに見せる本数
    max_after = {"h4": 75, "h1": 160, "m15": 380, "m5": 1100}  # エントリーからの上限
    minbars = {"h4": 120, "h1": 90, "m15": 60}     # 最低でもこれだけ遡る (H4 を広く)
    maxbars = {"h4": 260, "h1": 300, "m15": 340}   # トレンドが長すぎる時の上限
    order = ["h4", "h1", "m15", "m5"]
    fig, axes = plt.subplots(4, 1, figsize=(14, 16))

    for ax, tf in zip(axes, order):
        bars = tf_bars[tf]
        ei = bar_idx_at(bars, et)
        after = after_map[tf]
        if tf == "m5":
            lo = max(0, ei - 160)            # M5 は従来通りエントリー拡大表示
        else:
            # H4/H1/M15 は「トレンドの起点」から (ただし最低 minbars 本は遡って広く描く)
            ot = trend_origin_time(trade["piv"][tf], tr.get(tf))
            oi = bar_idx_at(bars, ot) if ot else (ei - minbars[tf])
            lo = min(oi - 5, ei - minbars[tf])  # 起点 or 最低幅、より過去の方を採用
            lo = max(0, lo, ei - maxbars[tf])   # 長すぎる時は上限でクランプ
        # 決済地点 + テール(後ろ)まで描画。最低でも after 本、上限 max_after でクランプ。
        xi = bar_idx_at(bars, xt_time)
        end = max(ei + after, xi + tail_map[tf])
        end = min(end, ei + max_after[tf])
        hi = min(len(bars), end + 1)
        win = bars[lo:hi]
        if len(win) < 3:
            ax.axis("off"); continue
        candles(ax, win)
        draw_zigzag(ax, win, trade["piv"][tf])

        # time -> 窓内相対 x (窓外でも負値で返す。トレンドライン延長用)
        def xmap(t, _bars=bars, _lo=lo):
            return bar_idx_at(_bars, t) - _lo

        up = trade["side"] == "long"
        ex = x_in_win(win, et)

        # --- エントリー / SL / TP は全パネルに描く (H4/H1 でもエントリー位置が分かるように) ---
        ax.axhline(trade["entry"], color="black", lw=0.9, alpha=0.8)
        ax.axhline(trade["sl"], color="tab:red", ls="--", lw=1.1, label="SL")
        ax.axhline(trade["tp"], color="tab:green", ls="--", lw=1.1, label="TP")
        if ex is not None:
            ax.axvline(ex, color="black", lw=1.4, alpha=0.55, ls=":")
            ax.scatter([ex], [trade["entry"]], marker="^" if up else "v", s=190,
                       color="black", zorder=8, edgecolor="white", lw=0.7,
                       label=("BUY entry" if up else "SELL entry"))
        # --- 決済マーカー (TP=緑X / SL=赤X) ---
        exx = x_in_win(win, xt_time)
        if exx is not None and trade.get("exit_reason"):
            er = trade["exit_reason"]
            ax.axvline(exx, color="0.35", lw=1.0, alpha=0.45, ls=":")
            ax.scatter([exx], [trade["exit_price"]], marker="X", s=180,
                       color="tab:green" if er == "TP" else "tab:red", zorder=9,
                       edgecolor="black", lw=0.6, label="exit " + er)

        if tf in ("h4", "h1"):
            ot_tf = trend_origin_time(trade["piv"][tf], tr.get(tf))
            draw_trendline(ax, win, trade["piv"][tf], tr.get(tf), xmap, ot_tf)
        if tf in ("m15", "m5"):
            # リスク/リワード箱 (entry→exit)
            xt = x_in_win(win, trade.get("exit_time", et)) or (len(win) - 1)
            x0 = ex if ex is not None else 0
            ax.fill_between([x0, xt], trade["entry"], trade["sl"], color="tab:red", alpha=0.12)
            ax.fill_between([x0, xt], trade["entry"], trade["tp"], color="tab:green", alpha=0.12)
        if tf == "m15":
            # SL アンカー (直近 M15 安値/高値ピボット) を ★ で明示
            up = trade["side"] == "long"
            kind = "low" if up else "high"
            same = [(pt, pr) for (pt, pr, k) in trade["piv"]["m15"] if k == kind]
            if same:
                apt, apr = same[-1]
                ax_x = x_in_win(win, apt)
                if ax_x is not None:
                    ax.scatter([ax_x], [apr], marker="*", s=320, color="orange",
                               edgecolor="black", lw=0.6, zorder=7,
                               label="SL anchor (last M15 %s)" % kind)

        set_xticks(ax, win)
        ax.set_ylabel(tf.upper(), fontsize=11, fontweight="bold")
        ax.set_title(f"{tf.upper()}  trend@entry = {tr.get(tf)}", fontsize=9, loc="left")
        ax.legend(fontsize=7, loc="best", framealpha=0.9)
        ax.grid(alpha=0.15)

    R = (trade["exit_price"] - trade["entry"]) / trade["sl_dist"] * (1 if trade["side"] == "long" else -1)
    fig.suptitle(f"{symbol}  {trade['side'].upper()} @ {dt(et):%Y-%m-%d %H:%M}   "
                 f"entry={trade['entry']:.4f} SL={trade['sl']:.4f} TP={trade['tp']:.4f}  "
                 f"-> {trade['exit_reason']} (R={R:+.2f})    "
                 f"align H4={tr['h4']} H1={tr['h1']} M15={tr['m15']} M5={tr['m5']}",
                 fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.985])
    fig.savefig(out_path, dpi=105)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="CADJPY")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--ntrades", type=int, default=3)
    ap.add_argument("--sl-buffer", type=float, default=0.0, help="SL バッファ (ATR 倍)。例 0.3")
    ap.add_argument("--structure", action="store_true", help="構造インタクト確認を有効化")
    ap.add_argument("--structure-close", action="store_true", help="構造判定を終値ベースに")
    ap.add_argument("--recent-low-sl", action="store_true", help="SL を直近実安値(ヒゲ)の下に")
    ap.add_argument("--final", action="store_true", help="最終形 (structCLOSE + 直近安値SL + buf0.3) を一括指定")
    ap.add_argument("--tp-rr", type=float, default=1.5, help="TP RR 倍率 (既定 1.5 = ライブと同じ)")
    ap.add_argument("--dow-sl", action="store_true", help="SL をダウ確定スイングにする")
    ap.add_argument("--room-r-max", type=float, default=0.0, help="room_R>=この値を除外 (例 2.5)")
    ap.add_argument("--min-sl-atr", type=float, default=0.3, help="最小SL幅 (ATR倍, 既定0.3)")
    ap.add_argument("--block-hours", type=str, default="", help="除外時間帯 UTC 例 '6,10' で6-10時")
    ap.add_argument("--best", action="store_true", help="現ベスト一括: room_R<2.0 + 6-10時除外 + minSL2.0")
    ap.add_argument("--m15-stale", action="store_true", help="新条件: M15極値未更新 (直近M15ピボットが逆側)")
    args = ap.parse_args()

    if args.best:
        args.tp_rr = 1.5
        args.room_r_max = 2.0
        args.min_sl_atr = 2.0
        args.block_hours = "6,10"

    if args.final:
        args.structure = True
        args.structure_close = True
        args.recent_low_sl = True
        if not args.sl_buffer:
            args.sl_buffer = 0.3

    print(f"loading {args.symbol} ...  (buf={args.sl_buffer} struct={args.structure} "
          f"close={args.structure_close} recentSL={args.recent_low_sl})")
    ticks = load_ticks(args.symbol)
    print(f"  {len(ticks)} ticks. running ...")
    bh = None
    if args.block_hours:
        a, b = args.block_hours.split(",")
        bh = (int(a), int(b))
    trades, tf_bars = run_capture(args.symbol, ticks, sl_buffer=args.sl_buffer,
                                  structure=args.structure, structure_close=args.structure_close,
                                  recent_low_sl=args.recent_low_sl, tp_rr=args.tp_rr, dow_sl=args.dow_sl,
                                  room_r_max=args.room_r_max, min_sl_atr=args.min_sl_atr, block_hours=bh,
                                  m15_stale=args.m15_stale)
    print(f"  total trades = {len(trades)}")

    out_dir = ROOT / "data" / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.final:
        suffix = "_FINAL"
    elif args.sl_buffer:
        suffix = f"_buf{args.sl_buffer}".replace(".", "p")
    else:
        suffix = ""
    sel = trades[args.start:args.start + args.ntrades]
    for i, tr in enumerate(sel):
        n = args.start + i
        p = out_dir / f"mtf_pb_{args.symbol}_entry{n}{suffix}.png"
        plot_entry(args.symbol, tr, tf_bars, p)
        print(f"  saved {p}  ({tr['side']} -> {tr['exit_reason']})")


if __name__ == "__main__":
    main()
