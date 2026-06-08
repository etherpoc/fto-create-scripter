"""
zigzag_line_break.py — 既存の strategies/zigzag_line_break/strategy.py を
レジストリへ登録するだけのアダプタ。

ロジックそのものは strategies/zigzag_line_break/strategy.py のまま (= ローカル
backtest と同じコードが走る)。ここではクラスに `PARAMS_CLS` 属性を生やして
registry に乗せる薄いラップだけを行う。
"""

from __future__ import annotations

from server.deciders.registry import register
from strategies.zigzag_line_break.strategy import (
    Params as _Params,
    ZigZagLineBreakStrategy as _ZigZagLineBreakStrategy,
)


# Python 戦略クラスにレジストリ用の属性を後付けする
_ZigZagLineBreakStrategy.PARAMS_CLS = _Params

# レジストリ登録
register("zigzag_line_break")(_ZigZagLineBreakStrategy)
