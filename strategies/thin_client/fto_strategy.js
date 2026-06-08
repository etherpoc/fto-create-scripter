/**
 * fto_strategy.js — Thin Client (汎用)、判断は全部ローカル Python サーバ
 *
 * 責務:
 *   1. wss://localhost:8443/ws/strategy への WebSocket 接続を維持
 *   2. init メッセージで「サーバ側で動かす戦略名 (Strategy Name)」を指定
 *   3. 確定足ごとに M15/H1/H4 の生 OHLC とポジション/残高を送信
 *   4. サーバから返ってきた commands (buy/sell/close) と draw (chart text) を実行
 *
 * このファイルには戦略ロジックは一切無い。**1 つの EA を使い回す。**
 * ロジック差し替えは UI の Strategy Name を変えるだけ。
 * Python 側のロジック修正はサーバ再起動だけ (EA 再アップロード不要)。
 *
 * サーバ側の追加方法は server/deciders/registry.py のコメント参照。
 */

// SDK インライン (UserStrategy.js 部分)
class StrategyImplementation {
  get API() { return this.api; }
  OnAttach(api) { this.api = api; }
  Reset() {}
  Done() {}
}

const TOptionType = Object.freeze({
  LONGWORD: 0, INTEGER: 1, DOUBLE: 2, STRING: 3, BOOLEAN: 4,
});
const TTradePositionType = Object.freeze({
  BUY: 0, SELL: 1, BUY_LIMIT: 2, SELL_LIMIT: 3, BUY_STOP: 4, SELL_STOP: 5,
});
const TObjectType = Object.freeze({
  TEXT: 11, TEXT_LABEL: 12,
});
// 色やフォントを後設定するための ObjProp ID (IStrategyProcRec.js より)
const ObjProp = Object.freeze({
  OBJPROP_COLOR: 3,        // 色 (整数 BGR、MT4 慣習)
  OBJPROP_FONTNAME: 14,
  OBJPROP_FONTSIZE: 15,
  OBJPROP_TEXT: 24,
});

// SERVER_URL は OnTick 初回時に組み立てる: wss://<host>:<port>/ws/strategy
// host は固定 localhost、port は UI パラメータで指定。
const SERVER_HOST = "localhost";

export default class ThinClientStrategy extends StrategyImplementation {

  Init() {
    this.api.setStrategyShortName("Thin Client (server-driven)");
    this.api.setStrategyDescription(
      "汎用 thin client。判断は wss://localhost:<port>/ws/strategy の Python サーバ。" +
      "戦略の切り替えはサーバ側の起動オプション (STRATEGY=...) で行う。"
    );

    // サーバの接続ポート。
    // 戦略を切り替えるときは、別 port でサーバを立てて、ここの値を変える。
    //   例: STRATEGY=zigzag_line_break で port 8443、STRATEGY=zigzag_ai で port 8444
    this.serverPort = this.api.createTOptValue_number(8443);
    this.api.RegOption("Server Port", TOptionType.INTEGER, this.serverPort);
    this.api.SetOptionRange("Server Port", 1, 65535);
    this.api.SetOptionDigits("Server Port", 0);

    // パラメータ (サーバへ送る)
    this.magicNumber = this.api.createTOptValue_number(123456);
    this.api.RegOption("Magic Number", TOptionType.INTEGER, this.magicNumber);
    this.api.SetOptionRange("Magic Number", 1, 999999);
    this.api.SetOptionDigits("Magic Number", 0);

    this.riskPct = this.api.createTOptValue_number(1.0);
    this.api.RegOption("Risk % per trade (1=1%)", TOptionType.DOUBLE, this.riskPct);
    this.api.SetOptionRange("Risk % per trade (1=1%)", 0.01, 10.0);
    this.api.SetOptionDigits("Risk % per trade (1=1%)", 2);

    this.enableDraw = this.api.createTOptValue_bool(true);
    this.api.RegOption("Draw on Chart", TOptionType.BOOLEAN, this.enableDraw);

    // 内部状態
    this.sessionId = "s_" + Math.random().toString(36).slice(2, 10);
    this.lastBarTime = null;
    this.ws = null;
    this.wsReady = false;
    this.queue = [];        // wsReady になるまでの送信待ち
    this.cmdQueue = [];     // ★ サーバから受け取った commands を OnTick で実行するためのキュー
    this.drawnNames = new Set();
    // ★ v5: チケット別 trailing メタ情報
    //   PlaceOrder 直後にサーバ提供の entry_price/sl_dist/trail_* を保存し、
    //   OnTick ごとに含み益を監視。+trail_activate_R 達成後、含み益が trail_stop_R
    //   まで戻ったら CloseOrder で安全側に逃げる (FTO は SL 修正不可なので疑似 BE)
    this.orderMeta = {};    // ticket(string) -> {side, entry_price, sl_dist, ...}
    // 自動再接続
    this.shuttingDown = false;           // Done() のとき true
    this.reconnectDelayMs = 2000;        // 最初の待ち時間
    this.reconnectMaxDelayMs = 30000;    // バックオフ上限
    this.reconnectTimer = null;

    // Init 時点では Symbol() が null を返すことが多く、init メッセージで送る
    // symbol が UNKNOWN になってしまうため、WS 接続は OnTick まで遅延し、
    // Symbol() が valid な値を返してから接続を開始する。
    // (this._connectWS() を Init で呼ばない)
  }

  OnTick() {
    try {
      // ★ WS は OnTick で初回接続を行う (Init では Symbol() が null のため遅延)
      if (this.ws === null && !this.shuttingDown) {
        const sym = this._safeSymbol();
        if (sym && sym !== "UNKNOWN") {
          console.log("[ws-ea] symbol ready (" + sym + "), connecting WS now");
          this._connectWS();
        }
        // まだ Symbol が確定していなければ次の tick で再試行 (何もしない)
      }

      // ★ サーバから届いた未処理コマンドをまず OnTick コンテキストで実行する。
      //   onmessage コールバックで直接 PlaceOrder すると "No strategy selected"
      //   で拒否されるため、ここで drain する。
      this._drainCmdQueue();

      // ★ v5: 保有ポジションの trailing close チェック (毎ティック)
      this._checkTrailingClose();

      const currentTime = this.api.Time(0);
      if (currentTime === null || currentTime === undefined) return;
      if (this.lastBarTime !== null && currentTime.valueOf() === this.lastBarTime.valueOf()) {
        return;
      }
      this.lastBarTime = currentTime;
      this._sendTick();
    } catch (e) {
      console.log("[ws-ea] OnTick err: " + (e && e.message ? e.message : e));
    }
  }

  _drainCmdQueue() {
    while (this.cmdQueue.length > 0) {
      const msg = this.cmdQueue.shift();
      this._executeCommandBundle(msg);
    }
  }

  Done() {
    this.shuttingDown = true;
    if (this.reconnectTimer !== null) {
      try { clearTimeout(this.reconnectTimer); } catch (e) {}
      this.reconnectTimer = null;
    }
    try {
      if (this.ws && this.wsReady) {
        this.ws.send(JSON.stringify({ type: "done", session_id: this.sessionId }));
        this.ws.close();
      }
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------------
  // WebSocket lifecycle
  // ------------------------------------------------------------------------
  _connectWS() {
    const port = (this.serverPort && this.serverPort.value) || 8443;
    const url = "wss://" + SERVER_HOST + ":" + port + "/ws/strategy";
    // 既存の WS が残っていたら明示的に閉じてから貼り直す。
    // FTO の Stop / Start / Recompile を繰り返すと、前の WS インスタンスが
    // GC されずに ESTABLISHED のまま蓄積し、サーバ側で同じブラウザから複数
    // セッションが同時に走る原因になっていた。ここで一旦切ってから繋ぐ。
    if (this.ws) {
      try {
        // onclose で再接続が走らないよう、いったんハンドラを外してから close
        this.ws.onopen = null;
        this.ws.onmessage = null;
        this.ws.onerror = null;
        this.ws.onclose = null;
        this.ws.close();
      } catch (e) { /* ignore */ }
      this.ws = null;
    }
    // 接続のたびに session_id を新規発行する。これで「1 ファイル = 1 WS 接続」になり
    // 再接続で同じ session_id が複数ファイルに散ることを防ぐ。
    this.sessionId = "s_" + Math.random().toString(36).slice(2, 10);
    try {
      this.ws = new WebSocket(url);
    } catch (e) {
      console.log("[ws-ea] WS constructor err: " + e.message);
      return;
    }
    this.ws.onopen = () => {
      console.log("[ws-ea] connected " + url + " session=" + this.sessionId);
      // 接続成功したのでバックオフを初期値に戻す
      this.reconnectDelayMs = 2000;
      // 戦略名はサーバ側 (env STRATEGY) で決定する。クライアントからは送らないが、
      // 旧サーバとの互換のため空文字でフィールドだけは入れておく。
      const initMsg = {
        type: "init",
        session_id: this.sessionId,
        symbol: this._safeSymbol(),
        strategy: "",
        params: {
          risk_pct: this.riskPct.value / 100.0,
        },
      };
      this.ws.send(JSON.stringify(initMsg));
    };
    this.ws.onmessage = (ev) => this._onServerMessage(ev.data);
    this.ws.onerror = (ev) => console.log("[ws-ea] error event");
    this.ws.onclose = (ev) => {
      this.wsReady = false;
      console.log("[ws-ea] closed code=" + ev.code +
                  " (will reconnect in " + this.reconnectDelayMs + "ms)");
      this._scheduleReconnect();
    };
  }

  _scheduleReconnect() {
    if (this.shuttingDown) return;            // Done() で意図的に閉じた場合は再接続しない
    if (this.reconnectTimer !== null) return; // 重複スケジュール防止
    const delay = this.reconnectDelayMs;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.shuttingDown) return;
      // 次回失敗時のためバックオフを倍に
      this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, this.reconnectMaxDelayMs);
      console.log("[ws-ea] reconnecting...");
      try { this._connectWS(); }
      catch (e) {
        console.log("[ws-ea] reconnect err: " + (e && e.message ? e.message : e));
        this._scheduleReconnect();
      }
    }, delay);
  }

  _onServerMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch (e) {
      console.log("[ws-ea] bad json from server: " + raw.slice(0, 200));
      return;
    }
    const t = msg.type;
    if (t === "init_ack") {
      if (msg.ok === false) {
        console.log("[ws-ea] init_ack FAILED: " + (msg.error || "unknown") +
                    " (will close + retry)");
        try { this.ws.close(); } catch (e) {}
        // onclose ハンドラが _scheduleReconnect を呼ぶ。次の試行で OnTick が
        // Symbol() を再確認してから繋ぐ。
        return;
      }
      this.wsReady = true;
      console.log("[ws-ea] init_ack session=" + msg.session_id +
                  " server_strategy=" + msg.strategy);
      // queue を流す
      while (this.queue.length > 0) {
        const q = this.queue.shift();
        this.ws.send(q);
      }
    } else if (t === "commands") {
      // ★ 直接実行せずキューに積む。次の OnTick で drain される。
      //   これをしないと PlaceOrder が "No strategy selected" で拒否される。
      this.cmdQueue.push(msg);
    } else if (t === "error") {
      console.log("[ws-ea] server-err: " + msg.msg);
    } else if (t === "bye") {
      console.log("[ws-ea] bye");
    } else {
      console.log("[ws-ea] unknown type: " + t);
    }
  }

  // ------------------------------------------------------------------------
  // データ送信
  // ------------------------------------------------------------------------
  _sendTick() {
    if (!this.ws) return;
    // 直近確定足 (index 1) の OHLC を取得
    let m15Bar;
    try {
      const t = this.api.Time(1);
      m15Bar = {
        time: Math.floor(t.valueOf() / 1000),  // UNIX 秒
        open: this.api.Open(1),
        high: this.api.High(1),
        low:  this.api.Low(1),
        close: this.api.Close(1),
        volume: this.api.Volume(1),
      };
    } catch (e) {
      return;  // まだバーが揃ってない
    }

    // MTF: 直近確定 H1 / H4 (なくても OK)
    const sym = this._safeSymbol();
    const h1Bar = this._fetchMtfBar(sym, 60);
    const h4Bar = this._fetchMtfBar(sym, 240);

    const tickMsg = {
      type: "tick",
      session_id: this.sessionId,
      m15: m15Bar,
      h1: h1Bar,
      h4: h4Bar,
      position: this._currentPosition(),
      balance: this._accountBalance(),
    };
    const payload = JSON.stringify(tickMsg);
    if (this.wsReady) {
      try { this.ws.send(payload); }
      catch (e) { console.log("[ws-ea] send err: " + e.message); }
    } else {
      // init_ack 前なら queue に積む
      if (this.queue.length < 100) this.queue.push(payload);
    }
  }

  _fetchMtfBar(sym, timeframe) {
    try {
      const t = this.api.iTime(sym, timeframe, 1);
      return {
        time: Math.floor(t.valueOf() / 1000),
        open: this.api.iOpen(sym, timeframe, 1),
        high: this.api.iHigh(sym, timeframe, 1),
        low:  this.api.iLow(sym, timeframe, 1),
        close: this.api.iClose(sym, timeframe, 1),
        volume: 0,
      };
    } catch (e) {
      return null;
    }
  }

  _safeSymbol() {
    try { return this.api.Symbol() || "UNKNOWN"; }
    catch (e) { return "UNKNOWN"; }
  }

  _accountBalance() {
    try { return this.api.GetAccountBalance() || 0; }
    catch (e) { return 0; }
  }

  _currentPosition() {
    try {
      const magic = this.magicNumber.value;
      const n = this.api.GetActiveOrderCount();
      for (let i = 0; i < n; i++) {
        this.api.SelectOrder(i, 0, 0);
        if (this.api.GetOrderSymbol() !== this.api.Symbol()) continue;
        if (this.api.GetOrderMagicNumber() !== magic) continue;
        const t = this.api.GetOrderType();
        if (t === TTradePositionType.BUY) return "long";
        if (t === TTradePositionType.SELL) return "short";
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  // ------------------------------------------------------------------------
  // コマンド実行
  // ------------------------------------------------------------------------
  _executeCommandBundle(msg) {
    // log の中身を console に流す
    const logs = msg.logs || [];
    for (const l of logs) console.log("[srv] " + l);

    // 描画 (enableDraw が ON のときだけ)
    const draws = msg.draw || [];
    if (this.enableDraw.value && draws.length > 0) {
      if (!this._drawCount) this._drawCount = 0;
      this._drawCount += draws.length;
      // 最初の draw 受信時に診断ログを出す
      if (!this._firstDrawLogged) {
        this._firstDrawLogged = true;
        const d0 = draws[0];
        console.log("[draw] first draw recv name=" + d0.name +
                    " label=" + d0.label + " color=" + d0.color +
                    " font=" + d0.font_size +
                    " typeof createFTODate=" + typeof this.api.createFTODate +
                    " typeof CreateChartObject=" + typeof this.api.CreateChartObject +
                    " typeof SetObjectText=" + typeof this.api.SetObjectText);
      }
      for (const d of draws) this._applyDraw(d);
    } else if (!this._firstNoDrawLogged) {
      this._firstNoDrawLogged = true;
      console.log("[draw] no draws yet (enableDraw=" + this.enableDraw.value +
                  " draws.length=" + draws.length + ")");
    }

    // 売買コマンド
    const cmds = msg.commands || [];
    for (const cmd of cmds) this._applyCommand(cmd);
  }

  _applyCommand(cmd) {
    try {
      if (cmd.type === "buy") {
        this._placeOrder(TTradePositionType.BUY, cmd, "long");
      } else if (cmd.type === "sell") {
        this._placeOrder(TTradePositionType.SELL, cmd, "short");
      } else if (cmd.type === "close") {
        this._closeAll();
      } else {
        console.log("[ws-ea] unknown command: " + cmd.type);
      }
    } catch (e) {
      console.log("[ws-ea] cmd err " + cmd.type + ": " + (e && e.message ? e.message : e));
    }
  }

  _placeOrder(side, cmd, sideLabel) {
    const sym = this.api.Symbol();
    const sl = (cmd.sl === null || cmd.sl === undefined) ? 0 : cmd.sl;
    const tp = (cmd.tp === null || cmd.tp === undefined) ? 0 : cmd.tp;
    const ticket = this.api.PlaceOrder(
      sym,
      side,
      0,
      cmd.volume,
      sl,
      tp,
      "zigzag_ws",
      this.magicNumber.value
    );
    // ★ v5: trail メタを ticket 単位で保存。サーバ送信値が無ければ trail 無効。
    if (ticket && cmd.entry_price && cmd.sl_dist && cmd.sl_dist > 0) {
      this.orderMeta[String(ticket)] = {
        side: sideLabel,
        entry_price: cmd.entry_price,
        sl_dist: cmd.sl_dist,
        trail_activate_R: (cmd.trail_activate_R !== undefined) ? cmd.trail_activate_R : 1.0,
        trail_stop_R: (cmd.trail_stop_R !== undefined) ? cmd.trail_stop_R : 0.0,
        best_profit_R: 0.0,
      };
    }
  }

  // ★ v5: 全保有ポジションについて含み益を計算し、トレイリング条件で CloseOrder。
  // OnTick から呼ぶ (毎ティック実行 = リアクション速い)。
  _checkTrailingClose() {
    let n;
    try { n = this.api.GetActiveOrderCount(); } catch (e) { return; }
    if (!n || n <= 0) return;
    const magic = this.magicNumber.value;
    let mySym;
    try { mySym = this.api.Symbol(); } catch (e) { return; }
    // 逆方向反復 (CloseOrder で index がずれるため)
    for (let i = n - 1; i >= 0; i--) {
      try {
        this.api.SelectOrder(i, 0, 0);
        if (this.api.GetOrderSymbol() !== mySym) continue;
        if (this.api.GetOrderMagicNumber() !== magic) continue;
        const ticket = String(this.api.GetOrderTicket());
        const meta = this.orderMeta[ticket];
        if (!meta) continue;
        // 現在価格 (close approximation)
        let cur;
        try { cur = this.api.Close(0); } catch (e) { continue; }
        if (cur === null || cur === undefined) continue;
        const profit_R = meta.side === "long"
          ? (cur - meta.entry_price) / meta.sl_dist
          : (meta.entry_price - cur) / meta.sl_dist;
        if (profit_R > meta.best_profit_R) meta.best_profit_R = profit_R;
        if (meta.best_profit_R >= meta.trail_activate_R && profit_R <= meta.trail_stop_R) {
          // 疑似 BE 発動: 含み益が +1R を一度でも達成して、現在は trail_stop_R 以下
          this.api.CloseOrder(this.api.GetOrderTicket());
          delete this.orderMeta[ticket];
          if (!this._trailCloseCount) this._trailCloseCount = 0;
          this._trailCloseCount++;
          if (this._trailCloseCount <= 10) {
            console.log("[trail] CloseOrder ticket=" + ticket +
                        " best_R=" + meta.best_profit_R.toFixed(2) +
                        " cur_R=" + profit_R.toFixed(2));
          }
        }
      } catch (e) { /* skip on iteration error */ }
    }
    // 保持していないチケット (= TP/SL で自然決済済み) のメタ情報を掃除
    for (const t of Object.keys(this.orderMeta)) {
      if (!this._ticketAlive(t, magic, mySym)) delete this.orderMeta[t];
    }
  }

  _ticketAlive(ticketStr, magic, sym) {
    let n;
    try { n = this.api.GetActiveOrderCount(); } catch (e) { return true; }
    for (let i = 0; i < n; i++) {
      try {
        this.api.SelectOrder(i, 0, 0);
        if (this.api.GetOrderSymbol() !== sym) continue;
        if (this.api.GetOrderMagicNumber() !== magic) continue;
        if (String(this.api.GetOrderTicket()) === ticketStr) return true;
      } catch (e) { /* skip */ }
    }
    return false;
  }

  _closeAll() {
    const magic = this.magicNumber.value;
    const sym = this.api.Symbol();
    const n = this.api.GetActiveOrderCount();
    for (let i = n - 1; i >= 0; i--) {
      this.api.SelectOrder(i, 0, 0);
      if (this.api.GetOrderSymbol() !== sym) continue;
      if (this.api.GetOrderMagicNumber() !== magic) continue;
      this.api.CloseOrder(this.api.GetOrderTicket());
    }
  }

  _applyDraw(d) {
    if (!d || !d.name) return;
    if (this.drawnNames.has(d.name)) return;
    try {
      // UTC 引数なしの元のシンプル呼び出しに戻す (UTC=1 で CreateChartObject が
      // false を返すバージョン対策)。横ズレは妥協する。
      let t = null;
      try { t = this.api.createFTODate(d.time * 1000, 1 /* MILLISECONDS */); }
      catch (e) {
        try { t = this.api.createFTODate(d.time, 0 /* SECONDS */); }
        catch (e2) { t = null; }
      }
      if (d.type === "text") {
        if (!t) {
          if (!this._drawFailLog) this._drawFailLog = 0;
          if (this._drawFailLog < 20) {
            this._drawFailLog++;
            console.log("[draw] createFTODate failed for " + d.name + " time=" + d.time);
          }
          return;
        }
        const ok = this.api.CreateChartObject(d.name, TObjectType.TEXT, 0, t, d.price);
        if (!ok) {
          // 最初の 20 件は詳細ログ、その後は 100 件ごとにサマリ
          if (!this._drawFailCount) this._drawFailCount = 0;
          this._drawFailCount++;
          if (this._drawFailCount <= 20) {
            console.log("[draw] CreateChartObject FAIL #" + this._drawFailCount +
                        " name=" + d.name +
                        " time=" + d.time + " price=" + d.price +
                        " t_type=" + (typeof t) +
                        " t_val=" + (t && t.valueOf ? t.valueOf() : t));
          } else if (this._drawFailCount % 100 === 0) {
            console.log("[draw] CreateChartObject FAIL count=" + this._drawFailCount);
          }
          return;
        }
        // 成功時にも最初の数件は記録 (= 完全に動いてないわけではないことの確認)
        if (!this._drawOkCount) this._drawOkCount = 0;
        this._drawOkCount++;
        if (this._drawOkCount <= 5) {
          console.log("[draw] CreateChartObject OK #" + this._drawOkCount +
                      " name=" + d.name + " time=" + d.time);
        }
        // SetObjectText でテキスト・フォント・暫定色 (白) をセット。
        // SetObjectText の color は何を渡しても反映されない FTO 仕様。
        this.api.SetObjectText(d.name, d.label, d.font_size || 12, "Arial", "White", false);
        // 本当の色は SetObjectProperty(OBJPROP_COLOR) で適用。
        // 直に HEX 文字列を渡すと FTO 内パースで cyan→青, pink→紫 のような偏りが
        // 出ることがあるため、ConvertColorToARGB で明示的に ARGB 整数化してから渡す。
        let colorApplied = false;
        let colorArgb = null;
        const colorIn = (typeof d.color === "string" && d.color.length > 0) ? d.color : "#FFFFFF";
        try {
          if (typeof this.api.ConvertColorToARGB === "function") {
            colorArgb = this.api.ConvertColorToARGB(colorIn);
          }
        } catch (e) { colorArgb = null; }
        try {
          const valueToSet = (typeof colorArgb === "number") ? colorArgb : colorIn;
          colorApplied = !!this.api.SetObjectProperty(d.name, ObjProp.OBJPROP_COLOR, valueToSet, false);
        } catch (e) { /* color setting failure is non-fatal */ }
        this.drawnNames.add(d.name);
        if (!this._firstDrawAppliedLogged) {
          this._firstDrawAppliedLogged = true;
          console.log("[draw] first object created OK name=" + d.name +
                      " color_in=" + d.color +
                      " argb=" + colorArgb +
                      " colorApplied=" + colorApplied);
        }
      }
    } catch (e) {
      if (!this._firstDrawErrLogged) {
        this._firstDrawErrLogged = true;
        console.log("[draw] err in _applyDraw: " + (e && e.message ? e.message : e));
      }
    }
  }
}
