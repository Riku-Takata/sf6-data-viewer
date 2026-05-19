# PROGRESS

このファイルは「**今どこまで進んだか / 次に何をやるか**」を一目で把握するための
進捗管理ファイル。各セッション終了時に更新し、次回セッション開始時に最初に読む。

---

## 🎯 現在のフェーズ

**Milestone**: M3 完了 + Layer 1 パッチ通知 **✅ 完了 (2026-05-19)**
**次**: M4 候補 — AWS 完全移行 (Bedrock LLM / Lambda CLI) または追加精度向上

## 📊 全体進捗

- [x] Layer 1: データ収集パイプライン (完了)
- [x] **M1: 基盤データ統合とコア検索 ✅ 完了**
  - [x] Phase 1: SuperCombo データの取り込み (4/4 タスク)
    - [x] Task 1-1: 新スキーマ設計
    - [x] Task 1-2: スキーマ適用とインポート実行 (2118件/30キャラ)
    - [x] Task 1-3: 正規化ビューの検証 (結合率94.3%、startup一致100%)
    - [x] Task 1-4: CAPCOM ↔ SuperCombo マッピング確認
  - [x] Phase 2: システム文書の取り込み — M1 スコープ外 (M2 で実施)
  - [x] Phase 3: LLM統合 (4/4 タスク)
    - [x] Task 3-1: Ollama + Gemma4 採用決定 (ADR-013)
    - [x] Task 3-2: LLMProvider + OllamaProvider 実装
    - [x] Task 3-3: Intent Parser (6種 intent_type, 特殊技ポストプロセス)
    - [x] Task 3-4: RAG Context Builder + 最終回答生成
  - [x] Phase 4: CLI統合と動作確認 (3/3 タスク)
    - [x] Task 4-1: `ask` サブコマンド追加 (-v デバッグモード付き)
    - [x] Task 4-2: 統合テスト 20問 → **14✅ 6⚠ 0❌ (合格ライン70%達成)**
    - [x] Task 4-3: README 整備 + M1 完了宣言
- [x] **M2: Logic Engine と推論 ✅ 完了 (2026-05-16)**
  - [x] Phase A: ゲームシステム文書の取り込み (4/4 タスク完了)
  - [x] Phase B: 必殺技マッピング (3/3 タスク完了)
  - [x] Phase C: 精度チューニング (2/2 タスク完了)
- [x] **M3: セットプレイ推論 + 必殺技汎用検索 ✅ 完了 (2026-05-19)**
  - [x] setplay_engine.py: KD有利パーサー・前ステップF 全キャラ動的取得 (doc_chunks)
  - [x] 必殺技検索の汎用化: _JP_MOVE_TO_EN ハードコーディング廃止、DB直接 ILIKE 検索
  - [x] 強度修飾子 (弱/中/強/OD/P系) の自動判別 (_pick_variant 改善)
  - [x] 派生技割り込み判定: フレームギャップ自動計算・コンテキスト提示
  - [x] intent_parser 汎用化: JP特殊技名自動抽出・英語技名自動抽出・OD対応
  - [x] 全キャラテスト: 30/30 ✅ (30キャラ × 複数 intent_type)
- [x] **Layer 1 パッチ通知 ✅ デプロイ済み (2026-05-19)**
  - [x] SNS トピック `sf6-patch-notification` 作成・デプロイ
  - [x] SSM Parameter Store `/sf6/notification-email` でメール管理 (コードに個人情報なし)
  - [x] samconfig.toml を .gitignore 追加 + samconfig.toml.example 作成
  - [x] ARCHITECTURE.md 作成 (Mermaid 構成図・フロー図・コスト比較表)

## 🚀 次にやること

**M3 + Layer 1 通知 完了 🎉🎉🎉**

現在の対応範囲:
- フレームデータ照会・確定反撃 (全30キャラ / 通常技・必殺技・SA)
- コンボ接続検証・最大コンボ計算 (ビームサーチ)
- セットプレイ (KD後の起き攻め択計算、全キャラダッシュF対応)
- 派生技の割り込み判定 (フレームギャップ自動計算)
- パッチ検知時のメール通知 (AWS SNS + SSM)

**M4 候補 (優先度順):**
1. **AWS 完全移行** — Bedrock Gemma3 + Lambda/API Gateway で CLI をクラウド化
   - BedrockProvider 実装 (LLMProvider 抽象化済みなので 1ファイル追加)
   - SNS 確認メールの承認 (初回パッチ検知時)
2. **統合テスト拡充** — setplay_analysis / punish_check + 派生 を 20問追加
3. **Web UI** — Slack Bot or 簡易 Web フロントエンド

## 📝 直近のセッションログ

### 2026-05-19 ★ M3 完了 + Layer 1 パッチ通知デプロイ
- **必殺技の汎用検索対応**: _fetch_move_by_name を DB 直接 ILIKE に刷新、_JP_MOVE_TO_EN はフォールバックのみ
- **強度修飾子判別**: _pick_variant に OD(KK/PP)・P系(LP/MP/HP)対応、「弱派生の弱」誤マッチ修正
- **派生技割り込み判定**: combo_info + move_name で `input~%` 派生を自動取得、ギャップ計算
- **intent_parser 汎用化**: JP特殊技名→move_name自動抽出、英語技名 (日本語文中) 自動抽出
- **全キャラテスト**: 30問 30/30 達成 (30キャラ × punish_check / lookup_move / setplay)
- **Layer 1 SNS通知**: lambda_function.py に notify_patch_detected() 追加、SSM からメール動的取得
- **セキュリティ**: samconfig.toml → .gitignore、メールアドレスは SSM のみ管理
- **デプロイ**: sam build && sam deploy → UPDATE_COMPLETE、SNS arn / SSM パラメータ作成済み
- **ARCHITECTURE.md 作成**: docs/ に Mermaid 構成図 + LLM コスト比較表

### 2026-05-18 ★ M3 セットプレイ推論 実装
- setplay_engine.py 新規作成: KD有利パーサー・アクションコスト計算・択列挙
  - 前ステップ23F定数 (Sagat 623HP KD+27→前ステップ→+4Fから実測算出)
  - compute_setplay() で即攻め/前ステップ/前ステップ×2 の3プリセットを自動計算
  - fetch_setplay_options() で残り有利F以内の発生を持つ通常技・必殺技・SAを取得
- intent_parser.py: setplay_analysis intent追加 (KD後/起き攻め/前ステップ後等のトリガー)
- rag_builder.py: JP技名マッピング補完 (モノリス/ノヴァ/グリード/マイト/ステハイ/ステロー)
  - _fetch_combo_data にpunish_adv追加、setplay_analysis ハンドラ追加
  - ANSWER_SYSTEM にセットプレイ回答指示追加
- 動作確認: 強アパカ KD+27→前ステップ→+4F (✅一致), モノリス KD+34→前ステップ→+11F (✅)

### 2026-05-16 (2) コンボ/キャンセル機能追加
- _fetch_combo_data() 新規実装 (dr_cancel_hit/after_dr_hit/cancel/notes を取得)
- _fmt_combo_context() でコンボ情報を構造化フォーマット
- combo_info intent で キャンセル・チェーン・DRキャンセル情報を提供
- lookup_move でもキャンセル・DR情報を自動付与
- Intent Parser: numpad 表記を正規表現で自動抽出するポストプロセス追加
- ANSWER_SYSTEM: 英語ノートを日本語で回答する指示を追加
- 対応クエリ例: '2MPの後に何が繋がる?', 'DRキャンセルすると何F?', 'SAに繋げられる?'

### 2026-05-16 ★ M2 完了セッション
- **M2 完成宣言**: 統合テスト 23/25 (92%) → M1 70% から大幅改善
- Phase C 完了: 精度チューニング
  - C-1: Counter-hitのキーワード修正(counter-hits)、ANSWER_SYSTEMに反撃判定直接引用ルール追加
  - C-2: compare_movesに move_name2 対応、explain_conceptに raw_query 使用
  - M1-10(CH vs PC): ✅、M1-14(竜巻反撃): ✅ に改善
- Phase B 完了: 必殺技マッピング (sc_moves.name ILIKE + 日英マッピング30件)
  - タイガーショット・波動拳・昇竜拳・サマーソルト等の必殺技データ取得可能に
- Phase A 完了: SuperCombo 7ページ(72チャンク) → pgvector + ハイブリッド検索

### 2026-05-15 ★ M1 完了セッション
- **M1 完成宣言**: `python -m sf6_engine.cli ask "サガットの2HKの発生は?"` → 発生11F ✅
- Task 4-3 完了: README.md 新規作成 (使い方/セットアップ/失敗パターンログ)
- Task 4-2 完了: 統合テスト 20問 → 14✅ 6⚠ 0❌ (合格ライン70%達成)
  - 修正: 波動拳/昇竜拳の誤マッピングを正規表現ポストプロセスで除去
  - 修正: punish_check に反撃可否の自動計算をコンテキストに追記
- Task 4-1 + Phase 3 完了: ask コマンド/Intent Parser/RAG Builder/OllamaProvider 実装
- LLM: Gemini → Ollama + Gemma4:e2b に変更 (ADR-013), 常時起動不要でゼロコスト運用
- **未取込4キャラ対応完了**: cammy/guile/ken/ingrid
  - cammy/guile/ken: Lambda の force_slugs で強制スクレイプ成功、CAPCOM+SC 両方取込
  - ingrid: CAPCOM ページにフレームテーブルなし、SC Wiki も数値未掲載 → M1 は SC 技名のみ
  - Lambda 改善: force_slugs対応、ALL_KNOWN_SLUGS補完、INSERT→UPSERT、move_name重複除去
  - unified_moves を Part A (CAPCOM+SC) + Part B (SC only) UNION 構造に更新
  - 最終: unified_moves 30キャラ 2344件 (29キャラ CAPCOM+SC, 1キャラ SC only)
- **Phase 1 完了**: SuperCombo データの取り込み (Task 1-1〜1-4 全完了)
- Task 1-4 完了: char_slug_map 30件照合 ✅、E2E デモ全件一致
  - サガット2HK: CAPCOM/SC startup 11F 一致、パニカン+45F、解説テキスト付き
  - 結合できない4キャラ (Cammy/Guile/Ingrid/Ken) は Layer 1 未取込のため想定内
- Task 1-3 完了: sc_move_normalized / unified_moves 検証
  - 正規化成功率: startup 96.9%, block_adv 95.2% — 失敗はデータなし値のみ
  - unified_moves 通常技結合率: 94.3% (476/505件)、Sagat startup 100%一致
  - 未結合29件の原因特定: [チェーンコンボ]/垂直ジャンプ/連打版/SC欠落 → M1 scope 外として許容
- Task 1-2 完了: Supabase スキーマ適用 + 全2118件インポート (30キャラ、エラー0)
  - service_role key を .env に追加 (get_write_client() で書き込み)
  - JSON 内の重複 (chara, input) 172件を事前除去して対応
  - Sagat 2HK: startup=11, block_adv=-12, hit_adv='KD +29', atk_range=1.91 を確認
- Task 1-1 完了: `sf6_engine_schema_v2.sql` 作成
  - `char_slug_map` テーブル (30キャラ、要 capcom_slug 検証)
  - `sc_moves` テーブル (SuperCombo 全フィールド対応)
  - `sc_move_normalized` ビュー (数値抽出・KD判定)
  - `capcom_to_numpad()` 関数 (通常技18パターン実装済み)
  - `unified_moves` ビュー (CAPCOM + SC LEFT JOIN)
- `scripts/html_strip.py` をエンジン配下に配置
- `importers/supercombo.py` 作成 (dry-run 2290件エラーなし確認済み)
- `requirements.txt` に `google-generativeai` 追加 (Phase 3 準備)
- 発見: SuperCombo JSON は dict 型 (chara名 → 技リスト)、moveType は 'ground_normal'/'air_normal' 等

### 2026-04-26
- Layer 1 が完成 (Lambda デプロイ済み、自動稼働中)
- Layer 2 の方針議論 → SuperCombo の Cargo API + システム文書の活用へと方針転換
- 道A (本格Layer 3設計) を選択、マイルストーン分割で進めることを決定
- ADR 作成、M1タスク分解、PROGRESS.md セットアップ

## 🚫 ブロッカー / 懸念

なし

## 💡 メモ・気づき

- Layer 1 のスクレイパーは EventBridge で毎日 03:00 JST に自動稼働中
- SuperCombo の最新データ (2026-04-26版) は手元にダウンロード済み
- SNS 通知: 次回パッチ検知時に確認メールが届く → 「Confirm subscription」を承認すること
- AWS デプロイ用 IAM ユーザー: sf6-deployer (SNS・SSM 権限を追加済み)
- samconfig.toml はローカルのみ (.gitignore 済み)、雛形は samconfig.toml.example
- LLM 移行候補: Bedrock Gemma3 12B ≈ $2.70/月 (2,000クエリ), EC2 は常時起動で割高
- 前ステップF は doc_chunks の Forward/Back Dashing テーブルから全キャラ動的取得 (lru_cache)

---

## 📂 重要ファイルの場所

| ファイル | 役割 |
|---|---|
| `ADR.md` | 設計判断ログ |
| `M1_TASKS.md` | M1 のタスク詳細分解 |
| `PROGRESS.md` (このファイル) | 進捗管理 |
| `sf6_schema.sql` | Layer 1 のDBスキーマ |
| `move_normalized_view.sql` | Layer 1 の正規化ビュー |
| `lambda_function.py` | Layer 1 のスクレイパー |
| `template.yaml` | Layer 1 のSAMテンプレート |
| `supercombo_sf6_2026-04-26.json` | SuperCombo の生データ (3.7MB) |

## 🔗 関連リソース

- Supabase Studio: (URL を記入)
- AWS Lambda Console: ap-northeast-1 / sf6-frame-scraper
- Gemini API キー: (取得後に追加)

---

## セッション終了時の更新手順

1. **完了したタスクにチェック** (M1_TASKS.md と本ファイルの両方)
2. **「次にやること」を更新** (具体的なタスク名と所要時間)
3. **セッションログ追加** (日付と何をやったか、3〜5行)
4. **新しい気づき・判断**があれば「メモ」に追加
5. **重要な設計判断**があれば ADR.md に追加 (ADR-0XX として)
