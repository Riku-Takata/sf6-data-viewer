# PROGRESS

このファイルは「**今どこまで進んだか / 次に何をやるか**」を一目で把握するための
進捗管理ファイル。各セッション終了時に更新し、次回セッション開始時に最初に読む。

---

## 🎯 現在のフェーズ

**Milestone**: M1 - 基盤データ統合とコア検索 **✅ 完了 (2026-05-15)**
**次**: M2 - Logic Engine と推論 (開始判断待ち)

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
- [ ] M3: 実戦活用 (M2完了後に判断)

## 🚀 次にやること

**M2 完了 🎉🎉🎉**

統合テスト 25問: **23✅ 2⚠ 0❌ → 92%達成**

次: M3 (実戦活用) への判断
- Web UI / Slack Bot 等の外部インターフェース
- 対戦前の相手キャラ分析レポート
- パッチ差分の自動解説

## 📝 直近のセッションログ

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
- マッピング作業は通常技 → 必殺技/SA の順で段階的に
- M1 の妥協ポイントを忘れない: 必殺技マッピングは M2、サガットだけ解説整備でOK

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
