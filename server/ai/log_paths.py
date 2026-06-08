"""
log_paths.py — AI 判断ログのファイルパス決定と session_start 書き込み。

ディレクトリ構造:
    data/ai_decisions/
        <SYMBOL>/
            <YYYYMMDD_HHMMSS>_<session_id>.jsonl

ファイル先頭には type="session_start" のメタデータレコードを書き、
何の戦略・モデル・パラメータで動かしたかを後から特定できるようにする。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
# 環境変数 AI_DECISIONS_DIR で出力先を上書き可能。
# 例: ベースライン比較 (zigzag_ai + stub-always-enter) を別ディレクトリに分けたいときに使う。
#   AI_DECISIONS_DIR=data/baseline_decisions python -m uvicorn server.main:app ...
_OVERRIDE = os.environ.get("AI_DECISIONS_DIR")
DATA_ROOT = Path(_OVERRIDE) if _OVERRIDE else (ROOT / "data" / "ai_decisions")


def resolve_log_path(
    symbol: str,
    session_id: str,
    started_at: Optional[datetime] = None,
) -> Path:
    """このセッションが書くべき JSONL のパスを返す。

    親ディレクトリは作らない (実書き込み時に作る)。
    """
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    sym = (symbol or "UNKNOWN").strip().upper().replace("/", "_") or "UNKNOWN"
    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    fname = f"{stamp}_{session_id}.jsonl"
    return DATA_ROOT / sym / fname


def write_session_start(
    path: Path,
    *,
    session_id: str,
    symbol: str,
    strategy: str,
    model_name: str,
    params: dict,
    extra: Optional[dict] = None,
) -> None:
    """ファイル先頭に session_start レコードを書く。

    既にファイルがあれば追記しない (二重起動時の安全のため)。
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "session_start",
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "symbol": symbol,
        "strategy": strategy,
        "model": model_name,
        "params": params,
    }
    if extra:
        record.update(extra)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_record(path: Path, record: dict) -> None:
    """汎用の追記関数。data_collector / outcome_tracker から共用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
