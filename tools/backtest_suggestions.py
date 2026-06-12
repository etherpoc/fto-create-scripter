"""
backtest_suggestions.py — FTO 自動分析の提案を WF で論理検証する。

テスト対象:
  #3 損失後クールダウン (リベンジ回避): 損失トレードの後、その保有時間の k 倍以内の
     新規エントリー(同一ペア)を除外。
  #5 時間エグジット (最大保有): SL/TP 到達前でも N 本経過で建値クローズ。
  #2 反マルチンゲール (連勝後サイズ倍々): 2連勝後、損失が出るまでサイズを倍化。
     ※過剰適合の反証目的。in-sample vs P1/P2 と DD で脆さを示す。

baseline = 現ベスト (H1+M15 + room_R<2.0 + 6-10時除外, RR1.5)。
評価: robust4 / 全12 で sumR を P1/P2 に分割 + ポートフォリオ総DD。1R=口座1%。

使い方:
    python tools/backtest_suggestions.py            # robust4
    python tools/backtest_suggestions.py --all
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks  # noqa: E402

ROBUST4 = ["EURUSD", "USDJPY", "GBPUSD", "EURJPY"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
PARAMS = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15",
              room_R_max=2.0, block_hour_start=6, block_hour_end=10)
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30


def capture(symbol, ticks, max_hold=0):
    """max_hold>0 なら N 本経過で建値クローズ。trades に bars_held を含む。"""
    strat = MtfPullbackStrategy(Params(**PARAMS))
    strat.symbol = symbol
    ctx = SimContext()
    last_h1 = last_h4 = -1
    ot = None
    trades = []
    for (m5, h1, h4) in ticks:
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60:
            ctx.bars_seq.pop(0)
        if h1 is not None and h1.time > last_h1:
            ctx.mtf[3600].append(h1); last_h1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        if ot is not None:
            ot["bars"] += 1
            hit = exitp = None
            if ot["side"] == "long":
                if m5.low <= ot["sl"]: hit, exitp = "sl", ot["sl"]
                elif m5.high >= ot["tp"]: hit, exitp = "tp", ot["tp"]
            else:
                if m5.high >= ot["sl"]: hit, exitp = "sl", ot["sl"]
                elif m5.low <= ot["tp"]: hit, exitp = "tp", ot["tp"]
            if not hit and max_hold and ot["bars"] >= max_hold:
                hit, exitp = "time", m5.close   # 時間切れ=建値近似クローズ
            if hit:
                pnl = (exitp - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - exitp)
                trades.append({"pair": symbol, "entry_t": ot["entry_t"], "exit_t": int(m5.time),
                               "R": pnl / ot["sld"], "bars": ot["bars"], "side": ot["side"]})
                ot = None; ctx._position = None
        ctx.pending_commands = []
        try:
            strat.on_bar(ctx)
        except Exception:
            pass
        if ot is None:
            for cmd in ctx.pending_commands:
                if cmd["type"] in ("buy", "sell"):
                    side = "long" if cmd["type"] == "buy" else "short"
                    entry = cmd.get("entry_price") or m5.close
                    sl, tp = cmd.get("sl"), cmd.get("tp")
                    sld = abs(entry - sl) if sl else 0
                    if not sl or not tp or sld <= 0:
                        continue
                    ot = {"side": side, "entry": entry, "sl": sl, "tp": tp, "sld": sld,
                          "entry_t": int(m5.time), "bars": 0}
                    ctx._position = side
                    break
    return trades


def split(trades):
    p1 = sum(t["R"] for t in trades if t["entry_t"] < P1_END)
    p2 = sum(t["R"] for t in trades if t["entry_t"] >= P1_END)
    return p1, p2


def maxdd(trades, weight=None):
    """決済時刻順に R(×weight) を積んで総DDを返す。weight: t->size。"""
    ev = sorted(trades, key=lambda x: x["exit_t"])
    eq = peak = dd = 0.0
    for t in ev:
        w = weight(t) if weight else 1.0
        eq += t["R"] * w
        peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def loss_cooldown(trades, k):
    """#3: 損失後、その保有時間の k 倍以内の同一ペア新規を除外 (ペア毎時系列)。"""
    by_pair = {}
    for t in sorted(trades, key=lambda x: x["entry_t"]):
        by_pair.setdefault(t["pair"], []).append(t)
    kept = []
    for pair, ts in by_pair.items():
        block_until = -1
        for t in ts:
            if t["entry_t"] < block_until:
                continue   # クールダウン中=スキップ
            kept.append(t)
            if t["R"] < 0:   # 損失なら次をブロック
                dur = t["exit_t"] - t["entry_t"]
                block_until = t["exit_t"] + int(k * dur)
    return kept


def martingale(trades):
    """#2: ペア毎、2連勝後に損失まで size を倍化。(equity, dd, p1, p2) を返す。"""
    by_pair = {}
    for t in sorted(trades, key=lambda x: x["entry_t"]):
        by_pair.setdefault(t["pair"], []).append(t)
    weighted = []
    for pair, ts in by_pair.items():
        streak = 0; size = 1.0
        for t in ts:
            t2 = dict(t); t2["w"] = size
            weighted.append(t2)
            if t["R"] > 0:
                streak += 1
                if streak >= 2:
                    size *= 2.0   # 2連勝以降は倍々
            else:
                streak = 0; size = 1.0
    p1 = sum(t["R"] * t["w"] for t in weighted if t["entry_t"] < P1_END)
    p2 = sum(t["R"] * t["w"] for t in weighted if t["entry_t"] >= P1_END)
    eq, dd = maxdd(weighted, weight=lambda t: t["w"])
    return eq, dd, p1, p2, max(t["w"] for t in weighted)


def wr(trades):
    n = len(trades); w = sum(1 for t in trades if t["R"] > 0)
    return 100 * w / n if n else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else ROBUST4
    tag = "全12" if args.all else "robust4"

    base = []
    for p in pairs:
        base += capture(p, load_ticks(p), max_hold=0)
        print(f"  {p} done", flush=True)

    bp1, bp2 = split(base)
    beq, bdd = maxdd(base)
    print("\n" + "=" * 78)
    print(f"baseline ({tag}, 現ベスト)  N={len(base)}  WR={wr(base):.0f}%")
    print(f"  sumR P1 {bp1:+.1f} ({bp1/P1_M:+.2f}%/月)  P2 {bp2:+.1f} ({bp2/P2_M:+.2f}%/月)  総DD {bdd:.1f}%")
    print("=" * 78)

    # #3 損失後クールダウン
    print(f"\n■ #3 損失後クールダウン (損失の保有時間×k 以内の同ペア再エントリー除外)")
    print(f"  {'k':>4} | {'N':>4} {'WR':>4} | {'P1':>7} {'P2':>7} | {'P2/月':>7} | {'総DD':>6} | 判定")
    for k in [0.5, 1.0, 2.0, 3.0]:
        kept = loss_cooldown(base, k)
        p1, p2 = split(kept); eq, dd = maxdd(kept)
        verdict = "改善" if (p1 > bp1 and p2 > bp2) else ("P2のみ改善" if p2 > bp2 else "悪化/同等")
        print(f"  {k:>4} | {len(kept):>4} {wr(kept):>3.0f}% | {p1:>+7.1f} {p2:>+7.1f} | {p2/P2_M:>+6.2f}% | {dd:>5.1f}% | {verdict}")

    # #2 反マルチンゲール
    print(f"\n■ #2 反マルチンゲール (2連勝後に損失までサイズ倍々) ※過剰適合の反証")
    eq, dd, p1, p2, mw = martingale(base)
    print(f"  in-sample 累積 {eq:+.1f}R (base {beq:+.1f}R)  最大サイズ倍率 {mw:.0f}x")
    print(f"  P1 {p1:+.1f} (base {bp1:+.1f})  P2 {p2:+.1f} (base {bp2:+.1f})  総DD {dd:.1f}% (base {bdd:.1f}%)")
    pass_prop = "✅" if dd < 10 else "❌プロップ即失格"
    print(f"  → 総DD {dd:.1f}% : {pass_prop}  / 両期間プラス維持: {'はい' if (p1>0 and p2>0) else 'いいえ'}")

    # #5 時間エグジット
    print(f"\n■ #5 時間エグジット (N本経過で建値クローズ)  ※要再シミュレーション")
    print(f"  {'maxhold':>8} | {'N':>4} {'WR':>4} | {'P1':>7} {'P2':>7} | {'P2/月':>7} | {'総DD':>6} | 判定")
    for mh in [12, 24, 48, 96]:   # M5本数 = 1h/2h/4h/8h
        tt = []
        for p in pairs:
            tt += capture(p, load_ticks(p), max_hold=mh)
        p1, p2 = split(tt); eq, dd = maxdd(tt)
        verdict = "改善" if (p1 > bp1 and p2 > bp2) else ("P2のみ改善" if p2 > bp2 else "悪化")
        print(f"  {mh:>4}本({mh*5//60}h) | {len(tt):>4} {wr(tt):>3.0f}% | {p1:>+7.1f} {p2:>+7.1f} | {p2/P2_M:>+6.2f}% | {dd:>5.1f}% | {verdict}", flush=True)


if __name__ == "__main__":
    main()
