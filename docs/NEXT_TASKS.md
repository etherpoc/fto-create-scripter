# 次にやること (Next Tasks)

優先度順に並べる。完了したら ☑ にしてコミット、もしくは PROGRESS.md に移動。
新タスク発生時はここに追記する。詳細は PROGRESS.md と CLAUDE.md を参照。

---

## 🔴 P0 — 直近

### v5 の効果検証 (完了 ☑)
- [x] 8 ペア並列で 6 か月相当バックテスト (118 outcomes)
- [x] 結果: WR 43.2% (+9.2pp vs BL)、PnL +170.46 (v4 の約 2 倍)
- [x] XAUUSD が大きく改善 (WR 47% → 61%、pnl +93 → +176)
- [x] PROGRESS.md 更新済み

### v5 の寄与アブレーション (どの変更が一番効いたか)
- [ ] tp_rr 1.5 だけにして他 OFF → 効果計測
- [ ] trailing だけにして他 OFF → 効果計測
- [ ] 時間帯フィルタだけ ON → 効果計測
- [ ] conf サイジングだけ ON → 効果計測
- [ ] 目的: v6 で「効くものだけ残す」判断材料を得る

### v6: AI conf x WR 逆相関を受けた contrarian sizing 検証
- v5 集計で「conf >= 0.85 群 WR 22% (vs conf < 0.85 群 WR 45%)」が判明
- 高信頼度判断ほど負ける = overconfidence bias
- 対応: `ai_conf_size_mult: 1.5 → 0.5` で逆方向のサイジングに
- [x] v6 = v5 + ai_conf_size_mult=0.5 でバックテスト (data/ai_v6_decisions/)
- [x] 結果: WR は変わらず (sizing は意思決定に影響なし)

---

## 🔴 P0 (2026-06-10 更新)

### M5 + ハイブリッド portfolio の精緻化
v8_38 + M5 検証で「USD系は M5、JPY 系は M15 が良い」と判明。
- [ ] M5 + gemma4 12 ペア完走 (実行中)
- [ ] 完了次第、ハイブリッド (M5 一部 + M15 一部) の精密な portfolio 計算
- [ ] 仮想ハイブリッドで月利 1-2% / 月の出口検討

### Out-of-sample 検証 (2016-2020) ← 最重要
v8_38 や M5 ハイブリッドの「真の robustness」を測定。
- [ ] FTO で 2016-01 〜 2020-12 を record_only で M15 + M5 録音 (5 年分)
  - 想定: M15 録音 6h、M5 録音 12h
- [ ] data/recorded_ticks_oos_m15 / m5 に保存
- [ ] v8_38, m5_38, ハイブリッド全部 replay → in-sample との差分計測
- [ ] 月利が +0.2% 以上維持できれば実運用候補

### ZigZag M5 パラメータスケーリング
M5 で z1_depth=25 は 2.08h で短すぎる。z1_depth=75 (= M15 と同じ 6.25h コンテキスト) で再検証。
- [ ] env で z1_depth 等を override 可能化 (現在は固定)
- [ ] m5_38_scaled (z1_depth=75, atr_period=42, lookback=150) で replay
- [ ] M5 + scale が JPY クロスの悪化を救えるか確認

---

## 🟡 P1

### 月利 6-8% への構造的アプローチ (1% リスク制約下)
現アーキテクチャの天井は月利 ~1%。ゴール到達には根本変更必要:

- [ ] 戦略本体の追加: ブレイクアウトロジックの併用 (押し目と排他ではなく重ね合わせ)
- [ ] M1 timeframe 検討 (件数 5x 想定、ノイズリスク大)
- [ ] 別ロジックの実装: トレンドフォロー + レンジ反発のマルチ戦略
- [ ] Fine-tuning: 5.5 年データの全 features → outcome で LoRA 学習
  - データ: 8000+ outcomes 利用可能
  - 想定効果: 不明、要実験

### Pip サイズ / spread コスト の現実反映
現状の R 計算は spread を考慮していない。
- [ ] outcome 記録時に spread を引いた pnl を別途保持
- [ ] 主要ペアの実 spread (M5/M15 平均) を調査
- [ ] spread-adjusted R で再評価

---

## 🔴 P0 (2026-06-10 5.5 年検証後の優先度更新)

### ゴール: 月利 6-8% 達成
5.5 年データで判明したのは「現ロジック延長線では月利 0.3-1% が天井」。
ゴールに届くには根本的な改善が必要。

### A. M5 timeframe 化 (期待度: 高、リスク: 中)
- [ ] M5 で record_only モードで 5.5 年データ収集 (今の M15 と並列で良い)
- [ ] ZigZag params を M5 スケールに調整 (z1_depth, atr_period)
- [ ] M5 + v7d/gemma4 設定で replay
- [ ] 期待: トレード数 3x で月利 ~3% になるか確認
- [ ] 失敗パターン: ノイズ増で WR が 30% 切ったら却下

### B. Fine-tuning 準備 (期待度: 中、ROI: 不明)
- [ ] 5.5 年データの全 outcomes を JSONL 化 (= `tools/build_training_data.py`)
- [ ] decision × outcome を join、prompt/completion ペア形式に
- [ ] LoRA 用に量子化前のベースモデル (gemma3:4b or gemma4:e4b) を取得
- [ ] Ollama では fine-tune できないので、unsloth or LLaMA-Factory で学習
- [ ] 学習後モデルを Ollama に import (`ollama create`)
- [ ] v8 として replay で評価

### C. 別期間 (2016-2020) で out-of-sample 検証 (= overfitting bias 測定)
- [ ] FTO で 2016-01 〜 2020-12 を record_only で収集
- [ ] data/recorded_ticks_oos に保存
- [ ] v7d/gemma4 (本検証で best) で replay
- [ ] in-sample (2021-26) vs out-of-sample (2016-20) で月利を比較
- [ ] 差が大きければ overfitting、小さければ robust
- [ ] **これをやらないと「月利 X% 達成」と主張できない**

### D. 戦略本体の見直し (期待度: 不明、規模: 大)
- ZigZag 単体ではなく、別ロジック追加: ブレイクアウト、レンジ反発、トレンドフォロー
- 複数 sub-strategy のアンサンブル
- 現在 1% リスク → トレードあたりの期待値構造を変える

### E. レバレッジ増 (期待度: 線形、リスク: 線形)
- リスク 1% → 2% (or 3%) で月利が線形に倍増
- ただし最大 DD も比例増。月利 6% 目標達成のため一時的に 2% に上げる検討
- スイッチは `risk_pct` パラメータ 1 つ

---

## 5.5 年検証で得られた学び (重要)

1. **「大きいモデル = 良い」は技術分析タスクでは不成立**
   - qwen2.5:7b (7B) は gemma4:e4b (4B 相当) より明確に劣る pair が多数
   - モデルサイズより prompt 設計と feature 表現の方が重要

2. **AI フィルタの効果は限定的 (+12 R / 5.5 年 = 月利 +0.2%)**
   - 「悪い負けを減らす」が主な貢献
   - 「勝ちトレードを増やす」効果は薄い
   - ゴール 月利 6-8% に届くためには別の lever が必要

3. **戦略本体のシグナル品質 (Baseline R) が天井を決める**
   - JPY クロス / XAUUSD = +EV、USD クロス = ほぼ -EV
   - AI は USD クロスを -EV → ±0 にする程度
   - 大きく稼ぐためにはシグナル自体の改善が必要

4. **in-sample 最適化に注意**
   - 「best variant per pair」は backtest 結果からの後付け選択
   - out-of-sample で同パフォーマンス保証されない
   - 必ず別期間で検証してから運用判断する

### 確認用コマンド
```bash
python tools/compare_baseline.py --ai data/ai_v5_decisions --baseline data/baseline_decisions
python tools/compare_baseline.py --ai data/ai_v5_decisions --baseline data/ai_v4_decisions
```

---

## 🟡 P1 — 短期 (v5 検証後)

### USDCAD 問題への対応
- v4 時点で USDCAD は 0W/5L (AI/baseline どちらも弱い)
- 原因: 原油・米経済指標連動でテクニカル単独で読みにくい
- 候補: USDCAD だけバックテスト対象から外す or 別ロジック適用
- 検討事項: 「ペア別 enable/disable」を Params に追加するか

### USDJPY のチューニング
- v4 で WR 25%、Baseline 27% でほぼ同等 = AI が貢献できてない
- JPY ペア特有の動き (Tokyo セッション集中、介入リスク) を prompt に追加検討
- 別 system prompt を用意して「JPY ペアモード」にする選択肢も

### Fine-tuning の準備
- v1-v5 で蓄積したラベル付きデータ (decision + outcome) を集計
- 量: 現状 350+ outcomes (5 版合計)、200-500 件で LoRA 試行可能
- ロードマップ:
  1. `tools/build_training_data.py` を CSV/JSONL で出力
  2. データを「prompt-completion」ペア形式に変換
  3. ローカルで LoRA 学習 (Ollama 単体では fine-tune できないので別途環境構築)
  4. 学習後モデルを Ollama に import (`ollama create`)
  5. v6 として A/B 比較

---

## 🟢 P2 — 中期 (将来検討)

### 5 分足 (M5) ベースへの移行検討
- 現状 M15 で 6 か月 6%、年率 12% 相当
- M5 化のメリット: トレード回数 3 倍
- M5 化のデメリット:
  - Spread が利益に占める割合増 (4% → 13%)
  - 実効 RR 低下 (1.88 → 1.65)
  - ノイズ増で WR 低下リスク
- 数学的試算: WR 維持なら期待値 +57%、WR 4pp 低下なら期待値 -90%
- 判断: **v5 の結果を見てから**。v5 で十分なら M5 不要、まだ伸び代欲しいなら検証

### ペア追加
- 現状 8 ペア → 12 ペアに拡張余地
- 候補: Silver、EURJPY、GBPJPY、SPX500、US100 (FTO の対応銘柄に依存)
- トレード回数を線形に増やせる、リスク低い

### A2000 Ada 16GB PC の活用
- 環境構築は後回し方針 (ユーザー判断)
- 検討時の選択肢:
  1. メイン PC のままサブで別バックテスト並列 (データ収集 2 倍速)
  2. AI ホストを移して qwen2.5:14b 等の大きいモデル試用 (判断質向上狙い)
  3. アンサンブル (小モデル + 大モデルで 2-of-2 合意のときのみ enter)
- 必要時に server/ai/ollama_client.py の `base_url` を env で切り替え可能化 (1 行追加で済む)

---

## 🔵 P3 — 改善アイデア (検証必要)

### Trailing パラメータの最適化
- 現状: `trail_activate_R=1.0, trail_stop_R=0.0` (+1R 達成後 BE に逃げる)
- 案: `activate_R=1.5, stop_R=0.5` (+1.5R 達成後 +0.5R で逃げる = 利益を残す)
- A/B テスト推奨

### conf サイジングの段階追加
- 現状: conf >= 0.85 で lot ×1.5、それ以外 ×1.0
- 案: 0.7-0.85 で ×0.7、0.85-0.95 で ×1.5、0.95+ で ×2.0
- リスクの段階化

### 既存戦略の見直し
- ZigZag のパラメータ (z1_depth=25, z2_depth=5) は M15 用にやや経験則
- グリッドサーチで最適 z1_depth / sl_buffer_k を探る価値あり
- ただし overfit 警戒

---

## 既知の制約 (workaround 済 / 対処不能)

- FTO は SL/TP の Modify 関数なし → trailing は CloseOrder で疑似実装
- FTO は Partial Close 関数なし → 2 オーダー分割で代替可能 (未実装)
- ブラウザのタブ throttling → Draw OFF + 再接続前提 ([fto-multitab-throttling])
- Ollama の同時並列リクエストは内部キューイング → 60 秒 timeout だと足りない場合あり (現状 120 秒に拡張済)

---

## このファイルの運用

- タスク追加時はこのファイルに追記
- 完了タスクは PROGRESS.md に短く要約して移動 (もしくは ☑ で残す)
- 長期化したアイデアは P3 → 別ドキュメント (e.g. `docs/IDEAS.md`) への分離を検討
- このファイルを CI 等で参照しない (人間用ドキュメント)
