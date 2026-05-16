# SF6 Engine — SF6 対戦アシスタント (M1)

Street Fighter 6 のフレームデータとゲーム知識を自然言語で引き出せる個人向け CLI アシスタント。

```
$ python -m sf6_engine.cli ask "サガットの2HKの発生は?"
サガットの2HK（しゃがみ強K）の発生は11Fです。

$ python -m sf6_engine.cli ask "サガットの2HKガードして反撃できる?"
ガード時 -12F → 発生 12F 以内の技なら確定反撃が入ります。
```

## アーキテクチャ

```
ユーザーの質問 (自然言語)
    │
    ▼
Intent Parser (Gemma4 via Ollama)
    │  "サガットの2HKの発生は?" → {type: lookup_move, chara: Sagat, input: 2HK}
    ▼
RAG Context Builder
    │  Supabase の unified_moves から該当技を取得
    ▼
Answer Generator (Gemma4 via Ollama)
    │  フレームデータ + ゲーム知識 → 自然言語回答
    ▼
CLI 出力
```

**データソース:**
- CAPCOM 公式 (Layer 1 Lambda で自動収集) → 発生/硬直/ダメージ等
- SuperCombo Wiki → リーチ/パニカン有利/解説テキスト等

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

## 使い方

### 質問する

```bash
PYTHONPATH=src python -m sf6_engine.cli ask "サガットの2HKの発生は?"
```

**対応している質問タイプ:**

| タイプ | 例 |
|---|---|
| 単一技照会 | 「サガットの2HKの発生は?」 |
| 反撃判定 | 「サガットの2HKガードして反撃できる?」 |
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

## 現在のデータカバレッジ (M1)

| カテゴリ | 状態 | 備考 |
|---|---|---|
| 通常技 (全30キャラ) | ✅ | 発生/硬直/リーチ/パニカン等 |
| 必殺技・SA | ❌ | M2 で対応予定 |
| ゲームシステム文書 | ❌ | Phase 2 (文書取込) 完了後 |
| Ingrid | ⚠ | CAPCOM / SC 両方未掲載 |

## M2 への引き継ぎ事項

- 必殺技・SA の CAPCOM ↔ SuperCombo マッピング
- ゲームシステム文書 (Gauges, Offense, Defense 等) のベクトル検索
- Ollama Embedding の pgvector 格納 (Phase 2)
- 他キャラの解説テキスト整備
- OllamaProvider のベクトル検索精度チューニング

## 失敗パターンログ

| パターン | 原因 | 対処済み |
|---|---|---|
| 波動拳/昇竜拳を通常技に誤マッピング | Gemma4 の学習バイアス | ✅ キーワード検出 + 事後除去 |
| punish_check でパニカンFを反撃Fと混同 | コンテキスト不足 | ✅ 反撃可否を自動計算して追記 |
| ゲーム概念の精度が低い | 文書データなし | Phase 2 で解決予定 |

## AWS EC2 でのリモート Ollama 利用

```bash
# .env の OLLAMA_BASE_URL を変更するだけで切り替え可能
OLLAMA_BASE_URL=http://<ec2-ip>:11434

# EC2 セットアップは ADR.md の ADR-013 を参照
```
