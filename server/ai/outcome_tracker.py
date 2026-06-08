"""
outcome_tracker.py — エントリ判断とその後のトレード結果 (outcome) を紐付けて記録する。

責務:
  - AI が "enter" 判定したときに pending_entry として保持
  - エントリ後にポジションが閉じたら、その結果 (exit_price, PnL, exit_reason) を記録
  - data/ai_decisions/<session>.jsonl に type="outcome" のレコードとして追記

これにより、後から decision × outcome を join して教師ラベル付き学習データを作れる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from server.ai.log_paths import append_record


@dataclass
class PendingEntry:
    decision_id: str
    side: str                 # "long" or "short"
    entry_price: float
    entry_bar_time: int
    entry_bar_count: int      # bar_idx 相当 (= self.barIdx at entry)
    lot: float
    sl: Optional[float]
    tp: Optional[float]
    features: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)


class OutcomeTracker:
    """1 セッション分の outcome 追跡。"""

    def __init__(
        self,
        session_id: str,
        symbol: str,
        strategy_name: str,
        log_path: Path,
    ) -> None:
        self.session_id = session_id
        self.symbol = symbol
        self.strategy_name = strategy_name
        self.log_path = log_path
        # 同一方向につき最大 1 件の pending (本戦略は同時 1 ポジション前提)
        self.pending: dict[str, PendingEntry] = {}

    # ---- API ----

    def register_entry(
        self,
        decision_id: str,
        direction: str,                  # "up" / "down"
        entry_price: float,
        entry_bar_time: int,
        entry_bar_count: int,
        lot: float,
        sl: Optional[float],
        tp: Optional[float],
        features: dict,
        decision: dict,
    ) -> None:
        side = "long" if direction == "up" else "short"
        # 既に同方向の pending があったら上書き (再エントリのケース)
        self.pending[side] = PendingEntry(
            decision_id=decision_id,
            side=side,
            entry_price=entry_price,
            entry_bar_time=entry_bar_time,
            entry_bar_count=entry_bar_count,
            lot=lot,
            sl=sl,
            tp=tp,
            features=features,
            decision={
                "action": decision.action,
                "confidence": decision.confidence,
                "reason": decision.reason,
            } if hasattr(decision, "action") else dict(decision),
        )

    def on_position_close(
        self,
        side: str,                       # 閉じた側 "long" / "short"
        exit_price: float,
        exit_bar_time: int,
        exit_bar_count: int,
        exit_reason: str,                # "strategy_close" / "tp_or_sl" / "session_end"
    ) -> Optional[dict]:
        entry = self.pending.pop(side, None)
        if entry is None:
            return None
        # 価格単位の PnL (符号付き)
        if side == "long":
            pnl_price = exit_price - entry.entry_price
        else:
            pnl_price = entry.entry_price - exit_price
        # SL/TP の判定: pnl の符号でざっくり推定
        if exit_reason == "tp_or_sl":
            exit_reason = "tp_hit" if pnl_price > 0 else "sl_hit"
        bars_held = max(0, exit_bar_count - entry.entry_bar_count)
        record = {
            "type": "outcome",
            "ts": datetime.now(timezone.utc).isoformat(),
            "decision_id": entry.decision_id,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "strategy": self.strategy_name,
            "side": side,
            "entry_price": entry.entry_price,
            "exit_price": exit_price,
            "entry_bar_time": entry.entry_bar_time,
            "exit_bar_time": exit_bar_time,
            "bars_held": bars_held,
            "pnl_price": pnl_price,
            "lot": entry.lot,
            "sl": entry.sl,
            "tp": entry.tp,
            "exit_reason": exit_reason,
        }
        self._append(record)
        return record

    def has_pending(self, side: str) -> bool:
        return side in self.pending

    def flush_remaining(self, exit_bar_time: int, exit_bar_count: int) -> None:
        """セッション終了時に未決済のままなら "session_end" として記録。"""
        for side in list(self.pending.keys()):
            entry = self.pending[side]
            # 損益は確定不可なので 0、reason=session_end
            self.on_position_close(
                side=side,
                exit_price=entry.entry_price,  # 暫定: エントリ価格と同じ → pnl=0
                exit_bar_time=exit_bar_time,
                exit_bar_count=exit_bar_count,
                exit_reason="session_end",
            )

    # ---- 内部 ----

    def _append(self, record: dict) -> None:
        append_record(self.log_path, record)
