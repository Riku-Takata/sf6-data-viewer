# M1 タスク分解: 基盤データ統合とコア検索

**目標**: 自然言語で「サガットの2HKって何?」のように聞くと、フレームデータと
ゲーム文脈を踏まえた回答が返る最小システム (CLI)。

**期間**: 8〜10週 (週8時間 = 約60〜80時間)

**完成判定**: 以下が動く状態
- `python -m sf6_engine.cli ask "サガットの2HKの発生は?"` → フレーム情報を返す
- `python -m sf6_engine.cli ask "ドライブインパクトって何?"` → システム仕様を踏まえた説明を返す
- `python -m sf6_engine.cli ask "サガットの2HKでパニッシュカウンター取ったらどうなる?"` → 数値+解説を返す

## Post-M1: 技集合のフレーム条件検索 (2026-07-13)

- [x] `query_moves` Intentを追加し、集合条件を単一技名として扱わない
- [x] CAPCOM/UFD/SuperCombo統合プロファイル上で `on_block` 条件を比較
- [x] 確定一致・条件付き一致・範囲/未収録の判定保留を分離
- [x] MCP / Discord router / CLI RAGを接続し、summaryを決定論返却
- [x] alias聞き返しを明示的な単一技の `move_not_found` だけに限定
- [x] parser / query service / router / RAG のunittest 48件を通過
- [x] SAM再デプロイ後、AWS MCP本番経路で集合検索を確認 (2026-07-14)

## Post-M1: 略称・かな・ローマ字・英語の技名解決 (2026-07-16)

- [x] 現行の公式名部分一致、英語ILIKE、fuzzy、旧alias学習、canonical DDLを監査
- [x] `中ネク / 弱はしょう / English / romaji / 下デヨ`の解決・聞き返し契約を設計
- [x] read-only resolver、検索form、variant gate、型付きstatus、評価gateを設計文書とADR-035に記録
- [ ] 既存ソース行を使う共通`MoveResolver`を実装し、全ツールの重複resolverを置換
- [ ] かな/読み/ローマ字formと部分一致scoreを実装し、frozen corpusでprecision gateを通す
- [ ] `needs_command`をDiscordの同一利用者pendingへ接続し、確認後に元質問を再実行
- [ ] `canonical_moves`をバックフィル後、private/review済みshared alias workflowを有効化

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
- [ ] UFD元GIFのオンデマンドgeometry解析と追加実測で相打ち・追撃の到達証明を拡張

## Post-M1: 連続ガード・割り込み解析 (2026-07-14)

- [x] 現行link timelineをcancel連携に適用できないことをコードと実DBで監査
- [x] Ken `2MK blockstun=16`, `236LK/MK/HK startup=12/16/25`をSC/UFDで確認
- [x] `TransitionProfile`、cancel timeline、確度分離、回答契約をADR-029/設計書に定義
- [x] production `analyze_sequence` の `_fetch_defender_profiles` 引数不整合を修正
- [x] 旧`rag_builder` の `abs(block_adv)-startup` gap式を削除し共通serviceへ統一
- [x] 「2中K」「中/大迅雷脚」「連続ガード/割り込める」のIntent正規化を実装
- [x] `通常技→日本語必殺技名`を汎用sequence intentへ分解し、キャラ固有技名をハードコードせずDB resolverへ委譲
- [x] 一意近似名だけを強度/SA prefix保持で補正し、同名の弱中強/ODは聞き返す
- [x] `→ / > / から / の後に / AをBでキャンセル / into` を決定論分解
- [x] link/special cancel/SA cancel/同一状態のlight chainを分け、防御技未指定でも連続ガード/連続ヒットを計算
- [x] cancel不可のnormal-to-special/SAは、不可を明示したうえでafter-recovery linkとして計算
- [x] Ken `2MK -> 236LK/MK/HK` のgolden test、実DB E2E、更新済みAWS MCP E2Eを確認
- [x] 全30キャラのSC入力2,118件・CAPCOM公式名2,357件・ordered pair 103,073件を総合監査
- [x] AWS MCPを再デプロイし、`query_targets`スキーマ、Ryu special cancel/light chainを本番E2E確認
- [x] 技間gapと2技目ガード/ヒット後硬直差を別targetにし、質問視点を保持して回答
- [x] Ryu `5LP -> 214LP`で終端`-3/+3F`を主回答、gap 3Fを補足する実DB E2Eを確認
- [x] AWS MCPへ終端硬直差APIを再デプロイし、Bearer認証付き本番E2Eとエラーログ0件を確認
- [x] 連続ガード/割り込みtargetを分離し、yes/no・gapを1行目に置く短いsummaryへ変更
- [x] Ryu `5LP -> 214LP`を「いいえ、隙間3F」の2行回答として実DB確認
- [x] 結論先行summaryをAWS MCPへ再デプロイし、認証付き本番E2Eを確認
- [x] 単体技の`2中P`表記を`2MP`へ正規化し、`は発生何フレ`を技名spanから除外
- [x] Sagat `2MP`の発生7Fを実DB・AWS MCP・Discord回答生成で確認
- [x] 強度省略の割り込み質問を`pressure_family_analysis`として分離し、単一技の確反判定へ誤送信しない
- [x] `analyze_sequence_family`でfamily候補を既存timelineへ個別投入し、弱/中/強を比較する
- [x] Ken `迅雷`のレビュー済み代表始動`2MK`をdata化し、前提表示付きで通常版を回答する
- [x] `analyze_sequence_family`をAWS MCPへデプロイし、Discord Bot→本番MCPのE2Eを確認
- [x] 2キャラ+「主な技」+割り込みの曖昧質問を`matchup_interrupt_overview`へ決定論分岐
- [x] 全キャラの通常技→通常版必殺技cancelと防御側地上通常技を既存timelineで比較し、AWS MCP本番E2Eを確認
- [ ] Discord Bot常駐ホストへfamily Intent/Router更新を配布・再起動し、実メンションで確認
- [ ] 20〜50件のframe-step blind検証でoff-by-one規約を確定
- [ ] canonical move / transition edgeをバックフィルしpatch失効を接続

## Post-M1: SuperCombo時間派生値の独立検証 (2026-07-14)

> ADR-028によりSuperComboはCC BY-NC-SA 3.0条件下で本番補助ソースとして維持する。
> 以下の未完了項目はSC排除ゲートではなく、CAPCOM/UFD/SC間の精度・版整合性監査として継続する。

- [x] SCを正解ラベルだけに使い、CAPCOM/UFD値を入力にする全30キャラ監査を実装
- [x] 基本地上通常技360件を固定技名変換で結合し、フレーム値による対応付けを禁止
- [x] CAPCOM由来hitstunを289/304件で計算し、280/289件 (96.89%) 完全一致
- [x] Sagat 5MP対4F地上通常技46件をSC入力なしで46/46完全再現 (`+6～+12F`)
- [x] total / blockstun / punishAdv / afterDR / perfParryAdv とUFD単独を層別評価
- [x] hitstop・距離・状態・notesは基本フレームから一意に導けないと分類
- [x] 監査スクリプト、詳細レポート、Proposed ADR-024を追加
- [ ] 同一パッチのCAPCOM/UFD/SCスナップショットで再監査
- [ ] SC読み取り禁止のsource-isolation E2Eを追加し、候補列挙もcanonical IDへ移行
- [ ] 相手技固定20～50件の実測でtrade offset `0/-1` とhitstop差をblind検証
- [ ] 検証ゲート通過後にADR-024をActive化し、derived temporal profileを本番へ反映

## Post-M1: CAPCOM備考とSC非依存ランタイム設計 (2026-07-14)

- [x] CAPCOM全2,357行の備考・属性・無敵・armor・空中・飛び道具・juggle候補を棚卸し
- [x] `N F増加/減少` の結果別硬直claimを決定論抽出し、不一致への補正効果を評価
- [x] hitstun/blockstun/totalの31不一致セル・24技をCAPCOM/UFD/SC notesまで個別追跡
- [x] 距離・無敵・armor・飛び道具・juggle・空中状態を時間誤差と接触gateに分離
- [x] raw snapshot / canonical move / fact / note claim / rule / proof / observationの設計を策定
- [x] 詳細監査レポート、再実行スクリプト、Proposed ADR-025を追加
- [ ] CAPCOM raw HTMLとUFD assetをpatch/SHA付きimmutable snapshotへ移行
- [ ] SC由来`special_move_map`とaliasをCAPCOM command/UFD input/review済みaliasで再構築
- [ ] 公式備考parserのgolden corpusを作り、実行対象grammarでprecision 100%を確認
- [ ] SC oracleをproductionと別DBへ分離し、接続不能状態のsource-isolation E2Eを追加
- [ ] `recovery_by_result / active_segments / contact_phase / variant_state` を型付き実装
- [ ] geometryとblind trade観測を投入し、ADR-024/025のActivation gateを通す

## Post-M1: 会話から更新する戦術知識基盤 (2026-07-14)

- [x] 現行の単発Intent、Discord会話状態、alias学習、観測、RAG経路を監査
- [x] 否定・仮説・伝聞・訂正・照応を含む会話contextプローブを実行 (期待一致1/10)
- [x] `sequence_observations`のreview/patch/conditions/競合安全性を再現 (安全0/3)
- [x] private/shared、同意、review、訂正、競合、失効、撤回のテスト専用契約を検証 (18/18)
- [x] identity、conversation、scenario、claim、evidence、review、RLS、削除の設計を策定
- [x] 180会話+否定minimal pair 120組の評価計画とrelease gateを定義
- [x] 詳細設計、再実行スクリプト、Proposed ADR-026を追加
- [ ] ADR-024/025のSC非依存canonical fact基盤とsource-isolation E2Eを先に完成
- [ ] `DialogueTurnAnalysis`のgolden-dev 90会話、frozen holdout 60会話、challenge 30会話を作成
- [x] 短期context compilerを実装し、polarity/照応/epistemic gateを単体テストで通す（8/8）
- [ ] 180会話frozen評価でread-only context compilerのrelease gateを通す
- [ ] 主体付き短命token、private RLS、consent、export/deleteを実装してからprivate memoryを有効化
- [ ] evidence/review/conflict workflowとreviewer専用権限を実装してからshared knowledgeを有効化
- [ ] 連携の決定論回答へlabel付き戦術contextを統合し、SC接続不能E2Eを通す

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
