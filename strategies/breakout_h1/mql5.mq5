//+------------------------------------------------------------------+
//|  strategies/breakout_h1/mql5.mq5                                  |
//|  H1 Donchian ブレイクアウト（long-only）— PropKit 基盤版           |
//|                                                                  |
//|  元 strategies/standalone/breakout_h1.mq5 を PropKit 基盤へ移植。  |
//|  口座照会・サイジング・overlay・プロップ・ガードはすべて PropKit。 |
//|  このファイルは「判定ロジック」だけを持つ。                       |
//|                                                                  |
//|  ★ プロファイルを実行時 input で選択（Fintokei/FTMO/Normal）。     |
//|     Normal は全ガード無効 = 元 breakout_h1.mq5 と挙動一致（A/B 用）。|
//|                                                                  |
//|  セットアップ:                                                    |
//|   1. framework/mql5/{PropKit.mqh, Profiles.mqh} を                |
//|      <data>/MQL5/Include/PropKit/ にコピー（tools/deploy.ps1 が行う）|
//|   2. 本ファイルを MQL5/Experts/ に置き F7 でコンパイル              |
//|   3. 7ペア(XAUUSD/USDJPY/EURJPY/AUDJPY/GBPJPY/CHFJPY/NZDJPY)の      |
//|      各 H1 チャートに適用                                          |
//+------------------------------------------------------------------+
#property copyright "fto-create-scripter"
#property version   "2.00"
#property strict

#include <PropKit/PropKit.mqh>

//============================ プロファイル ============================
input ENUM_PROFILE InpProfile      = PROF_FINTOKEI; // ★口座プロファイル
//--- プロファイル個別上書き（-1=プロファイル値を維持）
input double InpOvrDailyLossPct    = -1;   // 日次損失%上書き(-1=維持)
input double InpOvrTotalLossPct    = -1;   // 総損失%上書き(-1=維持)
input double InpOvrConcRiskPct     = -1;   // 同時保有リスク%上書き(-1=維持)
input int    InpOvrNewsFilter      = -1;   // ニュース(-1=維持/0=off/1=window)
input int    InpOvrMinHoldSec      = -1;   // 最小保有秒(-1=維持)
input string InpNewsWindowsCSV     = "";   // 手動ニュース窓 "2026.06.18 12:30;..." (UTC)
input bool   InpUseMt5Calendar     = false;// MT5経済指標カレンダー(opt-in/テスター不安定)

//============================ 戦略パラメータ ==========================
input int    InpEntryN     = 30;       // Donchian entry lookback (本)
input int    InpExitN      = 25;       // Donchian トレール lookback (本)
input int    InpAtrN       = 20;       // ATR period
input double InpSlAtr      = 3.0;      // SL = sl_atr × ATR
input int    InpSmaN       = 150;      // トレンドフィルタ SMA (0=off)
input double InpRiskPct    = 0.5;      // Risk % per trade
input bool   InpLongOnly   = true;     // long-only (推奨true)
input bool   InpShortOnly  = false;    // short-only (スリーブ運用用)
input long   InpMagic      = 220612;   // Magic Number
input double InpMaxLot     = 50.0;     // Max Lot (safety cap)
input bool   InpCsvLog     = true;     // entry/exit を CSV 保存
//--- エクイティカーブ・デリスク overlay
input bool   InpOverlay    = true;     // 口座残高<MAで新規ロット×InpOvMult
input int    InpOvDays     = 60;       // overlay: 残高MAの日数
input double InpOvMult     = 0.5;      // overlay: MA割れ時のロット倍率

#define TF PERIOD_H1

//+------------------------------------------------------------------+
int OnInit()
  {
   PkConfig cfg;
   cfg.strategy_name    = "breakout_h1";
   cfg.sym              = _Symbol;
   cfg.magic            = InpMagic;
   cfg.tf               = TF;
   cfg.risk_pct         = InpRiskPct;
   cfg.max_lot          = InpMaxLot;
   cfg.csv_log          = InpCsvLog;
   cfg.overlay          = InpOverlay;
   cfg.ov_days          = InpOvDays;
   cfg.ov_mult          = InpOvMult;
   cfg.news_windows_csv = InpNewsWindowsCSV;
   cfg.use_mt5_calendar = InpUseMt5Calendar;
   cfg.prof             = PkProfile(InpProfile);
   PkApplyOverrides(cfg.prof, InpOvrDailyLossPct, InpOvrTotalLossPct,
                    InpOvrConcRiskPct, InpOvrNewsFilter, InpOvrMinHoldSec);
   PkInit(cfg);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason){ PkDeinit(); }

//+------------------------------------------------------------------+
void OnTick()
  {
   if(!PkIsNewBar()) return;             // 新しい確定足のみ
   PkUpdateEquityBuffer();               // overlay: 日次残高サンプル
   if(PkBarCount() < InpEntryN+InpAtrN+InpSmaN+5) return;  // ウォームアップ

   double atr = PkATR(2, InpAtrN);
   if(atr<=0) return;

   int side;
   bool has = PkHasPosition(side);

   //--- 保有中: トレール/SL でクローズ
   if(has)
     {
      double bl=PkLow(1), bh=PkHigh(1);
      if(!PkMinHoldElapsed()) return;    // 最小保有時間ガード（実質作動しない安全弁）
      if(side==1)
        {
         double trail=PkLowest(2, InpExitN);
         if(bl<=trail) PkCloseOwn("trail");
        }
      else
        {
         double trail=PkHighest(2, InpExitN);
         if(bh>=trail) PkCloseOwn("trail");
        }
      return;
     }

   //--- プロップ・ガード（新規発注の可否）
   int g = PkGuardCheck();
   if(g!=PK_OK) return;
   if(PkNewsBlocksEntry()) return;

   //--- flat: エントリー判定（確定足 shift1）
   double bh1=PkHigh(1), bl1=PkLow(1), bc1=PkClose(1);
   double dhi=PkHighest(2, InpEntryN), dlo=PkLowest(2, InpEntryN);
   double sma=PkSMA(2, InpSmaN);
   bool longOK  = (bh1>dhi) && (InpSmaN==0 || bc1>sma) && !InpShortOnly;
   bool shortOK = (bl1<dlo) && (InpSmaN==0 || bc1<sma) && (InpShortOnly || !InpLongOnly);

   if(longOK)       OpenWithSl(1, atr);
   else if(shortOK) OpenWithSl(-1, atr);
  }

//+------------------------------------------------------------------+
void OpenWithSl(int side, double atr)
  {
   double price=(side==1)?PkAsk():PkBid();
   double slDist=InpSlAtr*atr;
   double sl=(side==1)?price-slDist:price+slDist;
   sl=NormalizeDouble(sl,_Digits);
   PkOpen(side, sl, "bo_h1");   // サイズ・リスク上限・ログは PropKit
  }
//+------------------------------------------------------------------+
