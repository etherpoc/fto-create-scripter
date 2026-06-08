"""
strategy_base.py — プラットフォーム非依存の戦略フレームワーク。

ここで定義した Bar / Context / Strategy / StrategyParams は、
ローカル backtest エンジンも FTO アダプタも共通で使う。
**FTO 固有の関数名は絶対にここへ書かない。** 戦略のロジックは
すべて Context (ctx) 経由で行うことで、配下のプラットフォームを差し替えられる。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Bar:
    """OHLC バー一本。time は UNIX 秒（int）。"""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class StrategyParams:
    """戦略パラメータの基底。各戦略はこれを継承して項目を追加する。"""

    volume: float = 1.0


class Context(ABC):
    """戦略がデータ参照・発注・ポジション照会に使う唯一の窓口。

    具象実装は backtest エンジン側、または FTO アダプタ側で行う。
    """

    @abstractmethod
    def price(self) -> float:
        """直近の確定足の終値。"""

    @abstractmethod
    def bars(self, n: int) -> list[Bar]:
        """直近 n 本の確定 Bar を古→新の順で返す。"""

    @abstractmethod
    def position(self) -> Optional[str]:
        """現在のポジション方向。None / "long" / "short"。"""

    @abstractmethod
    def buy(
        self,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> None:
        """成行ロング。SL / TP は価格指定。"""

    @abstractmethod
    def sell(
        self,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> None:
        """成行ショート。SL / TP は価格指定。"""

    @abstractmethod
    def close(self) -> None:
        """現在のポジションを全決済。"""

    @abstractmethod
    def log(self, msg: str) -> None:
        """戦略からのログ出力。"""

    # --- 拡張オプション (実装はエンジン側次第。デフォルトは未提供) ---

    def bars_mtf(self, period_seconds: int, n: int) -> list["Bar"]:
        """直近 n 本の **確定** 上位足 Bar を古→新で返す。

        確定の意味: 上位足の終端時刻が、現在処理中の確定足の終端時刻以下である
        ことを言う（ルックアヘッド禁止）。提供しないエンジンでは
        NotImplementedError を投げる。
        """
        raise NotImplementedError("MTF data is not provided by this Context")

    def account_balance(self) -> float:
        """口座残高（既にクローズ済みの損益を加味）。

        提供しないエンジンでは無限大を返し、リスクベースのロット計算は
        実質「volume パラメータ」を使うフォールバックになる想定。
        """
        return float("inf")


class Strategy:
    """戦略の基底クラス。各戦略は on_bar をオーバライドする。"""

    def __init__(self, params: StrategyParams) -> None:
        self.params = params

    def on_bar(self, ctx: Context) -> None:
        """確定足ごとに 1 回呼ばれる。

        この中ではデータ参照・発注を **必ず** ctx 経由で行う。
        FTO 固有の関数名を書いてはならない。
        """
        raise NotImplementedError
