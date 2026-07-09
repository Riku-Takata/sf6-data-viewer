"""全30キャラクター網羅テスト

DBから実フレームデータを取得してテストケースを自動生成し、
全キャラクターのフレームデータ照会精度を検証する。

テスト種別 (各キャラ最大2問):
  ① 代表技の発生F照会 (lookup_move)
  ② 代表技のガード時フレーム照会 (lookup_move / punish_check)

使い方:
  cd streetfighter6-engine
  PYTHONPATH=src python tests/char_coverage_test.py            # 全30キャラ 2問ずつ
  PYTHONPATH=src python tests/char_coverage_test.py --fast     # 発生Fのみ (30問)
  PYTHONPATH=src python tests/char_coverage_test.py --chars sagat ken ryu
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field


# 代表技の優先順位 (後ろほど低優先)
_PRIORITY_INPUTS = [
    '5HP', '2HP', '5HK', '2HK', '5MK', '5MP', '2MK', '2LP',
]


@dataclass
class CharResult:
    char_slug: str
    ja_name: str
    move_input: str
    startup_expected: str
    block_expected: str

    startup_result: str = "?"
    punish_result: str = "?"
    startup_answer: str = ""
    punish_answer: str = ""
    startup_intent: str = ""
    punish_intent: str = ""
    elapsed_startup: float = 0.0
    elapsed_punish: float = 0.0
    startup_reason: str = ""
    punish_reason: str = ""


def _extract_num(s) -> str | None:
    """'-5' → '-5', '7~10' → '7', 'KD' → None"""
    m = re.match(r'^(-?\d+)', str(s or '').strip())
    return m.group(1) if m else None


def build_cases() -> list[CharResult]:
    """DB からキャラリストと代表技を取得してテスト対象を生成。"""
    sys.path.insert(0, 'src')
    from sf6_engine.db import get_client
    sb = get_client()

    chars = sb.table('char_slug_map').select(
        'capcom_slug,display_name_ja,sc_chara'
    ).order('display_name_ja').execute().data

    results: list[CharResult] = []
    for char in chars:
        sc_chara = char.get('sc_chara') or ''
        ja_name  = char.get('display_name_ja') or char['capcom_slug']
        slug     = char['capcom_slug']

        if not sc_chara:
            continue

        # 全通常技を一括取得
        rows = sb.table('sc_moves').select(
            'input,startup,block_adv'
        ).eq('chara', sc_chara).execute().data
        move_map = {r['input']: r for r in rows}

        # 代表技を優先順で選択
        chosen: tuple[str, str, str] | None = None
        for inp in _PRIORITY_INPUTS:
            row = move_map.get(inp)
            if not row:
                continue
            st  = _extract_num(row.get('startup', ''))
            blk = _extract_num(row.get('block_adv', ''))
            if st:
                chosen = (inp, st, blk or '')
                break

        if not chosen:
            # フォールバック: 標準ナンバーパッド記法の技に限定
            # 例: 5HP, 2MK, j.HP など (4LPLK や 6HPHK は除外)
            _std_normal = re.compile(r'^[1-9][LMH][PK]$|^j\.[LMH][PK]$')
            for row in sorted(rows, key=lambda r: r['input']):
                if not _std_normal.match(row['input']):
                    continue
                st = _extract_num(row.get('startup', ''))
                if st:
                    blk = _extract_num(row.get('block_adv', ''))
                    chosen = (row['input'], st, blk or '')
                    break

        if chosen:
            inp, st, blk = chosen
            results.append(CharResult(
                char_slug=slug,
                ja_name=ja_name,
                move_input=inp,
                startup_expected=st,
                block_expected=blk,
            ))

    return results


def _check_answer(answer: str, expected: str) -> bool:
    """answer に expected の数値が含まれるか検証。
    例: expected='-5' → '-5', '-5F', '5F不利' などにマッチ"""
    ans = answer.lower()
    ev  = expected.lstrip('-+')  # 数値部分
    sign = '-' if expected.startswith('-') else '+'
    return (
        expected in ans         # "-5" そのまま
        or f'{expected}f' in ans  # "-5f"
        or f'{ev}f' in ans      # "5f" (符号なし)
        or f'{sign}{ev}' in ans   # "+5" / "-5"
    )


async def run_tests(cases: list[CharResult], fast: bool = False) -> None:
    from sf6_engine.factory import create_provider
    from sf6_engine.intent_parser import parse_intent
    from sf6_engine.rag_builder import build_context, generate_answer

    provider = create_provider()
    if not await provider.is_available():
        print("❌ Ollama が起動していません。`ollama serve` を実行してください。")
        sys.exit(1)

    model = getattr(provider, 'model', '?')
    mode_str = '発生F のみ' if fast else '発生F + ガード時'
    print(f"=== 全キャラ網羅テスト ({mode_str} | モデル: {model}) ===")
    print(f"対象: {len(cases)}キャラ  各{'1' if fast else '2'}問\n")

    for case in cases:
        print(f"[{case.char_slug}] {case.ja_name} / {case.move_input}", flush=True)

        # ① 発生F照会
        q1 = f"{case.ja_name}の{case.move_input}の発生は?"
        t0 = time.monotonic()
        try:
            intent = await parse_intent(q1, provider)
            ctx    = await build_context(intent, provider)
            ans    = await generate_answer(q1, ctx, provider)
            case.elapsed_startup = time.monotonic() - t0
            case.startup_answer  = ans
            case.startup_intent  = intent.get('intent_type', '?')
            passed = _check_answer(ans, case.startup_expected)
            case.startup_result = '✅' if passed else '❌'
            if not passed:
                case.startup_reason = f"期待値 {case.startup_expected} が応答に含まれない"
        except Exception as e:
            case.elapsed_startup = time.monotonic() - t0
            case.startup_result  = '❌'
            case.startup_reason  = str(e)

        print(
            f"  発生: {case.startup_result}  "
            f"({case.elapsed_startup:.1f}s | {case.startup_intent}) "
            f"expect={case.startup_expected}",
            flush=True,
        )
        ans_preview = case.startup_answer[:80].replace('\n', ' ')
        if ans_preview:
            print(f"    → {ans_preview}", flush=True)
        if case.startup_reason:
            print(f"    ! {case.startup_reason}", flush=True)

        # ② ガード時フレーム (fast モード時はスキップ)
        if not fast and case.block_expected:
            q2 = f"{case.ja_name}の{case.move_input}をガードしたら何F?"
            t0 = time.monotonic()
            try:
                intent = await parse_intent(q2, provider)
                ctx    = await build_context(intent, provider)
                ans    = await generate_answer(q2, ctx, provider)
                case.elapsed_punish = time.monotonic() - t0
                case.punish_answer  = ans
                case.punish_intent  = intent.get('intent_type', '?')
                passed = _check_answer(ans, case.block_expected)
                case.punish_result = '✅' if passed else '❌'
                if not passed:
                    case.punish_reason = f"期待値 {case.block_expected} が応答に含まれない"
            except Exception as e:
                case.elapsed_punish = time.monotonic() - t0
                case.punish_result  = '❌'
                case.punish_reason  = str(e)

            print(
                f"  ガード: {case.punish_result}  "
                f"({case.elapsed_punish:.1f}s | {case.punish_intent}) "
                f"expect={case.block_expected}",
                flush=True,
            )
            ans_preview2 = case.punish_answer[:80].replace('\n', ' ')
            if ans_preview2:
                print(f"    → {ans_preview2}", flush=True)
            if case.punish_reason:
                print(f"    ! {case.punish_reason}", flush=True)

        print(flush=True)

    _print_summary(cases, fast)


def _print_summary(cases: list[CharResult], fast: bool) -> None:
    st_ok  = sum(1 for c in cases if c.startup_result == '✅')
    st_ng  = sum(1 for c in cases if c.startup_result == '❌')
    pu_ok  = sum(1 for c in cases if c.punish_result == '✅')
    pu_ng  = sum(1 for c in cases if c.punish_result == '❌')
    pu_skip = sum(1 for c in cases if not c.block_expected)

    total = len(cases)
    avg_t = (
        sum(c.elapsed_startup + c.elapsed_punish for c in cases) / total
        if total else 0
    )

    print('=' * 65)
    print('=== 全キャラ網羅テスト 結果サマリー ===')
    print('=' * 65)
    print(f"キャラ数: {total}")
    print(f"発生F照会:  {st_ok:2}✅ {st_ng:2}❌  合格率 {st_ok * 100 // total if total else 0}%")
    if not fast:
        pu_total = total - pu_skip
        pct = pu_ok * 100 // pu_total if pu_total else 0
        print(f"ガード確定: {pu_ok:2}✅ {pu_ng:2}❌  合格率 {pct}%  (データなし{pu_skip}体)")
    total_q  = total + (total - pu_skip if not fast else 0)
    total_ok = st_ok + (pu_ok if not fast else 0)
    print(f"総合:  {total_ok}✅ / {total_q}問  合格率 {total_ok * 100 // total_q if total_q else 0}%")
    print(f"平均応答時間: {avg_t:.1f}秒/キャラ")

    # 失敗キャラ一覧
    failed = [
        c for c in cases
        if c.startup_result == '❌' or c.punish_result == '❌'
    ]
    if failed:
        print('\n【失敗キャラ一覧】')
        for c in failed:
            parts = []
            if c.startup_result == '❌':
                parts.append(f"発生(期待:{c.startup_expected})")
            if c.punish_result == '❌':
                parts.append(f"ガード(期待:{c.block_expected})")
            print(f"  {c.ja_name:10} ({c.char_slug}): {', '.join(parts)}")
    else:
        print('\n🎉 全キャラ合格!')

    # 全体スコアバー
    bar_ok  = '■' * st_ok
    bar_ng  = '□' * st_ng
    print(f"\n発生F [{bar_ok}{bar_ng}] {st_ok}/{total}")

    # JSON 保存
    results = [
        {
            'char_slug':        c.char_slug,
            'ja_name':          c.ja_name,
            'move_input':       c.move_input,
            'startup_expected': c.startup_expected,
            'startup_result':   c.startup_result,
            'startup_reason':   c.startup_reason,
            'block_expected':   c.block_expected,
            'punish_result':    c.punish_result,
            'punish_reason':    c.punish_reason,
        }
        for c in cases
    ]
    out = 'tests/char_coverage_results.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f'\n結果を {out} に保存しました。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='全キャラクター網羅テスト')
    parser.add_argument(
        '--chars', nargs='+', metavar='SLUG',
        help='テスト対象キャラスラグ (例: sagat ken ryu)',
    )
    parser.add_argument(
        '--fast', action='store_true',
        help='発生Fのみ (各1問、約15分)',
    )
    args = parser.parse_args()

    sys.path.insert(0, 'src')

    print('DB からテストデータを取得中...', flush=True)
    cases = build_cases()
    print(f'取得完了: {len(cases)} キャラ\n', flush=True)

    if args.chars:
        cases = [c for c in cases if c.char_slug in args.chars]
        if not cases:
            print(f'指定キャラが見つかりません: {args.chars}')
            sys.exit(1)
        print(f'フィルタ後: {len(cases)} キャラ\n', flush=True)

    asyncio.run(run_tests(cases, fast=args.fast))
