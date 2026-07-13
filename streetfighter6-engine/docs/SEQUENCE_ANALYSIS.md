# 連携・相打ち・追撃解析の設計

## 目的

単発技のフレーム表を読み上げるだけでなく、「5MP→5MPに最速4F暴れ」のような
複数の行動を共通タイムラインで評価する。時間、距離、状態、実測を分離し、根拠より
強い結論を返さない。

```mermaid
flowchart LR
  Q["自然言語の連携質問"] --> I["Sequence Intent Parser"]
  I --> R["攻撃側・防御側の技解決"]
  R --> F["CAPCOM主値 + UFD/SC補完"]
  F --> T["Shared Timeline"]
  T --> C["接触結果の確度判定"]
  C --> O["実測観測 / hitstunモデル"]
  O --> N["追撃の時間・距離・状態判定"]
  N --> A["決定論回答"]
```

## ソース方針

| 値 | 採用方針 |
|---|---|
| 発生・持続・硬直・ヒット/ガード差 | 既存の統合プロファイル。CAPCOM公式を主値、UFDとSCを補完に使う |
| `hitstun` / `blockstun` / `hitstop` | SuperComboの生値。統合値へ上書きせず補助根拠として保持 |
| リーチ・技解説 | SC `atk_range` / notes。単体リーチだけで接触確定にしない |
| 当たり判定GIF | UFD private Storage資産。座標校正済みgeometryへ変換後のみ計算に利用 |
| 相打ち結果・追撃 | 相手キャラ+技まで一致するレビュー済み `sequence_observations`を最優先。無ければラベル付きモデル |

SuperComboのアクセス制限を回避するクローラは実装しない。利用条件に従って人手で取得した
JSON/HTMLスナップショットを既存インポーターで取り込み、取得日・パッチ・根拠を残す。

## タイムライン

現在は攻撃側2技、1技目のblock/hit、2技目と防御側行動の遅延Fを扱う。

```text
attacker_ready = max(0, -initial_advantage)
defender_ready = max(0,  initial_advantage)
attacker_active = attacker_ready + attacker_delay + second_move_startup
defender_active = defender_ready + defender_delay + defender_move_startup
```

両者のactive開始が同じなら「フレーム上は同時」である。それだけで相打ちとは断定せず、
両方が届くこと、無敵・アーマー・飛び道具などの特殊相互作用がないことを別に要求する。
数値のない「ディレイ」は0Fへ潰さず、フレーム数を確認する。

## 相打ち後の結果

優先順位は次の通り。

1. 相手キャラ+技、現在のフレーム指紋、条件がすべて一致するレビュー済み観測
2. 相手技が特定済みの `hitstun` 差モデル
3. 「発生4F技」のように相手技が未特定なら、該当技を1件ずつ計算した分布と最小・最大区間
4. 必要値が無ければ未解決

同時の直接打撃に限る現モデルは次式を使う。

```text
attacker_advantage = attacker_inflicted_hitstun
                   - defender_inflicted_hitstun
                   - 1
```

共通のhitstopは差へ加算せず、生値は証拠に残す。この式は `calculation_model` と明記し、
実測値と同じ確度で表示しない。

発生Fは相打ちする時点を決める値であり、相打ち後の有利差を一意には決めない。同じ4F技でも
相手へ与えるhitstunが異なるため、相手技未指定時に代表値を選んではならない。

## 追撃の保証レベル

| フィールド | 意味 |
|---|---|
| `timing_connected` | 発生Fが有利差以内 |
| `spatial_connected` | 実測または校正済みgeometryで届く |
| `state_connected` | 立ち/しゃがみ/空中・juggle等の状態が適合 |
| `combo_confirmed` | 上記を直接実測で確認済み |

発生が間に合うだけの技は「フレーム上の追撃候補」とし、連続ヒット確定とは呼ばない。

## サガットの検証ケース

`5MP→5MP / ガード後 / 最速4F暴れ / 相打ち` は次の根拠で返す。

- 1発目のガード差は攻撃側 `+2F`（CAPCOM公式採用値）
- 2発目は発生 `6F`、防御側は2F遅れて発生4F技を開始するため両方とも共通6F目
- SC実データの地上4F通常技46件はhitstunが異なり、技別計算はサガット側 `+6～+12F`
- 例: `Ryu 2LP`はhitstun 15Fなので `25 - 15 - 1 = +9F`
- 例: `Sagat 2LP`はhitstun 17Fなので `25 - 17 - 1 = +7F`
- `2MP`は発生7Fなので時間上44/46技で接続するが、+6Fになる2技には接続しない
- 距離・接触状態を未検証のため、上記の時間候補をそのまま確認済みコンボとは呼ばない

## 永続化

`sql/sequence_analysis_migration.sql` を `contextual_frame_model_migration.sql` の後に適用する。

```bash
# SQL適用前のJSON検証
PYTHONPATH=src ./.venv312/bin/python -m \
  sf6_engine.importers.sequence_observations --dry-run

# SQL適用後に観測をupsert
PYTHONPATH=src ./.venv312/bin/python -m \
  sf6_engine.importers.sequence_observations
```

DB移行中でもLambda/Botを壊さないよう、観測JSONローダーを同梱する。現在の初期行は、過去に
報告された`+7F / 2MP`について相手技IDが記録されていないため、`reviewed=false`かつ
`usable_for_exact_answer=false`の不完全証拠としてのみ保存する。数値または確認済み追撃を持つ
レビュー済み観測には、相手キャラと技入力の両方を必須とする。観測keyにもこの2項目を含める。

## 現在の境界

対応済み:

- 2技のblock/hit連携、攻撃側・防御側それぞれの遅延
- 相手を発生Fだけで指定した区間結果と、キャラ+技指定の個別結果
- 同時発生、相手技別SC hitstun差モデル、追撃の確度分離
- Intent、CLI/RAG、MCP、Discord Botの共通エンジン化
- AWS MCP Lambdaへの反映と、ローカルフォールバック無効の本番E2E検証

未対応:

- 3技以上のシーケンス、移動・ジャンプ・飛び道具のイベント
- armor/invulnerability/throw/projectileの衝突解決
- UFD GIFの自動geometry抽出とpushbox/カメラ補正
- cancel窓、chain、juggle、空中高度を含む全追撃の確定
- 全キャラ・全技組合せの直接実測
