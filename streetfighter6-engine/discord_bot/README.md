# SF6 Discord Bot

自然言語の SF6 質問に答える Discord bot。**ローカル gemma4 (Ollama)** で質問を解析し、
**AWS 上の MCP サーバ**経由でフレームデータ系ツールを実行して回答する。

MCP を「開発者公開インターフェース」として外部ホストから使う dogfooding を兼ねる (ADR-017)。

## アーキテクチャ

```
Discord メッセージ
  → gemma4 (Ollama, ローカル)     : intent_parser で構造化
  → map_intent                    : intent → MCP ツール選択
  → MCP クライアント (streamable-http) → AWS MCP サーバ (Bearer 認証)
  → gemma4 (generate_answer)      : ツール結果から日本語回答生成
  → Discord に返信
```

bot は **Ollama (ローカル) と MCP サーバ (AWS) のみ**に依存。Supabase / Bedrock には
直接触れない (それらは MCP サーバ側に閉じている)。

## intent → MCP ツール対応

| intent_type | MCP ツール |
|---|---|
| lookup_move / combo_info | `lookup_move` |
| punish_check | `check_punish` |
| setplay_analysis | `compute_setplay` |
| max_combo | `analyze_combo` |
| explain_concept | `search_system_docs` |
| compare_moves | `lookup_move` ×2 |
| general_question | `search_system_docs` |

## セットアップ

### 1. Discord アプリ + Bot を作成
1. https://discord.com/developers/applications → New Application
2. 左メニュー **Bot** → Add Bot → **Token** をコピー
3. Bot 設定で **MESSAGE CONTENT INTENT** を ON にする (必須)
4. **OAuth2 → URL Generator** で scope=`bot`, 権限=`Send Messages`/`Read Message History`
   を選び、生成された URL で自分のサーバに招待

### 2. 依存インストール
```bash
cd streetfighter6-engine
./.venv312/bin/python -m pip install -r discord_bot/requirements.txt   # discord.py
# mcp / sf6_engine 本体は root requirements.txt で導入済み
```

### 3. 設定
```bash
cp discord_bot/.env.example discord_bot/.env
# discord_bot/.env を編集:
#   DISCORD_TOKEN  = 手順1のトークン
#   SF6_MCP_URL    = CloudFormation 出力の McpEndpoint
#   SF6_MCP_TOKEN  = SSM /sf6/mcp/auth-token の値
#   OLLAMA_MODEL   = gemma4:e2b 等
```

### 4. 起動
```bash
PYTHONPATH=src ./.venv312/bin/python -m discord_bot.bot
```

## 使い方

bot へのメンション、または `!sf6` プレフィックス:
```
@SF6Bot サガットの2HKガードして反撃できる?
!sf6 ルークの5MPからの最大コンボは?
!sf6 バーンアウトって何?
```

## 常駐化

Ollama (gemma4) が同じマシンで動いている必要がある。ローカル PC で動かし続けるか、
GPU/CPU リソースのある常時起動ホスト (EC2 等) に Ollama ごと載せる。

## 既知の制約 / MCP への改善フィードバック

- `compute_setplay` / `analyze_combo` は SC の numpad 入力 (例: `623HP`) を要求するため、
  必殺技を**名前**で指定した場合 (例: タイガーアッパー) は解決できないことがある。
  → MCP 側に「技名 → SC input」を解決するツールがあると開発者体験が向上する。
- `check_punish` 等は技を SuperCombo 名 (例: Tiger Kick) で返すため、呼び出し時の
  識別子 (2HK) との対応が分かりにくい。bot 側ではコンテキストに等値表記を補って回避。
  → MCP の戻り値に「呼び出し時 input」を含めると親切。
