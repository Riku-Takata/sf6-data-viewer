"""SF6 Discord Bot (常駐, discord.py)。

フロー:
  Discord メッセージ
    → gemma4 (Ollama) で intent_parser 構造化
    → map_intent で MCP ツール選択
    → AWS MCP サーバ経由でツール実行
    → gemma4 (generate_answer) で日本語回答生成
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
from sf6_engine.intent_parser import parse_intent  # noqa: E402
from sf6_engine.rag_builder import generate_answer  # noqa: E402

from discord_bot.mcp_router import call_tool, map_intent, result_to_context  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sf6_bot")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
COMMAND_PREFIX = os.environ.get("SF6_BOT_PREFIX", "!sf6")
DISCORD_MAX = 1900  # Discord の 2000 字制限に対する安全マージン

# --- 聞き返し学習ループ (技名未解決 → コマンドを聞いて move_aliases に登録) ---
PENDING_TTL = 300  # 聞き返しの有効期限 (秒)
# (channel_id, author_id) → {question, chara, slug, alias, expires}
_pending: dict[tuple[int, int], dict] = {}

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


# 「特定の技」を対象とするツール (未解決時に聞き返しループの対象になる)
_MOVE_TOOLS = {"lookup_move", "check_punish", "compute_setplay", "analyze_combo"}


def _is_unresolved(result: dict | None) -> bool:
    """MCP 結果が「技を解決できなかった」ことを示すか。"""
    if result is None:
        return False  # 通信エラー等は学習対象にしない
    return result.get("found") is False or bool(result.get("error"))


async def handle_question(question: str, pending_key: tuple[int, int] | None = None) -> str:
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
    logger.info("intent: %s", intent)

    # 2. MCP ツール選択
    calls = map_intent(intent)
    if not calls:
        return (
            "うまく解釈できませんでした。キャラ名と技を含めてみてください。\n"
            "例: `サガットの2HKの発生は?` / `ドライブインパクトって何?`"
        )

    # 3. AWS MCP サーバでツール実行
    contexts: list[str] = []
    move_unresolved = False
    for tool, args in calls:
        try:
            result = await call_tool(tool, args)
            contexts.append(result_to_context(tool, args, result))
            if tool in _MOVE_TOOLS and _is_unresolved(result):
                move_unresolved = True
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP call failed: %s %s", tool, args)
            contexts.append(f"[{tool}] 実行エラー: {type(e).__name__}")
    context = "\n\n".join(contexts)

    # 3.5. 技名未解決 + キャラ特定済み → コマンドを聞き返して学習につなげる
    chara = intent.get("chara")
    move_ident = intent.get("move_name") or intent.get("input")
    if move_unresolved and pending_key is not None and chara and move_ident:
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

    # 4. gemma4 で最終回答生成
    answer = await generate_answer(question, context, _provider)

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

    question = _extract_question(message)
    if question is None:
        return

    async with message.channel.typing():
        try:
            answer = await handle_question(question, pending_key=key)
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
