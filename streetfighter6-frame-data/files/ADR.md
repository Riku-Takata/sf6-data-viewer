# Architecture Decision Records (ADR)

このプロジェクトの主要な設計判断を時系列で記録する。**なぜそう決めたか**を残すことで、
将来の自分や他の貢献者が同じ議論を繰り返さずに済むようにする。

各ADRは以下の構造で記述する:
- **Context**: 何が問題だったか / 何を決める必要があったか
- **Decision**: 何を決めたか
- **Consequences**: その結果として何が起きるか / 何をトレードオフしたか
- **Alternatives considered**: 検討して却下した選択肢

---

## ADR-001: プロジェクトの3層構造

**Date**: 2026-04 (Layer 1 完了直後)
**Status**: Active

### Context
SF6のフレームデータを使った個人向けの「対戦アシスタント」を作りたい。スコープが
広すぎると挫折するので、段階的に進めたい。

### Decision
プロジェクトを以下の3層に分ける:
- **Layer 1**: データ収集パイプライン (CAPCOM公式のスクレイピング、DB保存、自動更新)
- **Layer 2**: 可視化・横断検索 (Web UI でのフレーム検索)
- **Layer 3**: 自然言語アシスタント (LLMによる対戦アドバイス)

### Consequences
- 各層を独立にデプロイできる
- 不確実性の低い順 (Layer 1) から進めることで、早期に「動くもの」が手に入る
- 結果的に Layer 2 はスキップして Layer 3 に統合する判断 (ADR-009 参照)

### Alternatives considered
- **一気にAIアシスタントを作る**: 設計の複雑さが見えず、データ整備の品質も低くなる懸念。却下。

---

## ADR-002: パッチ検知トリガー方式の採用

**Date**: 2026-04
**Status**: Active

### Context
Layer 1 のスクレイピング頻度をどう決めるか。CAPCOM公式は不定期にバランス調整パッチを
配信する。毎日全データを取りに行くのは無駄が多い。

### Decision
CAPCOM公式の「バトル変更リスト」ページを毎日1回チェックし、Updated 日付が変わった
時のみ全キャラのフレームデータを取得する。

### Consequences
- スクレイピング回数が年に数回〜10回程度に抑えられる (CAPCOMサーバーへの負荷最小)
- データ履歴が「パッチ単位」できれいに残る (`patches` テーブルが自然なバージョン軸)
- 検知ロジックは軽量 (1秒未満)、フルスクレイプ時のみ約3分

### Alternatives considered
- **週次フルスクレイプ**: 同値スナップショットが大量に残る。却下。
- **代表キャラのHTMLハッシュ比較**: パッチ表記より不正確。却下。

---

## ADR-003: AWS Lambda + Secrets Manager の採用

**Date**: 2026-04
**Status**: Active

### Context
Layer 1 のスクレイパーをどこで動かすか。

### Decision
- **実行基盤**: AWS Lambda + EventBridge (毎日 18:00 UTC = 03:00 JST)
- **認証情報管理**: AWS Secrets Manager (環境変数渡しではなく)
- **デプロイ**: AWS SAM CLI

### Consequences
- 月額コスト約 $0.40 (Secrets Manager のみ)、それ以外は無料枠内
- Service Role Key を本番運用環境にハードコードしない
- Logic Engine (Layer 2/3) は読み取り専用なので anon key で接続 (ADR-006 参照)

### Alternatives considered
- **GitHub Actions**: Git履歴が変更ログになる利点があるが、AWSエコシステムとの連携を優先
- **環境変数で認証情報**: 開発時のみ可。本番では Secrets Manager
- **SSM Parameter Store**: 無料だが、Secrets Manager の方が暗号化・ローテーション機能が手厚い

---

## ADR-004: Supabase の採用

**Date**: 2026-04
**Status**: Active

### Context
データベースとストレージをどう構成するか。

### Decision
- **DB**: Supabase (PostgreSQL 15+) の無料枠
- **Storage**: Supabase Storage (生HTML保管用、直近2世代ローテーション)
- **認証**: Service Role Key (Lambda書き込み) と anon key (Logic Engine読み取り) を使い分け
- **RLS**: 各テーブルに `public read` ポリシー設定済み

### Consequences
- 無料枠 (500MB) で年単位のデータが保存可能
- PostgREST API + Python SDK で直接アクセス
- Web公開時に anon key をフロントに埋めても比較的安全

### Alternatives considered
- **AWS RDS**: コスト高、運用負荷高
- **DynamoDB**: SQLが書けない、複雑なクエリで困る
- **SQLite + S3**: 同時書き込み・読み取りで詰まる

---

## ADR-005: スキーマ設計（時系列バージョニング）

**Date**: 2026-04
**Status**: Active

### Context
バランス調整パッチで技の数値が変わる。「過去のパッチでこの技は何Fだったか」を
追えるようにしたい。

### Decision
4テーブル + 1運用テーブル + 2ビューの構成:
- `characters`: キャラクタの静的メタ
- `patches`: パッチ単位の時系列軸
- `moves`: 技の論理ID (パッチを跨いで同じ技として識別)
- `move_snapshots`: 各パッチ時点のフレーム数値 (本体)
- `scrape_runs`: 運用ログ
- `move_latest` (view): 各技の最新スナップショット
- `move_diff_recent` (view): 直近2パッチ間の差分

### Consequences
- 「サガットの2HKは過去1年で何回変わったか」のような研究的クエリが書ける
- パッチノートの代わりに自動生成された差分が見られる
- フレーム数値はすべて `text` 型 (`5-7` や `D` などの非数値表現があるため)

### Alternatives considered
- **数値型カラム**: `5-7` のような範囲表記、`D` (ダウン) などの特殊値で破綻
- **JSONB単一カラム**: 一覧取得時のスキーマがゆるすぎる

---

## ADR-006: 正規化ビュー (move_normalized) による加工レイヤー

**Date**: 2026-04 (Layer 2 設計時)
**Status**: Active

### Context
`move_snapshots` の text 型カラムは生データとして正しいが、Logic Engine から
直接使うには都合が悪い (`5-7` を整数にしたい等)。

### Decision
PostgreSQL の正規表現関数で text → 整数 + bool への変換を行う SQL ビュー
`move_normalized` を作成。Lambda 側は触らない。

### Consequences
- スクレイパー側のコード無変更
- 計算列が増えてもビュー定義の修正だけで済む
- 範囲表記 (`-4～2` など) は最小値を採用 (安全側に倒す判定)
- `※` (註釈) があれば `has_conditional_note=true` を立て、LLM側で警告生成可能

### Alternatives considered
- **新テーブルで書き込み時に正規化**: Lambda側の変更が必要、テーブル再構築が必要
- **アプリ層でパース**: 毎回重い、キャッシュ管理が必要

---

## ADR-007: LLM戦略 (API先行 + 切り替え可能設計)

**Date**: 2026-04 (Layer 2/3 設計時)
**Status**: Active

### Context
LLM をローカル (Ollama+Gemma) で動かすかクラウドAPIにするか。

### Decision
- **本番運用**: Gemini Flash (またはClaude Haiku) - 月数円のコスト、応答 < 1秒
- **コード設計**: `LLMProvider` 抽象化により、後でローカルLLMに切り替え可能
- **ローカル開発時**: 環境変数で Provider を切り替えて Ollama も使える

### Consequences
- 個人利用の頻度なら API は実質無料
- ローカルLLMは「自宅PCで常時稼働」が現実的でない (PC占有問題)
- 将来 GPU環境が整えば 1ファイル追加でローカル切り替え可能

### Alternatives considered
- **完全ローカル**: 自宅PCの占有、応答 5〜15秒、SF6プレイ時にPCが重い
- **完全API依存**: 切り替え不可になる、ベンダーロックイン

---

## ADR-008: スコープを MVP-β (カテゴリ1〜4) に設定

**Date**: 2026-04
**Status**: Superseded by ADR-009

### Context
自然言語クエリに答えるシステムを、どの範囲まで作るか。

### Decision (当時)
MVP-β: ルールベースで機械的に答えられる4カテゴリに限定。
- カテゴリ1: 単一技照会
- カテゴリ2: 1対1の判定 (反撃)
- カテゴリ3: 連携検証 (gap analysis)
- カテゴリ4: 集計・ランキング

「戦術判断」(MVP-γ) はLLMの不得意領域なので除外。

### Status: Superseded
Layer 2 として Logic Engine を作る計画だったが、データ実態を調査した結果、
この設計だけでは不十分と判明 (ADR-009)。

---

## ADR-009: Layer 2 をスキップして Layer 3 に直接進む

**Date**: 2026-04
**Status**: Active

### Context
Layer 2 (カテゴリ1〜4 のルールベース判定) を実装中、ユーザーから
「ガードバックで届かない技を反撃候補に出してしまう」という指摘を受けた。

調査の結果、SuperCombo Wiki に以下の有用なデータが存在することが判明:
1. **数値データ** (Cargo API): `atkRange`, `punishAdv`, `perfParryAdv` など
   - CAPCOM公式にない情報多数
   - HTMLゴミ混じりだがパース可能
2. **システム文書**: Gauges, Movement, Offense, Defense など
   - ゲームの仕様 (Drive Gauge, Burnout, Block Pushback等) が体系的にまとまっている
3. **キャラページ本文の解説**: 各技に実戦的なコメント (例: サガットの2HKに「最大間合いではほぼ全ての足払いに反撃確定する」)

これらを統合すると、フレーム数値だけでなく**ゲーム文脈を理解したアシスタント**が
作れる。これは Layer 3 の本質的な能力。

### Decision
Layer 2 (簡易検索ツール) を作らず、最初から Layer 3 (RAG + Logic Engine + LLM)
を本格実装する方針に変更。

### Consequences
- 完成までの期間が長くなる (8〜9ヶ月、週8時間ペース)
- マイルストーン分割で進める (M1, M2, M3 各2〜3ヶ月)
- M1 完成時点で「自分用 SF6 知識ボット」が手に入る (M2/M3 進めるかは M1 後に判断)

### Alternatives considered
- **道B (Layer 2 を最小完成 → Layer 3 拡張)**: 動くものが早く手に入るが、
  二度手間になる可能性。Chestnutさんが「精度の高い完璧なもの」を志向しているため却下
- **道C (Layer 1 で一旦止める)**: モチベーション維持の観点で却下

---

## ADR-010: SuperCombo データ取得方針

**Date**: 2026-04
**Status**: Active

### Context
SuperCombo Wiki の Cargo API は通常のPython requests では Cloudflare に弾かれる
(403 Forbidden)。

### Decision
- **取得方法**: ユーザーがブラウザの開発者コンソールでJavaScriptスニペットを実行し、
  全キャラ分のJSONをダウンロードする方式 (手動)
- **更新頻度**: SuperCombo は人手更新で月1回程度の更新頻度なので、月1回手動更新で十分
- **倫理的位置づけ**: Cloudflare突破ライブラリ (cloudscraper等) は使わない。
  サイト運営者の意図を尊重する

### Consequences
- 自動化はできないが、更新頻度が低いので実用上問題なし
- 取得スクリプトは「ブラウザのセッションを使う」ため CloudFlare に弾かれない
- 将来 Web 公開する場合の倫理面・規約面でクリーン

### Alternatives considered
- **cloudscraper で技術突破**: 倫理的にグレー、将来の Web 公開で問題化
- **Claude in Chrome 等のブラウザ自動化**: 同上、より洗練された突破手段だが本質は同じ

---

## ADR-011: マイルストーン分割で進める

**Date**: 2026-04
**Status**: Active

### Context
道Aは8〜9ヶ月の長期プロジェクトになる。週8時間ペースだと「いつ完成するか分からない」
不安が挫折リスクを高める。

### Decision
プロジェクトを3つのマイルストーンに分割し、各マイルストーン完成時点で「使える状態」になる:
- **M1** (8〜10週): 基盤データ統合とコア検索 → 「自分用 SF6 知識ボット」
- **M2** (10〜13週): Logic Engineと推論 → 「本物のコーチング」
- **M3** (10〜15週): 実戦活用 → 「完成版 (Web UI 等)」

### Consequences
- M1完成時点で価値が手に入るので、M2/M3 進めるかは状況見て判断可能
- 「次に何をすればいいか」が常に明確
- 進捗を `PROGRESS.md` で可視化する (ADR-012)

### Alternatives considered
- **一気に設計して一気に作る**: 設計途中で挫折のリスク大
- **アジャイル週次スプリント**: 個人プロジェクトには重すぎる

---

## ADR-012: 進捗管理を PROGRESS.md で行う

**Date**: 2026-04
**Status**: Active

### Context
長期プロジェクトでは「今どこまで進んだか」が分からなくなり、セッション再開のたびに
コンテキスト読み込みコストが発生する。

### Decision
プロジェクトリポジトリのルートに `PROGRESS.md` を置き、各セッション終了時に
「次回やること」「現在の状態」を記録する。

### Consequences
- セッション開始時に `PROGRESS.md` を見るだけで状況把握可能
- Claude プロジェクト機能 (ADR-013) と組み合わせて、新しい会話でもコンテキストが引き継げる

### Alternatives considered
- **GitHub Issues**: 個人プロジェクトには重すぎる
- **記憶に頼る**: 1週間空くと忘れる

---

## ADR-013: LLM バックエンドを Ollama + Gemma に変更

**Date**: 2026-05-15
**Status**: Active (ADR-007 を部分的に Supersede)

### Context

当初 Gemini Flash API (ADR-007) を採用していたが、以下の方針変更が生じた:
- ローカル LLM (Gemma) を使いたい
- コストをゼロに近づけたい
- 将来的には AWS 上でセルフホストしたい

### Decision

- **プライマリ LLM**: Ollama + Gemma4:e2b (ローカル / EC2 Spot)
  - Gemma 4 Edge 2B: 7.2GB, 128K context, ~10GB RAM 必要
  - RAM が少ない場合のフォールバック: Gemma3:4b (2.5GB, ~4GB RAM)
- **埋め込みモデル**: nomic-embed-text (768次元, Ollama 経由)
- **コード設計**: LLMProvider 抽象化を維持し、`LLM_PROVIDER=gemini` で Gemini に戻せる
- **デプロイ戦略**: フェーズ別
  - 開発: `localhost:11434` (コスト$0、Mac の Ollama、Mac RAM 16GB+ 推奨)
  - 本番: AWS EC2 Spot **t4g.xlarge** (16GB RAM) ← Gemma4:e2b の最低限

### Consequences

- **コスト**: ローカル実行で推論コスト$0。EC2 Spot t4g.xlarge (~$0.04/hr) で月$3〜8程度
- **レスポンス速度**: Gemma4:e2b で CPU 推論 10〜30秒/クエリ (対戦準備には十分)
- **品質**: Gemini Flash より劣る可能性があるが、Intent Parser + RAG の組み合わせで補う
- **EC2 設定**: Ollama のポート (11434) はセキュリティグループで sf6_engine 実行元のみ許可

### AWS EC2 セットアップ手順 (参照用)

```bash
# 1. EC2 t4g.xlarge (ARM64 Ubuntu 22.04) を起動
#    インスタンスタイプ: t4g.xlarge (16GB RAM, 4 vCPU) Spot推奨
#    ※ Gemma4:e2b は ~10GB RAM 必要。t4g.large (8GB) では動作しない可能性大
#    セキュリティグループ: ポート22(SSH), 11434(Ollama)を自分のIPのみ許可

# 2. Ollama インストール (ARM64)
curl -fsSL https://ollama.ai/install.sh | sh
sudo systemctl enable ollama
sudo systemctl start ollama

# 3. モデルダウンロード
ollama pull gemma3:4b          # ~2.5GB
ollama pull nomic-embed-text   # ~274MB

# 4. 外部からアクセス可能にする
sudo systemctl edit ollama --force << 'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
EOF
sudo systemctl restart ollama

# 5. sf6_engine の .env を更新
# OLLAMA_BASE_URL=http://<ec2-ip>:11434
```

### Alternatives considered

- **Gemini Flash API**: 個人利用なら実質無料だが、API依存でネット必須

---

## ADR-014: 必殺技マッピング方針

**Date**: 2026-05-15
**Status**: Active

### Context

M1 では通常技18パターンのみ対応。必殺技・SA の質問 (タイガーショット等) には
「データなし」と返していた。M2 で必殺技にも対応する。

### Decision

- **Option C 採用**: `sc_moves.name` フィールドで ILIKE 部分一致検索
  - SuperCombo の `name` フィールドは英語 (Tiger Shot, Shoryuken 等)
  - 日本語技名 → 英語技名のマッピングテーブルを実装
  - 一致した名前の最初の1件 (最も一般的な強ボタン) を返す
- Intent Parser に `move_name` フィールドを追加 (特殊技名を格納)
- 日本語→英語マッピング: 約30件の主要必殺技をハードコード

### Consequences

- 「タイガーショット」→ High Tiger Shot (236MP) のデータを返せる
- 完全マッピングではないが、主要技はカバー
- 技名の表記ゆれ (タイガーアッパー / 昇竜拳タイプ等) は ILIKE で吸収
- 必殺技の CAPCOM データは M2 段階では未取込 → SC データのみで回答

### Alternatives considered

- **Option A (手動マッピングテーブル)**: 全技を手動登録 → 作業量大でスコープアウト
- **Option B (LLM変換)**: Gemma に技名変換させる → 信頼性低い
- **AWS Bedrock**: Gemma 未対応、Nova Micro でも月$0.5程度 (zero cost ではない)
- **AWS Lambda + 量子化モデル**: コールドスタート 30秒以上で実用的でない

---

## ADR-015: 必殺技検索をハードコーディングから DB 直接検索に移行

**Date**: 2026-05-19
**Status**: Active (ADR-014 を部分的に Supersede)

### Context

ADR-014 で採用した `_JP_MOVE_TO_EN` 約30件の日本語→英語マッピングテーブルは、
未登録の技 (Tiger Monolith, Nova Tiger, Jinrai Kick 等) でデータが取れない問題があった。
また、新キャラ追加のたびに手動でマッピングを更新する必要があった。

### Decision

- `_fetch_move_by_name()` の検索順序を以下に変更:
  1. **Special/Super タイプに絞って `sc_move_normalized.name ILIKE` 直接検索** (メイン)
  2. 単語分割して各単語で Special/Super 検索
  3. `_JP_MOVE_TO_EN` マッピング経由 (日本語技名フォールバック)
  4. タイプ不問で全技から検索 (コマンド通常技等)
  5. `raw_query` から `_JP_MOVE_TO_EN` を逆引き (LLM 誤訳リカバリー)
- `_pick_variant()` に強度修飾子の判別を追加:
  - 弱/中/強 → LP/LK, MP/MK, HP/HK (P系とK系の両方)
  - OD → KK/PP/LPMP 等のダブルボタン入力
  - 技名の直前にある強度語を優先 (「弱派生の弱」誤マッチ対策)

### Consequences

- 全30キャラの全必殺技が自動的に検索可能 (新キャラ追加時も対応不要)
- LLM が英語技名を正しく出力した場合、マッピングテーブル不要
- `_JP_MOVE_TO_EN` は LLM 誤訳 (瞬獄殺→Instant Kill 等) のリカバリーとして残す

### Alternatives considered

- **完全廃止**: `_JP_MOVE_TO_EN` を削除すると LLM 誤訳時に全滅するためフォールバックとして存続

---

## ADR-016: SNS + SSM による Layer 1 パッチ通知設計

**Date**: 2026-05-19
**Status**: Active

### Context

CAPCOM 公式でパッチ検知後、SuperCombo Wiki のデータ (必殺技フレーム等) も手動更新が必要。
しかし現状は Lambda の CloudWatch Logs を能動的に確認しなければ気づけなかった。

また、通知先メールアドレスをコード・設定ファイルに含めると git で公開されてしまう。

### Decision

- **通知手段**: AWS SNS (email プロトコル)
- **メール管理**: AWS SSM Parameter Store `/sf6/notification-email` に保存
  - コード・samconfig.toml いずれにもメールアドレスを書かない
- **サブスクリプション登録**: Lambda 実行時に SSM からメールを取得して動的登録
  - `_ensure_email_subscribed()`: 既登録チェック後に `sns:Subscribe` 発行
  - 初回のみ確認メールが届く (以降は自動)
- **通知失敗はエラーにしない**: `logger.warning` のみ、Lambda 全体はエラー扱いしない
- **samconfig.toml を .gitignore に追加**: `samconfig.toml.example` を git 管理用雛形として追加

### Consequences

- パッチ検知 → メール通知 → SuperCombo 手動更新という運用フローが確立
- メールアドレスは AWS SSM のみで管理 → git への個人情報漏洩なし
- SNS_TOPIC_ARN が空の場合は通知をスキップ (ローカル開発時に影響なし)

### Alternatives considered

- **メールを samconfig.toml に書く**: 簡単だが git に公開されるリスク
- **SNS サブスクリプションを CloudFormation で管理**: テンプレートにメールが含まれる → 却下
- **Slack Webhook**: 個人プロジェクトなので email で十分
- **Groq 無料枠**: Gemma 対応だが AWS ではない。フォールバックとして検討可

---

## ADR-017: 実装済み機能を AWS リモート MCP サーバとして切り出す

**Date**: 2026-06-08
**Status**: Active

### Context

当初は Bot 構築まで一気に進める計画だったが、すでに実装完了している
2 領域 — (1) Layer 1 の「CAPCOM 更新検知 → RDB 保存」、(2) M1〜M3 の
「フレームデータから技相性・確定反撃・コンボ・セットプレイに回答」— を、
いったん MCP (Model Context Protocol) サーバとして切り出して再利用可能にする。

現状の回答パイプラインは `parse_intent (LLM) → build_context (DB) →
generate_answer (LLM)` の 3 段で、Ollama/Gemini をサーバ内部に抱えている。

### Decision

- **LLM 段をサーバから外す**: `intent_parser` と `generate_answer` は MCP サーバに
  含めない。自然言語の解釈と回答生成はホスト側 LLM (Claude Desktop / 将来の Bot)
  が担う。MCP サーバは決定論的なロジック層のみを公開する。
  - 公開ツール: `lookup_move` / `check_punish` / `compute_setplay` /
    `analyze_combo` / `list_moves` / `search_system_docs` / `get_patch_status`
  - Resources: キャラ一覧・技一覧 (hallucination 抑制)
  - 既存の `handlers.lookup` / `combo_engine` / `setplay_engine` は LLM 非依存の
    ため薄くラップするのみ。
- **稼働先**: 最初から AWS リモート。API Gateway (HTTP API + トークン認証) →
  Lambda 上で FastMCP を stateless Streamable HTTP として公開。Layer 1 の
  SAM/IAM 資産を流用。
- **RDB**: Supabase (Postgres + pgvector) を維持。MCP サーバは読み取り専用。
  接続情報は SSM Parameter Store で管理 (ADR-016 のパターン踏襲)。
- **埋め込み**: `search_system_docs` の埋め込みを Ollama `nomic-embed-text` から
  Bedrock `amazon.titan-embed-text-v2:0` (ap-northeast-1) へ移行。
  - 次元が 768 → 1024 に変わるため、`doc_chunks` に `embedding_titan vector(1024)`
    を新設し 72 チャンクを再埋め込み (既存カラムは破壊しない)。
  - Lambda 実行ロールに `bedrock:InvokeModel` を追加。
- **Layer 1 は不変**: スクレイプ・書き込み・SNS 通知は別 Lambda のまま。

### Consequences

- Ollama 常時起動が不要になり、intent 誤分類リスクも消える (推論はホスト LLM)。
- どこからでも / 将来の Bot から MCP ツールとして呼び出し可能。
- 埋め込みモデル差し替えにより doc_chunks の再埋め込みが必要 (72 件のみ、軽微)。
- ベンダーロックを避けつつ AWS に集約 (Bedrock + Lambda + API GW + SSM)。

### Alternatives considered

- **ローカル stdio MCP**: Ollama 不要・ゼロコストだが Bot/リモート用途に届かない。
  → 将来必要なら同一 FastMCP コードを stdio transport でも併用可能。
- **LLM 段ごと MCP に内包**: 現行パイプラインをそのまま 1 ツール化する案。
  Ollama/Bedrock 依存と誤分類が残り、MCP の分業メリットを失うため却下。
- **RDB を AWS RDS/Aurora へ移行**: AWS 完結になるが月 $15〜40 のコスト発生。
  Supabase は実体が Postgres+pgvector で無料運用中のため現状維持。
- **Titan v2 を dimensions=768 で出力**: 既存カラム流用可だが再埋め込みは
  どのみち必須。次元混在を避けるため新カラム方式を採用。

---

## ADR-018: 必殺技の日本語名解決を DB 結合 + 対話学習に移行

**Date**: 2026-07-07
**Status**: Active (ADR-014/015 を部分的に Supersede)

### Context

必殺技の日本語名解決は `_JP_MOVE_TO_EN` / `_JP_SPECIAL_NAMES` のハードコード表に
依存しており、サガット約20件 vs 他キャラ0〜数件という偏りがあった。LLM (gemma4)
が日本語技名を正しい英語名に翻訳できない場合、これらのキャラでは解決に失敗する。

調査の結果、CAPCOM 側 (`move_normalized`) には全30キャラの必殺技/SA の
**公式日本語名** (強度prefix付き) が既に格納されており、欠けていたのは
「CAPCOM 日本語名 ⇔ SC input」の結合だけと判明 (unified_moves は通常技のみ
`capcom_to_numpad()` で結合)。

### Decision

1. **special_move_map テーブル新設**: CAPCOM 日本語技名 ⇔ SC input の対応表 (883件)。
   - 生成: `scripts/match_specials.py` — フレームシグネチャ (発生/ガード硬直/KD) の
     自動照合 + 強度prefix制約。曖昧・不一致は MANUAL_OVERRIDES (約120件) で確定。
   - recovery は照合に使わない (着地硬直のカウント方法が CAPCOM/SC で異なる)。
   - SC 側が旧パッチ数値の場合があるため、厳格一致→緩和一致の2パス方式。
   - 未対応 217件 (条件付き強化版等) は結合なしのまま許容。
2. **検索順序の変更** (`_fetch_move_by_name`): ステップ0 として special_move_map の
   日本語名 containment 検索を最優先に。LLM の翻訳を経由せず全キャラで一様に解決。
   `_JP_MOVE_TO_EN` は LLM 誤訳リカバリーの最終フォールバックとして残すが、
   今後の拡張は不要 (load-bearing でなくなった)。
3. **move_aliases テーブル + 対話学習ループ**: コミュニティ略称 (アパカ、フリッカー等、
   公式名でない呼び名) は Discord bot が聞き返しで学習する。
   - 技名未解決 + キャラ特定済み → 「コマンドを教えてください」と聞き返し
   - 返信のコマンドを MCP `register_move_alias` で実在検証 → 強度prefix を剥がして
     ファミリー単位で UPSERT → 復唱確認 → 元質問に即答
   - 強度解決は既存 `_pick_variant` に委ねる (1回の学習で弱/中/強/OD 全対応)
4. **MCP 読み取り専用ポリシーの限定緩和** (ADR-017 の例外): `register_move_alias`
   のみ move_aliases テーブルへ書き込み可。Lambda は SSM `/sf6/supabase-service-key`
   から service key を取得 (未設定なら登録ツールのみエラー、読み取り系は不変)。

### Consequences

- 公式日本語名 (ロン・ポワン、マネージュ・ドレ等) が全30キャラで LLM 翻訳なしに解決
- ハードコード表の拡張メンテが不要になり、略称は運用中に自動で蓄積
- 学習エイリアスは Supabase に保存されるため CLI / 他 MCP クライアントでも共有
- Discord は複数人が触れるため、コマンド実在検証 + 復唱で誤登録を抑止
- special_move_map は CAPCOM 技名の完全一致キーなので、パッチで技名が変わった場合は
  match_specials.py の再実行が必要 (頻度は低い)

### Alternatives considered

- **コマンド表記を主インターフェースにする**: ユーザーは技名で聞くのが自然。
  コマンドは補助経路 (既存実装) に留める。
- **bot が Supabase に直接書き込み**: bot の依存が増え、MCP dogfooding の趣旨に反する。
- **bot ローカルにエイリアス保存**: 学習成果が bot ホストに閉じ、移行時に失われる。
- **カタカナ⇔英語の自動翻字マッチング**: 信頼性が低い。フレームシグネチャ照合の方が
  データに基づく分、誤対応をレビューで捕捉しやすい。

---

## ADR-019: Ultimate Frame Data を補完ソースとして分離保存する

**Date**: 2026-07-10
**Status**: Active

### Context

CAPCOM公式のフレームデータは一次ソースだが、実戦上有用な全体フレーム・着地硬直・
キャンセル・パッチ由来の補足・当たり判定は十分に持たない。一方 Ultimate Frame Data
(UFD) は作者の実測値とHitbox Viewer由来のGIFをキャラ別に公開している。

両ソースの数値は更新時期や計測条件で差が出る可能性があるため、既存テーブルを上書き
すると出所と差分が失われる。またGIFのバイナリをPostgreSQLに格納すると、検索用RDBの
容量・バックアップ効率が悪化する。

### Decision

- UFDは `ufd_moves` へ別ソースとして保存する。CAPCOM / SuperCombo の行を更新しない。
- `sc_input` を解決できた行だけ既存の技照会に結合し、全体/持続/硬直/キャンセル/
  メモ/当たり判定URLを「Ultimate Frame Data 実測補足」として追加する。
- GIF本体はprivate Supabase Storage bucket `sf6-ufd-hitboxes` に保存する。DBには
  Storageパス、元URL、SHA-256、取得日時を保存する。
- Botからはprivate GIFを直接公開せず、公開元UFDのGIF URLを参照として示す。
  同一ソースURLの再インポートでは既存Storageオブジェクトを再利用する。
- 取得は公開HTMLをキャラ単位・待機付きで行い、Cloudflare回避などは行わない。

### Consequences

- 技の数値に食い違いがあっても、公式値を壊さず詳細な実測情報を回答へ使える。
- GIFをSupabase上にアーカイブしつつ、Bot利用者の閲覧は元公開URLに留められる。
- 新パッチ後はUFDインポーターを再実行して更新する必要がある。
- `ufd_moves` のDDLとAWS MCP Lambda再デプロイが導入時の追加運用になる。

### Alternatives considered

- **CAPCOMデータをUFDの数値で上書き**: 一次ソースと実測の境界が消えるため却下。
- **GIFをPostgreSQLのbyteaに保存**: DB容量と取得効率が悪く、Storageの責務と重複するため却下。
- **GIFをUFD URLだけ保持**: 元サイトのファイル更新・削除に備えられないため却下。

---

## ADR-020: コアフレーム回答を型付き統合プロファイルで決定論化する

**Date**: 2026-07-10
**Status**: Active

### Context

従来の技照会は、質問経路によって CAPCOM / SuperCombo のどちらを返すかが変わり、
UFD は補足テキストとして後付けされていた。また、各ソースの文字列から最初の整数だけを
取り出す処理では、CAPCOM の `13-38 13-15,30-38`、`24+着地後16`、`全体52`、
`-60※-93` の意味を失う。特に `全体52` を「硬直52F」と扱うことや、ガードした側の
値を LLM に符号反転させることは誤答につながる。

### Decision

1. `frame_data.py` をコアフレーム情報の唯一の照会サービスとし、CLI / MCP / Discord の
   全経路を `lookup_frame_data()` へ統一する。
2. 値は `単一値 / 範囲 / 条件別 / 段階別 / 複数持続区間 / 着地硬直 / 複合硬直 /
   総動作のみ / KD / ガード不成立 / 状況依存 / データなし` の型を保つ。生文字列と
   全ソース観測値も保持する。
3. 採用順はフィールド単位で CAPCOM → UFD → SuperCombo とする。ただし意味が違う値は
   採用しない。例: CAPCOM硬直欄の `全体52` は硬直値としては不採用にし、UFD/SCに
   硬直値があれば補完、なければ「硬直単独値なし」と返す。
4. `on_block` の保存・採用値は常に攻撃側視点とし、防御側視点はコードで機械的に
   符号反転する。単一値、範囲、条件別、段階別を反転し、ガード不成立・状況依存は
   数値へ変換しない。LLMに計算させない。
5. 同一入力の条件違いは、完全名一致を最優先し、フレームシグネチャは3項目以上一致かつ
   一意な場合だけ補助リンクに使う。未マッピングUFD完全名はUFD行をアンカーとし、
   同一技と証明できないCAPCOM/SC行を混ぜない。通常投げとターゲットコンボなど技区分が
   異なる行は照合対象外とする。既存マップで技ファミリーが確定している場合のみ、弱中強・
   OD・ホールドを入力から一意に解決する。`nj.HK` と `8HK` は同義入力として扱う。
6. 発生・持続・硬直・ガード両視点の回答文は決定論生成し、LLMはコア数値の選択・計算・
   転記に関与させない。3ソースに値がなければ明示的に「データなし」と返す。
7. キャラ単位の4ソース取得を5分TTLでキャッシュする。UFD再取込は安定したソースIDと
   既存GIFを再利用し、現行ページから消えた行のみ同期後に削除する。

### Consequences

- 質問表現やローカル/AWS経路によらず、同じ技は同じ採用値・出所・視点で回答される。
- 条件付き・空中・多段技を単一整数へ潰さず、確定反撃計算に使える値と説明専用値を
  区別できる。
- ソース差異はユーザー向け回答で必要な場合だけ示し、内部の自動検証JSONは表示しない。
- 全30キャラについて、ソース解決・型・視点・決定論回答 92,940 assertion と、Discord bot
  実経路8,950問を0失敗で検証する。今後のパッチ取込後も同じ監査を必須とする。
- 0失敗は「保存値を誤変換せず回答する」整合性保証であり、原典の数値網羅率100%を意味しない。
  CAPCOM上の攻撃行では、通常技578行は4項目すべて数値化済み。特殊技277行のガード差は
  265行が数値、7行がガード不成立、5行が未解決。必殺技830行は発生829、持続710、硬直796、
  ガード差742行が数値 (63行対象外、4行状況依存、21行未解決)。SA187行は発生180、持続171、
  硬直178、ガード差170行が数値 (9行対象外、8行未解決)。未収録値・未解決マッピングを
  「完成」と扱わず、監査レポートで継続管理する。
- 距離を含む確定反撃、ヒット後接続、条件別コンボはこのプロファイルを入力にする次段階で
  あり、本ADRだけで完成扱いにはしない。

### Alternatives considered

- **CAPCOM正規化ビューの先頭整数を常に採用**: 総動作・着地・条件別表記を誤解するため却下。
- **UFDでCAPCOMを上書き**: 一次ソースと実測値の出所が失われるため却下。
- **LLMにソース選択と符号反転を指示**: 同じ質問でも揺れ、視点誤答を防げないため却下。
- **入力だけを技の主キーにする**: 同じ入力のホールド・強化・スタンス条件を区別できないため却下。

---

## ADR-021: 質問状況・技同定・確反確度を独立した型として扱う

**Date**: 2026-07-13
**Status**: Active (アプリ・追加DBスキーマ・AWS MCP反映済み、未バックフィル)

### Context

単一技の基準フレームを正確に返せても、実戦の質問には距離、接触した持続F、立ち/しゃがみ、
空中、カウンター、バーンアウト、Drive Rush、画面端、連携の段数などが含まれる。従来は
これらが技名文字列へ混入するか Intent から失われ、条件付き数値のどの値に対応するかを
証明できなかった。また、技名の部分一致で複数強度・派生に当たっても先頭候補を採用し、
ガード硬直差だけでリーチを確認せず「確定反撃」と断定していた。発生Fだけで候補を取るため、
ジャンプ技や連携途中の技も反撃候補へ混ざっていた。

### Decision

1. 質問文から `scenario` を決定論抽出する。距離、接触持続、段数、相手状態、カウンター、
   防御側バーンアウト、Drive Rush、画面端、block/hit、攻撃側/防御側視点を技名と分離する。
   バーンアウト側など主語が不明なら `ambiguities` として聞き返し対象にする。正式技名中の
   `（遠距離版）` のようなvariantラベルは状況指定とみなさず、技識別子へ残す。
2. 技解決は `resolved / ambiguous / not_found`、解決手段、confidence、候補、根拠を返す。
   入力完全一致、正式名完全一致、部分一致、エイリアス推定を区別し、同名の強度・派生が
   複数なら暫定行を計算に使わない。
3. 条件適用後の値は `source_exact / derived_exact / derived_interval /
   conditional_unresolved / invalid_condition / data_missing / move_ambiguous` で表す。
   直接打撃の通常技で持続Fが明示され、基準値と持続が単一値の場合だけ、接触が1F遅れるごとに
   有利差が1F増える派生を許可する。先端だけで接触Fが不明なら区間を返す。飛び道具・特殊状態・
   構造化ルール未登録の補正は推測しない。
4. 確反は「時間」と「空間」を分離する。硬直差から `frame_punishable` と punish window は
   算出できるが、ガード後距離、押し戻し、反撃技の発生中リーチを証明できない間は
   `confirmed_punishable = null` とし、「フレーム上の候補（到達未検証）」と回答する。
   地上ニュートラルから直接出せないジャンプ技・連携途中技は候補から除外し、リソース・状態・
   リーチの未検証状態を候補ごとに付ける。
5. `lookup_move` / `check_punish` の MCP 引数へ任意の `scenario` を追加し、CLI RAG・AWS MCP・
   Discord local fallback を同じ `punish_service.py` へ統一する。
6. 既存ソース表を上書きせず、`contextual_frame_model_migration.sql` で次を追加する。
   - 正規技IDとソースリンク、variant単位の自然言語alias
   - 条件JSONと値型を持つフレーム観測
   - パッチ単位のシステムルール観測
   - 接触時の距離/状態/結果、GIFから抽出したフレーム別geometry
   - 技対技・状況別の直接確反実測
   - cancel/chain/link/juggleの技遷移と、状況別の直接コンボ実測
   追加テーブルはレビュー済み根拠を投入するまで空とし、未登録ルールをコード定数で補わない。

### Consequences

- 「先端」「最終持続」「相手バーンアウト」などを技名から失わず、回答がどの条件に対する
  ものかを機械判定できる。
- 現時点で安全に派生できる持続当ては計算し、それ以外は不足データ名まで示して保留できる。
- 従来「確反候補」と呼んでいた結果は、距離の証拠がない限り時間候補へ意味を訂正する。
- 追加DBマイグレーションは2026-07-13に適用済み。正規技IDのバックフィル、レビュー済み
  システムルール、UFD GIFのgeometry抽出、実測確反データは未完了である。よって距離込み
  確反を完成扱いしない。
- 同日にAWS MCP Lambdaへ再デプロイし、CloudFormation `UPDATE_COMPLETE` と本番scenario
  評価・条件付き確反の判定保留を確認した。
- ローカル実装は unittest 58件、全ソース統合監査92,940 assertion、Discord Bot実経路
  9,728問（うち確反提案・条件不足時の判定保留778問）をすべて0失敗で検証した。

### Alternatives considered

- **質問文をそのままLLMへ渡して条件推論**: 条件の欠落と数値補正の再現性を保証できないため却下。
- **先端ならSCリーチ値だけで到達判定**: SCの単一リーチ値はガード後間合い・押し戻し・
  反撃技の発生中移動を表さないため却下。
- **Burnout/DR補正をコードへ固定値で埋め込む**: パッチ・適用条件・出典を追跡できないため却下。
- **UFD GIFを見た目だけで即数値化**: 座標系・フレーム同期・pushboxを検証できない段階では
  誤った精密さになるため、assetとレビュー済みgeometryを分離する。
