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

## 関連: 次にやること

[NEXT_TASKS.md](./NEXT_TASKS.md) 参照。
