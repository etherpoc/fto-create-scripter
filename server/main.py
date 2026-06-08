"""
main.py — FTO Ping テスト用のローカルサーバ。

FastAPI + WebSocket。CORS を全許可 (Access-Control-Allow-Origin: *) してあるので、
FTO 上の JS から fetch() が届くか確認するために使う。

起動方法 (HTTP - 検証用):
    uvicorn server.main:app --host 0.0.0.0 --port 8080 --reload

起動方法 (HTTPS - 本番想定):
    uvicorn server.main:app --host 0.0.0.0 --port 8443 --reload \\
        --ssl-keyfile=server/certs/localhost-key.pem \\
        --ssl-certfile=server/certs/localhost.pem

証明書の準備は README.md 参照。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.session import StrategySession  # noqa: E402
from src.core.strategy_base import Bar  # noqa: E402

# ★ このサーバが束ねる戦略 (起動時に固定)
#   環境変数 STRATEGY で必ず指定する。未指定なら起動時エラー。
#   EA からの init メッセージの strategy フィールドは無視する。
SERVER_STRATEGY = os.environ.get("STRATEGY")
if not SERVER_STRATEGY:
    raise RuntimeError(
        "env STRATEGY is required (e.g. STRATEGY=zigzag_ai). "
        "Specify which strategy this server instance should run."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ping_server")

app = FastAPI(title="FTO Strategy Server")


@app.on_event("startup")
async def _on_startup() -> None:
    import server.deciders  # noqa: F401
    from server.deciders.registry import get_strategy_entry, list_strategies

    if get_strategy_entry(SERVER_STRATEGY) is None:
        raise RuntimeError(
            f"STRATEGY={SERVER_STRATEGY!r} is not registered. "
            f"available: {list_strategies()}"
        )
    log.info("Server bound to strategy: %s", SERVER_STRATEGY)

    # AI モデルのウォームアップ (コールドスタート対策)
    # zigzag_ai のように Ollama を使う戦略のとき、初回エントリでタイムアウトしないよう、
    # ダミーリクエストでモデルを VRAM に常駐させておく。
    # さらに、エントリ間隔が空いて Ollama がアイドル退避するのを防ぐため、
    # バックグラウンドタスクで定期的に warmup する。
    if SERVER_STRATEGY == "zigzag_ai":
        import asyncio
        import os as _os
        from server.ai.ollama_client import OllamaAIModel
        model_spec = _os.environ.get("ZIGZAG_AI_MODEL", "ollama:gemma3:4b")
        if model_spec.startswith("ollama:"):
            model_name = model_spec.split(":", 1)[1]
            ollama_model = OllamaAIModel(model=model_name)
            log.info("Warming up Ollama model: %s ...", model_name)
            try:
                ollama_model.warmup()
            except Exception as e:  # noqa: BLE001
                log.warning("Initial warmup failed: %s", e)

            # 定期ウォームアップ (90 秒ごと、Ollama 既定 5 分のアイドルキープに対し余裕)
            async def _periodic_warmup() -> None:
                while True:
                    await asyncio.sleep(90)
                    try:
                        await asyncio.to_thread(ollama_model.warmup)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Periodic warmup failed: %s", e)

            asyncio.create_task(_periodic_warmup())
            log.info("Periodic Ollama warmup task scheduled (every 90s)")

# CORS: FTO のページからの fetch を許可するため全許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
async def ping() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    log.info("GET /ping")
    return {"ok": True, "server_time_utc": now, "msg": "pong"}


@app.get("/strategies")
async def list_strategies_endpoint() -> dict:
    """このサーバが現在実行している戦略 + 登録済み全戦略を返す。"""
    import server.deciders  # noqa: F401
    from server.deciders.registry import list_strategies

    return {
        "running": SERVER_STRATEGY,
        "available": list_strategies(),
    }


@app.post("/decide")
async def decide(payload: dict) -> dict:
    """戦略判断のスタブ。本番では features → action の判定がここに入る。

    現状は受け取った内容をエコーバックするだけ。
    """
    log.info("POST /decide payload_keys=%s", list(payload.keys()))
    return {
        "action": "skip",
        "confidence": 0.0,
        "reason": "stub server, echoing only",
        "received_keys": list(payload.keys()),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Ping テスト用の汎用 echo エンドポイント。"""
    await ws.accept()
    log.info("WebSocket /ws connected: %s", ws.client)
    try:
        await ws.send_text("hello from server")
        while True:
            data = await ws.receive_text()
            log.info("ws recv: %s", data)
            await ws.send_text(f"echo: {data}")
    except WebSocketDisconnect:
        log.info("WebSocket /ws disconnected")


def _bar_from_dict(d: dict) -> Bar:
    return Bar(
        time=int(d["time"]),
        open=float(d["open"]),
        high=float(d["high"]),
        low=float(d["low"]),
        close=float(d["close"]),
        volume=float(d.get("volume", 0.0)),
    )


@app.websocket("/ws/strategy")
async def strategy_endpoint(ws: WebSocket) -> None:
    """戦略実行エンドポイント。

    EA 側のプロトコル:
      init       (EA→Server): {type, session_id, symbol, params, symbol_info?}
      init_ack   (Server→EA): {type, session_id, ok}
      tick       (EA→Server): {type, session_id, m15, h1?, h4?, position, balance}
      commands   (Server→EA): {type, session_id, bar_time, commands[], draw[], logs[]}
      done       (EA→Server): {type, session_id}
    """
    await ws.accept()
    log.info("WebSocket /ws/strategy connected: %s", ws.client)
    session: StrategySession | None = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "msg": "bad json"}))
                continue

            mtype = msg.get("type")

            if mtype == "init":
                sid = str(msg.get("session_id") or "")
                symbol = str(msg.get("symbol") or "UNKNOWN")
                # ★ EA からの 'strategy' フィールドは無視。サーバ起動時の固定値を使う。
                strategy_name = SERVER_STRATEGY
                params = msg.get("params") or {}
                # symbol 未確定 (UNKNOWN/空) で init を受け取った場合は拒否。
                # EA はこれを見て、Symbol() が確定するまで待ってから再試行する。
                if not symbol or symbol.upper() == "UNKNOWN":
                    await ws.send_text(json.dumps({
                        "type": "init_ack",
                        "session_id": sid,
                        "ok": False,
                        "error": "symbol not ready (got UNKNOWN); wait and retry",
                    }))
                    continue
                try:
                    session = StrategySession(sid, symbol, strategy_name, params)
                except ValueError as e:
                    await ws.send_text(json.dumps({
                        "type": "init_ack",
                        "session_id": sid,
                        "ok": False,
                        "error": str(e),
                    }))
                    continue
                log.info(
                    "session init id=%s symbol=%s strategy=%s params_keys=%s",
                    sid, symbol, strategy_name, list(params.keys()),
                )
                await ws.send_text(json.dumps({
                    "type": "init_ack",
                    "session_id": sid,
                    "strategy": strategy_name,
                    "ok": True,
                }))

            elif mtype == "tick":
                if session is None:
                    await ws.send_text(
                        json.dumps({"type": "error", "msg": "no session, send init first"})
                    )
                    continue
                m15_raw = msg.get("m15")
                if not m15_raw:
                    continue
                m15_bar = _bar_from_dict(m15_raw)
                h1_bar = _bar_from_dict(msg["h1"]) if msg.get("h1") else None
                h4_bar = _bar_from_dict(msg["h4"]) if msg.get("h4") else None
                pos = msg.get("position")  # None / "long" / "short"
                bal = float(msg.get("balance") or 0.0)
                # process_tick は AI 戦略の場合 Ollama への同期 HTTP 通信を含むため
                # FastAPI の event loop を直接ブロックしないよう、スレッド実行に逃がす。
                # これで複数 WS セッション (例: XAUUSD と EURUSD 同時 backtest) が
                # 互いの AI 計算を待たずに進行できる。
                response = await asyncio.to_thread(
                    session.process_tick, m15_bar, h1_bar, h4_bar, pos, bal
                )
                await ws.send_text(json.dumps(response))

            elif mtype == "done":
                log.info("session done id=%s", msg.get("session_id"))
                await ws.send_text(json.dumps({"type": "bye"}))
                break

            else:
                await ws.send_text(
                    json.dumps({"type": "error", "msg": f"unknown type: {mtype}"})
                )

    except WebSocketDisconnect:
        log.info("WebSocket /ws/strategy disconnected (session=%s)",
                 session.session_id if session else None)
    except Exception as e:  # noqa: BLE001
        log.exception("WS strategy error: %s", e)
