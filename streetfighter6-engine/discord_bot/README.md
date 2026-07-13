# SF6 Discord Bot

自然言語の SF6 質問に答える Discord bot。**ローカル gemma4 (Ollama)** で質問を解析し、
**AWS 上の MCP サーバ**経由でフレームデータ系ツールを実行して回答する。

MCP を「開発者公開インターフェース」として外部ホストから使う dogfooding を兼ねる (ADR-017)。

## アーキテクチャ

```
Discord メッセージ
  → intent_parser                 : 技名と距離/持続/状態/視点を分離して構造化
  → map_intent                    : intent → MCP ツール選択
  → MCP クライアント (streamable-http) → AWS MCP サーバ (Bearer 認証)
  → typed frame profile           : CAPCOM主値 + UFD/SC補完 + 両視点 + scenario評価
  → punish_service                : 時間窓と到達確度を分離
  → generate_answer               : コア数値は決定論、一般質問のみLLM
  → Discord に返信
```

bot は **Ollama (ローカル) と MCP サーバ (AWS) のみ**に依存。Supabase / Bedrock には
直接触れない (それらは MCP サーバ側に閉じている)。

`lookup_move` はCAPCOM/UFD/SuperComboの全観測値と採用ソースを返す。発生・持続・硬直、
ガードさせた側 (攻撃側)、ガードした側 (防御側) はLLMを介さず回答し、防御側の値は
攻撃側硬直差からコードで符号反転する。UFDのDB再取込だけならLambda再デプロイは不要で、
最大5分のキャッシュTTL後に反映される。

技名が複数強度・派生へ当たる場合は確認を促し、先頭候補の数値を断定しない。
反撃候補は発生Fが間に合っても到達距離を証明できるまでは「フレーム上の候補」とし、
確定反撃とは表示しない。ジャンプ技と連携途中技は地上ニュートラルの候補から除外する。

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

## 網羅評価

Discord へ実投稿せず、bot と同じ `intent_parser → MCP → generate_answer` 経路を検査する:

```bash
# ケース生成だけ確認
PYTHONPATH=src ./.venv312/bin/python tests/bot_comprehensive_eval.py --dry-run --exhaustive

# 小さく実行
PYTHONPATH=src ./.venv312/bin/python tests/bot_comprehensive_eval.py --chars sagat --max-per-bucket 1

# 全件実行 (数千問規模・推奨: API Gateway 429回避のためローカルMCP相当モード)
SF6_MCP_LOCAL_ONLY=1 PYTHONPATH=src ./.venv312/bin/python \
  tests/bot_comprehensive_eval.py --exhaustive \
  --case-types move_data active recovery guard_attack guard_defense \
  --concurrency 16 --quiet-success --progress-every 500 \
  --summary-only --jsonl ''

# AWS MCP 本番経路まで含めて確認したい場合 (429対策で低並列+長めリトライ)
PYTHONPATH=src ./.venv312/bin/python tests/bot_comprehensive_eval.py \
  --exhaustive --concurrency 1 --quiet-success --progress-every 250 \
  --retries 8 --retry-base-sleep 5
```

評価対象は通常技・特殊技・必殺技・SAの発生、持続、硬直、ガードさせた側/ガードした側。
確定反撃候補は `punish_suggestion` を追加指定して別途評価できる。
2026-07-13 時点の全件結果: **9,728/9,728**
(発生/持続/硬直/攻撃側/防御側 各1,790 + 確反提案・判定保留778、100%)。
これは保存済みプロファイルどおりに回答し、ガード視点を取り違えず、条件不足時に
確反を断定しないことの評価である。
原典に固定値がない技は「データなし」「対象外」「状況依存」と回答し、数値を補作しない。

## 常駐化

Ollama (gemma4) が同じマシンで動いている必要がある。ローカル PC で動かし続けるか、
GPU/CPU リソースのある常時起動ホスト (EC2 等) に Ollama ごと載せる。

## 次の改善対象

- ガード硬直差が範囲/条件別の場合の保証確反と可能確反の分離
- SCリーチとUFD当たり判定を使う「発生は間に合うが届かない」候補の除外
- ヒット後のキャンセル・チェーン・空中状態を含む接続候補の決定論化
- AWS本番経路の全件評価はAPI Gatewayレート制限に合わせて分割実行する
