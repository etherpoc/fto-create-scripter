# FTO API Reference

**ここが唯一の正。生成側は推測で関数名を作らない。**

## このドキュメントの適用範囲

新アーキテクチャでは、戦略ロジックは Python 側で書くため、通常開発で FTO API を
直接呼ぶ場面は無い。**このリファレンスは `strategies/thin_client/fto_strategy.js`
の保守や、薄い EA に新しい機能を足す場合にのみ使う**。

通常のロジック追加 (server/deciders/ に新規戦略を加える等) では参照不要。

出典:
- 公式ドキュメント: <https://fto-2.gitbook.io/fto-strategies-docs>
- 全文ダンプ: <https://fto-2.gitbook.io/fto-strategies-docs/llms-full.txt>

> 言語は **JavaScript** (FTO のアップロード UI は **`.js` 以外を弾く**)。
> ドキュメントのコード例は TypeScript 風だが、実際にアップロードできるのは
> **型注釈やインターフェース宣言を取り除いた素の `.js`** だけ。
> すべての API は `this.api.<関数名>(...)` の形で呼ぶ（クラスメソッド内）。
> 本リファレンスでは関数名のみを記載しているので、生成時には適宜 `this.api.`
> を前置すること。

> **未記入の項目に対して、生成側は `# TODO(FTO_API: ...)` を残す。
> 推測で関数名を発明してはならない。**

---

## ① ライフサイクル / エントリポイント

| 抽象操作 | FTO の実装 | 備考 |
|---|---|---|
| モジュール構造 | 下記コードブロック参照 (相対パスでの import + `export default class XXX extends StrategyImplementation`) | 契約が崩れると "Invalid module: Does not export a valid UserIndicator class" で弾かれる。bare specifier は loader が解決できず "Failed to resolve module specifier ..." になる |
| 足確定ごとに呼ばれる関数名 | **`OnTick()`** (※下記参照) | OnTick は「ティックごと」に発火する。**確定足検出は自前で `Time(0)` 比較**して実装する必要がある（下記コード例参照） |
| 初期化フック（パラメータ受け取り等） | **`Init()`** | 戦略がロードされたとき 1 回だけ呼ばれる。`createTOptValue_*` + `RegOption` でパラメータ宣言する |
| 終了 / クリーンアップフック | **`Done()`** | 戦略が無効化されたとき呼ばれる |
| 戦略表示名 | **`this.api.setStrategyShortName("...")`** | Init 内で呼ぶ。FTO の UI に出る |
| 戦略説明 | **`this.api.setStrategyDescription("...")`** | Init 内で呼ぶ |
| `this.api` | StrategyImplementation の継承で自動注入 | コンストラクタで受け取る必要なし |

### 確定足検出パターン（必須）
`OnTick()` はティックごとに呼ばれるため、「足確定ごとに 1 回」のロジックは
次のように **保存した直前バー時刻との比較** で実装する:

```typescript
const currentTime = this.api.Time(0);
if (currentTime.valueOf() === this.lastBarTime.valueOf()) {
  return;
}
this.lastBarTime = currentTime;
// --- ここから先が「新しい足が開いた」ときの処理 ---
```

つまり、ローカルの `Strategy.on_bar(ctx)` 相当のロジックは、
FTO 側ではこの `if` を抜けた後の領域に書く。

---

## ② データ参照

| 抽象操作 | FTO の実装 | 備考 |
|---|---|---|
| 現在の終値（最新確定足の close） | **`Close(0)`** | index 0 が現在バー |
| 現在 Bid / Ask | **`Bid()`** / **`Ask()`** | スプレッドが要る場面用 |
| 直近 n 本の終値 | `Close(0), Close(1), ..., Close(n-1)` を**ループで取得**して配列化 | **`CopyClose()` のような一括取得は無い**（公式に明記されていない）|
| 直近 n 本の高値 | `High(i)` を同様にループ | 同上 |
| 直近 n 本の安値 | `Low(i)` を同様にループ | 同上 |
| 直近 n 本の始値 | `Open(i)` を同様にループ | 同上 |
| 出来高 | **`Volume(i)`** | |
| バー時刻 | **`Time(i)`** | 戻り値型 `FTODate`。比較は `.valueOf()` で |
| **配列の並び（重要）** | **index 0 = 最新（現在バー）、index 1 = 1 本前、…** | ローカル `core` は「古→新」リストを前提とするため、**FTO 側で取り出した配列は逆順にしてから渡す**こと |
| 上位足 (MTF) の高値 | **`iHigh(symbol, timeframe, index)`** | `timeframe` は秒で指定。例: D1 = 1440 (※単位は要再確認。documentation の例では `iHigh(Symbol(), 1440, 1)`) |
| 上位足 (MTF) の安値 | **`iLow(symbol, timeframe, index)`** | 同上 |
| 上位足 (MTF) の移動平均 | **`iMA(...)`** | 同上 |
| 通貨ペア名 | **`Symbol()`** | |
| 現在の時間足 | **`Timeframe()`** | 秒単位（要再確認）|
| ポイント（最小価格刻み） | **`Point()`** | pip 換算に使う |
| 小数桁数 | **`Digits()`** | |

> ⚠ `Timeframe()` / `iHigh()` などの **時間足の単位 (秒/分)** は、ドキュメントの
> サンプルが `1440`（D1 を分換算なら 1440 分）になっており、秒なら 86400 のはず。
> 戦略生成時に MTF を使う場合は **値を入れる前に必ず手動で確認** すること。
> 不明な場合は `# TODO(FTO_API: mtf-timeframe-unit)` を残す。

---

## ③ 発注 / ポジション

| 抽象操作 | FTO の実装 | 備考 |
|---|---|---|
| 成行ロング / ショート（共通関数） | **`PlaceOrder(symbol, positionType, orderMode, volume, sl, tp, comment, magicNumber)`** | `orderMode = 0` で成行 |
| `positionType` の値 | **`TTradePositionType.BUY`** / **`TTradePositionType.SELL`** | enum |
| 戻り値 | チケット番号、失敗時は `null` | |
| 現在のポジション照会 | 専用関数なし。**ループで `getActiveOrderCount()` → `selectOrder(i, 0, 0)` → `getOrderType()`** | 自前で None / "long" / "short" に正規化する必要がある |
| アクティブ注文数 | **`GetActiveOrderCount()`** | SDK .d.ts で確認済 (PascalCase) |
| 注文選択（イテレート） | **`SelectOrder(i, EOrderSelectMode.SELECT_ORDER_BY_POSITION, searchMode?)`** | 第 2 引数: BY_POSITION=0 / BY_TICKET=1。第 3 引数 (TSearchMode): DATA_MODE_TRADES=0 (アクティブ) / DATA_MODE_HISTORY=1 |
| 注文方向 | **`GetOrderType()`** | 戻りは `TTradePositionType.BUY` / `.SELL` |
| 注文チケット | **`GetOrderTicket()`** | |
| 注文の通貨ペア | **`GetOrderSymbol()`** | |
| 注文のマジック番号 | **`GetOrderMagicNumber()`** | 自戦略の注文だけフィルタするのに使う |
| 決済（チケット単位） | **`CloseOrder(ticket)`** | 一括 `ClosePosition()` 相当は無い。**全決済はチケットを回して 1 件ずつ閉じる** |
| 数量の単位 | **ロット** | 例: `0.1` = 0.1 lot |
| SL / TP の単位 | **価格（pip ではない）** | ローカル backtest と一致。pip 指定したい場合は `Point()` を使って自前で換算 |
| SL / TP の指定 | **`PlaceOrder()` の引数で同時に渡す** | 後付け修正関数の有無は要確認 (TODO) |

### 「ポジション照会」を抽象化するためのラッパ例
ローカル `Context.position()` は None / "long" / "short" を返す前提。
FTO 側では自前のヘルパで作る:

```typescript
private currentPosition(magic: number): "long" | "short" | null {
  const n = this.api.getActiveOrderCount();
  for (let i = 0; i < n; i++) {
    this.api.selectOrder(i, 0, 0);
    if (this.api.getOrderSymbol() !== this.api.Symbol()) continue;
    if (this.api.getOrderMagicNumber() !== magic) continue;
    const t = this.api.getOrderType();
    if (t === TTradePositionType.BUY) return "long";
    if (t === TTradePositionType.SELL) return "short";
  }
  return null;
}
```

### 「全決済」ラッパ例
```typescript
private closeAll(magic: number): void {
  const n = this.api.getActiveOrderCount();
  for (let i = n - 1; i >= 0; i--) {
    this.api.selectOrder(i, 0, 0);
    if (this.api.getOrderSymbol() !== this.api.Symbol()) continue;
    if (this.api.getOrderMagicNumber() !== magic) continue;
    this.api.closeOrder(this.api.getOrderTicket());
  }
}
```

---

## ④ 口座 / その他

| 抽象操作 | FTO の実装 | 備考 |
|---|---|---|
| 口座残高 | **`GetAccountBalance()`** | |
| 口座エクイティ | **`GetAccountEquity()`** | |
| 現在の含み損益 | **`GetCurrentProfit()`** | エクイティ - 預託金 |
| 使用済み証拠金 | **`GetAccountMargin()`** | |
| 利用可能証拠金 | **`GetAvailableMargin()`** | |
| 口座通貨 | **`GetAccountCurrency()`** | |
| ブローカー名 | **`GetBrokerName()`** | |
| レバレッジ | **`GetLeverageRatio()`** | |
| ログ出力 | **`console.log(...)`** / **`console.error(...)`** | DevTools 相当で確認 |
| 最適化用パラメータ宣言 | **`createTOptValue_number(default)`** または **`createTOptValue_bool(default)`** を作って **`RegOption(name, TOptionType.XXX, value)`** で登録 | 範囲は **`SetOptionRange(name, min, max)`**、刻みは **`SetOptionStep(name, step)`** |

### 外部パラメータ宣言の典型パターン
```typescript
// Init() の中
this.LotSize = this.api.createTOptValue_number(0.1);
this.api.RegOption("Lot Size", TOptionType.DOUBLE, this.LotSize);
this.api.SetOptionRange("Lot Size", 0.01, 1000);

this.MagicNumber = this.api.createTOptValue_number(123456);
this.api.RegOption("Magic Number", TOptionType.INT, this.MagicNumber);

// 使うとき
const lot = this.LotSize.value;
```

---

## ⑤ 既知の制約・注意（言語固有の罠）

- **アップロードは `.js` 形式のみ**。`.ts` や `.py` は FTO の UI で弾かれる
  （"無効なファイル / ファイルは.js形式で..." のエラーが出る）。生成時は
  必ず素の JavaScript として書く（型注釈・`interface`・`as` キャスト・
  `private/public` キーワード等の TS シンタックスを使わない）。
- **モジュール契約**: `.js` を弾かれずに通しても、次に loader が
  "Invalid module: Does not export a valid **UserIndicator** class" の
  エラーを出す。これは loader 共通の文言で、本当の意味は
  「`export default class extends StrategyImplementation` (戦略) または
  `extends IndicatorImplementation` (インジ) が無い」という意味。
  戦略の場合は必ず **import 文を使わず、SDK をインライン化する**:
  ```js
  class StrategyImplementation {
    get API() { return this.api; }
    OnAttach(api) { this.api = api; }
    Reset() {}
    Done() {}
  }
  const TOptionType = Object.freeze({ LONGWORD:0, INTEGER:1, DOUBLE:2, STRING:3, BOOLEAN:4 });
  const TTradePositionType = Object.freeze({ BUY:0, SELL:1, BUY_LIMIT:2, SELL_LIMIT:3, BUY_STOP:4, SELL_STOP:5 });
  export default class XXX extends StrategyImplementation { ... }
  ```
  `module.exports = X` の CommonJS 形式ではダメ。
- **`import` 文は一切書けない**。FTO は uploaded `.js` を `blob:` URL から
  ES module として load するため、import の specifier がいずれも解決できない:
  - bare specifier (`"forex-tester-custom-strategy-api-test"`) →
    `Failed to resolve module specifier "..." Relative references must start with
    either "/", "./", or "../"`
  - 相対パス (`"../node_modules/.../dist/UserStrategy"`) →
    `Invalid relative url or base scheme isn't hierarchical` (blob: は非階層)
  必要な SDK 部分はファイル内に直接書き込む。値の出典は
  `forex-tester-custom-strategy-api-test` v1.2.1 の dist。
- 文法は `const` / `let`、`this.api.X`、セミコロン、`null` リテラル等の
  通常の JS。Python / MQL 系ではない。
- **`Time(0)` / `High(0)` etc. は「現在進行中の未確定バー」を指す** (MT4 慣習)。
  確定済みバーは **index 1 から**。`OnTick` で確定足検出した直後でも、
  `index 0` のデータはまだ open 値のスナップショット程度しか入っていない。
  指標計算 / ZigZag / 反転判定など「確定足のみ」が前提のロジックは、
  必ず `for (let i = n; i >= 1; i--)` のように **index 1 以上を読む**こと。
  index 0 を読むと「ピボットが隣のバーに 1 本ずれ、価格は open スナップショット」
  という off-by-one バグになる。
- メソッドのケースは **PascalCase が確定** (SDK の `dist/IStrategyProcRec.d.ts`
  を直接確認):
  - 注文系: `GetActiveOrderCount` / `SelectOrder` / `GetOrderType` /
    `GetOrderTicket` / `GetOrderSymbol` / `GetOrderMagicNumber` / `CloseOrder`
  - 価格系: `Time(i)` / `Open(i)` / `High(i)` / `Low(i)` / `Close(i)` / `Volume(i)` / `Symbol()`
  - 口座: `GetAccountBalance()`
  - 描画: `CreateChartObject` / `SetObjectProperty` / `SetObjectText`
  - 発注: `PlaceOrder(Symbol, OperationType, price, LotSize, StopLoss, TakeProfit, Comment, MagicNumber)` (price=0 で成行)
  - パラメータ宣言系: `RegOption` / `SetOptionRange` / `SetOptionDigits` (PascalCase)
  - **例外で camelCase**: `setStrategyShortName` / `setStrategyDescription` /
    `createTOptValue_number` / `createTOptValue_bool` / `createFTODate`
  - MTF (元 MQL 由来): `iTime` / `iOpen` / `iHigh` / `iLow` / `iClose`
    (先頭が小文字の `i`)
- docs 本文中には `getActiveOrderCount` (小文字) を含むコードサンプルもあるが、
  実装上は **動かない**。.d.ts (= SDK 自身の型定義) を正とすること。
- **OnTick は「ティック」ごと**に発火する。`Time(0)` で確定足判定を入れない限り、
  同じ足で何度も発注ロジックを走らせてしまう。**毎戦略でこの足確定検出を必ず入れる**。
- **配列の並びは index 0 = 最新**。古→新を前提とする `src/core/` の指標群に渡す前に
  必ず **逆順** にする。
- **`CopyClose()` のような一括配列取得関数は確認できない**。直近 n 本を取るには
  `Close(i)` を `i=0..n-1` でループする。
- **SL / TP は価格指定**（pips ではない）。ローカル backtest と一致するので
  そのまま `entry_price ± k×ATR` を渡してよい。
- **全決済関数は無い**。`getActiveOrderCount` でカウントを取り、`selectOrder` →
  `getOrderTicket` → `closeOrder(ticket)` を回す。
- **ポジション方向の取得関数は無い**。注文をループして `getOrderType()` を見る。
  `magicNumber` でフィルタしないと、他戦略の注文を巻き込むリスクがある。
- **`Timeframe()` / `iHigh()` の時間足単位（秒 or 分）はドキュメントの記述に揺れがある**。
  MTF を使う戦略を生成する前に必ず実物で確認。不明なら `# TODO(FTO_API: mtf-timeframe-unit)` を残す。
- `selectOrder(i, 0, 0)` の **第 2 / 第 3 引数の意味はドキュメントから明示できなかった**
  （プールやモードの指定の可能性）。実物に貼って動作を確認してから定数化すること。

---

## ⑥ 未確定 / 要追加調査の項目（TODO リスト）

- [ ] `iHigh / iLow / iMA` の `timeframe` 引数単位（分 or 秒 or 内部 enum）
- [ ] `selectOrder` の第 2, 3 引数の意味（プール種別 / モード）
- [ ] SL / TP を後から修正する関数（`OrderModify` 相当）の有無
- [ ] 部分決済（チケット指定 + 量指定）の関数の有無
- [ ] ペンディング注文（指値 / 逆指値）の `orderMode` 番号
- [ ] `Timeframe()` の戻り値の単位
- [ ] `iMA(symbol, timeframe, period, shift, method, applied_price)` 等の正式シグネチャ

---

## 置換マッピングの運用

- `src/fto/adapter_template.py` および生成された `strategies/<name>/fto_strategy.py`
  には `# FTO_API: <カテゴリ>` のマーカーがある。
- このリファレンスの該当行に値が入っていれば、生成時に **その値で機械置換** する。
- 値が空 / TODO 項目はそのまま `# TODO(FTO_API: <カテゴリ>)` を残す。
