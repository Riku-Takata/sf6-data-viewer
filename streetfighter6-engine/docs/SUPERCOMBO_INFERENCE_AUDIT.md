# SuperCombo 派生情報を CAPCOM / UFD から再構成できるか

**実施日**: 2026-07-14  
**結論**: 相打ち後の時間計算に必要な `hitstun` は、単純な直接打撃かつ第1持続接触なら
CAPCOM の発生・持続・硬直・ヒット差から高精度に再構成できる。サガット5MPの既存ケースは
SuperComboの `hitstun` を入力に使わず全46件を再現した。一方、相打ち成立の距離・状態、
`hitstop`、および相打ち後式そのものの `-1F` は基本フレーム表だけでは検証できない。

CAPCOM公式備考、属性、UFD条件値まで含めた不一致31セルの追跡と、SuperCombo非依存設計は
`docs/SUPERCOMBO_CONTEXT_AUDIT.md` を参照。

## 検証した仮説

第1持続が接触する単純な直接打撃では、フレーム表の定義から次を計算できる。

```text
total          = startup + active_duration + recovery - 1
hitstun        = active_duration + recovery + on_hit
blockstun      = active_duration + recovery + on_block
punish_adv     = on_hit + 4
after_dr_hit   = on_hit + 4
after_dr_block = on_block + 4
perf_parry_adv = 2 - active_duration - recovery
```

`hitstun` は次の形でも同じである。

```text
hitstun = total - startup + on_hit + 1
```

ただし、多段技、持続の途中当て、飛び道具、ヒット時と空振り時で異なる硬直、KD、空中状態、
強化状態などには、この単一式を無条件には適用しない。

## データリークを避けた方法

- SuperComboは正解ラベルにだけ使用した。
- 推論入力にはCAPCOM単独、UFD単独、CAPCOM優先UFD補完を別々に使用した。
- 対象は全30キャラの基本地上通常技12種、計360技とした。
- 同一技の対応は `立ち中P -> 5MP`、`Standing Medium Punch -> 5MP` の固定表記変換だけで行った。
- フレーム値の一致を使う技同定、SuperComboの技名・notes、既存のフレームシグネチャ照合は使っていない。
- SuperCombo内部の式一致は対照群であり、独立ソース検証の成績には数えない。

使用行数は CAPCOM 2,357、UFD 1,559、SuperCombo 2,118。スナップショットは揃っておらず、
SuperCombo手元JSONは2026-04-26取得・DB取込は2026-05-15、CAPCOMの大半は
2026-05-28パッチ、UFDは2026-07-10取得である。このため、未整合の生スコアと、式の入力値が
SuperComboと一致する版整合相当の層を分けた。

再実行スクリプト:

```bash
cd streetfighter6-engine
PYTHONPATH=src ./.venv312/bin/python \
  tests/supercombo_inference_audit.py \
  --output /tmp/supercombo_inference_results.json
```

## 全30キャラの結果

### CAPCOMだけを推論入力にした結果

| SuperCombo正解ラベル | 予測可能 / 正解あり | 完全一致 | 完全一致率 | 入力値がSCと同じ層 |
|---|---:|---:|---:|---:|
| `hitstun` | 289 / 304 | 280 | **96.89%** | 234 / 236 = **99.15%** |
| `blockstun` | 317 / 335 | 297 | **93.69%** | 252 / 253 = **99.60%** |
| `total` | 268 / 284 | 266 | **99.25%** | 259 / 259 = **100%** |
| `punishAdv` | 275 / 277 | 272 | **98.91%** | 272 / 274 = **99.27%** |
| `afterDRHit` | 299 / 305 | 297 | **99.33%** | 297 / 298 = **99.66%** |
| `afterDRBlk` | 324 / 331 | 321 | **99.07%** | 321 / 323 = **99.38%** |
| `perfParryAdv` | 307 / 329 | 288 | **93.81%** | 251 / 253 = **99.21%** |

生スコアの不一致は主に、版ずれ、SC側の条件付き硬直を単一値にできない行、ヒット・ガード・
空振りで硬直が異なる行に集中した。式の入力値が同じでも外れた代表例はJuri 5LKと
Zangief 2MKであり、Feng Shui Engineの連携状態や個別のフレーム定義を例外ルールとして
扱う必要がある。

### UFDだけを推論入力にした結果

| SuperCombo正解ラベル | 予測可能 / 正解あり | 完全一致 | 完全一致率 |
|---|---:|---:|---:|
| `hitstun` | 263 / 304 | 230 | **87.45%** |
| `blockstun` | 285 / 335 | 247 | **86.67%** |
| `total` | 267 / 284 | 257 | **96.25%** |

UFDは補完範囲を増やすが、パッチ識別子がなく、行内部の数値が総動作式と一致しない例もある。
例えばSagat 5MPはUFD上で startup 6 / active 3 / recovery 17 / total 24 /
on-hit +4 で、フィールド同士も一つの総動作式に揃わない。したがって、CAPCOM欠損時にUFDを
無条件採用すると、カバレッジと引き換えに正解率が下がる。UFD補完には同一パッチ確認または
行内整合性ゲートが必要である。

## 相打ち後ケースの再現

現行モデルの相打ち後有利は次式である。

```text
attacker_advantage = attacker_hitstun - defender_hitstun - 1
```

この式へ入れる各技の `hitstun` をCAPCOMだけから再構成し、SuperComboの `hitstun` 差を
正解ラベルとして比較した。

| 入力ソース | 標準地上通常技 × 4F技 | 完全一致率 | Sagat 5MP × 4F技 |
|---|---:|---:|---:|
| CAPCOM | 12,880 / 13,294 | **96.89%** | **46 / 46、+6～+12F** |
| CAPCOM優先 + UFD補完 | 12,972 / 13,708 | **94.63%** | **46 / 46、+6～+12F** |
| UFD | 9,902 / 11,835 | **83.67%** | 2 / 45、+5～+11F |

代表値もSuperComboを推論入力に使わず再現できた。

```text
Sagat 5MP hitstun = active 4 + recovery 15 + on-hit 6 = 25
Ryu   2LP hitstun = active 2 + recovery  9 + on-hit 4 = 15
post-trade        = 25 - 15 - 1 = +9F

Sagat 2LP hitstun = active 2 + recovery 10 + on-hit 5 = 17
post-trade        = 25 - 17 - 1 = +7F
```

これは「現行モデルへ渡す `hitstun` はSCなしで作れる」ことを示す。ただし、正解ラベル側も
同じ相打ち式で作っているため、末尾の `-1F` がゲーム内で正しいことまでは証明しない。

## 統一できる情報と、別の観測が要る情報

| 分類 | 項目 | 方針 |
|---|---|---|
| CAPCOMから派生可能 | `total`, `hitstun`, `blockstun`, `punishAdv`, `afterDRHit/Blk`, 多くの`perfParryAdv` | 条件付きの型を保った決定論ルールへ移す |
| System rule候補 | `hitstop` | ボタン強度の9/11/13F規則は306/328 = 93.29%で例外が多く、推測採用しない |
| Geometryが必要 | `atkRange`, 相打ち成立、ガード後到達 | UFD GIFを座標校正し、pushbox・開始距離・接触Fを含める |
| 状態/相互作用が必要 | KD、空中、juggle、armor、無敵、投げ、飛び道具、cancel | ゲームルールまたは技対技のレビュー済み観測を使う |
| 数値から生成不能 | 戦術notes、用途説明 | 必要なら文書ソースとして分離し、フレーム真値と混ぜない |

基本地上通常技360件ではSCの `atkRange` とnotesが各348件あり、UFD GIFは360件すべてに
存在した。しかし、フレーム総数から距離や解説文を逆算する式はない。時間データの統一と、
空間・状態・戦術文書の情報源は分けるべきである。

## 現行検証で残る循環

1. `analyze_sequence()` は現在も技列挙、`hitstun`、notesのため `sc_moves` を必ず読む。
2. 既存unit testはSC由来の25F/15Fを同じ式へ入れて+9Fを確認しており、ゲーム内真値の検証ではない。
3. 本番E2EもDB値から同じ式を再実行しているため、`-1F`やhitstop相殺を独立には保証しない。
4. 唯一の同梱`+7F`報告は相手技が不明で `reviewed=false` のため、式の校正には使えない。
5. 観測のpatch versionは現パッチと直接照合されず、fingerprintも防御側hitstun/hitstopを含まない。

## 推奨する移行順

1. `derived_temporal_profile` を追加し、CAPCOM採用値から `hitstun` 等を条件付きで生成する。
2. 基本地上通常技の候補列挙をcanonical move IDへ移し、SC読み取りを失敗させるテストクライアントで
   タイムライン・相打ち後区間・追撃タイミングまで動くことを保証する。
3. UFDは同一パッチまたは行内整合性が確認できた値だけ補完に使う。値が衝突したらCAPCOMを優先し、
   推測値ではなく保留を返す。
4. 同一パッチのCAPCOM / UFD / SCスナップショットで監査を再実行し、SCは実行時ソースではなく
   回帰用の正解ラベルへ降格する。
5. 相手技まで固定した20～50件のフレームステップ実測を集め、offset `0/-1`、異なるhitstop、
   立ち/しゃがみ、持続途中接触をblind holdoutで検証する。
6. `-1F` とhitstop方針が確定するまでは、相打ち後結果を `calculation_model` のまま表示し、
   実測確定値と同じ保証レベルに上げない。

したがって、情報源統一の第1段階は実行可能である。対象は「単純な直接打撃の時間情報」で、
CAPCOMを主値にすれば、現在SuperComboから読んでいる `hitstun` を実行時依存から外せる。
一方、SuperCombo全体をただちに削除するのではなく、同一パッチの検証oracleへ役割変更し、
空間・状態・戦術文書は別パイプラインとして残すのが安全である。
