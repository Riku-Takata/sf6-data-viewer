# SF6 Engine MCP サーバ

実装済みの決定論ロジック層 (フレームデータ照会・確定反撃・コンボ・セットプレイ) を
MCP (Model Context Protocol) ツールとして公開する。自然言語の解釈と回答生成は
ホスト側 LLM (Claude Desktop / 将来の Bot) が担う。LLM 段は本サーバに含めない。

参照: `ADR-017` (実装済み機能を AWS リモート MCP サーバとして切り出す)。

## 公開ツール

| ツール | 用途 | 主な入力 |
|---|---|---|
| `lookup_move` | 単一技のフレームデータ照会 (戻り値に SC input 付与) | character, move_name (日本語名 or numpad) |
| `check_punish` | ガード時の確定反撃判定 (候補技に input 付与, パリィ除外) | character, move_name, punisher(任意) |
| `compute_setplay` | KD/ヒット後の起き攻め択計算 | character, move_input (SC表記 or 技名) |
| `analyze_combo` | 始動技からの最大コンボ計算 | character, starter_input (SC表記 or 技名), use_dr, drive_bars |
| `list_moves` | 技一覧 (技名 + SC input。技名解決の補助) | character, keyword(任意, 技名/input 両対応) |
| `search_system_docs` | ゲームシステム文書のハイブリッド検索 | query, count, threshold |

`search_system_docs` の埋め込みは AWS Bedrock Titan V2 を使う (Ollama 非依存)。
実行プリンシパルに `bedrock:InvokeModel` (Titan) 権限が必要 (`deploy/iam/NOTES.md`)。

技名は CAPCOM 日本語名 (`タイガージャブ`) と SuperCombo numpad 表記 (`2HK`, `623HP`)
の両方を解決する。`compute_setplay` / `analyze_combo` は input が一致しない場合、
技名 (日本語/英語) からの逆引き解決を自動で試みる (強度修飾子・OD 判別込み)。
曖昧な場合は先に `list_moves` で確認すること。

## ローカル起動 (stdio)

```bash
cd streetfighter6-engine
PYTHONPATH=src ./.venv312/bin/python -m sf6_engine.mcp_server.server
```

Supabase の接続情報はプロジェクトルートの `.env` から自動ロードされる
(`SUPABASE_URL` / `SUPABASE_ANON_KEY`)。

## Claude Desktop への登録

`~/Library/Application Support/Claude/claude_desktop_config.json` に追記:

```json
{
  "mcpServers": {
    "sf6-engine": {
      "command": "/Users/riku/Documents/0_Privates/sf6-data-viewer/streetfighter6-engine/.venv312/bin/python",
      "args": ["-m", "sf6_engine.mcp_server.server"],
      "env": {
        "PYTHONPATH": "/Users/riku/Documents/0_Privates/sf6-data-viewer/streetfighter6-engine/src"
      }
    }
  }
}
```

登録後 Claude Desktop を再起動すると、チャットから上記ツールが呼べる。

## リモート (AWS Lambda + API Gateway)

ローカルでの HTTP 起動確認:
```bash
SF6_MCP_TRANSPORT=streamable-http PYTHONPATH=src \
  ./.venv312/bin/python -m sf6_engine.mcp_server.server
```

AWS デプロイ (SAM):
- ハンドラ: `app.py` (Mangum + 永続 lifespan + Bearer 認証ミドルウェア)
- テンプレート: `template-mcp.yaml` (Lambda + HTTP API + 実行ロール)
- **手順は `deploy/DEPLOY.md` を参照** (SSM 登録 / IAM 追加 / sam build & deploy)

実装メモ:
- FastMCP は `stateless_http=True, json_response=True` + DNS rebinding 保護 OFF。
- StreamableHTTP セッションマネージャは run-once 制約があるため、app.py は
  cold start で lifespan を 1 度だけ起動し、warm invocation を跨いで保持する。

## 既知の調整余地

- ~~`check_punish` の `punisher_options` にパリィ (発生1F) が混ざる~~
  → 対応済 (2026-07-06): 技名に「パリィ」を含む行を除外。
