"""
backtest_h4_sizing.py — H4 一致を確信度サイジングに使い、プロップDD効率で評価。

H4 状態 (一致/中立/不一致) ごとにポジションサイズ倍率を変え、
ポートフォリオ(全ペア時間統合)の リターン/最大DD 比で比較する。

  リターン/DD比 はサイズに非依存(両方が base サイズに線形)なので、
  「同じDD予算でどれだけ張れるか=プロップ効率」の公平な指標になる。
  採用基準 = 両期間(P1/P2)× 両ペアセット(robust4/全12) で flat を上回る。

baseline(flat) = 現ベスト H1+M15 + room_R<2.0 + 6-10時除外, RR1.5, 全トレード等倍。

使い方:
    python tools/backtest_h4_sizing.py            # robust4
    python tools/backtest_h4_sizing.py --all
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks         # noqa: E402

ROBUST4 = ["EURUSD", "USDJPY", "GBPUSD", "EURJPY"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
PARAMS = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15",
              room_R_max=2.0, block_hour_start=6, block_hour_end=10)
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30

# スキーム名 -> (w_一致, w_中立, w_不一致)
SCHEMES = {
    "flat        ": (1.0, 1.0, 1.0),
    "skipDis     ": (1.0, 1.0, 0.0),
    "1.5/1/0.5   ": (1.5, 1.0, 0.5),
    "1.5/1/0     ": (1.5, 1.0, 0.0),
    "2/1/0       ": (2.0, 1.0, 0.0),
    "1.5/0.75/0  ": (1.5, 0.75, 0.0),
    "2/1/0.5     ": (2.0, 1.0, 0.5),
}


def capture(symbol, ticks):
    strat = MtfPullbackStrategy(Params(**PARAMS))
    strat.symbol = symbol
    strat._dbg_h4 = None
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
            hit = exitp = None
            if ot["side"] == "long":
                if m5.low <= ot["sl"]: hit, exitp = "sl", ot["sl"]
                elif m5.high >= ot["tp"]: hit, exitp = "tp", ot["tp"]
            else:
                if m5.high >= ot["sl"]: hit, exitp = "sl", ot["sl"]
                elif m5.low <= ot["tp"]: hit, exitp = "tp", ot["tp"]
            if hit:
                pnl = (exitp - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - exitp)
                major = "up" if ot["side"] == "long" else "down"
                if ot["h4"] is None:
                    st = "neu"
                else:
                    st = "agr" if ot["h4"] == major else "dis"
                trades.append({"entry_t": ot["entry_t"], "exit_t": int(m5.time),
                               "R": pnl / ot["sld"], "h4st": st})
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
                          "entry_t": int(m5.time), "h4": getattr(strat, "_dbg_h4", None)}
                    ctx._position = side
                    break
    return trades


def weight(t, sch):
    return {"agr": sch[0], "neu": sch[1], "dis": sch[2]}[t["h4st"]]


def evaluate(trades, sch):
    """重み付きの P1/P2 sumR, 総DD, リターン/DD比 を返す(base size=1)。"""
    p1 = sum(t["R"] * weight(t, sch) for t in trades if t["entry_t"] < P1_END)
    p2 = sum(t["R"] * weight(t, sch) for t in trades if t["entry_t"] >= P1_END)
    # 全体DD (決済時刻順)
    ev = sorted(trades, key=lambda x: x["exit_t"])
    eq = peak = dd = 0.0
    for t in ev:
        eq += t["R"] * weight(t, sch)
        peak = max(peak, eq); dd = max(dd, peak - eq)
    # 期間別DDも (P2のみの効率を見る)
    def dd_period(lo, hi):
        e = p = d = 0.0
        for t in sorted([x for x in trades if lo <= x["entry_t"] < hi], key=lambda x: x["exit_t"]):
            e += t["R"] * weight(t, sch); p = max(p, e); d = max(d, p - e)
        return d
    dd1 = dd_period(0, P1_END); dd2 = dd_period(P1_END, 1e18)
    return {"p1": p1, "p2": p2, "dd": dd, "dd1": dd1, "dd2": dd2,
            "ratio": (p1 + p2) / dd if dd > 0 else 0,
            "r1": p1 / dd1 if dd1 > 0 else 0, "r2": p2 / dd2 if dd2 > 0 else 0,
            "maxw": max(weight(t, sch) for t in trades) if trades else 0,
            "n": sum(1 for t in trades if weight(t, sch) > 0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else ROBUST4
    tag = "全12" if args.all else "robust4"
    allt = []
    for p in pairs:
        allt += capture(p, load_ticks(p))
        print(f"  {p} done", flush=True)

    base = evaluate(allt, SCHEMES["flat        "])
    print("\n" + "=" * 96)
    print(f"H4 確信度サイジング ({tag})  base(flat) リターン/DD比 = {base['ratio']:.2f} "
          f"(P1比 {base['r1']:.2f} / P2比 {base['r2']:.2f})")
    print("リターン/DD比が高い=同じDD予算で大きく張れる=プロップ効率↑。採用=両期間で flat 超え。")
    print("=" * 96)
    print(f"  {'scheme':<13} | {'N':>4} | {'P1':>6} {'P2':>6} | {'総DD':>6} | "
          f"{'比(全)':>6} {'比P1':>6} {'比P2':>6} | 判定")
    print("-" * 96)
    for name, sch in SCHEMES.items():
        e = evaluate(allt, sch)
        better = (e["r1"] > base["r1"] and e["r2"] > base["r2"])
        v = "◎両期間↑" if better else ("○P2↑" if e["r2"] > base["r2"] else "—")
        if name.strip() == "flat":
            v = "(基準)"
        print(f"  {name} | {e['n']:>4} | {e['p1']:>+6.1f} {e['p2']:>+6.1f} | {e['dd']:>5.1f}% | "
              f"{e['ratio']:>6.2f} {e['r1']:>6.2f} {e['r2']:>6.2f} | {v}")
    print("\n※ 比(全/P1/P2) = sumR / 最大DD。maxw=最大サイズ倍率(プロップ1ポジ1.5%枠との兼ね合いに注意)。")


if __name__ == "__main__":
    main()
