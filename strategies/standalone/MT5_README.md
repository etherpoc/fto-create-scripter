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
| `Server->UTC offset hours` (`InpServerUtcOffset`) | **ブローカー依存** | ★Axiory 夏=-3 / 冬=-2。block_hour を UTC 基準で効かせるため |
| `TP RR (1.5=1:1.5)` | 1.5 | |
| `Align (1=H1+M15,...)` | 1 | H1+M15 のみ(H4/M30 は無関係) |
| `room_R max (0=off)` | 2.0 | |

> ⚠ **MT5 テスターの入力名は変数名でなく `//` コメント(ラベル)で表示される。** 上表のラベルで探すこと。
> ラベル行が無ければ古い .ex5 → MetaEditor で再コンパイルしてテスターの Expert を選び直す。

### 期待値(プロップ運用)

- **net 年 +4〜6% / 合成DD 約8% / 日次最悪 -1%台**。低リスク・プロップ適合の堅実戦略。
- ★ JPY3 は**対円相関が高く同時逆行で合成DDが重なる**(個別DD 3-8% でも 1%/pair 合成は 15.9%)。
  円露出キャップは DD を下げない(連続逆行が主因)ため、**per-pair risk=0.5% で抑えるのが正解**。
- これは 6-8%/月 の目標には届かない(約1/10)。**「負けない堅実な土台」**としての位置づけ。

### 起動後の確認(time_of_day 全損の教訓)

- Experts ログ `[mtfpb] init sym=... risk%=0.50 ... minSL=20p` で設定を確認。
- 最初のエントリー `[mtfpb] ENTRY ... risk$=...` で **risk$ が残高の約0.5%** か必ず目視。
- `InpCsvLog=true` で `MQL5/Files`(テスターは Tester 配下)に entry/outcome の CSV を保存。

---

## 移植の検証状況

- **コンパイル**: 0 errors / 0 warnings(MetaTrader5・Axiory 両 build)。
- **ロジック忠実性**: bit-exact 検証済 FTO 版(`verify_ea_port.mjs`)と構造同一。
- **実機一致**: USDJPY 実機 +9.8%/WR59% が Python net(+10.9%/WR59%)とほぼ一致 → 移植忠実・エッジ実在。
- **既知の差**: 上位足はネイティブ TF(M5集計と等価)、サイズは tick value(より正確)、block_hour は
  サーバー時刻→UTC 変換に `InpServerUtcOffset` が必要。
- 詳細な検証ログは `docs/IMPROVEMENT_RESULTS.md` の「MT5 Axiory 実機検証」節。
