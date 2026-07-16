# SF6 Engine — SF6 対戦アシスタント

Street Fighter 6 のフレームデータとゲーム知識を自然言語で引き出せる個人向け CLI アシスタント。

```
$ python -m sf6_engine.cli ask "サガットの2HKの発生は?"
サガットの2HK（しゃがみ強K）の発生は11Fです。

$ python -m sf6_engine.cli ask "サガットの2HKガードして反撃できる?"
ガード時 -12F → 発生12F以内がフレーム上の候補です。
ガード後距離と到達が未検証のため、確定反撃としては未確定です。
```

## アーキテクチャ

```
ユーザーの質問 (自然言語)
    │
    ▼
Intent Parser (定型フレーム質問は決定論 / その他はLLM)
    │  技名と距離/持続/状態/視点を分離して scenario 化
    ▼
Move Resolver
    │  resolved / ambiguous / not_found + 候補・根拠
    ▼
Typed Frame Profile Service
    │  CAPCOM公式を主値、UFD / SuperComboを補完値としてフィールド別に統合
    │  単一値・範囲・複数持続・着地硬直・条件別値・KDを型付きで保持
    │  ガード防御側の値は攻撃側値をコードで符号反転
    ▼
Scenario Evaluator / Punish Service
    │  条件適用値、時間窓、到達証明を別々に判定
    ▼
Sequence Engine
    │  2技連携、最速/ディレイ暴れ、相打ち後有利、追撃確度を計算
    ▼
Answer Generator
    │  コア数値と連携は決定論、一般知識はLLM
    ▼
CLI 出力
```

**データソース:**
- CAPCOM 公式 (Layer 1 Lambda で自動収集) → 発生/硬直/ダメージ等
- [SuperCombo Wiki](https://wiki.supercombo.gg/w/Street_Fighter_6) → リーチ/パニカン有利/解説テキスト等
- Ultimate Frame Data → 実測の全体/持続/着地硬直、キャンセル、パッチメモ、当たり判定GIF

同じ値を1列へ上書きせず、全ソースの生値・取得時点・採用ソースを保持する。
CAPCOM硬直欄の `全体52` は硬直52Fとは解釈せず、UFD/SCに硬直値があれば補完する。

SuperCombo由来データは
[CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/)
の条件下で非営利利用する。本プロジェクトではHTML/MediaWikiマークアップ除去、数値正規化、
入力表記変換、CAPCOM/UFDデータとの統合を行っている。帰属と再利用条件は
[THIRD_PARTY_DATA.md](THIRD_PARTY_DATA.md) を参照すること。

## 必要なもの

- Python 3.11+
- [Ollama](https://ollama.ai) + Gemma4 モデル
- Supabase プロジェクト (Layer 1 が構築済みであること)

## セットアップ

### 1. 依存ライブラリのインストール

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Ollama と Gemma4 のインストール

```bash
# Ollama インストール (macOS)
brew install ollama

# Gemma4 モデルの取得 (7.2GB)
ollama pull gemma4:e2b

# 埋め込みモデルの取得 (274MB)
ollama pull nomic-embed-text

# サーバー起動 (質問する前に必要)
ollama serve
```

> **RAM の目安:**
> - `gemma4:e2b` (7.2GB) → 16GB RAM 推奨
> - RAM が少ない場合は `gemma3:4b` (2.5GB) を代替として使用可

### 3. 環境変数の設定

`.env` ファイルを作成:

```bash
cp engine_dotenv_example.txt .env
# 実値を記入
```

| 変数名 | 説明 | デフォルト |
|---|---|---|
| `SUPABASE_URL` | Supabase プロジェクト URL | 必須 |
| `SUPABASE_ANON_KEY` | anon key (読み取り用) | 必須 |
| `LLM_PROVIDER` | `ollama` または `gemini` | `ollama` |
| `OLLAMA_BASE_URL` | Ollama サーバー URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | 使用モデル | `gemma4:e2b` |
| `OLLAMA_EMBED_MODEL` | 埋め込みモデル | `nomic-embed-text` |

### 4. Ultimate Frame Data の取り込み

まず [ultimate_frame_data_migration.sql](sql/ultimate_frame_data_migration.sql) を
Supabase Studio の SQL Editor で一度適用する。DDL はこのプロジェクトでは手動運用である。

```bash
# ケンだけを取り込む（GIF本体は保存しない）
PYTHONPATH=src python -m sf6_engine.importers.ultimate_frame_data --character ken

# 全キャラを取り込む。公開サイトへ負荷を掛けないようページ間を1秒空ける。
PYTHONPATH=src python -m sf6_engine.importers.ultimate_frame_data --all --delay 1.0

# HTML解析のみの確認（DB/Storageへ書き込まない）
PYTHONPATH=src python -m sf6_engine.importers.ultimate_frame_data \
  --character ken --dry-run --html-path /path/to/ken.html
```

GIF本体は容量が大きいため既定では保存しない。`ufd_moves` には技との対応と
UFD元URLを保持するため、Botから元GIFは引き続き参照できる。アーカイブが必要な
場合だけ `--gifs` を明示指定し、private bucket `sf6-ufd-hitboxes` へ保存する。

## 使い方

### 質問する

```bash
PYTHONPATH=src python -m sf6_engine.cli ask "サガットの2HKの発生は?"

# 連携・相打ち後・追撃まで解析
PYTHONPATH=src python -m sf6_engine.cli ask \
  "サガットの5MP→5MPに発生4Fで最速暴れすると相打ち後は?"
```

**対応している質問タイプ:**

| タイプ | 例 |
|---|---|
| 単一技照会 | 「サガットの2HKの発生は?」 |
| 持続・硬直 | 「ケンの大Kの持続は?」「硬直は?」 |
| ガード両視点 | 「ガードさせたら?」=攻撃側 / 「ガードしたら?」=防御側 |
| 技条件検索 | 「ラシードの技の中でガードさせて有利な技は?」(確定/条件付き/保留を分離) |
| 状況付き硬直差 | 「ケンの大Kを先端でガードしたら?」「最終持続をガードさせたら?」 |
| 反撃判定 | 「サガットの2HKガードして反撃できる?」(時間候補と到達確度を分離) |
| 連携・相打ち | 「5MP→5MPに最速4F暴れした相打ち後は?」(両視点の有利差と追撃) |
| 比較 | 「サガットとルーク、立ち強Pどっちがリーチ長い?」 |
| 複数フィールド | 「サガットの2HKでパニカン取ったら何F有利?」 |
| ゲーム概念 | 「ドライブインパクトって何?」(Phase 2 で精度向上) |

### デバッグモード (`-v`)

Intent 解析結果と参照データを表示する:

```bash
PYTHONPATH=src python -m sf6_engine.cli ask "サガットの2HKの発生は?" -v
```

```
[Intent]
  {"intent_type": "lookup_move", "chara": "Sagat", "input": "2HK", ...}

[参照データ]
  【sagat / 2HK (しゃがみ強K（タイガーキック）)】
  発生: 11F
  ガード時: -12F
  ...

[回答]
サガットの2HK（しゃがみ強K）の発生は11Fです。
```

### フレームデータ直接検索 (LLM なし)

```bash
PYTHONPATH=src python -m sf6_engine.cli lookup sagat "立ち弱P（タイガージャブ）"
```

## 現在のデータカバレッジ

| カテゴリ | 状態 | 備考 |
|---|---|---|
| 型付き統合照会 (全30キャラ) | ✅ | 原典にある値を発生/持続/硬直/ヒット・ガード差として保持 |
| 通常技 578攻撃行 | ✅ | 発生/持続/硬直/ガード差 578/578 |
| 特殊技・必殺技・SAの数値網羅 | ⚠ | 対象外・状況依存を分離済み。原典未収録/未解決値は継続対応 |
| 条件付き・多段・空中技 | ✅ | 範囲/複数区間/着地硬直を非スカラーで保持 |
| ガード攻撃側・防御側 | ✅ | 防御側は決定論的な符号反転 |
| UFD当たり判定GIF | ✅ | 取得可能773件をprivate Storageへ保存 |
| ゲームシステム文書 | ✅ | Bedrock Titanハイブリッド検索 |
| 質問条件の保持 | ✅ | 距離/接触持続/状態/DR/Burnout/画面端/視点をscenario化 |
| 技名の曖昧性 | ✅ | 複数強度・派生は数値計算せず確認候補を返す |
| フレーム上の反撃候補 | ✅ | ジャンプ技・連携途中を除外、到達/リソース未検証を明示 |
| 2技連携・相打ち解析 | ✅ | 両者の遅延、相手技別hitstun分布、完全一致した実測値と追撃確度を分離 |
| 距離込み確定反撃 | ⚠ | 時間と空間を分離済み。geometry/実測のDB投入は未完了 |
| ヒット後接続の全条件対応 | ⚠ | キャンセル/距離/空中状態の型拡張が必要 |

追加の条件付きデータモデルは [CONTEXTUAL_FRAME_MODEL.md](docs/CONTEXTUAL_FRAME_MODEL.md) と
[contextual_frame_model_migration.sql](sql/contextual_frame_model_migration.sql) を参照。
連携・相打ちモデルは [SEQUENCE_ANALYSIS.md](docs/SEQUENCE_ANALYSIS.md) と
[sequence_analysis_migration.sql](sql/sequence_analysis_migration.sql) を参照。

## 次の設計対象

- 追加スキーマ適用と正規技ID/条件付き観測のバックフィル
- `sequence_analysis_migration.sql` 適用とレビュー済み連携観測のupsert
- SC `atk_range`、ガード後距離、レビュー済みUFD geometryを使った到達可能性判定
- パッチ単位のBurnout/DR/カウンター補正ルール投入
- ヒット有利・キャンセル・チェーン・空中/KD状態を使う接続候補計算
- ソース更新日時/パッチ差分を使う鮮度監視

## 検証

```bash
# 単体テスト
PYTHONPATH=src ./.venv312/bin/python -m unittest discover -s tests -p 'test_*.py'

# 全ソース・全技の型/視点/回答監査
PYTHONPATH=src ./.venv312/bin/python tests/frame_profile_comprehensive_audit.py

# Discord bot実経路の全質問（基礎5系統 + 確反提案/判定保留）
SF6_MCP_LOCAL_ONLY=1 PYTHONPATH=src ./.venv312/bin/python \
  tests/bot_comprehensive_eval.py --exhaustive \
  --summary-only --jsonl '' --concurrency 16
```

2026-07-13時点: unittest 79/79、統合監査92,940 assertions、bot 9,728問
（発生/持続/硬直/攻撃側/防御側 各1,790 + 確反778）、すべて0失敗。
0失敗は保存済み値の型・出所・視点・回答の整合性を示し、数値網羅率100%を意味しない。
攻撃行の詳細な充足数は `tests/frame_profile_comprehensive_results.json` を参照。

## 失敗パターンログ

| パターン | 原因 | 対処済み |
|---|---|---|
| 波動拳/昇竜拳を通常技に誤マッピング | Gemma4 の学習バイアス | ✅ キーワード検出 + 事後除去 |
| punish_check でパニカンFを反撃Fと混同 | コンテキスト不足 | ✅ 反撃可否を自動計算して追記 |
| 発生だけでジャンプ技まで確反扱い | 距離・実行可能状態を未分離 | ✅ 時間候補へ訂正、非ニュートラル技を除外 |
| ゲーム概念の精度が低い | 文書データなし | Phase 2 で解決予定 |

## AWS EC2 でのリモート Ollama 利用

```bash
# .env の OLLAMA_BASE_URL を変更するだけで切り替え可能
OLLAMA_BASE_URL=http://<ec2-ip>:11434

# EC2 セットアップは ADR.md の ADR-013 を参照
```
