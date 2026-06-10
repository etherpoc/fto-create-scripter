"""
mtf_pullback.py — マルチTF押し目戦略 (strategies/mtf_pullback) をレジストリへ登録。
"""

from __future__ import annotations

from server.deciders.registry import register
from strategies.mtf_pullback.strategy import (
    Params as _Params,
    MtfPullbackStrategy as _MtfPullbackStrategy,
)


_MtfPullbackStrategy.PARAMS_CLS = _Params

register("mtf_pullback")(_MtfPullbackStrategy)
