"""回帰評価セット: 正解付き質問で E2E パイプラインを機械採点する。

使い方 (Ollama 起動が前提):
    PYTHONPATH=src ./.venv312/bin/python tests/regression_eval.py

各ケースは実データ (Supabase) から確定させた正解値を持つ。
期待値の根拠は各ケースの note を参照。データがパッチで変わったら更新すること。
"""
from __future__ import annotations

import asyncio
import re
import sys

sys.path.insert(0, 'src')

from sf6_engine.factory import create_provider  # noqa: E402
from sf6_engine.intent_parser import parse_intent  # noqa: E402
from sf6_engine.rag_builder import build_context, generate_answer  # noqa: E402


# expect_any: いずれか1つが回答にマッチすれば合格 (正規表現)
# expect_all: すべてマッチが必要 / forbid: マッチしたら不合格
CASES: list[dict] = [
    {
        "q": "サガットの立ち中Pをガードさせた時は何フレーム有利？",
        "expect_any": [r"\+2F"],
        "forbid": [r"(出した側|攻撃側)[^。\n]{0,14}-2F"],
        "note": "5MP blk=+2 (攻撃側視点)。視点反転の回帰検知",
    },
    {
        "q": "サガットの立ち中Pをガードした時は何フレーム有利？",
        "expect_any": [r"-2F"],
        "note": "防御側視点 → -2F",
    },
    {
        "q": "エドの236LKをためた時の性能は？",
        "expect_any": [r"26F", r"\+4F"],
        "note": "236[LK] Hold: 発生26F / ガード+4F。バリアント選択の回帰検知",
    },
    {
        "q": "エドの236LKの性能は？",
        "expect_any": [r"11F", r"-6F"],
        "expect_all": [r"ため|ホールド"],
        "note": "通常版 11F/-6F + ため版の存在に言及 (ルール16)",
    },
    {
        "q": "豪鬼の波動拳を最大までためた時の性能は？",
        "expect_any": [r"56F", r"\+20F"],
        "note": "236[P] Lv.3: 発生56 / ガード+20。強度→レベルのブリッジ検知",
    },
    {
        "q": "リリーがウィンドクラッド状態で出す623MPの発生は？",
        "expect_any": [r"(?<![\d+\-])6F", r"-30F"],
        "note": "W.623MP: 発生6F / ガード-30F",
    },
    {
        "q": "ケンの623HPは無敵ある？",
        "expect_any": [r"1-9", r"1〜9"],
        "note": "Shoryuken HP: invuln '1-9 Air'。input直指定必殺技フォールバック検知",
    },
    {
        "q": "サガットの2HKの発生は？",
        "expect_any": [r"11F"],
        "note": "2HK startup=11",
    },
    {
        "q": "サガットの2HKをガードしたら反撃できる？",
        "expect_all": [r"反撃"],
        "expect_any": [r"12F", r"-12F"],
        "note": "blk=-12 → 発生12F以内で確定反撃",
    },
    {
        "q": "バーンアウトになるとどうなる？",
        "expect_any": [r"チップ|削り|ドライブ|スタン"],
        "note": "概念質問 (doc_chunks 検索) の疎通確認",
    },
    {
        "q": "エドの6KKの性能は？",
        "expect_any": [r"Kill\s?Rush|キルラッシュ|5/6KK"],
        "note": "'6KK' → '5/6KK' の正規化キー再検索検知",
    },
    {
        "q": "ケンの立ち大Kの発生は？",
        "expect_any": [r"12F|発生[:：]?\s*12"],
        "note": "5HK startup=12。'大K'略称 + 質問フィールド判定 (発生に答える) の検知",
    },
    {
        "q": "ケンの前大Kの発生は？",
        "expect_any": [r"見つかり|ありません|存在しません|データ"],
        "forbid": [r"[+-]\d+F\s*有利"],
        "note": "Ken に 6HK は存在しない → 幻覚せず正直に「データなし」と答える",
    },
]


async def run() -> int:
    provider = create_provider()
    passed = 0
    failures: list[str] = []
    for i, case in enumerate(CASES, 1):
        q = case["q"]
        try:
            intent = await parse_intent(q, provider)
            ctx = await build_context(intent, provider)
            ans = await generate_answer(q, ctx, provider)
        except Exception as e:  # noqa: BLE001
            failures.append(f"[{i}] {q}\n    ERROR: {e}")
            print(f"✗ [{i}] {q} — ERROR: {e}")
            continue
        problems = []
        exp_any = case.get("expect_any")
        if exp_any and not any(re.search(p, ans) for p in exp_any):
            problems.append(f"expect_any 不成立: {exp_any}")
        for p in case.get("expect_all", []):
            if not re.search(p, ans):
                problems.append(f"expect_all 不成立: {p}")
        for p in case.get("forbid", []):
            if re.search(p, ans):
                problems.append(f"forbid 検出: {p}")
        if problems:
            failures.append(f"[{i}] {q}\n    {problems}\n    A: {ans[:200]}")
            print(f"✗ [{i}] {q}\n    {problems}\n    A: {ans[:200]}")
        else:
            passed += 1
            print(f"✓ [{i}] {q}")
    print(f"\n{'='*60}\n合格 {passed}/{len(CASES)}")
    if hasattr(provider, "usage"):
        from sf6_engine.token_usage import format_usage
        print(format_usage(provider.usage.totals()))
    if failures:
        print("\n失敗詳細は上記参照")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
