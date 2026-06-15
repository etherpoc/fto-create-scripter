//+------------------------------------------------------------------+
//| framework/mql4/PropKit.mqh                                       |
//|                                                                  |
//|  PropKit の MQL4 版。MQL5 版と「同一の Pk* API・同一の挙動」を保つ。 |
//|  違いは内部実装のみ（注文=OrderSend, 建玉照会=OrderSelect ループ,   |
//|  サイズ=MarketInfo の tick value）。戦略ファイルは MQL5 とほぼ共通。 |
//|                                                                  |
//|  ⚠ MQL4 には OrderCalcProfit が無い → open→SL 損失は MarketInfo    |
//|     MODE_TICKVALUE から算出。profit通貨≠口座通貨(JPY口座の金等)で    |
//|     ブローカーにより誤値の恐れ。発注ログの 実損≈ を実機デモで必ず   |
//|     目視確認すること（CLAUDE.md §6 / 146倍バグの系統）。           |
//|                                                                  |
//|  Profiles.mqh は config/profiles.yaml から自動生成（手で編集しない）。|
//+------------------------------------------------------------------+
#ifndef __PROPKIT_MQH__
#define __PROPKIT_MQH__

#include "Profiles.mqh"

#define PK_OK         0
#define PK_BLOCK_NEW  1
#define PK_HALT       2

struct PkConfig
  {
   string            strategy_name;
   string            sym;
   long              magic;
   ENUM_TIMEFRAMES   tf;
   double            risk_pct;
   double            max_lot;
   bool              csv_log;
   bool              overlay;
   int               ov_days;
   double            ov_mult;
   string            news_windows_csv;
   bool              use_mt5_calendar;   // MQL4 はカレンダー非対応 → 無視
   ProfileCfg        prof;
  };

//============================ 内部状態 ============================
PkConfig  g_pk;
datetime  g_pkLastBar  = 0;
int       g_pkCsv      = INVALID_HANDLE;
double    g_pkEqBuf[];
long      g_pkLastEqDay = -1;
double    g_pkOvMA      = 0.0;
double    g_pkInitBal   = 0.0;
long      g_pkGuardDay  = -1;
double    g_pkDayRef    = 0.0;
bool      g_pkDayBlocked = false;
bool      g_pkHalted    = false;
bool      g_pkEValid=false; int g_pkESide=0; double g_pkEEntry=0,g_pkESL=0,g_pkELot=0; datetime g_pkETime=0;
double    g_pkLastFill=0.0;

//============================ プロファイル解決 ====================
ProfileCfg PkProfile(ENUM_PROFILE id)
  {
   ProfileCfg arr[];
   ProfilesFill(arr);
   return arr[(int)id];
  }
void PkApplyOverrides(ProfileCfg &p,
                      double ov_daily_loss_pct,
                      double ov_total_loss_pct,
                      double ov_concurrent_risk_pct,
                      int    ov_news_filter,
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
   return StringConcatenate("PK_",tag,"_",g_pk.strategy_name,"_",
                            (string)AccountNumber(),"_",(string)g_pk.magic);
  }
void PkInit(PkConfig &cfg)
  {
   g_pk = cfg;
   if(g_pk.sym=="" || g_pk.sym==NULL) g_pk.sym=Symbol();

   bool tester = IsTesting();
   string kBal = PkStateKey("INITBAL");
   string kHalt= PkStateKey("HALT");
   if(!tester && GlobalVariableCheck(kBal))
      g_pkInitBal = GlobalVariableGet(kBal);
   else
     {
      g_pkInitBal = AccountBalance();
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
   PrintFormat("[PropKit] init strat=%s sym=%s ccy=%s magic=%d profile=%s "
               "risk%%=%.2f daily=%.1f/%s total=%.1f conc=%.1f news=%d minhold=%ds initBal=%.2f halted=%s",
               g_pk.strategy_name, g_pk.sym, AccountCurrency(), (int)g_pk.magic,
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
double   PkBalance()         { return AccountBalance(); }
double   PkEquity()          { return AccountEquity(); }
string   PkCcy()             { return AccountCurrency(); }
double   PkAsk()             { return MarketInfo(g_pk.sym, MODE_ASK); }
double   PkBid()             { return MarketInfo(g_pk.sym, MODE_BID); }
double   PkHigh(int shift)   { return iHigh (g_pk.sym, g_pk.tf, shift); }
double   PkLow(int shift)    { return iLow  (g_pk.sym, g_pk.tf, shift); }
double   PkClose(int shift)  { return iClose(g_pk.sym, g_pk.tf, shift); }
datetime PkTime(int shift)   { return iTime (g_pk.sym, g_pk.tf, shift); }
int      PkBarCount()        { return iBars(g_pk.sym, g_pk.tf); }

bool PkIsNewBar()
  {
   datetime t0 = iTime(g_pk.sym, g_pk.tf, 0);
   if(t0 <= 0 || t0 == g_pkLastBar) return false;
   g_pkLastBar = t0;
   return true;
  }

//============================ 汎用指標（確定足）===================
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
double PkSMA(int from_shift, int count)
  {
   if(count<=0) return 0.0;
   double s=0.0;
   for(int k=from_shift; k<from_shift+count; k++) s+=iClose(g_pk.sym,g_pk.tf,k);
   return s/count;
  }
double PkATR(int from_shift, int count)
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
bool PkHasPosition(int &side)
  {
   for(int i=OrdersTotal()-1; i>=0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol()!=g_pk.sym) continue;
      if(OrderMagicNumber()!=(int)g_pk.magic) continue;
      if(OrderType()==OP_BUY){ side=1; return true; }
      if(OrderType()==OP_SELL){ side=-1; return true; }
     }
   side=0; return false;
  }
datetime PkOwnOpenTime()
  {
   for(int i=OrdersTotal()-1; i>=0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol()!=g_pk.sym) continue;
      if(OrderMagicNumber()!=(int)g_pk.magic) continue;
      if(OrderType()==OP_BUY || OrderType()==OP_SELL) return OrderOpenTime();
     }
   return 0;
  }
int PkAccountOpenCount()
  {
   int c=0;
   for(int i=OrdersTotal()-1; i>=0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderType()==OP_BUY || OrderType()==OP_SELL) c++;
     }
   return c;
  }

//============================ サイジング ==========================
// open→SL の口座通貨損失（1 lot あたり）。MQL4 は OrderCalcProfit が無いため
// MarketInfo の tick value から算出。⚠ 通貨換算はブローカー依存（上部注記参照）。
double PkRiskMoney(int side, double entry, double sl)
  {
   double tickSize = MarketInfo(g_pk.sym, MODE_TICKSIZE);
   double tickVal  = MarketInfo(g_pk.sym, MODE_TICKVALUE);
   if(tickSize<=0 || tickVal<=0) return 0.0;
   return MathAbs(entry-sl)/tickSize*tickVal;   // side は対称なので未使用
  }
double PkNormalizeLot(double lot)
  {
   double mn=MarketInfo(g_pk.sym,MODE_MINLOT);
   double mx=MarketInfo(g_pk.sym,MODE_MAXLOT);
   double st=MarketInfo(g_pk.sym,MODE_LOTSTEP);
   if(st>0) lot=MathFloor(lot/st)*st;
   if(lot>g_pk.max_lot) lot=g_pk.max_lot;
   if(lot>mx) lot=mx;
   if(lot<mn) return 0.0;
   return lot;
  }
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
   double eq=PkBalance();
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

//============================ 同時保有リスク =====================
double PkAccountOpenRiskPct()
  {
   double bal=PkBalance();
   if(bal<=0) return 0.0;
   double total=0.0;
   for(int i=OrdersTotal()-1; i>=0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderType()!=OP_BUY && OrderType()!=OP_SELL) continue;
      double sl=OrderStopLoss();
      if(sl<=0) continue;
      string sym=OrderSymbol();
      double tickSize=MarketInfo(sym,MODE_TICKSIZE);
      double tickVal =MarketInfo(sym,MODE_TICKVALUE);
      if(tickSize<=0 || tickVal<=0) continue;
      double loss=MathAbs(OrderOpenPrice()-sl)/tickSize*tickVal*OrderLots();
      total += loss;
     }
   return 100.0*total/bal;
  }
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
// MQL4 は経済指標カレンダー API 非対応 → 手動窓のみ。
bool PkInCalendarNewsWindow(datetime now) { return false; }
bool PkNewsBlocksEntry()
  {
   if(g_pk.prof.news_filter != NEWS_WINDOW) return false;
   return PkInManualNewsWindow(TimeCurrent());
  }

//============================ 最小保有時間 ========================
bool PkMinHoldElapsed()
  {
   if(g_pk.prof.min_hold_seconds<=0) return true;
   datetime ot=PkOwnOpenTime();
   if(ot<=0) return true;
   return ((long)TimeCurrent()-(long)ot) >= g_pk.prof.min_hold_seconds;
  }

//============================ プロップ・ガード ====================
bool PkFindOwnTicket(int &ticket, int &type, double &lots)
  {
   for(int i=OrdersTotal()-1; i>=0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol()!=g_pk.sym) continue;
      if(OrderMagicNumber()!=(int)g_pk.magic) continue;
      if(OrderType()==OP_BUY || OrderType()==OP_SELL)
        { ticket=OrderTicket(); type=OrderType(); lots=OrderLots(); return true; }
     }
   return false;
  }
void PkFlattenOwn()
  {
   int ticket,type; double lots;
   if(!PkFindOwnTicket(ticket,type,lots)) return;
   double px=(type==OP_BUY)?MarketInfo(g_pk.sym,MODE_BID):MarketInfo(g_pk.sym,MODE_ASK);
   if(OrderClose(ticket, lots, px, 30, clrNONE))
      PrintFormat("[PropKit] FLATTEN(own) %s", g_pk.sym);
  }
void PkSetHalted()
  {
   g_pkHalted=true;
   if(!IsTesting()) GlobalVariableSet(PkStateKey("HALT"), 1.0);
  }
int PkGuardCheck()
  {
   if(g_pkHalted) return PK_HALT;
   double eq=PkEquity();

   long day=(long)(TimeCurrent()/86400);
   if(day!=g_pkGuardDay)
     {
      g_pkGuardDay=day;
      g_pkDayRef=(g_pk.prof.daily_loss_basis==BASIS_EQUITY)?eq:PkBalance();
      g_pkDayBlocked=false;
     }
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
   if(g_pk.prof.max_open_positions>0 && PkAccountOpenCount()>=g_pk.prof.max_open_positions)
      return PK_BLOCK_NEW;
   return PK_OK;
  }

//============================ 発注 / 決済 =========================
bool PkOpen(int side, double sl, string comment)
  {
   RefreshRates();
   double price=(side==1)?MarketInfo(g_pk.sym,MODE_ASK):MarketInfo(g_pk.sym,MODE_BID);
   double riskAmt,bal,moneyPerLot;
   double ovMult=PkOverlayMult();
   double lot=PkLotForRisk(side, price, sl, ovMult, riskAmt, bal, moneyPerLot);
   if(lot<=0){ PrintFormat("[PropKit] skip size %s (slDist=%.5f mpl=%.5f)", g_pk.sym, MathAbs(price-sl), moneyPerLot); return false; }

   double realRisk=lot*moneyPerLot;
   if(PkConcurrentRiskExceeded(realRisk)) return false;

   int type=(side==1)?OP_BUY:OP_SELL;
   int ticket=OrderSend(g_pk.sym, type, lot, price, 30, sl, 0.0, comment, (int)g_pk.magic, 0, clrNONE);
   if(ticket<0){ PrintFormat("[PropKit] ORDER FAIL err=%d", GetLastError()); return false; }
   double fill=price;
   if(OrderSelect(ticket, SELECT_BY_TICKET)) fill=OrderOpenPrice();
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
   int ticket,type; double lots;
   if(!PkFindOwnTicket(ticket,type,lots)) return false;
   double px=(type==OP_BUY)?MarketInfo(g_pk.sym,MODE_BID):MarketInfo(g_pk.sym,MODE_ASK);
   if(!OrderClose(ticket, lots, px, 30, clrNONE)){ PrintFormat("[PropKit] CLOSE FAIL err=%d", GetLastError()); return false; }
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
