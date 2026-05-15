# PROGRESS

このファイルは「**今どこまで進んだか / 次に何をやるか**」を一目で把握するための
進捗管理ファイル。各セッション終了時に更新し、次回セッション開始時に最初に読む。

---

## 🎯 現在のフェーズ

**Milestone**: M1 - 基盤データ統合とコア検索
**Phase**: Phase 1 - SuperCombo データの取り込み
**現在のタスク**: Task 1-3 - 正規化ビューの検証

## 📊 全体進捗

- [x] Layer 1: データ収集パイプライン (完了)
- [ ] **M1: 基盤データ統合とコア検索** (← 今ここ)
  - [ ] Phase 1: SuperCombo データの取り込み (2/4 タスク完了)
    - [x] Task 1-1: 新スキーマ設計 ✅
    - [x] Task 1-2: スキーマ適用とインポート実行 ✅ (2118件/30キャラ)
    - [ ] Task 1-3: 正規化ビューの検証 (← 次)
    - [ ] Task 1-4: CAPCOM ↔ SuperCombo マッピング
  - [ ] Phase 2: システム文書の取り込み (0/4 タスク)
  - [ ] Phase 3: LLM統合 (0/4 タスク)
  - [ ] Phase 4: CLI統合と動作確認 (0/3 タスク)
- [ ] M2: Logic Engineと推論 (M1完了後に判断)
- [ ] M3: 実戦活用 (M2完了後に判断)

## 🚀 次にやること

**最優先**: Task 1-3 — 正規化ビューの検証

確認内容:
- `sc_move_normalized` で Sagat 5HP のフレーム値が正しく抽出されているか
- パース失敗 (NULL) の行がないか確認
- unified_moves ビューで CAPCOM + SC の結合が通常技で成立しているか

**所要時間目安**: 1時間 (Task 1-4 と合わせて進められる)

## 📝 直近のセッションログ

### 2026-05-15
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
