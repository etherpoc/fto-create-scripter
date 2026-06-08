"""
engine.py — 標準ライブラリのみで動くローカル検証エンジン。

CSV 列: time, open, high, low, close[, volume]
  time は UNIX 秒 (int / float) または ISO8601 文字列。

各足について「先に既存ポジの SL/TP 判定 → strategy.on_bar」を行い、
最終足で残ポジを強制クローズする。
SL/TP は当該足の high/low で約定判定（保守的に SL 優先）。
同方向の追加発注は無視、逆方向はドテン（既存決済 → 反対側で新規）。

損益は pip ベース:
    pnl = 値幅 / pip_size * pip_value * volume
既定: pip_size = 0.0001, pip_value = 10
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.core.strategy_base import Bar, Context, Strategy


# -----------------------------------------------------------------------------
# CSV 読み込み
# -----------------------------------------------------------------------------

def _parse_time(s: str) -> int:
    """UNIX 秒 or ISO 文字列を UNIX 秒 (int) に。"""
    s = s.strip()
    try:
        # 数値の場合
        return int(float(s))
    except ValueError:
        pass
    # ISO 文字列
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def load_csv(path: str) -> list[Bar]:
    bars: list[Bar] = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = _parse_time(row["time"])
            vol_raw = row.get("volume")
            vol = float(vol_raw) if vol_raw not in (None, "") else 0.0
            bars.append(
                Bar(
                    time=t,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=vol,
                )
            )
    bars.sort(key=lambda b: b.time)
    return bars


# -----------------------------------------------------------------------------
# トレード記録
# -----------------------------------------------------------------------------

@dataclass
class Trade:
    direction: str  # "long" or "short"
    entry_time: int
    entry_price: float
    exit_time: Optional[int] = None
    exit_price: Optional[float] = None
    volume: float = 1.0
    sl: Optional[float] = None
    tp: Optional[float] = None
    pnl: float = 0.0
    reason: str = ""  # "sl" / "tp" / "reverse" / "close" / "eod"


# -----------------------------------------------------------------------------
# Context 実装
# -----------------------------------------------------------------------------

@dataclass
class _BacktestContext(Context):
    _bars: list[Bar] = field(default_factory=list)  # 確定済みバー（古→新）
    _open_trade: Optional[Trade] = None
    _pending_orders: list[dict] = field(default_factory=list)
    _logs: list[str] = field(default_factory=list)
    _mtf_all: dict[int, list[Bar]] = field(default_factory=dict)
    _base_period: int = 0  # ベース足の周期 [秒]。0 なら MTF 不可
    _balance: float = 0.0

    def price(self) -> float:
        if not self._bars:
            raise RuntimeError("no bars yet")
        return self._bars[-1].close

    def bars(self, n: int) -> list[Bar]:
        if n <= 0:
            return []
        return self._bars[-n:]

    def position(self) -> Optional[str]:
        return self._open_trade.direction if self._open_trade else None

    def bars_mtf(self, period_seconds: int, n: int) -> list[Bar]:
        if not self._bars or self._base_period == 0:
            raise NotImplementedError("MTF not configured on this engine")
        if period_seconds not in self._mtf_all:
            raise KeyError(
                f"MTF period {period_seconds}s not pre-aggregated; "
                f"add it to EngineConfig.mtf_periods"
            )
        # 現在処理中の確定足の終端時刻
        t_end = self._bars[-1].time + self._base_period
        # 上位足 Bar が確定している条件: その終端 (time + period) <= 現足終端
        candidates = [
            b for b in self._mtf_all[period_seconds]
            if b.time + period_seconds <= t_end
        ]
        if n <= 0:
            return []
        return candidates[-n:]

    def account_balance(self) -> float:
        return self._balance

    def buy(
        self,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> None:
        self._pending_orders.append(
            {"side": "long", "volume": volume, "sl": sl, "tp": tp}
        )

    def sell(
        self,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> None:
        self._pending_orders.append(
            {"side": "short", "volume": volume, "sl": sl, "tp": tp}
        )

    def close(self) -> None:
        self._pending_orders.append({"side": "close"})

    def log(self, msg: str) -> None:
        self._logs.append(msg)


# -----------------------------------------------------------------------------
# Engine
# -----------------------------------------------------------------------------

@dataclass
class EngineConfig:
    pip_size: float = 0.0001
    pip_value: float = 10.0
    initial_balance: float = 10_000.0


def aggregate_bars(bars: list[Bar], period_seconds: int) -> list[Bar]:
    """ベース足を上位 TF にエポック整列で集約。

    各上位足の開始時刻は `floor(time / period_seconds) * period_seconds`。
    出力 Bar の `time` は **その上位足の開始時刻**（= 終端は time + period_seconds）。
    """
    if not bars:
        return []
    out: list[Bar] = []
    bucket_start: int = -1
    o = h = l_ = c = 0.0
    vol = 0.0
    for b in bars:
        bs = (b.time // period_seconds) * period_seconds
        if bs != bucket_start:
            if bucket_start >= 0:
                out.append(Bar(bucket_start, o, h, l_, c, vol))
            bucket_start = bs
            o = b.open
            h = b.high
            l_ = b.low
            c = b.close
            vol = b.volume
        else:
            h = max(h, b.high)
            l_ = min(l_, b.low)
            c = b.close
            vol += b.volume
    if bucket_start >= 0:
        out.append(Bar(bucket_start, o, h, l_, c, vol))
    return out


class Engine:
    def __init__(
        self,
        bars: list[Bar],
        config: Optional[EngineConfig] = None,
        mtf_periods: Optional[list[int]] = None,
    ) -> None:
        self.bars = bars
        self.config = config or EngineConfig()
        self.ctx = _BacktestContext()
        self.trades: list[Trade] = []
        self.ctx._balance = self.config.initial_balance
        # ベース足の周期を推定（2 本目 - 1 本目）。バー数が 1 以下なら 0 のまま。
        if len(bars) >= 2:
            self.ctx._base_period = bars[1].time - bars[0].time
        # 上位足を事前集約
        for p in mtf_periods or []:
            self.ctx._mtf_all[p] = aggregate_bars(bars, p)

    # ---- pnl 計算 ----
    def _pnl(self, trade: Trade, exit_price: float) -> float:
        diff = exit_price - trade.entry_price
        if trade.direction == "short":
            diff = -diff
        return diff / self.config.pip_size * self.config.pip_value * trade.volume

    def _close_trade(
        self, exit_time: int, exit_price: float, reason: str
    ) -> None:
        t = self.ctx._open_trade
        if t is None:
            return
        t.exit_time = exit_time
        t.exit_price = exit_price
        t.pnl = self._pnl(t, exit_price)
        t.reason = reason
        self.trades.append(t)
        self.ctx._balance += t.pnl
        self.ctx._open_trade = None

    def _check_sl_tp(self, bar: Bar) -> None:
        """この足の high/low で SL/TP に当たっていれば決済する（SL 優先）。"""
        t = self.ctx._open_trade
        if t is None:
            return
        hit_sl = False
        hit_tp = False
        if t.direction == "long":
            if t.sl is not None and bar.low <= t.sl:
                hit_sl = True
            if t.tp is not None and bar.high >= t.tp:
                hit_tp = True
        else:  # short
            if t.sl is not None and bar.high >= t.sl:
                hit_sl = True
            if t.tp is not None and bar.low <= t.tp:
                hit_tp = True
        if hit_sl:
            self._close_trade(bar.time, t.sl, "sl")
        elif hit_tp:
            self._close_trade(bar.time, t.tp, "tp")

    def _apply_orders(self, bar: Bar) -> None:
        """on_bar の中で積まれた注文を、確定足の close で約定させる。"""
        orders = self.ctx._pending_orders
        self.ctx._pending_orders = []
        for o in orders:
            side = o["side"]
            if side == "close":
                if self.ctx._open_trade is not None:
                    self._close_trade(bar.time, bar.close, "close")
                continue
            # buy / sell
            cur = self.ctx._open_trade
            if cur is not None:
                if cur.direction == side:
                    # 同方向の追加発注は無視
                    continue
                # 逆方向 → ドテン
                self._close_trade(bar.time, bar.close, "reverse")
            self.ctx._open_trade = Trade(
                direction=side,
                entry_time=bar.time,
                entry_price=bar.close,
                volume=o["volume"],
                sl=o["sl"],
                tp=o["tp"],
            )

    # ---- メインループ ----
    def run(self, strategy: Strategy) -> None:
        for i, bar in enumerate(self.bars):
            # 1) 新しい足を確定として積む
            self.ctx._bars.append(bar)
            # 2) 先に既存ポジの SL/TP 判定
            self._check_sl_tp(bar)
            # 3) 戦略呼び出し
            try:
                strategy.on_bar(self.ctx)
            except Exception as e:  # noqa: BLE001
                self.ctx.log(f"on_bar error at {bar.time}: {e}")
                raise
            # 4) 注文の約定
            self._apply_orders(bar)
        # 最終足で残ポジは強制クローズ
        if self.ctx._open_trade is not None and self.bars:
            last = self.bars[-1]
            self._close_trade(last.time, last.close, "eod")

    # ---- 統計 ----
    def stats(self) -> dict:
        return compute_stats(self.trades)


def compute_stats(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "net_profit": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "avg_trade": 0.0,
        }
    wins = sum(1 for t in trades if t.pnl > 0)
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = sum(-t.pnl for t in trades if t.pnl < 0)
    net = gross_profit - gross_loss
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    # ドローダウン
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return {
        "trades": n,
        "win_rate": wins / n,
        "net_profit": net,
        "profit_factor": pf,
        "max_drawdown": max_dd,
        "avg_trade": net / n,
    }


def print_stats(stats: dict) -> None:
    print("=== Backtest stats ===")
    print(f"  trades       : {stats['trades']}")
    print(f"  win_rate     : {stats['win_rate'] * 100:.2f}%")
    print(f"  net_profit   : {stats['net_profit']:.2f}")
    pf = stats["profit_factor"]
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(f"  profit_factor: {pf_str}")
    print(f"  max_drawdown : {stats['max_drawdown']:.2f}")
    print(f"  avg_trade    : {stats['avg_trade']:.2f}")
