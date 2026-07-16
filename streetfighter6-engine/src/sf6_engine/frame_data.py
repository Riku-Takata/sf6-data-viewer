"""Deterministic multi-source frame-data lookup.

CAPCOM, Ultimate Frame Data (UFD), and SuperCombo describe the same move with
different schemas.  This module resolves a move once, keeps every source
observation, and selects each frame field with an explicit source policy:

* CAPCOM official values are primary.
* UFD measured values fill gaps and expose hitbox assets.
* SuperCombo fills remaining gaps and contributes supplemental metadata.

The returned profile always stores block advantage from the attacker's
perspective and derives the defender's perspective mechanically.  LLM code is
not involved in either source selection or sign inversion.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Iterable

from sf6_engine.db import get_client
from sf6_engine.frame_scenario import evaluate_frame_scenario, normalize_frame_scenario


SOURCE_LABELS = {
    "capcom": "CAPCOM公式",
    "ufd": "UFD",
    "supercombo": "SuperCombo",
}

_FRAME_CACHE_TTL_SECONDS = max(
    1, int(os.getenv("SF6_FRAME_CACHE_TTL_SECONDS", "300"))
)

_FIELD_LABELS = {
    "startup": "発生",
    "active": "持続",
    "recovery": "硬直",
    "on_block": "ガード時",
    "on_hit": "ヒット時",
    "total": "全体",
    "damage": "ダメージ",
}

_QUERY_OPERATORS = {"gt", "gte", "lt", "lte", "eq"}
_QUERY_SCOPES = {"all", "normal", "ground_normal", "special", "super"}
_QUERY_VARIANT_RE = re.compile(
    r"ホールド|ため|タメ|エアカレント|air current|windclad|"
    r"ウィンドクラッド|飲酒|drink(?:\s*level)?|(?:^|\W)lv\.?\s*\d|"
    r"charged|hold",
    re.IGNORECASE,
)

_NORMAL_PREFIXES = {
    "立ち弱p": "5LP", "立ち中p": "5MP", "立ち強p": "5HP",
    "立ち弱k": "5LK", "立ち中k": "5MK", "立ち強k": "5HK",
    "しゃがみ弱p": "2LP", "しゃがみ中p": "2MP", "しゃがみ強p": "2HP",
    "しゃがみ弱k": "2LK", "しゃがみ中k": "2MK", "しゃがみ強k": "2HK",
    "ジャンプ弱p": "j.LP", "ジャンプ中p": "j.MP", "ジャンプ強p": "j.HP",
    "ジャンプ弱k": "j.LK", "ジャンプ中k": "j.MK", "ジャンプ強k": "j.HK",
    "垂直ジャンプ弱p": "nj.LP", "垂直ジャンプ中p": "nj.MP",
    "垂直ジャンプ強p": "nj.HP", "垂直ジャンプ弱k": "nj.LK",
    "垂直ジャンプ中k": "nj.MK", "垂直ジャンプ強k": "nj.HK",
}

_CONDITIONAL_RE = re.compile(
    r"※|\.{2,}|条件|距離|持続|ホールド|ため|タメ|最大|最小|着地|空振り|"
    r"whiff|hold|variable|minimum|maximum",
    re.IGNORECASE,
)
_KNOCKDOWN_RE = re.compile(r"(?:^|\W)(?:KD|HKD|D)(?:\W|$)|knockdown|juggle", re.IGNORECASE)
_RANGE_RE = re.compile(
    r"([+-]?\d+)\s*(?:~|～|〜|\.{2,}|…+|-)\s*([+-]?\d+)"
)
_NUMBER_RE = re.compile(r"[+-]?\d+")


def _compact(value: str | None) -> str:
    """Normalize names for containment matching without losing JP characters."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龥]+", "", text)


def _name_forms(value: str | None) -> tuple[str, ...]:
    """Return full and parenthetical-free forms for official move names."""
    if not value:
        return ()
    full = _compact(value)
    base = _compact(re.sub(r"[（(【\[].*$", "", value).strip())
    return tuple(dict.fromkeys(v for v in (full, base) if len(v) >= 2))


def _name_score(name: str | None, query: str) -> int:
    if name and name.strip().casefold() == query.strip().casefold():
        return 6000 + len(name)
    query_compact = _compact(query)
    full = _compact(name)
    if not query_compact or not full:
        return 0
    base = _compact(re.sub(r"[（(【\[].*$", "", name or "").strip())
    if full == query_compact:
        return 5000 + len(full)
    if base and base == query_compact:
        return 4000 + len(base)

    best = 0
    for form in _name_forms(name):
        if form in query_compact:
            best = max(best, 3000 + len(form))
        elif len(query_compact) >= 3 and query_compact in form:
            best = max(best, 2000 + len(query_compact))
    return best


def _is_exact_named_row(row: dict[str, Any] | None, query: str) -> bool:
    if not row:
        return False
    name = row.get("move_name") or row.get("name")
    return bool(name and name.strip().casefold() == query.strip().casefold())


def _best_named_row(rows: Iterable[dict[str, Any]], query: str) -> dict[str, Any] | None:
    scored = [(_name_score(r.get("move_name") or r.get("name"), query), r) for r in rows]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            item[0],
            -len(item[1].get("move_name") or item[1].get("name") or ""),
        ),
        reverse=True,
    )
    return scored[0][1]


def _name_variant_prefix(value: str | None) -> str | None:
    """Return an explicit strength/super prefix used to guard fuzzy matching."""
    compact = _compact(value)
    match = re.match(r"^(od|sa[123]|ca|弱|中|強)", compact, re.IGNORECASE)
    return match.group(1).casefold() if match else None


def _best_unique_fuzzy_named_row(
    rows: Iterable[dict[str, Any]],
    query: str,
) -> dict[str, Any] | None:
    """Resolve a short typo only when one same-variant name clearly wins.

    This is deliberately a last-resort name resolver. It replaces per-move
    typo dictionaries while refusing ties and strength/SA-prefix changes.
    """
    query_compact = _compact(query)
    if len(query_compact) < 4:
        return None
    query_prefix = _name_variant_prefix(query)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        name = row.get("move_name") or row.get("name")
        if query_prefix and _name_variant_prefix(name) != query_prefix:
            continue
        forms = _name_forms(name)
        if not forms:
            continue
        score = max(
            SequenceMatcher(None, query_compact, form).ratio()
            for form in forms
        )
        ranked.append((score, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < 0.74:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.12:
        return None
    return ranked[0][1]


def _resolution_candidate_groups(
    query: str,
    cap_rows: Iterable[dict[str, Any]],
    sc_rows: Iterable[dict[str, Any]],
    ufd_rows: Iterable[dict[str, Any]],
    maps: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group name matches by canonical input for ambiguity reporting."""
    map_by_cap_name = {
        row.get("capcom_move_name"): row.get("sc_input")
        for row in maps if row.get("capcom_move_name")
    }
    grouped: dict[str, dict[str, Any]] = {}
    sources = (
        ("capcom", cap_rows, "move_name", lambda row: (
            map_by_cap_name.get(row.get("move_name"))
            or _capcom_normal_input(row.get("move_name"))
        )),
        ("supercombo", sc_rows, "name", lambda row: row.get("input")),
        ("ufd", ufd_rows, "move_name", lambda row: row.get("sc_input")),
    )
    for source, rows, name_key, input_getter in sources:
        for row in rows:
            name = row.get(name_key)
            score = _name_score(name, query)
            if score <= 0:
                continue
            move_input = input_getter(row)
            identity = (
                f"input:{_normalized_input_key(move_input)}" if move_input
                else f"name:{_compact(name)}"
            )
            candidate = grouped.setdefault(identity, {
                "input": move_input,
                "names": [],
                "sources": [],
                "score": score,
                "exact": False,
            })
            if name and name not in candidate["names"]:
                candidate["names"].append(name)
            if source not in candidate["sources"]:
                candidate["sources"].append(source)
            candidate["score"] = max(candidate["score"], score)
            candidate["exact"] = candidate["exact"] or bool(
                name and name.strip().casefold() == query.strip().casefold()
            )
    return sorted(
        grouped.values(),
        key=lambda item: (item["exact"], item["score"]),
        reverse=True,
    )


_EXPLICIT_STRENGTH_RE = re.compile(
    r"(?:^|[\s（(])(?:弱|中|強|OD|EX|LP|MP|HP|LK|MK|HK)",
    re.IGNORECASE,
)
_SC_STRENGTH_PREFIX_RE = re.compile(
    r"^(?:LP|MP|HP|LK|MK|HK|OD|EX)\s+", re.IGNORECASE
)


def _move_resolution(
    *,
    query: str,
    explicit_input: str | None,
    selected_input: str | None,
    selected_names: Iterable[str | None],
    candidates: list[dict[str, Any]],
    sc_row: dict[str, Any] | None,
    sc_rows: Iterable[dict[str, Any]],
    resolution_warnings: Iterable[str],
) -> dict[str, Any]:
    """Describe whether the query identifies one move strongly enough to calculate."""
    selected_names_clean = [name for name in selected_names if name]
    selected = {
        "input": selected_input,
        "names": list(dict.fromkeys(selected_names_clean)),
    }
    if explicit_input:
        return {
            "status": "resolved",
            "confidence": 1.0,
            "method": "explicit_input",
            "usable_for_calculation": True,
            "selected": selected,
            "candidates": candidates[:12],
        }

    exact_candidates = [candidate for candidate in candidates if candidate.get("exact")]
    if len(exact_candidates) == 1:
        return {
            "status": "resolved",
            "confidence": 0.99,
            "method": "exact_name",
            "usable_for_calculation": True,
            "selected": selected,
            "candidates": candidates[:12],
        }
    if len(exact_candidates) > 1:
        return {
            "status": "ambiguous",
            "confidence": 0.0,
            "method": "exact_family_name",
            "usable_for_calculation": False,
            "selected": selected,
            "candidates": exact_candidates[:12],
            "clarification": "同名の強度・派生が複数あります。強度またはコマンドを指定してください。",
        }

    # Alias lookup can resolve a family to its first row.  If that SC family has
    # multiple inputs and the question did not include a strength, keep it
    # ambiguous instead of treating the first row as an answer.
    if not candidates and sc_row:
        family = _SC_STRENGTH_PREFIX_RE.sub("", sc_row.get("name") or "")
        siblings = [
            row for row in sc_rows
            if family and _SC_STRENGTH_PREFIX_RE.sub("", row.get("name") or "") == family
            and row.get("input")
        ]
        sibling_inputs = list(dict.fromkeys(row.get("input") for row in siblings))
        if len(sibling_inputs) > 1 and not _EXPLICIT_STRENGTH_RE.search(query):
            return {
                "status": "ambiguous",
                "confidence": 0.0,
                "method": "alias_family",
                "usable_for_calculation": False,
                "selected": selected,
                "candidates": [
                    {
                        "input": row.get("input"),
                        "names": [row.get("name")],
                        "sources": ["supercombo"],
                    }
                    for row in siblings[:12]
                ],
                "clarification": "略称が複数強度に対応します。弱・中・強・ODを指定してください。",
            }

    if len(candidates) > 1:
        return {
            "status": "ambiguous",
            "confidence": 0.0,
            "method": "partial_name",
            "usable_for_calculation": False,
            "selected": selected,
            "candidates": candidates[:12],
            "clarification": "部分一致する技が複数あります。技名またはコマンドを詳しく指定してください。",
        }
    if any("同点" in warning or "不一致" in warning for warning in resolution_warnings):
        return {
            "status": "ambiguous",
            "confidence": 0.0,
            "method": "signature_tie",
            "usable_for_calculation": False,
            "selected": selected,
            "candidates": candidates[:12],
            "clarification": "ソース間の対応を一意に証明できません。正式名またはコマンドを指定してください。",
        }
    return {
        "status": "resolved",
        "confidence": 0.85 if candidates else 0.7,
        "method": "unique_partial_name" if candidates else "mapped_or_alias_name",
        "usable_for_calculation": True,
        "selected": selected,
        "candidates": candidates[:12],
    }


def _clean_raw(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return None if text in {"", "-", "--", "ー", "―", "—", "–"} else text


def _signed(value: int) -> str:
    return f"{value:+d}"


def _parse_frame_value(raw: object, *, advantage: bool = False) -> dict[str, Any] | None:
    """Parse a scalar/range frame expression while preserving its raw form."""
    raw_text = " ".join(str(raw).split()).strip() if raw is not None else ""
    if advantage and raw_text.casefold() in {
        "-", "--", "n/a", "na", "not applicable",
    }:
        return {
            "raw": raw_text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": [],
            "display": "対象外（ガード不成立）",
            "is_range": False,
            "conditional": False,
            "knockdown": False,
            "semantic": "not_applicable",
            "usable": True,
        }
    if advantage and raw_text.casefold() in {"varies", "variable"}:
        return {
            "raw": raw_text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": [],
            "display": "状況依存（固定値なし）",
            "is_range": False,
            "conditional": True,
            "knockdown": False,
            "semantic": "variable",
            "usable": True,
        }
    text = _clean_raw(raw)
    if text is None:
        return None

    conditional = bool(_CONDITIONAL_RE.search(text))
    knockdown = bool(_KNOCKDOWN_RE.search(text))
    range_match = _RANGE_RE.search(text)
    if range_match and not knockdown:
        first, last = int(range_match.group(1)), int(range_match.group(2))
        lo, hi = sorted((first, last))
        display = f"{_signed(lo) if advantage else lo}～{_signed(hi) if advantage else hi}F"
        return {
            "raw": text,
            "value": None,
            "min": lo,
            "max": hi,
            "alternatives": [],
            "display": display,
            "is_range": True,
            "conditional": conditional,
            "knockdown": False,
            "semantic": "range",
            "usable": True,
        }

    numbers = [int(value) for value in _NUMBER_RE.findall(text)]
    # KD/juggle prose can contain a numeric knockdown duration.  It is not a
    # regular hit-advantage scalar and must not be sign-inverted as one.
    if knockdown:
        values = [int(value) for value in _NUMBER_RE.findall(text)]
        return {
            "raw": text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": values,
            "display": text,
            "is_range": False,
            "conditional": conditional,
            "knockdown": True,
            "semantic": "knockdown",
            "usable": True,
        }
    if not numbers:
        return {
            "raw": text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": [],
            "display": text,
            "is_range": False,
            "conditional": conditional,
            "knockdown": False,
            "semantic": "text",
            "usable": False,
        }

    if len(numbers) > 1:
        # Expressions such as startup ``6+0`` and conditional block data such
        # as ``-60※-93`` must not be flattened to the first integer.
        compact = re.sub(r"\s+", "", text)
        if not advantage and re.fullmatch(r"[+-]?\d+\+[+-]?\d+", compact):
            display = f"{compact}F"
            semantic = "composite"
        else:
            rendered = [f"{_signed(value) if advantage else value}F" for value in numbers]
            display = " / ".join(rendered) + "（条件別）"
            semantic = "conditional_values"
        return {
            "raw": text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": numbers,
            "display": display,
            "is_range": False,
            "conditional": True,
            "knockdown": False,
            "semantic": semantic,
            "usable": True,
        }

    value = numbers[0]
    display = f"{_signed(value) if advantage else value}F"
    return {
        "raw": text,
        "value": value,
        "min": value,
        "max": value,
        "alternatives": [],
        "display": display,
        "is_range": False,
        "conditional": conditional,
        "knockdown": False,
        "semantic": "scalar",
        "usable": True,
    }


def _parse_capcom_active(raw: object) -> dict[str, Any] | None:
    """Convert CAPCOM absolute active-frame windows to active durations."""
    text = _clean_raw(raw)
    if text is None:
        return None
    ranges = [
        (int(a), int(b))
        for a, b in re.findall(r"(\d+)\s*(?:-|~|～|〜)\s*(\d+)", text)
    ]
    if ranges:
        # CAPCOM's cell may contain a summary span followed by the real active
        # phases, e.g. ``13-38 13-15, 30-38``.  The first span envelopes the
        # latter phases and must not be counted as an additional active hitbox.
        phases = ranges
        if len(ranges) > 1:
            summary, details = ranges[0], ranges[1:]
            if (summary[0] == min(start for start, _ in details)
                    and summary[1] == max(end for _, end in details)
                    and all(summary[0] <= start <= end <= summary[1]
                            for start, end in details)):
                phases = details

        durations = [abs(end - start) + 1 for start, end in phases]
        gaps = [
            max(0, phases[index][0] - phases[index - 1][1] - 1)
            for index in range(1, len(phases))
        ]
        if len(phases) == 1:
            display = f"{durations[0]}F"
        else:
            parts = [f"{durations[0]}F"]
            for gap, duration in zip(gaps, durations[1:]):
                if gap:
                    parts.append(f"空白{gap}F")
                parts.append(f"{duration}F")
            display = "→".join(parts) + "（複数持続区間）"
        return {
            "raw": text,
            "value": durations[0] if len(phases) == 1 else None,
            "min": durations[0] if len(phases) == 1 else None,
            "max": durations[0] if len(phases) == 1 else None,
            "alternatives": [],
            "display": display,
            "is_range": False,
            "conditional": bool(_CONDITIONAL_RE.search(text)) or len(phases) > 1,
            "knockdown": False,
            "semantic": "scalar" if len(phases) == 1 else "active_sequence",
            "usable": True,
            "active_segments": durations,
            "inactive_gaps": gaps,
        }
    numbers = _NUMBER_RE.findall(text)
    if len(numbers) == 1:
        # CAPCOM uses the absolute active frame number.  A scalar therefore
        # means one active frame, not N active frames.
        return {
            "raw": text,
            "value": 1,
            "min": 1,
            "max": 1,
            "alternatives": [],
            "display": "1F",
            "is_range": False,
            "conditional": bool(_CONDITIONAL_RE.search(text)),
            "knockdown": False,
            "semantic": "scalar",
            "usable": True,
            "active_segments": [1],
            "inactive_gaps": [],
        }
    return {
        "raw": text,
        "value": None,
        "min": None,
        "max": None,
        "alternatives": [],
        "display": text,
        "is_range": False,
        "conditional": True,
        "knockdown": False,
        "semantic": "text",
        "usable": bool(numbers),
        "active_segments": [],
        "inactive_gaps": [],
    }


def _parse_duration(raw: object) -> dict[str, Any] | None:
    """Parse UFD/SC duration notation without flattening multi-phase values."""
    text = _clean_raw(raw)
    if text is None:
        return None
    if re.fullmatch(r"\d+", text):
        value = int(text)
        return {
            "raw": text,
            "value": value,
            "min": value,
            "max": value,
            "alternatives": [],
            "display": f"{value}F",
            "is_range": False,
            "conditional": False,
            "knockdown": False,
            "semantic": "scalar",
            "usable": True,
            "active_segments": [value],
            "inactive_gaps": [],
        }

    compact = re.sub(r"\s+", "", text)
    sequence_parts = re.split(r"\((\d+)\)", compact)
    if (len(sequence_parts) >= 3 and len(sequence_parts) % 2 == 1
            and all(part.isdigit() for part in sequence_parts)):
        segments = [int(sequence_parts[index]) for index in range(0, len(sequence_parts), 2)]
        gaps = [int(sequence_parts[index]) for index in range(1, len(sequence_parts), 2)]
        display_parts = [f"{segments[0]}F"]
        for gap, segment in zip(gaps, segments[1:]):
            display_parts.extend((f"空白{gap}F", f"{segment}F"))
        return {
            "raw": text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": [],
            "display": "→".join(display_parts) + "（複数持続区間）",
            "is_range": False,
            "conditional": True,
            "knockdown": False,
            "semantic": "active_sequence",
            "usable": True,
            "active_segments": segments,
            "inactive_gaps": gaps,
        }

    if re.fullmatch(r"\d+(?:,\d+)+", compact):
        segments = [int(value) for value in compact.split(",")]
        return {
            "raw": text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": [],
            "display": "・".join(f"{value}F" for value in segments)
            + "（複数持続区間、間隔不明）",
            "is_range": False,
            "conditional": True,
            "knockdown": False,
            "semantic": "active_sequence",
            "usable": True,
            "active_segments": segments,
            "inactive_gaps": [],
        }

    parsed = _parse_frame_value(text)
    if parsed:
        parsed.setdefault("active_segments", [])
        parsed.setdefault("inactive_gaps", [])
    return parsed


def _parse_recovery(raw: object) -> dict[str, Any] | None:
    """Parse recovery without confusing total duration with recovery."""
    text = _clean_raw(raw)
    if text is None:
        return None

    total_match = re.fullmatch(r"全体\s*(※)?\s*(\d+)", text)
    if total_match:
        value = int(total_match.group(2))
        return {
            "raw": text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": [value],
            "display": f"硬直単独値なし（CAPCOM公式は全体{value}Fを掲載）",
            "is_range": False,
            "conditional": bool(total_match.group(1)),
            "knockdown": False,
            "semantic": "total_only",
            "usable": False,
        }

    landing_match = re.fullmatch(
        r"(?:(\d+)\s*\+\s*)?(?:着地後?|Landing)\s*(\d+)|"
        r"(\d+)\s*(?:Landing)",
        text,
        re.IGNORECASE,
    )
    if landing_match:
        before = landing_match.group(1)
        landing = landing_match.group(2) or landing_match.group(3)
        landing_value = int(landing)
        if before is None:
            return {
                "raw": text,
                "value": landing_value,
                "min": landing_value,
                "max": landing_value,
                "alternatives": [],
                "display": f"着地後{landing_value}F",
                "is_range": False,
                "conditional": True,
                "knockdown": False,
                "semantic": "landing_recovery",
                "usable": True,
            }
        before_value = int(before)
        return {
            "raw": text,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": [before_value, landing_value],
            "display": f"{before_value}F＋着地後{landing_value}F",
            "is_range": False,
            "conditional": True,
            "knockdown": False,
            "semantic": "composite_recovery",
            "usable": True,
        }

    parsed = _parse_frame_value(text)
    if parsed:
        parsed["semantic"] = (
            "recovery_" + parsed["semantic"]
            if parsed.get("semantic") not in {"scalar", "range"}
            else parsed["semantic"]
        )
    return parsed


def _parse_total(raw: object) -> dict[str, Any] | None:
    """Parse a total-duration value, including CAPCOM ``全体 N`` notation."""
    text = _clean_raw(raw)
    if text is None:
        return None
    stripped = re.sub(r"^全体\s*", "", text)
    return _parse_frame_value(stripped)


def _split_top_level_commas(text: str) -> list[str]:
    """Split UFD stage lists without splitting commas inside parentheses."""
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def _parse_stage_sequence(
    raw: object,
    *,
    field: str,
) -> dict[str, Any] | None:
    """Parse a target-combo row as per-stage facts, not conditions."""
    text = _clean_raw(raw)
    if text is None:
        return None
    parts = _split_top_level_commas(text)
    if len(parts) < 2:
        return None

    items: list[dict[str, Any] | None] = []
    rendered: list[str] = []
    for index, part in enumerate(parts, start=1):
        if field == "active":
            parsed = _parse_duration(part)
        elif field == "recovery":
            parsed = _parse_recovery(part)
        elif field == "total":
            parsed = _parse_total(part)
        else:
            parsed = _parse_frame_value(
                part, advantage=field in {"on_block", "on_hit"}
            )
        items.append(parsed)
        display = (parsed or {}).get("display") or "データなし"
        rendered.append(f"{index}段目: {display}")

    return {
        "raw": text,
        "value": None,
        "min": None,
        "max": None,
        "alternatives": [],
        "display": " / ".join(rendered),
        "is_range": False,
        "conditional": False,
        "knockdown": False,
        "semantic": "stage_sequence",
        "usable": any(item and item.get("usable") for item in items),
        "sequence_items": items,
    }


def _invert_advantage(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """Invert attacker advantage to the defender perspective."""
    if not parsed or parsed.get("knockdown"):
        return None
    if parsed.get("semantic") == "not_applicable":
        return {**parsed, "raw": None}
    if parsed.get("semantic") == "variable":
        return {**parsed, "raw": None}
    if parsed.get("semantic") == "stage_sequence":
        inverted_items: list[dict[str, Any] | None] = []
        rendered: list[str] = []
        for index, item in enumerate(parsed.get("sequence_items") or [], start=1):
            inverted = _invert_advantage(item) if item else None
            inverted_items.append(inverted)
            rendered.append(
                f"{index}段目: {(inverted or {}).get('display') or '算出不可'}"
            )
        return {
            **parsed,
            "raw": None,
            "value": None,
            "min": None,
            "max": None,
            "display": " / ".join(rendered),
            "sequence_items": inverted_items,
        }
    if parsed.get("value") is not None:
        value = -int(parsed["value"])
        return {
            **parsed,
            "raw": None,
            "value": value,
            "min": value,
            "max": value,
            "display": f"{value:+d}F",
        }
    if parsed.get("min") is not None and parsed.get("max") is not None:
        lo, hi = -int(parsed["max"]), -int(parsed["min"])
        return {
            **parsed,
            "raw": None,
            "min": lo,
            "max": hi,
            "display": f"{lo:+d}～{hi:+d}F",
        }
    alternatives = parsed.get("alternatives") or []
    if alternatives:
        values = [-int(value) for value in alternatives]
        return {
            **parsed,
            "raw": None,
            "value": None,
            "min": None,
            "max": None,
            "alternatives": values,
            "display": " / ".join(f"{value:+d}F" for value in values) + "（条件別）",
            "semantic": "conditional_values",
        }
    return None


def _capcom_normal_input(move_name: str | None) -> str | None:
    compact = _compact(move_name)
    for prefix, input_name in sorted(_NORMAL_PREFIXES.items(), key=lambda item: -len(item[0])):
        if compact.startswith(prefix):
            return input_name
    return None


def _normalized_input_key(value: str | None) -> str:
    """Normalize equivalent notation while keeping jump directions distinct."""
    normalized = unicodedata.normalize("NFKC", value or "").strip().casefold()
    match = re.fullmatch(r"nj\.([lmh][pk])", normalized)
    return f"8{match.group(1)}" if match else normalized


def _inputs_match(left: str | None, right: str | None) -> bool:
    return bool(
        left
        and right
        and _normalized_input_key(left) == _normalized_input_key(right)
    )


def _extract_explicit_input(query: str, known_inputs: Iterable[str]) -> str | None:
    stripped = query.strip()
    for value in sorted({v for v in known_inputs if v}, key=len, reverse=True):
        if _inputs_match(stripped, value):
            return value
        if value == "-":
            continue
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", query, re.IGNORECASE):
            return value
    return None


def _simple_int(raw: object, *, advantage: bool = False) -> int | None:
    parsed = _parse_frame_value(raw, advantage=advantage)
    return parsed.get("value") if parsed else None


def _parsed_signature_match(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool | None:
    """Return True/False for comparable parsed facts, or None if unavailable."""
    if not left or not right or not left.get("usable") or not right.get("usable"):
        return None
    if left.get("knockdown") or right.get("knockdown"):
        return bool(left.get("knockdown")) == bool(right.get("knockdown"))

    left_segments = left.get("active_segments") or []
    right_segments = right.get("active_segments") or []
    if left_segments or right_segments:
        if not left_segments or not right_segments:
            return None
        if left_segments != right_segments:
            return False
        left_gaps = left.get("inactive_gaps") or []
        right_gaps = right.get("inactive_gaps") or []
        return left_gaps == right_gaps if left_gaps and right_gaps else True

    if left.get("is_range") or right.get("is_range"):
        if not left.get("is_range") or not right.get("is_range"):
            return False
        return (
            left.get("min"), left.get("max")
        ) == (
            right.get("min"), right.get("max")
        )

    left_value = left.get("value")
    right_value = right.get("value")
    if left_value is not None and right_value is not None:
        return left_value == right_value

    left_values = left.get("alternatives") or []
    right_values = right.get("alternatives") or []
    if left_values and right_values:
        return tuple(left_values) == tuple(right_values)
    if left_value is not None and right_values:
        return left_value in right_values
    if right_value is not None and left_values:
        return right_value in left_values
    return None


def _signature_metrics(
    capcom: dict[str, Any],
    other: dict[str, Any],
) -> tuple[int, int, int]:
    """Return (matches, conflicts, comparable) for one cross-source pair."""
    parsers = (
        (
            _parse_frame_value(capcom.get("startup")),
            _parse_frame_value(other.get("startup")),
        ),
        (
            _parse_capcom_active(capcom.get("active")),
            _parse_duration(other.get("active")),
        ),
        (
            _parse_recovery(capcom.get("recovery")),
            _parse_recovery(other.get("recovery")),
        ),
        (
            _parse_frame_value(capcom.get("on_block"), advantage=True),
            _parse_frame_value(
                other.get("on_block")
                if other.get("on_block") is not None
                else other.get("block_adv"),
                advantage=True,
            ),
        ),
        (
            _parse_frame_value(capcom.get("on_hit"), advantage=True),
            _parse_frame_value(
                other.get("on_hit")
                if other.get("on_hit") is not None
                else other.get("hit_adv"),
                advantage=True,
            ),
        ),
    )
    comparisons = [_parsed_signature_match(left, right) for left, right in parsers]
    matches = sum(result is True for result in comparisons)
    conflicts = sum(result is False for result in comparisons)
    return matches, conflicts, matches + conflicts


def _source_kind(row: dict[str, Any]) -> str | None:
    move_type = (row.get("move_type") or "").casefold()
    if move_type:
        if move_type == "throw":
            return "throw"
        if move_type == "special":
            return "special"
        if move_type == "super":
            return "super"
        if move_type in {"ground_normal", "air_normal", "air_normal8"}:
            return "normal"
        if move_type in {"drive", "taunt"}:
            return "system"
    category = (row.get("category") or "").casefold()
    if "throw" in category:
        return "throw"
    if "special" in category:
        return "special"
    if "super" in category:
        return "super"
    if "target" in category or "unique" in category:
        return "unique"
    if "normal" in category or "jump" in category:
        return "normal"
    if "system" in category or "movement" in category:
        return "system"
    return None


def _section_compatible(capcom: dict[str, Any], other: dict[str, Any]) -> bool:
    """Prevent frame similarity from linking unrelated move categories."""
    section = capcom.get("section")
    kind = _source_kind(other)
    if not section or not kind:
        return True
    allowed = {
        "通常投げ": {"throw"},
        "必殺技": {"special"},
        "スーパーアーツ": {"super"},
        "通常技": {"normal"},
        "特殊技": {"normal", "unique"},
        "共通システム": {"system"},
    }
    return kind in allowed.get(section, {kind})


def _signature_score(capcom: dict[str, Any], other: dict[str, Any]) -> int:
    return _signature_metrics(capcom, other)[0]


def _signature_is_reliable(metrics: tuple[int, int, int]) -> bool:
    matches, conflicts, _ = metrics
    return matches >= 3 and matches > conflicts


def _unique_signature_match(
    capcom: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Choose only a uniquely best candidate backed by at least three facts."""
    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    for candidate in candidates:
        if not _section_compatible(capcom, candidate):
            continue
        matches, conflicts, comparable = _signature_metrics(capcom, candidate)
        if _signature_is_reliable((matches, conflicts, comparable)):
            ranked.append((matches, -conflicts, comparable, candidate))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    if not ranked:
        return None, []
    best_rank = ranked[0][:3]
    tied = [item[3] for item in ranked if item[:3] == best_rank]
    names = [
        row.get("move_name") or row.get("name") or row.get("input") or "?"
        for row in tied
    ]
    if len(tied) != 1:
        return None, names
    alternatives = [
        item[3].get("move_name") or item[3].get("name") or item[3].get("input") or "?"
        for item in ranked[1:]
    ]
    return tied[0], alternatives


_CAPCOM_VARIANT_PREFIX_RE = re.compile(
    r"(?:^|\s)(?:弱|中|強|OD|SA[123]|CA)\s*",
    re.IGNORECASE,
)
_CAPCOM_CONDITION_RE = re.compile(r"(?:【[^】]*】|\[[^\]]*\]|（[^）]*）)")
_BUTTON_HOLD_RE = re.compile(r"\[[LMH]?[PK]\]", re.IGNORECASE)
_OD_INPUT_RE = re.compile(r"(?:PP|KK|LPMP|LPHP|MPHP|LKMK|LKHK|MKHK)", re.IGNORECASE)


def _capcom_family_key(move_name: str | None) -> str:
    name = _CAPCOM_CONDITION_RE.sub("", move_name or "")
    name = _CAPCOM_VARIANT_PREFIX_RE.sub(" ", name)
    return _compact(name)


def _sc_family_key(move_name: str | None) -> str:
    name = re.sub(r"\([^)]*(?:hold|charged)[^)]*\)", "", move_name or "", flags=re.I)
    name = re.sub(r"^(?:OD|Overdrive)\s+", "", name, flags=re.I)
    return _compact(name)


def _capcom_strength(move_name: str | None) -> str | None:
    match = re.search(r"(?:^|\s)(弱|中|強)\s*", move_name or "")
    return match.group(1) if match else None


def _input_strength_matches(input_name: str | None, strength: str | None) -> bool:
    if not strength:
        return True
    text = (input_name or "").upper()
    keys = {"弱": ("LP", "LK"), "中": ("MP", "MK"), "強": ("HP", "HK")}[strength]
    if any(key in text for key in keys):
        return True
    # Some SC rows use a generic P/K input because strength does not alter data.
    return bool(re.search(r"(?:^|[^A-Z])[PK]$", text))


def _is_button_hold(row: dict[str, Any]) -> bool:
    return bool(
        _BUTTON_HOLD_RE.search(row.get("input") or "")
        or re.search(r"\bhold\b", row.get("name") or "", re.I)
    )


def _resolve_sc_variant_from_family(
    capcom_row: dict[str, Any],
    maps: list[dict[str, Any]],
    sc_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Fill a missing sibling mapping only when family and variant are unique."""
    target_family = _capcom_family_key(capcom_row.get("move_name"))
    if not target_family:
        return None
    siblings = [
        row for row in maps
        if _capcom_family_key(row.get("capcom_move_name")) == target_family
    ]
    known_sc_families = {
        _sc_family_key(row.get("sc_name")) for row in siblings if row.get("sc_name")
    }

    candidates = [row for row in sc_rows if _section_compatible(capcom_row, row)]
    if known_sc_families:
        candidates = [
            row for row in candidates
            if _sc_family_key(row.get("name")) in known_sc_families
        ]

    move_name = capcom_row.get("move_name") or ""
    wants_od = bool(re.search(r"(?:^|\s)OD\s*", move_name, re.I))
    wants_hold = "ホールド" in move_name or "ため" in move_name
    strength = _capcom_strength(move_name)
    candidates = [
        row for row in candidates
        if bool(_OD_INPUT_RE.search(row.get("input") or "")) == wants_od
        and _is_button_hold(row) == wants_hold
        and _input_strength_matches(row.get("input"), strength)
    ]
    if not candidates:
        return None
    if known_sc_families and len(candidates) == 1:
        return candidates[0]

    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    for row in candidates:
        matches, conflicts, comparable = _signature_metrics(capcom_row, row)
        minimum = 1 if known_sc_families else 2
        if matches >= minimum and conflicts == 0:
            ranked.append((matches, -conflicts, comparable, row))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    if not ranked:
        return None
    best = ranked[0][:3]
    tied = [item[3] for item in ranked if item[:3] == best]
    return tied[0] if len(tied) == 1 else None


def _unique_capcom_signature_match(
    candidates: Iterable[dict[str, Any]],
    other: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Reverse form of ``_unique_signature_match`` for input-first queries."""
    ranked: list[tuple[int, int, int, dict[str, Any]]] = []
    for candidate in candidates:
        if not _section_compatible(candidate, other):
            continue
        matches, conflicts, comparable = _signature_metrics(candidate, other)
        if _signature_is_reliable((matches, conflicts, comparable)):
            ranked.append((matches, -conflicts, comparable, candidate))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    if not ranked:
        return None, []
    best_rank = ranked[0][:3]
    tied = [item[3] for item in ranked if item[:3] == best_rank]
    names = [row.get("move_name") or "?" for row in tied]
    if len(tied) != 1:
        return None, names
    return tied[0], [item[3].get("move_name") or "?" for item in ranked[1:]]


def _choose_capcom_candidate(
    rows: list[dict[str, Any]],
    query: str,
    sc_row: dict[str, Any] | None,
    ufd_row: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not rows:
        return None, []
    if len(rows) == 1:
        return rows[0], []
    query_compact = _compact(query)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        name = row.get("move_name") or ""
        compact = _compact(name)
        score = _name_score(name, query)
        if "ca" in query_compact or "クリティカル" in query_compact:
            score += 500 if compact.startswith("ca") else 0
        elif compact.startswith("ca"):
            score -= 100
        wants_hold = any(word in query_compact for word in ("ホールド", "ため", "タメ", "溜め"))
        has_hold = any(word in compact for word in ("ホールド", "ため", "タメ", "溜め"))
        if wants_hold == has_hold:
            score += 100
        elif has_hold:
            score -= 40
        if sc_row:
            score += _signature_score(row, sc_row) * 120
        if ufd_row:
            score += _signature_score(row, ufd_row) * 120
        # Prefer the unqualified/base official row when the question itself is
        # only an input and all signatures tie.
        condition_penalty = sum(token in name for token in ("[", "【", "ホールド", "ジャスト", "CA "))
        scored.append((score, -condition_penalty, row))
    scored.sort(key=lambda item: (item[0], item[1], -len(item[2].get("move_name") or "")), reverse=True)
    selected = scored[0][2]
    alternatives = [r.get("move_name") for _, _, r in scored[1:] if r.get("move_name")]
    return selected, alternatives


def _choose_ufd_candidate(
    rows: list[dict[str, Any]],
    query: str,
    sc_row: dict[str, Any] | None,
    capcom_row: dict[str, Any] | None,
    direct: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if direct and direct in rows:
        return direct, []
    if not rows:
        return None, []
    if len(rows) == 1:
        return rows[0], []
    reference = (sc_row or {}).get("name") or ""
    query_compact = _compact(query)
    capcom_name = _compact((capcom_row or {}).get("move_name"))
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        name = row.get("move_name") or ""
        compact = _compact(name)
        score = _name_score(name, query)
        if reference:
            score += int(SequenceMatcher(None, compact, _compact(reference)).ratio() * 500)
        wants_air = any(token in query_compact + capcom_name for token in ("空中", "aerial", "jump"))
        is_air = any(token in compact for token in ("aerial", "jump"))
        if wants_air == is_air:
            score += 160
        wants_dash = "クイックダッシュ" in query_compact or "quickdash" in query_compact
        is_dash = "quickdash" in compact
        if wants_dash == is_dash:
            score += 160
        score += _signature_score(capcom_row, row) * 100 if capcom_row else 0
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, [row.get("move_name") for _, row in scored if row.get("move_name")]
    return scored[0][1], [row.get("move_name") for _, row in scored[1:] if row.get("move_name")]


def _observation(
    source: str,
    field: str,
    raw: object,
    row: dict[str, Any],
) -> dict[str, Any] | None:
    parsed = None
    if source == "ufd" and "~" in (row.get("sc_input") or ""):
        parsed = _parse_stage_sequence(raw, field=field)
    if parsed is None and field == "active":
        parsed = _parse_capcom_active(raw) if source == "capcom" else _parse_duration(raw)
    elif parsed is None and field == "recovery":
        parsed = _parse_recovery(raw)
    elif parsed is None and field == "total":
        parsed = _parse_total(raw)
    elif parsed is None:
        parsed = _parse_frame_value(raw, advantage=field in {"on_block", "on_hit"})
    if parsed is None:
        return None
    timestamp_key = {"capcom": "patch_date", "ufd": "scraped_at", "supercombo": "imported_at"}[source]
    return {
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "as_of": row.get(timestamp_key),
        "move_name": row.get("move_name") or row.get("name"),
        **parsed,
    }


def _build_fact(
    field: str,
    capcom_row: dict[str, Any] | None,
    ufd_row: dict[str, Any] | None,
    sc_row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    observations: list[dict[str, Any]] = []
    raw_keys = {
        "startup": "startup",
        "active": "active",
        "recovery": "recovery",
        "on_block": "on_block",
        "on_hit": "on_hit",
        "total": "total",
        "damage": "damage",
    }
    key = raw_keys[field]
    for source, row in (("capcom", capcom_row), ("ufd", ufd_row), ("supercombo", sc_row)):
        if not row:
            continue
        raw = row.get(key)
        if source == "supercombo" and field == "on_block":
            raw = row.get("block_adv")
        elif source == "supercombo" and field == "on_hit":
            raw = row.get("hit_adv")
        if field == "total" and source == "capcom" and not _clean_raw(raw):
            recovery = _clean_raw(row.get("recovery"))
            if recovery and recovery.startswith("全体"):
                raw = recovery
        observation = _observation(source, field, raw, row)
        if observation:
            observations.append(observation)
    if not observations:
        return None
    # CAPCOM remains primary when it publishes a value with the same semantic
    # meaning.  ``全体52`` in its recovery column is a total-duration value,
    # however, so UFD/SC recovery may safely fill that semantic gap.
    usable = [observation for observation in observations if observation.get("usable")]
    selected = usable[0] if usable else observations[0]
    selected_display = selected.get("display")
    if (selected.get("conditional") and selected_display
            and not any(token in selected_display for token in (
                "条件", "複数", "空白", "着地", "全体", "硬直単独値なし",
                "状況依存",
            ))):
        selected_display += "（条件付き）"

    def signature(observation: dict[str, Any]) -> tuple[Any, ...]:
        semantic = observation.get("semantic")
        if semantic == "stage_sequence":
            return (
                semantic,
                tuple(
                    (item or {}).get("display") or "missing"
                    for item in observation.get("sequence_items") or []
                ),
            )
        if semantic == "active_sequence":
            return (
                semantic,
                tuple(observation.get("active_segments") or []),
                tuple(observation.get("inactive_gaps") or []),
            )
        if observation.get("is_range"):
            return ("range", observation.get("min"), observation.get("max"))
        if observation.get("value") is not None:
            return ("scalar", observation.get("value"))
        if observation.get("alternatives"):
            return (semantic, tuple(observation["alternatives"]))
        if observation.get("knockdown"):
            return ("knockdown",)
        return (semantic, observation.get("display"))

    signatures = {signature(observation) for observation in observations}
    return {
        "field": field,
        "label": _FIELD_LABELS[field],
        "source": selected["source"],
        "source_label": selected["source_label"],
        "as_of": selected.get("as_of"),
        "raw": selected.get("raw"),
        "value": selected.get("value"),
        "min": selected.get("min"),
        "max": selected.get("max"),
        "display": selected_display,
        "is_range": selected.get("is_range", False),
        "conditional": selected.get("conditional", False),
        "knockdown": selected.get("knockdown", False),
        "semantic": selected.get("semantic"),
        "usable": selected.get("usable", False),
        "alternatives": selected.get("alternatives") or [],
        "active_segments": selected.get("active_segments") or [],
        "inactive_gaps": selected.get("inactive_gaps") or [],
        "sequence_items": selected.get("sequence_items") or [],
        "conflict": len(signatures) > 1,
        "observations": observations,
    }


def _resolve_character(client, character: str) -> tuple[str, str] | None:
    slug = character.lower()
    exact = (
        client.table("char_slug_map").select("capcom_slug,sc_chara")
        .eq("capcom_slug", slug).limit(1).execute().data or []
    )
    if exact:
        return exact[0]["capcom_slug"], exact[0]["sc_chara"]
    by_sc = (
        client.table("char_slug_map").select("capcom_slug,sc_chara")
        .ilike("sc_chara", character).limit(1).execute().data or []
    )
    return (by_sc[0]["capcom_slug"], by_sc[0]["sc_chara"]) if by_sc else None


def _all_character_rows(client, slug: str, sc_chara: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "capcom": client.table("move_latest").select(
            "character_slug,section,move_name,startup,active,recovery,on_hit,on_block,"
            "cancel,damage,note,patch_date"
        ).eq("character_slug", slug).limit(500).execute().data or [],
        "maps": client.table("special_move_map").select(
            "capcom_move_name,sc_input,sc_name,match_method"
        ).eq("capcom_slug", slug).limit(500).execute().data or [],
        "sc": client.table("sc_moves").select(
            "input,name,move_type,guard,startup,active,recovery,total,hit_adv,block_adv,"
            "punish_adv,perf_parry_adv,damage,atk_range,invuln,notes,imported_at"
        ).eq("chara", sc_chara).limit(500).execute().data or [],
        "ufd": client.table("ufd_moves").select(
            "category,move_name,sc_input,input_sequence,startup,total,damage,attack_type,"
            "cancellable,notes,hitbox_note,on_hit,on_block,active,recovery,"
            "hitbox_source_url,hitbox_storage_path,source_url,scraped_at"
        ).eq("character_slug", slug).limit(500).execute().data or [],
    }


def _cache_bucket() -> int:
    return int(time.monotonic() // _FRAME_CACHE_TTL_SECONDS)


@lru_cache(maxsize=256)
def _resolve_character_cached(
    character: str,
    bucket: int,
) -> tuple[str, str] | None:
    del bucket
    return _resolve_character(get_client(), character)


@lru_cache(maxsize=128)
def _all_character_rows_cached(
    slug: str,
    sc_chara: str,
    bucket: int,
) -> dict[str, list[dict[str, Any]]]:
    del bucket
    return _all_character_rows(get_client(), slug, sc_chara)


def clear_frame_data_cache() -> None:
    """Clear process-local source caches after an in-process data refresh."""
    _resolve_character_cached.cache_clear()
    _all_character_rows_cached.cache_clear()


def lookup_frame_data(
    character: str,
    move_query: str,
    *,
    scenario: dict[str, Any] | None = None,
    client=None,
) -> dict[str, Any]:
    """Resolve one move and return a deterministic multi-source frame profile."""
    normalized_scenario = normalize_frame_scenario(scenario)
    if client is None:
        bucket = _cache_bucket()
        resolved_character = _resolve_character_cached(character.casefold(), bucket)
    else:
        resolved_character = _resolve_character(client, character)
    if not resolved_character:
        return {
            "found": False,
            "character": character,
            "query_move_name": move_query,
            "candidate_names": [],
            "resolution": {
                "status": "not_found",
                "usable_for_calculation": False,
                "reason": "character_not_found",
            },
            "message": f"キャラクター '{character}' が見つかりません。",
        }
    slug, sc_chara = resolved_character
    rows = (
        _all_character_rows_cached(slug, sc_chara, bucket)
        if client is None
        else _all_character_rows(client, slug, sc_chara)
    )
    cap_rows, maps, sc_rows, ufd_rows = rows["capcom"], rows["maps"], rows["sc"], rows["ufd"]

    cap_direct = _best_named_row(cap_rows, move_query)
    sc_direct = _best_named_row(sc_rows, move_query)
    ufd_direct = _best_named_row(ufd_rows, move_query)
    fuzzy_source: str | None = None
    if not cap_direct and not sc_direct and not ufd_direct:
        cap_direct = _best_unique_fuzzy_named_row(cap_rows, move_query)
        if cap_direct:
            fuzzy_source = "CAPCOM公式"
        else:
            ufd_direct = _best_unique_fuzzy_named_row(ufd_rows, move_query)
            if ufd_direct:
                fuzzy_source = "UFD"
    cap_direct_exact = _is_exact_named_row(cap_direct, move_query)
    sc_direct_exact = _is_exact_named_row(sc_direct, move_query)
    ufd_direct_exact = _is_exact_named_row(ufd_direct, move_query)
    ufd_unmapped_anchor = bool(
        ufd_direct_exact and ufd_direct and not ufd_direct.get("sc_input")
    )
    resolution_warnings: list[str] = []
    if fuzzy_source:
        matched_name = (
            (cap_direct or {}).get("move_name")
            or (ufd_direct or {}).get("move_name")
        )
        resolution_warnings.append(
            f"入力名を{fuzzy_source}の『{matched_name}』へ一意の近似一致で補正しました。"
        )
    resolution_candidates = _resolution_candidate_groups(
        move_query, cap_rows, sc_rows, ufd_rows, maps
    )
    known_inputs = [r.get("input") for r in sc_rows] + [r.get("sc_input") for r in ufd_rows]
    explicit_input = _extract_explicit_input(move_query, (v for v in known_inputs if v))

    sc_input: str | None = explicit_input
    if not sc_input and cap_direct:
        mapped = next((m for m in maps if m.get("capcom_move_name") == cap_direct.get("move_name")), None)
        sc_input = mapped.get("sc_input") if mapped else _capcom_normal_input(cap_direct.get("move_name"))
    if not sc_input and ufd_direct:
        sc_input = ufd_direct.get("sc_input")
    if not sc_input and sc_direct and (
        not ufd_unmapped_anchor or sc_direct_exact
    ):
        sc_input = sc_direct.get("input")

    # Reuse the established alias/JP-name resolver only when direct matching
    # did not produce an anchor.  It returns an SC row but does not select facts.
    if not sc_input and not cap_direct and not ufd_direct and not sc_direct:
        try:
            from sf6_engine.rag_builder import _fetch_move_by_name
            fallback = _fetch_move_by_name(slug, move_query, raw_query=move_query)
            if fallback:
                sc_input = fallback.get("input")
        except Exception:
            fallback = None

    sc_row = next((
        r for r in sc_rows
        if _inputs_match(r.get("input"), sc_input)
    ), None)
    if sc_direct and (not sc_input or _inputs_match(sc_direct.get("input"), sc_input)) and (
        not ufd_unmapped_anchor or sc_direct_exact
    ):
        sc_row = sc_direct

    ufd_input_rows = [
        r for r in ufd_rows
        if sc_input and _inputs_match(r.get("sc_input"), sc_input)
    ]

    # For official moves not covered by the persistent mapping, resolve SC/UFD
    # independently.  Combining both source lists would make the same move look
    # like a tie merely because two sources agree on it.
    if cap_direct and not sc_input:
        family_variant = _resolve_sc_variant_from_family(cap_direct, maps, sc_rows)
        if family_variant:
            sc_input = family_variant.get("input")
            sc_row = family_variant
            ufd_input_rows = [
                r for r in ufd_rows if _inputs_match(r.get("sc_input"), sc_input)
            ]
        else:
            sc_signature, sc_ties = _unique_signature_match(cap_direct, sc_rows)
            ufd_signature, ufd_ties = _unique_signature_match(
                cap_direct, (row for row in ufd_rows if row.get("sc_input"))
            )
            proposals = {
                value for value in (
                    (sc_signature or {}).get("input"),
                    (ufd_signature or {}).get("sc_input"),
                ) if value
            }
            if len(proposals) == 1:
                sc_input = proposals.pop()
                sc_row = next(
                    (r for r in sc_rows if _inputs_match(r.get("input"), sc_input)),
                    None,
                )
                ufd_input_rows = [
                    r for r in ufd_rows if _inputs_match(r.get("sc_input"), sc_input)
                ]
            elif len(proposals) > 1:
                resolution_warnings.append(
                    "フレームシグネチャから推定した入力がソース間で不一致です: "
                    + " / ".join(sorted(proposals))
                )
            elif sc_ties or ufd_ties:
                resolution_warnings.append(
                    "フレームシグネチャが同点のため入力を推定していません: "
                    + " / ".join(list(dict.fromkeys([*sc_ties, *ufd_ties]))[:8])
                )

    cap_candidates: list[dict[str, Any]] = []
    if cap_direct and (cap_direct_exact or not ufd_unmapped_anchor):
        cap_candidates = [cap_direct]
    elif sc_input:
        names = {
            m.get("capcom_move_name") for m in maps
            if _inputs_match(m.get("sc_input"), sc_input)
        }
        cap_candidates.extend(r for r in cap_rows if r.get("move_name") in names)
        cap_candidates.extend(
            r for r in cap_rows
            if _inputs_match(_capcom_normal_input(r.get("move_name")), sc_input)
            and r not in cap_candidates
        )
        if ufd_unmapped_anchor and ufd_direct:
            cap_candidates = [
                candidate for candidate in cap_candidates
                if _signature_is_reliable(_signature_metrics(candidate, ufd_direct))
            ]
        if not cap_candidates and sc_row and not ufd_unmapped_anchor:
            inferred_cap, inferred_alternatives = _unique_capcom_signature_match(
                cap_rows, sc_row
            )
            if inferred_cap:
                cap_candidates.append(inferred_cap)
            elif inferred_alternatives:
                resolution_warnings.append(
                    "入力からCAPCOM公式技を一意に推定できません: "
                    + " / ".join(inferred_alternatives[:8])
                )

    provisional_ufd = ufd_direct if ufd_direct in ufd_input_rows else (ufd_input_rows[0] if len(ufd_input_rows) == 1 else None)
    cap_row, cap_alternatives = _choose_capcom_candidate(
        cap_candidates, move_query, sc_row, provisional_ufd
    )
    ufd_candidates = list(ufd_input_rows)
    if ufd_direct and ufd_direct_exact and ufd_direct not in ufd_candidates:
        ufd_candidates.insert(0, ufd_direct)
    if not ufd_candidates and ufd_direct:
        ufd_candidates.append(ufd_direct)
    ufd_row, ufd_alternatives = _choose_ufd_candidate(
        ufd_candidates,
        move_query,
        sc_row,
        cap_row or cap_direct,
        ufd_direct,
    )
    cap_row = cap_row or cap_direct

    if not any((cap_row, sc_row, ufd_row)):
        query_compact = _compact(move_query)
        candidates = []
        for row in [*cap_rows, *ufd_rows, *sc_rows]:
            name = row.get("move_name") or row.get("name")
            if name and query_compact and query_compact in _compact(name):
                candidates.append(name)
        return {
            "found": False,
            "character": slug,
            "query_move_name": move_query,
            "candidate_names": list(dict.fromkeys(candidates))[:10],
            "resolution": {
                "status": "not_found",
                "usable_for_calculation": False,
                "reason": "move_not_found",
                "candidates": resolution_candidates[:12],
            },
            "message": f"{slug} の技 '{move_query}' は見つかりませんでした。",
        }

    facts = {
        field: _build_fact(field, cap_row, ufd_row, sc_row)
        for field in ("startup", "active", "recovery", "on_block", "on_hit", "total", "damage")
    }
    facts = {field: fact for field, fact in facts.items() if fact is not None}
    block_fact = facts.get("on_block")
    defender_block = _invert_advantage(block_fact) if block_fact else None

    warnings: list[str] = list(resolution_warnings)
    if cap_alternatives:
        warnings.append(
            "同じ入力に複数のCAPCOM公式バリアントがあります: "
            + " / ".join(cap_alternatives[:5])
        )
    if ufd_alternatives:
        warnings.append(
            "同じ入力に複数のUFDバリアントがあります: "
            + " / ".join(ufd_alternatives[:5])
        )

    move_name_ja = (cap_row or {}).get("move_name")
    move_name_en = (sc_row or {}).get("name") or (ufd_row or {}).get("move_name")
    display_name = move_name_ja or move_name_en or sc_input or move_query
    section = (cap_row or {}).get("section") or (ufd_row or {}).get("category") or (sc_row or {}).get("move_type")
    move_type = (sc_row or {}).get("move_type") or section
    resolution = _move_resolution(
        query=move_query,
        explicit_input=explicit_input,
        selected_input=sc_input,
        selected_names=(
            move_name_ja,
            move_name_en,
            (ufd_row or {}).get("move_name"),
        ),
        candidates=resolution_candidates,
        sc_row=sc_row,
        sc_rows=sc_rows,
        resolution_warnings=resolution_warnings,
    )
    scenario_evaluation = evaluate_frame_scenario(
        facts=facts,
        scenario=normalized_scenario,
        resolution=resolution,
        section=section,
        move_type=move_type,
    )

    profile = {
        "character_slug": slug,
        "sc_chara": sc_chara,
        "query": move_query,
        "input": sc_input,
        "move_name": display_name,
        "move_name_ja": move_name_ja,
        "move_name_en": move_name_en,
        "section": section,
        "resolution": resolution,
        "scenario": normalized_scenario,
        "scenario_evaluation": scenario_evaluation,
        "facts": facts,
        "block_perspectives": {
            "attacker": block_fact,
            "defender": defender_block,
        },
        "notes": {
            "capcom": (cap_row or {}).get("note"),
            "ufd": (ufd_row or {}).get("notes"),
            "supercombo": (sc_row or {}).get("notes"),
        },
        "warnings": warnings,
        "source_rows": {
            "capcom_move_name": move_name_ja,
            "ufd_move_name": (ufd_row or {}).get("move_name"),
            "supercombo_move_name": (sc_row or {}).get("name"),
        },
    }

    def value(field: str) -> Any:
        fact = facts.get(field)
        return fact.get("value") if fact else None

    block_value = value("on_block")
    hit_fact = facts.get("on_hit") or {}
    atk_range = None
    range_match = re.search(r"\d+(?:\.\d+)?", str((sc_row or {}).get("atk_range") or ""))
    if range_match:
        atk_range = float(range_match.group(0))

    move = {
        "source": "resolved_frame_profile",
        "input": sc_input,
        "move_name": display_name,
        "move_name_ja": move_name_ja,
        "move_name_en": move_name_en,
        "move_type": move_type,
        "section": section,
        "guard": (sc_row or {}).get("guard"),
        "startup": value("startup"),
        "startup_display": (facts.get("startup") or {}).get("display"),
        "active": value("active"),
        "active_display": (facts.get("active") or {}).get("display"),
        "recovery": value("recovery"),
        "recovery_display": (facts.get("recovery") or {}).get("display"),
        "on_block": block_value,
        "on_block_attacker_display": (block_fact or {}).get("display"),
        "on_block_defender": (defender_block or {}).get("value"),
        "on_block_defender_display": (defender_block or {}).get("display"),
        "on_block_is_range": bool((block_fact or {}).get("is_range")),
        "on_hit": value("on_hit"),
        "on_hit_display": hit_fact.get("display"),
        "on_hit_is_knockdown": bool(hit_fact.get("knockdown")),
        "damage": value("damage"),
        "punish_adv": _simple_int((sc_row or {}).get("punish_adv"), advantage=True),
        "perf_parry_adv": _simple_int((sc_row or {}).get("perf_parry_adv"), advantage=True),
        "atk_range": atk_range,
        "invuln": (sc_row or {}).get("invuln"),
        "notes": (sc_row or {}).get("notes") or (ufd_row or {}).get("notes") or (cap_row or {}).get("note"),
        "raw": {field: fact.get("raw") for field, fact in facts.items()},
        "frame_profile": profile,
    }
    contextual_block = (
        scenario_evaluation.get("contextual_facts", {}).get("on_block") or {}
    )
    contextual_hit = (
        scenario_evaluation.get("contextual_facts", {}).get("on_hit") or {}
    )
    contextual_defender = (
        scenario_evaluation.get("block_perspectives", {}).get("defender") or {}
    )
    move.update({
        "contextual_on_block": contextual_block.get("value"),
        "contextual_on_block_display": contextual_block.get("display"),
        "contextual_on_block_status": contextual_block.get("status"),
        "contextual_on_block_defender": contextual_defender.get("value"),
        "contextual_on_block_defender_display": contextual_defender.get("display"),
        "contextual_on_hit": contextual_hit.get("value"),
        "contextual_on_hit_display": contextual_hit.get("display"),
        "contextual_on_hit_status": contextual_hit.get("status"),
        "scenario_evaluation": scenario_evaluation,
        "resolution": resolution,
    })
    if block_fact:
        atk = block_fact.get("display") or "データなし"
        dfn = (defender_block or {}).get("display") or "算出不可"
        move["frame_perspective_note"] = (
            f"ガード時は攻撃側 {atk} / 防御側 {dfn}。"
            "防御側は攻撃側の公式硬直差を決定論的に符号反転した値。"
        )
    if ufd_row:
        move["ufd"] = ufd_row

    return {
        "found": True,
        "character": slug,
        "query_move_name": move_query,
        "move": move,
        "resolution": resolution,
        "requires_clarification": resolution.get("status") != "resolved"
        or scenario_evaluation.get("overall_status") == "needs_clarification",
        "candidate_names": list(dict.fromkeys([
            *cap_alternatives,
            *(
                name
                for candidate in resolution.get("candidates") or []
                for name in candidate.get("names") or []
            ),
        ]))[:12],
        "message": "CAPCOM公式を主値、UFD・SuperComboを補完値として統合しました。",
    }


def _query_candidate_identifiers(rows: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Return exact source identifiers for a character-wide frame query.

    A frame query must enumerate CAPCOM-only, SuperCombo-only, and UFD-only
    rows.  The identifiers are deliberately not reduced to SC input: one input
    can represent multiple named condition variants.
    """
    identifiers: list[str] = []
    for row in rows.get("capcom", []):
        if row.get("move_name"):
            identifiers.append(str(row["move_name"]))
    for row in rows.get("ufd", []):
        identifier = row.get("move_name") or row.get("sc_input")
        if identifier:
            identifiers.append(str(identifier))
    for row in rows.get("sc", []):
        identifier = row.get("input") or row.get("name")
        if identifier:
            identifiers.append(str(identifier))
    return list(dict.fromkeys(identifiers))


def _query_profile_identity(profile: dict[str, Any]) -> tuple[Any, ...]:
    """Return a variant-preserving identity for deduplicating query results."""
    source_rows = profile.get("source_rows") or {}
    return (
        source_rows.get("capcom_move_name"),
        source_rows.get("ufd_move_name"),
        source_rows.get("supercombo_move_name"),
        profile.get("input"),
        profile.get("section"),
    )


def _query_scope_matches(profile: dict[str, Any], scope: str) -> bool:
    """Return whether a resolved profile belongs to the requested move scope."""
    if scope == "all":
        return True
    section = str(profile.get("section") or "").casefold()
    move_type = str(profile.get("move_type") or "").casefold()
    if scope == "normal":
        return (
            profile.get("section") in {"通常技", "特殊技"}
            or move_type in {"ground_normal", "air_normal", "air_normal8", "command_normal"}
        )
    if scope == "ground_normal":
        # CAPCOM の「通常技」区分にはジャンプ攻撃も含まれる。SC の move_type や
        # 入力・名称で空中技と分かる場合は、地上通常技検索から明示的に除外する。
        input_name = str(profile.get("input") or "").casefold()
        move_name = str(profile.get("move_name") or "")
        if (
            move_type in {"air_normal", "air_normal8"}
            or section in {"air_normal", "aerial"}
            or input_name.startswith(("j.", "nj."))
            or "ジャンプ" in move_name
        ):
            return False
        return profile.get("section") in {"通常技", "特殊技"} or move_type in {
            "ground_normal", "command_normal"
        }
    if scope == "special":
        return profile.get("section") == "必殺技" or move_type == "special" or section == "special"
    if scope == "super":
        return profile.get("section") == "スーパーアーツ" or move_type == "super" or section == "super"
    return False


def _query_compare(left: int, operator: str, right: int) -> bool:
    """Evaluate a validated numeric query predicate."""
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    return left == right


def _query_numeric_values(fact: dict[str, Any]) -> list[int]:
    """Return all numeric values represented by a typed frame fact."""
    value = fact.get("value")
    if isinstance(value, int):
        return [value]
    minimum, maximum = fact.get("min"), fact.get("max")
    if isinstance(minimum, int) and isinstance(maximum, int):
        return [minimum, maximum]
    return [item for item in fact.get("alternatives") or [] if isinstance(item, int)]


def _query_condition_labels(
    profile: dict[str, Any],
    fact: dict[str, Any],
    contextual: dict[str, Any] | None = None,
) -> list[str]:
    """Describe why a matching value is not a plain base-value result."""
    labels: list[str] = []
    if fact.get("is_range"):
        labels.append("範囲値")
    elif fact.get("alternatives"):
        labels.append("条件別値")
    elif fact.get("conditional"):
        labels.append("条件付き値")
    variants = " ".join(
        str(value or "")
        for value in (
            profile.get("input"),
            profile.get("move_name"),
            profile.get("move_name_ja"),
            profile.get("move_name_en"),
        )
    )
    if _QUERY_VARIANT_RE.search(variants):
        labels.append("技バリアント")
    if (contextual or {}).get("status") in {"derived_exact", "condition_selected"}:
        labels.append("指定条件")
    return labels


def _query_move_item(
    profile: dict[str, Any],
    fact: dict[str, Any],
    *,
    value: int | None,
    condition_labels: list[str],
    reason: str | None = None,
) -> dict[str, Any]:
    """Create one stable, JSON-serializable move-query item."""
    return {
        "input": profile.get("input"),
        "move_name": profile.get("move_name"),
        "section": profile.get("section"),
        "value": value,
        "display": fact.get("display") or "データなし",
        "source": fact.get("source"),
        "source_label": fact.get("source_label"),
        "condition_labels": condition_labels,
        "reason": reason,
        "_sort_value": value if isinstance(value, int) else (
            min(_query_numeric_values(fact)) if _query_numeric_values(fact) else None
        ),
    }


def _query_sort_items(items: list[dict[str, Any]], operator: str) -> None:
    """Sort query results deterministically without exposing internal ordering."""
    reverse = operator in {"gt", "gte"}
    # Stable sorts keep the secondary identifier/name ordering ascending even
    # when the primary frame value is descending.
    items.sort(
        key=lambda item: (str(item.get("input") or ""), str(item.get("move_name") or "")),
    )
    items.sort(
        key=lambda item: item.get("_sort_value") if isinstance(item.get("_sort_value"), int) else 0,
        reverse=reverse,
    )
    for item in items:
        item.pop("_sort_value", None)


def _query_operator_text(operator: str, value: int) -> str:
    """Format the predicate for a deterministic Japanese summary."""
    operators = {
        "gt": "より大きい",
        "gte": "以上",
        "lt": "より小さい",
        "lte": "以下",
        "eq": "と等しい",
    }
    return f"{value:+d}F {operators[operator]}"


def _format_move_query_summary(result: dict[str, Any]) -> str:
    """Render a completed answer for a typed character-wide move query."""
    character = result.get("character") or "指定キャラ"
    perspective = result.get("perspective")
    perspective_label = "攻撃側（ガードさせた側）" if perspective == "attacker" else "防御側（ガードした側）"
    scope_labels = {
        "all": "全技",
        "normal": "通常技・特殊技",
        "ground_normal": "地上通常技・特殊技",
        "special": "必殺技",
        "super": "スーパーアーツ",
    }
    lines = [
        "【技条件検索】",
        f"{character} の{scope_labels[result['scope']]}を、ガード時の{perspective_label}が "
        f"{_query_operator_text(result['operator'], result['value'])} で検索しました。",
    ]
    # Discord の1メッセージ制限内で、条件付き/保留の注意書きまで必ず見せる。
    # 完全な構造化リストは matches / conditional_matches に保持する。
    visible_budget = 12

    def append_items(title: str, items: list[dict[str, Any]], maximum: int) -> None:
        nonlocal visible_budget
        lines.append(f"【{title}（{len(items)}件）】")
        display_count = min(len(items), maximum, visible_budget)
        for item in items[:display_count]:
            identifier = item.get("input") or "入力不明"
            name = item.get("move_name") or "名称不明"
            source = item.get("source_label") or "出所不明"
            labels = " / ".join(item.get("condition_labels") or [])
            suffix = f"（{labels}）" if labels else ""
            lines.append(f"- {identifier} / {name}: {item['display']} [{source}]{suffix}")
        visible_budget -= display_count
        if len(items) > display_count:
            lines.append(f"- …ほか {len(items) - display_count} 件")

    matches = result.get("matches") or []
    conditional = result.get("conditional_matches") or []
    unresolved = result.get("unresolved") or []
    if matches:
        append_items("基準値で条件一致", matches, 20)
    if conditional:
        append_items("条件付きで条件一致", conditional, 20)
    if not matches and not conditional:
        lines.append("該当する技は、現在の収録データにはありません。")
    if unresolved:
        lines.append(f"※ {len(unresolved)}件は条件別・範囲・未収録のため、条件一致を確定できません。")
    if result.get("not_applicable_count"):
        lines.append(f"※ ガード不成立など対象外の技: {result['not_applicable_count']}件")
    return "\n".join(lines)


def query_frame_data(
    character: str,
    *,
    field: str = "on_block",
    operator: str = "gt",
    value: int = 0,
    perspective: str = "attacker",
    scope: str = "all",
    scenario: dict[str, Any] | None = None,
    limit: int = 100,
    client=None,
) -> dict[str, Any]:
    """Filter one character's moves through the typed frame-data contract.

    This is intentionally separate from ``list_moves``.  It uses the same
    CAPCOM → UFD → SuperCombo field selection and condition preservation as a
    single move lookup, then applies a numeric predicate only where the value
    is valid for the requested scenario and perspective.
    """
    if field != "on_block":
        return {
            "found": False,
            "character": character,
            "error": f"未対応の検索フィールドです: {field}",
        }
    if operator not in _QUERY_OPERATORS:
        return {
            "found": False,
            "character": character,
            "error": f"未対応の比較演算子です: {operator}",
        }
    if perspective not in {"attacker", "defender"}:
        return {
            "found": False,
            "character": character,
            "error": f"視点は attacker または defender を指定してください: {perspective}",
        }
    if scope not in _QUERY_SCOPES:
        return {
            "found": False,
            "character": character,
            "error": f"未対応の技区分です: {scope}",
        }
    if not isinstance(value, int):
        return {
            "found": False,
            "character": character,
            "error": "比較値は整数フレームで指定してください。",
        }
    limit = min(max(limit, 1), 100)

    if client is None:
        bucket = _cache_bucket()
        resolved_character = _resolve_character_cached(character.casefold(), bucket)
    else:
        resolved_character = _resolve_character(client, character)
    if not resolved_character:
        return {
            "found": False,
            "character": character,
            "message": f"キャラクター '{character}' が見つかりません。",
        }
    slug, sc_chara = resolved_character
    rows = (
        _all_character_rows_cached(slug, sc_chara, bucket)
        if client is None
        else _all_character_rows(client, slug, sc_chara)
    )

    matches: list[dict[str, Any]] = []
    conditional_matches: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    not_applicable_count = 0
    seen: set[tuple[Any, ...]] = set()

    for identifier in _query_candidate_identifiers(rows):
        lookup = lookup_frame_data(
            slug,
            identifier,
            scenario=scenario,
            client=client,
        )
        if not lookup.get("found") or not lookup.get("move"):
            continue
        profile = (lookup["move"] or {}).get("frame_profile") or {}
        if (profile.get("resolution") or {}).get("status") != "resolved":
            continue
        identity = _query_profile_identity(profile)
        if identity in seen or not _query_scope_matches(profile, scope):
            continue
        seen.add(identity)

        reference = (profile.get("block_perspectives") or {}).get(perspective) or {}
        contextual = (
            (profile.get("scenario_evaluation") or {})
            .get("block_perspectives", {})
            .get(perspective)
            or {}
        )
        if reference.get("semantic") == "not_applicable":
            not_applicable_count += 1
            continue

        contextual_status = contextual.get("status")
        contextual_value = contextual.get("value")
        if contextual_status in {"source_exact", "derived_exact", "condition_selected"} and isinstance(contextual_value, int):
            item = _query_move_item(
                profile,
                reference,
                value=contextual_value,
                condition_labels=_query_condition_labels(profile, reference, contextual),
            )
            if _query_compare(contextual_value, operator, value):
                if item["condition_labels"]:
                    conditional_matches.append(item)
                else:
                    matches.append(item)
            continue

        # An explicit scenario whose system rule is not modeled must remain
        # unresolved; falling back to the base value would be incorrect.
        if contextual.get("required_data") or contextual_status in {
            "invalid_condition", "move_ambiguous", "data_missing"
        }:
            unresolved.append(_query_move_item(
                profile,
                reference,
                value=None,
                condition_labels=_query_condition_labels(profile, reference, contextual),
                reason=contextual.get("display") or contextual_status,
            ))
            continue

        numeric_values = _query_numeric_values(reference)
        if not numeric_values:
            unresolved.append(_query_move_item(
                profile,
                reference,
                value=None,
                condition_labels=_query_condition_labels(profile, reference, contextual),
                reason=reference.get("display") or "数値化できないフレーム値",
            ))
            continue
        satisfied = [_query_compare(number, operator, value) for number in numeric_values]
        if all(satisfied):
            conditional_matches.append(_query_move_item(
                profile,
                reference,
                value=None,
                condition_labels=_query_condition_labels(profile, reference, contextual),
            ))
        elif any(satisfied):
            unresolved.append(_query_move_item(
                profile,
                reference,
                value=None,
                condition_labels=_query_condition_labels(profile, reference, contextual),
                reason="条件値の一部だけが検索条件に一致",
            ))

    _query_sort_items(matches, operator)
    _query_sort_items(conditional_matches, operator)
    _query_sort_items(unresolved, operator)
    result: dict[str, Any] = {
        "found": True,
        "character": slug,
        "field": field,
        "operator": operator,
        "value": value,
        "perspective": perspective,
        "scope": scope,
        "scenario": normalize_frame_scenario(scenario),
        "count": len(matches) + len(conditional_matches),
        "matches": matches[:limit],
        "conditional_matches": conditional_matches[:limit],
        "unresolved": unresolved[:limit],
        "unresolved_count": len(unresolved),
        "not_applicable_count": not_applicable_count,
    }
    result["summary"] = _format_move_query_summary(result)
    return result


def _fact_observation_text(fact: dict[str, Any]) -> str:
    return " / ".join(
        f"{obs['source_label']}={obs['raw']}"
        for obs in fact.get("observations", [])
    )


def format_frame_profile_context(profile: dict[str, Any]) -> str:
    """Format a profile for deterministic answer generation."""
    name = profile.get("move_name") or "不明"
    inp = profile.get("input") or "入力不明"
    char = profile.get("character_slug") or "?"
    lines = [f"【{char} / {inp} ({name}) — 統合フレームプロファイル】"]
    resolution = profile.get("resolution") or {}
    if resolution.get("status") != "resolved":
        lines.append(
            "【技解決:要確認】技を一意に特定できていないため、"
            "以下の暫定候補の数値を確定回答・計算に使用しないでください。"
        )
        if resolution.get("clarification"):
            lines.append(f"確認事項: {resolution['clarification']}")
        candidate_text = " / ".join(
            f"{candidate.get('input') or '入力不明'}:"
            f"{','.join(candidate.get('names') or [])}"
            for candidate in (resolution.get("candidates") or [])[:8]
        )
        if candidate_text:
            lines.append(f"技候補: {candidate_text}")
    else:
        lines.append(
            f"技解決: 一意 ({resolution.get('method') or 'unknown'}, "
            f"confidence={resolution.get('confidence')})"
        )
    facts = profile.get("facts") or {}
    for field in ("startup", "active", "recovery"):
        fact = facts.get(field)
        if fact:
            lines.append(
                f"{fact['label']}: {fact['display']} [採用: {fact['source_label']}]"
            )
        else:
            lines.append(f"{_FIELD_LABELS[field]}: データなし [採用: なし]")
    block = profile.get("block_perspectives") or {}
    attacker = block.get("attacker")
    defender = block.get("defender")
    if attacker:
        lines.append(
            f"ガード時（攻撃側・ガードさせた側）: {attacker['display']} "
            f"[採用: {attacker['source_label']}]"
        )
        lines.append(
            "ガード時（防御側・ガードした側）: "
            f"{(defender or {}).get('display') or '算出不可'} "
            f"[攻撃側の{attacker['source_label']}値を符号反転]"
        )
        if attacker.get("value") is not None and defender and defender.get("value") is not None:
            lines.append(
                f"ガード時: {attacker['value']:+d}F "
                f"(技を出した側が{attacker['value']:+d}F / "
                f"ガードした側は{defender['value']:+d}F)"
            )
    else:
        lines.append("ガード時（攻撃側・ガードさせた側）: データなし [採用: なし]")
        lines.append(
            "ガード時（防御側・ガードした側）: 算出不可 "
            "[攻撃側のなし値を符号反転]"
        )
    hit = facts.get("on_hit")
    if hit:
        lines.append(f"ヒット時（攻撃側）: {hit['display']} [採用: {hit['source_label']}]")
    total = facts.get("total")
    if total:
        lines.append(f"全体: {total['display']} [採用: {total['source_label']}]")

    scenario = profile.get("scenario") or {}
    evaluation = profile.get("scenario_evaluation") or {}
    if scenario:
        condition_parts = [
            f"{field}={scenario[field]}"
            for field in scenario.get("specified") or []
            if field in scenario
        ]
        lines.append("質問条件: " + (" / ".join(condition_parts) or "明示条件なし"))
        for ambiguity in scenario.get("ambiguities") or []:
            lines.append(f"条件の確認事項: {ambiguity.get('message')}")
        contextual_block = (
            evaluation.get("block_perspectives", {}).get("attacker") or {}
        )
        contextual_defender = (
            evaluation.get("block_perspectives", {}).get("defender") or {}
        )
        contextual_hit = (
            evaluation.get("contextual_facts", {}).get("on_hit") or {}
        )
        lines.append(
            "条件適用後ガード時（攻撃側）: "
            f"{contextual_block.get('display') or '算出不可'} "
            f"[{contextual_block.get('status') or 'unknown'}]"
        )
        lines.append(
            "条件適用後ガード時（防御側）: "
            f"{contextual_defender.get('display') or '算出不可'} "
            f"[{contextual_defender.get('status') or 'unknown'}]"
        )
        lines.append(
            "条件適用後ヒット時（攻撃側）: "
            f"{contextual_hit.get('display') or '算出不可'} "
            f"[{contextual_hit.get('status') or 'unknown'}]"
        )
        for contextual in (contextual_block, contextual_hit):
            if contextual.get("derivation"):
                lines.append(f"条件計算根拠: {contextual['derivation']}")
            if contextual.get("required_data"):
                lines.append(
                    "条件計算に不足: " + " / ".join(contextual["required_data"])
                )
        punish = evaluation.get("punish_assessment") or {}
        if punish.get("frame_punishable"):
            lines.append(
                f"確反評価: フレーム上は発生{punish.get('punish_window_f')}F以内。"
                "ただしガード後距離と反撃技の到達判定が未検証のため、"
                "確定反撃としては未確定。"
            )

    for field in ("startup", "active", "recovery", "on_block", "on_hit"):
        fact = facts.get(field)
        if fact and fact.get("conflict"):
            lines.append(
                f"【ソース差異:{fact['label']}】{_fact_observation_text(fact)}"
            )
    capcom_note = (profile.get("notes") or {}).get("capcom")
    if capcom_note:
        lines.append(f"CAPCOM公式注記: {capcom_note}")
    for warning in profile.get("warnings") or []:
        lines.append(f"データ解決上の注意: {warning}")
    return "\n".join(lines)
