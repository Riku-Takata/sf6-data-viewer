"""SuperCombo Cargo API データを sc_moves テーブルへインポートするスクリプト.

対応する JSON 構造:
  {
    "fetched_at": "...",
    "data": {
      "Sagat": [
        {"chara": "Sagat", "moveId": "sagat_2hk", "input": "2HK", ...},
        ...
      ],
      "Ryu": [...],
      ...
    }
  }

Usage (CLI として直接実行):
  python -m sf6_engine.importers.supercombo path/to/supercombo_sf6_YYYY-MM-DD.json

Usage (ライブラリとして):
  from sf6_engine.importers.supercombo import import_supercombo
  import_supercombo("supercombo_sf6_2026-04-26.json")
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from sf6_engine.db import get_client, get_write_client
from sf6_engine.scripts.html_strip import strip_html

logger = logging.getLogger(__name__)

# MediaWiki 未解決テンプレートのプレースホルダー ({{{fieldname}}})
RE_MW_PLACEHOLDER = re.compile(r'^\{+[^{}]+\}+$')

# バッチインポートのサイズ (2290 技を一括送信せず分割)
BATCH_SIZE = 100


def _clean(value: Any) -> str | None:
    """フィールド値をクリーニングして文字列を返す.

    - None / 空文字 → None
    - {{{fieldname}}} プレースホルダー → None
    - それ以外 → strip_html() 済み文字列
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if RE_MW_PLACEHOLDER.match(s):
        return None
    return strip_html(s)


def _clean_notes(value: Any) -> tuple[str | None, str | None]:
    """notes フィールドを (notes_raw, notes_clean) で返す."""
    if value is None:
        return None, None
    s = str(value).strip()
    if not s or RE_MW_PLACEHOLDER.match(s):
        return None, None
    return s, strip_html(s)


def _to_row(move: dict[str, Any]) -> dict[str, Any]:
    """SuperCombo の 1技レコード (dict) を sc_moves の INSERT 用 dict に変換."""
    notes_raw, notes_clean = _clean_notes(move.get("notes"))

    return {
        "chara":          move.get("chara"),
        "move_id":        _clean(move.get("moveId")),
        "input":          _clean(move.get("input")),
        "name":           _clean(move.get("name")),
        "move_type":      _clean(move.get("moveType")),
        "guard":          _clean(move.get("guard")),
        "damage":         _clean(move.get("damage")),
        "startup":        _clean(move.get("startup")),
        "active":         _clean(move.get("active")),
        "recovery":       _clean(move.get("recovery")),
        "total":          _clean(move.get("total")),
        "cancel":         _clean(move.get("cancel")),
        "hit_adv":        _clean(move.get("hitAdv")),
        "block_adv":      _clean(move.get("blockAdv")),
        "punish_adv":     _clean(move.get("punishAdv")),
        "perf_parry_adv": _clean(move.get("perfParryAdv")),
        "dr_cancel_hit":  _clean(move.get("DRcancelHit")),
        "dr_cancel_blk":  _clean(move.get("DRcancelBlk")),
        "after_dr_hit":   _clean(move.get("afterDRHit")),
        "after_dr_blk":   _clean(move.get("afterDRBlk")),
        "hitstun":        _clean(move.get("hitstun")),
        "blockstun":      _clean(move.get("blockstun")),
        "hitstop":        _clean(move.get("hitstop")),
        "drive_dmg_blk":  _clean(move.get("driveDmgBlk")),
        "drive_dmg_hit":  _clean(move.get("driveDmgHit")),
        "drive_gain":     _clean(move.get("driveGain")),
        "super_gain_hit": _clean(move.get("superGainHit")),
        "super_gain_blk": _clean(move.get("superGainBlk")),
        "invuln":         _clean(move.get("invuln")),
        "armor":          _clean(move.get("armor")),
        "airborne":       _clean(move.get("airborne")),
        "jug_start":      _clean(move.get("jugStart")),
        "jug_increase":   _clean(move.get("jugIncrease")),
        "jug_limit":      _clean(move.get("jugLimit")),
        "proj_speed":     _clean(move.get("projSpeed")),
        "atk_range":      _clean(move.get("atkRange")),
        "notes_raw":      notes_raw,
        "notes":          notes_clean,
        "raw_json":       move,
    }


def import_supercombo(
    json_path: str | Path,
    *,
    dry_run: bool = False,
    character_filter: str | None = None,
) -> dict[str, int]:
    """SuperCombo JSON を sc_moves テーブルにインポートする.

    Args:
        json_path:        取得済み JSON ファイルのパス。
        dry_run:          True の場合、DB への書き込みを行わず検証のみ。
        character_filter: 指定があれば、そのキャラのみ処理 (例: 'Sagat')。

    Returns:
        {"total": N, "upserted": N, "skipped": N, "errors": N} の集計。
    """
    path = Path(json_path)
    logger.info(f"Loading {path} ...")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # JSON 構造: {"data": {"Sagat": [...], "Ryu": [...], ...}}
    moves_by_char: dict[str, list[dict]] = data.get("data", data)

    if character_filter:
        moves_by_char = {
            k: v for k, v in moves_by_char.items()
            if k == character_filter
        }
        if not moves_by_char:
            logger.warning(f"Character '{character_filter}' not found in JSON.")
            return {"total": 0, "upserted": 0, "skipped": 0, "errors": 0}

    # 全技をフラット化し、(chara, input) 単位で重複除去
    # SuperCombo JSON には同一 (chara, input) が複数行入るケースがある
    # (Drive Impact の共通技、SAのバリアントなど)
    # → 後から出現した行で上書き (後の行が詳細情報を持つ傾向があるため)
    seen: dict[tuple[str, str], dict] = {}
    skipped_dup = 0
    for char, moves in moves_by_char.items():
        for move in moves:
            row = _to_row(move)
            if not row["chara"] or not row["input"]:
                logger.warning(f"Skipping row with missing chara/input: {move}")
                continue
            key = (row["chara"], row["input"])
            if key in seen:
                skipped_dup += 1
            seen[key] = row

    all_rows = list(seen.values())
    if skipped_dup:
        logger.info(f"Deduplicated {skipped_dup} rows (same chara+input)")

    total = len(all_rows)
    logger.info(f"Prepared {total} rows for upsert (dry_run={dry_run})")

    if dry_run:
        # 最初の3行をサンプル表示
        for row in all_rows[:3]:
            logger.info(f"  [DRY] chara={row['chara']}, input={row['input']}, "
                        f"startup={row['startup']}, block_adv={row['block_adv']}")
        return {"total": total, "upserted": 0, "skipped": total, "errors": 0}

    sb = get_write_client()
    upserted = 0
    errors = 0

    # BATCH_SIZE 件ずつ upsert (タイムアウト防止)
    for batch_start in range(0, total, BATCH_SIZE):
        batch = all_rows[batch_start:batch_start + BATCH_SIZE]
        try:
            sb.table("sc_moves").upsert(
                batch,
                on_conflict="chara,input",
            ).execute()
            upserted += len(batch)
            logger.info(
                f"  Batch {batch_start // BATCH_SIZE + 1}: "
                f"upserted {batch_start + len(batch)}/{total}"
            )
        except Exception as e:
            logger.error(f"  Batch {batch_start // BATCH_SIZE + 1} failed: {e}")
            errors += len(batch)

    logger.info(f"Done. upserted={upserted}, errors={errors}")
    return {"total": total, "upserted": upserted, "skipped": 0, "errors": errors}


def _verify_import(character: str = "Sagat") -> None:
    """インポート結果をサンプルクエリで確認する."""
    sb = get_client()

    # 件数確認
    res = sb.table("sc_moves").select("id", count="exact").eq("chara", character).execute()
    print(f"\n{character} の sc_moves 件数: {res.count}")

    # サンプル: Sagat の 2HK
    sample = (
        sb.table("sc_moves")
        .select("input,name,startup,block_adv,hit_adv,atk_range")
        .eq("chara", character)
        .eq("input", "2HK")
        .limit(1)
        .execute()
    )
    if sample.data:
        print(f"\nSagat 2HK サンプル:")
        for k, v in sample.data[0].items():
            if v is not None:
                print(f"  {k}: {v}")
    else:
        print(f"\nSagat の 2HK が見つかりません (インポート未完了の可能性)")

    # 全体件数
    total = sb.table("sc_moves").select("id", count="exact").execute()
    print(f"\nsc_moves 総件数: {total.count}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="SuperCombo JSON を Supabase の sc_moves テーブルへインポートする"
    )
    parser.add_argument("json_path", help="supercombo_sf6_YYYY-MM-DD.json のパス")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DB への書き込みを行わず、変換結果の確認のみ行う"
    )
    parser.add_argument(
        "--character", default=None,
        help="指定したキャラのみ処理 (例: Sagat)"
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="インポート後に検証クエリを実行する"
    )
    args = parser.parse_args()

    result = import_supercombo(
        args.json_path,
        dry_run=args.dry_run,
        character_filter=args.character,
    )
    print(f"\n結果: {result}")

    if args.verify and not args.dry_run:
        char = args.character or "Sagat"
        _verify_import(char)
