"""
Logic Engine が扱うデータ構造の定義.

dict をそのまま回すのではなく dataclass を使うことで、
- 型ヒントによる補完・静的解析
- asdict() でJSON化が容易
- handlers 間の契約が明確
になる.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any


@dataclass
class Cancellable:
    """キャンセル可能性フラグの集合."""
    chain: bool = False
    sa1: bool = False
    sa2: bool = False
    sa3: bool = False


@dataclass
class MoveDetail:
    """正規化された単一技の詳細情報.

    move_normalized ビューの1行に対応.
    """
    character: str
    move_name: str
    section: str

    # 数値系 (None = 該当データなし、または非数値表記)
    startup: int | None = None
    active_first: int | None = None
    active_last: int | None = None
    recovery: int | None = None
    on_block: int | None = None
    on_block_is_range: bool = False
    on_hit: int | None = None
    on_hit_is_knockdown: bool = False
    on_hit_is_range: bool = False
    damage: int | None = None

    cancellable: Cancellable = field(default_factory=Cancellable)
    has_conditional_note: bool = False

    # 生データ (註釈が気になる時に参照できるよう保持)
    raw_startup: str | None = None
    raw_active: str | None = None
    raw_recovery: str | None = None
    raw_on_block: str | None = None
    raw_on_hit: str | None = None
    raw_cancel: str | None = None
    raw_damage: str | None = None
    raw_note: str | None = None

    patch_date: date | None = None

    @classmethod
    def from_view_row(cls, row: dict[str, Any]) -> "MoveDetail":
        """move_normalized ビューの行 (dict) から MoveDetail を生成."""
        return cls(
            character=row["character_slug"],
            move_name=row["move_name"],
            section=row["section"],
            startup=row.get("startup_int"),
            active_first=row.get("active_first"),
            active_last=row.get("active_last"),
            recovery=row.get("recovery_int"),
            on_block=row.get("on_block_int"),
            on_block_is_range=row.get("on_block_is_range", False),
            on_hit=row.get("on_hit_int"),
            on_hit_is_knockdown=row.get("on_hit_is_knockdown", False),
            on_hit_is_range=row.get("on_hit_is_range", False),
            damage=row.get("damage_int"),
            cancellable=Cancellable(
                chain=row.get("cancellable_chain", False),
                sa1=row.get("cancellable_sa1", False),
                sa2=row.get("cancellable_sa2", False),
                sa3=row.get("cancellable_sa3", False),
            ),
            has_conditional_note=row.get("has_conditional_note", False),
            raw_startup=row.get("raw_startup"),
            raw_active=row.get("raw_active"),
            raw_recovery=row.get("raw_recovery"),
            raw_on_block=row.get("raw_on_block"),
            raw_on_hit=row.get("raw_on_hit"),
            raw_cancel=row.get("raw_cancel"),
            raw_damage=row.get("raw_damage"),
            raw_note=row.get("raw_note"),
            patch_date=row.get("patch_date"),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON化可能なdictに変換 (date は ISO文字列に)."""
        d = asdict(self)
        if self.patch_date:
            d["patch_date"] = self.patch_date.isoformat() \
                if hasattr(self.patch_date, "isoformat") \
                else str(self.patch_date)
        return d


@dataclass
class LookupResult:
    """lookup_move の結果.

    found=False の場合は candidate_names に近そうな技名を入れてユーザー補助する.
    """
    found: bool
    move: MoveDetail | None = None
    character: str | None = None
    query_move_name: str | None = None
    candidate_names: list[str] = field(default_factory=list)
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "move": self.move.to_dict() if self.move else None,
            "character": self.character,
            "query_move_name": self.query_move_name,
            "candidate_names": self.candidate_names,
            "message": self.message,
        }
