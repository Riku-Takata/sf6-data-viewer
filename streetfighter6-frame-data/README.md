# SF6 Frame Data Scraper - Layer 1

CAPCOM公式の Street Fighter 6 フレームデータを定期収集し、Supabase に履歴付きで保存する。

## アーキテクチャ

```
EventBridge (毎日 18:00 UTC)
    │
    ▼
Lambda: sf6-frame-scraper
    │
    ├── ① 検知: battle_change ページから最新パッチ日付を取得
    │       └── 既知ならここで終了 (実行 < 1秒)
    │
    └── ② 新パッチを検知 → 全30キャラのframeページをスクレイプ
            ├── Supabase Storage (current ↔ previous ローテーション)
            └── PostgreSQL (characters / patches / moves / move_snapshots)
```

## セットアップ手順

### 1. Supabase 側

1. プロジェクト作成済みであること
2. SQL Editor で `sf6_schema.sql` を実行
3. Storage で `sf6-html-archive` という名前のバケットを作成 (Private で良い)
4. `Project Settings → API` から以下を控える:
    - Project URL (例: `https://xxxx.supabase.co`)
    - service_role key (`anon` ではなく `service_role` の方)

### 2. ローカル動作確認 (推奨)

デプロイ前にローカルで1回流して、Supabase に正しく書き込めるか確認する。

```bash
# 仮想環境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 環境変数
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_KEY="eyJ..."         # service_role key
export SUPABASE_BUCKET="sf6-html-archive"

# 実行
python lambda_function.py
```

期待される出力:

- 初回: `{"status": "success", "patch_date": "2026-04-15", "characters_scraped": 30}`
- 2回目以降 (パッチ日が変わっていない): `{"status": "no_change", ...}`

実行後、Supabase Studio で確認:
- `characters` テーブルに30行
- `patches` テーブルに1行 (最新の Updated 日付)
- `moves` テーブルに約2,000行 (キャラ平均60-70技 × 30キャラ)
- `move_snapshots` テーブルに約2,000行
- Storage `sf6-html-archive/current/` に約30個の .html

### 3. AWS デプロイ (SAM CLI 使用)

```bash
# AWS CLI と SAM CLI を事前にインストール・設定
# https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html

# 初回ビルド & デプロイ
sam build
sam deploy --guided \
  --parameter-overrides \
    SupabaseUrl=https://xxxx.supabase.co \
    SupabaseServiceKey=eyJ... \
    SupabaseBucket=sf6-html-archive

# 2回目以降
sam deploy
```

`--guided` 実行時の選択:
- Stack Name: `sf6-frame-scraper`
- Region: `ap-northeast-1` (東京)
- Confirm changes before deploy: `Y`
- Allow IAM role creation: `Y`
- Save arguments to samconfig.toml: `Y`

### 4. 動作確認

```bash
# 手動でLambdaを起動して動作確認
aws lambda invoke --function-name sf6-frame-scraper /tmp/out.json
cat /tmp/out.json

# CloudWatch Logsで実行ログを確認
aws logs tail /aws/lambda/sf6-frame-scraper --follow
```

## 動作モード別の特徴

| モード | 実行時間 | コスト目安 (月額) | データ書き込み |
|---|---|---|---|
| 検知のみ (no_change) | < 1秒 | ほぼ$0 (無料枠内) | scrape_runs に1行 |
| パッチ検知時 (success) | 約2-3分 | 1回あたり数セント | 全テーブル更新 |

CAPCOM側のパッチ頻度は月1〜2か月に1回なので、月29-30回は no_change で終わる前提。

## セキュリティ上の注意

- `SUPABASE_SERVICE_KEY` は **絶対に Git にコミットしない**
- 本番運用では SAM のパラメータ渡しではなく **AWS Secrets Manager** に格納し、Lambdaから取得するのが望ましい (本テンプレートでは簡略化のため環境変数で渡している)
- Supabase の Storage バケットは **Public にしない** (RLSでservice_roleのみ書き込み可)

## トラブルシューティング

### `parse_frame_page()` が技を0件しか返さない
CAPCOMのHTML構造が変わった可能性。`current/{slug}.html` を Storage からダウンロードし、ローカルで `parse_frame_page()` をデバッグ。CSSクラスの prefix 名 (`frame_startup_frame` 等) が変わっていないか確認。

### Lambda がタイムアウト
`Timeout: 600` を `template.yaml` で延長しているが、それでも足りない場合はキャラ数増加が原因。`MemorySize` を上げると並列処理パワーも上がる (現状は逐次)。

### 同じパッチ日で繰り返し全件スクレイプされる
`patches.capcom_updated_date` が UNIQUE 制約付きで重複時は upsert される設計だが、`get_known_patch_dates()` の戻り値に該当日付が含まれていない場合に発生。Supabase Studio で `patches` テーブルを直接確認。

## 次のステップ (Layer 2 への布石)

- 表示名 (`display_name_ja`, `display_name_en`) を手動で characters テーブルに更新
- `move_diff_recent` ビューで「直近パッチで何が変わったか」が一覧できることを確認
- Web UI 構築開始
