"""
mtf_net_lab.py — mtf_pullback v2 の主要レバーを **net・全12ペア・WF** で再検証し、
精度向上と汎用化(viableペアを増やす)を狙う。過去の調整の多くは gross だったため net で測り直す。

net_R = gross_R - (comm_pips+spread)/sl_dist_pips。1R=口座1%。WF: P1=21-23/P2=24-26。
overlay = エクイティカーブ・デリスク(集約equityが直近K件MA割れでロット半減)を任意で適用。

  python tools/mtf_net_lab.py            # 主要レバー sweep
  python tools/mtf_net_lab.py --set align
"""
from __future__ import annotations
import sys, argparse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.mtf_pullback.strategy import Params, MtfPullbackStrategy   # noqa: E402
from tools.backtest_mtf_pb_variants import SimContext, load_ticks, run_pair  # noqa: E402

ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36.0, 30.0
SPREAD = 0.5
_ticks = {}


def pip_size(p): return 0.01 if "JPY" in p else (0.1 if "XAU" in p else 0.0001)
def comm_pips(p):
    if p.endswith("JPY"): return 1.8
    if p.startswith("USD"): return 1.5
    if "XAU" in p: return 0.5
    return 1.2


def capture(pair, ov):
    """params override ov で 1 ペアを回し、(t, R, sld_pips) のリストを返す。"""
    if pair not in _ticks:
        _ticks[pair] = load_ticks(pair)
    params = replace(Params(), skip_on_trendline_break=True, **ov)
    # run_pair は {t,R} のみ返すので sld を得るため自前で軽く回す
    strat = MtfPullbackStrategy(params); strat.symbol = pair
    ctx = SimContext(); lh1 = lh4 = -1; ot = None; tr = []
    ps = pip_size(pair)
    for (m5, h1, h4) in _ticks[pair]:
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
                pnl = (ep - ot["entry"]) if ot["side"] == "long" else (ot["entry"] - ep)
                tr.append((ot["t"], pnl / ot["sld"], ot["sld"] / ps)); ot = None; ctx._position = None
        ctx.pending_commands = []
        try: strat.on_bar(ctx)
        except Exception: pass
        if ot is None:
            for c in ctx.pending_commands:
                if c["type"] in ("buy", "sell"):
                    side = "long" if c["type"] == "buy" else "short"
                    e = c.get("entry_price") or m5.close; sl, tp = c.get("sl"), c.get("tp")
                    sld = c.get("sl_dist") or (abs(e - sl) if sl else 0)
                    if not sl or not tp or sld <= 0: continue
                    ot = {"side": side, "entry": e, "sl": sl, "tp": tp, "sld": sld, "t": int(m5.time)}
                    ctx._position = side; break
    return tr


def eq_overlay(trades, K=20, m=0.5):
    trades = sorted(trades, key=lambda x: x[0]); eq = 0.0; hist = []; out = []
    for (t, r) in trades:
        mult = 1.0
        if len(hist) >= K and eq < sum(hist[-K:]) / K: mult = m
        p = r * mult; eq += p; hist.append(eq); out.append((t, p))
    return out


def eval_variant(ov, overlay=False, pairs=ALL):
    """net 集約。返り値: dict(stats) と viableペアリスト。"""
    allnet = []; per = {}
    for p in pairs:
        tr = capture(p, ov)
        cst = comm_pips(p) + SPREAD
        nets = [(t, R - cst / max(sld, 1e-9)) for (t, R, sld) in tr]
        n1 = sum(r for (t, r) in nets if t < P1_END); n2 = sum(r for (t, r) in nets if t >= P1_END)
        per[p] = (len(nets), n1, n2)
        allnet += nets
    if overlay:
        allnet = eq_overlay(allnet)
    if not allnet:
        return None
    t = np.array([x[0] for x in allnet]); r = np.array([x[1] for x in allnet])
    n = len(r); w = int((r > 0).sum())
    p1 = float(r[t < P1_END].sum()); p2 = float(r[t >= P1_END].sum())
    o = np.argsort(t); eq = np.cumsum(r[o]); dd = float((np.maximum.accumulate(eq) - eq).max())
    viable = [p for p, (nn, a, b) in per.items() if a > 0 and b > 0]
    return dict(n=n, wr=100 * w / n, p1=p1, p2=p2, dd=dd, viable=viable, per=per)


def show(label, ov, overlay=False, pairs=ALL):
    s = eval_variant(ov, overlay, pairs)
    if not s:
        print(f"  {label:<34} | 0"); return
    freq = s["n"] / 5.5
    mar = (s["p1"] + s["p2"]) / s["dd"] if s["dd"] > 0 else 0
    print(f"  {label:<34} | N{s['n']:>4}({freq:>4.0f}/年) WR{s['wr']:>4.1f}% | "
          f"P1{s['p1']/P1_M:>+5.2f}% P2{s['p2']/P2_M:>+5.2f}%/月 DD{s['dd']:>4.1f} MAR{mar:>4.1f} | "
          f"viable {len(s['viable'])}/12")
    print(f"      {s['viable']}", flush=True)
    return s


_BASE = dict(tp_rr=1.5, align_tfs="h1,m15", room_R_max=2.0, block_hour_start=6, block_hour_end=10, min_sl_dist_pips=20.0)


def variant_sets(name):
    if name == "base":
        return [("現行production(h1m15/RR1.5/room2/skip6-10/minSL20)", _BASE, False)]
    if name == "overlay":
        return [("production", _BASE, False),
                ("production +overlay", _BASE, True)]
    if name == "rr":
        return [(f"RR{rr}{' +ov' if ov else ''}", {**_BASE, "tp_rr": rr}, ov)
                for rr in (1.5, 2.0, 2.5) for ov in (False, True)]
    if name == "align":
        return [(f"{a}{' +ov' if ov else ''}", {**_BASE, "align_tfs": a}, ov)
                for a in ("h1,m15", "h4,m15", "h4,h1,m15") for ov in (False, True)]
    if name == "minsl":
        return [(f"minSL{mn}", {**_BASE, "min_sl_dist_pips": mn}, False) for mn in (0, 15, 20, 25, 30)]
    if name == "room":
        return [(f"room_R<{rm}", {**_BASE, "room_R_max": rm}, False) for rm in (1.5, 2.0, 2.5, 3.0)]
    return [("production", _BASE, False)]


def _pp(ov):
    out = {}
    for p in ALL:
        tr = capture(p, ov); cst = comm_pips(p) + SPREAD
        nets = [(t, R - cst / max(sld, 1e-9)) for (t, R, sld) in tr]
        if not nets:
            out[p] = (0, 0, 0, 0); continue
        n1 = sum(r for (t, r) in nets if t < P1_END); n2 = sum(r for (t, r) in nets if t >= P1_END)
        w = sum(1 for (t, r) in nets if r > 0)
        out[p] = (len(nets), 100 * w / len(nets), n1 / P1_M, n2 / P2_M)
    return out


def detail():
    """h1,m15(production) vs h4,h1,m15(厳格) の per-pair net %/月を並べる。"""
    A = _pp(_BASE)                                   # h1,m15
    B = _pp({**_BASE, "align_tfs": "h4,h1,m15"})     # h4,h1,m15
    print("\n■ per-pair net %/月  [h1,m15] vs [h4,h1,m15]  ★両期間+ △片側+")
    print(f"    {'pair':<7} | {'h1m15 P1/P2':<18} | {'h4h1m15 P1/P2':<18}")
    def mk(v):
        n, wr, m1, m2 = v
        s = "★" if (m1 > 0 and m2 > 0) else ("△" if (m1 > 0 or m2 > 0) else " ")
        return f"{s}N{n:>3} {m1:>+5.2f}/{m2:>+5.2f}"
    for p in sorted(ALL, key=lambda x: -(B[x][2] + B[x][3])):
        print(f"    {p:<7} | {mk(A[p]):<18} | {mk(B[p]):<18}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="all")
    args = ap.parse_args()
    print("=" * 116)
    print(f"mtf_pullback net 全12ペア WF lab [{args.set}]  (net=コミ+spread, 1R=1%, P1=21-23/P2=24-26)")
    print("=" * 116)
    if args.set == "detail":
        detail(); return
    if args.set == "jpy3":
        JPY3 = ["USDJPY", "GBPJPY", "EURJPY"]
        R3 = ["USDJPY", "EURJPY", "EURUSD"]
        STRICT = {**_BASE, "align_tfs": "h4,h1,m15"}
        print("\n--- [deployed JPY3 = USDJPY/GBPJPY/EURJPY] ---")
        show("JPY3 production", _BASE, False, JPY3)
        show("JPY3 production +overlay", _BASE, True, JPY3)
        show("JPY3 strict-align(h4h1m15)", STRICT, False, JPY3)
        show("JPY3 strict-align +overlay", STRICT, True, JPY3)
        print("\n--- [pythonNet robust3 = USDJPY/EURJPY/EURUSD] ---")
        show("R3 production", _BASE, False, R3)
        show("R3 production +overlay", _BASE, True, R3)
        return
    sets = ["overlay", "rr", "align", "minsl", "room"] if args.set == "all" else [args.set]
    for s in sets:
        print(f"\n--- [{s}] ---")
        for (label, ov, overlay) in variant_sets(s):
            show(label, ov, overlay)


if __name__ == "__main__":
    main()
