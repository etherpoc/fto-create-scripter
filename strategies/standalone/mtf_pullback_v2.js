/**
 * mtf_pullback_v2.js — スタンドアロン FTO EA (サーバ非依存)
 *
 * strategies/mtf_pullback/strategy.py の v2 (skip_on_trendline_break=True) を
 * 丸ごと FTO 上の JavaScript に移植したもの。トレード判断はすべてこの EA 内で
 * 完結する。ローカル Python サーバとの WebSocket 往復は行わない (= ラグなし)。
 *
 * ログだけは fire-and-forget で https://localhost:<port>/log に POST する
 * (server/log_collector.py が data/fto_mtf_pb_v2_live/ に JSONL 保存)。
 * POST は await しないのでトレード経路の遅延はゼロ。POST 失敗時も console.log には残る。
 *
 * ==== 必ず M5 チャートで動かすこと ====
 * 戦略のベース足は M5。M15/M30 は M5 から内部集計し、H1/H4 は iXxx で取得する。
 * (検証済みバックテストと同じデータ構成: M5 base + 内部 M15/M30 + iXxx H1/H4)
 *
 * ==== ロジック (mtf_pullback v2 + 2026-06-12 改善) ====
 *   エントリー: 大局アラインメント (既定 H1=M15 のみ。H4/M30 は検証で無関係と判明) +
 *              M5 が直近で逆方向(押し戻し) + M5 が大局方向へ転換した瞬間 + クールダウン +
 *              時間帯フィルタ(既定 6-10時UTC除外) + [v2] トレンドライン未ブレイク +
 *              room_R フィルタ(直近M15高安までの余地/SL < 2.0 = タイトSL除外)
 *   SL: ロング→直近 M15 安値ピボット / ショート→直近 M15 高値ピボット (構造 anchor)
 *   TP: entry ± sl_dist × RR (既定 1.5)
 *   サイズ: 口座 risk_pct% を sl_dist で逆算 (口座通貨建てに正しく換算)
 *   決済: SL/TP は PlaceOrder に渡して FTO ネイティブに任せる (trailing なし)
 *
 * FTO API の癖は docs/fto_api_reference.md / 過去の thin client で検証済みのものを踏襲。
 *   - 確定足は index 1 以上 (index 0 は未確定)。Time(0) 変化で新足検出。
 *   - メソッドは PascalCase。iTime/iOpen/... の timeframe 単位は「分」(H1=60,H4=240)。
 *   - import 禁止、export default class extends StrategyImplementation、SDK インライン。
 */

// ============================================================================
// SDK インライン (UserStrategy.js 部分。import は FTO で解決できないため直書き)
// ============================================================================
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

const LOG_HOST = "localhost";

// ============================================================================
// 指標部品 (src/core/indicators.py の ZigZagTracker / atr / _dow_trend を移植)
// ============================================================================

/**
 * ZigZagTracker (メモリ軽量版)。
 * Python 版は全バーを self.bars に貯めるが、検出に必要なのは直近 (2*depth+1) 本
 * だけなので、リングウィンドウ + 絶対 index カウンタで等価に実装する。
 * pivot.index / count は「投入したバー列上の絶対位置」を表し、Python 版と一致する。
 */
class ZigZag {
  constructor(depth, deviation) {
    this.depth = depth;
    this.deviation = deviation;
    this.win = [];          // 直近 (2*depth+1) 本のバー {time,high,low,close,...}
    this.count = 0;         // 投入した総バー数 (= Python の len(self.bars))
    this.pivots = [];       // {index,time,kind,price} 確定ピボット (末尾が最新)
    this._maxPivots = 64;   // last 4 / last 2-same-kind しか使わないので上限を設ける
  }

  update(bar) {
    this.count += 1;
    const W = 2 * this.depth + 1;
    this.win.push(bar);
    if (this.win.length > W) this.win.shift();
    // Python: idx = len-1-depth; if idx < depth: return  →  count < 2*depth+1 はスキップ
    if (this.count < W) return;
    // candidate は中央 (win[depth])。絶対 index は count-1-depth。
    const candidate = this.win[this.depth];
    const idx = this.count - 1 - this.depth;
    let isHigh = true, isLow = true;
    for (let k = 0; k < W; k++) {
      if (k === this.depth) continue;
      const o = this.win[k];
      if (o.high >= candidate.high) isHigh = false;
      if (o.low <= candidate.low) isLow = false;
      if (!isHigh && !isLow) break;
    }
    if (!isHigh && !isLow) return;

    let newKind;
    if (isHigh && isLow) {
      newKind = (this.pivots.length && this.pivots[this.pivots.length - 1].kind === "high") ? "low" : "high";
    } else {
      newKind = isHigh ? "high" : "low";
    }
    const price = (newKind === "high") ? candidate.high : candidate.low;

    if (this.pivots.length === 0) {
      this._push({ index: idx, time: candidate.time, kind: newKind, price: price });
      return;
    }
    const last = this.pivots[this.pivots.length - 1];
    if (newKind === last.kind) {
      // 同方向: より極端になったときだけ更新
      if ((newKind === "high" && price > last.price) || (newKind === "low" && price < last.price)) {
        this.pivots[this.pivots.length - 1] = { index: idx, time: candidate.time, kind: newKind, price: price };
      }
      return;
    }
    // 逆方向: deviation 未満なら無視
    if (Math.abs(price - last.price) < this.deviation) return;
    this._push({ index: idx, time: candidate.time, kind: newKind, price: price });
  }

  _push(p) {
    this.pivots.push(p);
    if (this.pivots.length > this._maxPivots) this.pivots.shift();
  }
}

/** 直近 4 ピボットから Dow トレンド ("up"/"down"/null)。Python _dow_trend と等価。 */
function dowTrend(pivots) {
  const n = pivots.length;
  if (n < 4) {
    if (n >= 2) {
      const last = pivots[n - 1], prev = pivots[n - 2];
      if (last.kind === "high" && prev.kind === "low") return last.price > prev.price ? "up" : null;
      if (last.kind === "low" && prev.kind === "high") return last.price < prev.price ? "down" : null;
    }
    return null;
  }
  const last4 = pivots.slice(n - 4);
  const highs = last4.filter(p => p.kind === "high");
  const lows = last4.filter(p => p.kind === "low");
  if (highs.length >= 2 && lows.length >= 2) {
    const hh = highs[highs.length - 1].price > highs[highs.length - 2].price;
    const hl = lows[lows.length - 1].price > lows[lows.length - 2].price;
    const ll = lows[lows.length - 1].price < lows[lows.length - 2].price;
    const lh = highs[highs.length - 1].price < highs[highs.length - 2].price;
    if (hh && hl) return "up";
    if (ll && lh) return "down";
  }
  return null;
}

function opposite(d) { return d === "up" ? "down" : (d === "down" ? "up" : null); }

/**
 * 直近 2 つの同種ピボットからトレンドラインを引き、現在価格との距離 (符号) を返す。
 * Python _trendline_dist_atr と等価。v2 は符号だけ見るので atr_val は >0 なら何でもよい。
 * trend=='up' → 2 lows の昇支持線。戻り値 <0 で下抜け。
 * trend=='down' → 2 highs の降抵抗線。戻り値 >0 で上抜け。
 */
function trendlineDist(pivots, curIdx, curPrice, atrVal, trend) {
  if (trend !== "up" && trend !== "down") return null;
  const kind = (trend === "up") ? "low" : "high";
  const same = pivots.filter(p => p.kind === kind);
  if (same.length < 2) return null;
  const P1 = same[same.length - 2], P2 = same[same.length - 1];
  if (P2.index <= P1.index) return null;
  const slope = (P2.price - P1.price) / (P2.index - P1.index);
  const lineNow = P2.price + slope * (curIdx - P2.index);
  if (atrVal <= 0) return null;
  return (curPrice - lineNow) / atrVal;
}

// ============================================================================
// EA 本体
// ============================================================================
export default class MtfPullbackV2 extends StrategyImplementation {

  Init() {
    this.api.setStrategyShortName("mtf_pullback v2 (standalone)");
    this.api.setStrategyDescription(
      "MTF押し目 + v2トレンドラインskip。判断は全てEA内 (サーバ非依存)。M5チャートで使用。"
    );

    // ---- パラメータ (FTO UI) ----
    this.riskPct = this.api.createTOptValue_number(1.0);        // 1.0 = 口座1%/トレード
    this.api.RegOption("Risk % per trade (1=1%)", TOptionType.DOUBLE, this.riskPct);
    this.api.SetOptionRange("Risk % per trade (1=1%)", 0.01, 10.0);
    this.api.SetOptionDigits("Risk % per trade (1=1%)", 2);

    this.magicNumber = this.api.createTOptValue_number(220611);
    this.api.RegOption("Magic Number", TOptionType.INTEGER, this.magicNumber);
    this.api.SetOptionRange("Magic Number", 1, 9999999);
    this.api.SetOptionDigits("Magic Number", 0);

    this.maxLot = this.api.createTOptValue_number(50.0);        // 安全キャップ
    this.api.RegOption("Max Lot (safety cap)", TOptionType.DOUBLE, this.maxLot);
    this.api.SetOptionRange("Max Lot (safety cap)", 0.01, 100000.0);
    this.api.SetOptionDigits("Max Lot (safety cap)", 2);

    this.logPort = this.api.createTOptValue_number(8443);       // log_collector のポート
    this.api.RegOption("Log Server Port (0=off)", TOptionType.INTEGER, this.logPort);
    this.api.SetOptionRange("Log Server Port (0=off)", 0, 65535);
    this.api.SetOptionDigits("Log Server Port (0=off)", 0);

    // JPY クロス等で USDJPY をクロス参照できなかった場合のフォールバックレート
    this.usdJpyFallback = this.api.createTOptValue_number(150.0);
    this.api.RegOption("USDJPY fallback (sizing)", TOptionType.DOUBLE, this.usdJpyFallback);
    this.api.SetOptionRange("USDJPY fallback (sizing)", 50.0, 300.0);
    this.api.SetOptionDigits("USDJPY fallback (sizing)", 2);

    // ★ TP の RR 倍率。SL リスクは 1% のまま、TP = entry ± sl_dist × tpRR。
    //   5.5y 検証で RR1.5 が最も広く頑健 (全12/主要4 とも ROBUST)。RR2.0 は強ペアで更に高い。
    this.tpRR = this.api.createTOptValue_number(1.5);
    this.api.RegOption("TP RR (1.5=1:1.5, 2.0=1:2)", TOptionType.DOUBLE, this.tpRR);
    this.api.SetOptionRange("TP RR (1.5=1:1.5, 2.0=1:2)", 0.5, 5.0);
    this.api.SetOptionDigits("TP RR (1.5=1:1.5, 2.0=1:2)", 1);

    // ★ アラインメント階層数。1=H1+M15のみ(新ベスト) / 2=H4+H1+M15 / 3=H4+H1+M30+M15(旧)
    //   検証で H4/M30 は勝敗に無関係と判明 → 既定は H1+M15。
    this.alignMode = this.api.createTOptValue_number(1);
    this.api.RegOption("Align (1=H1+M15, 2=+H4, 3=+H4+M30)", TOptionType.INTEGER, this.alignMode);
    this.api.SetOptionRange("Align (1=H1+M15, 2=+H4, 3=+H4+M30)", 1, 3);

    // ★ room_R フィルタ: 直近 M15 高安までの余地/SL がこの値超なら skip (タイトSL=ノイズ負け除外)。0=off
    this.roomRMax = this.api.createTOptValue_number(2.0);
    this.api.RegOption("room_R max (0=off)", TOptionType.DOUBLE, this.roomRMax);
    this.api.SetOptionRange("room_R max (0=off)", 0.0, 10.0);
    this.api.SetOptionDigits("room_R max (0=off)", 1);

    // ★ 時間帯フィルタ: [start, end) UTC のエントリーを skip (6-10時=ロンドン午前の高ボラ)。-1=off
    this.blockHourStart = this.api.createTOptValue_number(6);
    this.api.RegOption("Block hour start UTC (-1=off)", TOptionType.INTEGER, this.blockHourStart);
    this.api.SetOptionRange("Block hour start UTC (-1=off)", -1, 23);
    this.blockHourEnd = this.api.createTOptValue_number(10);
    this.api.RegOption("Block hour end UTC", TOptionType.INTEGER, this.blockHourEnd);
    this.api.SetOptionRange("Block hour end UTC", 0, 24);

    // ---- 戦略パラメータ (Python Params の既定値と一致) ----
    this.P = {
      zz_depth_m5: 5, zz_depth_m15: 8, zz_depth_m30: 10, zz_depth_h1: 12, zz_depth_h4: 12,
      zz_dev_pips: 3.0,
      atr_period: 14,
      pullback_lookback_bars: 30,
      min_sl_dist_atr: 0.3,
      max_sl_dist_atr: 5.0,
      cooldown_bars: 6,
      skip_on_trendline_break: true,   // ★ v2
    };

    // ---- 内部状態 ----
    this.sessionId = "ea_" + Math.random().toString(36).slice(2, 10);
    this.lastBarTime = null;
    this.symbol = null;
    this.pipSize = null;           // ZigZag deviation 用。検証済み backtest と同じく 0.0001 固定
    this.accountCcy = "USD";

    this.zz = null;                // Init では Symbol() が null のため OnTick 初回で生成
    this.initedTrackers = false;

    this.m5buf = [];               // 直近 M5 確定足 (M15/M30 集計 + ATR 用)
    this.m5trendHist = [];         // M5 トレンド履歴 (押し戻し検出)
    this.lastM15Time = -1;
    this.lastM30Time = -1;
    this.lastH1Time = -1;
    this.lastH4Time = -1;

    this.barIdx = -1;
    this.lastEntryBarIdx = -1000000000;
    this.prevPosition = null;
    this.pendingEntry = null;      // outcome ログ用に直近エントリーを覚える
    this.prevM5Time = null;        // 足間隔の自己診断用
    this._intervalLogged = false;
    this._intervalSamples = [];    // 直近差分サンプル (最小=真の足間隔)

    this._loggedStart = false;

    // 診断: エントリー条件のファネル (どのゲートで落ちているか可視化)
    this.diag = { bars: 0, align: 0, pull: 0, flip: 0, cd: 0, tlpass: 0, entries: 0, skips: 0 };
    this.diagEvery = 500;          // この本数ごとに heartbeat を出す
  }

  OnTick() {
    try {
      const sym = this._safeSymbol();
      if (!sym || sym === "UNKNOWN") return;     // Symbol 未確定なら待つ
      if (!this.initedTrackers) this._initTrackers(sym);

      // 新しい確定足 (M5) の検出: Time(0) が変わったら 1 本進んだ
      const t0 = this.api.Time(0);
      if (t0 === null || t0 === undefined) return;
      if (this.lastBarTime !== null && t0.valueOf() === this.lastBarTime.valueOf()) return;
      this.lastBarTime = t0;

      // 直近確定 M5 (index 1) を取り出す
      let m5;
      try {
        const t1 = this.api.Time(1);
        m5 = {
          time: Math.floor(t1.valueOf() / 1000),
          open: this.api.Open(1), high: this.api.High(1),
          low: this.api.Low(1), close: this.api.Close(1),
          volume: this.api.Volume(1),
        };
      } catch (e) { return; }   // まだバーが揃っていない

      this._onBar(m5);
    } catch (e) {
      console.log("[mtfpb] OnTick err: " + (e && e.message ? e.message : e));
    }
  }

  Done() {
    this._log({ type: "session_end", bar_idx: this.barIdx });
  }

  // --------------------------------------------------------------------------
  _initTrackers(sym) {
    this.symbol = sym;
    // 検証済み backtest は全ペア pip_size=0.0001 で ZigZag deviation を計算していた。
    // 同じシグナルを再現するため deviation 用 pip は 0.0001 固定 (サイズ計算とは別物)。
    this.pipSize = 0.0001;
    try { this.accountCcy = (this.api.GetAccountCurrency() || "USD").toUpperCase(); }
    catch (e) { this.accountCcy = "USD"; }
    const dev = this.P.zz_dev_pips * this.pipSize;
    this.zz = {
      m5: new ZigZag(this.P.zz_depth_m5, dev),
      m15: new ZigZag(this.P.zz_depth_m15, dev),
      m30: new ZigZag(this.P.zz_depth_m30, dev),
      h1: new ZigZag(this.P.zz_depth_h1, dev),
      h4: new ZigZag(this.P.zz_depth_h4, dev),
    };
    this.initedTrackers = true;
    if (!this._loggedStart) {
      this._loggedStart = true;
      let tf = null;
      try { tf = this.api.Timeframe(); } catch (e) { tf = null; }
      this._log({
        type: "session_start", strategy: "mtf_pullback_v2_standalone",
        account_ccy: this.accountCcy, risk_pct: this.riskPct.value,
        magic: this.magicNumber.value, tp_rr: this.tpRR.value,
        align_mode: this.alignMode.value, room_R_max: this.roomRMax.value,
        block_hours: [this.blockHourStart.value, this.blockHourEnd.value],
        chart_timeframe: tf,          // ★ チャート時間足 (要 M5)。診断用に必ず残す
        expected_base_sec: 300,
        params: this.P,
      });
      console.log("[mtfpb] started sym=" + sym + " ccy=" + this.accountCcy +
        " timeframe=" + tf + " session=" + this.sessionId);
    }
  }

  // strategy.py の on_bar(ctx) 移植
  _onBar(m5) {
    const P = this.P;
    this.barIdx += 1;

    // ---- チャート時間足の自己診断 ----
    // ベース足は M5 (300s) が絶対前提。違うと上位足集計が一切働かず
    // (m5.time+300)%900 等が成立しない → トレンド全 null → エントリー永遠ゼロ。
    // ログ一発で気付けるよう実測する。週末ギャップ等で単発の差分は大きくなるため、
    // 直近 20 本の差分の「最小値」を真の足間隔とみなす (ギャップは間隔を増やすだけ)。
    if (this.prevM5Time !== null && !this._intervalLogged) {
      const dt = m5.time - this.prevM5Time;
      if (dt > 0) this._intervalSamples.push(dt);
      if (this._intervalSamples.length >= 20) {
        this._intervalLogged = true;
        const detected = Math.min.apply(null, this._intervalSamples);
        this._log({ type: "bar_interval", detected_sec: detected, expected_sec: 300, ok: (detected === 300) });
        if (detected !== 300) {
          console.log("[mtfpb] !!! WRONG CHART TIMEFRAME: 検出足間隔=" + detected +
            "s (M5=300s が必要)。上位足集計が働かずエントリーされません。M5 チャートに貼り直してください。");
        }
      }
    }
    this.prevM5Time = m5.time;

    // ---- M5 バッファ更新 (ATR + M15/M30/H1/H4 集計) ----
    // H4 集計に直近 48 本要るのでバッファは 60 本保持する。
    this.m5buf.push(m5);
    if (this.m5buf.length > 60) this.m5buf.shift();

    // ---- ZigZag 更新 ----
    this.zz.m5.update(m5);
    // 上位足はすべて M5 から内部集計する (iXxx の MTF 取得は FTO 環境差で null に
    // なりうるため依存しない。M15/M30 と同じ方式で H1/H4 も確実に作る)。
    // M15 (M5×3): (time+300)%900==0 で 15 分ブロック完了
    if ((m5.time + 300) % 900 === 0 && this.m5buf.length >= 3) {
      const m15 = this._agg(this.m5buf.slice(-3));
      if (m15.time > this.lastM15Time) { this.zz.m15.update(m15); this.lastM15Time = m15.time; }
    }
    // M30 (M5×6)
    if ((m5.time + 300) % 1800 === 0 && this.m5buf.length >= 6) {
      const m30 = this._agg(this.m5buf.slice(-6));
      if (m30.time > this.lastM30Time) { this.zz.m30.update(m30); this.lastM30Time = m30.time; }
    }
    // H1 (M5×12)
    if ((m5.time + 300) % 3600 === 0 && this.m5buf.length >= 12) {
      const h1 = this._agg(this.m5buf.slice(-12));
      if (h1.time > this.lastH1Time) { this.zz.h1.update(h1); this.lastH1Time = h1.time; }
    }
    // H4 (M5×48)
    if ((m5.time + 300) % 14400 === 0 && this.m5buf.length >= 48) {
      const h4 = this._agg(this.m5buf.slice(-48));
      if (h4.time > this.lastH4Time) { this.zz.h4.update(h4); this.lastH4Time = h4.time; }
    }

    // ---- ATR (M5、TR の SMA) ----
    const atrVal = this._atr(P.atr_period);
    if (atrVal === null || atrVal <= 0) return;

    // ---- 各 TF トレンド ----
    const m5t = dowTrend(this.zz.m5.pivots);
    const m15t = dowTrend(this.zz.m15.pivots);
    const m30t = dowTrend(this.zz.m30.pivots);
    const h1t = dowTrend(this.zz.h1.pivots);
    const h4t = dowTrend(this.zz.h4.pivots);

    // M5 トレンド履歴
    this.m5trendHist.push(m5t);
    if (this.m5trendHist.length > P.pullback_lookback_bars + 5) this.m5trendHist.shift();

    // ---- 診断 heartbeat (どこで詰まっているか可視化) ----
    if (this.barIdx % this.diagEvery === 0) {
      this._log({
        type: "diag", bar_idx: this.barIdx, atr: atrVal,
        trends: { m5: m5t, m15: m15t, m30: m30t, h1: h1t, h4: h4t },
        pivots: {
          m5: this.zz.m5.pivots.length, m15: this.zz.m15.pivots.length,
          m30: this.zz.m30.pivots.length, h1: this.zz.h1.pivots.length, h4: this.zz.h4.pivots.length,
        },
        mtf_bars: { h1: this.zz.h1.count, h4: this.zz.h4.count },
        funnel: Object.assign({}, this.diag),
      });
    }

    // ---- ポジション状態 (決済検出 + 既存ポジなら何もしない) ----
    const pos = this._currentPosition();
    if (this.prevPosition && pos !== this.prevPosition) {
      // long/short → 別状態に遷移 = 決済された (TP/SL)。outcome ログ。
      this._logClose(m5);
    }
    this.prevPosition = pos;
    if (pos !== null) return;     // 既にポジションあり → エントリー判定しない
    this.diag.bars += 1;          // flat でエントリー評価したバー

    // ---- エントリー条件 ----
    // 1. 大局アラインメント (alignMode で階層数を選択。既定=H1+M15 のみ)
    //    検証: H4/M30 は勝敗に無関係。H1+M15 のみが P2 リターン最良(全12 +1.08%/月)・頻度6倍。
    let alignTrends;
    const am = this.alignMode.value;
    if (am >= 3) alignTrends = [h4t, h1t, m30t, m15t];
    else if (am === 2) alignTrends = [h4t, h1t, m15t];
    else alignTrends = [h1t, m15t];
    if (alignTrends.some(t => t === null)) return;
    if (!alignTrends.every(t => t === alignTrends[0])) return;
    const majorDir = alignTrends[0];   // "up" / "down"
    this.diag.align += 1;

    // 2. M5 が直近 lookback 以内で逆方向だった (押し戻し)
    const recent = this.m5trendHist.slice(-P.pullback_lookback_bars);
    const opp = opposite(majorDir);
    if (!recent.some(t => t === opp)) return;
    this.diag.pull += 1;

    // 3. M5 が大局方向へ「今まさに転換」
    if (m5t !== majorDir) return;
    if (this.m5trendHist.length >= 2 && this.m5trendHist[this.m5trendHist.length - 2] === majorDir) return;
    this.diag.flip += 1;

    // 4. クールダウン
    if (this.barIdx - this.lastEntryBarIdx < P.cooldown_bars) return;
    this.diag.cd += 1;

    // 4b. 時間帯フィルタ (UTC)
    if (this.blockHourStart.value >= 0) {
      const hourUtc = Math.floor(m5.time / 3600) % 24;
      if (hourUtc >= this.blockHourStart.value && hourUtc < this.blockHourEnd.value) return;
    }

    const price = m5.close;

    // 5. v2: H4/H1 トレンドラインブレイク判定
    if (P.skip_on_trendline_break) {
      const h4d = trendlineDist(this.zz.h4.pivots, this.zz.h4.count - 1, price, atrVal, h4t);
      const h1d = trendlineDist(this.zz.h1.pivots, this.zz.h1.count - 1, price, atrVal, h1t);
      if (majorDir === "up") {
        if ((h4d !== null && h4d < 0) || (h1d !== null && h1d < 0)) return;
      } else {
        if ((h4d !== null && h4d > 0) || (h1d !== null && h1d > 0)) return;
      }
    }
    this.diag.tlpass += 1;

    // ---- SL / TP ----
    let sl, slDist, tp, side, sideLabel;
    if (majorDir === "up") {
      const lows = this.zz.m15.pivots.filter(p => p.kind === "low");
      if (lows.length === 0) return;
      sl = lows[lows.length - 1].price;
      slDist = price - sl;
      if (slDist <= 0) return;
      tp = price + slDist * this.tpRR.value;   // RR = tpRR (既定 1:1.5)
      side = TTradePositionType.BUY; sideLabel = "long";
    } else {
      const highs = this.zz.m15.pivots.filter(p => p.kind === "high");
      if (highs.length === 0) return;
      sl = highs[highs.length - 1].price;
      slDist = sl - price;
      if (slDist <= 0) return;
      tp = price - slDist * this.tpRR.value;
      side = TTradePositionType.SELL; sideLabel = "short";
    }
    // room_R フィルタ: 直近 M15 高安までの余地/SL が大きすぎる(タイトSL)を除外
    if (this.roomRMax.value > 0) {
      if (majorDir === "up") {
        const highsR = this.zz.m15.pivots.filter(p => p.kind === "high");
        if (highsR.length && (highsR[highsR.length - 1].price - price) / slDist >= this.roomRMax.value) return;
      } else {
        const lowsR = this.zz.m15.pivots.filter(p => p.kind === "low");
        if (lowsR.length && (price - lowsR[lowsR.length - 1].price) / slDist >= this.roomRMax.value) return;
      }
    }

    // sl_dist の妥当性 (スプレッド未満 / 遠すぎを排除)
    if (slDist < P.min_sl_dist_atr * atrVal) return;
    if (slDist > P.max_sl_dist_atr * atrVal) return;

    // ---- サイズ (口座通貨建てで正しく逆算) ----
    const sizing = this._riskLot(price, slDist);
    if (!sizing || sizing.lot < 0.01) {
      this.diag.skips += 1;
      this._log({
        type: "skip_size", side: sideLabel, price: price, sl_dist: slDist,
        reason: sizing ? "lot<0.01" : "value_calc_failed",
        lot: sizing ? sizing.lot : null,
      });
      return;
    }
    const lot = sizing.lot;

    // ---- 発注 ----
    let ticket = null;
    try {
      ticket = this.api.PlaceOrder(this.symbol, side, 0, lot, sl, tp,
        "mtfpb_v2", this.magicNumber.value);
    } catch (e) {
      this._log({ type: "order_error", side: sideLabel, err: (e && e.message) ? e.message : String(e) });
      return;
    }
    this.lastEntryBarIdx = this.barIdx;
    this.diag.entries += 1;
    this.pendingEntry = {
      side: sideLabel, entry_price: price, sl: sl, tp: tp, sl_dist: slDist,
      atr: atrVal, lot: lot, entry_bar_time: m5.time, entry_bar_idx: this.barIdx,
    };
    this._log({
      type: "entry", side: sideLabel, ticket: ticket ? String(ticket) : null,
      entry_price: price, sl: sl, tp: tp, sl_dist: slDist, atr: atrVal,
      lot: lot, risk_amount: sizing.riskAmount, value_per_price_per_lot: sizing.valuePerPrice,
      balance: sizing.balance, conv_path: sizing.path, major_dir: majorDir,
      tp_rr: this.tpRR.value, entry_bar_time: m5.time, bar_idx: this.barIdx,
    });
    console.log("[mtfpb] ENTRY " + sideLabel + " " + this.symbol +
      " price=" + price.toFixed(5) + " sl=" + sl.toFixed(5) + " tp=" + tp.toFixed(5) +
      " lot=" + lot.toFixed(2) + " risk$=" + sizing.riskAmount.toFixed(2) + " path=" + sizing.path);
  }

  // --------------------------------------------------------------------------
  // サイズ計算: 口座 risk% を sl_dist(価格) で逆算。FTO に tick value API が無いので
  // 通貨換算を自前で行う。money_per_lot = sl_dist × contractSize × (account/quote)。
  // lot = riskAmount / money_per_lot。算出根拠を全部ログに残して検証可能にする。
  _riskLot(price, slDist) {
    let balance;
    try { balance = this.api.GetAccountBalance() || 0; } catch (e) { balance = 0; }
    if (balance <= 0 || slDist <= 0) return null;
    const riskAmount = balance * (this.riskPct.value / 100.0);

    const sym = this.symbol.toUpperCase();
    const isGold = sym.indexOf("XAU") === 0;
    const contractSize = isGold ? 100 : 100000;
    // base/quote をシンボル名から抽出 (6 文字 FX 前提、XAUUSD は base=XAU/quote=USD)
    let quote;
    if (sym.length >= 6) quote = sym.slice(3, 6);
    else quote = "USD";

    const conv = this._quoteToAccount(sym, quote, price);
    if (conv === null || !(conv.factor > 0)) return null;

    const valuePerPrice = contractSize * conv.factor;   // 1.0 価格 / 1 lot の口座通貨価値
    const moneyPerLot = slDist * valuePerPrice;          // sl_dist あたり 1 lot の損失額
    if (moneyPerLot <= 0) return null;
    let lot = riskAmount / moneyPerLot;
    // 0.01 刻みに丸め、安全キャップを適用
    lot = Math.floor(lot * 100) / 100;
    if (lot > this.maxLot.value) lot = this.maxLot.value;
    return {
      lot: lot, riskAmount: riskAmount, balance: balance,
      valuePerPrice: valuePerPrice, path: conv.path,
    };
  }

  // quote 通貨 1 単位 = 口座通貨 何単位か (factor) を返す。
  _quoteToAccount(sym, quote, chartPrice) {
    const acct = this.accountCcy;
    if (quote === acct) return { factor: 1.0, path: "quote==acct" };

    // 口座通貨が base の直接ペア (例: 口座USD・quote JPY → USDJPY)
    const directSym = acct + quote;     // USDJPY, USDCAD, USDCHF ...
    if (directSym === sym) {
      // チャート自身が USDxxx。現在価格 = quote/acct の逆数換算。
      if (chartPrice > 0) return { factor: 1.0 / chartPrice, path: "self_USDxxx" };
    }
    // チャートが quote+acct (例: 口座USD・quote EUR → EURUSD): 1 quote = price acct
    const invSym = quote + acct;        // EURUSD, GBPUSD ...
    if (invSym === sym) {
      if (chartPrice > 0) return { factor: chartPrice, path: "self_xxxUSD" };
    }
    // クロス参照: directSym (例 USDJPY) の現在値を別シンボルから取得
    const r = this._tryICloseSym(directSym);
    if (r && r > 0) return { factor: 1.0 / r, path: "cross_" + directSym };
    const r2 = this._tryICloseSym(invSym);
    if (r2 && r2 > 0) return { factor: r2, path: "cross_" + invSym };

    // フォールバック: JPY quote なら USDJPY パラメータを使う (それ以外は失敗)
    if (quote === "JPY" && this.usdJpyFallback.value > 0) {
      return { factor: 1.0 / this.usdJpyFallback.value, path: "fallback_usdjpy" };
    }
    return null;
  }

  _tryICloseSym(otherSym) {
    try {
      const v = this.api.iClose(otherSym, 60, 1);   // H1 直近確定の close
      if (typeof v === "number" && isFinite(v) && v > 0) return v;
    } catch (e) { /* クロス参照非対応なら null */ }
    return null;
  }

  // --------------------------------------------------------------------------
  _agg(chunk) {
    let hi = chunk[0].high, lo = chunk[0].low, vol = 0;
    for (const b of chunk) { if (b.high > hi) hi = b.high; if (b.low < lo) lo = b.low; vol += (b.volume || 0); }
    return { time: chunk[0].time, open: chunk[0].open, high: hi, low: lo, close: chunk[chunk.length - 1].close, volume: vol };
  }

  _atr(period) {
    // TR の単純平均 (src/core indicators.atr = SMA of true_range)
    const buf = this.m5buf;
    if (buf.length < period + 1) return null;
    const need = period + 1;
    const seg = buf.slice(-need);   // period+1 本 (最初の 1 本は前足参照用)
    let sum = 0;
    for (let i = 1; i < seg.length; i++) {
      const h = seg[i].high, l = seg[i].low, pc = seg[i - 1].close;
      const tr = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
      sum += tr;
    }
    return sum / period;
  }

  _fetchMtf(tfMinutes) {
    try {
      const t = this.api.iTime(this.symbol, tfMinutes, 1);
      if (t === null || t === undefined) return null;
      return {
        time: Math.floor(t.valueOf() / 1000),
        open: this.api.iOpen(this.symbol, tfMinutes, 1),
        high: this.api.iHigh(this.symbol, tfMinutes, 1),
        low: this.api.iLow(this.symbol, tfMinutes, 1),
        close: this.api.iClose(this.symbol, tfMinutes, 1),
        volume: 0,
      };
    } catch (e) { return null; }
  }

  _safeSymbol() {
    try { return this.api.Symbol() || "UNKNOWN"; } catch (e) { return "UNKNOWN"; }
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

  // --------------------------------------------------------------------------
  _logClose(m5) {
    const e = this.pendingEntry;
    const exitPrice = m5.close;
    let pnlPrice = null;
    if (e) pnlPrice = (e.side === "long") ? (exitPrice - e.entry_price) : (e.entry_price - exitPrice);
    this._log({
      type: "outcome",
      side: e ? e.side : this.prevPosition,
      entry_price: e ? e.entry_price : null,
      exit_price_approx: exitPrice,
      sl: e ? e.sl : null, tp: e ? e.tp : null, sl_dist: e ? e.sl_dist : null,
      pnl_price_approx: pnlPrice,
      lot: e ? e.lot : null,
      entry_bar_time: e ? e.entry_bar_time : null,
      exit_bar_time: m5.time,
      note: "exit detected via position transition; exit_price is bar close approx (FTO report is authoritative)",
    });
    console.log("[mtfpb] EXIT " + (e ? e.side : "?") + " pnl_price~=" +
      (pnlPrice !== null ? pnlPrice.toFixed(5) : "?"));
    this.pendingEntry = null;
  }

  _log(rec) {
    try {
      rec.session_id = this.sessionId;
      rec.symbol = this.symbol || this._safeSymbol();
      if (rec.ts_ms === undefined) { try { rec.ts_ms = Date.now(); } catch (e) {} }
      const port = this.logPort.value;
      if (port && port > 0) {
        const url = "https://" + LOG_HOST + ":" + port + "/log";
        // fire-and-forget。await しない = トレード経路をブロックしない。
        try {
          fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(rec),
            keepalive: true,
          }).catch(() => {});
        } catch (e) { /* fetch 不可環境でも無視 */ }
      }
    } catch (e) { /* ログ失敗はトレードに影響させない */ }
  }
}
