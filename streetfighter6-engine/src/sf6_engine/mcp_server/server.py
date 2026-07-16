"""SF6 Engine MCP サーバ (FastMCP).

実装済みの決定論ロジック層を MCP ツールとして公開する。LLM 段は含めない
(ADR-017)。各ツールは JSON シリアライズ可能な dict を返し、人間/LLM が読みやすい
``summary`` フィールド (既存フォーマッタの出力) を併せて返す。

起動 (ローカル stdio):
  PYTHONPATH=src python -m sf6_engine.mcp_server.server

起動 (リモート Streamable HTTP, ステップ4で AWS Lambda 化):
  SF6_MCP_TRANSPORT=streamable-http PYTHONPATH=src python -m sf6_engine.mcp_server.server

公開ツール (ステップ1):
  - lookup_move      : 単一技のフレームデータ照会
  - check_punish     : ガード時の確定反撃判定 (任意で反撃側の確定択を列挙)
  - analyze_sequence : 連携・最速暴れ・相打ち後の有利と追撃を解析
  - compute_setplay  : KD/ヒット後の起き攻め択計算
  - analyze_combo    : 始動技からの最大コンボ計算 (ビームサーチ)
  - list_moves       : キャラの技名一覧 (技名解決の補助)
  - query_moves      : キャラ内の技をフレーム条件で集合検索
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from sf6_engine.db import get_client
from sf6_engine.frame_data import lookup_frame_data, query_frame_data
from sf6_engine.punish_service import check_punish_data
from sf6_engine.pressure_family import analyze_pressure_family
from sf6_engine.sequence_analysis import analyze_sequence as analyze_sequence_data
from sf6_engine.ufd import fetch_ufd_details

# stateless_http + json_response: HTTP transport を AWS Lambda / API Gateway 向けに
# セッションレス・単一JSONレスポンス化する (SSE/セッション不要)。stdio では無影響。
# transport_security: FastMCP は host=127.0.0.1 だと DNS rebinding 保護を自動 ON にし
# API Gateway のホスト名を 421 で弾く。認証は app.py の Bearer ミドルウェアで行うため無効化。
mcp = FastMCP(
    "sf6-engine",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

logger = logging.getLogger(__name__)


# ============================================================
# 内部ヘルパー
# ============================================================

def _resolve_sc_chara(chara: str) -> str | None:
    """capcom_slug → SuperCombo chara 名に変換する。見つからなければ None。"""
    sb = get_client()
    res = (
        sb.table("char_slug_map").select("sc_chara")
        .eq("capcom_slug", chara.lower()).execute()
    )
    if res.data:
        return res.data[0]["sc_chara"]
    res2 = (
        sb.table("char_slug_map").select("sc_chara")
        .ilike("sc_chara", f"%{chara}%").execute()
    )
    return res2.data[0]["sc_chara"] if res2.data else None


def _fetch_sc_move(sc_chara: str, move_input: str) -> dict | None:
    """sc_moves から input 完全一致で 1 件取得する。"""
    sb = get_client()
    res = (
        sb.table("sc_moves").select("input,name,hit_adv,block_adv,damage")
        .eq("chara", sc_chara).eq("input", move_input).limit(1).execute()
    )
    return res.data[0] if res.data else None


def _sc_input_lookup(character: str, move_input: str) -> dict | None:
    """sc_move_normalized から numpad input (例: '2HK', '623HP') で 1 件取得する。

    CAPCOM 側の日本語技名照会 (handlers.lookup) が解決できない numpad 表記を、
    SuperCombo 正規化ビューの input フィールドで決定論的に解決するフォールバック。
    """
    sc_chara = _resolve_sc_chara(character)
    if not sc_chara:
        return None
    sb = get_client()
    res = (
        sb.table("sc_move_normalized").select(
            "input,name,move_type,guard,startup_f,active_f,recovery_f,"
            "block_adv_f,hit_adv_f,hit_is_knockdown,punish_adv_f,perf_parry_adv_f,"
            "damage,atk_range_n,invuln,notes,hit_adv,block_adv,punish_adv"
        )
        .eq("chara", sc_chara)
        .ilike("input", move_input)  # ワイルドカードなし = 大文字小文字無視の完全一致
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _resolve_move_input(character: str, query: str) -> tuple[str, str] | None:
    """技名 (日本語/英語) を SuperCombo numpad input に解決する。

    '623HP' のような input 完全一致で見つからない場合に、sc_move_normalized の
    name ILIKE 検索 (rag_builder._fetch_move_by_name: 単語分割・JP→EN マッピング・
    強度修飾子判別込み) で input を逆引きするフォールバック。

    Args:
        character: capcom slug。
        query:     技名 (例: 'タイガーアッパーカット', 'Tiger Uppercut', '強タイガーニー')。

    Returns:
        (input, name) のタプル。解決できなければ None。
    """
    from sf6_engine.rag_builder import _fetch_move_by_name

    row = _fetch_move_by_name(character, query, raw_query=query)
    if row and row.get("input"):
        return row["input"], row.get("name") or query
    return None


def _sc_name_fallback(character: str, move_name: str) -> dict | None:
    """技名から SC 行を解決する最終フォールバック。

    _resolve_move_input (rag_builder._fetch_move_by_name) 経由なので、
    special_move_map (CAPCOM 公式日本語名) / move_aliases (学習略称) /
    英語名 ILIKE のすべてを試す。解決した input で完全な SC 行を再取得する。
    """
    resolved = _resolve_move_input(character, move_name)
    if not resolved:
        return None
    return _sc_input_lookup(character, resolved[0])


# フレーム有利の視点注記 (消費側 LLM の視点取り違え防止)。
# on_block/on_hit は「技を出した側」の視点。ガードした側/食らった側は符号反転。
def _perspective_note(on_block, on_hit) -> str | None:
    parts = []
    if isinstance(on_block, int):
        parts.append(
            f"ガード時{on_block:+d}F は技を出した側の視点 "
            f"(ガードした側は {-on_block:+d}F)")
    if isinstance(on_hit, int):
        parts.append(
            f"ヒット時{on_hit:+d}F は技を当てた側の視点 "
            f"(食らった側は {-on_hit:+d}F)")
    return "。".join(parts) or None


def _attach_ufd(move: dict, character_slug: str) -> dict:
    """UFD実測の補足をMCPレスポンスへ追加する。未導入時は既存形を保つ。"""
    details = fetch_ufd_details(
        character_slug,
        sc_input=move.get("input"),
        move_name=move.get("move_name"),
    )
    if details:
        move["ufd"] = details
    return move


def _sc_to_move(row: dict, character_slug: str) -> dict:
    """sc_move_normalized の行を MCP の move レスポンス形に正規化する。"""
    return _attach_ufd({
        "source": "supercombo",
        "frame_perspective_note": _perspective_note(
            row.get("block_adv_f"), row.get("hit_adv_f")),
        "input": row.get("input"),
        "move_name": row.get("name"),
        "move_type": row.get("move_type"),
        "guard": row.get("guard"),
        "startup": row.get("startup_f"),
        "active": row.get("active_f"),
        "recovery": row.get("recovery_f"),
        "on_block": row.get("block_adv_f"),
        "on_hit": row.get("hit_adv_f"),
        "on_hit_is_knockdown": row.get("hit_is_knockdown"),
        "punish_adv": row.get("punish_adv_f"),
        "perf_parry_adv": row.get("perf_parry_adv_f"),
        "damage": row.get("damage"),
        "atk_range": row.get("atk_range_n"),
        "invuln": row.get("invuln"),
        "notes": row.get("notes"),
        "raw": {
            "hit_adv": row.get("hit_adv"),
            "block_adv": row.get("block_adv"),
            "punish_adv": row.get("punish_adv"),
        },
    }, character_slug)


# ============================================================
# ツール: lookup_move
# ============================================================

def _lookup_move_impl(
    character: str,
    move_name: str,
    scenario: dict | None = None,
) -> dict:
    """MCP/ローカル経路で共有する決定論的な技照会実装。"""
    return lookup_frame_data(character, move_name, scenario=scenario)


@mcp.tool()
def lookup_move(
    character: str,
    move_name: str,
    scenario: dict | None = None,
) -> dict:
    """指定キャラの単一技のフレームデータ (発生・持続・硬直・ガード/ヒット時有利・
    ダメージ・キャンセル可否) を返す。

    Args:
        character: キャラの slug (例: 'sagat', 'ryu', 'ken')。
        move_name: 技名。正式名 (例: '立ち弱P（タイガージャブ）') または
                   部分一致 (例: 'タイガージャブ', '2HK') を受け付ける。

    Returns:
        dict: ``found`` が True なら ``move`` に統合フレームプロファイルと
              互換用の主要フィールドが入る。各値には採用ソースと全ソースの
              観測値が付き、ガード時は攻撃側・防御側の両視点を返す。
    """
    return _lookup_move_impl(character, move_name, scenario)


# ============================================================
# ツール: check_punish
# ============================================================

@mcp.tool()
def check_punish(
    character: str,
    move_name: str,
    punisher: str | None = None,
    scenario: dict | None = None,
) -> dict:
    """ある技をガードした後の反撃可能性を条件付きで判定する。

    フレーム窓と空間的な到達可能性を分離する。現行DBで到達を証明できない候補は
    ``timing_only`` とし、確定反撃が成立すると断定しない。

    Args:
        character: ガードされる技を持つキャラの slug。
        move_name: ガードされる技名 (部分一致可)。
        punisher:  反撃する側のキャラ slug (任意)。指定すると、そのキャラの
                   フレーム上の候補を列挙する。
        scenario:  距離・接触持続・相手状態など、質問に明示された状況条件。

    Returns:
        dict: ``frame_punishable`` (時間条件)、``confirmed_punishable``
              (時間+距離を含む確定状態)、``punish_window_f``、候補と検証状態。
    """
    return check_punish_data(character, move_name, punisher, scenario)


# ============================================================
# ツール: analyze_sequence
# ============================================================

@mcp.tool()
def analyze_sequence(
    character: str,
    attacker_sequence: list[str],
    initial_interaction: str = "block",
    defender_startup_f: int | None = None,
    defender_character: str | None = None,
    defender_move: str | None = None,
    expected_outcome: str | None = None,
    attacker_delay_f: int | None = 0,
    defender_delay_f: int | None = 0,
    query_targets: list[str] | None = None,
    terminal_interaction: str | None = None,
    terminal_perspective: str = "both",
) -> dict:
    """2技の連携と最速暴れを共通タイムライン上で解析する。

    2技とも統合DB resolverで解決する。通常のlinkは硬直差、SuperComboの
    Special/SA/Chain cancel根拠がtargetと一致する最速連携は、hitstop終了後の
    blockstun/hitstunを基準にする。後者では ``true_blockstring`` または
    ``interrupt_timing_win`` を返し、距離・リーチまで確認できない限り時間上の結果と
    実戦での確定を区別する。相打ち・追撃解析も従来どおり決定論で返す。

    Args:
        character: 攻撃側キャラのslug (例: ``sagat``)。
        attacker_sequence: 攻撃側の技列。現在は2技 (例: ``["5MP", "5MP"]``)。
        initial_interaction: 1技目の結果。``block`` または ``hit``。
        defender_startup_f: 防御側が最速で出す技の発生。
        defender_character: 防御側キャラ。特定技を解析するときに指定。
        defender_move: 防御側の技input/名前。characterと組で指定。
        expected_outcome: 質問で前提とされた結果 (例: ``trade``)。
        attacker_delay_f: 2発目の最速入力からの遅らせF。不明ならnull。
        defender_delay_f: 防御側行動の最速入力からの遅らせF。不明ならnull。
        query_targets: blockstring、combo_timing、timeline等の質問対象。
        terminal_interaction: 2技目の結果。終端硬直差質問のblock/hit。
        terminal_perspective: 終端硬直差の回答視点。attacker/defender/both。

    Returns:
        dict: 遷移種別、時系列、blockstring/割り込みの時間判定、接触確度、
              相打ち後有利・追撃候補（該当時）、根拠、決定論生成された ``summary``。
    """
    return analyze_sequence_data(
        character,
        attacker_sequence,
        initial_interaction=initial_interaction,
        defender_startup_f=defender_startup_f,
        defender_character=defender_character,
        defender_move=defender_move,
        expected_outcome=expected_outcome,
        attacker_delay_f=attacker_delay_f,
        defender_delay_f=defender_delay_f,
        query_targets=query_targets,
        terminal_interaction=terminal_interaction,
        terminal_perspective=terminal_perspective,
    )


@mcp.tool()
def analyze_sequence_family(
    character: str,
    family_move: str,
    opener: str | None = None,
    initial_interaction: str = "block",
    variant_scope: str = "normal",
) -> dict:
    """強度違いを持つ必殺技ファミリーを、同じ始動技からまとめて解析する。

    始動技が省略された場合は、レビュー済みの代表連携があるときだけその前提を
    回答内に明示して使用する。該当する代表連携がない場合は、始動技を聞き返す。

    Args:
        character: 攻撃側キャラのslug。
        family_move: 強度を省略した技ファミリー名（例: ``Jinrai Kick``）。
        opener: 1技目。省略時はレビュー済み代表連携のみ補完する。
        initial_interaction: 1技目の結果。``block`` または ``hit``。
        variant_scope: ``normal`` は弱/中/強、``all`` はOD等も含める。
    """
    return analyze_pressure_family(
        character,
        family_move,
        opener=opener,
        initial_interaction=initial_interaction,
        variant_scope=variant_scope,
    )


# ============================================================
# ツール: compute_setplay
# ============================================================

@mcp.tool()
def compute_setplay(character: str, move_input: str) -> dict:
    """技のヒット/ダウン (KD) 後の起き攻め (セットプレイ) 択を計算する。

    KD 後有利Fから前ステップ等のアクションコストを引き、残り有利F以内に
    発生する技・必殺技・投げを「確定する択」として列挙する。

    Args:
        character:  キャラの slug。
        move_input: 技の input (SuperCombo 表記。例: '623HP', '5HP', '236236P')
                    または技名 (例: 'タイガーアッパーカット', 'Tiger Uppercut')。
                    技名の場合は自動で input に解決する。

    Returns:
        dict: ``scenarios`` (即攻め/前ステップ等のシナリオ別の択リスト) と、
              人間/LLM が読みやすい ``summary`` テキスト。技名から解決した場合は
              ``queried_move`` に元の指定を残す。
    """
    from sf6_engine.setplay_engine import compute_setplay as _setplay
    from sf6_engine.setplay_engine import format_setplay_context

    sc_chara = _resolve_sc_chara(character)
    if not sc_chara:
        return {"found": False, "message": f"キャラ '{character}' が見つかりません。"}

    queried = move_input
    row = _fetch_sc_move(sc_chara, move_input)
    if row is None:
        # input 完全一致で見つからない → 技名 (日本語/英語) として解決を試みる
        resolved = _resolve_move_input(character, move_input)
        if resolved:
            move_input = resolved[0]
            row = _fetch_sc_move(sc_chara, move_input)
    if row is None:
        return {
            "found": False,
            "message": f"{character} の技 '{queried}' が見つかりません。"
                       " list_moves で input を確認してください。",
        }

    move_name = row.get("name") or move_input
    hit_adv_raw = row.get("hit_adv")

    scenarios = _setplay(character, move_input, move_name, hit_adv_raw)
    if not scenarios:
        return {
            "found": True,
            "character": character,
            "queried_move": queried,
            "move_input": move_input,
            "move_name": move_name,
            "hit_adv": hit_adv_raw,
            "scenarios": [],
            "summary": f"{move_input} ({move_name}) は有利F取得不可のためセットプレイ計算不可。",
        }

    return {
        "found": True,
        "character": character,
        "queried_move": queried,
        "move_input": move_input,
        "move_name": move_name,
        "hit_adv": hit_adv_raw,
        "scenarios": [asdict(s) for s in scenarios],
        "summary": format_setplay_context(move_input, move_name, hit_adv_raw, scenarios),
    }


# ============================================================
# ツール: analyze_combo
# ============================================================

@mcp.tool()
def analyze_combo(
    character: str,
    starter_input: str,
    use_dr: bool = True,
    drive_bars: int = 6,
) -> dict:
    """始動技から最大ダメージのコンボルートを計算する (ビームサーチ)。

    Args:
        character:     キャラの slug。
        starter_input: 始動技の input (SuperCombo 表記。例: '2MP', '5HP') または
                       技名 (例: 'タイガーニー')。技名の場合は自動で input に解決する。
        use_dr:        ドライブラッシュ (DR) キャンセルを許可するか。
        drive_bars:    使用可能なドライブゲージ本数 (0-6)。

    Returns:
        dict: ``found`` が True なら ``route`` (コンボ表記)、``total_damage``、
              ``total_drive_spent``、``ends_in_kd``、各ステップの ``steps``、
              人間/LLM が読みやすい ``summary`` を返す。コンボが成立しない
              (始動技が KD / 有利なし等) 場合は found=False。技名から解決した
              場合は ``queried_starter`` に元の指定を残す。
    """
    from sf6_engine.combo_engine import compute_max_combo

    # input 完全一致で存在しない場合は技名 (日本語/英語) → input 解決を試みる
    queried = starter_input
    sc_chara = _resolve_sc_chara(character)
    if sc_chara and _fetch_sc_move(sc_chara, starter_input) is None:
        resolved = _resolve_move_input(character, starter_input)
        if resolved:
            starter_input = resolved[0]

    result = compute_max_combo(
        character, starter_input, use_dr=use_dr, drive_bars=drive_bars
    )
    if result is None:
        return {
            "found": False,
            "character": character,
            "queried_starter": queried,
            "starter_input": starter_input,
            "message": f"{queried} からのコンボは見つかりませんでした"
                       " (始動技が KD / 有利なし / データ欠落のいずれか)。",
        }

    return {
        "found": True,
        "character": character,
        "queried_starter": queried,
        "starter_input": starter_input,
        "route": result.route_str(),
        "total_damage": result.total_damage,
        "total_drive_spent": result.total_drive_spent,
        "ends_in_kd": result.ends_in_kd,
        "initial_adv": result.initial_adv,
        "steps": [asdict(s) for s in result.steps],
        "summary": result.format_context(),
    }


# ============================================================
# ツール: list_moves
# ============================================================

@mcp.tool()
def list_moves(character: str, keyword: str | None = None) -> dict:
    """指定キャラの技一覧 (技名 + SC numpad input) を返す。
    自然言語の技名を input / 正式名に解決する補助。

    Args:
        character: キャラの slug。
        keyword:   技名または input の部分一致フィルタ (任意。例: 'タイガー', '2HK')。

    Returns:
        dict: ``moves`` (move_name / input / section のリスト)。``input`` は
              compute_setplay / analyze_combo にそのまま渡せる numpad 表記
              (通常技のみ。必殺技等は null の場合あり)。
    """
    sb = get_client()
    q = (
        sb.table("unified_moves").select("move_name,sc_input_key,section")
        .eq("character_slug", character.lower())
    )
    if keyword:
        # PostgREST の or_ フィルタは ',' が区切り文字のため除去してから埋め込む
        kw = keyword.replace(",", "")
        q = q.or_(f"move_name.ilike.%{kw}%,sc_input_key.ilike.%{kw}%")
    res = q.limit(300).execute()

    moves = [
        {
            "move_name": r["move_name"],
            "input": r.get("sc_input_key"),
            "section": r.get("section"),
        }
        for r in (res.data or [])
    ]
    return {
        "character": character,
        "count": len(moves),
        "moves": moves,
    }


# ============================================================
# ツール: query_moves
# ============================================================

@mcp.tool()
def query_moves(
    character: str,
    field: str = "on_block",
    operator: str = "gt",
    value: int = 0,
    perspective: str = "attacker",
    scope: str = "all",
    scenario: dict | None = None,
) -> dict:
    """キャラ内の技を、型付きフレーム条件で検索する。

    例: on_block > 0 を attacker 視点で指定すると、ガードさせて有利な技を
    検索する。条件付き技と範囲値は通常条件の確定結果と分けて返すため、
    単純な数値比較で誤って断定しない。

    Args:
        character: キャラの slug。
        field: 比較するフレーム項目。現在は on_block。
        operator: gt / gte / lt / lte / eq。
        value: 比較基準フレーム。
        perspective: attacker (技を出した側) または defender。
        scope: all / normal / ground_normal / special / super。
        scenario: 任意の条件コンテキスト。

    Returns:
        matches (通常条件で確定)、conditional_matches (条件付きで成立)、
        unresolved (条件・範囲のため断定不能) と読みやすい summary。
        キャラが存在し、該当が0件の場合も found=True を返す。
    """
    return query_frame_data(
        character,
        field=field,
        operator=operator,
        value=value,
        perspective=perspective,
        scope=scope,
        scenario=scenario,
    )


# ============================================================
# ツール: search_system_docs
# ============================================================

@mcp.tool()
def search_system_docs(query: str, count: int = 3, threshold: float = 0.4) -> dict:
    """SF6 のゲームシステム文書 (ドライブ系・パリィ・起き攻め・ジャグル等の
    メカニクス解説) をベクトル + キーワードのハイブリッドで検索する。

    lookup_move 等のフレームデータ系ツールでは答えられない「仕組み・概念」を、
    SuperCombo Wiki の解説文書 (72チャンク) から取得する。日本語/英語どちらの
    クエリも可。埋め込みは AWS Bedrock Titan V2 (Ollama 非依存)。

    Args:
        query:     検索クエリ (例: 'ドライブインパクトのアーマー',
                   'perfect parry timing', 'バーンアウトの効果')。
        count:     返す最大チャンク数 (デフォルト 3)。
        threshold: ベクトル検索のコサイン類似度の下限 (0〜1, デフォルト 0.4)。

    Returns:
        dict: ``results`` (page/heading/content/similarity のリスト) と、
              人間/LLM が読みやすい ``summary``。該当なしは results=[]。
    """
    from sf6_engine.rag_builder import _JP_TO_EN_CONCEPT, _keyword_search
    from sf6_engine.bedrock_embed import embed_text

    results: list[dict] = []
    seen: set[str] = set()

    # 1. キーワード検索 (JP→EN ILIKE, Bedrock 不要)
    for r in _keyword_search(query, count):
        if r["id"] not in seen:
            results.append(r)
            seen.add(r["id"])

    # 2. ベクトル検索 (Titan 埋め込み + search_docs_titan RPC)
    vector_error: str | None = None
    try:
        en_terms: list[str] = []
        for jp, en_list in _JP_TO_EN_CONCEPT.items():
            if jp in query:
                en_terms.extend(en_list)
        embed_q = " ".join(en_terms) if en_terms else query

        emb = embed_text(embed_q)
        sb = get_client()
        rpc = sb.rpc("search_docs_titan", {
            "query_embedding": emb,
            "match_threshold": threshold,
            "match_count": count,
        }).execute()
        for r in (rpc.data or []):
            if r["id"] not in seen:
                results.append(r)
                seen.add(r["id"])
    except Exception as e:  # ベクトル検索が落ちてもキーワード結果は返す
        vector_error = f"{type(e).__name__}: {str(e)[:160]}"

    # similarity 降順 → 上位 count 件
    results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    results = results[:count]

    out_results = [
        {
            "id": r["id"],
            "page": r.get("page"),
            "heading_h2": r.get("heading_h2"),
            "heading_h3": r.get("heading_h3"),
            "similarity": round(r.get("similarity", 0), 3),
            "content": (r.get("content") or "")[:800],
        }
        for r in results
    ]

    # summary (人間/LLM 向けの読みやすいテキスト)
    if out_results:
        blocks = []
        for r in out_results:
            head = f"【{r['page']} / {r['heading_h2']}"
            if r["heading_h3"]:
                head += f" / {r['heading_h3']}"
            head += f"】(類似度 {r['similarity']:.2f})"
            blocks.append(f"{head}\n{r['content']}")
        summary = "\n\n".join(blocks)
    else:
        summary = f"'{query}' に該当するシステム文書は見つかりませんでした。"

    out: dict = {
        "query": query,
        "count": len(out_results),
        "results": out_results,
        "summary": summary,
    }
    if vector_error:
        out["vector_search_error"] = vector_error
    return out


# ============================================================
# ツール: get_patch_status
# ============================================================

@mcp.tool()
def get_patch_status() -> dict:
    """SF6 の最新パッチ検知状況を返す (Layer 1 が CAPCOM 公式から自動検知した情報)。

    フレームデータが「いつ時点のものか」をユーザーに示すために使う。

    Returns:
        dict: ``latest_patch`` (CAPCOM 更新日・検知日時・パッチノートURL・概要) と、
              直近のスクレイプ実行情報 ``last_scrape`` (あれば)。
    """
    sb = get_client()

    patch = (
        sb.table("patches")
        .select("capcom_updated_date,detected_at,notes_url,summary")
        .order("capcom_updated_date", desc=True)
        .limit(1)
        .execute()
    )
    latest = patch.data[0] if patch.data else None

    last_scrape = None
    try:
        sr = (
            sb.table("scrape_runs").select("*")
            .order("id", desc=True).limit(1).execute()
        )
        last_scrape = sr.data[0] if sr.data else None
    except Exception:
        pass  # scrape_runs が無い/読めない場合はスキップ

    if latest:
        summary = (
            f"最新パッチ: CAPCOM 更新日 {latest.get('capcom_updated_date')}"
            f" (検知 {latest.get('detected_at')})。"
            f"フレームデータはこの時点のものです。"
        )
    else:
        summary = "パッチ検知履歴がありません。"

    return {
        "latest_patch": latest,
        "last_scrape": last_scrape,
        "summary": summary,
    }


# ============================================================
# ツール: register_move_alias (学習エイリアス登録)
# ============================================================

# alias / SC name の強度prefix (ファミリー正規化用)
_ALIAS_STRENGTH_RE = re.compile(r'^(弱|中|強|OD|オーバードライブ)\s*')
_SC_NAME_STRENGTH_RE = re.compile(r'^(LP|MP|HP|LK|MK|HK|OD|EX)\s+')
_ALIAS_COLLECTION_RE = re.compile(
    r"技(?:の中|のうち|一覧)|(?:有利|不利|五分).{0,8}技|全(?:部|て)|"
    r"すべて|プラスフレーム|マイナスフレーム",
    re.IGNORECASE,
)


@mcp.tool()
def register_move_alias(character: str, alias: str, move_input: str) -> dict:
    """技名の略称・通称と実際の技 (コマンド) の対応を学習登録する。

    ユーザーが「エドの弱フリッカー」のように未知の略称で質問し、コマンド
    (例: 236LK) を教えてくれた場合に呼び出す。登録後は lookup_move 等が
    その略称を全強度 (弱/中/強/OD) で解決できるようになる。

    Args:
        character:  capcom slug (例: 'ed', 'sagat')。
        alias:      ユーザーが使った技名・略称 (例: '弱フリッカー', 'アパカ')。
                    強度prefix (弱/中/強/OD) は自動で除去してファミリー名で保存する。
        move_input: ユーザーが教えたコマンド (例: '236LK', '623HP')。

    Returns:
        dict: 登録結果。``resolved_move`` (SC技名) / ``alias_family`` (保存した略称) /
              ``registered`` (bool)。move_input が実在しない場合は ``error``。
    """
    if _ALIAS_COLLECTION_RE.search(alias or ""):
        return {
            "error": "集合検索の条件は技名の別名として登録できません。",
            "registered": False,
        }

    sc_chara = _resolve_sc_chara(character)
    if not sc_chara:
        return {"error": f"キャラクター '{character}' が見つかりません。"}

    # 1. コマンドの実在検証 (いたずら・誤入力対策)
    row = _sc_input_lookup(character, move_input)
    if not row:
        return {
            "error": f"{sc_chara} にコマンド '{move_input}' の技が見つかりません。"
                     f"list_moves で確認してください。",
            "registered": False,
        }

    # 2. 強度prefix を除去してファミリー単位で正規化
    #    (弱フリッカー→フリッカー / LP Psycho Flicker→Psycho Flicker)
    alias_family = _ALIAS_STRENGTH_RE.sub('', alias.strip()).strip()
    sc_name = row.get("name") or ""
    sc_name_family = _SC_NAME_STRENGTH_RE.sub('', sc_name).strip()
    if not alias_family or not sc_name_family:
        return {"error": "alias または技名が空です。", "registered": False}

    # 3. move_aliases へ UPSERT (service key 必須)
    try:
        from sf6_engine.db import get_write_client
        wb = get_write_client()
        wb.table("move_aliases").upsert(
            {
                "sc_chara": sc_chara,
                "alias": alias_family,
                "sc_name_family": sc_name_family,
                "sc_input": row.get("input"),
                "source": "mcp",
            },
            on_conflict="sc_chara,alias",
        ).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("move_aliases upsert failed: %s", e)
        return {
            "error": f"登録に失敗しました: {type(e).__name__}",
            "registered": False,
        }

    return {
        "registered": True,
        "character": sc_chara,
        "alias_family": alias_family,
        "resolved_move": sc_name,
        "resolved_input": row.get("input"),
        "note": f"『{alias_family}』を {sc_chara} の {sc_name_family} として学習しました。"
                f"今後は弱/中/強/OD いずれの強度でも解決できます。",
    }


# ============================================================
# エントリポイント
# ============================================================

def main() -> None:
    """MCP サーバを起動する。

    SF6_MCP_TRANSPORT 環境変数で transport を切り替える:
      (未設定) / 'stdio'         : ローカル stdio (Claude Desktop 等)
      'streamable-http'          : リモート HTTP (ステップ4で AWS Lambda 化)
    """
    transport = os.getenv("SF6_MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
