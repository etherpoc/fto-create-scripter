// =====================================================================
//  strategies/breakout_h1/ctrader.cs
//  H1 Donchian ブレイクアウト（long-only）— PropKit 基盤版 (cTrader)
//
//  判定ロジックは MT4/MT5 版と同一。プラットフォーム差は基底クラス
//  PropKitRobot (framework/ctrader/PropKit.cs) が吸収する。
//
//  ★ プロファイルを [Parameter] で実行時選択（Fintokei/FTMO/Normal）。
//
//  セットアップ:
//   - cTrader Automate で新規 cBot を作り、PropKit.cs / Profiles.cs /
//     本ファイルを 1 つのソースにまとめて貼り付け（tools/deploy.ps1 が
//     using をホイストして 1 ファイルにバンドルする）→ Build。
//   - H1 で各シンボルに適用。
//
//  ⚠ 構造完成・ブローカー未検証（cTrader テスター/デモで要確認）。
// =====================================================================
using cAlgo.API;
using PropKit;

namespace cAlgo.Robots
{
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.UTC)]
    public class BreakoutH1 : PropKitRobot
    {
        // ---- プロファイル ----
        [Parameter("Profile", DefaultValue = ProfileId.Fintokei)]
        public ProfileId InpProfile { get; set; }
        [Parameter("Ovr DailyLoss% (-1=keep)", DefaultValue = -1)] public double InpOvrDaily { get; set; }
        [Parameter("Ovr TotalLoss% (-1=keep)", DefaultValue = -1)] public double InpOvrTotal { get; set; }
        [Parameter("Ovr ConcRisk% (-1=keep)", DefaultValue = -1)] public double InpOvrConc { get; set; }
        [Parameter("Ovr News (-1=keep/0=off/1=window)", DefaultValue = -1)] public int InpOvrNews { get; set; }
        [Parameter("Ovr MinHold sec (-1=keep)", DefaultValue = -1)] public int InpOvrMinHold { get; set; }
        [Parameter("News windows CSV (UTC)", DefaultValue = "")] public string InpNewsCsv { get; set; }

        // ---- 戦略パラメータ ----
        [Parameter("EntryN", DefaultValue = 30)] public int InpEntryN { get; set; }
        [Parameter("ExitN", DefaultValue = 25)] public int InpExitN { get; set; }
        [Parameter("AtrN", DefaultValue = 20)] public int InpAtrN { get; set; }
        [Parameter("SL x ATR", DefaultValue = 3.0)] public double InpSlAtr { get; set; }
        [Parameter("SMA N (0=off)", DefaultValue = 150)] public int InpSmaN { get; set; }
        [Parameter("Risk %", DefaultValue = 0.5)] public double InpRiskPct { get; set; }
        [Parameter("Long only", DefaultValue = true)] public bool InpLongOnly { get; set; }
        [Parameter("Short only", DefaultValue = false)] public bool InpShortOnly { get; set; }
        [Parameter("Magic", DefaultValue = 220612)] public int InpMagic { get; set; }
        [Parameter("Max lot", DefaultValue = 50.0)] public double InpMaxLot { get; set; }
        [Parameter("Overlay", DefaultValue = true)] public bool InpOverlay { get; set; }
        [Parameter("Overlay days", DefaultValue = 60)] public int InpOvDays { get; set; }
        [Parameter("Overlay mult", DefaultValue = 0.5)] public double InpOvMult { get; set; }

        protected override void OnStart()
        {
            PkStrategyName = "breakout_h1";
            PkMagic = InpMagic;
            PkTf = TimeFrame.Hour;
            PkRiskPct = InpRiskPct;
            PkMaxLot = InpMaxLot;
            PkOverlay = InpOverlay;
            PkOvDays = InpOvDays;
            PkOvMult = InpOvMult;
            PkNewsWindowsCsv = InpNewsCsv;
            Prof = PkProfile(InpProfile);
            PkApplyOverrides(ref Prof, InpOvrDaily, InpOvrTotal, InpOvrConc, InpOvrNews, InpOvrMinHold);
            PkInit();
        }

        protected override void OnTick()
        {
            if (!PkIsNewBar()) return;
            PkUpdateEquityBuffer();
            if (PkBarCount() < InpEntryN + InpAtrN + InpSmaN + 5) return;

            double atr = PkATR(2, InpAtrN);
            if (atr <= 0) return;

            int side = PkPositionSide();
            if (side != 0)
            {
                double bl = PkLow(1), bh = PkHigh(1);
                if (!PkMinHoldElapsed()) return;
                if (side == 1) { double trail = PkLowest(2, InpExitN); if (bl <= trail) PkCloseOwn("trail"); }
                else { double trail = PkHighest(2, InpExitN); if (bh >= trail) PkCloseOwn("trail"); }
                return;
            }

            if (PkGuardCheck() != PkGuard.Ok) return;
            if (PkNewsBlocksEntry()) return;

            double bh1 = PkHigh(1), bl1 = PkLow(1), bc1 = PkClose(1);
            double dhi = PkHighest(2, InpEntryN), dlo = PkLowest(2, InpEntryN);
            double sma = PkSMA(2, InpSmaN);
            bool longOK = (bh1 > dhi) && (InpSmaN == 0 || bc1 > sma) && !InpShortOnly;
            bool shortOK = (bl1 < dlo) && (InpSmaN == 0 || bc1 < sma) && (InpShortOnly || !InpLongOnly);

            if (longOK) OpenWithSl(1, atr);
            else if (shortOK) OpenWithSl(-1, atr);
        }

        private void OpenWithSl(int side, double atr)
        {
            double price = side == 1 ? PkAsk() : PkBid();
            double slDist = InpSlAtr * atr;
            double sl = side == 1 ? price - slDist : price + slDist;
            PkOpen(side, sl, "bo_h1");
        }
    }
}
