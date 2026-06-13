# MT5 EA — mtf_pullback v2 (`mtf_pullback_v2.mq5`)

FTO 版 `mtf_pullback_v2.js` を **MQL5 に忠実移植**した MetaTrader5 用 EA。
検証済み mtf_pullback v2(H1+M15 + room_R + 時間帯 + RR1.5)と同一ロジック。

- **コンパイル**: MetaEditor で F7、または CLI `metaeditor64.exe /compile:"...mtf_pullback_v2.mq5"`(0 errors/0 warnings 確認済)。
- **どのチャート足でも可**(M5 を `iX(_Symbol,PERIOD_M5,..)` で明示取得)。上位足はネイティブ TF を使用。
- **サイジングは `SYMBOL_TRADE_TICK_VALUE` で口座通貨に正確換算**(FTO の手動換算不要)。

---

## 🏆 確定運用設定 — JPY3 basket (2026-06-12 実機検証で確定)

実機(Axiory)+実コスト(スプレッド+往復コミッション$12/lot)検証の結論:
**勝つのは JPYペアのみ**(値幅が大きくコスト耐性が高い)。非JPY(EURUSD等タイトSL)はコストで負け。

| ペア | 実機net/2年 | WR | PF | DD |
|---|---|---|---|---|
| USDJPY | +9.8% | 59% | 1.92 | 3.3% |
| GBPJPY | +9.4% | 56% | 1.65 | 3.3% |
| EURJPY | +3.5% | 47% | 1.20 | 7.6% |
| ~~EURUSD~~ | ~~-3.5%~~ | 31% | 0.63 | — | ← **非JPYは除外** |

### 推奨パラメータ(各ペアの M5 チャートに 1 つずつ EA を貼る)

| テスター/EA 入力ラベル | 値 | 意味 |
|---|---|---|
| `Risk % per trade (1=1%)` | **0.5** | ★3枚合成DD<10%に収める唯一の値(1%だとDD15.9%で失格) |
| `絶対最小SL pips (...)` (`InpMinSlPips`) | **20** | タイトSL=コスト負け層を除外(実機で必須) |
| `Block hour start UTC (-1=off)` | **6** | ロンドン午前の高ボラ除外 |
| `Block hour end UTC` | 10 | |
| `UTCオフセット自動(...)` (`InpAutoUtcOffset`) | **true(既定)** | ★EET(GMT+2/+3)+DSTを日付から自動判定。**設定不要** |
| `手動Server->UTC offset` (`InpServerUtcOffset`) | (Autoがtrueなら無視) | 非EETブローカーのみ Auto=false で手動指定 |
| `TP RR (1.5=1:1.5)` | 1.5 | |
| `Align (1=H1+M15,...)` | 1 | H1+M15 のみ(H4/M30 は無関係) |
| `room_R max (0=off)` | 2.0 | |

> ⚠ **MT5 テスターの入力名は変数名でなく `//` コメント(ラベル)で表示される。** 上表のラベルで探すこと。
> ラベル行が無ければ古い .ex5 → MetaEditor で再コンパイルしてテスターの Expert を選び直す。

> **UTCオフセットは自動化済み(2026-06-12)**: `InpAutoUtcOffset=true`(既定)で、EET サーバー
> (GMT+2冬/+3夏 = Axiory・Fintokei 等ほぼ全FXブローカー)の DST をバー日付から自動判定。手動設定不要。
> `TimeGMT()` はテスターで信頼できない(GMT==server 扱い)ため日付ベースで算出 = テスター/ライブ共通で正しい。
> init ログに `UTCoffset = AUTO(EET+DST) ... server-3→UTC ... DST=夏` と解決値が出る。非EETブローカーのみ手動。

### 期待値(プロップ運用)

- **net 年 +4〜6% / 合成DD 約8% / 日次最悪 -1%台**。低リスク・プロップ適合の堅実戦略。
- ★ JPY3 は**対円相関が高く同時逆行で合成DDが重なる**(個別DD 3-8% でも 1%/pair 合成は 15.9%)。
  円露出キャップは DD を下げない(連続逆行が主因)ため、**per-pair risk=0.5% で抑えるのが正解**。
- これは 6-8%/月 の目標には届かない(約1/10)。**「負けない堅実な土台」**としての位置づけ。

### 起動後の確認(time_of_day 全損の教訓)

- Experts ログ `[mtfpb] init sym=... risk%=0.50 ... minSL=20p` で設定を確認。
- 最初のエントリー `[mtfpb] ENTRY ... risk$=...` で **risk$ が残高の約0.5%** か必ず目視。
- `InpCsvLog=true` で `MQL5/Files`(テスターは Tester 配下)に entry/outcome の CSV を保存。

### ★ エクイティカーブ・デリスク overlay (`InpOverlay`, 既定 true)

研究E章(2026-06-14)で検証した DD 削減層。**口座残高(realized)が直近 `InpOvDays`(既定60)日MAを
割ったら新規ロットを `InpOvMult`(既定0.5)倍**にする。**口座レベル=ペア構成に依らず効く汎用的な改善**
(breakout_h1 と同一ロジック・残高基準で一致)。entryロジック(エントリー/SL/TP)は不変。

JPY3 net 検証(全期間 WF, 1R=1%/pair):
| | WR | P1/P2 net | maxDD | MAR |
|---|---|---|---|---|
| production(overlay無) | 50.9% | +0.48/+0.71%/月 | 15.9% | 2.4 |
| **production +overlay** | 50.9% | +0.40/**+0.79**%/月 | **11.2%** | **3.4** |

→ **DD -30% / MAR +42% / リターン維持**。デプロイ(0.5%/pair)では合成DDが約8%→~5.6%級に低下。
- entry ログに `overlay=x0.5(MA=...)` が出る。`残高 < MA` の局面で x0.5 か目視。`InpOverlay=false` で従来挙動。
- 注: **align は変えない**(厳格h4h1m15は全12では良く見えるが、JPY3 実デプロイでは頻度減でMAR低下。production h1+m15 が最良)。

### Fintokei: 同時保有リスク上限 (`InpMaxTotalRiskPct`, 既定3.0)

プロップの「同時に持てるリスク 3% まで」ルール用。新規エントリー時に **口座全体(全シンボル・
全 magic・手動含む)の保有中ポジションSL基準リスク合計 + 新規リスク が上限超ならスキップ**
(`OrderCalcProfit` で各ポジの open→SL 損失を算定)。スキップ時は `[mtfpb] SKIP(Fintokei) ...`。0=off。

---

## 移植の検証状況

- **コンパイル**: 0 errors / 0 warnings(MetaTrader5・Axiory 両 build)。
- **ロジック忠実性**: bit-exact 検証済 FTO 版(`verify_ea_port.mjs`)と構造同一。
- **実機一致**: USDJPY 実機 +9.8%/WR59% が Python net(+10.9%/WR59%)とほぼ一致 → 移植忠実・エッジ実在。
- **既知の差**: 上位足はネイティブ TF(M5集計と等価)、サイズは tick value(より正確)、block_hour は
  サーバー時刻→UTC 変換に `InpServerUtcOffset` が必要。
- 詳細な検証ログは `docs/IMPROVEMENT_RESULTS.md` の「MT5 Axiory 実機検証」節。
