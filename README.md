# FTO Strategy Lab

自然言語で書かれた FX 売買ロジックを Python で実装し、**ローカル WS サーバ経由で
Forex Tester Online (FTO) のロボットへ配信して実行**するワークスペース。

---

## 🏆 完成モデル: mtf_pullback v2 — MT5 スタンドアロン EA (JPY3 basket)

研究の到達点。M5 + MTF(H1/M15)の ZigZag/ダウ理論ベースの押し目戦略を **MetaTrader5 の
MQL5 EA に移植**し、**実機(Axiory)+実スプレッド+往復コミッションで net 検証して確定**した運用モデル。

- 本体: [`strategies/standalone/mtf_pullback_v2.mq5`](strategies/standalone/mtf_pullback_v2.mq5)
- **運用ドキュメント(必読)**: [`strategies/standalone/MT5_README.md`](strategies/standalone/MT5_README.md)

### 実機検証で確定した結論

**勝つのは JPY ペアのみ**(値幅が大きくコスト耐性が高い)。非JPY(EURUSD 等タイトSL)はコストで負ける。

| ペア | 実機 net/2年 | WR | PF | DD |
|---|---|---|---|---|
| USDJPY | +9.8% | 59% | 1.92 | 3.3% |
| GBPJPY | +9.4% | 56% | 1.65 | 3.3% |
| EURJPY | +3.5% | 47% | 1.20 | 7.6% |

→ **JPY3 basket (USDJPY/GBPJPY/EURJPY)** を運用設定として確定。

### セットアップ方法 (MT5)

1. **コンパイル**: MetaEditor で `mtf_pullback_v2.mq5` を開き F7(0 errors/0 warnings)。
2. **各ペアの M5 チャートに EA を 1 つずつ適用**(USDJPY / GBPJPY / EURJPY の 3 枚)。
3. **パラメータ**(テスター/EA 入力欄は `//` コメントのラベルで表示される):

   | ラベル | 値 |
   |---|---|
   | `Risk % per trade (1=1%)` | **0.5** ← 3枚合成 DD<10% に収める唯一の値 |
   | `絶対最小SL pips (...)` | **20** ← タイトSL=コスト負け除外(実機必須) |
   | `Block hour start / end UTC` | **6 / 10** |
   | `Server->UTC offset hours` | **ブローカー依存**(Axiory 夏 −3 / 冬 −2) |
   | `TP RR` / `Align` / `room_R max` | 1.5 / 1 / 2.0 |

4. **起動後**: Experts ログの `[mtfpb] ENTRY ... risk$=...` で **risk$ が残高の約0.5%** か必ず目視確認。

### 期待値と位置づけ(誇張なし)

- **net 年 +4〜6% / 合成DD 約8% / 日次最悪 −1%台**。低リスク・プロップ適合の堅実戦略。
- ★ JPY3 は**対円相関が高く同時逆行で合成DDが重なる**ため、per-pair risk=0.5% が必須。
- これは 6-8%/月 の目標には届かない(約1/10)。**「負けない堅実な土台」**としての位置づけ。
- 検証の全過程(netコスト評価・ペア選定・サイジング)は [`docs/IMPROVEMENT_RESULTS.md`](docs/IMPROVEMENT_RESULTS.md)。

> ⚠️ **教訓**: Python バックテストは長らく **gross(スプレッド/コミッション抜き)** で、実コストを入れると
> 全12ペア分散は net 負け、JPY3 のみ生存と判明した。**実機・実コストで検証するまで「勝てる」と言わない**。

---

## 2 つの動作モード (FTO thin-client 構成)

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
