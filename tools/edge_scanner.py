"""
edge_scanner.py — 録音済みデータから統計的優位性を網羅検索する。

各仮説について:
- 条件 X を満たす bar をピックアップ
- N 本後の price 変化を計算 (forward return)
- 平均、勝率、サンプル数、t 統計量を計算
- P1 (2021-23 IS) と P2 (2024-26 OOS) で別々に計算 → 両方で有意なら真の edge

Usage:
    python tools/edge_scanner.py --source data/recorded_ticks_5y --hypothesis time_of_day
    python tools/edge_scanner.py --source data/recorded_ticks_5y --hypothesis all
"""

from __future__ import annotations

import argparse
import json
import glob
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional


# 既存 indicators
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.core.indicators import atr, rsi


@dataclass
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def load_bars(symbol_dir: str) -> list[Bar]:
    """1 シンボルの全 tick (= M15 bars) をロード、時刻順にソート、重複除去。"""
    bars: list[Bar] = []
    seen = set()
    for f in sorted(glob.glob(os.path.join(symbol_dir, "*.jsonl"))):
        with open(f) as fp:
            for line in fp:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if r.get("_type") != "tick":
                    continue
                m15 = r.get("m15") or {}
                try:
                    t = int(m15["time"])
                    if t in seen:
                        continue
                    seen.add(t)
                    bars.append(Bar(
                        time=t,
                        open=float(m15["open"]),
                        high=float(m15["high"]),
                        low=float(m15["low"]),
                        close=float(m15["close"]),
                        volume=float(m15.get("volume", 0)),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
    bars.sort(key=lambda b: b.time)
    return bars


# ---- forward return 計算 ----

def forward_returns_atr(bars: list[Bar], horizons: list[int],
                         atr_period: int = 14) -> list[dict]:
    """各 bar に対し、N 本後の終値変化を ATR 正規化したリストを返す。

    Returns: per-bar dict with {time, atr, fwd_N: signed_return_per_ATR, ...}
    """
    if len(bars) < atr_period + max(horizons) + 1:
        return []
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    atr_line = atr(highs, lows, closes, atr_period)
    out = []
    for i, b in enumerate(bars):
        a = atr_line[i]
        if a is None or a <= 0:
            continue
        rec = {"i": i, "time": b.time, "atr": a, "close": b.close}
        ok = True
        for h in horizons:
            if i + h >= len(bars):
                ok = False
                break
            fwd = (bars[i + h].close - b.close) / a
            rec[f"fwd_{h}"] = fwd
        if ok:
            out.append(rec)
    return out


# ---- 統計関数 ----

def t_stat(samples: list[float]) -> tuple[float, float, float]:
    """サンプル平均 t 統計量。返り値 (mean, std, t)。"""
    n = len(samples)
    if n < 2:
        return (0.0, 0.0, 0.0)
    mean = sum(samples) / n
    var = sum((x - mean) ** 2 for x in samples) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return (mean, 0.0, 0.0)
    t = mean / (std / math.sqrt(n))
    return (mean, std, t)


def summarize(returns: list[float]) -> dict:
    n = len(returns)
    if n == 0:
        return {"n": 0}
    mean, std, t = t_stat(returns)
    won = sum(1 for r in returns if r > 0)
    lost = sum(1 for r in returns if r < 0)
    wr = 100 * won / (won + lost) if (won + lost) else 0
    # 有意性判定: |t| > 1.96 で 5% 有意 (両側), > 2.58 で 1% 有意
    if abs(t) > 2.58:
        sig = "***"
    elif abs(t) > 1.96:
        sig = "**"
    elif abs(t) > 1.65:
        sig = "*"
    else:
        sig = ""
    return {"n": n, "mean": mean, "std": std, "t": t, "wr": wr, "sig": sig,
            "won": won, "lost": lost}


# ---- 仮説 1: 時間帯バイアス ----

def hypothesis_time_of_day(returns_by_bar: list[dict], horizon: int = 4) -> dict:
    """各 UTC 時間別に、N 本 (デフォルト 1 時間 = M15×4) 後の変化を集計。

    1 時間後の動き = M15 で 4 本後。
    返り値: {hour: stats}
    """
    by_hour = defaultdict(list)
    for rec in returns_by_bar:
        if f"fwd_{horizon}" not in rec:
            continue
        dt = datetime.fromtimestamp(rec["time"], tz=timezone.utc)
        by_hour[dt.hour].append(rec[f"fwd_{horizon}"])
    return {h: summarize(by_hour[h]) for h in sorted(by_hour.keys())}


# ---- 仮説 2: ATR 縮小→拡大の方向 ----

def hypothesis_atr_contraction(returns_by_bar: list[dict], bars: list[Bar],
                                 horizon: int = 4, contract_bars: int = 10) -> dict:
    """ATR が直近 N 本で縮小傾向後の、次の H 本の動きを集計。

    縮小判定: 直近 N 本の ATR 平均 < さらに前の N 本の ATR 平均 × 0.8
    """
    matching = []
    all_returns = []
    # ATR は returns_by_bar に入ってる
    atrs = [rec["atr"] for rec in returns_by_bar]
    for i, rec in enumerate(returns_by_bar):
        if f"fwd_{horizon}" not in rec:
            continue
        all_returns.append(rec[f"fwd_{horizon}"])
        if i < contract_bars * 2:
            continue
        recent = atrs[i - contract_bars: i]
        prev = atrs[i - contract_bars * 2: i - contract_bars]
        if not recent or not prev:
            continue
        avg_recent = sum(recent) / len(recent)
        avg_prev = sum(prev) / len(prev)
        if avg_recent < avg_prev * 0.8:
            matching.append(rec[f"fwd_{horizon}"])
    return {
        "matching": summarize(matching),
        "baseline": summarize(all_returns),
    }


# ---- 仮説 3: 連続同色キャンドル後 ----

def hypothesis_consecutive_candles(returns_by_bar: list[dict], bars: list[Bar],
                                     horizon: int = 4, n_consec: int = 3) -> dict:
    """連続 N 本同色 (陽 or 陰) 後の次 H 本の動きを集計。

    陽 = close > open、陰 = close < open
    """
    bull_match = []
    bear_match = []
    all_returns = []
    for rec in returns_by_bar:
        if f"fwd_{horizon}" not in rec:
            continue
        all_returns.append(rec[f"fwd_{horizon}"])
        i = rec["i"]
        if i < n_consec:
            continue
        # 直前 n_consec 本
        prev_bars = bars[i - n_consec: i]
        bullish = all(b.close > b.open for b in prev_bars)
        bearish = all(b.close < b.open for b in prev_bars)
        if bullish:
            bull_match.append(rec[f"fwd_{horizon}"])
        elif bearish:
            bear_match.append(rec[f"fwd_{horizon}"])
    return {
        "after_bull": summarize(bull_match),
        "after_bear": summarize(bear_match),
        "baseline": summarize(all_returns),
    }


# ---- 仮説 4: 直近 N 本高値ブレイク後 ----

def hypothesis_breakout(returns_by_bar: list[dict], bars: list[Bar],
                          horizon: int = 8, lookback: int = 20) -> dict:
    """直近 lookback 本の高値を更新した bar の、次 H 本の動きを集計。"""
    high_break = []
    low_break = []
    all_returns = []
    for rec in returns_by_bar:
        if f"fwd_{horizon}" not in rec:
            continue
        all_returns.append(rec[f"fwd_{horizon}"])
        i = rec["i"]
        if i < lookback:
            continue
        recent_high = max(b.high for b in bars[i - lookback: i])
        recent_low = min(b.low for b in bars[i - lookback: i])
        if bars[i].high > recent_high:
            high_break.append(rec[f"fwd_{horizon}"])
        elif bars[i].low < recent_low:
            low_break.append(rec[f"fwd_{horizon}"])
    return {
        "after_high_break": summarize(high_break),
        "after_low_break": summarize(low_break),
        "baseline": summarize(all_returns),
    }


# ---- 期間フィルタ ----

def filter_period(records: list[dict], start_dt: datetime, end_dt: datetime) -> list[dict]:
    return [r for r in records
            if start_dt.timestamp() <= r["time"] < end_dt.timestamp()]


# ---- main ----

P1_start = datetime(2021, 1, 1, tzinfo=timezone.utc)
P1_end   = datetime(2024, 1, 1, tzinfo=timezone.utc)
P2_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
P2_end   = datetime(2026, 7, 1, tzinfo=timezone.utc)


def fmt_stats(s: dict) -> str:
    if s.get("n", 0) == 0:
        return "n=0"
    return (f"n={s['n']:5d} mean={s['mean']:+.4f} t={s['t']:+5.2f}{s['sig']} "
            f"WR={s['wr']:5.1f}%")


def run_time_of_day(bars: list[Bar], symbol: str) -> None:
    print(f"\n=== {symbol} - Time-of-day bias (1h forward return / ATR) ===")
    records = forward_returns_atr(bars, horizons=[4])
    if not records:
        print("  insufficient data")
        return
    print(f"  {'Hour':>4s} | P1 (IS 2021-23) {'':22s} | P2 (OOS 2024-26)")
    print(f"       | {'n':>5s} {'mean':>8s} {'t':>6s} {'WR%':>6s} | {'n':>5s} {'mean':>8s} {'t':>6s} {'WR%':>6s}")
    p1_recs = filter_period(records, P1_start, P1_end)
    p2_recs = filter_period(records, P2_start, P2_end)
    p1_h = hypothesis_time_of_day(p1_recs)
    p2_h = hypothesis_time_of_day(p2_recs)
    robust_hours = []
    for hour in range(24):
        p1 = p1_h.get(hour, {"n": 0, "mean": 0, "t": 0, "wr": 0, "sig": ""})
        p2 = p2_h.get(hour, {"n": 0, "mean": 0, "t": 0, "wr": 0, "sig": ""})
        if p1.get("n", 0) == 0 and p2.get("n", 0) == 0:
            continue
        # ROBUST 判定: 両方有意 + 同方向
        if (p1.get("sig", "") and p2.get("sig", "")
                and p1["mean"] * p2["mean"] > 0):
            mark = " ROBUST"
            robust_hours.append((hour, p1, p2))
        else:
            mark = ""
        print(f"  {hour:>3d}h | "
              f"{p1.get('n', 0):5d} {p1.get('mean', 0):+8.4f} {p1.get('t', 0):+6.2f}{p1.get('sig','').ljust(4)} {p1.get('wr', 0):>5.1f}% | "
              f"{p2.get('n', 0):5d} {p2.get('mean', 0):+8.4f} {p2.get('t', 0):+6.2f}{p2.get('sig','').ljust(4)} {p2.get('wr', 0):>5.1f}%"
              f"{mark}")
    if robust_hours:
        print(f"  → ROBUST hours: {[h for h,_,_ in robust_hours]}")


def run_one_hypothesis(name: str, fn, bars: list[Bar], symbol: str) -> None:
    print(f"\n=== {symbol} - {name} ===")
    records = forward_returns_atr(bars, horizons=[4, 8, 16])
    if not records:
        print("  insufficient data")
        return
    p1_recs = filter_period(records, P1_start, P1_end)
    p2_recs = filter_period(records, P2_start, P2_end)
    p1_res = fn(p1_recs, bars)
    p2_res = fn(p2_recs, bars)
    keys = sorted(p1_res.keys())
    print(f"  {'Variant':22s} | {'P1 (IS 2021-23)':40s} | {'P2 (OOS 2024-26)':40s}")
    for k in keys:
        p1 = p1_res[k]
        p2 = p2_res[k]
        robust = ""
        if (isinstance(p1, dict) and p1.get("sig")
                and isinstance(p2, dict) and p2.get("sig")
                and p1["mean"] * p2["mean"] > 0):
            robust = "  ROBUST"
        print(f"  {k:22s} | {fmt_stats(p1):38s} | {fmt_stats(p2):38s}{robust}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/recorded_ticks_5y")
    ap.add_argument("--symbol", default=None,
                    help="特定 symbol だけ (デフォルト: 全 12 ペア)")
    ap.add_argument("--hypothesis", default="all",
                    choices=["all", "time_of_day", "atr_contraction",
                             "consecutive_candles", "breakout"])
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass

    syms = sorted(os.listdir(args.source))
    if args.symbol:
        syms = [args.symbol] if args.symbol in syms else []

    for sym in syms:
        sym_dir = os.path.join(args.source, sym)
        if not os.path.isdir(sym_dir):
            continue
        print(f"\n{'#'*70}\n# {sym}\n{'#'*70}")
        bars = load_bars(sym_dir)
        print(f"  Loaded {len(bars)} bars  ({datetime.fromtimestamp(bars[0].time, tz=timezone.utc):%Y-%m-%d} ~ {datetime.fromtimestamp(bars[-1].time, tz=timezone.utc):%Y-%m-%d})")
        if args.hypothesis in ("all", "time_of_day"):
            run_time_of_day(bars, sym)
        if args.hypothesis in ("all", "atr_contraction"):
            run_one_hypothesis("ATR contraction → forward return (h=8)",
                                lambda recs, b: hypothesis_atr_contraction(recs, b, horizon=8),
                                bars, sym)
        if args.hypothesis in ("all", "consecutive_candles"):
            run_one_hypothesis("3 consecutive candles → forward return (h=4)",
                                lambda recs, b: hypothesis_consecutive_candles(recs, b, horizon=4, n_consec=3),
                                bars, sym)
        if args.hypothesis in ("all", "breakout"):
            run_one_hypothesis("20-bar breakout → forward return (h=8)",
                                lambda recs, b: hypothesis_breakout(recs, b, horizon=8, lookback=20),
                                bars, sym)

    return 0


if __name__ == "__main__":
    sys.exit(main())
