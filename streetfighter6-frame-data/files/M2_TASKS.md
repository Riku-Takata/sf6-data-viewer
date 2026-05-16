# M2 タスク分解: Logic Engine と推論

**目標**: 必殺技データ + ゲームシステム文書を加え、本物のコーチングができる状態にする。

**M1 からの変化:**
- M1: 通常技のみ、ゲーム概念は「Phase 2 待ち」
- M2: 必殺技・SA も回答可、「ドライブインパクトって何?」に正確に答えられる

**期間目安**: 10〜13週 (週8時間ペース)

**完成判定:**
- `python -m sf6_engine.cli ask "サガットのタイガーショットは安全?"`
  → ガード時のフレームと解説を含む回答
- `python -m sf6_engine.cli ask "ドライブインパクトって何?"`
  → Drive Gauge消費・アーマー・壁やられを正確に説明
- `python -m sf6_engine.cli ask "バーンアウト中に相手のDIをガードするとどうなる?"`
  → ゲームシステムを踏まえた回答

---

## Phase A: ゲームシステム文書の取り込み (旧 Phase 2)

M1 統合テストで6問が「⚠ 要改善」だった主因。
ゲーム概念 (ドライブインパクト/バーンアウト等) の文書をベクトル検索できるようにする。

### Task A-1: SuperCombo 文書の取得 (2〜3時間)

ブラウザの開発者コンソールで JS スニペットを実行して HTML を取得する
(Cloudflare 対策のため手動取得)。

**取得対象 (優先度順):**
| ページ | 内容 | 優先度 |
|---|---|---|
| SF6/Gauges | Drive Gauge, Super Gauge, Burnout | 高 |
| SF6/Offense | Counter Hit, Punish Counter, DI | 高 |
| SF6/Defense | Block, Drive Parry, Drive Reversal | 高 |
| SF6/Movement | Dash, Jump, Drive Rush | 中 |
| SF6/Controls | Modern/Classic, Input shortcuts | 中 |
| SF6/Glossary | 用語集 | 低 |

取得後の保存先: `streetfighter6-engine/docs/supercombo_docs_YYYY-MM-DD.json`

- [ ] ブラウザ JS スニペットを実行して JSON をダウンロード
- [ ] docs/ ディレクトリに保存

### Task A-2: チャンク分割とクリーニング (3時間)

- [ ] HTML → Markdown テキスト抽出 (ナビゲーション・広告を除外)
- [ ] h2/h3 見出し単位でチャンク分割 (目標: 500〜1500文字)
- [ ] 各チャンクにメタデータ付与: ページ名・見出し・キーワード
- [ ] `importers/docs.py` 実装

### Task A-3: pgvector セットアップとベクトル格納 (4〜6時間)

- [ ] Supabase で pgvector 拡張を有効化
- [ ] `doc_chunks` テーブル作成 (id, page, heading, content, embedding, keywords)
- [ ] `search_docs()` 関数作成 (コサイン類似度検索)
- [ ] nomic-embed-text (768次元) でチャンクをベクトル化して格納
- [ ] SQL: `sql/doc_chunks_schema.sql` として保存

### Task A-4: RAG Builder に文書検索を統合 (3時間)

- [ ] `rag_builder.py` の `explain_concept` 処理を実装
  (現在は "Phase 2 待ち" と返しているのを実際の検索に置き換え)
- [ ] `punish_check` や `lookup_move` にも関連文書を追加
- [ ] 統合テスト 20問を再実行して ✅ 数が増えることを確認

---

## Phase B: 必殺技マッピング

M1 で「サガットのタイガーショットのデータ教えて」に答えられなかった問題を解決。

### Task B-1: SuperCombo ↔ CAPCOM 必殺技マッピングの設計 (2時間)

- [ ] CAPCOM の技名と SuperCombo の input を突き合わせるアプローチ検討
  - 選択肢A: キャラ別手動マッピングテーブル (確実だが作業量大)
  - 選択肢B: Gemma に技名→numpad 変換を学習させる (不確実性あり)
  - 選択肢C: SuperCombo の `name` フィールドで部分一致検索 (シンプル)
- [ ] 選択肢を ADR に記録

### Task B-2: 必殺技検索の実装 (4〜6時間)

- [ ] `capcom_to_numpad()` を必殺技にも拡張
  または `search_sc_by_name()` を実装 (SuperCombo の `name` で ILIKE 検索)
- [ ] Intent Parser の system prompt に必殺技の numpad 変換例を追加
  (例: タイガーショット → `236P`/`214P`)
- [ ] unified_moves または sc_move_normalized から必殺技を取得できるように
- [ ] サガット必殺技でテスト: タイガーショット、タイガーアッパー、SA

### Task B-3: 統合テスト更新 (2時間)

- [ ] 必殺技照会 5問を追加してテスト
- [ ] 失敗パターンを記録・対処
- [ ] PROGRESS.md を更新

---

## Phase C: 精度チューニング (M2 後半)

A + B が完了してから実施。

### Task C-1: プロンプト調整 (2時間)

- [ ] M2 後の統合テスト 25問以上で再評価
- [ ] ゲーム概念回答の精度確認 (Burnout, Drive Rush キャンセル等)
- [ ] ハルシネーションがないか確認 (データなし時は正直に)

### Task C-2: OllamaProvider 埋め込みの精度チューニング (3時間)

- [ ] `search_docs()` の閾値 (match_threshold) 調整
- [ ] キーワードマッチとベクトル検索のハイブリッド検討
- [ ] 検索失敗ケースの記録

---

## 進め方のヒント

- Phase A → Phase B → Phase C の順を推奨
- Phase A だけで M2 の体感改善は大きい (ゲーム概念が答えられるようになる)
- Phase B は作業量が多いが、実戦用途では必殺技データが重要
- 1セッション (2〜3時間) で 1タスクが現実的

## M2 段階の妥協ポイント

- ✅ 全必殺技でなく、サガットの必殺技だけでOK (他キャラはM3)
- ✅ ゲーム文書は 高優先度3ページ (Gauges/Offense/Defense) だけでOK
- ✅ 埋め込み精度は「大体合ってれば」OKで、チューニングは後回し
- ❌ 「知らないことを知らないと言える」設計は引き続き必須
