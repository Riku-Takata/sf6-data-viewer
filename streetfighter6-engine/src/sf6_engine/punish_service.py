"""Single deterministic punish-analysis service used by MCP and Discord."""
from __future__ import annotations

from typing import Any

from sf6_engine.db import get_client
from sf6_engine.frame_data import lookup_frame_data
from sf6_engine.punish import filter_timing_candidates


def _display_move(query: str, resolved: str) -> str:
    return resolved if query == resolved else f"{query}（{resolved}）"


def check_punish_data(
    character: str,
    move_name: str,
    punisher: str | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return timing evidence separately from end-to-end punish certainty."""
    lookup = lookup_frame_data(character, move_name, scenario=scenario)
    if not lookup.get("found") or not lookup.get("move"):
        return {
            "found": False,
            "message": lookup.get("message"),
            "resolution": lookup.get("resolution"),
            "candidate_names": lookup.get("candidate_names") or [],
        }

    move = lookup["move"]
    resolved_name = move.get("move_name") or move_name
    display_name = _display_move(move_name, resolved_name)
    profile = move.get("frame_profile") or {}
    resolution = profile.get("resolution") or lookup.get("resolution") or {}
    evaluation = profile.get("scenario_evaluation") or {}
    contextual_block = (
        evaluation.get("block_perspectives", {}).get("attacker") or {}
    )
    contextual_perspectives = evaluation.get("block_perspectives") or {}
    assessment = evaluation.get("punish_assessment") or {}

    common: dict[str, Any] = {
        "found": True,
        "character": character,
        "queried_move": move_name,
        "move_name": resolved_name,
        "resolution": resolution,
        "scenario": evaluation.get("scenario") or scenario or {},
        "block_perspectives": contextual_perspectives,
        "reference_block_perspectives": profile.get("block_perspectives"),
        "frame_profile": profile,
        "scenario_evaluation": evaluation,
        "punisher": punisher,
        "punisher_options": [],
    }

    if resolution.get("status") != "resolved":
        return {
            **common,
            "block_adv": None,
            "frame_punishable": None,
            "punishable": None,
            "confirmed_punishable": None,
            "summary": (
                f"'{move_name}' は技を一意に特定できません。"
                f"{resolution.get('clarification') or '正式名またはコマンドを指定してください。'}"
            ),
            "candidate_names": lookup.get("candidate_names") or [],
        }

    block_adv = (
        contextual_block.get("value")
        if contextual_block.get("usable_for_calculation") else None
    )
    if not isinstance(block_adv, int):
        required_data = contextual_block.get("required_data") or []
        missing = f" 不足データ: {' / '.join(required_data)}。" if required_data else ""
        return {
            **common,
            "block_adv": None,
            "block_adv_is_range": contextual_block.get("status") in {
                "interval", "derived_interval",
            },
            "frame_punishable": None,
            "punishable": None,
            "confirmed_punishable": None,
            "punish_status": assessment.get("status"),
            "summary": (
                f"{display_name} の条件適用後ガード時硬直差は "
                f"{contextual_block.get('display') or '算出不可'}。"
                f"今回の条件で単一値を確定できないため反撃判定を保留します。{missing}"
            ),
        }

    window = assessment.get("punish_window_f")
    frame_punishable = assessment.get("frame_punishable") is True
    confirmed = assessment.get("confirmed_punishable")
    if frame_punishable:
        summary = (
            f"{display_name} は今回の条件でガード時 {block_adv:+d}F。"
            f"ガードした側は +{window}F なので、発生 {window}F 以内がフレーム上の候補です。"
            "ただし、ガード後距離・押し戻し・反撃技の到達を検証するデータがないため、"
            "確定反撃としての成立は未確定です。"
        )
    else:
        summary = (
            f"{display_name} は今回の条件でガード時 {block_adv:+d}F。"
            "防御側に反撃可能なフレーム窓はありません。"
        )

    out = {
        **common,
        "block_adv": block_adv,
        "reference_block_adv": move.get("on_block"),
        "block_adv_is_range": False,
        "block_adv_source": (
            ((profile.get("facts") or {}).get("on_block") or {}).get("source_label")
        ),
        "punish_window_f": window,
        "frame_punishable": frame_punishable,
        "punishable": confirmed,
        "confirmed_punishable": confirmed,
        "punish_status": assessment.get("status"),
        "summary": summary,
    }

    if punisher and frame_punishable and isinstance(window, int):
        rows = (
            get_client()
            .table("unified_moves")
            .select("move_name,sc_input_key,c_startup,c_on_block,section")
            .eq("character_slug", punisher.lower())
            .lte("c_startup", window)
            .not_.is_("c_startup", "null")
            .not_.ilike("move_name", "%パリィ%")
            .order("c_startup")
            .limit(80)
            .execute()
            .data
            or []
        )
        out["punisher_options"] = filter_timing_candidates(rows, limit=8)
        out["candidate_kind"] = "timing_only"
    return out

