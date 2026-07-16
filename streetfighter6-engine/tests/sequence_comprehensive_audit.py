"""Exhaustive data audit for natural-language two-move sequence analysis.

The conversational parser deliberately leaves character-specific move names
opaque.  This audit therefore checks the database resolver separately from the
pure transition/timeline evaluator:

* every stored SuperCombo input and move name is sent through the shared frame
  resolver for every character;
* every CAPCOM official move name is sent through the same resolver;
* every ordered pair of resolved, non-composite SuperCombo moves is classified
  as a link or an evidence-backed cancel and evaluated on block;
* composite branches remain strict and are counted as unresolved unless a
  direct/reviewed edge supplies their timing.

Missing scalar frames are reported, not treated as audit failures.  A correct
answer in that case is a typed ``unresolved`` result rather than a fabricated
number.

Usage:
  PYTHONPATH=src ./.venv312/bin/python tests/sequence_comprehensive_audit.py
  PYTHONPATH=src ./.venv312/bin/python tests/sequence_comprehensive_audit.py \
      --output /tmp/sequence-comprehensive-audit.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from sf6_engine.db import get_client  # noqa: E402
from sf6_engine.frame_data import lookup_frame_data  # noqa: E402
from sf6_engine.sequence_analysis import (  # noqa: E402
    MoveInteractionProfile,
    _apply_integrated_frame_profile,
    _cancel_timeline,
    _direct_block_note_timeline,
    _profile_from_row,
    _timeline,
    _transition_profile,
)
from sf6_engine.transition_rules import is_composite_input  # noqa: E402


SC_COLUMNS = (
    "chara,input,name,move_type,startup,active,recovery,hit_adv,block_adv,"
    "hitstun,blockstun,hitstop,atk_range,cancel,notes"
)
EXAMPLE_LIMIT = 40


def _append_example(items: list[dict[str, Any]], value: dict[str, Any]) -> None:
    if len(items) < EXAMPLE_LIMIT:
        items.append(value)


def _unique_present(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value not in (None, "")))


def _timeline_for(
    opener: MoveInteractionProfile,
    target: MoveInteractionProfile,
    transition: dict[str, Any],
) -> dict[str, Any]:
    if transition.get("timing_basis") in {"direct_block_note", "direct_block_gap"}:
        return _direct_block_note_timeline(transition, None, 0, 0)
    if transition.get("type") == "cancel":
        return _cancel_timeline(opener, target, "block", None, 0, 0)
    return _timeline(opener, target, "block", None, 0, 0)


def run_audit() -> dict[str, Any]:
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
    counts: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()
    timeline_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = {
        "not_found": [],
        "ambiguous_or_unusable": [],
        "timeline_unresolved": [],
        "composite_unresolved": [],
    }

    for character in characters:
        slug = str(character["capcom_slug"])
        sc_character = str(character["sc_chara"])
        counts["characters"] += 1
        sc_rows = (
            client.table("sc_moves")
            .select(SC_COLUMNS)
            .eq("chara", sc_character)
            .limit(500)
            .execute()
            .data
            or []
        )
        capcom_rows = (
            client.table("move_latest")
            .select("move_name")
            .eq("character_slug", slug)
            .limit(500)
            .execute()
            .data
            or []
        )
        counts["supercombo_rows"] += len(sc_rows)
        counts["capcom_rows"] += len(capcom_rows)

        # One lookup warms the per-character multi-source cache.  Subsequent
        # identifiers are resolved in memory against the same source snapshot.
        resolution_by_input: dict[str, dict[str, Any]] = {}
        for identifier in _unique_present(row.get("input") for row in sc_rows):
            result = lookup_frame_data(slug, identifier)
            counts["sc_input_queries"] += 1
            resolution_by_input[identifier] = result
            if not result.get("found"):
                counts["not_found"] += 1
                _append_example(examples["not_found"], {
                    "character": sc_character,
                    "kind": "sc_input",
                    "query": identifier,
                    "status": result.get("status") or (result.get("resolution") or {}).get("status"),
                })
            elif not (result.get("resolution") or {}).get("usable_for_calculation"):
                counts["ambiguous_or_unusable"] += 1
                _append_example(examples["ambiguous_or_unusable"], {
                    "character": sc_character,
                    "kind": "sc_input",
                    "query": identifier,
                    "resolution": result.get("resolution"),
                })
            else:
                counts["sc_input_usable"] += 1

        for name in _unique_present(row.get("name") for row in sc_rows):
            result = lookup_frame_data(slug, name)
            counts["sc_name_queries"] += 1
            if not result.get("found"):
                counts["not_found"] += 1
                _append_example(examples["not_found"], {
                    "character": sc_character,
                    "kind": "sc_name",
                    "query": name,
                })
            elif not (result.get("resolution") or {}).get("usable_for_calculation"):
                counts["ambiguous_or_unusable"] += 1
                _append_example(examples["ambiguous_or_unusable"], {
                    "character": sc_character,
                    "kind": "sc_name",
                    "query": name,
                    "resolution": result.get("resolution"),
                })
            else:
                counts["sc_name_usable"] += 1

        for name in _unique_present(row.get("move_name") for row in capcom_rows):
            result = lookup_frame_data(slug, name)
            counts["capcom_name_queries"] += 1
            if not result.get("found"):
                counts["not_found"] += 1
                _append_example(examples["not_found"], {
                    "character": sc_character,
                    "kind": "capcom_name",
                    "query": name,
                })
            elif not (result.get("resolution") or {}).get("usable_for_calculation"):
                counts["ambiguous_or_unusable"] += 1
                _append_example(examples["ambiguous_or_unusable"], {
                    "character": sc_character,
                    "kind": "capcom_name",
                    "query": name,
                    "resolution": result.get("resolution"),
                })
            else:
                counts["capcom_name_usable"] += 1

        profiles: list[MoveInteractionProfile] = []
        for row in sc_rows:
            identifier = str(row.get("input") or "")
            resolved = resolution_by_input.get(identifier) or {}
            profile = _apply_integrated_frame_profile(_profile_from_row(row), resolved)
            profiles.append(profile)

        ordinary = [move for move in profiles if not is_composite_input(move.input)]
        composites = [move for move in profiles if is_composite_input(move.input)]
        for opener in ordinary:
            for target in ordinary:
                counts["ordinary_pairs"] += 1
                transition = _transition_profile(opener, target, "block")
                transition_counts[str(transition.get("cancel_category") or transition.get("type"))] += 1
                timeline = _timeline_for(opener, target, transition)
                status = str(timeline.get("status") or "unknown")
                timeline_counts[status] += 1
                if status != "resolved":
                    for reason in timeline.get("reason_codes") or ["unknown"]:
                        reason_counts[str(reason)] += 1
                    _append_example(examples["timeline_unresolved"], {
                        "character": sc_character,
                        "opener": opener.input,
                        "target": target.input,
                        "transition": transition,
                        "timeline": timeline,
                    })

        # A composite row is a candidate only for the source input named before
        # its first '~'.  Auditing every unrelated opener would inflate the
        # unresolved count without describing an executable transition.
        by_input = {move.input.casefold(): move for move in ordinary}
        for target in composites:
            opener = by_input.get(target.input.split("~", 1)[0].strip().casefold())
            if opener is None:
                counts["composite_without_source_row"] += 1
                continue
            counts["composite_pairs"] += 1
            transition = _transition_profile(opener, target, "block")
            transition_counts[str(transition.get("type") or "unknown")] += 1
            if transition.get("status") != "resolved":
                counts["composite_unresolved"] += 1
                for reason in transition.get("reason_codes") or ["unknown"]:
                    reason_counts[str(reason)] += 1
                _append_example(examples["composite_unresolved"], {
                    "character": sc_character,
                    "opener": opener.input,
                    "target": target.input,
                    "transition": transition,
                })
                continue
            timeline = _timeline_for(opener, target, transition)
            timeline_counts[str(timeline.get("status") or "unknown")] += 1

    failures: list[str] = []
    if not counts["characters"]:
        failures.append("character catalog is empty")
    if counts["not_found"]:
        failures.append(f"{counts['not_found']} stored identifiers were not found")
    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "counts": dict(sorted(counts.items())),
        "transition_counts": dict(sorted(transition_counts.items())),
        "timeline_counts": dict(sorted(timeline_counts.items())),
        "unresolved_reason_counts": dict(sorted(reason_counts.items())),
        "examples": examples,
        "policy": {
            "spatial": "not audited; timing-only answers must retain distance/pushback caveats",
            "missing_scalar": "reported as typed unresolved, never flattened or fabricated",
            "composite": "requires direct note or reviewed exact transition edge",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    result = run_audit()
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.quiet:
        print(json.dumps({
            "status": result["status"],
            "failures": result["failures"],
            "counts": result["counts"],
            "timeline_counts": result["timeline_counts"],
        }, ensure_ascii=False))
    else:
        print(payload)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
