# SF6 MCP サーバ — AWS デプロイ手順 (ADR-017 / M4 ステップ4)

API Gateway (HTTP API) + Lambda で MCP サーバを公開する。コードは
`src/sf6_engine/mcp_server/` (server.py = ツール定義, app.py = Lambda ハンドラ)、
SAM テンプレートは `template-mcp.yaml`。

ローカル検証済み: 全7ツール / warm invocation / Bearer 認証 (`app.py` をユニット実行)。

---

## 前提条件 (デプロイ前に1回だけ)

### 1. SSM パラメータを登録 (Supabase 接続情報 + 認証トークン)

```bash
# Supabase URL (anon, 読み取り専用)
aws ssm put-parameter --region ap-northeast-1 --name /sf6/supabase-url \
  --type String --value "https://xxxx.supabase.co"

# Supabase anon key (SecureString)
aws ssm put-parameter --region ap-northeast-1 --name /sf6/supabase-anon-key \
  --type SecureString --value "<anon key>"

# MCP Bearer 認証トークン (SecureString, 任意だが本番では必須)
aws ssm put-parameter --region ap-northeast-1 --name /sf6/mcp/auth-token \
  --type SecureString --value "$(openssl rand -hex 24)"
```

> `sf6-deployer` は `ssm:PutParameter` on `/sf6/*` を持つので、このユーザーで登録可能。

### 2. デプロイ用 IAM 権限を追加 (sf6-deployer に不足)

`SF6FrameScraperDeployerPolicy` には **apigateway 権限が無い**。
`deploy/iam/apigateway-deploy-policy.json` の文を同ポリシーに追加する
(管理者プリンシパルで実行)。Lambda 実行ロール (sf6-*) / SSM / Bedrock /
CloudFormation / Lambda は既存権限でカバー済み。

### 3. Bedrock Titan V2 (ap-northeast-1) のモデルアクセスが有効であること
ステップ2 で再埋め込み済みなら確認済み。

---

## デプロイ

```bash
cd streetfighter6-engine
cp samconfig-mcp.toml.example samconfig-mcp.toml

# arm64 ネイティブ依存 (pydantic-core 等) を正しくビルドするため --use-container 推奨
sam build -t template-mcp.yaml --use-container
sam deploy --config-file samconfig-mcp.toml
```

デプロイ後、出力 `McpEndpoint` がエンドポイント URL:
```
https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/prod/mcp
```

## クライアント登録 (Claude Desktop 等)

リモート MCP として上記 URL を登録し、ヘッダに Bearer トークンを付与する:
```
Authorization: Bearer <auth-token の値>
```

## 動作確認

```bash
TOKEN=$(aws ssm get-parameter --region ap-northeast-1 --name /sf6/mcp/auth-token \
  --with-decryption --query Parameter.Value --output text)
URL=https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/prod/mcp

curl -s "$URL" -X POST \
  -H "authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -H "accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
# → serverInfo {"name":"sf6-engine",...} が返れば成功
```

## コスト

- Lambda: 無料枠内 (個人利用)。API Gateway HTTP API: $1.00/100万リクエスト + 無料枠。
- Bedrock Titan V2: search_system_docs 1回 ≈ $0.000001 (ステップ2 試算参照)。
- スロットリング (template の DefaultRouteSettings: burst 10 / rate 5) でコスト暴発を抑制。
