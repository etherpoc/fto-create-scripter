"""
indicators.py — 純粋関数の指標群。

すべての関数は次の規約に従う:
- 入力は **古い→新しい** 順のリスト。
- 戻り値は入力と同じ長さのリスト。値が定義されない先頭部分は None で埋める。
- None 安全（クロス判定など、片方が None ならイベントなしと扱う）。
- ルックアヘッドを作らない。`pivot_high` / `pivot_low` は中心から right 本進んだ
  index に値を入れる（リアルタイム時点では right 本後でなければ確定しない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from src.core.strategy_base import Bar

Number = float
NumList = list[Optional[Number]]


@dataclass
class Pivot:
    """ZigZag が検出するピボット 1 個。

    - `index`: トラッカに食わせたバー列上の位置（0 始まり）
    - `time`: そのバーの time
    - `kind`: "high" or "low"
    - `price`: ピボット価格
    - `confirmed_at`: トラッカ上で「確定した瞬間」のバー index
      （= index + depth、未来から見て depth 本先で確定）
    """

    index: int
    time: int
    kind: str
    price: float
    confirmed_at: int


class ZigZagTracker:
    """簡易 MT4 風 ZigZag のインクリメンタル実装。

    - `depth`: ピボット候補の左右に最低この本数を要求する（ローカル極値判定）。
    - `deviation`: **価格単位の最小スイング幅**。直前と逆方向の新ピボットは、
      直前ピボットから少なくとも deviation 離れていないと採用しない。
      pip 単位で渡したい場合は呼び出し側で `pip_size * pips` に変換してから渡す。

    使い方:
        zz = ZigZagTracker(depth=25, deviation=0.0005)
        for bar in bars_oldest_to_newest:
            zz.update(bar)
        # zz.pivots は「現在までに確定したピボット列」
    """

    def __init__(self, depth: int, deviation: float = 0.0) -> None:
        if depth <= 0:
            raise ValueError("depth must be > 0")
        self.depth = depth
        self.deviation = deviation
        self.bars: list[Bar] = []
        self.pivots: list[Pivot] = []

    def update(self, bar: Bar) -> None:
        self.bars.append(bar)
        idx = len(self.bars) - 1 - self.depth
        if idx < self.depth:
            return
        candidate = self.bars[idx]
        is_high = True
        is_low = True
        for j in range(idx - self.depth, idx + self.depth + 1):
            if j == idx:
                continue
            other = self.bars[j]
            if other.high >= candidate.high:
                is_high = False
            if other.low <= candidate.low:
                is_low = False
            if not is_high and not is_low:
                break
        if not is_high and not is_low:
            return
        confirmed_at = len(self.bars) - 1

        # 両方成立 (横ばい高値=安値) の場合は直前ピボットの逆方向に振る
        if is_high and is_low:
            new_kind = "low" if (self.pivots and self.pivots[-1].kind == "high") else "high"
        else:
            new_kind = "high" if is_high else "low"
        price = candidate.high if new_kind == "high" else candidate.low

        if not self.pivots:
            self.pivots.append(
                Pivot(idx, candidate.time, new_kind, price, confirmed_at)
            )
            return

        last = self.pivots[-1]
        if new_kind == last.kind:
            # 同方向: 既存ピボットを更新するのは「より極端」になったときだけ
            if (new_kind == "high" and price > last.price) or (
                new_kind == "low" and price < last.price
            ):
                self.pivots[-1] = Pivot(
                    idx, candidate.time, new_kind, price, confirmed_at
                )
            return

        # 逆方向: deviation 未満なら無視
        if abs(price - last.price) < self.deviation:
            return
        self.pivots.append(
            Pivot(idx, candidate.time, new_kind, price, confirmed_at)
        )


def sma(values: Sequence[Optional[Number]], period: int) -> NumList:
    """単純移動平均。先頭 period-1 本は None。"""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    out: NumList = [None] * n
    window_sum = 0.0
    count = 0
    for i in range(n):
        v = values[i]
        if v is None:
            # None が入っていたらウィンドウをリセットしておく方が安全だが、
            # 通常 OHLC では起きない。ここでは「その点は None で進む」扱い。
            out[i] = None
            continue
        window_sum += v
        count += 1
        if i >= period:
            old = values[i - period]
            if old is None:
                # period 前が None だったケース。素直に再計算。
                window_sum = sum(
                    x for x in values[i - period + 1 : i + 1] if x is not None
                )
                count = sum(
                    1 for x in values[i - period + 1 : i + 1] if x is not None
                )
            else:
                window_sum -= old
                count -= 1
        if count == period:
            out[i] = window_sum / period
    return out


def ema(values: Sequence[Optional[Number]], period: int) -> NumList:
    """指数移動平均。最初の有効値は period 個の SMA をシードに使う。"""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    out: NumList = [None] * n
    k = 2.0 / (period + 1)
    # シード探し
    seed_idx = None
    seed_sum = 0.0
    have = 0
    for i in range(n):
        v = values[i]
        if v is None:
            continue
        seed_sum += v
        have += 1
        if have == period:
            seed_idx = i
            out[i] = seed_sum / period
            break
    if seed_idx is None:
        return out
    prev = out[seed_idx]
    for i in range(seed_idx + 1, n):
        v = values[i]
        if v is None or prev is None:
            out[i] = prev
            continue
        cur = (v - prev) * k + prev
        out[i] = cur
        prev = cur
    return out


def true_range(
    highs: Sequence[Optional[Number]],
    lows: Sequence[Optional[Number]],
    closes: Sequence[Optional[Number]],
) -> NumList:
    """True Range。先頭 1 本は None（前足が無いため）。"""
    n = len(highs)
    if len(lows) != n or len(closes) != n:
        raise ValueError("inputs must have same length")
    out: NumList = [None] * n
    for i in range(n):
        if i == 0:
            out[i] = None
            continue
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if h is None or l is None or pc is None:
            out[i] = None
            continue
        out[i] = max(h - l, abs(h - pc), abs(l - pc))
    return out


def atr(
    highs: Sequence[Optional[Number]],
    lows: Sequence[Optional[Number]],
    closes: Sequence[Optional[Number]],
    period: int,
) -> NumList:
    """ATR。シンプルに TR の SMA を採用。"""
    tr = true_range(highs, lows, closes)
    return sma(tr, period)


def rsi(closes: Sequence[Optional[Number]], period: int) -> NumList:
    """Wilder の RSI。先頭 period 本は None。"""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(closes)
    out: NumList = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        c, p = closes[i], closes[i - 1]
        if c is None or p is None:
            return out
        diff = c - p
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, n):
        c, p = closes[i], closes[i - 1]
        if c is None or p is None:
            out[i] = out[i - 1]
            continue
        diff = c - p
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def pivot_high(
    highs: Sequence[Optional[Number]], left: int, right: int
) -> NumList:
    """ピボットハイ。中心から right 本進んだ index に値を入れる（先読み回避）。

    つまり戻り値 out[i] は「i - right 番目の足を中心としたピボット」を表す。
    """
    if left < 0 or right < 0:
        raise ValueError("left/right must be >= 0")
    n = len(highs)
    out: NumList = [None] * n
    for center in range(left, n - right):
        c = highs[center]
        if c is None:
            continue
        ok = True
        for j in range(center - left, center + right + 1):
            if j == center:
                continue
            v = highs[j]
            if v is None or v >= c:
                ok = False
                break
        if ok:
            out[center + right] = c
    return out


def pivot_low(
    lows: Sequence[Optional[Number]], left: int, right: int
) -> NumList:
    """ピボットロー。同じく right 本進んだ index に入れる。"""
    if left < 0 or right < 0:
        raise ValueError("left/right must be >= 0")
    n = len(lows)
    out: NumList = [None] * n
    for center in range(left, n - right):
        c = lows[center]
        if c is None:
            continue
        ok = True
        for j in range(center - left, center + right + 1):
            if j == center:
                continue
            v = lows[j]
            if v is None or v <= c:
                ok = False
                break
        if ok:
            out[center + right] = c
    return out


def crossover(
    a: Sequence[Optional[Number]],
    b: Sequence[Optional[Number]],
    i: int,
) -> bool:
    """i 本目で a が b を上抜けしたか。None 安全。"""
    if i <= 0 or i >= len(a) or i >= len(b):
        return False
    a1, a0 = a[i], a[i - 1]
    b1, b0 = b[i], b[i - 1]
    if a1 is None or a0 is None or b1 is None or b0 is None:
        return False
    return a0 <= b0 and a1 > b1


def crossunder(
    a: Sequence[Optional[Number]],
    b: Sequence[Optional[Number]],
    i: int,
) -> bool:
    """i 本目で a が b を下抜けしたか。None 安全。"""
    if i <= 0 or i >= len(a) or i >= len(b):
        return False
    a1, a0 = a[i], a[i - 1]
    b1, b0 = b[i], b[i - 1]
    if a1 is None or a0 is None or b1 is None or b0 is None:
        return False
    return a0 >= b0 and a1 < b1
