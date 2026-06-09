"""
replay_ticks.py — 録音した tick JSONL を WS サーバへ再生する。

サーバ側で RECORD_TICKS_DIR を設定して 1 回 FTO で回しておけば、その後
別の AI 設定 (モデル / プロンプト / features) で同じ期間を高速に検証できる。

特徴:
  - m15/h1/h4 はファイルに記録した値をそのまま送る
  - position / balance はクライアント側でシミュレート (= 我々が「FTO の代わり」)
    - サーバから返ってきた buy/sell/close コマンドを解釈
    - 次の bar で SL / TP がヒットしたか判定
    - v5+ の trailing close ロジックも Python 側で再現
  - サーバの「受信したら即返信」を待ってから次の tick を送る
    → backtest 速度は LLM 応答速度に律速 (FTO 経由よりは速い、AI ロジック同等)

Usage:
    # 全 symbol を順に流す
    python tools/replay_ticks.py \\
        --source data/recorded_ticks \\
        --port 8443

    # 特定 symbol だけ
    python tools/replay_ticks.py --source data/recorded_ticks --symbol XAUUSD --port 8443

    # 1 ファイル
    python tools/replay_ticks.py --file data/recorded_ticks/XAUUSD/xxx.jsonl --port 8443

サーバ側は通常通り起動。例:
    AI_DECISIONS_DIR=data/ai_v8_decisions \\
    AI_CONF_THRESHOLD=0.7 \\
    STRATEGY=zigzag_ai \\
    ZIGZAG_AI_MODEL=ollama:qwen2.5:7b \\
    python -m uvicorn server.main:app --port 8443 \\
        --ssl-keyfile=... --ssl-certfile=...
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import ssl
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import websockets

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class OpenTrade:
    """サーバが buy/sell コマンドを送ってきた後の保有ポジション状態。"""
    side: str           # "long" / "short"
    entry_price: float
    sl: float
    tp: float
    lot: float
    sl_dist: float
    # v5+ trailing
    trail_activate_R: float = 0.0
    trail_stop_R: float = 0.0
    best_profit_R: float = 0.0

    def check_close(self, bar: dict) -> tuple[Optional[str], Optional[float]]:
        """この bar 内で SL/TP/trailing どれかにヒットしたか判定。

        Returns:
            (exit_reason, exit_price) もしくは (None, None)
        """
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        if self.side == "long":
            # 同 bar 内で SL も TP も触れたらどっち優先か曖昧だが、
            # 保守的に SL 優先 (一般に SL が近いケースが多い)
            if low <= self.sl:
                return ("sl_hit", self.sl)
            if high >= self.tp:
                return ("tp_hit", self.tp)
            profit_R = (close - self.entry_price) / self.sl_dist
        else:  # short
            if high >= self.sl:
                return ("sl_hit", self.sl)
            if low <= self.tp:
                return ("tp_hit", self.tp)
            profit_R = (self.entry_price - close) / self.sl_dist

        if profit_R > self.best_profit_R:
            self.best_profit_R = profit_R

        # trailing: 含み益が trail_activate_R 達した後、trail_stop_R まで戻ったら close
        if (
            self.trail_activate_R > 0
            and self.best_profit_R >= self.trail_activate_R
            and profit_R <= self.trail_stop_R
        ):
            return ("trailing_close", close)

        return (None, None)


@dataclass
class ReplayState:
    """1 セッション分の position シミュレーション状態。"""
    position: Optional[str] = None  # None / "long" / "short"
    balance: float = 10_000.0
    open_trade: Optional[OpenTrade] = None
    n_ticks: int = 0
    n_entries: int = 0
    n_exits_sl: int = 0
    n_exits_tp: int = 0
    n_exits_trail: int = 0
    n_exits_close: int = 0


async def replay_file(file_path: Path, server_url: str, verbose: bool = False) -> ReplayState:
    """1 ファイルを WS サーバへ再生して結果を返す。"""
    state = ReplayState()

    # ファイル全体を読む (大きくないので問題なし)
    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]

    if not lines:
        return state
    first = json.loads(lines[0])
    if first.get("_type") != "session_meta":
        print(f"  WARN: first line is not session_meta, skipping {file_path.name}")
        return state

    meta = first
    sid = meta["session_id"]
    symbol = meta["symbol"]
    params = meta.get("params") or {}

    # SSL: 自己署名証明書なので verify は無効
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect(server_url, ssl=ssl_ctx, max_size=None) as ws:
        # init を送る
        init_msg = {
            "type": "init",
            "session_id": sid,
            "symbol": symbol,
            "strategy": "",
            "params": params,
        }
        await ws.send(json.dumps(init_msg))
        ack_raw = await ws.recv()
        ack = json.loads(ack_raw)
        if not ack.get("ok"):
            print(f"  ERROR: init_ack failed: {ack.get('error')}")
            return state

        # tick を順次送る
        for line in lines[1:]:
            rec = json.loads(line)
            if rec.get("_type") == "session_done":
                break
            if rec.get("_type") != "tick":
                continue

            m15 = rec.get("m15") or {}
            # 1) このバー内で既存ポジが SL/TP/trail にヒットしたか確認
            if state.open_trade is not None:
                reason, exit_price = state.open_trade.check_close(m15)
                if reason:
                    if reason == "sl_hit":
                        state.n_exits_sl += 1
                    elif reason == "tp_hit":
                        state.n_exits_tp += 1
                    elif reason == "trailing_close":
                        state.n_exits_trail += 1
                    pnl = (exit_price - state.open_trade.entry_price) * (
                        1 if state.open_trade.side == "long" else -1
                    )
                    state.balance += pnl * state.open_trade.lot  # 粗利 (price 単位)
                    if verbose:
                        print(f"    {symbol} [{reason}] t={m15.get('time')} exit={exit_price:.5f} pnl_price={pnl:+.5f}")
                    state.open_trade = None
                    state.position = None

            # 2) tick メッセージを送信 (position/balance は我々が seed)
            tick_msg = {
                "type": "tick",
                "session_id": sid,
                "m15": m15,
                "h1": rec.get("h1"),
                "h4": rec.get("h4"),
                "position": state.position,
                "balance": state.balance,
            }
            await ws.send(json.dumps(tick_msg))
            resp_raw = await ws.recv()
            resp = json.loads(resp_raw)
            state.n_ticks += 1

            # 3) サーバから返ってきたコマンドを処理
            cmds = resp.get("commands") or []
            for cmd in cmds:
                t = cmd.get("type")
                if t in ("buy", "sell") and state.open_trade is None:
                    side = "long" if t == "buy" else "short"
                    entry_price = float(cmd.get("entry_price") or m15.get("close") or 0.0)
                    sl = float(cmd.get("sl") or 0.0)
                    tp = float(cmd.get("tp") or 0.0)
                    sl_dist = float(cmd.get("sl_dist") or abs(entry_price - sl))
                    if sl_dist <= 0:
                        continue
                    state.open_trade = OpenTrade(
                        side=side,
                        entry_price=entry_price,
                        sl=sl,
                        tp=tp,
                        lot=float(cmd.get("volume") or 0.01),
                        sl_dist=sl_dist,
                        trail_activate_R=float(cmd.get("trail_activate_R") or 0.0),
                        trail_stop_R=float(cmd.get("trail_stop_R") or 0.0),
                    )
                    state.position = side
                    state.n_entries += 1
                    if verbose:
                        print(f"    {symbol} [entry-{side}] entry={entry_price:.5f} sl={sl:.5f} tp={tp:.5f}")
                elif t == "close" and state.open_trade is not None:
                    exit_price = float(m15.get("close") or 0.0)
                    pnl = (exit_price - state.open_trade.entry_price) * (
                        1 if state.open_trade.side == "long" else -1
                    )
                    state.balance += pnl * state.open_trade.lot
                    state.n_exits_close += 1
                    if verbose:
                        print(f"    {symbol} [strategy-close] exit={exit_price:.5f} pnl_price={pnl:+.5f}")
                    state.open_trade = None
                    state.position = None

        # done
        await ws.send(json.dumps({"type": "done", "session_id": sid}))
        try:
            await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    return state


def find_files(source: Path, symbol: Optional[str]) -> list[Path]:
    if source.is_file():
        return [source]
    out: list[Path] = []
    for sym_dir in sorted(source.iterdir()):
        if not sym_dir.is_dir():
            continue
        if symbol and sym_dir.name.upper() != symbol.upper():
            continue
        out.extend(sorted(sym_dir.glob("*.jsonl")))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/recorded_ticks", help="録音 dir or jsonl ファイル")
    ap.add_argument("--file", default=None, help="単一ファイル指定")
    ap.add_argument("--symbol", default=None, help="特定 symbol だけ")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if args.file:
        files = [Path(args.file)]
    else:
        files = find_files(Path(args.source), args.symbol)

    if not files:
        print("No files to replay.")
        return 1

    server_url = f"wss://{args.host}:{args.port}/ws/strategy"
    print(f"Replay target: {server_url}")
    print(f"Files: {len(files)}")
    print()

    total_entries = 0
    total_sl = 0
    total_tp = 0
    total_trail = 0
    total_close = 0

    for i, fp in enumerate(files, 1):
        try:
            disp = fp.resolve().relative_to(ROOT)
        except ValueError:
            disp = fp
        print(f"[{i}/{len(files)}] {disp}")
        try:
            state = await replay_file(fp, server_url, verbose=args.verbose)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {e}")
            continue
        print(
            f"  ticks={state.n_ticks} entries={state.n_entries}  "
            f"exit_sl={state.n_exits_sl} exit_tp={state.n_exits_tp} "
            f"exit_trail={state.n_exits_trail} exit_close={state.n_exits_close}"
        )
        total_entries += state.n_entries
        total_sl += state.n_exits_sl
        total_tp += state.n_exits_tp
        total_trail += state.n_exits_trail
        total_close += state.n_exits_close

    print()
    print("=== TOTAL ===")
    print(f"  entries={total_entries}")
    print(f"  exit_sl={total_sl} exit_tp={total_tp} exit_trail={total_trail} exit_close={total_close}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
