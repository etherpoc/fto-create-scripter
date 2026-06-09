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
