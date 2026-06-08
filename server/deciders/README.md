# server/deciders/ — 戦略ロジックの置き場

ここに Python モジュールを 1 つ作るたびに、FTO Thin Client EA から名前で呼び出せる
ロジックが 1 つ増える。**EA は触らない。サーバ再起動だけで反映**。

## 新しいロジックを追加する手順

### 1. 戦略本体を作る（既存パターン: `src/core/` のフレームワークを使う）

たとえば `strategies/my_new_strategy/strategy.py`:

```python
from dataclasses import dataclass
from src.core.strategy_base import Context, Strategy, StrategyParams

@dataclass
class Params(StrategyParams):
    foo: int = 10
    bar: float = 1.5

class MyNewStrategy(Strategy):
    def __init__(self, params: Params) -> None:
        super().__init__(params)
        self.p = params
        # 状態 (ZigZag トラッカ等) の初期化

    def on_bar(self, ctx: Context) -> None:
        # ctx.bars(n), ctx.bars_mtf(period_sec, n) でデータ取得
        # ctx.buy / ctx.sell / ctx.close / ctx.log で発注 (実行はサーバが buffer)
        ...
```

### 2. server/deciders/ にレジストリ用アダプタを作る

`server/deciders/my_new_strategy.py`:

```python
from server.deciders.registry import register
from strategies.my_new_strategy.strategy import MyNewStrategy, Params

MyNewStrategy.PARAMS_CLS = Params
register("my_new_strategy")(MyNewStrategy)
```

### 3. server/deciders/__init__.py に import を追加

```python
from . import zigzag_line_break  # noqa: F401
from . import my_new_strategy    # noqa: F401   ← この行を追加
```

### 4. サーバ再起動 (uvicorn --reload なら自動)

### 5. FTO 側で Thin Client EA の UI から "Strategy Name" を "my_new_strategy" に設定

EA を再アップロードする必要はない。

## 動作確認

ブラウザで `https://localhost:8443/strategies` にアクセスすると登録済み一覧が見える:

```json
{"strategies": ["zigzag_line_break", "my_new_strategy"]}
```

## 戦略 → サーバの注意点

- **状態は Python オブジェクトの instance attribute** に持つ (ZigZag トラッカ等)。
  セッションごとに 1 インスタンス。WS が切れたらリセット。
- **描画**は session.py の `_emit_new_pivot_draws` で「新規ピボット差分」を自動 emit する。
  zigzag を使わない戦略では何も出ない (getattr で None 安全)。エントリ/決済時の
  マーカーは自動で出るので最低限のチャート可視化はある。
- **MTF データ**は EA が毎ティック H1/H4 の最新確定足を送ってくれる。
  サーバ側では `last_h1_time` / `last_h4_time` で dedup される。
- ロジック側からは ctx.bars_mtf(3600, n) / ctx.bars_mtf(14400, n) でアクセス。
