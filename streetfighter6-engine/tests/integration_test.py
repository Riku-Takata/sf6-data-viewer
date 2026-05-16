"""M1 統合テスト: 想定20問での動作評価。

使い方:
  PYTHONPATH=src python tests/integration_test.py

評価基準:
  ✅ 合格  : 正確な数値 + 適切な文脈説明
  ⚠  要改善: 数値は正しいが文脈不足、または文脈はあるが数値不正確
  ❌ 不合格: 数値が間違い、hallucination あり、またはクラッシュ

M1 合格ライン: 14問以上 ✅ (70%)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field


@dataclass
class TestCase:
    q: str           # 質問文
    category: str    # カテゴリ
    expect: str      # 期待する回答の要件 (評価基準)

    answer: str = ""         # 実際の回答
    intent: dict = field(default_factory=dict)
    elapsed: float = 0.0
    result: str = "?"        # ✅ / ⚠ / ❌ / ? (未評価)
    note: str = ""           # 評価メモ


TEST_CASES: list[TestCase] = [
    # === カテゴリ1: 単一技照会 (6問) ===
    TestCase("サガットの2HKの発生は?",
             "単一技照会",
             "startup の数値 (11F) を含む"),
    TestCase("サガットの立ち強Pはガードされたら何F?",
             "単一技照会",
             "on_block の数値 (-5F) を含む"),
    TestCase("ルークの5MPのリーチは?",
             "単一技照会",
             "atk_range の数値を含む"),
    TestCase("サガットの2HKでパニカン取ったら何F有利?",
             "単一技照会",
             "punish_adv の数値 (+45F) を含む"),
    TestCase("ケンの昇竜拳のガード硬直は?",
             "単一技照会",
             "on_block を含む、大幅不利 or データなしと回答"),
    TestCase("サガットのジャンプ強Kの発生は?",
             "単一技照会",
             "j.HK の startup を含む"),

    # === カテゴリ2: ゲーム概念 (5問) ===
    TestCase("ドライブインパクトって何?",
             "ゲーム概念",
             "Drive Gauge消費、壁やられ、アーマー等に言及 or Phase 2 待ちと明示"),
    TestCase("バーンアウトってどうなるの?",
             "ゲーム概念",
             "Drive Gauge枯渇の状態 or Phase 2 待ちと明示"),
    TestCase("パーフェクトパリィって何が起きる?",
             "ゲーム概念",
             "有利フレーム or Phase 2 待ちと明示"),
    TestCase("パニッシュカウンターとカウンターヒットの違いは?",
             "ゲーム概念",
             "有利フレームの違い or Phase 2 待ちと明示"),
    TestCase("ドライブラッシュのキャンセル版って何?",
             "ゲーム概念",
             "通常技キャンセルからのDR or Phase 2 待ちと明示"),

    # === カテゴリ3: 反撃・判定 (5問) ===
    TestCase("サガットの2HKガードして反撃できる?",
             "反撃・判定",
             "on_block の数値 (-12F)、反撃可能かの言及"),
    TestCase("リュウの波動拳ガードしたら何Fある?",
             "反撃・判定",
             "on_block を含む、または特殊技でデータなしと回答"),
    TestCase("ケンの竜巻はガードして反撃確定?",
             "反撃・判定",
             "on_block を含む、または特殊技でデータなしと回答"),
    TestCase("サガットの5HPの先端はリーチどのくらい?",
             "反撃・判定",
             "atk_range の数値 (2.17) を含む"),
    TestCase("-6Fの技に発生6Fの技で反撃できる?",
             "反撃・判定",
             "理論的にはYes、実戦では距離依存と回答 or データなしと明示"),

    # === カテゴリ4: エッジケース (4問) ===
    TestCase("サガットのタイガーショットのデータ教えて",
             "エッジケース",
             "必殺技→M1段階では未マッピングとデータなし回答"),
    TestCase("ザンギエフの投げのリーチは?",
             "エッジケース",
             "データがない場合は正直にそう回答"),
    TestCase("今日のパッチで何が変わった?",
             "エッジケース",
             "リアルタイム情報はないと回答"),
    TestCase("サガットの対空最強技は?",
             "エッジケース",
             "戦術判断→データのみ提示し断定しない"),
]


async def run_all(cases: list[TestCase]) -> None:
    from sf6_engine.factory import create_provider
    from sf6_engine.intent_parser import parse_intent
    from sf6_engine.rag_builder import build_context, generate_answer

    provider = create_provider()

    if not await provider.is_available():
        print("❌ Ollama が起動していません。`ollama serve` を実行してください。")
        sys.exit(1)

    print(f"=== M1 統合テスト (モデル: {provider.model}) ===\n")

    for i, tc in enumerate(cases, start=1):
        print(f"[{i:02d}/{len(cases)}] {tc.q}")
        t0 = time.monotonic()
        try:
            intent  = await parse_intent(tc.q, provider)
            context = await build_context(intent)
            answer  = await generate_answer(tc.q, context, provider)
            tc.intent  = intent
            tc.answer  = answer
            tc.elapsed = time.monotonic() - t0
            print(f"       → {answer[:100].replace(chr(10), ' ')}")
            print(f"         ({tc.elapsed:.1f}s | intent={intent.get('intent_type')})")
        except Exception as e:
            tc.answer  = f"ERROR: {e}"
            tc.elapsed = time.monotonic() - t0
            print(f"       ❌ ERROR: {e}")
        print()


def print_summary(cases: list[TestCase]) -> None:
    print("\n" + "="*60)
    print("=== 統合テスト結果サマリー ===")
    print("="*60)

    categories: dict[str, list[TestCase]] = {}
    for tc in cases:
        categories.setdefault(tc.category, []).append(tc)

    for cat, tcs in categories.items():
        print(f"\n【{cat}】")
        for tc in tcs:
            print(f"  {tc.result:2s} Q: {tc.q}")
            print(f"     A: {tc.answer[:80].replace(chr(10),' ')}")
            print(f"     期待: {tc.expect}")

    ok  = sum(1 for tc in cases if tc.result == "✅")
    warn = sum(1 for tc in cases if tc.result == "⚠")
    ng  = sum(1 for tc in cases if tc.result == "❌")
    pending = sum(1 for tc in cases if tc.result == "?")

    avg_time = sum(tc.elapsed for tc in cases) / len(cases) if cases else 0

    print("\n" + "-"*60)
    print(f"合計: {len(cases)}問  ✅{ok}  ⚠{warn}  ❌{ng}  ?{pending}(未評価)")
    print(f"平均レスポンス時間: {avg_time:.1f}秒")
    print(f"M1 合格ライン: 14/20問以上 ✅")
    if ok >= 14:
        print("🎉 M1 合格！")
    else:
        print(f"📋 あと {14 - ok} 問の改善が必要")

    # 結果を JSON で保存
    results = [
        {
            "q": tc.q, "category": tc.category,
            "result": tc.result, "answer": tc.answer,
            "intent": tc.intent, "elapsed": round(tc.elapsed, 2),
        }
        for tc in cases
    ]
    with open("tests/integration_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n結果を tests/integration_results.json に保存しました。")


if __name__ == "__main__":
    asyncio.run(run_all(TEST_CASES))

    print("\n--- 評価フェーズ ---")
    print("各回答を確認し、✅/⚠/❌ を入力してください (Enter でスキップ → ?)")
    print("q で終了してサマリーを表示\n")

    for i, tc in enumerate(TEST_CASES, start=1):
        print(f"[{i:02d}] Q: {tc.q}")
        print(f"  A: {tc.answer[:120]}")
        print(f"  期待: {tc.expect}")
        raw = input("  結果 [✅/⚠/❌/s=skip/q=quit]: ").strip()
        if raw == "q":
            break
        elif raw in ("✅", "1", "ok", "y"):
            tc.result = "✅"
        elif raw in ("⚠", "2", "warn", "w"):
            tc.result = "⚠"
        elif raw in ("❌", "3", "ng", "n"):
            tc.result = "❌"
        print()

    print_summary(TEST_CASES)
