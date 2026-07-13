"""Import reviewed multi-move observations into Supabase.

The bundled JSON remains a deploy-safe bootstrap so the deterministic engine
can answer while a migration is rolling out.  Importing the same file makes the
observation queryable and replaceable without a code deployment.

Usage:
  PYTHONPATH=src python -m sf6_engine.importers.sequence_observations --dry-run
  PYTHONPATH=src python -m sf6_engine.importers.sequence_observations
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from sf6_engine.db import get_write_client
from sf6_engine.sequence_analysis import make_sequence_key


DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "sequence_observations.json"
)
TABLE_COLUMNS = {
    "observation_key",
    "attacker_character_slug",
    "attacker_sequence",
    "initial_interaction",
    "defender_character_slug",
    "defender_move_input",
    "defender_profile",
    "outcome",
    "attacker_advantage_f",
    "defender_advantage_f",
    "confirmed_followups",
    "result_state",
    "conditions",
    "source",
    "evidence_url",
    "evidence_storage_path",
    "test_protocol",
    "patch_version",
    "confidence",
    "reviewed",
    "observed_at",
}


def load_observations(path: str | Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_rows = payload.get("observations", payload)
    if not isinstance(raw_rows, list):
        raise ValueError("observation file must contain a list")
    return [validate_observation(row) for row in raw_rows]


def validate_observation(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate identity and return only columns accepted by Supabase."""
    row = {key: raw.get(key) for key in TABLE_COLUMNS if key in raw}
    sequence = row.get("attacker_sequence")
    if not isinstance(sequence, list) or len(sequence) < 2:
        raise ValueError("attacker_sequence must contain at least two events")
    inputs = [
        str(item.get("input") or "") for item in sequence
        if isinstance(item, Mapping)
    ]
    if len(inputs) != len(sequence) or any(not value for value in inputs):
        raise ValueError("every attacker event must have an input")
    required = (
        "attacker_character_slug",
        "initial_interaction",
        "outcome",
        "source",
        "patch_version",
    )
    missing = [key for key in required if not row.get(key)]
    if missing:
        raise ValueError(f"missing required observation fields: {', '.join(missing)}")
    defender = row.get("defender_profile") or {}
    second_event = sequence[1] if isinstance(sequence[1], Mapping) else {}
    attacker_delay_f = second_event.get("delay_f")
    if attacker_delay_f is None and second_event.get("timing") == "earliest":
        attacker_delay_f = 0
    defender_delay_f = defender.get("delay_f") if isinstance(defender, Mapping) else None
    if (
        defender_delay_f is None
        and isinstance(defender, Mapping)
        and defender.get("timing") == "earliest"
    ):
        defender_delay_f = 0
    expected_key = make_sequence_key(
        str(row["attacker_character_slug"]),
        inputs,
        str(row["initial_interaction"]),
        defender.get("startup_f") if isinstance(defender, Mapping) else None,
        str(row["outcome"]),
        attacker_delay_f=attacker_delay_f,
        defender_delay_f=defender_delay_f,
        defender_character_slug=(
            str(row.get("defender_character_slug") or "") or None
        ),
        defender_move_input=(str(row.get("defender_move_input") or "") or None),
    )
    if row.get("observation_key") != expected_key:
        raise ValueError(
            f"observation_key mismatch: {row.get('observation_key')} != {expected_key}"
        )
    attacker_adv = row.get("attacker_advantage_f")
    defender_adv = row.get("defender_advantage_f")
    if (
        isinstance(attacker_adv, int)
        and isinstance(defender_adv, int)
        and attacker_adv != -defender_adv
    ):
        raise ValueError("attacker/defender advantage must be opposite values")
    has_exact_post_result = (
        isinstance(attacker_adv, int)
        or isinstance(defender_adv, int)
        or bool(row.get("confirmed_followups"))
    )
    if row.get("reviewed") and has_exact_post_result and not (
        row.get("defender_character_slug") and row.get("defender_move_input")
    ):
        raise ValueError(
            "reviewed post-trade advantage/followups require exact defender "
            "character and move"
        )
    row.setdefault("defender_profile", {})
    row.setdefault("confirmed_followups", [])
    row.setdefault("result_state", {})
    row.setdefault("conditions", {})
    row.setdefault("confidence", 1.0)
    row.setdefault("reviewed", False)
    return row


def import_sequence_observations(
    path: str | Path = DEFAULT_PATH,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    rows = load_observations(path)
    result: dict[str, Any] = {
        "path": str(Path(path)),
        "validated": len(rows),
        "upserted": 0,
        "dry_run": dry_run,
        "observation_keys": [row["observation_key"] for row in rows],
    }
    if dry_run or not rows:
        return result
    response = (
        get_write_client()
        .table("sequence_observations")
        .upsert(rows, on_conflict="observation_key,source,patch_version")
        .execute()
    )
    result["upserted"] = len(response.data or rows)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=str(DEFAULT_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(
        import_sequence_observations(args.path, dry_run=args.dry_run),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
