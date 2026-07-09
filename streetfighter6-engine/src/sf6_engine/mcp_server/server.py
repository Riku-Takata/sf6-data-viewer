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
  - compute_setplay  : KD/ヒット後の起き攻め択計算
  - analyze_combo    : 始動技からの最大コンボ計算 (ビームサーチ)
  - list_moves       : キャラの技名一覧 (技名解決の補助)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from sf6_engine.db import get_client

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


def _sc_to_move(row: dict) -> dict:
    """sc_move_normalized の行を MCP の move レスポンス形に正規化する。"""
    return {
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
    }


# ============================================================
# ツール: lookup_move
# ============================================================

@mcp.tool()
def lookup_move(character: str, move_name: str) -> dict:
    """指定キャラの単一技のフレームデータ (発生・持続・硬直・ガード/ヒット時有利・
    ダメージ・キャンセル可否) を返す。

    Args:
        character: キャラの slug (例: 'sagat', 'ryu', 'ken')。
        move_name: 技名。正式名 (例: '立ち弱P（タイガージャブ）') または
                   部分一致 (例: 'タイガージャブ', '2HK') を受け付ける。

    Returns:
        dict: ``found`` が True なら ``move`` に詳細 (``move.source`` が
              'capcom' か 'supercombo')。曖昧な場合は ``candidate_names`` に
              候補技名が入る。技名が分からない場合は先に list_moves で確認すること。
    """
    from sf6_engine.handlers.lookup import lookup_move as _lookup

    result = _lookup(character, move_name)
    if result.found and result.move is not None:
        d = result.to_dict()
        if d.get("move"):
            d["move"]["source"] = "capcom"
            d["move"]["frame_perspective_note"] = _perspective_note(
                d["move"].get("on_block"), d["move"].get("on_hit"))
            # SC input (numpad) を付与 — compute_setplay / analyze_combo への連携用
            try:
                r = (
                    get_client().table("unified_moves").select("sc_input_key")
                    .eq("character_slug", character.lower())
                    .eq("move_name", d["move"].get("move_name"))
                    .limit(1).execute()
                )
                if r.data and r.data[0].get("sc_input_key"):
                    d["move"]["input"] = r.data[0]["sc_input_key"]
            except Exception as e:
                logger.warning(f"sc_input_key 付与に失敗 (継続): {e}")
        return d

    # CAPCOM 日本語名で解決できない場合 → SuperCombo の numpad input で再試行
    sc = _sc_input_lookup(character, move_name)
    if sc:
        return {
            "found": True,
            "character": character,
            "query_move_name": move_name,
            "move": _sc_to_move(sc),
            "message": "SuperCombo の input 一致で取得しました。",
        }

    # 技名解決フォールバック (special_move_map 日本語公式名 / 学習エイリアス / 英語名)
    sc = _sc_name_fallback(character, move_name)
    if sc:
        return {
            "found": True,
            "character": character,
            "query_move_name": move_name,
            "move": _sc_to_move(sc),
            "message": "技名解決 (special_move_map / エイリアス) で取得しました。",
        }

    return result.to_dict()


# ============================================================
# ツール: check_punish
# ============================================================

@mcp.tool()
def check_punish(character: str, move_name: str, punisher: str | None = None) -> dict:
    """ある技をガードした際に確定反撃が入るかを判定する。

    技のガード時硬直差 (block_adv) から「ガードした側の有利フレーム」を算出し、
    その発生F以内の技が確定反撃になる、という決定論的な判定を返す。

    Args:
        character: ガードされる技を持つキャラの slug。
        move_name: ガードされる技名 (部分一致可)。
        punisher:  反撃する側のキャラ slug (任意)。指定すると、そのキャラの
                   確定反撃候補 (発生F が punish window 以内の技) を列挙する。

    Returns:
        dict: ``punishable`` (確定反撃可否)、``block_adv`` (ガード時硬直差)、
              ``punish_window_f`` (反撃できる発生Fの上限)、``summary`` (説明文)、
              punisher 指定時は ``punisher_options`` (確定反撃候補リスト)。
    """
    from sf6_engine.handlers.lookup import lookup_move as _lookup

    # CAPCOM 日本語名 → SuperCombo numpad input の順で技を解決
    result = _lookup(character, move_name)
    if result.found and result.move is not None:
        resolved_name = result.move.move_name
        block_adv = result.move.on_block
        block_adv_is_range = result.move.on_block_is_range
    else:
        sc = _sc_input_lookup(character, move_name) or _sc_name_fallback(character, move_name)
        if sc is None:
            return {
                "found": False,
                "message": result.message,
                "candidate_names": result.candidate_names,
            }
        resolved_name = sc.get("name") or move_name
        block_adv = sc.get("block_adv_f")
        block_adv_is_range = False

    # 呼び出し時の識別子 (例: 2HK) と解決後の名前 (例: Tiger Kick) が異なる場合は
    # 「2HK（Tiger Kick）」と併記し、消費側 LLM が同一技だと分かるようにする。
    display_name = (
        resolved_name if resolved_name == move_name
        else f"{move_name}（{resolved_name}）"
    )

    if block_adv is None:
        return {
            "found": True,
            "character": character,
            "queried_move": move_name,
            "move_name": resolved_name,
            "block_adv": None,
            "punishable": None,
            "summary": f"{display_name} のガード時硬直差データがありません (判定不能)。",
        }

    window = -block_adv if block_adv < 0 else 0
    punishable = window > 0

    if punishable:
        summary = (
            f"{display_name} はガード時 {block_adv:+d}F。"
            f"ガードした側は +{window}F 有利 → 発生 {window}F 以内の技が確定反撃。"
        )
    else:
        summary = (
            f"{display_name} はガード時 {block_adv:+d}F。"
            f"ガードした側が不利のため確定反撃はできません。"
        )

    out: dict = {
        "found": True,
        "character": character,
        "queried_move": move_name,
        "move_name": resolved_name,
        "block_adv": block_adv,
        "block_adv_is_range": block_adv_is_range,
        "punish_window_f": window,
        "punishable": punishable,
        "summary": summary,
    }

    # punisher 指定時: 確定反撃候補を発生F昇順で列挙
    # unified_moves を使い SC input (numpad) も併記 → analyze_combo へ直接渡せる。
    # ドライブパリィ (発生1F) は打撃反撃ではないため除外。
    if punisher and punishable:
        sb = get_client()
        res = (
            sb.table("unified_moves")
            .select("move_name,sc_input_key,c_startup,c_on_block,section")
            .eq("character_slug", punisher.lower())
            .lte("c_startup", window)
            .not_.is_("c_startup", "null")
            .not_.ilike("move_name", "%パリィ%")
            .order("c_startup")
            .limit(8)
            .execute()
        )
        out["punisher"] = punisher
        out["punisher_options"] = [
            {
                "move_name": r["move_name"],
                "input": r.get("sc_input_key"),
                "startup": r.get("c_startup"),
                "on_block": r.get("c_on_block"),
                "section": r.get("section"),
            }
            for r in (res.data or [])
        ]

    return out


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
