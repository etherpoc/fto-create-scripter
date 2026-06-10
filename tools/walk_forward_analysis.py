"""
walk_forward_analysis.py — 既存 outcome を期間分割して各 variant の安定性を見る。

Period 1 (IS):  2021-01 ~ 2023-12 (3 年、in-sample 想定)
Period 2 (OOS): 2024-01 ~ 2026-06 (2.5 年、out-of-sample 相当)

各 variant について、IS / OOS の月利を出して比較する。
真の edge があれば両期間で似た数値、bias なら IS だけ大きくなる。
"""

from __future__ import annotations
import json, glob, os
from datetime import datetime, timezone
from collections import defaultdict


def load_trades(root: str) -> list[dict]:
    """全 outcome を (entry_time, R, symbol) で返す。"""
    out = []
    if not os.path.isdir(root):
        return out
    for sym in sorted(os.listdir(root)):
        sym_dir = os.path.join(root, sym)
        if not os.path.isdir(sym_dir):
            continue
        for f in sorted(glob.glob(os.path.join(sym_dir, "*.jsonl"))):
            with open(f) as fp:
                for line in fp:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if r.get("type") != "outcome":
                        continue
                    try:
                        ep = float(r["entry_price"])
                        sl = float(r["sl"])
                        pnl = float(r["pnl_price"])
                        t = int(r["entry_bar_time"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    sld = abs(ep - sl)
                    if sld <= 0:
                        continue
                    R = pnl / sld
                    out.append({"t": t, "R": R, "sym": sym})
    return out


def period_stats(trades: list[dict], start: datetime, end: datetime) -> dict:
    """期間内のトレードを集計。"""
    in_period = [t for t in trades
                 if start.timestamp() <= t["t"] < end.timestamp()]
    if not in_period:
        return {"n": 0, "won": 0, "lost": 0, "sumR": 0.0, "avgR": 0.0, "wr": 0.0,
                "n_months": 0, "monthly_R": 0.0}
    n = len(in_period)
    won = sum(1 for t in in_period if t["R"] > 0)
    lost = sum(1 for t in in_period if t["R"] < 0)
    sumR = sum(t["R"] for t in in_period)
    avgR = sumR / n if n else 0
    wr = 100 * won / (won + lost) if (won + lost) else 0
    # 月数
    n_months = (end.year - start.year) * 12 + (end.month - start.month)
    if n_months <= 0:
        n_months = 1
    monthly_R = sumR / n_months
    return {"n": n, "won": won, "lost": lost, "sumR": sumR, "avgR": avgR,
            "wr": wr, "n_months": n_months, "monthly_R": monthly_R}


def main() -> int:
    # 期間定義
    P1_start = datetime(2021, 1, 1, tzinfo=timezone.utc)
    P1_end   = datetime(2024, 1, 1, tzinfo=timezone.utc)
    P2_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    P2_end   = datetime(2026, 7, 1, tzinfo=timezone.utc)

    variants = [
        ("M15 baseline (旧)",   "data/eval_5y/baseline"),
        ("M15 v7d/gemma4",      "data/eval_5y/v7d_gemma4"),
        ("M15 v8_50",           "data/eval_5y/v8_baseline"),
        ("M15 v8_38",           "data/eval_5y/v8_38_baseline"),
        ("M15 v8_62",           "data/eval_5y/v8_62_baseline"),
        ("M15 v9 (v8_38+AI)",   "data/eval_5y/v9_gemma4"),
        ("M5 baseline",         "data/eval_5y/m5_38_baseline"),
        ("M5 + gemma4",         "data/eval_5y/m5_38_gemma4"),
    ]

    print("=" * 95)
    print(f"  Walk-Forward 分析: Period1 (2021-2023, 36月) vs Period2 (2024-2026/6, 30月)")
    print("=" * 95)
    print(f"  {'Variant':22s} | {'Period1 (IS)':<30s} | {'Period2 (OOS)':<30s} | Verdict")
    print(f"  {'':22s} | {'n':>4s} {'WR%':>5s} {'sumR':>8s} {'月利%':>7s} | {'n':>4s} {'WR%':>5s} {'sumR':>8s} {'月利%':>7s} |")
    print("-" * 95)

    for name, path in variants:
        trades = load_trades(path)
        p1 = period_stats(trades, P1_start, P1_end)
        p2 = period_stats(trades, P2_start, P2_end)
        if p1["n"] == 0 and p2["n"] == 0:
            continue
        # 月利 %
        p1_pct = p1["monthly_R"] * 0.01 * 100  # = monthly_R%
        p2_pct = p2["monthly_R"] * 0.01 * 100
        # Verdict
        if p1_pct > 0.1 and p2_pct > 0.1:
            verdict = "✅ ROBUST"
        elif p1_pct > 0.1 and p2_pct < -0.1:
            verdict = "❌ Edge GONE"
        elif p1_pct > 0.1 and abs(p2_pct) < 0.1:
            verdict = "⚠️  Decayed"
        elif p1_pct < -0.1 and p2_pct < -0.1:
            verdict = "💀 Always BAD"
        else:
            verdict = "?? mixed"
        print(f"  {name:22s} | "
              f"{p1['n']:>4d} {p1['wr']:>4.1f}% {p1['sumR']:>+8.2f} {p1_pct:>+6.2f}% | "
              f"{p2['n']:>4d} {p2['wr']:>4.1f}% {p2['sumR']:>+8.2f} {p2_pct:>+6.2f}% | "
              f"{verdict}")

    print()
    print("=" * 95)
    print("  各ペアの v8_38 baseline の Walk-Forward (= 真の問題はどのペアにあるか)")
    print("=" * 95)
    trades = load_trades("data/eval_5y/v8_38_baseline")
    # ペア別に集計
    per_pair = defaultdict(list)
    for t in trades:
        per_pair[t["sym"]].append(t)
    print(f"  {'Symbol':10s} | {'P1':<30s} | {'P2':<30s} | Verdict")
    print(f"  {'':10s} | {'n':>4s} {'WR%':>5s} {'sumR':>8s} {'/月':>7s} | {'n':>4s} {'WR%':>5s} {'sumR':>8s} {'/月':>7s} |")
    print("-" * 95)
    for sym in sorted(per_pair.keys()):
        p1 = period_stats(per_pair[sym], P1_start, P1_end)
        p2 = period_stats(per_pair[sym], P2_start, P2_end)
        if p1["n"] == 0 and p2["n"] == 0:
            continue
        p1_pct = p1["monthly_R"] * 0.01 * 100
        p2_pct = p2["monthly_R"] * 0.01 * 100
        if p1_pct > 0 and p2_pct > 0:
            verdict = "✅ both +"
        elif p1_pct > 0 and p2_pct < 0:
            verdict = "❌ P1+ P2-"
        elif p1_pct < 0 and p2_pct > 0:
            verdict = "🔄 P1- P2+"
        else:
            verdict = "💀 both -"
        print(f"  {sym:10s} | "
              f"{p1['n']:>4d} {p1['wr']:>4.1f}% {p1['sumR']:>+8.2f} {p1_pct:>+6.2f}% | "
              f"{p2['n']:>4d} {p2['wr']:>4.1f}% {p2['sumR']:>+8.2f} {p2_pct:>+6.2f}% | "
              f"{verdict}")

    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    sys.exit(main())
