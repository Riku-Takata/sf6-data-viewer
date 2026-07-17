"""intent_parser の出力 → MCP ツール呼び出しへのマッピングと、AWS MCP サーバの実行。

設計 (ADR-017 の dogfooding):
  gemma4 が intent_parser で構造化した intent を、本モジュールが MCP ツール名 + 引数に
  変換し、streamable-http の MCP クライアントで AWS の MCP サーバを呼ぶ。
  DB / Bedrock アクセスは MCP サーバ側に閉じる。

intent_type → MCP ツール対応:
  lookup_move      → lookup_move(character, move_name)
  punish_check     → check_punish(character, move_name, punisher?)
  sequence_analysis→ analyze_sequence(character, attacker_sequence, defender action)
  matchup_interrupt_overview → analyze_matchup_interrupt_overview(attacker, defender)
  setplay_analysis → compute_setplay(character, move_input)   ※ move_input は numpad/SC入力
  max_combo        → analyze_combo(character, starter_input)
  combo_info       → lookup_move (キャンセル情報を含むため代替)
  query_moves      → query_moves(character, typed frame filter)
  explain_concept  → search_system_docs(query)
  compare_moves    → lookup_move ×2
  general_question → search_system_docs(query)
"""
from __future__ import annotations

import json
import logging
import os
import re

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sf6_engine.frame_scenario import strip_scenario_phrases

logger = logging.getLogger("sf6_bot.mcp")

MCP_URL = os.environ.get("SF6_MCP_URL", "")
MCP_TOKEN = os.environ.get("SF6_MCP_TOKEN", "")

# SuperCombo 英語名 (intent_parser の chara 出力) → CAPCOM slug。
# char_slug_map (Supabase) のスナップショット。lookup_move / check_punish は
# capcom slug を要求するため変換が必要 (7キャラが単純小文字化と異なる)。
SC_TO_SLUG: dict[str, str] = {
    "A.K.I.": "aki", "Akuma": "gouki_akuma", "Alex": "alex", "Blanka": "blanka",
    "C.Viper": "cviper", "Cammy": "cammy", "Chun-Li": "chunli", "Dee_Jay": "deejay",
    "Dhalsim": "dhalsim", "E.Honda": "ehonda", "Ed": "ed", "Elena": "elena",
    "Guile": "guile", "Ingrid": "ingrid", "Jamie": "jamie", "JP": "jp",
    "Juri": "juri", "Ken": "ken", "Kimberly": "kimberly", "Lily": "lily",
    "Luke": "luke", "M.Bison": "vega_mbison", "Mai": "mai", "Manon": "manon",
    "Marisa": "marisa", "Rashid": "rashid", "Ryu": "ryu", "Sagat": "sagat",
    "Terry": "terry", "Zangief": "zangief",
}

JP_TO_SLUG: dict[str, str] = {
    "リュウ": "ryu", "ケン": "ken", "サガット": "sagat", "ルーク": "luke",
    "ガイル": "guile", "春麗": "chunli", "チュンリー": "chunli",
    "キャミィ": "cammy", "豪鬼": "gouki_akuma", "アクマ": "gouki_akuma",
    "ザンギエフ": "zangief", "ブランカ": "blanka", "ダルシム": "dhalsim",
    "本田": "ehonda", "エドモンド本田": "ehonda", "エホンダ": "ehonda",
    "ジュリ": "juri", "マリーザ": "marisa", "ジェイミー": "jamie",
    "キンバリー": "kimberly", "リリー": "lily", "マノン": "manon",
    "ラシード": "rashid", "ディージェイ": "deejay", "エド": "ed",
    "テリー": "terry", "舞": "mai", "エレナ": "elena",
    "イングリッド": "ingrid", "アレックス": "alex", "JP": "jp",
    "A.K.I.": "aki", "AKI": "aki", "ベガ": "vega_mbison",
    "M.バイソン": "vega_mbison", "バイソン": "vega_mbison",
    "C.ヴァイパー": "cviper", "ヴァイパー": "cviper",
}


def _slug(chara: str | None) -> str | None:
    """SC 英語名 → capcom slug。未知なら小文字化でフォールバック。"""
    if not chara:
        return None
    return SC_TO_SLUG.get(chara, chara.lower())


def _infer_punisher_from_raw(raw: str, attacker_slug: str | None) -> str | None:
    """「リュウでガード」のような表現から反撃側キャラを補完する。"""
    del attacker_slug  # 同キャラ反撃もあり得るため除外には使わない
    if not raw:
        return None
    for name, slug in sorted(JP_TO_SLUG.items(), key=lambda item: -len(item[0])):
        escaped = re.escape(name)
        if re.search(rf"{escaped}(?:で|が).{{0,10}}ガード", raw):
            return slug
        if re.search(rf"反撃側(?:は|が|:|：)?\s*{escaped}", raw):
            return slug
        if re.search(rf"{escaped}(?:で|が).{{0,10}}反撃", raw):
            return slug
    return None


def _raw_move_phrase(raw: str) -> str | None:
    """日本語質問の「キャラの{技}の項目/をガード」から技部分を抜く。"""
    if not raw:
        return None
    m = re.search(
        r'の(.+?)(?:'
        r'の(?:発生|持続|硬直(?:差)?|全体|ガード|ヒット|ダメージ|性能|フレーム)'
        r'|を|について'
        r'|は(?=(?:発生|持続|硬直(?:差)?|全体|ガード|ヒット|ダメージ|性能|フレーム|何\s*(?:F|フレ|フレーム)))'
        r'|は[？?]|$)',
        raw,
    )
    if not m:
        return None
    phrase = m.group(1).strip()
    return strip_scenario_phrases(phrase)


def _looks_like_input_phrase(text: str) -> bool:
    """SC input らしい表記か判定する (5HP~HP, j.HP, 236[LP] など)。"""
    if not text:
        return False
    if text == "-":
        return True
    if re.fullmatch(r'[LMH]?[PK]{1,3}(?:~[LMH]?[PK]{1,3})*', text):
        return True
    if re.fullmatch(r'[1-9](?:~[1-9])+', text):
        return True
    if re.fullmatch(r'[1-9]\[[1-9]\]', text):
        return True
    if re.fullmatch(r'~[LMH][PK]\s*\([A-Za-z ]+\)', text):
        return True
    return bool(
        re.fullmatch(r'[A-Za-z0-9jJ.\[\]{}()/,+~ \-]+', text)
        and (re.search(r'\d', text) or re.search(r'(?i)j\.', text))
        and re.search(r'[LPKMH]', text.upper())
    )


def _looks_like_jp_normal_shorthand(text: str) -> bool:
    """大K/中P/大足など、intent の input を信じた方がよい通常技略称。"""
    compact = re.sub(r'\s+', '', text)
    return bool(
        re.fullmatch(r'(?:立ち|しゃがみ|屈|ジャンプ|J)?(?:弱|中|強|小|大)?[PKＰＫ]', compact)
        or re.fullmatch(r'[1-9][弱小中強大][PKＰＫ]', compact, re.IGNORECASE)
        or compact in {
            "小足", "中足", "大足", "小パン", "中パン", "大パン",
            "小キック", "中キック", "大キック",
        }
    )


def _move_identifier(intent: dict) -> str | None:
    """MCP に渡す技識別子。

    input は通常技略称の正規化結果を優先する。日本語の必殺技名・略称は
    質問文から技部分だけを抽出し、統合プロファイル側の名前解決に委ねる。
    """
    raw = intent.get("raw_query", "")
    raw_phrase = _raw_move_phrase(raw)
    if intent.get("input"):
        if (
            raw_phrase
            and raw_phrase != intent["input"]
            and _looks_like_input_phrase(raw_phrase)
        ):
            return raw_phrase
        if (
            raw_phrase
            and re.search(r'[ぁ-んァ-ン一-龥]', raw_phrase)
            and not _looks_like_jp_normal_shorthand(raw_phrase)
        ):
            return raw_phrase
        return intent["input"]
    move_name = intent.get("move_name")
    if move_name and raw_phrase and re.search(r'[ぁ-んァ-ン一-龥]', raw_phrase):
        return raw_phrase
    return move_name


def is_alias_learnable_result(tool: str, result: dict | None) -> bool:
    """未知の単一技だけを別名学習の聞き返し対象にする。

    集合検索の0件、キャラ未解決、通信・ツールエラーを「技名が未知」と扱うと、
    質問文そのものを alias として永続保存してしまう。そのため統合プロファイルが
    明示した move_not_found のみを許可する。
    """
    if tool not in {"lookup_move", "check_punish"} or not result:
        return False
    if result.get("found") is not False:
        return False
    resolution = result.get("resolution") or {}
    return (
        resolution.get("status") == "not_found"
        and resolution.get("reason") == "move_not_found"
    )


def map_intent(intent: dict) -> list[tuple[str, dict]]:
    """intent dict を [(tool_name, arguments), ...] に変換する。

    複数呼び出し (compare_moves) もありうる。解決不能なら空リスト。
    """
    it = intent.get("intent_type")
    chara = intent.get("chara")
    slug = _slug(chara)
    move = _move_identifier(intent)  # 技識別子
    raw = intent.get("raw_query", "")
    scenario = intent.get("scenario")

    if it == "lookup_move" or it == "combo_info":
        # combo_info は MCP に専用ツールが無いため lookup_move (キャンセル情報含む) で代替
        if slug and move:
            args = {"character": slug, "move_name": move}
            if scenario:
                args["scenario"] = scenario
            return [("lookup_move", args)]
        return []

    if it == "punish_check":
        if not (slug and move):
            return []
        args = {"character": slug, "move_name": move}
        if scenario:
            args["scenario"] = scenario
        punisher = _slug(intent["chara2"]) if intent.get("chara2") else None
        punisher = punisher or _infer_punisher_from_raw(raw, slug)
        if punisher:
            args["punisher"] = punisher
        return [("check_punish", args)]

    if it == "sequence_analysis":
        sequence = intent.get("attacker_sequence") or []
        if not (slug and len(sequence) == 2):
            return []
        defender = intent.get("defender_action") or {}
        args: dict = {
            "character": slug,
            "attacker_sequence": sequence,
            "initial_interaction": intent.get("initial_interaction") or "block",
        }
        attacker_timing = intent.get("attacker_timing") or {}
        if "delay_f" in attacker_timing:
            args["attacker_delay_f"] = attacker_timing.get("delay_f")
        if isinstance(defender.get("startup_f"), int):
            args["defender_startup_f"] = defender["startup_f"]
        if "delay_f" in defender:
            args["defender_delay_f"] = defender.get("delay_f")
        if defender.get("character"):
            args["defender_character"] = _slug(defender["character"])
        if defender.get("move"):
            args["defender_move"] = defender["move"]
        if intent.get("expected_outcome"):
            args["expected_outcome"] = intent["expected_outcome"]
        if intent.get("query_targets"):
            args["query_targets"] = intent["query_targets"]
        terminal_state = intent.get("terminal_state") or {}
        if terminal_state.get("interaction") in {"block", "hit"}:
            args["terminal_interaction"] = terminal_state["interaction"]
            args["terminal_perspective"] = (
                terminal_state.get("perspective") or "both"
            )
        return [("analyze_sequence", args)]

    if it == "pressure_family_analysis":
        family_move = intent.get("family_move") or intent.get("move_name")
        if not (slug and family_move):
            return []
        args = {
            "character": slug,
            "family_move": family_move,
            "initial_interaction": intent.get("initial_interaction") or "block",
            "variant_scope": intent.get("variant_scope") or "normal",
        }
        if intent.get("opener"):
            args["opener"] = intent["opener"]
        return [("analyze_sequence_family", args)]

    if it == "matchup_interrupt_overview":
        defender = _slug(intent.get("chara2"))
        if slug and defender:
            return [(
                "analyze_matchup_interrupt_overview",
                {"attacker": slug, "defender": defender},
            )]
        return []

    if it == "query_moves":
        if not slug:
            return []
        move_filter = intent.get("move_filter") or {}
        args: dict = {
            "character": slug,
            "field": move_filter.get("field") or "on_block",
            "operator": move_filter.get("operator") or "gt",
            "value": move_filter.get("value", 0),
            "perspective": move_filter.get("perspective") or "attacker",
            "scope": intent.get("move_scope") or "all",
        }
        if scenario:
            args["scenario"] = scenario
        return [("query_moves", args)]

    if it == "setplay_analysis":
        # compute_setplay は SC chara をそのまま解決可。move_input は numpad/SC入力。
        if (chara or slug) and move:
            return [("compute_setplay", {"character": chara or slug, "move_input": move})]
        return []

    if it == "max_combo":
        if (chara or slug) and move:
            return [("analyze_combo", {"character": chara or slug, "starter_input": move})]
        return []

    if it == "explain_concept":
        return [("search_system_docs", {"query": intent.get("concept") or raw})]

    if it == "compare_moves":
        calls: list[tuple[str, dict]] = []
        if slug and move:
            calls.append(("lookup_move", {"character": slug, "move_name": move}))
        c2 = _slug(intent.get("chara2") or chara)
        m2 = intent.get("input2") or intent.get("move_name2")
        if c2 and m2:
            calls.append(("lookup_move", {"character": c2, "move_name": m2}))
        return calls

    # general_question / フォールバック
    return [("search_system_docs", {"query": raw})]


def _local_sc_chara(capcom_slug: str) -> str | None:
    from sf6_engine.db import get_client

    rows = (
        get_client()
        .table("char_slug_map")
        .select("sc_chara")
        .eq("capcom_slug", capcom_slug)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0]["sc_chara"] if rows else None


def _local_find_move_row(character: str, move_name: str) -> dict | None:
    """ローカル Supabase の sc_move_normalized からMCP相当の1技を取得する。"""
    from sf6_engine.db import get_client

    sc_chara = _local_sc_chara(character) or character
    select_cols = (
        "chara,input,name,move_type,startup_f,active_f,recovery_f,"
        "block_adv_f,hit_adv_f,hit_is_knockdown,punish_adv_f,"
        "perf_parry_adv_f,atk_range_n,invuln,notes,damage"
    )
    sb = get_client()

    rows = (
        sb.table("sc_move_normalized")
        .select(select_cols)
        .eq("chara", sc_chara)
        .eq("input", move_name)
        .limit(2)
        .execute()
        .data
        or []
    )
    if rows:
        return rows[0]

    rows = (
        sb.table("sc_move_normalized")
        .select(select_cols)
        .eq("chara", sc_chara)
        .ilike("name", f"%{move_name}%")
        .limit(10)
        .execute()
        .data
        or []
    )
    if not rows:
        return None

    move_type_order = {"Special": 0, "special": 0, "Super": 1, "super": 1}
    rows.sort(key=lambda r: (move_type_order.get(r.get("move_type"), 9), r.get("input") or ""))
    return rows[0]


def _local_move_payload(character: str, query_move_name: str, row: dict) -> dict:
    block_adv = row.get("block_adv_f")
    hit_adv = row.get("hit_adv_f")
    note = None
    if isinstance(block_adv, int) or isinstance(hit_adv, int):
        parts = []
        if isinstance(block_adv, int):
            parts.append(
                f"ガード時{block_adv:+d}F は技を出した側の視点 "
                f"(ガードした側は {-block_adv:+d}F)"
            )
        if isinstance(hit_adv, int):
            parts.append(
                f"ヒット時{hit_adv:+d}F は技を当てた側の視点 "
                f"(食らった側は {-hit_adv:+d}F)"
            )
        note = "。".join(parts)

    move = {
        "source": "local_supabase",
        "frame_perspective_note": note,
        "input": row.get("input"),
        "move_name": row.get("name"),
        "move_type": row.get("move_type"),
        "guard": None,
        "startup": row.get("startup_f"),
        "active": row.get("active_f"),
        "recovery": row.get("recovery_f"),
        "on_block": block_adv,
        "on_hit": hit_adv,
        "on_hit_is_knockdown": bool(row.get("hit_is_knockdown")),
        "punish_adv": row.get("punish_adv_f"),
        "perf_parry_adv": row.get("perf_parry_adv_f"),
        "damage": row.get("damage"),
        "atk_range": row.get("atk_range_n"),
        "invuln": row.get("invuln"),
        "notes": row.get("notes"),
        "raw": {},
    }
    try:
        from sf6_engine.ufd import fetch_ufd_details

        details = fetch_ufd_details(
            character, sc_input=move["input"], move_name=move["move_name"]
        )
        if details:
            move["ufd"] = details
    except Exception as exc:  # migration未適用や一時DB障害では既存回答を優先する
        logger.debug("local UFD lookup failed: %s", exc)

    return {
        "found": True,
        "character": character,
        "query_move_name": query_move_name,
        "move": move,
        "message": "ローカル Supabase から取得しました。",
    }


def _call_local_tool(name: str, arguments: dict) -> dict | None:
    """API Gateway のレート制限時に使うローカルMCP相当実装。"""
    if name == "analyze_sequence":
        from sf6_engine.sequence_analysis import analyze_sequence

        return analyze_sequence(
            arguments.get("character", ""),
            arguments.get("attacker_sequence") or [],
            initial_interaction=arguments.get("initial_interaction") or "block",
            defender_startup_f=arguments.get("defender_startup_f"),
            defender_character=arguments.get("defender_character"),
            defender_move=arguments.get("defender_move"),
            expected_outcome=arguments.get("expected_outcome"),
            attacker_delay_f=(
                arguments["attacker_delay_f"]
                if "attacker_delay_f" in arguments else 0
            ),
            defender_delay_f=(
                arguments["defender_delay_f"]
                if "defender_delay_f" in arguments else 0
            ),
            query_targets=arguments.get("query_targets"),
            terminal_interaction=arguments.get("terminal_interaction"),
            terminal_perspective=arguments.get("terminal_perspective") or "both",
        )
    if name == "analyze_sequence_family":
        from sf6_engine.pressure_family import analyze_pressure_family

        return analyze_pressure_family(
            arguments.get("character", ""),
            arguments.get("family_move", ""),
            opener=arguments.get("opener"),
            initial_interaction=arguments.get("initial_interaction") or "block",
            variant_scope=arguments.get("variant_scope") or "normal",
        )
    if name == "analyze_matchup_interrupt_overview":
        from sf6_engine.matchup_interrupt import analyze_matchup_interrupt_overview

        return analyze_matchup_interrupt_overview(
            arguments.get("attacker", ""),
            arguments.get("defender", ""),
        )
    if name == "query_moves":
        from sf6_engine.frame_data import query_frame_data

        return query_frame_data(
            arguments.get("character", ""),
            field=arguments.get("field") or "on_block",
            operator=arguments.get("operator") or "gt",
            value=arguments.get("value", 0),
            perspective=arguments.get("perspective") or "attacker",
            scope=arguments.get("scope") or "all",
            scenario=arguments.get("scenario"),
        )
    if name not in {"lookup_move", "check_punish"}:
        return None
    character = arguments.get("character")
    move_name = arguments.get("move_name")
    if not character or not move_name:
        return {"found": False, "message": "character / move_name が不足しています。"}

    scenario = arguments.get("scenario")
    if name == "check_punish":
        from sf6_engine.punish_service import check_punish_data

        return check_punish_data(
            character,
            move_name,
            arguments.get("punisher"),
            scenario,
        )

    from sf6_engine.frame_data import lookup_frame_data

    return lookup_frame_data(character, move_name, scenario=scenario)


async def call_tool(name: str, arguments: dict) -> dict | None:
    """AWS MCP サーバのツールを 1 回呼び出して結果 (JSON dict) を返す。

    stateless サーバなのでリクエスト毎に接続する。失敗時は例外を投げる。
    """
    if os.environ.get("SF6_MCP_LOCAL_ONLY") == "1":
        local = _call_local_tool(name, arguments)
        if local is not None:
            return local
    if not MCP_URL:
        raise RuntimeError("SF6_MCP_URL が未設定です (.env を確認)")
    headers = {"Authorization": f"Bearer {MCP_TOKEN}"} if MCP_TOKEN else None

    try:
        async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    except Exception:
        if os.environ.get("SF6_MCP_LOCAL_FALLBACK", "1") == "1":
            local = _call_local_tool(name, arguments)
            if local is not None:
                return local
        raise

    if result.content and getattr(result.content[0], "type", None) == "text":
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
    return None


def _fmt_signed(value: object) -> str:
    if isinstance(value, int):
        return f"{value:+d}F"
    return "データなし" if value is None else f"{value}"


def _format_lookup_context(tool: str, args: dict, result: dict) -> str | None:
    """lookup_move の move dict を回答生成向けの安定したテキストにする。"""
    if tool != "lookup_move" or not result.get("move"):
        return None
    move = result["move"]
    profile = move.get("frame_profile")
    if profile:
        from sf6_engine.frame_data import format_frame_profile_context

        lines = [format_frame_profile_context(profile)]
        ufd = move.get("ufd") or {}
        if ufd.get("hitbox_source_url"):
            lines.append(f"当たり判定GIF (UFD): {ufd['hitbox_source_url']}")
        if ufd.get("notes"):
            lines.append(f"UFDメモ: {ufd['notes'][:500]}")
        return "\n".join(lines)

    ch = result.get("character") or args.get("character") or "?"
    inp = move.get("input") or args.get("move_name") or "?"
    name = move.get("move_name") or args.get("move_name") or "?"
    lines = [f"【{ch} / {inp} ({name})】"]

    if move.get("startup") is not None:
        lines.append(f"発生: {move['startup']}F")
    if move.get("active") is not None:
        lines.append(f"持続: {move['active']}F")
    if move.get("recovery") is not None:
        lines.append(f"硬直: {move['recovery']}F")

    block_adv = move.get("on_block")
    if isinstance(block_adv, int):
        lines.append(
            f"ガード時: {block_adv:+d}F "
            f"(技を出した側が{block_adv:+d}F / ガードした側は{-block_adv:+d}F)"
        )
    elif block_adv is not None:
        lines.append(f"ガード時: {block_adv}")

    hit_adv = move.get("on_hit")
    if isinstance(hit_adv, int):
        lines.append(
            f"ヒット時: {hit_adv:+d}F "
            f"(技を当てた側が{hit_adv:+d}F / 食らった側は{-hit_adv:+d}F)"
        )
    elif hit_adv is not None:
        lines.append(f"ヒット時: {hit_adv}")

    if move.get("damage") is not None:
        lines.append(f"ダメージ: {move['damage']}")
    if move.get("atk_range") is not None:
        lines.append(f"リーチ: {move['atk_range']}")
    if move.get("invuln"):
        lines.append(f"無敵: {move['invuln']}")
    if move.get("punish_adv") is not None:
        lines.append(f"パニッシュカウンター時: {_fmt_signed(move['punish_adv'])}")
    if move.get("notes"):
        lines.append(f"メモ: {move['notes']}")
    if move.get("ufd"):
        try:
            from sf6_engine.ufd import format_ufd_details

            lines.append(format_ufd_details(move["ufd"]))
        except Exception as exc:
            logger.debug("UFD context formatting failed: %s", exc)
    return "\n".join(lines)


def _format_punish_context(tool: str, result: dict) -> str | None:
    """check_punish の summary に確定反撃候補を補う。"""
    if tool != "check_punish" or not result.get("summary"):
        return None
    lines = [result["summary"]]
    options = result.get("punisher_options") or []
    if options:
        punisher = result.get("punisher") or "反撃側"
        lines.append(f"【{punisher} のフレーム上の反撃候補（到達未検証）】")
        for opt in options:
            inp = opt.get("input") or "-"
            name = opt.get("move_name") or "不明"
            startup = opt.get("startup")
            resource = f" / {opt['resource_requirement']}" if opt.get("resource_requirement") else ""
            lines.append(
                f"- {inp} / {name}: 発生{startup}F / リーチ未検証{resource}"
            )
    return "\n".join(lines)


def result_to_context(tool: str, args: dict, result: dict | None) -> str:
    """MCP ツール結果を generate_answer 用のコンテキスト文字列に整形する。

    質問で使われた技識別子 (character / move) をヘッダに明示し、MCP が
    SuperCombo 名で返す技 (例: 2HK=Tiger Kick) を LLM が橋渡しできるようにする。
    """
    if tool == "query_moves" and result and result.get("summary"):
        return str(result["summary"])

    ch = args.get("character")
    mv = args.get("move_name") or args.get("move_input") or args.get("starter_input")
    if tool == "analyze_sequence":
        sequence = args.get("attacker_sequence") or []
        mv = " -> ".join(sequence) if sequence else "連携"
    if tool == "analyze_sequence_family":
        mv = args.get("family_move") or "技ファミリー"
    if tool == "analyze_matchup_interrupt_overview":
        ch = args.get("attacker")
        mv = f"対 {args.get('defender') or '?'} の代表連携"
    # MCP が返す解決後の技名 (SuperCombo 名)。質問の識別子と異なる場合は等値で示し、
    # 「2HK と Tiger Kick は同一技」と LLM が理解できるようにする。
    resolved = (result or {}).get("move_name") or ((result or {}).get("move") or {}).get("move_name")
    ident = mv
    if mv and resolved and resolved != mv:
        ident = f"{mv}（{resolved}）"
    head = ""
    if ch or ident:
        who = f"{ch}の" if ch else ""
        head = f"以下は {who}技「{ident}」に関する構造化データです。解決状態と条件評価に従って回答してください:\n"

    if result is None:
        return f"{head}[{tool}] 結果が取得できませんでした。"
    if result.get("found") is False:
        msg = result.get("message") or "該当データなし"
        cands = result.get("candidate_names")
        extra = f" 候補: {', '.join(cands)}" if cands else ""
        return f"{head}[{tool}] {msg}{extra}"
    formatted = _format_lookup_context(tool, args, result)
    if formatted:
        return f"{head}{formatted}"
    formatted = _format_punish_context(tool, result)
    if formatted:
        return f"{head}{formatted}"
    # summary を持つツール (check_punish/setplay/combo/docs/patch) はそれを使う
    if result.get("summary"):
        return f"{head}{result['summary']}"
    # lookup_move 等は move dict をそのまま渡す (gemma が各フィールドを読む)
    return f"{head}{json.dumps(result, ensure_ascii=False)[:1800]}"
