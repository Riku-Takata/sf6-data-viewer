# PROGRESS

このファイルは「**今どこまで進んだか / 次に何をやるか**」を一目で把握するための
進捗管理ファイル。各セッション終了時に更新し、次回セッション開始時に最初に読む。

---

## 🎯 現在のフェーズ

**Milestone**: **決定論的な連携・相打ち・追撃解析 ✅ ローカル/AWS本番反映済 (2026-07-13)**
**現在**: 2技連携を共通タイムラインで計算し、CAPCOM主値+UFD/SC補完、相手技固有
hitstun、ディレイ、相打ち後の両視点有利差、追撃確度をIntent/MCP/Discord/RAGで共通化。
サガット5MP→5MPは、4F地上通常技46件を技別計算して `+6～+12F`。`Ryu 2LP`は
`+9/-9`、`Sagat 2LP`は`+7/-7`となることを実DB E2E確認済み。
**追加（ローカル、2026-07-13）**: 技の集合質問を `query_moves` として型付きフレーム条件へ
正規化し、統合プロファイル上で確定・条件付き・保留を分離して検索する。質問文を別名として
登録しないガードも追加した。関連unittest 48件は通過済み。
**本番反映（2026-07-14）**: 全30キャラの同形質問を実データで確認後、`query_moves` を含む
MCP Lambdaを再デプロイした。CloudFormationは `UPDATE_COMPLETE`、ローカルフォールバックを
無効化した本番MCPの検索も成功した。
**追加検証（2026-07-14）**: SCを正解ラベルだけにした独立監査で、CAPCOM由来hitstunは
基本地上通常技280/289件 (96.89%) 完全一致。Sagat 5MP対4F技は46/46件で`+6～+12F`を
SC入力なしに再現した。時間派生値のSCランタイム依存廃止は実行可能だが、trade式の`-1F`・
hitstop相殺・距離/状態は独立実測が必要 (ADR-024 Proposed)。
**備考再監査（2026-07-14）**: CAPCOM備考1,781/2,357行、属性2,032/2,357行を確認。
通常技の不一致31セルは公式備考だけで7件完全補正+2件部分補正、UFDの独立条件まで加えると
15件を説明可能。残り12件は現取得データでSC-only条件、4件は未整合。SCを本番から物理分離し、
公式fact・備考claim・geometry・独立観測・導出proofへ分けるADR-025をProposedとした。
**更新型Bot実装（2026-07-14）**: ADR-026の安全な縦切りとして、同一主体・会話の短期context、
否定/仮説/伝聞/注入のgate、明示「保存する」による本人限定privateメモ、patch/fingerprint完全一致検索、
証拠+review必須のshared workflowをローカル実装し、追加8件と既存関連62件を通過した。既定は永続化disabled、
SC依存のglobal alias即時学習もdisabled。Supabase migrationの8テーブルは適用済みで、Bot設定を
`supabase`+HMACへ有効化し、MCP Lambdaを`UPDATE_COMPLETE`まで再デプロイしてBearer initialize HTTP 200を確認した。
主体JWT/RLS gateway、180会話golden/holdout、Discord Bot常駐先の特定・再起動は未実施。
**Storage整理（2026-07-14）**: UFD GIF 773件が4,207,176,129 bytesを使用していた。
孤児・重複がないことを確認後、復旧用manifestをローカ保存してStorageから全削除。
DBのStorageパス/ハッシュは0件、UFD元URLは775件保持。今後のGIF保存は `--gifs` 指定時のみ（ADR-027）。
**SuperCombo利用方針（2026-07-14）**: SuperCombo WikiのCC BY-NC-SA 3.0表示を前提に、
帰属、ライセンスリンク、改変表示、非営利、ShareAlikeを守って当初通り本番の補助データに使用する。
ADR-024/025のSC分離提案はADR-028で取り消し、独立監査だけ回帰検証として維持する。
**連続ガード設計（2026-07-14）**: 現行の2技連携はlink式のみで、cancel連携を誤判定することを確認。
Ken `2MK -> 236MK` はSC blockstun 16Fと中迅雷startup 16Fでgap 0F、`2MK -> 236HK`は
25-16=9Fの行動可能時間。link/cancel/chain/専用派生を分けるADR-029と設計書を追加。
**専用派生ルール実装（2026-07-14）**: `~` を含む全30キャラの入力を個別edgeとして扱う。
SuperCombo注記に直接書かれたblock gap/true blockstringだけを即時判定し、強度・状態・派生windowが
明記されないものはlink/cancelへfallbackしない。`source_transition_rules` migrationとレビュー候補
importerを追加。419候補中71件は直接根拠あり、330件はtiming review待ち、7件はソース値競合。
ローカル/AWS MCPではA.K.I. 5LP→5LP~LP（3F gap）、豪鬼214HP→214HP~6P（連続ガード）、
Ken中迅雷→派生（保留）を確認済み。MCP LambdaはUPDATE_COMPLETE。
**Storage調査の訂正と復元（2026-07-14）**: 削除前の`sf6-html-archive`は60件・
24,436,612 bytesだけであり、1GB超過警告の直接原因ではなかった。誤って削除したHTMLは
CAPCOM公式から再取得し、`current/`と`previous/`の各30件（計60件・24,848,282 bytes）として復元。
`move_snapshots.raw_html_uri` も4,637件を再接続した。Layer 1は元どおり毎回HTMLを
ローテーション保存する設定へAWS本番を含めて戻した。UFD GIFバケットは意図どおり0件のまま維持する。
**全キャラ共通の自然言語連携解析（AWS MCP本番反映済み、2026-07-16）**: Intent Parserのキャラ固有必殺技マップと
個別誤記表を廃止し、技名は不透明なままCAPCOM/UFD/SC統合resolverへ渡す。`→ / から / の後に /
AをBでキャンセル / into`に対応し、link、special/SA cancel、同一状態の地上弱攻撃chainを別timelineで計算する。
全30キャラのSC入力2,118件とCAPCOM公式名2,357件は未検出0件、同名の強度省略263件は誤選択せず
聞き返す。ordered pair 103,073件は70,006件を数値解決、33,145件をscalar不足の理由付き保留とした。
実DB E2EでRyu `5LP -> 214LP` gap 3F、`5LP -> 2LP` gap -5F、Chun-Li `5MP -> 236LK` gap -10Fを確認した。
AWS MCPはCloudFormation `UPDATE_COMPLETE`、本番ツールスキーマの`query_targets`公開、
Ryu `5LP -> 弱波衝撃`=`214LP`/gap 3F、`5LP -> 2LP`=chain/gap -5FをBearer認証付きで確認済み。
**連携質問の終端硬直差（AWS MCP本番反映済み、2026-07-16）**: 「5LP→弱波衝撃をガードして何F有利？」を
単なるgap質問として処理していた原因を、1技目の接触、2技目の接触、相打ち後結果の混同に分解した。
`terminal_frame_advantage`と終端interaction/視点を追加し、実DBで2技目`214LP`の攻撃側-3F・
ガード側+3Fを主回答、技間gap 3Fを補足として返すことを確認。unittest 129件通過。
MCP LambdaはCloudFormation `UPDATE_COMPLETE`、認証付き本番E2E成功、デプロイ後エラーログ0件。
**技名の多表記・未知通称設計（設計のみ、2026-07-16）**: `中ネク`、`弱はしょう`、英語、
ローマ字はDB由来の公式名/読み/英語formから一意候補を解決し、`下デヨ`のように字面が無関係な
通称はコマンドを聞く設計をADR-035へ記録。共通read-only resolver、variant優先gate、
型付きclarification、session-only確認、review済みshared alias、frozen評価gateを定義した。
実DBではcanonical move/aliasが0件、旧global aliasが2件のため、実装時も先にread-only解決と
session確認を導入し、旧global UPSERTは再有効化しない。
**連携回答の結論先行化（AWS MCP本番反映済み、2026-07-16）**: `blockstring`と`interrupt`を別targetにし、
連続ガード/割り込みのyes/noとgap/先行Fを1行目へ移動。blockstun、cancel可否、hitstop基準は
構造化結果へ保持し、単純質問の前段から除外した。Ryu `5LP -> 214LP`は「いいえ、隙間3F」+
距離注意の2行となることを実DBと認証付き本番MCPで確認。CloudFormationは`UPDATE_COMPLETE`。
**方向数字+日本語強度の単体技修正（2026-07-16）**: 「サガットの2中pは発生何フレ？」で
`は発生何フレ？`まで技名へ混入していた。技spanの`は+質問項目`境界を追加し、`[1-9]+弱中強+P/K`
を単体技でもSC入力へ正規化、Discord routerが原文で上書きしないよう修正した。Intentは`2MP`、
実DB/AWS MCPは「しゃがみ中P（ミドルフック）、発生7F」、Discord回答生成はCAPCOM公式7Fを返した。
unittest 134件が成功。
**強度省略の割り込み質問（AWS MCP本番反映済み、2026-07-16）**: 「ケンの迅雷って割り込める？」を
`punish_check`へ送っていた経路を、`pressure_family_analysis`と新MCP `analyze_sequence_family`へ分離した。
単体resolverの強度曖昧性は保ったまま、family比較時だけ候補を列挙し、各variantを既存
`analyze_sequence`で計算する。レビュー済みdataにKen `迅雷 -> Jinrai Kick / 2MK`を登録し、
通常版は弱12F/-4F、中16F/0F、強25F/9F（発生8F以下が時間上先行、9F同時）として前提付きで返す。
未登録familyは始動技を聞き返す。unittest 140件が成功し、CloudFormation `UPDATE_COMPLETE`と
認証付きDiscord Bot→AWS MCP E2Eを確認した。
**実装・運用状態の記録（2026-07-16 14:48 JST）**: 上記の最終表示（結論先行、弱/中/強の個別結果、
距離等の注意書き）はAWS MCPへ再デプロイ済み。`SF6_MCP_LOCAL_FALLBACK=0`で同一質問を実行し、
本番MCPまでのE2Eで期待どおりの回答を確認した。実装は`661b1d6`（`main`/`origin/main`）に記録済みで、
この進捗記録の更新のみが未コミットである。
一方、実際にDiscordへ応答する常駐Botホストへのコード配布・再起動は、このリポジトリ/AWS権限の範囲外であり、
ライブのメンションによる最終確認は未完了。
**次**: Discord Bot常駐ホストへIntent Parserを配布・再起動して例のメンション質問を再確認する
（30〜60分）。略称対応に着手する場合は、共通read-only `MoveResolver`とfrozen corpusの縦切りを
先に実装する（4〜6時間）。その後に`needs_command`のsession確認を接続する（2〜3時間）。
`source_transition_rules_migration.sql`の71件stageはこれらと独立して継続する。

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

**M4 完了 🎉 — AWS リモート MCP サーバが本番稼働中**

- エンドポイント: API Gateway (HTTP API, stage prod) → Lambda `sf6-mcp-server`
  URL は CloudFormation 出力 `McpEndpoint` / 認証は SSM `/sf6/mcp/auth-token` の Bearer
- 公開ツール8種すべて本番疎通確認済 (DB照会 / Bedrock Titan / パッチ状況 / query_moves)
- クライアント登録: リモートMCPとして URL + `Authorization: Bearer <token>` を設定すれば利用可

以下は実装時の方針メモ (ADR-017):

**M4: AWS リモート MCP サーバ切り出し (ADR-017 で方針確定)**

LLM 段 (intent_parser / generate_answer) はサーバから外し、決定論ロジック層のみを
MCP ツールとして公開。ホスト LLM (Claude Desktop / 将来 Bot) が推論役を担う。

実装ステップ:
1. [x] `mcp_server/` を FastMCP で作成、決定論ツール5種をラップ ✅ (2026-06-08)
       (lookup_move / check_punish / compute_setplay / analyze_combo / list_moves)
       → `src/sf6_engine/mcp_server/{server.py,README.md}`、実DB スモークテスト全通過
2. [x] `doc_chunks.embedding_titan vector(1024)` カラム追加 + 72チャンク Titan v2 再埋め込み ✅ (2026-06-08)
       → migration SQL 適用済 / IAM の BedrockDenyAllOtherModels に Titan ARN 追加で疎通
       → reembed_titan.py で 72/72 成功 / search_docs_titan RPC 実クエリ検証OK
       → (任意) migration STEP 3 の IVFFlat 索引は未実行 (72件なら全件スキャンで実用上問題なし)
3. [x] `search_system_docs` MCPツール追加 (Bedrock Titan + search_docs_titan ハイブリッド) ✅ (2026-06-08)
4. [x] Streamable HTTP stateless 化 + SAM (Lambda + API GW + トークン認証) ✅ デプロイ済 (2026-06-08)
5. [x] Lambda 実行ロールに `bedrock:InvokeModel` + SSM(Supabase接続情報) ✅
6. [x] デプロイ → 本番エンドポイントで全7ツール疎通確認 ✅
       → 残: クライアント(Claude Desktop等)へのリモート登録 (URL + Bearer ヘッダ)

確定事項: 稼働先=AWSリモート / RDB=Supabase維持 / 埋め込み=Bedrock Titan v2

---

現在の対応範囲 (値が原典に存在する場合。未収録は明示的にデータなし):
- 型付きフレームデータ照会 (全30キャラ / 通常技・特殊技・必殺技・SA)
  - 発生 / 実持続区間 / 硬直・着地硬直 / 総動作 / ヒット・ガード差
  - ガードさせた側=攻撃側、ガードした側=防御側を必ず両視点で機械算出
  - CAPCOM主値 + UFD/SC補完 + ソース差異・条件注記
- フレーム上の反撃候補 (単一のガード硬直差。距離未検証なら確定反撃とは断定しない)
- コンボ接続検証・最大コンボ計算 (ビームサーチ)
- セットプレイ (KD後の起き攻め択計算、全キャラダッシュF対応)
- 派生技の割り込み判定 (フレームギャップ自動計算)
- パッチ検知時のメール通知 (AWS SNS + SSM)

**現行ローカル実装の検証 (2026-07-13)**: unittest **79/79**、統合監査
**92,940 assertions / 0失敗**、Discord Bot実経路 **9,728/9,728**
(発生/持続/硬直/攻撃側/防御側 各1,790 + 確反提案・判定保留778)。

**M4 候補 (優先度順):**
1. **AWS 完全移行** — Bedrock Gemma3 + Lambda/API Gateway で CLI をクラウド化
   - BedrockProvider 実装 (LLMProvider 抽象化済みなので 1ファイル追加)
   - SNS 確認メールの承認 (初回パッチ検知時)
2. ~~**統合テスト拡充** — setplay_analysis / punish_check + 派生 を 20問追加~~ **✅ 完了 (2026-05-29)**
3. **Web UI** — Slack Bot or 簡易 Web フロントエンド

## 📝 直近のセッションログ

### 2026-07-16 ★ 連続ガード・割り込み回答を結論先行へ変更
- **原因**: `blockstring` summaryが計算入力を順番に説明し、質問への結論を3段落目に置いていた。
- **Intent**: 連続ガード/gapの`blockstring`と、指定技での`interrupt`を別focusへ分離。
- **回答**: yes/noまたは判定保留を1行目、距離等の適用範囲を2行目に限定。詳細値はJSONに保持。
- **検証**: Ryu実DBで`5LP -> 214LP`の2行回答とgap 3F、全unittest 132件成功。AWS反映はこの後実施。

### 2026-07-16 ★ 技名の略称・かな・ローマ字・英語解決を設計
- **現行監査**: 名前解決が`frame_data`/`rag_builder`/MCP/Discordへ分散し、旧alias登録が
  variantをfamilyへ拡大して即時global保存する問題を確認。現在は安全上disabled。
- **DB確認**: `中 タイガーネクサス=214MK`、`弱 波掌撃=214LP`を確認。
  `canonical_moves=0`、`canonical_move_aliases=0`、旧`move_aliases=2`。
- **設計**: DB由来検索form、読み/romaji、部分一致の一意性gate、`needs_command`、同一利用者pending、
  session/private/reviewed sharedの段階公開、precision 99.5%等のrelease gateを定義。
- **成果物**: `docs/MOVE_ALIAS_RESOLUTION_DESIGN.md`、ADR-035。今回は本格実装・DB変更・AWS反映なし。

### 2026-07-16 ★ 連携質問の終端硬直差を文脈どおり回答
- **原因**: `initial_interaction`しかなく、`post_interaction_advantage`も相打ち専用だったため、
  cancel解析が2技目の`on_block/on_hit`を読まずgap summaryで終了していた。
- **Intent/API**: `terminal_state`と`terminal_frame_advantage`を追加し、曖昧な「ガードして」は両視点、
  「ガードした側」「攻撃側」は明示視点としてMCPまで保持する。
- **回答**: 終端硬直差を先頭、連続ガード/gapを補足に変更。Ryu実DBで`214LP`の-3/+3F、gap 3Fを確認。
- **回帰/本番**: parser/evaluator/router/RAGを含むunittest 129件が成功。MCP Lambdaを再デプロイし、
  CloudFormation `UPDATE_COMPLETE`、Bearer認証付き本番E2E成功、デプロイ後エラーログ0件を確認。

### 2026-07-16 ★ 全キャラ共通連携解析をAWS MCPへ本番反映
- **デプロイ**: `sam build --template-file template-mcp.yaml`と`sam deploy` が成功。
  `sf6-mcp-server` はリソース置換なしでCloudFormation `UPDATE_COMPLETE`。
- **スキーマ確認**: 認証付き本番MCPの`analyze_sequence` input schemaに`query_targets`があることを確認。
- **本番E2E**: Ryu `5LP -> 弱波衝撃`は`5LP -> 214LP` / special cancel / gap 3F / `gap_open`、
  Ryu `5LP -> 2LP`はchain / gap -5F / `true_blockstring`を返した。
- **運用確認**: デプロイ後のLambdaログにERROR、Traceback、timeoutは0件。

### 2026-07-16 ★ 全キャラ共通の自然言語連携解析へ拡張
- **汎用技名解決**: sequence parserの迅雷脚強度mapと`波衝撃`個別補正を削除。キャラ固有技名は
  不透明なまま統合DB resolverへ渡し、強度/SA prefixを保存した一意近似名だけを安全に補正する。
- **自然文分解**: `→`, `>`, `から`, `の後に`, `AをBでキャンセル`, `into`をLLMなしで
  `sequence_analysis`へ正規化。連続ガードだけでなく連続ヒット/コンボ質問も初期interactionを分ける。
- **遷移モデル**: link、special cancel、SA cancel、`Chn`根拠の同一状態地上弱攻撃chainを分離。
  cancel不可の必殺技/SAは、不可を明示して出し切り後のlinkとして計算。専用`A~B`は従来どおり根拠なしで推測しない。
- **全件監査**: `tests/sequence_comprehensive_audit.py`を追加。SC入力2,118件とCAPCOM公式名2,357件は
  未検出0件、曖昧な強度省略名263件は聞き返し、ordered pair 103,073件のうち70,006件を数値解決した。
- **E2E/回帰**: Ryu `5LP -> 214LP`=gap 3F、Ryu `5LP -> 2LP`=chain/gap -5F、
  Chun-Li `5MP -> 236LK`=gap -10Fを実DBで確認。関連unittest 91件が通過。
- **未反映**: ローカル変更のみ。Discord Bot常駐プロセスとAWS MCPへの配布・再起動は未実施。

### 2026-07-14 ★ Storage調査の判断訂正・HTMLアーカイブ復元
- **判断訂正**: 1GB警告の調査時点でHTMLアーカイブは60件・約24.4MBだけであり、現在の
  Storage実体による容量超過ではなかった。HTML削除とアーカイブ無効化は不要な変更だった。
- **復元**: `sf6-frame-scraper`を元のcurrent→previousローテーションへCloudFormation
  `UPDATE_COMPLETE`まで戻し、全30キャラを2回取得。`current/`・`previous/`各30件、計60件・
  24,848,282 bytesを再作成した（削除前の過去内容そのものではなく、復元時点のCAPCOM公式HTML）。
- **参照復元**: NULL化した過去snapshot 2,281件をキャラ別`current/`へ再接続し、既に復元処理で
  書き戻された2,356件と合わせて`raw_html_uri`は4,637件となった。UFD GIFの削除は維持する。

### 2026-07-14 ★ 全キャラ専用派生edge・レビュー運用の実装
- **安全な派生判定**: `A~B`を通常link/special cancelとして扱わず、SuperCombo注記に直接ある
  `Nf blockstring gap` / `true blockstring`だけを`direct_block_note`として実行。強度指定の注記は
  同系統の他強度へ流用せず、注記なしのKen中迅雷→派生は`transition_unresolved`で止める。
- **全キャラ候補化**: snapshot全30キャラを監査し、419個のsource-input edgeに正規化。
  直接根拠71件、要timing review330件、source競合7件、親技不足11件を区別した。
- **永続化準備**: `source_transition_rules_migration.sql`と`source_transition_rules` importerを追加。
  DBには`reviewed=false`でのみstageし、runtimeはreview済みexact ruleを最優先、migration未適用時は
  SuperComboの直接注記ルールへ安全にフォールバックする。
- **回帰防止と本番**: `Chn`を無条件99F有利にする最大コンボの旧推測を廃止。unittest 66件、
  SAM validate/build、CloudFormation UPDATE_COMPLETE、認証付き本番MCP4ケースを確認した。

### 2026-07-14 ★ 連続ガード・割り込み解析の実装・AWS MCP更新
- **実装**: `sequence_analysis` にlinkと最速normal→special cancelの遷移判定を追加。SuperComboの
  `cancel=Sp`、blockstun/hitstun、統合startupを使い、hitstop終了後を共通0Fとして
  `true_blockstring` / `interrupt_timing_win` を時間上の結論として返す。遷移根拠がないspecialは
  link式へfallbackせず保留する。
- **自然文・経路統一**: `2中K`、中/大迅雷脚、連続ガード、割り込めるをLLMなしでsequence intentに
  正規化。旧RAGの`abs(block_adv)-startup`派生gap式を削除し、CLI/RAG/MCP/Discord Routerの連携判定を
  共通serviceへ寄せた。
- **検証**: unittest 102/102、Ken `2MK -> 236LK/MK/HK` golden、実Supabase経路、Discord Routerの
  local MCP経路を確認。実データで中迅雷脚はgap 0F（連続ガード）、大迅雷脚はgap 9F・generic 4Fが
  5F先にactiveとなることを確認した。
- **全キャラ範囲監査**: SC全2,118行では、29キャラ・257個の通常技がspecial cancelと単一の
  blockstunを持ち、491個の通常必殺技起点に時間判定を適用できる。Ryu `2MK -> 236LP`も本番MCPで
  gap 0Fの連続ガードを確認した。一方、`~`を含む専用派生は30キャラ・376行あり、chain/派生windowを
  別モデル化するまで本機能の対象外とする。
- **デプロイ**: `sam validate --lint`、arm64 `sam build`、`sf6-mcp-server`のCloudFormation
  `UPDATE_COMPLETE`を確認。本番MCPの`analyze_sequence(ken, [2MK, 236HK], 4F)`が
  `gap_open / 9F / interrupt_timing_win`を返すことを検証した。
- **運用境界**: Discord Botの常駐ホスト定義はリポジトリ/AWS権限内に無いため、Botプロセスへの
  intent parser更新の配布・再起動はホスト特定後に行う。既存Botが同じMCP URLを呼ぶ連携計算自体は
  本デプロイで反映済み。

### 2026-07-14 ★ 連続ガード・割り込み解析の現状監査と再設計
- 現行`sequence_analysis` が1技目のrecovery後を基準とするlink式のみで、cancel、chain、専用派生を区別しないことを確認。
- Supabase実データでKen 2MKのblockstun 16F / special cancel可、迅雷脚の弱中強startup 12/16/25Fを確認。
- 標準最速cancelなら弱・中は連続ガード、強はgap 9Fで4F技が時間上5F先にactiveとなる基準ケースを定義。
- productionのgeneric 4F経路のTypeError、旧RAG gap式、自然文variant解決、遷移観測0行をブロッカーとして記録。
- `docs/BLOCKSTRING_ANALYSIS.md`、ADR-029、Post-M1タスクを追加。実装とAWSデプロイは未実施。

### 2026-07-14 ★ SuperComboをCC BY-NC-SA 3.0条件下で継続利用
- SuperCombo Wikiの表示ライセンスを前提に、本番ランタイムの補助データとして維持するADR-028をActive化。
- 帰属に加え、非営利、ShareAlike、ライセンスリンク、HTML除去・数値正規化・入力変換等の改変表示を必須条件とした。
- `THIRD_PARTY_DATA.md` とREADMEにSuperCombo Wiki contributorsへの帰属、参照先、ライセンス、改変内容を追加。
- ADR-024/025のSC分離方針はSupersededとし、独立監査と型付きclaim設計は精度検証として残す。

### 2026-07-14 ★ SC遮断ローカル変更を取り消し
- `RuntimeSourceClient` と `sc_moves` fail-closed境界、CAPCOM/UFD専用profile、SC依存機能の停止を取り消し、
  CAPCOM主値 + UFD・SuperCombo補完へ復帰した。コンボ、セットプレイ、連携、技一覧、RAGは従来のSC補助経路を維持する。
- 本番にはSC遮断版をデプロイしていなかったため、AWS/Discordの稼働状態は変更していない。
- 関連unittest 90件、Python構文検査、`sam validate --lint` を通過した。

### 2026-07-14 ★ SuperCombo非依存・更新型チャットBotの設計テスト
- **現行context監査**: 明示的な初回連携は解析できるが、否定、仮説、伝聞、訂正、前ターン照応を
  含む期待10件中1件だけ一致。関連する既存unittestは42/42通過し、既存機能の回帰ではなく
  会話知識用schemaとsession stateの不足だと切り分けた。
- **既存観測の安全性**: 証拠なし・unknown patchのreview済み行を受理し、旧patch/画面端限定観測を
  条件未指定質問へ採用し、同confidence競合を入力順で選ぶことを3/3プローブで再現した。
- **提案契約**: 質問/仮説/伝聞の非昇格、明示同意、本人限定検索、人間review、injection隔離、
  訂正revision、競合保留、patch失効、撤回のテスト専用状態機械を18/18通過した。
- **設計**: raw会話、typed scenario、claim/evidence/relation/review/consent、eligible viewを分離し、
  単一Bearer/service-role/public-readを主体付きtoken+RLSへ移す段階設計を策定した。
- **成果物**: `tests/conversational_knowledge_design_eval.py`、
  `docs/CONVERSATIONAL_KNOWLEDGE_DESIGN.md`、ADR-026 (Proposed)。本番実装は行っていない。

### 2026-07-14 ★ 更新型チャットBotの安全な縦切り実装
- **会話compiler**: `conversation_knowledge.py` に会話単位の型付きscenario/candidate、同一主体だけの
  30分TTL照応、否定訂正、仮説/伝聞/質問の非昇格、HMAC subject key、PII伏せ字を実装した。
- **保存・検索**: `conversation_service.py` と `knowledge_repository.py` で「記録して」→「保存する」の
  明示確認、private-first、patch/fingerprint完全一致、撤回、証拠+review後のみsharedを実装した。
  `sql/conversational_knowledge_migration.sql` はRLS有効・public policyなしで追加したが未適用。
- **Bot統合**: privateメモは本人へ未検証ラベルを付けて決定論回答の後に表示し、従来のSC依存global alias
  即時学習は `SF6_ENABLE_LEGACY_SC_ALIAS_LEARNING=1` を明示しない限り無効にした。
- **検証**: 新規 `test_conversation_knowledge` 8/8、関連する既存テストを含め70/70成功。Discord import smokeも成功。
  Supabase/AWSへの書込み・migration適用・デプロイは行っていない。

### 2026-07-14 ★ 永続メモ有効化・MCP本番デプロイ
- **Supabase**: `knowledge_*` 8テーブルの存在を匿名readで確認（全て0件）。Discord Bot設定へ
  `SF6_KNOWLEDGE_STORE=supabase` と新規HMAC secretを設定し、service repositoryで空検索できることを確認した。
- **AWS MCP**: `sam validate --lint`、arm64 `sam build`後、`sf6-mcp-server` をデプロイ。
  CloudFormation `UPDATE_COMPLETE`、Lambda `Active`、Bearer付き`initialize` HTTP 200を確認した。
- **残る運用境界**: Botを常駐させるAWS定義はなく、deployerにはECS/EC2/App RunnerのList/Describe権限もない。
  実Botの再起動はホストまたは運用サービスを特定してから行う。主体JWT/RLS gateway・reviewer権限・holdout評価も未完了。

### 2026-07-14 ★ CAPCOM備考/UFDによる不一致再監査とSC非依存設計
- **公式データinventory**: CAPCOM 2,357行中、備考1,781行 (75.56%)、属性2,032行
  (86.21%)。明示的な結果別硬直209 claim、無敵453行、空中判定272行、armor67行を確認した。
- **状態coverage**: 無敵記載は適合率94.6%/再現率74.3%、自身の空中判定100%/71.1%、
  飛び道具存在は属性`弾`で72.7%/97.4%。一方、数値rangeの再現率0.55%、juggle数値と
  弾速数値の公式記載は0件で、公式表だけでは復元不能。
- **不一致31セル**: CAPCOM備考で7件完全補正+2件部分補正。UFDの条件値・notesを加えると
  計15件の原因を独立に特定。12件は現取得データでSC-only、4件はSC値でも未整合。
- **原因**: outcome別recovery、接触phase、固定ガード回復、variant identityが中心。
  距離/無敵/armor/飛び道具/juggle/空中は接触成立・結果状態・branch選択のgateだった。
- **成果物**: `tests/supercombo_context_audit.py`、`docs/SUPERCOMBO_CONTEXT_AUDIT.md`、
  ADR-025 (Proposed)。ランタイムコード、DB、AWS本番は未変更。

### 2026-07-14 ★ SuperCombo時間派生値のCAPCOM/UFD独立検証
- **リーク防止監査**: 全30キャラ・基本地上通常技360件を固定技名変換だけで対応付け、SCは
  正解ラベルに限定。CAPCOM 2,357 / UFD 1,559 / SC 2,118行を別ソースとして評価した。
- **結果**: CAPCOM由来hitstunは280/289件 (96.89%)、版整合相当層234/236件 (99.15%)。
  total 266/268、blockstun 297/317、afterDRHit 297/299。UFD単独hitstunは230/263で、
  パッチ不明・行内不整合を無条件補完しない方針とした。
- **相打ちケース**: Sagat 5MPと4F地上通常技46件はCAPCOMだけで46/46完全一致し、
  `+6～+12F`、Ryu 2LP `+9F`、Sagat 2LP `+7F`を再現した。
- **境界**: 現行trade式末尾の`-1F`とhitstop相殺はSC由来値の再計算しかなく、実ゲーム真値は
  未検証。hitstop・距離・状態・notesは基本フレームと分離し、実測/geometryを要求する。
- **成果物**: `tests/supercombo_inference_audit.py`、`docs/SUPERCOMBO_INFERENCE_AUDIT.md`、
  ADR-024 (Proposed) を追加。ランタイムコードとAWS本番は未変更。

### 2026-07-14 ★ 全キャラ集合検索を検証し、AWS MCPへ再デプロイ
- **全キャラ実データ検証**: 30キャラすべてについて「{キャラ}の技の中でガードさせて有利な技は？」を
  決定論Intent、MCP引数、Supabase実データ検索まで通し、30/30で `query_moves` として成功。
  各キャラで基準値一致・条件付き一致・保留を含む型付き結果を返した。
- **デプロイ**: `sam validate --lint`、arm64ローカルビルドを通過後、
  `sf6-mcp-server` を更新。Docker未起動のためコンテナビルドは使わず、arm64ホストと
  Python 3.12 arm64ランタイムの一致を確認して通常ビルドを使用した。
- **本番確認**: CloudFormation `UPDATE_COMPLETE`。ローカルフォールバックを無効化した
  本番MCPで `query_moves(rashid, on_block > 0, attacker)` が `found=true`、
  基準値一致3件・条件付き15件・保留20件を返すことを確認。

### 2026-07-13 ★ 技の集合フレーム条件検索と別名学習ガードを実装
- **Intent/MCP**: 「ラシードの技の中でガードさせて有利な技は？」を、
  `on_block > 0 / attacker / all` の `query_moves` へLLMなしで正規化。
  MCP、Discord router、CLI RAGを接続し、summaryはLLMに再要約させない。
- **検索契約**: `lookup_frame_data()` と同じCAPCOM主値+UFD/SC補完、視点反転、
  scenario評価を各候補へ適用。確定一致・条件付き一致・範囲/未収録による保留を分離し、
  ガード不成立を数値検索から除外する。
- **安全性**: alias聞き返しは明示的な単一技 `move_not_found` に限定し、集合検索0件、
  キャラ未解決、ツールエラーを登録対象から除外。登録MCPも集合表現を拒否する。
- **検証**: parser、統合検索、router、決定論回答のunittest **48件**を通過。
  AWS MCPへのSAM再デプロイとDiscord本番E2Eは未実施。

### 2026-07-13 ★ 連携・相打ち後有利・追撃の決定論解析を実装
- **Sequence Engine**: 2技連携を共通タイムラインへ配置し、ガード/ヒット後有利、両者の
  ディレイ、最速暴れ、同時発生、相打ち後の両視点有利差を計算。相手技未指定なら
  SCの該当発生技群からhitstunモデル区間を返し、単一値を作らない。
- **ソース統合**: 発生/ガード差等はCAPCOM主値+UFD/SC補完の既存統合プロファイル、
  `hitstun / blockstun / hitstop / atk_range / notes` はSC補助根拠として合成。パッチ変化で
  フレーム指紋が変われば旧観測を自動失効する。
- **自然言語と全経路統合**: `sequence_analysis` Intentを追加。複数キャラ名から攻撃側/暴れ側を分離し、
  相手技固有の計算に対応。MCP `analyze_sequence`、Discord local/AWS router、CLI RAGを共通化し、
  最終summaryはLLMに再要約させず数値・視点・確度を保存する。
- **観測と保証レベル**: `sequence_observations` DDL、スキーマ検証付きJSON、upsertインポーターを追加。
  観測keyとレビュー条件に相手キャラ+技を必須化。相手技IDのない過去の`+7F / 2MP`報告は
  未レビュー資料へ降格し、回答から除外した。追撃は timing/spatial/state/confirmed を分離する。
- **技別計算**: サガット5MPのhitstun 25Fと、SCの4F地上通常技46件それぞれのhitstunから
  `25 - 相手hitstun - 1`を計算。結果は`+6～+12F`、`Ryu 2LP`は`+9F`、`Sagat 2LP`は`+7F`。
  `2MP`は時間上44/46技で接続するが、全技共通または距離込みの確定追撃とはしない。
- **検証**: unittest **79/79**、観測JSON dry-run **1/1**、全ソース統合監査
  **92,940 assertions / 0失敗**、Discord Bot実経路 **9,728/9,728 / 0失敗**、
  Supabase実データE2Eで汎用4F技の分布と、`Ryu 2LP`指定時の`+9/-9`を確認。
- **本番反映**: SAM lint・arm64コンテナbuild後、CloudFormation `sf6-mcp-server` を
  `UPDATE_COMPLETE`まで更新。ローカルフォールバック無効の本番E2Eで汎用`+6～+12F / 44/46技`と
  `Ryu 2LP +9/-9 / 2MP猶予2F`を確認。Supabaseの `sequence_analysis_migration.sql` は未適用。

### 2026-07-13 ★ 状況付きフレーム契約・技同定・確反確度分離を実装
- **状況を型化**: 距離、接触持続F、段数、相手状態、カウンター、Burnout、DR、
  画面端、block/hit、視点を `scenario` として技名から分離。主語不明は確認対象にする。
- **技同定を型化**: `resolved / ambiguous / not_found` と候補・解決手段・confidenceを返し、
  弱中強や派生が一意でない技を計算へ流さない。正式技名の `（遠距離版）` は状況語ではなく
  技IDの一部として保持する。
- **条件評価を型化**: exact / derived / interval / unresolvedを分離。通常技の明示された
  持続接触だけ安全に派生し、先端だけ・Burnout/DR・空中結果などはルール未登録なら保留する。
  条件付き参照値は両視点へ正しく反転表示する一方、条件未選択なら確反計算へ使用しない。
- **確反を時間と空間へ分離**: 発生が間に合う地上ニュートラル技だけを時間候補として返す。
  ガード後距離・押し戻し・到達が未検証なら `confirmed_punishable=null` とし、確定反撃とは
  断定しない。MCP / Discord / RAG は共通 `punish_service.py` を使用する。
- **保持モデル**: 正規技ID、source link/alias、条件付きframe observation、system rule、
  interaction、frame別geometry、実測punish、cancel/chain/juggle遷移、実測combo linkを追加する
  `contextual_frame_model_migration.sql` を作成。Supabaseへ適用し、追加10テーブルの存在・
  公開読み取り・全テーブル0行を確認。**バックフィルは未実施**で、既存ソース表は変更していない。
- **全件検証**: unittest **58/58**、CAPCOM 2,357 / SC 2,118 / UFD 1,559行を使う
  統合監査 **92,940 assertions / 0失敗**、Bot **9,728/9,728 / 0失敗**。
- **本番反映**: SAM arm64コンテナbuild + lint、CloudFormation `sf6-mcp-server` を
  `UPDATE_COMPLETE` まで更新。本番MCPでKen 5HK最終持続=-4F/+4F、春麗214P~LKの
  条件付き確反=`timing_unresolved`を確認。次は正規技IDバックフィル・レビュー済み
  ルール/geometry投入後、距離込み確反とヒット後接続を実装する。

### 2026-07-10 ★ 型付き統合フレームプロファイル基盤完成・全件検証・AWS本番反映
- **誤評価を訂正**: 旧実装は単純値の取得はできたが、CAPCOM硬直欄の
  `着地後3` / `24+着地後16` / `全体52`、複数持続、条件別ガード差を先頭整数へ
  潰すため、要求性能を満たしていなかった。
- **実装**: `frame_data.py` を追加し、CAPCOM主値・UFD/SC補完のフィールド別採用、
  全ソース観測、型、条件、差異、公式注記を1プロファイルへ統合 (ADR-020)。
- **視点保証**: on_blockは攻撃側値として保持し、防御側は単一値・範囲・条件別値を
  Pythonで符号反転。発生/持続/硬直/両ガード視点はLLMなしで回答する。
- **技解決**: 生文字列完全一致を最優先。同一入力の条件違いは3項目以上の一意な
  フレームシグネチャだけで補助解決し、未マッピングUFD行へ別技を混ぜない。
  技区分境界、`nj.HK`/`8HK`同義表記、既知ファミリー内の弱中強・OD・ホールド補完を追加。
- **非数値型**: ターゲットコンボの段階別値、ガード不成立、状況依存を欠損や単一値に
  潰さず保持。キャミィ通常投げとターゲットコンボの誤結合を解消。
- **全経路統一**: MCP / Discord local・AWS / CLI RAG を同じlookupへ移行。
  旧routerの「日本語正式名→SC input先行変換」を削除し、正式名の条件を保持。
- **UFD同期**: 1,559行/30キャラ、sc_input 1,179件 (1,090→+89)、GIF 773件保存。
  元URLあり775件のうち2件 (Ryu 5MP / Lily 5HP) はUFD側404で保存不能。
- **検証**: unittest **41/41**、統合監査 **92,940 assertions / 0失敗**、
  Discord bot実経路 **8,950/8,950** (各1,790: 発生/持続/硬直/攻撃側/防御側)。
- **本番**: SAM arm64 build + validate、CloudFormation `sf6-mcp-server` を
  `UPDATE_COMPLETE` まで更新。Ken 5HK、C.Viper 8HK、Terry段階技、Edガード不成立、
  豪鬼の状況依存をローカルフォールバックなしで確認。
- **重要: 数値網羅は未完了**: 通常技578攻撃行は4項目100%。特殊技277攻撃行は
  発生/持続/硬直100%、ガード差は265数値+7対象外+5未解決。必殺技830攻撃行は
  発生829、持続710、硬直796、ガード差742数値+63対象外+4状況依存+21未解決。
  SA187攻撃行は発生180、持続171、硬直178、ガード差170数値+9対象外+8未解決。
  監査0失敗は回答整合性であり、原典数値の100%充足を意味しない。
- **次**: 未解決値を原典未収録とマッピング漏れに分離して解消。続いて範囲硬直差の
  保証/可能確反、SCリーチと当たり判定を使う到達判定、ヒット後接続を実装。

### 2026-07-10 ★ Ultimate Frame Data 統合の実装・DB適用・全キャラ同期
- **追加**: `ufd_moves` 分離テーブルとprivate Storage bucket
  `sf6-ufd-hitboxes` のマイグレーションを追加。CAPCOM/SCの生データを上書きせず、
  UFD実測値・メモ・GIF元URL/Storageパス/SHA-256を出所付きで保存する方針 (ADR-019)。
- **追加**: `importers/ultimate_frame_data.py`。UFDの静的キャラページをカテゴリ/技単位で
  解析し、通常技/コマンドをSC inputへ可能な範囲で正規化、GIFをStorageへ保存する。
  既存URLのGIFは再アップロードしない。
- **Bot/MCP反映**: `lookup_move` のCAPCOM・SC・ローカルフォールバック全経路で
  `ufd` 補足を返し、Bot/RAG回答に「Ultimate Frame Data 実測補足」を追加。
- **検証**: ケンの実ページHTMLから60技抽出（5HK=発生12/持続2/硬直25/全体38/
  ガード-5、GIF対応）を確認。unittest 15件・py_compile OK。
- **本番反映**: ARM64 Dockerビルド後、AWSスタック `sf6-mcp-server` を
  `UPDATE_COMPLETE` まで再デプロイ済み。UFDデータ取り込み後は同MCPが即座に補足を返す。
- **DB反映済み**: migration適用、全30キャラ同期、private Storage保存まで完了。

### 2026-07-10 ★ Discord Bot 全件網羅評価 完走 5,562/5,562 ✅
- **全件実行**: `tests/bot_comprehensive_eval.py --exhaustive` を bot executor で実行。
  通常技/特殊技/必殺技/SA の発生、ガードさせた側、ガードした側、確定反撃候補提案を
  合計 5,562 ケースで機械採点し、最終結果 **5,562✅ / 0❌ / 0⚠ (100%)**。
- **安定化 (intent_parser)**: 定型の「キャラの技の発生」「ガードさせた/した」
  「○○でガード後の確定反撃」を LLM なしで intent 化。`5HP~HP`, `j.HP~j.HP`,
  `KK~MK`, `2~8`, `~HK (End)`, `6[6]`, `-` など特殊な SC input も保持。
- **安定化 (mcp_router)**: API Gateway 429 回避用に `SF6_MCP_LOCAL_ONLY=1` の
  ローカルMCP相当モードを追加 (`lookup_move` / `check_punish`)。AWS MCP 失敗時の
  ローカルフォールバックも追加。
- **評価ハーネス改善**: `--concurrency`, `--quiet-success`, `--progress-every`,
  `--retries`, `--retry-base-sleep` を追加。失敗詳細はJSONLへ全保存。
- **検証**: py_compile OK / unittest 12件 OK /
  ブランカ+キャミィ特殊技 170/170 ✅ / edge input 対象キャラ 578/578 ✅ /
  全件 5,562/5,562 ✅。結果: `streetfighter6-engine/tests/bot_comprehensive_results.json(l)`。

### 2026-07-09 (8) ★ Discord Bot 全技網羅評価ハーネス追加
- **追加 (tests/bot_comprehensive_eval.py)**: DB から全キャラ×通常技/特殊技/必殺技/SA の
  ケースを生成し、bot と同じ `intent_parser → mcp_router → generate_answer` 経路を機械採点。
  発生照会 / ガードさせた側 / ガードした側 / 確定反撃候補提案を評価。
- **bot改善 (discord_bot/mcp_router)**: lookup_move の MCP JSON を視点付きテキストに整形し、
  check_punish の `punisher_options` を回答コンテキストへ含めるよう変更。
  raw query の「リュウでガード」等から `punisher` を決定論補完。
- **回答補完 (rag_builder)**: 確定反撃候補がコンテキストにあるのに LLM が落とした場合、
  候補上位を決定論で追記。
- **検証**: py_compile OK / unittest 2件 OK / bot executor 小ケース 4/4 OK /
  既存 `tests/regression_eval.py` 13/13 OK。全件 dry-run は 5,562ケース生成
  (move_data 1,718 / guard各1,524 / punish 796)。

### 2026-07-09 (7) ★ Discord Bot の自動検証デバッグ表示を非表示化
- **報告**: 「ケンの大Kの発生は?」で正答後に
  `⚠ 自動検証で数値の不一致を検出しました` と参照JSONが Discord へ表示される。
- **修正 (rag_builder)**: 検証NGの参照データ抜粋は `logger.warning` のみに残し、
  `generate_answer` のユーザー向け戻り値へ混ぜないよう変更。
- **過剰検証の緩和**: 構造化出力の転記値が `12F` でも回答本文の `12です` を
  正当な値使用として扱う `_answer_mentions_transcribed_values` を追加。
- **検証**: 追加 unittest 2件 OK。権限付き回帰評価 `tests/regression_eval.py` 13/13 OK。

### 2026-07-09 (6) ★ 質問フィールド判定 + 日本語略称の拡張 (「前大K」事故対応)
- **報告**: 「ケンの前大Kの発生は?」に ①move_name='Forward K' に化け ②発生でなく
  ガード有利を回答 (しかも +3F/-4F と数値も幻覚)
- **略称拡張 (intent_parser)**: _JP_ABBREV_TO_NUMPAD に方向+強度を自動生成で追加
  (前大K→6HK / 後ろ強P→4HP / 下大K→2HK / 立ち大K→5HK / 素の大K→5HK 等、
  大=強・小=弱の別表記対応)。挿入順=照合優先順 (方向付き→位置付き→素)
- **質問フィールド判定 (rag_builder)**: _FIELD_SPECS (発生/持続/硬直/ダメージ/無敵/リーチ)
  を質問文から決定論判定し、①「## 質問フィールド判定」指示を注入
  ②正解候補値をコンテキストから抽出して質問直前に再掲 (一意時のみ)
  ③回答に値が含まれるかを検証 (含まれなければリトライ)。
  値抽出はテキスト形式 (build_context) と JSON形式 (MCP move dict) の両対応
- **発見**: bot経路の lookup コンテキストは MCP move dict の生JSON
  (mcp_router.result_to_context) — 検証正規表現は両形式を意識すること
- **回帰評価 13/13** (新ケース: 立ち大K発生=12F / 前大K→「データなし」と正直に回答)

### 2026-07-09 (5) ★ トークン使用量の計測とコスト換算
- **token_usage.py 新規**: UsageTracker (ラベル別累積) + usage_label (contextvars、
  async安全) + usage_diff / format_usage / estimate_cost
- **ollama_provider**: generate / generate_structured / embed の3経路すべてで
  Ollama 実測値 (prompt_eval_count / eval_count) を記録 (structured と embed は
  従来破棄していた)
- **ラベル**: intent / answer / answer_retry / answer_fallback / embed
- **bot.py**: handle_question の最後に1質問分の消費とコスト換算をログ出力。
  regression_eval.py も末尾に累積を表示
- **コスト換算 env**: SF6_COST_INPUT_PER_MTOK / SF6_COST_OUTPUT_PER_MTOK /
  SF6_COST_CURRENCY (未設定ならトークン数のみ)。トークナイザ差で1〜2割誤差あり
- **実測の知見**: intent 解析だけで入力 ~2,400 tok/質問 (SYSTEM_PROMPT が大きい)。
  検証リトライ発生時は answer コストが倍になる → 今後の削減ポイント

### 2026-07-09 (4) ★ 構造化回答出力 + 決定論検証 + 回帰評価基盤 (コンテキストエンジニアリング適用)
- **背景**: Agentic RAG 記事 + 蒲生氏「コンテキスト/ハーネスエンジニアリング」PDF の
  設計見直し。gemma4 を推論エージェントにせず、制御構造は Python 決定論で実装する方針
- **構造化回答出力 (プロパティ名CoT, rag_builder)**:
  - ANSWER_JSON_SCHEMA: プロパティ名に指示を埋め込み (「参照データから符号ごと
    一字一句転記したフレーム数値のリスト」→「回答文_転記した数値だけを使い…」の生成順CoT)
  - 決定論検証: _phantom_frame_tokens (幻覚数値) / _perspective_violations (視点と
    数値の結び付け照合) / _foreign_chara_mentions (無関係キャラの幻覚 — EdのQに
    Manon解説を返す事故が実際に発生) / 転記数値未使用チェック
  - 検証NG→エラーをフィードバックして1回再生成 (再帰修正)、なお NG なら
    ⚠付きで参照データ抜粋を添付。JSON失敗時は自由文にフォールバック
  - _ensure_move_reference: 主語なし回答に技ヘッダを決定論で前置
  - _variant_mention_note: バリアント存在への言及 (ルール16) を LLM 任せにせず後付け
- **Lost in the Middle 対策**: _recap_lines — 視点判定・バリアント判定に該当する
  重要行を質問直前に再掲 (生成直前に質の良い情報を置く)
- **temperature=0 (ollama_provider)**: Ollama 既定0.8で intent 分類・転記が実行ごとに
  ブレていた。env OLLAMA_TEMPERATURE で変更可
- **intent_parser**: _COMMAND_NUMPAD を拡張 (236KK/22P/6KK/63214KK 等のOD・
  複数ボタン表記も明示入力として抽出)
- **回帰評価基盤**: tests/regression_eval.py — DB確定の正解値付き11問
  (視点×2/ため×2/レベル/Windclad/無敵/通常技/反撃/概念/正規化キー) を機械採点。
  **2周連続 11/11 合格**。今後の変更は必ずこれを回してから終了すること

### 2026-07-09 (3) ★ バリアントグルーピングの一般化 (全キャラの特殊表記対応)
- **DB全体調査 (2118行)**: Edのため版は氷山の一角 — ホールド[]66行 / 部分ため{}系 /
  Lily Windclad (W.prefix) 13行 / Rashid (Air Current)注釈入力 / Jamie 飲酒DL 53行+ /
  Luke pf. / 入力欄注釈 22P (hold) / 代替表記 5/6KK 等98行 / チャージコマンド[4]6P 67行(束ね禁止)
- **実装 (旧 _fetch_charge_variants を一般化)**:
  - _canon_input_keys(): 注釈除去 + []{}ボタンホールド剥がし + W./pf.prefix除去 +
    '5/6KK'→(5KK,6KK)展開。チャージコマンドの数字[]は保持。1文字キーは除外
  - _variant_label(): 技名括弧修飾+入力表記から条件ラベルを決定論導出
    (ため版/部分ため版/Lv.N版/ウィンドクラッド版/飲酒レベル/エアカレント版/…)
  - _fetch_variant_group(): 正規化キー交差でグルーピング。強度付き→強度なし
    ブリッジ (Akuma 236LP→236{P}/[P]) はバリアントラベル行限定で誤結合防止。
    drive/taunt は除外 (Drive ParryとDRCが'MPMK'で対になる誤爆防止)
  - input完全一致ミス時の canon 再検索 ('6KK'→'5/6KK' Kill Rush 等)
  - _variant_directive(): 質問の条件語 (ためた/ウィンドクラッド/DL2/…) を正規表現
    判定し「## バリアント判定」をプロンプト注入 (視点判定と同パターン)
- **視点判定の強化**: 指示に転記の作業例を埋め込み (gemma4 が指示だけでは+2F→-2Fに
  反転する事例が E2E で発覚したため)
- **intent_parser**: max_combo も combo_info と同様にコンボ語なしなら lookup_move 降格
  (「最大までためた」→max_combo 誤分類対策)。_COMBO_INDICATORS に「火力」追加
- **検証**: 全DB掃引でユニークペア151組・3件以上の過大グループ0・誤結合サンプル0。
  E2E (実LLM): 最大ため波動拳 (Lv.3 56F/+20F を通常版と比較して正答) /
  Lily Windclad 623MP / サガット立ち中Pガードさせた+2F×2回 すべて ✅

### 2026-07-09 (2) ★ ため (ホールド) 版バリアントの自動添付 + input指定必殺技のフォールバック
- **要望**: Ed の 236LK/236HK 等の「ためられる技」の情報も渡せる仕組みに (dogfooding)
- **データ確認**: SC はホールド版を `236[LK]` / `5[HP]` のような []付き入力の**別行**で収録
  (Ed: Psycho Flicker (Hold) ×3, Psycho Knuckle Lv.2)。unified_moves には必殺技も
  []付き行も存在しない (通常技のみ)
- **実装 (rag_builder)**:
  - _fetch_charge_variants(): []を剥がした入力が一致する同キャラ行をため/ためなし
    バリアントとして取得 (双方向)。ガイル [4]6P のようなタメ"コマンド"は誤爆しない
  - _fmt_charge_variant_section(): 「【ため (ホールド) 版あり】」セクション生成
  - build_context の3経路 (名前検索 / unified input / inputフォールバック) にフック
    (compare_moves 経路は未フック — 必要になったら追加)
  - **副次修正**: input指定の必殺技 (236LK等) が unified_moves ミスで
    「M2以降で対応予定」エラーになっていた → sc_move_normalized への
    フォールバック追加 (punish_check の反撃判定も対応)
  - ANSWER_SYSTEM ルール16: ため言及あり→ため版 / なし→通常版+ため版の存在に言及
- **検証**: Ed 6ケース (236LK/236[LK]/名前/5HP/2MK回帰/punish 236HK) + Guile/Sagat 回帰 ✅
- **追加修正 (dogfooding で発覚)**: gemma4 が「236LK」を input='2LK' に壊し
  intent も combo_info に誤分類 → intent_parser に決定論補正を2つ追加:
  - (4c) クエリに明示されたコマンド表記 (_COMMAND_NUMPAD、[]ホールド表記も対応) は
    LLM の input 出力より**常に優先**して上書き
  - (5) コンボ関連語 (_COMBO_INDICATORS) がクエリに無い combo_info は lookup_move に降格
- **E2E (実LLM)**: 「エドの236LKをためた時の性能は?」→ override発動 → Hold版 (発生26F/
  ガード+4F/KD) を攻撃側視点で正答 ✅
- **気づき (未修正)**: 「弱タイガーショット」の名前検索が 236MP を返す
  (_pick_variant の強度解決の既存挙動、今回の変更とは無関係)

### 2026-07-09 ★ 視点判定の決定論化 — 「ガードさせた時」(使役形) の誤判定修正
- **問題**: 「ガードさせた時何F有利?」(使役=攻撃側視点) に防御側視点で回答していた。
  gemma4 が「ガードさせた」と「ガードした」をプロンプトルールだけでは区別できない
  (2026-07-07 の既知の限界と同根)
- **修正 (LLM 判定 → Python 正規表現判定に移行)**:
  - rag_builder: _perspective_directive() 追加 — _ATTACKER_VIEW_RE (ガードさせ/ガードされ/
    硬直差/当てた時 等) / _DEFENDER_VIEW_RE (ガードした/食らった 等) で質問文を判定し、
    「## 視点判定 (システムによる自動判定)」セクションをプロンプト冒頭に注入
  - ANSWER_SYSTEM ルール15: 使役形の注意書き + 「視点判定セクションには無条件で従う」を追加
- **検証**: 判定ロジック9パターン (攻撃側4/防御側4/判定なし1) 全通過。
  LLM 込み E2E は Ollama 停止中のため未実施 — 次回ボット起動時に
  「サガットの立ち中Pをガードさせた時は何フレーム有利?」で確認すること

### 2026-07-07 (2) ★ フレーム有利の視点取り違え修正 (dogfooding フィードバック)
- **問題**: 「立ち中Pをガードした時何F有利?」に攻撃側視点 (+2F) で回答していた
  (正: ガードした側 -2F)。LLM は符号反転を自力で計算できない
- **修正 (決定論層で両視点を明示)**:
  - rag_builder: _block_adv_line/_hit_adv_line 追加 —
    「ガード時: +2F (技を出した側が+2F / ガードした側は-2F)」形式で全フォーマッタ統一
    (_fmt_sc_move / unified / _fmt_combo_context)
  - ANSWER_SYSTEM ルール15追加: 視点判別 (「Xをガードした」=受け手 /
    「Xがガードされた」=出し手) + 括弧書きの数値をそのまま引用させる
  - MCP: move dict に frame_perspective_note フィールド追加 → 本番デプロイ済
- **副次修正**: LLM が move_name を出力しない場合、raw_query を
  _fetch_move_by_name に渡すフォールバック (special_move_map の containment 検索が
  クエリ文中の技名を拾う) → 「弱ロン・ポワンを食らった時」が正答するように
- **E2E**: 元の報告ケース「ガードした側は -2F (2フレーム不利)」✅ /
  ヒット時受け手視点 (-3F) ✅ / 両視点の括弧書きが常に回答に含まれる
- **既知の限界**: 「Xはガードされた時何F?」の主語曖昧な聞き方は gemma4 が
  受け手視点で答えることがある (両視点の括弧書きは必ず併記されるため実害小)

### 2026-07-07 ★ ADR-018 — 必殺技の日本語名解決を DB 結合 + 対話学習に移行 (コード完成)
- **special_move_map シード生成**: match_specials.py (フレームシグネチャ自動照合 2パス +
  手動オーバーライド約120件) → 883件マッチ / 曖昧0 / 未対応217 (条件付き強化版等は許容)
  - 発見: recovery は CAPCOM/SC でカウント方法が異なる (着地硬直) → 照合から除外
  - 発見: SC 側が旧パッチ数値のことがある → 厳格一致だけでは不足、緩和パス必須
- **SQL**: sql/special_move_map_migration.sql (special_move_map + move_aliases + RLS)
- **rag_builder**: _fetch_move_by_name にステップ0 (special_move_map 日本語名) と
  0.5 (move_aliases 学習略称) を追加。既存 EN 検索・_JP_MOVE_TO_EN はフォールバックに降格
- **MCP**: register_move_alias ツール追加 (コマンド実在検証 → 強度prefix剥がし →
  ファミリー単位 UPSERT)。app.py が SSM /sf6/supabase-service-key を追加ロード
- **Discord bot**: 聞き返し学習ループ (技名未解決+キャラ特定済み → コマンド聞き返し →
  register_move_alias → 復唱 → 元質問に即答)。保留5分TTL、コマンド正規表現抽出
- **✅ 本番反映完了 (2026-07-07)**: migration 適用 → シード883件投入 → 再デプロイ
  - 追加修正: lookup_move / check_punish に _sc_name_fallback を追加
    (handlers.lookup → input一致 → 技名解決の3段。special_move_map/エイリアスが効くように)
  - ローカル解決テスト 10/10 (マノン/エド/JP/リリー/舞/マリーザ/テリー/サガット/キンバリー)
  - 本番E2E 4/4: lookup ODロン・ポワン→236KK / 学習エイリアス 強フリッカー→236HK /
    punish 中トルバラン(-8F) / setplay 強タイガーアッパーカット→623HP
  - register_move_alias: 誤コマンド拒否・強度prefix除去・復唱 すべて動作確認済
- deploy コマンド: `sam build --use-container -t template-mcp.yaml --config-file samconfig-mcp.toml && sam deploy --config-file samconfig-mcp.toml --no-confirm-changeset`

### 2026-07-06 ★ MCP改善候補を全実装 + 本番デプロイ (check_punish 改善含む)
- **技名→SC input 解決**: compute_setplay / analyze_combo が input 不一致時に
  rag_builder._fetch_move_by_name (日英ILIKE + 強度/OD判別) で自動逆引き
  → 「強タイガーアッパーカット」→623HP、「タイガーニー」→236HK を実証
- **戻り値に input 付与**: list_moves を unified_moves に切替 (input列 + keyword が技名/input両対応)、
  lookup_move (CAPCOM解決時も sc_input_key 付与)、check_punish の punisher_options に input 付与
- **既知課題解消**: punisher_options のパリィ (発生1F) 混入を move_name フィルタで除外
- ローカルスモークテスト 10/10 ✅ → sam build --use-container && sam deploy → 本番疎通4件 ✅
  (2026-06-09 の check_punish queried_move 改善もこのデプロイで本番反映)
- 残: Discord Developer Portal でトークン設定 (ユーザー操作のみ)

### 2026-06-09 ★ dogfooding反映 — check_punish の戻り値を曖昧性なしに改善
- check_punish: 呼び出し識別子(2HK)と解決名(Tiger Kick)が異なる場合「2HK（Tiger Kick）」併記
  + 戻り値に queried_move 追加。公開MCPとして消費側LLMが同一技だと分かるよう根本対応
- ローカル検証OK。**反映には MCP 再デプロイが必要** (sam build && sam deploy, スタック更新)
- 残改善候補: list_moves に SC input 付与 (技名→SC input 解決, setplay/combo の必殺技対応)

### 2026-06-08 (8) ★ M5 — Discord Bot (MCP経由 dogfooding) 動作確認
- `discord_bot/` 新規: bot.py(discord.py) / mcp_router.py(intent→MCPツール変換+MCPクライアント) / README
- 設計: gemma4(ローカル)でintent解析 → map_intentでMCPツール選択 → AWS MCP経由実行 → gemma4で回答
  → bot は Ollama + MCP のみに依存 (Supabase/Bedrock非依存)。開発者公開MCPのdogfooding
- キャラ名: intent_parserはSC英語名(Sagat/M.Bison)出力 → char_slug_mapスナップショットで
  capcom slug変換 (M.Bison→vega_mbison 等7キャラ)。SC_TO_SLUG をbotに静的保持
- **E2E検証 (gemma4 + 本番AWS MCP)**: 反撃判定/最大コンボ/バーンアウト説明/発生F すべて正答
- **dogfooding発見**: check_punish等がSC名(Tiger Kick)で返すため質問の2HKと結びつかず誤答
  → bot側でコンテキストに「2HK（Tiger Kick）」等値表記を補って解決。MCP戻り値にinput付与が改善候補
- 残: Discord Developer Portalでbot作成+トークン設定 (DISCORD_TOKEN/SF6_MCP_URL/SF6_MCP_TOKEN)

### 2026-06-08 (7) ★ M4 完了 — 本番デプロイ & 全ツール疎通確認
- SAM デプロイ成功 (sf6-mcp-server スタック)。Lambda + HTTP API + 実行ロール作成
- デプロイ中の IAM ハマり 2件を解決:
  1) deployer に apigateway 権限なし → apigateway-deploy-policy.json (Resource を /* に拡大)
  2) `apigateway:TagResource` が IAM 可視エディタで「存在しない」エラー = AWS既知の未文書化アクション
     → CLI/JSONエディタで保存すれば実際は有効 (CloudFormationが要求する実在アクション)
- デプロイ後 404 → 原因: HTTP API 非$defaultステージは path に /prod を含む
  → Mangum api_gateway_base_path="/prod" (MCP_BASE_PATH 環境変数) で剥がして解決
- **本番エンドポイントで全7ツール疎通OK**: lookup/punish/setplay/combo(3500dmg)/docs(Titan)/patch + 認証(401/200)

### 2026-06-08 (6) ★ M4 ステップ4 — AWSリモート化 (コード完成・ローカル検証済)
- `app.py`: Lambda ハンドラ (Mangum + Bearer認証ミドルウェア + SSM bootstrap)
- `server.py`: stateless_http+json_response、DNS rebinding保護OFF、get_patch_status追加 (7ツール)
- `template-mcp.yaml`: Lambda + HTTP API + 実行ロール(ssm:GetParameter /sf6/*, bedrock:InvokeModel Titan)
  + スロットリング(burst10/rate5)、`src/requirements.txt`(Lambda依存)、samconfig-mcp.toml.example
- **核心の技術課題2件を解決**:
  1) StreamableHTTP セッションマネージャは run-once 制約 → Mangum auto/on は warm invocation で破綻
     → cold start で lifespan を1度だけ起動し保持 (LifespanCycle.__enter__ のみ、shutdown呼ばず)
  2) FastMCP が host=127.0.0.1 で DNS rebinding保護を自動ON → 421 → transport_security で無効化(認証はBearer)
- **ローカル検証 (Mangum合成イベント)**: initialize×2(warm含む)/tools/list(7種)/tools/call(lookup_move)
  /Bearer認証(401/401/200) すべて成功。streamablehttp_client 実接続も全7ツール動作確認済
- デプロイ手順: `deploy/DEPLOY.md` (SSM登録/IAM追加/sam build --use-container & deploy)
- 残ブロッカー(外部操作): ①SSM 3パラメータ登録 ②deployerに apigateway 権限追加(deploy/iam/apigateway-deploy-policy.json)
- 次回: 上記解消後デプロイ → エンドポイントで疎通 → クライアント登録

### 2026-06-08 (5) ★ M4 ステップ3 完了 — search_system_docs MCPツール追加
- server.py に `search_system_docs` 追加 (公開ツール 5→6種)
- 元 rag_builder._search_docs のハイブリッド構成を移植: キーワード検索(JP→EN ILIKE,
  Ollama不要) + ベクトル検索(Titan埋め込み + search_docs_titan RPC)。埋め込みのみ Bedrock 化
- ベクトル検索が落ちてもキーワード結果は返すフォールバック設計、vector_search_error で可視化
- 検証: 'ドライブインパクトのアーマー'(JP)→Drive Impact / 'perfect parry'(EN)→Perfect Parry
  / 'バーンアウト'(JP)→Burnout すべて的確にヒット、vector_error なし
- README にツール表更新・Bedrock権限要件を追記
- 次回(ステップ4): Streamable HTTP stateless 化 + SAM(Lambda+API GW) デプロイ
  + get_patch_status ツール + Lambda実行ロールにbedrock:InvokeModel

### 2026-06-08 (4) ★ M4 ステップ2 完了 — Titan v2 再埋め込み実行
- IAM 真因確定: `SF6FrameScraperDeployerPolicy` の `BedrockDenyAllOtherModels` が
  NotResource(Gemma系)以外を明示Deny → Titan ARN を NotResource に1行追加で解消
  (deploy/iam/NOTES.md に記録)
- Bedrock 疎通OK (1024次元) → reembed_titan.py 実行 → **72/72 成功 (err=0)**
- 検証: embedding_titan 72/72 投入済 / search_docs_titan RPC で
  'drive impact armor break' → Drive Impact(0.625)/Armor(0.588) と的確にヒット
- 次回(ステップ3): search_system_docs MCPツール追加 (search_docs_titan + Titanクエリ埋め込み)

### 2026-06-08 (3) ★ M4 ステップ2 — Bedrock Titan v2 再埋め込み基盤 (コード完成)
- `sql/doc_chunks_titan_migration.sql`: embedding_titan vector(1024) + search_docs_titan() 関数
  (既存 embedding 768次元/search_docs は温存して並存、ロールバック容易)
- `src/sf6_engine/bedrock_embed.py`: Titan V2 埋め込みヘルパー (boto3, Ollama非依存)。step3でも共用
- `scripts/reembed_titan.py`: 72チャンクを元importerと同じ入力テキストで再埋め込み (--dry-run対応)
- requirements に boto3 追加、.venv312 に導入
- **検証済み**: AWS認証OK / Titan v2 ap-northeast-1 で ACTIVE / dry-run で72件読込・入力構築OK
- **ブロッカー2件 (私が実行不可な外部操作)**:
  a) Supabase Studio で migration SQL 適用 (Supabaseへの直接DDLは手動運用)
  b) sf6-deployer に bedrock:InvokeModel 権限なし (明示deny検出) → deploy/iam/bedrock-invoke-titan.json
- 解消後: `PYTHONPATH=src python scripts/reembed_titan.py` → 全72件 embedding_titan 投入
- 次回(ステップ3): search_system_docs ツール追加 (search_docs_titan RPC + Titanクエリ埋め込み)

### 2026-06-08 (2) ★ M4 ステップ1 — mcp_server スキャフォールド完了
- `src/sf6_engine/mcp_server/` を FastMCP で新規作成。決定論ツール5種を公開:
  lookup_move / check_punish / compute_setplay / analyze_combo / list_moves
- 既存の handlers.lookup / combo_engine / setplay_engine を薄くラップ (LLM段は非搭載)
- **重要発見**: numpad表記 (`2HK`) は move_normalized (CAPCOM日本語名) で解決不可。
  → MCPラッパー層に sc_move_normalized の input フォールバックを追加 (既存handlerは不変)
  → `2HK` で発生11F/ガード-12F/KD を決定論取得できることを確認
- requirements.txt に `mcp>=1.2.0` 追加、.venv312 に導入
- 実DBスモークテスト全5ツール通過 (sagat 2HK/2MP/623HP、ryu反撃択列挙、タイガージャブ)
- README.md に起動方法・Claude Desktop登録例を記載
- 次回: ステップ2 (doc_chunks に embedding_titan カラム + 72チャンク Titan v2 再埋め込み)
- 既知調整: check_punish の punisher_options にパリィ(発生1F)が混ざる → 後続でフィルタ

### 2026-06-08 ★ M4 方針確定 — AWS リモート MCP 切り出し設計
- Bot 構築の前段として、実装済みの Layer 1 (CAPCOM更新→RDB) と M1〜M3 (技相性等回答) を
  MCP サーバとして切り出す方針を決定 (ADR-017)
- **核心判断**: LLM 段 (intent_parser/generate_answer) はサーバから外し、決定論ロジック層
  (lookup/combo/setplay) のみツール公開。推論はホスト LLM が担う → Ollama 依存・誤分類が消える
- **3つの確定事項**: 稼働先=AWSリモート(API GW+Lambda, Streamable HTTP stateless) /
  RDB=Supabase維持(読み取りのみ) / 埋め込み=Bedrock Titan v2(768→1024次元のため再埋め込み必要)
- ADR-017 記録、M4 実装ステップ6項目を PROGRESS に記載
- 次回: ステップ1 (`mcp_server/` FastMCP スキャフォールド) から着手

### 2026-05-29 (3) ★ 全キャラ網羅テスト構築
- **char_coverage_test.py 新規作成**: DBから実フレームデータを動的取得して全30キャラのテストケースを自動生成
  - `--fast` (発生F 29問) / フル (発生F + ガード 58問) / `--chars` フィルタ対応
  - イングリッドのみ標準ナンバーパッド技がないためスキップ (29体対象)
- **バグ修正 2件**:
  - factory.py: `localhost` → `127.0.0.1` (macOS がIPv6 `::1` を試みて Ollama IPv4に失敗するため)
  - 代表技フォールバック: `_std_normal` 正規表現で `[1-9][LMH][PK]` 以外の技を除外
- **実行コマンド**: `PYTHONPATH=src python tests/char_coverage_test.py --fast` (~10分)
- **全29キャラ結果: 29✅ 0❌ 合格率 100%** (平均応答 12.5s/キャラ)

### 2026-05-29 (2) ★ M4 統合テスト拡充 — 新20問追加 (100% 合格)
- **capability_test.py に 20問追加**: S/P/R/Q 4領域、合計 56問に拡張
  - S-01〜S-06: セットプレイ追加 (Ken/Guile/Cammy/Luke/Ryu の KD後 択)
  - P-01〜P-05: 確定反撃追加 (Sagat/Luke/Ryu/Guile/Zangief)
  - R-01〜R-05: 派生技追加 (ケン迅雷脚 中/強派生 フレームデータ)
  - Q-01〜Q-04: 複合クエリ (DR cancel / パニカン後セットプレイ等)
- **バグ修正 3件**:
  - A-04 回帰修正: `_fmt_combo_context` が `punish_adv` を表示していなかった → 追加 (HKD +45 ✅)
  - Ollama タイムアウト: `factory.py` で `OLLAMA_TIMEOUT` 環境変数対応 (デフォルト 300s)
  - R-01/R-02: 質問文中の `（6HK）（6MK）` が `_NUMPAD_EXPLICIT` に誤マッチ → 表記を削除
- **新20問 結果: 20✅ 0❌ (合格率 100%)**

### 2026-05-29 ★ 対応範囲テスト + バグ修正 (80% → 88% / 実質 91.7%)
- **対応範囲テスト新規作成**: `tests/capability_test.py` (36問 / 5領域自動評価)
- **修正4件**:
  - A-13: `_fetch_move_by_name` 単語分割でタイガーアッパー→GreedyTiger誤返却 → 残り単語による絞り込みで修正
  - B-02: "DRキャンセルすると何F?" が `lookup_move` 誤分類 → SYSTEM_PROMPT に例追加で `combo_info` へ
  - C-01: `_NUMPAD_EXPLICIT` の lookbehind が数字を除外せず `623HP` → `3HP` 誤抽出 → `(?<![A-Za-z0-9])` に修正 + `_COMMAND_NUMPAD` 追加
  - C-04: 「強フラッシュナックル」→ move_name 未解決 → `_JP_SPECIAL_NAMES` / `_JP_MOVE_TO_EN` にルーク技追加
- **副次改善**: intent_type バリデーション追加 (punish_adv等の無効値を lookup_move にフォールバック)
- **notes 切り詰め**: `_fmt_sc_move` で 400 字制限 → 平均応答 26.9s → 15.9s (41% 高速化)
- **.venv312 作成**: Python 3.9 venv シンボリックリンク切れのため Python 3.12 で新規 venv

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

- 外部反映: Supabase SQL Editorで `streetfighter6-engine/sql/sequence_analysis_migration.sql` を適用する。
- SQL適用後: `sf6_engine.importers.sequence_observations` を実行し、DB行の読み出しを確認する。

## 💡 メモ・気づき

- Layer 1 のスクレイパーは EventBridge で毎日 03:00 JST に自動稼働中
- SuperCombo の最新データ (2026-04-26版) は手元にダウンロード済み
- SNS 通知: 次回パッチ検知時に確認メールが届く → 「Confirm subscription」を承認すること
- AWS デプロイ用 IAM ユーザー: sf6-deployer (SNS・SSM 権限を追加済み)
- samconfig.toml はローカルのみ (.gitignore 済み)、雛形は samconfig.toml.example
- LLM 移行候補: Bedrock Gemma3 12B ≈ $2.70/月 (2,000クエリ), EC2 は常時起動で割高
- 前ステップF は doc_chunks の Forward/Back Dashing テーブルから全キャラ動的取得 (lru_cache)
- UFD GIFは773件だけで4.21GB。Botは元URL参照のため、geometry解析時のオンデマンド取得で十分。

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
