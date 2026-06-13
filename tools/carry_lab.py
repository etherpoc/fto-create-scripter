"""
carry_lab.py — 金利差/キャリーを「BOと無相関の新ストリーム」候補として検証。

核心の問い(go/no-go): キャリー信号は (1) net で両期間+か、(2) 既存BOと無相関か。
無相関+EVなら合算でDD圧縮→再レバ→月利上振れ([[target-feasibility-math]] の lever)。
※ 強い事前予想: キャリーは円キャリー=BOが乗るレジームそのものなので **正相関の懸念**。それを実測で確かめる。

データ: data/yields/<CCY>.csv (Stooq形式 Date,Open,High,Low,Close)。無ければ tools/fetch_yields.py を促す。
月次・ポイントインタイム厳守: 月末Mまでに判明した利回りで信号 → 翌月M+1のリターンに適用(ルックアヘッド無)。

  python tools/carry_lab.py              # 本番(要 data/yields/)
  python tools/carry_lab.py --selftest   # 合成データでパイプライン検証(コードのみ)
"""
from __future__ import annotations
import sys, argparse, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bo_fast import cached_arrays                          # noqa: E402
from tools.backtest_breakout import pip as bpip, comm as bcomm   # noqa: E402

ALL = ["AUDJPY", "AUDUSD", "CADJPY", "EURJPY", "EURUSD", "GBPJPY",
       "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
P1_END_Y = 2024
SPREAD = 0.5
YDIR = ROOT / "data" / "yields"


def month_key(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def pair_monthly_close(sym):
    """h4 配列から月末終値の系列 {YYYY-MM: close} を作る。"""
    t, o, h, l, c = cached_arrays(sym, "h4")
    out = {}
    for i in range(len(t)):
        out[month_key(int(t[i]))] = float(c[i])   # 同月は後の足で上書き=月末終値
    return out


def load_yield(ccy):
    """data/yields/<ccy>.csv → {YYYY-MM: 月末利回り}. 無ければ None."""
    f = YDIR / f"{ccy}.csv"
    if not f.exists():
        return None
    out = {}
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        date = parts[0]
        try:
            close = float(parts[4])
        except ValueError:
            continue
        out[date[:7]] = close   # YYYY-MM → 月末(後の日付で上書き)
    return out or None


def base_quote(sym):
    return sym[:3], sym[3:6]


def bo_monthly():
    """overlay入りBO H1-long basket の月次純R系列(相関比較用)。"""
    from tools.portfolio_lab import _bo_stream, BO_LONG
    tr = _bo_stream(BO_LONG, "h1", overlay=True)
    m = {}
    for (t, r) in tr:
        m[month_key(t)] = m.get(month_key(t), 0.0) + r
    return m


def build_carry(yields, mode="level", mom_k=3):
    """各ペア・各月の (信号方向, 翌月リターン, ペア) を返す。ポイントインタイム。"""
    rows = []
    for sym in ALL:
        b, q = base_quote(sym)
        yb = yields.get(b) if b != "XAU" else {}   # 金は利回り0扱い(USD金利で資金調達=負キャリー)
        yq = yields.get(q)
        if yq is None or (b != "XAU" and yb is None):
            continue
        closes = pair_monthly_close(sym)
        months = sorted(closes)
        # 差分系列 diff[M] = y_base - y_quote (Mまでに判明)
        diff = {}
        for M in months:
            vb = 0.0 if b == "XAU" else yb.get(M)
            vq = yq.get(M)
            if vb is None or vq is None:
                continue
            diff[M] = vb - vq
        dms = sorted(diff)
        ps = bpip(sym); cst = (bcomm(sym) + SPREAD) * ps   # 1回フリップの片道コスト(価格)
        prev_sign = 0
        for i in range(len(months) - 1):
            M, Mn = months[i], months[i + 1]
            if M not in diff:
                continue
            if mode == "level":
                sig = diff[M]
            else:  # momentum: 差分の変化(Mと mom_k 月前の差)
                past = [diff[x] for x in dms if x <= M][-mom_k - 1:]
                if len(past) < mom_k + 1:
                    continue
                sig = past[-1] - past[0]
            sign = 1 if sig > 0 else (-1 if sig < 0 else 0)
            if sign == 0:
                continue
            # 翌月リターン(価格比)
            if M not in closes or Mn not in closes or closes[M] <= 0:
                continue
            ret = closes[Mn] / closes[M] - 1.0
            pnl = sign * ret
            if sign != prev_sign and prev_sign != 0:
                pnl -= cst / closes[M]   # フリップ時のみコスト(価格比)
            prev_sign = sign
            ts = int(datetime.strptime(Mn, "%Y-%m").replace(tzinfo=timezone.utc).timestamp())
            rows.append((ts, pnl, sym))
    return rows


def summarize(rows, label, bo_m):
    if not rows:
        print(f"  {label}: トレード無し(データ不足)"); return
    # 月次集計(全ペア合算, 等リスク=各ペア月次リターンを単純合算/ペア数で正規化は省略し合算R的に)
    m = {}
    for (t, p, s) in rows:
        m[month_key(t)] = m.get(month_key(t), 0.0) + p
    keys = sorted(m)
    vals = np.array([m[k] for k in keys])
    p1 = sum(v for k, v in m.items() if int(k[:4]) < P1_END_Y)
    p2 = sum(v for k, v in m.items() if int(k[:4]) >= P1_END_Y)
    # 相関 to BO
    allk = sorted(set(m) | set(bo_m))
    x = np.array([m.get(k, 0.0) for k in allk]); y = np.array([bo_m.get(k, 0.0) for k in allk])
    cc = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else 0.0
    sharpe = vals.mean() / vals.std() * math.sqrt(12) if vals.std() > 0 else 0
    print(f"  {label:<18}: N月{len(keys)} 月平均{vals.mean()*100:+.2f}% Sharpe{sharpe:+.2f} | "
          f"P1{p1*100:+.1f}% P2{p2*100:+.1f}% | BO相関 {cc:+.2f}  "
          f"{'★無相関+両期間+' if (p1>0 and p2>0 and abs(cc)<0.3) else ''}")


def run(yields):
    bo_m = bo_monthly()
    print("=" * 96)
    print("キャリー/金利差ストリーム検証 (月次, ポイントインタイム, net)。go/no-go = net両期間+ かつ BO無相関")
    print("=" * 96)
    print(f"  利回り取得済 通貨: {sorted(yields)}")
    for mode, kk in [("level", 0), ("mom", 3), ("mom", 6)]:
        rows = build_carry(yields, mode, kk)
        lbl = f"{mode}" + (f"_k{kk}" if mode == "mom" else "")
        summarize(rows, lbl, bo_m)
    print("\n判定: ★が付けば次段(SLベースのtradeable版+合算DD圧縮)へ。正相関or±0なら不採用(BOの焼き直し)。")


def selftest():
    """合成利回りでパイプラインがルックアヘッド無に走るか検証(エッジの有無は問わない)。"""
    rng = np.random.default_rng(0)
    months = [f"{y}-{mo:02d}" for y in range(2021, 2027) for mo in range(1, 13)]
    yields = {}
    for ccy in ["USD", "JPY", "EUR", "GBP", "AUD", "CAD", "CHF", "NZD"]:
        lvl = 1.0 + rng.random()
        d = {}
        for M in months:
            lvl += rng.normal(0, 0.1); d[M] = lvl
        yields[ccy] = d
    print("[selftest] 合成利回りでパイプライン実行(コード検証のみ):")
    run(yields)
    print("[selftest] OK: 例外なく完走")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest(); return
    yields = {}
    for ccy in ["USD", "JPY", "EUR", "GBP", "AUD", "CAD", "CHF", "NZD"]:
        y = load_yield(ccy)
        if y:
            yields[ccy] = y
    if len(yields) < 2:
        print("❌ data/yields/ に利回りCSVが足りません。先に取得してください:")
        print("    python tools/fetch_yields.py      (ネット可の手元 or `! python tools/fetch_yields.py`)")
        print("  取得後に再実行:  python tools/carry_lab.py")
        return
    run(yields)


if __name__ == "__main__":
    main()
