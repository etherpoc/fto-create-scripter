"""
base.py — AIModel プロトコル。

「特徴量 dict を受け取って action を返す」という最小契約。
Stub / Ollama / sklearn / cloud LLM など、すべての実装はこれに従う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Action = Literal["enter", "skip"]


@dataclass
class AIDecision:
    """AI モデルの返答。"""

    action: Action
    confidence: float = 0.5  # 0.0..1.0
    reason: str = ""
    raw: dict | None = None  # 生レスポンス (デバッグ用)


@runtime_checkable
class AIModel(Protocol):
    """すべての AI 実装が満たすべき最小インタフェース。"""

    name: str  # モデル識別子 (ログや学習データの紐付けに使う)

    def predict(self, features: dict) -> AIDecision:
        """features dict を受け取って判断を返す。

        実装は I/O 失敗 / モデルエラー / 無効なレスポンスのとき
        action="skip" のフォールバックを返すこと (常に安全側)。
        """
        ...
