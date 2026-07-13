# SF6 Engine MCP サーバ

実装済みの決定論ロジック層 (フレームデータ照会・確定反撃・コンボ・セットプレイ) を
MCP (Model Context Protocol) ツールとして公開する。自然言語の解釈と回答生成は
ホスト側 LLM (Claude Desktop / 将来の Bot) が担う。LLM 段は本サーバに含めない。

参照: `ADR-017` (実装済み機能を AWS リモート MCP サーバとして切り出す)。

## 公開ツール

| ツール | 用途 | 主な入力 |
|---|---|---|
| `lookup_move` | 型付き統合フレームプロファイル (全ソース・採用値・両視点・状況評価) | character, move_name, scenario(任意) |
| `check_punish` | フレーム窓と到達確度を分離した反撃判定 | character, move_name, punisher(任意), scenario(任意) |
| `analyze_sequence` | 2技連携・最速/ディレイ暴れ・相打ち後有利・追撃確度 | character, attacker_sequence, defender_startup/move, delay |
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

`lookup_move` のコア値はフィールドごとに CAPCOM公式 → UFD → SuperCombo の順で採用する。
ただしCAPCOM硬直欄の `全体 N` は硬直値として使わず、UFD/SCの硬直で補完する。
ガード時は攻撃側値を正規値として保持し、防御側値を機械的に符号反転する。範囲・条件別・
段階別・複数持続・着地硬直は単一整数へ潰さず、`frame_profile.facts` に型と生値を保持する。
ガード不能/非攻撃動作は `not_applicable`、固定値がない場合は `variable` とし、欠損値や
仮の0Fへ変換しない。

`scenario` は距離、接触持続F、段数、相手状態、Burnout/DR、画面端、block/hit、視点を
受け取る。技解決が複数強度・派生へ当たる場合は `resolution=ambiguous` として計算を止める。
`check_punish` は硬直差から `frame_punishable` を返すが、ガード後距離・押し戻し・技の
到達が未検証なら `confirmed_punishable=null` のままにし、候補を `timing_only` と表示する。

`analyze_sequence` は発生・ガード差に統合プロファイルを使い、SCの技別hitstun/hitstopと
相手キャラ+技まで一致するレビュー済み観測を補助根拠にする。相手技未指定時は該当技を
個別計算して分布を返す。同時発生だけで相打ちを断定せず、追撃も距離・
状態の観測がある場合だけ `combo_confirmed=true` にする。詳細は
`docs/SEQUENCE_ANALYSIS.md` を参照。

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
- 範囲/条件別ガード硬直差は単一windowに丸めず、条件が結合できない場合は判定保留。
- リーチ/当たり判定を使う到達判定は未統合。候補は「フレーム上・到達未検証」と明示する。
- `contextual_frame_model_migration.sql` の適用・バックフィル後に、レビュー済みgeometryと
  直接実測を使う `confirmed_punishable=true` を実装する。
- `sequence_analysis_migration.sql` 適用後にレビュー済み連携観測をDBへupsertする。
