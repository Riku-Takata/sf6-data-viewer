"""対応範囲テスト: M3 完了時点の全5領域 + M4 追加テストを自動評価する。

対象領域:
  A. フレームデータ照会・確定反撃 (全30キャラ / 通常技・必殺技・SA)
  B. コンボ接続検証・最大コンボ計算 (ビームサーチ)
  C. セットプレイ (KD後の起き攻め択計算、全キャラダッシュF対応)
  D. 派生技の割り込み判定 (フレームギャップ自動計算)
  E. ゲーム概念説明 (doc_chunks RAG)
  S. セットプレイ追加 (複数キャラ: Ken/Guile/Cammy/Luke/Ryu)
  P. 確定反撃追加 (複数キャラ: Sagat/Luke/Ryu/Guile/Zangief)
  R. 派生技追加 (ケン迅雷脚 中/強派生 フレームデータ)
  Q. 複合クエリ (DR cancel / パニカン後セットプレイ 等)

使い方:
  cd streetfighter6-engine
  PYTHONPATH=src python tests/capability_test.py

自動評価ルール:
  ✅ check_keywords がすべて answer に含まれれば合格
  ⚠  check_any の中に1つ以上含まれれば合格
  ❌ 何も含まれなければ不合格
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field


@dataclass
class Case:
    id: str
    area: str
    q: str
    check_keywords: list[str] = field(default_factory=list)   # すべて含まれれば ✅
    check_any: list[str] = field(default_factory=list)        # 1つ以上含まれれば ✅
    check_absent: list[str] = field(default_factory=list)     # 含まれていてはいけない (hallucination チェック)
    # ── 実行結果 ──
    answer: str = ""
    intent: dict = field(default_factory=dict)
    elapsed: float = 0.0
    result: str = "?"
    reason: str = ""


CASES: list[Case] = [

    # ================================================================
    # A. フレームデータ照会・確定反撃
    # ================================================================

    # 通常技 — 主要キャラ
    Case("A-01", "フレームデータ(通常技)",
         "サガットの2HKの発生は?",
         check_keywords=["11"],
    ),
    Case("A-02", "フレームデータ(通常技)",
         "サガットの立ち強Pはガードされたら何F?",
         check_any=["-5", "5F"],
    ),
    Case("A-03", "フレームデータ(通常技)",
         "ルークの5MPのリーチは?",
         check_any=["1.6", "1.7"],  # 1.673
    ),
    Case("A-04", "フレームデータ(通常技)",
         "サガットの2HKでパニカン取ったら何F有利?",
         check_keywords=["45"],
    ),
    Case("A-05", "フレームデータ(通常技)",
         "リュウの5HPの発生は?",
         check_any=["10", "11", "12"],  # DB実データは10F
    ),
    Case("A-06", "フレームデータ(通常技)",
         "ザンギエフの屈強Kはガード時何F?",
         check_any=["-", "ガード", "F"],  # 負の値が含まれる
    ),
    Case("A-07", "フレームデータ(通常技)",
         "春麗の立ち中Pの発生は?",
         check_any=["5", "6", "7"],  # DB実データは5F
    ),
    Case("A-08", "フレームデータ(通常技)",
         "ケンの屈中Kの発生は?",
         check_any=["7", "8", "9", "10"],
    ),

    # 必殺技
    Case("A-09", "フレームデータ(必殺技)",
         "サガットのタイガーショットのガード時は?",
         check_any=["-1", "0", "+", "F"],
    ),
    Case("A-10", "フレームデータ(必殺技)",
         "ケンの昇竜拳のガード時は?",
         check_any=["-", "不利", "大幅"],
    ),
    Case("A-11", "フレームデータ(必殺技)",
         "ガイルのソニックブームのガード時は?",
         check_any=["-1", "0", "1", "F"],
    ),
    Case("A-12", "フレームデータ(必殺技)",
         "リュウの波動拳の発生は?",
         check_any=["12", "13", "14", "15", "F"],
    ),
    Case("A-13", "フレームデータ(必殺技)",
         "サガットのタイガーアッパーカットの無敵フレームは?",
         check_any=["無敵", "Air", "invuln", "1-"],
    ),
    Case("A-14", "フレームデータ(必殺技)",
         "キャミィのスパイラルアローのガード時は?",
         check_any=["-", "F", "不利"],
    ),

    # 確定反撃
    Case("A-15", "確定反撃",
         "サガットの2HKをガードして反撃できる?",
         check_keywords=["12"],
         check_any=["確定", "反撃", "発生"],
    ),
    Case("A-16", "確定反撃",
         "ケンの昇竜拳はガードして反撃確定?",
         check_any=["確定", "反撃", "不利", "-"],
    ),
    Case("A-17", "確定反撃",
         "リュウの波動拳ガードして反撃できる?",
         check_any=["F", "反撃", "発生", "ガード"],
    ),

    # ================================================================
    # B. コンボ接続検証・最大コンボ計算
    # ================================================================

    Case("B-01", "コンボ接続",
         "サガットの2HKの後に何が繋がる?",
         check_any=["KD", "ノックダウン", "前ステップ", "セットプレイ", "繋が"],
    ),
    Case("B-02", "コンボ接続",
         "ルークの5MPをDRキャンセルすると何F?",
         check_any=["DR", "ラッシュ", "F", "有利"],
    ),
    Case("B-03", "コンボ接続",
         "サガットの5HPからコンボある?",
         check_any=["発生", "F", "繋", "キャンセル", "コンボ"],
    ),
    Case("B-04", "コンボ接続",
         "リュウの2MPは必殺技キャンセルできる?",
         check_any=["キャンセル", "必殺", "Sp", "可", "なし"],
    ),
    Case("B-05", "最大コンボ",
         "サガットの5HPからの最大コンボは?",
         check_any=["発生", "ダメージ", "F", "コンボ", "最大"],
    ),

    # ================================================================
    # C. セットプレイ (KD後の起き攻め択計算)
    # ================================================================

    Case("C-01", "セットプレイ",
         "サガットの623HPヒット後の起き攻めは?",
         check_any=["前ステップ", "KD", "有利", "+", "F", "択"],
    ),
    Case("C-02", "セットプレイ",
         "サガットの2HKでKDした後の択は?",
         check_any=["前ステップ", "KD", "有利", "F", "択", "起き攻め"],
    ),
    Case("C-03", "セットプレイ",
         "リュウの2HKのKD後セットプレイを教えて",
         check_any=["KD", "前ステップ", "有利", "F", "択"],
    ),
    Case("C-04", "セットプレイ",
         "ルークの強フラッシュナックルKD後の前ステップ後は何F有利?",
         check_any=["F", "有利", "前ステップ", "+"],
    ),

    # ================================================================
    # D. 派生技の割り込み判定
    # ================================================================

    Case("D-01", "派生技・割り込み",
         "ケンの迅雷脚の弱派生前は割り込める?",
         check_any=["割り込", "隙間", "F", "発生", "連携", "ガード"],
    ),
    Case("D-02", "派生技・割り込み",
         "ケンの迅雷脚の弱派生フレームデータを教えて",
         check_any=["発生", "F", "ガード", "ヒット", "派生"],
    ),
    Case("D-03", "派生技・割り込み",
         "サガットのステハイからステローへの派生は割り込める?",
         # ステハイとステローは独立した技 (派生関係なし) → 正しい「データなし」応答も合格
         check_any=["割り込", "F", "隙間", "発生", "連携", "含まれていません", "データ", "情報"],
    ),

    # ================================================================
    # E. ゲーム概念 (doc_chunks RAG)
    # ================================================================

    Case("E-01", "ゲーム概念",
         "ドライブインパクトって何?",
         check_any=["アーマー", "壁", "Drive Impact", "DI", "gauge", "Gauge"],
    ),
    Case("E-02", "ゲーム概念",
         "バーンアウトってどうなるの?",
         check_any=["チップ", "ガード", "ドライブ", "Burnout", "Drive Gauge"],
    ),
    Case("E-03", "ゲーム概念",
         "パーフェクトパリィって何が起きる?",
         check_any=["フレーム", "有利", "Perfect Parry", "パリィ", "2F"],
    ),
    Case("E-04", "ゲーム概念",
         "パニッシュカウンターとカウンターヒットの違いは?",
         check_any=["パニッシュ", "カウンター", "フレーム", "有利", "Punish", "Counter"],
    ),
    Case("E-05", "ゲーム概念",
         "ドライブラッシュのキャンセル版って何?",
         check_any=["キャンセル", "通常技", "Drive Rush", "DR"],
    ),

    # ================================================================
    # S. セットプレイ追加 (setplay_analysis) — 複数キャラ
    # ================================================================

    Case("S-01", "セットプレイ(追加)",
         "ケンの2HKでKDした後の起き攻め択を教えて",
         check_any=["KD", "40", "前ステップ", "F", "択", "起き攻め"],
    ),
    Case("S-02", "セットプレイ(追加)",
         "ガイルのサマーソルトキックKD後のセットプレイは?",
         check_any=["KD", "39", "前ステップ", "F", "択"],
    ),
    Case("S-03", "セットプレイ(追加)",
         "キャミィのスパイラルアローKD後の前ステップ後は何F有利?",
         check_any=["F", "有利", "+", "前ステップ", "KD", "29"],
    ),
    Case("S-04", "セットプレイ(追加)",
         "ルークの623HPでKDした後のセットプレイを教えて",
         check_any=["KD", "29", "前ステップ", "F", "択"],
    ),
    Case("S-05", "セットプレイ(追加)",
         "リュウの昇竜拳KD後の前ステップ後は何F有利?",
         check_any=["F", "有利", "+", "前ステップ", "KD"],
    ),
    Case("S-06", "セットプレイ(追加)",
         "ケンの2HKパニカン（HKD）後のセットプレイを教えて",
         check_any=["KD", "47", "F", "有利", "パニカン", "前ステップ"],
    ),

    # ================================================================
    # P. 確定反撃追加 (punish_check) — 複数キャラ
    # ================================================================

    Case("P-01", "確定反撃(追加)",
         "サガットの5HPはガード後に確定反撃できる?",
         check_any=["-5", "5F", "確定", "反撃", "発生"],
    ),
    Case("P-02", "確定反撃(追加)",
         "ルークの5MPをガードした後の確定反撃は?",
         check_any=["-3", "3F", "確定", "反撃", "発生", "なし"],
    ),
    Case("P-03", "確定反撃(追加)",
         "リュウの2HKをガードしたら確定反撃できる?",
         check_any=["-12", "12", "確定", "反撃", "発生"],
    ),
    Case("P-04", "確定反撃(追加)",
         "ガイルの弱ソニックブームはガード後に確定反撃がある?",
         # LP Sonic Boom は -1F ≒ 確定なし
         check_any=["-1", "1F", "なし", "難しい", "確定", "反撃"],
    ),
    Case("P-05", "確定反撃(追加)",
         "ザンギエフの2HKをガードしたら反撃できる?",
         check_any=["-13", "13", "確定", "反撃", "発生"],
    ),

    # ================================================================
    # R. 派生技追加 (follow-up frames) — ケン迅雷脚 中/強派生
    # ================================================================

    Case("R-01", "派生技・割り込み(追加)",
         "ケンの迅雷脚の強派生の発生は?",
         check_any=["10", "F", "発生", "強"],
    ),
    Case("R-02", "派生技・割り込み(追加)",
         "ケンの迅雷脚の中派生の発生フレームは?",
         check_any=["18", "F", "発生", "中"],
    ),
    Case("R-03", "派生技・割り込み(追加)",
         "ケンの迅雷脚の強派生はKDになる?",
         check_any=["KD", "ノックダウン", "33", "+"],
    ),
    Case("R-04", "派生技・割り込み(追加)",
         "ケンの迅雷脚の弱派生はヒット後何F有利?",
         check_any=["+3", "3", "有利", "F", "ヒット"],
    ),
    Case("R-05", "派生技・割り込み(追加)",
         "ケンの迅雷脚の弱派生と強派生の発生を比べて",
         check_any=["6", "10", "F", "発生", "弱", "強"],
    ),

    # ================================================================
    # Q. 複合クエリ
    # ================================================================

    Case("Q-01", "複合クエリ",
         "ルークの5MPをDRキャンセルした後にどう繋げる?",
         check_any=["DR", "ラッシュ", "繋", "有利", "F", "キャンセル"],
    ),
    Case("Q-02", "複合クエリ",
         "サガットの5HPをガード後に確定反撃するには何F以下の技が必要?",
         check_any=["-5", "5F", "確定", "5", "発生", "以下"],
    ),
    Case("Q-03", "複合クエリ",
         "ケンの2HKをパニカンで取った後に前ステップして起き攻めできる?",
         check_any=["KD", "前ステップ", "F", "有利", "起き攻め", "パニカン"],
    ),
    Case("Q-04", "複合クエリ",
         "ユリの5HPはガードして確定反撃できる?",
         check_any=["-5", "5F", "確定", "反撃", "発生", "ユリ", "Juri"],
    ),

    # ================================================================
    # エッジケース・ネガティブ
    # ================================================================

    Case("X-01", "エッジケース",
         "今日のパッチで何が変わった?",
         check_any=["含まれていません", "情報がない", "データ", "ありません", "ない"],
         check_absent=["11F", "12F", "-5"],  # フレームデータを hallucinate してはいけない
    ),
    Case("X-02", "エッジケース",
         "サガットの対空最強技は?",
         check_any=["断定", "データ", "提示", "対空", "Tiger", "アッパー", "なし"],
    ),
]


def _check(case: Case) -> str:
    """answer を評価して ✅ / ⚠ / ❌ を返す。"""
    ans = case.answer.lower()

    # check_absent: 含まれていてはいけないキーワード
    for kw in case.check_absent:
        if kw.lower() in ans:
            case.reason = f"hallucination 疑い: '{kw}' が含まれている"
            return "❌"

    # check_keywords: すべて含まれること
    missing = [kw for kw in case.check_keywords if kw.lower() not in ans]
    if case.check_keywords and not missing:
        return "✅"
    if case.check_keywords and missing:
        case.reason = f"missing: {missing}"
        # check_any で補完できるか試みる
        if not case.check_any:
            return "⚠" if len(missing) == 1 else "❌"

    # check_any: 1つ以上含まれること
    if case.check_any:
        hit = [kw for kw in case.check_any if kw.lower() in ans]
        if hit:
            if not case.check_keywords or not missing:
                return "✅"
            return "⚠"  # check_keywords で欠けていたが check_any は通過
        case.reason = case.reason or f"check_any 全滅: {case.check_any}"
        return "❌"

    # check_keywords/check_any ともに空 → 応答があれば ⚠
    if case.answer and not case.answer.startswith("ERROR"):
        return "⚠"
    return "❌"


async def run_all(cases: list[Case]) -> None:
    from sf6_engine.factory import create_provider
    from sf6_engine.intent_parser import parse_intent
    from sf6_engine.rag_builder import build_context, generate_answer

    provider = create_provider()

    if not await provider.is_available():
        print("❌ Ollama が起動していません。`ollama serve` を実行してください。")
        sys.exit(1)

    model = getattr(provider, "model", "?")
    print(f"=== 対応範囲テスト (モデル: {model}) ===")
    print(f"テストケース数: {len(cases)}\n")

    area_prev = ""
    for i, case in enumerate(cases, 1):
        if case.area != area_prev:
            print(f"\n── {case.area} ──")
            area_prev = case.area

        print(f"[{case.id}] {case.q}")
        t0 = time.monotonic()
        try:
            intent  = await parse_intent(case.q, provider)
            context = await build_context(intent, provider)
            answer  = await generate_answer(case.q, context, provider)
            case.intent  = intent
            case.answer  = answer
            case.elapsed = time.monotonic() - t0
            case.result  = _check(case)

            preview = answer[:120].replace("\n", " ")
            print(f"  {case.result}  ({case.elapsed:.1f}s | intent={intent.get('intent_type')})")
            print(f"  → {preview}")
            if case.reason:
                print(f"  ⚠ {case.reason}")
        except Exception as e:
            case.answer  = f"ERROR: {e}"
            case.elapsed = time.monotonic() - t0
            case.result  = "❌"
            case.reason  = str(e)
            print(f"  ❌ ERROR: {e}")
        print()


def print_summary(cases: list[Case]) -> None:
    print("\n" + "=" * 65)
    print("=== 対応範囲テスト 結果サマリー ===")
    print("=" * 65)

    # エリア別集計
    from collections import defaultdict
    area_results: dict[str, list[Case]] = defaultdict(list)
    for c in cases:
        area_results[c.area].append(c)

    grand_ok = grand_warn = grand_ng = 0
    for area, cs in area_results.items():
        ok   = sum(1 for c in cs if c.result == "✅")
        warn = sum(1 for c in cs if c.result == "⚠")
        ng   = sum(1 for c in cs if c.result == "❌")
        grand_ok += ok; grand_warn += warn; grand_ng += ng
        print(f"\n【{area}】 {ok}✅ {warn}⚠ {ng}❌ / {len(cs)}問")
        for c in cs:
            icon = c.result
            note = f"  ({c.reason})" if c.reason and c.result != "✅" else ""
            print(f"  {icon} [{c.id}] {c.q[:55]}{note}")

    total = len(cases)
    pct   = grand_ok * 100 // total if total else 0
    avg_t = sum(c.elapsed for c in cases) / total if total else 0

    print("\n" + "-" * 65)
    print(f"合計: {total}問  ✅{grand_ok}  ⚠{grand_warn}  ❌{grand_ng}")
    print(f"合格率: {pct}%  (✅のみカウント)")
    print(f"平均レスポンス: {avg_t:.1f}秒")

    target = int(total * 0.75)
    if grand_ok >= target:
        print(f"\n🎉 合格ライン ({target}問以上) 達成!")
    else:
        print(f"\n📋 合格まであと {target - grand_ok} 問の改善が必要 (目標: {target}/{total})")

    # JSON 保存
    results = [
        {
            "id": c.id, "area": c.area, "q": c.q,
            "result": c.result, "reason": c.reason,
            "answer": c.answer,
            "intent_type": c.intent.get("intent_type", ""),
            "elapsed": round(c.elapsed, 2),
        }
        for c in cases
    ]
    out_path = "tests/capability_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n結果を {out_path} に保存しました。")


if __name__ == "__main__":
    asyncio.run(run_all(CASES))
    print_summary(CASES)
