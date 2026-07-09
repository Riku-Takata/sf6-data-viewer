"""special_move_map シードデータ投入。

match_specials.py が生成した scripts/out/special_move_map_seed.json を
special_move_map テーブルへ UPSERT する (service_role key 使用)。

前提: sql/special_move_map_migration.sql が Supabase Studio で適用済み。

使い方:
  PYTHONPATH=src ./.venv312/bin/python scripts/load_special_move_map.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sf6_engine.db import get_write_client

SEED_PATH = Path(__file__).parent / "out" / "special_move_map_seed.json"
BATCH = 200


def main() -> None:
    dry_run = '--dry-run' in sys.argv
    rows = json.loads(SEED_PATH.read_text())
    print(f"seed: {len(rows)} 件 ({SEED_PATH})")
    if dry_run:
        for r in rows[:5]:
            print("  sample:", r)
        print("dry-run のため投入せず終了")
        return

    sb = get_write_client()
    total = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        sb.table('special_move_map').upsert(
            batch, on_conflict='capcom_slug,capcom_move_name'
        ).execute()
        total += len(batch)
        print(f"  upserted {total}/{len(rows)}")

    check = sb.table('special_move_map').select('id', count='exact').execute()
    print(f"完了: special_move_map 総件数 = {check.count}")


if __name__ == '__main__':
    main()
