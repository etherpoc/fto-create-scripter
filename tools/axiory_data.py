"""
axiory_data.py — Axiory 公開ヒストリカル(M1 OHLCV, 2015-2026, 15ペア)のローダ/集計。

実ブローカー(=デプロイ先)のM1データを M5/M15/H1/H4 に集計し npz キャッシュ。
bo_fast.cached_arrays と同じ (t,o,h,l,c) 配列を返すので既存エンジンをそのまま回せる。
ファイルは data/axiory/<PAIR>/ と data/axiory/ 直下に散在 → 再帰globで全月収集(_all集約は除外)。

CSV形式: 2015.01.02,00:00,open,high,low,close,volume  (ヘッダ無し, 1分足, サーバ時刻)
※ 時刻はブローカーサーバ時刻(Axiory=EET, GMT+2/+3)。utc_offset_sec で UTC 補正可(時間帯フィルタ用)。

  python tools/axiory_data.py            # 全ペアを全TFで集計・キャッシュ
  python tools/axiory_data.py USDJPY h1  # 単体確認
"""
from __future__ import annotations
import sys, glob, os
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AX = ROOT / "data" / "axiory"
CACHE = ROOT / "data" / "axiory_cache"

PAIRS = ["AUDJPY", "AUDUSD", "CADJPY", "CHFJPY", "EURGBP", "EURJPY", "EURUSD",
         "GBPJPY", "GBPUSD", "NZDJPY", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD"]
TF = {"m1": 60, "m5": 300, "m15": 900, "h1": 3600, "h4": 14400}
# Axiory サーバ時刻 → UTC。EET(GMT+2)冬 / EEST(GMT+3)夏。breakout(時間帯非依存)では影響なし。
UTC_OFFSET_SEC = -3 * 3600   # GMT+3 を既定(MT5 EAの InpServerUtcOffset=-3 と整合)


def _files(pair):
    fs = glob.glob(str(AX / "**" / f"{pair}_*.csv"), recursive=True)
    fs += glob.glob(str(AX / f"{pair}_*.csv"))
    fs = sorted(set(f for f in fs if "_all" not in os.path.basename(f).lower()))
    return fs


def load_m1(pair, utc_offset_sec=UTC_OFFSET_SEC):
    """全月CSVを結合し M1 を (ts, o,h,l,c,v) DataFrame で返す(時刻昇順, dedup)。ts=UTC秒。"""
    fs = _files(pair)
    if not fs:
        raise FileNotFoundError(f"no CSV for {pair} under {AX}")
    parts = []
    for f in fs:
        df = pd.read_csv(f, header=None, names=["d", "t", "o", "h", "l", "c", "v"],
                         dtype={"o": float, "h": float, "l": float, "c": float, "v": float},
                         na_values=[""], keep_default_na=False)
        parts.append(df)
    df = pd.concat(parts, ignore_index=True)
    dt = pd.to_datetime(df["d"].str.strip() + " " + df["t"].str.strip(),
                        format="%Y.%m.%d %H:%M", errors="coerce")
    df["ts"] = (dt.astype("int64") // 10**9) + utc_offset_sec
    df = df[dt.notna()].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def aggregate(df, tf_sec):
    """M1 DataFrame → 指定TFの (t,o,h,l,c) 配列。bucket=floor(ts/tf)*tf。"""
    ts = df["ts"].values.astype(np.int64)
    bucket = (ts // tf_sec) * tf_sec
    g = pd.DataFrame({"b": bucket, "o": df["o"].values, "h": df["h"].values,
                      "l": df["l"].values, "c": df["c"].values})
    a = g.groupby("b", sort=True).agg(o=("o", "first"), h=("h", "max"),
                                      l=("l", "min"), c=("c", "last"))
    return (a.index.values.astype(np.int64), a["o"].values, a["h"].values,
            a["l"].values, a["c"].values)


_mem = {}


def cached_arrays(pair, tf="h1"):
    key = (pair, tf)
    if key in _mem:
        return _mem[key]
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{pair}_{tf}.npz"
    if f.exists():
        d = np.load(f); arr = (d["t"], d["o"], d["h"], d["l"], d["c"])
    else:
        arr = aggregate(load_m1(pair), TF[tf])
        np.savez(f, t=arr[0], o=arr[1], h=arr[2], l=arr[3], c=arr[4])
    _mem[key] = arr
    return arr


def _fmt(ts):
    return pd.Timestamp(int(ts), unit="s", tz="UTC").strftime("%Y-%m-%d")


def main():
    args = sys.argv[1:]
    if len(args) == 2:
        pair, tf = args
        arr = cached_arrays(pair, tf)
        t = arr[0]
        print(f"{pair} {tf}: {len(t)} bars  {_fmt(t[0])} ~ {_fmt(t[-1])}")
        print(f"  last close={arr[4][-1]:.5f}  sample O/H/L/C={arr[1][-1]:.5f}/{arr[2][-1]:.5f}/{arr[3][-1]:.5f}/{arr[4][-1]:.5f}")
        return
    # 全ペア×全TF キャッシュ生成
    for pair in PAIRS:
        line = f"{pair:8s}"
        for tf in ("m5", "m15", "h1", "h4"):
            try:
                arr = cached_arrays(pair, tf)
                line += f"  {tf}:{len(arr[0])}"
            except Exception as e:
                line += f"  {tf}:ERR({e})"
        a = cached_arrays(pair, "h1")
        line += f"  [{_fmt(a[0][0])}~{_fmt(a[0][-1])}]"
        print(line, flush=True)


if __name__ == "__main__":
    main()
