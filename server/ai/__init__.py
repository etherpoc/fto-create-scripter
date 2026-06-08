"""AI 判断レイヤ。

- base.py        : AIModel プロトコル (差し替え可能な抽象)
- features.py    : 戦略状態 → 特徴量 dict (FeatureBuilder)
- stub.py        : テスト用 (always_enter / random / rule-based) のモデル
- ollama_client.py : Ollama (ローカル LLM) を叩くアダプタ
- data_collector.py: 判断履歴を JSONL に蓄積 (将来の学習データ)
"""
