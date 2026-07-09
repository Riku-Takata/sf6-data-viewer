"""intent_parser の出力 → MCP ツール呼び出しへのマッピングと、AWS MCP サーバの実行。

設計 (ADR-017 の dogfooding):
  gemma4 が intent_parser で構造化した intent を、本モジュールが MCP ツール名 + 引数に
  変換し、streamable-http の MCP クライアントで AWS の MCP サーバを呼ぶ。
  DB / Bedrock アクセスは MCP サーバ側に閉じる。

intent_type → MCP ツール対応:
  lookup_move      → lookup_move(character, move_name)
  punish_check     → check_punish(character, move_name, punisher?)
  setplay_analysis → compute_setplay(character, move_input)   ※ move_input は numpad/SC入力
  max_combo        → analyze_combo(character, starter_input)
  combo_info       → lookup_move (キャンセル情報を含むため代替)
  explain_concept  → search_system_docs(query)
  compare_moves    → lookup_move ×2
  general_question → search_system_docs(query)
"""
from __future__ import annotations

import json
import logging
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

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


def _slug(chara: str | None) -> str | None:
    """SC 英語名 → capcom slug。未知なら小文字化でフォールバック。"""
    if not chara:
        return None
    return SC_TO_SLUG.get(chara, chara.lower())


def map_intent(intent: dict) -> list[tuple[str, dict]]:
    """intent dict を [(tool_name, arguments), ...] に変換する。

    複数呼び出し (compare_moves) もありうる。解決不能なら空リスト。
    """
    it = intent.get("intent_type")
    chara = intent.get("chara")
    slug = _slug(chara)
    move = intent.get("input") or intent.get("move_name")  # 技識別子
    raw = intent.get("raw_query", "")

    if it == "lookup_move" or it == "combo_info":
        # combo_info は MCP に専用ツールが無いため lookup_move (キャンセル情報含む) で代替
        if slug and move:
            return [("lookup_move", {"character": slug, "move_name": move})]
        return []

    if it == "punish_check":
        if not (slug and move):
            return []
        args = {"character": slug, "move_name": move}
        if intent.get("chara2"):
            args["punisher"] = _slug(intent["chara2"])
        return [("check_punish", args)]

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


async def call_tool(name: str, arguments: dict) -> dict | None:
    """AWS MCP サーバのツールを 1 回呼び出して結果 (JSON dict) を返す。

    stateless サーバなのでリクエスト毎に接続する。失敗時は例外を投げる。
    """
    if not MCP_URL:
        raise RuntimeError("SF6_MCP_URL が未設定です (.env を確認)")
    headers = {"Authorization": f"Bearer {MCP_TOKEN}"} if MCP_TOKEN else None

    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)

    if result.content and getattr(result.content[0], "type", None) == "text":
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}
    return None


def result_to_context(tool: str, args: dict, result: dict | None) -> str:
    """MCP ツール結果を generate_answer 用のコンテキスト文字列に整形する。

    質問で使われた技識別子 (character / move) をヘッダに明示し、MCP が
    SuperCombo 名で返す技 (例: 2HK=Tiger Kick) を LLM が橋渡しできるようにする。
    """
    ch = args.get("character")
    mv = args.get("move_name") or args.get("move_input") or args.get("starter_input")
    # MCP が返す解決後の技名 (SuperCombo 名)。質問の識別子と異なる場合は等値で示し、
    # 「2HK と Tiger Kick は同一技」と LLM が理解できるようにする。
    resolved = (result or {}).get("move_name")
    ident = mv
    if mv and resolved and resolved != mv:
        ident = f"{mv}（{resolved}）"
    head = ""
    if ch or ident:
        who = f"{ch}の" if ch else ""
        head = f"以下は {who}技「{ident}」に関する確定データです（{ident}についての質問にこのまま答えてよい）:\n"

    if result is None:
        return f"{head}[{tool}] 結果が取得できませんでした。"
    if result.get("found") is False:
        msg = result.get("message") or "該当データなし"
        cands = result.get("candidate_names")
        extra = f" 候補: {', '.join(cands)}" if cands else ""
        return f"{head}[{tool}] {msg}{extra}"
    # summary を持つツール (check_punish/setplay/combo/docs/patch) はそれを使う
    if result.get("summary"):
        return f"{head}{result['summary']}"
    # lookup_move 等は move dict をそのまま渡す (gemma が各フィールドを読む)
    return f"{head}{json.dumps(result, ensure_ascii=False)[:1800]}"
