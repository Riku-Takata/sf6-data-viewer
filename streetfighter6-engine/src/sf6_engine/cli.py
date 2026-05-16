"""
Logic Engine の CLI エントリポイント.

使い方:
  python -m sf6_engine.cli ask "サガットの2HKの発生は?"
  python -m sf6_engine.cli lookup sagat "立ち弱P（タイガージャブ）"

例:
  python -m sf6_engine.cli ask "ドライブインパクトって何?"
  python -m sf6_engine.cli ask "ガイルの2HKガードして反撃できる?" -v
  python -m sf6_engine.cli lookup sagat "2HK"
"""
from __future__ import annotations

import asyncio
import json

import click

from sf6_engine.handlers.lookup import lookup_move


@click.group()
def cli():
    """SF6 Logic Engine CLI."""
    pass


@cli.command()
@click.argument("character")
@click.argument("move_name")
@click.option("--json-output", "json_output", is_flag=True,
              help="JSONで出力 (デフォルトは人間可読表示)")
def lookup(character: str, move_name: str, json_output: bool):
    """単一技の性能データを照会.

    CHARACTER: キャラのslug (例: sagat, ryu)
    MOVE_NAME: 技名 (正式名 or 部分一致)
    """
    result = lookup_move(character, move_name)

    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return

    # 人間可読表示
    if not result.found:
        click.echo(f"❌ 技が見つかりません: {character} / {move_name}")
        if result.message:
            click.echo(f"   {result.message}")
        if result.candidate_names:
            click.echo(f"\n   候補 ({len(result.candidate_names)}件):")
            for name in result.candidate_names:
                click.echo(f"     - {name}")
        return

    m = result.move
    click.echo(f"\n📘 {m.character} / {m.move_name} [{m.section}]")
    if result.message:
        click.echo(f"   ({result.message})")
    click.echo()

    # フレーム情報
    click.echo("  フレーム:")
    click.echo(f"    発生:   {_fmt(m.startup)} F  {_raw(m.raw_startup)}")
    active = f"{m.active_first}-{m.active_last}" if m.active_first else _fmt(None)
    click.echo(f"    持続:   {active} F  {_raw(m.raw_active)}")
    click.echo(f"    硬直:   {_fmt(m.recovery)} F  {_raw(m.raw_recovery)}")

    # 有利不利
    block_str = _fmt(m.on_block)
    if m.on_block_is_range:
        block_str += " (範囲)"
    click.echo(f"    ガード: {block_str} F  {_raw(m.raw_on_block)}")

    hit_str = "D (ダウン)" if m.on_hit_is_knockdown else _fmt(m.on_hit)
    if m.on_hit_is_range:
        hit_str += " (範囲)"
    click.echo(f"    ヒット: {hit_str}  {_raw(m.raw_on_hit)}")

    # ダメージ
    click.echo(f"\n  ダメージ: {_fmt(m.damage)}  {_raw(m.raw_damage)}")

    # キャンセル
    cancels = []
    if m.cancellable.chain: cancels.append("チェーン/通常キャンセル")
    if m.cancellable.sa1:   cancels.append("SA1")
    if m.cancellable.sa2:   cancels.append("SA2")
    if m.cancellable.sa3:   cancels.append("SA3")
    click.echo(f"  キャンセル: {', '.join(cancels) if cancels else 'なし'}  {_raw(m.raw_cancel)}")

    # 警告
    if m.has_conditional_note:
        click.echo()
        click.secho("  ⚠  このデータには ※ が付いています (条件付き).", fg="yellow")
        if m.raw_note:
            click.echo(f"     note: {m.raw_note}")

    click.echo()


def _fmt(v) -> str:
    """None/空を '---' に統一."""
    return str(v) if v is not None else "---"


def _raw(v) -> str:
    """生データを『(元値: xxx)』の形で返す. None/空なら空文字."""
    if v is None or v == "":
        return ""
    return f"(元値: {v})"


@cli.command()
@click.argument("question")
@click.option("--verbose", "-v", is_flag=True, help="Intent と参照データも表示する")
@click.option("--provider", default=None, help="LLM プロバイダー (ollama/gemini)")
def ask(question: str, verbose: bool, provider: str | None):
    """自然言語で SF6 の質問をする。

    例:\n
      python -m sf6_engine.cli ask "サガットの2HKの発生は?"\n
      python -m sf6_engine.cli ask "ドライブインパクトって何?" -v
    """
    asyncio.run(_ask(question, verbose, provider))


async def _ask(question: str, verbose: bool, provider_name: str | None) -> None:
    from sf6_engine.factory import create_provider
    from sf6_engine.intent_parser import parse_intent
    from sf6_engine.rag_builder import build_context, generate_answer

    provider = create_provider(provider_name)

    if not await provider.is_available():
        click.secho(
            "❌ Ollama が起動していません。`ollama serve` を実行してください。",
            fg="red",
        )
        return

    # Step 1: Intent 解析
    intent = await parse_intent(question, provider)
    if verbose:
        click.secho("\n[Intent]", fg="cyan", bold=True)
        click.echo(f"  {json.dumps(intent, ensure_ascii=False)}")

    # Step 2: RAG コンテキスト構築 (provider を渡してベクトル検索を有効化)
    context = await build_context(intent, provider)
    if verbose:
        click.secho("\n[参照データ]", fg="cyan", bold=True)
        for line in context.splitlines():
            click.echo(f"  {line}")

    # Step 3: 最終回答生成
    if verbose:
        click.secho("\n[回答]", fg="green", bold=True)
    answer = await generate_answer(question, context, provider)
    click.echo(answer)


if __name__ == "__main__":
    cli()
