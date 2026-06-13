# MT5 EA — mtf_pullback v2 (`mtf_pullback_v2.mq5`) ※**不採用・記録のみ**

> ## ⚠ 2026-06-14: mtf_pullback (MTF) は **不採用**
>
> 実Axiory公開ヒストリカル(2015-2026, 11年, `data/axiory/`)で再検証した結果、
> **OOS(2015-20)・IS(2021-26) の両方で net 負け**(`tools/axiory_mtf.py`)。
> FTO録音データでは微益(JPY3 年+4-6%)だったが、**実ブローカーの実コストで薄い平均回帰エッジが消滅**した
> ([[mt5-edge-does-not-transfer]] と同型=「エッジは実フィードに転移しない」)。
> breakout との月次相関も **+0.12** と高く、合算すると効率(sumR/DD)が **18.5 → 13.5 に悪化** → **併用の価値なし**。
>
> **→ 運用は breakout_h1 のみ**（[BREAKOUT_README.md](./BREAKOUT_README.md)）。本ファイルは経緯の記録として残す。
> 既にMTFポジションがある場合は**強制決済せず自然にSL/TPで閉じるのを待ち、新規だけ止める**(MTF EAを外す)。

---

## 記録 — 何だったか(復元用のログ)

- **ロジック**: H4/H1/M30/M15 のトレンド整合 + M5 が押し戻し→大局方向へ転換した瞬間にエントリー。
  SL=M15直近スイング(構造anchor)、TP=RR1.5。証拠金1%(後に0.5%)リスクで逆算。
- **v2 採用フィルタ**: H1+M15整合 / `room_R<2.0` / 6-10時UTC除外 / `minSL=20pips`(タイトSL=コスト死を除外) /
  H4/H1トレンドライン破れで skip。
- **EA**: `mtf_pullback_v2.mq5`(MQL5移植・tick valueサイジング・UTCオフセット自動(EET+DST)・overlay搭載・
  3%ルール`InpMaxTotalRiskPct`)。Magic=220611。**コードは残置**(再検証・参考用)。
- **旧「確定運用」JPY3 basket**(USDJPY/GBPJPY/EURJPY, risk0.5%/pair): FTO検証で年+4-6%/合成DD~8%、
  MT5実機の2年窓(2024-26)では net+ だった。**しかし11年実データでは非加算的=不採用。**

## なぜ消したか(教訓)

- **実データ・実OOSが唯一の最終判定。** FTOデータ由来の「JPY3が勝つ/breakoutと併用が正解」は、
  実Axiory 11年(真のOOS含む)で**覆った**。2年の好窓だけ見ると誤る。
- 詳細な検証経緯は `docs/PROGRESS.md`「F章(net再検証)」「E/G章(overlay・実Axiory)」、
  `docs/IMPROVEMENT_RESULTS.md` を参照。

---

> 補足: overlay(エクイティカーブ・デリスク)と3%ルールは breakout_h1 と同一仕様で本EAにも実装済みだが、
> 上記の通り戦略自体が不採用のため運用しない。
