"""
compare_variants_5y.py — 複数の AI 設定 (variant) を 5.5 年データで横並び比較。

Usage:
    python tools/compare_variants_5y.py \\
        --variant baseline=data/eval_5y/baseline \\
        --variant v7d_gemma4=data/eval_5y/v7d_gemma4 \\
        --variant qwen25_7b=data/eval_5y/qwen25_7b

出力:
  - variant × pair の R 合計マトリクス
  - 全体メトリクス比較 (WR, Sum R, 月利)
  - "best variant per pair" portfolio (= 各ペアで最高の variant を採用した場合)
  - "uniform best variant" portfolio (= 全ペア共通の best variant)
  - 重要: in-sample 評価であることを明示
"""

from __future__ import annotations

import argparse
import json
import glob
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone


def load_variant(root: str) -> dict[str, list[dict]]:
    """variant の outcome を symbol 別にロード。"""
    out: dict[str, list[dict]] = defaultdict(list)
    if not os.path.isdir(root):
        return dict(out)
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
                    if r.get("type") == "outcome":
                        out[sym].append(r)
    return dict(out)


def trade_R(oc: dict) -> float | None:
    try:
        ep = float(oc["entry_price"])
        sl = float(oc["sl"])
        pnl = float(oc["pnl_price"])
    except (KeyError, TypeError, ValueError):
        return None
    sld = abs(ep - sl)
    if sld <= 0:
        return None
    return pnl / sld


def summarize_pair(outcomes: list[dict]) -> dict:
    """1 variant × 1 pair の集計。"""
    n = 0
    won = 0
    lost = 0
    sumR = 0.0
    for oc in outcomes:
        R = trade_R(oc)
        if R is None:
            continue
        n += 1
        sumR += R
        if R > 0:
            won += 1
        elif R < 0:
            lost += 1
    wins_losses = won + lost
    wr = 100 * won / wins_losses if wins_losses else 0
    avgR = sumR / n if n else 0
    return {"n": n, "won": won, "lost": lost, "wr": wr, "sumR": sumR, "avgR": avgR}


def month_count(outcomes_by_sym: dict[str, list[dict]]) -> int:
    months = set()
    for ocs in outcomes_by_sym.values():
        for oc in ocs:
            try:
                t = int(oc.get("entry_bar_time") or 0)
                dt = datetime.fromtimestamp(t, tz=timezone.utc)
                months.add(dt.strftime("%Y-%m"))
            except Exception:  # noqa: BLE001
                pass
    return len(months) or 66


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--variant",
        action="append",
        required=True,
        help="形式: NAME=DIR  例: baseline=data/eval_5y/baseline",
    )
    ap.add_argument("--risk-pct", type=float, default=0.01)
    args = ap.parse_args()

    # variant ロード
    variants: dict[str, dict[str, list[dict]]] = {}
    for spec in args.variant:
        if "=" not in spec:
            print(f"bad --variant: {spec}", file=sys.stderr)
            return 1
        name, root = spec.split("=", 1)
        variants[name] = load_variant(root)

    all_pairs = sorted({p for v in variants.values() for p in v.keys()})
    n_months = month_count(next(iter(variants.values()), {}))

    # === 1. variant × pair の R マトリクス
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        try: _sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("=" * 80)
    print(f"variant x pair (Sum R)  [period = {n_months} months]")
    print("=" * 80)
    header = f"  {'Symbol':10s}"
    for name in variants:
        header += f" {name:>12s}"
    print(header)
    print("-" * (12 + 13 * len(variants)))

    # ペア別の各 variant 統計
    pair_stats: dict[str, dict[str, dict]] = defaultdict(dict)
    for name, byp in variants.items():
        for sym in all_pairs:
            pair_stats[sym][name] = summarize_pair(byp.get(sym, []))

    for sym in all_pairs:
        row = f"  {sym:10s}"
        for name in variants:
            s = pair_stats[sym][name]
            row += f" {s['sumR']:+12.2f}"
        print(row)

    # === 2. 全体合計 (uniform = 全ペアでその variant 使う)
    print()
    print("=" * 80)
    print(f"Uniform (= 全 12 ペアで同じ variant)")
    print("=" * 80)
    print(f"  {'Variant':12s} {'Trades':>8s} {'WR%':>6s} {'sumR':>8s} {'avgR':>8s} {'Monthly%':>10s} {'Total%':>9s}")
    print("-" * 70)
    for name in variants:
        n=won=lost=0; sumR=0.0
        for sym in all_pairs:
            s = pair_stats[sym][name]
            n += s["n"]; won += s["won"]; lost += s["lost"]; sumR += s["sumR"]
        wr = 100*won/(won+lost) if (won+lost) else 0
        avgR = sumR/n if n else 0
        avg_month_pct = sumR / n_months * args.risk_pct * 100  # 簡易月利%
        # 複利
        # 単純化: 月毎の合計 R が大体均等と仮定して compound
        # 厳密には月別計算が必要 (この compare では省略、avg を使う)
        total_pct = ((1 + avg_month_pct/100) ** n_months - 1) * 100
        print(f"  {name:12s} {n:8d} {wr:5.1f}% {sumR:+8.2f} {avgR:+8.4f} {avg_month_pct:+9.3f}% {total_pct:+8.1f}%")

    # === 3. Best variant per pair (= 各ペアで best な variant を選ぶ場合)
    print()
    print("=" * 80)
    print(f"⚠️ Best-per-pair portfolio (IN-SAMPLE optimization, overfitting risk!)")
    print("=" * 80)
    print("  各ペアで Sum R が一番高い variant を選んだ場合の架空のポートフォリオ。")
    print("  これは「結果から逆算した最適選択」なので、将来の利益保証ではない。")
    print()
    print(f"  {'Symbol':10s} {'Best':12s} {'sumR':>8s} {'WR%':>6s} {'n':>5s}")
    print("-" * 50)
    total_n = total_w = total_l = 0
    total_sumR = 0.0
    best_choices = {}
    for sym in all_pairs:
        best_name = None
        best_sumR = -1e9
        for name in variants:
            s = pair_stats[sym][name]
            if s["n"] >= 30 and s["sumR"] > best_sumR:  # 最低 30 件は必要 (noise 排除)
                best_sumR = s["sumR"]
                best_name = name
        if best_name is None:
            continue
        s = pair_stats[sym][best_name]
        best_choices[sym] = best_name
        total_n += s["n"]; total_w += s["won"]; total_l += s["lost"]; total_sumR += s["sumR"]
        print(f"  {sym:10s} {best_name:12s} {s['sumR']:+8.2f} {s['wr']:5.1f}% {s['n']:5d}")
    wr_t = 100*total_w/(total_w+total_l) if (total_w+total_l) else 0
    avg_R = total_sumR/total_n if total_n else 0
    avg_month = total_sumR/n_months*args.risk_pct*100
    total_pct = ((1 + avg_month/100) ** n_months - 1) * 100
    print("-" * 50)
    print(f"  TOTAL: n={total_n} WR={wr_t:.1f}% sumR={total_sumR:+.2f} avgR={avg_R:+.4f}")
    print(f"         月利 {avg_month:+.3f}% (単利) / 累積 {total_pct:+.1f}% ({n_months}ヶ月複利想定)")

    # === 4. Positive-only portfolio (= sumR > 0 ペアだけ使う、各ペアで best variant)
    print()
    print("=" * 80)
    print(f"⚠️ Positive-pair-only portfolio (これも IN-SAMPLE)")
    print("=" * 80)
    print("  Best variant で sumR > 0 のペアだけ採用 (= 損するペアは取引しない)")
    print()
    total_n = total_w = total_l = 0
    total_sumR = 0.0
    print(f"  {'Symbol':10s} {'Variant':12s} {'sumR':>8s} {'WR%':>6s}")
    print("-" * 50)
    for sym in all_pairs:
        best_name = best_choices.get(sym)
        if not best_name:
            continue
        s = pair_stats[sym][best_name]
        if s["sumR"] <= 0:
            continue
        total_n += s["n"]; total_w += s["won"]; total_l += s["lost"]; total_sumR += s["sumR"]
        print(f"  {sym:10s} {best_name:12s} {s['sumR']:+8.2f} {s['wr']:5.1f}%")
    wr_t = 100*total_w/(total_w+total_l) if (total_w+total_l) else 0
    avg_R = total_sumR/total_n if total_n else 0
    avg_month = total_sumR/n_months*args.risk_pct*100
    total_pct = ((1 + avg_month/100) ** n_months - 1) * 100
    print("-" * 50)
    print(f"  TOTAL: n={total_n} WR={wr_t:.1f}% sumR={total_sumR:+.2f}")
    print(f"         月利 {avg_month:+.3f}% / 累積 {total_pct:+.1f}%")

    # === 5. Caveat
    print()
    print("=" * 80)
    print("⚠️ OVERFITTING WARNING")
    print("=" * 80)
    print("  - 「Best-per-pair」「Positive-only」は backtest 結果からの最適化なので")
    print("    in-sample bias を含む。実運用で同じパフォーマンスは保証されない。")
    print("  - 結論を出す前に、別期間 (例: 2016-2020) でのバックテスト必須。")
    print("  - 1 ペアあたり n<50 だと統計的信頼性が低い (95% CI が広い)。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
