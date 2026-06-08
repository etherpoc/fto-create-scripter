"""
remote_context.py — 既存 src/core/strategy_base.Context を実装し、
コマンドを「実行せず buffer に積む」サーバ側実装。

これにより、`strategies/zigzag_line_break/strategy.py` の Strategy.on_bar(ctx) を
**コードに一切手を加えずに** サーバで動かすことができる。
on_bar が ctx.buy / ctx.sell / ctx.close / ctx.log を呼ぶたびに、それらは
pending_commands に詰まれる。サーバはセッション処理後にそれを EA へ返す。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.strategy_base import Bar, Context  # noqa: E402


@dataclass
class RemoteContext(Context):
    """サーバ側 Context。命令を実行せず辞書として buffer に積む。"""

    bars_seq: list[Bar] = field(default_factory=list)
    mtf_bars: dict[int, list[Bar]] = field(default_factory=dict)  # period_sec -> bars
    current_position: Optional[str] = None
    current_balance: float = 0.0

    pending_commands: list[dict[str, Any]] = field(default_factory=list)
    pending_draws: list[dict[str, Any]] = field(default_factory=list)
    pending_logs: list[str] = field(default_factory=list)

    # ---- Context インタフェース実装 ----
    def price(self) -> float:
        if not self.bars_seq:
            raise RuntimeError("no bars")
        return self.bars_seq[-1].close

    def bars(self, n: int) -> list[Bar]:
        if n <= 0:
            return []
        return self.bars_seq[-n:]

    def bars_mtf(self, period_seconds: int, n: int) -> list[Bar]:
        arr = self.mtf_bars.get(period_seconds, [])
        if n <= 0:
            return []
        return arr[-n:]

    def position(self) -> Optional[str]:
        return self.current_position

    def account_balance(self) -> float:
        return self.current_balance

    def buy(self, volume: float, sl: Optional[float] = None, tp: Optional[float] = None) -> None:
        self.pending_commands.append(
            {"type": "buy", "volume": volume, "sl": sl, "tp": tp}
        )

    def sell(self, volume: float, sl: Optional[float] = None, tp: Optional[float] = None) -> None:
        self.pending_commands.append(
            {"type": "sell", "volume": volume, "sl": sl, "tp": tp}
        )

    def close(self) -> None:
        self.pending_commands.append({"type": "close"})

    def log(self, msg: str) -> None:
        self.pending_logs.append(msg)

    # ---- 描画ヘルパ (Context I/F の拡張、EA に描画指示を渡すため) ----
    def draw_text(
        self,
        name: str,
        time_unix: int,
        price: float,
        label: str,
        color: str = "White",
        font_size: int = 12,
    ) -> None:
        self.pending_draws.append(
            {
                "type": "text",
                "name": name,
                "time": time_unix,
                "price": price,
                "label": label,
                "color": color,
                "font_size": font_size,
            }
        )
