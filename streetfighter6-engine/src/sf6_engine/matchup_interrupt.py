"""Broad, data-driven matchup interruption analysis.

Unlike a specified two-move question, a wording such as "主な技" cannot be
resolved from frame data or assumed from usage.  This service therefore scans
every scalar, standard normal-to-special cancel for the named attacker and
shows only sequences the named defender can interrupt on timing.
"""
from __future__ import annotations

from typing import Any, Iterable

from sf6_engine.sequence_analysis import (
    MoveInteractionProfile,
    enumerate_special_cancel_timelines,
    list_ground_normal_profiles,
)

_DISPLAY_LIMIT = 8
_CANDIDATE_DISPLAY_LIMIT = 3


def _profile_label(profile: MoveInteractionProfile) -> str:
    return f"{profile.name or profile.input}（{profile.input}、発生{profile.startup_f}F）"


def _candidate_profiles(
    profiles: Iterable[MoveInteractionProfile],
    gap_f: int,
) -> list[MoveInteractionProfile]:
    """Return only techniques that beat, rather than tie, the second attack."""
    return [profile for profile in profiles if int(profile.startup_f or 999) < gap_f]


def _sequence_label(item: dict[str, Any]) -> str:
    opener = item["opener"]
    followup = item["followup"]
    return f"{opener.input}→{followup.input}"


def _candidate_payload(profiles: Iterable[MoveInteractionProfile]) -> list[dict[str, Any]]:
    return [
        {"input": profile.input, "name": profile.name, "startup_f": profile.startup_f}
        for profile in profiles
    ]


def _format_candidates(candidates: list[dict[str, Any]]) -> str:
    visible = candidates[:_CANDIDATE_DISPLAY_LIMIT]
    text = "、".join(
        f"{candidate['name'] or candidate['input']}（{candidate['input']}、発生{candidate['startup_f']}F）"
        for candidate in visible
    )
    if len(candidates) > len(visible):
        text += f"、ほか{len(candidates) - len(visible)}技"
    return text


def analyze_matchup_interrupt_overview(
    attacker: str,
    defender: str,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Find defender ground normals that interrupt any standard special cancel.

    The calculation remains timing-only.  It finds the complete numeric cancel
    candidate set for whichever attacker is requested, so no character-specific
    pressure list or LLM-generated move name is needed.
    """
    defender_profiles = list_ground_normal_profiles(defender, client=client)
    if not defender_profiles:
        return {
            "found": False,
            "status": "defender_normals_unavailable",
            "attacker": attacker,
            "defender": defender,
            "message": f"{defender} の地上通常技の発生データを取得できませんでした。",
        }

    timelines = enumerate_special_cancel_timelines(attacker, client=client)
    results: list[dict[str, Any]] = []
    unresolved_count = 0
    for item in timelines:
        gap_f = (item.get("timeline") or {}).get("actionable_gap_f")
        if not isinstance(gap_f, int):
            unresolved_count += 1
            continue
        candidates = _candidate_profiles(defender_profiles, gap_f) if gap_f > 0 else []
        if not candidates:
            continue
        results.append({
            **item,
            "gap_f": gap_f,
            "timing_candidates": _candidate_payload(candidates),
        })

    # The smallest viable gap is the most useful threshold to know first.
    # Stable inputs make ties deterministic without pretending to know usage.
    results.sort(key=lambda item: (
        int(item["gap_f"]),
        item["opener"].input,
        item["followup"].input,
    ))
    shown = results[:_DISPLAY_LIMIT]
    fastest = defender_profiles[0]
    attacker_label = next((item["opener"].character for item in timelines if item["opener"].character), attacker)
    defender_label = fastest.character or defender
    if shown:
        conclusion = (
            f"はい。{attacker_label}の通常技→通常版必殺技キャンセルのうち、"
            f"{defender_label}の地上通常技でフレーム上割り込める連携は{len(results)}件あります。"
        )
    else:
        conclusion = (
            f"いいえ。数値化できた{attacker_label}の通常技→通常版必殺技キャンセルでは、"
            f"{defender_label}の最速地上通常技（{_profile_label(fastest)}）が間に合う連携はありません。"
        )

    lines = [
        conclusion,
        "前提: 使用率としての『主な技』は推測せず、数値化できる通常技→通常版必殺技キャンセルを全件比較しています。",
    ]
    for item in shown:
        lines.append(
            f"- {_sequence_label(item)}: 隙間{item['gap_f']}F。"
            f"{_format_candidates(item['timing_candidates'])}がフレーム上の割り込み候補です。"
        )
    if len(results) > len(shown):
        lines.append(f"- …ほか{len(results) - len(shown)}件（具体的な連携を指定すると詳細解析できます）。")
    if not timelines:
        lines.append("- 数値化できる通常技→通常版必殺技キャンセルを取得できませんでした。")
    if unresolved_count:
        lines.append(f"- {unresolved_count}件は単一フレーム値を確定できないため除外しました。")
    lines.append(
        "※地上通常技だけを比較した時間上の候補です。距離・pushback・姿勢・無敵、"
        "OD/必殺技は個別連携を指定して確認してください。"
    )
    return {
        "found": True,
        "status": "resolved",
        "attacker": attacker,
        "defender": defender,
        "selection_scope": "all_scalar_ground_normal_to_standard_special_cancels",
        "defender_scope": "ground_normal",
        "scanned_pair_count": len(timelines),
        "interruptible_count": len(results),
        "sequences": shown,
        "summary": "\n".join(lines),
    }
