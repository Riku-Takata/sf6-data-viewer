"""Discord bot 経路の網羅評価ハーネス。

DB から全キャラ・全技の期待値を生成し、Discord bot と同じ
intent_parser → mcp_router → generate_answer の経路で回答を検証する。

使い方:
  cd streetfighter6-engine
  PYTHONPATH=src ./.venv312/bin/python tests/bot_comprehensive_eval.py --dry-run
  PYTHONPATH=src ./.venv312/bin/python tests/bot_comprehensive_eval.py --chars sagat ken --max-per-bucket 2
  PYTHONPATH=src ./.venv312/bin/python tests/bot_comprehensive_eval.py --exhaustive

注意:
  --exhaustive は数千問規模になり、Ollama と MCP/API Gateway を長時間使う。
  まず --dry-run / --limit-cases / --max-per-bucket でケース数を確認すること。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "discord_bot" / ".env", override=False)


NORMAL_MOVE_TYPES = {"ground_normal", "air_normal", "air_normal8"}
SPECIAL_MOVE_TYPES = {"Special", "special"}
SUPER_MOVE_TYPES = {"Super", "super"}
SKIP_MOVE_TYPES = {"throw", "drive", "taunt"}

STANDARD_NORMAL_RE = re.compile(r"^(?:[25][LMH][PK]|j\.[LMH][PK])$")

CASE_TYPE_ORDER = (
    "move_data", "active", "recovery",
    "guard_attack", "guard_defense", "punish_suggestion",
)
BUCKET_ORDER = ("normal", "unique", "special", "super")


@dataclass(frozen=True)
class Character:
    slug: str
    ja_name: str
    sc_chara: str


@dataclass(frozen=True)
class Move:
    character: Character
    input: str
    name: str
    move_type: str
    bucket: str
    startup: int | None
    block_adv: int | None
    startup_display: str | None = None
    active_display: str | None = None
    recovery_display: str | None = None
    block_attacker_display: str | None = None
    block_defender_display: str | None = None
    block_usable_for_calculation: bool = False
    capcom_move_name: str | None = None

    @property
    def query_name(self) -> str:
        """ユーザー質問に入れる技識別子。

        必殺技/SA は special_move_map の公式日本語名を優先し、通常技/特殊技は
        input を使う。input なら intent_parser の誤訳を避けて bot 経路を検査できる。
        """
        if self.bucket in {"special", "super"} and self.capcom_move_name:
            return self.capcom_move_name
        if self.input == "-":
            return self.name
        return self.input


@dataclass(frozen=True)
class PunisherOption:
    move_name: str
    input: str | None
    startup: int


@dataclass
class BotCase:
    id: str
    case_type: str
    bucket: str
    character: str
    character_ja: str
    move_input: str
    move_name: str
    query_move_name: str
    question: str
    expected_frame: int | None = None
    expected_display: str | None = None
    expected_options: list[PunisherOption] = field(default_factory=list)
    expect_punish_unresolved: bool = False
    punisher: str | None = None
    punisher_ja: str | None = None
    direct_tool: str = "lookup_move"
    direct_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseResult:
    case: BotCase
    result: str = "?"
    reason: str = ""
    answer: str = ""
    intent: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any] | None] = field(default_factory=list)
    elapsed: float = 0.0


def _normalize_text(text: str) -> str:
    table = str.maketrans(
        "０１２３４５６７８９＋－．　",
        "0123456789+-. ",
    )
    return text.translate(table).lower()


def _num_boundary_pattern(value: int, *, allow_unsigned: bool) -> re.Pattern[str]:
    sign = "+" if value > 0 else ""
    signed = f"{sign}{value}" if value != 0 else "0"
    abs_value = str(abs(value))
    if allow_unsigned:
        core = rf"(?:{re.escape(signed)}|{re.escape(abs_value)})"
    else:
        core = re.escape(signed)
    return re.compile(rf"(?<![\d.]){core}\s*(?:f|フレーム)?(?![\d.])")


def _signed_polarity_conflict(answer: str, value: int) -> bool:
    if value == 0:
        return False
    signed = f"{value:+d}"
    wrong_word = "有利" if value < 0 else "不利"
    return bool(
        re.search(rf"{re.escape(signed.lower())}\s*(?:f|フレーム)?[^。\n]{{0,10}}{wrong_word}", answer)
        or re.search(rf"{wrong_word}[^。\n]{{0,10}}{re.escape(signed.lower())}\s*(?:f|フレーム)?", answer)
    )


def answer_contains_frame(answer: str, value: int, *, field: str) -> bool:
    """回答に期待フレームが含まれるかを判定する。

    ガード有利は「-5F」だけでなく「5F不利」も正答にする。
    発生は「12です」のような単位なしも許容する。
    """
    ans = _normalize_text(answer)
    if field == "advantage" and _signed_polarity_conflict(ans, value):
        return False
    if value == 0:
        return bool(re.search(r"(?<![\d.])0\s*(?:f|フレーム)?(?![\d.])|五分", ans))

    if field == "startup":
        return bool(_num_boundary_pattern(value, allow_unsigned=True).search(ans))

    if _num_boundary_pattern(value, allow_unsigned=False).search(ans):
        return True

    abs_value = abs(value)
    polarity = "有利" if value > 0 else "不利"
    return bool(
        re.search(rf"(?<![\d.]){abs_value}\s*(?:f|フレーム)?[^。\n]{{0,10}}{polarity}", ans)
        or re.search(rf"{polarity}[^。\n]{{0,10}}(?<![\d.]){abs_value}\s*(?:f|フレーム)?", ans)
    )


def answer_mentions_option(answer: str, option: PunisherOption) -> bool:
    ans = _normalize_text(answer).replace(" ", "")
    if option.input and option.input.lower().replace(" ", "") in ans:
        return True
    name = _normalize_text(option.move_name).replace(" ", "")
    return bool(name and name in ans)


def answer_contains_display(answer: str, expected: str) -> bool:
    """範囲・複数区間・着地硬直を含む採用表示が回答に残っているか。"""
    normalized_answer = re.sub(r"\s+", "", _normalize_text(answer))
    normalized_expected = re.sub(r"\s+", "", _normalize_text(expected))
    if normalized_expected in normalized_answer:
        return True
    if expected == "データなし":
        return "データがない" in answer or "データがありません" in answer
    if expected == "算出不可":
        return "算出不可" in answer or "算出できません" in answer
    return False


def classify_bucket(move_type: str | None, inp: str) -> str | None:
    mt = move_type or ""
    if mt in SKIP_MOVE_TYPES:
        return None
    if mt in SPECIAL_MOVE_TYPES:
        return "special"
    if mt in SUPER_MOVE_TYPES:
        return "super"
    if mt in NORMAL_MOVE_TYPES:
        return "normal" if STANDARD_NORMAL_RE.match(inp) else "unique"
    return "unique"


def load_characters(chars_filter: set[str] | None = None) -> list[Character]:
    from sf6_engine.db import get_client

    rows = (
        get_client()
        .table("char_slug_map")
        .select("capcom_slug,display_name_ja,sc_chara")
        .order("capcom_slug")
        .execute()
        .data
        or []
    )
    chars = [
        Character(
            slug=r["capcom_slug"],
            ja_name=r.get("display_name_ja") or r["capcom_slug"],
            sc_chara=r["sc_chara"],
        )
        for r in rows
        if r.get("sc_chara")
    ]
    if chars_filter:
        chars = [c for c in chars if c.slug in chars_filter or c.sc_chara.lower() in chars_filter]
    return chars


def load_special_names() -> dict[tuple[str, str], str]:
    """(capcom_slug, sc_input) → CAPCOM 公式日本語技名。

    同じ SC input に複数の条件付き日本語名がある場合は、短い名前を優先する。
    """
    from sf6_engine.db import get_client

    rows = (
        get_client()
        .table("special_move_map")
        .select("capcom_slug,capcom_move_name,sc_input")
        .limit(2000)
        .execute()
        .data
        or []
    )
    out: dict[tuple[str, str], str] = {}
    for r in rows:
        key = (r["capcom_slug"], r["sc_input"])
        name = r["capcom_move_name"]
        if key not in out or len(name) < len(out[key]):
            out[key] = name
    return out


def load_moves(character: Character, special_names: dict[tuple[str, str], str]) -> list[Move]:
    from sf6_engine.db import get_client

    rows = (
        get_client()
        .table("sc_move_normalized")
        .select("input,name,move_type")
        .eq("chara", character.sc_chara)
        .limit(500)
        .execute()
        .data
        or []
    )
    moves: list[Move] = []
    for r in rows:
        inp = r.get("input")
        if not inp:
            continue
        bucket = classify_bucket(r.get("move_type"), inp)
        if not bucket:
            continue
        capcom_move_name = special_names.get((character.slug, inp))
        query_name = (
            capcom_move_name
            if bucket in {"special", "super"} and capcom_move_name
            else (r.get("name") or inp) if inp == "-" else inp
        )
        from sf6_engine.frame_data import lookup_frame_data

        lookup = lookup_frame_data(character.slug, query_name)
        profile = ((lookup.get("move") or {}).get("frame_profile") or {})
        facts = profile.get("facts") or {}
        startup = facts.get("startup") or {}
        active = facts.get("active") or {}
        recovery = facts.get("recovery") or {}
        block = facts.get("on_block") or {}
        defender = (profile.get("block_perspectives") or {}).get("defender") or {}
        contextual_block = (
            (profile.get("scenario_evaluation") or {})
            .get("block_perspectives", {})
            .get("attacker", {})
        )
        moves.append(
            Move(
                character=character,
                input=inp,
                name=r.get("name") or inp,
                move_type=r.get("move_type") or "",
                bucket=bucket,
                startup=startup.get("value"),
                block_adv=block.get("value"),
                startup_display=startup.get("display") or "データなし",
                active_display=active.get("display") or "データなし",
                recovery_display=recovery.get("display") or "データなし",
                block_attacker_display=block.get("display") or "データなし",
                block_defender_display=defender.get("display") or "算出不可",
                block_usable_for_calculation=bool(
                    contextual_block.get("usable_for_calculation")
                ),
                capcom_move_name=capcom_move_name,
            )
        )
    return sorted(moves, key=lambda m: (BUCKET_ORDER.index(m.bucket), m.input, m.name))


def load_punisher_options(punisher: Character, window: int) -> list[PunisherOption]:
    if window <= 0:
        return []
    from sf6_engine.db import get_client

    rows = (
        get_client()
        .table("unified_moves")
        .select("move_name,sc_input_key,c_startup,section")
        .eq("character_slug", punisher.slug)
        .lte("c_startup", window)
        .not_.is_("c_startup", "null")
        .not_.ilike("move_name", "%パリィ%")
        .order("c_startup")
        .limit(80)
        .execute()
        .data
        or []
    )
    from sf6_engine.punish import filter_timing_candidates

    candidates = filter_timing_candidates(rows, limit=8)
    return [
        PunisherOption(
            move_name=r["move_name"],
            input=r.get("input"),
            startup=int(r["startup"]),
        )
        for r in candidates
        if r.get("startup") is not None
    ]


def resolve_punishers(spec: str, owner: Character, all_chars: list[Character]) -> list[Character]:
    if spec == "standard":
        wanted_slug = "ryu" if owner.slug != "ryu" else "ken"
        return [c for c in all_chars if c.slug == wanted_slug]
    if spec == "same":
        return [owner]
    if spec == "all":
        return all_chars
    wanted = {s.strip().lower() for s in spec.split(",") if s.strip()}
    return [c for c in all_chars if c.slug in wanted or c.sc_chara.lower() in wanted]


def _case_id(prefix: str, n: int) -> str:
    return f"{prefix}-{n:05d}"


def build_cases(
    *,
    chars: list[Character],
    all_chars: list[Character],
    buckets: set[str],
    case_types: set[str],
    max_per_bucket: int,
    punishers_spec: str,
) -> list[BotCase]:
    special_names = load_special_names()
    punisher_cache: dict[tuple[str, int], list[PunisherOption]] = {}
    cases: list[BotCase] = []
    counters: Counter[str] = Counter()

    for char in chars:
        grouped: dict[str, list[Move]] = defaultdict(list)
        for move in load_moves(char, special_names):
            if move.bucket in buckets:
                grouped[move.bucket].append(move)

        for bucket in BUCKET_ORDER:
            moves = grouped.get(bucket, [])
            if max_per_bucket > 0:
                moves = moves[:max_per_bucket]
            for move in moves:
                label = move.query_name

                if "move_data" in case_types:
                    counters["move_data"] += 1
                    cases.append(
                        BotCase(
                            id=_case_id("MD", counters["move_data"]),
                            case_type="move_data",
                            bucket=bucket,
                            character=char.slug,
                            character_ja=char.ja_name,
                            move_input=move.input,
                            move_name=move.name,
                            query_move_name=label,
                            question=f"{char.ja_name}の{label}の発生は？",
                            expected_frame=move.startup,
                            expected_display=move.startup_display,
                            direct_tool="lookup_move",
                            direct_args={"character": char.slug, "move_name": label},
                        )
                    )

                if "active" in case_types:
                    counters["active"] += 1
                    cases.append(
                        BotCase(
                            id=_case_id("AC", counters["active"]),
                            case_type="active",
                            bucket=bucket,
                            character=char.slug,
                            character_ja=char.ja_name,
                            move_input=move.input,
                            move_name=move.name,
                            query_move_name=label,
                            question=f"{char.ja_name}の{label}の持続は？",
                            expected_display=move.active_display,
                            direct_tool="lookup_move",
                            direct_args={"character": char.slug, "move_name": label},
                        )
                    )

                if "recovery" in case_types:
                    counters["recovery"] += 1
                    cases.append(
                        BotCase(
                            id=_case_id("RC", counters["recovery"]),
                            case_type="recovery",
                            bucket=bucket,
                            character=char.slug,
                            character_ja=char.ja_name,
                            move_input=move.input,
                            move_name=move.name,
                            query_move_name=label,
                            question=f"{char.ja_name}の{label}の硬直は？",
                            expected_display=move.recovery_display,
                            direct_tool="lookup_move",
                            direct_args={"character": char.slug, "move_name": label},
                        )
                    )

                if "guard_attack" in case_types:
                    counters["guard_attack"] += 1
                    cases.append(
                        BotCase(
                            id=_case_id("GA", counters["guard_attack"]),
                            case_type="guard_attack",
                            bucket=bucket,
                            character=char.slug,
                            character_ja=char.ja_name,
                            move_input=move.input,
                            move_name=move.name,
                            query_move_name=label,
                            question=f"{char.ja_name}の{label}をガードさせたら何F？",
                            expected_frame=move.block_adv,
                            expected_display=move.block_attacker_display,
                            direct_tool="lookup_move",
                            direct_args={"character": char.slug, "move_name": label},
                        )
                    )

                if "guard_defense" in case_types:
                    counters["guard_defense"] += 1
                    cases.append(
                        BotCase(
                            id=_case_id("GD", counters["guard_defense"]),
                            case_type="guard_defense",
                            bucket=bucket,
                            character=char.slug,
                            character_ja=char.ja_name,
                            move_input=move.input,
                            move_name=move.name,
                            query_move_name=label,
                            question=f"{char.ja_name}の{label}をガードしたら何F？",
                            expected_frame=-move.block_adv if move.block_adv is not None else None,
                            expected_display=move.block_defender_display,
                            direct_tool="lookup_move",
                            direct_args={"character": char.slug, "move_name": label},
                        )
                    )

                if (
                    "punish_suggestion" in case_types
                    and move.block_adv is not None
                    and move.block_adv < 0
                ):
                    window = -move.block_adv
                    for punisher in resolve_punishers(punishers_spec, char, all_chars):
                        unresolved = not move.block_usable_for_calculation
                        options: list[PunisherOption] = []
                        if not unresolved:
                            key = (punisher.slug, window)
                            if key not in punisher_cache:
                                punisher_cache[key] = load_punisher_options(
                                    punisher, window
                                )
                            options = punisher_cache[key]
                            if not options:
                                continue
                        counters["punish_suggestion"] += 1
                        cases.append(
                            BotCase(
                                id=_case_id("PS", counters["punish_suggestion"]),
                                case_type="punish_suggestion",
                                bucket=bucket,
                                character=char.slug,
                                character_ja=char.ja_name,
                                move_input=move.input,
                                move_name=move.name,
                                query_move_name=label,
                                question=(
                                    f"{char.ja_name}の{label}を{punisher.ja_name}でガードした後、"
                                    "確定反撃に使える技を提案して"
                                ),
                                expected_frame=None if unresolved else window,
                                expected_options=options,
                                expect_punish_unresolved=unresolved,
                                punisher=punisher.slug,
                                punisher_ja=punisher.ja_name,
                                direct_tool="check_punish",
                                direct_args={
                                    "character": char.slug,
                                    "move_name": label,
                                    "punisher": punisher.slug,
                                },
                            )
                        )

    return cases


async def ask_via_bot(case: BotCase, provider: Any) -> tuple[str, dict, list[dict], list[dict | None]]:
    from discord_bot.mcp_router import call_tool, map_intent, result_to_context
    from sf6_engine.intent_parser import parse_intent
    from sf6_engine.rag_builder import generate_answer

    intent = await parse_intent(case.question, provider)
    calls_raw = map_intent(intent)
    calls = [{"tool": tool, "args": args} for tool, args in calls_raw]
    contexts: list[str] = []
    results: list[dict | None] = []
    for tool, args in calls_raw:
        result = await call_tool(tool, args)
        results.append(result)
        contexts.append(result_to_context(tool, args, result))
    context = "\n\n".join(contexts)
    answer = await generate_answer(case.question, context, provider)
    return answer, intent, calls, results


async def ask_via_direct_mcp(case: BotCase, provider: Any) -> tuple[str, dict, list[dict], list[dict | None]]:
    from discord_bot.mcp_router import call_tool, result_to_context
    from sf6_engine.rag_builder import generate_answer

    tool = case.direct_tool
    args = dict(case.direct_args)
    result = await call_tool(tool, args)
    context = result_to_context(tool, args, result)
    answer = await generate_answer(case.question, context, provider)
    return answer, {"intent_type": f"direct:{tool}", "raw_query": case.question}, [
        {"tool": tool, "args": args}
    ], [result]


def evaluate(case: BotCase, answer: str, calls: list[dict[str, Any]]) -> tuple[str, str]:
    if "自動検証で数値の不一致" in answer or "正確な参照データ" in answer:
        return "❌", "デバッグ用の自動検証メッセージがユーザー向け回答に含まれている"

    if case.case_type == "move_data":
        if case.expected_display and answer_contains_display(answer, case.expected_display):
            return "✅", ""
        return "❌", f"発生 {case.expected_display!r} が回答に含まれない"

    if case.case_type in {"active", "recovery"}:
        if case.expected_display and answer_contains_display(answer, case.expected_display):
            return "✅", ""
        label = "持続" if case.case_type == "active" else "硬直"
        return "❌", f"{label} {case.expected_display!r} が回答に含まれない"

    if case.case_type in {"guard_attack", "guard_defense"}:
        if case.expected_display and answer_contains_display(answer, case.expected_display):
            return "✅", ""
        perspective = "攻撃側(ガードさせた側)" if case.case_type == "guard_attack" else "防御側(ガードした側)"
        return "❌", f"{perspective} の期待値 {case.expected_display!r} が回答に含まれない"

    if case.case_type == "punish_suggestion":
        if not any(c["tool"] == "check_punish" for c in calls):
            return "❌", "bot 経路で check_punish が呼ばれていない"
        if case.expect_punish_unresolved:
            if (
                re.search(r"単一値.*確定でき|反撃判定.*保留|候補.*保留", answer)
                and not re.search(r"発生\s*\d+F\s*以内.*確定反撃", answer)
            ):
                return "✅", ""
            return "❌", "条件付き硬直差を確定値として扱わず、反撃判定を保留していない"
        if case.expected_frame is not None and not answer_contains_frame(
            answer, case.expected_frame, field="startup"
        ):
            return "❌", f"確定反撃 window {case.expected_frame}F が回答に含まれない"
        if not case.expected_options:
            return "⚠", "期待する確定反撃候補がDBから取れない"
        if any(answer_mentions_option(answer, opt) for opt in case.expected_options):
            return "✅", ""
        opts = ", ".join(
            f"{o.input or '-'}:{o.move_name}/{o.startup}F" for o in case.expected_options[:5]
        )
        return "❌", f"確定反撃候補が回答に含まれない。候補: {opts}"

    return "⚠", f"未知の case_type: {case.case_type}"


async def run_cases(
    cases: list[BotCase],
    *,
    executor: str,
    output_jsonl: Path | None,
    fail_fast: bool,
    concurrency: int,
    quiet_success: bool,
    progress_every: int,
    retries: int,
    retry_base_sleep: float,
    compact_results: bool,
) -> list[CaseResult]:
    from sf6_engine.factory import create_provider
    from sf6_engine.token_usage import format_usage

    provider = create_provider()
    if hasattr(provider, "is_available") and not await provider.is_available():
        raise RuntimeError("Ollama が起動していません。`ollama serve` を実行してください。")

    if fail_fast and concurrency > 1:
        print("--fail-fast 指定時は結果順を保つため concurrency=1 で実行します。")
        concurrency = 1
    concurrency = max(1, concurrency)

    ask = ask_via_bot if executor == "bot" else ask_via_direct_mcp
    total = len(cases)
    results: list[CaseResult | None] = [None] * total
    semaphore = asyncio.Semaphore(concurrency)
    output_lock = asyncio.Lock()
    completed = 0
    counts: Counter[str] = Counter()

    jsonl_f = output_jsonl.open("w", encoding="utf-8") if output_jsonl else None
    try:
        async def run_one(index: int, case: BotCase) -> CaseResult:
            nonlocal completed
            async with semaphore:
                if not quiet_success:
                    async with output_lock:
                        print(f"[{index + 1:05d}/{total:05d}] {case.id} {case.question}", flush=True)

                cr = CaseResult(case=case)
                t0 = time.monotonic()
                for attempt in range(retries + 1):
                    try:
                        answer, intent, calls, tool_results = await ask(case, provider)
                        cr.elapsed = time.monotonic() - t0
                        cr.answer = answer
                        cr.intent = intent
                        cr.calls = calls
                        cr.tool_results = tool_results
                        cr.result, cr.reason = evaluate(case, answer, calls)
                        if compact_results and cr.result == "✅":
                            # Exhaustive profiles contain every source observation
                            # and are large.  Once graded, successful payloads need
                            # not remain resident in memory.
                            cr.tool_results = []
                        break
                    except Exception as e:  # noqa: BLE001
                        if attempt < retries:
                            await asyncio.sleep(min(60.0, retry_base_sleep * (2 ** attempt)))
                            continue
                        cr.elapsed = time.monotonic() - t0
                        cr.result = "❌"
                        cr.reason = f"{type(e).__name__}: {e}"

                results[index] = cr

                preview = cr.answer[:140].replace("\n", " ")
                async with output_lock:
                    completed += 1
                    counts[cr.result] += 1

                    show_detail = not quiet_success or cr.result != "✅"
                    show_progress = (
                        quiet_success
                        and progress_every > 0
                        and (completed % progress_every == 0 or completed == total)
                    )
                    if show_detail:
                        print(
                            f"  [{index + 1:05d}/{total:05d}] {case.id} {cr.result} {cr.elapsed:.1f}s "
                            f"intent={cr.intent.get('intent_type', '?')} calls={[c['tool'] for c in cr.calls]}"
                        )
                        print(f"  ? {case.question}", flush=True)
                        if preview:
                            print(f"  → {preview}", flush=True)
                        if cr.reason:
                            print(f"  ! {cr.reason}", flush=True)
                    elif show_progress:
                        print(
                            f"[progress] {completed}/{total} "
                            f"✅={counts['✅']} ⚠={counts['⚠']} ❌={counts['❌']}",
                            flush=True,
                        )

                    if jsonl_f:
                        payload = result_to_json(cr)
                        payload["index"] = index + 1
                        payload["total"] = total
                        jsonl_f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                        jsonl_f.flush()

                return cr

        if concurrency == 1:
            for i, case in enumerate(cases):
                cr = await run_one(i, case)
                if fail_fast and cr.result == "❌":
                    break
        else:
            await asyncio.gather(*(run_one(i, case) for i, case in enumerate(cases)))
    finally:
        if jsonl_f:
            jsonl_f.close()

    if hasattr(provider, "usage"):
        print("\n" + format_usage(provider.usage.totals()))
    return [r for r in results if r is not None]


def result_to_json(result: CaseResult) -> dict[str, Any]:
    d = asdict(result)
    d["case"]["expected_options"] = [asdict(o) for o in result.case.expected_options]
    return d


def print_case_summary(cases: list[BotCase]) -> None:
    by_type = Counter(c.case_type for c in cases)
    by_bucket = Counter(c.bucket for c in cases)
    print("生成ケース数:", len(cases))
    print("case_type:", ", ".join(f"{k}={by_type[k]}" for k in CASE_TYPE_ORDER))
    print("bucket:", ", ".join(f"{k}={by_bucket[k]}" for k in BUCKET_ORDER))


def print_result_summary(results: list[CaseResult]) -> None:
    total = len(results)
    counts = Counter(r.result for r in results)
    print("\n" + "=" * 72)
    print("=== Bot Comprehensive Eval Summary ===")
    print("=" * 72)
    print(f"total={total}  ✅={counts['✅']}  ⚠={counts['⚠']}  ❌={counts['❌']}")
    if total:
        print(f"pass_rate={counts['✅'] * 100 / total:.1f}%")

    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        by_type[r.case.case_type][r.result] += 1
        by_bucket[r.case.bucket][r.result] += 1

    print("\nBy case_type")
    for key in CASE_TYPE_ORDER:
        c = by_type[key]
        print(f"  {key:18} ✅{c['✅']:4} ⚠{c['⚠']:4} ❌{c['❌']:4}")

    print("\nBy bucket")
    for key in BUCKET_ORDER:
        c = by_bucket[key]
        print(f"  {key:18} ✅{c['✅']:4} ⚠{c['⚠']:4} ❌{c['❌']:4}")

    failures = [r for r in results if r.result == "❌"]
    if failures:
        print("\nFailures (first 20)")
        for r in failures[:20]:
            print(f"  {r.case.id} [{r.case.bucket}/{r.case.case_type}] {r.case.question}")
            print(f"    {r.reason}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord bot 経路の全技網羅評価")
    parser.add_argument("--chars", nargs="+", help="対象キャラスラグ (例: sagat ken ryu)")
    parser.add_argument(
        "--buckets",
        nargs="+",
        choices=BUCKET_ORDER,
        default=list(BUCKET_ORDER),
        help="対象カテゴリ",
    )
    parser.add_argument(
        "--case-types",
        nargs="+",
        choices=CASE_TYPE_ORDER,
        default=list(CASE_TYPE_ORDER),
        help="評価種別",
    )
    parser.add_argument(
        "--max-per-bucket",
        type=int,
        default=1,
        help="各キャラ×カテゴリから何技取るか。0 は全件。",
    )
    parser.add_argument(
        "--exhaustive",
        action="store_true",
        help="全件実行。--max-per-bucket 0 と同じ。",
    )
    parser.add_argument(
        "--punishers",
        default="standard",
        help="反撃側。standard(リュウ、リュウ相手はケン) / same / all / カンマ区切りslug。既定 standard。",
    )
    parser.add_argument("--limit-cases", type=int, default=0, help="先頭Nケースだけ実行")
    parser.add_argument(
        "--executor",
        choices=("bot", "direct-mcp"),
        default="bot",
        help="bot は intent_parser 込み、direct-mcp は既知のMCP呼び出しを直接使う",
    )
    parser.add_argument("--dry-run", action="store_true", help="ケース生成だけ行い、LLM/MCPは呼ばない")
    parser.add_argument("--fail-fast", action="store_true", help="最初の失敗で停止")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="同時実行数。Ollama/API Gateway の負荷に応じて調整する。",
    )
    parser.add_argument("--quiet-success", action="store_true", help="成功ケースの詳細ログを省略する")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="--quiet-success 時に進捗を表示する完了件数間隔",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="ケース単位の一時エラー再試行回数",
    )
    parser.add_argument(
        "--retry-base-sleep",
        type=float,
        default=0.5,
        help="一時エラー再試行の初期待機秒数。指数バックオフで最大60秒。",
    )
    parser.add_argument(
        "--output",
        default="tests/bot_comprehensive_results.json",
        help="最終 JSON レポートの保存先",
    )
    parser.add_argument(
        "--jsonl",
        default="tests/bot_comprehensive_results.jsonl",
        help="逐次 JSONL レポートの保存先。空文字なら無効。",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="最終JSONに全成功ケースを複製せず、失敗サンプルと集計だけ保存する。",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    if args.exhaustive:
        args.max_per_bucket = 0

    all_chars = load_characters(None)
    chars_filter = {c.lower() for c in args.chars} if args.chars else None
    chars = [c for c in all_chars if not chars_filter or c.slug in chars_filter or c.sc_chara.lower() in chars_filter]
    if not chars:
        print("対象キャラが見つかりません。", file=sys.stderr)
        return 2

    cases = build_cases(
        chars=chars,
        all_chars=all_chars,
        buckets=set(args.buckets),
        case_types=set(args.case_types),
        max_per_bucket=args.max_per_bucket,
        punishers_spec=args.punishers,
    )
    if args.limit_cases > 0:
        cases = cases[: args.limit_cases]

    print_case_summary(cases)
    if args.dry_run:
        for case in cases[:30]:
            print(f"  {case.id} [{case.bucket}/{case.case_type}] {case.question}")
        if len(cases) > 30:
            print(f"  ... and {len(cases) - 30} more")
        return 0

    jsonl = Path(args.jsonl) if args.jsonl else None
    results = await run_cases(
        cases,
        executor=args.executor,
        output_jsonl=jsonl,
        fail_fast=args.fail_fast,
        concurrency=args.concurrency,
        quiet_success=args.quiet_success,
        progress_every=args.progress_every,
        retries=args.retries,
        retry_base_sleep=args.retry_base_sleep,
        compact_results=args.summary_only,
    )
    print_result_summary(results)

    report: dict[str, Any] = {
        "meta": {
            "executor": args.executor,
            "chars": [c.slug for c in chars],
            "buckets": args.buckets,
            "case_types": args.case_types,
            "max_per_bucket": args.max_per_bucket,
            "punishers": args.punishers,
            "concurrency": args.concurrency,
            "quiet_success": args.quiet_success,
            "progress_every": args.progress_every,
            "retries": args.retries,
            "retry_base_sleep": args.retry_base_sleep,
            "summary_only": args.summary_only,
        },
        "summary": Counter(r.result for r in results),
    }
    if args.summary_only:
        report["failure_samples"] = [
            result_to_json(result) for result in results if result.result != "✅"
        ][:100]
    else:
        report["results"] = [result_to_json(r) for r in results]
    out = Path(args.output)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果を {out} に保存しました。")

    return 1 if any(r.result == "❌" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
