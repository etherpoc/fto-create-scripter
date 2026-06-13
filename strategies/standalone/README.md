# Standalone EA — mtf_pullback v2 ※**不採用(記録のみ)**

> ⚠ **2026-06-14: mtf_pullback (MTF) は不採用。** 実Axiory 11年検証で OOS/IS 両方 net 負け。
> 運用は breakout_h1 のみ([BREAKOUT_README](./BREAKOUT_README.md))。経緯は [MT5_README](./MT5_README.md)。本ファイルは記録として残置。

サーバ非依存の **完全スタンドアロン FTO EA**。`strategies/mtf_pullback` の v2
(`skip_on_trendline_break=True`) を丸ごと JavaScript に移植したもの。

薄い thin client (`strategies/thin_client/fto_strategy.js`) と違い、トレード判断は
**すべて EA 内で完結** する。ローカル Python サーバとの WebSocket 往復が無いので
**判断ラグがゼロ**。サーバはログ保存のためだけに（任意で）使う。

- EA 本体: [`mtf_pullback_v2.js`](./mtf_pullback_v2.js)
- ログ収集サーバ: [`server/log_collector.py`](../../server/log_collector.py)（ログ専用・トレード非関与）

> ⚠ thin client は「EA を薄く保つ」設計だったが、本 EA はユーザ判断
> （サーバ往復のラグ回避）で **意図的にロジックを EA に内包** している。
> ロジック修正時は **この .js を再アップロード** する必要がある（thin client と違う）。

---

## ロジック（検証済み mtf_pullback v2 と同一）

- **ベース足 = M5**。M15/M30 は M5 から内部集計、H1/H4 は `iXxx` で取得。
- **エントリー**: H4=H1=M30=M15 トレンド一致（大局）+ M5 が直近30本以内で逆方向（押し戻し）
  + M5 が大局方向へ転換した瞬間 + クールダウン(6本)経過 +
  **[v2]** エントリー方向の H4/H1 トレンドラインを割っていない
- **SL**: ロング→直近 M15 安値ピボット / ショート→直近 M15 高値ピボット
- **TP**: entry ± sl_dist（1:1 RR）
- **サイズ**: 口座 risk% を sl_dist で逆算（口座通貨建てに正しく換算、JPYクロス対応）
- **決済**: SL/TP を `PlaceOrder` に渡して FTO ネイティブに任せる（trailing なし）

純粋ロジック部（ZigZag / Dow / 集計 / トレンドライン）は Python 実装と
**完全一致を実データで検証済み**: `node tools/verify_ea_port.mjs <m5.jsonl> <N>`

---

## 使い方

### 1. ログ収集サーバを起動（任意だがログ保存に必要）

```powershell
PS> ./tools/run_log_collector.ps1
# → https://localhost:8443/log で待受、data/fto_mtf_pb_v2_live/ に JSONL 保存
```

初回のみ、ブラウザで `https://localhost:8443/ping` を開いて自己署名証明書を許可する
（前回 wss で承認済みなら不要）。証明書を信頼していないと EA の fetch がブロックされる。

> ログ不要なら EA の `Log Server Port` を **0** にすればサーバ無しで動く
> （その場合ログは FTO の DevTools コンソールにのみ出る）。

### 2. FTO に EA をアップロード

- `strategies/standalone/mtf_pullback_v2.js` をアップロード（`.js` のみ受付）
- **必ず M5 チャートに適用する**（ベース足が M5 のため）
- 推奨ペア（5.5y 検証で頑健）: **EURUSD / USDJPY / XAUUSD / CADJPY / EURJPY**
  - 各ペアの M5 チャートに 1 つずつ EA を貼る

### 3. パラメータ（FTO UI）

| 項目 | 既定 | 説明 |
|---|---|---|
| Risk % per trade | 1.0 | 口座の 1% を SL までの距離でロット逆算 |
| **TP RR** | **1.5** | TP = entry ± sl_dist × RR。5.5y 検証で最も広く頑健。強ペア限定なら 2.0 も可 |
| **Align** | **1** | アラインメント階層。**1=H1+M15(新ベスト)** / 2=+H4 / 3=+H4+M30。H4/M30は無関係と判明 |
| **room_R max** | **2.0** | 直近M15高安までの余地/SL がこの値超を除外(タイトSL=ノイズ負け)。0=off |
| **Block hour start/end** | **6 / 10** | この時間帯(UTC)のエントリーを除外(ロンドン午前の高ボラ)。start=-1 でoff |
| Magic Number | 220611 | 自戦略の注文識別 |
| Max Lot (safety cap) | 50 | 算出ロットの安全上限 |
| Log Server Port | 8443 | log_collector のポート。0 でログ送信オフ |
| USDJPY fallback | 150 | JPYクロスで USDJPY をクロス参照できない時のサイズ計算用レート |

> **2026-06-12 改善**: アラインメントを H1+M15 のみに(H4/M30外す)＋room_Rフィルタ＋6-10時除外。
> 5.5y WF で P2リターン約3倍・頻度約6倍 (全12 P2 +1.08%/月)。ただし全12 WR 42%は損益分岐40%に近く
> スプレッド余裕が薄いので実機検証必須。詳細は `docs/IMPROVEMENT_RESULTS.md`。

> **TP RR について（重要）**: SL リスクは常に口座 1%。RR1.5 = 利確を損切り幅の 1.5 倍に置く。
> 5.5 年検証では **RR1.5 が全12ペア/主要4ペアとも ROBUST**（RR1.0 は Edge GONE）。
> SL バッファ・構造フィルタ等の追加ロジックは**むしろ成績を下げた**ため EA には入れていない
> （素の v2 ロジック + RR1.5 が最良）。詳細は docs/PROGRESS.md。

### 4. 起動後の確認（重要）

- DevTools コンソールに `[mtfpb] started ...` が出る
- 最初のエントリーで `[mtfpb] ENTRY long EURUSD ... lot=0.50 risk$=100.00 path=...`
  が出る。**`risk$` が口座残高の約1%になっているか必ず確認**
  （JPYクロスで桁がおかしければ `USDJPY fallback` を実レートに合わせる）
- `path=` がサイズ計算の換算経路（`quote==acct` / `self_USDxxx` / `cross_USDJPY` /
  `fallback_usdjpy` 等）。`fallback_*` が出ていたらクロス参照が効いていない合図

---

## ログの中身（data/fto_mtf_pb_v2_live/<SYMBOL>/<session>.jsonl）

- `session_start` — 起動時。account_ccy / params
- `entry` — 発注時。entry/sl/tp/sl_dist/atr/lot/risk_amount/value_per_price/conv_path
- `outcome` — 決済検出時（ポジション遷移で検出）。exit_price は bar close 近似。
  **正確な損益は FTO のレポートが正**（EA ログは補助）
- `skip_size` — ロットが 0.01 未満等でスキップした時
- `session_end` — 停止時

---

## サイズ計算の注意（time_of_day 全損の教訓）

FTO には tick value / contract size の API が無いため、口座通貨建ての
1 lot あたり価値を **シンボル名から通貨換算して自前計算** している:

```
money_per_lot = sl_dist(価格) × contractSize × (口座通貨 / quote通貨)
lot           = (balance × risk%) / money_per_lot
```

- USD クォート（EURUSD 等）: 換算不要、正確
- USDxxx（USDCAD 等）: チャート自身の価格で換算、正確
- JPY クロス（CADJPY/EURJPY 等）: USDJPY を `iClose("USDJPY",...)` でクロス参照。
  取得できなければ `USDJPY fallback` パラメータを使用（要・実レート設定）

**本番投入前に FTO バックテストで最初の数トレードの `risk$` ログを必ず目視確認すること。**
スプレッドは FTO 側の設定が実際に適用される（バックテスト＝実スプレッドで検証可能）。
