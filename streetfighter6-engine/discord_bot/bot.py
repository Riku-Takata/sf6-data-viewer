"""SF6 Discord Bot (常駐, discord.py)。

フロー:
  Discord メッセージ
    → intent_parser で構造化 (定型連携は決定論、その他は gemma4)
    → map_intent で MCP ツール選択
    → AWS MCP サーバ経由でツール実行
    → 連携解析は決定論 summary、それ以外は generate_answer
    → Discord に返信

起動 (engine ルートから):
  PYTHONPATH=src python -m discord_bot.bot

依存: discord.py / mcp / sf6_engine (intent_parser, factory, generate_answer)。
必要な環境変数は discord_bot/.env (.env.example 参照)。
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import discord
from dotenv import load_dotenv

# discord_bot/.env (DISCORD_TOKEN / SF6_MCP_URL / SF6_MCP_TOKEN / OLLAMA_*) を読む
load_dotenv(Path(__file__).resolve().parent / ".env")

from sf6_engine.factory import create_provider  # noqa: E402
from sf6_engine.conversation_knowledge import derive_subject_key, is_save_confirmation  # noqa: E402
from sf6_engine.conversation_service import ConversationKnowledgeService  # noqa: E402
from sf6_engine.intent_parser import parse_intent  # noqa: E402
from sf6_engine.rag_builder import generate_answer  # noqa: E402

from discord_bot.mcp_router import (  # noqa: E402
    call_tool,
    is_alias_learnable_result,
    map_intent,
    result_to_context,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sf6_bot")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
COMMAND_PREFIX = os.environ.get("SF6_BOT_PREFIX", "!sf6")
DISCORD_MAX = 1900  # Discord の 2000 字制限に対する安全マージン
# Legacy alias registration validates against SuperCombo and immediately writes
# a global family alias.  ADR-026 replaces it with a reviewed canonical alias
# workflow, so it is opt-in only during migration.
ENABLE_LEGACY_SC_ALIAS_LEARNING = os.environ.get(
    "SF6_ENABLE_LEGACY_SC_ALIAS_LEARNING", "0"
) == "1"

# --- 聞き返し学習ループ (技名未解決 → コマンドを聞いて move_aliases に登録) ---
PENDING_TTL = 300  # 聞き返しの有効期限 (秒)
# (channel_id, author_id) → {question, chara, slug, alias, expires}
_pending: dict[tuple[int, int], dict] = {}

# --- 会話コンテキスト / 本人限定戦術メモ (ADR-026) ---
# 永続保存は SQL migration + SF6_KNOWLEDGE_STORE=supabase + subject HMAC を
# 明示設定した時だけ有効。既定では短期会話文脈のみを扱う。
_knowledge_service = ConversationKnowledgeService()

# ユーザーの返信からコマンド表記を抽出 (236LK / 623HP / j.214KK / [4]6HP / 22P 等)
_CMD_TOKEN = re.compile(
    r'(?<![A-Za-z0-9])'
    r'((?:j\.)?\[?\d[\d\[\]\]]*(?:LP|MP|HP|LK|MK|HK|PP|KK|P|K)+(?:~\S+)?)',
    re.IGNORECASE,
)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Ollama プロバイダはプロセス内で再利用
_provider = create_provider()


async def handle_question(
    question: str,
    pending_key: tuple[int, int] | None = None,
    conversation_id: str | None = None,
    subject_key: str | None = None,
) -> str:
    """1 つの質問を intent 解析 → MCP 実行 → 回答生成まで処理する。

    技名が解決できず、キャラは特定できている場合は「コマンドを教えて」と
    聞き返し、pending_key に保留状態を登録する (聞き返し学習ループ)。
    """
    question = question.strip()
    if not question:
        return "質問を入力してください。例: `!sf6 サガットの2HKガードして反撃できる?`"

    usage_before = _provider.usage.totals() if hasattr(_provider, "usage") else None

    # 1. gemma4 で intent 構造化
    intent = await parse_intent(question, _provider)
    knowledge_turn = None
    if conversation_id and subject_key:
        knowledge_turn = _knowledge_service.process_turn(
            text=question,
            intent=intent,
            conversation_id=conversation_id,
            subject_key=subject_key,
        )
        intent = knowledge_turn.analysis.resolved_intent
    logger.info("intent: %s", intent)

    # 2. MCP ツール選択
    calls = map_intent(intent)
    if not calls:
        answer = (
            "うまく解釈できませんでした。キャラ名と技を含めてみてください。\n"
            "例: `サガットの2HKの発生は?` / `ドライブインパクトって何?`"
        )
        if knowledge_turn and knowledge_turn.save_message:
            answer += "\n\n" + knowledge_turn.save_message
        return answer[:DISCORD_MAX]

    # 3. AWS MCP サーバでツール実行
    contexts: list[str] = []
    move_unresolved = False
    deterministic_answer: str | None = None
    for tool, args in calls:
        try:
            result = await call_tool(tool, args)
            contexts.append(result_to_context(tool, args, result))
            if tool in {"analyze_sequence", "analyze_sequence_family", "query_moves"} and result:
                if result.get("summary"):
                    deterministic_answer = str(result["summary"])
                elif result.get("message"):
                    deterministic_answer = str(result["message"])
            if is_alias_learnable_result(tool, result):
                move_unresolved = True
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP call failed: %s %s", tool, args)
            contexts.append(f"[{tool}] 実行エラー: {type(e).__name__}")
    context = "\n\n".join(contexts)

    # 3.5. 技名未解決 + キャラ特定済み → コマンドを聞き返して学習につなげる
    chara = intent.get("chara")
    move_ident = intent.get("move_name") or intent.get("input")
    if (
        move_unresolved
        and ENABLE_LEGACY_SC_ALIAS_LEARNING
        and pending_key is not None
        and chara
        and move_ident
    ):
        from discord_bot.mcp_router import _slug
        _pending[pending_key] = {
            "question": question,
            "chara": chara,
            "slug": _slug(chara),
            "alias": move_ident,
            "expires": time.time() + PENDING_TTL,
        }
        logger.info("pending alias question: %s / %s", chara, move_ident)
        return (
            f"キャラクターは {chara} ですね。技名（{move_ident}）のデータが見つかりませんでした。\n"
            f"その技のコマンドを教えてください（例: `236LK`、`623HP`）。"
            f"教えていただければ今後この呼び方で答えられるようになります。"
        )
    if move_unresolved and not ENABLE_LEGACY_SC_ALIAS_LEARNING:
        return (
            "技名を一意に解決できませんでした。現在はSuperCombo依存の即時グローバル別名登録を"
            "無効化しています。正式名またはコマンドを指定してください。"
        )

    # 4. 時系列と観測値を返す連携解析は、LLM の言い換えで
    #    数値や確度が変わらないよう決定論 summary をそのまま返す。
    if deterministic_answer is not None:
        answer = deterministic_answer
    else:
        answer = await generate_answer(question, context, _provider)

    # Private/shared tactical text is never fed back into the core numeric
    # answer prompt.  It is displayed separately with provenance so an
    # unverified user memo cannot overwrite CAPCOM-derived facts.
    if knowledge_turn and knowledge_turn.private_context:
        answer += "\n\n" + knowledge_turn.private_context
    if knowledge_turn and knowledge_turn.save_message:
        answer += "\n\n" + knowledge_turn.save_message

    # 5. この質問での LLM トークン消費をログ (コスト単価は env で設定)
    if usage_before is not None:
        from sf6_engine.token_usage import format_usage, usage_diff
        logger.info(format_usage(usage_diff(usage_before, _provider.usage.totals())))

    return answer[:DISCORD_MAX]


async def handle_alias_reply(pend: dict, command: str) -> str:
    """聞き返しへの返信 (コマンド) を検証・登録し、元の質問に即答する。"""
    try:
        res = await call_tool("register_move_alias", {
            "character": pend["slug"],
            "alias": pend["alias"],
            "move_input": command,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("register_move_alias failed")
        return f"エイリアス登録でエラーが発生しました: {type(e).__name__}"

    if not res or not res.get("registered"):
        msg = (res or {}).get("error", "不明なエラー")
        return (
            f"`{command}` を確認できませんでした: {msg}\n"
            f"もう一度コマンドだけ返信してください（例: `236LK`）。"
        )

    # 復唱 (検証済みの解決結果) + 元質問への回答
    confirm = (
        f"✅ `{res.get('resolved_input')}` = **{res.get('resolved_move')}** ですね。"
        f"『{res.get('alias_family')}』として学習しました。\n"
    )
    answer = await handle_question(pend["question"])
    return (confirm + "\n" + answer)[:DISCORD_MAX]


def _extract_question(message: discord.Message) -> str | None:
    """メッセージがトリガー条件を満たすか判定し、質問本文を返す。

    トリガー: bot へのメンション、またはプレフィックス (既定 !sf6) で始まる。
    """
    content = message.content or ""
    if client.user and client.user in message.mentions:
        for m in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
            content = content.replace(m, "")
        return content.strip()
    if content.startswith(COMMAND_PREFIX):
        return content[len(COMMAND_PREFIX):].strip()
    return None


@client.event
async def on_ready() -> None:
    logger.info("logged in as %s (MCP=%s)", client.user, os.environ.get("SF6_MCP_URL"))
    # 参加サーバーの可視化 (0 なら招待されていない / チャンネルが見えない疑い)
    logger.info("guilds: %d", len(client.guilds))
    for g in client.guilds:
        me = g.me
        readable = [
            c.name for c in g.text_channels
            if me and c.permissions_for(me).read_messages
        ]
        logger.info("  - %s (id=%s) 閲覧可能チャンネル: %s", g.name, g.id, readable or "なし!")


@client.event
async def on_message(message: discord.Message) -> None:
    # 受信診断: content が空なら MESSAGE CONTENT INTENT 未許可の疑い
    logger.info(
        "on_message: author=%s channel=%s content=%r",
        message.author, getattr(message.channel, 'name', message.channel), message.content[:80],
    )
    if message.author == client.user or message.author.bot:
        return

    key = (message.channel.id, message.author.id)
    persistent_subject = derive_subject_key("discord", message.author.id)
    subject_key = persistent_subject or f"session:discord:{message.author.id}"
    persistent_conversation = derive_subject_key("discord-channel", message.channel.id)
    conversation_id = persistent_conversation or f"session:discord-channel:{message.channel.id}"

    # 聞き返しへの返信を優先処理 (プレフィックスなしのコマンド単体も受け付ける)
    pend = _pending.get(key)
    if pend is not None:
        if time.time() > pend["expires"]:
            _pending.pop(key, None)
        else:
            m = _CMD_TOKEN.search(message.content or "")
            if m:
                _pending.pop(key, None)
                async with message.channel.typing():
                    try:
                        answer = await handle_alias_reply(pend, m.group(1))
                    except Exception as e:  # noqa: BLE001
                        logger.exception("handle_alias_reply failed")
                        answer = f"エラーが発生しました: {type(e).__name__}"
                await message.reply(answer, mention_author=False)
                return

    # An explicit save confirmation is accepted without a command prefix, but
    # only for the same user and channel that created the pending candidate.
    if _knowledge_service.has_pending_save(
        conversation_id=conversation_id,
        subject_key=subject_key,
    ) and is_save_confirmation((message.content or "").strip()):
        async with message.channel.typing():
            confirmed = _knowledge_service.confirm_pending_save(
                text=(message.content or "").strip(),
                conversation_id=conversation_id,
                subject_key=subject_key,
            )
        await message.reply(confirmed.message, mention_author=False)
        return

    question = _extract_question(message)
    if question is None:
        return

    async with message.channel.typing():
        try:
            answer = await handle_question(
                question,
                pending_key=key,
                conversation_id=conversation_id,
                subject_key=subject_key,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("handle_question failed")
            answer = f"エラーが発生しました: {type(e).__name__}"
    await message.reply(answer, mention_author=False)


def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN が未設定です (discord_bot/.env を確認)")
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
