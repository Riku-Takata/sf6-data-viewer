"""
Supabase DBへの接続とクエリ基盤.

Layer 2 は読み取り専用なので anon key で接続する (service_role は使わない).
RLSは Supabase 側で 'public read' ポリシーが設定されている前提.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client
from supabase.client import ClientOptions

# プロジェクトルートの .env を読み込む
# srcレイアウトのため、__file__ の祖父ディレクトリが project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Supabaseクライアントを取得 (プロセス内で1つだけ生成).

    Raises:
        RuntimeError: 環境変数が未設定の場合.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_ANON_KEY must be set. "
            "Create .env from .env.example."
        )
    options = ClientOptions(postgrest_client_timeout=30)
    return create_client(url, key, options=options)


def test_connection() -> dict:
    """接続テスト: characters テーブルから1件だけ取得.

    CLIから 'python -m sf6_engine.db' で呼ぶ想定.
    """
    sb = get_client()
    res = sb.table("characters").select("slug, display_name_ja").limit(1).execute()
    return {
        "connected": True,
        "sample_character": res.data[0] if res.data else None,
        "total_rows_preview": len(res.data),
    }


if __name__ == "__main__":
    import json
    try:
        result = test_connection()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Connection failed: {e}")
        raise