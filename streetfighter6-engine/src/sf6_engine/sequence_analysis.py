"""Deterministic analysis for multi-move pressure and trade scenarios.

Frame-table rows describe one move in isolation.  A pressure question such as
``5MP -> 5MP versus a reversal 4f normal`` needs a shared timeline, both move
profiles, an interaction result, and the state after that interaction.  This
module performs that orchestration without asking an LLM to calculate frames.

The first implementation intentionally models a narrow, evidence-backed slice:

* two attacker moves with explicit earliest or delayed timing;
* the first move resolves on hit or block;
* the defender performs a strike with a known startup and delay;
* same-frame direct strikes may trade if both attacks reach;
* post-trade advantage is taken from a reviewed observation when available,
  otherwise a clearly labelled hitstun model is returned.

Spatial confirmation remains observation-backed.  A timing tie never proves a
trade by itself.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sf6_engine.db import get_client
from sf6_engine.frame_data import lookup_frame_data


logger = logging.getLogger(__name__)

SEQUENCE_SCHEMA_VERSION = 2
TRADE_MODEL_VERSION = "simultaneous_direct_strike_v1"
_DATA_PATH = Path(__file__).resolve().parent / "data" / "sequence_observations.json"
_SC_MOVE_COLUMNS = (
    "chara,input,name,move_type,startup,active,recovery,hit_adv,block_adv,"
    "hitstun,blockstun,hitstop,atk_range,notes"
)
_GROUND_NEUTRAL_INPUT = re.compile(r"^[1-9][LMH][PK]$")


@dataclass(frozen=True)
class MoveInteractionProfile:
    """Scalar fields needed by the sequence evaluator."""

    character: str
    input: str
    name: str | None
    move_type: str | None
    startup_f: int | None
    active_f: int | None
    recovery_f: int | None
    on_block_f: int | None
    on_hit_f: int | None
    hitstun_f: int | None
    blockstun_f: int | None
    hitstop_f: int | None
    atk_range: float | None
    notes: str | None
    frame_sources: dict[str, str] | None = None
    supplemental_sources: dict[str, str] | None = None


def _strict_int(value: Any) -> int | None:
    """Parse a scalar frame value without flattening ranges or conditions."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        return None
    match = re.fullmatch(r"\s*([+-]?\d+)\s*F?\s*", str(value))
    return int(match.group(1)) if match else None


def _strict_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is None:
        return None
    match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*", str(value))
    return float(match.group(1)) if match else None


def _profile_from_row(row: Mapping[str, Any]) -> MoveInteractionProfile:
    return MoveInteractionProfile(
        character=str(row.get("chara") or ""),
        input=str(row.get("input") or ""),
        name=str(row["name"]) if row.get("name") else None,
        move_type=str(row["move_type"]) if row.get("move_type") else None,
        startup_f=_strict_int(row.get("startup")),
        active_f=_strict_int(row.get("active")),
        recovery_f=_strict_int(row.get("recovery")),
        on_block_f=_strict_int(row.get("block_adv")),
        on_hit_f=_strict_int(row.get("hit_adv")),
        hitstun_f=_strict_int(row.get("hitstun")),
        blockstun_f=_strict_int(row.get("blockstun")),
        hitstop_f=_strict_int(row.get("hitstop")),
        atk_range=_strict_float(row.get("atk_range")),
        notes=str(row["notes"]) if row.get("notes") else None,
        frame_sources=None,
        supplemental_sources={
            field: "SuperCombo"
            for field, key in (
                ("hitstun", "hitstun"),
                ("blockstun", "blockstun"),
                ("hitstop", "hitstop"),
                ("atk_range", "atk_range"),
                ("notes", "notes"),
            )
            if row.get(key) not in (None, "", "-")
        },
    )


def _apply_integrated_frame_profile(
    base: MoveInteractionProfile,
    resolved: Mapping[str, Any],
) -> MoveInteractionProfile:
    """Overlay CAPCOM/UFD/SC adopted frame facts on SC supplemental data."""
    if not resolved.get("found"):
        return base
    resolution = resolved.get("resolution") or {}
    if not resolution.get("usable_for_calculation"):
        return base
    move = resolved.get("move") or {}
    profile = move.get("frame_profile") or {}
    facts = profile.get("facts") or {}

    def adopted(field: str, fallback: int | None) -> int | None:
        if field not in facts:
            return fallback
        return _strict_int(move.get(field))

    frame_sources = {
        field: str(fact.get("source_label") or fact.get("source"))
        for field, fact in facts.items()
        if field in {"startup", "active", "recovery", "on_block", "on_hit"}
        and (fact.get("source_label") or fact.get("source"))
    }
    return MoveInteractionProfile(
        character=base.character,
        input=str(move.get("input") or base.input),
        name=str(move.get("move_name") or base.name or "") or None,
        move_type=str(move.get("move_type") or base.move_type or "") or None,
        startup_f=adopted("startup", base.startup_f),
        active_f=adopted("active", base.active_f),
        recovery_f=adopted("recovery", base.recovery_f),
        on_block_f=adopted("on_block", base.on_block_f),
        on_hit_f=adopted("on_hit", base.on_hit_f),
        hitstun_f=base.hitstun_f,
        blockstun_f=base.blockstun_f,
        hitstop_f=base.hitstop_f,
        atk_range=base.atk_range,
        notes=base.notes,
        frame_sources=frame_sources,
        supplemental_sources=base.supplemental_sources,
    )


def make_sequence_key(
    character_slug: str,
    attacker_inputs: Sequence[str],
    initial_interaction: str,
    defender_startup_f: int | None,
    outcome: str | None,
    attacker_delay_f: int | None = 0,
    defender_delay_f: int | None = 0,
    defender_character_slug: str | None = None,
    defender_move_input: str | None = None,
) -> str:
    """Build the stable key shared by bundled and database observations."""
    startup = "unknown" if defender_startup_f is None else str(defender_startup_f)
    defender_character = (
        defender_character_slug.casefold() if defender_character_slug else "any"
    )
    defender_move = defender_move_input or "any"
    return (
        f"{character_slug.casefold()}|{'>'.join(attacker_inputs)}|"
        f"after:{initial_interaction}|atk-delay:{attacker_delay_f}|"
        f"def:startup:{startup}|def-char:{defender_character}|"
        f"def-move:{defender_move}|def-delay:{defender_delay_f}|"
        f"outcome:{outcome or 'any'}"
    )


@lru_cache(maxsize=1)
def load_bundled_sequence_observations() -> tuple[dict[str, Any], ...]:
    """Load reviewed bootstrap observations shipped with the engine."""
    if not _DATA_PATH.exists():
        return ()
    payload = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    rows = payload.get("observations", payload)
    if not isinstance(rows, list):
        raise ValueError("sequence_observations.json must contain a list")
    return tuple(dict(row) for row in rows if isinstance(row, Mapping))


def _resolve_sc_character(character: str, client: Any) -> tuple[str, str]:
    """Return ``(capcom_slug, SuperCombo name)`` for either identifier form."""
    raw = character.strip()
    result = (
        client.table("char_slug_map")
        .select("capcom_slug,sc_chara")
        .eq("capcom_slug", raw.casefold())
        .limit(1)
        .execute()
    )
    if result.data:
        row = result.data[0]
        return str(row["capcom_slug"]), str(row["sc_chara"])
    result = (
        client.table("char_slug_map")
        .select("capcom_slug,sc_chara")
        .ilike("sc_chara", raw)
        .limit(1)
        .execute()
    )
    if result.data:
        row = result.data[0]
        return str(row["capcom_slug"]), str(row["sc_chara"])
    return raw.casefold(), raw


def _fetch_move_profile(
    character_slug: str,
    sc_character: str,
    identifier: str,
    client: Any,
    *,
    frame_client: Any | None,
) -> MoveInteractionProfile | None:
    resolved = lookup_frame_data(
        character_slug,
        identifier,
        client=frame_client,
    )
    if resolved.get("found") and not (
        resolved.get("resolution") or {}
    ).get("usable_for_calculation"):
        return None
    resolved_move = resolved.get("move") or {}
    supplemental_identifier = str(resolved_move.get("input") or identifier)
    rows = (
        client.table("sc_moves")
        .select(_SC_MOVE_COLUMNS)
        .eq("chara", sc_character)
        .ilike("input", supplemental_identifier)
        .limit(2)
        .execute()
        .data
        or []
    )
    if not rows:
        rows = (
            client.table("sc_moves")
            .select(_SC_MOVE_COLUMNS)
            .eq("chara", sc_character)
            .ilike("name", f"%{identifier}%")
            .limit(10)
            .execute()
            .data
            or []
        )
    if rows:
        exact = [
            row for row in rows
            if str(row.get("input") or "").casefold() == supplemental_identifier.casefold()
            or str(row.get("name") or "").casefold() == identifier.casefold()
        ]
        base = _profile_from_row((exact or rows)[0])
    elif resolved.get("found"):
        base = MoveInteractionProfile(
            character=sc_character,
            input=supplemental_identifier,
            name=str(resolved_move.get("move_name") or identifier),
            move_type=str(resolved_move.get("move_type") or "") or None,
            startup_f=None,
            active_f=None,
            recovery_f=None,
            on_block_f=None,
            on_hit_f=None,
            hitstun_f=None,
            blockstun_f=None,
            hitstop_f=None,
            atk_range=None,
            notes=None,
        )
    else:
        return None
    return _apply_integrated_frame_profile(base, resolved)


def _fetch_defender_profiles(
    startup_f: int,
    client: Any,
    *,
    sc_character: str | None = None,
) -> list[MoveInteractionProfile]:
    """Fetch neutral ground normals matching an explicitly requested startup."""
    query = (
        client.table("sc_moves")
        .select(_SC_MOVE_COLUMNS)
        .eq("move_type", "ground_normal")
        .eq("startup", str(startup_f))
    )
    if sc_character:
        query = query.eq("chara", sc_character)
    rows = query.limit(500).execute().data or []
    profiles = [
        _profile_from_row(row) for row in rows
        if _GROUND_NEUTRAL_INPUT.fullmatch(str(row.get("input") or ""))
    ]
    unique: dict[tuple[str, str], MoveInteractionProfile] = {}
    for profile in profiles:
        unique[(profile.character, profile.input)] = profile
    return list(unique.values())


def _fetch_followup_profiles(
    character_slug: str,
    sc_character: str,
    client: Any,
    *,
    frame_client: Any | None,
) -> list[MoveInteractionProfile]:
    rows = (
        client.table("sc_moves")
        .select(_SC_MOVE_COLUMNS)
        .eq("chara", sc_character)
        .eq("move_type", "ground_normal")
        .limit(300)
        .execute()
        .data
        or []
    )
    profiles = [
        _profile_from_row(row) for row in rows
        if _GROUND_NEUTRAL_INPUT.fullmatch(str(row.get("input") or ""))
    ]
    return [
        _apply_integrated_frame_profile(
            profile,
            lookup_frame_data(
                character_slug,
                profile.input,
                client=frame_client,
            ),
        )
        for profile in profiles
    ]


def _fetch_database_observations(
    character_slug: str,
    client: Any,
) -> list[dict[str, Any]]:
    """Read reviewed observations when the additive migration is available."""
    try:
        rows = (
            client.table("sequence_observations")
            .select("*")
            .eq("attacker_character_slug", character_slug)
            .eq("reviewed", True)
            .limit(100)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # Migration is optional during rolling deploys.
        logger.debug("sequence_observations unavailable: %s", exc)
        return []
    return [dict(row) for row in rows]


def _all_observations(character_slug: str, client: Any) -> list[dict[str, Any]]:
    database = _fetch_database_observations(character_slug, client)
    bundled = [
        dict(row) for row in load_bundled_sequence_observations()
        if str(row.get("attacker_character_slug") or "").casefold()
        == character_slug.casefold()
    ]
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in [*bundled, *database]:
        key = (
            str(row.get("observation_key") or ""),
            str(row.get("source") or ""),
            str(row.get("patch_version") or ""),
        )
        merged[key] = row
    return list(merged.values())


def _matching_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    character_slug: str,
    attacker_inputs: Sequence[str],
    initial_interaction: str,
    defender_startup_f: int | None,
    expected_outcome: str | None,
    exact_defender_requested: bool,
    defender_character_slug: str | None,
    defender_move_input: str | None,
    attacker_delay_f: int | None,
    defender_delay_f: int | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for raw in observations:
        row = dict(raw)
        if not row.get("reviewed"):
            continue
        if str(row.get("attacker_character_slug") or "").casefold() != character_slug.casefold():
            continue
        sequence = row.get("attacker_sequence") or []
        observed_inputs = [
            str(item.get("input")) for item in sequence if isinstance(item, Mapping)
        ]
        if observed_inputs != list(attacker_inputs):
            continue
        if row.get("initial_interaction") != initial_interaction:
            continue
        second_event = sequence[1] if len(sequence) > 1 and isinstance(sequence[1], Mapping) else {}
        observed_attacker_delay = second_event.get("delay_f")
        if observed_attacker_delay is None and second_event.get("timing") == "earliest":
            observed_attacker_delay = 0
        if observed_attacker_delay != attacker_delay_f:
            continue
        defender = row.get("defender_profile") or {}
        if defender_startup_f is not None and defender.get("startup_f") != defender_startup_f:
            continue
        observed_character = (
            row.get("defender_character_slug")
            or defender.get("character_slug")
        )
        if defender_character_slug and (
            not observed_character
            or str(observed_character).casefold() != defender_character_slug.casefold()
        ):
            continue
        observed_move = row.get("defender_move_input") or defender.get("move_input")
        has_exact_post_result = (
            isinstance(row.get("attacker_advantage_f"), int)
            or isinstance(row.get("defender_advantage_f"), int)
            or bool(row.get("confirmed_followups"))
        )
        # Post-trade advantage and confirmed followups belong to one exact
        # opposing move. Startup alone describes a class of moves whose
        # hitstun values can differ, so it can never select an exact result.
        if has_exact_post_result and (
            not exact_defender_requested
            or not observed_character
            or not observed_move
        ):
            continue
        if defender_move_input and (
            not observed_move
            or str(observed_move).casefold() != defender_move_input.casefold()
        ):
            continue
        observed_defender_delay = defender.get("delay_f")
        if observed_defender_delay is None and defender.get("timing") == "earliest":
            observed_defender_delay = 0
        if observed_defender_delay != defender_delay_f:
            continue
        if expected_outcome and row.get("outcome") != expected_outcome:
            continue
        if exact_defender_requested and not observed_move:
            continue
        matches.append(row)
    matches.sort(
        key=lambda row: (bool(row.get("reviewed")), float(row.get("confidence") or 0)),
        reverse=True,
    )
    return matches


def _observation_frame_fingerprint_matches(
    observation: Mapping[str, Any],
    opener: MoveInteractionProfile,
    pressure_move: MoveInteractionProfile,
) -> bool:
    """Reject a reviewed result when current adopted frame facts have changed."""
    conditions = observation.get("conditions") or {}
    fingerprint = conditions.get("frame_fingerprint") or {}
    if not fingerprint:
        return True
    current = {
        "opener_on_block_f": opener.on_block_f,
        "opener_on_hit_f": opener.on_hit_f,
        "pressure_startup_f": pressure_move.startup_f,
        "pressure_hitstun_f": pressure_move.hitstun_f,
        "pressure_hitstop_f": pressure_move.hitstop_f,
    }
    return all(current.get(key) == expected for key, expected in fingerprint.items())


def calculate_trade_advantage_from_hitstun(
    attacker_move: MoveInteractionProfile,
    defender_move: MoveInteractionProfile,
) -> int | None:
    """Return attacker advantage after a simultaneous direct-strike trade.

    Both attacks are interrupted by the trade, so their remaining active and
    recovery frames no longer determine readiness.  For the currently modelled
    same-frame direct-strike case, shared hit freeze cancels out and the frame
    table convention is ``inflicted hitstun difference - 1``.  This model is
    surfaced in results and never substitutes for a reviewed exact observation.
    """
    if attacker_move.hitstun_f is None or defender_move.hitstun_f is None:
        return None
    return attacker_move.hitstun_f - defender_move.hitstun_f - 1


def _timeline(
    opener: MoveInteractionProfile,
    pressure_move: MoveInteractionProfile,
    initial_interaction: str,
    defender_startup_f: int | None,
    attacker_delay_f: int | None,
    defender_delay_f: int | None,
) -> dict[str, Any]:
    advantage = opener.on_block_f if initial_interaction == "block" else opener.on_hit_f
    if (
        advantage is None
        or pressure_move.startup_f is None
        or defender_startup_f is None
        or attacker_delay_f is None
        or defender_delay_f is None
    ):
        return {
            "status": "unresolved",
            "reason_codes": ["scalar_timeline_input_missing"],
            "initial_advantage_f": advantage,
            "attacker_delay_f": attacker_delay_f,
            "defender_delay_f": defender_delay_f,
        }

    attacker_ready = max(0, -advantage)
    defender_ready = max(0, advantage)
    attacker_action_start = attacker_ready + attacker_delay_f
    defender_action_start = defender_ready + defender_delay_f
    attacker_active = attacker_action_start + pressure_move.startup_f
    defender_active = defender_action_start + defender_startup_f
    delta = defender_active - attacker_active
    if delta == 0:
        timing_outcome = "simultaneous"
    elif delta > 0:
        timing_outcome = "attacker_first"
    else:
        timing_outcome = "defender_first"
    return {
        "status": "resolved",
        "initial_interaction": initial_interaction,
        "initial_advantage_f": advantage,
        "attacker_ready_frame": attacker_ready,
        "defender_ready_frame": defender_ready,
        "attacker_delay_f": attacker_delay_f,
        "defender_delay_f": defender_delay_f,
        "attacker_action_start_frame": attacker_action_start,
        "defender_action_start_frame": defender_action_start,
        "attacker_first_active_frame": attacker_active,
        "defender_first_active_frame": defender_active,
        "active_frame_delta_f": delta,
        "timing_outcome": timing_outcome,
        "events": [
            {"frame": attacker_ready, "actor": "attacker", "event": "actionable"},
            {"frame": defender_ready, "actor": "defender", "event": "actionable"},
            {"frame": attacker_action_start, "actor": "attacker", "event": "action_start"},
            {"frame": defender_action_start, "actor": "defender", "event": "action_start"},
            {"frame": attacker_active, "actor": "attacker", "event": "first_active"},
            {"frame": defender_active, "actor": "defender", "event": "first_active"},
        ],
    }


def _derived_trade_profiles(
    pressure_move: MoveInteractionProfile,
    defender_profiles: Sequence[MoveInteractionProfile],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for move in defender_profiles:
        advantage = calculate_trade_advantage_from_hitstun(pressure_move, move)
        if advantage is None:
            continue
        rows.append({
            "defender_character": move.character,
            "defender_input": move.input,
            "defender_name": move.name,
            "defender_startup_f": move.startup_f,
            "defender_hitstun_f": move.hitstun_f,
            "defender_hitstop_f": move.hitstop_f,
            "attacker_hitstun_f": pressure_move.hitstun_f,
            "attacker_hitstop_f": pressure_move.hitstop_f,
            "attacker_advantage_f": advantage,
            "defender_advantage_f": -advantage,
            "calculation_model": TRADE_MODEL_VERSION,
            "calculation_expression": (
                f"{pressure_move.hitstun_f} - {move.hitstun_f} - 1 = {advantage}"
            ),
        })
    return rows


def _followup_results(
    followup_profiles: Sequence[MoveInteractionProfile],
    *,
    exact_advantage_f: int | None,
    derived_min_f: int | None,
    derived_max_f: int | None,
    derived_profiles: Sequence[Mapping[str, Any]],
    observation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    observed = {
        str(item.get("input")): dict(item)
        for item in ((observation or {}).get("confirmed_followups") or [])
        if isinstance(item, Mapping) and item.get("input")
    }
    candidates: list[dict[str, Any]] = []
    derived_advantages = [
        int(item["attacker_advantage_f"])
        for item in derived_profiles
        if isinstance(item.get("attacker_advantage_f"), int)
    ]
    for move in followup_profiles:
        if move.startup_f is None:
            continue
        maximum = exact_advantage_f if exact_advantage_f is not None else derived_max_f
        if maximum is None or move.startup_f > maximum:
            continue
        direct = observed.get(move.input)
        if exact_advantage_f is not None:
            timing_status = (
                "timing_connected" if move.startup_f <= exact_advantage_f else "too_slow"
            )
            leniency = exact_advantage_f - move.startup_f
            connected_profile_count = 1 if timing_status == "timing_connected" else 0
            total_profile_count = 1
            connected_leniencies = (
                [exact_advantage_f - move.startup_f] if connected_profile_count else []
            )
        elif derived_min_f is not None and move.startup_f <= derived_min_f:
            timing_status = "timing_connected_for_all_profiles"
            leniency = derived_min_f - move.startup_f
            connected_profile_count = len(derived_advantages)
            total_profile_count = len(derived_advantages)
            connected_leniencies = [
                advantage - move.startup_f for advantage in derived_advantages
            ]
        else:
            timing_status = "timing_connected_for_some_profiles"
            leniency = None
            connected_leniencies = [
                advantage - move.startup_f
                for advantage in derived_advantages
                if advantage >= move.startup_f
            ]
            connected_profile_count = len(connected_leniencies)
            total_profile_count = len(derived_advantages)
        candidates.append({
            "input": move.input,
            "move_name": move.name,
            "startup_f": move.startup_f,
            "timing_status": timing_status,
            "timing_connected": timing_status != "too_slow",
            "leniency_f": leniency,
            "leniency_min_f": min(connected_leniencies) if connected_leniencies else None,
            "leniency_max_f": max(connected_leniencies) if connected_leniencies else None,
            "timing_connected_profile_count": connected_profile_count,
            "timing_total_profile_count": total_profile_count,
            "spatial_connected": direct.get("spatial_connected") if direct else None,
            "state_connected": direct.get("state_connected") if direct else None,
            "combo_confirmed": bool(direct and direct.get("combo_confirmed")),
            "evidence": direct.get("evidence") if direct else None,
        })
    candidates.sort(
        key=lambda item: (
            not item["combo_confirmed"],
            item["startup_f"],
            item["input"],
        )
    )
    return {
        "confirmed": [item for item in candidates if item["combo_confirmed"]],
        "timing_candidates": candidates[:30],
        "spatial_policy": (
            "Only observation-backed candidates are marked combo_confirmed."
        ),
    }


def _format_summary(result: Mapping[str, Any]) -> str:
    sequence = result["attacker_sequence"]
    first, second = sequence[0], sequence[1]
    timeline = result["timeline"]
    collision = result["collision"]
    post = result["post_interaction"]
    lines = [
        f"【{result['attacker_character']} / {first['input']} -> {second['input']} 連携解析】"
    ]
    if timeline.get("status") == "resolved":
        initial = timeline["initial_advantage_f"]
        attacker_active = timeline["attacker_first_active_frame"]
        defender_active = timeline["defender_first_active_frame"]
        if attacker_active == defender_active:
            active_text = f"両者の攻撃判定は共通タイムラインの{attacker_active}F目"
        else:
            active_text = (
                f"共通タイムライン上で攻撃側は{attacker_active}F目、"
                f"防御側は{defender_active}F目"
            )
        lines.append(
            f"1発目の{first['input']}後は攻撃側が{initial:+d}Fです"
            f"{_source_suffix(first, 'on_block' if result['initial_interaction'] == 'block' else 'on_hit')}。"
            f"次の{second['input']}は{_delay_text(timeline.get('attacker_delay_f'))}"
            f"発生{second['startup_f']}F"
            f"{_source_suffix(second, 'startup')}、"
            f"防御側の暴れは{_delay_text(timeline.get('defender_delay_f'))}"
            f"発生{result['defender_action']['startup_f']}Fで、"
            f"{active_text}に出ます。"
        )
        if timeline["timing_outcome"] == "simultaneous":
            lines.append(
                "フレーム上は同時です。両方の攻撃判定が届き、無敵や特殊な相互作用が"
                "なければ相打ちになります。"
            )
        elif timeline["timing_outcome"] == "attacker_first":
            lines.append("フレーム上は攻撃側の次の技が先に発生します。")
        else:
            lines.append("フレーム上は防御側の暴れが先に発生します。")
    else:
        lines.append("時系列計算に必要な単一フレーム値が不足しています。")

    advantage = post.get("attacker_advantage_f")
    if timeline.get("timing_outcome") != "simultaneous":
        lines.append("フレーム上は同時発生でないため、相打ち後の有利差は算出しません。")
    elif isinstance(advantage, int):
        label = "検証済み観測" if post.get("status") == "observed_exact" else "計算モデル"
        exact_profiles = post.get("derived_profiles") or []
        expression = ""
        if len(exact_profiles) == 1 and exact_profiles[0].get("calculation_expression"):
            profile = exact_profiles[0]
            expression = (
                f"（{second['input']}のhitstun {profile['attacker_hitstun_f']} - "
                f"{profile['defender_character']} {profile['defender_input']}のhitstun "
                f"{profile['defender_hitstun_f']} - 1 = {advantage:+d}）"
            )
        lines.append(
            f"{label}では相打ち後、攻撃側が{advantage:+d}F、"
            f"防御側が{-advantage:+d}Fです{expression}。"
        )
    elif isinstance(post.get("min_f"), int) and isinstance(post.get("max_f"), int):
        lines.append(
            f"相手の技が未指定のため単一値にはできません。SCの該当技"
            f"{post['derived_profile_summary']['profile_count']}件を技別に計算すると、"
            f"攻撃側は{post['min_f']:+d}～{post['max_f']:+d}F、"
            f"防御側は{-post['max_f']:+d}～{-post['min_f']:+d}Fです。"
        )
        if second.get("hitstun_f") is not None:
            lines.append(
                f"各技の計算式は {second['hitstun_f']} - 相手技のhitstun - 1 です。"
            )
        grouped: dict[int, list[str]] = {}
        for profile in post.get("derived_profiles") or []:
            value = profile.get("attacker_advantage_f")
            if not isinstance(value, int):
                continue
            grouped.setdefault(value, []).append(
                f"{profile.get('defender_character')} {profile.get('defender_input')}"
            )
        if grouped:
            breakdown = []
            for value, labels in sorted(grouped.items()):
                examples = "、".join(labels[:3])
                more = f"ほか{len(labels) - 3}技" if len(labels) > 3 else ""
                separator = "、" if examples and more else ""
                breakdown.append(
                    f"{value:+d}F: {len(labels)}技（{examples}{separator}{more}）"
                )
            lines.append("技別内訳: " + " / ".join(breakdown))
    else:
        lines.append("相打ち後の有利差は、相手技のhitstunまたは直接観測が不足しています。")

    confirmed = result.get("followups", {}).get("confirmed") or []
    for followup in confirmed:
        leniency = followup.get("leniency_f")
        leniency_text = (
            f"猶予{leniency}F" if isinstance(leniency, int) else "猶予は観測条件依存"
        )
        lines.append(
            f"確認済み追撃は{followup['input']}（発生{followup['startup_f']}F）です。"
            f"最速で出すと連続ヒットします（{leniency_text}）。"
        )
    if not confirmed:
        timing_candidates = result.get("followups", {}).get("timing_candidates") or []
        common_candidates = [
            item for item in timing_candidates
            if item.get("timing_status") in {
                "timing_connected",
                "timing_connected_for_all_profiles",
            }
        ]
        conditional_candidates = [
            item for item in timing_candidates
            if item.get("timing_status") == "timing_connected_for_some_profiles"
        ]
        if common_candidates:
            exact_move = bool(
                result.get("defender_action", {}).get("exact_move_specified")
            )
            candidate_text = " / ".join(
                (
                    f"{item['input']}(発生{item['startup_f']}F、"
                    f"猶予{item['leniency_f']}F)"
                    if exact_move and isinstance(item.get("leniency_f"), int)
                    else f"{item['input']}(発生{item['startup_f']}F)"
                )
                for item in common_candidates[:8]
            )
            scope = (
                "フレーム上の追撃候補"
                if exact_move
                else "フレーム上、全対象で時間的に接続する追撃候補"
            )
            lines.append(
                f"{scope}は{candidate_text}です。"
                "これらは距離・接触状態が未検証のため、連続ヒット確定とは扱いません。"
            )
        if conditional_candidates:
            candidate_text = " / ".join(
                f"{item['input']}(発生{item['startup_f']}F: "
                f"{item['timing_connected_profile_count']}/"
                f"{item['timing_total_profile_count']}技)"
                for item in conditional_candidates[:8]
            )
            lines.append(
                f"相手技によって時間的に接続する候補は{candidate_text}です。"
                "相手技を特定するまで確定追撃とは断定できません。"
            )

    if collision.get("spatial_status") == "unverified":
        lines.append("距離・当たり判定は未検証なので、相打ち成立自体は条件付きです。")
    return "\n".join(lines)


def _source_suffix(move: Mapping[str, Any], field: str) -> str:
    source = (move.get("frame_sources") or {}).get(field)
    return f"（{source}）" if source else ""


def _delay_text(delay_f: Any) -> str:
    return f"{delay_f}Fディレイ後に" if isinstance(delay_f, int) and delay_f > 0 else ""


def evaluate_sequence(
    *,
    character_slug: str,
    sc_character: str,
    attacker_moves: Sequence[MoveInteractionProfile],
    initial_interaction: str,
    defender_startup_f: int | None,
    defender_profiles: Sequence[MoveInteractionProfile],
    followup_profiles: Sequence[MoveInteractionProfile],
    expected_outcome: str | None,
    observations: Sequence[Mapping[str, Any]],
    exact_defender_requested: bool = False,
    defender_character_slug: str | None = None,
    defender_move_input: str | None = None,
    attacker_delay_f: int | None = 0,
    defender_delay_f: int | None = 0,
) -> dict[str, Any]:
    """Pure sequence evaluation once source rows have been resolved."""
    if len(attacker_moves) != 2:
        return {
            "found": False,
            "status": "unsupported_sequence_length",
            "message": "現在の連携解析は攻撃側2技のシーケンスに対応しています。",
        }
    opener, pressure_move = attacker_moves
    timeline = _timeline(
        opener,
        pressure_move,
        initial_interaction,
        defender_startup_f,
        attacker_delay_f,
        defender_delay_f,
    )
    matching = _matching_observations(
        observations,
        character_slug=character_slug,
        attacker_inputs=[move.input for move in attacker_moves],
        initial_interaction=initial_interaction,
        defender_startup_f=defender_startup_f,
        expected_outcome=expected_outcome,
        exact_defender_requested=exact_defender_requested,
        defender_character_slug=defender_character_slug,
        defender_move_input=defender_move_input,
        attacker_delay_f=attacker_delay_f,
        defender_delay_f=defender_delay_f,
    )
    observation = next((
        row for row in matching
        if _observation_frame_fingerprint_matches(row, opener, pressure_move)
    ), None)

    note_supports_trade = bool(
        pressure_move.notes
        and re.search(r"trade combo", pressure_move.notes, re.IGNORECASE)
    )
    simultaneous = timeline.get("timing_outcome") == "simultaneous"
    if observation and observation.get("outcome") == "trade" and not simultaneous:
        observation = None
    if simultaneous and observation and observation.get("outcome") == "trade":
        collision_outcome = "trade"
        spatial_status = "observed"
    elif simultaneous and (expected_outcome == "trade" or note_supports_trade):
        collision_outcome = "trade_if_both_reach"
        spatial_status = "community_note" if note_supports_trade else "unverified"
    elif simultaneous:
        collision_outcome = "simultaneous_contact_unresolved"
        spatial_status = "unverified"
    else:
        collision_outcome = timeline.get("timing_outcome") or "unresolved"
        spatial_status = "unverified"

    derived = _derived_trade_profiles(pressure_move, defender_profiles) if simultaneous else []
    advantages = [row["attacker_advantage_f"] for row in derived]
    min_adv = min(advantages) if advantages else None
    max_adv = max(advantages) if advantages else None
    histogram = Counter(advantages)
    derived_summary = {
        "status": "derived_interval" if len(set(advantages)) > 1 else "derived_exact",
        "min_f": min_adv,
        "max_f": max_adv,
        "profile_count": len(derived),
        "distribution": [
            {"advantage_f": value, "count": count}
            for value, count in sorted(histogram.items())
        ],
        "calculation_model": TRADE_MODEL_VERSION,
        "formula": "attacker_inflicted_hitstun - defender_inflicted_hitstun - 1",
    } if advantages else {
        "status": "unresolved",
        "profile_count": 0,
        "calculation_model": TRADE_MODEL_VERSION,
    }

    exact_advantage: int | None = None
    if (
        exact_defender_requested
        and observation
        and isinstance(observation.get("attacker_advantage_f"), int)
    ):
        exact_advantage = int(observation["attacker_advantage_f"])
        post_status = "observed_exact"
        post_source = {
            "source": observation.get("source"),
            "patch_version": observation.get("patch_version"),
            "confidence": observation.get("confidence"),
            "observation_key": observation.get("observation_key"),
        }
    elif exact_defender_requested and len(derived) == 1:
        exact_advantage = int(derived[0]["attacker_advantage_f"])
        post_status = "derived_exact"
        post_source = {"calculation_model": TRADE_MODEL_VERSION}
    elif advantages:
        post_status = (
            "derived_interval" if min_adv != max_adv else "derived_profile_set"
        )
        post_source = {"calculation_model": TRADE_MODEL_VERSION}
    else:
        post_status = "unresolved"
        post_source = {}

    followups = _followup_results(
        followup_profiles,
        exact_advantage_f=exact_advantage,
        derived_min_f=min_adv,
        derived_max_f=max_adv,
        derived_profiles=derived,
        observation=observation,
    )
    result: dict[str, Any] = {
        "schema_version": SEQUENCE_SCHEMA_VERSION,
        "found": True,
        "status": (
            "resolved"
            if post_status == "observed_exact" and collision_outcome == "trade"
            else "partially_resolved"
        ),
        "attacker_character_slug": character_slug,
        "attacker_character": sc_character,
        "attacker_sequence": [asdict(move) for move in attacker_moves],
        "initial_interaction": initial_interaction,
        "attacker_timing": {
            "timing": "delayed" if attacker_delay_f else "earliest",
            "second_move_delay_f": attacker_delay_f,
        },
        "defender_action": {
            "timing": "delayed" if defender_delay_f else "earliest",
            "startup_f": defender_startup_f,
            "delay_f": defender_delay_f,
            "exact_move_specified": exact_defender_requested,
            "character_slug": defender_character_slug,
            "move_input": defender_move_input,
            "candidate_profile_count": len(defender_profiles),
        },
        "timeline": timeline,
        "collision": {
            "timing_status": timeline.get("status"),
            "timing_outcome": timeline.get("timing_outcome"),
            "outcome": collision_outcome,
            "spatial_status": spatial_status,
            "note_supports_trade": note_supports_trade,
            "requires_both_moves_to_reach": True,
        },
        "post_interaction": {
            "interaction": "trade" if collision_outcome.startswith("trade") else collision_outcome,
            "status": post_status,
            "attacker_advantage_f": exact_advantage,
            "defender_advantage_f": -exact_advantage if exact_advantage is not None else None,
            "min_f": min_adv,
            "max_f": max_adv,
            "source": post_source,
            "derived_profile_summary": derived_summary,
            "derived_profiles": derived[:100],
            "hitstop_policy": (
                "Shared freeze is not added to the advantage difference for the "
                "simultaneous direct-strike model. Raw hitstop remains in each profile."
            ),
        },
        "followups": followups,
        "evidence": {
            "reviewed_observation": observation,
            "supercombo_note": pressure_move.notes if note_supports_trade else None,
        },
    }
    result["summary"] = _format_summary(result)
    return result


def analyze_sequence(
    character: str,
    attacker_sequence: Sequence[str],
    *,
    initial_interaction: str = "block",
    defender_startup_f: int | None = None,
    defender_character: str | None = None,
    defender_move: str | None = None,
    expected_outcome: str | None = None,
    attacker_delay_f: int | None = 0,
    defender_delay_f: int | None = 0,
    client: Any | None = None,
) -> dict[str, Any]:
    """Resolve source data and analyze a two-move pressure sequence."""
    if initial_interaction not in {"block", "hit"}:
        return {
            "found": False,
            "status": "invalid_interaction",
            "message": "initial_interaction は block または hit を指定してください。",
        }
    if len(attacker_sequence) != 2:
        return {
            "found": False,
            "status": "unsupported_sequence_length",
            "message": "現在は2技の連携を指定してください。",
        }
    if (
        (attacker_delay_f is not None and attacker_delay_f < 0)
        or (defender_delay_f is not None and defender_delay_f < 0)
    ):
        return {
            "found": False,
            "status": "invalid_delay",
            "message": "ディレイは0F以上で指定してください。",
        }
    if attacker_delay_f is None or defender_delay_f is None:
        return {
            "found": False,
            "status": "delay_unspecified",
            "message": "ディレイ連携の計算には、遅らせるフレーム数を指定してください。",
        }
    if bool(defender_character) != bool(defender_move) and defender_move:
        return {
            "found": False,
            "status": "defender_character_required",
            "message": "相手技を指定する場合は、相手キャラも指定してください。",
        }
    if defender_startup_f is None and not defender_move:
        return {
            "found": False,
            "status": "defender_action_required",
            "message": "防御側の技、または発生フレームを指定してください。",
        }
    sb = client or get_client()
    # Explicit clients are preserved for unit/integration isolation. Normal
    # production calls use frame_data's process cache for all source rows.
    frame_client = client
    character_slug, sc_character = _resolve_sc_character(character, sb)
    attacker_moves: list[MoveInteractionProfile] = []
    missing: list[str] = []
    for identifier in attacker_sequence:
        profile = _fetch_move_profile(
            character_slug,
            sc_character,
            identifier,
            sb,
            frame_client=frame_client,
        )
        if profile is None:
            missing.append(identifier)
        else:
            attacker_moves.append(profile)
    if missing:
        return {
            "found": False,
            "status": "move_not_found",
            "message": f"{sc_character} の技が見つかりません: {', '.join(missing)}",
            "missing_moves": missing,
        }

    defender_slug: str | None = None
    defender_sc: str | None = None
    if defender_character:
        defender_slug, defender_sc = _resolve_sc_character(str(defender_character), sb)
    exact_defender = bool(defender_sc and defender_move)
    defender_profiles: list[MoveInteractionProfile] = []
    if exact_defender:
        defender_profile = _fetch_move_profile(
            str(defender_slug),
            str(defender_sc),
            str(defender_move),
            sb,
            frame_client=frame_client,
        )
        if defender_profile is None:
            return {
                "found": False,
                "status": "defender_move_not_found",
                "message": f"{defender_sc} の技 {defender_move} が見つかりません。",
            }
        defender_profiles = [defender_profile]
        defender_startup_f = defender_profile.startup_f
    elif defender_startup_f is not None:
        defender_profiles = _fetch_defender_profiles(
            defender_startup_f,
            sb,
            sc_character=defender_sc,
        )

    followups = _fetch_followup_profiles(
        character_slug,
        sc_character,
        sb,
        frame_client=frame_client,
    )
    observations = _all_observations(character_slug, sb)
    return evaluate_sequence(
        character_slug=character_slug,
        sc_character=sc_character,
        attacker_moves=attacker_moves,
        initial_interaction=initial_interaction,
        defender_startup_f=defender_startup_f,
        defender_profiles=defender_profiles,
        followup_profiles=followups,
        expected_outcome=expected_outcome,
        observations=observations,
        exact_defender_requested=exact_defender,
        defender_character_slug=defender_slug,
        defender_move_input=(defender_profiles[0].input if exact_defender else None),
        attacker_delay_f=attacker_delay_f,
        defender_delay_f=defender_delay_f,
    )
