"""Evidence-backed timing rules for character-specific move transitions.

``sc_moves`` contains both ordinary moves and composite inputs such as
``236K~6LK``.  The latter cannot be evaluated with the normal link or
normal-to-special-cancel formula: the table's startup is relative to the
branch input, while the branch window is character- and move-specific.

This module deliberately accepts only timing facts that the source states
directly (for example, "3f blockstring gap" or "true blockstring").  Other
composite rows remain unresolved until a reviewed transition edge supplies a
window or a direct gap.  That is safer than treating every ``~`` row as an
ordinary special cancel.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


_COMPOSITE_INPUT = re.compile(r"~")
_STRENGTH_BUTTON = re.compile(r"([LMH])([PK])")
_TRUE_BLOCKSTRING = re.compile(
    r"\b(?:always\s+)?(?:a\s+)?true\s+blockstring"
    r"(?:\s+from\s+(?P<source>[A-Za-z0-9.\[\]~]+))?",
    re.IGNORECASE,
)
_BLOCK_GAP = re.compile(
    r"\b(?P<gap>\d+)\s*f\s+(?:blockstring\s+)?gap\b",
    re.IGNORECASE,
)
_SOURCE_REFERENCE = re.compile(
    r"\b(?:from|using)\s+(?P<source>[A-Za-z0-9.\[\]~]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TransitionRule:
    """One timing rule that the sequence evaluator can execute safely."""

    transition_type: str
    status: str
    timing_basis: str | None
    timing_reference: str | None
    gap_min_f: int | None
    gap_max_f: int | None
    source: str | None
    evidence: str | None
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason_codes"] = list(self.reason_codes)
        return data


def is_composite_input(input_value: str | None) -> bool:
    """Return whether an SC input represents an explicit branch/sequence."""
    return bool(
        input_value
        and _COMPOSITE_INPUT.search(input_value)
        # A formatted alternatives row (``A~B or C~D``) is not one executable
        # target edge.  It must be split/reviewed before runtime use.
        and not re.search(r"\s+(?:or|and)\s+", input_value, re.IGNORECASE)
    )


def _family_input(input_value: str) -> str:
    """Remove strength only for matching a family such as 236MK / 236K."""
    return _STRENGTH_BUTTON.sub(r"\2", input_value).casefold()


def input_family(input_value: str) -> str:
    """Return the strength-agnostic family key used for candidate discovery."""
    return _family_input(input_value)


def _base_input(input_value: str) -> str:
    return input_value.split("~", 1)[0].strip()


def _source_matches(
    opener_input: str,
    target_input: str,
    note_source: str | None,
) -> bool:
    """Check an optional prose source qualifier without guessing variants."""
    if note_source:
        note_source = note_source.rstrip(".,;:")
        if opener_input.casefold() == note_source.casefold():
            return True
        # A note that names an actual strength (236HP, 214LP, ...) must not
        # silently become evidence for its other strengths.  A generic form
        # such as 236K may deliberately cover a family, so only that form
        # falls back to the family key.
        if _STRENGTH_BUTTON.search(note_source):
            return False
        return _family_input(opener_input) == _family_input(note_source)
    base = _base_input(target_input)
    return opener_input.casefold() == base.casefold()


def _transition_type(
    opener_cancel_raw: str | None,
    target_move_type: str | None,
) -> str:
    cancel = str(opener_cancel_raw or "")
    if re.search(r"(?:^|[\s,;/])Chn(?:$|[\s,;/])", cancel):
        return "chain"
    if str(target_move_type or "").casefold() == "ground_normal":
        return "target_combo"
    return "stance_followup"


def _sentence_containing(text: str, offset: int) -> str:
    """Return the punctuation-bounded note clause around a match."""
    start_candidates = [text.rfind(mark, 0, offset) for mark in ".!?\n;"]
    start = max(start_candidates) + 1
    end_candidates = [text.find(mark, offset) for mark in ".!?\n;"]
    ends = [value for value in end_candidates if value >= 0]
    end = min(ends) if ends else len(text)
    return text[start:end]


def _is_block_timing_sentence(sentence: str) -> bool:
    """Reject hit-only or state-conditional gaps for an after-block query."""
    lower = sentence.casefold()
    if "block" not in lower:
        return False
    if (
        re.search(r"\bon hit\b|\bhit[- ]only\b", lower)
        and "on block" not in lower
    ):
        return False
    # The current runtime does not yet model stance, distance, counter state,
    # or contact-specific predicates.  A conditional source sentence belongs
    # in the review queue until those conditions are typed on an exact edge.
    return not bool(re.search(
        r"\b(?:if|when|unless|only|crouch(?:ing)?|counter(?:-hit)?|"
        r"distance|range|spaced|corner)\b",
        lower,
    ))


def resolve_composite_transition_rule(
    *,
    opener_input: str,
    opener_cancel_raw: str | None,
    target_input: str,
    target_move_type: str | None,
    target_notes: str | None,
    initial_interaction: str,
) -> TransitionRule:
    """Resolve direct block timing evidence for a ``~`` transition.

    The result is intentionally conservative.  A composite row without a
    direct block gap or true-blockstring statement is a *candidate*, not a
    calculable edge.  Its startup alone is not a branch-start offset.
    """
    if not is_composite_input(target_input):
        return TransitionRule(
            transition_type="other",
            status="unresolved",
            timing_basis=None,
            timing_reference=None,
            gap_min_f=None,
            gap_max_f=None,
            source=None,
            evidence=None,
            reason_codes=("composite_input_required",),
        )
    transition_type = _transition_type(opener_cancel_raw, target_move_type)
    if initial_interaction != "block":
        return TransitionRule(
            transition_type=transition_type,
            status="unresolved",
            timing_basis=None,
            timing_reference=None,
            gap_min_f=None,
            gap_max_f=None,
            source="SuperCombo",
            evidence=target_notes,
            reason_codes=("composite_hit_timing_rule_missing",),
        )

    notes = str(target_notes or "")
    for match in _TRUE_BLOCKSTRING.finditer(notes):
        sentence = _sentence_containing(notes, match.start())
        if not _is_block_timing_sentence(sentence):
            continue
        if _source_matches(opener_input, target_input, match.group("source")):
            return TransitionRule(
                transition_type=transition_type,
                status="resolved",
                timing_basis="direct_block_note",
                timing_reference="defender_actionable",
                gap_min_f=None,
                gap_max_f=0,
                source="SuperCombo",
                evidence=match.group(0),
            )

    for match in _BLOCK_GAP.finditer(notes):
        sentence = _sentence_containing(notes, match.start())
        if not _is_block_timing_sentence(sentence):
            continue
        source_match = _SOURCE_REFERENCE.search(sentence)
        note_source = source_match.group("source") if source_match else None
        # A bare "Nf gap" is still accepted only when the composite row's
        # prefix identifies this exact opener/family.  This avoids applying a
        # note about one strength to every version of the move.
        if _source_matches(opener_input, target_input, note_source):
            gap_f = int(match.group("gap"))
            return TransitionRule(
                transition_type=transition_type,
                status="resolved",
                timing_basis="direct_block_note",
                timing_reference="defender_actionable",
                gap_min_f=gap_f,
                gap_max_f=gap_f,
                source="SuperCombo",
                evidence=sentence.strip(),
            )

    return TransitionRule(
        transition_type=transition_type,
        status="unresolved",
        timing_basis=None,
        timing_reference=None,
        gap_min_f=None,
        gap_max_f=None,
        source="SuperCombo",
        evidence=target_notes,
        reason_codes=("composite_transition_timing_rule_missing",),
    )


def transition_candidate_from_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Produce a review-worklist row from one SC composite move row.

    This does not claim that the candidate is executable.  It is used by the
    audit/import command to cover every character without hand-writing Python
    branches per move.
    """
    target_input = str(row.get("input") or "")
    if not is_composite_input(target_input):
        return None
    base = _base_input(target_input)
    return {
        "character": row.get("chara"),
        "source_input_family": base,
        "target_input": target_input,
        "transition_type": _transition_type(
            None, row.get("move_type") or row.get("moveType")
        ),
        "startup_raw": row.get("startup"),
        "blockstun_raw": row.get("blockstun"),
        "notes": row.get("notes"),
        "review_status": "candidate",
    }
