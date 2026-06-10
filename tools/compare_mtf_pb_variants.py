"""
compare_mtf_pb_variants.py — mtf_pullback v1/v2/v3 を 1 画面で比較。

各 variant の Walk-Forward (P1=2021-23 / P2=2024-26) と全期間月利を、
ペア別と合計でテーブル化する。

使い方:
    python tools/compare_mtf_pb_variants.py
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

VARIANTS = [
    ("v1", "data/eval_5y/mtf_pb_v1"),
    ("v2", "data/eval_5y/mtf_pb_v2"),
    ("v3", "data/eval_5y/mtf_pb_v3"),
]

P1_START = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P2_START = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P2_END = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
P1_MONTHS = 36
P2_MONTHS = 30


def load_trades_by_pair(path: str) -> dict[str, list[dict]]:
    by_pair: dict[str, list[dict]] = defaultdict(list)
    for f in glob.glob(os.path.join(path, "*", "*.jsonl")):
        sym = os.path.basename(os.path.dirname(f))
        with open(f) as fp:
            for line in fp:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except Exception:
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
                by_pair[sym].append({"t": t, "R": pnl / sld})
    return by_pair


def split_periods(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    p1 = [x for x in trades if P1_START <= x["t"] < P1_END]
    p2 = [x for x in trades if P2_START <= x["t"] < P2_END]
    return p1, p2


def fmt_period(trades: list[dict], months: int) -> str:
    if not trades:
        return "n= 0"
    n = len(trades)
    w = sum(1 for x in trades if x["R"] > 0)
    sumR = sum(x["R"] for x in trades)
    wr = 100 * w / n
    pct = sumR / months * 0.01 * 100  # 1% risk per trade
    return f"n={n:3d} WR={wr:5.1f}% sumR={sumR:+7.2f} ({pct:+.2f}%/mo)"


def verdict(p1_sumR: float, p2_sumR: float) -> str:
    p1_pos = p1_sumR > 0
    p2_pos = p2_sumR > 0
    if p1_pos and p2_pos:
        return "[ROBUST]"
    if p1_pos and not p2_pos:
        return "[Edge GONE]"
    if not p1_pos and p2_pos:
        return "[OOS+ only]"
    return "[BAD both]"


def main() -> None:
    all_data: dict[str, dict[str, list[dict]]] = {}
    for name, path in VARIANTS:
        all_data[name] = load_trades_by_pair(path)
    all_pairs = sorted(set().union(*(set(d.keys()) for d in all_data.values())))

    print("=" * 120)
    print("mtf_pullback v1 / v2 / v3 比較 (Walk-Forward: P1=2021-23, P2=2024-26)")
    print("=" * 120)

    for sym in all_pairs:
        print(f"\n[{sym}]")
        for name, _ in VARIANTS:
            trades = all_data[name].get(sym, [])
            p1, p2 = split_periods(trades)
            p1_sumR = sum(x["R"] for x in p1)
            p2_sumR = sum(x["R"] for x in p2)
            v = verdict(p1_sumR, p2_sumR)
            print(f"  {name}  P1 {fmt_period(p1, P1_MONTHS):60s}  P2 {fmt_period(p2, P2_MONTHS):60s}  {v}")

    print("\n" + "=" * 120)
    print("ALL PAIRS TOTAL")
    print("=" * 120)
    for name, _ in VARIANTS:
        p1_total: list[dict] = []
        p2_total: list[dict] = []
        for sym in all_pairs:
            p1, p2 = split_periods(all_data[name].get(sym, []))
            p1_total += p1
            p2_total += p2
        p1_sumR = sum(x["R"] for x in p1_total)
        p2_sumR = sum(x["R"] for x in p2_total)
        all_sumR = p1_sumR + p2_sumR
        all_n = len(p1_total) + len(p2_total)
        v = verdict(p1_sumR, p2_sumR)
        print(f"  {name}  P1 {fmt_period(p1_total, P1_MONTHS)}  P2 {fmt_period(p2_total, P2_MONTHS)}  ALL n={all_n} sumR={all_sumR:+.2f} ({all_sumR/66*0.01*100:+.3f}%/mo)  {v}")

    print("\n" + "=" * 120)
    print("ROBUST ペアのみ portfolio (= v2 と v3 が両期間 + のペアだけ取引した想定)")
    print("=" * 120)
    for name, _ in VARIANTS:
        robust_pairs = []
        for sym in all_pairs:
            p1, p2 = split_periods(all_data[name].get(sym, []))
            p1_sumR = sum(x["R"] for x in p1)
            p2_sumR = sum(x["R"] for x in p2)
            if p1_sumR > 0 and p2_sumR > 0:
                robust_pairs.append(sym)
        p1_robust = sum(
            x["R"] for sym in robust_pairs
            for x in split_periods(all_data[name].get(sym, []))[0]
        )
        p2_robust = sum(
            x["R"] for sym in robust_pairs
            for x in split_periods(all_data[name].get(sym, []))[1]
        )
        print(f"  {name}  ROBUST = {robust_pairs}")
        print(f"        P1 sumR={p1_robust:+.2f} ({p1_robust/P1_MONTHS*0.01*100:+.3f}%/mo)  "
              f"P2 sumR={p2_robust:+.2f} ({p2_robust/P2_MONTHS*0.01*100:+.3f}%/mo)")


if __name__ == "__main__":
    main()
