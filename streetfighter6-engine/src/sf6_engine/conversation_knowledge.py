"""Conversation-scoped tactical knowledge extraction and safety policy.

This module is deliberately independent from SuperCombo tables and from the
frame calculator.  It turns one user utterance plus the user's own short-lived
conversation context into a typed knowledge candidate.  Persistence and
review live in :mod:`sf6_engine.knowledge_repository`.

The compiler is conservative by design:

* a question is never a claim;
* hypotheses and hearsay retain their epistemic label;
* follow-ups may inherit one unambiguous, same-user sequence anchor only;
* missing patch/variant/spacing fields are recorded as unknown, not defaulted;
* text that asks to change system instructions or publication state is data,
  never an authorization instruction.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

from sf6_engine.frame_scenario import merge_frame_scenarios, parse_frame_scenario


CONVERSATION_SCHEMA_VERSION = 1
DEFAULT_SESSION_TTL_SECONDS = 30 * 60
MAX_REDACTED_EXCERPT_CHARS = 500

_SAVE_REQUEST_RE = re.compile(r"(?:記録|保存|覚え(?:て)?|学習)(?:して|したい|します|しておいて)?")
_CONFIRM_RE = re.compile(r"^(?:保存する|保存します|はい、保存|記録する|記録します)$")
_REFERENCE_RE = re.compile(r"(?:その時|その後|さっき|先ほど|これ|それ|こっち|あの連携)")
_CORRECTION_RE = re.compile(r"(?:じゃなく(?:て)?|ではなく|やっぱり|訂正|ごめん|取り消)")
_HEARSAY_RE = re.compile(r"(?:友達|フレンド|Wiki|ウィキ|聞いた|言ってた|らしい)", re.IGNORECASE)
_HYPOTHESIS_RE = re.compile(r"(?:たぶん|おそらく|と思う|はず|かも)")
_FIRSTHAND_RE = re.compile(r"(?:トレモ|トレーニング|試した|確認した|録画|検証した|相打ちした|つながった)")
_INJECTION_PATTERNS = (
    re.compile(r"(?:前(?:の)?指示|system\s*prompt|開発者指示).{0,24}(?:無視|上書き)", re.IGNORECASE),
    re.compile(r"(?:reviewed|レビュー済み|公開|管理者).{0,24}(?:にして|にしろ|として保存)", re.IGNORECASE),
)
_MOVE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:j\.)?[1-9][LMH][PK](?![A-Za-z0-9])", re.IGNORECASE)
_ADVANTAGE_RE = re.compile(r"(?<!\d)([+-]\d+)\s*(?:F|フレーム)?")


@dataclass(frozen=True)
class ConversationKey:
    """One user's isolated conversation/session partition."""

    conversation_id: str
    subject_key: str


@dataclass
class DialogueContext:
    """Short-lived same-user context; it is not persistent knowledge."""

    last_intent: dict[str, Any] | None = None
    last_scenario: dict[str, Any] | None = None
    last_turn_id: str | None = None
    expires_at: float = 0.0

    def active(self, now: float | None = None) -> bool:
        return self.expires_at > (time.time() if now is None else now)


@dataclass(frozen=True)
class TacticalScenario:
    """Normalized condition identity.  Result values are intentionally absent."""

    attacker_character: str | None
    attacker_sequence: tuple[str, ...]
    initial_interaction: str | None
    defender_character: str | None
    defender_move: str | None
    attacker_delay_f: int | None
    defender_delay_f: int | None
    distance: str | None
    corner: bool | None
    opponent_state: str | None
    counter_state: str | None
    defender_burnout: bool | None
    game_version_id: str | None
    dependency_fingerprint: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attacker_character": self.attacker_character,
            "attacker_sequence": list(self.attacker_sequence),
            "initial_interaction": self.initial_interaction,
            "defender_character": self.defender_character,
            "defender_move": self.defender_move,
            "attacker_delay_f": self.attacker_delay_f,
            "defender_delay_f": self.defender_delay_f,
            "distance": self.distance,
            "corner": self.corner,
            "opponent_state": self.opponent_state,
            "counter_state": self.counter_state,
            "defender_burnout": self.defender_burnout,
            "game_version_id": self.game_version_id,
            "dependency_fingerprint": self.dependency_fingerprint,
        }

    @property
    def key(self) -> str:
        canonical = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KnowledgeCandidate:
    """An attributed, non-authoritative candidate produced from one turn."""

    claim_kind: str
    scenario: TacticalScenario
    payload: dict[str, Any]
    polarity: str
    epistemic_basis: str
    evidence_type: str
    source_turn_id: str
    raw_text_sha256: str
    redacted_excerpt: str
    critical_unknowns: tuple[str, ...]
    injection_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_kind": self.claim_kind,
            "scenario": self.scenario.to_dict(),
            "scenario_key": self.scenario.key,
            "payload": self.payload,
            "polarity": self.polarity,
            "epistemic_basis": self.epistemic_basis,
            "evidence_type": self.evidence_type,
            "source_turn_id": self.source_turn_id,
            "raw_text_sha256": self.raw_text_sha256,
            "redacted_excerpt": self.redacted_excerpt,
            "critical_unknowns": list(self.critical_unknowns),
            "injection_flags": list(self.injection_flags),
        }


@dataclass(frozen=True)
class DialogueTurnAnalysis:
    """Typed result passed to the bot and persistence layer."""

    schema_version: int
    resolved_intent: dict[str, Any]
    speech_acts: tuple[str, ...]
    references: tuple[dict[str, Any], ...]
    state_ops: tuple[dict[str, Any], ...]
    scenario: TacticalScenario
    candidate: KnowledgeCandidate | None
    save_requested: bool
    clarification_fields: tuple[str, ...]
    injection_flags: tuple[str, ...]


def derive_subject_key(platform: str, external_id: str | int, secret: str | None = None) -> str | None:
    """Return a stable non-reversible subject key; never persist platform IDs.

    Returning ``None`` without a configured secret deliberately disables
    persistent private memory instead of silently storing a Discord ID.
    """
    key = secret or os.environ.get("SF6_KNOWLEDGE_SUBJECT_SECRET")
    if not key:
        return None
    message = f"{platform}:{external_id}".encode("utf-8")
    digest = hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{platform}:{digest}"


def redact_excerpt(text: str) -> str:
    """Keep a short quote for review without retaining common direct PII."""
    redacted = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", text)
    redacted = re.sub(r"(?:https?://|discord\.gg/)\S+", "[url]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"<@!?\d+>", "[mention]", redacted)
    redacted = re.sub(r"(?<!\d)(?:\+?81[-\s]?)?0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}(?!\d)", "[phone]", redacted)
    redacted = re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", "[ip]", redacted)
    redacted = re.sub(r"[\x00-\x1f\x7f]", " ", redacted)
    return re.sub(r"\s+", " ", redacted).strip()[:MAX_REDACTED_EXCERPT_CHARS]


def _injection_flags(text: str) -> tuple[str, ...]:
    return tuple(
        f"suspicious_instruction_{index}"
        for index, pattern in enumerate(_INJECTION_PATTERNS, start=1)
        if pattern.search(text)
    )


def _epistemic_basis(text: str) -> str:
    if _HEARSAY_RE.search(text):
        return "hearsay"
    if _HYPOTHESIS_RE.search(text):
        return "hypothesis"
    if _FIRSTHAND_RE.search(text):
        return "firsthand_observation"
    return "asserted_report"


def _speech_acts(text: str, intent: Mapping[str, Any]) -> tuple[str, ...]:
    acts: list[str] = []
    if "?" in text or "？" in text or re.search(r"(?:どう|何F|なに|ですか|教えて)", text):
        acts.append("ask")
    if _CORRECTION_RE.search(text):
        acts.append("correct")
    if re.search(r"(?:取り消|撤回)", text):
        acts.append("retract")
    if re.search(r"(?:相打ち|つなが|重なる|スカ|潰せ|確認|試した|トレモ|\+\d|−\d|-\d)", text):
        acts.append("report")
    if intent.get("intent_type") == "sequence_analysis" and "ask" not in acts:
        acts.append("report")
    return tuple(dict.fromkeys(acts))


def _explicit_scenario_corrections(text: str) -> dict[str, Any]:
    """Correct high-risk Japanese negation pairs before generic parsing."""
    corrected: dict[str, Any] = {}
    if re.search(r"密着(?:じゃなく|ではなく).{0,12}先端", text):
        corrected["distance"] = "tip"
    elif re.search(r"先端(?:じゃなく|ではなく).{0,12}(?:密着|至近距離)", text):
        corrected["distance"] = "point_blank"
    if re.search(r"ガード(?:じゃなく|ではなく).{0,12}ヒット", text):
        corrected["interaction"] = "hit"
    elif re.search(r"ヒット(?:じゃなく|ではなく).{0,12}ガード", text):
        corrected["interaction"] = "block"
    if re.search(r"相手(?:は|が)?バーンアウト(?:じゃない|ではない|していない)", text):
        corrected["defender_burnout"] = False
    return corrected


def _meaningful(value: Any) -> bool:
    return value not in (None, "", [], {})


def _merge_intent_with_context(
    current: Mapping[str, Any],
    context: DialogueContext | None,
    text: str,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Resolve a same-user follow-up against one unambiguous sequence anchor."""
    result = copy.deepcopy(dict(current))
    references: list[dict[str, Any]] = []
    state_ops: list[dict[str, Any]] = []
    # ConversationContextStore already enforces TTL.  Keeping this helper
    # independent of wall-clock time also makes a supplied frozen context
    # deterministic in tests and offline replay.
    if not context or not context.last_intent or not _REFERENCE_RE.search(text):
        return result, tuple(references), tuple(state_ops)

    anchor = context.last_intent
    if anchor.get("intent_type") != "sequence_analysis":
        references.append({"status": "unresolved", "reason": "no_sequence_anchor"})
        return result, tuple(references), tuple(state_ops)

    references.append({
        "status": "resolved",
        "target": "last_sequence",
        "source_turn_id": context.last_turn_id,
    })
    for field in ("chara", "attacker_sequence", "attacker_timing", "initial_interaction", "defender_action"):
        if not _meaningful(result.get(field)) and _meaningful(anchor.get(field)):
            result[field] = copy.deepcopy(anchor[field])
    if result.get("intent_type") == "general_question":
        result["intent_type"] = "sequence_analysis"

    inherited = anchor.get("scenario") or {}
    parsed = result.get("scenario") or {}
    merged = merge_frame_scenarios(parsed, inherited)
    corrections = _explicit_scenario_corrections(text)
    if corrections:
        merged = {**merged, **corrections}
        specified = list(merged.get("specified") or [])
        for field in corrections:
            if field not in specified:
                specified.append(field)
        merged["specified"] = specified
        state_ops.extend({"op": "replace", "path": f"/scenario/{field}", "value": value}
                         for field, value in corrections.items())
    if merged:
        result["scenario"] = merged
    return result, tuple(references), tuple(state_ops)


def _scenario_from_intent(intent: Mapping[str, Any], *, game_version_id: str | None, dependency_fingerprint: str | None) -> TacticalScenario:
    defender = intent.get("defender_action") or {}
    timing = intent.get("attacker_timing") or {}
    raw_scenario = dict(intent.get("scenario") or parse_frame_scenario(str(intent.get("raw_query") or "")))
    raw_scenario.update(_explicit_scenario_corrections(str(intent.get("raw_query") or "")))
    sequence = intent.get("attacker_sequence") or []
    return TacticalScenario(
        attacker_character=intent.get("chara"),
        attacker_sequence=tuple(str(item) for item in sequence),
        initial_interaction=intent.get("initial_interaction") or raw_scenario.get("interaction"),
        defender_character=defender.get("character"),
        defender_move=defender.get("move"),
        attacker_delay_f=timing.get("delay_f") if isinstance(timing.get("delay_f"), int) else None,
        defender_delay_f=defender.get("delay_f") if isinstance(defender.get("delay_f"), int) else None,
        distance=raw_scenario.get("distance"),
        corner=raw_scenario.get("corner") if isinstance(raw_scenario.get("corner"), bool) else None,
        opponent_state=raw_scenario.get("opponent_state"),
        counter_state=raw_scenario.get("counter_state"),
        defender_burnout=(raw_scenario.get("defender_burnout")
                          if isinstance(raw_scenario.get("defender_burnout"), bool) else None),
        game_version_id=game_version_id,
        dependency_fingerprint=dependency_fingerprint,
    )


def _critical_unknowns(scenario: TacticalScenario, claim_kind: str) -> tuple[str, ...]:
    missing: list[str] = []
    if not scenario.attacker_character:
        missing.append("attacker_character")
    if len(scenario.attacker_sequence) < 2:
        missing.append("attacker_sequence")
    if not scenario.initial_interaction:
        missing.append("initial_interaction")
    if claim_kind in {"sequence_observation", "post_trade_advantage", "confirmed_followup"}:
        if not scenario.defender_character:
            missing.append("defender_character")
        if not scenario.defender_move:
            missing.append("defender_move")
    if claim_kind in {"confirmed_followup", "spatial_outcome"} and not scenario.distance:
        missing.append("distance")
    if not scenario.game_version_id:
        missing.append("game_version_id")
    if not scenario.dependency_fingerprint:
        missing.append("dependency_fingerprint")
    return tuple(missing)


def _claim_kind(text: str, intent: Mapping[str, Any]) -> str | None:
    if re.search(r"(?:つなが|繋が|コンボになる|追撃)", text):
        return "confirmed_followup"
    if re.search(r"(?:相打ち|一方勝ち|潰せ|スカ|空振)", text):
        return "sequence_observation"
    if re.search(r"(?:重なる|投げ抜け狩り|起き攻め|セットプレイ|対策)", text):
        return "tactical_pattern"
    if intent.get("intent_type") == "sequence_analysis" and re.search(r"(?:\+|−|-)?\d+\s*F", text):
        return "post_trade_advantage"
    return None


def _payload(text: str, kind: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"statement": redact_excerpt(text)}
    advantage = _ADVANTAGE_RE.search(text)
    if advantage:
        payload["attacker_advantage_f"] = int(advantage.group(1))
    if kind == "confirmed_followup":
        moves = _MOVE_TOKEN_RE.findall(text)
        if moves:
            payload["followup_move"] = moves[-1].upper().replace("J.", "j.")
    if "相打ち" in text:
        payload["outcome"] = "trade"
    elif re.search(r"(?:一方勝ち|潰せ)", text):
        payload["outcome"] = "attacker_hit"
    elif re.search(r"(?:スカ|空振)", text):
        payload["outcome"] = "whiff"
    return payload


def _polarity(text: str) -> str:
    if re.search(r"(?:相打ち|つなが|重なる|成立).{0,8}(?:ない|なかった|しない|しなかった)", text):
        return "negated"
    return "affirmed"


def compile_dialogue_turn(
    text: str,
    intent: Mapping[str, Any],
    *,
    context: DialogueContext | None = None,
    turn_id: str,
    game_version_id: str | None = None,
    dependency_fingerprint: str | None = None,
) -> DialogueTurnAnalysis:
    """Compile one turn without persisting it or granting any privilege."""
    resolved, references, state_ops = _merge_intent_with_context(intent, context, text)
    resolved["raw_query"] = text
    # Explicit correction pairs apply even when there is no previous anchor.
    corrections = _explicit_scenario_corrections(text)
    if corrections and not state_ops:
        current_scenario = resolved.get("scenario") or {}
        merged = {**current_scenario, **corrections}
        merged["specified"] = list(dict.fromkeys([
            *(current_scenario.get("specified") or []), *corrections.keys(),
        ]))
        resolved["scenario"] = merged
        state_ops = tuple({"op": "replace", "path": f"/scenario/{field}", "value": value}
                          for field, value in corrections.items())

    acts = _speech_acts(text, resolved)
    kind = _claim_kind(text, resolved) if "report" in acts else None
    # "つながる？" is a question, whereas "つながったが、その後は？" contains
    # an independently asserted observation plus a question.  Only the latter
    # may create a candidate.
    if (
        "ask" in acts
        and not re.search(r"(?:相打ちした|つながった|重なった|試した|確認した|検証した)", text)
    ):
        kind = None
    flags = _injection_flags(text)
    scenario = _scenario_from_intent(
        resolved,
        game_version_id=game_version_id,
        dependency_fingerprint=dependency_fingerprint,
    )
    candidate = None
    unknowns: tuple[str, ...] = ()
    if kind:
        unknowns = _critical_unknowns(scenario, kind)
        candidate = KnowledgeCandidate(
            claim_kind=kind,
            scenario=scenario,
            payload=_payload(text, kind),
            polarity=_polarity(text),
            epistemic_basis=_epistemic_basis(text),
            evidence_type="user_report",
            source_turn_id=turn_id,
            raw_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            redacted_excerpt=redact_excerpt(text),
            critical_unknowns=unknowns,
            injection_flags=flags,
        )
    return DialogueTurnAnalysis(
        schema_version=CONVERSATION_SCHEMA_VERSION,
        resolved_intent=resolved,
        speech_acts=acts,
        references=references,
        state_ops=state_ops,
        scenario=scenario,
        candidate=candidate,
        save_requested=bool(_SAVE_REQUEST_RE.search(text)),
        clarification_fields=unknowns,
        injection_flags=flags,
    )


class ConversationContextStore:
    """In-memory session context; keys always include the user subject."""

    def __init__(self, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._contexts: dict[ConversationKey, DialogueContext] = {}

    def get(self, key: ConversationKey, *, now: float | None = None) -> DialogueContext | None:
        context = self._contexts.get(key)
        if context and context.active(now):
            return context
        self._contexts.pop(key, None)
        return None

    def update(self, key: ConversationKey, analysis: DialogueTurnAnalysis, *, turn_id: str, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        resolved = analysis.resolved_intent
        if resolved.get("intent_type") != "sequence_analysis":
            return
        self._contexts[key] = DialogueContext(
            last_intent=copy.deepcopy(resolved),
            last_scenario=copy.deepcopy(resolved.get("scenario") or {}),
            last_turn_id=turn_id,
            expires_at=timestamp + self.ttl_seconds,
        )


def is_save_confirmation(text: str) -> bool:
    """Return true only for a short, explicit confirmation phrase."""
    return bool(_CONFIRM_RE.fullmatch(re.sub(r"\s+", "", text)))
