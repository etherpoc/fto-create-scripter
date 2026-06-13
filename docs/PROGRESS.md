# 進捗ログ (Progress Log)

このリポジトリの開発進捗を時系列で記録するファイル。
コード上から読み取りにくい「なぜそうしたか」「何を試して効いたか / 効かなかったか」を残す。

---

## アーキテクチャの確立 (初期)

### EA + ローカル Python サーバ の二段構え
- 戦略ロジック差し替え時に EA 再アップロード不要にする目的
- `strategies/thin_client/fto_strategy.js` (汎用 EA、戦略ロジック持たない)
- `server/main.py` (FastAPI + WebSocket、戦略実行サーバ)
- `wss://localhost:8443/ws/strategy` で接続、JSON で tick / commands 交換
- 詳細は [CLAUDE.md](../CLAUDE.md) と [fto_api_reference.md](./fto_api_reference.md)

### 既定戦略
- `zigzag_line_break`: M15 + H1/H4 MTF の ZigZag ベース、ダウ転換 + ライン抜けでエントリ
- `zigzag_ai`: 上の base に AI フィルタを差し込んだバリエーション

---

## AI フィルタの進化 (v1 → v5)

### バックテスト構成
- 8 ペア並列 (AUDUSD / EURUSD / GBPUSD / NZDUSD / USDCAD / USDCHF / USDJPY / XAUUSD)
- M15 ベース、H1/H4 MTF context
- リスク 1% / トレード (デフォルト)、min_rr 1.0、TP は次の H1/H4 ライン or fallback

### 5 版比較サマリ (3 か月相当バックテスト)

| 版 | 主要変更 | 出力先 | enter率 | 勝率 | サンプル | sum_pnl |
|---|---|---|---|---|---|---|
| v1 | 元プロンプト (保守的) | `data/ai_decisions/` | 64.3% | 35.8% | 67 oc | +72.05 |
| Baseline | AlwaysEnter (AI 無効化) | `data/baseline_decisions/` | 100% | 34.0% | 97 oc | -72.73 |
| v2 | プロンプト緩和 (= ほぼ baseline) | `data/ai_v2_decisions/` | 93% | 37.3% | 75 oc | -65.74 |
| v3 | TF 不一致を SKIP 要因に復活 | `data/ai_v3_decisions/` | 68.2% | 40.5% | 42 oc | -43.25 |
| v4 | + RSI / ATR ratio / セッション / conf 閾値 0.7 | `data/ai_v4_decisions/` | 50.3% | 42.5% | 80 oc | +92.91 |
| **v5** | + 時間帯フィルタ + conf サイジング + trailing + tp_rr 1.5 | `data/ai_v5_decisions/` | **50.0%** | **43.2%** ✨ | **118 oc** | **+170.46** ✨ |
| v6 | + contrarian sizing (conf>=0.85 で lot ×0.5) | 未収集 | - | - | - | - |
| v7 | + チャートパターン features + per-symbol guidance + recent_5_ohlc | `data/ai_v7_decisions/` | 62.5% | **36.3%** ⚠️ | 91 oc | +36.48 |

### 各段階で得られた知見

#### v1 (初期実装)
- features: ZigZag pivots + 上位足 pivots + 「壁」距離
- LLM: Gemma 4 E4B (Ollama 経由)
- 結果: WR 35.8%、ペア別では XAUUSD で +95 pnl の独り勝ち、他は微益微損

#### v2 (失敗例: 緩和しすぎ)
- 「null = リスクではなく中立」「default は enter」と書き換え
- 結果: AI が事実上 baseline 化、skip 7% のみ。WR は微改善だが PnL マイナス
- **学び: 「pioneering ENTER」と「フィルタする AI」のバランスは難しい**

#### v3 (TF 不一致復活)
- direction_intent と h1_trend / h4_trend が**逆向き**なら SKIP 寄りに復活
- null は引き続き neutral として扱う
- 結果: WR 40.5%、初めて baseline を意味のある幅で超える
- ただしサンプル小 (42 outcomes)

#### v4 (大きな飛躍)
- 追加 features:
  - `symbol` (LLM の銘柄知識を活用)
  - `h1_trend` / `h4_trend` (Python 側で trend を確定済みで渡す)
  - `wall_blocking_h4_atr` / `wall_supporting_h4_atr` (方向相対)
  - `recent_close_diffs_atr` (直近 5 本の動き)
  - `rsi_m15` (RSI 14)
  - `atr_ratio_vs_recent` (ボラ regime)
  - `hour_utc` / `weekday` / `tokyo_open` / `london_open` / `ny_open` / `is_overlap` / `is_quiet`
- LLM 入力からノイズ項 (bar_time / bar_idx / atr / price) 除去
- AI confidence threshold (env `AI_CONF_THRESHOLD=0.7`)
- 結果:
  - WR 42.3% (+8.3pp vs Baseline)
  - PnL +91 (Baseline -73 から大逆転)
  - XAUUSD: 17% → 47% WR、EURUSD: 38% → 71% WR
  - 8 ペア中 6 ペアで Baseline 越え
- **学び**:
  - 数値スケールを明示 (例: `wall_blocking_h4_atr < 0.5 = SKIP`) すると AI 精度が安定
  - features の方向相対化 (「進行方向の壁」) は小型 LLM で効く
  - 時間帯 / セッション情報は AI が活用してくれる
  - USDCAD は AI/baseline どちらも弱い (戦略自体の限界)

#### v5 (6 か月相当バックテスト完了、現状ベスト)
変更内容:
- TP 近め: `tp_rr 2.0 → 1.5` (WR up を狙う)
- 時間帯ハードフィルタ (Mon < 7 UTC / Fri ≥ 18 UTC / 土日 を Python 側で SKIP)
- AI conf >= 0.85 のとき lot ×1.5 (高確信時は積極)
- EA 側 trailing close (含み益 +1R 到達後 0R に戻ったら CloseOrder = 疑似 BE 保護)
  - FTO API に SL Modify / Partial Close が無いための代替実装

結果:
- WR 43.2% (+9.2pp vs Baseline、+0.7pp vs v4)
- sum_pnl **+170.46** (v4 の +93 から **約 2 倍**)
- outcomes 118 (これまでで最大、信頼性向上)
- ペア別:
  - **XAUUSD WR 47% → 61%** (pnl +93 → +176) — TP closer + trailing が金で効いた可能性高い
  - USDCHF / EURUSD / NZDUSD で改善維持
  - USDJPY / USDCAD は依然弱い (戦略本体の限界か)

**学び**:
- TP を近づけて WR を上げる戦略は ZigZag 戦略と相性が良い (ライン抜けの初動を取れる)
- Trailing は急変動するペア (XAUUSD) で効果大、小ボラペアでは影響薄
- 時間帯フィルタは件数を大きく変えなかった (= AI 単体でもある程度時間帯判断できていた)
- conf サイジングの寄与は単独では切り分け困難。アブレーションは next task

ユーザー実測: v4 6 か月 6% → v5 はおおよそ 11% 相当 (年率 22% 程度) の見込み

##### AI confidence × 勝率の意外な逆相関 (v5 集計後判明)
```
conf < 0.85 (113件): WR 45.0%  sum_pnl +171.21  avg +1.57/件
conf >= 0.85 ( 9件): WR 22.2%  sum_pnl  -0.75  avg -0.08/件
```

- AI の高確信判断 (>=0.85) は **むしろ負けてた**
- 「分かりやすい setup ほど大口に狩られる」現象 (overconfidence bias)
- v5 の 1.5x lot ブーストは逆効果 (損失も 1.5 倍化)
- → **v6: `ai_conf_size_mult` を `1.5 → 0.5`** に反転、contrarian sizing
  - 高 conf 時こそ薄く張る、低 conf を主力に
- 注: 9 件はサンプル少。v6 で再検証必要

##### v5 の純 PnL を計算すると
- 全 +170.46 のうち conf<0.85 群 = +171.21 (= 主力)
- 高 conf 群の 1.5x boost は実質ノイズ程度の害
- = TP closer (1.5R) + trailing が v5 改善の主因と推定

#### v7 (改悪、ablation で原因特定)
変更内容:
- think:False (gemma4:e4b の thinking model 対応、JSON 空応答回避)
- チャートパターン features 追加 (double_top, recent_5_ohlc, touches_*)
- ペア別 SYMBOL_GUIDANCE 追加 (USDJPY/USDCAD/XAUUSD/EURUSD/GBPUSD)
- contrarian sizing (conf>=0.85 で lot ×0.5)

結果: WR 43.2% (v5) → **36.3% (v7) で大幅悪化**、PnL +170 → +36

ablation 結果 (replay で env 切り替えて検証、 XAUUSD/USDJPY 2 ペアで):

| variant | XAUUSD WR | USDJPY WR |
|---|---|---|
| v7 (full) | 43% | 8% |
| v7a (DISABLE_PER_SYMBOL_GUIDANCE) | **50%** | **29%** ✨ |
| v7b (AI_CONF_SIZE_MULT=1.0) | 40% | 8% (同じ) |
| v7c (DISABLE_V7_FEATURES) | 56% | 9% |

**結論**:
- **per-symbol guidance が主犯** (両ペアで悪化)。「BoJ 介入リスク」「oil 相関」
  などの prompt が AI を過度に保守化、エントリ機会を潰していた。
  → コードから `SYMBOL_GUIDANCE` を空に戻した
- v7 features は XAUUSD には軽く逆効果 (43% → 56% で改善)、USDJPY 無影響
  → 保留 (env DISABLE_V7_FEATURES=1 で OFF にできるよう env switch を維持)
- contrarian sizing は意思決定に影響なし (lot だけ変わる)

**学び**:
- LLM に「特化ガイド」を書くと過度に偏った判断に誘導しがち
- LLM の事前知識 (symbol 名から推測) に任せた方が良い場合あり
- ablation には replay 必須。FTO で都度回すと時間掛かりすぎる

---

## v8 / v9 / M5 検証 (2026-06-10)

### 重要発見: 押し目エントリ + H1/H4 alignment

旧ロジック (Z2 ブレイクアウト) を「押し目戻し」に変更し、H1/H4 トレンド一致を hard filter:
- **v8_50** (pullback ratio 0.5): 累積 -5.1%、悪化
- **v8_38** (pullback ratio 0.382): **累積 +23.7%、月利 +0.33%** ← **初の明確な +EV**
- **v8_62** (pullback ratio 0.618): 累積 +15.1%、月利 +0.22%

U 字型: 両端 (0.382 / 0.618) が良く、中間 (0.5) が悪い。

**ペアによって最適 ratio が違う**:
- JPY クロス → 浅い押し目 (0.382): CADJPY +18, AUDJPY +10, EURJPY +8
- USD クロス + XAUUSD → 深い押し目 (0.618): XAUUSD +10.6, USDCHF +8.2, AUDUSD +4.9

### AI 効果の構造的法則

```
緩い base ロジック + AI: 改善 (v7d で +12.7R)
厳しい base ロジック + AI: 悪化 (v8 で -14R)
```
v8 で AI 無効 (baseline) が常に最良。チャートパターン feature, トレンドライン
feature 等を追加しても AI は救えず。

### M5 timeframe 検証

5.5 年 × 12 ペア = 4.8M ticks (= M15 の 3 倍) を record_only で収集 (2.17 GB)。

**m5_38_baseline (AI 無):**
| 指標 | 値 | vs M15 v8_38 |
|---|---|---|
| Trades | 1760 | +3.6x ✅ |
| WR | 36.6% | -4.8pp ❌ |
| Sum R | -1.73 | -23.0R 悪化 ❌ |
| 月利 | -0.03% | -0.36pp |

Uniform で見ると M5 は M15 より悪い。

**ペア別では真逆のパターン**:
- USD クロス + XAUUSD: M5 大幅改善 (USDJPY -8→+22 で大反転、XAUUSD +7→+22)
- JPY クロス: M5 で全部マイナス化 (CADJPY +18→-6 など)

**法則: USD クロス + XAUUSD は M5、JPY クロスは M15**

### In-sample ハイブリッド portfolio (overfit risk あり)

M5 上位 4 (XAUUSD/USDJPY/GBPUSD/AUDUSD) + M15 上位 4 (CADJPY/AUDJPY/EURJPY/GBPJPY):
- Sum R: +97.91 / 66 ヶ月
- 月利 単利: **+1.48%/月** (年率 ~18%)
- v8_38 単独 (+0.33%/月) から **4.5x 改善**

ただし強い in-sample bias。out-of-sample 検証必須。

### ゴール 月利 6-8% との距離

```
v8_38 単独:                  +0.33%/月 (現実値)
in-sample ハイブリッド:      +1.48%/月 (上限)
out-of-sample 想定:          +0.5-1.0%/月 (= 年率 6-12%)
ゴール:                      +6-8%/月
依然 4-6 倍の gap
```

現アーキテクチャの延長線では月利 6-8% は構造的に到達不可能。1% リスク制約下で
M5 + ハイブリッド最適化でも年率 12% 程度が現実的天井。

---

## 5.5 年データセット (2021-01 ~ 2026-06) (2026-06-09 構築)

過去 5.5 年 × 12 ペア (= 4 主要 USD + 4 USDxxx + 4 JPY クロス) の M15/H1/H4 を
record_only モードで FTO から収集。約 160 万 ticks、737 MB。

これ以降のすべての検証はこの「固定データセット」に対する replay で行う:
- FTO は不要 (5 分で再起動・ロジック変更可能)
- 全 variant が**同じ市場データ**を見るのでフェア比較が成立
- サンプル数: 1 ペア 80-200 outcomes、12 ペア合計 1000-2200 outcomes

### Baseline (always-enter、AI 無効) 結果

| 指標 | 値 |
|---|---|
| 期間 | 2021-01 ~ 2026-06 (66 ヶ月) |
| Trades | 2208 |
| WR | 35.4% |
| Avg R/trade | -0.005 |
| Sum R | -11.96 |
| 月利 (単利) | -0.18% |
| 月利 (複利) | -0.29% |
| 合計リターン | -17.3% |

**戦略本体のみでは赤字**。ペア別では JPY クロス + XAUUSD が +EV、USD クロスは
ほぼ全部 −EV。USDCAD/AUDUSD で −18 / −30 R と大きく失う。

### v7d/gemma4 (DISABLE_PER_SYMBOL_GUIDANCE + DISABLE_V7_FEATURES) 結果

| 指標 | 値 | vs Baseline |
|---|---|---|
| Trades | 1373 | -38% (フィルタ動作) |
| WR | 35.3% | ほぼ同等 |
| Sum R | +0.74 | **+12.7 R 改善** |
| 月利 (単利) | +0.01% | +0.19pp |
| 月利 (複利) | -0.07% | +0.22pp |
| 合計リターン | -4.7% | +12.6pp |

**AI フィルタは確かに損失を圧縮**。ただし break-even 付近で、月利 6-8% には程遠い。
ペア別では USDCAD (-18 → -7)、NZDUSD (-7 → -2.8) が顕著改善。EURUSD だけ
悪化 (-1.9 → -6.2)。XAUUSD/AUDJPY/EURJPY のような元々 +EV ペアでは AI フィルタの
追加価値は小さい。

### qwen2.5:7b (v7d 同設定、モデルだけ切替) 結果

| 指標 | 値 | vs gemma4 |
|---|---|---|
| Trades | 1723 | +25% (skip 少なめ) |
| WR | 35.0% | -0.3pp |
| Sum R | -12.01 | **-12.8 R 悪化** |
| 月利 (単利) | -0.18% | -0.19pp |
| 合計リターン | -16.4% | -11.7pp |

**qwen2.5:7b は baseline と同等レベルで gemma4:e4b より明確に劣る**。「大きい
モデル = 良い判断」は成立しなかった。USDJPY (+1.7 → +7.3)、GBPUSD (-0.3 → +4.5) で
qwen が勝つペアあり、USDCAD (-6.9 → -22) では qwen が大幅悪化。**ペアごとに
得意なモデルが違う** が、Uniform で見ると gemma4 が安定して最良。

### 3 variant 横並びまとめ (Uniform = 全 12 ペア同 variant)

| Variant | Trades | WR | Sum R | 月利 単利 | 累積 (66ヶ月) |
|---|---|---|---|---|---|
| baseline | 2208 | 35.4% | -11.96 | -0.18% | **-11.3%** |
| **v7d_gemma4** | **1373** | **35.3%** | **+0.74** | **+0.01%** | **+0.7%** ← Uniform best |
| qwen25_7b | 1723 | 35.0% | -12.01 | -0.18% | -11.3% |

### 架空 portfolio (IN-SAMPLE、参考値)

| Portfolio | Sum R | 月利 単利 | 累積 |
|---|---|---|---|
| Best-per-pair (各ペア best 選択) | +33.8 | +0.51% | +40.1% |
| Positive-pair-only (8 ペアのみ) | +66.1 | +1.00% | **+93.0%** |

**⚠️ 上記 2 つは backtest 結果から後付けで最適化したもので overfitting risk あり**。
実運用では下振れすると見るのが妥当。

### ゴール「月利 6-8%」との距離

```
理論最高 (Positive-only, in-sample): +1.00%/月  →  ゴールの 1/6 〜 1/8
v7d_gemma4 Uniform 実力:             +0.01%/月  →  ほぼ break-even
```

**現在のロジック (M15 ZigZag + AI フィルタ) の延長線では月利 6-8% は構造的に
届かない**水準。out-of-sample で目減りを考慮すると、現実的な天井は **月利 0.3-0.5%
(年率 4-6%) ほど**。FX 個人運用としては悪くないが、ゴールとは 1 桁違う。

### 何が必要か (= NEXT_TASKS の P0 候補)

1. **M5 タイムフレーム** (= トレード数 3x) → 月利 ~3% の可能性
2. **Fine-tuning** (5.5 年 × 2000+ outcomes をデータセット化、戦略専用モデル訓練)
3. **戦略本体の見直し** (ZigZag 単体ではなく、複合戦略やトレンドフォロー追加)
4. **レバレッジ増** (リスク 1% → 2-3% で線形改善、ただし DD も 2-3 倍)
5. **out-of-sample 検証** (= 2016-2020 の 5 年で同検証 → in-sample bias を測る)

**学び**:
- AI モデルの優劣は「ペアによって違う」 (universal best はない)
- 「大きいモデルが偉い」は技術分析タスクでは成立しない (qwen 7B > gemma 4B ではなかった)
- 戦略本体のシグナル品質 (Baseline) が天井を決める。AI は「悪い負けを減らす」だけ
- ペア選択 (loss pair を除外する) は AI フィルタより効果大。ただし overfitting 注意

---

## 実装で踏んだ落とし穴 (検証済み事項)

### FTO 固有の罠
- アップロードは `.js` のみ受付 (TS シンタックス禁止) → [fto-api-language](../.claude/projects/.../fto-api-language.md)
- ES Module + `export default class extends StrategyImplementation` + import 文禁止 (SDK インライン化) → [fto-module-contract]
- メソッド名は PascalCase (`GetActiveOrderCount`, `PlaceOrder`) → [fto-method-casing]
- `Time(0)` は **未確定足**、確定足は index 1 以上 → [fto-api-quirks]
- 描画オブジェクト名は **session_id プレフィックス必須** (古い run の同名と衝突すると CreateChartObject が false) → [fto-drawing-and-commands]
- 色は `SetObjectProperty(OBJPROP_COLOR)` + `ConvertColorToARGB` 経由 (HEX 直渡しはズレる、整数直渡しは alpha=0 で透明)
- `Init()` 時点で `Symbol()` が null を返すことがある → WS 接続は OnTick まで遅延
- SL/TP の **Modify 関数なし** → trailing は CloseOrder で疑似実装
- 部分決済関数なし → 必要なら 2 オーダー分割発注で代替

### マルチペアバックテスト固有の罠
- **ブラウザのタブ throttling** でバックグラウンドタブの描画 API と WS が落ちる → [fto-multitab-throttling]
  - データ収集だけなら EA UI の `Draw on Chart` を OFF にして回避
  - WS は再接続される (session_id が次々増える) ので、`compare_baseline.py` のような集計ツールで対応
- Stop/Start を繰り返すと EA インスタンスが前の WebSocket を close せずに新接続を貼って累積 → 修正済 (`_connectWS` 冒頭で既存 WS close)

---

## 比較 / 分析ツール

- `tools/run_backtest.py <name>` — ローカル backtest 実行
- `tools/compare_baseline.py --ai <dir> --baseline <dir>` — 2 つのデータディレクトリを並べてペア別比較
- `tools/build_training_data.py` — decisions × outcomes を decision_id で join、CSV/JSONL に書き出し

---

## 現在の数値感 (v5)

6 か月相当バックテスト、リスク 1%/トレード、tp_rr 1.5、trailing 有効:
- 全体: WR **43.2%** / sum_pnl **+170.46** (price 単位)
- ユーザー実測: **6 か月で約 11% 利益** (年率 22% 程度) と推定
- XAUUSD が圧倒的稼ぎ頭 (WR 61% / pnl +176)、USDJPY と USDCAD は依然弱い

---

## 2026-06-11: mtf_pullback 戦略 (新ロジック)

### 経緯
- v8/v9 系 AI 戦略は v8_62 を除き ROBUST (P1/P2 両期間 +) と認定できず、月利目標 6-8% から遠い
- time_of_day 戦略は backtest +19.9% だったが FTO 実戦で **$100k → $372 (13 min)** で全損 (スプレッド未考慮)
- 「説得力ある手法ではないのでやめる」とユーザ判断、ロジックを 1 から考え直し

### 仕様 (ユーザ指定)
- **エントリー条件**: H4 / H1 / M30 / M15 のトレンド方向が全て一致 (大局アラインメント) +
  M5 が直近で反対トレンドだった (押し戻し) + M5 が大局方向に転換した瞬間
- **SL**: ロング → M15 直近 Z1 安値 / ショート → M15 直近 Z1 高値 (構造に anchor)
- **TP**: SL と同じ価格距離 (1:1 RR)
- **ポジションサイズ**: 証拠金の 1% リスクで逆算
- **スプレッド対策**: `min_sl_dist_atr = 0.3` (ATR の 30% 未満の SL は skip)

### v2 / v3 のスキップ条件 (ユーザ追加指示)
- **v2**: H4 / H1 のエントリー方向トレンドラインを既にブレイクしている場合 skip
  - 上昇トレンド時: H4/H1 の 2 つの安値を結ぶ線より価格が下なら skip
  - 下降トレンド時: 2 つの高値を結ぶ線より価格が上なら skip
- **v3**: v2 + D1/W1 重要ピボットが進行方向に近接 (2 ATR 以内) なら skip

### 5.5 年 (2021-01〜2026-06) M5 リプレイ結果

**全 12 ペア合算**

| 版 | n | P1 (2021-23) | P2 (2024-26) | 全期間 |
|---|---|---|---|---|
| v1 | 452 | +0.48%/月 | **-0.56%/月** | +0.004%/月 |
| v2 | 335 | +0.58%/月 | **-0.11%/月** | +0.266%/月 |
| v3 | 269 | +0.41%/月 | **-0.05%/月** | +0.198%/月 |

**P2 の負け幅が劇的に改善** (-0.56% → -0.11%)。v2 のトレンドラインフィルタが効いている。

**ROBUST ペア portfolio (両期間 + のペアのみ取引)**

| 版 | ROBUST ペア | P1 月利 | P2 月利 |
|---|---|---|---|
| v1 | CADJPY/EURJPY/USDCAD/USDCHF/XAUUSD (5) | +0.85% | +0.52% |
| **v2** ⭐ | CADJPY/EURJPY/EURUSD/USDCAD (4) | **+0.44%** | **+0.54%** |
| v3 | CADJPY/EURJPY/USDCAD/USDCHF (4) | +0.68% | +0.35% |

⭐ **v2 が最良**: P1 と P2 の月利がほぼ等しい (+0.44 vs +0.54) = 真の Edge の可能性が高い。
v8_62 (P1 +0.18% / P2 +0.24%) を大きく上回り、過学習しにくい。

### 各版の効果分析
- **v2 → v1 改善ペア**: AUDUSD (BAD both → OOS+), EURUSD (Edge GONE → ROBUST), GBPUSD (BAD both → Edge GONE)
  → トレンドライン break filter が「逆張り型のシグナル」を排除した効果
- **v2 → v1 悪化ペア**: USDCHF (ROBUST → Edge GONE), XAUUSD (ROBUST → Edge GONE)
  → これら 2 ペアは「ライン突破時にむしろ加速する」性質？
- **v3 → v2**: D1/W1 ライン近接 filter は微妙 (entries 335→269 で 20% 減るが PnL も僅減)。トレンドラインに比べ寄与小。

### 注意点 (誇張なし)
- **これは backtest 結果でスプレッド未考慮** — 実戦では SL hit が増える可能性
- ただし min_sl_dist_atr=0.3 で「スプレッド未満の SL」は事前排除済 → time_of_day のような壊滅は避けられる想定
- **ROBUST 抽出は in-sample selection** で OOS bias 残存 — 真の利益はもう少し低い可能性
- WR P2 が 48-49% で 1:1 RR なので、ブレイクイーブン近辺 = 実戦では微赤の可能性も
- FTO 実戦検証が必須 (まだ未実施)

### 次のステップ候補
1. v2 を FTO で実戦検証 (まずは ROBUST 4 ペアのみ)
2. WR を 50% 超に持っていく additional filter の検証
3. min_sl_dist_atr の調整 (0.3 → 0.5) で取引数減らす代わりに勝率上げる

---

## 純粋ダウ理論 単体ベースライン検証 (2026-06-11)

### 背景・目的
mtf_pullback は「ダウ理論で方向判定 → MTF 整合 + 押し目で絞り込み → 構造 SL」の多層構成。
ユーザ質問「純粋なダウ理論だけならどうなるか」に答えるため、**フィルタを全部外したダウ単体**の
素の実力を 5.5 年録音データ (12 ペア) で測定。

### 設計 (ユーザ指定)
- 単一 TF = **M15 のみ** (MTF 整合・押し目・トレンドライン等のフィルタなし)
- トレンド判定 = `_dow_trend` (HH/HL=up, LL/LH=down) を mtf_pullback から再利用
- エグジット = **トレンド転換でドテン** (常時ポジション、SL/TP なし)
- 専用スクリプト `tools/backtest_pure_dow.py`
  (既存 WS replay は「1 entry→SL/TP 決済」前提でドテン式を回せない。録音 M5 を直接読み
   M15 集計して Python 内でドテンをシミュレート)

### リスク単位の扱い (重要)
ドテンは SL を置かないので「1 トレード=口座 1%」固定リスクが定義不能。主指標を変更:
- **R_atr = pnl_price / entry時ATR** (ボラ正規化したエッジ尺度)
- **profit factor** (総利益/総損失)、期待値 (平均 R_atr)、**最大単発損失 R_atr** (裾リスク)
- 参考 %/mo は「1×ATR 逆行=口座1%・損失キャップなし」仮定 (mtf_pullback の SL=1% 固定とは別 model)

### 結果 (Walk-Forward P1=2021-23 / P2=2024-26、約19,700 トレード)
| | PF | 期待値R | sumR | 判定 |
|---|---|---|---|---|
| 全12ペア P1 | 1.04 | +0.085 | +918.6 | わずか＋ |
| 全12ペア P2 | **0.98** | -0.039 | -343.8 | **Edge GONE** |
| FXのみ P2 | **0.95** | -0.110 | -899.9 | さらに悪化 |

ペア別: ROBUST=3 (AUDJPY/USDJPY/XAUUSD)、Edge GONE=5 (CADJPY/EURJPY/EURUSD/GBPJPY/GBPUSD)、
OOS+のみ=2 (USDCAD/USDCHF)、BAD both=2 (AUDUSD/NZDUSD)。

### 結論 (誇張なし)
1. **純粋ダウ単体にエッジはほぼ無い。** 全体 PF≈1.0、期待値≈0、P1(＋)→P2(−) で符号反転。
   FXのみ P2 は明確にマイナス。実質コインフリップ。
2. 勝率は全ペア一貫して 40-43%。PF≈1.0 = 少数のトレンド乗りがチョップ負けを相殺するだけ。
3. **ノーストップの裾リスクが致命的。** 最悪単発 -49R (=1トレードで口座-49% 換算)。期待値以前に運用不可。
4. **スプレッド未考慮。** 1ペア5.5年で約1,600トレード (月24回) → コスト控除後はほぼ確実に PF<1.0。
5. ROBUST 3ペアは in-sample 選別バイアスの可能性大 (12分の3は偶然と区別不能)。

### 含意
mtf_pullback の「ダウを方向素材に使い、MTF+押し目フィルタで絞り、構造 SL で裾を切る」設計は
この結果で逆に正当化される。**フィルタと SL こそが (薄いながら) エッジと生存性を作っている。**
ダウ単体や逆方向 (ドテン常時ポジ) には戻らず、ダウを足場にフィルタ/SL を強化する方向が正しい。

### 成果物
- `tools/backtest_pure_dow.py` (専用バックテスト)
- `data/eval_5y/pure_dow_m15/<SYM>/*.jsonl` (outcome、gitignore 対象)
- `data/eval_5y/pure_dow_m15_result.log` (集計ログ)

---

## スタンドアロン EA 作成 — mtf_pullback v2 (2026-06-11)

### 経緯・方針転換
ユーザ判断「**薄い EA はサーバとのラグがあるのでもう使わない**」を受け、mtf_pullback v2 を
**完全スタンドアロン EA** として実装。CLAUDE.md の「EA は薄く保つ」絶対原則を、ユーザの
明示判断（ラグ回避）で**意図的に上書き**。判断ロジックを全部 FTO 上の JS に内包する。
（ロジック修正時は .js 再アップロードが必要になる = thin client と運用が変わる）

### 構成
- **EA**: `strategies/standalone/mtf_pullback_v2.js` (M5 チャート用、サーバ非依存)
  - `strategies/mtf_pullback/strategy.py` の v2 (skip_on_trendline_break=True) を JS 移植
  - ZigZag/Dow/M15・M30集計/トレンドラインを全部 JS で再実装
  - 決済は SL/TP を PlaceOrder に渡して FTO ネイティブ任せ (trailing なし)
- **ログ**: fire-and-forget で `https://localhost:8443/log` に POST (await しない=遅延ゼロ)
  - `server/log_collector.py` (ログ専用・トレード非関与) が `data/fto_mtf_pb_v2_live/` に JSONL 保存
  - `tools/run_log_collector.ps1` で起動。証明書は前回 wss で信頼済みの server/certs を再利用
  - EA の `Log Server Port=0` でサーバ無し運用も可 (ログは DevTools のみ)

### サイズ計算 (time_of_day 全損の教訓)
FTO に tick value / contract size の API が**無い**ため、口座通貨建ての 1 lot 価値を
**シンボル名から通貨換算して自前計算**:
`money_per_lot = sl_dist × contractSize × (口座通貨/quote通貨)`, `lot = balance×risk% / money_per_lot`
- USD クォート(EURUSD)=換算不要 / USDxxx(USDCAD)=自身価格で換算 / JPYクロス(CADJPY/EURJPY)=
  `iClose("USDJPY")` クロス参照、失敗時は `USDJPY fallback` パラメータ
- 各エントリーで `risk$` / `conv_path` をログ出力 → 本番前に「リスクが残高の約1%か」目視検証可能

### 移植の正確性検証 (重要)
純粋ロジック部 (ZigZag/Dow/M15集計/M5トレンド系列) が Python 実装と **完全一致** することを
実データで検証: `tools/verify_ea_port.mjs` (JS) vs `tools/_ea_port_ref.py` (Python)。
EURUSD/CADJPY/EURJPY/USDCAD で **bit-exact 一致を確認済**。EA 構文も `node --check` で検証済。
（検証できないのは FTO 固有部: iXxx の H1/H4 取得、実発注、スプレッド。これは FTO 実機で確認）

### 未検証・本番前にやること
- FTO 実機 (M5チャート) で 4 ペア起動 → 最初の数トレードの `risk$` ログ目視確認
- スプレッド込みで月利 0.4%超 出るか (FTO バックテスト=実スプレッドで検証可能)
- `conv_path` が `fallback_*` ならクロス参照が効いていない → USDJPY fallback を実レートに

---

## RR 改善 → スタンドアロン EA → FTO 実機検証クリア (2026-06-11)

### 経緯
mtf_pullback v2 (RR1.0) は全12/主要ペアで Edge GONE。ユーザ提案の **RR (利確倍率) 引き上げ**
が決定打となった。チャート精読から複数の改善案を検証した結果、判明したこと:

### 検証で分かった「効くもの / 効かないもの」(5.5y WF、R ベース)
- **RR を伸ばすのが最も効く** (利益源は「勝ちを伸ばす」):
  - RR1.0 → Edge GONE / RR1.5 → ROBUST (全12 P2+0.12, 主要4 P2+0.20) / RR2.0 → 強ペアで更に高い
    (ただし全12では P2≈0、弱ペアの WR が損益分岐33%を割る)
  - **RR1.5 が最も広く頑健**。RR2.0 は強ペア限定向き。
- **効かなかった / 逆効果だった案** (いずれもユーザのチャート指摘から実装・検証):
  - SL バッファ (0.3ATR): 4ペアでは改善も全12/主要では限定的
  - 構造インタクト (終値ベース): 4ペアで 4/4 robust も、全12/主要では効果薄
  - 直近安値SL アンカー: ほぼ中立
  - **TP 内スイング (目標を直近高安に抑制): 明確に悪化** (主要 P2 −0.33) ← 利を抑えるのは逆
  → **素の v2 ロジック + RR1.5 が最良**。凝った追加ロジックは EA に入れない判断。

### スタンドアロン EA に反映
`strategies/standalone/mtf_pullback_v2.js` に **TP RR を UI パラメータ化** (既定 1.5)。
SL リスクは常に口座 1%、TP = entry ± sl_dist × RR。FTO 上で 1.5⇄2.0 を再アップロードなしで切替可。
(検証ツール: `tools/backtest_mtf_pb_variants.py` で env PAIRS/ALL 指定、`tools/plot_mtf_pb_entry.py`
で H4/H1/M15/M5 のエントリー検証チャート、`tools/sim_ea.mjs`/`verify_ea_port.mjs` でEA検証)

### ★ FTO 実機バックテスト結果 (スプレッド込み、主要4ペア EURUSD/USDJPY/GBPUSD/XAUUSD、31ヶ月)
| 指標 | 値 |
|---|---|
| 純利益 | **+10,660 (+10.66%)** → **月利 +0.34%** |
| プロフィットファクター | **1.38** |
| 勝率 | 53% (27/51) |
| 最大DD | 7.9% (リターン/DD = 1.35) |
| 平均利益/平均損失 | 1440/1176 = **実効RR 1.22** (狙い1.5、スプレッドが削った) |
| 月次シャープ | 0.27 (低い) |
| 1トレード最大損失 | $1500 = 1.5% (SL スリッページで狙いの1%超過の回あり) |

### 意義と限界 (誇張なし)
- **初めてスプレッド込みでプラス確定。** WR がログ(52%)と FTO レポート(53%)で一致 = スプレッドが
  勝敗を歪めていない。time_of_day (スプレッドで全損) との決定的な違い。**薄いが本物のエッジ。**
- **スプレッドのコストが可視化**: 実効 RR が 1.5→1.22 に低下。それでも WR53% が吸収して純益プラス。
- **ただし月利 +0.34% (年率~4%)、シャープ0.27** で、目標 6-8%/月には構造的に遠い (天井 ~0.3-0.5%/月)。
- **51トレードは小サンプル**、PF1.38 もまだ高信頼ではない。継続してサンプルを増やす必要。

### 次の候補
1. 継続でサンプル増 (100超) + GBPUSD(弱) 外し・CADJPY/EURJPY(強) 追加でポートフォリオ改善
2. RR2.0 を実機検証 (強ペアで高リターン期待、DD も増)
3. この薄いエッジを土台に複数戦略合算等で収益強化を検討

---

## エントリー品質フィルタ探索 → 勝率改善 (2026-06-11)

ライブ(v2+RR1.5)から「勝率・利益を上げる」ロジック改善を、チャート精読＋定量分析＋
WF検証で探索。詳細は **[IMPROVEMENT_RESULTS.md](./IMPROVEMENT_RESULTS.md)**。要点:

- **効いた**: `room_R<2.0` (直近M15スイングまでの余地/SLが大=タイトSL=ノイズ負け を除外) と
  `6-10時UTC除外` (ロンドン午前の高ボラ=ダマシ)。両方とも主要4/全12・両期間に汎化。
  - **ベスト = v2 + RR1.5 + room_R<2.0 + skip6-10h**: 主要4 WR 57→68%/P2 +0.20→+0.38, 4/4。
    全12 WR 49→56%/P2 +0.12→+0.37, 5/12。minSL2.0 は冗長で外した。
- **効かなかった (棄却)**: SLバッファ / ダウ確定SL / 構造インタクト / TP内スイング /
  エグジット改善(建値・部分利確・トレール=利を削る) / M15極値未更新条件 / M15推進波拡大(加速) /
  HTFモード(H4=H1+M15転換=高頻度だがWR35%で品質低)。
- **教訓**: 負けは「タイトSLのノイズ刈られ」と「本物の反転」。エッジは「勝ちを伸ばす(RR↑)」と
  「悪トレード除外(room_R/時間帯)」にあり、SL微調整や利確の早仕舞いは逆効果。
  チャート目視の仮説は定量検証で何度も逆だった (room_R, m15極値, expanding)。
- **過剰適合注記**: 閾値/時間帯はデータ選別。汎化はしたが真のOOSは割引。改善はP1偏重、月利は依然+0.3-0.4%/月。

成果物: `tools/analyze_entry_quality.py` (多特徴 vs 勝敗), `tools/backtest_exit_modes.py` (エグジット比較),
`tools/backtest_mtf_pb_variants.py` (アブレーション, env ALL/PAIRS/HTF/RRTEST), strategy.py に各条件を
パラメータ追加 (全て既定 off = ライブ不変)。

---

## 現状整理: MT5 移植 → net コスト reckoning → JPY3 確定 (2026-06-12)

この日に「ロジック改善の打ち止め → 実コスト評価 → MT5 実機 → 運用設定確定」まで一気に進めた。
詳細データは **[IMPROVEMENT_RESULTS.md](./IMPROVEMENT_RESULTS.md)**、運用設定は
**[../strategies/standalone/MT5_README.md](../strategies/standalone/MT5_README.md)**。

### 1. ロジック改善は実用的天井に到達(全て gross で検証していた)
- H4一致は WR に効くと判明(旧「無関係」を訂正)が、フィルタ/サイジング化は P1偏重で不採用。
- FTO自動分析の提案(連勝マルチンゲール/時間エグジット/損失クールダウン)を全検証 → 頑健な改善ゼロ。
  最大の数字(マルチンゲール+$18k)が最も危険な過剰適合(総DD72-125%)。
- Fintokei プロップ制約(同時3%/日次5%)はこの戦略には緩く効かない。真の拘束は総DD=サイジング一点。
- → エントリー精密化は打ち止め。**ここまで全て gross(スプレッド/コミッション抜き)だった。**

### 2. ★ net コスト reckoning — 結論を塗り替えた
- MT5 移植版を Axiory 実機で回したら **gross +6% が実機 -13.5%(EURUSD)**。実コストでエッジが消えた。
- net 評価(`tools/backtest_net_cost.py` 他, 往復コミ$12/lot+spread)を導入:
  - **全12分散は net 負け(コミだけで-0.7%/月)。「全12 +1.08%」は gross の幻。**
  - **生き残るのは JPYペアのみ**(値幅大→SL広→コスト相対影響小)。非JPYはタイトSLがコストに食われ負け。
- **絶対最小SL(`min_sl_dist_pips`)** をフィルタ追加 → タイトSL=コスト死を除外。確定値=20pips。

### 3. ★ MT5 実機検証(Axiory, block6/offset/minSL20)で「勝てるペア」確定
| ペア | 実機net/2年 | WR | PF | DD | Python net | 転移 |
|---|---|---|---|---|---|---|
| USDJPY | +9.8% | 59% | 1.92 | 3.3% | +10.9% | ◎ |
| GBPJPY | +9.4% | 56% | 1.65 | 3.3% | +11.2% | ◎ |
| EURJPY | +3.5% | 47% | 1.20 | 7.6% | +5.9% | ○ |
| EURUSD | -3.5% | 31% | 0.63 | — | +1.6% | ✗ |
| XAUUSD | net負け(2024+ -4%) | — | — | — | — | ✗ |
- **移植は忠実**(USDJPY 実機+9.8%≒Python+10.9%)。パイプライン全体が正しいと実証。
- GOLD は SL が元々大($11平均)で minSL が効かず、直近レジームで net 負け → 除外確定。

### 4. ★ 確定運用モデル = JPY3 basket
- **USDJPY / GBPJPY / EURJPY を各 M5 チャートに1つ。Risk 0.5%/pair**(対円相関で合成DDが重なるため。
  1%だと合成DD15.9%で失格、0.5%でDD7.9%・プロップ適合)。`tools/finalize_jpy3.py` で確定。
- **net 年+4〜6% / 合成DD約8% / 低リスク**。複利(現在残高ベース)。**6-8%/月目標には届かない(約1/10)が、
  実コスト・実フィードで黒字の「負けない堅実な土台」**。
- 円露出キャップは DD を下げない(連続逆行が主因)ため不要。

### 5. EA の仕上げ
- `strategies/standalone/mtf_pullback_v2.mq5`(MQL5 移植, tick value サイジング, ネイティブMTF)。
- **UTCオフセット自動化**(`InpAutoUtcOffset=true`): EET+DST をバー日付から判定(TimeGMTはテスター不可のため)。
- **minSL デフォルト20**。コンパイル0/0。README/MT5_README に運用手順を記載。

### 6. 判断と次
- ユーザ判断: **現状(JPY3 basket)のまま数か月フォワード運用**して様子見。
- 残課題: Fintokei は履歴品質76%で要再DL。6-8%/月を狙うなら別ロジック(集中型/ブレイクアウト/イベント)が必要。

### この日の教訓(最重要)
- **Python バックテストは長らく gross だった。実コスト(特にコミッション)を入れるまで「勝てる」と言わない。**
  time_of_day(未モデルのスプレッドで全損)と同型の罠が、今度はコミッションで再現しかけた。
- **戦略はデータフィードに敏感**(ブローカーで成績が変わる)。実機・実コストが唯一の最終判定。

---

## B: ブレイクアウト戦略の探索 (2026-06-12, 別ロジックで6-8%/月狙い)

JPY3プルバック(年+5%)は天井。6-8%/月へ「別ロジック=ブレイクアウト/モメンタム」を最初から net で探索。

### 到達点(戦略系譜)
**JPY3プルバック +5%/yr → 金単体ブレイク +11% → H4 long-only バスケット +28% →
H1 long-only バスケット +32〜47% → H1ブレイク + JPY3プルバック合算 +29%/DD9%(プロップ可・無敗)**

### 効いた / 効かなかった(全て net WF × 全12横断で検証)
- **効いた**: ②long-only(ショートのダマシ除外で viable 3→5、P2 -1.27→+4.10%)、
  TF=**H1がH4超え**(agg P2 +4.1→+5.4、金+80→+133、効率R/DD 9.85→16.5)、
  グリッドで H1 en30/ex25/SL2/SMA100(無敗6年・0.25%/pairで年+32%/DD9%)。
- **棄却**: ①ピラミッディング(レバレッジで同DDではbase負け・悪年深掘り)、
  ③広いトレール/chandelier、④終値ブレイク確定、MA交差(Donchianに劣後)。
- **△**: ⑤ボラターゲ(リスクベースで実装済)、⑥部分利確@2R(分散↑だがgold tail↓)。
- **コスト感応**: H1はSL細いが、3pipスプレッドでも両期間+で崩れず(懸念は否定)。

### ★ 合算ポートフォリオ(現状の最高到達点, `tools/combined_portfolio.py`)
H1ブレイク(7ペア,トレンド) + JPY3プルバック(3ペア,平均回帰):
- **月次相関 +0.00(完全無相関)・DD 単体和16.1%→合算9.1%(44%圧縮)**。
- **合算 +28.9%/年・DD 9.1%・無敗年・効率15.6**(BO単体14.9を上回る)。

### ⚠️ 注意 / 残課題
- **6-8%/月は安全DDでは未達**(3倍サイズで届くがDD~27%)。年+29%が安全圏の天井。
- **レジーム依存**: long-only が 2021-26 の金高/円安トレンドに乗る。反転で同時逆行(2026鈍化が予兆かも)。
- **未だ実機未検証**。JPY3の教訓(実コスト/実フィード)から MT5 EA化→実機検証が必須(次の課題)。
- 高速化: `tools/bo_fast.py`(NumPy一括前計算+TF配列ディスクキャッシュ)で約290倍速、旧実装と完全一致。

成果物: `tools/` の backtest_breakout / breakout_deepdive / breakout_lab / breakout_basket /
momentum_lab / breakout_grid / breakout_h1_deepdive / gold_only / gold_pyramid / combined_portfolio / bo_fast。

---

## C: 目標(月利6-8% / DD<=10%)の到達可能性を数式で確定 (2026-06-14)

「6-8%/月をDD10%で」を狙う新ロジック検討の前に、**必要な勝率×トレード数を定量化**
(`tools/target_feasibility.py`、複利・netコスト0.1R・DD評価=24ヶ月MC・p95=プロップ安全側)。

### 土台の法則
- **DD固定下の月利 ≈ (1トレードSharpe) × √N × 定数**。月利はWR↑/RR↑/N↑のどれでも上がるが、
  **Nは平方根でしか効かない**(回数増はコスパ悪)。重要: この√Nは**トレードが独立な場合のみ**成立。
  相関したトレード(同一レジームの5ペア・連続する日中)は実効Nが激減し恩恵が出ない。

### 月利7%を満たす最小N/月 (DD=p95, cost0.1R)
| WR | RR1.0 | RR1.5 | RR2.0 |
|---|---|---|---|
| 50% | 不可 | ~200本/月 | ~40本/月 |
| 55% | 不可(μ≈0) | ~60本/月 | ~20本/月 |
| 60% | ~300本/月 | ~25本/月 | ~12本/月 |
| 65% | ~60本/月 | ~15本/月 | ~8本/月 |
| 70% | ~30本/月 | ~10本/月 | ~6本/月 |

### 結論 (誇張なし)
1. **現状が月2.6%で頭打ちな理由はほぼ全て「トレード数の少なさ」**。breakout_h1 は月5-10本、
   pullback JPY3 は月3本しか撃っていない。WR/RR は既に良い水準で、伸びしろは**N(回数)に集中**。
   目標に必要なのは月15-40本(WR/RRによる) = 現状の **5〜15倍の頻度**。
2. **現実的狙い目ゾーン = WR55-65% × RR1.5-2.0 × 月15-40本**。WR70%超もスキャル(月200本超)も不要。
3. **コストが高頻度を殺す**: RR1.5/WR55%/目標7%で必要N は cost0.0R→30本 / 0.1R→60本 / 0.2R→150本。
   回数増→SL細化→コスト/R増 の悪循環。「タイトSLは全部コスト死」の数学的裏側。
4. **核心: 「実効的に独立なトレードを月25-40本作る」=目標達成の必要十分条件**。これは
   combined_portfolio で実証した「無相関ストリームを増やす」と完全に同じ lever。

### 新ロジックの設計目標(数値で確定)
**WR55-65% / RR1.5-2.0 / 既存basketと無相関 / 月15-40本 / netコスト0.1R以下に収まるSL幅**。
最有力候補 = **クロスペア・スプレッド平均回帰**(方向に賭けない=無相関、ペア追加で独立Nを稼ぐ、
JPYクロスならSL幅も確保しやすい)。次タスク: ①を2-3ペアでプロトタイプ→5.5年net検証→既存との月次相関実測。

成果物: `tools/target_feasibility.py`。

---

## D: 流動性スイープ逆張り (LSR) → エッジ無し確定 (2026-06-14)

「汎用・心理ベースの新観点チャート分析」要望に対し、**Liquidity Sweep Reversal**を実装・検証。
新観点 = 古典パターン(ダブルトップ/三尊/前日高値)を「ストップが溜まった流動性プール」と読み替え、
**パターンの方向に乗らず、そこがスイープ(ストップ狩り)→リクレイム(失敗)する瞬間を逆張り**
(ICT/タートルスープ/Raschke 系)。トラップされたブレイク勢の踏みが燃料、という心理仮説。

### 結果: 全パラメータ空間でエッジ無し (`tools/sweep_lab.py`, H1, 全12ペア net WF)
| variant | N | WR | RR(net) | P1 | P2 | robust |
|---|---|---|---|---|---|---|
| base(k3/lb60/sw3/RR1.5/minSL10) | 17152 | 39.7% | 1.29 | -29.2% | -21.9%/月 | 0/12 |
| RR1.0 | 21638 | **49.5%** | 0.83 | -38% | -29% | 0/12 |
| RR2.0 | 14210 | 32.5% | 1.75 | -29% | -20% | 0/12 |
| long-only | 13085 | 40.6% | 1.28 | -20% | -10% | 0/12 |
| +trend100 with/against | — | 38-40% | 1.28 | 負け | 負け | 0/12 |
| +double(2点タッチ) | 16486 | 39.8% | 1.29 | -26% | -22% | 0/12 |
| minSL20pips | 11681 | 40.0% | 1.36 | -12.6% | -8.0%/月 | 0/12 |

### 結論 (誇張なし)
1. **エントリーに方向の予測力ゼロ。** 決定打は RR 行: どの RR でも **WR×RR ≈ gross ブレイクイーブン**
   (RR1.0=WR49.5%=完全なコインフリップ)。スイープ・リクレイムは予測情報を持たない。
2. **net が負けるのは純粋にコスト。** 頻度が高すぎ(22本/ペア/月)、minSL20 で net 改善するのも
   「コスト死を減らしただけ」で土台はコイントス。**0/12 robust が全変種で一貫**。
3. 心理(トラップ勢の踏み)は実在するが、H1 の機械的シグナルとしては既に織り込み済み/ノイズ過多で抽出不能。
4. これ以上フィルタを足すのは**コインフリップの過剰適合**にしかならない(0/12 + gross≈0 が根拠)→ 深追いしない。

### 含意
古典パターン系・逆張り系の「素のTAシグナル」はFX H1ではほぼコインフリップ(net負け)、という
過去の蓄積(純粋ダウ PF≈1.0、AI フィルタ +0.2%/月止まり)と完全に整合。**エッジは "賢い分類" ではなく
"構造/レジーム" 側にある** (効いたのは long-only がトレンドに乗る breakout だけ)。次は深層学習の検討へ
(ただし「同じ OHLC 特徴に大きいモデル」では届かない見込み。下記 NEXT 参照)。

成果物: `tools/sweep_lab.py`。

---

## E: ポートフォリオ最適化 + エクイティカーブ・オーバーレイ → 月利を2倍化 (2026-06-14, 自律セッション)

LSR がエッジ無しと判明後、「DDを削って再レバ / 無相関の新エッジを足す」の2軸で体系探索
(`tools/portfolio_lab.py`, `tools/regime_overlay.py`)。**月利を 2.1%→4.4%/DD10% に2倍化(信頼値)。**

### ベースラインと「手持ちは実質1エッジ」の確定
- 現行合算(BO0.25%×7 + PB0.5%×3): 年+28.9% / DD9.1% / 月+2.1% / MAR3.2。
- ストリーム単体 MAR: BO_H1_long 4.1 / BO_H4_long 2.8(**H1とcorr0.66=同機序で冗長**) /
  BO_short 0.3(レジームで負け) / PB 0.5(自前利益ほぼ無=DD削減役)。
- → **手持ちは実質「トレンド1エッジ」**。足し算しても√Nが効かない。最適配合でも無オーバーレイは月+2.81%/DD10%が天井。

### ★ 効いた: エクイティカーブ・デリスク (overlay)
BO basket の**実現エクイティが直近K件MAを割ったら次トレードを半分**(ルックアヘッド無=過去の実現益のみ)。
| overlay | 年率 | maxDD | MAR | P1 DD | P2 DD |
|---|---|---|---|---|---|
| baseline | +135% | 32.4% | 4.2 | 32.4 | 27.4 |
| **eqMA K20 m0.5** | +127% | **20.0%** | **6.4** | 20.0 | 16.1 |
- **DD 32→20%(-38%)、リターンほぼ維持、P1/P2両方でDD減、K20/40/80で頑健**(ノブ非依存=過剰適合でない)。
- 口座DD基準版(`DDthr 4% m0.5`, MAR5.4)も有効 → **MT5各EAが口座エクイティをネイティブ参照でき実装容易**。

### ★ 単層オーバーレイ portfolio (信頼できる主結果)
overlay入りBOを再最適化→DD10%スケール: **月+4.40% / 年+67.7% / DD10% / MAR6.8**。
全年プラス(21:+50…26:+11)、P1 DD9.7%/P2 DD10.0%、最悪月-5.1%、マイナス月33%。頑健版(PB保険を残す)でも同等。
**ライブ比2倍、目標7%の63%。**

### ⚠️ 二層オーバーレイ = 過剰適合の疑い (採用しない)
per-stream + 総額の二重がけで紙上 **月+8.32%/DD10%(目標達成!)** だが、K,m感応度スキャンで
**+5.14%〜+10.09%の単調変化**(デリスクを強めるほど数字↑=自然な最適点なし)=典型的overfit tell。
信頼できる核は最保守(K80,m0.7)の**+5.1%**まで。**+8%は in-sample 蜃気楼。$18kマルチンゲールと同型の罠。**

### 結論(誇張なし)と残るギャップ
1. **信頼値 = 月+4.4%/DD10%**(overlay単層+再レバ)。ライブ2倍だが目標6-8%の63%。
2. 二層の上振れ(+5〜8%)は forward 検証なしに信じない・投入しない。
3. **再レバの代償=レジーム集中**: BO H1+H4(corr0.64)に寄せDD10%まで上げる=円高/金安の急反転で同時逝き、
   overlayは半減止まり。最悪月-5.1%×連続でforward DD>10%の現実リスク。2026+11はレジーム減衰の予兆。
4. **durableに6-8%を埋める残路は「真に無相関の+EV新エッジ」=価格外データ(金利差/キャリー)のみ**
   (価格由来は trend edge + overlay + 再レバで搾り尽くした)。クロスセクションmom等は同レジームで相関する見込み。

成果物: `tools/portfolio_lab.py`(ストリーム相関+MAR最大配合+DD10%スケール+overlay+overfit診断)、
`tools/regime_overlay.py`(BO basketのoverlayアブレーション)。

### overlay forward/頑健性検証 → 合格 → MT5 実装 (`tools/overlay_validate.py`, `breakout_h1.mq5`)
MT5投入前の最終チェックを実施し、overlay が本物と確認:
1. **年次一貫**: DD削減が全年(2021-26)で一貫(各年 DD ~33→20, 23→16, … 27→16)。一度の幸運でない。
2. **シャッフル対照(決定打)**: トレード順をシャッフルしDDクラスタを壊すと overlay 改善が **+2.25→-0.18 で消失**。
   = overlay は **本物の連続DD(レジームchop)を捉えている**、機械的デレバの蜃気楼ではない。
   (実データ baseline MAR 4.28 < シャッフル 5.30 = 実データはDDがクラスタ=だから効く、と内部整合)。
3. **ペア単体は4/7改善** = バスケット/口座レベルの効果(個別では弱い)→ 口座エクイティ基準で実装すれば正しく機能。
4. **MT5実装版(暦日MA)** MAR5.3 が proven版(trade-MA 6.5)に肉薄、かつ**スケール不変**(絶対閾値チューニング不要)。

**MT5実装**: `breakout_h1.mq5` に overlay 追加(`InpOverlay/InpOvDays60/InpOvMult0.5`)。口座エクイティを
日次サンプル→過去60日MAを割ったら新規ロット半減。全EAが口座エクイティから同一計算=バスケットレベル。
**段階運用を README に明記**: まず overlay ON のまま risk0.5% で DD を 9.4→6%級に下げ(安全)、フォワードで
DD低下を実機確認してから risk% を上げて再レバ(順序厳守=過剰適合回避)。要 MetaEditor F7 (実機側)。

成果物追加: `tools/overlay_validate.py`、`breakout_h1.mq5`(overlay版)、BREAKOUT_README(overlay節+段階運用)。

### ②金利差/キャリーストリーム — パイプライン完成・データ取得待ち (2026-06-14)
「価格外データで無相関の新エッジ」を狙い、金利差キャリーの検証パイプラインを構築。
**ただしこの実行環境はネット遮断(Bash不通・WebFetchがFRED/Stooqを403/空)** のため Claude はデータ取得不可。
**記憶からの利回り手入力は『未モデルデータで偽エッジ』(time_of_day型の罠)なので厳禁**とし、データ取得だけ
ユーザ手元(ネット可)に委ねる構成にした:
- `tools/fetch_yields.py`: Stooq から各国2年債(無ければ10年債)を `data/yields/<CCY>.csv` に保存。
  ユーザが `! python tools/fetch_yields.py` で実行。
- `tools/carry_lab.py`: 月次・**ポイントインタイム厳守**(月末Mの利回り→翌月M+1リターン)でキャリー信号
  (level / 金利差モメンタム)を検証。go/no-go = **net両期間+ かつ BO無相関**。`--selftest` でコード検証済(完走)。
- **強い事前予想**: キャリー=円キャリー=BOが乗るレジームそのもの→**正相関の懸念**。carry_lab で実測して判定。
  無相関+EVなら tradeable版(SL/サイズ)へ、正相関or±0なら「BOの焼き直し」として不採用。

成果物追加: `tools/fetch_yields.py`, `tools/carry_lab.py`。

### 再レバの尾部/レジーム反転ストレステスト → 段階再レバを結論 (`tools/stress_test.py`)
+4.4%/月の再レバ採用サイズ(BO_H1 0.44%/BO_H4 0.33%/short 0.05%)の尾部リスクを定量化:
- 実測: 月平均+4.44% / maxDD10.1% / 最悪月-5.2% / 最悪2-3月窓-5.9%(散らばれば10%枠に収まる)。
- **悪月クラスタ境界(=レジーム反転の姿)**: 最悪3月が連続なら **DD-13.5%(枠突破)**、6月で-22%、12月で-34.7%。
  → **in-sample DD10% は『benignな2021-26で悪月が散らばる』前提。反転耐性を保証しない。**
- **overlay の反応lag**: BO_H1 で **損失の63%が overlay 作動前(full size)に発生**。
  = overlay は漸進的減衰向けの保護で、**急反転の第一撃は防げない**。
- 年別 canary: 2021+50/22+83/23+25/24+81/25+43/**26+11(最弱=減衰の予兆)**。
- **結論=段階再レバ**: DD枠を埋め切る一括再レバ(0.44/0.33%)は尾部過大。まず採用5ペア risk0.5% のまま
  overlay ON で DD を 9.4→~6% に下げ(安全を銀行)、フォワードでDD低下を実機確認 → その後 0.6-0.7% へ部分的に。

### この自律セッションの到達点(誇張なし)
- **信頼できる前進**: overlay(検証合格・MT5実装済)で、現行と同じ risk0.5% でも **DD 9.4→~6% に低下**(安全の純増)。
  再レバすれば月+4.4%/DD10%も射程だが**段階的に・フォワード確認後**。月+8%(二層overlay)は過剰適合で不採用。
- **未達**: 目標6-8%/月を**安全DDで durable に**は依然未達(信頼値は目標の63%)。残路は金利差/キャリー(pipeline完成・
  データ待ち)か、より高DD許容。価格由来エッジは trend+overlay+再レバで搾り尽くした。

成果物追加: `tools/stress_test.py`。

---

## F: mtf_pullback の net 再検証 + overlay 移植 (2026-06-14)

「MTFの精度向上と汎用化」要望に対し、主要レバーを **net・全12ペア・WF** で測り直した(過去の多くは gross)。
`tools/mtf_net_lab.py`。

### 汎用化は構造的に不可(データが拒否)
- **全12ペア均等は net 負け**(-1〜-14%/年)。viable は実質 **3ペア(EURJPY/EURUSD/USDJPY)** のみ。
- RR↑(1.5→2.5)は viable を減らす(3→1, WR42.9→30.9%)。align/minSL/room でも viable は増えず。
- = **平均回帰エッジは本質的に narrow**(純粋ダウ/LSR がコインフリップだった系譜と一致)。「同一パラメータ・
  選択的デプロイ」が現実。EURUSD は Python net では★だが MT5実機(Axiory)で-3.5%失敗 → 実機を信じ JPY3 維持。

### ★ 真の改善 = overlay(口座レベル=ペア非依存の汎用改善層)
デプロイ JPY3(USDJPY/GBPJPY/EURJPY) net WF:
| | WR | P1/P2 | DD | MAR |
|---|---|---|---|---|
| production | 50.9% | +0.48/+0.71%/月 | 15.9% | 2.4 |
| **production +overlay** | 50.9% | +0.40/+0.79 | **11.2%** | **3.4** |
- **DD -30% / MAR +42% / リターン維持**。breakout と同じ overlay を `mtf_pullback_v2.mq5` に移植(残高基準で一致)。

### 学び: 全12 と 実デプロイで最適 align が逆
- 全12では厳格 align(h4,h1,m15)が WR↑/DD半減で良く見えたが、**JPY3 実デプロイでは頻度減(39→14/年)で
  MAR が逆に低下(3.4→2.3)**。**entry は変えず production(h1,m15)+overlay が最良**。集約指標で判断せず
  実デプロイ単位で確かめる重要性(gross→net, 全12→JPY3 と二度同じ教訓)。

成果物追加: `tools/mtf_net_lab.py`、`mtf_pullback_v2.mq5`(overlay版)、MT5_README(overlay節)。

---

## G: 実Axioryデータ(11年)導入 → breakout真OOS検証・MTF不採用・7ペアへ更新 (2026-06-14)

ユーザがAxiory公開ヒストリカル(M1 OHLCV, 2015-2026, 15ペア)をDL→ `data/axiory/` 展開(gitignore)。
**実ブローカー(=デプロイ先)のM1・11年・2015-20が真の未知データ** → 本物のWF-OOSが可能に。
ローダ `tools/axiory_data.py`(M1→M5/M15/H1/H4集計+npz, bo_fastと同I/F)。詳細 [[axiory-real-data]]。

### 1. ★ breakout は実データ・真OOSで生存(最強の検証)
`tools/axiory_validate.py`: breakout long-only を実Axiory 11年、OOS=2015-20/IS=2021-26 で検証。
- **デプロイ5ペア全てOOS+IS両期間+**。XAUUSDが両期間最強。「2021-22円安の産物では」の懸念に、別レジームの
  2015-20でも黒字という答え。**ただしレジーム依存(IS≈OOSの約2.4倍)** = 強トレンド期に大・平時に小。
- 全15ペアスキャン: **robust(両期間+)=7ペア**(現5 + **CHFJPY/NZDJPY**)。USD系/EURGBPは2015-20○→2021-26で死亡
  = **稼ぐペアはレジームで移動する**(トレンド機序は普遍だがペア選択は固定し過ぎない)。

### 2. ★ 採用ペアを 5→7 に更新(CHFJPY/NZDJPY追加)
`tools/axiory_basket.py`: BO5 vs BO7 を overlay入り・DD10%スケールで比較。
- **効率(sumR/DD) 18.9→23.7、OOS +0.84→+1.24%/月、IS +2.01→+2.32%/月**。明確に改善 → **7ペア採用**。
- BREAKOUT_README/README/CLAUDE 更新。EAコードは _Symbol 単位(ペア非ハードコード)なのでチャート追加のみ。

### 3. ★ MTF(mtf_pullback/JPY3) を不採用に(実データが覆した)
`tools/axiory_mtf.py`: MTFをAxiory M5で回し breakout との合算を実測。
- **MTF JPY3 は OOS・IS 両方で net 負け**(FTO録音では微益)。実コストで薄い平均回帰エッジが消滅([[sweep-reversal-no-edge]]
  系)。breakout相関 +0.12、合算効率 18.5→13.5 **悪化** → **併用の価値なし=不採用**。
- README/MT5_README/CLAUDE から運用推奨を削除し**不採用ログ化**(コードは残置)。FTO時代の「両方が正解」は
  **FTOデータの産物**で実データが覆した。**無相関ヘッジは現状不在**、候補は価格外データ(金利差)のみ。

### 4. エクイティ曲線の直視で誤りを訂正(重要)
MT5単体テスト(USDJPY 2016-26)の**推移CSV**を精査: ①2016-2020はトレード皆無(データが2020から)=「OOS」評価は
誤りだった(訂正)。②+28%は**2021-22に集中、2023-26は横ばい**=レジーム依存が実曲線で可視化。
→ **サマリー(PF/DD)だけで判断せず曲線を見る**。実Axiory導入の動機。

### この日の教訓
- **実ブローカー・実データ・真OOSが唯一の最終判定。** breakoutは生存(本物)、MTFは脱落、ペアは7へ。
- **良いデータは“勝てる保証”でなく“正しい判定”**。FTO由来の結論(JPY3併用)が実データで複数覆った。

成果物: `tools/axiory_data.py` / `axiory_validate.py` / `axiory_mtf.py` / `axiory_basket.py`。

---

## H: ★ ショート・スリーブ発見 — long+short で risk調整後リターン約2倍 (2026-06-14)

MTFが実データで脱落し「無相関ヘッジ不在」となった穴を、**価格データ内**で探索(`tools/axiory_direction.py`)。
**第一候補=ショート側が当たり。** long-only は上昇相場が要る → ショートは下落/リスクオフで稼ぐ=無相関。

### 発見
- robust-7 の Donchian **short-only: OOS +4.12%/月 / IS +0.82%/月(両期間net+)**。
  **long basket と月次相関 -0.18(負=真のヘッジ)**。ショートISも+0.82=上昇相場でも僅か+で出血しない。
- MA交差は Donchian-long と相関+0.78=冗長(不採用)。
- per-pair short: USDJPY/EURUSD/USDCHF/GBPJPY/EURJPY のショートが両期間+(longが死ぬIS局面で稼ぐペアあり)。

### ★ long + α·short スリーブ最適化(`tools/axiory_longshort.py`, overlay, DD10%スケール)
| α(short比) | OOS%/月 | IS%/月 | 効率 |
|---|---|---|---|
| 0.0(long-only) | +1.24 | +2.32 | 23.7 |
| **0.4(最適)** | **+2.64** | **+3.72** | **42.7** |
- **OOS・IS両方で約2倍**。α=0.2-0.5で効率39-43=**ノブ非依存(過剰適合でない)**。両期間でα≈0.4が最適=WF頑健。
- 機序は同一(Donchianブレイクの逆方向)=新規の過剰適合機序ではない。**IS +3.72%/月は6-8%目標の下端が視野。**

### 正直な但し書き
- まだPythonバックテスト(実M1・close約定)。**MT5実機未検証。** ショートはギャップ/スクイーズの裾リスクあり。
- DD10%スケールは in-sample DD前提。新レジームでのショートDD挙動は不確実。
- それでも**価格内で見つかった初の本物の無相関補完**。FTOで全部上昇だったため見逃していた(2015-20のリスクオフが鍵)。

### 実装/次
- `breakout_h1.mq5` に `InpShortOnly`(既定false=従来不変)追加 → **ショート専用インスタンスを別magic・0.4×riskで**
  7チャートに重ねればスリーブ運用可(ヘッジ口座前提。ネッティング口座はlong/shortが相殺するので要注意)。
- 次: MT5実機でlong+shortを検証 / α・ショートペア選択のWF頑健性をさらに詰める。

成果物: `tools/axiory_direction.py` / `axiory_longshort.py`。

---

## 関連: 次にやること

[NEXT_TASKS.md](./NEXT_TASKS.md) 参照。
