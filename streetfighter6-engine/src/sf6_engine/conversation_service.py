"""Safe orchestration for conversational private tactical memory."""
from __future__ import annotations

import uuid
import time
from dataclasses import dataclass
from typing import Any, Mapping

from sf6_engine.conversation_knowledge import (
    ConversationContextStore,
    ConversationKey,
    DialogueTurnAnalysis,
    KnowledgeCandidate,
    compile_dialogue_turn,
    is_save_confirmation,
)
from sf6_engine.knowledge_repository import (
    KnowledgeRepository,
    KnowledgeRepositoryError,
    StoredKnowledgeClaim,
    create_default_repository,
)


PENDING_SAVE_TTL_SECONDS = 5 * 60


@dataclass(frozen=True)
class ConversationTurnResult:
    """Result for one normal chat turn; no raw text is retained here."""

    analysis: DialogueTurnAnalysis
    private_context: str | None
    save_confirmation_required: bool
    save_message: str | None


@dataclass(frozen=True)
class SaveConfirmationResult:
    """Result after a user explicitly confirms a pending private save."""

    saved: bool
    message: str
    claim: StoredKnowledgeClaim | None = None


class ConversationKnowledgeService:
    """Owns short-lived context and the explicit private-save interaction.

    A pending candidate stays in process memory until the user says exactly
    ``保存する``.  The initial report is therefore not persisted merely because
    it happened to contain tactical information.
    """

    def __init__(
        self,
        *,
        repository: KnowledgeRepository | None = None,
        context_store: ConversationContextStore | None = None,
    ) -> None:
        self.repository = repository or create_default_repository()
        self.context_store = context_store or ConversationContextStore()
        self._pending: dict[ConversationKey, tuple[KnowledgeCandidate, float]] = {}

    @staticmethod
    def _turn_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _private_context(claims: list[StoredKnowledgeClaim], subject_key: str) -> str | None:
        if not claims:
            return None
        values = {
            str(claim.candidate.payload)
            for claim in claims
            if claim.workflow_state == "approved_shared"
        }
        conflict = len(values) > 1
        lines: list[str] = []
        for claim in claims[:3]:
            label = claim.answer_label(subject_key)
            if not label:
                continue
            statement = claim.candidate.redacted_excerpt
            lines.append(f"【{label}】{statement}")
        if conflict:
            lines.append("【共有観測の競合】同一条件の共有観測が競合しているため、確定値として使いません。")
        return "\n".join(lines) or None

    def process_turn(
        self,
        *,
        text: str,
        intent: Mapping[str, Any],
        conversation_id: str,
        subject_key: str,
        game_version_id: str | None = None,
        dependency_fingerprint: str | None = None,
    ) -> ConversationTurnResult:
        key = ConversationKey(conversation_id=conversation_id, subject_key=subject_key)
        analysis = compile_dialogue_turn(
            text,
            intent,
            context=self.context_store.get(key),
            turn_id=self._turn_id(),
            game_version_id=game_version_id,
            dependency_fingerprint=dependency_fingerprint,
        )
        self.context_store.update(key, analysis, turn_id=(analysis.candidate.source_turn_id if analysis.candidate else self._turn_id()))

        private_context = None
        if analysis.scenario.attacker_character and len(analysis.scenario.attacker_sequence) >= 2:
            private_context = self._private_context(
                self.repository.retrieve(
                    requester_subject_key=subject_key,
                    scenario=analysis.scenario,
                ),
                subject_key,
            )

        if not analysis.save_requested:
            return ConversationTurnResult(analysis, private_context, False, None)
        if not analysis.candidate:
            return ConversationTurnResult(
                analysis,
                private_context,
                False,
                "質問だけはメモとして保存しません。実測した連携・状況・結果を含めてください。",
            )
        if self.repository.storage_label == "disabled":
            return ConversationTurnResult(
                analysis,
                private_context,
                False,
                "永続メモは未設定です。migration適用後に安全なsubject keyと "
                "SF6_KNOWLEDGE_STORE=supabase を設定してください。",
            )
        if self.repository.storage_label == "supabase" and subject_key.startswith("session:"):
            return ConversationTurnResult(
                analysis,
                private_context,
                False,
                "永続メモにはSF6_KNOWLEDGE_SUBJECT_SECRETの設定が必要です。"
                "Discord IDそのものは保存しません。",
            )
        if analysis.injection_flags:
            return ConversationTurnResult(
                analysis,
                private_context,
                False,
                "この内容には権限・公開を指示する文が含まれるため、知識メモとして保存できません。",
            )
        self._pending[key] = (analysis.candidate, time.time() + PENDING_SAVE_TTL_SECONDS)
        unknown = "、".join(analysis.clarification_fields)
        suffix = (
            f" 未指定の条件（{unknown}）は未検証のまま記録されます。"
            if unknown else ""
        )
        return ConversationTurnResult(
            analysis,
            private_context,
            True,
            "この内容をあなた専用の未検証メモとして保存しますか？"
            "保存する場合は5分以内に「保存する」と返信してください。" + suffix,
        )

    def has_pending_save(self, *, conversation_id: str, subject_key: str) -> bool:
        key = ConversationKey(conversation_id, subject_key)
        pending = self._pending.get(key)
        if not pending:
            return False
        if pending[1] <= time.time():
            self._pending.pop(key, None)
            return False
        return True

    def confirm_pending_save(
        self,
        *,
        text: str,
        conversation_id: str,
        subject_key: str,
    ) -> SaveConfirmationResult:
        key = ConversationKey(conversation_id, subject_key)
        if not is_save_confirmation(text):
            return SaveConfirmationResult(False, "保存する場合は「保存する」と返信してください。")
        pending = self._pending.pop(key, None)
        if not pending or pending[1] <= time.time():
            return SaveConfirmationResult(False, "保存待ちのメモはありません。")
        candidate = pending[0]
        try:
            claim = self.repository.save_confirmed_private(
                owner_subject_key=subject_key,
                conversation_id=conversation_id,
                candidate=candidate,
            )
        except KnowledgeRepositoryError as exc:
            return SaveConfirmationResult(False, str(exc))
        persistent = "永続" if self.repository.storage_label == "supabase" else "このBot稼働中のみ"
        return SaveConfirmationResult(
            True,
            f"保存しました（{persistent}・あなた専用・未検証）。"
            "共有回答への利用には別途証拠とreviewが必要です。",
            claim,
        )
