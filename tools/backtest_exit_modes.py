"""
backtest_exit_modes.py — エントリーを固定し、エグジット方式だけ変えて比較する。

エントリー = 現ベスト (mtf_pullback v2 + room_R<2.0)。SL リスクは常に 1R。
同じエントリー集合に対し、複数のエグジット方式を M5 足でシミュレートして
ウォークフォワード (P1/P2) で R ベース比較する。

エグジット方式:
  base       : 固定 TP=1.5R / SL=1R (現ライブ)
  BE@1R      : TP=1.5R、+1R 到達で SL を建値に
  part1_run15: +1R で半分利確、残りは建値 SL で TP=1.5R まで
  part1_run25: +1R で半分利確、残りは建値 SL で TP=2.5R まで
  trail1g1   : +1R 到達後、(最高益-1R) に SL をトレール (固定 TP なし)
  run3_be1   : TP=3.0R、+1R で建値 SL

使い方:
    python tools/backtest_exit_modes.py            # 主要4ペア
    python tools/backtest_exit_modes.py --all      # 全12ペア
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks  # noqa: E402

MAJORS = ["EURUSD", "USDJPY", "GBPUSD", "XAUUSD"]
ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]

P1_START = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P2_END = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
P1_MONTHS, P2_MONTHS = 36, 30
MAX_HORIZON = 2500   # 1 トレードが開いていられる M5 本数上限


def run_entries(symbol, ticks):
    """room_R<2.0 のエントリーを (idx, side, entry, sl, sld, time) で収集 + M5 配列を返す。"""
    m5_arr = [(int(t[0].time), t[0].high, t[0].low, t[0].close) for t in ticks]
    strat = MtfPullbackStrategy(Params(skip_on_trendline_break=True, tp_rr=1.5, room_R_max=2.0))
    strat.symbol = symbol
    ctx = SimContext()
    last_h1 = last_h4 = -1
    pos = False
    entries = []
    for i, (m5, h1, h4) in enumerate(ticks):
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60:
            ctx.bars_seq.pop(0)
        if h1 is not None and h1.time > last_h1:
            ctx.mtf[3600].append(h1); last_h1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 is not None and h4.time > last_h4:
            ctx.mtf[14400].append(h4); last_h4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        # ポジション中はエントリーしない。決済は exit sim 側で扱うので、ここでは
        # 「次のエントリーまで前のトレードが終わっている」近似で position をすぐ空ける必要がある。
        # 現実は同時 1 ポジ。簡易に「エントリー直後は cooldown 分あけ、重複は無視」とする。
        ctx._position = None
        ctx.pending_commands = []
        try:
            strat.on_bar(ctx)
        except Exception:
            pass
        for cmd in ctx.pending_commands:
            if cmd["type"] in ("buy", "sell"):
                side = "long" if cmd["type"] == "buy" else "short"
                entry = cmd.get("entry_price") or m5.close
                sl = cmd.get("sl")
                sld = abs(entry - sl) if sl else 0
                if not sl or sld <= 0:
                    continue
                # 直前トレードとオーバーラップしないよう、前回 idx から最低 1 本あける
                if entries and i <= entries[-1][0]:
                    continue
                entries.append((i, side, entry, sl, sld, int(m5.time)))
                break
    return entries, m5_arr


def sim_exit(entry_rec, m5_arr, mode):
    """1 エントリーに mode を適用し net R を返す (部分利確はブレンド R)。"""
    idx, side, entry, sl0, sld, _t = entry_rec
    tp_R = mode.get("tp_R", 1.5)
    be_at = mode.get("be_at_R", 0)
    p_at = mode.get("partial_at_R", 0)
    p_frac = mode.get("partial_frac", 0.5)
    p_run = mode.get("partial_runner_R", None)
    tr_start = mode.get("trail_start_R", 0)
    tr_gap = mode.get("trail_gap_R", 0)

    sl = sl0
    locked = 0.0
    size = 1.0
    peak_R = 0.0
    long = side == "long"
    tp_price = entry + tp_R * sld if long else entry - tp_R * sld

    end = min(idx + 1 + MAX_HORIZON, len(m5_arr))
    for j in range(idx + 1, end):
        _tm, hi, lo, cl = m5_arr[j]
        adverse = lo if long else hi
        favor = hi if long else lo
        # 1) SL ヒット (保守的に先に判定)
        if (long and adverse <= sl) or ((not long) and adverse >= sl):
            return locked + size * ((sl - entry) / sld if long else (entry - sl) / sld)
        # 2) 部分利確
        if p_at and size >= 0.999 and ((long and favor >= entry + p_at * sld) or
                                       ((not long) and favor <= entry - p_at * sld)):
            locked += p_frac * p_at
            size -= p_frac
            sl = entry                       # 残りは建値
            if p_run is not None:
                tp_price = entry + p_run * sld if long else entry - p_run * sld
        # 3) TP (ランナー含む) ヒット
        if (long and favor >= tp_price) or ((not long) and favor <= tp_price):
            return locked + size * ((tp_price - entry) / sld if long else (entry - tp_price) / sld)
        # 4) 建値ストップ
        if be_at and ((long and favor >= entry + be_at * sld) or
                      ((not long) and favor <= entry - be_at * sld)):
            sl = max(sl, entry) if long else min(sl, entry)
        # 5) トレーリング
        if tr_start:
            fav_R = (favor - entry) / sld if long else (entry - favor) / sld
            if fav_R >= tr_start:
                peak_R = max(peak_R, fav_R)
                new_sl = entry + (peak_R - tr_gap) * sld if long else entry - (peak_R - tr_gap) * sld
                sl = max(sl, new_sl) if long else min(sl, new_sl)
    # ホライズン終了: 最終 close で時価決済
    _tm, hi, lo, cl = m5_arr[end - 1]
    return locked + size * ((cl - entry) / sld if long else (entry - cl) / sld)


MODES = {
    "base (TP1.5/SL1)": {"tp_R": 1.5},
    "BE@1R": {"tp_R": 1.5, "be_at_R": 1.0},
    "part1_run15": {"partial_at_R": 1.0, "partial_frac": 0.5, "partial_runner_R": 1.5, "tp_R": 1.5},
    "part1_run25": {"partial_at_R": 1.0, "partial_frac": 0.5, "partial_runner_R": 2.5, "tp_R": 99},
    "trail1g1": {"tp_R": 99, "trail_start_R": 1.0, "trail_gap_R": 1.0},
    "run3_be1": {"tp_R": 3.0, "be_at_R": 1.0},
}


def wf(rs_with_t):
    p1 = [r for (r, t) in rs_with_t if P1_START <= t < P1_END]
    p2 = [r for (r, t) in rs_with_t if P1_END <= t < P2_END]
    return p1, p2


def fmt(rs, months):
    if not rs:
        return "n=0"
    n = len(rs)
    w = sum(1 for r in rs if r > 0)
    s = sum(rs)
    return f"n={n:3d} WR={100*w/n:4.1f}% sumR={s:+7.1f} ({s/months*1:+.2f}%/mo) exp={s/n:+.3f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    pairs = ALL if args.all else MAJORS

    all_entries = {}
    for p in pairs:
        ticks = load_ticks(p)
        ent, m5 = run_entries(p, ticks)
        all_entries[p] = (ent, m5)
        print(f"  {p}: {len(ent)} entries")

    print("\n" + "=" * 96)
    print(f"エグジット方式比較 ({'全12' if args.all else '主要4'}ペア)  "
          f"エントリー=v2+room_R<2.0 固定、SL=1R。WF P1=21-23/P2=24-26")
    print("=" * 96)
    for name, mode in MODES.items():
        rs = []
        for p in pairs:
            ent, m5 = all_entries[p]
            for e in ent:
                rs.append((sim_exit(e, m5, mode), e[5]))
        p1, p2 = wf(rs)
        v = "[ROBUST]" if sum(p1) > 0 and sum(p2) > 0 else ("[Edge GONE]" if sum(p1) > 0 else "[BAD]")
        print(f"\n■ {name}")
        print(f"   P1 {fmt(p1, P1_MONTHS)}")
        print(f"   P2 {fmt(p2, P2_MONTHS)}   {v}")


if __name__ == "__main__":
    main()
