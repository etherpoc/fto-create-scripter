"""
build_training_data.py — 蓄積された判断ログから学習データを組み立てる。

各セッションの JSONL を読み、decision レコードと outcome レコードを decision_id で
join して、教師ラベル付き 1 行 = 1 判断のレコードを CSV / JSONL に書き出す。

使い方:
    python tools/build_training_data.py                    # 全銘柄をまとめて
    python tools/build_training_data.py --symbol XAUUSD    # 特定の銘柄だけ
    python tools/build_training_data.py --out data/train.jsonl --format jsonl
    python tools/build_training_data.py --since 2026-06-01 # 期間指定

出力スキーマ (1 行 1 判断):
    {
      "decision_id": "...",
      "session_id": "...",
      "symbol": "...",
      "ts_decision": "...",
      "bar_time": int,
      "features": {...},
      "model": "...",
      "action": "enter" | "skip",
      "confidence": float,
      "ai_reason": "...",
      // 以下、対応する outcome があれば付与
      "has_outcome": bool,
      "exit_reason": "tp_hit" | "sl_hit" | "strategy_close" | "session_end" | null,
      "pnl_price": float | null,
      "bars_held": int | null,
      "won": bool | null,
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data" / "ai_decisions"


def iter_session_files(symbol_filter: str | None, since: datetime | None) -> Iterator[Path]:
    if not DATA_ROOT.exists():
        return
    for sym_dir in sorted(DATA_ROOT.iterdir()):
        if not sym_dir.is_dir():
            continue
        if symbol_filter and sym_dir.name.upper() != symbol_filter.upper():
            continue
        for fp in sorted(sym_dir.glob("*.jsonl")):
            if since is not None:
                # ファイル名先頭の YYYYMMDD_HHMMSS で簡易フィルタ
                stem = fp.stem
                try:
                    file_dt = datetime.strptime(stem[:15], "%Y%m%d_%H%M%S")
                    if file_dt < since:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            yield fp


def join_session(fp: Path) -> Iterator[dict]:
    """1 セッションファイルから decision×outcome を join した行を返す。"""
    decisions: dict[str, dict] = {}
    outcomes: dict[str, dict] = {}
    session_meta: dict = {}
    with open(fp, "r", encoding="utf-8") as f:
        for raw in f:
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            t = r.get("type")
            if t == "session_start":
                session_meta = r
            elif t == "decision":
                did = r.get("decision_id") or ""
                if did:
                    decisions[did] = r
            elif t == "outcome":
                did = r.get("decision_id") or ""
                if did:
                    outcomes[did] = r

    for did, d in decisions.items():
        oc = outcomes.get(did)
        row = {
            "decision_id": did,
            "session_id": d.get("session_id") or session_meta.get("session_id"),
            "symbol":     d.get("symbol") or session_meta.get("symbol"),
            "ts_decision": d.get("ts"),
            "bar_time":   d.get("bar_time"),
            "features":   d.get("features") or {},
            "model":      d.get("model") or session_meta.get("model"),
            "action":     (d.get("decision") or {}).get("action"),
            "confidence": (d.get("decision") or {}).get("confidence"),
            "ai_reason":  (d.get("decision") or {}).get("reason"),
        }
        if oc:
            row["has_outcome"] = True
            row["exit_reason"] = oc.get("exit_reason")
            row["pnl_price"] = oc.get("pnl_price")
            row["bars_held"] = oc.get("bars_held")
            pnl = oc.get("pnl_price")
            row["won"] = (pnl is not None and pnl > 0)
        else:
            row["has_outcome"] = False
            row["exit_reason"] = None
            row["pnl_price"] = None
            row["bars_held"] = None
            row["won"] = None
        yield row


def write_jsonl(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_csv(rows: list[dict], out_path: Path) -> None:
    import csv
    if not rows:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # features を 1 階層 flat に展開
    def flat(r: dict) -> dict:
        flat_row = {k: v for k, v in r.items() if k != "features"}
        feats = r.get("features") or {}
        for k, v in feats.items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                flat_row[f"f_{k}"] = v
        return flat_row
    flat_rows = [flat(r) for r in rows]
    fieldnames = sorted({k for r in flat_rows for k in r.keys()})
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in flat_rows:
            w.writerow(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None, help="特定の銘柄だけ (例: XAUUSD)")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD 以降のセッションのみ")
    ap.add_argument("--out", default=str(ROOT / "data" / "training.jsonl"))
    ap.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    args = ap.parse_args()

    since_dt = None
    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d")

    rows: list[dict] = []
    files = list(iter_session_files(args.symbol, since_dt))
    for fp in files:
        rows.extend(join_session(fp))

    out_path = Path(args.out)
    if args.format == "jsonl":
        write_jsonl(rows, out_path)
    else:
        if out_path.suffix == ".jsonl":
            out_path = out_path.with_suffix(".csv")
        write_csv(rows, out_path)

    # サマリ出力
    n_total = len(rows)
    n_with_outcome = sum(1 for r in rows if r["has_outcome"])
    n_enter = sum(1 for r in rows if r["action"] == "enter")
    n_won = sum(1 for r in rows if r["won"])
    print(f"sessions read    : {len(files)}")
    print(f"decisions joined : {n_total}")
    print(f"  with outcome   : {n_with_outcome}")
    print(f"  enter actions  : {n_enter}")
    print(f"  won            : {n_won}")
    print(f"output           : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
