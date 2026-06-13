"""
sweep_lab.py — Liquidity Sweep Reversal (LSR) の体系検証エンジン。

新観点: 古典パターン(ダブルトップ/三尊/前日高値)は「ストップが溜まった流動性プール」。
パターンの方向に乗るのではなく、そこが**スイープ(ストップ狩り)されて失敗=リクレイム**
する瞬間を逆張りで取る。トラップされたブレイク勢の踏みが燃料。

- 確定 H1 足ベース(チャート足非依存、bo_fast の cached_arrays を再利用)。
- ルックアヘッド無し: ピボットは中心から k 本後に確定、シグナルは確定足のみ。
- net = 往復コミ+spread (breakout_lab と同一規約)。1R=口座1%。WF: P1=21-23 / P2=24-26。
- robust = 両期間 net+。

使い方:
    python tools/sweep_lab.py            # 既定アブレーション
    python tools/sweep_lab.py --set rr   # RR 感応
    python tools/sweep_lab.py --set tf --tf h4
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bo_fast import cached_arrays                  # noqa: E402
from tools.backtest_breakout import pip, comm            # noqa: E402

ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
P1_M, P2_M = 36, 30
SPREAD = 0.5


def _atr(h, l, c, n):
    N = len(c)
    tr = np.zeros(N)
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    out = np.full(N, np.nan)
    csum = np.cumsum(tr)
    out[n:] = (csum[n:] - csum[:-n]) / n  # mean tr over [i-n, i)
    return out


def run_lsr(arr, P):
    """Liquidity Sweep Reversal。trades: list[dict(t,R,sld,side,win)]."""
    t, o, h, l, c = arr
    N = len(c)
    k = P.get("k", 3)                      # ピボット半幅
    atr_n = P.get("atr_n", 20)
    lookback = P.get("lookback", 60)       # 流動性レベルの有効本数
    sweep_win = P.get("sweep_win", 3)      # スイープ→リクレイムの窓(本)
    require_double = P.get("require_double", False)
    eq_tol_atr = P.get("eq_tol_atr", 0.5)  # 2点が同値とみなす許容(ATR)
    buf_atr = P.get("buf_atr", 0.1)        # SL バッファ(スイープ極値の外)
    rr = P.get("rr", 1.5)
    min_sl_pips = P.get("min_sl_pips", 0.0)
    max_sl_atr = P.get("max_sl_atr", 6.0)
    direction = P.get("direction", "both")
    trend_n = P.get("trend_n", 0)          # >0 で SMA トレンドフィルタ
    trend_mode = P.get("trend_mode", "")   # "with"=トレンド方向の押し目スイープのみ, "against"=逆張りのみ
    cooldown = P.get("cooldown", 3)
    ps = pip_size = P.get("pip_size", None)

    atr = _atr(h, l, c, atr_n)
    sma = None
    if trend_n > 0:
        cs = np.cumsum(c)
        sma = np.full(N, np.nan)
        sma[trend_n:] = (cs[trend_n:] - cs[:-trend_n]) / trend_n

    ph_idx, ph_px = [], []   # 確定スイング高(中心index, price)
    pl_idx, pl_px = [], []
    warm = max(2 * k + 1, atr_n, lookback, trend_n) + 2
    pos = None
    last_entry = -10**9
    trades = []

    for i in range(warm, N):
        # --- ピボット確定 (中心 = i-k、窓 [i-2k, i] は全て ≤ i) ---
        cj = i - k
        seg_h = h[cj - k:cj + k + 1]
        seg_l = l[cj - k:cj + k + 1]
        if h[cj] == seg_h.max() and np.argmax(seg_h) == k:
            ph_idx.append(cj); ph_px.append(h[cj])
        if l[cj] == seg_l.min() and np.argmin(seg_l) == k:
            pl_idx.append(cj); pl_px.append(l[cj])

        a = atr[i]
        if not (a > 0):
            continue

        # --- ポジション保有中: SL/TP 判定 ---
        if pos is not None:
            if pos["side"] == "short":
                if h[i] >= pos["sl"]:
                    R = (pos["e"] - pos["sl"]) / pos["sld"]; trades.append(_mk(t, pos, R, 0)); pos = None
                elif l[i] <= pos["tp"]:
                    R = (pos["e"] - pos["tp"]) / pos["sld"]; trades.append(_mk(t, pos, R, 1)); pos = None
            else:
                if l[i] <= pos["sl"]:
                    R = (pos["sl"] - pos["e"]) / pos["sld"]; trades.append(_mk(t, pos, R, 0)); pos = None
                elif h[i] >= pos["tp"]:
                    R = (pos["tp"] - pos["e"]) / pos["sld"]; trades.append(_mk(t, pos, R, 1)); pos = None
            if pos is not None:
                continue

        if i - last_entry < cooldown:
            continue

        tr_up = (sma is not None and c[i] > sma[i])
        tr_dn = (sma is not None and c[i] < sma[i])

        # ============ SHORT setup: 直近スイング高(流動性)をスイープ→close で下に戻す ============
        if direction in ("both", "short"):
            # 有効な直近スイング高レベル
            lvl = None; lvl_j = None
            for j in range(len(ph_idx) - 1, -1, -1):
                if ph_idx[j] < i - lookback:
                    break
                if ph_idx[j] > i - sweep_win - 1:
                    continue  # 近すぎ(スイープ対象として確定してない)
                lvl = ph_px[j]; lvl_j = ph_idx[j]
                break
            if lvl is not None:
                ok = True
                if require_double:
                    # eq_tol 以内の同種ピボットが他にもう1つ(=2点タッチ)
                    cnt = sum(1 for px in ph_px if abs(px - lvl) <= eq_tol_atr * a)
                    ok = cnt >= 2
                if ok:
                    win_h = h[i - sweep_win + 1:i + 1]
                    swept = win_h.max() > lvl              # 窓内でレベル超え(ストップ狩り)
                    reclaim = c[i] < lvl and h[i] > c[i]    # 当足は上ヒゲ付けて下に戻す
                    # トレンドフィルタ
                    tok = True
                    if trend_mode == "with":   tok = tr_dn   # 下降トレンドの戻り高値を売る
                    elif trend_mode == "against": tok = tr_up
                    if swept and reclaim and tok:
                        sweep_hi = win_h.max()
                        sl = sweep_hi + buf_atr * a
                        e = c[i]
                        sld = sl - e
                        if sld > 0 and _sl_ok(sld, a, max_sl_atr, min_sl_pips, sym=P.get("_sym")):
                            tp = e - rr * sld
                            pos = dict(side="short", e=e, sl=sl, tp=tp, sld=sld, i=i)
                            last_entry = i
                            continue

        # ============ LONG setup: 直近スイング安(流動性)をスイープ→close で上に戻す ============
        if direction in ("both", "long"):
            lvl = None
            for j in range(len(pl_idx) - 1, -1, -1):
                if pl_idx[j] < i - lookback:
                    break
                if pl_idx[j] > i - sweep_win - 1:
                    continue
                lvl = pl_px[j]
                break
            if lvl is not None:
                ok = True
                if require_double:
                    cnt = sum(1 for px in pl_px if abs(px - lvl) <= eq_tol_atr * a)
                    ok = cnt >= 2
                if ok:
                    win_l = l[i - sweep_win + 1:i + 1]
                    swept = win_l.min() < lvl
                    reclaim = c[i] > lvl and l[i] < c[i]
                    tok = True
                    if trend_mode == "with":   tok = tr_up
                    elif trend_mode == "against": tok = tr_dn
                    if swept and reclaim and tok:
                        sweep_lo = win_l.min()
                        sl = sweep_lo - buf_atr * a
                        e = c[i]
                        sld = e - sl
                        if sld > 0 and _sl_ok(sld, a, max_sl_atr, min_sl_pips, sym=P.get("_sym")):
                            tp = e + rr * sld
                            pos = dict(side="long", e=e, sl=sl, tp=tp, sld=sld, i=i)
                            last_entry = i
                            continue
    return trades


def _mk(t, pos, R, win):
    return dict(t=int(t[pos["i"]]), R=R, sld=pos["sld"], side=pos["side"], win=win, units=1)


def _sl_ok(sld, a, max_sl_atr, min_sl_pips, sym=None):
    if sld > max_sl_atr * a:
        return False
    if min_sl_pips > 0 and sym is not None:
        ps = pip(sym)
        if sld / ps < min_sl_pips:
            return False
    return True


def eval_pairs(P, pairs=ALL, tf="h1"):
    agg = dict(n=0, p1=0.0, p2=0.0, w=0, gross=0.0, wins=0, losses=0, winR=0.0, lossR=0.0)
    rob = 0; viable = []; per = {}
    for sym in pairs:
        Pc = dict(P); Pc["_sym"] = sym
        tr = run_lsr(cached_arrays(sym, tf), Pc)
        if not tr:
            per[sym] = (0, 0, 0, 0); continue
        ps = pip(sym); cst = comm(sym) + SPREAD
        for x in tr:
            x["nR"] = x["R"] - cst / max(x["sld"] / ps, 1e-9)
        n1 = sum(x["nR"] for x in tr if x["t"] < P1_END)
        n2 = sum(x["nR"] for x in tr if x["t"] >= P1_END)
        w = sum(1 for x in tr if x["nR"] > 0)
        agg["n"] += len(tr); agg["p1"] += n1; agg["p2"] += n2; agg["w"] += w
        agg["gross"] += sum(x["R"] for x in tr)
        for x in tr:
            if x["nR"] > 0: agg["wins"] += 1; agg["winR"] += x["nR"]
            else: agg["losses"] += 1; agg["lossR"] += x["nR"]
        r = (n1 > 0 and n2 > 0); rob += r
        if r: viable.append(sym)
        per[sym] = (len(tr), 100 * w / len(tr), n1, n2)
    return agg, rob, viable, per


def report(label, P, tf="h1"):
    agg, rob, viable, per = eval_pairs(P, tf=tf)
    n = agg["n"]
    if n == 0:
        print(f"  {label:<34} | 0 trades"); return
    months = P1_M + P2_M
    wr = 100 * agg["w"] / n
    tpm = n / months / 12  # 12ペア合計の月間 → 1ペア平均/月
    avg_w = agg["winR"] / agg["wins"] if agg["wins"] else 0
    avg_l = agg["lossR"] / agg["losses"] if agg["losses"] else 0
    rr_real = abs(avg_w / avg_l) if avg_l else 0
    print(f"  {label:<34} | N{n:>5} ({n/months:>4.1f}/月,{tpm:>4.1f}/ペア) | "
          f"WR{wr:>4.1f}% RR{rr_real:>4.2f} | P1{agg['p1']/P1_M:>+6.2f}% P2{agg['p2']/P2_M:>+6.2f}%/月 | "
          f"rob{rob:>2}/12 | gross{agg['gross']:>+6.1f}")
    print(f"      viable: {','.join(viable) or '(なし)'}", flush=True)
    return agg, rob, viable, per


def variant_sets(name):
    base = dict(k=3, atr_n=20, lookback=60, sweep_win=3, rr=1.5, buf_atr=0.1,
                min_sl_pips=10.0, max_sl_atr=6.0, direction="both", label="base(k3/lb60/sw3/RR1.5/minSL10)")
    if name == "base":
        return [base]
    if name == "dir":
        return [base,
                dict(base, direction="long", label="long-only(安値スイープ)"),
                dict(base, direction="short", label="short-only(高値スイープ)")]
    if name == "rr":
        return [dict(base, rr=r, label=f"RR{r}") for r in (1.0, 1.5, 2.0, 2.5, 3.0)]
    if name == "double":
        return [base,
                dict(base, require_double=True, eq_tol_atr=0.5, label="+double(2点タッチ,tol0.5ATR)"),
                dict(base, require_double=True, eq_tol_atr=1.0, label="+double(tol1.0ATR)")]
    if name == "trend":
        return [base,
                dict(base, trend_n=100, trend_mode="with", label="+trend100 with(順張り押目スイープ)"),
                dict(base, trend_n=100, trend_mode="against", label="+trend100 against(逆張りのみ)")]
    if name == "minsl":
        return [dict(base, min_sl_pips=m, label=f"minSL{m}pips") for m in (0, 5, 10, 15, 20)]
    if name == "k":
        return [dict(base, k=kk, label=f"k={kk}(ピボット半幅)") for kk in (2, 3, 4, 5)]
    if name == "sweep":
        return [dict(base, sweep_win=s, label=f"sweep_win={s}") for s in (1, 2, 3, 5)]
    return [base]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="all")
    ap.add_argument("--tf", default="h1")
    args = ap.parse_args()
    print("=" * 118)
    print(f"Liquidity Sweep Reversal (LSR) 検証 [{args.set}] tf={args.tf} — 全12ペア net WF")
    print("流動性スイープ→リクレイムを逆張り。robust=両期間net+。1R=口座1%。")
    print("=" * 118)
    sets = ["dir", "rr", "double", "trend", "minsl", "k", "sweep"] if args.set == "all" else [args.set]
    for s in sets:
        print(f"\n--- [{s}] ---")
        for P in variant_sets(s):
            report(P["label"], P, tf=args.tf)


if __name__ == "__main__":
    main()
