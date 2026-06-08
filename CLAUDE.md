# CLAUDE.md — FTO Strategy Lab 運用ルール

このリポジトリは「自然言語で書かれた FX 売買ロジック → ローカル検証済み Python コード →
**ローカル WS サーバが配信して FTO 上の汎用 EA が実行**する」二段構えのワークスペース。

EA は 1 つだけ (`strategies/thin_client/fto_strategy.js`)。判断ロジックはすべて
`server/` 配下の Python で動かす。新しいロジックを足すたびに EA を再アップロード
する必要はなく、**サーバの Python を直してプロセス再起動するだけ** で反映される。

---

## アーキテクチャ

```
┌───────────────────────────────┐       wss://localhost:8443/ws/strategy
│ FTO Robot (.js, thin client)  │ ◀────────────────────────────────────▶
│  - OnTick → raw OHLC を送信   │       ┌─────────────────────────────┐
│  - 受信 commands を発注 / 描画 │       │ Local Python Server          │
│  - 戦略ロジックは持たない      │       │  - WS endpoint              │
└───────────────────────────────┘       │  - Strategy registry        │
                                        │  - 既存 Python 戦略を再利用 │
                                        │  - 将来: AI 判断レイヤ      │
                                        └─────────────────────────────┘
                                                    ↑
                                                    │
                                        ┌─────────────────────────────┐
                                        │ ローカル backtest            │
                                        │ tools/run_backtest.py       │
                                        │ (同じ Python ロジックを使用) │
                                        └─────────────────────────────┘
```

**同じ Python ロジックがローカル backtest と FTO 本番の両方で走る。**

---

## 設計の絶対原則（破ってはいけない）

1. **EA は薄く保つ。** `strategies/thin_client/fto_strategy.js` には戦略ロジックを
   書かない。データ取得・発注・描画指示の実行だけ。判断はサーバ側 Python に任せる。
2. **ロジックは Python で書く。** `src/core/` は標準ライブラリのみで動く前提を維持。
   `Strategy.on_bar(ctx)` インタフェースを使い、ctx 経由でデータ参照・発注を行う。
   このインタフェースのおかげでローカル backtest engine と RemoteContext (サーバ)
   の両方が同じコードを動かせる。
3. **FTO 固有 API 名は推測しない。** EA 側で必要になった場合、`docs/fto_api_reference.md`
   を唯一の正とする。未記入は `// TODO(FTO_API: ...)` で残す。
4. **FTO に貼る前にローカル検証する。** 戦略は確定足ベースで回し、未確定足を参照する
   先読み (ルックアヘッド) を作らない。
5. **EA を触らない運用を維持する。** ロジック修正・パラメータ追加・AI 統合の作業は
   すべてサーバ側で完結させる。EA を再アップロードしなければならない事態は最小化。

---

## 新しいロジックを追加するときの手順

### 1. 仕様化
`docs/strategy_spec_template.md` をベースに `strategies/<name>/spec.md` を作る。
曖昧点はユーザに必ず質問する (仮定で埋めない)。

### 2. core 実装 (Python)
`strategies/<name>/strategy.py` を以下の規約で作る:
- `@dataclass Params(StrategyParams)` で戦略パラメータを定義
- `class XxxStrategy` を継承して `on_bar(self, ctx)` を実装
- データ参照・ポジション照会・発注は **すべて `ctx` 経由**
- 指標は `src/core/indicators.py` を使用
- FTO 固有 API 名を一切書かない

### 3. ローカル検証
`python tools/run_backtest.py <name>` を実行。
0 トレードや異常値ならロジックを疑いステップ 2 に戻る。

### 4. サーバへ登録
`server/deciders/<name>.py` を作って `@register("<name>")` で登録。
`server/deciders/__init__.py` に `from . import <name>` を 1 行追加。
詳細は `server/deciders/README.md` 参照。

### 5. 動作確認
- サーバ再起動 (`--reload` なら自動)
- `curl https://localhost:8443/strategies` でロジック名一覧を確認
- FTO で `Thin Client (server-driven)` EA を起動し、UI の `Strategy Name` を
  追加したロジック名に変更してバックテスト
- DevTools の `[srv] ...` ログとサーバログ両方で動作確認

---

## 既存戦略の現状

### `zigzag_line_break`
- M15 + H1/H4 MTF の ZigZag ベース戦略 (spec.md 参照)
- ローカル backtest 検証済み
- サーバ登録済み (`server/deciders/zigzag_line_break.py`)
- FTO で `Strategy Name = "zigzag_line_break"` で動作確認済

### `example_sma_cross`
- SMA クロスのサンプル戦略 (ローカル backtest デモ用)
- サーバ未登録 (デモなので)

---

## MTF (マルチタイムフレーム) の注意

- 上位足は **確定足のみ** を下位足インデックスに展開して `ctx` に渡す
- 未確定の上位足を参照してはならない (ルックアヘッドになる)
- EA → サーバの tick メッセージは「直近確定 M15 / H1 / H4」を毎ティック送る
- サーバ側で `last_h1_time` / `last_h4_time` で同一バー重複を dedup

## ルックアヘッド回避

- `pivot_high` / `pivot_low` は中心から `right` 本進んだ index に値を入れる
- `crossover` / `crossunder` は閉じた足同士の比較のみ
- 戦略は `on_bar` を「確定足ごとに 1 回」呼ばれる前提で書く

## FTO 側で覚えておく癖 (EA を直接いじる必要が出たとき)

`docs/fto_api_reference.md` 参照。要点だけ:
- アップロードは `.js` のみ
- 必ず `export default class extends StrategyImplementation`
- SDK import は使えないので必要な部分はインラインで書く
- `Time(0)` / `High(0)` 等の index 0 は「現在進行中の未確定バー」。確定足は index 1 以上
- メソッド名は PascalCase が正 (`GetActiveOrderCount`, `PlaceOrder` etc.)
- `setStrategyShortName` / `createTOptValue_*` だけは camelCase
- Init() の段階で `Symbol()` が null を返すことがあるので、symbol 依存処理は OnTick へ遅延

これらの罠はすべて `strategies/thin_client/fto_strategy.js` 内で処理済み。
通常のロジック開発で再度踏むことはない。

---

## ドキュメント運用ルール (PROGRESS.md / NEXT_TASKS.md など)

このリポジトリには「人間用ナレッジ」のドキュメントが `docs/` 配下にある:

- **`docs/PROGRESS.md`** — これまでの進捗ログ。アーキテクチャ確立、AI 戦略 v1〜vN
  の進化、各段階で得られた知見、踏んだ落とし穴を時系列で残す。
  「なぜそうしたか」「何を試して効いた / 効かなかったか」を記録。
- **`docs/NEXT_TASKS.md`** — 次にやることリスト (優先度別 P0/P1/P2/P3)。
  完了したタスクは PROGRESS.md に要約を移すか ☑ で残す。

### Claude が守る運用ルール

1. **新しいロジック / 設定 / アプローチを試したら、PROGRESS.md に要約を追記する。**
   コミットメッセージや会話で「効いた / 効かなかった」を述べただけで終わらせず、
   未来の自分や別セッションが参照できる形で残す。
2. **新しいタスクが発生したら NEXT_TASKS.md に追記する。** 優先度を P0-P3 で付ける。
   「あとで考える」系のアイデアも P2/P3 に残しておく (忘却防止)。
3. **完了タスクは NEXT_TASKS.md から PROGRESS.md に移すか、☑ チェックで残す。**
4. **新しい運用ドキュメント (`docs/IDEAS.md`、`docs/INCIDENT_LOG.md` 等) を増やしたら、
   この CLAUDE.md にも「何のためのファイルか」を 1 行で追記する。**
   未来のセッションが docs/ を見たときに目的を理解できるように。
5. **PROGRESS.md / NEXT_TASKS.md はコミット粒度の節目で更新する** (大きな変更入れた直後、
   実験結果が出た直後など)。毎コミットではなく、メリハリつけて。
