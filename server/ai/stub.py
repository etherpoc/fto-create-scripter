"""
stub.py — テスト用ダミー AI モデル。

3 種類:
- AlwaysEnterAI : すべて enter (= 現行戦略と同じトレード量)
- RandomAI      : 確率 P で enter (デフォルト 0.5)
- RuleBasedAI   : 上位足壁とトレンドを使った簡易ルール (ベースライン参考)

接続テストや Ollama 不調時のフォールバックに使う。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from server.ai.base import AIDecision


@dataclass
class AlwaysEnterAI:
    name: str = "stub-always-enter"

    def predict(self, features: dict) -> AIDecision:
        return AIDecision(action="enter", confidence=1.0, reason="stub: always enter")


@dataclass
class RandomAI:
    enter_probability: float = 0.5
    name: str = "stub-random"

    def __post_init__(self) -> None:
        self._rng = random.Random()

    def predict(self, features: dict) -> AIDecision:
        if self._rng.random() < self.enter_probability:
            return AIDecision(action="enter", confidence=0.5, reason="stub: random enter")
        return AIDecision(action="skip", confidence=0.5, reason="stub: random skip")


@dataclass
class RuleBasedAI:
    """簡易ルールベース AI (ベースライン参考)。

    - 上位足 (H4) ラインがエントリ方向にすぐ立ち塞がってたら skip
    - 上位足トレンドと逆方向への入りは skip
    - それ以外は enter
    """

    wall_atr_threshold: float = 0.5  # ATR×0.5 以内に H4 壁があれば skip
    name: str = "stub-rule-based"

    def predict(self, features: dict) -> AIDecision:
        direction = features.get("direction_intent")
        if direction == "up":
            wall = features.get("nearest_wall_above_h4_atr")
            if wall is not None and wall < self.wall_atr_threshold:
                return AIDecision(
                    action="skip",
                    confidence=0.7,
                    reason=f"H4 wall above at {wall:.2f} ATR",
                )
        elif direction == "down":
            wall = features.get("nearest_wall_below_h4_atr")
            if wall is not None and wall < self.wall_atr_threshold:
                return AIDecision(
                    action="skip",
                    confidence=0.7,
                    reason=f"H4 wall below at {wall:.2f} ATR",
                )
        return AIDecision(action="enter", confidence=0.6, reason="rule-based: no wall, allow")
