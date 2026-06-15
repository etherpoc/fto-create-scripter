"""
convert_yields.py — curl で落とした FRED 生CSV → carry_lab 互換形式に変換 (ネット不要)。

背景: urllib 直DL が環境のプロキシ/TLSで stall するため、ダウンロードは Windows標準 curl.exe に分離。
   ユーザが data/yields/raw/<CCY>.csv (FRED生) を置く → 本スクリプトが data/yields/<CCY>.csv に変換。
   ネットを使わないので Claude 側でも実行可能。

FRED生形式 : 1行目ヘッダ "observation_date,<SID>" (or "DATE,<SID>")、以降 "YYYY-MM-DD,value" (欠損は ".")
出力形式   : "Date,Open,High,Low,Close" (O=H=L=C=利回り) ← carry_lab.load_yield が parts[4] を読む
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
YDIR = ROOT / "data" / "yields"
RAW = YDIR / "raw"

# FRED系列ID → 通貨。ブラウザDLだと既定ファイル名が <SID>.csv になるため対応。
SID2CCY = {
    "IRLTLT01USM156N": "USD", "IRLTLT01JPM156N": "JPY", "IRLTLT01DEM156N": "EUR",
    "IRLTLT01GBM156N": "GBP", "IRLTLT01AUM156N": "AUD", "IRLTLT01CAM156N": "CAD",
    "IRLTLT01CHM156N": "CHF", "IRLTLT01NZM156N": "NZD",
}
# 生CSVを探す場所(repo直下 / data/yields/raw / Downloads は対象外)。
SEARCH_DIRS = [ROOT, RAW]


def ccy_of(stem: str) -> str:
    """ファイル名(stem)から通貨を判定。<SID> でも <CCY> でも可。"""
    if stem.upper() in SID2CCY.values():
        return stem.upper()
    return SID2CCY.get(stem, stem)


def convert_one(raw_path: Path, ccy: str) -> tuple[int, str | None]:
    txt = raw_path.read_text(encoding="utf-8", errors="replace")
    lines = txt.splitlines()
    if not lines or "," not in lines[0]:
        return 0, f"CSVでない (head: {txt[:80]!r})"
    rows = []
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) < 2:
            continue
        date, val = p[0].strip(), p[1].strip()
        if val in ("", "."):
            continue
        try:
            float(val)
        except ValueError:
            continue
        rows.append((date, val))
    if len(rows) < 24:
        return 0, f"有効行 {len(rows)} (head: {lines[0][:80]})"
    out = ["Date,Open,High,Low,Close"]
    out += [f"{d},{v},{v},{v},{v}" for d, v in rows]
    YDIR.mkdir(parents=True, exist_ok=True)
    (YDIR / f"{ccy}.csv").write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(rows), None


def main():
    # repo直下 + data/yields/raw から FRED生CSV(IRLTLT01*M156N.csv / <CCY>.csv)を収集。
    found = {}
    for d in SEARCH_DIRS:
        if not d.exists():
            continue
        for f in list(d.glob("IRLTLT01*M156N.csv")) + list(d.glob("???.csv")):
            ccy = ccy_of(f.stem)
            if ccy in SID2CCY.values():
                found[ccy] = f   # 後勝ち(raw を優先)
    if not found:
        print("[NG] 生CSVが見つかりません。ブラウザでFREDからDLし repo直下 か data/yields/raw に置いてください。")
        sys.exit(1)
    ok = 0
    for ccy in ["USD", "JPY", "EUR", "GBP", "AUD", "CAD", "CHF", "NZD"]:
        if ccy not in found:
            print(f"[--] {ccy}  未取得 (skip)")
            continue
        n, err = convert_one(found[ccy], ccy)
        if n:
            print(f"[OK] {ccy}  {n}行 → data/yields/{ccy}.csv  ({found[ccy].name})")
            ok += 1
        else:
            print(f"[NG] {ccy}  {err}")
    print(f"\n{ok}/8 通貨を変換。次: python tools/carry_lab.py")


if __name__ == "__main__":
    main()
