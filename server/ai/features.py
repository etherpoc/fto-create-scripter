"""
features.py — 戦略状態 → 特徴量 dict。

ATR で正規化して銘柄横断で扱える形にする (ピップ単位等は使わない)。
LLM への入力としても、後の sklearn 学習にも使える共通フォーマット。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src.core.indicators import rsi as _rsi

# zigzag_line_break の Strategy 内部状態にアクセスするため。
# 型は緩く扱う (Strategy 種別を増やすときに別 builder を作れるよう)。


# AI (LLM) への入力時に除外するフィールド。
# これらは outcome 紐付けやログ用のメタ情報で、判断材料には使わない。
# data_collector のログには残るが、Ollama へは送らない。
LLM_NOISE_FIELDS = frozenset({"bar_time", "bar_idx", "atr", "price"})


def features_for_llm(features: dict) -> dict:
    """LLM へ送る用に、判断に不要な ID/メタ情報を取り除いた features を返す。"""
    return {k: v for k, v in features.items() if k not in LLM_NOISE_FIELDS}


def _dow_trend_from_pivots(pivots: list) -> Optional[str]:
    """直近 2 つの同方向ピボットから Dow トレンドを返す: "up" / "down" / None。

    strategies/zigzag_line_break/strategy.py の同名関数と同じロジック。
    上位足の trend を AI に明示するために features.py 側でも持っておく。
    """
    if len(pivots) < 4:
        if len(pivots) >= 2:
            last = pivots[-1]
            prev = pivots[-2]
            if last.kind == "high" and prev.kind == "low":
                return "up" if last.price > prev.price else None
            if last.kind == "low" and prev.kind == "high":
                return "down" if last.price < prev.price else None
        return None
    last4 = pivots[-4:]
    highs = [p for p in last4 if p.kind == "high"]
    lows = [p for p in last4 if p.kind == "low"]
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        ll = lows[-1].price < lows[-2].price
        lh = highs[-1].price < highs[-2].price
        if hh and hl:
            return "up"
        if ll and lh:
            return "down"
    return None


def _session_flags(hour_utc: int) -> dict[str, bool]:
    """UTC 時間から主要セッション稼働状況を返す。

    実装は近似値で、夏時間 (DST) シフトは無視 (1 時間ズレる可能性あり)。
    AI が「東京だけの薄い時間」「ロンドン+NY ピーク」を区別できる程度の粒度。
    """
    tokyo = 0 <= hour_utc < 9
    london = 7 <= hour_utc < 16
    ny = 13 <= hour_utc < 22
    return {
        "tokyo_open": tokyo,
        "london_open": london,
        "ny_open": ny,
        # 最も流動性が高い London+NY オーバーラップ
        "is_overlap": london and ny,
        # 主要セッションが全部閉じている (= ボラ薄、ノイズ多)
        "is_quiet": not (tokyo or london or ny),
    }


def build_zigzag_features(
    strategy: Any,
    atr_val: float,
    cur_bar: Any,
    direction: str,
    recent_closes: Optional[list[float]] = None,
    atr_line: Optional[list[Optional[float]]] = None,
) -> dict[str, Any]:
    """ZigZag 系戦略の現在状態から、AI 用の特徴量 dict を組む。

    - 価格はすべて「ATR 倍率」「現在価格との差分 / ATR」で正規化
    - 銘柄や時間軸が変わっても同じスケールで AI に投げられる

    Args:
        strategy: ZigZagLineBreakStrategy インスタンス (z1, z2 等のトラッカを持つ)
        atr_val:  現在の ATR 値 (価格単位)
        cur_bar:  最新確定足 (Bar)
        direction: "up" or "down" (これから入ろうとしているエントリ方向)
        recent_closes: 直近 N 本の M15 close (cur_bar 含む。新しいほど末尾)
    """
    price = cur_bar.close
    if atr_val is None or atr_val <= 0:
        atr_val = max(price * 0.0005, 0.0001)  # 安全フォールバック

    # 直近 Z1 ピボット情報 (新しい順、N 個)
    n_recent = 5
    recent_z1 = []
    for p in list(strategy.z1.pivots)[-n_recent:]:
        recent_z1.append({
            "kind": p.kind,
            "price_diff_atr": (p.price - price) / atr_val,  # >0=上, <0=下
        })

    # 直近 Z2 ピボット
    recent_z2 = []
    for p in list(strategy.z2.pivots)[-n_recent:]:
        recent_z2.append({
            "kind": p.kind,
            "price_diff_atr": (p.price - price) / atr_val,
        })

    # 上位足 (H1 / H4) Z1 ピボット
    def _mtf(tracker):
        out = []
        for p in list(tracker.pivots)[-n_recent:]:
            out.append({
                "kind": p.kind,
                "price_diff_atr": (p.price - price) / atr_val,
            })
        return out

    z1_h1 = _mtf(strategy.z1_h1)
    z1_h4 = _mtf(strategy.z1_h4)

    # ★ 上位足 trend を Python 側で計算して明示する。
    # 4B クラスの小型 LLM だとピボット配列だけから trend を推論しきれないため、
    # M15 と同じ _dow_trend_from_pivots を H1/H4 にも適用して結論を渡す。
    h1_trend = _dow_trend_from_pivots(list(strategy.z1_h1.pivots))
    h4_trend = _dow_trend_from_pivots(list(strategy.z1_h4.pivots))

    # 「壁」までの距離 (= 上位足ラインへの距離 / ATR)
    above_h1 = [p.price for p in strategy.z1_h1.pivots if p.price > price]
    below_h1 = [p.price for p in strategy.z1_h1.pivots if p.price < price]
    above_h4 = [p.price for p in strategy.z1_h4.pivots if p.price > price]
    below_h4 = [p.price for p in strategy.z1_h4.pivots if p.price < price]

    def _nearest_diff_atr(prices: list[float], above: bool) -> Optional[float]:
        if not prices:
            return None
        if above:
            return (min(prices) - price) / atr_val
        return (price - max(prices)) / atr_val

    nearest_above_h1 = _nearest_diff_atr(above_h1, above=True)
    nearest_below_h1 = _nearest_diff_atr(below_h1, above=False)
    nearest_above_h4 = _nearest_diff_atr(above_h4, above=True)
    nearest_below_h4 = _nearest_diff_atr(below_h4, above=False)

    # ★ 方向相対の wall。AI が「自分の進行方向にある壁が近いか?」を即判断できるよう
    # direction_intent と組み合わせて事前に計算。
    # blocking = 進行方向にある壁 (邪魔)、supporting = 反対方向にある壁 (背中の砦)
    if direction == "up":
        wall_blocking_h4_atr = nearest_above_h4
        wall_supporting_h4_atr = nearest_below_h4
        wall_blocking_h1_atr = nearest_above_h1
        wall_supporting_h1_atr = nearest_below_h1
    elif direction == "down":
        wall_blocking_h4_atr = nearest_below_h4
        wall_supporting_h4_atr = nearest_above_h4
        wall_blocking_h1_atr = nearest_below_h1
        wall_supporting_h1_atr = nearest_above_h1
    else:
        wall_blocking_h4_atr = None
        wall_supporting_h4_atr = None
        wall_blocking_h1_atr = None
        wall_supporting_h1_atr = None

    # SL/TP 設計の根拠となる reversal pivot 価格 (これから SL を置く位置の元)
    rev_pivot_diff_atr = None
    if strategy._reversal_z1_pivot_price is not None:
        rev_pivot_diff_atr = (strategy._reversal_z1_pivot_price - price) / atr_val

    # 「転換からこのバーまで何本経過したか」
    bars_since_reversal = strategy._bar_idx - strategy._last_reversal_bar_idx

    # ★ 直近 M15 close の動き (cur_bar を含む直近 N 本)。
    # ピボット列はスパースなので、AI に「直近数本の値動きの勢い」を別途渡す。
    # 各値は「前の close からの差分 / ATR」(>0=上昇足, <0=下落足)。
    n_close = 5
    recent_close_diffs_atr: list[float] = []
    if recent_closes and len(recent_closes) >= 2:
        tail = list(recent_closes)[-(n_close + 1):]  # diff を n_close 個出すために +1 本
        for prev_c, cur_c in zip(tail[:-1], tail[1:]):
            recent_close_diffs_atr.append((cur_c - prev_c) / atr_val)

    # ★ 銘柄情報。LLM の事前知識 (ゴールドはレンジ抜け継続しやすい、JPY はトレンド持続
    # など) を活用するため、ペア名を明示的に渡す。
    symbol = getattr(strategy, "symbol", "UNKNOWN")

    # ★ RSI(14): closes が十分あれば計算。M15 過熱感を AI に渡す。
    rsi_m15: Optional[float] = None
    if recent_closes and len(recent_closes) >= 15:
        try:
            rsi_line = _rsi(list(recent_closes), 14)
            v = rsi_line[-1]
            rsi_m15 = float(v) if v is not None else None
        except Exception:  # noqa: BLE001
            rsi_m15 = None

    # ★ ATR ratio: 直近 ATR / 過去 20 本の ATR 平均。
    # > 1.5 = ボラ拡大中 (= ニュース反応 / トレンド初動), < 0.7 = レンジ縮小中 (= 騙し多)
    atr_ratio: Optional[float] = None
    if atr_line:
        recent_atr = [x for x in atr_line[-20:] if x is not None and x > 0]
        if len(recent_atr) >= 5:
            mean = sum(recent_atr) / len(recent_atr)
            if mean > 0:
                atr_ratio = atr_val / mean

    # ★ 時間帯 / 曜日 (UTC 基準): 流動性と相関が高い
    try:
        dt = datetime.fromtimestamp(int(cur_bar.time), tz=timezone.utc)
        hour_utc = dt.hour
        weekday = dt.weekday()  # 0=Mon ... 6=Sun
    except Exception:  # noqa: BLE001
        hour_utc = 0
        weekday = 0
    session_flags = _session_flags(hour_utc)

    return {
        "symbol": symbol,
        "direction_intent": direction,             # "up" | "down" (これから入ろうとする方向)
        "trend": strategy._dow_trend,              # M15 Dow トレンド
        "h1_trend": h1_trend,                      # ★ 新規: H1 Dow トレンド
        "h4_trend": h4_trend,                      # ★ 新規: H4 Dow トレンド
        "bars_since_reversal": bars_since_reversal,
        "reversal_z1_pivot_diff_atr": rev_pivot_diff_atr,  # SL 元になるピボット位置
        "atr_relative": atr_val / price if price > 0 else 0,
        "recent_z1": recent_z1,
        "recent_z2": recent_z2,
        "z1_h1": z1_h1,
        "z1_h4": z1_h4,
        # 旧フィールド (方向非依存)。学習データとして残す。
        "nearest_wall_above_h1_atr": nearest_above_h1,
        "nearest_wall_below_h1_atr": nearest_below_h1,
        "nearest_wall_above_h4_atr": nearest_above_h4,
        "nearest_wall_below_h4_atr": nearest_below_h4,
        # ★ 新規: 方向相対の wall (AI が即理解できる形)
        "wall_blocking_h1_atr": wall_blocking_h1_atr,
        "wall_supporting_h1_atr": wall_supporting_h1_atr,
        "wall_blocking_h4_atr": wall_blocking_h4_atr,
        "wall_supporting_h4_atr": wall_supporting_h4_atr,
        # ★ 新規: 直近 close diff の系列 (M15)
        "recent_close_diffs_atr": recent_close_diffs_atr,
        # ★ 新規: 指標と時間軸
        "rsi_m15": rsi_m15,
        "atr_ratio_vs_recent": atr_ratio,
        "hour_utc": hour_utc,
        "weekday": weekday,  # 0=Mon ... 6=Sun
        **session_flags,
        # 後で outcome と join するための時刻と価格 (生値) — LLM には渡らない
        "bar_time": int(cur_bar.time),
        "price": float(price),
        "atr": float(atr_val),
        "bar_idx": int(getattr(strategy, "_bar_idx", 0)),
    }
