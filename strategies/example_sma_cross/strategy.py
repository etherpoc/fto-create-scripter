"""
example_sma_cross/strategy.py — 写経の手本。

判断ロジックは ctx 経由・指標は indicators の純粋関数のみを使い、
FTO 固有の関数名は一切書かない。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.indicators import atr, crossover, crossunder, sma
from src.core.strategy_base import Context, Strategy, StrategyParams


@dataclass
class Params(StrategyParams):
    fast: int = 20
    slow: int = 50
    atr_period: int = 14
    sl_atr: float = 2.0
    tp_atr: float = 3.0


class SmaCrossStrategy(Strategy):
    def __init__(self, params: Params) -> None:
        super().__init__(params)
        self.p: Params = params

    def on_bar(self, ctx: Context) -> None:
        p = self.p
        need = max(p.slow, p.atr_period) + 2
        bars = ctx.bars(need)
        if len(bars) < need:
            return

        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]

        fast_line = sma(closes, p.fast)
        slow_line = sma(closes, p.slow)
        atr_line = atr(highs, lows, closes, p.atr_period)

        i = len(closes) - 1
        a = atr_line[i]
        if a is None:
            return

        price = ctx.price()
        pos = ctx.position()

        if crossover(fast_line, slow_line, i):
            if pos == "short":
                ctx.close()
            sl = price - p.sl_atr * a
            tp = price + p.tp_atr * a
            ctx.buy(p.volume, sl=sl, tp=tp)
            ctx.log(f"BUY t={bars[-1].time} price={price:.5f} sl={sl:.5f} tp={tp:.5f}")
        elif crossunder(fast_line, slow_line, i):
            if pos == "long":
                ctx.close()
            sl = price + p.sl_atr * a
            tp = price - p.tp_atr * a
            ctx.sell(p.volume, sl=sl, tp=tp)
            ctx.log(f"SELL t={bars[-1].time} price={price:.5f} sl={sl:.5f} tp={tp:.5f}")
