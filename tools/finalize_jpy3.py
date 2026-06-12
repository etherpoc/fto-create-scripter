"""
finalize_jpy3.py — JPY3 basket (USDJPY/GBPJPY/EURJPY) の運用設定を確定する。

JPYペアは対円相関が高く同時逆行で合成DDが重なる。全ペアを時間統合した
ポートフォリオの net 合成DDを実測し、プロップ(総DD<10%)に収まる per-pair risk を決める。
さらに「円露出キャップ」(同方向の対円ポジ同時保有を制限)でDDを下げられるか検証。

コスト対応 net: block ON + minSL20 + コミ往復$12/lot + spread0.5p。
net_R = gross_R - cost_pips/sl_pips。決済時刻順に net_R×per_pos を積んで合成DD。

使い方:
    python tools/finalize_jpy3.py
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks         # noqa: E402

JPY3 = ["USDJPY", "GBPJPY", "EURJPY"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30
SPREAD = 0.5


def pip(p): return 0.01 if "JPY" in p else 0.0001
def comm(p): return 1.8 if p.endswith("JPY") else 1.2


def capture(symbol):
    pr = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15", room_R_max=2.0,
              block_hour_start=6, block_hour_end=10, min_sl_dist_pips=20)
    s = MtfPullbackStrategy(Params(**pr)); s.symbol = symbol
    ctx = SimContext(); lh1 = lh4 = -1; ot = None; tr = []
    for (m5, h1, h4) in load_ticks(symbol):
        ctx.bars_seq.append(m5)
        if len(ctx.bars_seq) > 60: ctx.bars_seq.pop(0)
        if h1 and h1.time > lh1: ctx.mtf[3600].append(h1); lh1 = h1.time; ctx.mtf[3600] = ctx.mtf[3600][-300:]
        if h4 and h4.time > lh4: ctx.mtf[14400].append(h4); lh4 = h4.time; ctx.mtf[14400] = ctx.mtf[14400][-300:]
        if ot:
            hit = ep = None
            if ot["side"] == "long":
                if m5.low <= ot["sl"]: hit, ep = 1, ot["sl"]
                elif m5.high >= ot["tp"]: hit, ep = 1, ot["tp"]
            else:
                if m5.high >= ot["sl"]: hit, ep = 1, ot["sl"]
                elif m5.low <= ot["tp"]: hit, ep = 1, ot["tp"]
            if hit:
                pn = (ep - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - ep)
                gR = pn / ot["sld"]; spips = ot["sld"] / pip(symbol)
                nR = gR - (comm(symbol) + SPREAD) / max(spips, 1e-9)
                tr.append({"entry_t": ot["entry_t"], "exit_t": int(m5.time), "nR": nR,
                           "side": ot["side"], "pair": symbol})
                ot = None; ctx._position = None
        ctx.pending_commands = []
        try: s.on_bar(ctx)
        except Exception: pass
        if ot is None:
            for c in ctx.pending_commands:
                if c["type"] in ("buy", "sell"):
                    sd = "long" if c["type"] == "buy" else "short"; sl, tp = c.get("sl"), c.get("tp")
                    e = c.get("entry_price") or m5.close
                    if not sl or not tp or abs(e-sl) <= 0: continue
                    ot = {"side": sd, "entry": e, "sl": sl, "tp": tp, "sld": abs(e-sl),
                          "entry_t": int(m5.time)}
                    ctx._position = sd; break
    return tr


def portfolio(trades, per_pos, jpy_cap=0.0):
    """円露出キャップ付きポートフォリオ。jpy_cap>0 なら net円露出が cap 超のエントリーをブロック。
       long XXXJPY = 円ショート(-1)、short = 円ロング(+1)。"""
    trades = sorted(trades, key=lambda x: x["entry_t"])
    openp = []  # (exit_t, jpy_dir)
    acc = []
    for t in trades:
        tm = t["entry_t"]
        openp = [o for o in openp if o[0] > tm]
        jdir = -1 if t["side"] == "long" else 1
        if jpy_cap > 0:
            net = sum(o[1] for o in openp) + jdir
            if abs(net) * per_pos > jpy_cap + 1e-9:
                continue  # 円露出オーバー → ブロック
        acc.append(t)
        openp.append((t["exit_t"], jdir))
    return acc


def equity_dd(trades, per_pos):
    ev = sorted(trades, key=lambda x: x["exit_t"])
    eq = peak = dd = 0.0
    for t in ev:
        eq += t["nR"] * per_pos
        peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def daily_worst(trades, per_pos):
    by = {}
    for t in trades:
        d = datetime.fromtimestamp(t["exit_t"], tz=timezone.utc).strftime("%Y-%m-%d")
        by[d] = by.get(d, 0.0) + t["nR"] * per_pos
    return min(by.values()) if by else 0.0


def split(trades, per_pos):
    p1 = sum(t["nR"] for t in trades if t["entry_t"] < P1_END) * per_pos
    p2 = sum(t["nR"] for t in trades if t["entry_t"] >= P1_END) * per_pos
    return p1, p2


def main():
    allt = []
    for p in JPY3:
        t = capture(p); allt += t
        print(f"  {p}: {len(t)} trades", flush=True)

    print("\n" + "=" * 92)
    print("JPY3 basket (USDJPY/GBPJPY/EURJPY) net合成 — per-pair risk sweep")
    print("プロップ合格 = 総DD<10% & 日次>-5%。net=実コスト込み。1R=口座1%。")
    print("=" * 92)
    print(f"{'risk/pair':>9} | {'円cap':>6} | {'採用':>5} | {'net P1/月':>9} {'net P2/月':>9} | "
          f"{'年複利':>7} | {'合成DD':>7} | {'最悪日':>7} | 合否")
    print("-" * 92)
    for cap in [0.0, 2.0]:   # 円露出キャップ無し / 2%(=同方向2枚相当)
        for pp in [0.5, 0.7, 0.85, 1.0, 1.25]:
            sub = portfolio(allt, pp, cap)
            p1, p2 = split(sub, pp)
            eq, dd = equity_dd(sub, pp)
            wd = daily_worst(sub, pp)
            m2 = p2 / P2_M
            yr = ((1 + m2/100) ** 12 - 1) * 100
            ok = "PASS" if (dd < 10 and wd > -5) else "FAIL"
            caps = "なし" if cap == 0 else f"{cap:.0f}%"
            print(f"  {pp:>6.2f}% | {caps:>6} | {len(sub):>5} | {p1/P1_M:>+8.2f}% {p2/P2_M:>+8.2f}% | "
                  f"{yr:>+6.1f}% | {dd:>6.1f}% | {wd:>+6.1f}% | {ok}")
        print("-" * 92)


if __name__ == "__main__":
    main()
