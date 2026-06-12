"""
backtest_net_cost.py — 実コスト(スプレッド+コミッション)を入れた net 評価。

これまでの WF 数値は全て gross(コスト抜き)。Axiory 実機(EURUSD -13.5%)で
判明した通り、薄いエッジは raw spread + コミッションで消える。ここで net を出す。

net_R = gross_R - cost_pips / sl_dist_pips
  (SL が狭いほど cost の R 換算が爆発。タイトSLがコスト負けする機序を正しく反映)

コストモデル (Axiory raw 実測ベース):
  - コミッション 往復 $12/lot ≈ USD建てペアで 1.2pips / JPY建てで ~1.8pips 相当
  - スプレッド raw: 入力 spread_pips を上乗せ (感度を見るため sweep)

設定 = 現ベスト H1+M15 + room_R<2.0 + 6-10時除外(block ON=実運用設定), RR1.5。

使い方:
    python tools/backtest_net_cost.py            # robust3/robust4/全12 を net 評価
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks         # noqa: E402

ROBUST3 = ["EURUSD", "USDJPY", "EURJPY"]
ROBUST4 = ["EURUSD", "USDJPY", "GBPUSD", "EURJPY"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
PARAMS = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15",
              room_R_max=2.0, block_hour_start=6, block_hour_end=10)  # block ON=実運用
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30


def pip_size(pair):
    if "JPY" in pair: return 0.01
    if "XAU" in pair: return 0.1
    return 0.0001


def comm_pips(pair):
    # 往復 $12/lot の pip 換算 (概算)。JPY建てクロス/USDJPY は pip 価値が低く pip 数は多い
    if pair.endswith("JPY"): return 1.8
    if pair.startswith("USD"): return 1.5   # USDCAD/CHF
    if "XAU" in pair: return 0.5
    return 1.2                              # EURUSD/GBPUSD など USD建て


_cache = {}


def capture(symbol):
    if symbol in _cache: return _cache[symbol]
    strat = MtfPullbackStrategy(Params(**PARAMS)); strat.symbol = symbol
    ctx = SimContext(); lh1 = lh4 = -1; ot = None; trades = []
    for (m5, h1, h4) in load_ticks(symbol):
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60: ctx.bars_seq.pop(0)
        if h1 and h1.time > lh1: ctx.mtf[3600].append(h1); lh1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 and h4.time > lh4: ctx.mtf[14400].append(h4); lh4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        if ot:
            hit = ep = None
            if ot["side"] == "long":
                if m5.low <= ot["sl"]: hit, ep = 1, ot["sl"]
                elif m5.high >= ot["tp"]: hit, ep = 1, ot["tp"]
            else:
                if m5.high >= ot["sl"]: hit, ep = 1, ot["sl"]
                elif m5.low <= ot["tp"]: hit, ep = 1, ot["tp"]
            if hit:
                pnl = (ep - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - ep)
                trades.append({"t": ot["t"], "R": pnl / ot["sld"],
                               "sld_pips": ot["sld"] / pip_size(symbol), "pair": symbol})
                ot = None; ctx._position = None
        ctx.pending_commands = []
        try: strat.on_bar(ctx)
        except Exception: pass
        if ot is None:
            for c in ctx.pending_commands:
                if c["type"] in ("buy", "sell"):
                    side = "long" if c["type"] == "buy" else "short"
                    e = c.get("entry_price") or m5.close; sl, tp = c.get("sl"), c.get("tp")
                    sld = abs(e - sl) if sl else 0
                    if not sl or not tp or sld <= 0: continue
                    ot = {"side": side, "entry": e, "sl": sl, "tp": tp, "sld": sld, "t": int(m5.time)}
                    ctx._position = side; break
    _cache[symbol] = trades
    return trades


def net_R(t, spread_pips):
    cost = comm_pips(t["pair"]) + spread_pips
    return t["R"] - cost / max(t["sld_pips"], 1e-9)


def evalset(pairs, spread_pips):
    trades = []
    for p in pairs: trades += capture(p)
    g1 = sum(t["R"] for t in trades if t["t"] < P1_END)
    g2 = sum(t["R"] for t in trades if t["t"] >= P1_END)
    n1 = sum(net_R(t, spread_pips) for t in trades if t["t"] < P1_END)
    n2 = sum(net_R(t, spread_pips) for t in trades if t["t"] >= P1_END)
    gw = sum(1 for t in trades if t["R"] > 0)
    nw = sum(1 for t in trades if net_R(t, spread_pips) > 0)
    n = len(trades)
    tight = sum(1 for t in trades if t["sld_pips"] < 10)   # 10pips未満のタイトSL率
    return dict(n=n, g1=g1, g2=g2, n1=n1, n2=n2, gwr=100*gw/n, nwr=100*nw/n, tight=100*tight/n)


def main():
    print("=" * 96)
    print("net コスト評価 (block ON=実運用設定, RR1.5)  コミッション往復$12/lot + spread")
    print("gross→net で /月 がどう変わるか。SLが狭いほどコスト痛打。1R=口座1%。")
    print("=" * 96)
    for name, pairs in [("robust3", ROBUST3), ("robust4", ROBUST4), ("全12", ALL)]:
        print(f"\n■ {name} ({'/'.join(pairs) if len(pairs)<=4 else '12ペア'})")
        print(f"  {'spread':>7} | {'N':>5} {'gWR':>5} {'nWR':>5} {'tightSL%':>8} | "
              f"{'gross P1/月':>11} {'gross P2/月':>11} | {'net P1/月':>10} {'net P2/月':>10}")
        for sp in [0.0, 0.5, 1.0, 1.5]:
            e = evalset(pairs, sp)
            tag = "(コミのみ)" if sp == 0 else ""
            print(f"  +{sp:>4.1f}p | {e['n']:>5} {e['gwr']:>4.0f}% {e['nwr']:>4.0f}% {e['tight']:>7.0f}% | "
                  f"{e['g1']/P1_M:>+10.2f}% {e['g2']/P2_M:>+10.2f}% | "
                  f"{e['n1']/P1_M:>+9.2f}% {e['n2']/P2_M:>+9.2f}% {tag}")
    print("\n※ コミッションは全 spread 行に含む(往復$12/lot)。spread はそれに上乗せする raw スプレッド。")
    print("※ tightSL% = SL<10pips のトレード割合 (コスト感応度が高い層)。")

    # ---- 絶対最小SLフィルタ sweep (spread=+0.5p 固定, タイトSL除外でnet改善するか) ----
    print("\n" + "=" * 96)
    print("絶対最小SLフィルタ sweep (spread +0.5p, タイトSL除外でnet改善するか)  ※両期間プラス維持が条件")
    print("=" * 96)
    SP = 0.5
    for name, pairs in [("robust3", ROBUST3), ("robust4", ROBUST4), ("全12", ALL)]:
        trades = []
        for p in pairs: trades += capture(p)
        print(f"\n■ {name}")
        print(f"  {'minSL':>6} | {'N':>5} {'nWR':>5} | {'net P1/月':>10} {'net P2/月':>10} | 判定")
        base2 = None
        for mn in [0, 10, 15, 20, 25]:
            sub = [t for t in trades if t["sld_pips"] >= mn]
            if not sub: continue
            n1 = sum(net_R(t, SP) for t in sub if t["t"] < P1_END)
            n2 = sum(net_R(t, SP) for t in sub if t["t"] >= P1_END)
            nw = sum(1 for t in sub if net_R(t, SP) > 0)
            if mn == 0: base2 = n2 / P2_M
            v = "(基準)" if mn == 0 else ("◎改善" if n2/P2_M > base2 else "—")
            print(f"  {mn:>4}p | {len(sub):>5} {100*nw/len(sub):>4.0f}% | "
                  f"{n1/P1_M:>+9.2f}% {n2/P2_M:>+9.2f}% | {v}")


if __name__ == "__main__":
    main()
