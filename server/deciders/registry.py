"""
registry.py — 戦略名 → (StrategyClass, ParamsClass) のレジストリ。

新しいロジックを追加するときの手順:
  1. server/deciders/<your_strategy>.py を作る
  2. その先頭で `from server.deciders.registry import register` し、`@register("name")`
     デコレータでクラスを登録する
  3. server/deciders/__init__.py の auto-import 行に追加する
     (あるいは __init__.py の動的 import に任せる)

EA 側からは init メッセージで `{"strategy": "name", ...}` を送るだけで、
レジストリから対応する Strategy / Params を解決する。
"""

from __future__ import annotations

from typing import Type

_REGISTRY: dict[str, tuple[Type, Type]] = {}


def register(name: str):
    """`@register("zigzag_line_break")` で Strategy クラスを登録するデコレータ。

    対応する Params クラスは、Strategy.__init__ が受け取る型から自動的に
    取り出せないので、戦略モジュール側で `params_class` を class 属性として
    持たせるか、別途 `register_params` を呼ぶ運用にする。
    ここではシンプルに「Strategy.PARAMS_CLS で参照する」運用を要求する。
    """

    def decorator(cls):
        params_cls = getattr(cls, "PARAMS_CLS", None)
        if params_cls is None:
            raise RuntimeError(
                f"Strategy {cls.__name__} must define a PARAMS_CLS class attribute "
                f"(e.g. `PARAMS_CLS = Params`)"
            )
        _REGISTRY[name] = (cls, params_cls)
        return cls

    return decorator


def get_strategy_entry(name: str) -> tuple[Type, Type] | None:
    return _REGISTRY.get(name)


def list_strategies() -> list[str]:
    return sorted(_REGISTRY.keys())
