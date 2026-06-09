"""
session.py — 1 つの EA 接続に対応するサーバ側セッション状態。

責務:
  - 戦略インスタンスを保持 (= ZigZag トラッカ等の状態を持ち続ける)
  - tick メッセージで届いた新規バーを履歴に追加
  - RemoteContext を組んで Strategy.on_bar(ctx) を呼ぶ
  - ctx に積まれた commands / logs を集める
  - トラッカに新規ピボットが入ったら描画指示を auto emit
  - EA に返すレスポンス dict を返す
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.remote_context import RemoteContext  # noqa: E402
from src.core.strategy_base import Bar  # noqa: E402

# レジストリ参照のため、deciders を import してロジックを自己登録させる
import server.deciders  # noqa: F401,E402
from server.deciders.registry import get_strategy_entry, list_strategies  # noqa: E402
from server.ai.outcome_tracker import OutcomeTracker  # noqa: E402
from server.ai.log_paths import resolve_log_path, write_session_start  # noqa: E402


# ============================================================================
# 描画カラー設定 (チャート上のピボットマーカー)
# ============================================================================
# ここを編集するだけで色とサイズを変えられる。サーバ再起動で反映。
# 色は FTO 側の文字列指定 (Red/Lime/Orange/DodgerBlue/Magenta/Aqua 等が無難)。
# 16 進指定 ("#FF8800" 等) でも一応通るが、銘柄/環境によって解釈差が出るため
# 名前で指定する方が安定する。
#
# kind: "high" = ▼ (Z 高値ピボット), "low" = ▲ (Z 安値ピボット)
# tag : 描画オブジェクトの名前 prefix。同じ tag で kind+index が重ならないよう、
#       trakker ごとに一意にする。
# 色は HEX 文字列 (#RRGGBB) で渡す。FTO の SetObjectProperty(OBJPROP_COLOR) は
# 文字列を ARGB に自動変換し、alpha=FF (opaque) を補完してくれる。
# 整数 (12632256 等) を渡すと最上位バイトを alpha と解釈され、A=00=透明になり消える。
# Z1 系 (M15/H1/H4) はすべて Yellow、Z2 は White に統一。
# 時間軸の違いはフォントサイズで表現する (M15 12, H1 14, H4 16)。
# 高値 (▼) / 安値 (▲) はラベル文字で識別。
Z1_COLOR = "#FFFF00"  # Yellow
Z2_COLOR = "#FFFFFF"  # White

DRAW_STYLES = {
    "z1_m15": {
        "tag": "z1m15",
        "drawn_attr": "drawn_z1_m15",
        "color_high": Z1_COLOR,
        "color_low":  Z1_COLOR,
        "font_size":  12,
    },
    "z2_m15": {
        "tag": "z2m15",
        "drawn_attr": "drawn_z2_m15",
        "color_high": Z2_COLOR,
        "color_low":  Z2_COLOR,
        "font_size":  10,
    },
    "z1_h1": {
        "tag": "z1h1",
        "drawn_attr": "drawn_z1_h1",
        "color_high": Z1_COLOR,
        "color_low":  Z1_COLOR,
        "font_size":  14,
    },
    "z1_h4": {
        "tag": "z1h4",
        "drawn_attr": "drawn_z1_h4",
        "color_high": Z1_COLOR,
        "color_low":  Z1_COLOR,
        "font_size":  16,
    },
}

# エントリ系マーカーの色
ENTRY_BUY_COLOR  = "#00FFFF"  # Cyan
ENTRY_SELL_COLOR = "#FF69B4"  # Hot Pink (視認性重視)
ENTRY_CLOSE_COLOR = "#FFA500" # Orange (Z1 Yellow と被らないように)


class StrategySession:
    """1 つの WS 接続に対応するセッション。レジストリで Strategy を解決する。"""

    def __init__(
        self,
        session_id: str,
        symbol: str,
        strategy_name: str,
        params_overrides: dict | None = None,
    ) -> None:
        self.session_id = session_id
        self.symbol = symbol
        self.strategy_name = strategy_name

        entry = get_strategy_entry(strategy_name)
        if entry is None:
            available = ", ".join(list_strategies()) or "(none)"
            raise ValueError(
                f"unknown strategy '{strategy_name}'. registered: {available}"
            )
        StrategyCls, ParamsCls = entry

        params = ParamsCls()
        # クライアントから来た override を反映
        for k, v in (params_overrides or {}).items():
            if hasattr(params, k):
                setattr(params, k, v)
        # ★ アブレーション用 env オーバーライド
        # AI_CONF_SIZE_MULT=1.0 で contrarian sizing を無効化など。
        import os
        env_overrides = {
            "ai_conf_size_mult": "AI_CONF_SIZE_MULT",
            "ai_conf_size_high": "AI_CONF_SIZE_HIGH",
            "tp_rr": "TP_RR",
            "block_low_liquidity": "BLOCK_LOW_LIQUIDITY",
        }
        for attr, env_key in env_overrides.items():
            v = os.environ.get(env_key)
            if v is not None and hasattr(params, attr):
                cur = getattr(params, attr)
                try:
                    if isinstance(cur, bool):
                        new = v.lower() in ("1", "true", "yes", "on")
                    else:
                        new = type(cur)(v)
                    setattr(params, attr, new)
                except (TypeError, ValueError):
                    pass
        self.strategy = StrategyCls(params)

        # ログファイルパスを銘柄ごとに解決して、session_start レコードを書き出す。
        from datetime import datetime, timezone
        started_at = datetime.now(timezone.utc)
        self.log_path = resolve_log_path(symbol, session_id, started_at=started_at)
        try:
            model_name = getattr(getattr(self.strategy, "ai_model", None), "name", "")
        except Exception:  # noqa: BLE001
            model_name = ""
        try:
            write_session_start(
                self.log_path,
                session_id=session_id,
                symbol=symbol,
                strategy=strategy_name,
                model_name=model_name,
                params=dict(params_overrides or {}),
                extra={
                    "started_at": started_at.isoformat(),
                },
            )
        except Exception:  # noqa: BLE001
            pass

        # AI 戦略がログ蓄積に使う識別情報を伝える (持っていれば)
        if hasattr(self.strategy, "session_id"):
            self.strategy.session_id = session_id
        if hasattr(self.strategy, "symbol"):
            self.strategy.symbol = symbol
        if hasattr(self.strategy, "log_path"):
            self.strategy.log_path = self.log_path

        # outcome tracker を立てて戦略に渡す
        self.outcome_tracker = OutcomeTracker(
            session_id=session_id,
            symbol=symbol,
            strategy_name=strategy_name,
            log_path=self.log_path,
        )
        if hasattr(self.strategy, "outcome_tracker"):
            self.strategy.outcome_tracker = self.outcome_tracker

        # ポジション遷移検出のための前回 tick での状態
        self.previous_position = None

        self.ctx = RemoteContext()

        # 描画 dedup: 既に EA に送ったピボット数を tracker 別に追跡
        self.drawn_z1_m15 = 0
        self.drawn_z2_m15 = 0
        self.drawn_z1_h1 = 0
        self.drawn_z1_h4 = 0
        self.entry_counter = 0

        # MTF dedup (同じ MTF バーを 2 回 update に渡さないため)
        self.last_h1_time = -1
        self.last_h4_time = -1

    # ---- tick 処理 ----
    def process_tick(
        self,
        m15_bar: Bar,
        h1_bar: Optional[Bar],
        h4_bar: Optional[Bar],
        position: Optional[str],
        balance: float,
    ) -> dict[str, Any]:
        """EA からの 1 ティックを処理して、返すべきメッセージを組む。"""
        # 状態を ctx に反映
        self.ctx.current_position = position
        self.ctx.current_balance = balance

        # 履歴に新バーを追加
        self.ctx.bars_seq.append(m15_bar)
        # MTF (新しい時間のものだけ追加)
        if h1_bar is not None and h1_bar.time > self.last_h1_time:
            self.ctx.mtf_bars.setdefault(3600, []).append(h1_bar)
            self.last_h1_time = h1_bar.time
        if h4_bar is not None and h4_bar.time > self.last_h4_time:
            self.ctx.mtf_bars.setdefault(14400, []).append(h4_bar)
            self.last_h4_time = h4_bar.time

        # buffer リセットして戦略実行
        self.ctx.pending_commands = []
        self.ctx.pending_draws = []
        self.ctx.pending_logs = []
        n_commands_before = 0  # 描画は戦略実行後に新規ピボット差分から作る

        try:
            self.strategy.on_bar(self.ctx)
        except Exception as e:  # noqa: BLE001
            self.ctx.pending_logs.append(f"[server-error] on_bar: {e}")

        # 新規ピボットを描画指示に変換
        self._emit_new_pivot_draws()

        # === ポジション遷移検出 (long/short → 別状態) ===
        # EA から届いた position と、前 tick で覚えていた position を比較。
        # 何かが「閉じた」と判断できたら outcome_tracker.on_position_close を呼ぶ。
        if self.previous_position in ("long", "short") and position != self.previous_position:
            # 同 tick で strategy が ctx.close() を呼んでいたら "strategy_close" (= reversal)
            # それ以外は "tp_or_sl" (TP/SL の到達による自動決済)
            had_strategy_close = any(
                c["type"] == "close" for c in self.ctx.pending_commands[n_commands_before:]
            )
            exit_reason = "strategy_close" if had_strategy_close else "tp_or_sl"
            try:
                self.outcome_tracker.on_position_close(
                    side=self.previous_position,
                    exit_price=m15_bar.close,
                    exit_bar_time=m15_bar.time,
                    exit_bar_count=getattr(self.strategy, "_bar_idx", 0),
                    exit_reason=exit_reason,
                )
            except Exception:  # noqa: BLE001
                pass
        self.previous_position = position

        # エントリ系コマンドが出ていたら描画指示も入れる + outcome_tracker に pending 登録
        for cmd in self.ctx.pending_commands[n_commands_before:]:
            if cmd["type"] in ("buy", "sell"):
                self.entry_counter += 1
                self.ctx.draw_text(
                    name=f"{self.session_id}_mark_{self.entry_counter}_{cmd['type']}",
                    time_unix=m15_bar.time,
                    price=m15_bar.close,
                    label="BUY" if cmd["type"] == "buy" else "SELL",
                    color=ENTRY_BUY_COLOR if cmd["type"] == "buy" else ENTRY_SELL_COLOR,
                    font_size=14,
                )
                # AI が紐付けようとしている decision があれば pending として登録
                last_ai = getattr(self.strategy, "_last_ai_decision", None)
                if last_ai and last_ai.get("direction"):
                    try:
                        self.outcome_tracker.register_entry(
                            decision_id=last_ai["decision_id"],
                            direction=last_ai["direction"],
                            entry_price=m15_bar.close,
                            entry_bar_time=m15_bar.time,
                            entry_bar_count=getattr(self.strategy, "_bar_idx", 0),
                            lot=float(cmd.get("volume") or 0.0),
                            sl=cmd.get("sl"),
                            tp=cmd.get("tp"),
                            features=last_ai.get("features") or {},
                            decision=last_ai.get("decision") or {},
                        )
                        self.strategy._last_ai_decision = None
                    except Exception:  # noqa: BLE001
                        pass
            elif cmd["type"] == "close":
                self.entry_counter += 1
                self.ctx.draw_text(
                    name=f"{self.session_id}_mark_{self.entry_counter}_close",
                    time_unix=m15_bar.time,
                    price=m15_bar.close,
                    label="CLOSE",
                    color="Yellow",
                    font_size=14,
                )

        return {
            "type": "commands",
            "session_id": self.session_id,
            "bar_time": m15_bar.time,
            "commands": self.ctx.pending_commands,
            "draw": self.ctx.pending_draws,
            "logs": self.ctx.pending_logs,
        }

    def _emit_new_pivot_draws(self) -> None:
        """各 ZigZag tracker で「まだ送ってないピボット」を draw に追加。

        色とサイズは DRAW_STYLES (ファイル上部) で集中管理。
        他のロジック (zigzag を使わない戦略) では該当属性が無いので getattr で
        None 安全に。
        """
        # トラッカ属性名 → DRAW_STYLES のキー
        tracker_to_style = [
            ("z1",    "z1_m15"),
            ("z2",    "z2_m15"),
            ("z1_h1", "z1_h1"),
            ("z1_h4", "z1_h4"),
        ]
        for attr_name, style_key in tracker_to_style:
            tracker = getattr(self.strategy, attr_name, None)
            style = DRAW_STYLES.get(style_key)
            if tracker is None or style is None:
                continue
            self._emit_for_tracker(
                tracker=tracker,
                tag=style["tag"],
                drawn_attr=style["drawn_attr"],
                color_high=style["color_high"],
                color_low=style["color_low"],
                font_size=style["font_size"],
            )

    def _emit_for_tracker(
        self,
        tracker: Any,
        tag: str,
        drawn_attr: str,
        color_high: str,
        color_low: str,
        font_size: int,
    ) -> None:
        if tracker is None:
            return
        current = getattr(self, drawn_attr)
        total = len(tracker.pivots)
        # 同名オブジェクトが過去 run から残っていて CreateChartObject が false を
        # 返す問題を避けるため、session_id をプレフィックスして名前を一意化する。
        name_prefix = f"{self.session_id}_{tag}"
        for idx in range(current, total):
            p = tracker.pivots[idx]
            # tracker.bars はベースの bar list (Bar dataclass)。pivot.index で参照可能。
            try:
                bar_time = tracker.bars[p.index].time
            except Exception:
                bar_time = p.time
            self.ctx.draw_text(
                name=f"{name_prefix}_{idx}_{p.kind}",
                time_unix=bar_time,
                price=p.price,
                label="▼" if p.kind == "high" else "▲",
                color=color_high if p.kind == "high" else color_low,
                font_size=font_size,
            )
        setattr(self, drawn_attr, total)
