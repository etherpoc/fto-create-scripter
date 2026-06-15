"""
verify_short_sleeve.py — breakout_h1.mq5 のショート経路を実機前にオフライン検証。

CLAUDE.md 鉄則#4: ユーザに MT5 で動かしてもらう前に、全パイプラインをオフラインで通す。
MT5 は Claude から実行できないので、EA の「確定足ロジック」を Python で bit-faithful に再現し、
  (A) ショートが研究(run_bo_fast direction=short)と整合する net を出すか
  (B) long + 0.4×short スリーブが axiory_longshort の改善を再現するか
を確認する。EA と完全一致を狙うのは entry=次足始値 / SL=ブローカー側 の約定規約まで写すこと。

EA 規約(breakout_h1.mq5)を忠実に写経:
  - 指標は shift2..n+1 (確定シグナル足 shift1 の手前 n 本)。DonchHigh/Low, SmaPrev, AtrPrev。
  - flat時: shift1 が Donchian ブレイク & SMA 同方向 → 次足始値で成行。SL = fill ∓ sl_atr×ATR。
  - 保有時: 確定足が DonchLow/High(exit_n) トレール割れ → 次足始値で決済。SL はザラ場で約定。
  - 同足 SL/トレール両成立は SL 優先(保守)。

  python tools/verify_short_sleeve.py
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.axiory_data import cached_arrays           # noqa: E402
from tools.backtest_breakout import pip as bpip, comm as bcomm  # noqa: E402

SPREAD = 0.5
SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
BO7 = ["XAUUSD", "USDJPY", "EURJPY", "AUDJPY", "GBPJPY", "CHFJPY", "NZDJPY"]
P = dict(entry_n=30, exit_n=25, atr_n=20, sl_atr=3.0, sma_n=150)  # EA 既定 = 研究 BO


def ea_trades(t, o, h, l, c, direction):
    """breakout_h1.mq5 の確定足ロジックを忠実に再現。trades=[dict(t,R,sld,units=1)]。"""
    en, ex, an, sl_atr, sma_n = P["entry_n"], P["exit_n"], P["atr_n"], P["sl_atr"], P["sma_n"]
    N = len(c)
    warm = max(en, an, sma_n) + 2
    trades = []
    pos = None
    i = warm
    while i < N - 1:
        # --- 指標(shift2..n+1 = bars i-n .. i-1) ---
        dhi = h[i - en:i].max()
        dlo = l[i - en:i].min()
        sma = c[i - sma_n:i].mean()
        # ATR: TR(j) over j=i-an..i-1
        s = 0.0
        for j in range(i - an, i):
            s += max(h[j] - l[j], abs(h[j] - c[j - 1]), abs(l[j] - c[j - 1]))
        atr = s / an
        if atr <= 0:
            i += 1
            continue
        if pos is None:
            # shift1 = bar i がブレイク → 次足 i+1 始値で約定
            long_ok = (h[i] > dhi) and (c[i] > sma) and direction in ("long", "both")
            short_ok = (l[i] < dlo) and (c[i] < sma) and direction in ("short", "both")
            if long_ok or short_ok:
                side = 1 if long_ok else -1
                entry = o[i + 1]
                sl = entry - sl_atr * atr if side == 1 else entry + sl_atr * atr
                sld = abs(entry - sl)
                pos = dict(side=side, entry=entry, sl=sl, sld=sld, i=i)
            i += 1
            continue
        # --- 保有中: bar i が確定。トレール/SL 判定 ---
        side = pos["side"]
        exit_price = None
        if side == 1:
            if l[i] <= pos["sl"]:
                exit_price = pos["sl"]
            else:
                trail = l[i - ex:i].min()
                if l[i] <= trail:
                    exit_price = o[i + 1]
        else:
            if h[i] >= pos["sl"]:
                exit_price = pos["sl"]
            else:
                trail = h[i - ex:i].max()
                if h[i] >= trail:
                    exit_price = o[i + 1]
        if exit_price is not None:
            pnl = (exit_price - pos["entry"]) if side == 1 else (pos["entry"] - exit_price)
            trades.append(dict(t=t[pos["i"]], R=pnl / pos["sld"], sld=pos["sld"]))
            pos = None
        i += 1
    return trades


def stop_trades(t, o, h, l, c, direction, slip_pips, ps):
    """逆指値(ストップ)約定版: ブレイク当足でレベル±slipで約定(=研究のレベル約定にslip付与)。
    現EAは成行(ea_trades)。EAをストップ注文に変えた場合の上限見積り用。"""
    en, ex, an, sl_atr, sma_n = P["entry_n"], P["exit_n"], P["atr_n"], P["sl_atr"], P["sma_n"]
    N = len(c); warm = max(en, an, sma_n) + 2; trades = []; pos = None; slip = slip_pips * ps
    for i in range(warm, N):
        dhi = h[i - en:i].max(); dlo = l[i - en:i].min(); sma = c[i - sma_n:i].mean()
        s = 0.0
        for j in range(i - an, i):
            s += max(h[j] - l[j], abs(h[j] - c[j - 1]), abs(l[j] - c[j - 1]))
        atr = s / an
        if atr <= 0:
            continue
        if pos is None:
            lo = (h[i] > dhi) and (c[i] > sma) and direction in ("long", "both")
            so = (l[i] < dlo) and (c[i] < sma) and direction in ("short", "both")
            if lo or so:
                side = 1 if lo else -1
                e = (dhi + slip) if side == 1 else (dlo - slip)   # ストップは不利方向にslip
                sl = e - sl_atr * atr if side == 1 else e + sl_atr * atr
                pos = dict(side=side, e=e, sl=sl, sld=abs(e - sl), i=i)
            continue
        side = pos["side"]; xp = None
        nxt = o[i + 1] if i + 1 < N else c[i]
        if side == 1:
            trail = l[i - ex:i].min()
            if l[i] <= pos["sl"]: xp = pos["sl"]
            elif l[i] <= trail: xp = min(trail, nxt) - slip
        else:
            trail = h[i - ex:i].max()
            if h[i] >= pos["sl"]: xp = pos["sl"]
            elif h[i] >= trail: xp = max(trail, nxt) + slip
        if xp is not None:
            pnl = (xp - pos["e"]) if side == 1 else (pos["e"] - xp)
            trades.append(dict(t=t[pos["i"]], R=pnl / pos["sld"], sld=pos["sld"])); pos = None
    return trades


def net_stream(pair, direction, mode="market", slip=0.0):
    t, o, h, l, c = cached_arrays(pair, "h1")
    ps = bpip(pair); cost = bcomm(pair) + SPREAD
    tr = ea_trades(t, o, h, l, c, direction) if mode == "market" else stop_trades(t, o, h, l, c, direction, slip, ps)
    return [(x["t"], x["R"] - cost / max(x["sld"] / ps, 1e-9)) for x in tr]


def eq_overlay(trades, K=20, m=0.5):
    trades = sorted(trades, key=lambda x: x[0]); eq = 0.0; hist = []; out = []
    for (tt, r) in trades:
        mult = 1.0
        if len(hist) >= K and eq < sum(hist[-K:]) / K:
            mult = m
        p = r * mult; eq += p; hist.append(eq); out.append((tt, p))
    return out


def metrics(trades):
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t = t[o]; r = r[o]
    dd = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    mo1 = max(1.0, (SPLIT - t[0]) / (365.25 * 86400) * 12)
    mo2 = max(1.0, (t[-1] - SPLIT) / (365.25 * 86400) * 12)
    p1 = float(r[t < SPLIT].sum()); p2 = float(r[t >= SPLIT].sum())
    return dd, p1 / mo1, p2 / mo2, len(r), int((r > 0).sum())


def main():
    print("=" * 96)
    print("breakout_h1.mq5 ショート経路 オフライン検証 (EA確定足ロジック写経 vs 研究, 実Axiory H1 7ペア)")
    print("=" * 96)

    L = []; S = []
    print(f"  {'pair':<8} {'long N/WR':>12} {'long net/月(OOS,IS)':>22} | {'short N/WR':>12} {'short net/月(OOS,IS)':>22}")
    for p in BO7:
        ls = net_stream(p, "long"); ss = net_stream(p, "short")
        L += ls; S += ss
        ld, l1, l2, ln, lw = metrics(ls); sd, s1, s2, sn, sw = metrics(ss)
        print(f"  {p:<8} {ln:>4}/{100*lw//max(ln,1):>3}%      {l1:>+7.2f}R {l2:>+7.2f}R     | "
              f"{sn:>4}/{100*sw//max(sn,1):>3}%      {s1:>+7.2f}R {s2:>+7.2f}R")

    print("-" * 96)
    print("  合算スリーブ (overlay適用, DD10%スケール換算):")
    print(f"  {'構成':<22} | {'OOS%/月':>8} {'IS%/月':>8} | {'効率sumR/DD':>11} | {'risk%/pair':>10}")
    for label, a in [("long-only (α=0)", 0.0), ("long+0.2short", 0.2),
                     ("long+0.4short ★研究最適", 0.4), ("long+0.5short", 0.5)]:
        comb = eq_overlay(L + [(tt, a * r) for (tt, r) in S])
        dd, m1, m2, n, w = metrics(comb)
        s = 10.0 / dd if dd > 0 else 0
        eff = (m1 + m2) * 1.0  # 月利合計でなく sumR/DD で見たいので別計算
        # sumR/DD
        rr = np.array([x[1] for x in comb]); sumR = float(rr.sum())
        print(f"  {label:<22} | {m1*s:>+7.2f} {m2*s:>+7.2f} | {sumR/dd:>11.2f} | {s:>10.3f}")
    print("-" * 96)

    # --- 約定規約の感応度: 研究(レベル/ストップ)と実EA(成行)の乖離 ---
    def tot(direction, mode, slip=0.0):
        st = []
        for p in BO7:
            st += net_stream(p, direction, mode, slip)
        d, m1, m2, n, w = metrics(st)
        return m1, m2, n  # OOS/月, IS/月, N (※metricsはmo割りなので合計Rは別途)

    def sumR(direction, mode, slip=0.0):
        r = []
        for p in BO7:
            r += [x[1] for x in net_stream(p, direction, mode, slip)]
        return float(np.sum(r)), len(r)

    print("  約定モデル別 sumR (long / short, net, 7ペア11年):")
    print(f"  {'モデル':<26} | {'LONG sumR':>10} | {'SHORT sumR':>10}")
    for label, mode, slip in [("逆指値 slip=0p(理想/研究相当)", "stop", 0.0),
                              ("逆指値 slip=1p(現実的)", "stop", 1.0),
                              ("逆指値 slip=2p(金/JPY相当)", "stop", 2.0),
                              ("成行・次足始値(現EAの実挙動)", "market", 0.0)]:
        lr, _ = sumR("long", mode, slip); sr, _ = sumR("short", mode, slip)
        print(f"  {label:<26} | {lr:>+9.1f}R | {sr:>+9.1f}R")
    print("-" * 96)
    print("判定(2026-06-16): ショートは逆指値slip=0でも直近ISが赤字、現実的slip/成行で全体赤字 → NO-GO。")
    print("研究 axiory_longshort の +3.72%/月 は『同足レベル約定』の楽観フィルが作った幻。long-onlyは全モデルで黒字。")


if __name__ == "__main__":
    main()
