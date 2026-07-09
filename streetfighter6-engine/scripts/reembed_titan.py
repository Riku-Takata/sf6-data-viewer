"""doc_chunks を Bedrock Titan V2 で再埋め込みし embedding_titan を埋める (ADR-017).

埋め込み対象テキストは元のインポータ (importers/docs.py) と同じ構築方法
(heading_h2 [/ heading_h3] + content 先頭500文字) を再現する。

前提:
  1. sql/doc_chunks_titan_migration.sql の STEP 1〜2 を Supabase Studio で適用済み
  2. AWS 認証情報に bedrock:InvokeModel (Titan V2) 権限あり
  3. .env に SUPABASE_SERVICE_KEY (書き込み用) が設定済み

実行:
  PYTHONPATH=src python scripts/reembed_titan.py            # 本実行
  PYTHONPATH=src python scripts/reembed_titan.py --dry-run  # Bedrock を呼ばず対象を表示
"""
from __future__ import annotations

import argparse
import sys

from sf6_engine.db import get_client, get_write_client


def _embed_input(row: dict) -> str:
    """importers/docs.py と同じ埋め込み入力テキストを構築する。"""
    text = row.get("heading_h2") or ""
    if row.get("heading_h3"):
        text += f" / {row['heading_h3']}"
    text += f"\n\n{(row.get('content') or '')[:500]}"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="doc_chunks を Bedrock Titan V2 で再埋め込みする"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Bedrock を呼ばず、対象チャンクと入力テキストだけ表示")
    args = parser.parse_args()

    # 読み取りは anon、書き込みは service_role
    rows = (
        get_client().table("doc_chunks")
        .select("id,heading_h2,heading_h3,content")
        .execute()
        .data
    )
    print(f"対象チャンク: {len(rows)} 件")

    if args.dry_run:
        for r in rows[:3]:
            print(f"\n--- {r['id']} ---")
            print(_embed_input(r)[:200])
        print(f"\n[dry-run] Bedrock 未呼び出し。本実行は --dry-run を外す。")
        return 0

    from sf6_engine.bedrock_embed import embed_text, EMBED_DIM

    sb = get_write_client()
    ok = err = 0
    for i, row in enumerate(rows, 1):
        try:
            vec = embed_text(_embed_input(row))
            sb.table("doc_chunks").update(
                {"embedding_titan": vec}
            ).eq("id", row["id"]).execute()
            ok += 1
            print(f"  [{i:03d}/{len(rows)}] OK   {row['id']} (dim={len(vec)})")
        except Exception as e:  # 1件失敗しても続行し、最後に集計
            err += 1
            print(f"  [{i:03d}/{len(rows)}] ERR  {row['id']}: {type(e).__name__}: {str(e)[:160]}")

    print(f"\n完了: ok={ok}, err={err}, dim={EMBED_DIM}")
    print("→ 全件成功後、sql/doc_chunks_titan_migration.sql の STEP 3 (索引) を実行推奨。")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
