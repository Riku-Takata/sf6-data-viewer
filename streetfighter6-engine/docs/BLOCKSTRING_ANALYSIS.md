# 連続ガード・割り込み解析の設計

## 現在の対応範囲

`sequence_analysis` は、2技をDBで解決した後に技間の遷移を分類する。通常のlinkは
`on_block/on_hit + startup`、special/SA/連打キャンセルは1技目のrecoveryを使わず、
hitstop終了後のblockstun/hitstunとtarget startupを共通基準に評価する。
キャラ固有の必殺技名はIntent Parserに登録せず、不透明な技名のまま統合DB resolverへ渡す。

2026-07-14時点で次を実装済みである。

- `Sp`、`SA`/`SA1..3`、`Chn` とtarget種別から、special/SA/地上弱攻撃chainを識別する。
- `→`, `>`, `から`, `の後に`, `AをBでキャンセル`, `into` を決定論のsequence intentに分解する。
- `2中K`や`立ち弱P`の汎用入力表記だけを正規化し、`弱 波掌撃`のような固有名はDBで全技共通解決する。
- 誤記は、強度/SA prefixが一致し、他候補と十分な差がある一意の近似名だけを補正する。
- `actionable_gap_f` と防御側技のfirst activeを分け、genericな4F指定は時間上の結果だけを返す。
- 旧RAGの `abs(block_adv)-startup` 派生gap式を削除し、単体派生技データから割り込み可否を断定しない。
- generic 4Fの実DB経路の引数不整合を修正した。

`~`を含む専用派生は、通常のlink/special cancelとは別のsource-input edgeとして扱う。
SuperCombo注記にblock上の`Nf gap`または`true blockstring`が直接書かれたedgeだけは
`defender_actionable`基準で実行する。注記なしのchain、Drive Rush cancel、構え・空中・溜め・hit-only
などは、個別のreview済みedgeがない限りlink式へfallbackせず判定保留にする。

## ケンの基準ケース

2026-07-14取得済みデータでは次の値が一致している。

| 技 | SuperCombo | UFD | 用途 |
|---|---:|---:|---|
| `2MK` blockstun | 16F | - | 防御側が動けるまでの時間 |
| `2MK` cancel | `Sp SA` | `Special, Super` | special cancel可能性 |
| `236LK` startup | 12F | 12F | 弱迅雷脚の最初の攻撃判定 |
| `236MK` startup | 16F | 16F | 中迅雷脚の最初の攻撃判定 |
| `236HK` startup | 25F | 25F | 強迅雷脚の最初の攻撃判定 |

標準的な接触時最速special cancelとして、hitstop終了後を共通の基準時点にすると次のようになる。

```text
target_active_f       = transition_start_offset_f + attacker_delay_f + target.startup_f
defender_actionable_f = opener.blockstun_f + scenario_blockstun_modifier_f
defender_active_f     = defender_actionable_f + defender_delay_f + defender.startup_f

actionable_gap_f      = target_active_f - defender_actionable_f
active_delta_f        = target_active_f - defender_active_f
```

最速・追加補正なし・`transition_start_offset_f=0`なら以下になる。

| 連携 | actionable gap | 最速4F技との時間比較 | 判定 |
|---|---:|---:|---|
| `2MK -> 236LK` | `12 - 16 = -4F` | 防御側は行動不能 | 連続ガード |
| `2MK -> 236MK` | `16 - 16 = 0F` | 防御側は行動不能 | 連続ガード |
| `2MK -> 236HK` | `25 - 16 = 9F` | 4F技が5F先にactive | 時間上は割り込み可能 |

`236HK`への4F割り込みは時間上成立する。ただし「どの4F技でも実際に当たる」とは断定しない。
相手キャラと技を指定し、距離、pushback、無敵、攻撃属性、当たり判定を実測またはgeometryで
確認できた場合だけ `interrupt_confirmed=true` とする。

## 遷移モデル

結果の `transition` は、2技間の遷移種別と根拠を返す。現在の実装対象は`link`、category-derivedな
最速`cancel` (special / super / light chain)、および直接根拠またはreview済みedgeを持つ
`chain`/`target_combo`/`stance_followup`である。

```text
TransitionProfile
  type: link | cancel | chain | target_combo | stance_followup
  status: resolved | unresolved
  timing_reference: recovery_end | hitstop_end | defender_actionable
  source: SuperCombo (category-derived時)
  cancel_raw: 例 "Sp SA"
```

遷移種別ごとの評価は分離する。

| 種別 | 2技目の開始基準 | 必須データ |
|---|---|---|
| `link` | 1技目recovery終了 | 条件適用後on_block/on_hit、target startup |
| `cancel` | 接触・hitstop終了 | blockstun/hitstun、cancel category、target startup、cancel offset |
| `chain` | 接触・hitstop終了（汎用地上弱攻撃） / 個別window | `Chn`+同一状態の地上弱攻撃、またはreview済みedge |
| `stance_followup` | branch window | 個別派生edge、window、専用startup定義 |
| `drive_rush_cancel` | DR到着時点 | DR cancel可否、freeze/到着rule、状態補正 |

### 遷移合法性の根拠

優先順位は次の通りとする。

1. patch一致のレビュー済み `source_transition_rules` exact edge
2. SuperCombo注記が直接示すblock gap / true blockstring edge
3. CAPCOM/UFD/SCのcancel categoryが一致する標準category rule
4. 根拠不足なら `transition_unresolved`

category ruleは「通常技がspecial cancel可、targetが地上special」のような狭いpredicateだけを
実行可能にする。構え、空中、溜め、リソース、特定派生、hit-only等は個別edgeなしに推測しない。
`source_transition_rules`はcanonical move backfill前のレビュー先である。`A~B`候補は
`importers/source_transition_rules.py`で全キャラを抽出し、`reviewed=true`のexact edgeだけが
注記fallbackより優先される。`canonical_moves`と`move_transition_observations`のバックフィル後も、
同じpatch・条件・evidence契約を保って移行する。

## 判定契約

`actionable_gap_f` と指定技の勝敗を分ける。

| 条件 | timing result | 表示 |
|---|---|---|
| `actionable_gap_f <= 0` | `true_blockstring` | 連続ガード。通常入力では行動不能 |
| gapあり、defender activeが先 | `interrupt_timing_win` | フレーム上は割り込み可能 |
| 両activeが同時 | `interrupt_trade_if_reach` | フレーム上同時。到達時は相打ち候補 |
| target activeが先 | `frame_trap` | 押せるが次技に負ける |
| 必須値・遷移根拠不足 | `unresolved` | 不足フィールドを明示して保留 |

さらに以下を独立に返す。

```text
timing_interruptible: true | false | null
spatial_connected: true | false | null
state_compatible: true | false | null
interaction_compatible: true | false | null
interrupt_confirmed: true | false | null
```

genericな「4F技」では `timing_interruptible` までを返す。実際の成功を求める場合は相手キャラと
技を確認し、4F通常技が届くか、無敵技か、投げ/飛び道具/armor等かを別評価する。

## Scenario

同じ連携でも次の条件で結果が変わり得るため、質問とcache keyへ含める。

- block / hit、通常ガード / Burnout、Drive Rush強化
- 1技目の接触active、持続当て、段数
- cancelの最速/ディレイ、派生入力window
- 立ち/しゃがみ/空中、画面端、距離
- target variant、強化状態、リソース
- defender character / move / delay / reversal属性
- patch version

## Intentと回答

sequence fast pathは以下を決定論で扱う。

- 表記: `2中K`, `屈中K`, `2MK`
- variant付き技名: `弱/中/強/OD 迅雷脚`（技名はハードコードしない）
- 日本語公式技名: `立ち弱P→弱 波掌撃`（DB解決後は`5LP→214LP`）
- 自然言語の接続: `立ち弱Pからしゃがみ弱P`, `立ち中Pの後に弱 百裂脚`, `2MKをOD必殺技でキャンセル`
- P/Kの大文字小文字を同一視し、技名の誤記は全技共通の一意近似解決に限る
- 質問語: `連続ガード`, `割り込める`, `暴れられる`, `フレームトラップ`, `隙間`
- 明示command: `2MK -> 236MK`, `2MK -> 236HK`

Intentは回答の主題も分ける。

- `blockstring`: 連続ガードか、技間gapはいくつか
- `interrupt`: 指定した発生F/技で割り込めるか
- `combo_timing`: 連続ヒットか
- `terminal_frame_advantage`: 2技目をガード/ヒットさせた後の有利不利

summaryは主題への結論を1行目に置く。遷移種別、blockstun、startup、共通タイムラインは
構造化レスポンスに保持するが、単純なyes/no質問の前段には並べない。2行目には、結論の適用範囲に
必要な距離・pushback等の注意だけを残す。

想定回答:

```text
はい、フレーム上は連続ガードです。技間の隙間は0Fです。
※距離・pushback・姿勢・無敵により、2技目が実際に届くかは別途確認が必要です。
```

```text
はい、フレーム上は発生4F技で割り込めます。2発目より5F先に発生します。
※距離・pushback・姿勢・無敵により、2技目が実際に届くかは別途確認が必要です。
```

## 次の検証ゲート

1. Ken `2MK -> 236LK/MK/HK`を含む各強度と、具体的な相手4F技をトレーニングモードでframe step確認する。
2. `tests/sequence_comprehensive_audit.py` で、全30キャラの保存済み入力・公式名と全ordered pairを回帰監査する。
3. `source_transition_rules_migration.sql`を適用し、直接根拠候補を`reviewed=false`でstageする。
4. 残る派生候補をframe-stepでreviewし、競合edgeを解消してから`reviewed=true`にする。
5. Burnout/DR、hit-only、window依存のchain/stance followupへ遷移種別ごとに範囲を広げる。

2026-07-16の実DB監査では、全30キャラのSuperCombo入力2,118件とCAPCOM公式技名
2,357件は未検出0件だった。強度省略の同名263件は一意に選ばず聞き返す。通常入力の
ordered pair 103,073件は全て遷移分類を実行し、70,006件は単一値でtimeline解決、33,145件は
空中技・投げ・条件値等のscalar不足を理由付きで保留した。

完了条件は、Ken goldenが期待どおりであることだけではない。全経路が同じserviceを使用し、
遷移根拠がないケースを誤ってlink/cancelと断定しないこと、generic 4Fで到達まで断定しないこと、
patch変更時に旧edgeを失効できることを必須とする。
