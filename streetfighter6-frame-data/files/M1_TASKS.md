# M1 タスク分解: 基盤データ統合とコア検索

**目標**: 自然言語で「サガットの2HKって何?」のように聞くと、フレームデータと
ゲーム文脈を踏まえた回答が返る最小システム (CLI)。

**期間**: 8〜10週 (週8時間 = 約60〜80時間)

**完成判定**: 以下が動く状態
- `python -m sf6_engine.cli ask "サガットの2HKの発生は?"` → フレーム情報を返す
- `python -m sf6_engine.cli ask "ドライブインパクトって何?"` → システム仕様を踏まえた説明を返す
- `python -m sf6_engine.cli ask "サガットの2HKでパニッシュカウンター取ったらどうなる?"` → 数値+解説を返す

## Post-M1: 連携・相打ち解析 (2026-07-13)

- [x] 2技連携の共通タイムラインと攻撃/防御側ディレイを実装
- [x] CAPCOM主値+UFD/SC補完とSC hitstun/hitstopを連携解析で統合
- [x] 相手の発生F指定は区間、キャラ+技指定は個別モデルとして分離
- [x] 相打ち後の両視点有利差と追撃の timing/spatial/state/confirmed を分離
- [x] Intent / MCP / Discord / CLI RAGを共通 `sequence_analysis.py` へ接続
- [x] `sequence_observations` DDL、相手技ID必須の検証、upsertインポーターを実装
- [x] 不完全な`+7/2MP`報告を未レビュー資料へ降格し、汎用回答から除外
- [x] unittest 79/79、JSON dry-run 1/1、Supabase実データE2Eを確認
- [x] 4F地上通常技46件を技別計算し、+6～+12Fの分布と条件付き追撃を確認
- [ ] Supabaseへ `sequence_analysis_migration.sql` を適用
- [ ] 相手キャラ+技が特定された実測観測を収集し、レビュー後にupsert
- [x] AWS MCPを訂正版へ再デプロイし、本番で4F技分布と`Ryu 2LP +9/-9`を確認
- [ ] UFD GIF geometryと追加実測で相打ち・追撃の到達証明を拡張

---

## Phase 1: SuperCombo データの取り込み (12〜16時間)

既に手元に `supercombo_sf6_2026-04-26.json` (3.7MB, 2290技) がある。
これを Supabase に取り込む。

### Task 1-1: 新スキーマ設計 (2時間) ✅ 2026-05-15 完了
- [x] SuperCombo データを格納するテーブル設計を確定
  - `sc_moves` テーブル (SuperCombo の SF6_FrameData 1行に対応)
  - 主要カラム: `chara`, `move_id`, `input`, `name`, `move_type`, 数値系, 解説系
  - 実際の JSON フィールド名に対応: `hitAdv` → `hit_adv`, `blockAdv` → `block_adv`
- [x] HTMLストリップ済みの正規化ビュー `sc_move_normalized` の設計
- [x] `move_normalized` (CAPCOM) と `sc_move_normalized` を結合する `unified_moves` ビューの設計
- [x] スキーマSQLファイル作成: `streetfighter6-engine/sql/sf6_engine_schema_v2.sql`
- [x] `capcom_to_numpad()` 関数実装 (通常技18パターン)
- [x] `importers/supercombo.py` 作成 (dry-run 2290件動作確認済み)
- ⚠ 要確認: `char_slug_map` の `capcom_slug` を `characters` テーブルと照合すること

### Task 1-2: スキーマ適用と SuperCombo データインポート (3時間) ✅ 2026-05-15 完了
- [x] Supabase SQL Editor で新スキーマ適用
- [x] JSON → Supabase へインポートする Python スクリプト作成 (`importers/supercombo.py`)
- [x] HTMLゴミ除去 (`scripts/html_strip.py` 適用済み)
- [x] 全2118件の取り込み実行 (2290件 - 重複172件 = 2118件、30キャラ)
- [x] サンプルクエリで取り込みが正しいか検証 (Sagat 2HK / 5HP 確認済み)
- 発見: JSON内に同一 (chara, input) の重複エントリが172件 → 後出し優先で除去
- 発見: 書き込みには SUPABASE_SERVICE_KEY (service_role) が必要 → .env に追加済み

### Task 1-3: 正規化ビューの実装 (3時間) ✅ 2026-05-15 完了
- [x] `sc_move_normalized` ビュー作成 (数値抽出・KD判定・is_normal フラグ)
- [x] パース失敗ケースの確認
  - startup: 96.9%, block_adv: 95.2%, hit_adv: 95.4% — 失敗はすべて '-' / '?(+)' (正当なデータなし)
  - atk_range: 84.5% — 未達分は派生技など SC 側に値なし (正常)
- [x] 正規化結果のサンプル確認 (Sagat 5HP: startup=15, block_adv=-5, punish_adv=4, atk_range=2.17 ✅)
- [x] unified_moves 結合率確認: 通常技 94.3% (476/505件) 結合成功
  - CAPCOM vs SC startup 値: Sagat 全通常技 100% 一致
  - 未結合 29件は M1 scope 外 ([チェーンコンボ]/垂直ジャンプ/連打版/一部キャラのSCデータ欠落)

### Task 1-4: CAPCOM <-> SuperCombo の最低限マッピング (4〜8時間) ✅ 2026-05-15 完了
- [x] キャラスラッグマッピング表の Supabase 登録 (30件、全件 characters テーブルと照合済み)
- [x] **通常技の自動マッピング**: capcom_to_numpad() で 18パターン実装
  - 26キャラで 100%、Zangief 87%（[連打版]3件）、Chun-Li 90%（垂直ジャンプ/SC欠落）
  - Juri 47% だが原因は [チェーンコンボ] 命名 (M2対応)、通常18技は100%結合
  - Cammy/Guile/Ingrid/Ken は CAPCOM Layer 1 データ未取込のため対象外
- [x] 必殺技・SA は未マッピング (M2 対応)
- [x] 統合ビュー `unified_moves` 作成・動作確認
  - 全体結合率: 94.3% (476/505件)
  - エンドツーエンド確認: サガット2HK/5HP、リュウ5LP、ルーク2MK → 全件期待値一致

---

## Phase 2: システム文書の取り込み (10〜14時間)

SuperCombo の Game Mechanics ページ群 (Gauges, Movement, Offense等) を
LLM が参照できる形で保存する。

### Task 2-1: 文書取得スクリプト (3時間)
- [ ] 取得対象ページのリスト確定 (Gauges, Movement, Offense, Defense, Glossary, HUD, Controls)
- [ ] ブラウザJSスニペットで各ページのHTMLを取得 (Cloudflare対策)
- [ ] テキスト抽出: 不要なナビゲーション・広告を除いた本文だけ抽出する Python パーサー
- [ ] 取得結果を Markdown または JSON で保存

### Task 2-2: 文書のチャンク分割 (3時間)
- [ ] 各ページを意味的なチャンク (h2/h3 単位) に分割
- [ ] 各チャンクにメタ情報を付与: ページ名、見出し、概念キーワード
- [ ] チャンクの長さ調整 (LLMコンテキストに収まるサイズ)

### Task 2-3: ベクトル化と検索基盤 (4〜6時間)
- [ ] 埋め込みモデル選定 (OpenAI text-embedding-3-small または無料の代替)
- [ ] チャンクをベクトル化して保存
  - 選択肢A: Supabase pgvector 拡張 (DBに統合)
  - 選択肢B: ローカルの SQLite + sqlite-vec
  - 選択肢C: Chroma 等のローカルベクトルDB
- [ ] 類似検索の動作確認: 「Drive Impact とは?」で関連チャンクが返るか

### Task 2-4: キャラページの解説テキスト取り込み (4時間)
- [ ] 既に手元にある Sagat の HTML から技ごとの解説テキスト (notes 相当) を抽出
- [ ] 同様にして他キャラも追加 (まずは サガットだけで動かして拡張)
- [ ] `sc_moves.notes` 列または別テーブルに保存

---

## Phase 3: LLM統合 (10〜14時間)

Gemini Flash と RAG を組み合わせた最小構成。

### Task 3-1: API キー取得と疎通確認 (1時間)
- [ ] Google AI Studio で Gemini API キー取得
- [ ] `.env` に `GEMINI_API_KEY` 追加
- [ ] 簡単なリクエストで疎通確認

### Task 3-2: LLMProvider 抽象化 (3時間)
- [ ] `llm_provider.py` インターフェース定義
- [ ] `GeminiProvider` 実装 (構造化出力対応)
- [ ] (将来用) `OllamaProvider` のスケルトンだけ作っておく
- [ ] 環境変数 `LLM_PROVIDER` で切り替え可能に

### Task 3-3: Intent Parser の実装 (3時間)
- [ ] ユーザーの自然言語クエリを構造化JSONに変換するプロンプト設計
- [ ] 出力スキーマ定義: `{type: "lookup_move" | "explain_concept" | ...}`
- [ ] サンプル質問10個程度で動作確認

### Task 3-4: RAG コンテキスト構築 (3〜5時間)
- [ ] Intent Parser の出力に応じて、関連するチャンクを取得
  - 「サガットの2HKって何?」→ サガットのフレームデータ + 2HK の解説
  - 「ドライブインパクトって何?」→ Gauges ページの該当チャンク
- [ ] LLMに渡すコンテキストを組み立てる関数
- [ ] プロンプトテンプレート設計: フレームデータ + 関連文書 + ユーザーの質問 → 回答

---

## Phase 4: CLI 統合と動作確認 (5〜8時間)

### Task 4-1: CLI コマンド `ask` の実装 (2時間)
- [ ] 既存の `cli.py` に `ask` サブコマンドを追加
- [ ] `python -m sf6_engine.cli ask "<質問>"` で動く形に

### Task 4-2: 統合テスト (2〜4時間) ✅ 2026-05-15 完了
- [x] 想定質問リスト20問作成 → tests/integration_test.py
- [x] 各質問を流して目視評価
  - 初回: 11✅ 6⚠ 3❌
  - 修正後: **14✅ 6⚠ 0❌ → M1 合格 🎉**
- [x] 失敗パターン修正:
  - 波動拳/昇竜拳/タイガーショット → 必殺技は input 省略 (プロンプト強化 + ポストプロセス検証)
  - punish_check の反撃可否を context に追記 (発生 N F 以内なら確定反撃)
- ⚠ 残課題: ゲーム概念 (Q7-11) は Phase 2 (文書取込) 完了後に改善予定

### Task 4-3: ドキュメント整備 (1〜2時間) ✅ 2026-05-15 完了
- [x] README.md 新規作成 (streetfighter6-engine/README.md)
  - セットアップ手順 (Ollama + Gemma4 + Supabase)
  - ask コマンドの使い方 / -v デバッグモード
  - データカバレッジ表
  - 失敗パターンログ
  - AWS EC2 リモート Ollama 利用手順
- [x] PROGRESS.md を「M1完了」状態に更新
- [x] M2 への引き継ぎ事項を README と PROGRESS.md に記載

---

## 進め方のヒント

### 1セッションで取り組むタスク数
週8時間ペースなら、1セッション (2〜3時間) で**1〜2タスク**が現実的。
無理に詰め込まず、「確実に1つ完成させる」を優先する。

### 詰まったときの対処
- **30分以上同じところで止まったら**: いったん別タスクに移る、または Claude に質問
- **データが想定と違う**: それは新しい発見。スキーマや方針を修正する判断を ADR に追加
- **やる気が出ない週**: スキップしてOK。8〜9ヶ月の長期戦で1週休んでも大局には影響しない

### 優先度の整理
- Phase 1 → Phase 3 → Phase 2 → Phase 4 の順でも進められる
  - Phase 2 (RAG) は時間かかる可能性がある
  - Phase 3 (LLM統合) を Phase 1 だけのデータで試すと、早く「動くもの」が見える

### M1 段階の妥協ポイント
- ✅ 通常技だけマッピングOK (必殺技は M2)
- ✅ サガットだけ解説テキスト整備OK (他キャラは M2)
- ✅ ベクトル検索の精度はそこそこでOK (チューニングは M2)
- ❌ 「動くけど嘘をつくAI」は不可 (LLM が知らないことを知らないと言える設計に)
