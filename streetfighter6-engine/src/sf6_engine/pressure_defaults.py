"""Patch-reviewable defaults for otherwise underspecified pressure questions."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


_DEFAULTS_PATH = Path(__file__).resolve().parent / "data" / "pressure_family_defaults.json"


def compact_pressure_form(value: str) -> str:
    """Normalize a move-family form without turning it into a fuzzy match."""
    return re.sub(r"[\s_\-]+", "", value).casefold()


@lru_cache(maxsize=1)
def reviewed_pressure_defaults() -> tuple[dict[str, Any], ...]:
    """Read reviewed defaults packaged with the current engine version."""
    try:
        raw = json.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    return tuple(row for row in raw if isinstance(row, dict))


def resolve_reviewed_pressure_default(
    character: str,
    family_form: str,
) -> dict[str, Any] | None:
    """Return an exact reviewed family default, never a fuzzy name guess."""
    character_key = character.casefold()
    family_key = compact_pressure_form(family_form)
    for row in reviewed_pressure_defaults():
        if str(row.get("character") or "").casefold() != character_key:
            continue
        forms = row.get("family_forms") or []
        if any(compact_pressure_form(str(form)) == family_key for form in forms):
            return row
    return None
