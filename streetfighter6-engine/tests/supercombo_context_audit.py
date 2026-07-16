"""Audit CAPCOM notes and contextual causes behind SuperCombo mismatches.

This is the second stage of ``supercombo_inference_audit.py``.  It answers two
separate questions:

1. Can CAPCOM's official ``note`` / ``attribute`` cells replace contextual
   SuperCombo fields such as invulnerability, airborne state, and projectiles?
2. Are temporal formula mismatches actually caused by range/armor/juggle, or
   by conditional recovery, source-version skew, and move-specific rules?

SuperCombo remains an offline comparison label.  The leakage-free mismatch
analysis uses only the twelve fixed-name standard ground normals.  The broader
metadata comparison uses ``special_move_map`` for row alignment and therefore
reports that alignment separately; it never uses SC fields as CAPCOM features.

Usage:
  PYTHONPATH=src ./.venv312/bin/python tests/supercombo_context_audit.py \
      --output /tmp/supercombo_context_results.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sf6_engine.db import get_client
from sf6_engine.frame_data import _capcom_normal_input

from supercombo_inference_audit import (
    CAPCOM_COLUMNS,
    FORMULA_FEATURES,
    SC_COLUMNS,
    AuditMove,
    _feature,
    _load_moves,
    _predict,
    _strict_int,
)


CAPCOM_CONTEXT_PATTERNS: dict[str, re.Pattern[str]] = {
    "invulnerability": re.compile(r"無敵"),
    "armor_state": re.compile(r"(?<!ブレイク)(?:スーパー)?アーマー(?!ブレイク)"),
    "armor_break": re.compile(r"アーマー\s*ブレイク|アーマーブレイク"),
    "airborne_state": re.compile(r"空中(?:判定|状態|に移行|へ移行)|地上判定ではなく"),
    "projectile": re.compile(r"飛び道具|(?:^|[、。\s])弾(?:[、。\s]|$)|相殺"),
    "juggle_or_air_result": re.compile(
        r"追撃|空中ヒット|吹き飛び|きりもみ|バウンド|叩きつけ|壁やられ"
    ),
    "distance_or_contact": re.compile(r"距離|先端|間合い|近距離|遠距離|密着|持続当て"),
    "conditional_recovery": re.compile(
        r"空振り|ヒット時|ガード時|硬直|着地|動作途中|持続"
    ),
    "install_or_stance": re.compile(
        r"風水|酔い|強化|構え|レベル|風纏い|ホールド|チャージ"
    ),
}

SC_NOTE_PATTERNS: dict[str, re.Pattern[str]] = {
    "invulnerability": re.compile(r"invul|invinc", re.IGNORECASE),
    "armor_state": re.compile(r"armor", re.IGNORECASE),
    "airborne_state": re.compile(r"airborne|air state|leaves the ground", re.IGNORECASE),
    "projectile": re.compile(r"projectile", re.IGNORECASE),
    "juggle_or_air_result": re.compile(
        r"juggle|air hit|airborne opponent|ground bounce|wall splat", re.IGNORECASE
    ),
    "distance_or_contact": re.compile(
        r"range|tip|distance|close|far|point.blank|active frame", re.IGNORECASE
    ),
    "conditional_recovery": re.compile(
        r"whiff|on hit|on block|recovery|active frame", re.IGNORECASE
    ),
    "install_or_stance": re.compile(
        r"install|stance|drink level|feng shui|charge|hold", re.IGNORECASE
    ),
}

UFD_CONTEXT_PATTERNS: dict[str, re.Pattern[str]] = {
    "invulnerability": re.compile(r"invul|invinc", re.IGNORECASE),
    "armor_state": re.compile(r"armor", re.IGNORECASE),
    "airborne_state": re.compile(r"airborne|air state", re.IGNORECASE),
    "projectile": re.compile(r"projectile", re.IGNORECASE),
    "juggle_or_air_result": re.compile(r"juggle|air hit|bounce|wall", re.IGNORECASE),
    "distance_or_contact": re.compile(r"range|distance|close|far|tip", re.IGNORECASE),
    "conditional_recovery": re.compile(r"whiff|on hit|on block|recovery", re.IGNORECASE),
    "install_or_stance": re.compile(r"install|stance|level|charge|hold", re.IGNORECASE),
}

RECOVERY_DELTA_PATTERN = re.compile(
    r"硬直(?:が)?(?P<frames>\d+)F(?P<direction>増加|減少)"
)
EXPLICIT_FRAME_WINDOW_PATTERN = re.compile(
    r"\d+\s*(?:-|－|～|~)\s*\d+\s*F|\d+\s*F\s*(?:から|以降|～|~)"
)

# Refined metadata benchmark grammar.  Keep these patterns explicit in the
# report so that the quoted precision/recall values are reproducible and do
# not depend on the broader exploratory tag vocabulary above.
HIGH_CONFIDENCE_MAPPING_METHODS = frozenset({
    "fixed_normal_name",
    "special_move_map:manual",
    "special_move_map:auto-sig3",
})
CLOSED_FRAME_RANGE_PATTERN = re.compile(
    r"(?P<start>\d+)\s*(?:-|－|～|~)\s*(?P<end>\d+)\s*F?"
)
CAPCOM_SELF_AIRBORNE_PATTERN = re.compile(
    r"空中判定|空中状態|地上判定ではなく"
)
CAPCOM_AIR_INVULNERABILITY_CONTEXT_PATTERN = re.compile(r"空中判定の打撃")
CAPCOM_ARMOR_CANDIDATE_PATTERN = re.compile(
    r"(?<!ブレイク)(?:スーパー)?アーマー(?:判定)?"
)
CAPCOM_ARMOR_BREAK_PATTERN = re.compile(r"アーマー\s*ブレイク|アーマーブレイク")
CAPCOM_RANGE_CANDIDATE_PATTERN = re.compile(
    r"リーチ|距離|先端|間合い|近距離|遠距離|密着|射程|届"
)
CAPCOM_JUGGLE_CANDIDATE_PATTERN = re.compile(
    r"追撃|空中ヒット|吹き飛び|きりもみ|バウンド|叩きつけ|壁やられ|打ち上げ|浮き"
)
CAPCOM_PROJECTILE_SPEED_PATTERN = re.compile(
    r"弾速|飛翔速度|飛び道具.*速度"
)
CAPCOM_JUGGLE_NUMERIC_PATTERN = re.compile(
    r"追撃.*(?:値|回|\d)|(?:ジャグル|追撃値)"
)
SC_ACTUAL_ARMOR_VALUE_PATTERN = re.compile(r"\d|release", re.IGNORECASE)


@dataclass(frozen=True)
class ExpandedMove:
    """CAPCOM-to-SC row correspondence used only for metadata coverage."""

    character_slug: str
    sc_character: str
    capcom: Mapping[str, Any]
    sc: Mapping[str, Any]
    input: str
    mapping_method: str


def _present(value: object) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return text not in {"", "-", "--", "None"} and not text.startswith("{{{")


def _joined_text(row: Mapping[str, Any] | None, fields: Sequence[str]) -> str:
    if not row:
        return ""
    return "\n".join(str(row.get(field) or "") for field in fields)


def _tags(text: str, patterns: Mapping[str, re.Pattern[str]]) -> set[str]:
    return {name for name, pattern in patterns.items() if pattern.search(text)}


def _capcom_tags(row: Mapping[str, Any] | None) -> set[str]:
    return _tags(_joined_text(row, ("note", "attribute", "cancel")), CAPCOM_CONTEXT_PATTERNS)


def _capcom_recovery_claims(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Extract narrow, deterministic recovery modifiers from official notes.

    This deliberately handles only explicit ``N F 増加/減少`` sentences.  It
    does not promote the claims to executable runtime rules: CAPCOM's scalar
    recovery cell is not documented in the dataset as the base branch for all
    such sentences, so the audit later reports compatibility in both the
    direct and inverse directions.
    """

    if not row:
        return []
    claims: list[dict[str, Any]] = []
    for raw_line in str(row.get("note") or "").splitlines():
        line = re.sub(r"\s+", "", raw_line)
        match = RECOVERY_DELTA_PATTERN.search(line)
        if not match:
            continue
        prefix = line[:match.start()]
        outcomes: list[str] = []
        if "ヒット" in prefix:
            outcomes.append("hit")
        if "ガード" in prefix:
            outcomes.append("block")
        if "空振り" in prefix:
            outcomes.append("whiff")
        if not outcomes:
            continue
        frames = int(match.group("frames"))
        delta = frames if match.group("direction") == "増加" else -frames
        claims.append({
            "outcomes": outcomes,
            "delta": delta,
            "text": raw_line.strip(),
        })
    return claims


def _sc_tags(row: Mapping[str, Any]) -> set[str]:
    tags = _tags(str(row.get("notes") or ""), SC_NOTE_PATTERNS)
    if _present(row.get("invuln")):
        tags.add("invulnerability")
    armor = str(row.get("armor") or "")
    if _present(armor):
        if re.search(r"break", armor, re.IGNORECASE):
            tags.add("armor_break")
        else:
            tags.add("armor_state")
    if _present(row.get("airborne")):
        tags.add("airborne_state")
    if _present(row.get("proj_speed")):
        tags.add("projectile")
    if any(_present(row.get(field)) for field in ("jug_start", "jug_increase", "jug_limit")):
        tags.add("juggle_values_present")
    if _present(row.get("atk_range")):
        tags.add("range_value_present")
    recovery = str(row.get("recovery") or "")
    if recovery and _strict_int(recovery) is None:
        tags.add("conditional_recovery")
    return tags


def _ufd_tags(row: Mapping[str, Any] | None) -> set[str]:
    return _tags(_joined_text(row, ("notes", "hitbox_note", "attack_type")), UFD_CONTEXT_PATTERNS)


def _load_expanded() -> tuple[list[ExpandedMove], list[dict[str, Any]], dict[str, Any]]:
    client = get_client()
    characters = (
        client.table("char_slug_map")
        .select("capcom_slug,sc_chara")
        .order("capcom_slug")
        .limit(100)
        .execute()
        .data
        or []
    )
    expanded: list[ExpandedMove] = []
    all_capcom: list[dict[str, Any]] = []
    mapping_methods: Counter[str] = Counter()
    for character in characters:
        slug = str(character["capcom_slug"])
        sc_character = str(character["sc_chara"])
        capcom_rows = (
            client.table("move_latest")
            .select(CAPCOM_COLUMNS)
            .eq("character_slug", slug)
            .limit(500)
            .execute()
            .data
            or []
        )
        sc_rows = (
            client.table("sc_moves")
            .select(SC_COLUMNS)
            .eq("chara", sc_character)
            .limit(500)
            .execute()
            .data
            or []
        )
        map_rows = (
            client.table("special_move_map")
            .select("capcom_move_name,sc_input,match_method")
            .eq("capcom_slug", slug)
            .limit(500)
            .execute()
            .data
            or []
        )
        all_capcom.extend({"character_slug": slug, **dict(row)} for row in capcom_rows)
        sc_by_input = {
            str(row["input"]): row for row in sc_rows if _present(row.get("input"))
        }
        mapped_by_name = {
            str(row["capcom_move_name"]): row for row in map_rows
        }
        for capcom in capcom_rows:
            input_name = _capcom_normal_input(str(capcom.get("move_name") or ""))
            method = "fixed_normal_name"
            if not input_name:
                mapped = mapped_by_name.get(str(capcom.get("move_name") or ""))
                if not mapped:
                    continue
                input_name = str(mapped.get("sc_input") or "")
                method = "special_move_map:" + str(mapped.get("match_method") or "unknown")
            sc = sc_by_input.get(input_name)
            if not sc:
                continue
            mapping_methods[method] += 1
            expanded.append(ExpandedMove(
                character_slug=slug,
                sc_character=sc_character,
                capcom=capcom,
                sc=sc,
                input=input_name,
                mapping_method=method,
            ))
    return expanded, all_capcom, {
        "characters": len(characters),
        "mapped_rows": len(expanded),
        "mapping_methods": dict(mapping_methods),
        "note": (
            "fixed_normal_name rows are leakage-free; special_move_map rows use an "
            "existing SC-assisted correspondence only for metadata alignment."
        ),
    }


def _capcom_note_text(move: ExpandedMove) -> str:
    return str(move.capcom.get("note") or "")


def _capcom_note_lines(move: ExpandedMove) -> list[str]:
    return [line.strip() for line in _capcom_note_text(move).splitlines() if line.strip()]


def _is_self_airborne_line(line: str) -> bool:
    """Return whether a line claims the actor is airborne.

    ``空中判定の打撃...に対して無敵`` describes the attack class against
    which a move is invulnerable, not the actor's own airborne state.  It is
    therefore excluded before applying the positive grammar.
    """

    return bool(
        CAPCOM_SELF_AIRBORNE_PATTERN.search(line)
        and not CAPCOM_AIR_INVULNERABILITY_CONTEXT_PATTERN.search(line)
    )


def _sc_actual_armor(move: ExpandedMove) -> bool:
    """Distinguish actual armor windows from the SC ``Break`` capability.

    Numeric/range values and ``release`` are treated as actual armor.  A value
    such as ``Break; 1-23`` is intentionally positive for both actual armor
    and armor break; a plain ``Break`` is not actual armor.
    """

    armor = str(move.sc.get("armor") or "")
    return bool(_present(armor) and SC_ACTUAL_ARMOR_VALUE_PATTERN.search(armor))


def _sc_nondefault_juggle_tuple(move: ExpandedMove) -> bool:
    """Use any non-empty juggle component other than scalar 0/1 as positive."""

    values = [
        str(move.sc.get(field) or "").strip()
        for field in ("jug_start", "jug_increase", "jug_limit")
    ]
    return any(value and value not in {"0", "1"} for value in values)


def _binary_metric(
    moves: Sequence[ExpandedMove],
    label: Callable[[ExpandedMove], bool],
    prediction: Callable[[ExpandedMove], bool],
) -> dict[str, Any]:
    """Return the full confusion matrix over every mapped row.

    Precision and recall use SC as a comparison label, not as a runtime input:
    ``precision = TP / (TP + FP)`` and ``recall = TP / (TP + FN)``.
    Rows without a CAPCOM keyword and without an SC label remain true
    negatives; the denominator is never restricted to rows with notes.
    """

    pairs = [(bool(label(move)), bool(prediction(move))) for move in moves]
    true_positive = sum(truth and predicted for truth, predicted in pairs)
    false_positive = sum(not truth and predicted for truth, predicted in pairs)
    false_negative = sum(truth and not predicted for truth, predicted in pairs)
    true_negative = len(pairs) - true_positive - false_positive - false_negative
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    f1_denominator = 2 * true_positive + false_positive + false_negative
    return {
        "mapped_rows_denominator": len(pairs),
        "sc_positive": recall_denominator,
        "capcom_positive": precision_denominator,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": (
            true_positive / precision_denominator
            if precision_denominator else None
        ),
        "recall": (
            true_positive / recall_denominator
            if recall_denominator else None
        ),
        "f1": (
            2 * true_positive / f1_denominator if f1_denominator else None
        ),
        "accuracy": (
            (true_positive + true_negative) / len(pairs) if pairs else None
        ),
    }


def _closed_frame_ranges(text: str) -> set[tuple[int, int]]:
    return {
        (int(match.group("start")), int(match.group("end")))
        for match in CLOSED_FRAME_RANGE_PATTERN.finditer(text)
    }


def _interval_agreement(
    moves: Sequence[ExpandedMove],
    label: Callable[[ExpandedMove], bool],
    sc_field: str,
    capcom_line: Callable[[str], bool],
) -> dict[str, Any]:
    """Measure row-level closed-interval overlap for SC-positive rows.

    A row is an interval match when at least one ``(start, end)`` pair from a
    relevant CAPCOM note line equals one pair parsed from the SC field.  This
    is deliberately weaker than equality of every interval: type labels,
    ordering, open-ended windows, parenthetical variants, and FKD qualifiers
    are not compared by this diagnostic.
    """

    labelled = [move for move in moves if label(move)]
    documented = 0
    matched = 0
    for move in labelled:
        capcom_ranges: set[tuple[int, int]] = set()
        for line in _capcom_note_lines(move):
            if capcom_line(line):
                capcom_ranges.update(_closed_frame_ranges(line))
        if not capcom_ranges:
            continue
        documented += 1
        sc_ranges = _closed_frame_ranges(str(move.sc.get(sc_field) or ""))
        if capcom_ranges & sc_ranges:
            matched += 1
    return {
        "sc_positive": len(labelled),
        "capcom_closed_range": documented,
        "capcom_closed_range_coverage": (
            documented / len(labelled) if labelled else None
        ),
        "at_least_one_exact_interval": matched,
        "exact_interval_rate_among_documented": (
            matched / documented if documented else None
        ),
    }


def _refined_metadata_benchmarks(
    expanded: Sequence[ExpandedMove],
) -> dict[str, Any]:
    """Reproduce the report's refined metadata precision/recall table."""

    high_confidence = [
        move
        for move in expanded
        if move.mapping_method in HIGH_CONFIDENCE_MAPPING_METHODS
    ]

    metric_specs: dict[str, dict[str, Any]] = {
        "invulnerability_any": {
            "label": lambda move: _present(move.sc.get("invuln")),
            "prediction": lambda move: "無敵" in _capcom_note_text(move),
            "label_definition": "SC invuln is present",
            "prediction_definition": "CAPCOM note contains the literal 無敵",
            "prediction_regex": "無敵",
        },
        "airborne_self": {
            "label": lambda move: _present(move.sc.get("airborne")),
            "prediction": lambda move: any(
                _is_self_airborne_line(line) for line in _capcom_note_lines(move)
            ),
            "label_definition": "SC airborne is present",
            "prediction_definition": (
                "a CAPCOM note line matches the self-airborne grammar after "
                "excluding 空中判定の打撃 invulnerability context"
            ),
            "prediction_regex": CAPCOM_SELF_AIRBORNE_PATTERN.pattern,
            "exclusion_regex": CAPCOM_AIR_INVULNERABILITY_CONTEXT_PATTERN.pattern,
        },
        "actual_armor_candidate": {
            "label": _sc_actual_armor,
            "prediction": lambda move: any(
                CAPCOM_ARMOR_CANDIDATE_PATTERN.search(line)
                for line in _capcom_note_lines(move)
            ),
            "label_definition": (
                "SC armor is present and contains a digit or case-insensitive release; "
                "plain Break is excluded"
            ),
            "prediction_definition": (
                "a CAPCOM note line contains an armor candidate token; actor and "
                "armor-hit semantics are intentionally not resolved at this stage"
            ),
            "prediction_regex": CAPCOM_ARMOR_CANDIDATE_PATTERN.pattern,
            "warning": (
                "This candidate grammar can match アーマーヒット and the アーマー "
                "prefix of アーマーブレイク; its precision is not executable-rule precision."
            ),
        },
        "armor_break_note": {
            "label": lambda move: bool(re.search(
                r"break", str(move.sc.get("armor") or ""), re.IGNORECASE
            )),
            "prediction": lambda move: bool(
                CAPCOM_ARMOR_BREAK_PATTERN.search(_capcom_note_text(move))
            ),
            "label_definition": "SC armor contains case-insensitive Break",
            "prediction_definition": "CAPCOM note explicitly says armor break",
            "prediction_regex": CAPCOM_ARMOR_BREAK_PATTERN.pattern,
        },
        "projectile_with_sc_speed": {
            "label": lambda move: _present(move.sc.get("proj_speed")),
            "prediction": lambda move: "弾" in str(move.capcom.get("attribute") or ""),
            "label_definition": "SC proj_speed is present (a projectile proxy label)",
            "prediction_definition": "CAPCOM attribute contains the literal 弾",
            "prediction_regex": "弾",
            "warning": (
                "SC proj_speed presence is not an exhaustive truth set for every "
                "projectile; this metric tests the numeric-speed subset."
            ),
        },
        "cancel_presence": {
            "label": lambda move: _present(move.sc.get("cancel")),
            "prediction": lambda move: _present(move.capcom.get("cancel")),
            "label_definition": "SC cancel is present",
            "prediction_definition": "CAPCOM cancel cell is present",
            "prediction_regex": None,
        },
        "atk_range_presence_from_note": {
            "label": lambda move: _present(move.sc.get("atk_range")),
            "prediction": lambda move: bool(
                CAPCOM_RANGE_CANDIDATE_PATTERN.search(_capcom_note_text(move))
            ),
            "label_definition": "SC atk_range is present",
            "prediction_definition": "CAPCOM note contains a distance/range token",
            "prediction_regex": CAPCOM_RANGE_CANDIDATE_PATTERN.pattern,
            "warning": "Keyword presence does not recover the numeric range value.",
        },
        "nondefault_juggle_tuple_from_note": {
            "label": _sc_nondefault_juggle_tuple,
            "prediction": lambda move: bool(
                CAPCOM_JUGGLE_CANDIDATE_PATTERN.search(_capcom_note_text(move))
            ),
            "label_definition": (
                "at least one of SC jug_start/jug_increase/jug_limit is non-empty "
                "and not exactly scalar 0 or 1"
            ),
            "prediction_definition": "CAPCOM note contains a juggle/air-result token",
            "prediction_regex": CAPCOM_JUGGLE_CANDIDATE_PATTERN.pattern,
            "warning": "Keyword presence does not recover the numeric juggle tuple.",
        },
    }

    definitions = {
        name: {
            key: value
            for key, value in spec.items()
            if key not in {"label", "prediction"}
        }
        for name, spec in metric_specs.items()
    }

    def run(population: Sequence[ExpandedMove]) -> dict[str, Any]:
        return {
            name: _binary_metric(population, spec["label"], spec["prediction"])
            for name, spec in metric_specs.items()
        }

    interval_specs: dict[str, dict[str, Any]] = {
        "invulnerability": {
            "label": metric_specs["invulnerability_any"]["label"],
            "sc_field": "invuln",
            "capcom_line": lambda line: "無敵" in line,
            "capcom_line_definition": "note line contains 無敵",
        },
        "airborne_self": {
            "label": metric_specs["airborne_self"]["label"],
            "sc_field": "airborne",
            "capcom_line": _is_self_airborne_line,
            "capcom_line_definition": (
                "note line matches self-airborne grammar and not the air-invulnerability exclusion"
            ),
        },
        "actual_armor": {
            "label": _sc_actual_armor,
            "sc_field": "armor",
            "capcom_line": lambda line: "アーマー" in line and "ブレイク" not in line,
            "capcom_line_definition": "note line contains アーマー and not ブレイク",
        },
    }

    def run_intervals(population: Sequence[ExpandedMove]) -> dict[str, Any]:
        return {
            name: {
                "sc_field": spec["sc_field"],
                "capcom_line_definition": spec["capcom_line_definition"],
                **_interval_agreement(
                    population,
                    spec["label"],
                    spec["sc_field"],
                    spec["capcom_line"],
                ),
            }
            for name, spec in interval_specs.items()
        }

    return {
        "methodology": {
            "unit": "one CAPCOM-to-SC mapped row",
            "all_mapping_denominator": len(expanded),
            "high_confidence_mapping_denominator": len(high_confidence),
            "high_confidence_mapping_methods": sorted(HIGH_CONFIDENCE_MAPPING_METHODS),
            "precision_formula": "TP / (TP + FP)",
            "recall_formula": "TP / (TP + FN)",
            "negative_rows": (
                "all mapped rows not satisfying the SC label or CAPCOM predicate; "
                "the population is not filtered to rows with notes"
            ),
            "mapping_warning": (
                "special_move_map was originally SC-assisted. These are aligned-row "
                "coverage diagnostics, not an independent move-identity benchmark."
            ),
            "closed_interval_regex": CLOSED_FRAME_RANGE_PATTERN.pattern,
            "interval_match_rule": (
                "at least one identical (start,end) pair; not equality of all windows, "
                "types, qualifiers, or open-ended intervals"
            ),
        },
        "metric_definitions": definitions,
        "all_mappings": run(expanded),
        "high_confidence_mapping_sensitivity": run(high_confidence),
        "closed_interval_agreement": {
            "all_mappings": run_intervals(expanded),
            "high_confidence_mapping_sensitivity": run_intervals(high_confidence),
        },
        "explicit_numeric_evidence": {
            "mapped_rows_denominator": len(expanded),
            "capcom_notes_with_projectile_speed_word": sum(
                bool(CAPCOM_PROJECTILE_SPEED_PATTERN.search(_capcom_note_text(move)))
                for move in expanded
            ),
            "projectile_speed_regex": CAPCOM_PROJECTILE_SPEED_PATTERN.pattern,
            "capcom_notes_with_range_candidate": sum(
                bool(CAPCOM_RANGE_CANDIDATE_PATTERN.search(_capcom_note_text(move)))
                for move in expanded
            ),
            "range_regex": CAPCOM_RANGE_CANDIDATE_PATTERN.pattern,
            "capcom_notes_with_juggle_numeric_word": sum(
                bool(CAPCOM_JUGGLE_NUMERIC_PATTERN.search(_capcom_note_text(move)))
                for move in expanded
            ),
            "juggle_numeric_regex": CAPCOM_JUGGLE_NUMERIC_PATTERN.pattern,
        },
    }


def _inventory(all_capcom: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tag_counts: Counter[str] = Counter()
    tag_examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    attributes: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    notes_nonempty = 0
    note_texts: Counter[str] = Counter()
    multiline_notes = 0
    attributes_nonempty = 0
    recovery_claim_rows = 0
    recovery_claims = 0
    recovery_claim_outcomes: Counter[str] = Counter()
    tags_with_explicit_window: Counter[str] = Counter()
    for row in all_capcom:
        sections[str(row.get("section") or "unknown")] += 1
        note = str(row.get("note") or "").strip()
        attribute = str(row.get("attribute") or "").strip()
        if note:
            notes_nonempty += 1
            note_texts[note] += 1
            if "\n" in note:
                multiline_notes += 1
        if attribute:
            attributes_nonempty += 1
            attributes[attribute] += 1
        claims = _capcom_recovery_claims(row)
        if claims:
            recovery_claim_rows += 1
            recovery_claims += len(claims)
            for claim in claims:
                recovery_claim_outcomes.update(claim["outcomes"])
        row_tags = _capcom_tags(row)
        for tag in row_tags:
            tag_counts[tag] += 1
            if len(tag_examples[tag]) < 12:
                tag_examples[tag].append({
                    "character_slug": str(row.get("character_slug") or ""),
                    "move_name": str(row.get("move_name") or ""),
                    "note": note,
                    "attribute": attribute,
                })
        for line in note.splitlines():
            if not EXPLICIT_FRAME_WINDOW_PATTERN.search(line):
                continue
            line_tags = _tags(line, CAPCOM_CONTEXT_PATTERNS)
            tags_with_explicit_window.update(line_tags)
    total = len(all_capcom)
    return {
        "rows": total,
        "rows_with_note": notes_nonempty,
        "note_coverage_rate": notes_nonempty / total if total else None,
        "unique_note_texts": len(note_texts),
        "multiline_note_rows": multiline_notes,
        "explicit_recovery_claim_rows": recovery_claim_rows,
        "explicit_recovery_claims": recovery_claims,
        "explicit_recovery_claim_outcomes": dict(recovery_claim_outcomes),
        "rows_with_attribute": attributes_nonempty,
        "attribute_coverage_rate": attributes_nonempty / total if total else None,
        "sections": dict(sections),
        "tag_counts": dict(tag_counts),
        "tag_lines_with_explicit_frame_window": dict(tags_with_explicit_window),
        "tag_examples": dict(tag_examples),
        "attribute_values": dict(attributes.most_common()),
    }


def _comparison(
    expanded: Sequence[ExpandedMove],
    concept: str,
    sc_positive: Callable[[ExpandedMove], bool],
) -> dict[str, Any]:
    cap_positive = [move for move in expanded if concept in _capcom_tags(move.capcom)]
    sc_rows = [move for move in expanded if sc_positive(move)]
    overlap = [move for move in cap_positive if sc_positive(move)]
    cap_only = [move for move in cap_positive if not sc_positive(move)]
    sc_only = [move for move in sc_rows if concept not in _capcom_tags(move.capcom)]

    def samples(rows: Sequence[ExpandedMove]) -> list[dict[str, Any]]:
        return [{
            "character_slug": move.character_slug,
            "move_name": move.capcom.get("move_name"),
            "input": move.input,
            "capcom_note": move.capcom.get("note"),
            "capcom_attribute": move.capcom.get("attribute"),
            "sc_value": {
                "invulnerability": move.sc.get("invuln"),
                "armor_state": move.sc.get("armor"),
                "armor_break": move.sc.get("armor"),
                "airborne_state": move.sc.get("airborne"),
                "projectile": move.sc.get("proj_speed"),
            }.get(concept),
            "sc_notes": move.sc.get("notes"),
            "mapping_method": move.mapping_method,
        } for move in rows[:20]]

    return {
        "mapped_rows": len(expanded),
        "capcom_mentions": len(cap_positive),
        "sc_explicit_or_note_labels": len(sc_rows),
        "overlap": len(overlap),
        "precision_against_sc": len(overlap) / len(cap_positive) if cap_positive else None,
        "recall_against_sc": len(overlap) / len(sc_rows) if sc_rows else None,
        "capcom_only": len(cap_only),
        "sc_only": len(sc_only),
        "capcom_only_samples": samples(cap_only),
        "sc_only_samples": samples(sc_only),
    }


def _metadata_comparisons(expanded: Sequence[ExpandedMove]) -> dict[str, Any]:
    return {
        "invulnerability": _comparison(
            expanded,
            "invulnerability",
            lambda move: _present(move.sc.get("invuln")),
        ),
        "armor_state": _comparison(
            expanded,
            "armor_state",
            lambda move: _present(move.sc.get("armor"))
            and not re.search(r"break", str(move.sc.get("armor")), re.IGNORECASE),
        ),
        "armor_break": _comparison(
            expanded,
            "armor_break",
            lambda move: bool(re.search(
                r"break", str(move.sc.get("armor") or ""), re.IGNORECASE
            )),
        ),
        "airborne_state": _comparison(
            expanded,
            "airborne_state",
            lambda move: _present(move.sc.get("airborne")),
        ),
        "projectile": _comparison(
            expanded,
            "projectile",
            lambda move: _present(move.sc.get("proj_speed"))
            or "projectile" in str(move.sc.get("notes") or "").casefold(),
        ),
    }


def _float_value(value: object) -> float | None:
    if value is None:
        return None
    match = re.search(r"[+-]?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _numeric_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": max(values),
    }


def _context_record(move: AuditMove) -> dict[str, Any]:
    return {
        "capcom_note": (move.capcom or {}).get("note"),
        "capcom_attribute": (move.capcom or {}).get("attribute"),
        "capcom_cancel": (move.capcom or {}).get("cancel"),
        "capcom_tags": sorted(_capcom_tags(move.capcom)),
        "ufd_notes": (move.ufd or {}).get("notes"),
        "ufd_hitbox_note": (move.ufd or {}).get("hitbox_note"),
        "ufd_attack_type": (move.ufd or {}).get("attack_type"),
        "ufd_tags": sorted(_ufd_tags(move.ufd)),
        "sc_recovery_raw": move.sc.get("recovery"),
        "sc_active_raw": move.sc.get("active"),
        "sc_hit_adv_raw": move.sc.get("hit_adv"),
        "sc_block_adv_raw": move.sc.get("block_adv"),
        "sc_hitstop": move.sc.get("hitstop"),
        "sc_atk_range": move.sc.get("atk_range"),
        "sc_invuln": move.sc.get("invuln"),
        "sc_armor": move.sc.get("armor"),
        "sc_airborne": move.sc.get("airborne"),
        "sc_juggle": {
            "start": move.sc.get("jug_start"),
            "increase": move.sc.get("jug_increase"),
            "limit": move.sc.get("jug_limit"),
        },
        "sc_proj_speed": move.sc.get("proj_speed"),
        "sc_notes": move.sc.get("notes"),
        "sc_tags": sorted(_sc_tags(move.sc)),
    }


def _causes(
    move: AuditMove,
    target: str,
    source_features: Mapping[str, int],
    sc_features: Mapping[str, int | None],
) -> list[str]:
    causes: list[str] = []
    raw_recovery = str(move.sc.get("recovery") or "")
    if raw_recovery and _strict_int(raw_recovery) is None:
        causes.append("supercombo_conditional_or_composite_recovery")
    if not raw_recovery and target in {"hitstun", "blockstun", "total"}:
        causes.append("supercombo_recovery_missing_but_target_present")
    if "conditional_recovery" in _capcom_tags(move.capcom):
        causes.append("capcom_note_has_condition_or_recovery_text")
    comparable = [
        field for field, value in source_features.items()
        if sc_features.get(field) is not None and sc_features.get(field) != value
    ]
    if comparable:
        causes.append("capcom_and_supercombo_base_values_differ:" + ",".join(comparable))
    if source_features and all(
        sc_features.get(field) == value for field, value in source_features.items()
    ):
        causes.append("formula_exception_or_contact_convention")
    context_tags = _capcom_tags(move.capcom) | _sc_tags(move.sc) | _ufd_tags(move.ufd)
    for tag in (
        "distance_or_contact", "invulnerability", "armor_state", "armor_break",
        "projectile", "juggle_or_air_result", "airborne_state", "install_or_stance",
    ):
        if tag in context_tags:
            causes.append("context_present:" + tag)
    if not causes:
        causes.append("unresolved_source_version_or_hidden_condition")
    return causes


def _official_note_recovery_fit(
    move: AuditMove,
    target: str,
    label: int,
    predicted: int,
) -> dict[str, Any]:
    """Measure whether an explicit official recovery claim explains an error.

    ``direct`` means the listed recovery is treated as an unmodified baseline.
    ``inverse`` means the listed scalar appears to be the named conditional
    branch (for example Cammy 5HK's whiff recovery), so the other outcome is
    recovered by subtracting that claim.  The latter is diagnostic evidence,
    not safe enough to become a runtime rule without a reviewed branch schema.
    """

    outcome = {"hitstun": "hit", "blockstun": "block"}.get(target)
    claims = _capcom_recovery_claims(move.capcom)
    if not outcome or not claims:
        return {
            "status": "no_explicit_claim",
            "claims": claims,
            "required_recovery_delta": None,
            "best_error": abs(predicted - label),
        }
    required_delta = label - predicted
    candidates: list[dict[str, Any]] = []
    for claim in claims:
        delta = int(claim["delta"])
        if outcome in claim["outcomes"]:
            candidates.append({
                "mode": "direct",
                "applied_delta": delta,
                "prediction": predicted + delta,
                "claim": claim,
            })
        elif outcome not in claim["outcomes"]:
            candidates.append({
                "mode": "inverse",
                "applied_delta": -delta,
                "prediction": predicted - delta,
                "claim": claim,
            })
    for candidate in candidates:
        candidate["error"] = int(candidate["prediction"]) - label
    best = min(candidates, key=lambda candidate: abs(int(candidate["error"])))
    raw_error = abs(predicted - label)
    best_error = abs(int(best["error"]))
    if best_error == 0:
        status = "exact_compatible"
    elif best_error < raw_error:
        status = "partial_improvement"
    else:
        status = "not_explanatory"
    return {
        "status": status,
        "claims": claims,
        "required_recovery_delta": required_delta,
        "best_error": best_error,
        "best_candidate": best,
        "warning": (
            "inverse compatibility does not identify the scalar cell's base "
            "branch and must not be executed without reviewed semantics"
            if best.get("mode") == "inverse"
            else None
        ),
    }


def _mismatch_analysis(moves: Sequence[AuditMove], target: str) -> dict[str, Any]:
    exact_rows: list[tuple[AuditMove, int]] = []
    mismatches: list[dict[str, Any]] = []
    cause_counts: Counter[str] = Counter()
    group_tags: dict[str, Counter[str]] = {
        "exact": Counter(),
        "mismatch": Counter(),
    }
    group_hitstop: dict[str, list[float]] = {"exact": [], "mismatch": []}
    group_range: dict[str, list[float]] = {"exact": [], "mismatch": []}
    predictable = 0
    official_note_fit_counts: Counter[str] = Counter()
    for move in moves:
        label = _strict_int(move.sc.get(target))
        if label is None or not move.capcom:
            continue
        prediction = _predict(move.capcom, "capcom", target)
        if not prediction:
            continue
        predictable += 1
        predicted, source_features = prediction
        error = predicted - label
        group = "exact" if error == 0 else "mismatch"
        for tag in _capcom_tags(move.capcom) | _sc_tags(move.sc) | _ufd_tags(move.ufd):
            group_tags[group][tag] += 1
        hitstop = _float_value(move.sc.get("hitstop"))
        atk_range = _float_value(move.sc.get("atk_range"))
        if hitstop is not None:
            group_hitstop[group].append(hitstop)
        if atk_range is not None:
            group_range[group].append(atk_range)
        if error == 0:
            exact_rows.append((move, predicted))
            continue
        sc_features = {
            field: _feature(move.sc, "supercombo", field)
            for field in FORMULA_FEATURES[target]
        }
        causes = _causes(move, target, source_features, sc_features)
        official_note_fit = _official_note_recovery_fit(
            move, target, label, predicted
        )
        official_note_fit_counts[official_note_fit["status"]] += 1
        cause_counts.update(causes)
        mismatches.append({
            "character_slug": move.character_slug,
            "sc_character": move.sc_character,
            "input": move.input,
            "move_name_capcom": (move.capcom or {}).get("move_name"),
            "move_name_sc": move.sc.get("name"),
            "target": label,
            "predicted": predicted,
            "error": error,
            "source_features": source_features,
            "supercombo_features": sc_features,
            "causes": causes,
            "official_note_recovery_fit": official_note_fit,
            "context": _context_record(move),
        })
    exact_count = len(exact_rows)
    mismatch_count = len(mismatches)
    tag_comparison: dict[str, Any] = {}
    all_tags = sorted(set(group_tags["exact"]) | set(group_tags["mismatch"]))
    for tag in all_tags:
        exact_tag = group_tags["exact"][tag]
        mismatch_tag = group_tags["mismatch"][tag]
        exact_rate = exact_tag / exact_count if exact_count else None
        mismatch_rate = mismatch_tag / mismatch_count if mismatch_count else None
        tag_comparison[tag] = {
            "exact_count": exact_tag,
            "exact_rate": exact_rate,
            "mismatch_count": mismatch_tag,
            "mismatch_rate": mismatch_rate,
            "risk_ratio": (
                mismatch_rate / exact_rate
                if exact_rate not in {None, 0} and mismatch_rate is not None
                else None
            ),
        }
    return {
        "formula": {
            "hitstun": "active + recovery + on_hit",
            "blockstun": "active + recovery + on_block",
            "total": "startup + active + recovery - 1",
        }[target],
        "predictable": predictable,
        "exact": exact_count,
        "mismatch": mismatch_count,
        "cause_counts": dict(cause_counts),
        "official_note_recovery_fit_counts": dict(official_note_fit_counts),
        "tag_prevalence": tag_comparison,
        "hitstop": {
            "exact": _numeric_summary(group_hitstop["exact"]),
            "mismatch": _numeric_summary(group_hitstop["mismatch"]),
        },
        "atk_range": {
            "exact": _numeric_summary(group_range["exact"]),
            "mismatch": _numeric_summary(group_range["mismatch"]),
        },
        "mismatches": mismatches,
    }


def _juggle_inventory(expanded: Sequence[ExpandedMove]) -> dict[str, Any]:
    fields = ("jug_start", "jug_increase", "jug_limit")
    values = {field: Counter() for field in fields}
    cap_mentions = 0
    for move in expanded:
        for field in fields:
            if _present(move.sc.get(field)):
                values[field][str(move.sc.get(field))] += 1
        if "juggle_or_air_result" in _capcom_tags(move.capcom):
            cap_mentions += 1
    return {
        "mapped_rows": len(expanded),
        "capcom_notes_with_juggle_or_air_result_language": cap_mentions,
        "supercombo_value_counts": {
            field: dict(counter.most_common()) for field, counter in values.items()
        },
        "interpretation": (
            "CAPCOM prose can describe a result state, but it does not expose the "
            "numeric juggle start/increase/limit tuple used by SC."
        ),
    }


def build_report() -> dict[str, Any]:
    strict_moves, strict_sources = _load_moves()
    expanded, all_capcom, expanded_scope = _load_expanded()
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "purpose": (
            "Measure what CAPCOM official notes/attributes can replace and explain the "
            "remaining temporal mismatches without using SC as a runtime feature."
        ),
        "scope": {
            "leakage_free_standard_ground_normals": len(strict_moves),
            "strict_sources": strict_sources,
            "expanded_metadata_alignment": expanded_scope,
        },
        "capcom_note_inventory": _inventory(all_capcom),
        "cross_source_metadata_coverage": _metadata_comparisons(expanded),
        "refined_metadata_benchmarks": _refined_metadata_benchmarks(expanded),
        "juggle_inventory": _juggle_inventory(expanded),
        "temporal_mismatch_analysis": {
            target: _mismatch_analysis(strict_moves, target)
            for target in ("hitstun", "blockstun", "total")
        },
        "interpretation_limits": [
            "A note keyword is evidence text, not yet a frame-indexed rule; exact frame ranges must be parsed and reviewed.",
            "SC armor='Break' describes armor-breaking capability and is separated from a move having armor.",
            "Range, invulnerability, armor, projectile, juggle, and airborne state govern collision/result validity; they do not by themselves change the hitstun identity.",
            "special_move_map is used only for the expanded metadata comparison and was originally SC-assisted; fixed-name normal mismatch analysis remains source-isolated.",
            "Different source snapshots can make a correct physical formula disagree with the chosen SC label.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
