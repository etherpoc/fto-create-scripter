# Local Ping Test Server

FTO 上の戦略 (.js) から `fetch()` でローカルにリクエストを飛ばせるか検証するための、
最小の FastAPI サーバ。

## 0. セットアップ

リポジトリ root から:

```powershell
python -m pip install -r server/requirements.txt
```

(Windows / PowerShell 想定。bash / zsh も同じ)

## 1. まずは HTTP で起動して fetch 動作を確認する

ターミナル A で:

```powershell
python -m uvicorn server.main:app --host 0.0.0.0 --port 8080 --reload
```

ターミナル B で動作確認:

```powershell
curl http://localhost:8080/ping
```

`{"ok":true,...}` が返ってくれば起動 OK。

その状態で、FTO の Strategies に **`strategies/ping_test/fto_strategy.js`** をアップロードして
1 分ほどバックテストを動かし、DevTools (F12) のコンソールを見ます。

期待される出力パターンと意味:

| コンソール出力 | 意味 | 次の手 |
|---|---|---|
| `[ping-http] status=200` | HTTP fetch が **完全に通った** | 自己署名 HTTPS の手順をスキップして、本番設計に進める (※ 推奨は HTTPS) |
| `[ping-http] err TypeError: Failed to fetch` | fetch は呼べるが **Mixed Content / CORS でブロック** | 下記 §2 で HTTPS を立てる |
| `[ping-http] err ReferenceError: fetch is not defined` | サンドボックスに **fetch が無い** | WebSocket (`[ws]` 行) のログを確認し、それも無いなら別経路を検討 |
| `[ws] opened ws://localhost:8080/ws` | WebSocket 経路は通る | fetch が無くても通信手段はある |
| (何も出ない) | fetch が黙殺 | ブラウザ DevTools の Network タブで CSP 違反を確認 |

ここでの観測結果を貼ってもらえれば、次の構成を提案します。

## 2. HTTPS で起動する (fetch が Mixed Content でブロックされた場合)

FTO の Web UI は HTTPS で動いているため、ブラウザは HTTPS ページから HTTP への
fetch を **必ずブロック** します (Mixed Content policy)。ローカルサーバも HTTPS で
立てる必要があります。

### 2-1. 自己署名証明書を作る (mkcert を使う方法、推奨)

[mkcert](https://github.com/FiloSottile/mkcert) を入れて:

```powershell
# 一度だけ: ローカル CA をシステムに登録
mkcert -install

# プロジェクト用の証明書を発行
mkdir server\certs
cd server\certs
mkcert localhost 127.0.0.1 ::1
```

`localhost+2.pem` (証明書) と `localhost+2-key.pem` (秘密鍵) が生成される。

### 2-2. 自己署名証明書を作る (OpenSSL を使う方法、mkcert を入れたくない場合)

```powershell
mkdir server\certs
cd server\certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 `
    -keyout localhost-key.pem -out localhost.pem `
    -subj "/CN=localhost" `
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

ただし mkcert と違って **ブラウザに信頼登録されない** ため、Chrome で
`https://localhost:8443/ping` を一度開いて「詳細設定 → アクセスする (安全ではありません)」
を押して例外登録しておく必要がある (= 一度だけ手動で許可)。

### 2-3. HTTPS で起動

mkcert で作った場合 (ファイル名に注意):

```powershell
python -m uvicorn server.main:app --host 0.0.0.0 --port 8443 --reload `
    --ssl-keyfile=server/certs/localhost+2-key.pem `
    --ssl-certfile=server/certs/localhost+2.pem
```

openssl で作った場合:

```powershell
python -m uvicorn server.main:app --host 0.0.0.0 --port 8443 --reload `
    --ssl-keyfile=server/certs/localhost-key.pem `
    --ssl-certfile=server/certs/localhost.pem
```

確認:

```powershell
curl -k https://localhost:8443/ping
```

`{"ok":true,...}` が返れば OK。

再度 FTO 上で ping テスト .js を動かして DevTools を確認。
`[ping-https] status=200` が出れば本格設計に進めます。

## 3. これから増やすエンドポイント

- `POST /decide` — 戦略判断 (現状はスタブ、本番では特徴量 → action)
- `POST /trade-log` — トレード結果の蓄積
- `GET  /health` — ヘルスチェック
