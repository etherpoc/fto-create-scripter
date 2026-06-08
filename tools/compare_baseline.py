"""
compare_baseline.py — AI フィルタ有り (data/ai_decisions/) と
ベースライン (data/baseline_decisions/) のバックテスト結果を並べて比較する。

使い方:
    python tools/compare_baseline.py
    python tools/compare_baseline.py --ai data/ai_decisions --baseline data/baseline_decisions
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict


def load_dir(root: Path) -> dict:
    """ディレクトリ配下の全 JSONL を読んで symbol ごとに集計する。"""
    by_symbol = defaultdict(lambda: {
        "files": 0,
        "decisions": 0,
        "enter": 0,
        "skip": 0,
        "outcomes": 0,
        "won": 0,
        "lost": 0,
        "tp_hit": 0,
        "sl_hit": 0,
        "other_exit": 0,
        "sum_pnl": 0.0,
    })
    if not root.exists():
        return dict(by_symbol)
    for sym_dir in sorted(root.iterdir()):
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name
        for fp in sorted(sym_dir.glob("*.jsonl")):
            by_symbol[sym]["files"] += 1
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    t = r.get("type")
                    if t == "decision":
                        by_symbol[sym]["decisions"] += 1
                        act = (r.get("decision") or {}).get("action")
                        if act == "enter":
                            by_symbol[sym]["enter"] += 1
                        elif act == "skip":
                            by_symbol[sym]["skip"] += 1
                    elif t == "outcome":
                        by_symbol[sym]["outcomes"] += 1
                        pnl = r.get("pnl_price") or 0.0
                        by_symbol[sym]["sum_pnl"] += pnl
                        if pnl > 0:
                            by_symbol[sym]["won"] += 1
                        elif pnl < 0:
                            by_symbol[sym]["lost"] += 1
                        er = r.get("exit_reason")
                        if er == "tp_hit":
                            by_symbol[sym]["tp_hit"] += 1
                        elif er == "sl_hit":
                            by_symbol[sym]["sl_hit"] += 1
                        else:
                            by_symbol[sym]["other_exit"] += 1
    return dict(by_symbol)


def fmt_row(label: str, ai: dict, base: dict) -> list[str]:
    def wr(d: dict) -> str:
        n = d["outcomes"]
        if n == 0:
            return " - "
        return f"{100.0*d['won']/n:.1f}%"
    def pnl(d: dict) -> str:
        return f"{d['sum_pnl']:+.4f}"
    return [
        label,
        f"{ai['enter']}",         f"{base['enter']}",
        f"{ai['outcomes']}",      f"{base['outcomes']}",
        f"{ai['won']}/{ai['lost']}",   f"{base['won']}/{base['lost']}",
        wr(ai),                   wr(base),
        pnl(ai),                  pnl(base),
    ]


def print_table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    def line(r): return "  ".join(c.ljust(w) for c, w in zip(r, widths))
    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))
    print(line(header))
    print(sep)
    for r in rows:
        print(line(r))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai", default="data/ai_decisions")
    ap.add_argument("--baseline", default="data/baseline_decisions")
    args = ap.parse_args()

    ai_data = load_dir(Path(args.ai))
    base_data = load_dir(Path(args.baseline))
    all_symbols = sorted(set(ai_data.keys()) | set(base_data.keys()))
    empty = {
        "files": 0, "decisions": 0, "enter": 0, "skip": 0,
        "outcomes": 0, "won": 0, "lost": 0,
        "tp_hit": 0, "sl_hit": 0, "other_exit": 0, "sum_pnl": 0.0,
    }

    header = [
        "Symbol",
        "AI:ent", "BL:ent",
        "AI:oc",  "BL:oc",
        "AI:W/L", "BL:W/L",
        "AI:WR",  "BL:WR",
        "AI:PnL", "BL:PnL",
    ]
    rows = []
    total_ai = dict(empty)
    total_base = dict(empty)
    for sym in all_symbols:
        a = ai_data.get(sym, empty)
        b = base_data.get(sym, empty)
        rows.append(fmt_row(sym, a, b))
        for k in total_ai:
            total_ai[k] = total_ai[k] + a.get(k, 0)
            total_base[k] = total_base[k] + b.get(k, 0)
    rows.append(fmt_row("TOTAL", total_ai, total_base))

    print(f"AI:       {args.ai}")
    print(f"Baseline: {args.baseline}")
    print()
    print_table(rows, header)
    print()

    # AI 効果サマリ (両方に outcome があれば)
    if total_ai["outcomes"] > 0 and total_base["outcomes"] > 0:
        ai_wr = total_ai["won"] / total_ai["outcomes"]
        base_wr = total_base["won"] / total_base["outcomes"]
        diff = (ai_wr - base_wr) * 100
        print(f"Win rate diff:  AI - Baseline = {diff:+.2f}pp")
        skip_rate = total_ai["skip"] / max(1, total_ai["decisions"]) * 100
        print(f"AI skip rate:   {skip_rate:.1f}% ({total_ai['skip']} / {total_ai['decisions']} decisions)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
