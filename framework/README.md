# PropKit — マルチプラットフォーム EA 開発基盤 (MT5 / MT4 / cTrader)

1 つの戦略ロジックを **MT5 (MQL5) / MT4 (MQL4) / cTrader (cAlgo C#)** の 3 プラットフォームへ
展開し、**Fintokei / FTMO / 通常口座** の 3 プロファイルを**実行時に切り替え**られる基盤。

## 設計の3原則

1. **`config/profiles.yaml` が唯一の真実の源**。各言語の `Profiles.*` は
   `python tools/gen_profiles.py` で**自動生成**（手で編集しない）。
2. **戦略は「判定ロジック」だけ書く**。口座照会・サイジング・overlay・プロップ・ガード・
   発注はすべて各言語の **PropKit** に集約。
3. **プロファイルは実行時 input で選択**。`Profile = Fintokei/FTMO/Normal`。
   個別 input（`-1`=維持のセンチネル）で各ガード値を上書き可能。

```
config/profiles.yaml                       ← 真実の源（プロップ・ルール）
framework/
  mql5/{PropKit.mqh, Profiles.mqh(生成)}    ← MT5 共通機構
  mql4/{PropKit.mqh, Profiles.mqh(生成)}    ← MT4 共通機構
  ctrader/{PropKit.cs, Profiles.cs(生成)}   ← cTrader 共通機構（基底クラス）
strategies/breakout_h1/{mql5.mq5, mql4.mq4, ctrader.cs}   ← 戦略（判定のみ）
tools/{gen_profiles.py, deploy.ps1}
build/                                      ← 配備ビルド（gitignore）
```

---

## プロファイル（`config/profiles.yaml`）

| 項目 | Fintokei | FTMO | Normal |
|---|---|---|---|
| 日次損失 | 5%（**equity**・含み損対象） | 5%（**開始時 balance** 基準） | off |
| 全体損失 | 10%（初期残高基準） | 10%（equity<初期90%） | off |
| 同時保有リスク上限 | 3% | off | off |
| ニュース回避 | off | ±2分（window） | off |
| 最小保有秒 | 30 | 15 | 0 |
| 同時/日次注文上限 | なし | 200 / 2000 | off |
| 日次到達時 | 新規停止 | 新規停止 | — |
| 全体到達時 | 決済+停止 | 決済+停止 | — |

- **`[GUARD]`** = EA が強制するルール。**`[INFO]`** = 記録のみ（`max_strategy_usd` 等）。
- 値の出典は `C:\Users\ether\workspace\Stock-Dashboard\prop_rule.md`（2026-06-14 時点）。
  **両社ともルール改定が頻繁。資金提供前に必ず公式FAQで最新値を確認すること。**
- 変更したら必ず `python tools/gen_profiles.py` を再実行。`--check` で「生成が最新か」を検査
  （CI / pre-commit 用、古ければ非0終了）。

---

## 配備（`tools/deploy.ps1`）

```powershell
# 全プラットフォームを build/ にステージング（コンパイルは各GUIで）
pwsh tools/deploy.ps1 -Platform all

# MT5 端末へ直接配備（Include/PropKit/ と Experts/ にコピー）
pwsh tools/deploy.ps1 -Platform mt5 -TerminalPath "$env:APPDATA\MetaQuotes\Terminal\<hash>"

# cTrader 用に1ファイルへバンドル（using をホイスト）
pwsh tools/deploy.ps1 -Platform ctrader -Bundle
```

- **MT5/MT4**: `#include` が使えるので、`-TerminalPath` 指定で `MQL{5,4}/Include/PropKit/` に
  `PropKit.mqh`+`Profiles.mqh`、`Experts/` に戦略をコピー。MetaEditor で **F7 コンパイル**。
  `-Bundle` を付けると include をインライン化した単一ファイルを `build/` に出力（配布用・任意）。
- **cTrader**: `#include` 不可なので **常にバンドル**（`Profiles.cs`+`PropKit.cs`+戦略を 1 つの
  `.cs` に結合、`using` をファイル先頭へホイスト）。cTrader Automate に貼って **Build**。

---

## 新しい戦略の追加手順

1. `strategies/<name>/` に `mql5.mq5` / `mql4.mq4` / `ctrader.cs` を作る
   （`breakout_h1` を雛形に。**判定ロジックだけ**書き、`Pk*` を呼ぶ）。
2. 各 PropKit が提供する API（抜粋）:
   - 確定足: `PkHigh/Low/Close/Time(shift)`（shift≥1=確定足）, `PkIsNewBar`, `PkBarCount`
   - 指標: `PkHighest/PkLowest/PkSMA/PkATR(from_shift, count)`
   - 口座: `PkBalance/PkEquity/PkAsk/PkBid`
   - サイズ&発注: `PkOpen(side, sl価格, comment)` / `PkCloseOwn(reason)`（risk%・上限・ログ込み）
   - ガード: `PkGuardCheck()`→OK/BLOCK_NEW/HALT, `PkNewsBlocksEntry()`, `PkMinHoldElapsed()`
   - overlay: `PkUpdateEquityBuffer()`（毎足）, `PkOverlayMult()`（`PkOpen` 内で自動適用）
3. `pwsh tools/deploy.ps1 -Platform all` で配備 → 各 GUI でコンパイル/検証。

---

## ⚠ プラットフォーム別の罠と限界（正直な記載）

| 項目 | MT5 | MT4 | cTrader |
|---|---|---|---|
| 口座通貨サイズ計算 | `OrderCalcProfit`（正確） | `MarketInfo` tick value（**罠A**） | `PipValue`×units（クリーン） |
| 経済指標カレンダー | `MqlCalendar`（opt-in/テスター不安定） | **無し**（手動窓のみ） | **無し**（手動窓のみ） |
| 初期残高の永続化 | GlobalVariable（live 永続/tester毎回） | 同左 | **起動時取得（再起動でリセット）** |
| 検証状態 | ✅ A/B検証可（下記） | ⚠ 構造完成・実機未検証 | ⚠ 構造完成・実機未検証 |

- **罠A（MT4 のサイズ）**: MT4 には `OrderCalcProfit` が無く `MODE_TICKVALUE` を使う。
  profit通貨≠口座通貨（JPY口座のゴールド等）でブローカーにより誤値の恐れ（MT5 で直した
  146倍バグの系統）。**MT4 は発注ログの `実損≈` が「残高×risk%」と一致するか実機デモで必ず目視確認**。
- **cTrader の初期残高**: 起動時にのみ取得するため、cTrader を再起動すると総損失ガードの
  ベースラインがリセットされる。長期チャレンジは稼働を切らさない運用にすること（LocalStorage
  永続化は将来課題。`docs/NEXT_TASKS.md`）。
- **`max_strategy_usd`** は EA から厳密計測できないため `[INFO]`（warn のみ）。
- **ニュース自動回避は MT5 の `MqlCalendar` のみ**（`InpUseMt5Calendar`）。MT4/cTrader は
  手動窓 CSV（`InpNewsWindowsCSV="2026.06.18 12:30;..."` UTC）。なお `breakout_h1` は
  **H1 確定足 close でのみ発注**するためニューススパイクに非感応＝実害は小さい。

---

## 検証（breakout_h1）

1. **MT5 A/B（権威）**: 元 `strategies/standalone/breakout_h1.mq5` vs 新 `strategies/breakout_h1/mql5.mq5`
   を同条件で Strategy Tester。**Profile=Normal + 元既定一致の input** ならガード無効化で
   トレード数/時刻/lot/SL が一致するはず（CSV ログ `breakout_h1_<sym>.csv` を diff）。
   次に Profile=Fintokei で元の同時リスク3%スキップ挙動を再現。
2. **数値パリティ**: PropKit 指標（`PkATR/PkHighest/...`）は元 EA と同式。`tools/bo_fast.py` の
   トレードと時刻・R を突き合わせ可能。
3. **MT4 / cTrader**: まず各テスターで **MT5 と同じ entry/exit 足時刻**になるか（=ロジック一致）、
   次に実機デモで `実損≈ 残高×risk%`（=サイズ一致）を確認。**ブローカー検証が済むまで funded 不可**。

採用運用設定（7ペア・risk・overlay 等）は `strategies/standalone/BREAKOUT_README.md` を参照。
