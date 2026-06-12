"""
backtest_pure_dow.py — 「純粋なダウ理論だけ」を 5.5 年録音データで検証する。

設計 (ユーザ指定):
  - 単一 TF = M15 のみ。MTF 整合・押し目・トレンドライン等のフィルタは一切なし。
  - トレンド判定 = ダウ理論 (_dow_trend: HH/HL=up, LL/LH=down)。mtf_pullback と同じ関数を再利用。
  - エグジット = トレンド転換でドテン (常時ポジション)。SL/TP なし。
    - flat のとき初トレンドでエントリー
    - トレンドが反対に転換したら即ドテン (close + reverse)
    - トレンド = None の間は現ポジ保持 (ダウ: 転換確定まで継続)

なぜ専用スクリプトか:
  既存の WS replay (tools/replay_ticks.py) は「1 entry → SL/TP 決済」前提で、
  SL/TP なしのドテン式を正しく回せない (tp=0 で即時決済バグ)。よって録音 M5 を
  直接読み、M15 に集計して Python 内でドテンをシミュレートする。

リスク単位について (重要・正直な注記):
  ドテン式は SL を置かないので「1 トレード = 口座 1%」という固定リスクが定義できない
  (負け方向に止め値なく走るとロスが青天井)。よって主指標は:
    - R_atr = pnl_price / (エントリー時 ATR)   ← ボラ正規化したエッジ尺度
    - profit factor (総利益 / 総損失)
    - 期待値 (平均 R_atr / トレード)
    - 最大単発損失 R_atr (ノーストップの裾リスク可視化)
  参考として「1×ATR 逆行 = 口座 1% と仮定したら月利何 %」も出すが、これは損失を
  キャップしない前提なので mtf_pullback (SL で 1% に固定) とは risk model が違う。

Usage:
    python tools/backtest_pure_dow.py
    python tools/backtest_pure_dow.py --symbol EURUSD
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.indicators import ZigZagTracker, atr  # noqa: E402
from src.core.strategy_base import Bar  # noqa: E402
from strategies.mtf_pullback.strategy import _dow_trend  # noqa: E402

SRC_DIR = "data/recorded_ticks_5y_m5"
OUT_DIR = "data/eval_5y/pure_dow_m15"

# Walk-Forward (compare_mtf_pb_variants.py と同じ区切り)
P1_START = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P2_START = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P2_END = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
P1_MONTHS = 36
P2_MONTHS = 30

# ZigZag (M15) — mtf_pullback の zz_depth_m15 / zz_dev_pips と揃える
ZZ_DEPTH_M15 = 8
ZZ_DEV_PIPS = 3.0
ATR_PERIOD = 14
M15_SEC = 900
M5_SEC = 300


def pip_size_for(symbol: str) -> float:
    s = symbol.upper()
    if s == "XAUUSD":
        return 0.1
    if s.endswith("JPY"):
        return 0.01
    return 0.0001


def load_m5_bars(pair_dir: Path) -> list[Bar]:
    """ペアの全 jsonl から M5 バーを読み、time で dedup・ソートして返す。"""
    seen: dict[int, Bar] = {}
    for fp in sorted(pair_dir.glob("*.jsonl")):
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                if '"tick"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("_type") != "tick":
                    continue
                m = rec.get("m15")  # 旧名フィールドに M5 バーが入っている
                if not m:
                    continue
                t = int(m["time"])
                if t in seen:
                    continue
                seen[t] = Bar(
                    time=t,
                    open=float(m["open"]),
                    high=float(m["high"]),
                    low=float(m["low"]),
                    close=float(m["close"]),
                    volume=float(m.get("volume") or 0.0),
                )
    return [seen[t] for t in sorted(seen.keys())]


def aggregate_m15(m5: list[Bar]) -> list[Bar]:
    """M5 を M15 グリッド (time % 900 == 0 開始) で 3 本ずつまとめる。完全な 3 本のみ採用。"""
    out: list[Bar] = []
    # 900 境界に乗る最初の index を探す
    i = 0
    n = len(m5)
    while i < n and m5[i].time % M15_SEC != 0:
        i += 1
    while i + 2 < n:
        b0, b1, b2 = m5[i], m5[i + 1], m5[i + 2]
        # 連続性チェック (欠損があればその塊はスキップして次の境界へ)
        if b1.time - b0.time != M5_SEC or b2.time - b1.time != M5_SEC:
            # 次の 900 境界まで進める
            i += 1
            while i < n and m5[i].time % M15_SEC != 0:
                i += 1
            continue
        out.append(Bar(
            time=b0.time,
            open=b0.open,
            high=max(b0.high, b1.high, b2.high),
            low=min(b0.low, b1.low, b2.low),
            close=b2.close,
            volume=b0.volume + b1.volume + b2.volume,
        ))
        i += 3
    return out


def run_pair(symbol: str, m15: list[Bar]) -> list[dict]:
    """M15 列に対し純粋ダウのドテンを回し、outcome レコード列を返す。"""
    pip = pip_size_for(symbol)
    zz = ZigZagTracker(ZZ_DEPTH_M15, ZZ_DEV_PIPS * pip)

    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []

    # ポジション状態
    pos_side: Optional[str] = None      # "long" / "short" / None
    entry_price = 0.0
    entry_time = 0
    entry_atr = 0.0
    entry_idx = 0

    outcomes: list[dict] = []

    def close_position(exit_price: float, exit_time: int, idx: int) -> None:
        nonlocal pos_side
        if pos_side is None:
            return
        if pos_side == "long":
            pnl = exit_price - entry_price
        else:
            pnl = entry_price - exit_price
        outcomes.append({
            "type": "outcome",
            "symbol": symbol,
            "side": pos_side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_bar_time": entry_time,
            "exit_bar_time": exit_time,
            "bars_held": max(0, idx - entry_idx),
            "pnl_price": pnl,
            "atr_at_entry": entry_atr,
            "exit_reason": "dow_reverse",
        })
        pos_side = None

    for idx, bar in enumerate(m15):
        zz.update(bar)
        highs.append(bar.high)
        lows.append(bar.low)
        closes.append(bar.close)

        trend = _dow_trend(zz.pivots)

        # ATR (確定足ベース)
        if len(closes) < ATR_PERIOD + 1:
            continue
        atr_val = atr(highs[-(ATR_PERIOD + 2):], lows[-(ATR_PERIOD + 2):], closes[-(ATR_PERIOD + 2):], ATR_PERIOD)[-1]
        if atr_val is None or atr_val <= 0:
            continue

        if trend is None:
            continue  # ダウ: トレンド未確定の間は現ポジ保持

        desired = "long" if trend == "up" else "short"
        if pos_side == desired:
            continue  # 同方向 → 保持

        # 転換 or 初エントリー: 既存があれば決済してドテン
        if pos_side is not None:
            close_position(bar.close, bar.time, idx)
        # 新規エントリー
        pos_side = desired
        entry_price = bar.close
        entry_time = bar.time
        entry_atr = atr_val
        entry_idx = idx

    return outcomes


def write_outcomes(symbol: str, outcomes: list[dict]) -> None:
    out_path = ROOT / OUT_DIR / symbol
    out_path.mkdir(parents=True, exist_ok=True)
    fp = out_path / f"{symbol.lower()}.jsonl"
    with open(fp, "w", encoding="utf-8") as f:
        for r in outcomes:
            f.write(json.dumps(r) + "\n")


# ---------------- 集計 ----------------

def split_periods(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    p1 = [x for x in trades if P1_START <= x["entry_bar_time"] < P1_END]
    p2 = [x for x in trades if P2_START <= x["entry_bar_time"] < P2_END]
    return p1, p2


def stats(trades: list[dict], months: int) -> dict:
    if not trades:
        return {"n": 0}
    n = len(trades)
    rs = [t["pnl_price"] / t["atr_at_entry"] for t in trades if t["atr_at_entry"] > 0]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    sumR = sum(rs)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return {
        "n": n,
        "wr": 100 * len(wins) / n,
        "sumR": sumR,
        "expR": sumR / n,
        "pf": pf,
        "worst": min(rs) if rs else 0.0,
        "best": max(rs) if rs else 0.0,
        "pct_mo": sumR / months * 0.01 * 100,  # 仮定: 1×ATR=口座1%、損失キャップなし
    }


def fmt(s: dict) -> str:
    if s.get("n", 0) == 0:
        return "n=  0"
    pf = s["pf"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (f"n={s['n']:4d} WR={s['wr']:4.1f}% PF={pf_s:>4} "
            f"expR={s['expR']:+.3f} sumR={s['sumR']:+7.1f} "
            f"worst={s['worst']:+.1f} ({s['pct_mo']:+.2f}%/mo*)")


def verdict(p1: dict, p2: dict) -> str:
    a = p1.get("sumR", 0) > 0
    b = p2.get("sumR", 0) > 0
    if a and b:
        return "[ROBUST]"
    if a and not b:
        return "[Edge GONE]"
    if not a and b:
        return "[OOS+ only]"
    return "[BAD both]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None, help="特定 symbol だけ")
    ap.add_argument("--no-write", action="store_true", help="outcome を書かない (集計のみ)")
    args = ap.parse_args()

    src = ROOT / SRC_DIR
    pair_dirs = sorted(d for d in src.iterdir() if d.is_dir())
    if args.symbol:
        pair_dirs = [d for d in pair_dirs if d.name.upper() == args.symbol.upper()]

    all_outcomes: dict[str, list[dict]] = {}
    for d in pair_dirs:
        sym = d.name
        m5 = load_m5_bars(d)
        m15 = aggregate_m15(m5)
        outs = run_pair(sym, m15)
        all_outcomes[sym] = outs
        if not args.no_write:
            write_outcomes(sym, outs)
        print(f"  {sym}: M5={len(m5)} M15={len(m15)} trades={len(outs)}")

    print()
    print("=" * 132)
    print("純粋ダウ理論 (M15単独・ドテン式・SL/TPなし)  Walk-Forward  P1=2021-23(36mo) / P2=2024-26(30mo)")
    print("  R = pnl_price / entry時ATR (ボラ正規化)。 *%/mo は『1×ATR逆行=口座1%・損失キャップなし』仮定。")
    print("=" * 132)

    fx_p1: list[dict] = []
    fx_p2: list[dict] = []
    all_p1: list[dict] = []
    all_p2: list[dict] = []

    for sym in sorted(all_outcomes.keys()):
        trades = all_outcomes[sym]
        p1, p2 = split_periods(trades)
        s1, s2 = stats(p1, P1_MONTHS), stats(p2, P2_MONTHS)
        print(f"\n[{sym}]")
        print(f"  P1 {fmt(s1)}")
        print(f"  P2 {fmt(s2)}   {verdict(s1, s2)}")
        all_p1 += p1
        all_p2 += p2
        if sym.upper() != "XAUUSD":
            fx_p1 += p1
            fx_p2 += p2

    print("\n" + "=" * 132)
    print("TOTAL (全12ペア)")
    s1, s2 = stats(all_p1, P1_MONTHS), stats(all_p2, P2_MONTHS)
    print(f"  P1 {fmt(s1)}")
    print(f"  P2 {fmt(s2)}   {verdict(s1, s2)}")

    print("\nTOTAL (XAUUSD除く FX のみ)")
    s1, s2 = stats(fx_p1, P1_MONTHS), stats(fx_p2, P2_MONTHS)
    print(f"  P1 {fmt(s1)}")
    print(f"  P2 {fmt(s2)}   {verdict(s1, s2)}")
    print("=" * 132)
    return 0


if __name__ == "__main__":
    sys.exit(main())
