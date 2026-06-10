/**
 * tod_strategy.js — Time-of-Day FX/Gold 戦略 (スタンドアロン EA、サーバ不要)
 *
 * edge_scanner.py で発見した「過去 5.5 年で両期間 ROBUST な時間帯バイアス」を
 * EA に直接埋め込んだ版。WebSocket レイテンシなしで正確な時刻実行可能。
 *
 * 戦略:
 *   EURUSD/GBPUSD/XAUUSD: 21:00 UTC で BUY、22:00 UTC で CLOSE
 *   USDJPY:                20:00 UTC で SELL、21:00 UTC で CLOSE
 *   SL = 0.5 × ATR(14)、TP = 1.0 × ATR(14)
 *   リスク 1% / トレード (lot は手動指定または自動計算)
 *
 * バックテスト結果 (replay 5.5 年):
 *   月利 +19.9% (理論)、両期間 (2021-23、2024-26) で ROBUST
 *   WR 56%、avg R +0.38、3443 trades
 *
 * 使い方:
 *   1. FTO の M15 チャート (EURUSD/GBPUSD/USDJPY/XAUUSD のいずれか) に貼る
 *   2. UI で Lot Size か Risk % のどちらを使うか選択
 *   3. Backtest 開始 (期間は録音と被らない方が OOS 評価できる)
 *
 * EA 単体動作のためサーバ不要。
 */

// ────────────────────────────────────────────────────────────────
// SDK インライン (UserStrategy.js 部分)
// ────────────────────────────────────────────────────────────────
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

// ────────────────────────────────────────────────────────────────
// 戦略設定 — 編集する場合はここだけ
// ────────────────────────────────────────────────────────────────
const ATR_PERIOD = 14;
const SL_ATR_MULT = 0.5;
const TP_ATR_MULT = 1.0;

// シンボル別ルール: 該当シンボルなら entry_hour に direction でエントリー、
// exit_hour に強制クローズ。
const SYMBOL_RULES = {
  "EURUSD": { entry_hour: 21, direction: "buy",  exit_hour: 22 },
  "GBPUSD": { entry_hour: 21, direction: "buy",  exit_hour: 22 },
  "XAUUSD": { entry_hour: 21, direction: "buy",  exit_hour: 22 },
  "USDJPY": { entry_hour: 20, direction: "sell", exit_hour: 21 },
};

// 1% リスクでロット自動計算するための pip 値テーブル (口座通貨 USD 想定)。
// pip_size = 価格 1 pip 分、pip_value = 1 lot × 1 pip の USD 換算。
// これらは broker / 通貨ペアで多少違うので、近似値として使用。
const PIP_INFO_DEFAULT = {
  "EURUSD": { pip_size: 0.0001, pip_value: 10 },
  "GBPUSD": { pip_size: 0.0001, pip_value: 10 },
  "USDJPY": { pip_size: 0.01,   pip_value: 6.7 },  // 1 lot × 1 pip / USDJPY rate (≈ 150)
  "XAUUSD": { pip_size: 0.01,   pip_value: 1 },    // 1 lot = 100 oz、 1 cent move = $1
};

// ────────────────────────────────────────────────────────────────
// 描画カラー
// ────────────────────────────────────────────────────────────────
const COLOR_BUY = "#00FFFF";   // Cyan
const COLOR_SELL = "#FF69B4";  // Hot Pink
const COLOR_CLOSE = "#FFA500"; // Orange

const ObjProp = Object.freeze({
  OBJPROP_COLOR: 3,
  OBJPROP_FONTNAME: 14,
  OBJPROP_FONTSIZE: 15,
  OBJPROP_TEXT: 24,
});
const TObjectType = Object.freeze({ TEXT: 11, TEXT_LABEL: 12 });

// ────────────────────────────────────────────────────────────────
// メイン
// ────────────────────────────────────────────────────────────────
export default class TimeOfDayStrategy extends StrategyImplementation {

  Init() {
    this.api.setStrategyShortName("Time-of-Day v1");
    this.api.setStrategyDescription(
      "21UTC BUY (EUR/GBP/XAU) + 20UTC SELL (JPY). " +
      "ATR-based SL/TP. M15 timeframe required."
    );

    // パラメータ
    this.magicNumber = this.api.createTOptValue_number(910001);
    this.api.RegOption("Magic Number", TOptionType.INTEGER, this.magicNumber);
    this.api.SetOptionRange("Magic Number", 1, 999999);
    this.api.SetOptionDigits("Magic Number", 0);

    this.riskPct = this.api.createTOptValue_number(1.0);
    this.api.RegOption("Risk % per trade (auto lot)", TOptionType.DOUBLE, this.riskPct);
    this.api.SetOptionRange("Risk % per trade (auto lot)", 0.01, 10.0);
    this.api.SetOptionDigits("Risk % per trade (auto lot)", 2);

    this.manualLot = this.api.createTOptValue_number(0.0);
    this.api.RegOption("Manual Lot (0 = auto by risk)", TOptionType.DOUBLE, this.manualLot);
    this.api.SetOptionRange("Manual Lot (0 = auto by risk)", 0, 100);
    this.api.SetOptionDigits("Manual Lot (0 = auto by risk)", 2);

    this.enableDraw = this.api.createTOptValue_bool(true);
    this.api.RegOption("Draw on Chart", TOptionType.BOOLEAN, this.enableDraw);

    // 内部状態
    this.sessionId = "tod_" + Math.random().toString(36).slice(2, 8);
    this.lastBarTime = null;
    this.exitHour = null;      // 保有中ならクローズすべき UTC 時刻
    this.entryCount = 0;       // 描画 dedup 用カウンタ

    // ★ 非同期ロガー (取引ロジックには影響しない fire-and-forget)
    // wss://localhost:8443/ws/logs に接続を貼って、定期的にバッファをまとめて送信。
    // 接続失敗・送信失敗してもトレーディング処理は継続する。
    this.logBuffer = [];
    this.logWS = null;
    this.logWSReady = false;
    this.logReconnectAt = 0;
    this.logMaxBuffer = 500;     // バッファ上限 (溢れたら古いものから捨てる)
    this.logFlushEveryN = 5;     // この件数以上溜まったら送信
    this._initLogger();

    this._log("Init", "tod strategy initialized session=" + this.sessionId);
  }

  // ────────────────────────────────────────────────────────────────
  // 非同期ロガー
  // ────────────────────────────────────────────────────────────────

  _initLogger() {
    try {
      this.logWS = new WebSocket("wss://localhost:8443/ws/logs");
      this.logWS.onopen = () => {
        this.logWSReady = true;
        this._flushLogs();
      };
      this.logWS.onmessage = (ev) => { /* discard */ };
      this.logWS.onerror = (ev) => { /* discard */ };
      this.logWS.onclose = (ev) => {
        this.logWSReady = false;
        this.logReconnectAt = Date.now() + 5000;  // 5s 後に再接続試行
      };
    } catch (e) {
      this.logWS = null;
      this.logWSReady = false;
    }
  }

  // ローカル console と同時にリモートバッファへ積む。
  // 第 1 引数 = タグ (entry / exit / error / info 等)、第 2 引数 = 本文。
  _log(tag, msg) {
    const line = "[tod-" + tag + "] " + msg;
    try { console.log(line); } catch (e) {}
    if (this.logBuffer.length >= this.logMaxBuffer) {
      this.logBuffer.shift();  // 古いものから捨てる
    }
    this.logBuffer.push({
      ts: Date.now(),
      session: this.sessionId,
      symbol: this._safeSymbol(),
      tag: tag,
      msg: msg,
    });
  }

  // バッファをサーバへ送信 (fire-and-forget、失敗してもエラー無視)
  _flushLogs() {
    if (this.logBuffer.length === 0) return;
    // 接続切れてたら再接続を試行
    if (this.logWS === null || !this.logWSReady) {
      if (Date.now() >= this.logReconnectAt) {
        this._initLogger();
      }
      return;
    }
    try {
      // バッファ全部を一括送信 (= 1 メッセージで複数行)
      const batch = this.logBuffer.splice(0);
      this.logWS.send(JSON.stringify({
        type: "ea_logs",
        entries: batch,
      }));
    } catch (e) {
      // 送信失敗。ログそのものは捨てる (重要度低いので)
    }
  }

  OnTick() {
    // ログを定期的に flush (= 取引判定と独立、 fire-and-forget)
    if (this.logBuffer.length >= this.logFlushEveryN) {
      this._flushLogs();
    }
    try {
      // ★ 新規確定足の検出。確定足 (= 1 つ前のバー) でのみロジック動かす。
      // FTO の Time(0) は未確定バー、Time(1) は直近確定バー。
      const time0 = this.api.Time(0);
      if (time0 === null || time0 === undefined) return;
      if (this.lastBarTime !== null && time0.valueOf() === this.lastBarTime.valueOf()) {
        return;
      }
      this.lastBarTime = time0;

      // 確定バーの時刻を取得
      const closedTime = this.api.Time(1);
      if (closedTime === null || closedTime === undefined) return;
      const dt = new Date(closedTime.valueOf());
      const hour = dt.getUTCHours();
      const minute = dt.getUTCMinutes();

      const sym = this._safeSymbol();
      if (!sym || sym === "UNKNOWN") return;

      const rules = SYMBOL_RULES[sym];
      if (!rules) {
        // 対象外シンボル、無視 (ログ出すだけ)
        if (!this._logged_unsupported) {
          this._logged_unsupported = true;
          this._log("info", "Symbol " + sym + " has no entry rules; idling.");
        }
        return;
      }

      const pos = this._currentPosition();

      // === 退場ロジック (保有中) ===
      if (pos !== null) {
        if (this.exitHour !== null && hour === this.exitHour && minute === 0) {
          this._log("exit", sym + " closed at " + this._pad2(hour) + ":00 UTC (time exit) bal=" + this.api.GetAccountBalance());
          this._closeAll();
          this._drawMarker(closedTime, this.api.Close(1), "CLOSE", COLOR_CLOSE);
          this.exitHour = null;
        }
        return;
      }

      // === 新規エントリー ===
      if (hour !== rules.entry_hour || minute !== 0) return;

      // ATR(14) 計算
      const atrVal = this._computeATR(ATR_PERIOD);
      if (atrVal === null || atrVal <= 0) {
        this._log("skip", sym + " ATR not ready, skip entry");
        return;
      }

      const entryPrice = this.api.Close(1);
      if (entryPrice === null || entryPrice === undefined) return;

      const slDist = SL_ATR_MULT * atrVal;
      const tpDist = TP_ATR_MULT * atrVal;
      let sl, tp, side, sideLabel;
      if (rules.direction === "buy") {
        sl = entryPrice - slDist;
        tp = entryPrice + tpDist;
        side = TTradePositionType.BUY;
        sideLabel = "BUY";
      } else {
        sl = entryPrice + slDist;
        tp = entryPrice - tpDist;
        side = TTradePositionType.SELL;
        sideLabel = "SELL";
      }

      // ロット決定: manualLot > 0 なら固定、それ以外は risk% から自動計算
      let vol = this.manualLot.value;
      if (vol <= 0) {
        vol = this._riskBasedLot(sym, slDist);
      }
      if (vol <= 0) {
        this._log("skip", sym + " vol <= 0, skip entry");
        return;
      }

      // 注文
      const ticket = this.api.PlaceOrder(
        sym, side, 0, vol, sl, tp, "tod_" + this.sessionId, this.magicNumber.value
      );
      if (!ticket) {
        this._log("error", sym + " PlaceOrder failed");
        return;
      }
      this._log("entry",
        sym + " " + sideLabel +
        " at " + this._pad2(hour) + ":00 UTC" +
        " price=" + entryPrice.toFixed(5) +
        " sl=" + sl.toFixed(5) + " tp=" + tp.toFixed(5) +
        " vol=" + vol.toFixed(3) +
        " atr=" + atrVal.toFixed(5) +
        " ticket=" + ticket +
        " bal=" + this.api.GetAccountBalance());
      this.exitHour = rules.exit_hour;
      this._drawMarker(closedTime, entryPrice, sideLabel,
                       rules.direction === "buy" ? COLOR_BUY : COLOR_SELL);

    } catch (e) {
      this._log("error", "OnTick exception: " + (e && e.message ? e.message : e));
    }
  }

  Done() {
    // 終了時に保有ポジションがあれば強制クローズ + 残ログを送信
    try { this._closeAll(); } catch (e) {}
    try { this._log("done", "session ended"); this._flushLogs(); } catch (e) {}
    try { if (this.logWS) this.logWS.close(); } catch (e) {}
  }

  // ────────────────────────────────────────────────────────────────
  // ヘルパ
  // ────────────────────────────────────────────────────────────────

  _safeSymbol() {
    try { return this.api.Symbol() || "UNKNOWN"; }
    catch (e) { return "UNKNOWN"; }
  }

  _pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  // ATR(period) を確定バーから計算 (Wilder 法ではなく単純移動平均)。
  // Python 側 src/core/indicators.py の atr() と一致。
  _computeATR(period) {
    let sumTR = 0;
    for (let i = 1; i <= period; i++) {
      const h = this.api.High(i);
      const l = this.api.Low(i);
      const pc = this.api.Close(i + 1);
      if (h === null || l === null || pc === null ||
          h === undefined || l === undefined || pc === undefined) {
        return null;
      }
      const tr = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
      sumTR += tr;
    }
    return sumTR / period;
  }

  // 1% リスクでロット計算。pip 値テーブルを参照。
  _riskBasedLot(sym, slDist) {
    const balance = this.api.GetAccountBalance();
    if (!balance || balance <= 0) return 0;
    const info = PIP_INFO_DEFAULT[sym] || { pip_size: 0.0001, pip_value: 10 };
    if (info.pip_size <= 0 || info.pip_value <= 0) return 0;
    const pipsAtRisk = slDist / info.pip_size;
    const moneyPerLot = pipsAtRisk * info.pip_value;
    if (moneyPerLot <= 0) return 0;
    const riskAmount = balance * (this.riskPct.value / 100);
    const lot = riskAmount / moneyPerLot;
    // 下限/上限丸め
    return Math.max(0, Math.round(lot * 100) / 100);
  }

  _currentPosition() {
    try {
      const magic = this.magicNumber.value;
      const sym = this.api.Symbol();
      const n = this.api.GetActiveOrderCount();
      for (let i = 0; i < n; i++) {
        this.api.SelectOrder(i, 0, 0);
        if (this.api.GetOrderSymbol() !== sym) continue;
        if (this.api.GetOrderMagicNumber() !== magic) continue;
        const t = this.api.GetOrderType();
        if (t === TTradePositionType.BUY) return "long";
        if (t === TTradePositionType.SELL) return "short";
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  _closeAll() {
    try {
      const magic = this.magicNumber.value;
      const sym = this.api.Symbol();
      const n = this.api.GetActiveOrderCount();
      for (let i = n - 1; i >= 0; i--) {
        this.api.SelectOrder(i, 0, 0);
        if (this.api.GetOrderSymbol() !== sym) continue;
        if (this.api.GetOrderMagicNumber() !== magic) continue;
        this.api.CloseOrder(this.api.GetOrderTicket());
      }
    } catch (e) { /* ignore */ }
  }

  // チャート描画 (Draw on Chart が ON のときだけ)
  _drawMarker(timeObj, price, label, hexColor) {
    if (!this.enableDraw.value) return;
    this.entryCount++;
    const name = this.sessionId + "_" + this.entryCount + "_" + label;
    try {
      const ok = this.api.CreateChartObject(name, TObjectType.TEXT, 0, timeObj, price);
      if (!ok) return;
      this.api.SetObjectText(name, label, 14, "Arial", "White", false);
      // 色は ARGB 整数で設定 (HEX 文字列直渡しは broker 依存)
      let colorArgb = null;
      try {
        if (typeof this.api.ConvertColorToARGB === "function") {
          colorArgb = this.api.ConvertColorToARGB(hexColor);
        }
      } catch (e) { colorArgb = null; }
      try {
        const v = (typeof colorArgb === "number") ? colorArgb : hexColor;
        this.api.SetObjectProperty(name, ObjProp.OBJPROP_COLOR, v, false);
      } catch (e) { /* color is best-effort */ }
    } catch (e) { /* drawing is best-effort */ }
  }
}
