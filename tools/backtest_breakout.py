"""
backtest_breakout.py — ブレイクアウト/モメンタム戦略のフィージビリティ検証 (net コスト込み)。

タートル流 Donchian ブレイクアウト:
  - エントリー: 直近 entry_n 本の高値/安値をブレイク → トレンド方向に成行
  - SL: entry ± sl_atr × ATR
  - エグジット: 逆方向 Donchian(exit_n) ブレイクでトレーリング決済(トレンド追従)
  - 任意フィルタ: SMA(sma_n) トレンド方向のみ(レンジのダマシ除去)

★ 最初から net 評価(往復コミ$12/lot + spread)。gross の罠を繰り返さない。
WF: P1=2021-23 / P2=2024-26。robust = 両期間 net プラス。

使い方:
    python tools/backtest_breakout.py            # H4, 主要4ペア
    python tools/backtest_breakout.py --tf h1 --all
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.backtest_mtf_pb_variants import load_ticks  # noqa: E402

MAJORS = ["EURUSD", "USDJPY", "GBPUSD", "EURJPY"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30
SPREAD = 0.5


def pip(p): return 0.01 if "JPY" in p else (0.1 if "XAU" in p else 0.0001)
def comm(p):
    if p.endswith("JPY"): return 1.8
    if p.startswith("USD"): return 1.5
    if "XAU" in p: return 0.5
    return 1.2


def extract_tf(sym, tf):
    """load_ticks のストリームから TF バー列を抽出(重複除去, 時刻昇順)。"""
    idx = {"h1": 1, "h4": 2}[tf]
    seen = set(); bars = []
    for tup in load_ticks(sym):
        b = tup[idx]
        if b is None: continue
        if b.time in seen: continue
        seen.add(b.time); bars.append(b)
    bars.sort(key=lambda x: x.time)
    return bars


def atr_at(bars, i, n):
    if i < n: return None
    s = 0.0
    for k in range(i - n + 1, i + 1):
        h, l, pc = bars[k].high, bars[k].low, bars[k - 1].close
        s += max(h - l, abs(h - pc), abs(l - pc))
    return s / n


def run_breakout(bars, P):
    """Donchian ブレイクアウト。trades=[{t,R,sl_pips}] を返す。"""
    en, ex, an = P["entry_n"], P["exit_n"], P["atr_n"]
    sl_atr, sma_n = P["sl_atr"], P["sma_n"]
    trades = []
    pos = None  # {"side","entry","sl","entry_i"}
    closes = [b.close for b in bars]
    warm = max(en, an, sma_n) + 1
    for i in range(warm, len(bars)):
        b = bars[i]
        atr = atr_at(bars, i - 1, an)   # 確定足ベース(前足までの ATR)
        if atr is None or atr <= 0:
            continue
        # Donchian レベル(直近 en 本, 当足を含まない)
        donch_hi = max(bb.high for bb in bars[i - en:i])
        donch_lo = min(bb.low for bb in bars[i - en:i])
        sma = sum(closes[i - sma_n:i]) / sma_n if sma_n > 0 else None

        if pos is None:
            # ブレイク判定(当足の高安がレベル超え)
            long_ok = b.high > donch_hi and (sma is None or b.close > sma)
            short_ok = b.low < donch_lo and (sma is None or b.close < sma)
            if long_ok:
                entry = donch_hi             # レベルで約定(保守的)
                pos = {"side": "long", "entry": entry, "sl": entry - sl_atr * atr, "i": i}
            elif short_ok:
                entry = donch_lo
                pos = {"side": "short", "entry": entry, "sl": entry + sl_atr * atr, "i": i}
            continue
        # 保有中: SL or トレーリング Donchian(exit_n) で決済
        exit_price = None
        if pos["side"] == "long":
            trail = min(bb.low for bb in bars[i - ex:i])
            if b.low <= pos["sl"]:
                exit_price = pos["sl"]
            elif b.low <= trail:                       # トレール割れ
                exit_price = min(trail, b.open)        # ギャップ考慮
        else:
            trail = max(bb.high for bb in bars[i - ex:i])
            if b.high >= pos["sl"]:
                exit_price = pos["sl"]
            elif b.high >= trail:
                exit_price = max(trail, b.open)
        if exit_price is not None:
            sl_dist = abs(pos["entry"] - pos["sl"])
            pnl = (exit_price - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - exit_price)
            trades.append({"t": bars[pos["i"]].time, "R": pnl / sl_dist, "sl_dist": sl_dist})
            pos = None
    return trades


def evaluate(pairs, tf, P):
    rows = []
    tot = {"n": 0, "g1": 0.0, "g2": 0.0, "n1": 0.0, "n2": 0.0, "w": 0}
    rob = 0
    for sym in pairs:
        bars = extract_tf(sym, tf)
        tr = run_breakout(bars, P)
        ps = pip(sym); cost = comm(sym) + SPREAD
        def nR(t): return t["R"] - cost / max(t["sl_dist"] / ps, 1e-9)
        n = len(tr)
        if n == 0:
            rows.append((sym, 0, 0, 0, 0, 0)); continue
        g1 = sum(t["R"] for t in tr if t["t"] < P1_END)
        g2 = sum(t["R"] for t in tr if t["t"] >= P1_END)
        n1 = sum(nR(t) for t in tr if t["t"] < P1_END)
        n2 = sum(nR(t) for t in tr if t["t"] >= P1_END)
        w = sum(1 for t in tr if t["R"] > 0)
        rob += (n1 > 0 and n2 > 0)
        rows.append((sym, n, 100 * w / n, n1, n2, n1 + n2))
        tot["n"] += n; tot["g1"] += g1; tot["g2"] += g2; tot["n1"] += n1; tot["n2"] += n2; tot["w"] += w
    return rows, tot, rob


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tf", default="h4", choices=["h1", "h4"])
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else MAJORS
    tag = "全12" if args.all else "主要4"

    # 代表的な Donchian 構成を sweep
    CONFIGS = [
        dict(entry_n=20, exit_n=10, atr_n=20, sl_atr=2.0, sma_n=0,   label="D20/E10/SL2/filtなし"),
        dict(entry_n=20, exit_n=10, atr_n=20, sl_atr=2.0, sma_n=50,  label="D20/E10/SL2/SMA50"),
        dict(entry_n=40, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=0,   label="D40/E20/SL2/filtなし"),
        dict(entry_n=55, exit_n=20, atr_n=20, sl_atr=2.0, sma_n=100, label="D55/E20/SL2/SMA100(turtle)"),
        dict(entry_n=10, exit_n=5,  atr_n=14, sl_atr=1.5, sma_n=0,   label="D10/E5/SL1.5(短期)"),
    ]
    print("=" * 100)
    print(f"ブレイクアウト feasibility ({tag}, {args.tf.upper()})  net=往復コミ+spread0.5p。robust=両期間net+")
    print("=" * 100)
    print(f"  {'構成':<30} | {'N':>4} {'WR':>5} | {'net P1/月':>9} {'net P2/月':>9} | {'年複利':>7} | {'robust':>6}")
    print("-" * 100)
    for P in CONFIGS:
        rows, tot, rob = evaluate(pairs, args.tf, P)
        if tot["n"] == 0:
            print(f"  {P['label']:<30} | 0 trades"); continue
        m2 = tot["n2"] / P2_M
        yr = ((1 + m2 / 100) ** 12 - 1) * 100 if m2 > -100 else 0
        print(f"  {P['label']:<30} | {tot['n']:>4} {100*tot['w']/tot['n']:>4.0f}% | "
              f"{tot['n1']/P1_M:>+8.2f}% {tot['n2']/P2_M:>+8.2f}% | {yr:>+6.1f}% | {rob:>2}/{len(pairs)}")
        if args.detail:
            for (sym, n, wr, n1, n2, tt) in rows:
                mark = "✓" if (n1 > 0 and n2 > 0) else " "
                print(f"      {mark} {sym:<8} N={n:>3} WR={wr:>3.0f}% net P1={n1:>+6.1f} P2={n2:>+6.1f}")
    print("-" * 100)
    print("net P1/P2 = 期間内 net sumR を月割り(1R=口座1%)。年複利は P2 月利ベース。")


if __name__ == "__main__":
    main()
