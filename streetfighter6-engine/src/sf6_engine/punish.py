"""Shared helpers for conservative punish-candidate presentation."""
from __future__ import annotations

import re
from typing import Any, Iterable


_GROUND_NEUTRAL_INPUT_RE = re.compile(
    r"^(?:[12456][LMH][PK]|[LMH][PK]|[12456](?:PP|KK)|LP\+LK|"
    r"[1-6]{2,6}[LMH]?[PK]{1,3})$",
    re.IGNORECASE,
)
_JUMP_INPUT_RE = re.compile(r"^(?:j\.|nj\.|[789](?:[LMH][PK]|PP|KK))", re.IGNORECASE)
_ATTACK_SECTIONS = {
    "通常技", "特殊技", "必殺技", "スーパーアーツ", "通常投げ", "ドライブ",
    "ground_normal", "command_normal", "special", "super", "throw", "drive",
}
_ATTACK_SECTIONS_CASEFOLD = {value.casefold() for value in _ATTACK_SECTIONS}


def _resource_requirement(row: dict[str, Any]) -> str | None:
    section = str(row.get("section") or "")
    move_input = str(row.get("sc_input_key") or row.get("input") or "")
    if "スーパーアーツ" in section or re.search(r"(?:236236|214214)", move_input):
        return "SAゲージ量の確認が必要"
    if re.search(r"(?:PP|KK)", move_input, re.IGNORECASE):
        return "ドライブゲージ量の確認が必要"
    return None


def filter_timing_candidates(
    rows: Iterable[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Remove moves that cannot start directly from a grounded neutral state.

    Returned moves are still timing candidates, not confirmed punishes: reach,
    pushback, resources, stance and character-state restrictions remain open.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        move_input = row.get("sc_input_key") or row.get("input")
        name = row.get("move_name") or ""
        if not move_input:
            continue
        input_text = str(move_input).strip()
        section = str(row.get("section") or "")
        if (
            "パリィ" in name
            or _JUMP_INPUT_RE.search(input_text)
            or "~" in input_text
            or input_text.startswith("-")
            or (section and section.casefold() not in _ATTACK_SECTIONS_CASEFOLD)
        ):
            continue
        startup = row.get("c_startup") if row.get("c_startup") is not None else row.get("startup")
        if not isinstance(startup, int):
            continue
        identity = (input_text.casefold(), str(name).casefold())
        if identity in seen:
            continue
        seen.add(identity)
        neutral_direct = bool(_GROUND_NEUTRAL_INPUT_RE.fullmatch(input_text))
        candidates.append({
            "move_name": name or None,
            "input": input_text,
            "startup": startup,
            "on_block": row.get("c_on_block") if "c_on_block" in row else row.get("on_block"),
            "section": row.get("section"),
            "timing_eligible": True,
            "neutral_availability_status": "direct" if neutral_direct else "unverified",
            "reach_status": "unverified",
            "resource_requirement": _resource_requirement(row),
            "confirmed_punish": None,
        })
        if len(candidates) >= limit:
            break
    return candidates
