# FTO Strategy Lab

自然言語で書かれた FX 売買ロジックを Python で実装し、**ローカル WS サーバ経由で
Forex Tester Online (FTO) のロボットへ配信して実行**するワークスペース。

## 2 つの動作モード

### A. ローカル backtest (FTO なしで完結)

```
python tools/run_backtest.py example_sma_cross
```

`strategies/<name>/strategy.py` をそのまま `src/backtest/engine.py` で回す。
標準ライブラリのみで動く。

### B. FTO 本番モード (サーバ + 薄い EA)

```
                   wss://localhost:8443/ws/strategy
┌──────────────┐ ◀───────────────────────▶ ┌────────────────────┐
│ FTO Robot    │                            │ Python Server      │
│ thin client  │ ──── raw OHLC ────────▶   │ ・既存戦略実行     │
│ (.js)        │ ◀──── commands ──────     │ ・AI 統合 (将来)   │
└──────────────┘                            └────────────────────┘
```

- EA は 1 つだけ (`strategies/thin_client/fto_strategy.js`)。汎用、再アップ不要。
- 判断ロジックはサーバ Python が実行。**ローカル backtest と同じコード**。
- UI の "Strategy Name" でサーバ側のどのロジックを呼ぶか指定 (`zigzag_line_break` 等)。

## ディレクトリ構成

```
fto-create-scripter/
├── CLAUDE.md                              # Claude 向け運用ルール
├── README.md
├── requirements.txt
├── docs/
│   ├── fto_api_reference.md               # FTO API リファレンス
│   └── strategy_spec_template.md          # 戦略仕様テンプレ
│
├── src/                                   # 共通フレームワーク (標準ライブラリのみ)
│   ├── core/
│   │   ├── strategy_base.py               # Bar / Context / Strategy / StrategyParams
│   │   └── indicators.py                  # SMA/EMA/ATR/RSI/Pivot/Crossover/ZigZag
│   └── backtest/
│       └── engine.py                      # ローカル検証エンジン
│
├── strategies/                            # 戦略ロジック (Python)
│   ├── thin_client/
│   │   └── fto_strategy.js                # ★ FTO 用の唯一の EA
│   ├── zigzag_line_break/
│   │   ├── spec.md
│   │   └── strategy.py                    # サーバとローカル両方で動く
│   └── example_sma_cross/
│       ├── spec.md
│       └── strategy.py                    # ローカル backtest デモ用
│
├── server/                                # ローカル WS サーバ (FastAPI)
│   ├── main.py                            # /ping, /strategies, /ws/strategy
│   ├── session.py                         # セッション状態
│   ├── remote_context.py                  # Context I/F のサーバ実装
│   ├── make_cert.py                       # 自己署名証明書ジェネレータ
│   ├── certs/                             # localhost.pem / localhost-key.pem
│   ├── deciders/                          # ★ 戦略レジストリ
│   │   ├── README.md                      # 新ロジック追加手順
│   │   ├── registry.py
│   │   ├── __init__.py
│   │   └── zigzag_line_break.py
│   └── README.md                          # サーバ起動手順
│
├── tools/
│   ├── make_sample_data.py                # ダミー OHLC 生成
│   └── run_backtest.py                    # ローカル検証エントリ
│
└── data/                                  # OHLC CSV 置き場
```

## セットアップ

### ローカル backtest だけしたい場合

Python 3.10+ があれば動く。追加依存なし。

```
python tools/run_backtest.py example_sma_cross
```

### FTO 本番モード

```
# サーバ依存
python -m pip install -r server/requirements.txt
python -m pip install cryptography

# 自己署名証明書を生成 (一度だけ)
python server/make_cert.py

# サーバ起動 (HTTPS)
python -m uvicorn server.main:app --host 0.0.0.0 --port 8443 \
    --ssl-keyfile=server/certs/localhost-key.pem \
    --ssl-certfile=server/certs/localhost.pem \
    --reload
```

ブラウザで `https://localhost:8443/ping` にアクセスし、自己署名証明書を「アクセスする」
で許可 (1 回だけ)。FTO 側で `strategies/thin_client/fto_strategy.js` をアップロード、
UI の `Strategy Name` で動かしたいロジック名 (例: `zigzag_line_break`) を指定。

詳細手順は `server/README.md`。

## 新しいロジックを追加するとき

1. `strategies/<name>/spec.md` を書く (`docs/strategy_spec_template.md` をコピー)
2. `strategies/<name>/strategy.py` を実装 (`src/core/strategy_base.Strategy` を継承)
3. `python tools/run_backtest.py <name>` でローカル検証
4. `server/deciders/<name>.py` を作成して `@register("<name>")` で登録
5. `server/deciders/__init__.py` に import を追加
6. サーバ再起動 → FTO で `Strategy Name = "<name>"` に変更してテスト

EA (`strategies/thin_client/fto_strategy.js`) は触らない。詳細は
`server/deciders/README.md`。

## 設計の肝

1. **ロジックと FTO API を分離する。** 売買判断は Python 側に集約。EA は実行係。
2. **同じ Python コードがローカル backtest と FTO 本番で走る。** Context 抽象のおかげ。
3. **新ロジック追加で EA を触らない。** サーバの Python と registry に追加するだけ。

## なぜこの構成か

- FTO のような外部プラットフォームは仕様変更がありうる。ロジックを Python に持って
  おけば、プラットフォーム差し替えが容易。
- Python なら sklearn / PyTorch / transformers を直接 import できる。AI 統合が自然。
- ローカル backtest と本番が同じコードなので、「backtest で勝っていたのに本番は違う」
  という再現性問題が起きにくい。
- EA が薄いので、FTO の API 仕様変更の影響を最小化できる。
