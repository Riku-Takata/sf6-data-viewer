"""Exhaustive deterministic audit for the multi-source frame profile.

This is deliberately separate from the conversational bot evaluation.  It
checks source resolution and frame semantics against every stored CAPCOM, UFD,
and SuperCombo row before an LLM can influence the result.

Usage:
  PYTHONPATH=src ./.venv312/bin/python tests/frame_profile_comprehensive_audit.py
  PYTHONPATH=src ./.venv312/bin/python tests/frame_profile_comprehensive_audit.py \
      --output /tmp/frame-profile-audit.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from sf6_engine.db import get_client  # noqa: E402
from sf6_engine.frame_data import (  # noqa: E402
    _all_character_rows_cached,
    _cache_bucket,
    _clean_raw,
    _invert_advantage,
    _parse_capcom_active,
    _parse_frame_value,
    _parse_recovery,
    format_frame_profile_context,
    lookup_frame_data,
)
from sf6_engine.rag_builder import _deterministic_frame_answer  # noqa: E402


CORE_FIELDS = ("startup", "active", "recovery", "on_block", "on_hit")
REQUIREMENT_SECTIONS = ("通常技", "特殊技", "必殺技", "スーパーアーツ")
SC_RAW_KEYS = {
    "startup": "startup",
    "active": "active",
    "recovery": "recovery",
    "on_block": "block_adv",
    "on_hit": "hit_adv",
}
UFD_RAW_KEYS = {
    "startup": "startup",
    "active": "active",
    "recovery": "recovery",
    "on_block": "on_block",
    "on_hit": "on_hit",
}


def _capcom_parser(field: str) -> Callable[[object], dict[str, Any] | None]:
    if field == "active":
        return _parse_capcom_active
    if field == "recovery":
        return _parse_recovery
    if field in {"on_block", "on_hit"}:
        return lambda value: _parse_frame_value(value, advantage=True)
    return _parse_frame_value


def _same_derived_fact(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("value", "min", "max", "display", "is_range", "alternatives")
    return all(left.get(key) == right.get(key) for key in keys)


def _source_observation(
    fact: dict[str, Any] | None,
    source: str,
) -> dict[str, Any] | None:
    if not fact:
        return None
    return next(
        (row for row in fact.get("observations") or [] if row.get("source") == source),
        None,
    )


class Audit:
    def __init__(self, max_failures: int) -> None:
        self.max_failures = max_failures
        self.assertions = 0
        self.failures = 0
        self.failure_codes: Counter[str] = Counter()
        self.failure_samples: list[dict[str, Any]] = []
        self.coverage: Counter[str] = Counter()
        self.selected_sources: dict[str, Counter[str]] = defaultdict(Counter)
        self.semantics: dict[str, Counter[str]] = defaultdict(Counter)
        self.requirement_coverage: dict[str, dict[str, Counter[str]]] = defaultdict(
            lambda: defaultdict(Counter)
        )
        self.requirement_gap_samples: list[dict[str, Any]] = []

    def check(
        self,
        condition: bool,
        code: str,
        character: str,
        move: str,
        detail: str,
    ) -> None:
        self.assertions += 1
        if condition:
            return
        self.failures += 1
        self.failure_codes[code] += 1
        if len(self.failure_samples) < self.max_failures:
            self.failure_samples.append({
                "code": code,
                "character": character,
                "move": move,
                "detail": detail,
            })

    def record_profile(self, profile: dict[str, Any]) -> None:
        facts = profile.get("facts") or {}
        for field in CORE_FIELDS:
            fact = facts.get(field)
            if fact:
                self.selected_sources[field][fact.get("source") or "none"] += 1
                self.semantics[field][fact.get("semantic") or "unknown"] += 1
            else:
                self.selected_sources[field]["none"] += 1
                self.semantics[field]["missing"] += 1
        source_rows = profile.get("source_rows") or {}
        linked = sum(bool(source_rows.get(key)) for key in (
            "capcom_move_name", "ufd_move_name", "supercombo_move_name"
        ))
        self.coverage[f"profiles_with_{linked}_sources"] += 1
        self.coverage["resolution_warnings"] += len(profile.get("warnings") or [])

    def record_requirement_profile(
        self,
        *,
        character: str,
        section: str,
        move_name: str,
        damage_raw: object,
        profile: dict[str, Any],
    ) -> None:
        if section not in REQUIREMENT_SECTIONS:
            return
        facts = profile.get("facts") or {}
        damage_values = [int(value) for value in re.findall(r"\d+", str(damage_raw or ""))]
        is_attack = any(value > 0 for value in damage_values)
        exact_semantics = {
            "startup": {"scalar", "range", "conditional_values", "composite"},
            "active": {"scalar", "active_sequence", "conditional_values"},
            "recovery": {
                "scalar",
                "range",
                "landing_recovery",
                "composite_recovery",
                "recovery_conditional_values",
            },
            "on_block": {"scalar", "range", "conditional_values", "stage_sequence"},
        }
        for field, accepted in exact_semantics.items():
            counts = self.requirement_coverage[section][field]
            counts["total"] += 1
            if is_attack:
                counts["attack_total"] += 1
            fact = facts.get(field)
            if fact:
                counts["answerable"] += 1
            semantic = (fact or {}).get("semantic")
            if field == "on_block" and semantic == "not_applicable":
                counts["not_applicable"] += 1
                if is_attack:
                    counts["attack_not_applicable"] += 1
                continue
            if field == "on_block" and semantic == "variable":
                counts["variable"] += 1
                if is_attack:
                    counts["attack_variable"] += 1
                continue
            exact = bool(fact and fact.get("usable") and semantic in accepted)
            if exact:
                counts["exact_typed_value"] += 1
                if is_attack:
                    counts["attack_exact_typed_value"] += 1
                continue
            counts["exact_value_gap"] += 1
            if is_attack:
                counts["attack_exact_value_gap"] += 1
            if len(self.requirement_gap_samples) < self.max_failures:
                counts_label = (fact or {}).get("display") or "データなし"
                self.requirement_gap_samples.append({
                    "character": character,
                    "section": section,
                    "move": move_name,
                    "field": field,
                    "is_attack": is_attack,
                    "damage_raw": damage_raw,
                    "semantic": semantic or "missing",
                    "display": counts_label,
                })


def _audit_answers(
    audit: Audit,
    slug: str,
    move_name: str,
    profile: dict[str, Any],
) -> None:
    context = format_frame_profile_context(profile)
    facts = profile.get("facts") or {}
    questions = (
        ("startup", f"{slug}の{move_name}の発生は？"),
        ("active", f"{slug}の{move_name}の持続は？"),
        ("recovery", f"{slug}の{move_name}の硬直は？"),
    )
    for field, question in questions:
        answer = _deterministic_frame_answer(question, context)
        audit.coverage[f"answer_{field}"] += 1
        audit.check(
            answer is not None,
            "answer_not_deterministic",
            slug,
            move_name,
            f"{field}: deterministic answer was None",
        )
        if answer is None:
            continue
        fact = facts.get(field)
        expected = fact.get("display") if fact else None
        audit.check(
            (expected in answer) if expected else ("データがありません" in answer),
            "answer_field_value",
            slug,
            move_name,
            f"{field}: expected={expected!r}, answer={answer!r}",
        )

    attacker = facts.get("on_block")
    defender = (profile.get("block_perspectives") or {}).get("defender")
    for perspective, question, fact in (
        ("attacker", f"{slug}の{move_name}をガードさせたら何F？", attacker),
        ("defender", f"{slug}の{move_name}をガードしたら何F？", defender),
    ):
        answer = _deterministic_frame_answer(question, context)
        audit.coverage[f"answer_guard_{perspective}"] += 1
        audit.check(
            answer is not None,
            "answer_guard_not_deterministic",
            slug,
            move_name,
            f"{perspective}: deterministic answer was None",
        )
        if answer is None:
            continue
        if attacker is None:
            valid = "データがない" in answer and "算出できません" in answer
        elif fact is None:
            valid = "算出不可" in answer
        else:
            valid = fact.get("display") in answer
        audit.check(
            valid,
            "answer_guard_value",
            slug,
            move_name,
            f"{perspective}: expected={(fact or {}).get('display')!r}, answer={answer!r}",
        )


def _audit_capcom_row(
    audit: Audit,
    slug: str,
    row: dict[str, Any],
    *,
    check_answers: bool,
) -> None:
    move_name = row.get("move_name") or "?"
    result = lookup_frame_data(slug, move_name)
    audit.coverage["capcom_queries"] += 1
    audit.check(
        bool(result.get("found") and result.get("move")),
        "capcom_not_found",
        slug,
        move_name,
        result.get("message") or "not found",
    )
    if not result.get("found") or not result.get("move"):
        return

    profile = result["move"].get("frame_profile") or {}
    audit.record_profile(profile)
    selected_name = (profile.get("source_rows") or {}).get("capcom_move_name")
    audit.check(
        selected_name == move_name,
        "capcom_wrong_variant",
        slug,
        move_name,
        f"selected CAPCOM row: {selected_name!r}",
    )

    facts = profile.get("facts") or {}
    audit.record_requirement_profile(
        character=slug,
        section=row.get("section") or "",
        move_name=move_name,
        damage_raw=row.get("damage"),
        profile=profile,
    )
    for field in CORE_FIELDS:
        raw = row.get(field)
        parsed = _capcom_parser(field)(raw)
        if parsed is None:
            continue
        fact = facts.get(field)
        observation = _source_observation(fact, "capcom")
        audit.check(
            observation is not None,
            "capcom_observation_missing",
            slug,
            move_name,
            f"{field}: raw={raw!r}",
        )
        if parsed.get("usable"):
            audit.check(
                bool(fact and fact.get("source") == "capcom"),
                "capcom_primary_not_selected",
                slug,
                move_name,
                f"{field}: selected={(fact or {}).get('source')}, raw={raw!r}",
            )
            audit.check(
                bool(fact and fact.get("raw") == parsed.get("raw")),
                "capcom_selected_raw_mismatch",
                slug,
                move_name,
                f"{field}: expected={parsed.get('raw')!r}, actual={(fact or {}).get('raw')!r}",
            )

    attacker = facts.get("on_block")
    defender = (profile.get("block_perspectives") or {}).get("defender")
    expected_defender = _invert_advantage(attacker) if attacker else None
    audit.check(
        (defender is None and expected_defender is None)
        or bool(defender and expected_defender and _same_derived_fact(defender, expected_defender)),
        "guard_inversion_mismatch",
        slug,
        move_name,
        f"attacker={(attacker or {}).get('display')}, defender={(defender or {}).get('display')}",
    )
    context = format_frame_profile_context(profile)
    audit.check(
        "ガード時（攻撃側・ガードさせた側）" in context
        and "ガード時（防御側・ガードした側）" in context,
        "guard_perspective_context_missing",
        slug,
        move_name,
        context[:500],
    )

    if check_answers:
        _audit_answers(audit, slug, move_name, profile)


def _audit_source_row(
    audit: Audit,
    *,
    slug: str,
    query: str,
    row: dict[str, Any],
    source: str,
    raw_keys: dict[str, str],
) -> None:
    result = lookup_frame_data(slug, query)
    audit.coverage[f"{source}_queries"] += 1
    audit.check(
        bool(result.get("found") and result.get("move")),
        f"{source}_not_found",
        slug,
        query,
        result.get("message") or "not found",
    )
    if not result.get("found") or not result.get("move"):
        return
    profile = result["move"].get("frame_profile") or {}
    expected_name = row.get("move_name") or row.get("name")
    source_key = "ufd_move_name" if source == "ufd" else "supercombo_move_name"
    selected_name = (profile.get("source_rows") or {}).get(source_key)
    audit.check(
        selected_name == expected_name,
        f"{source}_wrong_variant",
        slug,
        query,
        f"expected={expected_name!r}, selected={selected_name!r}",
    )
    facts = profile.get("facts") or {}
    for field, raw_key in raw_keys.items():
        raw = _clean_raw(row.get(raw_key))
        if raw is None:
            continue
        observation = _source_observation(facts.get(field), source)
        audit.check(
            bool(observation and observation.get("raw") == raw),
            f"{source}_observation_missing",
            slug,
            query,
            f"{field}: expected raw={raw!r}, observation={(observation or {}).get('raw')!r}",
        )


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    sb = get_client()
    character_rows = (
        sb.table("char_slug_map")
        .select("capcom_slug,sc_chara,display_name_ja")
        .order("capcom_slug")
        .execute()
        .data
        or []
    )
    if args.chars:
        wanted = {value.casefold() for value in args.chars}
        character_rows = [
            row for row in character_rows
            if row["capcom_slug"].casefold() in wanted
            or row["sc_chara"].casefold() in wanted
        ]

    audit = Audit(args.max_failure_samples)
    source_counts: Counter[str] = Counter()

    for index, character in enumerate(character_rows, start=1):
        slug = character["capcom_slug"]
        sc_chara = character["sc_chara"]
        rows = _all_character_rows_cached(slug, sc_chara, _cache_bucket())
        source_counts["capcom"] += len(rows["capcom"])
        source_counts["supercombo"] += len(rows["sc"])
        source_counts["ufd"] += len(rows["ufd"])

        for row in rows["capcom"]:
            _audit_capcom_row(
                audit,
                slug,
                row,
                check_answers=not args.skip_answers,
            )

        if not args.skip_supercombo:
            for row in rows["sc"]:
                _audit_source_row(
                    audit,
                    slug=slug,
                    query=row.get("input") or row.get("name") or "?",
                    row=row,
                    source="supercombo",
                    raw_keys=SC_RAW_KEYS,
                )

        if not args.skip_ufd:
            by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
            by_input: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows["ufd"]:
                if row.get("move_name"):
                    by_name[row["move_name"]].append(row)
                if row.get("sc_input"):
                    by_input[row["sc_input"]].append(row)
            for name, grouped in by_name.items():
                if len(grouped) == 1:
                    _audit_source_row(
                        audit,
                        slug=slug,
                        query=name,
                        row=grouped[0],
                        source="ufd",
                        raw_keys=UFD_RAW_KEYS,
                    )
                else:
                    audit.coverage["ufd_ambiguous_names_skipped"] += len(grouped)
            for input_name, grouped in by_input.items():
                if len(grouped) == 1:
                    _audit_source_row(
                        audit,
                        slug=slug,
                        query=input_name,
                        row=grouped[0],
                        source="ufd",
                        raw_keys=UFD_RAW_KEYS,
                    )
                else:
                    audit.coverage["ufd_ambiguous_inputs_skipped"] += len(grouped)

        if not args.quiet:
            print(
                f"[{index:02d}/{len(character_rows):02d}] {slug}: "
                f"CAPCOM={len(rows['capcom'])} SC={len(rows['sc'])} UFD={len(rows['ufd'])} "
                f"assertions={audit.assertions} failures={audit.failures}",
                flush=True,
            )

    elapsed = time.monotonic() - started
    report = {
        "ok": audit.failures == 0,
        "elapsed_seconds": round(elapsed, 3),
        "characters": len(character_rows),
        "source_rows": dict(source_counts),
        "assertions": audit.assertions,
        "failures": audit.failures,
        "failure_codes": dict(audit.failure_codes),
        "failure_samples": audit.failure_samples,
        "coverage": dict(audit.coverage),
        "selected_sources_by_field": {
            field: dict(counts) for field, counts in audit.selected_sources.items()
        },
        "selected_semantics_by_field": {
            field: dict(counts) for field, counts in audit.semantics.items()
        },
        "requirement_coverage_by_section": {
            section: {
                field: dict(counts)
                for field, counts in fields.items()
            }
            for section, fields in audit.requirement_coverage.items()
        },
        "requirement_gap_samples": audit.requirement_gap_samples,
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CAPCOM/UFD/SuperCombo統合フレームプロファイル全件監査"
    )
    parser.add_argument("--chars", nargs="+", help="対象キャラスラッグ/SC名")
    parser.add_argument("--skip-answers", action="store_true")
    parser.add_argument("--skip-supercombo", action="store_true")
    parser.add_argument("--skip-ufd", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-failure-samples", type=int, default=100)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
