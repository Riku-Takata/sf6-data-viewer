"""Ultimate Frame Data (UFD) の補完データを取得・整形する共通ヘルパー。"""
from __future__ import annotations

import logging
from typing import Any

from sf6_engine.db import get_client

logger = logging.getLogger(__name__)

_UFD_SELECT = (
    "category,move_name,sc_input,input_sequence,startup,total,damage,"
    "attack_type,cancellable,notes,hitbox_note,on_hit,on_block,active,recovery,"
    "hitbox_source_url,hitbox_storage_path,source_url,scraped_at"
)


def fetch_ufd_details(
    character_slug: str,
    *,
    sc_input: str | None = None,
    move_name: str | None = None,
) -> dict[str, Any] | None:
    """UFDの補完行を取得する。input完全一致を優先し、技名でフォールバックする。"""
    try:
        query = get_client().table("ufd_moves").select(_UFD_SELECT).eq(
            "character_slug", character_slug
        )
        if sc_input:
            result = query.eq("sc_input", sc_input).limit(1).execute()
            if result.data:
                return result.data[0]
        if move_name:
            result = (
                get_client().table("ufd_moves").select(_UFD_SELECT)
                .eq("character_slug", character_slug)
                .ilike("move_name", f"%{move_name}%").limit(1).execute()
            )
            if result.data:
                return result.data[0]
    except Exception as exc:  # migration未適用時も既存回答は継続する
        logger.debug("UFD details unavailable: %s", exc)
    return None


def format_ufd_details(row: dict[str, Any]) -> str:
    """UFD補完行を、回答モデルに渡す短い参照ブロックへ変換する。"""
    lines = ["【Ultimate Frame Data 実測補足】"]
    category = row.get("category")
    if category:
        lines.append(f"分類: {category}")
    for label, key in (
        ("発生", "startup"), ("持続", "active"), ("硬直", "recovery"),
        ("全体", "total"), ("ダメージ", "damage"), ("攻撃属性", "attack_type"),
        ("キャンセル", "cancellable"), ("ヒット時", "on_hit"),
        ("ガード時", "on_block"), ("コマンド", "input_sequence"),
    ):
        value = row.get(key)
        if value:
            lines.append(f"{label}: {value}")
    if row.get("notes"):
        lines.append(f"UFDメモ: {row['notes'][:500]}")
    if row.get("hitbox_note"):
        lines.append(f"当たり判定メモ: {row['hitbox_note']}")
    if row.get("hitbox_source_url"):
        lines.append(f"当たり判定GIF (UFD): {row['hitbox_source_url']}")
    lines.append("※ UFDは実測データ。公式/SuperComboと差がある場合は出所を明記して扱うこと。")
    return "\n".join(lines)
