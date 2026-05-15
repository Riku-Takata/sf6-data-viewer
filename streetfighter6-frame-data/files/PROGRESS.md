# PROGRESS

このファイルは「**今どこまで進んだか / 次に何をやるか**」を一目で把握するための
進捗管理ファイル。各セッション終了時に更新し、次回セッション開始時に最初に読む。

---

## 🎯 現在のフェーズ

**Milestone**: M1 - 基盤データ統合とコア検索
**Phase**: Phase 1 - SuperCombo データの取り込み
**現在のタスク**: Task 1-1 - 新スキーマ設計

## 📊 全体進捗

- [x] Layer 1: データ収集パイプライン (完了)
- [ ] **M1: 基盤データ統合とコア検索** (← 今ここ)
  - [ ] Phase 1: SuperCombo データの取り込み (0/4 タスク)
  - [ ] Phase 2: システム文書の取り込み (0/4 タスク)
  - [ ] Phase 3: LLM統合 (0/4 タスク)
  - [ ] Phase 4: CLI統合と動作確認 (0/3 タスク)
- [ ] M2: Logic Engineと推論 (M1完了後に判断)
- [ ] M3: 実戦活用 (M2完了後に判断)

## 🚀 次にやること

**最優先**: Task 1-1 の新スキーマ設計
- SuperCombo データを格納する `sc_moves` テーブルの構造を決める
- Claude と一緒に SQL ファイル `sf6_engine_schema_v2.sql` を作る

**所要時間目安**: 2時間

## 📝 直近のセッションログ

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
