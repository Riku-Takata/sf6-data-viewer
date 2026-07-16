"""Executable design evaluation for an updateable SF6 knowledge bot.

This file is deliberately kept under ``tests/``.  It is a non-production
contract spike: it reads no remote data, writes no database rows, and is not
imported by the bot.  It has two jobs:

1. record the current parser/sequence-observation limitations with reproducible
   probes; and
2. exercise the safety invariants required before conversational knowledge can
   be connected to the production answer path.

Run:

    PYTHONPATH=src:. ./.venv312/bin/python \
      tests/conversational_knowledge_design_eval.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from sf6_engine.frame_scenario import parse_frame_scenario  # noqa: E402
from sf6_engine.importers.sequence_observations import validate_observation  # noqa: E402
from sf6_engine.intent_parser import parse_intent  # noqa: E402
from sf6_engine.sequence_analysis import (  # noqa: E402
    MoveInteractionProfile,
    evaluate_sequence,
    make_sequence_key,
)


CURRENT_PATCH = "fixture-p2"


class GeneralQuestionProvider:
    """Stable fallback for utterances outside the deterministic parser."""

    async def generate_structured(self, *, prompt: str, **_: Any) -> dict[str, Any]:
        query = prompt.rsplit("\n\n", 1)[-1]
        return {"intent_type": "general_question", "raw_query": query}


def _move(
    character: str,
    input_: str,
    *,
    startup: int,
    block: int | None = None,
    hitstun: int | None = None,
) -> MoveInteractionProfile:
    return MoveInteractionProfile(
        character=character,
        input=input_,
        name=input_,
        move_type="ground_normal",
        startup_f=startup,
        active_f=2,
        recovery_f=10,
        on_block_f=block,
        on_hit_f=None,
        hitstun_f=hitstun,
        blockstun_f=None,
        hitstop_f=9,
        atk_range=None,
        notes=None,
    )


SAGAT_5MP = _move("Sagat", "5MP", startup=6, block=2, hitstun=25)
SAGAT_2MP = _move("Sagat", "2MP", startup=7, hitstun=23)
RYU_2LP = _move("Ryu", "2LP", startup=4, hitstun=15)


def _observation(
    advantage: int,
    *,
    patch: str,
    conditions: dict[str, Any] | None = None,
    source: str = "user",
) -> dict[str, Any]:
    return {
        "observation_key": make_sequence_key(
            "sagat",
            ["5MP", "5MP"],
            "block",
            4,
            "trade",
            defender_character_slug="ryu",
            defender_move_input="2LP",
        ),
        "attacker_character_slug": "sagat",
        "attacker_sequence": [
            {"input": "5MP", "interaction": "block"},
            {"input": "5MP", "timing": "earliest", "delay_f": 0},
        ],
        "initial_interaction": "block",
        "defender_character_slug": "ryu",
        "defender_move_input": "2LP",
        "defender_profile": {
            "startup_f": 4,
            "timing": "earliest",
            "delay_f": 0,
        },
        "outcome": "trade",
        "attacker_advantage_f": advantage,
        "defender_advantage_f": -advantage,
        "confirmed_followups": [{"input": "2MP", "combo_confirmed": True}],
        "conditions": conditions or {},
        "source": source,
        "patch_version": patch,
        "reviewed": True,
    }


async def current_context_probes() -> list[dict[str, Any]]:
    """Compare current single-turn extraction with the desired semantics."""
    scenario_cases: list[tuple[str, Callable[[dict[str, Any]], bool], str]] = [
        (
            "密着じゃなくて先端でガードさせた",
            lambda value: value.get("distance") == "tip",
            "distance=tip（密着は否定対象）",
        ),
        (
            "相手はバーンアウトじゃない",
            lambda value: value.get("defender_burnout") is False,
            "defender_burnout=false",
        ),
        (
            "ガードじゃなくてヒットした",
            lambda value: value.get("interaction") == "hit",
            "interaction=hit",
        ),
        (
            "たぶん先端で当たった",
            lambda value: value.get("epistemic_basis") == "hypothesis",
            "distance=tip + epistemic_basis=hypothesis",
        ),
        (
            "友達が先端なら+4って言ってた",
            lambda value: value.get("epistemic_basis") == "hearsay",
            "引用条件distance=tip + hearsay + attribution",
        ),
    ]
    results: list[dict[str, Any]] = []
    for query, predicate, expected in scenario_cases:
        actual = parse_frame_scenario(query)
        results.append({
            "query": query,
            "expected": expected,
            "actual": actual,
            "desired_match": predicate(actual),
        })

    provider = GeneralQuestionProvider()
    first = await parse_intent(
        "サガットの5MP→5MPの連携でリュウの2LPと相打ちになる",
        provider,
    )
    followups = [
        "その時2MPがつながるよ",
        "近距離なら投げも重なる",
        "さっきの連携は画面端限定です",
        "さっきのやつ、やっぱり2F遅らせだった",
    ]
    results.append({
        "query": "[turn 1] explicit sequence",
        "expected": "sequence_analysis with exact actors and moves",
        "actual": first,
        "desired_match": (
            first.get("intent_type") == "sequence_analysis"
            and first.get("chara") == "Sagat"
            and (first.get("defender_action") or {}).get("character") == "Ryu"
        ),
    })
    for query in followups:
        actual = await parse_intent(query, provider)
        results.append({
            "query": f"[follow-up] {query}",
            "expected": "reference to turn 1 plus a typed state/claim operation",
            "actual": actual,
            "desired_match": bool(actual.get("references") and actual.get("state_ops")),
        })
    return results


def current_observation_probes() -> list[dict[str, Any]]:
    """Show why raw user reports cannot enter sequence_observations directly."""
    unsafe = _observation(9, patch="unknown")
    validated = validate_observation(unsafe)

    corner_old = _observation(99, patch="old", conditions={"corner": True})
    normal_query = evaluate_sequence(
        character_slug="sagat",
        sc_character="Sagat",
        attacker_moves=[SAGAT_5MP, SAGAT_5MP],
        initial_interaction="block",
        defender_startup_f=4,
        defender_profiles=[RYU_2LP],
        followup_profiles=[SAGAT_2MP],
        expected_outcome="trade",
        observations=[corner_old],
        exact_defender_requested=True,
        defender_character_slug="ryu",
        defender_move_input="2LP",
    )

    old = _observation(99, patch="old")
    new = _observation(9, patch=CURRENT_PATCH)

    def selected(rows: list[dict[str, Any]]) -> int | None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[RYU_2LP],
            followup_profiles=[SAGAT_2MP],
            expected_outcome="trade",
            observations=rows,
            exact_defender_requested=True,
            defender_character_slug="ryu",
            defender_move_input="2LP",
        )
        return (result.get("post_interaction") or {}).get("attacker_advantage_f")

    return [
        {
            "probe": "review validation without evidence/reviewer/known patch",
            "expected_safe_result": "reject",
            "actual": {
                "accepted": True,
                "reviewed": validated.get("reviewed"),
                "confidence": validated.get("confidence"),
                "patch_version": validated.get("patch_version"),
                "has_test_protocol": bool(validated.get("test_protocol")),
            },
            "safe": False,
        },
        {
            "probe": "old corner-only observation on an unqualified current query",
            "expected_safe_result": "exclude observation",
            "actual": {
                "status": (normal_query.get("post_interaction") or {}).get("status"),
                "attacker_advantage_f": (
                    normal_query.get("post_interaction") or {}
                ).get("attacker_advantage_f"),
                "matched_conditions": (
                    (normal_query.get("evidence") or {}).get("reviewed_observation")
                    or {}
                ).get("conditions"),
            },
            "safe": False,
        },
        {
            "probe": "equal-confidence conflicting observations",
            "expected_safe_result": "surface conflict; order must not select a value",
            "actual": {
                "old_then_new": selected([old, new]),
                "new_then_old": selected([new, old]),
            },
            "safe": False,
        },
    ]


# ---------------------------------------------------------------------------
# Proposed executable policy contract (test-only; not production code)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    attacker: str | None
    sequence: tuple[str, ...]
    interaction: str | None
    defender: str | None
    defender_move: str | None
    distance: str | None
    corner: bool | None
    patch: str | None
    dependency_fingerprint: str | None


@dataclass
class Claim:
    claim_id: str
    owner: str
    kind: str
    value: Any
    scenario: Scenario
    epistemic: str = "firsthand_observation"
    polarity: str = "affirmed"
    scope: str = "private"
    state: str = "draft"
    evidence_kind: str = "user_report"
    source_turn_ids: list[str] = field(default_factory=list)
    explicit_save_consent: bool = False
    explicit_share_consent: bool = False
    relations: list[tuple[str, str]] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)


class DesignLedger:
    """Small state machine used only to test the proposed trust contract."""

    REVIEWABLE_EVIDENCE = {
        "frame_step_video",
        "developer_reproduction",
        "official_source",
    }
    NON_ASSERTIONS = {"question", "instruction"}
    NON_OBSERVATIONS = {"hypothesis", "hearsay", "subjective_preference"}

    def __init__(self) -> None:
        self.claims: dict[str, Claim] = {}

    @staticmethod
    def missing_critical_fields(claim: Claim) -> list[str]:
        scenario = claim.scenario
        missing: list[str] = []
        if not scenario.attacker:
            missing.append("attacker")
        if len(scenario.sequence) < 2:
            missing.append("sequence")
        if not scenario.interaction:
            missing.append("interaction")
        if not scenario.patch or scenario.patch == "unknown":
            missing.append("patch")
        if claim.kind in {
            "sequence_outcome",
            "post_trade_advantage",
            "confirmed_followup",
        }:
            if not scenario.defender:
                missing.append("defender")
            if not scenario.defender_move:
                missing.append("defender_move")
        if claim.kind in {"spatial_outcome", "confirmed_followup"} and not scenario.distance:
            missing.append("distance")
        if claim.kind in {
            "sequence_outcome",
            "post_trade_advantage",
            "confirmed_followup",
            "spatial_outcome",
        } and not scenario.dependency_fingerprint:
            missing.append("dependency_fingerprint")
        return missing

    def capture(self, claim: Claim) -> Claim | None:
        # Speech-act classification is authoritative. Text that says "publish"
        # or "reviewed" never changes scope/state/permissions.
        if claim.epistemic in self.NON_ASSERTIONS:
            return None
        claim.scope = "private"
        claim.state = (
            "needs_clarification"
            if self.missing_critical_fields(claim)
            else "private_candidate"
        )
        self.claims[claim.claim_id] = claim
        return claim

    def confirm_private(self, claim_id: str) -> Claim:
        claim = self.claims[claim_id]
        if claim.state != "private_candidate":
            raise ValueError("claim is incomplete or unavailable")
        if not claim.explicit_save_consent:
            raise PermissionError("private save consent is required")
        # Hypotheses, hearsay and preferences may be remembered for the same
        # user, but keep their epistemic label and never become factual/shared
        # observations through this transition.
        claim.state = "confirmed_private"
        return claim

    def request_share(self, claim_id: str) -> Claim:
        claim = self.claims[claim_id]
        if claim.state != "confirmed_private":
            raise ValueError("only a confirmed private observation may be shared")
        if claim.epistemic in self.NON_OBSERVATIONS:
            raise ValueError("non-observation needs a separate editorial workflow")
        if not claim.explicit_share_consent:
            raise PermissionError("share consent is required")
        claim.state = "review_pending"
        return claim

    def approve(self, claim_id: str, *, reviewer_role: str) -> Claim:
        claim = self.claims[claim_id]
        if reviewer_role != "knowledge_reviewer":
            raise PermissionError("reviewer role is required")
        if claim.state != "review_pending":
            raise ValueError("claim is not pending review")
        if claim.evidence_kind not in self.REVIEWABLE_EVIDENCE:
            raise ValueError("user report alone is insufficient for publication")
        if claim.injection_flags:
            raise ValueError("quarantined content cannot be published")
        claim.state = "approved_shared"
        claim.scope = "community"
        return claim

    def correct(self, old_id: str, replacement: Claim) -> Claim:
        old = self.claims[old_id]
        if replacement.owner != old.owner:
            raise PermissionError("another user cannot correct this claim")
        captured = self.capture(replacement)
        if captured is None:
            raise ValueError("correction must contain an assertion")
        relation = "disputes" if old.state == "approved_shared" else "supersedes"
        captured.relations.append((relation, old_id))
        if relation == "supersedes":
            old.state = "superseded"
        return captured

    def retract(self, claim_id: str, *, requester: str) -> None:
        claim = self.claims[claim_id]
        if requester != claim.owner:
            raise PermissionError("only the owner may retract a private claim")
        claim.state = "withdrawn"

    def invalidate_patch(self, active_patch: str, fingerprints: set[str]) -> None:
        for claim in self.claims.values():
            if claim.state not in {"confirmed_private", "approved_shared"}:
                continue
            if (
                claim.scenario.patch != active_patch
                or claim.scenario.dependency_fingerprint not in fingerprints
            ):
                claim.state = "stale_patch"

    @staticmethod
    def _compatible(stored: Scenario, query: Scenario) -> bool:
        if stored.patch != query.patch:
            return False
        if stored.dependency_fingerprint != query.dependency_fingerprint:
            return False
        for name in (
            "attacker",
            "sequence",
            "interaction",
            "defender",
            "defender_move",
            "distance",
            "corner",
        ):
            value = getattr(stored, name)
            if value not in (None, (), "") and getattr(query, name) != value:
                return False
        return True

    def retrieve(self, scenario: Scenario, *, requester: str) -> dict[str, Any]:
        eligible: list[dict[str, Any]] = []
        for claim in self.claims.values():
            if not self._compatible(claim.scenario, scenario):
                continue
            if claim.state == "approved_shared":
                eligible.append({
                    "id": claim.claim_id,
                    "value": claim.value,
                    "label": "reviewed_shared_observation",
                    "numeric_authority": False,
                })
            elif claim.state == "confirmed_private" and claim.owner == requester:
                private_label = {
                    "hypothesis": "your_hypothesis",
                    "hearsay": "your_hearsay_note",
                    "subjective_preference": "your_preference",
                }.get(claim.epistemic, "your_unverified_memory")
                eligible.append({
                    "id": claim.claim_id,
                    "value": claim.value,
                    "label": private_label,
                    "numeric_authority": False,
                })
        values = {json.dumps(row["value"], sort_keys=True) for row in eligible}
        return {
            "status": "conflict" if len(values) > 1 else "resolved" if eligible else "empty",
            "items": eligible,
        }


def _complete_scenario(**changes: Any) -> Scenario:
    base = Scenario(
        attacker="sagat",
        sequence=("5MP", "5MP"),
        interaction="block",
        defender="ryu",
        defender_move="2LP",
        distance="point_blank",
        corner=False,
        patch=CURRENT_PATCH,
        dependency_fingerprint="sha256:frame-fixture-p2",
    )
    return replace(base, **changes)


def _claim(claim_id: str, owner: str = "user-a", **changes: Any) -> Claim:
    base = Claim(
        claim_id=claim_id,
        owner=owner,
        kind="post_trade_advantage",
        value=9,
        scenario=_complete_scenario(),
        explicit_save_consent=True,
        explicit_share_consent=True,
        source_turn_ids=[f"turn:{claim_id}"],
    )
    for name, value in changes.items():
        setattr(base, name, value)
    return base


def proposed_contract_tests() -> list[dict[str, Any]]:
    """Run release-blocking invariants against the proposed state contract."""
    results: list[dict[str, Any]] = []

    def check(name: str, test: Callable[[], None]) -> None:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report every contract failure
            results.append({"name": name, "passed": False, "error": repr(exc)})
        else:
            results.append({"name": name, "passed": True})

    def assert_raises(error: type[BaseException], operation: Callable[[], Any]) -> None:
        try:
            operation()
        except error:
            return
        raise AssertionError(f"expected {error.__name__}")

    def question_is_not_claim() -> None:
        ledger = DesignLedger()
        assert ledger.capture(_claim("q", epistemic="question")) is None

    def incomplete_claim_needs_clarification() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim(
            "missing",
            scenario=_complete_scenario(defender_move=None, patch=None),
        ))
        assert saved and saved.state == "needs_clarification"
        assert set(ledger.missing_critical_fields(saved)) >= {"defender_move", "patch"}

    def hypothesis_never_becomes_fact() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("hypothesis", epistemic="hypothesis"))
        assert saved and saved.state == "private_candidate"
        ledger.confirm_private(saved.claim_id)
        result = ledger.retrieve(saved.scenario, requester="user-a")
        assert result["items"][0]["label"] == "your_hypothesis"
        assert result["items"][0]["numeric_authority"] is False
        assert_raises(ValueError, lambda: ledger.request_share(saved.claim_id))

    def hearsay_never_becomes_fact() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("hearsay", epistemic="hearsay"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        result = ledger.retrieve(saved.scenario, requester="user-a")
        assert result["items"][0]["label"] == "your_hearsay_note"
        assert result["items"][0]["numeric_authority"] is False
        assert_raises(ValueError, lambda: ledger.request_share(saved.claim_id))

    def private_save_requires_consent() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("no-consent", explicit_save_consent=False))
        assert saved
        assert_raises(PermissionError, lambda: ledger.confirm_private(saved.claim_id))

    def private_claim_does_not_leak() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("private"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        assert ledger.retrieve(saved.scenario, requester="user-a")["items"]
        assert ledger.retrieve(saved.scenario, requester="user-b")["items"] == []

    def unreviewed_claim_is_not_shared() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("pending"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        ledger.request_share(saved.claim_id)
        assert ledger.retrieve(saved.scenario, requester="user-b")["items"] == []

    def user_cannot_self_approve() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("self-approve", evidence_kind="frame_step_video"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        ledger.request_share(saved.claim_id)
        assert_raises(
            PermissionError,
            lambda: ledger.approve(saved.claim_id, reviewer_role="contributor"),
        )

    def bare_user_report_cannot_publish() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("bare-report"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        ledger.request_share(saved.claim_id)
        assert_raises(
            ValueError,
            lambda: ledger.approve(saved.claim_id, reviewer_role="knowledge_reviewer"),
        )

    def reviewed_evidence_can_publish() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("reviewed", evidence_kind="developer_reproduction"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        ledger.request_share(saved.claim_id)
        approved = ledger.approve(saved.claim_id, reviewer_role="knowledge_reviewer")
        retrieved = ledger.retrieve(saved.scenario, requester="user-b")
        assert approved.scope == "community"
        assert retrieved["items"][0]["label"] == "reviewed_shared_observation"
        assert retrieved["items"][0]["numeric_authority"] is False

    def injection_text_cannot_publish() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim(
            "injection",
            evidence_kind="developer_reproduction",
            injection_flags=["instruction_override"],
        ))
        assert saved and saved.scope == "private"
        ledger.confirm_private(saved.claim_id)
        ledger.request_share(saved.claim_id)
        assert_raises(
            ValueError,
            lambda: ledger.approve(saved.claim_id, reviewer_role="knowledge_reviewer"),
        )

    def correction_supersedes_without_new_support() -> None:
        ledger = DesignLedger()
        original = ledger.capture(_claim("original"))
        assert original
        ledger.confirm_private(original.claim_id)
        corrected = ledger.correct("original", _claim("corrected", value=7))
        assert original.state == "superseded"
        assert corrected.relations == [("supersedes", "original")]
        assert len(corrected.source_turn_ids) == 1

    def verified_claim_is_disputed_not_overwritten() -> None:
        ledger = DesignLedger()
        original = ledger.capture(_claim(
            "published",
            evidence_kind="developer_reproduction",
        ))
        assert original
        ledger.confirm_private(original.claim_id)
        ledger.request_share(original.claim_id)
        ledger.approve(original.claim_id, reviewer_role="knowledge_reviewer")
        correction = ledger.correct("published", _claim("counterexample", value=7))
        assert original.state == "approved_shared"
        assert correction.relations == [("disputes", "published")]

    def conflicting_verified_values_are_not_last_write_wins() -> None:
        ledger = DesignLedger()
        for claim_id, value in (("plus-nine", 9), ("plus-seven", 7)):
            saved = ledger.capture(_claim(
                claim_id,
                value=value,
                evidence_kind="developer_reproduction",
            ))
            assert saved
            ledger.confirm_private(saved.claim_id)
            ledger.request_share(saved.claim_id)
            ledger.approve(saved.claim_id, reviewer_role="knowledge_reviewer")
        result = ledger.retrieve(_complete_scenario(), requester="user-c")
        assert result["status"] == "conflict"
        assert {row["value"] for row in result["items"]} == {7, 9}

    def condition_mismatch_is_not_retrieved() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("corner", scenario=_complete_scenario(corner=True)))
        assert saved
        ledger.confirm_private(saved.claim_id)
        assert ledger.retrieve(
            _complete_scenario(corner=False), requester="user-a"
        )["items"] == []

    def patch_and_fingerprint_mismatch_become_stale() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("old"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        ledger.invalidate_patch("fixture-p3", {"sha256:frame-fixture-p3"})
        assert saved.state == "stale_patch"
        assert ledger.retrieve(saved.scenario, requester="user-a")["items"] == []

    def withdrawal_removes_from_retrieval() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("withdraw"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        ledger.retract(saved.claim_id, requester="user-a")
        assert ledger.retrieve(saved.scenario, requester="user-a")["items"] == []

    def another_user_cannot_correct_or_retract() -> None:
        ledger = DesignLedger()
        saved = ledger.capture(_claim("owned"))
        assert saved
        ledger.confirm_private(saved.claim_id)
        assert_raises(
            PermissionError,
            lambda: ledger.correct("owned", _claim("foreign", owner="user-b")),
        )
        assert_raises(
            PermissionError,
            lambda: ledger.retract("owned", requester="user-b"),
        )

    checks = [
        ("question_is_not_claim", question_is_not_claim),
        ("incomplete_claim_needs_clarification", incomplete_claim_needs_clarification),
        ("hypothesis_never_becomes_fact", hypothesis_never_becomes_fact),
        ("hearsay_never_becomes_fact", hearsay_never_becomes_fact),
        ("private_save_requires_consent", private_save_requires_consent),
        ("private_claim_does_not_leak", private_claim_does_not_leak),
        ("unreviewed_claim_is_not_shared", unreviewed_claim_is_not_shared),
        ("user_cannot_self_approve", user_cannot_self_approve),
        ("bare_user_report_cannot_publish", bare_user_report_cannot_publish),
        ("reviewed_evidence_can_publish", reviewed_evidence_can_publish),
        ("injection_text_cannot_publish", injection_text_cannot_publish),
        ("correction_supersedes_without_new_support", correction_supersedes_without_new_support),
        ("verified_claim_is_disputed_not_overwritten", verified_claim_is_disputed_not_overwritten),
        ("conflicting_verified_values_are_not_last_write_wins", conflicting_verified_values_are_not_last_write_wins),
        ("condition_mismatch_is_not_retrieved", condition_mismatch_is_not_retrieved),
        ("patch_and_fingerprint_mismatch_become_stale", patch_and_fingerprint_mismatch_become_stale),
        ("withdrawal_removes_from_retrieval", withdrawal_removes_from_retrieval),
        ("another_user_cannot_correct_or_retract", another_user_cannot_correct_or_retract),
    ]
    for name, test in checks:
        check(name, test)
    return results


async def main() -> int:
    context = await current_context_probes()
    observation = current_observation_probes()
    contract = proposed_contract_tests()
    report = {
        "scope": "non-production executable design evaluation",
        "supercombo_runtime_reads": 0,
        "current_baseline": {
            "context": {
                "probes": len(context),
                "desired_matches": sum(row["desired_match"] for row in context),
                "results": context,
            },
            "observation_safety": {
                "probes": len(observation),
                "safe_results": sum(row["safe"] for row in observation),
                "results": observation,
            },
        },
        "proposed_contract": {
            "tests": len(contract),
            "passed": sum(row["passed"] for row in contract),
            "results": contract,
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(row["passed"] for row in contract) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
