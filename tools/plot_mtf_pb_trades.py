"""
plot_mtf_pb_trades.py — mtf_pullback v2 のトレードをチャート描画してロジックを目視検証。

検証済み MtfPullbackStrategy を実データで回し、各トレードの
エントリー位置 / SL / TP / 決済 / M15 ZigZag 構造 を重ねた PNG を出す。
「SL が本当に直近 M15 スイングに乗っているか」「エントリーが押し目方向か」を目で確認できる。

使い方:
    python tools/plot_mtf_pb_trades.py --symbol CADJPY --ntrades 6 --start 0
    python tools/plot_mtf_pb_trades.py --symbol EURUSD --ntrades 6
出力: data/charts/mtf_pb_<SYMBOL>_trades.png  (+ overview)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy, _dow_trend  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks  # noqa: E402


def run_capture(symbol: str, ticks):
    """戦略を回し、全 M5 バー・全トレード・M15 ピボットを記録して返す。"""
    params = Params(skip_on_trendline_break=True)
    strat = MtfPullbackStrategy(params)
    strat.symbol = symbol
    ctx = SimContext()
    last_h1 = last_h4 = -1
    open_trade = None
    trades = []
    bars = []  # (time, open, high, low, close)

    for (m5, h1, h4) in ticks:
        idx = len(bars)
        bars.append((m5.time, m5.open, m5.high, m5.low, m5.close))
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 40:
            ctx.bars_seq.pop(0)
        if h1 is not None and h1.time > last_h1:
            ctx.mtf[3600].append(h1); last_h1 = h1.time
            if len(ctx.mtf[3600]) > 200: ctx.mtf[3600].pop(0)
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time
            if len(ctx.mtf[14400]) > 200: ctx.mtf[14400].pop(0)

        # SL/TP 決済
        if open_trade is not None:
            hit = exitp = None
            if open_trade["side"] == "long":
                if m5.low <= open_trade["sl"]: hit, exitp = "SL", open_trade["sl"]
                elif m5.high >= open_trade["tp"]: hit, exitp = "TP", open_trade["tp"]
            else:
                if m5.high >= open_trade["sl"]: hit, exitp = "SL", open_trade["sl"]
                elif m5.low <= open_trade["tp"]: hit, exitp = "TP", open_trade["tp"]
            if hit:
                open_trade["exit_idx"] = idx
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
                    open_trade = {"side": side, "entry": entry, "sl": sl, "tp": tp,
                                  "sl_dist": sld, "entry_idx": idx, "entry_time": m5.time,
                                  # エントリー時の各 TF トレンド (ロジック条件の事後検証用)
                                  "trends": {
                                      "h4": _dow_trend(strat.zz_h4.pivots),
                                      "h1": _dow_trend(strat.zz_h1.pivots),
                                      "m30": _dow_trend(strat.zz_m30.pivots),
                                      "m15": _dow_trend(strat.zz_m15.pivots),
                                      "m5": _dow_trend(strat.zz_m5.pivots),
                                  }}
                    ctx._position = side
                    break

    # 最終的な M15 ピボット (time, price, kind)
    m15_piv = [(p.time, p.price, p.kind) for p in strat.zz_m15.pivots]
    return bars, trades, m15_piv


def dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def plot_trade(ax, bars, trade, m15_piv):
    e, x = trade["entry_idx"], trade.get("exit_idx", trade["entry_idx"])
    lo = max(0, e - 150)
    hi = min(len(bars), x + 40)
    seg = bars[lo:hi]
    times = [dt(b[0]) for b in seg]
    closes = [b[4] for b in seg]
    highs = [b[2] for b in seg]
    lows = [b[3] for b in seg]
    t0, t1 = seg[0][0], seg[-1][0]

    ax.fill_between(times, lows, highs, color="0.88", linewidth=0)   # high-low range
    ax.plot(times, closes, color="0.25", lw=0.9)                     # close line

    # M15 ZigZag: connect pivots in window (shows the swing structure SL anchors to)
    zz = [(dt(pt), pr) for (pt, pr, k) in m15_piv if t0 <= pt <= t1]
    if len(zz) >= 2:
        ax.plot([z[0] for z in zz], [z[1] for z in zz], color="tab:blue", lw=1.1,
                marker="o", ms=3, alpha=0.65, label="M15 ZigZag")

    et = dt(trade["entry_time"])
    xt = dt(trade["exit_time"])
    up = trade["side"] == "long"

    # Risk box (entry<->SL, red) and Reward box (entry<->TP, green) over entry..exit
    ax.fill_between([et, xt], trade["entry"], trade["sl"], color="tab:red", alpha=0.13)
    ax.fill_between([et, xt], trade["entry"], trade["tp"], color="tab:green", alpha=0.13)
    ax.hlines(trade["entry"], et, xt, color="black", lw=1.0)
    ax.hlines(trade["sl"], et, xt, color="tab:red", ls="--", lw=1.2, label="SL")
    ax.hlines(trade["tp"], et, xt, color="tab:green", ls="--", lw=1.2, label="TP")

    # entry marker
    ax.scatter([et], [trade["entry"]], marker="^" if up else "v",
               s=170, color="black", zorder=6,
               label=("BUY (long)" if up else "SELL (short)"))
    # exit marker
    reason = trade["exit_reason"]
    ax.scatter([xt], [trade["exit_price"]], marker="X", s=150,
               color="tab:green" if reason == "TP" else "tab:red", zorder=6,
               edgecolor="black", lw=0.5, label="exit " + reason)

    R = (trade["exit_price"] - trade["entry"]) / trade["sl_dist"] * (1 if up else -1)
    sl_side = "below" if trade["sl"] < trade["entry"] else "above"
    tr = trade.get("trends", {})
    want = "up" if up else "down"
    aligned = (tr.get("h4") == want and tr.get("h1") == want and tr.get("m15") == want)
    trend_txt = (f"trend@entry H4={tr.get('h4')} H1={tr.get('h1')} M30={tr.get('m30')} "
                 f"M15={tr.get('m15')} M5={tr.get('m5')}  "
                 f"[align={'OK' if aligned else 'MISMATCH!'}]")
    ax.set_title(f"{'LONG' if up else 'SHORT'}  entry={trade['entry']:.4f}  "
                 f"SL={trade['sl']:.4f} ({sl_side})  TP={trade['tp']:.4f}  "
                 f"-> {reason} (R={R:+.2f})   {et:%Y-%m-%d %H:%M}\n{trend_txt}",
                 fontsize=8.5, color=("black" if aligned else "red"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=7, loc="best", framealpha=0.9)
    ax.grid(alpha=0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="CADJPY")
    ap.add_argument("--ntrades", type=int, default=6)
    ap.add_argument("--start", type=int, default=0, help="何番目のトレードから描くか")
    args = ap.parse_args()

    print(f"loading {args.symbol} ...")
    ticks = load_ticks(args.symbol)
    print(f"  {len(ticks)} M5 ticks. running strategy ...")
    bars, trades, m15_piv = run_capture(args.symbol, ticks)
    print(f"  total trades = {len(trades)}")
    if not trades:
        print("no trades.")
        return

    out_dir = ROOT / "data" / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)

    sel = trades[args.start:args.start + args.ntrades]
    n = len(sel)
    fig, axes = plt.subplots(n, 1, figsize=(13, 3.2 * n))
    axes = axes if n > 1 else [axes]
    for i, tr in enumerate(sel):
        plot_trade(axes[i], bars, tr, m15_piv)
    fig.suptitle(f"mtf_pullback v2 - {args.symbol}  trades #{args.start}..{args.start + n - 1}   "
                 f"(black=entry, red dashed=SL, green dashed=TP, blue=M15 ZigZag, "
                 f"red box=risk, green box=reward)", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.99])
    p = out_dir / f"mtf_pb_{args.symbol}_trades.png"
    fig.savefig(p, dpi=110)
    print(f"saved: {p}")

    # 勝敗サマリ
    wins = sum(1 for t in trades if
               ((t["exit_price"] - t["entry"]) * (1 if t["side"] == "long" else -1)) > 0)
    print(f"WR = {100*wins/len(trades):.1f}%  ({wins}W / {len(trades)-wins}L)")

    # ★ 全トレードのロジック検証: エントリー時に H4/H1/M15 が方向一致していたか
    bad = []
    for i, t in enumerate(trades):
        want = "up" if t["side"] == "long" else "down"
        tr = t.get("trends", {})
        if not (tr.get("h4") == want and tr.get("h1") == want and tr.get("m15") == want):
            bad.append((i, t["side"], tr))
        # SL/TP の向きと 1:1 もチェック
        up = t["side"] == "long"
        sl_ok = (t["sl"] < t["entry"]) if up else (t["sl"] > t["entry"])
        tp_ok = (t["tp"] > t["entry"]) if up else (t["tp"] < t["entry"])
        rr = abs(t["tp"] - t["entry"]) / abs(t["entry"] - t["sl"])
        if not sl_ok or not tp_ok or abs(rr - 1.0) > 0.02:
            bad.append((i, "SLTP", {"sl_ok": sl_ok, "tp_ok": tp_ok, "rr": round(rr, 3)}))
    if bad:
        print(f"!!! ロジック不整合 {len(bad)} 件:")
        for b in bad[:10]:
            print("   ", b)
    else:
        print("OK: 全トレードでエントリー時 H4=H1=M15 が方向一致 / SL・TP の向きと 1:1 RR も正常")


if __name__ == "__main__":
    main()
