"""
log_collector.py — スタンドアロン EA 用の「ログだけ」受け取りサーバ。

スタンドアロン EA (strategies/standalone/mtf_pullback_v2.js) はトレード判断を
すべて FTO 上の JS 内で完結させる (サーバ非依存)。このサーバは **トレードには
一切関与せず**、EA が fire-and-forget で POST してくるエントリー/決済/スキップの
ログを JSONL でディスクに永続化するだけ。

なぜ別サーバか:
  - 旧 thin client の WS サーバ (server/main.py) は STRATEGY を要求し、判断ループを
    持つ。ここは判断ループ不要・STRATEGY 不要のログ専用にしたいので分離。
  - FTO ページは HTTPS なので mixed-content を避けるため HTTPS で待ち受ける。
    前回 wss で信頼済みの localhost 証明書 (server/certs) をそのまま再利用する。

出力:
  data/fto_mtf_pb_v2_live/<SYMBOL>/<session_id>.jsonl   (LOG_DIR で上書き可)

起動 (PowerShell、tools/run_log_collector.ps1 がラップ):
  python -m uvicorn server.log_collector:app --host 0.0.0.0 --port 8443 \
      --ssl-keyfile server/certs/localhost-key.pem \
      --ssl-certfile server/certs/localhost.pem
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(os.environ.get("LOG_DIR") or (ROOT / "data" / "fto_mtf_pb_v2_live"))

app = FastAPI(title="FTO Standalone EA Log Collector")
# FTO ページ (HTTPS) からの fetch を通すため CORS 全許可。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe(s: str, default: str, upper: bool = False) -> str:
    s = (s or "").strip().replace("/", "_").replace("\\", "_")
    if upper:
        s = s.upper()
    # パス区切りや危険文字を除去 (ディレクトリトラバーサル防止)
    s = "".join(ch for ch in s if ch.isalnum() or ch in ("_", "-", "."))
    return s or default


@app.get("/ping")
async def ping() -> dict:
    return {"ok": True, "service": "log_collector", "out_dir": str(OUT_DIR)}


@app.post("/log")
async def log(req: Request) -> dict:
    """EA からの 1 レコード (または配列) を受けて JSONL に追記する。"""
    try:
        body = await req.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    records = body if isinstance(body, list) else [body]
    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        sym = _safe(rec.get("symbol", ""), "UNKNOWN", upper=True)
        sid = _safe(rec.get("session_id", ""), "nosession")
        rec["server_recv_ts"] = now_iso
        path = OUT_DIR / sym / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        written += 1
    return {"ok": True, "written": written}
