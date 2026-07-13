"""Structured match-scenario parsing and conservative frame evaluation.

Frame tables normally publish one reference observation for a move.  They do
not, by themselves, prove which active frame connected, the post-block spacing,
or whether system modifiers such as Drive Rush or Burnout were applied.  This
module keeps those question conditions separate from the move name and marks
unsupported calculations as unresolved instead of silently using the reference
number.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping


SCENARIO_SCHEMA_VERSION = 1

_ALLOWED_VALUES = {
    "distance": {"point_blank", "close", "mid", "far", "tip", "max_range"},
    "contact_timing": {"first_active", "late_active", "last_active", "active_frame"},
    "opponent_state": {"standing", "crouching", "airborne"},
    "counter_state": {"normal", "counter", "punish_counter"},
    "drive_rush": {"raw", "cancel"},
    "interaction": {"block", "hit"},
    "perspective": {"attacker", "defender"},
}


def _record(
    scenario: dict[str, Any],
    field: str,
    value: Any,
    evidence: str,
) -> None:
    scenario[field] = value
    if field not in scenario["specified"]:
        scenario["specified"].append(field)
    scenario["evidence"][field] = evidence


def parse_frame_scenario(query: str) -> dict[str, Any]:
    """Extract only explicit match conditions from a Japanese/English query."""
    text = query or ""
    scenario: dict[str, Any] = {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "specified": [],
        "evidence": {},
        "ambiguities": [],
    }

    distance_patterns = (
        ("point_blank", r"密着(?!版)|至近距離(?!版)|point\s*blank"),
        ("max_range", r"最大(?:間合い|距離|リーチ)|maximum\s*range"),
        ("tip", r"先端(?:当て)?|tip\s*range|at\s*the\s*tip"),
        ("close", r"近距離(?!版)|close\s*range"),
        ("mid", r"中距離(?!版)|mid(?:dle)?\s*range"),
        ("far", r"遠距離(?!版)|far\s*range"),
    )
    for value, pattern in distance_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            _record(scenario, "distance", value, match.group(0))
            break

    numeric_distance = re.search(
        r"(?:距離|間合い)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:マス|目盛|units?)?",
        text,
        re.IGNORECASE,
    )
    if numeric_distance:
        _record(
            scenario,
            "distance_value",
            float(numeric_distance.group(1)),
            numeric_distance.group(0),
        )
        scenario["distance_unit"] = "training_unit_unspecified"

    active_frame_match = re.search(
        r"(?:持続(?:の)?\s*)?(\d+)\s*F?目(?:の持続|で当て|当て)?|"
        r"第\s*(\d+)\s*持続|持続\s*(\d+)\s*F?目",
        text,
        re.IGNORECASE,
    )
    if active_frame_match:
        active_frame = next(
            int(group) for group in active_frame_match.groups() if group is not None
        )
        _record(scenario, "contact_timing", "active_frame", active_frame_match.group(0))
        _record(scenario, "active_frame", active_frame, active_frame_match.group(0))
    else:
        last_active = re.search(r"最終持続|持続(?:の)?最後|last\s*active", text, re.IGNORECASE)
        late_active = re.search(r"持続当て|遅らせ当て|late\s*active|meaty", text, re.IGNORECASE)
        first_active = re.search(r"初持続|持続1F目|first\s*active", text, re.IGNORECASE)
        if last_active:
            _record(scenario, "contact_timing", "last_active", last_active.group(0))
        elif late_active:
            _record(scenario, "contact_timing", "late_active", late_active.group(0))
        elif first_active:
            _record(scenario, "contact_timing", "first_active", first_active.group(0))

    stage_match = re.search(
        r"(?:の\s*(?:第\s*)?|第\s*)(\d+)\s*段目|"
        r"(?:の\s*|第\s*)([一二三四五六七八九十])段目",
        text,
    )
    if stage_match:
        jp_digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                     "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        stage = int(stage_match.group(1)) if stage_match.group(1) else jp_digits[stage_match.group(2)]
        _record(scenario, "stage_index", stage, stage_match.group(0).strip())

    state_patterns = (
        ("airborne", r"相手(?:が|は)?(?:空中|ジャンプ中)|空中の相手|airborne\s+opponent"),
        ("crouching", r"相手(?:が|は)?しゃがみ|しゃがみ状態の相手|しゃがみガード|crouching\s+opponent"),
        ("standing", r"相手(?:が|は)?立ち状態|立ち状態の相手|立ちガード|standing\s+opponent"),
    )
    for value, pattern in state_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            _record(scenario, "opponent_state", value, match.group(0))
            break

    punish_counter = re.search(r"パニッシュカウンター|パニカン|punish\s*counter", text, re.IGNORECASE)
    counter = re.search(r"(?:カウンター(?:ヒット|時)?|counter\s*hit)", text, re.IGNORECASE)
    if punish_counter:
        _record(scenario, "counter_state", "punish_counter", punish_counter.group(0))
    elif counter:
        _record(scenario, "counter_state", "counter", counter.group(0))

    defender_burnout = re.search(
        r"相手(?:が|は|の)?バーンアウト|バーンアウト中の相手|"
        r"defender\s+(?:is\s+)?in\s+burnout",
        text,
        re.IGNORECASE,
    )
    generic_burnout = re.search(r"バーンアウト|burnout", text, re.IGNORECASE)
    if defender_burnout:
        _record(scenario, "defender_burnout", True, defender_burnout.group(0))
    elif generic_burnout:
        scenario["ambiguities"].append({
            "field": "burnout_actor",
            "evidence": generic_burnout.group(0),
            "message": "バーンアウトしている側が攻撃側か防御側か特定できません。",
        })

    dr_cancel = re.search(r"DR\s*キャンセル|ドライブラッシュキャンセル", text, re.IGNORECASE)
    drive_rush = re.search(
        r"(?:DR|ドライブラッシュ)(?:から|中|通常技|攻撃)|drive\s*rush",
        text,
        re.IGNORECASE,
    )
    if dr_cancel:
        _record(scenario, "drive_rush", "cancel", dr_cancel.group(0))
    elif drive_rush:
        _record(scenario, "drive_rush", "raw", drive_rush.group(0))

    corner = re.search(r"画面端|コーナー|corner", text, re.IGNORECASE)
    if corner:
        _record(scenario, "corner", True, corner.group(0))

    # Order matters: "ガードさせた" contains the broader guard expression.
    guard_attacker = re.search(r"ガードさせ(?:た|る|て)|on\s*block\s+as\s+attacker", text, re.IGNORECASE)
    guard_defender = re.search(
        r"(?:を|で)ガードし(?:た|て)|ガードし(?:た|て)|ガードした側|ガード後|"
        r"after\s+(?:I\s+)?block",
        text,
        re.IGNORECASE,
    )
    hit_defender = re.search(r"食ら(?:った|う)|被弾後|after\s+getting\s+hit", text, re.IGNORECASE)
    hit_attacker = re.search(r"ヒットさせ(?:た|る)|当て(?:た|て)側|on\s*hit\s+as\s+attacker", text, re.IGNORECASE)
    if guard_attacker:
        _record(scenario, "interaction", "block", guard_attacker.group(0))
        _record(scenario, "perspective", "attacker", guard_attacker.group(0))
    elif guard_defender:
        _record(scenario, "interaction", "block", guard_defender.group(0))
        _record(scenario, "perspective", "defender", guard_defender.group(0))
    elif "ガード" in text or re.search(r"on\s*block", text, re.IGNORECASE):
        _record(scenario, "interaction", "block", "ガード")
    elif hit_defender:
        _record(scenario, "interaction", "hit", hit_defender.group(0))
        _record(scenario, "perspective", "defender", hit_defender.group(0))
    elif hit_attacker:
        _record(scenario, "interaction", "hit", hit_attacker.group(0))
        _record(scenario, "perspective", "attacker", hit_attacker.group(0))
    elif "ヒット" in text or re.search(r"on\s*hit", text, re.IGNORECASE):
        _record(scenario, "interaction", "hit", "ヒット")

    return normalize_frame_scenario(scenario)


def normalize_frame_scenario(scenario: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-safe, validated scenario while preserving explicitness."""
    if not scenario:
        return {}
    normalized: dict[str, Any] = {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "specified": [],
        "evidence": {},
        "ambiguities": [],
    }
    specified = scenario.get("specified") or []
    for field, allowed in _ALLOWED_VALUES.items():
        value = scenario.get(field)
        if value in allowed:
            normalized[field] = value
            normalized["specified"].append(field)
    for field in ("active_frame", "stage_index"):
        value = scenario.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            normalized[field] = value
            normalized["specified"].append(field)
    distance_value = scenario.get("distance_value")
    if isinstance(distance_value, (int, float)) and not isinstance(distance_value, bool):
        normalized["distance_value"] = float(distance_value)
        normalized["distance_unit"] = str(
            scenario.get("distance_unit") or "training_unit_unspecified"
        )
        normalized["specified"].append("distance_value")
    for field in ("defender_burnout", "corner"):
        if scenario.get(field) is True:
            normalized[field] = True
            normalized["specified"].append(field)
    for field in specified:
        if field in normalized and field not in normalized["specified"]:
            normalized["specified"].append(field)
    evidence = scenario.get("evidence")
    if isinstance(evidence, Mapping):
        normalized["evidence"] = {
            str(key): str(value) for key, value in evidence.items()
            if key in normalized
        }
    ambiguities = scenario.get("ambiguities")
    if isinstance(ambiguities, list):
        normalized["ambiguities"] = [
            deepcopy(item) for item in ambiguities if isinstance(item, Mapping)
        ]
    if not normalized["specified"] and not normalized["ambiguities"]:
        return {}
    return normalized


def merge_frame_scenarios(
    primary: Mapping[str, Any] | None,
    fallback: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge scenarios, preferring explicitly parsed values in ``primary``."""
    merged = dict(normalize_frame_scenario(fallback))
    parsed = normalize_frame_scenario(primary)
    for key, value in parsed.items():
        if key not in {"specified", "evidence", "ambiguities", "schema_version"}:
            merged[key] = value
    merged["specified"] = list(dict.fromkeys([
        *(merged.get("specified") or []), *(parsed.get("specified") or []),
    ]))
    merged["evidence"] = {
        **(merged.get("evidence") or {}), **(parsed.get("evidence") or {}),
    }
    merged["ambiguities"] = [
        *(merged.get("ambiguities") or []), *(parsed.get("ambiguities") or []),
    ]
    merged["schema_version"] = SCENARIO_SCHEMA_VERSION
    return normalize_frame_scenario(merged)


_MOVE_NOISE_PATTERNS = (
    r"(?:密着|至近距離|近距離|中距離|遠距離|先端(?:当て)?|最大(?:間合い|距離|リーチ))(?!版)(?:で|から|の)?",
    r"(?:最終持続|持続(?:の)?最後|持続当て|遅らせ当て|持続\s*\d+\s*F?目|"
    r"\d+\s*F?目(?:の持続|で当て)?|第\s*\d+\s*持続)(?:で|を|の)?",
    r"(?:の\s*(?:第\s*)?|第\s*)\d+\s*段目(?:で|を|の)?",
    r"(?:相手(?:が|は|の)?バーンアウト|バーンアウト中の相手)(?:に|へ|で|の)?",
    r"(?:DR\s*キャンセル|ドライブラッシュキャンセル|DR|ドライブラッシュ)(?:から|中|で|の)?",
    r"(?:画面端|コーナー)(?:で|の)?",
)


def strip_scenario_phrases(move_text: str | None) -> str | None:
    """Remove recognized scenario phrases accidentally attached to a move name."""
    if not move_text:
        return move_text
    cleaned = move_text
    for pattern in _MOVE_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:で|の|から|に)+", "", cleaned)
    cleaned = re.sub(r"(?:で|の|から|に)+$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" 、,")
    return cleaned or None


def _fact_result(fact: Mapping[str, Any] | None) -> dict[str, Any]:
    if not fact:
        return {
            "status": "data_missing",
            "value": None,
            "display": "データなし",
            "usable_for_calculation": False,
            "reason_codes": ["frame_observation_missing"],
        }
    semantic = fact.get("semantic")
    if semantic == "not_applicable":
        return {
            "status": "not_applicable",
            "value": None,
            "display": fact.get("display"),
            "usable_for_calculation": False,
            "reason_codes": ["interaction_not_applicable"],
        }
    value = fact.get("value")
    if isinstance(value, int) and not fact.get("conditional"):
        return {
            "status": "source_exact",
            "value": value,
            "display": f"{value:+d}F",
            "base_value": value,
            "base_display": fact.get("display"),
            "usable_for_calculation": True,
            "reason_codes": [],
        }
    if isinstance(value, int):
        return {
            "status": "conditional_unresolved",
            "value": None,
            "reference_value": value,
            "display": fact.get("display") or f"{value:+d}F（条件付き）",
            "base_display": fact.get("display"),
            "usable_for_calculation": False,
            "reason_codes": ["condition_binding_missing"],
        }
    if fact.get("is_range"):
        return {
            "status": "interval",
            "value": None,
            "min": fact.get("min"),
            "max": fact.get("max"),
            "display": fact.get("display"),
            "usable_for_calculation": False,
            "reason_codes": ["condition_for_range_endpoint_missing"],
        }
    return {
        "status": "conditional_unresolved",
        "value": None,
        "display": fact.get("display") or "条件別データ",
        "base_display": fact.get("display"),
        "alternatives": list(fact.get("alternatives") or []),
        "sequence_items": list(fact.get("sequence_items") or []),
        "usable_for_calculation": False,
        "reason_codes": ["condition_binding_missing"],
    }


def _stage_result(
    result: dict[str, Any],
    fact: Mapping[str, Any] | None,
    stage_index: int | None,
) -> dict[str, Any]:
    if not stage_index:
        return result
    if not fact or fact.get("semantic") != "stage_sequence":
        return {
            **result,
            "status": "conditional_unresolved",
            "value": None,
            "usable_for_calculation": False,
            "reason_codes": ["stage_specific_observation_missing"],
            "required_data": [f"frame_observation:stage_{stage_index}"],
            "display": f"{stage_index}段目に対応する構造化観測なし",
        }
    items = fact.get("sequence_items") or []
    if stage_index > len(items):
        return {
            "status": "invalid_condition",
            "value": None,
            "display": f"{stage_index}段目のデータなし",
            "usable_for_calculation": False,
            "reason_codes": ["stage_out_of_range"],
        }
    item = items[stage_index - 1] or {}
    selected = _fact_result(item)
    selected["stage_index"] = stage_index
    selected["derivation"] = f"段階別データの{stage_index}段目を選択"
    if selected["status"] == "source_exact":
        selected["status"] = "condition_selected"
    return selected


def _direct_strike_can_shift(section: str | None, move_type: str | None) -> bool:
    section_text = (section or "").casefold()
    move_type_text = (move_type or "").casefold()
    return (
        section in {"通常技", "特殊技"}
        or "normal attack" in section_text
        or move_type_text in {"ground_normal", "command_normal"}
    )


def _apply_contact_timing(
    result: dict[str, Any],
    *,
    active_fact: Mapping[str, Any] | None,
    scenario: Mapping[str, Any],
    section: str | None,
    move_type: str | None,
) -> dict[str, Any]:
    timing = scenario.get("contact_timing")
    distance = scenario.get("distance")
    has_distance = bool(distance or scenario.get("distance_value") is not None)
    if not timing and not has_distance:
        return result
    if not result.get("usable_for_calculation"):
        return result
    active = (active_fact or {}).get("value")
    if not isinstance(active, int) or active < 1 or (active_fact or {}).get("conditional"):
        return {
            **result,
            "status": "conditional_unresolved",
            "value": None,
            "usable_for_calculation": False,
            "reason_codes": [*result.get("reason_codes", []), "active_window_not_scalar"],
        }
    if not _direct_strike_can_shift(section, move_type):
        return {
            **result,
            "status": "conditional_unresolved",
            "value": None,
            "usable_for_calculation": False,
            "reason_codes": [*result.get("reason_codes", []), "contact_timing_model_unavailable"],
        }

    base = result["value"]
    selected_frame: int | None = None
    if timing == "first_active":
        selected_frame = 1
    elif timing == "active_frame":
        selected_frame = scenario.get("active_frame")
        if not isinstance(selected_frame, int) or selected_frame > active:
            return {
                **result,
                "status": "invalid_condition",
                "value": None,
                "display": f"指定持続は{selected_frame}F目、技の持続は{active}F",
                "usable_for_calculation": False,
                "reason_codes": ["active_frame_out_of_range"],
            }
    elif timing == "last_active":
        selected_frame = active
    elif timing == "late_active" or (has_distance and active > 1):
        if active == 1:
            selected_frame = 1
        else:
            return {
                **result,
                "status": "derived_interval",
                "value": None,
                "min": base,
                "max": base + active - 1,
                "display": f"{base:+d}～{base + active - 1:+d}F",
                "usable_for_calculation": False,
                "reason_codes": ["contact_active_frame_unspecified"],
                "derivation": (
                    f"基準値 {base:+d}F に、接触した持続Fによる0～{active - 1}Fを加算"
                ),
            }

    if selected_frame is None:
        return result
    offset = selected_frame - 1
    value = base + offset
    return {
        **result,
        "status": "derived_exact" if offset else "source_exact",
        "value": value,
        "display": f"{value:+d}F",
        "active_frame": selected_frame,
        "usable_for_calculation": True,
        "derivation": (
            f"基準値 {base:+d}F + 持続{selected_frame}F目補正 {offset:+d}F"
        ),
    }


def _apply_unmodeled_modifiers(
    field: str,
    result: dict[str, Any],
    scenario: Mapping[str, Any],
) -> dict[str, Any]:
    missing: list[str] = []
    if field == "on_block" and scenario.get("defender_burnout"):
        missing.append("system_rule:defender_burnout_blockstun_modifier")
    if scenario.get("drive_rush"):
        missing.append(f"system_rule:drive_rush_{scenario['drive_rush']}_{field}_modifier")
    if field == "on_hit" and scenario.get("counter_state") in {"counter", "punish_counter"}:
        missing.append(f"system_rule:{scenario['counter_state']}_hit_modifier")
    if field == "on_hit" and scenario.get("opponent_state") == "airborne":
        missing.append("interaction_rule:airborne_hit_outcome")
    if field == "on_block" and scenario.get("opponent_state") == "airborne":
        missing.append("system_rule:air_block_availability")
    if not missing:
        return result
    return {
        **result,
        "status": "conditional_unresolved",
        "value": None,
        "usable_for_calculation": False,
        "reason_codes": [*result.get("reason_codes", []), "structured_system_rule_missing"],
        "required_data": missing,
        "base_display": result.get("display"),
        "display": "条件補正を適用する構造化ルールが未登録",
    }


def _invert_contextual_advantage(result: Mapping[str, Any]) -> dict[str, Any]:
    inverted = deepcopy(dict(result))
    value = result.get("value")
    if isinstance(value, int):
        inverted["value"] = -value
        inverted["display"] = f"{-value:+d}F"
    elif isinstance(result.get("min"), int) and isinstance(result.get("max"), int):
        lo, hi = -int(result["max"]), -int(result["min"])
        inverted["min"] = lo
        inverted["max"] = hi
        inverted["display"] = f"{lo:+d}～{hi:+d}F"
    base_value = result.get("base_value")
    if isinstance(base_value, int):
        inverted["base_value"] = -base_value
    reference_value = result.get("reference_value")
    if isinstance(reference_value, int):
        inverted["reference_value"] = -reference_value
        inverted["display"] = re.sub(
            r"(?<!\d)([+-]?\d+)F",
            lambda match: f"{-int(match.group(1)):+d}F",
            str(result.get("display") or ""),
        )
    elif result.get("alternatives"):
        values = [-int(item) for item in result["alternatives"]]
        inverted["alternatives"] = values
        inverted["display"] = (
            " / ".join(f"{item:+d}F" for item in values) + "（条件別）"
        )
    elif result.get("sequence_items"):
        inverted_items: list[dict[str, Any] | None] = []
        rendered: list[str] = []
        for index, item in enumerate(result["sequence_items"], start=1):
            item_result = _fact_result(item) if item else None
            item_inverted = (
                _invert_contextual_advantage(item_result) if item_result else None
            )
            inverted_items.append(item_inverted)
            rendered.append(
                f"{index}段目: "
                f"{(item_inverted or {}).get('display') or '算出不可'}"
            )
        inverted["sequence_items"] = inverted_items
        inverted["display"] = " / ".join(rendered)
    inverted["perspective"] = "defender"
    return inverted


def evaluate_frame_scenario(
    *,
    facts: Mapping[str, Mapping[str, Any]],
    scenario: Mapping[str, Any] | None,
    resolution: Mapping[str, Any] | None,
    section: str | None,
    move_type: str | None,
) -> dict[str, Any]:
    """Evaluate contextual advantages without inventing unsupported precision."""
    normalized = normalize_frame_scenario(scenario)
    resolved_move = (resolution or {}).get("status") == "resolved"
    contextual: dict[str, dict[str, Any]] = {}
    for field in ("on_block", "on_hit"):
        fact = facts.get(field)
        result = _fact_result(fact)
        result = _stage_result(result, fact, normalized.get("stage_index"))
        if not resolved_move:
            result = {
                **result,
                "status": "move_ambiguous",
                "value": None,
                "usable_for_calculation": False,
                "reason_codes": ["move_resolution_not_unique"],
            }
        result = _apply_contact_timing(
            result,
            active_fact=facts.get("active"),
            scenario=normalized,
            section=section,
            move_type=move_type,
        )
        result = _apply_unmodeled_modifiers(field, result, normalized)
        if normalized.get("ambiguities"):
            result = {
                **result,
                "status": "conditional_unresolved",
                "value": None,
                "usable_for_calculation": False,
                "reason_codes": ["scenario_subject_ambiguous"],
                "required_data": [
                    f"clarification:{item.get('field') or 'scenario'}"
                    for item in normalized["ambiguities"]
                ],
                "display": "質問条件の主語が未確定",
            }
        contextual[field] = result

    block = contextual["on_block"]
    block_value = block.get("value") if block.get("usable_for_calculation") else None
    if isinstance(block_value, int):
        window = max(0, -block_value)
        frame_punishable: bool | None = block_value < 0
        timing_status = "resolved"
    else:
        window = None
        frame_punishable = None
        timing_status = "unresolved"

    spatial_reasons = [
        "post_block_distance_missing",
        "defender_move_reach_by_startup_missing",
        "pushback_and_collision_state_missing",
    ]
    if frame_punishable is False:
        punish_status = "no_frame_window"
        confirmed_punishable: bool | None = False
    elif frame_punishable is True:
        punish_status = "timing_only_spatial_unverified"
        confirmed_punishable = None
    else:
        punish_status = "timing_unresolved"
        confirmed_punishable = None

    overall = "resolved"
    if normalized.get("ambiguities"):
        overall = "needs_clarification"
    elif not resolved_move:
        overall = "move_ambiguous"
    elif any(
        value.get("status") in {
            "conditional_unresolved", "invalid_condition", "move_ambiguous",
        }
        for value in contextual.values()
    ):
        overall = "partially_resolved"

    return {
        "scenario": normalized,
        "overall_status": overall,
        "contextual_facts": contextual,
        "block_perspectives": {
            "attacker": {**contextual["on_block"], "perspective": "attacker"},
            "defender": _invert_contextual_advantage(contextual["on_block"]),
        },
        "punish_assessment": {
            "status": punish_status,
            "timing_status": timing_status,
            "frame_punishable": frame_punishable,
            "punish_window_f": window,
            "spatial_status": "not_required" if frame_punishable is False else "unverified",
            "confirmed_punishable": confirmed_punishable,
            "required_data": [] if frame_punishable is False else spatial_reasons,
        },
    }
