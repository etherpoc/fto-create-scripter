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

## 関連: 次にやること

[NEXT_TASKS.md](./NEXT_TASKS.md) 参照。
