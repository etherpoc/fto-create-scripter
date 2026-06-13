//+------------------------------------------------------------------+
//|  breakout_h1.mq5 — MT5 H1 Donchian ブレイクアウト EA (long-only)   |
//|                                                                  |
//|  研究(B)で見つけた H1 long-only ブレイクアウト・バスケットの実装。   |
//|  確定H1足ベース。tools/breakout_lab.run_bo (NumPy版 bo_fast) と     |
//|  同一ロジックを MQL5 へ移植。                                       |
//|                                                                  |
//|  == ロジック ==                                                   |
//|   エントリー(flat時): 前 entry_n 本の高値を当足高値がブレイク かつ   |
//|     close > SMA(sma_n)  → 成行ロング。SL = fill - sl_atr×ATR。      |
//|     (long-only。short は研究で負けと判明)                          |
//|   エグジット: 前 exit_n 本安値のトレール割れ、または SL ヒット。      |
//|   サイズ: 口座 risk% を SL距離で逆算 (tick value で口座通貨建て)。   |
//|                                                                  |
//|  == 採用運用設定 (MT5実機検証で確定 2026-06-12) ==                 |
//|   ペア: XAUUSD/USDJPY/EURJPY/AUDJPY/GBPJPY の5枚を各H1チャートに。   |
//|     (USDCHF/CADJPY は実機で剥落 → 除外)                          |
//|   既定 SL3/SMA150/long-only/risk0.5%。                            |
//|   実機補正・合算: 月利+2.62% / 合成DD9.4% / 年+36%(2024-26)。       |
//|   ★ レジーム依存(2021-26の金高/円安トレンドに乗る)。反転に注意。   |
//|   ★ サイズは OrderCalcProfit で口座通貨正確換算(非USD口座も安全)。 |
//|                                                                  |
//|  ※ バックテストは Donchian level 約定、本EAは確定足 close 成行約定   |
//|     なので実機はわずかに保守的(正直方向)。実コスト/フィードで要確認。 |
//+------------------------------------------------------------------+
#property copyright "fto-create-scripter"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

//============================ 入力 ============================
input int    InpEntryN     = 30;       // Donchian entry lookback (本)
input int    InpExitN      = 25;       // Donchian トレール lookback (本)
input int    InpAtrN       = 20;       // ATR period
input double InpSlAtr      = 3.0;      // SL = sl_atr × ATR (SL3推奨/2も可)
input int    InpSmaN       = 150;      // トレンドフィルタ SMA (150推奨/100も可, 0=off)
input double InpRiskPct    = 0.5;      // Risk % per trade (採用5ペア合算で月+2.62%/DD9.4%)
input int    InpMagic      = 220612;   // Magic Number
input double InpMaxLot     = 50.0;     // Max Lot (safety cap)
input double InpMaxTotalRiskPct = 3.0; // Fintokei: 同時保有リスク上限% (0=off)。超える新規はスキップ
input bool   InpLongOnly   = true;     // long-only (推奨true。falseでshortも)
input bool   InpCsvLog     = true;     // entry/exit を CSV (MQL5/Files) に保存
//--- エクイティカーブ・デリスク(overlay): 口座エクイティが暦日MAを割ったら新規ロット半減
//    研究E章で検証。BO basket DD 32→20%(全年一貫)・シャッフル対照で本物(連続DD捉え)と確認。
//    口座エクイティ基準=全EA共有=バスケットレベルで機能。スケール不変。1枚運用でも口座全体で効く。
input bool   InpOverlay    = true;     // overlay 有効(口座エクイティ<MAで新規ロット×InpOvMult)
input int    InpOvDays     = 60;       // overlay: 口座エクイティMAの日数(検証値60)
input double InpOvMult     = 0.5;      // overlay: MA割れ時のロット倍率(検証値0.5)

#define TF PERIOD_H1

CTrade   g_trade;
datetime g_lastBar = 0;
int      g_csv = INVALID_HANDLE;
//--- overlay 用: 日次口座エクイティのバッファ(過去 InpOvDays 日)
double   g_eqBuf[];
long     g_lastEqDay = -1;
double   g_ovMA = 0.0;
// 直近エントリー記録(outcomeログ用)
bool     g_eValid=false; int g_eSide=0; double g_eEntry=0,g_eSL=0,g_eLot=0; datetime g_eTime=0;

//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.SetDeviationInPoints(30);
   if(InpCsvLog)
   {
      string fn = "breakout_h1_" + _Symbol + ".csv";
      g_csv = FileOpen(fn, FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
      if(g_csv != INVALID_HANDLE)
         FileWrite(g_csv, "type","time","side","price","sl","lot","risk_amt","balance","atr");
   }
   PrintFormat("[bo_h1] init sym=%s ccy=%s en=%d ex=%d atr=%d SL=%.1f SMA=%d risk%%=%.2f longonly=%s",
               _Symbol, AccountInfoString(ACCOUNT_CURRENCY), InpEntryN, InpExitN, InpAtrN,
               InpSlAtr, InpSmaN, InpRiskPct, (InpLongOnly?"true":"false"));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(g_csv != INVALID_HANDLE){ FileClose(g_csv); g_csv = INVALID_HANDLE; }
}

//+------------------------------------------------------------------+
//| 指標ヘルパー(確定足 shift>=1 ベース。当足=shift1)                  |
//+------------------------------------------------------------------+
// 前 n 本(shift 2..n+1)の最高値 = 当足(shift1)が超えるべき Donchian
double DonchHigh(int n)
{
   double m = -DBL_MAX;
   for(int k = 2; k <= n + 1; k++){ double v = iHigh(_Symbol, TF, k); if(v > m) m = v; }
   return m;
}
double DonchLow(int n)
{
   double m = DBL_MAX;
   for(int k = 2; k <= n + 1; k++){ double v = iLow(_Symbol, TF, k); if(v < m) m = v; }
   return m;
}
// 前 n 本(shift 2..n+1)の SMA(close)
double SmaPrev(int n)
{
   if(n <= 0) return 0.0;
   double s = 0.0;
   for(int k = 2; k <= n + 1; k++) s += iClose(_Symbol, TF, k);
   return s / n;
}
// 前 n 本(shift 2..n+1)の TR 平均 = atr_at(bars,i-1,n) 相当
double AtrPrev(int n)
{
   double s = 0.0;
   for(int k = 2; k <= n + 1; k++)
   {
      double hh = iHigh(_Symbol, TF, k), ll = iLow(_Symbol, TF, k), pc = iClose(_Symbol, TF, k + 1);
      if(pc == 0) return 0.0;
      s += MathMax(hh - ll, MathMax(MathAbs(hh - pc), MathAbs(ll - pc)));
   }
   return s / n;
}

//+------------------------------------------------------------------+
// overlay: 新しい暦日ごとに口座エクイティを1点サンプルし過去 InpOvDays 日を保持
void UpdateEquityBuffer()
{
   long day = (long)(TimeCurrent() / 86400);
   if(day == g_lastEqDay) return;
   g_lastEqDay = day;
   // 検証ロジックは realized(確定)エクイティ基準 → BALANCE(含み損益を含めない)。
   double eq = AccountInfoDouble(ACCOUNT_BALANCE);
   int n = ArraySize(g_eqBuf);
   if(n < InpOvDays)
   {
      ArrayResize(g_eqBuf, n + 1);
      g_eqBuf[n] = eq;
   }
   else
   {
      for(int i = 0; i < n - 1; i++) g_eqBuf[i] = g_eqBuf[i + 1];  // shift left
      g_eqBuf[n - 1] = eq;
   }
}
// overlay 倍率: 口座エクイティが直近 InpOvDays 日MAを割れば InpOvMult、否なら 1.0。
// スケール不変(eq も MA も同率)。ウォームアップ不足時は通常サイズ。
double OverlayMult()
{
   if(!InpOverlay) return 1.0;
   int n = ArraySize(g_eqBuf);
   if(n < MathMax(5, InpOvDays / 3)) return 1.0;
   double s = 0.0; for(int i = 0; i < n; i++) s += g_eqBuf[i];
   g_ovMA = s / n;
   double eq = AccountInfoDouble(ACCOUNT_BALANCE);   // realized 基準(検証ロジックと一致)
   return (eq < g_ovMA) ? InpOvMult : 1.0;
}

//+------------------------------------------------------------------+
// Fintokei: 口座全体の「保有中ポジションのSL基準リスク合計%」(全シンボル・全magic・手動含む)
double AccountOpenRiskPct()
{
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   if(bal <= 0) return 0.0;
   double total = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || !PositionSelectByTicket(tk)) continue;
      double sl = PositionGetDouble(POSITION_SL);
      if(sl <= 0) continue;   // SL未設定はリスク算定不可
      string sym = PositionGetString(POSITION_SYMBOL);
      double vol = PositionGetDouble(POSITION_VOLUME);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      ENUM_ORDER_TYPE ot = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      double profit = 0.0;
      if(OrderCalcProfit(ot, sym, vol, open, sl, profit) && profit < 0)
         total += -profit;   // open→SL の損失額=このポジションのリスク
   }
   return 100.0 * total / bal;
}

bool HasPosition(int &side)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || !PositionSelectByTicket(tk)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      side = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? 1 : -1;
      return true;
   }
   side = 0; return false;
}

// SL距離あたりの 1lot 損失を OrderCalcProfit で正確に計算(通貨換算を全自動)。
// SYMBOL_TRADE_TICK_VALUE はゴールド×非USD口座等で誤値を返すため信用しない。
double RiskLot(int side, double entry, double sl, double ovMult, double &riskAmt, double &bal, double &moneyPerLot)
{
   bal = AccountInfoDouble(ACCOUNT_BALANCE);
   riskAmt = bal * (InpRiskPct / 100.0) * ovMult;   // overlay でリスクを縮小
   moneyPerLot = 0.0;
   if(bal <= 0) return 0.0;
   double slDist = MathAbs(entry - sl);
   if(slDist <= 0) return 0.0;
   ENUM_ORDER_TYPE ot = (side == 1) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double profit = 0.0;
   // entry→sl の損益(口座通貨)。買いなら sl<entry で負、その絶対値が 1lot のリスク。
   if(!OrderCalcProfit(ot, _Symbol, 1.0, entry, sl, profit)) return 0.0;
   moneyPerLot = MathAbs(profit);
   if(moneyPerLot <= 0) return 0.0;
   double lot = riskAmt / moneyPerLot;
   double mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double st = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(st > 0) lot = MathFloor(lot / st) * st;
   if(lot > InpMaxLot) lot = InpMaxLot;
   if(lot > mx) lot = mx;
   if(lot < mn) return 0.0;
   return lot;
}

//+------------------------------------------------------------------+
void OnTick()
{
   datetime t0 = iTime(_Symbol, TF, 0);
   if(t0 <= 0 || t0 == g_lastBar) return;   // 新しい確定足のみ処理
   g_lastBar = t0;
   UpdateEquityBuffer();   // overlay: 日次口座エクイティを更新
   if(Bars(_Symbol, TF) < InpEntryN + InpAtrN + InpSmaN + 5) return;  // ウォームアップ

   double atr = AtrPrev(InpAtrN);
   if(atr <= 0) return;

   int side; bool has = HasPosition(side);

   // 保有中: トレール/SL でクローズ
   if(has)
   {
      double bl = iLow(_Symbol, TF, 1), bh = iHigh(_Symbol, TF, 1);
      if(side == 1)
      {
         double trail = DonchLow(InpExitN);
         // SL は建玉に設定済(ブローカー側)。トレール割れは EA がクローズ
         if(bl <= trail) { ClosePos("trail"); }
      }
      else
      {
         double trail = DonchHigh(InpExitN);
         if(bh >= trail) { ClosePos("trail"); }
      }
      return;
   }

   // flat: エントリー判定(確定足 shift1)
   double bh1 = iHigh(_Symbol, TF, 1), bl1 = iLow(_Symbol, TF, 1), bc1 = iClose(_Symbol, TF, 1);
   double dhi = DonchHigh(InpEntryN), dlo = DonchLow(InpEntryN);
   double sma = SmaPrev(InpSmaN);
   bool longOK  = (bh1 > dhi) && (InpSmaN == 0 || bc1 > sma);
   bool shortOK = (!InpLongOnly) && (bl1 < dlo) && (InpSmaN == 0 || bc1 < sma);

   double ovMult = OverlayMult();
   if(longOK)       OpenPos(1, atr, ovMult);
   else if(shortOK) OpenPos(-1, atr, ovMult);
}

//+------------------------------------------------------------------+
void OpenPos(int side, double atr, double ovMult)
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double price = (side == 1) ? ask : bid;
   double slDist = InpSlAtr * atr;
   double sl = (side == 1) ? price - slDist : price + slDist;
   sl = NormalizeDouble(sl, _Digits);
   double riskAmt, bal, moneyPerLot;
   double lot = RiskLot(side, price, sl, ovMult, riskAmt, bal, moneyPerLot);
   if(lot <= 0){ PrintFormat("[bo_h1] skip size slDist=%.5f", slDist); return; }
   // サイズ検証: この lot の実損(口座通貨)が risk% 近傍か
   double realRisk = lot * moneyPerLot;
   // Fintokei: 同時保有リスク上限チェック
   if(InpMaxTotalRiskPct > 0)
   {
      double curRiskPct = AccountOpenRiskPct();
      double newRiskPct = 100.0 * realRisk / bal;
      if(curRiskPct + newRiskPct > InpMaxTotalRiskPct + 1e-9)
      {
         PrintFormat("[bo_h1] SKIP(Fintokei) %s 保有リスク%.2f%% + 新規%.2f%% > 上限%.1f%%",
                     _Symbol, curRiskPct, newRiskPct, InpMaxTotalRiskPct);
         return;
      }
   }
   bool ok = (side == 1) ? g_trade.Buy(lot, _Symbol, 0.0, sl, 0.0, "bo_h1")
                         : g_trade.Sell(lot, _Symbol, 0.0, sl, 0.0, "bo_h1");
   if(!ok){ PrintFormat("[bo_h1] ORDER FAIL ret=%d %s", g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription()); return; }
   double fill = g_trade.ResultPrice(); if(fill <= 0) fill = price;
   g_eValid=true; g_eSide=side; g_eEntry=fill; g_eSL=sl; g_eLot=lot; g_eTime=iTime(_Symbol,TF,0);
   PrintFormat("[bo_h1] ENTRY %s %s price=%.5f sl=%.5f lot=%.2f risk%s=%.2f 実損≈%.2f (bal=%.2f) atr=%.5f overlay=x%.1f(eq=%.0f MA=%.0f)",
               (side==1?"long":"short"), _Symbol, fill, sl, lot,
               AccountInfoString(ACCOUNT_CURRENCY), riskAmt, realRisk, bal, atr,
               ovMult, AccountInfoDouble(ACCOUNT_BALANCE), g_ovMA);
   if(g_csv != INVALID_HANDLE)
   {
      FileWrite(g_csv, "entry", TimeToString(g_eTime, TIME_DATE|TIME_MINUTES),
                (side==1?"long":"short"), fill, sl, lot, riskAmt, bal, atr);
      FileFlush(g_csv);
   }
}

void ClosePos(string reason)
{
   if(!g_trade.PositionClose(_Symbol))
   {
      PrintFormat("[bo_h1] CLOSE FAIL ret=%d", g_trade.ResultRetcode()); return;
   }
   double px = g_trade.ResultPrice();
   double pnl = (g_eSide == 1) ? (px - g_eEntry) : (g_eEntry - px);
   PrintFormat("[bo_h1] EXIT(%s) %s px=%.5f pnl_price~=%.5f", reason, _Symbol, px, pnl);
   if(g_csv != INVALID_HANDLE)
   {
      FileWrite(g_csv, "exit", TimeToString(iTime(_Symbol,TF,0), TIME_DATE|TIME_MINUTES),
                (g_eSide==1?"long":"short"), px, g_eSL, g_eLot, 0.0, AccountInfoDouble(ACCOUNT_BALANCE), reason);
      FileFlush(g_csv);
   }
   g_eValid = false;
}
//+------------------------------------------------------------------+
