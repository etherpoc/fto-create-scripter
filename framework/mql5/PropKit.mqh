//+------------------------------------------------------------------+
//| framework/mql5/PropKit.mqh                                       |
//|                                                                  |
//|  マルチプラットフォーム EA 開発基盤の MQL5 共通機構ライブラリ。     |
//|  戦略ファイルは「判定ロジック」だけを書き、口座照会・サイジング・   |
//|  発注・overlay・プロップ・ガードはすべて Pk* 関数に委譲する。      |
//|                                                                  |
//|  プロファイル定数 (Fintokei/FTMO/Normal) は Profiles.mqh から来る。 |
//|  Profiles.mqh は config/profiles.yaml から自動生成される（手で      |
//|  編集しない）。 → python tools/gen_profiles.py                     |
//|                                                                  |
//|  ⚠ MQL4 版・cTrader 版と「同一の挙動」を保つ契約。式を変えるときは   |
//|     3 言語すべてを揃えること。                                     |
//+------------------------------------------------------------------+
#ifndef __PROPKIT_MQH__
#define __PROPKIT_MQH__

#include <Trade/Trade.mqh>
#include "Profiles.mqh"

//--- PkGuard 戻り値
#define PK_OK         0   // 新規発注して良い
#define PK_BLOCK_NEW  1   // 新規発注のみ停止（日次到達など）
#define PK_HALT       2   // 完全停止（総損失到達など）

//+------------------------------------------------------------------+
//| 戦略が設定する PropKit のコンフィグ                               |
//+------------------------------------------------------------------+
struct PkConfig
  {
   string            strategy_name;     // ログ/永続化キー用
   string            sym;               // 対象シンボル（通常 _Symbol）
   long              magic;             // Magic Number（自分の建玉識別）
   ENUM_TIMEFRAMES   tf;                // 戦略の確定足タイムフレーム
   double            risk_pct;          // 1 トレードのリスク%
   double            max_lot;           // 安全上限ロット
   bool              csv_log;           // CSV ログ出力
   //--- overlay（口座残高<MAで新規ロット縮小）
   bool              overlay;
   int               ov_days;
   double            ov_mult;
   //--- ニュース手動ブラックリスト（"2026.06.18 12:30;..." UTC, ; 区切り）
   string            news_windows_csv;
   bool              use_mt5_calendar;  // MT5 経済指標カレンダー（opt-in, テスター不安定）
   //--- 解決済みプロファイル（ProfilesFill→選択→上書き 済み）
   ProfileCfg        prof;
  };

//============================ 内部状態 ============================
PkConfig  g_pk;
CTrade    g_pkTrade;
datetime  g_pkLastBar  = 0;
int       g_pkCsv      = INVALID_HANDLE;
//--- overlay 用日次残高バッファ
double    g_pkEqBuf[];
long      g_pkLastEqDay = -1;
double    g_pkOvMA      = 0.0;
//--- ガード用
double    g_pkInitBal   = 0.0;   // 総損失基準（初期残高）
long      g_pkGuardDay  = -1;    // 日次基準を取った暦日
double    g_pkDayRef    = 0.0;   // 日次基準値（basis に応じ equity/balance）
bool      g_pkDayBlocked = false;// 日次到達でその日新規停止
bool      g_pkHalted    = false; // 総損失到達で完全停止（永続）
//--- 直近エントリー記録（exit ログ用）
bool      g_pkEValid=false; int g_pkESide=0; double g_pkEEntry=0,g_pkESL=0,g_pkELot=0; datetime g_pkETime=0;

//============================ プロファイル解決 ====================
// ProfilesFill→選択。戦略はこれを呼び、必要なら PkApplyOverrides で上書き。
ProfileCfg PkProfile(ENUM_PROFILE id)
  {
   ProfileCfg arr[];
   ProfilesFill(arr);
   return arr[(int)id];
  }
// 個別 input による上書き。各引数 <0（センチネル -1）なら「プロファイル値を維持」。
void PkApplyOverrides(ProfileCfg &p,
                      double ov_daily_loss_pct,
                      double ov_total_loss_pct,
                      double ov_concurrent_risk_pct,
                      int    ov_news_filter,      // -1=維持, 0=off, 1=window
                      int    ov_min_hold_sec)
  {
   if(ov_daily_loss_pct      >= 0) p.daily_loss_pct          = ov_daily_loss_pct;
   if(ov_total_loss_pct      >= 0) p.total_loss_pct          = ov_total_loss_pct;
   if(ov_concurrent_risk_pct >= 0) p.max_concurrent_risk_pct = ov_concurrent_risk_pct;
   if(ov_news_filter         >= 0) p.news_filter             = (ENUM_NEWS_MODE)ov_news_filter;
   if(ov_min_hold_sec        >= 0) p.min_hold_seconds        = ov_min_hold_sec;
  }

//============================ 初期化 / 後始末 =====================
string PkStateKey(string tag)
  {
   return StringFormat("PK_%s_%s_%I64d_%I64d", tag, g_pk.strategy_name,
                       AccountInfoInteger(ACCOUNT_LOGIN), g_pk.magic);
  }

void PkInit(PkConfig &cfg)
  {
   g_pk = cfg;
   if(g_pk.sym=="" || g_pk.sym==NULL) g_pk.sym=_Symbol;
   g_pkTrade.SetExpertMagicNumber((int)g_pk.magic);
   g_pkTrade.SetTypeFillingBySymbol(g_pk.sym);
   g_pkTrade.SetDeviationInPoints(30);

   // 総損失基準（初期残高）。tester は毎回フレッシュ、live は永続化。
   bool tester = (bool)MQLInfoInteger(MQL_TESTER);
   string kBal = PkStateKey("INITBAL");
   string kHalt= PkStateKey("HALT");
   if(!tester && GlobalVariableCheck(kBal))
      g_pkInitBal = GlobalVariableGet(kBal);
   else
     {
      g_pkInitBal = AccountInfoDouble(ACCOUNT_BALANCE);
      if(!tester) GlobalVariableSet(kBal, g_pkInitBal);
     }
   g_pkHalted = (!tester && GlobalVariableCheck(kHalt) && GlobalVariableGet(kHalt) > 0.5);

   if(g_pk.csv_log)
     {
      string fn = g_pk.strategy_name + "_" + g_pk.sym + ".csv";
      g_pkCsv = FileOpen(fn, FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
      if(g_pkCsv != INVALID_HANDLE)
         FileWrite(g_pkCsv,"type","time","side","price","sl","lot","risk_amt","balance","atr");
     }
   PrintFormat("[PropKit] init strat=%s sym=%s ccy=%s magic=%I64d profile=%s "
               "risk%%=%.2f daily=%.1f/%s total=%.1f conc=%.1f news=%d minhold=%ds initBal=%.2f halted=%s",
               g_pk.strategy_name, g_pk.sym, AccountInfoString(ACCOUNT_CURRENCY), g_pk.magic,
               g_pk.prof.display_name, g_pk.risk_pct,
               g_pk.prof.daily_loss_pct, (g_pk.prof.daily_loss_basis==BASIS_EQUITY?"eq":"bal"),
               g_pk.prof.total_loss_pct, g_pk.prof.max_concurrent_risk_pct,
               (int)g_pk.prof.news_filter, g_pk.prof.min_hold_seconds,
               g_pkInitBal, (g_pkHalted?"YES":"no"));
  }

void PkDeinit()
  {
   if(g_pkCsv != INVALID_HANDLE){ FileClose(g_pkCsv); g_pkCsv = INVALID_HANDLE; }
  }

//============================ 口座 / バー =========================
double   PkBalance()         { return AccountInfoDouble(ACCOUNT_BALANCE); }
double   PkEquity()          { return AccountInfoDouble(ACCOUNT_EQUITY); }
string   PkCcy()             { return AccountInfoString(ACCOUNT_CURRENCY); }
double   PkAsk()             { return SymbolInfoDouble(g_pk.sym, SYMBOL_ASK); }
double   PkBid()             { return SymbolInfoDouble(g_pk.sym, SYMBOL_BID); }
double   PkHigh(int shift)   { return iHigh (g_pk.sym, g_pk.tf, shift); }
double   PkLow(int shift)    { return iLow  (g_pk.sym, g_pk.tf, shift); }
double   PkClose(int shift)  { return iClose(g_pk.sym, g_pk.tf, shift); }
datetime PkTime(int shift)   { return iTime (g_pk.sym, g_pk.tf, shift); }
int      PkBarCount()        { return Bars(g_pk.sym, g_pk.tf); }

// 新しい確定足が出たら true（同一足の重複処理を防ぐ）。1 tick に 1 回呼ぶ。
bool PkIsNewBar()
  {
   datetime t0 = iTime(g_pk.sym, g_pk.tf, 0);
   if(t0 <= 0 || t0 == g_pkLastBar) return false;
   g_pkLastBar = t0;
   return true;
  }

//============================ 汎用指標（確定足）===================
// いずれも shift [from, from+count-1] の確定足を対象（from>=1）。
// breakout_h1 は from=2,count=n（直近確定足 shift1 を除外）で元 EA と一致。
double PkHighest(int from_shift, int count)
  {
   double m=-DBL_MAX;
   for(int k=from_shift; k<from_shift+count; k++){ double v=iHigh(g_pk.sym,g_pk.tf,k); if(v>m) m=v; }
   return m;
  }
double PkLowest(int from_shift, int count)
  {
   double m=DBL_MAX;
   for(int k=from_shift; k<from_shift+count; k++){ double v=iLow(g_pk.sym,g_pk.tf,k); if(v<m) m=v; }
   return m;
  }
double PkSMA(int from_shift, int count)   // close の単純平均
  {
   if(count<=0) return 0.0;
   double s=0.0;
   for(int k=from_shift; k<from_shift+count; k++) s+=iClose(g_pk.sym,g_pk.tf,k);
   return s/count;
  }
double PkATR(int from_shift, int count)    // TR(k) は iClose(k+1) を前足とする
  {
   if(count<=0) return 0.0;
   double s=0.0;
   for(int k=from_shift; k<from_shift+count; k++)
     {
      double hh=iHigh(g_pk.sym,g_pk.tf,k), ll=iLow(g_pk.sym,g_pk.tf,k), pc=iClose(g_pk.sym,g_pk.tf,k+1);
      if(pc==0) return 0.0;
      s += MathMax(hh-ll, MathMax(MathAbs(hh-pc), MathAbs(ll-pc)));
     }
   return s/count;
  }

//============================ ポジション ==========================
// 自分（magic+symbol）の建玉があるか。side: 1=long,-1=short
bool PkHasPosition(int &side)
  {
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      ulong tk=PositionGetTicket(i);
      if(tk==0 || !PositionSelectByTicket(tk)) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_pk.sym) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != g_pk.magic) continue;
      side = (PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY)?1:-1;
      return true;
     }
   side=0; return false;
  }
// 自分の建玉の建値時刻（無ければ 0）
datetime PkOwnOpenTime()
  {
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      ulong tk=PositionGetTicket(i);
      if(tk==0 || !PositionSelectByTicket(tk)) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_pk.sym) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != g_pk.magic) continue;
      return (datetime)PositionGetInteger(POSITION_TIME);
     }
   return 0;
  }
// 口座全体の建玉数（FTMO 上限チェック用）
int PkAccountOpenCount() { return PositionsTotal(); }

//============================ サイジング ==========================
// open→SL の口座通貨損失（1 lot あたり）。OrderCalcProfit で通貨換算を全自動。
// SYMBOL_TRADE_TICK_VALUE はゴールド×非USD口座で誤値を返すため使わない。
double PkRiskMoney(int side, double entry, double sl)
  {
   ENUM_ORDER_TYPE ot = (side==1)?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
   double profit=0.0;
   if(!OrderCalcProfit(ot, g_pk.sym, 1.0, entry, sl, profit)) return 0.0;
   return MathAbs(profit);
  }
double PkNormalizeLot(double lot)
  {
   double mn=SymbolInfoDouble(g_pk.sym,SYMBOL_VOLUME_MIN);
   double mx=SymbolInfoDouble(g_pk.sym,SYMBOL_VOLUME_MAX);
   double st=SymbolInfoDouble(g_pk.sym,SYMBOL_VOLUME_STEP);
   if(st>0) lot=MathFloor(lot/st)*st;
   if(lot>g_pk.max_lot) lot=g_pk.max_lot;
   if(lot>mx) lot=mx;
   if(lot<mn) return 0.0;
   return lot;
  }
// risk% を SL距離で逆算しロット算出。riskMoney/moneyPerLot を out で返す（ログ検証用）。
double PkLotForRisk(int side, double entry, double sl, double ovMult,
                    double &riskAmt, double &bal, double &moneyPerLot)
  {
   bal = PkBalance();
   riskAmt = bal * (g_pk.risk_pct/100.0) * ovMult;
   moneyPerLot=0.0;
   if(bal<=0) return 0.0;
   if(MathAbs(entry-sl)<=0) return 0.0;
   moneyPerLot = PkRiskMoney(side, entry, sl);
   if(moneyPerLot<=0) return 0.0;
   return PkNormalizeLot(riskAmt/moneyPerLot);
  }

//============================ overlay =============================
void PkUpdateEquityBuffer()
  {
   long day=(long)(TimeCurrent()/86400);
   if(day==g_pkLastEqDay) return;
   g_pkLastEqDay=day;
   double eq=PkBalance();   // realized 基準（含み損は含めない＝検証ロジックと一致）
   int n=ArraySize(g_pkEqBuf);
   if(n<g_pk.ov_days){ ArrayResize(g_pkEqBuf,n+1); g_pkEqBuf[n]=eq; }
   else { for(int i=0;i<n-1;i++) g_pkEqBuf[i]=g_pkEqBuf[i+1]; g_pkEqBuf[n-1]=eq; }
  }
double PkOverlayMult()
  {
   if(!g_pk.overlay) return 1.0;
   int n=ArraySize(g_pkEqBuf);
   if(n<MathMax(5, g_pk.ov_days/3)) return 1.0;
   double s=0.0; for(int i=0;i<n;i++) s+=g_pkEqBuf[i];
   g_pkOvMA=s/n;
   double eq=PkBalance();
   return (eq<g_pkOvMA)?g_pk.ov_mult:1.0;
  }
double PkOverlayMA() { return g_pkOvMA; }

//============================ 同時保有リスク (Fintokei) ===========
// 口座全体（全シンボル・全 magic・手動含む）の open→SL リスク合計 %。
double PkAccountOpenRiskPct()
  {
   double bal=PkBalance();
   if(bal<=0) return 0.0;
   double total=0.0;
   for(int i=PositionsTotal()-1; i>=0; i--)
     {
      ulong tk=PositionGetTicket(i);
      if(tk==0 || !PositionSelectByTicket(tk)) continue;
      double sl=PositionGetDouble(POSITION_SL);
      if(sl<=0) continue;   // SL 未設定はリスク算定不可
      string sym=PositionGetString(POSITION_SYMBOL);
      double vol=PositionGetDouble(POSITION_VOLUME);
      double open=PositionGetDouble(POSITION_PRICE_OPEN);
      ENUM_ORDER_TYPE ot=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY)?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
      double profit=0.0;
      if(OrderCalcProfit(ot, sym, vol, open, sl, profit) && profit<0)
         total += -profit;
     }
   return 100.0*total/bal;
  }
// 新規 realRisk(口座通貨) を足したら同時保有上限を超えるか
bool PkConcurrentRiskExceeded(double newRealRisk)
  {
   if(g_pk.prof.max_concurrent_risk_pct<=0) return false;
   double bal=PkBalance(); if(bal<=0) return false;
   double cur=PkAccountOpenRiskPct();
   double add=100.0*newRealRisk/bal;
   if(cur+add > g_pk.prof.max_concurrent_risk_pct + 1e-9)
     {
      PrintFormat("[PropKit] SKIP(conc-risk) %s 保有%.2f%%+新規%.2f%% > 上限%.1f%%",
                  g_pk.sym, cur, add, g_pk.prof.max_concurrent_risk_pct);
      return true;
     }
   return false;
  }

//============================ ニュース・フィルタ ==================
// 手動 CSV 窓（"2026.06.18 12:30;...") のいずれかと ±news_window_min 以内なら true。
bool PkInManualNewsWindow(datetime now)
  {
   if(g_pk.news_windows_csv=="") return false;
   int w = g_pk.prof.news_window_min*60;
   string parts[];
   int n=StringSplit(g_pk.news_windows_csv, ';', parts);
   for(int i=0;i<n;i++)
     {
      string s=parts[i];
      StringTrimLeft(s); StringTrimRight(s);
      if(s=="") continue;
      datetime t=StringToTime(s);
      if(t>0 && MathAbs((long)now-(long)t) <= w) return true;
     }
   return false;
  }
// MT5 経済指標カレンダー（opt-in）。シンボルの基軸/決済通貨に該当する高重要度
// イベントの ±window 内なら true。テスターでは値が来ないことが多い。
bool PkInCalendarNewsWindow(datetime now)
  {
   if(!g_pk.use_mt5_calendar) return false;
   int w=g_pk.prof.news_window_min*60;
   string cur[2];
   cur[0]=StringSubstr(g_pk.sym,0,3);
   cur[1]=StringSubstr(g_pk.sym,3,3);
   for(int c=0;c<2;c++)
     {
      MqlCalendarValue values[];
      datetime from=now-w-3600, to=now+w+3600;
      if(CalendarValueHistory(values, from, to, NULL, cur[c])<=0) continue;
      for(int i=0;i<ArraySize(values);i++)
        {
         MqlCalendarEvent ev;
         if(!CalendarEventById(values[i].event_id, ev)) continue;
         if(ev.importance < CALENDAR_IMPORTANCE_HIGH) continue;
         if(MathAbs((long)now-(long)values[i].time) <= w) return true;
        }
     }
   return false;
  }
// news_filter==window のときのみ作動。窓内なら true（=新規スキップ）。
bool PkNewsBlocksEntry()
  {
   if(g_pk.prof.news_filter != NEWS_WINDOW) return false;
   datetime now=TimeCurrent();
   return PkInManualNewsWindow(now) || PkInCalendarNewsWindow(now);
  }

//============================ 最小保有時間 ========================
// 自分の建玉が min_hold_seconds 以上保有されているか（EA 主導の決済前ゲート）。
// 建玉が無い／min_hold=0 なら true。
bool PkMinHoldElapsed()
  {
   if(g_pk.prof.min_hold_seconds<=0) return true;
   datetime ot=PkOwnOpenTime();
   if(ot<=0) return true;
   return ((long)TimeCurrent()-(long)ot) >= g_pk.prof.min_hold_seconds;
  }

//============================ プロップ・ガード ====================
void PkFlattenOwn()   // 自分の建玉を成行決済
  {
   if(g_pkTrade.PositionClose(g_pk.sym))
      PrintFormat("[PropKit] FLATTEN(own) %s", g_pk.sym);
  }
void PkSetHalted()
  {
   g_pkHalted=true;
   if(!(bool)MQLInfoInteger(MQL_TESTER)) GlobalVariableSet(PkStateKey("HALT"), 1.0);
  }

// 新規発注の可否を判定。1 tick / 1 bar に呼ぶ。breach 時は設定に従い決済も行う。
int PkGuardCheck()
  {
   if(g_pkHalted) return PK_HALT;

   double eq=PkEquity();

   // 日次基準の更新（暦日が変わったら basis に応じてリファレンス取得）
   long day=(long)(TimeCurrent()/86400);
   if(day!=g_pkGuardDay)
     {
      g_pkGuardDay=day;
      g_pkDayRef=(g_pk.prof.daily_loss_basis==BASIS_EQUITY)?eq:PkBalance();
      g_pkDayBlocked=false;
     }

   // 総損失（initial_balance 基準・現在 equity と比較）
   if(g_pk.prof.total_loss_pct>0.0 && g_pkInitBal>0.0)
     {
      double floor=g_pkInitBal*(1.0-g_pk.prof.total_loss_pct/100.0);
      if(eq<floor)
        {
         PrintFormat("[PropKit] TOTAL-LOSS breach eq=%.2f < floor=%.2f (init=%.2f -%.1f%%)",
                     eq, floor, g_pkInitBal, g_pk.prof.total_loss_pct);
         if(g_pk.prof.on_total_breach==BREACH_FLATTEN_HALT){ PkFlattenOwn(); PkSetHalted(); }
         return PK_HALT;
        }
     }

   // 日次損失（day ref から現在 equity の下落%）
   if(g_pk.prof.daily_loss_pct>0.0 && g_pkDayRef>0.0)
     {
      double dd=100.0*(g_pkDayRef-eq)/g_pkDayRef;
      if(dd>=g_pk.prof.daily_loss_pct)
        {
         if(!g_pkDayBlocked)
            PrintFormat("[PropKit] DAILY-LOSS breach dd=%.2f%% >= %.1f%% (ref=%.2f eq=%.2f)",
                        dd, g_pk.prof.daily_loss_pct, g_pkDayRef, eq);
         g_pkDayBlocked=true;
         if(g_pk.prof.on_daily_breach==BREACH_FLATTEN_HALT) PkFlattenOwn();
         return PK_BLOCK_NEW;
        }
     }

   // FTMO 同時建玉数上限
   if(g_pk.prof.max_open_positions>0 && PkAccountOpenCount()>=g_pk.prof.max_open_positions)
      return PK_BLOCK_NEW;

   return PK_OK;
  }

//============================ 発注 / 決済 =========================
double g_pkLastFill=0.0;
// side:1=buy,-1=sell。SL は価格。成功なら true、g_pkLastFill に約定価格。
bool PkOpen(int side, double sl, string comment)
  {
   double ask=SymbolInfoDouble(g_pk.sym,SYMBOL_ASK);
   double bid=SymbolInfoDouble(g_pk.sym,SYMBOL_BID);
   double price=(side==1)?ask:bid;
   double riskAmt,bal,moneyPerLot;
   double ovMult=PkOverlayMult();
   double lot=PkLotForRisk(side, price, sl, ovMult, riskAmt, bal, moneyPerLot);
   if(lot<=0){ PrintFormat("[PropKit] skip size %s (slDist=%.5f mpl=%.5f)", g_pk.sym, MathAbs(price-sl), moneyPerLot); return false; }

   double realRisk=lot*moneyPerLot;
   if(PkConcurrentRiskExceeded(realRisk)) return false;

   bool ok=(side==1)? g_pkTrade.Buy(lot, g_pk.sym, 0.0, sl, 0.0, comment)
                    : g_pkTrade.Sell(lot, g_pk.sym, 0.0, sl, 0.0, comment);
   if(!ok){ PrintFormat("[PropKit] ORDER FAIL ret=%d %s", g_pkTrade.ResultRetcode(), g_pkTrade.ResultRetcodeDescription()); return false; }
   double fill=g_pkTrade.ResultPrice(); if(fill<=0) fill=price;
   g_pkLastFill=fill;
   g_pkEValid=true; g_pkESide=side; g_pkEEntry=fill; g_pkESL=sl; g_pkELot=lot; g_pkETime=PkTime(0);
   PrintFormat("[PropKit] ENTRY %s %s price=%.5f sl=%.5f lot=%.2f risk%s=%.2f 実損≈%.2f (bal=%.2f) overlay=x%.1f(eq=%.0f MA=%.0f)",
               (side==1?"long":"short"), g_pk.sym, fill, sl, lot, PkCcy(),
               riskAmt, realRisk, bal, ovMult, PkBalance(), g_pkOvMA);
   if(g_pkCsv!=INVALID_HANDLE)
     {
      FileWrite(g_pkCsv,"entry",TimeToString(g_pkETime,TIME_DATE|TIME_MINUTES),
                (side==1?"long":"short"), fill, sl, lot, riskAmt, bal, 0.0);
      FileFlush(g_pkCsv);
     }
   return true;
  }
bool PkCloseOwn(string reason)
  {
   if(!g_pkTrade.PositionClose(g_pk.sym)){ PrintFormat("[PropKit] CLOSE FAIL ret=%d", g_pkTrade.ResultRetcode()); return false; }
   double px=g_pkTrade.ResultPrice();
   PrintFormat("[PropKit] EXIT(%s) %s px=%.5f", reason, g_pk.sym, px);
   if(g_pkCsv!=INVALID_HANDLE)
     {
      FileWrite(g_pkCsv,"exit",TimeToString(PkTime(0),TIME_DATE|TIME_MINUTES),
                (g_pkESide==1?"long":"short"), px, g_pkESL, g_pkELot, 0.0, PkBalance(), reason);
      FileFlush(g_pkCsv);
     }
   g_pkEValid=false;
   return true;
  }

#endif // __PROPKIT_MQH__
