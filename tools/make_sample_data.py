"""
make_sample_data.py — ダミー H1 OHLC CSV を生成する。

ランダムウォーク + ゆるいトレンド成分。seed 固定で完全再現可能。
出力: data/sample_H1.csv

実データが無くてもエンジン・指標・戦略が落ちずに動くか確認するためのもの。
**勝ち負けの数値自体には意味がない**。
"""

from __future__ import annotations

import csv
import math
import os
import random
import sys
from pathlib import Path

# リポジトリ root を sys.path に通す
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def generate(
    out_path: str,
    n_bars: int | None = None,
    start_price: float = 1.1000,
    seed: int = 42,
    period_seconds: int = 3600,
) -> None:
    """ランダムウォーク + sin トレンドのダミー OHLC を生成。

    `period_seconds` で時間足を切り替える。既定は H1 (3600)。
    `n_bars` は既定で「約 1 年分」をその周期から自動算出する。
    """
    if n_bars is None:
        bars_per_year = 365 * 24 * 3600 // period_seconds
        n_bars = bars_per_year  # 約 1 年分

    rng = random.Random(seed)
    t = 1_500_000_000  # 固定 UNIX 秒（再現性のため）
    dt = period_seconds

    # ボラと wick を周期に比例させる（H1 ~5pips → M15 ~2.5pips になるよう sqrt スケール）
    scale = math.sqrt(period_seconds / 3600.0)
    step_sigma = 0.0005 * scale
    wick_sigma = 0.0003 * scale
    trend_amp = 0.00003 * scale

    price = start_price
    rows: list[tuple[int, float, float, float, float, float]] = []

    for i in range(n_bars):
        trend = math.sin(i / 800.0) * trend_amp
        step = rng.gauss(0.0, step_sigma) + trend
        new_close = max(0.5, price + step)

        wick_h = abs(rng.gauss(0.0, wick_sigma))
        wick_l = abs(rng.gauss(0.0, wick_sigma))
        o = price
        c = new_close
        h = max(o, c) + wick_h
        l = min(o, c) - wick_l
        rows.append((t, o, h, l, c, 0.0))

        price = new_close
        t += dt

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for row in rows:
            w.writerow(
                [
                    row[0],
                    f"{row[1]:.5f}",
                    f"{row[2]:.5f}",
                    f"{row[3]:.5f}",
                    f"{row[4]:.5f}",
                    f"{row[5]:.2f}",
                ]
            )


def default_path() -> str:
    return str(ROOT / "data" / "sample_H1.csv")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else default_path()
    generate(path)
    print(f"wrote sample data: {path}")
