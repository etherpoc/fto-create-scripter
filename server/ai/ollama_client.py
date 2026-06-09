"""
ollama_client.py — Ollama (ローカル LLM サーバ) を叩いて判断を取る AIModel。

Ollama のデフォルトエンドポイント http://localhost:11434/api/chat を使う。
レスポンスは JSON での action を期待し、パース失敗時は安全側 (skip) に倒す。

使い方:
    from server.ai.ollama_client import OllamaAIModel
    ai = OllamaAIModel(model="gemma3:4b")
    decision = ai.predict({"direction_intent": "up", "trend": "up", ...})
    print(decision.action, decision.confidence, decision.reason)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from server.ai.base import AIDecision

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a trading decision assistant for an FX/commodity ZigZag swing strategy
operating on M15 bars with H1/H4 higher-timeframe context.

You will receive the current market state as a JSON object. All prices are
normalized to ATR multiples relative to the current price (negative = below,
positive = above). Use your knowledge of the instrument (`symbol`) to judge
how typical patterns play out (e.g. metals tend to keep momentum after a
structural breakout, JPY pairs trend persistently, EUR/GBP often range).

Key fields:
- `symbol`              : asset (XAUUSD, USDJPY, EURUSD, ...)
- `direction_intent`    : "up" (planning to BUY) or "down" (planning to SELL)
- `trend` / `h1_trend` / `h4_trend` : Dow trend on each TF ("up" / "down" / null)
- `wall_blocking_h4_atr`   : nearest H4 line **in the direction of the trade**.
                             VERY SMALL (< 0.5) = wall is right above/below, immediate target/risk.
- `wall_supporting_h4_atr` : nearest H4 line behind the trade (backstop).
- `wall_blocking_h1_atr` / `wall_supporting_h1_atr` : same for H1.
- `reversal_z1_pivot_diff_atr` : where SL will sit (signed).
- `recent_close_diffs_atr` : last 5 M15 close-to-close moves (signed). Reads
                              immediate momentum.
- `bars_since_reversal`  : how many bars since the structural reversal fired.
- `rsi_m15`              : RSI(14) on M15. 30 / 70 standard thresholds.
- `atr_ratio_vs_recent`  : current ATR / mean of last 20 ATR. >1.5 = volatility
                            expansion (news / impulsive). <0.7 = contracting range.
- `hour_utc`             : 0–23 UTC.
- `weekday`              : 0=Mon … 6=Sun.
- `tokyo_open` / `london_open` / `ny_open` : session booleans.
- `is_overlap`           : London+NY overlap (peak liquidity).
- `is_quiet`             : no major session open (low liquidity, noise risk).
- `double_top_m15` / `double_bottom_m15` : two recent Z1 pivots at very similar
                            prices on M15. A real double top is a strong reversal
                            signal — be wary of buying into one.
- `double_top_h4` / `double_bottom_h4`    : same on H4 (much stronger).
- `touches_above_h4` / `touches_below_h4` : how many recent pivots tapped the
                            nearest H4 wall (above/below). 3+ = strong S/R level.
- `bars_since_20bar_high` / `bars_since_20bar_low` : freshness of recent extremes.
                            0 = just made a new extreme. Larger = stale.
- `recent_5_ohlc`        : last 5 M15 bars as {o,h,l,c}_atr (price diffs / ATR).
                            Read the SHAPE: long upper wicks, engulfings, dojis,
                            momentum bars. This is your visual of the chart.

Decide whether to ENTER the trade or SKIP it. The rule-based layer has already
validated the basic setup (reversal + line trigger), so you are filtering edge
cases, not re-checking the strategy.

Reply with VALID JSON ONLY in this exact format (no surrounding markdown, no commentary):
{"action": "enter" | "skip", "confidence": <float 0..1>, "reason": "<short text>"}

Decision rules (READ CAREFULLY):

1. **Bias toward ENTER, but enforce real risk filters.**
   The strategy already validated the setup, so you're filtering edge cases.
   But "no walls + null trend" is NOT a sufficient reason on its own — you must
   also have no negative signals from rules 2–5 below.

2. **TF disagreement is a real negative signal:**
   - If direction_intent="up" and (h1_trend="down" OR h4_trend="down"):
     → likely SKIP. Upper TF is in the opposite direction = high reversal risk.
   - If direction_intent="down" and (h1_trend="up" OR h4_trend="up"):
     → likely SKIP. Same reason.
   - When TF is null, treat as neutral (not negative).
   - Override: if multiple positives elsewhere (e.g. strong aligned momentum
     + close supporting wall), you may still enter with low confidence.

3. **Wall semantics (numbers are ATR-multiples; larger = farther = safer):**
   - `wall_blocking_h4_atr` ∈ [0, 0.5) = wall is right there → SKIP (immediate target).
   - `wall_blocking_h4_atr` ∈ [0.5, 1.5) = cramped room → SKIP unless TF aligned.
   - `wall_blocking_h4_atr` >= 1.5 or null = plenty of room → not a reason to skip,
     but also not a reason to enter on its own.

4. **Momentum check (recent_close_diffs_atr is signed; positive = upward bars):**
   - direction_intent="up" and at least 3 of last 5 diffs <= -0.5 → SKIP
     (price is actively going against you).
   - direction_intent="down" and at least 3 of last 5 diffs >= 0.5 → SKIP.
   - Otherwise (mixed or aligned) → momentum is fine.

5. **NULL handling:** treat null as "no signal" (neutral). Do not count null as
   either positive or negative.

6. **bars_since_reversal**: 0–100 is normal. Treat as stale only if > 150.

7. **Symbol prior**: XAUUSD/metals tend to follow through on breakouts; consider
   slightly more lenient. JPY pairs trend persistently; trend alignment matters
   more. EUR/GBP often range; mean reversion is more common.

8. **RSI sanity check (use only when extreme):**
   - direction_intent="up" and rsi_m15 > 75 → buying into overbought; SKIP unless
     strong momentum confirms (breakout continuation possible).
   - direction_intent="down" and rsi_m15 < 25 → shorting into oversold; SKIP unless
     strong downside momentum.
   - 25–75 range → not a factor.

9. **Liquidity / session filter:**
   - `is_quiet=true` → low liquidity, prefer SKIP unless setup is very strong.
   - `is_overlap=true` (London/NY peak) → best execution; lean ENTER on borderline.
   - weekday=4 (Fri) after ~18 UTC, weekday=0 (Mon) before ~7 UTC → thin liquidity.

10. **Volatility regime (atr_ratio_vs_recent):**
    - `< 0.7` (contraction) → range market, false breakouts more likely → SKIP bias.
    - `0.7–1.5` → normal.
    - `> 1.5` (expansion) → impulsive move ongoing → ENTER bias if aligned.

11. **Confidence**: 0.5 = borderline; 0.7+ = clear setup; 0.8+ = strong setup with
    multiple aligned signals.

Be concise. JSON only. No reasoning text outside the JSON.
"""


# ★ v7 で実験的に導入したペア別ガイダンスは、replay アブレーション (2026-06-09)
# の結果「逆効果」だったので空に戻す。
# 詳細: USDJPY WR が 8% → 29% に改善、XAUUSD WR が 43% → 50% に改善。
# AI を過度に保守的にし「BoJ 介入リスク」「oil 相関」を過剰警戒させていた。
# LLM の事前知識に任せた方が良いと判明。再追加する際は事前にアブレーション必須。
# (旧版は git history 参照)
SYMBOL_GUIDANCE: dict[str, str] = {}


def _user_content_for(features: dict) -> str:
    """LLM への user メッセージを組み立てる。symbol 別ガイドラインを前置きする。

    env DISABLE_PER_SYMBOL_GUIDANCE=1 で guidance を抑制 (アブレーション用)。
    """
    import os
    symbol = str(features.get("symbol") or "").upper()
    if os.environ.get("DISABLE_PER_SYMBOL_GUIDANCE"):
        guidance = ""
    else:
        guidance = SYMBOL_GUIDANCE.get(symbol, "")
    prefix = ""
    if guidance:
        prefix = f"Symbol guidance for {symbol}:\n{guidance}\n\n"
    return prefix + "Features:\n" + json.dumps(features, ensure_ascii=False)


@dataclass
class OllamaAIModel:
    """Ollama 経由でローカル LLM に問い合わせる AIModel 実装。"""

    model: str = "gemma3:4b"  # Ollama のモデル名
    base_url: str = "http://localhost:11434"
    # 8 ペア並列 backtest だと Ollama がリクエストを直列処理してキューが詰まり、
    # 60 秒の timeout を超えるケースがあった。120 秒に伸ばしてカバー。
    timeout_seconds: float = 120.0
    temperature: float = 0.0  # 判断はなるべく決定的に
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = f"ollama:{self.model}"

    def warmup(self) -> bool:
        """サーバ起動時に 1 回叩いてモデルを VRAM に常駐させる。

        最初の実トレード判断でタイムアウトを起こさないため。返り値はウォームアップ
        成功フラグ (失敗しても致命ではないので戻り値を見ない呼び出しでも OK)。
        """
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "stream": False,
                        "options": {"temperature": 0.0, "num_predict": 1},
                        "messages": [
                            {"role": "user", "content": "ok"},
                        ],
                    },
                )
                resp.raise_for_status()
            log.info("Ollama warmup OK for model=%s", self.model)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Ollama warmup failed: %s", e)
            return False

    def predict(self, features: dict) -> AIDecision:
        try:
            content = self._call(features)
        except Exception as e:  # noqa: BLE001
            log.warning("Ollama call failed (%s); falling back to skip", e)
            return AIDecision(
                action="skip",
                confidence=0.0,
                reason=f"ollama-error: {e}",
            )
        return self._parse(content)

    # ---- 内部 ----
    def _call(self, features: dict) -> str:
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                # gemma4:e4b 等の thinking モデル対応:
                # default の num_predict が小さいと thinking で打ち切られ、JSON
                # 出力前に done_reason="length" で終わってしまうので余裕を持たせる。
                "num_predict": 4096,
            },
            # thinking モデルでも JSON を出させるため、thinking を抑制する。
            # Ollama 0.3+ で対応。古いバージョンでは無視されるだけで害なし。
            "think": False,
            "format": "json",  # Ollama に JSON Mode を要求
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_content_for(features)},
            ],
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        # Ollama /api/chat レスポンス形: {"message": {"content": "..."}}
        msg = data.get("message") or {}
        return str(msg.get("content") or "")

    @staticmethod
    def _parse(content: str) -> AIDecision:
        if not content.strip():
            return AIDecision(action="skip", confidence=0.0, reason="ollama-empty-response")
        try:
            obj: Any = json.loads(content)
        except json.JSONDecodeError as e:
            log.warning("invalid JSON from ollama: %s", e)
            return AIDecision(
                action="skip",
                confidence=0.0,
                reason=f"ollama-invalid-json: {content[:80]}",
                raw={"content": content},
            )
        if not isinstance(obj, dict):
            return AIDecision(action="skip", confidence=0.0, reason="ollama-non-dict")

        action = str(obj.get("action") or "").lower().strip()
        if action not in ("enter", "skip"):
            return AIDecision(
                action="skip",
                confidence=0.0,
                reason=f"ollama-bad-action: {action!r}",
                raw=obj,
            )

        # confidence は 0..1 にクランプ
        try:
            confidence = float(obj.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        reason = str(obj.get("reason") or "")[:200]
        return AIDecision(action=action, confidence=confidence, reason=reason, raw=obj)
