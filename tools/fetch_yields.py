"""
fetch_yields.py — キャリー/金利差ストリーム用に各国国債利回りを Stooq から取得。

★ この環境(Claude実行サンドボックス)はネット遮断のため Claude は実行できない。
   **ユーザが手元(ネット可)で実行する**:
       python tools/fetch_yields.py
   または Claude Code セッションで:  ! python tools/fetch_yields.py

取得先 = Stooq の CSV ダイレクトDL (無料・キー不要):
   https://stooq.com/q/d/l/?s=<SYM>&i=d   (日次)
保存先 = data/yields/<CCY>.csv  (Date,Open,High,Low,Close 形式)。

2年債(2*y.b)を優先(政策金利期待に敏感=キャリーに最適)、無ければ10年債(10*y.b)に自動フォールバック。
"""
from __future__ import annotations
import sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "yields"

# 通貨 → (2Y優先, 10Yフォールバック) の Stooq シンボル
SYMS = {
    "USD": ("2usy.b", "10usy.b"),
    "JPY": ("2jpy.b", "10jpy.b"),
    "EUR": ("2dey.b", "10dey.b"),   # ドイツ国債を EUR 代表
    "GBP": ("2uky.b", "10uky.b"),
    "AUD": ("2auy.b", "10auy.b"),
    "CAD": ("2cay.b", "10cay.b"),
    "CHF": ("2chy.b", "10chy.b"),
    "NZD": ("2nzy.b", "10nzy.b"),
}
URL = "https://stooq.com/q/d/l/?s={sym}&i=d"


def fetch(sym):
    req = urllib.request.Request(URL.format(sym=sym), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read().decode("utf-8", "replace")
    # Stooq は無効シンボルで "No data" 等を返す。ヘッダ Date を含むか確認。
    if "Date" not in data.splitlines()[0] if data.splitlines() else True:
        return None
    if len(data.splitlines()) < 50:
        return None
    return data


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for ccy, (s2, s10) in SYMS.items():
        got = None; used = None
        for sym in (s2, s10):
            try:
                d = fetch(sym)
                if d:
                    got = d; used = sym; break
            except Exception as e:
                print(f"  {ccy} {sym}: {e}")
            time.sleep(1.0)
        if got:
            (OUT / f"{ccy}.csv").write_text(got, encoding="utf-8")
            n = len(got.splitlines()) - 1
            last = got.splitlines()[-1]
            print(f"✅ {ccy} <- {used}  {n}行  最新: {last}")
        else:
            print(f"❌ {ccy}: 2Y/10Y どちらも取得失敗。Stooq でシンボルを確認: https://stooq.com/t/?i=597")
        time.sleep(1.0)
    print(f"\n保存先: {OUT}")
    print("次: python tools/carry_lab.py")


if __name__ == "__main__":
    main()
