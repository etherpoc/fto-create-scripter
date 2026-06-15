//+------------------------------------------------------------------+
//|  strategies/breakout_h1/mql4.mq4                                  |
//|  H1 Donchian ブレイクアウト（long-only）— PropKit 基盤版 (MT4)     |
//|                                                                  |
//|  判定ロジックは MT5 版 (mql5.mq5) と同一。プラットフォーム差は      |
//|  すべて framework/mql4/PropKit.mqh が吸収する。                    |
//|                                                                  |
//|  ⚠ MT4 はサイズ計算が OrderCalcProfit ではなく MarketInfo tick     |
//|     value 依存。発注ログの 実損≈ を実機デモで必ず目視確認すること。 |
//|     （構造完成・ブローカー未検証。framework/README.md 参照）       |
//|                                                                  |
//|  セットアップ:                                                    |
//|   1. framework/mql4/{PropKit.mqh, Profiles.mqh} を                |
//|      <data>/MQL4/Include/PropKit/ にコピー（tools/deploy.ps1）     |
//|   2. 本ファイルを MQL4/Experts/ に置き F7 でコンパイル              |
//+------------------------------------------------------------------+
#property copyright "fto-create-scripter"
#property version   "2.00"
#property strict

#include <PropKit/PropKit.mqh>

//============================ プロファイル ============================
input ENUM_PROFILE InpProfile      = PROF_FINTOKEI; // ★口座プロファイル
input double InpOvrDailyLossPct    = -1;   // 日次損失%上書き(-1=維持)
input double InpOvrTotalLossPct    = -1;   // 総損失%上書き(-1=維持)
input double InpOvrConcRiskPct     = -1;   // 同時保有リスク%上書き(-1=維持)
input int    InpOvrNewsFilter      = -1;   // ニュース(-1=維持/0=off/1=window)
input int    InpOvrMinHoldSec      = -1;   // 最小保有秒(-1=維持)
input string InpNewsWindowsCSV     = "";   // 手動ニュース窓 "2026.06.18 12:30;..." (UTC)
input bool   InpUseMt5Calendar     = false;// MT4 は非対応（無視）

//============================ 戦略パラメータ ==========================
input int    InpEntryN     = 30;
input int    InpExitN      = 25;
input int    InpAtrN       = 20;
input double InpSlAtr      = 3.0;
input int    InpSmaN       = 150;
input double InpRiskPct    = 0.5;
input bool   InpLongOnly   = true;
input bool   InpShortOnly  = false;
input long   InpMagic      = 220612;
input double InpMaxLot     = 50.0;
input bool   InpCsvLog     = true;
input bool   InpOverlay    = true;
input int    InpOvDays     = 60;
input double InpOvMult     = 0.5;

#define TF PERIOD_H1

//+------------------------------------------------------------------+
int OnInit()
  {
   PkConfig cfg;
   cfg.strategy_name    = "breakout_h1";
   cfg.sym              = Symbol();
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
   if(!PkIsNewBar()) return;
   PkUpdateEquityBuffer();
   if(PkBarCount() < InpEntryN+InpAtrN+InpSmaN+5) return;

   double atr = PkATR(2, InpAtrN);
   if(atr<=0) return;

   int side;
   bool has = PkHasPosition(side);

   if(has)
     {
      double bl=PkLow(1), bh=PkHigh(1);
      if(!PkMinHoldElapsed()) return;
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

   int g = PkGuardCheck();
   if(g!=PK_OK) return;
   if(PkNewsBlocksEntry()) return;

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
   PkOpen(side, sl, "bo_h1");
  }
//+------------------------------------------------------------------+
