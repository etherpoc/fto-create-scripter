"""
time_of_day/strategy.py — 時間帯バイアスを使ったマルチペア戦略。

edge_scanner.py で発見された 「両期間 ROBUST な時間帯エッジ」 を使う:

  EURUSD/GBPUSD/XAUUSD: 21:00 UTC で BUY、22:00 UTC で CLOSE
    - WR 57-63%、平均 +0.15〜+0.59 ATR の上昇バイアス
  USDJPY: 20:00 UTC で SELL、21:00 UTC で CLOSE
    - WR 33-43% (= 57-67% で売り勝ち)、平均 -0.23〜-0.37 ATR の下落バイアス

これらは「過去 5.5 年で両 IS/OOS で有意に再現」確認済み。
NY 16:00 close 周辺の機関投資家の rebalance flow が原因と推測。

リスク管理:
  - 1% リスク/トレード (既存戦略と同じ)
  - SL = 0.5 ATR (近め、回転速い)
  - TP = 1.0 ATR (= 2:1 RR)
  - 退場: TP/SL ヒット or exit_hour 到達 (= 必ず短時間で抜ける)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.core.indicators import atr
from src.core.strategy_base import Bar, Context, Strategy, StrategyParams


# run_backtest が読む宣言
SAMPLE_PERIOD_SECONDS = 900  # M15
DEFAULT_CSV = "sample_M15.csv"
INITIAL_BALANCE = 10_000.0


@dataclass
class Params(StrategyParams):
    # リスク
    risk_pct: float = 0.01
    # ATR
    atr_period: int = 14
    # SL / TP (ATR 倍率)
    sl_atr_mult: float = 0.5
    tp_atr_mult: float = 1.0
    # 換算 (FTO 側でも同じ)
    pip_size: float = 0.0001
    pip_value: float = 10.0
    # 強制決済までの最大保有バー数 (= 4 M15 bars = 1h)
    # exit_hour で本来抜けるが、想定外の時刻ズレなどに備える保険
    max_hold_bars: int = 8


# シンボル別ルール: (entry_hour_utc, direction, exit_hour_utc)
# edge_scanner.py の結果から選定 (両期間 ROBUST かつ |mean| >= 0.15 ATR のみ)
SYMBOL_RULES: dict[str, list[tuple[int, str, int]]] = {
    "EURUSD": [(21, "buy", 22)],
    "GBPUSD": [(21, "buy", 22)],
    "XAUUSD": [(21, "buy", 22)],
    "USDJPY": [(20, "sell", 21)],
}


class TimeOfDayStrategy(Strategy):
    """時間帯エッジに基づく短期戦略。"""

    def __init__(self, params: Params) -> None:
        super().__init__(params)
        self.p: Params = params
        # session.py から差し込まれる
        self.symbol: str = "UNKNOWN"
        # ポジション状態
        self._exit_hour: Optional[int] = None
        self._hold_bars: int = 0

    def on_bar(self, ctx: Context) -> None:
        p = self.p
        bars = ctx.bars(p.atr_period + 2)
        if len(bars) < p.atr_period + 1:
            return
        cur = bars[-1]
        try:
            dt = datetime.fromtimestamp(cur.time, tz=timezone.utc)
        except Exception:  # noqa: BLE001
            return
        hour = dt.hour
        minute = dt.minute
        pos = ctx.position()

        # === 退場ロジック (ポジ保有中) ===
        if pos is not None:
            self._hold_bars += 1
            # 1. exit_hour に到達したら強制クローズ
            if self._exit_hour is not None and hour == self._exit_hour and minute == 0:
                ctx.log(f"[exit-time] {self.symbol} closed at {hour:02d}:{minute:02d}")
                ctx.close()
                self._exit_hour = None
                self._hold_bars = 0
                return
            # 2. 保険: max_hold_bars を超えたら強制クローズ
            if self._hold_bars >= p.max_hold_bars:
                ctx.log(f"[exit-maxhold] {self.symbol} closed at {hour:02d}:{minute:02d}")
                ctx.close()
                self._exit_hour = None
                self._hold_bars = 0
                return
            # まだホールド中
            return

        # === 新規エントリー判定 (ポジ無し) ===
        sym = (self.symbol or "").upper()
        rules = SYMBOL_RULES.get(sym, [])
        if not rules:
            return  # 対象外シンボル

        for entry_hour, direction, exit_hour in rules:
            # 該当時刻の最初の M15 バーでだけエントリー (= minute == 0)
            if hour != entry_hour or minute != 0:
                continue

            # ATR 計算
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            closes = [b.close for b in bars]
            atr_line = atr(highs, lows, closes, p.atr_period)
            atr_val = atr_line[-1]
            if atr_val is None or atr_val <= 0:
                continue

            price = cur.close
            sl_dist = p.sl_atr_mult * atr_val
            tp_dist = p.tp_atr_mult * atr_val
            if sl_dist <= 0:
                continue

            vol = self._risk_lot(ctx, sl_dist)
            if vol <= 0:
                continue

            if direction == "buy":
                sl = price - sl_dist
                tp = price + tp_dist
                ctx.buy(vol, sl=sl, tp=tp)
                self._attach_metadata(ctx, price, sl_dist)
                ctx.log(
                    f"[entry-tod] {sym} BUY at {hour:02d}:00 "
                    f"price={price:.5f} sl={sl:.5f} tp={tp:.5f} vol={vol:.3f}"
                )
            elif direction == "sell":
                sl = price + sl_dist
                tp = price - tp_dist
                ctx.sell(vol, sl=sl, tp=tp)
                self._attach_metadata(ctx, price, sl_dist)
                ctx.log(
                    f"[entry-tod] {sym} SELL at {hour:02d}:00 "
                    f"price={price:.5f} sl={sl:.5f} tp={tp:.5f} vol={vol:.3f}"
                )
            else:
                continue

            self._exit_hour = exit_hour
            self._hold_bars = 0
            break

    def _risk_lot(self, ctx: Context, sl_dist: float) -> float:
        balance = ctx.account_balance()
        if balance <= 0:
            return 0.0
        risk_amount = balance * self.p.risk_pct
        pips_at_risk = sl_dist / self.p.pip_size
        money_per_lot = pips_at_risk * self.p.pip_value
        if money_per_lot <= 0:
            return 0.0
        return risk_amount / money_per_lot

    def _attach_metadata(self, ctx: Context, price: float, sl_dist: float) -> None:
        """trailing close 等を使わないがメタ情報は EA 用に付与しておく。"""
        if hasattr(ctx, "pending_commands") and ctx.pending_commands:
            cmd = ctx.pending_commands[-1]
            cmd["entry_price"] = float(price)
            cmd["sl_dist"] = float(sl_dist)
            # 時間帯戦略では trailing 不要 (短時間 hold)
            cmd["trail_activate_R"] = 0.0
            cmd["trail_stop_R"] = 0.0
