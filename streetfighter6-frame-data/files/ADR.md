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

---

## ADR-022: 連携質問は共通タイムラインとレビュー済み観測で解く

**Date**: 2026-07-13
**Status**: Active (ローカル/AWS本番反映済み、追加DDL適用待ち)

### Context

単発技の立ち中Pを正しく検索できても、「立ち中P→立ち中Pに最速4F暴れしたら相打ち後は」
という質問に単発の発生6F・ガード+2Fだけを返しても答えにならない。必要なのは、1発目後の
行動可能時点、2発目と防御側技の発生、遅延、両者の到達、相打ち後のhitstun、そこからの
追撃を同一状況で評価することである。また、「発生4F技」だけでは技ごとのhitstun差を特定できず、
同時発生だけでは距離込みの相打ち成立も証明できない。

### Decision

1. `sequence_analysis` Intentを追加し、攻撃側技列、1発目のblock/hit、攻撃側・防御側の遅延F、
   防御側の発生Fまたはキャラ+技、想定結果を別フィールドで保持する。複数キャラ名は矢印と技の
   位置から攻撃側と暴れ側へ分離する。「ディレイ」の数値が無ければ0Fと推定せず確認を返す。
2. 発生・持続・硬直・ヒット/ガード差はADR-020の統合プロファイルを使い、CAPCOM公式を主値、
   UFD/SCを補完にする。SC固有の `hitstun / blockstun / hitstop / atk_range / notes` は生の補助根拠として
   併合し、統合値を上書きしない。
3. 1発目後の有利差から両者のactionable時点を置き、遅延+発生を加えたactive開始を共通タイムラインで
   比較する。activeが同じでも、距離・無敵・armor・projectile等が未検証なら `trade_if_both_reach`
   とし、時間計算と接触成立を分離する。
4. 相打ち後はパッチ、遅延、相手技、現在フレーム指紋が一致するレビュー済み観測を最優先する。
   無ければ同時の直接打撃に限り `攻撃側が与えたhitstun - 防御側が与えたhitstun - 1`
   をラベル付きモデルとして返す。相手技未指定なら該当技を1件ずつ計算し、分布と区間を残して
   単一値を作らない。
5. 追撃は `timing_connected / spatial_connected / state_connected / combo_confirmed` を独立させる。発生が
   間に合うだけなら「フレーム上の候補」とし、直接実測がある場合だけ `combo_confirmed=true` にする。
6. `sequence_observations` にイベント列、両視点有利差、確認済み追撃、条件、根拠、パッチ、レビュー状態を保持する。
   観測keyには相手キャラ+技を含め、数値または確認済み追撃を持つレビュー済み行では両方を必須にする。
   相手技IDのない過去の`+7F / 2MP`報告は未レビューの不完全証拠として残すが、回答には使用しない。
7. 連携解析の `summary` は決定論で完成文を生成し、Discord/CLIでLLMに再要約させない。技名・状況の
   解釈補助にLLMを使えても、数値計算と確度ラベルはコードが決定する。
8. SuperComboのrobots/Cloudflare等の制限を人間に擬態して回避する自動取得は行わない。利用条件の範囲で
   人手取得したJSON/HTMLスナップショットをレビューして取り込む。UFD GIFはassetの保存と、座標校正・
   フレーム同期済みgeometryを分離し、後者だけを到達証明に使う。

### Consequences

- 対象質問は `+2F` の読み上げで終わらず、共通6F目の同時発生まで計算する。相手技未指定では
  SCの4F地上通常技46件を個別計算し、サガット側`+6～+12F`の分布を返す。
- `Ryu 2LP`指定では `25 - 15 - 1 = +9F`、`Sagat 2LP`指定では
  `25 - 17 - 1 = +7F`となり、同じ発生4Fでも結果を分ける。
- `2MP`は時間上44/46技で接続するが、相手技未指定・距離未検証では確定追撃と断定しない。
- フレーム指紋がパッチで変わると旧観測は自動失効し、モデル区間または未解決へ落ちる。
- ローカルはunittest 79/79、観測JSON dry-run 1/1、Supabase実データE2Eで検証した。
  既存機能を含む全件回帰は、全ソース統合監査92,940 assertions / 0失敗、Discord Bot実経路
  9,728/9,728 / 0失敗である。
- `sequence_analysis_migration.sql` は未適用である。DBの観測照会404は捕捉し、相打ち後有利は
  SCの技別hitstunから計算できる。不完全な同梱観測は回答へ使用しない。
- AWS MCPは訂正版を再デプロイしてCloudFormation `UPDATE_COMPLETE`。ローカルフォールバックを
  無効化した本番E2Eで、汎用`+6～+12F / 2MP 44/46技`と
  `Ryu 2LP +9/-9 / 2MP猶予2F`を確認した。
- 現在は2技連携の直接打撃が対象。3技以上、無敵/armor/projectile/throw、cancel/chain/juggle、
  UFD GIF geometryによる自動到達証明は次段階である。

### Alternatives considered

- **フレーム表全体をLLMへ渡して答えさせる**: 時間軸、視点、相手技ごとの差、根拠確度が再現できないため却下。
- **「+2Fの後の6Fと4Fは相打ち」を固定テンプレート化**: パッチ、ディレイ、異なる連携に拡張できないため却下。
- **同じ発生Fの技は相打ち後も同じとみなす**: hitstun/hitstopが技ごとに異なるため却下。
- **時間上間に合う追撃をすべて確定コンボと呼ぶ**: 距離・状態・cancel可否を証明していないため却下。

---

## ADR-023: 技の集合質問は別名ではなく型付きフレーム条件として検索する

**Date**: 2026-07-13
**Status**: Active (ローカル全キャラ検証・AWS MCP本番反映済み)

### Context

「ラシードの技の中でガードさせて有利な技は？」のような質問は、特定の技名を解決する
質問ではない。従来の単一技fast pathは「技の中でガードさせて有利な技」を move_name として
`lookup_move` へ渡し、見つからない結果を別名学習の聞き返しへ流していた。その結果、質問条件
そのものを永続 alias として登録できてしまい、以後の技解決を汚染する危険があった。

単純なSQL整数フィルタも、CAPCOM/UFD/SuperCombo間のフィールド選択、ホールド等の技
バリアント、範囲値、条件別値、ガード不能を失わせるため、ADR-020/021の型付き統合
プロファイル契約に反する。

### Decision

1. `query_moves` IntentとMCPツールを追加する。条件は `field / operator / value /
   perspective / scope / scenario` の型付きフィルタで渡し、単一技の `move_name` と `input` は
   設定しない。初期対応フィールドは `on_block` とする。
2. `query_frame_data()` はキャラのCAPCOM・UFD・SuperCombo行を候補として列挙し、各候補に
   ADR-020の `lookup_frame_data()` と同じ統合・視点反転・scenario評価を適用してから比較する。
   データベースの正規化済み整数だけで判定しない。
3. 結果を `matches`（基準条件で確定）、`conditional_matches`（明示された技バリアントまたは
   全条件値で成立）、`unresolved`（範囲の一部だけ成立・条件未選択・未収録）の3区分にする。
   ガード不成立は対象外として数値条件に含めない。キャラが存在する0件検索は `found=true` とする。
4. 条件検索のsummaryは決定論生成し、Discord/CLIの回答段でLLMに再要約させない。
5. 別名学習の聞き返しは `lookup_move`/`check_punish` が `resolution.status=not_found` かつ
   `reason=move_not_found` を返した単一技だけに限定する。`register_move_alias` も集合表現を
   防御的に拒否する。

### Consequences

- 「ガードさせて有利」「通常技で+2F以上」「防御側が不利な必殺技」などを、数値と視点を
  取り違えずに検索できる。
- ホールド・強化・条件別の技と範囲値を基準値の結果へ混ぜず、ユーザーへ条件付き/保留を示せる。
- 集合検索0件、キャラ不明、通信エラーは別名学習を開始しない。
- 2026-07-13時点で、決定論Intent、統合プロファイル検索、MCP/Discord/CLI RAG接続、
  別名学習ガードを実装し、関連unittest 48件を通過した。2026-07-14に全30キャラの
  自然文→Intent→MCP引数→Supabase実データ検索を確認し、SAMでAWS MCPを再デプロイした。
  CloudFormationは `UPDATE_COMPLETE`、ローカルフォールバックを無効化した本番MCPの
  `query_moves(rashid, on_block > 0, attacker)` も成功した。

### Alternatives considered

- **質問文を技名として lookup_move に渡す**: 集合条件を解決不能な技名と誤認し、alias汚染を
  起こすため却下。
- **list_moves の文字列フィルタ後にフレーム値を比較する**: 条件値と採用ソース、視点反転の
  契約を回避するため却下。
- **LLMに全技一覧から選別させる**: 数値比較と条件ラベルが再現不能で、同じ質問の回答が
  安定しないため却下。

---

## ADR-024: SuperComboの時間派生値を実行時ソースから検証oracleへ移す

**Date**: 2026-07-14
**Status**: Superseded by ADR-028（独立検証の方法と結果は維持）

### Context

ADR-022の連携解析は、単発フレームの主値にCAPCOMを使う一方、相打ち後有利には
SuperCombo固有の `hitstun` を使う。SuperComboの手動取得を継続せず情報源を統一できるか、
SuperComboを正解ラベル、CAPCOM/UFDだけを入力にした独立ベンチマークで確認した。

全30キャラの基本地上通常技360件を、技名からの固定入力変換だけで対応付けた。CAPCOMから
`hitstun = active + recovery + on_hit` を計算すると、SCスカラーラベル304件中289件を計算でき、
280/289件 (96.89%) が完全一致した。式の入力値自体が同じ層では234/236件 (99.15%)。
Sagat 5MPとSC上の4F地上通常技46件の相打ち後ラベルは46/46件一致し、`+6～+12F`、
Ryu 2LP `+9F`、Sagat 2LP `+7F`をSC値なしで再現した。

一方、UFD単独のhitstun一致は230/263件 (87.45%) で、取得日がパッチ版を保証しないこと、
行内でtotalとstartup/active/recoveryが一致しない例があることを確認した。また、現行の
`hitstun差 - 1` とhitstop相殺方針は、SC由来値を同じ式へ戻すテストしかなく、ゲーム内の
独立観測では未校正である。

### Proposed decision

1. 単純な第1持続接触の直接打撃では、CAPCOMの型付き統合値から `total / hitstun /
   blockstun / punishAdv / afterDRHit / afterDRBlk / perfParryAdv` を決定論で派生する。
   条件別硬直、多段、飛び道具、KD、空中・強化状態は適用対象外または区間とする。
2. SuperComboの上記スカラー列は、対応範囲ではランタイム入力ではなく、オフライン回帰の
   正解ラベルへ役割変更する。SCテーブルを読むと失敗するsource-isolation testを追加する。
3. UFDの数値補完は、同一パッチまたは行内フレーム恒等式を確認できた場合だけ採用する。
   衝突時はCAPCOMを優先し、欠損を無条件にUFDで埋めない。
4. `hitstop / atkRange / geometry / invuln / armor / projectile / juggle / notes` は時間の
   基本4項目から生成しない。hitstopはsystem ruleまたは実測、距離は校正済みUFD geometry、
   戦術解説は文書ソースとして分離する。
5. 相打ち後式のoffsetとhitstop方針は、相手キャラ+技まで固定した20～50件のフレームステップ
   実測でblind検証する。完了までは `calculation_model` の保証レベルを維持する。
6. 本提案をActiveにする条件は、同一パッチの再監査、SC読み取り禁止E2E、例外ルールの型化、
   相打ち式の独立実測をすべて通過することとする。

### Consequences

- 相打ち後の時間差と追撃タイミングは、対応範囲でCAPCOM中心へ統一できる。
- SuperComboの手動更新はランタイム必須運用ではなくなり、必要時の回帰検証へ頻度を下げられる。
- CAPCOM/UFD/SCの版ずれを、推論誤差として誤集計しない監査契約が必要になる。
- 空間・状態・特殊相互作用・戦術文書は別の観測層として残り、全情報源を一つのフレーム表へ
  潰すことはしない。
- 詳細な方法と結果は `streetfighter6-engine/docs/SUPERCOMBO_INFERENCE_AUDIT.md`、再実行は
  `streetfighter6-engine/tests/supercombo_inference_audit.py` に記録する。

### Alternatives considered

- **SuperComboを即時削除**: hitstop、距離、例外注記、特殊状態の代替がなく、相打ち式自体も
  未実測なので却下。
- **UFDで全欠損を埋める**: パッチ識別と行内整合性が不足し、今回の生一致率を下げたため却下。
- **SC内部の式一致だけで移行判断**: 入力と正解が同じソースになり循環するため却下。
- **機械学習で例外を補間**: 1F単位の保証と新キャラ・新パッチへの一般化を証明できないため、
  まず物理式と明示的な例外ルールを優先する。

---

## ADR-025: CAPCOM備考を型付きclaimへ変換し、SuperCombo非依存ランタイムを構築する

**Date**: 2026-07-14
**Status**: Superseded by ADR-028（監査結果と型付きclaim設計は維持）

### Context

ADR-024では、基本地上通常技の時間派生値をCAPCOMから96.89%再現できることを確認した。
残る不一致について、CAPCOM公式の備考・属性、UFD、距離、無敵、armor、飛び道具、juggle、
空中状態、SC戦術notesまで再監査した。

CAPCOM 2,357行中、備考は1,781行 (75.56%)、属性は2,032行 (86.21%) に存在した。
公式の結果別硬直 `N F増加/減少` は197行・209 claimを決定論抽出できる。通常技の
hitstun/blockstun/total不一致31セル・24技では、CAPCOM備考だけで7セルを完全補正し2セルを
部分補正でき、UFDの独立条件まで加えると計15セルの原因を特定できた。12セルは現在の
取得データではSCだけが条件を持ち、4セルはSC自身の主要値でも未整合だった。

不一致の直接原因は主に結果別recovery、接触phase、固定ガード回復、variant identityである。
距離・無敵・armor・飛び道具・juggle・空中状態は原則、接触成立と結果状態を選ぶgateであり、
時間式へ直接加算する値ではない。CAPCOM属性から飛び道具の存在は高coverageで判定できるが、
数値range、弾速、juggle tupleは公式表だけから一意に復元できない。

### Proposed decision

1. SuperComboをproductionの技同定・検索・推論・説明から切り離し、移行中だけ別DBのoffline
   oracleとして回帰評価に使う。production credential、schema、build artifact、query logの
   全てでSC readを禁止する。
2. `game_versions / source_snapshots / source_records` を追加し、CAPCOM raw HTMLとUFD assetを
   patch・SHA・parser version付きで不変保存する。計算時はlatestではなくtarget versionを固定する。
3. canonical技IDをSCのinput、技名、フレームシグネチャから生成しない。CAPCOM公式command、
   UFD自身のinput、review済みaliasから `canonical_move_versions` を構築する。
4. 原典値 `move_facts`、備考原文に対応する `note_claims`、review済み `rule_versions`、全入力を
   追跡する `derived_proofs` を分離する。導出入力が変わればproofをstaleにする。
5. `recovery_by_result / recovery_trigger / active_segments / contact_phase / variant_state /
   result_state` を型付きにする。単純hitstun式は、適用predicateが全て成立する場合だけ実行する。
6. 備考parserはraw spanを保存し、決定論grammarとgolden testを通った狭いclaimだけ
   `executable=true` にする。LLM抽出結果や曖昧文を直接ruleとして実行しない。
7. 距離は校正済みgeometry、相打ちoffsetとhitstopは絶対frameの独立観測で補う。patch不一致、
   branch未選択、証拠競合、適用外状態では推測せず `unresolved + reason_codes` を返す。

### Activation gates

- immutable snapshot、同一patch、SC由来mapping 0件
- note grammarの監査済み範囲でprecision 100%
- 全導出値にrule version・input hash・proofがありstale 0件
- tradeのblind holdoutで採用ruleが0F誤差
- SCへ接続不能な状態でCLI/MCP/Discord/combo/setplayの全E2E通過

### Consequences

- CAPCOM公式を中心に情報源とpatchを統一しつつ、公式が公開していない数値を捏造しない。
- SuperComboの手動更新は本番運用から不要になる。独立golden corpusが整えばoffline oracleも廃止できる。
- 完全なfield parityより、根拠付きの確定値と明示的な保留を優先する。
- 詳細は `streetfighter6-engine/docs/SUPERCOMBO_CONTEXT_AUDIT.md`、再実行は
  `streetfighter6-engine/tests/supercombo_context_audit.py` に記録する。

### Alternatives considered

- **CAPCOM備考を全文LLM解釈して即実行**: actor・条件・基準branchを誤ると1F単位の保証がなくなるため却下。
- **SCの条件値だけ新テーブルへコピー**: 出典を隠した依存が残り、source isolationにならないため却下。
- **不足range/juggleを多数派defaultで補完**: 高coverageに見えても個別技の真値を証明しないため却下。
- **SCを本番DBへ残してflagで無効化**: service roleや別経路からのreadを検知できないため却下。

---

## ADR-026: 会話からの「学習」は型付き知識の段階的公開として実装する

**Date**: 2026-07-14
**Status**: Proposed（会話知識の安全設計は維持。SuperCombo非依存化の前提だけADR-028で取り消し）

### Context

SuperComboをproduction runtimeから外しても、戦術notesが表していた連携結果、距離、狙い、弱点、
counterplayは有用である。利用者や開発者が会話で報告した状況を保存し、今後の質問へ再利用できれば、
手動管理された単一Wikiに依存せず知識を更新できる。

しかし現行`parse_intent()`は各発話を単独処理し、Discordの状態は技別名の聞き返しだけである。
追加プローブでは、否定、仮説、伝聞、訂正、前ターン照応を含む期待10件中1件だけが一致した。
さらに`sequence_observations`はpatchとconditionsを検索時に照合せず、証拠なし・unknown patchでも
`reviewed=true`を受理し、同confidenceの競合を入力順で選ぶ。既存表へユーザー投稿を直結できない。

現行MCPは単一Bearer token、Lambdaはservice-role keyを持ち、alias・contextual・sequence表は
public-readであるため、本人限定メモと共有知識をRLSで分離することもできない。

### Proposed decision

1. 「学習」はモデルのオンラインfine-tuningではなく、会話から抽出した型付き知識をRAGで
   蓄積・訂正・失効・撤回することと定義する。
2. `session working memory / confirmed private knowledge / reviewed shared knowledge`を分離する。
   private保存と共有には別々の明示同意を要求する。
3. 質問Intentとは別に、speech act、照応、state operation、polarity、epistemic basis、attribution、
   critical unknownを持つ`DialogueTurnAnalysis`を導入する。LLMは候補抽出だけを行い、保存、権限、
   review、数値計算を決めない。
4. raw conversation、typed scenario、knowledge claim、evidence、relation、revision、review、consent、
   embedding、audit、deletionを別テーブルにする。raw発話をsystem instructionとして扱わない。
5. workflow (`draft -> clarification -> confirmed_private -> review_pending -> approved_shared`) と
   validity (`active -> disputed/stale/superseded/withdrawn/deleted`) を分離する。
6. scenario keyには結果値を含めず、patch、canonical move version、距離、corner、状態、delay等の
   条件を含める。同条件の異なる結果はconflict setへ入れ、last-write-winsで選ばない。
7. 公式fact/決定論導出、review済み独立観測、review済み戦術、本人privateメモの順に証拠を分ける。
   下位claimは上位factを上書きせず、privateメモは本人にだけ未検証と帰属表示する。
8. 全claimにgame version、canonical move version、依存fact/rule、dependency fingerprintを持たせる。
   patchまたは依存値が変わればstaleへ落とし、旧行を書き換えず新revisionでcarry-forwardする。
9. 単一Bearer/service-roleのuser-facing経路を廃止し、主体付き短命tokenとRLSを導入する。
   ingestion、parser、review、answerを別credentialにし、answer runtimeはeligible viewのSELECTだけを持つ。
10. 既存`move_aliases`のglobal SC-family UPSERTは廃止し、canonical move versionを指すalias candidateを
    同じreview経路へ移す。SC文書用`doc_chunks`へユーザー戦術を混在させない。
11. SuperComboは別環境のoffline oracleに限定し、productionの知識は公式備考、patch整合UFD asset、
    独立実測、開発者note、同意済みユーザー投稿から構築する。
12. 実装は、SC非依存事実基盤、read-only context compiler、private memory、review workflow、
    answer統合の順に段階導入する。各段の安全gateを通すまで次のwrite/public機能を有効化しない。

### Activation gates

- 会話180件+否定minimal pair 120組のfrozen評価を構築する。
- scenario slot F1 0.97以上、照応exact 0.95以上、polarity F1 0.99以上、epistemic F1 0.95以上。
- 曖昧時abstention precision 0.99以上、critical unknown recall 0.98以上。
- cross-user leak、質問/仮説/伝聞の誤昇格、未review global利用、公式上書き、injection write、
  stale混入、last-write-wins、訂正/削除後残存を全て0件にする。
- private raw/claim/embedding/asset、review権限、answer read-only credentialのRLS統合テストを全件通す。
- SC credential/table/artifactへ接続不能な状態で会話保存・検索・回答E2Eを通す。

### Consequences

- `conversation_knowledge.py`、`conversation_service.py`、`knowledge_repository.py`、専用migrationを
  ローカル実装した。既定repositoryはdisabledであり、raw会話やDiscord IDを永続化しない。
- Discordには同一会話・同一主体だけの短期照応、明示保存確認、本人限定private表示を統合した。
  SC依存のglobal alias即時学習は環境変数で明示有効化するまで停止する。
- migrationは未適用で、AWS/MCPのデプロイも未実施。主体JWT/RLSの統合テストと180件のfrozen評価を
  通過するまでshared knowledgeは有効化しない。
- 利用者の戦術報告を、本人メモとして即時に役立てつつ、未検証情報を他人の確定事実へしない。
- 訂正、反証、撤回、patch更新を履歴付きで反映でき、誰のどの条件の情報かを回答で表示できる。
- conversation context、identity/RLS、review UI、削除処理が必要となり、単純なvector store追加より
  実装量は増える。
- 現行`sequence_observations`は条件・patch・競合照合を修正するまで公開知識の直接保存先に使えない。
- テスト専用状態機械では、同意、ユーザー分離、review、注入隔離、訂正、競合、失効、撤回の
  18不変条件を18/18通過した。詳細は`docs/CONVERSATIONAL_KNOWLEDGE_DESIGN.md`、再実行は
  `tests/conversational_knowledge_design_eval.py`に記録する。

### Alternatives considered

- **ユーザー発話ごとにLLMをfine-tuning**: 削除、patch失効、出典、ユーザー分離を即時反映できず、
  poisoning除去も困難なため却下。
- **発話をそのままvector DBへ保存**: 条件、否定、話者、確度、権限を検索時に保証できないため却下。
- **ユーザー投稿を`sequence_observations`へ直接INSERT**: 現行の条件/patch未照合とreview検証不足により却下。
- **複数投稿またはconfidence閾値だけで自動公開**: 同一動画転載、sybil、条件違い、伝聞を独立検証と
  誤認するため却下。
- **SC戦術notesをproduction知識へコピー**: 出典を隠した依存が残り、SC非依存の目的を満たさないため却下。

---

## ADR-027: UFD GIFはSupabaseに常設保存せず元URLからオンデマンド取得する

**Date**: 2026-07-14
**Status**: Active

### Context

UFD当たり判定GIF 773件は4,207,176,129 bytes（約3.92 GiB）あり、
Supabase Storageの1GB枠を大幅に超過した。全オブジェクトが現行DB行から参照され、
孤児0件、SHA-256重複0件であったため、不要ファイルの整理だけでは解消できない。
Botはprivate Storageを配信せずUFD元URLを表示しており、GIF本体は現行の回答経路に必須ではない。

### Decision

- UFD GIFは既定でSupabase Storageへ保存しない。インポータの `--gifs` 指定時だけ保存する。
- `ufd_moves.hitbox_source_url` は保持し、Botと将来のgeometry解析は元URLから参照・一時取得する。
- 既存773件は技メタデータ、元URL、SHA-256、サイズをローカルmanifestに保存後に削除する。
- 削除後は無効な `hitbox_storage_path` と `hitbox_sha256` をNULLにし、元URLは残す。
- 将来、原典消失に備えた全件アーカイブが必要になった場合は、Supabase無料枠ではなく
  容量に適した別オブジェクトストレージを選定する。

### Consequences

- Supabaseから4.21GB分を解放し、UFD再同期での自動再アップロードを防止する。
- フレーム回答とUFD元GIFリンクは維持される。
- geometry解析はネットワークと原典の可用性に依存するため、解析実行時に一時キャッシュと取得失敗の記録が必要になる。

---

## ADR-028: SuperComboをCC BY-NC-SA 3.0の条件に従って本番データソースとして維持する

**Date**: 2026-07-14
**Status**: Active

### Context

SuperCombo Wikiのデータは、2026-07-14に利用者がサイト表示を確認した
[Creative Commons Attribution-NonCommercial-ShareAlike 3.0 Unported](https://creativecommons.org/licenses/by-nc-sa/3.0/)
（CC BY-NC-SA 3.0）の条件下で利用可能と確認した。ライセンスはクレジット表示だけでなく、
ライセンスへのリンク、改変の表示、非営利利用、派生データの同一または互換ライセンスによる
共有を求める。

これまでのADR-024/025では、出典分離と独立検証を強めるためSuperComboを
offline oracleに限定する案を検討した。しかし、ライセンス条件を満たして利用できるため、
データの精度・カバレッジと現行機能の維持を優先する。

### Decision

1. SuperCombo Wikiを引き続き production runtimeの補助データソースとして使用する。
2. CAPCOM公式を主値、UFD / SuperComboを補完値とする現行の出典分離と型付き統合を維持する。
3. リポジトリと利用者向け配布物に次の帰属情報を表示する。
   - データソース: SuperCombo Wiki / SuperCombo Wiki contributors
   - 参照先: https://wiki.supercombo.gg/w/Street_Fighter_6
   - ライセンス: CC BY-NC-SA 3.0とそのリンク
   - 改変内容: HTML/MediaWikiマークアップ除去、数値正規化、入力表記変換、CAPCOM/UFDとの統合
4. SuperCombo由来データとその派生データの利用は非営利に限定する。商用化する場合は、
   SuperCombo由来データを分離するか、権利者から別途許諾を得るまで公開しない。
5. SuperCombo由来の改変データを配布する場合はCC BY-NC-SA 3.0または互換ライセンスで提供する。
   プロジェクトのソフトウェアコード全体にCCライセンスを適用するとは限らず、対象データとコードを区別する。
6. 取得時はrobots.txt、Cloudflare、レート制限等を回避せず、現行の人手取得・低頻度更新方針を維持する。
7. 公開前に、対象のSuperComboページと個別メディアに別ライセンス表示がないことを再確認する。

### Consequences

- SC由来の `hitstun / blockstun / hitstop / atk_range / notes`と技名マッピングを引き続き活用できる。
- ADR-024/025の独立監査はデータ精度とパッチ整合性を検証する回帰テストとして維持する。
- ライセンス条件の対象はSuperCombo由来データとその派生物であり、CAPCOM/UFD由来データの権利を覆うものではない。
- この判断は法的助言ではない。利用形態が営利に変わる場合やライセンス範囲が不明な場合は再確認する。

---

## ADR-029: 連続ガードと割り込みは技間遷移種別ごとのタイムラインで判定する

**Date**: 2026-07-14
**Status**: Active（ADR-033でSA/汎用弱攻撃chainと全技DB解決を追加。DR/個別windowは未対応）

### Context

ユーザーが求める「ケン `2MK -> 中迅雷脚`は連続ガード、`2MK -> 強迅雷脚`は
4F技で割り込み可能」という回答は、単発のガード硬直差だけでは求められない。

現行`sequence_analysis` は1技目のrecovery後に2技目を出すlinkのみを、
`attacker_ready = max(0, -on_block)` で評価する。cancelは1技目のrecoveryを打ち切るため、
この式を使うと中迅雷脚も強迅雷脚も誤判定する。

2026-07-14の実DBには、SuperCombo由来のKen `2MK blockstun=16F`, `cancel=Sp SA`、
UFD/SCで一致する`236LK=12F`, `236MK=16F`, `236HK=25F`がある。標準的な最速
special cancelなら、弱は-4F、中は0Fで連続ガード、強は9Fの行動可能時間となる。

同時に次の実装不備も確認した。

- 自然文の「2中K」「中/大迅雷脚」「連続ガード」は決定論sequence intentにならない。
- `rag_builder` の旧派生gap計算はblockstunでなく `abs(block_adv)-startup` を使う。
- productionのgeneric 4F経路は `_fetch_defender_profiles()` に未対応引数を渡しTypeErrorになる。
- `canonical_moves`, `move_transition_observations`, `combo_link_observations` は本番DBで0行である。

### Decision

1. 技Aと技Bの間に遷移種別を必須とし、現時点では `link / cancel` を分離する。`chain /
   drive_rush_cancel / target_combo / stance_followup / juggle / unknown` は個別根拠の導入時に追加する。
2. linkは従来のon_block/on_hit式、cancelはblockstun/hitstunとcancel開始基準、chainと専用派生は
   個別windowで計算する。遷移根拠がない場合は他種別の式へfallbackしない。
3. cancelの基準式はhitstop終了後を共通基準にし、
   `target_active = transition_offset + delay + startup`,
   `defender_actionable = blockstun + scenario_modifier` とする。
4. `target_active - defender_actionable <= 0` は `true_blockstring`、gapがある場合は指定された
   防御側技のfirst activeと比較し、`interrupt_timing_win / interrupt_trade_if_reach /
   frame_trap` を返す。
5. genericな「4F技」では時間上の割り込み可否までを返す。実際の成功は相手キャラ+技、
   距離、pushback、当たり判定、無敵/armor/投げ/飛び道具相互作用を評価できた場合だけ
   `interrupt_confirmed=true` とする。
6. 遷移根拠はpatch一致のreview済みexact edge、CAPCOM/UFD/SCのcategory rule、
   SCの専用`A~B` edgeの順に使う。構え・空中・溜め・hit-only等は個別edgeなしに推測しない。
7. Intentは「2中K/屈中K/2MK」「弱中強大+技名」「連続ガード/割り込める/暴れられる/
   フレームトラップ/隙間」を決定論正規化する。
8. 旧`rag_builder` gap計算を廃止し、CLI / MCP / Discord / RAGの全経路を1つの
   blockstring serviceへ統一する。

### Activation results (2026-07-14)

- [x] productionのgeneric 4F `analyze_sequence` 経路の引数不整合を解消し、実DBと更新済みAWS MCPで確認した。
- [x] Ken `2MK -> 236LK/MK/HK`のgolden testで、弱/中は連続ガード、強はgap 9F・generic 4Fが5F先にactiveとなることを固定した。
- [x] 「2中K→中迅雷脚」「2中K→大迅雷脚は発生4Fで割り込める？」をLLMなしで同じ正規技ID・遷移へ解決した。
- [x] 旧gap式を全回答経路から削除し、special targetの遷移根拠不足時はlinkとして計算せず保留する。

### Remaining gates

- [x] 全キャラの保存済み技名と非composite ordered pairを全件監査し、未検出・曖昧名・scalar欠損を分離した。
- 20〜50件をトレーニングモードのframe stepでblind検証し、off-by-one規約と0F誤差を確認する。
- patch変更で遷移edgeまたは依存frame値が変わった場合に旧結果をstale化する。

### Consequences

- 現行データだけでKenの基準ケースの時間判定は実装可能である。
- 「連続ガード」「押せるが潰される」「相打ち」「割り込み側が先」を混同せず回答できる。
- SuperComboのblockstun/cancel/notesを活用しつつ、原典値、遷移ルール、距離・状態・実測の確度を分離できる。
- 詳細設計と検証順序は `streetfighter6-engine/docs/BLOCKSTRING_ANALYSIS.md` を正とする。

---

## ADR-030: 専用派生はsource-input edgeとしてレビューし、直接根拠だけを自動実行する

**Date**: 2026-07-14
**Status**: Active

### Context

SuperComboの`input`にある`A~B`は、target combo、連打、構え派生、必殺技後派生などを同じ表記で
含む。2技目の`startup`は派生ボタン入力からの値であり、1技目のblockstun/recoveryやgeneric special
cancel式にそのまま代入しても、技間の隙間にはならない。特に強度・派生・状態によってwindowが異なる
ケースでは、`Chn`や同じ入力familyだけから対象技を推測すると誤判定する。

2026-04-26のSuperCombo snapshotを監査すると、30キャラに419個のsource-input edge候補がある。
注記からblock上の`Nf gap`または`true blockstring`を直接読めるのは71件、派生window等のreviewが
必要なものは330件、同一edgeの値が競合するものは7件、親技が同snapshotで特定できないものは11件だった。

### Decision

1. `A~B`入力は通常link/special cancelとして評価しない。まず専用edgeとして分類する。
2. SuperCombo注記がblock上の`Nf blockstring gap`または`true blockstring`を直接述べる場合だけ、
   `defender_actionable`基準の`direct_block_note`として実行する。`true blockstring`はgap `<= 0`を
   意味するが、根拠にない負の正確な数値は作らない。
3. 明示された強度（例: `236HK`）の根拠は、`236MK`/`236LK`へfamily matchで流用しない。generic表記
   （例: `236K`）だけが同familyの候補となる。
4. `sql/source_transition_rules_migration.sql`のsource-addressable tableを、canonical move backfill前の
   永続レビュー先とする。runtimeはpatch付きの`reviewed=true` exact edgeを最優先にする。
5. `importers/source_transition_rules.py`は全キャラ候補を生成し、直接根拠候補だけを`reviewed=false`で
   stageできる。未review行、値競合、親技不足、window未収録はruntimeで使わない。
6. `Chn`はカテゴリ情報に留め、最大コンボ探索で任意の高速通常技を「99F有利で繋がる」とする旧推測を
   廃止する。review済みhit edgeが導入されるまでは通常のhit advantageのみで探索する。

### Consequences

- A.K.I. `5LP -> 5LP~LP`の3F gap、豪鬼`214HP -> 214HP~6P`の連続ガードのような、直接記載された
  全キャラの派生情報をBot/MCPで安全に回答できる。
- Ken `236MK -> 236K~6LK`のように派生入力自体は分かってもblock timingが明記されない連携は、
  「判定保留」と理由を返す。誤った「連続ガード」または「割り込み可」を出さない。
- migrationを適用しなくてもdirect-note fallbackで既存Bot/MCPは動作する。migration適用後は、
  実測でreviewされた例外・patch差分がコード変更なしで優先される。
- 残る331候補はトレーニングモードframe-step、CAPCOM/UFD根拠、または信頼できる一次資料で
  reviewする必要がある。自動的な全件確定は行わない。

---

## ADR-031: 生HTMLアーカイブは既定で保存しない

**Date**: 2026-07-14
**Status**: Superseded by ADR-032

### Context

Supabase Storageの棚卸しでは、過去に削除したUFD GIF 773件が4,207,176,129 bytes、
現行のCAPCOM生HTMLアーカイブが60件・24,436,612 bytesを占めていた。Botの実行時は
正規化済みのPostgreSQLデータを読むため、`move_snapshots.raw_html_uri` の生HTMLコピーを必要としない。
一方、スクレイパーが既定で `current/` と `previous/` を保管し続けると、調査用データが無期限に残り、
Storage使用量を再び増やす。

### Decision

1. `sf6-html-archive` の全60件を削除し、4,637件の `move_snapshots.raw_html_uri` をNULLにする。
2. `ARCHIVE_RAW_HTML=false` をLambda/SAMテンプレートの既定値とする。この状態ではStorage APIを呼ばず、
   新しいsnapshotの `raw_html_uri` はNULLで保存する。
3. HTML構造の障害調査など必要な期間だけ `ArchiveRawHtml=true` を明示して有効化し、完了後にfalseへ戻して
   アーカイブを削除する。
4. 削除前に、オブジェクトのパス・サイズ・時刻とDB参照数をmanifestとしてローカル保存する。

### Consequences

- ランタイムのBot回答やフレームデータの更新処理は影響を受けない。
- 生HTMLを直接参照するデバッグでは、一時アーカイブまたはローカルで再取得したHTMLが必要になる。
- Supabaseの使用量はGB時間の請求指標のため、削除後に管理画面の警告が即時に消えるとは限らない。

---

## ADR-032: 生HTMLアーカイブを復元し、原因と無関係な削除を行わない

**Date**: 2026-07-14
**Status**: Active

### Context

削除前のStorage API棚卸しでは、`sf6-html-archive` は60件・24,436,612 bytesで、
`sf6-ufd-hitboxes` は0件だった。したがって、HTMLアーカイブは1GB超過警告の直接原因ではない。
過去に削除したUFD GIF約4.21GBの期間使用量、または管理画面上の別の使用量指標を先に確認すべきであり、
この時点でHTMLを削除する根拠はなかった。

### Decision

1. ADR-031を取り消し、スクレイパーを従来どおり`current/`→`previous/`のHTMLローテーション保存へ戻す。
2. CAPCOM公式から全30キャラを2回再取得し、削除した60件のアーカイブ構成を再構築する。
3. NULL化した過去の`move_snapshots.raw_html_uri`は、同キャラの復元済み`current/{slug}.html`へ再接続する。
   削除前の過去HTMLは復元不能なため、URIは復元時点の公式ページを指すことを明示する。
4. 今後Storageを削除する前に、現行オブジェクトサイズ・バケット別内訳・課金期間の使用量を分けて確認し、
   容量超過の直接原因であることを確認してから実行する。

### Consequences

- `sf6-html-archive`は再び約25MBの調査用アーカイブとして維持される。
- フレームデータのデバッグ用HTMLと既存snapshot URIは利用可能な状態へ戻る。
- Storage警告が残る場合でも、現行の24MBアーカイブを原因と断定せず、Supabaseの期間使用量・請求画面を確認する。

---

## ADR-033: 自然言語の2技連携は技名をDB解決し、遷移種別ごとに計算する

**Date**: 2026-07-16
**Status**: Active

### Context

`sequence_analysis` 自体はDBの技行を使っていたが、Intent Parserには迅雷脚の強度別入力と
`波衝撃 -> 波掌撃` の個別表が残っていた。そのため、未登録の必殺技名を含む質問は単体技の
`lookup_move`へ落ち、全キャラ・全技に拡張できなかった。また、防御側技を指定しない
「連続ガードか」はタイムラインの入力不足となり、`Chn`がある弱攻撃連携を通常linkとして
処理すると連打キャンセルを誤判定する。

### Decision

1. Intent Parserは、`→ / > / から / の後に / AをBでキャンセル / into` で技を分割し、
   `2中K`や`立ち弱P`の汎用入力表記だけを正規化する。キャラ固有の必殺技名・SA名・派生名は
   不透明な技識別子のまま後段へ渡し、ハードコードしない。
2. 2技はどちらも`lookup_frame_data`のCAPCOM/UFD/SuperCombo/必殺技マッピングで解決する。
   誤記補正は、同一の強度/SA prefix内で一候補が閾値と次点差を満たす場合だけ許可する。
   弱中強やODが残る同名技は自動選択せず、強度またはコマンドを聞き返す。
3. 解決後の遷移は、`link / special cancel / super cancel / light chain / exact composite edge`に
   分ける。`Sp`、`SA`/`SA1..3`、`Chn`はSuperComboのカテゴリ根拠とし、targetの種別と
   一致する場合だけキャンセルtimelineを実行する。
4. generic `Chn` ruleは同じ状態の地上弱攻撃targetに限定する。Feng Shui Engine等の状態付き
   入力は状慈suffixが一致しない通常技へ流用しない。任意の中/強攻撃やtarget comboを`Chn`だけで
   接続可能とは推測しない。
5. 防御側技が未指定でも、防御側の行動可能フレームと2技目のfirst activeから
   `true_blockstring / true_combo / gap_open` を返す。防御技が指定された場合だけ、そのfirst activeと追加比較する。
6. cancel根拠のないnormal-to-special/SAはキャンセルとしては否定し、ユーザーが実際に
   1技目を出し切ってから2技目を最速入力する場合の`after_recovery link`として計算する。
   「キャンセル不可のため出し切り後」と回答に明記する。
7. `A~B`の専用派生はADR-030の厳格なedgeルールを維持し、startupだけでlink/cancelに代用しない。
8. `tests/sequence_comprehensive_audit.py`で全キャラの保存済み入力・公式名とordered pairを定期監査する。
   入力未検出は失敗、原典に単一値がない場合は理由付き`unresolved`とし、数値を作らない。

### Activation results (2026-07-16)

- SuperCombo入力2,118件とCAPCOM公式技名2,357件は、全30キャラで未検出0件。
- SuperComboの強度省略名263件は、誤解決せず曖昧性と候補を返した。
- 非compositeのordered pair 103,073件で遷移分類を実行し、70,006件はtimeline解決、
  33,145件は空中技・投げ・条件値等のscalar不足を理由に保留した。
- 実DB E2EでRyu `5LP -> 214LP`はspecial cancel・gap 3F、Ryu `5LP -> 2LP`はlight chain・
  gap -5F、Chun-Li `5MP -> 236LK`はspecial cancel・gap -10Fと解決した。

### Consequences

- 新キャラや新技はDB取り込み後、Intent Parserの技名辞書を変更せず連携解析の対象になる。
- 「全技対応」は全保存技を解決・分類することを意味し、原典にblockstun/on_block/startupの
  単一値がない技に対して数値を捜造することは意味しない。その場合は不足フィールドと条件を明示する。
- 時間上の連続ガード/連続ヒットと、実際に届くか・無敵で抜けるか・姿勢/状態が適合するかは分離し、
  距離・pushback・当たり判定・無敵・空中/構え・溜めの注意を回答に残す。

---

## ADR-034: 連携の技間タイミングと2技目接触後の硬直差を分離する

**Date**: 2026-07-16
**Status**: Active

### Context

「リュウの5LP→弱波掌撃をガードして何F有利？」に対し、技間gap 3Fと連続ガードでないことだけを
返していた。従来スキーマは1技目の接触を表す`initial_interaction`しか持たず、
`post_interaction_advantage`は相打ち後のhitstun差を表す用途だった。cancel経路はtimeline専用summaryへ
短絡するため、解決済み2技目の`on_block=-3`を回答に使えなかった。

### Decision

1. 1技目の接触は`initial_interaction`、2技目の接触は`terminal_state.interaction`として別に保持する。
2. 技間の隙間/連続ガードは`timeline`または`blockstring`、2技目接触後の通常の硬直差は
   `terminal_frame_advantage`、相打ち後の派生有利差は`post_interaction_advantage`へ分ける。
3. `terminal_frame_advantage`は2技目の統合プロファイルの`on_block/on_hit`を攻撃側値とし、
   防御側値は符号反転して構造化する。値が条件付き・未収録なら単一値を作らず保留する。
4. 「ガードして何F」の主体が省略された場合は両視点を返す。「ガードした側」「攻撃側」などが
   明示された場合は、その視点をIntentからMCP、Evaluator、summaryまで保持する。
5. 回答は質問された終端硬直差を先に出し、技間gap/連続ガード可否と距離等の空間条件を補足する。

### Consequences

- 「2技目をガードした後の有利不利」と「2技目まで強制的にガードさせられるか」を同時に、混同せず説明できる。
- 技名やキャラ固有分岐は増えず、統合プロファイルに`on_block/on_hit`がある全技へ同じ処理を適用できる。
- 3技以上へ拡張する場合は`move_index`を固定値ではなく各接触イベントへ一般化する必要がある。

---

## ADR-035: 技名解決をDB由来の多表記検索と型付きコマンド確認へ統合する

**Date**: 2026-07-16
**Status**: Proposed

### Context

ユーザーは公式名だけでなく、`中ネク`、`弱はしょう`、英語、ローマ字、`下デヨ`のような
字面が無関係な通称で技を指定する。現行は公式日本語名containment、英語ILIKE、一意fuzzy、
旧`move_aliases`を持つが、解決処理が複数モジュールへ分散し、かな/読み/ローマ字の共通indexがない。

旧`register_move_alias`は、ユーザーが示した一つのコマンドから強度を除去したSC family aliasを
即時global UPSERTする。variant固有の通称を全強度へ広げること、誤登録を全利用者へ公開することを
防げないため、ADR-026により既定無効になっている。実DBの`canonical_moves`と
`canonical_move_aliases`もまだ0件である。

### Proposed decision

1. `MoveResolver`を単一のread-only serviceとして導入し、単体技、確反、コンボ、セットプレイ、
   連携の全技を同じ候補生成・一意性判定へ通す。
2. Intent Parserは技名spanを翻訳せず原文のまま保持し、強度、入力、locale/script hintを分離する。
3. CAPCOM公式日本語名、UFD/SC英語名、入力、レビュー済みaliasからキャラ別検索formを生成する。
   NFKC、かな統一、レビュー済み読み、ローマ字、英語tokenを別の根拠種別として保持する。
4. 部分一致・trigram・編集距離は候補生成にだけ使う。キャラ、variant、最低score、次点差、
   根拠品質を満たす一候補だけを自動解決する。
5. 結果を`resolved / needs_confirmation / ambiguous / needs_command / invalid_input`へ型付けする。
   字面から候補を作れない通称は推測せず、そのキャラのコマンドを聞く。
6. コマンド返信は同一利用者・同一会話・短いTTLのpending内でexact input検証し、対象技を復唱して
   確認後に元質問を再実行する。初期段階ではsession内だけで使い、永続化しない。
7. 一つのvariant確認からfamily aliasを推定しない。global aliasはcanonical move versionを指す
   review済み行だけをeligible viewへ公開する。
8. canonical backfill前は既存ソース行を`character + input + variant`で一時group化する。
   backfill後に名前form indexと`canonical_move_aliases`へ移行し、旧`move_aliases`への新規書き込みを廃止する。

### Consequences

- 新しい公式技・英語名はデータ更新後にコード変更なしで検索対象になる。
- `中ネク`、`弱はしょう`、英語/ローマ字は一意性とvariant条件を満たす場合だけ解決される。
- `下デヨ`のような通称は初回にコマンド確認が必要だが、誤った技へのfuzzy解決を避けられる。
- Botの即時global学習は復活させず、session、private、review済みsharedを段階分離する。
- 詳細契約と評価計画は`docs/MOVE_ALIAS_RESOLUTION_DESIGN.md`に記録する。

---

## ADR-036: 連携回答は質問の結論を1行目に置く

**Date**: 2026-07-16
**Status**: Active

### Context

「5LP→弱波掌撃は連続ガードか」というyes/no質問に対し、従来summaryはblockstun、cancel可否、
hitstop基準、両者の行動可能Fを先に説明し、結論を3段落目に置いていた。計算根拠は正しいが、
Discordで知りたい回答へ到達するまでが長い。

またIntentの`blockstring` targetが「連続ガード」と「指定技で割り込めるか」を兼用しており、
質問ごとの結論文を選べなかった。

### Decision

1. sequence query targetを`blockstring / interrupt / combo_timing / terminal_frame_advantage`へ分ける。
2. 各focus summaryは、`はい / いいえ / 判定できません`と直接結果を1行目に置く。
3. `blockstring`は連続ガード可否とgap、`interrupt`は指定発生F技の時間上の勝敗を先に返す。
4. blockstun、startup、transition source、timelineは構造化結果へ保持するが、単純な質問の前段には出さない。
5. 2行目には距離・pushback・姿勢・無敵等、結論の適用範囲に必要な注意だけを残す。
6. 終端硬直差と相打ち後結果は既存の専用summaryを維持し、focusを混在させない。

### Consequences

- Ryu `5LP -> 214LP`の連続ガード質問は「いいえ、技間の隙間は3F」と1行目で回答する。
- Ken `2MK -> 236HK`への4F割り込み質問は「はい、5F先に発生」と1行目で回答する。
- 詳細根拠はAPIレスポンスから失われず、将来の詳細表示やデバッグで参照できる。

---

## ADR-037: 強度省略の割り込み質問は技ファミリー比較として扱う

**Date**: 2026-07-16
**Status**: Active

### Context

「ケンの迅雷って割り込める？」は、技単体をガードした後の確反ではなく、始動技から
弱/中/強迅雷脚へキャンセルした時の技間を比較したい質問である。単一技resolverはこの強度省略を
正しく曖昧として止めるが、従来Botは`punish_check`へ誤分類し、原文の質問句を技名としてMCPへ渡していた。

### Decision

1. 始動・variantが省略された割り込み質問を`pressure_family_analysis`として型付けする。
2. `analyze_sequence_family`はfamily候補を列挙し、各variantを既存`analyze_sequence`で個別に評価する。
   新しいgap式は作らない。
3. 単体照会では曖昧性を維持する。family列挙は明示的に比較を求める質問だけで許可する。
4. 始動が省略された場合は、パッチレビュー済みdataのexact family formだけを補完し、前提を回答に表示する。
   デフォルトがなければ始動技を聞く。parserへキャラ固有の始動をハードコードしない。
5. `variant_scope=normal`は弱/中/強、`all`はOD等も含める。ODは通常版の比較へ暗黙追加しない。
6. gapが正なら、発生`gap-1`F以下は時間上先行、発生`gap`Fは同時として表示する。距離・姿勢・無敵・
   当たり判定は従来どおり別確度とする。

### Consequences

- Ken `迅雷`はレビュー済みformとして`Jinrai Kick`へ解決し、2MKからの弱/中/強を比較できる。
- 未登録の略称・始動省略は、誤った代表連携を推測せず始動指定を求める。
- 全キャラ展開はJSONのレビュー済みdefault行を追加することで行え、parser/計算式の変更を要しない。

---

## ADR-038: 曖昧な対戦カード割り込み質問は代表連携の明示的比較に限定する

**Date**: 2026-07-16
**Status**: Active

### Context

「リュウの主な技に対してキンバリーが割り込める技を教えてください」は、攻撃側の技も連携先も、
防御側の行動種別も省略している。これを既存の単一技・連携Intentへ渡すと、LLMが無関係な技名を
生成して`sequence_analysis`へ送るか、技名不足で失敗する。「主な技」を使用率や印象で推定することも
データ根拠のない戦術断定になる。一方、少数の代表連携を固定すると、実際に割り込める連携を見落とす。

### Decision

1. 2キャラ、主な/代表技、割り込みの3条件を満たす質問だけを、LLMより先に
   `matchup_interrupt_overview`へ決定論分類する。`chara`を攻撃側、`chara2`を防御側として保持する。
2. 攻撃側の対象は、数値化できる地上通常技→通常版必殺技の全special cancelとする。使用率の主張はせず、
   回答内で選定範囲を明示する。OD、空中専用、非攻撃動作、単一値を解決できない技は除外する。
3. cancel時系列は既存`_cancel_timeline`を共用する。一括照会用に各技プロファイルを一度だけ統合し、
   新しいgap式やキャラ固有のキャンセル推測は追加しない。
4. 防御側は統合フレームプロファイルの地上通常技だけを比較し、`startup < gap`を時間上の割り込み候補、
   `startup = gap`を同時、`startup > gap`を不可とする。候補は距離未検証であることを常に表示する。
5. OD/必殺技、無敵、リーチ、姿勢、pushback、使用率は概観回答に混ぜず、具体的な連携・防御技を
   指定した既存`analyze_sequence`へ委ねる。

### Consequences

- 曖昧質問でも、無関係な技名をLLMが捏造せず、根拠のある候補または「候補なし」を返せる。
- Ryu対Kimberlyでは数値化できた144組の通常技→通常版必殺技cancelを走査し、29組でKimberlyの
  地上通常技が時間上先行した。`5LP→214MK`はgap 5Fに対して2LP（4F）が1F先行することを、
  既存の詳細`analyze_sequence`でも照合した。
- 新キャラ・技はSC/CAPCOMデータ取り込み後に同じ走査対象となり、キャラごとの代表連携JSON、
  Intent、計算式を追加せず全キャラへ適用できる。
- 単体技、明示連携、技ファミリー比較の既存回答契約は変わらない。
