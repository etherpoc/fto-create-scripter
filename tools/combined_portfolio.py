"""
combined_portfolio.py — H1ブレイクアウト(トレンド) + JPY3プルバック(平均回帰) の合算。

2戦略は機序が逆(トレンド追従 vs 平均回帰)=無相関のはず。合算で
レジーム反転をヘッジし、リターン/DD効率が上がるかを実測。月次相関も出す。

net = 往復コミ + spread0.5p。1R=口座1%。WF: P1=21-23 / P2=24-26。
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from tools.bo_fast import cached_arrays, run_bo_fast              # noqa
from tools.breakout_lab import pip as bpip, comm as bcomm         # noqa
from tools.backtest_net_minsl import capture as pb_capture, net_R as pb_net  # noqa

P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
SPREAD = 0.5

# ブレイクアウト: H1 SL3/SMA150(7ペア分散版)
BO_CFG = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150, direction="long")
BO_PAIRS = ["XAUUSD", "USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "USDCHF"]
# プルバック: JPY3
PB_PAIRS = ["USDJPY", "GBPJPY", "EURJPY"]


def breakout_trades(risk):
    out = []
    for s in BO_PAIRS:
        tr = run_bo_fast(cached_arrays(s, "h1"), BO_CFG)
        c = bcomm(s) + SPREAD; ps = bpip(s)
        n1 = sum(t["R"] - c*t["units"]/max(t["sld"]/ps,1e-9) for t in tr if t["t"] < P1_END)
        n2 = sum(t["R"] - c*t["units"]/max(t["sld"]/ps,1e-9) for t in tr if t["t"] >= P1_END)
        if not (n1 > 0 and n2 > 0):  # viable のみ
            continue
        for t in tr:
            nR = t["R"] - c*t["units"]/max(t["sld"]/ps,1e-9)
            out.append({"t": t["t"], "pnl": nR * risk, "strat": "BO"})
    return out


def pullback_trades(risk):
    out = []
    for s in PB_PAIRS:
        tr = pb_capture(s, 20)  # minSL20, block-on
        for t in tr:
            out.append({"t": t["t"], "pnl": pb_net(t) * risk, "strat": "PB"})
    return out


def dd_curve(trades):
    ev = sorted(trades, key=lambda x: x["t"]); eq = peak = dd = 0.0
    for t in ev:
        eq += t["pnl"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    return eq, dd


def split(trades):
    return (sum(t["pnl"] for t in trades if t["t"] < P1_END),
            sum(t["pnl"] for t in trades if t["t"] >= P1_END))


def monthly(trades):
    m = {}
    for t in trades:
        k = datetime.fromtimestamp(t["t"], tz=timezone.utc).strftime("%Y-%m")
        m[k] = m.get(k, 0.0) + t["pnl"]
    return m


def corr(a, b):
    keys = sorted(set(a) | set(b))
    xs = [a.get(k, 0.0) for k in keys]; ys = [b.get(k, 0.0) for k in keys]
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))/n
    sx = (sum((x-mx)**2 for x in xs)/n) ** 0.5; sy = (sum((y-my)**2 for y in ys)/n) ** 0.5
    return cov/(sx*sy) if sx*sy > 0 else 0


def report(name, trades):
    p1, p2 = split(trades); eq, dd = dd_curve(trades)
    yr = ((1 + (eq/66)/100)**12 - 1)*100
    print(f"  {name:<22}: P1{p1/36:>+6.2f}% P2{p2/30:>+6.2f}%/月  累積{eq:>+7.1f}%  DD{dd:>5.1f}%  年複利{yr:>+6.1f}%")
    return eq, dd


def main():
    # サイジング: BO 0.25%/pair, PB 0.5%/pair (各単体でDD~9%付近)
    rb, rp = 0.25, 0.5
    bo = breakout_trades(rb)
    pb = pullback_trades(rp)
    print("=" * 92)
    print(f"合算ポートフォリオ: H1ブレイクアウト(BO {rb}%/pair×{len(BO_PAIRS)}) + JPY3プルバック(PB {rp}%/pair×3)")
    print("=" * 92)
    eqb, ddb = report("ブレイクアウト単体", bo)
    eqp, ddp = report("プルバック単体", pb)
    comb = bo + pb
    eqc, ddc = report("★合算", comb)
    print("-" * 92)
    mc = corr(monthly(bo), monthly(pb))
    print(f"  月次リターン相関(BO vs PB): {mc:+.2f}  ({'無相関〜負=分散効果大' if mc < 0.3 else '相関あり=効果薄'})")
    print(f"  DD: 単体和 {ddb+ddp:.1f}% → 合算 {ddc:.1f}%  (分散で {100*(1-ddc/(ddb+ddp)):.0f}% 圧縮)")
    print(f"  効率(累積/DD): BO {eqb/ddb:.1f} / PB {eqp/ddp:.1f} / 合算 {eqc/ddc:.1f}")
    # 年別(合算)
    years = {}
    for t in comb:
        y = datetime.fromtimestamp(t["t"], tz=timezone.utc).year
        years[y] = years.get(y, 0.0) + t["pnl"]
    print(f"  合算 年別: " + " ".join(f"{y}:{years[y]:+.0f}" for y in sorted(years)) +
          f"  (マイナス年 {[y for y in years if years[y]<0] or 'なし'})")


if __name__ == "__main__":
    main()
