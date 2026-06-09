"""
compute_monthly_returns.py — outcome ログから R 単位の月利・勝率を計算する。

各トレードを以下で評価:
  - R = pnl_price / sl_dist  (符号付き: +R = 勝ち R 倍、 -1 = SL 通り損失)
  - リスク 1% / トレード前提で、各トレードの口座変動 = R × 1%
  - 月別に集計 → 月利% (単利合計および複利)

Usage:
    python tools/compute_monthly_returns.py --dir data/eval_5y/baseline
    python tools/compute_monthly_returns.py --dir data/eval_5y/v7d_gemma4 --by-pair
"""

from __future__ import annotations

import argparse
import json
import glob
import os
import sys
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def iter_outcomes(root: str):
    """指定 dir 配下のすべての outcome レコードを順にロード。"""
    if not os.path.isdir(root):
        return
    for sym in sorted(os.listdir(root)):
        sym_dir = os.path.join(root, sym)
        if not os.path.isdir(sym_dir):
            continue
        for f in sorted(glob.glob(os.path.join(sym_dir, "*.jsonl"))):
            with open(f, "r", encoding="utf-8") as fp:
                for line in fp:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if r.get("type") == "outcome":
                        yield sym, r


def trade_R(outcome: dict) -> float | None:
    """1 トレードの R を計算 (signed)。

    R = pnl_price / sl_dist
    sl_dist = abs(entry_price - sl)
    """
    try:
        entry_price = float(outcome["entry_price"])
        sl = float(outcome["sl"])
        pnl_price = float(outcome["pnl_price"])
    except (KeyError, TypeError, ValueError):
        return None
    sl_dist = abs(entry_price - sl)
    if sl_dist <= 0:
        return None
    return pnl_price / sl_dist


def month_key(ts: int | float) -> str:
    """UNIX 秒から "YYYY-MM" の月キーへ。"""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m")
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def analyze(root: str, by_pair: bool = False, risk_pct: float = 0.01) -> dict:
    """月別の R 集計と勝率を計算する。"""
    # 全トレード
    trades_all: list[tuple[str, str, float]] = []  # (sym, month, R)
    won = 0
    lost = 0
    drops = 0
    for sym, oc in iter_outcomes(root):
        R = trade_R(oc)
        if R is None:
            drops += 1
            continue
        m = month_key(oc.get("entry_bar_time") or 0)
        trades_all.append((sym, m, R))
        if R > 0:
            won += 1
        elif R < 0:
            lost += 1

    total = len(trades_all)
    if total == 0:
        return {"total": 0, "drops": drops}

    # 全体メトリクス
    wr = won / (won + lost) if (won + lost) > 0 else 0
    avg_R = sum(r for _, _, r in trades_all) / total
    sum_R = sum(r for _, _, r in trades_all)

    # 月別 R 合計
    monthly = defaultdict(lambda: {"R": 0.0, "n": 0, "won": 0, "lost": 0})
    for _, m, R in trades_all:
        monthly[m]["R"] += R
        monthly[m]["n"] += 1
        if R > 0:
            monthly[m]["won"] += 1
        elif R < 0:
            monthly[m]["lost"] += 1

    months = sorted(monthly.keys())
    n_months = len(months)
    monthly_pcts = [monthly[m]["R"] * risk_pct * 100 for m in months]  # % per month
    avg_monthly_pct = sum(monthly_pcts) / n_months if n_months else 0

    # 複利月利 (= geometric average)
    # balance growth factor per month = product(1 + R_m * risk_pct)
    growth_factor = 1.0
    for p in monthly_pcts:
        growth_factor *= (1 + p / 100)
    geom_monthly_pct = ((growth_factor ** (1 / n_months) - 1) * 100) if n_months else 0
    total_return_pct = (growth_factor - 1) * 100

    # 月別 WR 中央値 (= 単月のばらつきを抑えた指標)
    monthly_wrs = []
    for m in months:
        mm = monthly[m]
        if mm["won"] + mm["lost"] > 0:
            monthly_wrs.append(100 * mm["won"] / (mm["won"] + mm["lost"]))
    monthly_wrs.sort()
    median_monthly_wr = monthly_wrs[len(monthly_wrs) // 2] if monthly_wrs else 0

    result = {
        "total": total,
        "drops": drops,
        "won": won,
        "lost": lost,
        "wr_pct": 100 * wr,
        "avg_R": avg_R,
        "sum_R": sum_R,
        "n_months": n_months,
        "month_range": f"{months[0]} ~ {months[-1]}" if months else "",
        "avg_monthly_pct": avg_monthly_pct,
        "geom_monthly_pct": geom_monthly_pct,
        "total_return_pct": total_return_pct,
        "median_monthly_wr": median_monthly_wr,
    }

    if by_pair:
        per_pair = defaultdict(lambda: {"R": 0.0, "n": 0, "won": 0, "lost": 0})
        for sym, _, R in trades_all:
            per_pair[sym]["R"] += R
            per_pair[sym]["n"] += 1
            if R > 0:
                per_pair[sym]["won"] += 1
            elif R < 0:
                per_pair[sym]["lost"] += 1
        result["per_pair"] = dict(per_pair)

    return result


def fmt_table(label: str, r: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"  Range:        {r.get('month_range', '?')}  ({r.get('n_months', 0)} months)")
    print(f"  Trades:       {r['total']}  (won={r['won']} lost={r['lost']})")
    print(f"  WR:           {r['wr_pct']:.1f}%")
    print(f"  Avg R/trade:  {r['avg_R']:+.4f}")
    print(f"  Sum R:        {r['sum_R']:+.2f}  (= total return)")
    print(f"  Avg monthly%: {r['avg_monthly_pct']:+.3f}%  (= simple, 1% risk/trade)")
    print(f"  Geom monthly%:{r['geom_monthly_pct']:+.3f}%  (= compound)")
    print(f"  Total return: {r['total_return_pct']:+.2f}%  ({r['n_months']} months compound)")
    print(f"  Median month WR: {r['median_monthly_wr']:.1f}%")
    if "per_pair" in r:
        print(f"\n  Per-pair (by R):")
        items = sorted(r["per_pair"].items(), key=lambda kv: kv[1]["R"], reverse=True)
        print(f"    {'Symbol':10s} {'n':>5s} {'W':>4s} {'L':>4s} {'WR%':>6s} {'sumR':>8s} {'avgR':>8s}")
        for sym, v in items:
            wins_losses = v["won"] + v["lost"]
            wr = 100 * v["won"] / wins_losses if wins_losses else 0
            avg = v["R"] / v["n"] if v["n"] else 0
            print(f"    {sym:10s} {v['n']:5d} {v['won']:4d} {v['lost']:4d} {wr:5.1f}% {v['R']:+8.3f} {avg:+8.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="outcomes 含む dir (例: data/eval_5y/baseline)")
    ap.add_argument("--by-pair", action="store_true")
    ap.add_argument("--risk-pct", type=float, default=0.01)
    args = ap.parse_args()

    r = analyze(args.dir, by_pair=args.by_pair, risk_pct=args.risk_pct)
    if r["total"] == 0:
        print(f"No outcomes found in {args.dir}")
        return 1
    fmt_table(args.dir, r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
