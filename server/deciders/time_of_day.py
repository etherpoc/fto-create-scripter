"""
time_of_day.py — 時間帯戦略 (strategies/time_of_day/strategy.py) をレジストリへ登録。
"""

from __future__ import annotations

from server.deciders.registry import register
from strategies.time_of_day.strategy import (
    Params as _Params,
    TimeOfDayStrategy as _TimeOfDayStrategy,
)


_TimeOfDayStrategy.PARAMS_CLS = _Params

register("time_of_day")(_TimeOfDayStrategy)
