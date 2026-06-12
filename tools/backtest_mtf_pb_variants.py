"""
backtest_mtf_pb_variants.py — mtf_pullback v2 の「頻度を上げる緩和版」を 5.5 年データで比較。

ユーザ指定の緩和レバー:
  - M30 を大局アラインメントから外す (require_m30_alignment=False)
  - ZigZag depth を下げる (h1/h4/m30/m15 を浅く)

検証済みの MtfPullbackStrategy 本体をそのまま使い、最小 Context で M5 を 1 本ずつ流す。
H1/H4 は録音データの値を bars_mtf 経由で与える (= 検証済み v2 と同じデータ構成)。
SL/TP 決済を内部シミュレートし、R=pnl_price/sl_dist でウォークフォワード集計。

使い方:
    python tools/backtest_mtf_pb_variants.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.strategy_base import Bar  # noqa: E402
from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402

SRC = "data/recorded_ticks_5y_m5"
# ペア選択: env PAIRS="EURUSD,USDJPY,..." で明示指定、ALL=1 で全12、既定は ROBUST 4
_ALL = os.environ.get("ALL") in ("1", "true", "yes")
_PAIRS_ENV = os.environ.get("PAIRS")
if _PAIRS_ENV:
    PAIRS = [s.strip().upper() for s in _PAIRS_ENV.split(",") if s.strip()]
elif _ALL:
    PAIRS = [
        "AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
        "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD",
    ]
else:
    PAIRS = ["CADJPY", "EURJPY", "EURUSD", "USDCAD"]

P1_START = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P2_END = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
P1_MONTHS = 36
P2_MONTHS = 30

# 比較するバリアント (name -> Params への上書き dict)
_H = {"block_hour_start": 6, "block_hour_end": 10}
_RB = {"tp_rr": 1.5, "room_R_max": 2.0}
_BEST = {**_RB, **_H, "min_sl_dist_atr": 2.0}   # room_R<2.0 + 6-10h除外 + minSL2.0
VARIANTS_HTF = {
    "HTF RR1.5": {"htf_mode": True, "tp_rr": 1.5},
    "HTF RR2.0": {"htf_mode": True, "tp_rr": 2.0},
    "HTF RR2.0 +skip6-10h": {"htf_mode": True, "tp_rr": 2.0, **_H},
}
VARIANTS_RR = {
    "room+hour RR1.5 (parsim best)": {"tp_rr": 1.5, "room_R_max": 2.0, **_H},
    "room+hour RR2.0": {"tp_rr": 2.0, "room_R_max": 2.0, **_H},
    "room+hour RR2.5": {"tp_rr": 2.5, "room_R_max": 2.0, **_H},
}
VARIANTS = {
    # --- アブレーション: base(RR1.5) に 1 条件ずつ追加 ---
    "RR1.5 素のv2": {"tp_rr": 1.5},
    "+room_R<2.0": _RB,
    "+skip6-10h": {"tp_rr": 1.5, **_H},
    "+minSL2.0": {"tp_rr": 1.5, "min_sl_dist_atr": 2.0},
    "+expanding_legs(T1)": {"tp_rr": 1.5, "require_expanding_legs": True},
    "+m15_unupdated": {"tp_rr": 1.5, "require_m15_unupdated_extreme": True},
    # --- 組合せ / leave-one-out ---
    "現ベスト(room+hour+minSL)": _BEST,
    "best -minSL (room+hour)": {**_RB, **_H},
    "best -hour (room+minSL)": {**_RB, "min_sl_dist_atr": 2.0},
    "best +expanding": {**_BEST, "require_expanding_legs": True},
    # --- タスク2: HTF (H4=H1, M15転換) ---
    "HTF RR1.5": {"htf_mode": True, "tp_rr": 1.5},
    "HTF RR1.5 +skip6-10h": {"htf_mode": True, "tp_rr": 1.5, **_H},
    "HTF RR2.0": {"htf_mode": True, "tp_rr": 2.0},
}
_F = {"tp_rr": 1.5, "room_R_max": 2.0, **_H}   # room_R<2.0 + 6-10h除外
VARIANTS_ALIGN = {
    "ref H4+H1+M15 (素)": {"tp_rr": 1.5, "align_tfs": "h4,h1,m15"},
    "H4+M15のみ (素)": {"tp_rr": 1.5, "align_tfs": "h4,m15"},
    "H1+M15のみ (素)": {"tp_rr": 1.5, "align_tfs": "h1,m15"},
    "ref H4+H1+M15 +filters": {**_F, "align_tfs": "h4,h1,m15"},
    "H4+M15のみ +filters": {**_F, "align_tfs": "h4,m15"},
    "H1+M15のみ +filters": {**_F, "align_tfs": "h1,m15"},
}
if os.environ.get("HTF"):
    VARIANTS = VARIANTS_HTF
if os.environ.get("RRTEST"):
    VARIANTS = VARIANTS_RR
_HM = {"tp_rr": 1.5, "align_tfs": "h1,m15", "room_R_max": 2.0, **_H}   # 新ベスト H1+M15
VARIANTS_VOL = {
    "H1+M15 ベース": _HM,
    "+ maxATR% 0.07": {**_HM, "max_atr_pct": 0.07},
    "+ maxATR% 0.05": {**_HM, "max_atr_pct": 0.05},
}
if os.environ.get("ALIGN"):
    VARIANTS = VARIANTS_ALIGN
VARIANTS_CONF = {
    "H1+M15 ベース": _HM,
    "+ confluence 1.0": {**_HM, "require_h1_confluence": True, "confluence_atr": 1.0},
    "+ confluence 2.0": {**_HM, "require_h1_confluence": True, "confluence_atr": 2.0},
    "+ confluence 3.0": {**_HM, "require_h1_confluence": True, "confluence_atr": 3.0},
}
if os.environ.get("VOL"):
    VARIANTS = VARIANTS_VOL
VARIANTS_IMP = {
    "H1+M15 ベース": _HM,
    "+ impulse 0.8": {**_HM, "require_impulse": True, "impulse_atr": 0.8},
    "+ impulse 1.2": {**_HM, "require_impulse": True, "impulse_atr": 1.2},
    "+ impulse 1.6": {**_HM, "require_impulse": True, "impulse_atr": 1.6},
}
if os.environ.get("CONF"):
    VARIANTS = VARIANTS_CONF
VARIANTS_RVOL = {
    "H1+M15 ベース": _HM,
    "+ relVol 1.5x": {**_HM, "max_atr_ratio": 1.5},
    "+ relVol 2.0x": {**_HM, "max_atr_ratio": 2.0},
    "+ relVol 2.5x": {**_HM, "max_atr_ratio": 2.5},
}
if os.environ.get("IMP"):
    VARIANTS = VARIANTS_IMP
if os.environ.get("RVOL"):
    VARIANTS = VARIANTS_RVOL


class SimContext:
    """strategy.on_bar が使う最小 Context。"""
    def __init__(self) -> None:
        self.bars_seq: list[Bar] = []
        self.mtf: dict[int, list[Bar]] = {3600: [], 14400: []}
        self._position: Optional[str] = None
        self._balance = 100000.0
        self.pending_commands: list[dict] = []

    def bars(self, n: int) -> list[Bar]:
        return self.bars_seq[-n:]

    def bars_mtf(self, period_seconds: int, n: int) -> list[Bar]:
        arr = self.mtf.get(period_seconds, [])
        return arr[-n:] if arr else []

    def position(self) -> Optional[str]:
        return self._position

    def buy(self, volume, sl=None, tp=None) -> None:
        self.pending_commands.append({"type": "buy", "volume": volume, "sl": sl, "tp": tp})

    def sell(self, volume, sl=None, tp=None) -> None:
        self.pending_commands.append({"type": "sell", "volume": volume, "sl": sl, "tp": tp})

    def close(self) -> None:
        self.pending_commands.append({"type": "close"})

    def account_balance(self) -> float:
        return self._balance

    def log(self, msg: str) -> None:
        pass


def _bar(d: dict) -> Optional[Bar]:
    t = int(d["time"])
    if t <= 0:
        return None
    return Bar(time=t, open=d["open"], high=d["high"], low=d["low"], close=d["close"], volume=d.get("volume", 0) or 0)


def load_ticks(pair: str) -> list[tuple[Bar, Optional[Bar], Optional[Bar]]]:
    """ペアの全 jsonl から (m5, h1, h4) を時刻昇順・dedup で返す。"""
    seen: dict[int, dict] = {}
    for fp in sorted(glob.glob(os.path.join(ROOT, SRC, pair, "*.jsonl"))):
        with open(fp, encoding="utf-8") as f:
            for line in f:
                if '"tick"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("_type") != "tick":
                    continue
                m = r.get("m15")
                if not m:
                    continue
                t = int(m["time"])
                if t in seen:
                    continue
                seen[t] = r
    out = []
    for t in sorted(seen):
        r = seen[t]
        m5 = _bar(r["m15"])
        if m5 is None:
            continue
        h1 = _bar(r["h1"]) if r.get("h1") else None
        h4 = _bar(r["h4"]) if r.get("h4") else None
        out.append((m5, h1, h4))
    return out


def run_pair(pair: str, params: Params, ticks) -> list[dict]:
    strat = MtfPullbackStrategy(params)
    strat.symbol = pair
    ctx = SimContext()
    last_h1 = -1
    last_h4 = -1
    open_trade: Optional[dict] = None
    outcomes: list[dict] = []

    for (m5, h1, h4) in ticks:
        # bars 履歴 (直近 30 本で十分: ATR=24, M30集計=6)
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60:
            ctx.bars_seq.pop(0)
        if h1 is not None and h1.time > last_h1:
            ctx.mtf[3600].append(h1); last_h1 = h1.time
            if len(ctx.mtf[3600]) > 200:
                ctx.mtf[3600].pop(0)
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time
            if len(ctx.mtf[14400]) > 200:
                ctx.mtf[14400].pop(0)

        # SL/TP 決済 (この M5 で既存ポジが触れたか。SL 優先)
        if open_trade is not None:
            hit = None; exitp = None
            if open_trade["side"] == "long":
                if m5.low <= open_trade["sl"]:
                    hit, exitp = "sl", open_trade["sl"]
                elif m5.high >= open_trade["tp"]:
                    hit, exitp = "tp", open_trade["tp"]
            else:
                if m5.high >= open_trade["sl"]:
                    hit, exitp = "sl", open_trade["sl"]
                elif m5.low <= open_trade["tp"]:
                    hit, exitp = "tp", open_trade["tp"]
            if hit:
                pnl = (exitp - open_trade["entry"]) if open_trade["side"] == "long" else (open_trade["entry"] - exitp)
                outcomes.append({
                    "t": open_trade["entry_time"],
                    "R": pnl / open_trade["sl_dist"],
                })
                open_trade = None
                ctx._position = None

        # 戦略実行
        ctx.pending_commands = []
        try:
            strat.on_bar(ctx)
        except Exception:
            pass

        # 発注処理
        if open_trade is None:
            for cmd in ctx.pending_commands:
                if cmd["type"] in ("buy", "sell"):
                    side = "long" if cmd["type"] == "buy" else "short"
                    entry = cmd.get("entry_price") or m5.close
                    sl = cmd.get("sl"); tp = cmd.get("tp")
                    sld = cmd.get("sl_dist") or (abs(entry - sl) if sl else 0)
                    if not sl or not tp or sld <= 0:
                        continue
                    open_trade = {"side": side, "entry": entry, "sl": sl, "tp": tp,
                                  "sl_dist": sld, "entry_time": m5.time}
                    ctx._position = side
                    break
    return outcomes


def stats(trades, months):
    if not trades:
        return dict(n=0, wr=0, sumR=0, pct=0)
    n = len(trades)
    w = sum(1 for x in trades if x["R"] > 0)
    sumR = sum(x["R"] for x in trades)
    return dict(n=n, wr=100 * w / n, sumR=sumR, pct=sumR / months * 0.01 * 100)


def fmt(s):
    if s["n"] == 0:
        return "n=  0"
    return f"n={s['n']:3d} WR={s['wr']:4.1f}% sumR={s['sumR']:+7.2f} ({s['pct']:+.2f}%/mo)"


def verdict(p1, p2):
    a, b = p1["sumR"] > 0, p2["sumR"] > 0
    return "[ROBUST]" if a and b else ("[Edge GONE]" if a and not b else ("[OOS+only]" if b else "[BAD both]"))


def main():
    base = Params()
    total_months = (P1_MONTHS + P2_MONTHS)
    # 省メモリ: ペアを 1 つずつロード → 全 variant を回す → R 結果だけ蓄積して破棄。
    acc = {name: {"p1": [], "p2": [], "pp": []} for name in VARIANTS}
    for p in PAIRS:
        ticks = load_ticks(p)
        print(f"  {p}: {len(ticks)} M5 ticks")
        for name, ov in VARIANTS.items():
            params = replace(base, skip_on_trendline_break=True, **ov)
            outs = run_pair(p, params, ticks)
            pp1 = [x for x in outs if P1_START <= x["t"] < P1_END]
            pp2 = [x for x in outs if P1_END <= x["t"] < P2_END]
            acc[name]["p1"] += pp1
            acc[name]["p2"] += pp2
            acc[name]["pp"].append((p, len(pp1), len(pp2),
                                    sum(x["R"] for x in pp1), sum(x["R"] for x in pp2)))
        del ticks

    print("\n" + "=" * 120)
    print("mtf_pullback v2 頻度緩和バリアント比較  (WF P1=21-23/P2=24-26)")
    print("=" * 120)
    for name in VARIANTS:
        r = acc[name]
        s1, s2 = stats(r["p1"], P1_MONTHS), stats(r["p2"], P2_MONTHS)
        ntot = s1["n"] + s2["n"]
        freq = ntot / (total_months / 12)
        robust_pairs = [pp[0] for pp in r["pp"] if pp[3] > 0 and pp[4] > 0]
        print(f"\n■ {name}")
        print(f"    P1 {fmt(s1)}")
        print(f"    P2 {fmt(s2)}   {verdict(s1, s2)}")
        print(f"    合計 {ntot} トレード / 5.5年 = {freq:.1f}/年 ({len(PAIRS)}ペア計)")
        print(f"    ROBUST ペア {len(robust_pairs)}/{len(PAIRS)}: {robust_pairs}")
        if len(PAIRS) <= 6:
            for (pn, n1, n2, r1, r2) in r["pp"]:
                v = "ROBUST" if r1 > 0 and r2 > 0 else ("Edge GONE" if r1 > 0 else ("OOS+" if r2 > 0 else "BAD"))
                print(f"      {pn:7s} P1 n={n1:3d} sumR={r1:+6.1f} ({r1/P1_MONTHS:+.2f})  "
                      f"P2 n={n2:3d} sumR={r2:+6.1f} ({r2/P2_MONTHS:+.2f})  [{v}]")
        else:
            print(f"    per-pair n={ {pp[0]: pp[1]+pp[2] for pp in r['pp']} }")
    print("\n" + "=" * 120)


if __name__ == "__main__":
    main()
