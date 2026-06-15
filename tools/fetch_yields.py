"""
fetch_yields.py — キャリー/金利差ストリーム用に各国国債利回りを FRED から取得。

★ この環境(Claude実行サンドボックス)はネット遮断のため Claude は実行できない。
   **ユーザが手元(ネット可)で実行する**:
       python tools/fetch_yields.py
   または Claude Code セッションで:  ! python tools/fetch_yields.py

取得先 = FRED (米セントルイス連銀) の CSV ダイレクトDL (無料・APIキー不要・安定):
   https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>
※ 旧版は Stooq を使っていたが「日次ヒット上限」で無音失敗するため FRED へ移行。

採用 = OECD「長期金利(10年国債)」月次系列 IRLTLT01<国>M156N を全通貨で統一。
   carry_lab は月末利回りしか使わない(月次で十分) → 全通貨を同一ソース・同一年限で揃え一貫性を確保。
保存先 = data/yields/<CCY>.csv  (Date,Open,High,Low,Close 形式。O=H=L=C=利回り)。
"""
from __future__ import annotations
import sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "yields"

# 通貨 → FRED系列ID。OECD長期金利(10年国債利回り,月次)。EUR=ドイツ代表。
SERIES = {
    "USD": "IRLTLT01USM156N",
    "JPY": "IRLTLT01JPM156N",
    "EUR": "IRLTLT01DEM156N",   # ドイツ
    "GBP": "IRLTLT01GBM156N",
    "AUD": "IRLTLT01AUM156N",
    "CAD": "IRLTLT01CAM156N",
    "CHF": "IRLTLT01CHM156N",
    "NZD": "IRLTLT01NZM156N",
}
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"


def fetch(sid):
    """FRED CSV を取得し (rows, raw_head) を返す。失敗時 (None, snippet)。"""
    req = urllib.request.Request(URL.format(sid=sid), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read().decode("utf-8", "replace")
    lines = data.splitlines()
    if not lines or "," not in lines[0]:
        return None, data[:200]
    # FRED: 1行目ヘッダ "observation_date,<SID>"(または "DATE,<SID>")。以降 "YYYY-MM-DD,value"。
    rows = []
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) < 2:
            continue
        date, val = p[0].strip(), p[1].strip()
        if val in ("", "."):     # 欠損
            continue
        try:
            float(val)
        except ValueError:
            continue
        rows.append((date, val))
    if len(rows) < 24:           # 月次で最低2年は無いと異常
        return None, f"有効行 {len(rows)} (head: {lines[0][:80]})"
    return rows, None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ok = 0
    for ccy, sid in SERIES.items():
        try:
            rows, err = fetch(sid)
        except Exception as e:
            print(f"❌ {ccy} ({sid}): {e}")
            time.sleep(0.5)
            continue
        if not rows:
            print(f"❌ {ccy} ({sid}): 取得失敗 — {err}")
            time.sleep(0.5)
            continue
        # carry_lab 互換形式: Date,Open,High,Low,Close (O=H=L=C=利回り)
        out_lines = ["Date,Open,High,Low,Close"]
        for date, val in rows:
            out_lines.append(f"{date},{val},{val},{val},{val}")
        (OUT / f"{ccy}.csv").write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"✅ {ccy} <- {sid}  {len(rows)}行  最新: {rows[-1][0]} {rows[-1][1]}%")
        ok += 1
        time.sleep(0.5)
    print(f"\n保存先: {OUT}  ({ok}/{len(SERIES)} 通貨取得)")
    if ok >= 2:
        print("次: python tools/carry_lab.py")
    else:
        print("⚠ 取得不足。FREDが落ちている/シンボル変更の可能性。上の err メッセージを共有してください。")


if __name__ == "__main__":
    main()
