// =====================================================================
//  framework/ctrader/PropKit.cs
//
//  PropKit の cTrader (cAlgo / C#) 版。MT4/MT5 版と「同一の Pk* API・
//  同一の挙動」を保つ契約。cTrader の口座/発注 API は Robot の protected
//  メンバなので、PropKit は「基底クラス」として提供し、戦略がこれを継承する。
//
//      [Robot] public class BreakoutH1 : PropKitRobot { ... }
//
//  cTrader は units（数量）モデル。MT の lot に対して volumeInUnits を使う。
//  open→SL 損失は pips * Symbol.PipValue * units（口座通貨）で算出でき、
//  3 プラットフォーム中もっともクリーン（MT4 の tick value 問題が無い）。
//
//  Profiles.cs（enum/struct/Profiles.All）は config/profiles.yaml から
//  自動生成（手で編集しない）。 → python tools/gen_profiles.py
//
//  ⚠ 限界（README 参照）:
//    - 初期残高は起動時に取得（cTrader 再起動でベースラインがリセット）。
//      長期チャレンジは稼働を切らさない運用にすること。
//    - 経済指標カレンダー API は無いため news は手動窓のみ。
//    - CSV ではなく Print ログ（FullAccess 不要にするため）。
// =====================================================================
using System;
using System.Globalization;
using cAlgo.API;
using cAlgo.API.Internals;
using PropKit;

namespace cAlgo
{
    public enum PkGuard { Ok = 0, BlockNew = 1, Halt = 2 }

    public abstract class PropKitRobot : Robot
    {
        // --- 戦略が OnStart で設定してから PkInit() を呼ぶ ---------------
        protected string PkStrategyName = "strategy";
        protected long PkMagic = 0;            // cTrader は Label で識別
        protected TimeFrame PkTf = TimeFrame.Hour;
        protected double PkRiskPct = 0.5;
        protected double PkMaxLot = 50.0;      // lot 単位（units へ換算）
        protected bool PkOverlay = true;
        protected int PkOvDays = 60;
        protected double PkOvMult = 0.5;
        protected string PkNewsWindowsCsv = "";
        protected ProfileCfg Prof;

        // --- 内部状態 ----------------------------------------------------
        private Bars _bars;
        private string _label;
        private DateTime _lastBar = DateTime.MinValue;
        private readonly System.Collections.Generic.List<double> _eqBuf = new System.Collections.Generic.List<double>();
        private long _lastEqDay = -1;
        private double _ovMA = 0.0;
        private double _initBal = 0.0;
        private long _guardDay = -1;
        private double _dayRef = 0.0;
        private bool _dayBlocked = false;
        private bool _halted = false;
        // 直近エントリー記録
        private int _eSide = 0; private double _eEntry = 0, _eSL = 0; private double _eVol = 0;

        // --- プロファイル解決 -------------------------------------------
        protected ProfileCfg PkProfile(ProfileId id) { return Profiles.Get(id); }
        // 個別 override。各引数 <0（センチネル -1）なら「プロファイル値を維持」。
        protected void PkApplyOverrides(ref ProfileCfg p, double ovDailyLossPct, double ovTotalLossPct,
                                        double ovConcRiskPct, int ovNewsFilter, int ovMinHoldSec)
        {
            if (ovDailyLossPct >= 0) p.DailyLossPct = ovDailyLossPct;
            if (ovTotalLossPct >= 0) p.TotalLossPct = ovTotalLossPct;
            if (ovConcRiskPct >= 0) p.MaxConcurrentRiskPct = ovConcRiskPct;
            if (ovNewsFilter >= 0) p.NewsFilter = (NewsMode)ovNewsFilter;
            if (ovMinHoldSec >= 0) p.MinHoldSeconds = ovMinHoldSec;
        }

        // --- 初期化 ------------------------------------------------------
        protected void PkInit()
        {
            _bars = MarketData.GetBars(PkTf);
            _label = PkStrategyName + "_" + PkMagic.ToString(CultureInfo.InvariantCulture);
            _initBal = Account.Balance;   // cTrader は起動時取得（再起動でリセット・README 参照）
            Print("[PropKit] init strat={0} sym={1} ccy={2} profile={3} risk%={4} daily={5}/{6} total={7} conc={8} news={9} minhold={10}s initBal={11}",
                PkStrategyName, Symbol.Name, Account.Asset.Name, Prof.DisplayName, PkRiskPct,
                Prof.DailyLossPct, Prof.DailyLossBasis, Prof.TotalLossPct, Prof.MaxConcurrentRiskPct,
                Prof.NewsFilter, Prof.MinHoldSeconds, _initBal);
        }

        // --- 口座 / バー -------------------------------------------------
        protected double PkBalance() { return Account.Balance; }
        protected double PkEquity() { return Account.Equity; }
        protected double PkAsk() { return Symbol.Ask; }
        protected double PkBid() { return Symbol.Bid; }
        // 確定足アクセス。Last(0)=形成中, Last(1)=直近確定足（MQL の shift と 1:1）。
        protected double PkHigh(int shift) { return _bars.HighPrices.Last(shift); }
        protected double PkLow(int shift) { return _bars.LowPrices.Last(shift); }
        protected double PkClose(int shift) { return _bars.ClosePrices.Last(shift); }
        protected DateTime PkTime(int shift) { return _bars.OpenTimes.Last(shift); }
        protected int PkBarCount() { return _bars.Count; }

        protected bool PkIsNewBar()
        {
            DateTime t0 = _bars.OpenTimes.Last(0);
            if (t0 == _lastBar) return false;
            _lastBar = t0;
            return true;
        }

        // --- 汎用指標（確定足）-----------------------------------------
        protected double PkHighest(int fromShift, int count)
        {
            double m = double.MinValue;
            for (int k = fromShift; k < fromShift + count; k++) { double v = _bars.HighPrices.Last(k); if (v > m) m = v; }
            return m;
        }
        protected double PkLowest(int fromShift, int count)
        {
            double m = double.MaxValue;
            for (int k = fromShift; k < fromShift + count; k++) { double v = _bars.LowPrices.Last(k); if (v < m) m = v; }
            return m;
        }
        protected double PkSMA(int fromShift, int count)
        {
            if (count <= 0) return 0.0;
            double s = 0.0;
            for (int k = fromShift; k < fromShift + count; k++) s += _bars.ClosePrices.Last(k);
            return s / count;
        }
        protected double PkATR(int fromShift, int count)
        {
            if (count <= 0) return 0.0;
            double s = 0.0;
            for (int k = fromShift; k < fromShift + count; k++)
            {
                double hh = _bars.HighPrices.Last(k), ll = _bars.LowPrices.Last(k), pc = _bars.ClosePrices.Last(k + 1);
                if (pc == 0) return 0.0;
                s += Math.Max(hh - ll, Math.Max(Math.Abs(hh - pc), Math.Abs(ll - pc)));
            }
            return s / count;
        }

        // --- ポジション --------------------------------------------------
        // 自分（Label）の建玉。side: 1=long,-1=short, 0=なし
        protected int PkPositionSide()
        {
            foreach (var p in Positions)
            {
                if (p.SymbolName != Symbol.Name) continue;
                if (p.Label != _label) continue;
                return p.TradeType == TradeType.Buy ? 1 : -1;
            }
            return 0;
        }
        protected Position PkOwnPosition()
        {
            foreach (var p in Positions)
                if (p.SymbolName == Symbol.Name && p.Label == _label) return p;
            return null;
        }
        protected int PkAccountOpenCount() { return Positions.Count; }

        // --- サイジング（units モデル）---------------------------------
        protected double PkNormalizeVolume(double volume)
        {
            volume = Symbol.NormalizeVolumeInUnits(volume, RoundingMode.Down);
            double maxUnits = Symbol.QuantityToVolumeInUnits(PkMaxLot);
            if (volume > maxUnits) volume = maxUnits;
            if (volume > Symbol.VolumeInUnitsMax) volume = Symbol.VolumeInUnitsMax;
            if (volume < Symbol.VolumeInUnitsMin) return 0.0;
            return volume;
        }

        // --- overlay -----------------------------------------------------
        protected void PkUpdateEquityBuffer()
        {
            long day = Server.Time.Ticks / TimeSpan.TicksPerDay;
            if (day == _lastEqDay) return;
            _lastEqDay = day;
            double eq = Account.Balance;   // realized 基準
            _eqBuf.Add(eq);
            while (_eqBuf.Count > PkOvDays) _eqBuf.RemoveAt(0);
        }
        protected double PkOverlayMult()
        {
            if (!PkOverlay) return 1.0;
            int n = _eqBuf.Count;
            if (n < Math.Max(5, PkOvDays / 3)) return 1.0;
            double s = 0.0; foreach (var v in _eqBuf) s += v;
            _ovMA = s / n;
            return Account.Balance < _ovMA ? PkOvMult : 1.0;
        }

        // --- 同時保有リスク（口座全体）---------------------------------
        protected double PkAccountOpenRiskPct()
        {
            double bal = Account.Balance;
            if (bal <= 0) return 0.0;
            double total = 0.0;
            foreach (var p in Positions)
            {
                if (p.StopLoss == null) continue;
                Symbol s;
                try { s = Symbols.GetSymbol(p.SymbolName); } catch { continue; }
                if (s == null || s.PipSize <= 0 || s.PipValue <= 0) continue;
                double pips = Math.Abs(p.EntryPrice - p.StopLoss.Value) / s.PipSize;
                total += pips * s.PipValue * p.VolumeInUnits;
            }
            return 100.0 * total / bal;
        }
        protected bool PkConcurrentRiskExceeded(double newRealRisk)
        {
            if (Prof.MaxConcurrentRiskPct <= 0) return false;
            double bal = Account.Balance; if (bal <= 0) return false;
            double cur = PkAccountOpenRiskPct();
            double add = 100.0 * newRealRisk / bal;
            if (cur + add > Prof.MaxConcurrentRiskPct + 1e-9)
            {
                Print("[PropKit] SKIP(conc-risk) {0} 保有{1:F2}%+新規{2:F2}% > 上限{3:F1}%",
                    Symbol.Name, cur, add, Prof.MaxConcurrentRiskPct);
                return true;
            }
            return false;
        }

        // --- ニュース・フィルタ -----------------------------------------
        protected bool PkInManualNewsWindow(DateTime now)
        {
            if (string.IsNullOrEmpty(PkNewsWindowsCsv)) return false;
            int w = Prof.NewsWindowMin * 60;
            foreach (var part in PkNewsWindowsCsv.Split(';'))
            {
                string s = part.Trim();
                if (s.Length == 0) continue;
                DateTime t;
                if (DateTime.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out t))
                    if (Math.Abs((now - t).TotalSeconds) <= w) return true;
            }
            return false;
        }
        protected bool PkNewsBlocksEntry()
        {
            if (Prof.NewsFilter != NewsMode.Window) return false;
            return PkInManualNewsWindow(Server.Time);
        }

        // --- 最小保有時間 ------------------------------------------------
        protected bool PkMinHoldElapsed()
        {
            if (Prof.MinHoldSeconds <= 0) return true;
            var p = PkOwnPosition();
            if (p == null) return true;
            return (Server.Time - p.EntryTime).TotalSeconds >= Prof.MinHoldSeconds;
        }

        // --- プロップ・ガード -------------------------------------------
        protected void PkFlattenOwn()
        {
            var p = PkOwnPosition();
            if (p != null) { ClosePosition(p); Print("[PropKit] FLATTEN(own) {0}", Symbol.Name); }
        }
        protected PkGuard PkGuardCheck()
        {
            if (_halted) return PkGuard.Halt;
            double eq = Account.Equity;

            long day = Server.Time.Ticks / TimeSpan.TicksPerDay;
            if (day != _guardDay)
            {
                _guardDay = day;
                _dayRef = Prof.DailyLossBasis == LossBasis.Equity ? eq : Account.Balance;
                _dayBlocked = false;
            }
            if (Prof.TotalLossPct > 0.0 && _initBal > 0.0)
            {
                double floor = _initBal * (1.0 - Prof.TotalLossPct / 100.0);
                if (eq < floor)
                {
                    Print("[PropKit] TOTAL-LOSS breach eq={0:F2} < floor={1:F2} (init={2:F2} -{3:F1}%)", eq, floor, _initBal, Prof.TotalLossPct);
                    if (Prof.OnTotalBreach == BreachAction.FlattenHalt) { PkFlattenOwn(); _halted = true; }
                    return PkGuard.Halt;
                }
            }
            if (Prof.DailyLossPct > 0.0 && _dayRef > 0.0)
            {
                double dd = 100.0 * (_dayRef - eq) / _dayRef;
                if (dd >= Prof.DailyLossPct)
                {
                    if (!_dayBlocked)
                        Print("[PropKit] DAILY-LOSS breach dd={0:F2}% >= {1:F1}% (ref={2:F2} eq={3:F2})", dd, Prof.DailyLossPct, _dayRef, eq);
                    _dayBlocked = true;
                    if (Prof.OnDailyBreach == BreachAction.FlattenHalt) PkFlattenOwn();
                    return PkGuard.BlockNew;
                }
            }
            if (Prof.MaxOpenPositions > 0 && PkAccountOpenCount() >= Prof.MaxOpenPositions)
                return PkGuard.BlockNew;
            return PkGuard.Ok;
        }

        // --- 発注 / 決済 -------------------------------------------------
        // side:1=buy,-1=sell。slPrice は価格。units を risk% から逆算して発注。
        protected bool PkOpen(int side, double slPrice, string comment)
        {
            double price = side == 1 ? Symbol.Ask : Symbol.Bid;
            double pipsRisk = Math.Abs(price - slPrice) / Symbol.PipSize;
            if (pipsRisk <= 0) return false;
            double ovMult = PkOverlayMult();
            double bal = Account.Balance;
            double riskAmt = bal * (PkRiskPct / 100.0) * ovMult;
            double moneyPerUnit = pipsRisk * Symbol.PipValue;   // 1 unit の open→SL 損失
            if (moneyPerUnit <= 0) return false;
            double volume = PkNormalizeVolume(riskAmt / moneyPerUnit);
            if (volume <= 0) { Print("[PropKit] skip size {0} pipsRisk={1:F1}", Symbol.Name, pipsRisk); return false; }

            double realRisk = volume * moneyPerUnit;
            if (PkConcurrentRiskExceeded(realRisk)) return false;

            var tt = side == 1 ? TradeType.Buy : TradeType.Sell;
            var r = ExecuteMarketOrder(tt, Symbol.Name, volume, _label, pipsRisk, null, comment);
            if (!r.IsSuccessful) { Print("[PropKit] ORDER FAIL {0}", r.Error); return false; }
            double fill = r.Position != null ? r.Position.EntryPrice : price;
            _eSide = side; _eEntry = fill; _eSL = slPrice; _eVol = volume;
            Print("[PropKit] ENTRY {0} {1} price={2} sl={3} units={4} risk{5}={6:F2} 実損≈{7:F2} (bal={8:F2}) overlay=x{9}(eq={10:F0} MA={11:F0})",
                side == 1 ? "long" : "short", Symbol.Name, fill, slPrice, volume, Account.Asset.Name,
                riskAmt, realRisk, bal, ovMult, Account.Balance, _ovMA);
            return true;
        }
        protected bool PkCloseOwn(string reason)
        {
            var p = PkOwnPosition();
            if (p == null) return false;
            var r = ClosePosition(p);
            if (!r.IsSuccessful) { Print("[PropKit] CLOSE FAIL {0}", r.Error); return false; }
            Print("[PropKit] EXIT({0}) {1}", reason, Symbol.Name);
            return true;
        }
    }
}
