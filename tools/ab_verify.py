"""
ab_verify.py — YouTube「AB手法」(HighLeveFX) の機械化骨格を実Axioryで再現性検証。

手法(裁量含む)の機械化できる部分だけを厳密化して測る。詳細仕様: strategies/ab_method/spec.md
  ステップ1: ema20×ema200 クロスで bias 固定 (GC=long / DC=short)
  ステップ2: bias後、価格が ema200 にタッチ
  ステップ3: タッチ後、直近K本高安のブレイク(=斜めライン近似)で成行
  SL=直近スイング(min low / max high over M), TP=entry±RR*risk(=N計算近似)
裁量(綺麗な斜めライン/波形/勢い/厳選/手動決済)は再現対象外 → 「勝率9割」は厳選が主因と想定し、
機械骨格の net 期待値(spread+commission後)と IS/OOS 再現性を測る。

  python tools/ab_verify.py            # 推奨セット(XAU+高vol) M5
  python tools/ab_verify.py all        # 全15ペア M5
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.backtest_breakout import pip as bpip, comm as bcomm   # noqa: E402
import tools.axiory_data as ax                                   # noqa: E402

SPREAD = 0.5
SPLIT = datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
# 推奨: 動画が薦める高vol(XAU)＋トレンド向きJPY＋為替majors代表
FOCUS = ["XAUUSD", "GBPJPY", "EURJPY", "USDJPY", "EURUSD", "GBPUSD"]


def ema_sma_seed(x, period):
    """SMAシードのEMA(indicators.py規約)。先頭 period-1 は NaN。"""
    n = len(x)
    out = np.full(n, np.nan)
    if n < period:
        return out
    k = 2.0 / (period + 1)
    seed = x[:period].mean()
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (x[i] - prev) * k + prev
        out[i] = prev
    return out


def atr_arr(h, l, c, n):
    tr = np.zeros(len(c))
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    return pd.Series(tr).rolling(n).mean().values


def h4_trend_on_m5(pair, t_m5):
    """各M5バー時点で確定しているH4の trend(sign(close-ema200)) を返す(ルックアヘッド無)。"""
    th, _, _, _, ch = ax.cached_arrays(pair, "h4")
    e = ema_sma_seed(ch, 200)
    tr = np.sign(ch - e)               # +1 上 / -1 下 / nan
    close_t = th + 14400               # H4 確定時刻
    # 各 m5 終端(t+300) 以下で最後に確定した H4 index
    idx = np.searchsorted(close_t, t_m5 + 300, side="right") - 1
    out = np.full(len(t_m5), np.nan)
    valid = idx >= 0
    out[valid] = tr[idx[valid]]
    return out


def ab(arr, K=10, M=10, RR=1.0, ema_s=20, ema_l=200,
       atr_n=20, sep_min=0.0, slope_n=0, mom_body=0.0, ext_max=0.0,
       be_trig=0.0, h4=None):
    """AB手法機械化骨格 + 定義可能な厳選フィルタ。trades=[(exit_ts, R, sl_dist)]。

    sep_min : |ema20-ema200|/ATR の下限 (レンジ/EMA絡み回避)。0=off
    slope_n : ema200 が slope_n 本前より方向通り傾く事を要求。0=off
    mom_body: ブレイク足の実体 (close-open)/ATR の下限 (勢い)。0=off
    ext_max : (close-ema200)/ATR の上限 (伸び切り=終盤回避)。0=off
    be_trig : 含み +be_trig*risk でSLを建値へ(柔軟決済/BE)。0=off
    h4      : H4トレンド配列(m5整列)。Noneでフィルタ無し。
    """
    t, o, h, l, c = arr
    e20 = ema_sma_seed(c, ema_s)
    e200 = ema_sma_seed(c, ema_l)
    atr = atr_arr(h, l, c, atr_n)
    hh = pd.Series(h).rolling(K).max().shift(1).values   # max high i-K..i-1
    ll = pd.Series(l).rolling(K).min().shift(1).values   # min low  i-K..i-1
    sw_low = pd.Series(l).rolling(M).min().values        # swing low  i-M+1..i
    sw_high = pd.Series(h).rolling(M).max().values       # swing high i-M+1..i

    trades = []
    bias = 0
    touched = False
    pos = 0           # +1 long / -1 short
    ent = sl = tp = 0.0
    sld = 0.0
    moved_be = False
    warm = max(ema_l, atr_n, K, M) + 2
    for i in range(warm, len(c)):
        if np.isnan(e200[i]) or np.isnan(e20[i]) or np.isnan(e20[i - 1]) or not (atr[i] > 0):
            continue
        gc = e20[i] > e200[i] and e20[i - 1] <= e200[i - 1]
        dc = e20[i] < e200[i] and e20[i - 1] >= e200[i - 1]
        if gc:
            bias, touched = +1, False
        elif dc:
            bias, touched = -1, False

        if pos != 0:
            # --- 柔軟決済: 含み +be_trig*risk で建値へ ---
            if be_trig > 0 and not moved_be:
                if pos == 1 and h[i] >= ent + be_trig * sld:
                    sl, moved_be = ent, True
                elif pos == -1 and l[i] <= ent - be_trig * sld:
                    sl, moved_be = ent, True
            ex = None
            if pos == 1:
                if l[i] <= sl: ex = sl
                elif h[i] >= tp: ex = tp
            else:
                if h[i] >= sl: ex = sl
                elif l[i] <= tp: ex = tp
            if ex is not None:
                trades.append((int(t[i]), (ex - ent) * pos / sld, sld))
                pos = 0
            continue

        if bias == +1 and l[i] <= e200[i]:
            touched = True
        elif bias == -1 and h[i] >= e200[i]:
            touched = True

        # --- 厳選フィルタ (方向別に判定) ---
        def ok(side):
            sep = (e20[i] - e200[i]) / atr[i] * side
            if sep_min > 0 and sep < sep_min:
                return False
            if slope_n > 0 and not np.isnan(e200[i - slope_n]):
                if (e200[i] - e200[i - slope_n]) * side <= 0:
                    return False
            if mom_body > 0 and (c[i] - o[i]) * side < mom_body * atr[i]:
                return False
            if ext_max > 0 and (c[i] - e200[i]) * side > ext_max * atr[i]:
                return False
            if h4 is not None and not (h4[i] == side):
                return False
            return True

        if bias == +1 and touched and not np.isnan(hh[i]):
            if c[i] > e200[i] and c[i] > hh[i] and ok(+1):
                slp = sw_low[i]
                if slp < c[i]:
                    ent, sl, sld = c[i], slp, c[i] - slp
                    tp = ent + RR * sld
                    pos, touched, moved_be = 1, False, False
        elif bias == -1 and touched and not np.isnan(ll[i]):
            if c[i] < e200[i] and c[i] < ll[i] and ok(-1):
                shp = sw_high[i]
                if shp > c[i]:
                    ent, sl, sld = c[i], shp, shp - c[i]
                    tp = ent - RR * sld
                    pos, touched, moved_be = -1, False, False
    return trades


def evalp(pair, tf, **kw):
    arr = ax.cached_arrays(pair, tf)
    tr = ab(arr, **kw)
    cost = bcomm(pair) + SPREAD
    ps = bpip(pair)
    return [(t, R - cost / max(sld / ps, 1e-9)) for (t, R, sld) in tr]


def st(trades):
    if not trades:
        return None
    t = np.array([x[0] for x in trades]); r = np.array([x[1] for x in trades])
    o = np.argsort(t); t, r = t[o], r[o]
    dd = float((np.maximum.accumulate(np.cumsum(r)) - np.cumsum(r)).max())
    wr = 100 * (r > 0).mean()
    exp = float(r.mean())
    return len(r), wr, exp, float(r[t < SPLIT].sum()), float(r[t >= SPLIT].sum()), dd


def show(label, trades):
    s = st(trades)
    if not s:
        print(f"  {label:<14}| 0 trades"); return
    n, wr, exp, p1, p2, dd = s
    rob = "✅両+" if (p1 > 0 and p2 > 0) else ("△IS+" if p2 > 0 else ("△OOS+" if p1 > 0 else "✗"))
    tot = p1 + p2
    print(f"  {label:<14}| N{n:>5} WR{wr:>4.1f}% exp{exp:>+6.3f}R | net{tot:>+7.1f}R "
          f"(OOS{p1:>+6.1f} IS{p2:>+6.1f}) DD{dd:>5.1f} | {rob}")


def run_configs(pairs, configs):
    for cfgname, kw in configs:
        print(f"\n--- {cfgname} ---")
        agg = []
        for p in pairs:
            tr = evalp(p, "m5", **kw)
            show(p, tr); agg += tr
        show("★合算", agg)


def ablation(pairs):
    """素の骨格 → 厳選フィルタを1つずつ → 全部入り。WRと net がどう動くか。"""
    base = dict(RR=1.5, K=20, M=20)   # ベース(WR/lossが最良だった素設定)
    steps = [
        ("素(filterなし)", {}),
        ("+EMA乖離 sep1.0", dict(sep_min=1.0)),
        ("+ema200傾き s50", dict(slope_n=50)),
        ("+勢い body0.5", dict(mom_body=0.5)),
        ("+伸び切り回避 ext3", dict(ext_max=3.0)),
        ("+H4整列", dict(h4=True)),
        ("+建値BE 1.0R", dict(be_trig=1.0)),
        ("★全部入り", dict(sep_min=1.0, slope_n=50, mom_body=0.5, ext_max=3.0, h4=True, be_trig=1.0)),
        ("★厳選のみ(BE無)", dict(sep_min=1.0, slope_n=50, mom_body=0.5, ext_max=3.0, h4=True)),
    ]
    for label, extra in steps:
        kw = dict(base)
        use_h4 = extra.pop("h4", False)
        kw.update(extra)
        agg = []
        for p in pairs:
            arr = ax.cached_arrays(p, "m5")
            h4 = h4_trend_on_m5(p, arr[0]) if use_h4 else None
            tr = ab(arr, h4=h4, **kw)
            cost = bcomm(p) + SPREAD; ps = bpip(p)
            agg += [(t, R - cost / max(sld / ps, 1e-9)) for (t, R, sld) in tr]
        show(label, agg)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    pairs = ax.PAIRS if mode == "all" else FOCUS
    print("=" * 104)
    print("AB手法(機械化骨格) @ 実Axiory M5 — 再現性検証 (net: spread+comm後, R単位, WF OOS<2021<IS)")
    print("  ※裁量を定義化したフィルタ込み。素骨格はコスト後マイナス・WR≒コインフリップ。")
    print("=" * 104)
    if mode == "abl":
        print("\n=== アブレーション: ベースRR1.5/K20/M20 に厳選フィルタを定義化して加算 (合算) ===")
        ablation(pairs)
        return
    if mode == "rr":
        print("\n=== 低RR掃引: 動画『小さい波N計算=高勝率』の正体(RR<1で9割に届くか? 利益になるか?) ===")
        run_configs(pairs, [
            ("RR0.3 K20 M20", dict(RR=0.3, K=20, M=20)),
            ("RR0.5 K20 M20", dict(RR=0.5, K=20, M=20)),
            ("RR0.7 K20 M20", dict(RR=0.7, K=20, M=20)),
            ("RR0.5 +厳選",   dict(RR=0.5, K=20, M=20, sep_min=1.0, slope_n=50, ext_max=3.0)),
        ])
        return
    run_configs(pairs, [
        ("RR1.0 K10 M10", dict(RR=1.0, K=10, M=10)),
        ("RR1.5 K10 M10", dict(RR=1.5, K=10, M=10)),
        ("RR2.0 K10 M10", dict(RR=2.0, K=10, M=10)),
        ("RR1.0 K20 M20", dict(RR=1.0, K=20, M=20)),
        ("RR1.5 K20 M20", dict(RR=1.5, K=20, M=20)),
    ])


if __name__ == "__main__":
    main()
