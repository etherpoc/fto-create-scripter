"""
data_collector.py — AI 判断を 1 行 1 件で JSON Lines に追記する。

書き込み先のパスは log_paths.resolve_log_path() で銘柄ごとに決まる。
session.py が StrategySession 作成時にパスを resolve し、戦略に渡してくれる。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.ai.log_paths import append_record


def log_decision(
    *,
    log_path: Path,
    session_id: str,
    symbol: str,
    strategy: str,
    model_name: str,
    features: dict,
    decision: Any,  # AIDecision
    bar_time: int,
    price: float,
    decision_id: str = "",
) -> None:
    """1 つの判断を JSONL に追記する。

    log_path は session.py 側で解決済みのフルパスを渡してもらう。
    """
    record = {
        "type": "decision",
        "ts": datetime.now(timezone.utc).isoformat(),
        "decision_id": decision_id,
        "session_id": session_id,
        "symbol": symbol,
        "strategy": strategy,
        "model": model_name,
        "features": features,
        "decision": {
            "action": decision.action,
            "confidence": decision.confidence,
            "reason": decision.reason,
        },
        "bar_time": bar_time,
        "price": price,
    }
    append_record(log_path, record)
