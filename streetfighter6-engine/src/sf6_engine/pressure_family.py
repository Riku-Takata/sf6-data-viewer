"""Compare every applicable variant of an underspecified pressure family.

An ordinary move lookup must abstain when a name means several strength
variants.  An interruption question is different: comparing those variants is
the useful answer.  This module resolves the family once, then delegates every
concrete pair to :mod:`sf6_engine.sequence_analysis` so the timeline formula
remains identical to an explicit ``A -> B`` question.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from sf6_engine.frame_data import lookup_frame_data
from sf6_engine.pressure_defaults import resolve_reviewed_pressure_default
from sf6_engine.sequence_analysis import analyze_sequence


_OD_RE = re.compile(r"(?:\b(?:od|ex|overdrive)\b|オーバードライブ)", re.IGNORECASE)
_VARIANT_ORDER = (
    (re.compile(r"(?:弱|light)", re.IGNORECASE), 0),
    (re.compile(r"(?:中|medium)", re.IGNORECASE), 1),
    (re.compile(r"(?:強|heavy)", re.IGNORECASE), 2),
    (_OD_RE, 3),
)


def _default_context(character: str, family: str) -> dict[str, Any] | None:
    return resolve_reviewed_pressure_default(character, family)


def _is_overdrive(candidate: dict[str, Any]) -> bool:
    names = " ".join(str(name) for name in candidate.get("names") or ())
    return bool(_OD_RE.search(names))


def _variant_sort_key(candidate: dict[str, Any]) -> tuple[int, str]:
    names = " ".join(str(name) for name in candidate.get("names") or ())
    for pattern, rank in _VARIANT_ORDER:
        if pattern.search(names):
            return rank, str(candidate.get("input") or "")
    return len(_VARIANT_ORDER), str(candidate.get("input") or "")


def _family_candidates(lookup: dict[str, Any], *, variant_scope: str) -> list[dict[str, Any]]:
    """Return one executable input per candidate variant from a lookup result."""
    resolution = lookup.get("resolution") or (
        ((lookup.get("move") or {}).get("frame_profile") or {}).get("resolution") or {}
    )
    raw_candidates: Iterable[dict[str, Any]] = resolution.get("candidates") or ()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        input_ = raw.get("input")
        if not input_ or input_ in seen:
            continue
        candidate = {
            "input": str(input_),
            "names": list(raw.get("names") or ()),
        }
        if variant_scope == "normal" and _is_overdrive(candidate):
            continue
        seen.add(candidate["input"])
        candidates.append(candidate)

    if candidates:
        return sorted(candidates, key=_variant_sort_key)

    selected = resolution.get("selected") or {}
    selected_input = selected.get("input")
    if selected_input:
        candidate = {"input": str(selected_input), "names": list(selected.get("names") or ())}
        if variant_scope != "normal" or not _is_overdrive(candidate):
            return [candidate]
    return []


def _display_name(candidate: dict[str, Any], input_: str) -> str:
    names = [str(name) for name in candidate.get("names") or () if name]
    japanese = next((name for name in names if re.search(r"[ぁ-んァ-ン一-龥]", name)), None)
    if japanese:
        return japanese
    variant_name = next(
        (
            name for name in names
            if any(pattern.search(name) for pattern, _rank in _VARIANT_ORDER)
        ),
        None,
    )
    return variant_name or (names[0] if names else input_)


def _variant_summary_line(
    candidate: dict[str, Any],
    analysis: dict[str, Any],
) -> str:
    sequence = analysis.get("attacker_sequence") or []
    second = sequence[1] if len(sequence) > 1 else {}
    input_ = str(second.get("input") or candidate["input"])
    startup = second.get("startup_f")
    name = str(second.get("name") or _display_name(candidate, input_))
    label = f"{name}（{input_}" + (f"、発生{startup}F" if isinstance(startup, int) else "") + "）"
    timeline = analysis.get("timeline") or {}
    gap = timeline.get("actionable_gap_f")
    if not isinstance(gap, int):
        return f"- {label}: 技間の単一フレーム値を確定できないため判定保留です。"
    if gap < 0:
        return f"- {label}: 連続ガードです。防御側が動ける{abs(gap)}F前に発生します。"
    if gap == 0:
        return f"- {label}: 連続ガードです。技間の隙間は0Fです。"
    return (
        f"- {label}: 技間は{gap}Fです。フレーム上、発生{gap - 1}F以下なら割り込め、"
        f"発生{gap}Fは同時です。"
    )


def _family_conclusion(variants: Iterable[dict[str, Any]]) -> str:
    """Return the direct answer before the per-variant evidence."""
    interruptible: list[tuple[str, int]] = []
    for variant in variants:
        analysis = variant.get("analysis") or {}
        gap = (analysis.get("timeline") or {}).get("actionable_gap_f")
        if not isinstance(gap, int) or gap <= 0:
            continue
        sequence = analysis.get("attacker_sequence") or []
        second = sequence[1] if len(sequence) > 1 else {}
        input_ = str(second.get("input") or variant.get("input") or "")
        name = str(second.get("name") or _display_name(variant, input_))
        interruptible.append((name, gap))
    if not interruptible:
        return "いいえ、比較対象の技間は連続ガードのため、通常入力の技では割り込めません。"
    if len(interruptible) == 1:
        name, gap = interruptible[0]
        return (
            f"はい、{name}だけに{gap}Fの隙間があり、"
            f"発生{gap - 1}F以下ならフレーム上割り込めます。"
        )
    names = "、".join(name for name, _gap in interruptible)
    return f"はい、{names}には技間の隙間があるため、発生Fごとに割り込み判定が異なります。"


def analyze_pressure_family(
    character: str,
    family_move: str,
    *,
    opener: str | None = None,
    initial_interaction: str = "block",
    variant_scope: str = "normal",
    client: Any | None = None,
) -> dict[str, Any]:
    """Analyze an underspecified special-move family as a set of variants.

    ``opener`` is explicit when supplied.  Otherwise a reviewed default may be
    used, and the response always exposes that assumption instead of silently
    treating it as a fact from the question.
    """
    if variant_scope not in {"normal", "all"}:
        return {
            "found": False,
            "status": "invalid_variant_scope",
            "message": "variant_scope は normal または all を指定してください。",
        }

    default = None
    assumption_source = "explicit"
    if not opener:
        default = _default_context(character, family_move)
        if not default:
            return {
                "found": False,
                "status": "opener_unspecified",
                "family_move": family_move,
                "message": (
                    f"{family_move} のどの技からの連携か指定してください。"
                    "例: `2MK→中迅雷脚は割り込める？`"
                ),
            }
        opener = str(default["opener"])
        initial_interaction = str(default.get("initial_interaction") or initial_interaction)
        assumption_source = str(default.get("evidence") or "reviewed_default")

    lookup = lookup_frame_data(character, family_move, client=client)
    candidates = _family_candidates(lookup, variant_scope=variant_scope)
    if not candidates:
        resolution = lookup.get("resolution") or {}
        return {
            "found": False,
            "status": "family_not_resolved",
            "family_move": family_move,
            "resolution": resolution,
            "message": (
                resolution.get("clarification")
                or f"{character} の技ファミリー {family_move} を解決できませんでした。"
            ),
        }

    variants: list[dict[str, Any]] = []
    for candidate in candidates:
        analysis = analyze_sequence(
            character,
            [opener, candidate["input"]],
            initial_interaction=initial_interaction,
            query_targets=["interrupt", "timeline"],
            client=client,
        )
        variants.append({
            "input": candidate["input"],
            "names": candidate["names"],
            "analysis": analysis,
        })

    assumption = (
        str(default.get("label")) if default else
        f"{opener}から最速で入力した場合"
    )
    interaction_label = {"block": "ガード時", "hit": "ヒット時"}.get(
        initial_interaction, initial_interaction
    )
    lines = [
        _family_conclusion(variants),
        f"前提: {assumption}（{interaction_label}）を比較しています。",
    ]
    lines.extend(
        _variant_summary_line(
            {"input": variant["input"], "names": variant["names"]},
            variant["analysis"],
        )
        for variant in variants
    )
    lines.append(
        "※距離・pushback・姿勢・無敵により、実際に割り込み技が届くかは別途確認が必要です。"
    )
    return {
        "found": True,
        "status": "resolved",
        "character": character,
        "family_move": family_move,
        "opener": opener,
        "initial_interaction": initial_interaction,
        "variant_scope": variant_scope,
        "assumption": {
            "source": assumption_source,
            "text": assumption,
        },
        "variants": variants,
        "summary": "\n".join(lines),
    }
