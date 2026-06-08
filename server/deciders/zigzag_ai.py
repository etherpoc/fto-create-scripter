"""
zigzag_ai.py — zigzag_line_break + AI フィルタ。

既存の ZigZagLineBreakStrategy をそのまま継承し、`entry_filter` フックに
AI モデル (Ollama / Stub) を差し込む。

選択する AI モデルは環境変数 ZIGZAG_AI_MODEL で切り替え可能:
  - "stub-always-enter"  : 全部 enter (= 現行と同じ)
  - "stub-random"        : 50% でランダム skip
  - "stub-rule-based"    : H4 壁ルールベース
  - "ollama:<model>"     : Ollama 経由のローカル LLM (例: "ollama:gemma3:4b")

未指定なら "ollama:gemma3:4b" を試す。Ollama が起動してなければエラーは
predict 内でフォールバック (skip) するので戦略自体は安全に動く。
"""

from __future__ import annotations

import os
import uuid
from typing import Optional

from server.ai.base import AIDecision
from server.ai.data_collector import log_decision
from server.ai.features import features_for_llm
from server.ai.ollama_client import OllamaAIModel
from server.ai.stub import AlwaysEnterAI, RandomAI, RuleBasedAI
from server.deciders.registry import register
from strategies.zigzag_line_break.strategy import (
    Params as _BaseParams,
    ZigZagLineBreakStrategy as _Base,
)


def _build_ai_model(spec: str):
    """環境変数 / 設定文字列から AIModel インスタンスを作る。"""
    if spec == "stub-always-enter":
        return AlwaysEnterAI()
    if spec == "stub-random":
        return RandomAI(enter_probability=0.5)
    if spec == "stub-rule-based":
        return RuleBasedAI()
    if spec.startswith("ollama:"):
        model_name = spec.split(":", 1)[1] or "gemma3:4b"
        return OllamaAIModel(model=model_name)
    # フォールバック: 既定の Ollama モデル
    return OllamaAIModel(model="gemma3:4b")


class ZigZagAIStrategy(_Base):
    """既存 zigzag_line_break に AI フィルタを足しただけのバリエーション。"""

    PARAMS_CLS = _BaseParams  # registry が探す class 属性

    def __init__(self, params) -> None:  # type: ignore[no-untyped-def]
        super().__init__(params)
        model_spec = os.environ.get("ZIGZAG_AI_MODEL", "ollama:gemma3:4b")
        self.ai_model = _build_ai_model(model_spec)

        # AI が enter を返しても confidence が閾値未満なら skip 扱いにする。
        # 0.0 で無効化。デフォルトは無効。env で 0.7 等を入れると有効。
        # 「自信のある判断だけ取る」フィルタなので、件数は減るが質は上がる想定。
        try:
            self.conf_threshold = float(os.environ.get("AI_CONF_THRESHOLD", "0.0"))
        except (TypeError, ValueError):
            self.conf_threshold = 0.0

        # session.py から差し込まれる:
        self.session_id = "unknown"
        self.symbol = "unknown"
        self.log_path = None  # type: ignore[assignment]
        self.outcome_tracker = None  # type: ignore[assignment]

        # session.py が ctx.buy/sell を見たときに参照する直近 AI 判断スナップショット
        self._last_ai_decision: Optional[dict] = None

        # entry_filter をラップ関数で接続
        self.entry_filter = self._ai_filter

    def _ai_filter(self, direction: str, features: dict) -> bool:
        # LLM へは判断に不要なメタ情報 (bar_time / bar_idx / atr / price) を
        # 取り除いた縮約版を送る。トークン節約 + 注意散漫の防止。
        # ログにはフル features を残すので学習データには影響しない。
        decision: AIDecision = self.ai_model.predict(features_for_llm(features))
        # 信頼度スレッショルド: AI が enter でも低自信なら skip 扱い
        if (
            decision.action == "enter"
            and self.conf_threshold > 0
            and decision.confidence < self.conf_threshold
        ):
            decision = AIDecision(
                action="skip",
                confidence=decision.confidence,
                reason=f"conf<{self.conf_threshold}: {decision.reason[:80]}",
                raw=decision.raw,
            )
        decision_id = str(uuid.uuid4())
        # ログ蓄積 (将来の学習データ)
        try:
            if self.log_path is not None:
                bar_time = int(features.get("bar_time") or 0)
                price = float(features.get("price") or 0.0)
                log_decision(
                    log_path=self.log_path,
                    session_id=self.session_id,
                    symbol=self.symbol,
                    strategy="zigzag_ai",
                    model_name=self.ai_model.name,
                    features=features,
                    decision=decision,
                    bar_time=bar_time,
                    price=price,
                    decision_id=decision_id,
                )
        except Exception:  # noqa: BLE001
            pass  # 学習データ書き出し失敗は無視

        if decision.action == "enter":
            # session.py が ctx.buy/sell を検出した直後に取りに来る情報
            self._last_ai_decision = {
                "decision_id": decision_id,
                "direction": direction,
                "features": features,
                "decision": decision,
            }
            return True
        return False


# registry 登録
register("zigzag_ai")(ZigZagAIStrategy)
