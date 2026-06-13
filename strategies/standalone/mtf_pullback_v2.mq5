//+------------------------------------------------------------------+
//|  mtf_pullback_v2.mq5 — MT5 スタンドアロン EA                       |
//|                                                                  |
//|  strategies/mtf_pullback/strategy.py の v2 (skip_on_trendline_   |
//|  break=True) + 2026-06-12 改善 (H1+M15 / room_R / 時間帯) を      |
//|  MQL5 へ忠実移植。検証済み FTO 版 mtf_pullback_v2.js と同一ロジック。 |
//|                                                                  |
//|  == ロジック ==                                                   |
//|   エントリー: 大局アラインメント(既定 H1=M15。H4/M30 は無関係と判明)  |
//|     + M5 が直近で逆方向(押し戻し) + M5 が大局方向へ転換した瞬間      |
//|     + クールダウン + 時間帯フィルタ + [v2]トレンドライン未ブレイク    |
//|     + room_R フィルタ(直近M15高安までの余地/SL < 2.0)              |
//|   SL: ロング→直近 M15 安値ピボット / ショート→直近 M15 高値ピボット   |
//|   TP: entry ± sl_dist × RR (既定 1.5)                            |
//|   サイズ: 口座 risk% を sl_dist で逆算 (tick value で口座通貨建て)   |
//|   決済: SL/TP をブローカーに渡してネイティブ決済 (trailing なし)     |
//|                                                                  |
//|  == MT5 版の特徴 ==                                               |
//|   - 上位足は iHigh/iLow/... のネイティブ TF を使用 (M5 集計と等価)。  |
//|   - サイズは SYMBOL_TRADE_TICK_VALUE で口座通貨に正確換算 (手動換算   |
//|     不要)。本番前に Experts ログの risk$ が残高の約 risk% か要確認。  |
//|   - M5 を明示取得するのでチャート足に非依存 (M5 推奨だが必須でない)。  |
//|   - ★ ブローカーのサーバー時刻が UTC でない場合、Server->UTC offset  |
//|     を設定すること (block_hour は UTC 基準)。GMT+3 なら -3。         |
//|                                                                  |
//|  参考: https://www.mql5.com/en/docs                              |
//+------------------------------------------------------------------+
#property copyright "fto-create-scripter"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

//============================ 入力パラメータ ============================
input double InpRiskPct         = 1.0;     // Risk % per trade (1=1%)
input int    InpMagic           = 220611;  // Magic Number
input double InpMaxLot          = 50.0;    // Max Lot (safety cap)
input double InpMaxTotalRiskPct = 3.0;     // Fintokei: 同時保有リスク上限% (0=off)。超える新規はスキップ
input double InpTpRR            = 1.5;      // TP RR (1.5=1:1.5)
input int    InpAlignMode       = 1;       // Align (1=H1+M15, 2=+H4, 3=+H4+M30)
input double InpRoomRMax        = 2.0;      // room_R max (0=off)
input int    InpBlockHourStart  = 6;       // Block hour start UTC (-1=off)
input int    InpBlockHourEnd    = 10;      // Block hour end UTC
input bool   InpAutoUtcOffset   = true;    // ★UTCオフセット自動(EET=GMT+2/+3前提・DST自動)。falseで下記手動値を使用
input int    InpServerUtcOffset = -3;      // 手動Server->UTC offset (AutoがOFFの時のみ。GMT+3=>-3)
input double InpMinSlPips        = 20.0;    // 絶対最小SL pips (タイトSL=コスト負け除外, 0=off, 確定値20)
input bool   InpCsvLog          = true;    // entry/outcome を CSV (MQL5/Files) に保存
input int    InpDiagEvery       = 0;       // N本ごとに診断 Print (0=off)
//--- エクイティカーブ・デリスク(overlay): 口座残高(realized)が暦日MAを割ったら新規ロット×InpOvMult
//    研究E章で検証(breakoutと同一の口座レベル汎用ロジック)。DD削減=ペア構成に依らず効く改善層。
input bool   InpOverlay          = true;    // overlay 有効(口座残高<MAで新規ロット半減)
input int    InpOvDays           = 60;      // overlay: 口座残高MAの日数(検証値60)
input double InpOvMult           = 0.5;     // overlay: MA割れ時のロット倍率(検証値0.5)

//============================ 戦略定数 (Python Params 既定と一致) ======
#define ZZ_DEPTH_M5   5
#define ZZ_DEPTH_M15  8
#define ZZ_DEPTH_M30  10
#define ZZ_DEPTH_H1   12
#define ZZ_DEPTH_H4   12
#define ZZ_DEV_PIPS   3.0
#define ATR_PERIOD    14
#define PULLBACK_LB   30
#define MIN_SL_ATR    0.3
#define MAX_SL_ATR    5.0
#define COOLDOWN_BARS 6
#define SKIP_ON_TL    true
// 検証済み backtest は全ペア pip=0.0001 で ZigZag deviation を計算していた。
// 同じシグナルを再現するため deviation 用 pip は 0.0001 固定 (サイズ計算とは別物)。
#define DEV_PIP       0.0001

#define KIND_HIGH   1
#define KIND_LOW   -1
#define TR_NONE     0
#define TR_UP       1
#define TR_DOWN    -1

//+------------------------------------------------------------------+
//| CZigZag — indicators.py の ZigZagTracker 移植 (メモリ軽量版)        |
//|  pivot.index / count は投入バー列上の絶対位置 (Python/JS と一致)。   |
//+------------------------------------------------------------------+
class CZigZag
{
private:
   int      m_depth;
   double   m_dev;
   int      m_count;            // 投入総バー数 (= Python len(self.bars))
   // 直近 (2*depth+1) 本のウィンドウ
   double   m_wH[], m_wL[];
   datetime m_wT[];
   // 確定ピボット (末尾が最新)
   double   m_pPrice[];
   int      m_pKind[];
   int      m_pIdx[];
   datetime m_pTime[];
   int      m_maxPiv;

   void PushPivot(int idx, datetime t, int kind, double price)
   {
      int s = ArraySize(m_pPrice);
      ArrayResize(m_pPrice, s+1); ArrayResize(m_pKind, s+1);
      ArrayResize(m_pIdx,   s+1); ArrayResize(m_pTime, s+1);
      m_pPrice[s]=price; m_pKind[s]=kind; m_pIdx[s]=idx; m_pTime[s]=t;
      if(ArraySize(m_pPrice) > m_maxPiv)
      {
         ArrayRemove(m_pPrice,0,1); ArrayRemove(m_pKind,0,1);
         ArrayRemove(m_pIdx,0,1);   ArrayRemove(m_pTime,0,1);
      }
   }

public:
   CZigZag(int depth, double dev)
   {
      m_depth=depth; m_dev=dev; m_count=0; m_maxPiv=64;
   }

   int  Count()              const { return m_count; }
   int  PivN()               const { return ArraySize(m_pPrice); }
   double PivPrice(int i)    const { return m_pPrice[i]; }
   int  PivKind(int i)       const { return m_pKind[i]; }
   int  PivIdx(int i)        const { return m_pIdx[i]; }

   // 末尾から kind 一致の最終ピボット価格
   bool LastPriceOfKind(int kind, double &out) const
   {
      for(int i=ArraySize(m_pPrice)-1; i>=0; i--)
         if(m_pKind[i]==kind){ out=m_pPrice[i]; return true; }
      return false;
   }

   void Update(double bh, double bl, double bc, double bo, datetime bt)
   {
      m_count++;
      int W = 2*m_depth + 1;
      int s = ArraySize(m_wH);
      ArrayResize(m_wH, s+1); ArrayResize(m_wL, s+1); ArrayResize(m_wT, s+1);
      m_wH[s]=bh; m_wL[s]=bl; m_wT[s]=bt;
      if(ArraySize(m_wH) > W)
      {
         ArrayRemove(m_wH,0,1); ArrayRemove(m_wL,0,1); ArrayRemove(m_wT,0,1);
      }
      // Python: count < 2*depth+1 はスキップ
      if(m_count < W) return;

      // candidate は中央 (win[depth])。絶対 index = count-1-depth。
      double ch = m_wH[m_depth];
      double cl = m_wL[m_depth];
      datetime ct = m_wT[m_depth];
      int idx = m_count - 1 - m_depth;

      bool isHigh=true, isLow=true;
      for(int k=0; k<W; k++)
      {
         if(k==m_depth) continue;
         if(m_wH[k] >= ch) isHigh=false;
         if(m_wL[k] <= cl) isLow=false;
         if(!isHigh && !isLow) break;
      }
      if(!isHigh && !isLow) return;

      int np = PivN();
      int newKind;
      if(isHigh && isLow)
         newKind = (np>0 && m_pKind[np-1]==KIND_HIGH) ? KIND_LOW : KIND_HIGH;
      else
         newKind = isHigh ? KIND_HIGH : KIND_LOW;
      double price = (newKind==KIND_HIGH) ? ch : cl;

      if(np==0){ PushPivot(idx, ct, newKind, price); return; }

      int    lastKind  = m_pKind[np-1];
      double lastPrice = m_pPrice[np-1];
      if(newKind == lastKind)
      {
         // 同方向: より極端になったときだけ更新
         if((newKind==KIND_HIGH && price>lastPrice) ||
            (newKind==KIND_LOW  && price<lastPrice))
         {
            m_pPrice[np-1]=price; m_pKind[np-1]=newKind;
            m_pIdx[np-1]=idx;     m_pTime[np-1]=ct;
         }
         return;
      }
      // 逆方向: deviation 未満なら無視
      if(MathAbs(price - lastPrice) < m_dev) return;
      PushPivot(idx, ct, newKind, price);
   }
};

//============================ トレンド判定 (純関数) ====================

// 直近 4 ピボットから Dow トレンド。Python _dow_trend / JS dowTrend と等価。
int DowTrend(CZigZag &zz)
{
   int n = zz.PivN();
   if(n < 4)
   {
      if(n >= 2)
      {
         int    lk=zz.PivKind(n-1), pk=zz.PivKind(n-2);
         double lp=zz.PivPrice(n-1), pp=zz.PivPrice(n-2);
         if(lk==KIND_HIGH && pk==KIND_LOW)  return (lp>pp)? TR_UP   : TR_NONE;
         if(lk==KIND_LOW  && pk==KIND_HIGH) return (lp<pp)? TR_DOWN : TR_NONE;
      }
      return TR_NONE;
   }
   double hpr[4]; double lpr[4]; int hc=0, lc=0;
   for(int i=n-4; i<n; i++)
   {
      if(zz.PivKind(i)==KIND_HIGH) hpr[hc++]=zz.PivPrice(i);
      else                          lpr[lc++]=zz.PivPrice(i);
   }
   if(hc>=2 && lc>=2)
   {
      bool hh = hpr[hc-1] > hpr[hc-2];
      bool hl = lpr[lc-1] > lpr[lc-2];
      bool ll = lpr[lc-1] < lpr[lc-2];
      bool lh = hpr[hc-1] < hpr[hc-2];
      if(hh && hl) return TR_UP;
      if(ll && lh) return TR_DOWN;
   }
   return TR_NONE;
}

// 直近 2 つの同種ピボットでトレンドラインを引き、現在価格との符号付き距離/ATR。
// Python _trendline_dist_atr / JS trendlineDist と等価。valid=false なら無効。
double TrendlineDist(CZigZag &zz, int curIdx, double curPrice, double atrVal,
                     int trend, bool &valid)
{
   valid=false;
   if(trend!=TR_UP && trend!=TR_DOWN) return 0.0;
   int kind = (trend==TR_UP) ? KIND_LOW : KIND_HIGH;
   int n = zz.PivN();
   int i2=-1, i1=-1;
   for(int i=n-1; i>=0; i--)
   {
      if(zz.PivKind(i)==kind)
      {
         if(i2<0) i2=i;
         else { i1=i; break; }
      }
   }
   if(i1<0 || i2<0) return 0.0;
   int    idx1=zz.PivIdx(i1), idx2=zz.PivIdx(i2);
   double p1=zz.PivPrice(i1), p2=zz.PivPrice(i2);
   if(idx2 <= idx1) return 0.0;
   double slope   = (p2 - p1) / (double)(idx2 - idx1);
   double lineNow = p2 + slope * (curIdx - idx2);
   if(atrVal <= 0) return 0.0;
   valid=true;
   return (curPrice - lineNow) / atrVal;
}

int Opposite(int d){ return d==TR_UP ? TR_DOWN : (d==TR_DOWN ? TR_UP : TR_NONE); }

//============================ グローバル状態 ===========================
CZigZag *g_zzM5  = NULL;
CZigZag *g_zzM15 = NULL;
CZigZag *g_zzM30 = NULL;
CZigZag *g_zzH1  = NULL;
CZigZag *g_zzH4  = NULL;

datetime g_lastBar    = 0;
datetime g_lastM5Fed  = 0;
datetime g_lastM15Fed = 0;
//--- overlay 用: 日次口座残高(realized)のバッファ(過去 InpOvDays 日)
double   g_eqBuf[];
long     g_lastEqDay = -1;
double   g_ovMA = 0.0;
datetime g_lastM30Fed = 0;
datetime g_lastH1Fed  = 0;
datetime g_lastH4Fed  = 0;

int      g_barIdx        = -1;
int      g_lastEntryBar  = -1000000000;
bool     g_prevHasPos    = false;

int      g_m5hist[];          // M5 トレンド履歴 (ring)

CTrade   g_trade;
int      g_csv = INVALID_HANDLE;

// 直近エントリー (outcome ログ用)
bool     g_eValid=false;
int      g_eSide=0;
double   g_eEntry=0, g_eSL=0, g_eTP=0, g_eSLdist=0, g_eLot=0;
datetime g_eTime=0;

//+------------------------------------------------------------------+
//| UTC オフセット解決: Auto=EET(GMT+2/+3) + EU DST を日付から自動判定。 |
//|  TimeGMT() はテスターで信頼できない(GMT==server扱い)ため、バーの    |
//|  日付から DST を計算する方式にする(テスター/ライブ共通で正しく動く)。 |
//|  返り値 = server時刻に足すと UTC になる時間数 (GMT+3 => -3)。         |
//+------------------------------------------------------------------+
datetime LastSundayUTC(int year, int month)
{
   // 当月末日を求める(翌月1日の前日)
   int nm = (month == 12) ? 1 : month + 1;
   int ny = (month == 12) ? year + 1 : year;
   MqlDateTime t1; t1.year=ny; t1.mon=nm; t1.day=1; t1.hour=0; t1.min=0; t1.sec=0;
   datetime firstNext = StructToTime(t1);
   datetime lastDay = firstNext - 86400;             // 当月末日 00:00
   MqlDateTime w; TimeToStruct(lastDay, w);
   return lastDay - w.day_of_week * 86400;           // day_of_week: 0=日曜
}

bool IsEuDst(datetime t)
{
   MqlDateTime d; TimeToStruct(t, d);
   datetime start = LastSundayUTC(d.year, 3);        // 3月最終日曜
   datetime end   = LastSundayUTC(d.year, 10);       // 10月最終日曜
   return (t >= start && t < end);
}

int ResolveUtcOffset(datetime serverTime)
{
   if(!InpAutoUtcOffset) return InpServerUtcOffset;
   // EET: 夏(DST)= GMT+3 => offset -3 / 冬 = GMT+2 => offset -2
   return IsEuDst(serverTime) ? -3 : -2;
}

//+------------------------------------------------------------------+
int OnInit()
{
   double dev = ZZ_DEV_PIPS * DEV_PIP;
   g_zzM5  = new CZigZag(ZZ_DEPTH_M5,  dev);
   g_zzM15 = new CZigZag(ZZ_DEPTH_M15, dev);
   g_zzM30 = new CZigZag(ZZ_DEPTH_M30, dev);
   g_zzH1  = new CZigZag(ZZ_DEPTH_H1,  dev);
   g_zzH4  = new CZigZag(ZZ_DEPTH_H4,  dev);

   ArrayResize(g_m5hist, 0);

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.SetDeviationInPoints(20);

   if(InpCsvLog)
   {
      string fn = "mtfpb_v2_" + _Symbol + ".csv";
      g_csv = FileOpen(fn, FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
      if(g_csv!=INVALID_HANDLE)
         FileWrite(g_csv, "type","time","side","entry","sl","tp","sl_dist",
                          "lot","risk_amt","balance","atr","major");
   }

   datetime now = TimeCurrent();
   int offNow = ResolveUtcOffset(now);
   PrintFormat("[mtfpb] init sym=%s acct_ccy=%s risk%%=%.2f rr=%.1f align=%d roomR=%.1f block=[%d,%d) minSL=%.0fp",
               _Symbol, AccountInfoString(ACCOUNT_CURRENCY), InpRiskPct, InpTpRR,
               InpAlignMode, InpRoomRMax, InpBlockHourStart, InpBlockHourEnd, InpMinSlPips);
   PrintFormat("[mtfpb] UTCoffset = %s (現時点 server%+d→UTC, server=%s / DST=%s)",
               (InpAutoUtcOffset ? "AUTO(EET+DST)" : "MANUAL"), offNow,
               TimeToString(now, TIME_DATE|TIME_MINUTES), (IsEuDst(now) ? "夏" : "冬"));
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_csv!=INVALID_HANDLE){ FileClose(g_csv); g_csv=INVALID_HANDLE; }
   if(g_zzM5 !=NULL){ delete g_zzM5;  g_zzM5 =NULL; }
   if(g_zzM15!=NULL){ delete g_zzM15; g_zzM15=NULL; }
   if(g_zzM30!=NULL){ delete g_zzM30; g_zzM30=NULL; }
   if(g_zzH1 !=NULL){ delete g_zzH1;  g_zzH1 =NULL; }
   if(g_zzH4 !=NULL){ delete g_zzH4;  g_zzH4 =NULL; }
}

//+------------------------------------------------------------------+
void OnTick()
{
   // 新しい確定 M5 足の検出 (チャート足に依存せず M5 を明示取得)
   datetime t0 = iTime(_Symbol, PERIOD_M5, 0);
   if(t0 <= 0) return;
   if(t0 == g_lastBar) return;     // 同じ進行中バー
   g_lastBar = t0;
   UpdateEquityBuffer();           // overlay: 日次口座残高を更新
   OnNewM5Bar();
}

//+------------------------------------------------------------------+
// overlay: 新しい暦日ごとに口座残高(realized)を1点サンプルし過去 InpOvDays 日を保持
void UpdateEquityBuffer()
{
   long day = (long)(TimeCurrent() / 86400);
   if(day == g_lastEqDay) return;
   g_lastEqDay = day;
   double eq = AccountInfoDouble(ACCOUNT_BALANCE);   // realized 基準(検証ロジックと一致)
   int n = ArraySize(g_eqBuf);
   if(n < InpOvDays) { ArrayResize(g_eqBuf, n + 1); g_eqBuf[n] = eq; }
   else { for(int i = 0; i < n - 1; i++) g_eqBuf[i] = g_eqBuf[i + 1]; g_eqBuf[n - 1] = eq; }
}
// overlay 倍率: 口座残高が直近 InpOvDays 日MAを割れば InpOvMult、否なら 1.0。スケール不変。
double OverlayMult()
{
   if(!InpOverlay) return 1.0;
   int n = ArraySize(g_eqBuf);
   if(n < MathMax(5, InpOvDays / 3)) return 1.0;
   double s = 0.0; for(int i = 0; i < n; i++) s += g_eqBuf[i];
   g_ovMA = s / n;
   return (AccountInfoDouble(ACCOUNT_BALANCE) < g_ovMA) ? InpOvMult : 1.0;
}

//+------------------------------------------------------------------+
//| ATR (M5、TR の単純平均)。iATR は Wilder 平滑なので使わず手計算で一致。 |
//+------------------------------------------------------------------+
double AtrM5(int period)
{
   double sum=0;
   for(int i=1; i<=period; i++)
   {
      double h  = iHigh(_Symbol, PERIOD_M5, i);
      double l  = iLow (_Symbol, PERIOD_M5, i);
      double pc = iClose(_Symbol, PERIOD_M5, i+1);
      if(h==0 || l==0 || pc==0) return 0.0;   // 履歴不足
      double tr = MathMax(h-l, MathMax(MathAbs(h-pc), MathAbs(l-pc)));
      sum += tr;
   }
   return sum / period;
}

//+------------------------------------------------------------------+
//| 上位 TF の直近確定足 (index 1) を 1 回だけ ZigZag に投入            |
//+------------------------------------------------------------------+
void FeedTF(CZigZag &zz, ENUM_TIMEFRAMES tf, datetime &lastFed)
{
   datetime tt = iTime(_Symbol, tf, 1);
   if(tt > 0 && tt > lastFed)
   {
      zz.Update(iHigh(_Symbol,tf,1), iLow(_Symbol,tf,1),
                iClose(_Symbol,tf,1), iOpen(_Symbol,tf,1), tt);
      lastFed = tt;
   }
}

void PushM5Trend(int t)
{
   int s=ArraySize(g_m5hist);
   ArrayResize(g_m5hist, s+1);
   g_m5hist[s]=t;
   int cap=PULLBACK_LB+5;
   if(ArraySize(g_m5hist) > cap)
      ArrayRemove(g_m5hist, 0, ArraySize(g_m5hist)-cap);
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
      double slp = PositionGetDouble(POSITION_SL);
      if(slp <= 0) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      double vol = PositionGetDouble(POSITION_VOLUME);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      ENUM_ORDER_TYPE ot = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      double pr = 0.0;
      if(OrderCalcProfit(ot, sym, vol, open, slp, pr) && pr < 0) total += -pr;
   }
   return 100.0 * total / bal;
}

//+------------------------------------------------------------------+
//| 現在ポジション (自 magic / 自 symbol)。side: 1=long -1=short 0=none |
//+------------------------------------------------------------------+
bool HasPosition(int &side)
{
   for(int i=PositionsTotal()-1; i>=0; i--)
   {
      ulong tk = PositionGetTicket(i);
      if(tk==0) continue;
      if(!PositionSelectByTicket(tk)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      side = (PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY) ? 1 : -1;
      return true;
   }
   side=0;
   return false;
}

//+------------------------------------------------------------------+
//| メイン: strategy.py の on_bar(ctx) 移植                            |
//+------------------------------------------------------------------+
void OnNewM5Bar()
{
   datetime t1 = iTime(_Symbol, PERIOD_M5, 1);   // 直近確定 M5
   if(t1 <= 0) return;
   if(t1 <= g_lastM5Fed) return;                 // 既処理

   // ---- ZigZag 更新 (M5 → 上位足の順、JS と同順) ----
   g_zzM5.Update(iHigh(_Symbol,PERIOD_M5,1), iLow(_Symbol,PERIOD_M5,1),
                 iClose(_Symbol,PERIOD_M5,1), iOpen(_Symbol,PERIOD_M5,1), t1);
   g_lastM5Fed = t1;
   g_barIdx++;

   FeedTF(g_zzM15, PERIOD_M15, g_lastM15Fed);
   FeedTF(g_zzM30, PERIOD_M30, g_lastM30Fed);
   FeedTF(g_zzH1,  PERIOD_H1,  g_lastH1Fed);
   FeedTF(g_zzH4,  PERIOD_H4,  g_lastH4Fed);

   // ---- ATR ----
   double atr = AtrM5(ATR_PERIOD);
   if(atr <= 0) return;

   // ---- 各 TF トレンド ----
   int m5t  = DowTrend(g_zzM5);
   int m15t = DowTrend(g_zzM15);
   int m30t = DowTrend(g_zzM30);
   int h1t  = DowTrend(g_zzH1);
   int h4t  = DowTrend(g_zzH4);

   PushM5Trend(m5t);

   if(InpDiagEvery>0 && (g_barIdx % InpDiagEvery)==0)
      PrintFormat("[mtfpb] diag bar=%d atr=%.5f trends m5=%d m15=%d h1=%d h4=%d piv(m15=%d h1=%d)",
                  g_barIdx, atr, m5t, m15t, h1t, h4t, g_zzM15.PivN(), g_zzH1.PivN());

   // ---- ポジション状態 (決済検出 + 既存なら何もしない) ----
   double price = iClose(_Symbol, PERIOD_M5, 1);
   int curSide; bool hasPos = HasPosition(curSide);
   if(g_prevHasPos && !hasPos) LogOutcome(price, t1);
   g_prevHasPos = hasPos;
   if(hasPos) return;

   //============ エントリー条件 ============

   // 1. 大局アラインメント
   int a[4]; int an=0;
   if(InpAlignMode >= 3)      { a[0]=h4t; a[1]=h1t; a[2]=m30t; a[3]=m15t; an=4; }
   else if(InpAlignMode == 2) { a[0]=h4t; a[1]=h1t; a[2]=m15t;            an=3; }
   else                       { a[0]=h1t; a[1]=m15t;                     an=2; }
   for(int i=0;i<an;i++) if(a[i]==TR_NONE) return;
   for(int i=1;i<an;i++) if(a[i]!=a[0])    return;
   int majorDir = a[0];

   // 2. M5 が直近 lookback 以内で逆方向だった (押し戻し)
   int opp = Opposite(majorDir);
   int hn = ArraySize(g_m5hist);
   int from = MathMax(0, hn - PULLBACK_LB);
   bool pulled=false;
   for(int i=from; i<hn; i++) if(g_m5hist[i]==opp){ pulled=true; break; }
   if(!pulled) return;

   // 3. M5 が大局方向へ「今まさに転換」
   if(m5t != majorDir) return;
   if(hn >= 2 && g_m5hist[hn-2]==majorDir) return;

   // 4. クールダウン
   if(g_barIdx - g_lastEntryBar < COOLDOWN_BARS) return;

   // 4b. 時間帯フィルタ (UTC)。オフセットは Auto(EET+DST) or 手動。
   if(InpBlockHourStart >= 0)
   {
      int off = ResolveUtcOffset(t1);
      int hourUtc = (int)(((long)t1/3600 + off) % 24);
      if(hourUtc < 0) hourUtc += 24;
      if(hourUtc >= InpBlockHourStart && hourUtc < InpBlockHourEnd) return;
   }

   // 5. v2: H4/H1 トレンドラインブレイク判定
   if(SKIP_ON_TL)
   {
      bool v4, v1;
      double h4d = TrendlineDist(g_zzH4, g_zzH4.Count()-1, price, atr, h4t, v4);
      double h1d = TrendlineDist(g_zzH1, g_zzH1.Count()-1, price, atr, h1t, v1);
      if(majorDir==TR_UP)
      {
         if((v4 && h4d<0) || (v1 && h1d<0)) return;
      }
      else
      {
         if((v4 && h4d>0) || (v1 && h1d>0)) return;
      }
   }

   // ---- SL / TP ----
   double sl, slDist, tp;
   int side;
   if(majorDir==TR_UP)
   {
      double lowP;
      if(!g_zzM15.LastPriceOfKind(KIND_LOW, lowP)) return;
      sl = lowP; slDist = price - sl;
      if(slDist <= 0) return;
      tp = price + slDist * InpTpRR;
      side = 1;
   }
   else
   {
      double highP;
      if(!g_zzM15.LastPriceOfKind(KIND_HIGH, highP)) return;
      sl = highP; slDist = sl - price;
      if(slDist <= 0) return;
      tp = price - slDist * InpTpRR;
      side = -1;
   }

   // room_R フィルタ (直近 M15 高安までの余地/SL が大きすぎる=タイトSL を除外)
   if(InpRoomRMax > 0)
   {
      if(majorDir==TR_UP)
      {
         double hp;
         if(g_zzM15.LastPriceOfKind(KIND_HIGH, hp) && (hp - price)/slDist >= InpRoomRMax) return;
      }
      else
      {
         double lp;
         if(g_zzM15.LastPriceOfKind(KIND_LOW, lp) && (price - lp)/slDist >= InpRoomRMax) return;
      }
   }

   // sl_dist 妥当性 (スプレッド未満 / 遠すぎを排除)
   if(slDist < MIN_SL_ATR * atr) return;
   if(slDist > MAX_SL_ATR * atr) return;
   // 絶対最小SL (pips): タイトSL=コスト(spread+commission)負け層を除外
   if(InpMinSlPips > 0)
   {
      double pip = ((_Digits==3 || _Digits==5) ? 10*_Point : _Point);
      if(slDist < InpMinSlPips * pip) return;
   }

   // ---- サイズ (tick value で口座通貨建てに正確逆算) ----
   double ovMult = OverlayMult();
   double riskAmt, balance;
   double lot = RiskLot(slDist, ovMult, riskAmt, balance);
   if(lot <= 0)
   {
      if(InpDiagEvery>0) PrintFormat("[mtfpb] skip_size slDist=%.5f", slDist);
      return;
   }

   // ---- 発注 ----
   double nSL = NormalizeDouble(sl, _Digits);
   // Fintokei: 同時保有リスク上限チェック(新規の実リスクは OrderCalcProfit で算定)
   if(InpMaxTotalRiskPct > 0)
   {
      double curRiskPct = AccountOpenRiskPct();
      double newProfit = 0.0, newRiskPct = 0.0;
      ENUM_ORDER_TYPE ot = (side==1) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      if(OrderCalcProfit(ot, _Symbol, lot, price, nSL, newProfit))
         newRiskPct = 100.0 * MathAbs(newProfit) / balance;
      if(curRiskPct + newRiskPct > InpMaxTotalRiskPct + 1e-9)
      {
         PrintFormat("[mtfpb] SKIP(Fintokei) %s 保有リスク%.2f%% + 新規%.2f%% > 上限%.1f%%",
                     _Symbol, curRiskPct, newRiskPct, InpMaxTotalRiskPct);
         return;
      }
   }
   double nTP = NormalizeDouble(tp, _Digits);
   bool ok;
   if(side==1) ok = g_trade.Buy (lot, _Symbol, 0.0, nSL, nTP, "mtfpb_v2");
   else        ok = g_trade.Sell(lot, _Symbol, 0.0, nSL, nTP, "mtfpb_v2");

   if(!ok)
   {
      PrintFormat("[mtfpb] ORDER FAIL %s ret=%d %s",
                  (side==1?"BUY":"SELL"), g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
      return;
   }

   g_lastEntryBar = g_barIdx;
   g_eValid=true; g_eSide=side; g_eEntry=price; g_eSL=nSL; g_eTP=nTP;
   g_eSLdist=slDist; g_eLot=lot; g_eTime=t1;

   PrintFormat("[mtfpb] ENTRY %s %s price=%.5f sl=%.5f tp=%.5f lot=%.2f risk$=%.2f (bal=%.2f) atr=%.5f overlay=x%.1f(MA=%.0f)",
               (side==1?"long":"short"), _Symbol, price, nSL, nTP, lot, riskAmt, balance, atr, ovMult, g_ovMA);

   if(g_csv!=INVALID_HANDLE)
   {
      FileWrite(g_csv, "entry", TimeToString(t1, TIME_DATE|TIME_MINUTES),
                (side==1?"long":"short"), price, nSL, nTP, slDist, lot, riskAmt, balance, atr,
                (majorDir==TR_UP?"up":"down"));
      FileFlush(g_csv);
   }
}

//+------------------------------------------------------------------+
//| 口座 risk% を sl_dist で逆算。tick value(口座通貨) を使うので正確。   |
//|  return: lot (0=不可)。riskAmt/balance を out で返す。              |
//+------------------------------------------------------------------+
double RiskLot(double slDist, double ovMult, double &riskAmt, double &balance)
{
   balance = AccountInfoDouble(ACCOUNT_BALANCE);
   riskAmt = balance * (InpRiskPct / 100.0) * ovMult;   // overlay でリスクを縮小
   if(balance<=0 || slDist<=0 || riskAmt<=0) return 0.0;

   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE); // 1tick/1lot の口座通貨価値
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickVal<=0 || tickSize<=0) return 0.0;

   double moneyPerLot = (slDist / tickSize) * tickVal;   // SL まで動いた時の 1lot 損失
   if(moneyPerLot<=0) return 0.0;

   double lot = riskAmt / moneyPerLot;

   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step>0) lot = MathFloor(lot/step)*step;
   if(lot > InpMaxLot) lot = InpMaxLot;
   if(lot > maxLot)    lot = maxLot;
   if(lot < minLot)    return 0.0;
   return lot;
}

//+------------------------------------------------------------------+
//| 決済検出時の outcome ログ (exit は近似。正確な損益はレポートが正)。   |
//+------------------------------------------------------------------+
void LogOutcome(double exitPrice, datetime t)
{
   if(!g_eValid) return;
   double pnlPrice = (g_eSide==1) ? (exitPrice - g_eEntry) : (g_eEntry - exitPrice);
   PrintFormat("[mtfpb] EXIT %s pnl_price~=%.5f", (g_eSide==1?"long":"short"), pnlPrice);
   if(g_csv!=INVALID_HANDLE)
   {
      FileWrite(g_csv, "outcome", TimeToString(t, TIME_DATE|TIME_MINUTES),
                (g_eSide==1?"long":"short"), g_eEntry, g_eSL, g_eTP, g_eSLdist,
                g_eLot, pnlPrice, AccountInfoDouble(ACCOUNT_BALANCE), 0.0, "");
      FileFlush(g_csv);
   }
   g_eValid=false;
}
//+------------------------------------------------------------------+
