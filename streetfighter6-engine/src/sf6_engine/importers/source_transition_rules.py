"""Build a review queue for SuperCombo composite move transitions.

The source table can describe a branch as ``A~B`` but its ``startup`` is often
relative to the branch input, not to the blocked parent move.  This command
enumerates every such row across all characters and promotes only direct
``Nf gap`` / ``true blockstring`` statements into *unreviewed* candidates.
The remaining rows are deliberately retained in the JSON review queue rather
than being guessed from their startup.

Usage:
  PYTHONPATH=src python -m sf6_engine.importers.source_transition_rules \
    /path/to/supercombo_sf6_YYYY-MM-DD.json --output /tmp/transition-candidates.json
  PYTHONPATH=src python -m sf6_engine.importers.source_transition_rules \
    /path/to/supercombo_sf6_YYYY-MM-DD.json --apply

``--apply`` requires ``source_transition_rules_migration.sql`` and only writes
direct-evidence candidates with ``reviewed=false``.  The runtime ignores those
rows until a human confirms the exact move, state and patch.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from sf6_engine.db import get_write_client
from sf6_engine.transition_rules import (
    input_family,
    is_composite_input,
    resolve_composite_transition_rule,
)


def _normalized_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_data = payload.get("data")
    if not isinstance(raw_data, Mapping):
        raise ValueError("SuperCombo snapshot must contain a data object")
    rows: list[dict[str, Any]] = []
    for character, raw_moves in raw_data.items():
        if not isinstance(raw_moves, list):
            continue
        for raw in raw_moves:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["chara"] = str(row.get("chara") or character)
            row["move_type"] = row.get("move_type") or row.get("moveType")
            rows.append(row)
    return rows


def _source_rows_for_target(
    rows: Iterable[Mapping[str, Any]],
    target_input: str,
) -> list[Mapping[str, Any]]:
    """Find exact parents, or strength variants for a generic parent token."""
    base = target_input.split("~", 1)[0].strip()
    exact = [row for row in rows if str(row.get("input") or "") == base]
    if exact:
        return exact
    family = input_family(base)
    return [
        row for row in rows
        if input_family(str(row.get("input") or "")) == family
        and not is_composite_input(str(row.get("input") or ""))
    ]


def build_transition_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one audit row per possible parent -> SuperCombo branch edge."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _normalized_rows(payload):
        grouped[str(row["chara"])].append(row)

    raw_candidates: list[dict[str, Any]] = []
    for character, rows in grouped.items():
        for target in rows:
            target_input = str(target.get("input") or "")
            if not is_composite_input(target_input):
                continue
            sources = _source_rows_for_target(rows, target_input)
            if not sources:
                raw_candidates.append({
                    "sc_chara": character,
                    "source_input": target_input.split("~", 1)[0].strip(),
                    "target_input": target_input,
                    "transition_type": "other",
                    "initial_interaction": "block",
                    "candidate_status": "parent_move_missing",
                    "reason_codes": ["parent_move_not_found_in_snapshot"],
                    "notes": target.get("notes"),
                })
                continue
            for source in sources:
                source_input = str(source.get("input") or "")
                rule = resolve_composite_transition_rule(
                    opener_input=source_input,
                    opener_cancel_raw=(
                        str(source["cancel"]) if source.get("cancel") else None
                    ),
                    target_input=target_input,
                    target_move_type=str(target.get("move_type") or "") or None,
                    target_notes=str(target["notes"]) if target.get("notes") else None,
                    initial_interaction="block",
                )
                candidate = {
                    "sc_chara": character,
                    "source_input": source_input,
                    "target_input": target_input,
                    "transition_type": rule.transition_type,
                    "initial_interaction": "block",
                    "timing_basis": rule.timing_basis,
                    "timing_reference": rule.timing_reference,
                    "gap_min_f": rule.gap_min_f,
                    "gap_max_f": rule.gap_max_f,
                    "evidence": rule.evidence,
                    "notes": target.get("notes"),
                    "candidate_status": (
                        "direct_evidence_ready_for_review"
                        if rule.status == "resolved"
                        else "needs_timing_review"
                    ),
                    "reason_codes": list(rule.reason_codes),
                }
                raw_candidates.append(candidate)

    # SuperCombo snapshots can contain duplicate formatted rows.  Identical
    # direct statements collapse to one candidate, but conflicting direct gap
    # values become an explicit review task instead of whichever row happened
    # to be read first.
    grouped_candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in raw_candidates:
        key = (
            str(candidate.get("sc_chara") or ""),
            str(candidate.get("source_input") or ""),
            str(candidate.get("target_input") or ""),
        )
        grouped_candidates[key].append(candidate)

    candidates: list[dict[str, Any]] = []
    for key in sorted(grouped_candidates):
        grouped_rows = grouped_candidates[key]
        direct_rows = [
            row for row in grouped_rows
            if row.get("candidate_status") == "direct_evidence_ready_for_review"
        ]
        direct_gaps = {
            (row.get("gap_min_f"), row.get("gap_max_f")) for row in direct_rows
        }
        if len(direct_gaps) > 1:
            conflict = dict(direct_rows[0])
            conflict["candidate_status"] = "conflicting_source_evidence"
            conflict["reason_codes"] = ["conflicting_direct_gap_values"]
            conflict["gap_min_f"] = None
            conflict["gap_max_f"] = None
            conflict["evidence_options"] = [
                row.get("evidence") for row in direct_rows if row.get("evidence")
            ]
            candidates.append(conflict)
        elif direct_rows:
            candidates.append(direct_rows[0])
        else:
            candidates.append(grouped_rows[0])
    return candidates


def reviewed_rule_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    character_slug_by_sc_name: Mapping[str, str],
    patch_version: str | None,
) -> list[dict[str, Any]]:
    """Return safe unreviewed DB candidates from direct source statements."""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("candidate_status") != "direct_evidence_ready_for_review":
            continue
        character = str(candidate.get("sc_chara") or "")
        character_slug = character_slug_by_sc_name.get(character.casefold())
        if not character_slug:
            continue
        rows.append({
            "character_slug": character_slug,
            "source_input": candidate["source_input"],
            "target_input": candidate["target_input"],
            "transition_type": candidate["transition_type"],
            "initial_interaction": "block",
            "timing_basis": "direct_block_gap",
            "timing_reference": "defender_actionable",
            "gap_min_f": candidate.get("gap_min_f"),
            "gap_max_f": candidate.get("gap_max_f"),
            "conditions": {"source_snapshot_candidate": True},
            "condition_key": "default",
            "source": "SuperCombo",
            "evidence": candidate.get("evidence"),
            "patch_version": patch_version,
            "confidence": 0.8,
            "reviewed": False,
        })
    return rows


def _character_slugs(client: Any) -> dict[str, str]:
    response = client.table("char_slug_map").select("capcom_slug,sc_chara").execute()
    return {
        str(row["sc_chara"]).casefold(): str(row["capcom_slug"])
        for row in (response.data or [])
        if row.get("sc_chara") and row.get("capcom_slug")
    }


def audit_snapshot(
    snapshot_path: str | Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Audit one downloaded snapshot and optionally stage direct candidates."""
    path = Path(snapshot_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = build_transition_candidates(payload)
    counts = Counter(str(row["candidate_status"]) for row in candidates)
    result: dict[str, Any] = {
        "snapshot": str(path),
        "fetched_at": payload.get("fetched_at"),
        "candidate_count": len(candidates),
        "status_counts": dict(sorted(counts.items())),
        "candidates": candidates,
        "staged_unreviewed": 0,
    }
    if not apply:
        return result

    client = get_write_client()
    patch_version = str(payload.get("fetched_at") or "")[:10] or None
    rows = reviewed_rule_candidates(
        candidates,
        character_slug_by_sc_name=_character_slugs(client),
        patch_version=patch_version,
    )
    if rows:
        response = (
            client.table("source_transition_rules")
            .upsert(
                rows,
                on_conflict=(
                    "character_slug,source_input,target_input,transition_type,"
                    "initial_interaction,condition_key,patch_version,source"
                ),
            )
            .execute()
        )
        result["staged_unreviewed"] = len(response.data or rows)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", help="path to downloaded SuperCombo JSON")
    parser.add_argument(
        "--output",
        help="write the complete review queue to this JSON file",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="stage direct-evidence candidates as reviewed=false DB rows",
    )
    args = parser.parse_args()
    result = audit_snapshot(args.snapshot, apply=args.apply)
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    printable = {key: value for key, value in result.items() if key != "candidates"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
