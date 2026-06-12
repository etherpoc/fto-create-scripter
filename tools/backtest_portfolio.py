"""
backtest_portfolio.py — プロップ(Fintokei)の同時リスク制約下でのポートフォリオ検証。

ルール:
  - 同時保有リスク合計 <= MAX_TOTAL_RISK (既定 3%)
  - 1 ポジションのリスク <= PER_POS_RISK (既定 1.0%、最大 1.5%)

全ペアを時間同期して回し、新規エントリー時点の「保有中ポジションのリスク合計 + 新規リスク」
が上限を超える場合はそのエントリーを **ブロック** する。
→ 「複数ペアが同時にシグナルを出す利益クラスタ」がどれだけ抑制されるかを定量化。

エントリー設定 = 現ベスト (H1+M15 + room_R<2.0 + 6-10時除外, RR1.5)。
SL リスク = PER_POS_RISK。勝ち = +tp_rr×risk、負け = -1×risk。

使い方:
    python tools/backtest_portfolio.py                  # robust4 + per_pos 1.0%
    python tools/backtest_portfolio.py --per-pos 1.5
    python tools/backtest_portfolio.py --all            # 全12ペアのバスケット
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

ROBUST4 = ["EURUSD", "USDJPY", "GBPUSD", "EURJPY"]   # H1+M15 で全12中 robust だった4
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]

PARAMS = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15",
              room_R_max=2.0, block_hour_start=6, block_hour_end=10)

P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_MONTHS, P2_MONTHS = 36, 30


def capture_trades(symbol, ticks):
    """(entry_time, exit_time, R, pair) のリストを返す。"""
    strat = MtfPullbackStrategy(Params(**PARAMS))
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
        if open_trade is not None:
            hit = exitp = None
            if open_trade["side"] == "long":
                if m5.low <= open_trade["sl"]: hit, exitp = "sl", open_trade["sl"]
                elif m5.high >= open_trade["tp"]: hit, exitp = "tp", open_trade["tp"]
            else:
                if m5.high >= open_trade["sl"]: hit, exitp = "sl", open_trade["sl"]
                elif m5.low <= open_trade["tp"]: hit, exitp = "tp", open_trade["tp"]
            if hit:
                pnl = (exitp - open_trade["entry"]) if open_trade["side"] == "long" else (open_trade["entry"] - exitp)
                trades.append({"pair": symbol, "entry_t": open_trade["entry_t"],
                               "exit_t": int(m5.time), "R": pnl / open_trade["sld"],
                               "side": open_trade["side"]})
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
                    open_trade = {"side": side, "entry": entry, "sl": sl, "tp": tp,
                                  "sld": sld, "entry_t": int(m5.time)}
                    ctx._position = side
                    break
    return trades


def _ccys(pair):
    """ペア名から (base, quote) を返す。XAUUSD 等も対応。"""
    return pair[:3], pair[3:6]


def portfolio_sim(trades, per_pos, max_total, corr_cap=0.0):
    """時系列でポートフォリオ制約を適用。accepted/blocked に分ける。
    corr_cap>0 なら、新規エントリー後の各通貨の純露出(risk%)が corr_cap を超えるならブロック。"""
    trades = sorted(trades, key=lambda x: x["entry_t"])
    open_pos = []   # (exit_t, risk, pair, side)
    accepted, blocked = [], []
    max_concurrent = 0
    for tr in trades:
        t = tr["entry_t"]
        open_pos = [o for o in open_pos if o[0] > t]   # 決済済みを解放
        cur_risk = sum(o[1] for o in open_pos)
        ok = cur_risk + per_pos <= max_total + 1e-9
        if ok and corr_cap > 0:
            # 純通貨露出を計算 (long: +base/-quote, short: 逆)
            net = {}
            for (_, r, p, s) in open_pos + [(0, per_pos, tr["pair"], tr["side"])]:
                b, q = _ccys(p)
                sgn = 1 if s == "long" else -1
                net[b] = net.get(b, 0) + sgn * r
                net[q] = net.get(q, 0) - sgn * r
            if any(abs(v) > corr_cap + 1e-9 for v in net.values()):
                ok = False
        if ok:
            accepted.append(tr)
            open_pos.append((tr["exit_t"], per_pos, tr["pair"], tr["side"]))
            max_concurrent = max(max_concurrent, len(open_pos))
        else:
            blocked.append(tr)
    return accepted, blocked, max_concurrent


def ret_split(trades, per_pos):
    """P1/P2 の合計リターン(%) を返す (R×per_pos の和)。"""
    p1 = sum(x["R"] for x in trades if x["entry_t"] < P1_END) * per_pos
    p2 = sum(x["R"] for x in trades if x["entry_t"] >= P1_END) * per_pos
    return p1, p2


def equity_dd(trades, per_pos):
    """決済時刻順に損益を積んで、合計リターンと最大DD(%)を返す。"""
    ev = sorted(trades, key=lambda x: x["exit_t"])
    eq = 0.0; peak = 0.0; maxdd = 0.0
    for x in ev:
        eq += x["R"] * per_pos
        peak = max(peak, eq)
        maxdd = max(maxdd, peak - eq)
    return eq, maxdd


def daily_loss(trades, per_pos):
    """決済日(UTC)ごとの損益を集計し、最悪日(最大損失%)と 5%超過日数を返す。"""
    by_day = {}
    for x in trades:
        day = datetime.fromtimestamp(x["exit_t"], tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0.0) + x["R"] * per_pos
    worst = min(by_day.values()) if by_day else 0.0
    breaches = sum(1 for v in by_day.values() if v <= -5.0)
    worst_day = min(by_day, key=by_day.get) if by_day else "-"
    return worst, worst_day, breaches, len(by_day)


def sweep(all_trades, max_total, corr_cap=0.0):
    """per_pos を振って、リターン/総DD/最悪日/プロップ合否を表化。
    プロップ合格 = 総DD<10% かつ 最悪日>-5%。"""
    print("\n" + "=" * 92)
    tag = f"  相関キャップ={corr_cap}%" if corr_cap else ""
    print(f"サイジング sweep (同時上限{max_total}%{tag}) — プロップ合格 = 総DD<10% & 日次>-5%")
    print("=" * 92)
    print(f"{'1ポジ%':>6} | {'採用/ブロック':>12} | {'P2月利':>7} | {'累積':>7} | "
          f"{'総DD':>6} | {'最悪日':>7} | {'5%超':>4} | 合否")
    print("-" * 92)
    for pp in [0.5, 0.75, 1.0, 1.25, 1.5]:
        acc, blk, _ = portfolio_sim(all_trades, pp, max_total, corr_cap)
        _, ap2 = ret_split(acc, pp)
        aeq, add = equity_dd(acc, pp)
        aw, _, ab, _ = daily_loss(acc, pp)
        ok = "✅PASS" if (add < 10.0 and aw > -5.0) else "❌FAIL"
        print(f"{pp:>6.2f} | {len(acc):>5}/{len(blk):<6} | {ap2/P2_MONTHS:>+6.2f}% | "
              f"{aeq:>+6.1f}% | {add:>5.1f}% | {aw:>+6.1f}% | {ab:>4} | {ok}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--per-pos", type=float, default=1.0, help="1ポジのリスク%% (<=1.5)")
    ap.add_argument("--max-total", type=float, default=3.0, help="同時保有リスク上限%%")
    ap.add_argument("--sweep", action="store_true", help="per_pos を振って表化")
    ap.add_argument("--corr-cap", type=float, default=0.0, help="同一通貨の純露出上限%% (0=off)")
    ap.add_argument("--pairs", type=str, default="", help="カンマ区切りの任意バスケット")
    args = ap.parse_args()
    pairs = args.pairs.split(",") if args.pairs else (ALL if args.all else ROBUST4)
    per_pos = args.per_pos
    max_total = args.max_total

    all_trades = []
    for p in pairs:
        all_trades += capture_trades(p, load_ticks(p))
        print(f"  {p} done")

    if args.sweep:
        sweep(all_trades, max_total, 0.0)
        if args.corr_cap:
            sweep(all_trades, max_total, args.corr_cap)
        return

    print("\n" + "=" * 84)
    print(f"ポートフォリオ検証 ({'全12' if args.all else 'robust4'}ペア)  "
          f"1ポジ={per_pos}% / 同時上限={max_total}% (=最大 {int(max_total/per_pos)} ポジ同時)")
    print(f"エントリー = H1+M15 + room_R<2.0 + 6-10時除外, RR1.5。全 {len(all_trades)} シグナル")
    print("=" * 84)

    # 制約なし (各ペア独立=理論値)
    up1, up2 = ret_split(all_trades, per_pos)
    ueq, udd = equity_dd(all_trades, per_pos)
    print(f"\n■ 制約なし (理論値)")
    print(f"   合計R% = P1 {up1:+.1f}% ({up1/P1_MONTHS:+.2f}%/月)  P2 {up2:+.1f}% ({up2/P2_MONTHS:+.2f}%/月)")
    print(f"   累積 {ueq:+.1f}%  最大DD {udd:.1f}%")

    # 制約あり
    acc, blk, maxc = portfolio_sim(all_trades, per_pos, max_total)
    ap1, ap2 = ret_split(acc, per_pos)
    bp1, bp2 = ret_split(blk, per_pos)
    aeq, add = equity_dd(acc, per_pos)
    print(f"\n■ 制約あり (同時{max_total}%上限)")
    print(f"   採用 {len(acc)} / ブロック {len(blk)} ({100*len(blk)/max(1,len(all_trades)):.0f}%)  最大同時保有 {maxc} ポジ")
    print(f"   合計R% = P1 {ap1:+.1f}% ({ap1/P1_MONTHS:+.2f}%/月)  P2 {ap2:+.1f}% ({ap2/P2_MONTHS:+.2f}%/月)")
    print(f"   累積 {aeq:+.1f}%  最大DD {add:.1f}%")
    print(f"\n■ 抑制された分 (ブロックされたシグナルの損益)")
    print(f"   ブロック {len(blk)} 件の合計R% = P1 {bp1:+.1f}%  P2 {bp2:+.1f}%  (これが取れなかった利益/損失)")
    lost = (up1 + up2) - (ap1 + ap2)
    print(f"   制約による利益減 = {lost:+.1f}% (累積 {ueq:+.1f}% → {aeq:+.1f}%)")

    # 日次損失 5% 制約 (Fintokei デイリーDD)
    uw, uwd, ub, _ = daily_loss(all_trades, per_pos)
    aw, awd, ab, nd = daily_loss(acc, per_pos)
    print(f"\n■ 日次損失 (5%上限ルール, 決済日UTC基準)")
    print(f"   制約なし: 最悪日 {uw:+.1f}% ({uwd})  5%超え {ub} 日")
    print(f"   制約あり: 最悪日 {aw:+.1f}% ({awd})  5%超え {ab} 日 / 全{nd}営業日")


if __name__ == "__main__":
    main()
