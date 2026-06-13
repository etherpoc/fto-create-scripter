"""
axiory_mtf.py — mtf_pullback を実Axioryデータ(M5+H1+H4)で回し、breakoutとの相関/合算DDを実測。

問い: 実データで「breakout単体」 vs 「breakout + MTF」 — MTFの無相関DD圧縮はまだ効くか?
MTFの自前リターンは小さい(narrow)が、平均回帰×トレンドの無相関で合算DDを削れるなら併用の価値あり。

Axiory M5 を 1 本ずつ MtfPullbackStrategy に流し、H1/H4 は同ペアの集計を時刻同期で供給(ルックアヘッド無)。
net=コミ+spread。WF: OOS=2015-20 / IS=2021-26。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.strategy_base import Bar                              # noqa: E402
from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy  # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext              # noqa: E402
import tools.axiory_data as ax                                     # noqa: E402
from tools.axiory_validate import pair_net as bo_net, BREAKOUT5, eq_overlay  # noqa: E402

SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
SPREAD = 0.5
JPY3 = ["USDJPY", "GBPJPY", "EURJPY"]
PB = dict(skip_on_trendline_break=True, tp_rr=1.5, align_tfs="h1,m15",
          room_R_max=2.0, block_hour_start=6, block_hour_end=10, min_sl_dist_pips=20.0)


def _pip(p): return 0.01 if "JPY" in p else (0.1 if "XAU" in p else 0.0001)
def _comm(p):
    if p.endswith("JPY"): return 1.8
    if p.startswith("USD"): return 1.5
    if "XAU" in p: return 0.5
    return 1.2


def _arr_bars(arr):
    t, o, h, l, c = arr
    return t, o, h, l, c


def pullback_net(pair):
    """Axiory M5 を流して MtfPullback の net トレード [(t, nR)] を返す。"""
    m5 = ax.cached_arrays(pair, "m5")
    h1 = ax.cached_arrays(pair, "h1")
    h4 = ax.cached_arrays(pair, "h4")
    t5, o5, h5, l5, c5 = m5
    t1 = h1[0]; t4 = h4[0]
    strat = MtfPullbackStrategy(Params(**PB)); strat.symbol = pair
    ctx = SimContext()
    i1 = 0; i4 = 0; ot = None; tr = []
    ps = _pip(pair); cst = _comm(pair) + SPREAD
    N = len(t5)
    for k in range(N):
        tm = int(t5[k])
        bar = Bar(time=tm, open=float(o5[k]), high=float(h5[k]), low=float(l5[k]), close=float(c5[k]))
        ctx.bars_seq.append(bar)
        if len(ctx.bars_seq) > 60:
            ctx.bars_seq.pop(0)
        # H1: 完了済み(end<=tm)の最新を供給。H1バーは始端時刻なので end=t1[i]+3600<=tm
        while i1 < len(t1) and t1[i1] + 3600 <= tm:
            ctx.mtf[3600].append(Bar(time=int(t1[i1]), open=float(h1[1][i1]), high=float(h1[2][i1]),
                                     low=float(h1[3][i1]), close=float(h1[4][i1])))
            if len(ctx.mtf[3600]) > 300: ctx.mtf[3600].pop(0)
            i1 += 1
        while i4 < len(t4) and t4[i4] + 14400 <= tm:
            ctx.mtf[14400].append(Bar(time=int(t4[i4]), open=float(h4[1][i4]), high=float(h4[2][i4]),
                                      low=float(h4[3][i4]), close=float(h4[4][i4])))
            if len(ctx.mtf[14400]) > 300: ctx.mtf[14400].pop(0)
            i4 += 1
        # 決済
        if ot:
            hit = ep = None
            if ot["side"] == "long":
                if bar.low <= ot["sl"]: hit, ep = 1, ot["sl"]
                elif bar.high >= ot["tp"]: hit, ep = 1, ot["tp"]
            else:
                if bar.high >= ot["sl"]: hit, ep = 1, ot["sl"]
                elif bar.low <= ot["tp"]: hit, ep = 1, ot["tp"]
            if hit:
                pnl = (ep - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - ep)
                R = pnl / ot["sld"]; sld_pips = ot["sld"] / ps
                tr.append((ot["t"], R - cst / max(sld_pips, 1e-9)))
                ot = None; ctx._position = None
        ctx.pending_commands = []
        try: strat.on_bar(ctx)
        except Exception: pass
        if ot is None:
            for cmd in ctx.pending_commands:
                if cmd["type"] in ("buy", "sell"):
                    side = "long" if cmd["type"] == "buy" else "short"
                    e = cmd.get("entry_price") or bar.close; sl = cmd.get("sl"); tp = cmd.get("tp")
                    sld = cmd.get("sl_dist") or (abs(e - sl) if sl else 0)
                    if not sl or not tp or sld <= 0: continue
                    ot = {"side": side, "entry": e, "sl": sl, "tp": tp, "sld": sld, "t": tm}
                    ctx._position = side; break
    return tr


def monthly(trades):
    m = {}
    for (t, r) in trades:
        k = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")
        m[k] = m.get(k, 0.0) + r
    return m


def corr(a, b):
    ks = sorted(set(a) | set(b)); x = np.array([a.get(k, 0.0) for k in ks]); y = np.array([b.get(k, 0.0) for k in ks])
    return float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else 0.0


def dd_split(trades):
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    def seg(mask):
        e = np.cumsum(r[mask]); return float((np.maximum.accumulate(e) - e).max()) if len(e) else 0.0, float(r[mask].sum())
    dd_all = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    d1, p1 = seg(t < SPLIT); d2, p2 = seg(t >= SPLIT)
    return dd_all, d1, p1, d2, p2


def main():
    print("=" * 100)
    print("実Axiory: breakout単体 vs breakout+MTF — 無相関DD圧縮はまだ効くか (1R=1%/pair, OOS/IS)")
    print("=" * 100)
    # breakout: robust5、 pullback: JPY3
    bo = []
    for p in BREAKOUT5:
        bo += bo_net(p)
    pb = []
    for p in JPY3:
        print(f"  MTF {p} 計算中...", flush=True)
        pb += pullback_net(p)

    bo_dd, bd1, bp1, bd2, bp2 = dd_split(bo)
    pb_dd, pd1, pp1, pd2, pp2 = dd_split(pb)
    print(f"\n  breakout5 単体 : OOS sumR{bp1:>+6.1f}(DD{bd1:.0f}) IS sumR{bp2:>+6.1f}(DD{bd2:.0f})  全DD{bo_dd:.0f}")
    print(f"  MTF JPY3 単体  : OOS sumR{pp1:>+6.1f}(DD{pd1:.0f}) IS sumR{pp2:>+6.1f}(DD{pd2:.0f})  全DD{pb_dd:.0f}")
    print(f"  月次相関(BO vs MTF): {corr(monthly(bo), monthly(pb)):+.2f}")

    comb = bo + pb
    c_dd, cd1, cp1, cd2, cp2 = dd_split(comb)
    print(f"\n  ★合算         : OOS sumR{cp1:>+6.1f}(DD{cd1:.0f}) IS sumR{cp2:>+6.1f}(DD{cd2:.0f})  全DD{c_dd:.0f}")
    print(f"  DD: 単体和 {bo_dd+pb_dd:.0f} → 合算 {c_dd:.0f}  ({100*(1-c_dd/(bo_dd+pb_dd)):.0f}% 圧縮)")
    eff_bo = (bp1 + bp2) / bo_dd; eff_c = (cp1 + cp2) / c_dd
    print(f"  効率(sumR/DD): breakout単体 {eff_bo:.2f} → 合算 {eff_c:.2f}  ({'改善' if eff_c > eff_bo else '悪化/不変'})")
    # overlay も
    cov = eq_overlay(comb); ov_dd, _, ovp1, _, ovp2 = dd_split(cov)
    print(f"  ★合算+overlay : OOS sumR{ovp1:>+6.1f} IS sumR{ovp2:>+6.1f}  全DD{ov_dd:.0f}  効率{(ovp1+ovp2)/ov_dd:.2f}")


if __name__ == "__main__":
    main()
