"""
Supabase DBへの接続とクエリ基盤.

- get_client()       : anon key で接続 (読み取り専用、RLS public read が前提)
- get_write_client() : service_role key で接続 (インポーター等の書き込み用)
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client
from supabase.client import ClientOptions

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


@lru_cache(maxsize=1)
def get_client() -> Client:
    """読み取り用 Supabase クライアント (anon key)."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env"
        )
    options = ClientOptions(postgrest_client_timeout=30)
    return create_client(url, key, options=options)


@lru_cache(maxsize=1)
def get_write_client() -> Client:
    """書き込み用 Supabase クライアント (service_role key).

    インポータや DDL 実行など、RLS を bypass する操作に使う。
    .env に SUPABASE_SERVICE_KEY が必要。
    Supabase Dashboard → Settings → API → service_role (secret) から取得。
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_KEY が .env に設定されていません。\n"
            "Supabase Dashboard → Settings → API → service_role から取得して設定してください。"
        )
    options = ClientOptions(postgrest_client_timeout=60)
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