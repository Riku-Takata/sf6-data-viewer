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
- **Groq 無料枠**: Gemma 対応だが AWS ではない。フォールバックとして検討可
