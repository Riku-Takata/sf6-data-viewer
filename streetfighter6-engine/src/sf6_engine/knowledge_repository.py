"""Persistence boundary for private and reviewed tactical knowledge.

The repository never accepts an untyped raw chat message.  Callers must first
pass :class:`~sf6_engine.conversation_knowledge.KnowledgeCandidate`, which
contains a redacted excerpt, provenance hash, conditions and epistemic label.
The Discord bot uses a disabled repository by default; enabling Supabase
requires both the SQL migration and an explicit environment setting.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sf6_engine.conversation_knowledge import KnowledgeCandidate, TacticalScenario


class KnowledgeRepositoryError(RuntimeError):
    """Raised for unavailable storage or an invalid state transition."""


@dataclass(frozen=True)
class StoredKnowledgeClaim:
    """Repository-neutral representation used by answers and tests."""

    claim_id: str
    owner_subject_key: str
    candidate: KnowledgeCandidate
    workflow_state: str
    validity_state: str
    visibility_scope: str

    @property
    def scenario_key(self) -> str:
        return self.candidate.scenario.key

    def answer_label(self, requester_subject_key: str) -> str | None:
        if self.validity_state != "active":
            return None
        if self.workflow_state == "approved_shared" and self.visibility_scope == "community":
            return "レビュー済み共有検証"
        if (
            self.workflow_state == "confirmed_private"
            and self.visibility_scope == "private"
            and self.owner_subject_key == requester_subject_key
        ):
            labels = {
                "hypothesis": "あなたの仮説メモ（未検証）",
                "hearsay": "あなたの伝聞メモ（未検証）",
            }
            return labels.get(self.candidate.epistemic_basis, "あなたの未検証メモ")
        return None


class KnowledgeRepository(Protocol):
    """Minimal operations exposed to the bot; all writes are private first."""

    storage_label: str

    def save_confirmed_private(
        self,
        *,
        owner_subject_key: str,
        conversation_id: str,
        candidate: KnowledgeCandidate,
    ) -> StoredKnowledgeClaim:
        ...

    def retrieve(
        self,
        *,
        requester_subject_key: str,
        scenario: TacticalScenario,
    ) -> list[StoredKnowledgeClaim]:
        ...

    def request_share(self, *, claim_id: str, owner_subject_key: str) -> StoredKnowledgeClaim:
        ...

    def approve_shared(self, *, claim_id: str, reviewer_subject_key: str, evidence_kind: str) -> StoredKnowledgeClaim:
        ...

    def retract(self, *, claim_id: str, owner_subject_key: str) -> StoredKnowledgeClaim:
        ...


class DisabledKnowledgeRepository:
    """Default repository: session context works, persistent memory does not."""

    storage_label = "disabled"

    def save_confirmed_private(self, **_: Any) -> StoredKnowledgeClaim:
        raise KnowledgeRepositoryError(
            "永続メモは未設定です。SQL migration適用後に "
            "SF6_KNOWLEDGE_STORE=supabase を設定してください。"
        )

    def retrieve(self, **_: Any) -> list[StoredKnowledgeClaim]:
        return []

    def request_share(self, **_: Any) -> StoredKnowledgeClaim:
        raise KnowledgeRepositoryError("永続メモは未設定です。")

    def approve_shared(self, **_: Any) -> StoredKnowledgeClaim:
        raise KnowledgeRepositoryError("永続メモは未設定です。")

    def retract(self, **_: Any) -> StoredKnowledgeClaim:
        raise KnowledgeRepositoryError("永続メモは未設定です。")


class InMemoryKnowledgeRepository:
    """Process-local implementation for tests and local development only."""

    storage_label = "memory"
    _REVIEWABLE_EVIDENCE = {"frame_step_video", "developer_reproduction", "official_source"}
    _NON_SHAREABLE_EPISTEMIC = {"hypothesis", "hearsay", "subjective_preference"}

    def __init__(self) -> None:
        self._claims: dict[str, StoredKnowledgeClaim] = {}

    def save_confirmed_private(
        self,
        *,
        owner_subject_key: str,
        conversation_id: str,
        candidate: KnowledgeCandidate,
    ) -> StoredKnowledgeClaim:
        del conversation_id  # Session provenance is already in source_turn_id.
        claim = StoredKnowledgeClaim(
            claim_id=str(uuid.uuid4()),
            owner_subject_key=owner_subject_key,
            candidate=candidate,
            workflow_state="confirmed_private",
            validity_state="active",
            visibility_scope="private",
        )
        self._claims[claim.claim_id] = claim
        return claim

    def retrieve(
        self,
        *,
        requester_subject_key: str,
        scenario: TacticalScenario,
    ) -> list[StoredKnowledgeClaim]:
        matches = [
            claim for claim in self._claims.values()
            if claim.scenario_key == scenario.key
            and claim.answer_label(requester_subject_key) is not None
        ]
        values = {
            str(claim.candidate.payload)
            for claim in matches
            if claim.workflow_state == "approved_shared"
        }
        # A conflicting shared claim is never selected by input order.  The
        # caller receives every value and renders a conflict warning.
        return matches if len(values) <= 1 else matches

    def _owned(self, claim_id: str, owner_subject_key: str) -> StoredKnowledgeClaim:
        claim = self._claims.get(claim_id)
        if not claim:
            raise KnowledgeRepositoryError("knowledge claim not found")
        if claim.owner_subject_key != owner_subject_key:
            raise PermissionError("このメモを変更する権限がありません。")
        return claim

    def request_share(self, *, claim_id: str, owner_subject_key: str) -> StoredKnowledgeClaim:
        claim = self._owned(claim_id, owner_subject_key)
        if claim.workflow_state != "confirmed_private":
            raise KnowledgeRepositoryError("共有申請できるのは確認済みprivateメモだけです。")
        if claim.candidate.epistemic_basis in self._NON_SHAREABLE_EPISTEMIC:
            raise KnowledgeRepositoryError("仮説・伝聞・主観は共有事実として申請できません。")
        pending = StoredKnowledgeClaim(
            **{**claim.__dict__, "workflow_state": "review_pending"}
        )
        self._claims[claim_id] = pending
        return pending

    def approve_shared(self, *, claim_id: str, reviewer_subject_key: str, evidence_kind: str) -> StoredKnowledgeClaim:
        if not reviewer_subject_key:
            raise PermissionError("認証済みreviewerが必要です。")
        claim = self._claims.get(claim_id)
        if not claim:
            raise KnowledgeRepositoryError("knowledge claim not found")
        if claim.workflow_state != "review_pending":
            raise KnowledgeRepositoryError("review pendingではありません。")
        if evidence_kind not in self._REVIEWABLE_EVIDENCE:
            raise KnowledgeRepositoryError("ユーザー報告だけでは共有公開できません。")
        if claim.candidate.injection_flags:
            raise KnowledgeRepositoryError("quarantined candidateは公開できません。")
        approved = StoredKnowledgeClaim(
            **{
                **claim.__dict__,
                "workflow_state": "approved_shared",
                "visibility_scope": "community",
            }
        )
        self._claims[claim_id] = approved
        return approved

    def retract(self, *, claim_id: str, owner_subject_key: str) -> StoredKnowledgeClaim:
        claim = self._owned(claim_id, owner_subject_key)
        retracted = StoredKnowledgeClaim(
            **{**claim.__dict__, "validity_state": "withdrawn"}
        )
        self._claims[claim_id] = retracted
        return retracted


class SupabaseKnowledgeRepository:
    """Supabase implementation for the migration's service-only tables.

    The user-facing bot must provide a verified HMAC-derived subject key.  The
    tables have no anonymous/authenticated policies: only the application
    gateway/service account may access them until a subject JWT bridge is added.
    """

    storage_label = "supabase"

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from sf6_engine.db import get_write_client
            client = get_write_client()
        self.client = client

    @staticmethod
    def _row_to_claim(row: dict[str, Any]) -> StoredKnowledgeClaim:
        scenario_data = dict(row.get("scenario") or {})
        scenario = TacticalScenario(
            attacker_character=scenario_data.get("attacker_character"),
            attacker_sequence=tuple(scenario_data.get("attacker_sequence") or ()),
            initial_interaction=scenario_data.get("initial_interaction"),
            defender_character=scenario_data.get("defender_character"),
            defender_move=scenario_data.get("defender_move"),
            attacker_delay_f=scenario_data.get("attacker_delay_f"),
            defender_delay_f=scenario_data.get("defender_delay_f"),
            distance=scenario_data.get("distance"),
            corner=scenario_data.get("corner"),
            opponent_state=scenario_data.get("opponent_state"),
            counter_state=scenario_data.get("counter_state"),
            defender_burnout=scenario_data.get("defender_burnout"),
            game_version_id=scenario_data.get("game_version_id"),
            dependency_fingerprint=scenario_data.get("dependency_fingerprint"),
        )
        candidate = KnowledgeCandidate(
            claim_kind=str(row["claim_kind"]),
            scenario=scenario,
            payload=dict(row.get("payload") or {}),
            polarity=str(row.get("polarity") or "affirmed"),
            epistemic_basis=str(row.get("epistemic_basis") or "asserted_report"),
            evidence_type=str(row.get("evidence_type") or "user_report"),
            source_turn_id=str(row.get("source_turn_id") or ""),
            raw_text_sha256=str(row.get("raw_text_sha256") or ""),
            redacted_excerpt=str(row.get("redacted_excerpt") or ""),
            critical_unknowns=tuple(row.get("critical_unknowns") or ()),
            injection_flags=tuple(row.get("injection_flags") or ()),
        )
        return StoredKnowledgeClaim(
            claim_id=str(row["id"]),
            owner_subject_key=str(row["owner_subject_key"]),
            candidate=candidate,
            workflow_state=str(row["workflow_state"]),
            validity_state=str(row["validity_state"]),
            visibility_scope=str(row["visibility_scope"]),
        )

    def save_confirmed_private(
        self,
        *,
        owner_subject_key: str,
        conversation_id: str,
        candidate: KnowledgeCandidate,
    ) -> StoredKnowledgeClaim:
        self.client.table("knowledge_subjects").upsert({
            "subject_key": owner_subject_key,
            "platform": owner_subject_key.split(":", 1)[0],
        }, on_conflict="subject_key").execute()
        self.client.table("knowledge_turns").upsert({
            "id": candidate.source_turn_id,
            "conversation_id": conversation_id,
            "speaker_subject_key": owner_subject_key,
            "raw_text_sha256": candidate.raw_text_sha256,
            "redacted_excerpt": candidate.redacted_excerpt,
            "retention_expires_at": None,
        }, on_conflict="id").execute()
        # Consent is an auditable event, distinct from the claim itself.  A
        # duplicate private save records a new consent rather than silently
        # inferring permission from the presence of a claim.
        self.client.table("knowledge_consents").insert({
            "subject_key": owner_subject_key,
            "consent_kind": "private_memory",
            "granted": True,
            "source_turn_id": candidate.source_turn_id,
        }).execute()
        row = {
            "owner_subject_key": owner_subject_key,
            "scenario_key": candidate.scenario.key,
            "scenario": candidate.scenario.to_dict(),
            "claim_kind": candidate.claim_kind,
            "payload": candidate.payload,
            "polarity": candidate.polarity,
            "epistemic_basis": candidate.epistemic_basis,
            "evidence_type": candidate.evidence_type,
            "source_turn_id": candidate.source_turn_id,
            "raw_text_sha256": candidate.raw_text_sha256,
            "redacted_excerpt": candidate.redacted_excerpt,
            "critical_unknowns": list(candidate.critical_unknowns),
            "injection_flags": list(candidate.injection_flags),
            "workflow_state": "confirmed_private",
            "validity_state": "active",
            "visibility_scope": "private",
        }
        response = self.client.table("knowledge_claims").insert(row).execute()
        if not response.data:
            raise KnowledgeRepositoryError("private claim could not be stored")
        return self._row_to_claim(dict(response.data[0]))

    def retrieve(
        self,
        *,
        requester_subject_key: str,
        scenario: TacticalScenario,
    ) -> list[StoredKnowledgeClaim]:
        columns = "*"
        private = self.client.table("knowledge_claims").select(columns).eq(
            "owner_subject_key", requester_subject_key
        ).eq("scenario_key", scenario.key).eq("workflow_state", "confirmed_private").eq(
            "validity_state", "active"
        ).execute().data or []
        shared = self.client.table("knowledge_claims").select(columns).eq(
            "scenario_key", scenario.key
        ).eq("workflow_state", "approved_shared").eq("validity_state", "active").eq(
            "visibility_scope", "community"
        ).execute().data or []
        claims = [self._row_to_claim(dict(row)) for row in [*private, *shared]]
        return [claim for claim in claims if claim.answer_label(requester_subject_key)]

    def _owned_row(self, claim_id: str, owner_subject_key: str) -> dict[str, Any]:
        response = self.client.table("knowledge_claims").select("*").eq("id", claim_id).eq(
            "owner_subject_key", owner_subject_key
        ).limit(1).execute()
        if not response.data:
            raise PermissionError("このメモを変更する権限がありません。")
        return dict(response.data[0])

    def request_share(self, *, claim_id: str, owner_subject_key: str) -> StoredKnowledgeClaim:
        row = self._owned_row(claim_id, owner_subject_key)
        candidate = self._row_to_claim(row).candidate
        if row.get("workflow_state") != "confirmed_private":
            raise KnowledgeRepositoryError("共有申請できる状態ではありません。")
        if candidate.epistemic_basis in {"hypothesis", "hearsay", "subjective_preference"}:
            raise KnowledgeRepositoryError("仮説・伝聞・主観は共有事実として申請できません。")
        self.client.table("knowledge_consents").insert({
            "subject_key": owner_subject_key,
            "consent_kind": "share_request",
            "granted": True,
            "source_turn_id": row.get("source_turn_id"),
        }).execute()
        response = self.client.table("knowledge_claims").update({
            "workflow_state": "review_pending",
        }).eq("id", claim_id).eq("owner_subject_key", owner_subject_key).execute()
        return self._row_to_claim(dict(response.data[0]))

    def approve_shared(self, *, claim_id: str, reviewer_subject_key: str, evidence_kind: str) -> StoredKnowledgeClaim:
        if not reviewer_subject_key:
            raise PermissionError("認証済みreviewerが必要です。")
        if evidence_kind not in InMemoryKnowledgeRepository._REVIEWABLE_EVIDENCE:
            raise KnowledgeRepositoryError("ユーザー報告だけでは共有公開できません。")
        response = self.client.table("knowledge_claims").select("*").eq("id", claim_id).limit(1).execute()
        if not response.data:
            raise KnowledgeRepositoryError("knowledge claim not found")
        row = dict(response.data[0])
        candidate = self._row_to_claim(row).candidate
        if row.get("workflow_state") != "review_pending" or candidate.injection_flags:
            raise KnowledgeRepositoryError("このcandidateは公開できません。")
        self.client.table("knowledge_subjects").upsert({
            "subject_key": reviewer_subject_key,
            "platform": reviewer_subject_key.split(":", 1)[0],
        }, on_conflict="subject_key").execute()
        self.client.table("knowledge_reviews").insert({
            "claim_id": claim_id,
            "reviewer_subject_key": reviewer_subject_key,
            "decision": "approved",
            "evidence_kind": evidence_kind,
        }).execute()
        # A review cannot turn an unsupported chat report into shared fact.
        # Store the independently supplied verification method as typed
        # evidence before exposing the claim to the community query.
        self.client.table("knowledge_evidence").insert({
            "claim_id": claim_id,
            "relation": "supports",
            "evidence_kind": evidence_kind,
        }).execute()
        updated = self.client.table("knowledge_claims").update({
            "workflow_state": "approved_shared",
            "visibility_scope": "community",
        }).eq("id", claim_id).execute()
        return self._row_to_claim(dict(updated.data[0]))

    def retract(self, *, claim_id: str, owner_subject_key: str) -> StoredKnowledgeClaim:
        self._owned_row(claim_id, owner_subject_key)
        response = self.client.table("knowledge_claims").update({
            "validity_state": "withdrawn",
        }).eq("id", claim_id).eq("owner_subject_key", owner_subject_key).execute()
        return self._row_to_claim(dict(response.data[0]))


def create_default_repository() -> KnowledgeRepository:
    """Create storage only when explicitly configured by the operator.

    ``disabled`` is deliberately the default: applying the migration and
    configuring an HMAC identity secret are prerequisites for persistence.
    ``memory`` is useful for local demonstrations and tests, but is labelled
    as process-local to callers.
    """
    mode = os.environ.get("SF6_KNOWLEDGE_STORE", "disabled").casefold()
    if mode == "supabase":
        return SupabaseKnowledgeRepository()
    if mode == "memory":
        return InMemoryKnowledgeRepository()
    return DisabledKnowledgeRepository()
