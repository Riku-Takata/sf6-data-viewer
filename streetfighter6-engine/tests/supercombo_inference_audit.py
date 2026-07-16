"""Audit whether SuperCombo temporal fields can be derived independently.

The current sequence engine reads ``hitstun`` and ``blockstun`` from
SuperCombo.  For a simple direct strike that connects on its first active
frame, both values should be redundant with the ordinary frame table:

``hitstun   = active_duration + recovery + on_hit``
``blockstun = active_duration + recovery + on_block``
``total     = startup + active_duration + recovery - 1``

This audit treats SuperCombo values as labels and uses only CAPCOM or UFD
values as prediction inputs.  The primary benchmark is deliberately limited
to the twelve standard ground normals.  Their cross-source identity is
derived from the move name (for example, ``立ち中P`` / ``Standing Medium
Punch`` -> ``5MP``), so neither SuperCombo frame values nor frame-signature
matching can leak into the prediction.

Usage:
  PYTHONPATH=src ./.venv312/bin/python tests/supercombo_inference_audit.py
  PYTHONPATH=src ./.venv312/bin/python tests/supercombo_inference_audit.py \
      --output tests/supercombo_inference_results.json
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sf6_engine.db import get_client
from sf6_engine.frame_data import (
    _capcom_normal_input,
    _parse_capcom_active,
    _parse_duration,
    _parse_frame_value,
    _parse_recovery,
)


GROUND_NORMAL_INPUT = re.compile(r"^[25][LMH][PK]$")
SC_COLUMNS = (
    "input,name,move_type,guard,damage,cancel,startup,active,recovery,total,hit_adv,block_adv,"
    "punish_adv,perf_parry_adv,after_dr_hit,after_dr_blk,hitstun,blockstun,"
    "hitstop,atk_range,invuln,armor,airborne,jug_start,jug_increase,jug_limit,"
    "proj_speed,notes,imported_at"
)
CAPCOM_COLUMNS = (
    "move_name,section,startup,active,recovery,on_hit,on_block,cancel,damage,"
    "attribute,note,patch_date"
)
UFD_COLUMNS = (
    "move_name,category,sc_input,startup,active,recovery,total,on_hit,on_block,damage,"
    "attack_type,cancellable,notes,hitbox_note,hitbox_source_url,scraped_at"
)
UFD_GROUND_NORMAL_INPUTS = {
    "Standing Light Punch": "5LP",
    "Standing Medium Punch": "5MP",
    "Standing Heavy Punch": "5HP",
    "Standing Light Kick": "5LK",
    "Standing Medium Kick": "5MK",
    "Standing Heavy Kick": "5HK",
    "Crouching Light Punch": "2LP",
    "Crouching Medium Punch": "2MP",
    "Crouching Heavy Punch": "2HP",
    "Crouching Light Kick": "2LK",
    "Crouching Medium Kick": "2MK",
    "Crouching Heavy Kick": "2HK",
}
FORMULA_FEATURES = {
    "hitstun": ("active", "recovery", "on_hit"),
    "blockstun": ("active", "recovery", "on_block"),
    "total": ("startup", "active", "recovery"),
    "punish_adv": ("on_hit",),
    "perf_parry_adv": ("active", "recovery"),
    "after_dr_hit": ("on_hit",),
    "after_dr_blk": ("on_block",),
}


@dataclass(frozen=True)
class AuditMove:
    """One leakage-free standard-normal correspondence."""

    character_slug: str
    sc_character: str
    input: str
    sc: Mapping[str, Any]
    capcom: Mapping[str, Any] | None
    ufd: Mapping[str, Any] | None


def _strict_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        return None
    match = re.fullmatch(r"\s*([+-]?\d+)\s*F?\s*", str(value))
    return int(match.group(1)) if match else None


def _scalar(parsed: Mapping[str, Any] | None) -> int | None:
    if not parsed or not parsed.get("usable"):
        return None
    if parsed.get("semantic") != "scalar" or parsed.get("conditional"):
        return None
    value = parsed.get("value")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _feature(row: Mapping[str, Any], source: str, field: str) -> int | None:
    raw_key = field
    if source == "supercombo":
        raw_key = {"on_hit": "hit_adv", "on_block": "block_adv"}.get(field, field)
    raw = row.get(raw_key)
    if field == "active":
        parsed = _parse_capcom_active(raw) if source == "capcom" else _parse_duration(raw)
    elif field == "recovery":
        parsed = _parse_recovery(raw)
    else:
        parsed = _parse_frame_value(raw, advantage=field in {"on_hit", "on_block"})
    return _scalar(parsed)


def _predict(row: Mapping[str, Any], source: str, target: str) -> tuple[int, dict[str, int]] | None:
    features: dict[str, int] = {}
    for field in FORMULA_FEATURES[target]:
        value = _feature(row, source, field)
        if value is None:
            return None
        features[field] = value
    if target == "hitstun":
        value = features["active"] + features["recovery"] + features["on_hit"]
    elif target == "blockstun":
        value = features["active"] + features["recovery"] + features["on_block"]
    elif target == "total":
        value = features["startup"] + features["active"] + features["recovery"] - 1
    elif target == "punish_adv":
        value = features["on_hit"] + 4
    elif target == "perf_parry_adv":
        value = 2 - features["active"] - features["recovery"]
    elif target == "after_dr_hit":
        value = features["on_hit"] + 4
    elif target == "after_dr_blk":
        value = features["on_block"] + 4
    else:  # pragma: no cover - FORMULA_FEATURES is closed above.
        raise ValueError(target)
    return value, features


def _unique_by_input(
    rows: Sequence[Mapping[str, Any]],
    input_for_row: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, Mapping[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        input_name = input_for_row(row)
        if input_name and GROUND_NORMAL_INPUT.fullmatch(input_name):
            grouped[input_name].append(row)
    return {
        input_name: candidates[0]
        for input_name, candidates in grouped.items()
        if len(candidates) == 1
    }


def _load_moves() -> tuple[list[AuditMove], dict[str, Any]]:
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
    moves: list[AuditMove] = []
    snapshots: dict[str, Counter[str]] = {
        "capcom_patch_dates": Counter(),
        "supercombo_imported_at": Counter(),
        "ufd_scraped_at": Counter(),
    }
    row_counts: Counter[str] = Counter()
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
        ufd_rows = (
            client.table("ufd_moves")
            .select(UFD_COLUMNS)
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
        row_counts.update({
            "capcom": len(capcom_rows),
            "ufd": len(ufd_rows),
            "supercombo": len(sc_rows),
        })
        for row in capcom_rows:
            if row.get("patch_date"):
                snapshots["capcom_patch_dates"][str(row["patch_date"])] += 1
        for row in sc_rows:
            if row.get("imported_at"):
                snapshots["supercombo_imported_at"][str(row["imported_at"])[:10]] += 1
        for row in ufd_rows:
            if row.get("scraped_at"):
                snapshots["ufd_scraped_at"][str(row["scraped_at"])[:10]] += 1

        capcom_by_input = _unique_by_input(
            capcom_rows,
            lambda row: _capcom_normal_input(str(row.get("move_name") or "")),
        )
        # Deliberately reconstruct the UFD input from its English normal name.
        # The stored sc_input is not used because some importer mappings are
        # validated against SuperCombo inputs.
        ufd_by_input = _unique_by_input(
            ufd_rows,
            lambda row: UFD_GROUND_NORMAL_INPUTS.get(str(row.get("move_name") or "")),
        )
        sc_by_input = _unique_by_input(
            [row for row in sc_rows if row.get("move_type") == "ground_normal"],
            lambda row: str(row.get("input") or "") or None,
        )
        for input_name, sc_row in sorted(sc_by_input.items()):
            moves.append(AuditMove(
                character_slug=slug,
                sc_character=sc_character,
                input=input_name,
                sc=sc_row,
                capcom=capcom_by_input.get(input_name),
                ufd=ufd_by_input.get(input_name),
            ))
    return moves, {
        "characters": len(characters),
        "row_counts": dict(row_counts),
        **{key: dict(value) for key, value in snapshots.items()},
    }


def _row_for_source(move: AuditMove, source: str, target: str) -> tuple[Mapping[str, Any], str] | None:
    if source == "supercombo":
        return move.sc, source
    if source == "capcom" and move.capcom:
        return move.capcom, source
    if source == "ufd" and move.ufd:
        return move.ufd, source
    if source == "capcom_then_ufd":
        if move.capcom and _predict(move.capcom, "capcom", target):
            return move.capcom, "capcom"
        if move.ufd and _predict(move.ufd, "ufd", target):
            return move.ufd, "ufd"
    return None


def _metric(moves: Sequence[AuditMove], target: str, source: str) -> dict[str, Any]:
    errors: list[int] = []
    eligible = 0
    matched_rows = 0
    predictable = 0
    aligned = 0
    aligned_exact = 0
    source_usage: Counter[str] = Counter()
    mismatch_reasons: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for move in moves:
        label = _strict_int(move.sc.get(target))
        if label is None:
            continue
        eligible += 1
        selected = _row_for_source(move, source, target)
        if not selected:
            continue
        row, actual_source = selected
        matched_rows += 1
        prediction = _predict(row, actual_source, target)
        if not prediction:
            continue
        predicted, features = prediction
        predictable += 1
        source_usage[actual_source] += 1
        error = predicted - label
        errors.append(error)

        sc_features = {
            field: _feature(move.sc, "supercombo", field)
            for field in FORMULA_FEATURES[target]
        }
        inputs_aligned = all(
            sc_features[field] is not None and sc_features[field] == value
            for field, value in features.items()
        )
        if inputs_aligned:
            aligned += 1
            aligned_exact += error == 0
        if error == 0:
            continue
        reason = "formula_mismatch_with_aligned_inputs" if inputs_aligned else "source_snapshot_or_measurement_diff"
        mismatch_reasons[reason] += 1
        if len(samples) < 20:
            samples.append({
                "character_slug": move.character_slug,
                "sc_character": move.sc_character,
                "input": move.input,
                "source": actual_source,
                "target": label,
                "predicted": predicted,
                "error": error,
                "source_features": features,
                "supercombo_features": sc_features,
                "reason": reason,
            })
    exact = sum(error == 0 for error in errors)
    within_one = sum(abs(error) <= 1 for error in errors)
    return {
        "formula": {
            "hitstun": "active + recovery + on_hit",
            "blockstun": "active + recovery + on_block",
            "total": "startup + active + recovery - 1",
            "punish_adv": "on_hit + 4",
            "perf_parry_adv": "2 - active - recovery",
            "after_dr_hit": "on_hit + 4",
            "after_dr_blk": "on_block + 4",
        }[target],
        "eligible_supercombo_labels": eligible,
        "matched_source_rows": matched_rows,
        "predictable": predictable,
        "coverage_rate": predictable / eligible if eligible else None,
        "exact": exact,
        "exact_rate": exact / predictable if predictable else None,
        "within_1f": within_one,
        "within_1f_rate": within_one / predictable if predictable else None,
        "mean_absolute_error_f": (
            sum(abs(error) for error in errors) / len(errors) if errors else None
        ),
        "max_absolute_error_f": max((abs(error) for error in errors), default=None),
        "error_distribution": dict(Counter(str(error) for error in errors)),
        "source_usage": dict(source_usage),
        "source_inputs_equal_supercombo": aligned,
        "aligned_exact": aligned_exact,
        "aligned_exact_rate": aligned_exact / aligned if aligned else None,
        "mismatch_reasons": dict(mismatch_reasons),
        "mismatch_samples": samples,
    }


def _hitstop_strength_metric(moves: Sequence[AuditMove]) -> dict[str, Any]:
    expected_by_strength = {"L": 9, "M": 11, "H": 13}
    errors: list[int] = []
    samples: list[dict[str, Any]] = []
    for move in moves:
        label = _strict_int(move.sc.get("hitstop"))
        match = re.fullmatch(r"[25]([LMH])[PK]", move.input)
        if label is None or not match:
            continue
        predicted = expected_by_strength[match.group(1)]
        error = predicted - label
        errors.append(error)
        if error and len(samples) < 20:
            samples.append({
                "character_slug": move.character_slug,
                "input": move.input,
                "target": label,
                "predicted": predicted,
                "error": error,
            })
    exact = sum(error == 0 for error in errors)
    return {
        "hypothesis": "light=9F, medium=11F, heavy=13F",
        "note": "This is a system-rule hypothesis, not an algebraic identity from frame totals.",
        "eligible": len(errors),
        "exact": exact,
        "exact_rate": exact / len(errors) if errors else None,
        "mean_absolute_error_f": (
            sum(abs(error) for error in errors) / len(errors) if errors else None
        ),
        "mismatch_samples": samples,
    }


def _predicted_hitstuns(
    moves: Sequence[AuditMove],
    source: str,
) -> dict[tuple[str, str], tuple[int, int, int | None]]:
    values: dict[tuple[str, str], tuple[int, int, int | None]] = {}
    for move in moves:
        label = _strict_int(move.sc.get("hitstun"))
        sc_startup = _strict_int(move.sc.get("startup"))
        selected = _row_for_source(move, source, "hitstun")
        if label is None or not selected:
            continue
        row, actual_source = selected
        prediction = _predict(row, actual_source, "hitstun")
        if prediction:
            values[(move.character_slug, move.input)] = (
                label,
                prediction[0],
                sc_startup,
            )
    return values


def _trade_metric(moves: Sequence[AuditMove], source: str) -> dict[str, Any]:
    values = _predicted_hitstuns(moves, source)
    defenders = [key for key, value in values.items() if value[2] == 4]
    errors: list[int] = []
    sagat_errors: list[int] = []
    sagat_samples: list[dict[str, Any]] = []
    for attacker_key, (attacker_label, attacker_predicted, _) in values.items():
        for defender_key in defenders:
            defender_label, defender_predicted, _ = values[defender_key]
            label = attacker_label - defender_label - 1
            predicted = attacker_predicted - defender_predicted - 1
            error = predicted - label
            errors.append(error)
            if attacker_key == ("sagat", "5MP"):
                sagat_errors.append(error)
                if len(sagat_samples) < 100:
                    sagat_samples.append({
                        "defender_character_slug": defender_key[0],
                        "defender_input": defender_key[1],
                        "target_advantage_f": label,
                        "predicted_advantage_f": predicted,
                        "error": error,
                    })
    exact = sum(error == 0 for error in errors)
    sagat_exact = sum(error == 0 for error in sagat_errors)
    return {
        "formula": "attacker_hitstun - defender_hitstun - 1",
        "scope": "all predictable standard ground normals versus SC-labelled 4F standard normals",
        "predictable_moves": len(values),
        "defender_4f_moves": len(defenders),
        "pairs": len(errors),
        "exact_pairs": exact,
        "exact_rate": exact / len(errors) if errors else None,
        "within_1f_rate": (
            sum(abs(error) <= 1 for error in errors) / len(errors) if errors else None
        ),
        "mean_absolute_error_f": (
            sum(abs(error) for error in errors) / len(errors) if errors else None
        ),
        "sagat_5mp_vs_4f": {
            "pairs": len(sagat_errors),
            "exact_pairs": sagat_exact,
            "exact_rate": sagat_exact / len(sagat_errors) if sagat_errors else None,
            "predicted_min_f": min(
                (row["predicted_advantage_f"] for row in sagat_samples),
                default=None,
            ),
            "predicted_max_f": max(
                (row["predicted_advantage_f"] for row in sagat_samples),
                default=None,
            ),
            "samples": sagat_samples,
        },
    }


def _non_temporal_coverage(moves: Sequence[AuditMove]) -> dict[str, Any]:
    return {
        "supercombo_atk_range_labels": sum(
            bool(str(move.sc.get("atk_range") or "").strip()) for move in moves
        ),
        "ufd_rows_with_hitbox_gif": sum(
            bool(move.ufd and move.ufd.get("hitbox_source_url")) for move in moves
        ),
        "supercombo_rows_with_notes": sum(
            bool(str(move.sc.get("notes") or "").strip()) for move in moves
        ),
        "interpretation": (
            "Range, collision geometry, move-state exceptions, and prose notes have no "
            "algebraic target in the scalar CAPCOM/UFD frame table. UFD GIF assets can "
            "support a separate calibrated geometry pipeline but are not frame totals."
        ),
    }


def build_report() -> dict[str, Any]:
    moves, sources = _load_moves()
    benchmark_sources = ("supercombo", "capcom", "ufd", "capcom_then_ufd")
    temporal = {
        source: {
            target: _metric(moves, target, source)
            for target in FORMULA_FEATURES
        }
        for source in benchmark_sources
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "purpose": (
            "Treat SuperCombo as the label and test whether ordinary CAPCOM/UFD frame "
            "facts reproduce its derived temporal fields without using SuperCombo values "
            "as prediction inputs."
        ),
        "scope": {
            "mapping_policy": (
                "Twelve standard ground normals only; correspondence comes from fixed "
                "Japanese/English move-name notation, never frame-signature matching."
            ),
            "moves": len(moves),
            "characters": len({move.character_slug for move in moves}),
            "sources": sources,
        },
        "temporal_field_benchmarks": temporal,
        "hitstop_strength_rule": _hitstop_strength_metric(moves),
        "post_trade_benchmarks": {
            source: _trade_metric(moves, source)
            for source in ("capcom", "ufd", "capcom_then_ufd")
        },
        "non_temporal_fields": _non_temporal_coverage(moves),
        "methodological_limits": [
            "The SuperCombo-internal control establishes algebraic redundancy but is not an independent-source test.",
            "CAPCOM, UFD, and SuperCombo snapshots are not guaranteed to represent the same patch; raw mismatches therefore combine inference error with source-version skew.",
            "The trade formula's final -1F and hitstop-cancellation policy require an independent in-game observation suite; reproducing a SuperCombo-derived label does not validate that physical model.",
            "The formulas apply to first-active-frame, simple direct strikes with scalar recovery/advantage. Multi-hit, projectile, late-active, knockdown, airborne, and state-dependent cases need separate rules.",
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
