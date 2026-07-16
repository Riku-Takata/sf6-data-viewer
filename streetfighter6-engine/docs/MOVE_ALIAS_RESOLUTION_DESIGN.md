# 技名の略称・かな・ローマ字・英語解決設計

## 1. 目的

ユーザーが公式名やnumpad入力だけでなく、次のような表記で技を指定しても、キャラクター内の
既存技データから安全に技を解決できるようにする。

- 公式名の一部を含む略称: `サガットの中ネク` → `中 タイガーネクサス` (`214MK`)
- 読みを使った略称: `リュウの弱はしょう` → `弱 波掌撃` (`214LP`)
- 英語名: `medium Tiger Nexus` → `214MK`
- 日本語名のローマ字: `taigaa nekusasu`、`hashougeki`
- 技名と字面が無関係な通称: `サガットの下デヨ` → 自動推測せずコマンドを聞く

本設計は技名をコードへ追加していく方式を採らない。公式/CAPCOM、UFD、SuperCombo、レビュー済み
aliasから検索表記を構築し、解決不能時だけユーザーにコマンドを求める。

## 2. 現行実装の確認

### 既に利用できるもの

- `frame_data.lookup_frame_data()`はCAPCOM/UFD/SuperCombo名をキャラ単位で収集し、完全一致、
  部分一致、一意な近似一致、入力表記を扱う。
- `special_move_map`はCAPCOM公式日本語名とSuperCombo inputを結合している。
- `rag_builder._fetch_move_by_name()`は公式日本語名、旧`move_aliases`、英語名ILIKE、単語分割検索を持つ。
- `canonical_moves`と`canonical_move_aliases`のDDLは既にある。
- Discord Botには、技未解決時に同一利用者へコマンドを聞く300秒のpending状態がある。

### 現状の問題

1. 名前解決が`frame_data`、`rag_builder`、MCP server、Discord local fallbackに分散している。
2. カタカナとひらがな、漢字の読み、ローマ字を同じ検索表記として扱っていない。
3. 部分一致・近似一致・学習済みaliasの結果契約が統一されていない。
4. 旧`register_move_alias`は1件の回答から強度prefixを除去し、SC familyへglobal UPSERTする。
   `中ネク`や`下デヨ=2HP`のようなvariant固有aliasを誤って全強度へ広げる可能性がある。
5. 旧aliasは投稿直後に全ユーザーへ公開されるため、現在は既定で無効化されている。
6. 実DBでは2026-07-16時点で`canonical_moves=0`、`canonical_move_aliases=0`であり、
   canonical IDを前提にした永続alias検索はまだ有効化できない。

## 3. 基本方針

### 3.1 名前解決とフレーム計算を分離する

新しい`MoveResolver`を1つのread-only serviceとして定義する。`lookup_move`、確反、コンボ、
セットプレイ、2技連携の各技、Discord local fallbackはすべて同じresolverを使う。

```text
raw query
  -> MoveReference抽出
  -> 表記・強度・条件の正規化
  -> キャラ内候補生成
  -> variant制約
  -> score/一意性判定
  -> resolved / ambiguous / needs_confirmation / needs_command
  -> 解決済みinputを既存のフレーム計算へ渡す
```

resolverはフレーム値を選ばない。フレーム値の統合は引き続き`frame_data`が担当する。

### 3.2 LLMに技名を翻訳させない

Intent Parserは技名らしいspanを原文のまま切り出し、キャラ、強度、入力、条件を構造化する。
英語化や正式名の推測は行わない。

```json
{
  "raw_text": "中ネク",
  "character_slug": "sagat",
  "explicit_input": null,
  "strength": "medium",
  "locale_hint": "ja",
  "script_hint": "kana"
}
```

2技連携も文字列配列の各要素を同じ`MoveReference`として解決する。単体技と連携で別のalias規則を
持たない。

## 4. 検索表記の生成

### 4.1 名前ソース

キャラクターごとに次の行を同一の技inputへ集約する。

| 種別 | 既存データ | 例 |
|---|---|---|
| 公式日本語名 | `move_latest.move_name` / `special_move_map.capcom_move_name` | `中 タイガーネクサス` |
| 英語名 | `sc_moves.name` / `ufd_moves.move_name` | `Tiger Nexus`, `Hashogeki` |
| 入力 | `special_move_map.sc_input` / SC/UFD input | `214MK`, `2HP` |
| レビュー済み通称 | 将来の`canonical_move_aliases` | `下デヨ` |

候補は最初にinputとvariant条件でグループ化する。同じ技が複数ソースにあることを「複数候補」と
数えない。

### 4.2 正規化form

名前行から、検索専用のformを生成する。

1. Unicode NFKC、casefold、全半角・空白・句読点の正規化
2. 強度語を名前本体から分離
   - `弱/light/weak/LP/LK` → `light`
   - `中/medium/mid/MP/MK` → `medium`
   - `強/heavy/hard/HP/HK` → `heavy`
   - `OD/EX/overdrive` → `od`
3. カタカナをひらがなへ統一したform
4. 公式日本語名から生成した読み仮名form
5. レビュー済み読み仮名から生成したローマ字form
6. 英語名を英数字tokenへ分割したform

漢字の読みはリクエスト時に推測せず、index作成時に生成する。一般的な読み生成器は候補作成にだけ
使い、domain固有名で信頼できない行は`reviewed=false`とする。`波掌撃 -> はしょうげき`のような
レビュー済み読みを持てば、`はしょう`を安全に部分照合できる。

長音はformを無制限に増やさず、レビュー済み読みから制御された候補だけを作る。

- `たいがー` → `taigā`, `taigaa`, `taiga`
- `ねくさす` → `nekusasu`

英語名`Tiger Nexus`は翻字せず独立した一次検索formとして保持する。

### 4.3 部分略称

すべての部分文字列をDBへ保存しない。ユーザーspanを検索formに対して照合する。

- かな: 2文字以上。ただし2文字はキャラ指定+強度/variant指定+候補1件を必須とする。
- Latin: 3文字以上。token prefixまたは連続部分一致を使う。
- 漢字: 1文字だけでは自動解決しない。完全一致または他の強度/語との組合せを要求する。
- typo近似: 同一script、長さ差2以内、共通2-gramまたは漢字anchorあり、variant prefix一致を必須とする。

`pg_trgm`や編集距離は候補生成にだけ使い、最終確定条件にはしない。現在の「最高scoreが一件」という
条件だけでは、字面が無関係な長い通称を偶然の一位へ誤解決する可能性があるためである。

## 5. variantと強度の扱い

強度・OD・SA・構え・ホールド・空中状態は名前scoreより先に候補filterとして適用する。

- `中ネク`: `strength=medium`で候補を絞り、`ネク`が`タイガーネクサス`へ一意一致 → `214MK`
- `ネク`: familyは一意でも弱/中/強/ODが残る → 強度またはコマンドを聞く
- `弱はしょう`: `strength=light` + 読み`はしょうげき` → `214LP`
- `はしょう`: 通常版、電刃版、SA2等が残る場合は自動選択しない
- `下デヨ`: 名前・読み・英語名・レビュー済みaliasの候補0件 → コマンドを聞く

一つのvariantからfamily aliasを推定しない。`中ネク -> 214MK`が確認できても、`ネク`を全強度へ
自動登録してはならない。family aliasは別claimとしてレビューする。

## 6. 解決結果の契約

```json
{
  "status": "resolved",
  "query": {
    "raw_text": "中ネク",
    "normalized": "ねく",
    "strength": "medium"
  },
  "selected": {
    "character_slug": "sagat",
    "canonical_move_id": null,
    "input": "214MK",
    "display_name": "中 タイガーネクサス"
  },
  "match": {
    "method": "unique_reading_substring",
    "score": 0.94,
    "evidence": ["official_ja", "reviewed_reading"],
    "runner_up_margin": 0.31
  },
  "candidates": []
}
```

statusは次に限定する。

| status | 意味 | UIの動作 |
|---|---|---|
| `resolved` | 高confidenceかつvariantまで一意 | 既存ツールを実行 |
| `needs_confirmation` | 一候補だが自動確定閾値未満 | 「○○のことですか？」 |
| `ambiguous` | 複数input/variantが残る | 候補と強度/コマンドを提示 |
| `needs_command` | 字面から候補を作れない | キャラとaliasを復唱してコマンドを聞く |
| `invalid_input` | 返信されたコマンドがそのキャラに存在しない | 再入力を依頼 |
| `character_not_found` | キャラ未解決 | キャラ名を聞く |

`found=false`と自由文だけで制御せず、`reason_code`と`clarification.type`をMCPレスポンスへ持たせる。
Discordは`move_not_found`文字列を見て学習可否を推測しない。

## 7. 未知通称のコマンド確認フロー

### 7.1 初回

```text
User: サガットの下デヨってガードで何F？
Bot: サガットの「下デヨ」は技名データから一意に特定できませんでした。
     コマンドを教えてください（例: 2HP、214MK）。
```

pendingには次を保存する。

```text
pending_id / subject_key / conversation_id / channel_id
character_slug / alias_raw / alias_normalized
original_intent / original_question / expires_at / attempts
```

同一利用者・同一会話だけが返信でき、TTLと試行回数を制限する。質問文全体ではなく、抽出済み
`alias_raw`だけを保持する。

### 7.2 コマンド返信

```text
User: 2HP
Bot: サガットの2HP（しゃがみ強P）ですね。「下デヨ」をこの技として扱いますか？
User: はい
Bot: （元の質問を2HPで再実行して回答）
```

コマンドは`MoveResolver`のexact input経路で検証する。確認が終わるまで元質問を実行せず、別名も
保存しない。

### 7.3 保存範囲

初期実装では確認済み対応をセッション内だけで利用する。自動global登録は行わない。

- 保存指示なし: session alias。TTL終了で破棄
- 「自分用に覚えて」: identity/RLS実装後にprivate alias candidate
- 「みんな向けに登録」: review_pending。reviewer承認後だけshared resolverへ投入
- 管理者seed: evidence付き`reviewed=true`として投入可能

投稿数や同じ回答の多数決だけで共有aliasへ昇格しない。

## 8. 永続データ設計

### 8.1 canonical移行前

`canonical_moves`が空の間は、既存のキャラ別ソース行をプロセスcacheへ読み、
`character_slug + normalized input + variant_key`を一時identityとしてresolverを構築する。
永続aliasは書かない。既存`move_aliases`はread-only互換経路として残すが、新規書き込みは停止する。

### 8.2 canonical移行後

`canonical_moves`をバックフィルした後、次の検索用テーブルを追加する。

```text
canonical_move_name_forms
  id
  character_slug
  canonical_move_id / family_key
  term
  term_normalized
  term_kind          official_ja | source_en | kana_reading | romaji
  locale / script
  strength / variant_conditions
  source / confidence / reviewed
  valid_from_patch / valid_to_patch
```

ユーザー通称は既存`canonical_move_aliases`を使う。ただし`reviewed`だけでなく、ADR-026の
`workflow_state`、`validity_state`、owner/evidence/revisionを持つalias candidateから、利用可能な
private/shared viewだけをresolverへ供給する。

旧`move_aliases`の2件は、対象inputを確認してcanonical variantへ移す。`sc_name_family`だけを
引き継がず、aliasごとにvariant/familyのどちらかをレビューする。

## 9. APIと実装境界

### Python

```text
src/sf6_engine/move_resolver.py
  normalize_move_reference()
  build_character_search_index()
  resolve_move_reference()
  validate_move_input()
```

`frame_data`から`rag_builder._fetch_move_by_name()`をprivate importする現状を廃止する。

### MCP

read-onlyの`resolve_move`を追加するか、`lookup_move`等の共通内部serviceとして呼ぶ。
外部MCPクライアントにも同じclarification contractを返すため、専用ツールとして公開する方が望ましい。

`register_move_alias`はlegacy扱いとし、user-facing経路から外す。将来は次へ分ける。

- `confirm_move_reference`: pending commandを検証し、元intent用の一時inputを返す
- `submit_move_alias_candidate`: 明示同意後にprivate/review_pending candidateを作る
- `review_move_alias_candidate`: reviewer専用

### Discord

現在の`_pending[(channel_id, author_id)]`を型付きpendingへ置換する。BotはMCPが返した
`clarification.type`に従い、独自に「学習可能」と判定しない。

## 10. 判定基準

自動解決には次をすべて要求する。

1. キャラクターが一意
2. 入力/強度/variant条件に矛盾なし
3. canonical inputでgroup化した候補が一件
4. match methodごとの最低score以上
5. 次点との差が閾値以上
6. 未レビュー読みだけを根拠に短い略称を確定しない
7. family候補が複数variantを持つ場合は強度等が明示済み

推奨初期閾値:

| method | 自動解決 |
|---|---|
| explicit input / exact official / reviewed alias | 常に（variant一意時） |
| exact English / exact reviewed reading | score 0.98以上 |
| unique substring / token prefix | score 0.90以上、次点差0.15以上 |
| reviewed reading substring / romaji | score 0.92以上、次点差0.15以上 |
| typo fuzzy | score 0.86以上、次点差0.15以上、anchor必須 |
| generated unreviewed reading | 候補提示のみ |

数値はgolden corpusで調整し、productionへ直書きしない。

## 11. 評価計画

キャラ、強度、表記種別が偏らないfrozen corpusを作る。

| bucket | 例 | 期待 |
|---|---|---|
| 公式名/入力 | `中 タイガーネクサス`, `214MK` | resolved |
| かな差 | `たいがーねくさす` | resolved |
| 読み略称 | `弱はしょう` | resolved 214LP |
| 英語 | `medium Tiger Nexus` | resolved 214MK |
| ローマ字 | `hashougeki`, `taigaa nekusasu` | resolved |
| 部分略称 | `中ネク` | resolved 214MK |
| variant不足 | `ネク`, `はしょう` | ambiguous |
| typo | `弱波衝撃` | resolved 214LP |
| 無関係通称 | `下デヨ` | needs_command |
| 無効コマンド | `下デヨ`への`2HK`誤返信 | invalid/confirmation、保存なし |
| cross-character | 他キャラの同一alias | 漏洩0 |
| adversarial | 質問全文、集合条件、命令文 | alias化0 |

release gate:

- high-confidence auto-resolution precision 99.5%以上
- strength/variant誤選択 0件
- opaque aliasの誤自動解決 0件
- ambiguous質問の自動確定 0件
- 未レビューaliasのcross-user利用 0件
- 単体技、連携1技目/2技目、確反、コンボの解決結果一致100%

## 12. 段階的な実装順

1. **read-only resolver統合**: 既存ソース行から統一candidate/resultを返す。書き込みなし。
2. **表記normalizer**: かな、英語、レビュー済み読み、ローマ字、部分一致を追加。
3. **Discord clarification**: `needs_command`を型付きpendingへ接続し、確認後に元質問を再実行。
4. **golden評価**: 全キャラの表記変換 + opaque/adversarial holdoutでrelease gateを通す。
5. **canonical backfill**: `canonical_moves`とsource linksを投入し、検索formを永続index化。
6. **private alias**: 主体JWT/RLSと明示同意後に本人限定aliasを有効化。
7. **shared review**: review済みaliasだけを全ユーザー検索へ公開し、旧`move_aliases`を廃止。

最初の実装範囲は1〜4とする。これだけで`中ネク`、`弱はしょう`、英語/ローマ字、
`下デヨ -> コマンド確認 -> その場で回答`まで対応でき、危険なglobal学習を再開せずに済む。
