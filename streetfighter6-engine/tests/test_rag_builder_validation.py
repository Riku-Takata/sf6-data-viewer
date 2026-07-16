"""rag_builder の回答検証まわりの単体テスト。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.llm_provider import LLMResponse  # noqa: E402
from sf6_engine.rag_builder import generate_answer  # noqa: E402


ANSWER_KEY = "回答文_上で転記した数値だけを使い日本語で簡潔に"
TRANSCRIBED_KEY = "参照データから符号ごと一字一句転記したフレーム数値のリスト"


class FakeProvider:
    def __init__(self, structured_outputs: list[dict]) -> None:
        self.structured_outputs = structured_outputs
        self.calls = 0

    async def generate_structured(self, prompt: str, schema: dict, system: str = "") -> dict:
        del prompt, schema, system
        self.calls += 1
        return self.structured_outputs[min(self.calls - 1, len(self.structured_outputs) - 1)]

    async def generate(self, prompt: str, system: str = "") -> LLMResponse:
        del prompt, system
        return LLMResponse(text="fallback")


class GenerateAnswerValidationTest(unittest.TestCase):
    def test_move_query_summary_is_returned_without_llm_rephrasing(self) -> None:
        provider = FakeProvider([])
        context = (
            "【技条件検索】\n"
            "rashid の全技を、ガード時の攻撃側（ガードさせた側）が +0F より大きいで検索しました。\n"
            "【基準値で条件一致（1件）】\n"
            "- 5MP / 立ち中P: +2F [CAPCOM公式]"
        )

        answer = asyncio.run(
            generate_answer("ラシードのガードで有利な技は？", context, provider)
        )

        self.assertEqual(answer, context)
        self.assertEqual(provider.calls, 0)

    def test_bare_frame_number_is_accepted_for_transcribed_value(self) -> None:
        provider = FakeProvider([{
            TRANSCRIBED_KEY: ["12F"],
            ANSWER_KEY: "発生は12です。",
        }])
        context = (
            "【ken / 5HK (Standing Heavy Kick)】\n"
            "発生: 12\n"
            "ガード時: -5F (技を出した側が-5F / ガードした側は+5F)"
        )

        answer = asyncio.run(generate_answer("ケンの大Kについて教えて", context, provider))

        self.assertEqual(provider.calls, 1)
        self.assertIn("発生は12です。", answer)
        self.assertNotIn("自動検証", answer)

    def test_startup_question_uses_deterministic_fast_path(self) -> None:
        provider = FakeProvider([])
        context = (
            "【ken / 5HK (Standing Heavy Kick)】\n"
            "発生: 12F\n"
            "ガード時: -5F (技を出した側が-5F / ガードした側は+5F)"
        )

        answer = asyncio.run(generate_answer("ケンの大Kの発生は？", context, provider))

        self.assertEqual(provider.calls, 0)
        self.assertIn("発生は12Fです。", answer)

    def test_validation_debug_excerpt_is_not_returned_to_user(self) -> None:
        provider = FakeProvider([{
            TRANSCRIBED_KEY: ["99F"],
            ANSWER_KEY: "発生は99Fです。",
        }])
        context = (
            "【ken / 5HK (Standing Heavy Kick)】\n"
            "発生: 12F\n"
            "ガード時: -5F (技を出した側が-5F / ガードした側は+5F)"
        )

        answer = asyncio.run(generate_answer("ケンの大Kについて教えて", context, provider))

        self.assertEqual(provider.calls, 2)
        self.assertNotIn("99F", answer)
        self.assertIn("矛盾のない回答文を生成できませんでした", answer)
        self.assertNotIn("自動検証", answer)
        self.assertNotIn("正確な参照データ", answer)

    def test_integrated_profile_answers_all_core_fields_without_llm(self) -> None:
        provider = FakeProvider([])
        context = (
            "【ken / 5HK (立ち強K) — 統合フレームプロファイル】\n"
            "発生: 12F [採用: CAPCOM公式]\n"
            "持続: 2F [採用: CAPCOM公式]\n"
            "硬直: 23F（条件値あり） [採用: CAPCOM公式]\n"
            "ガード時（攻撃側・ガードさせた側）: -5F [採用: CAPCOM公式]\n"
            "ガード時（防御側・ガードした側）: +5F [攻撃側のCAPCOM公式値を符号反転]\n"
            "ガード時: -5F (技を出した側が-5F / ガードした側は+5F)\n"
            "【ソース差異:硬直】CAPCOM公式=※23 / UFD=25 / SuperCombo=23(25)\n"
            "CAPCOM公式注記: ※空振り時硬直2F増加"
        )

        active = asyncio.run(generate_answer("ケンの大Kの持続は？", context, provider))
        recovery = asyncio.run(generate_answer("ケンの大Kの硬直は？", context, provider))
        attacker = asyncio.run(generate_answer("ケンの大Kをガードさせたら何F？", context, provider))
        defender = asyncio.run(generate_answer("ケンの大Kをガードしたら何F？", context, provider))
        both = asyncio.run(generate_answer("ケンの大Kのガード時フレームは？", context, provider))

        self.assertEqual(provider.calls, 0)
        self.assertIn("持続は2F", active)
        self.assertIn("硬直は23F", recovery)
        self.assertIn("UFD=25", recovery)
        self.assertIn("攻撃側）は-5F", attacker)
        self.assertIn("防御側）は+5F", defender)
        self.assertIn("攻撃側が-5F", both)
        self.assertIn("防御側が+5F", both)

    def test_integrated_profile_reports_missing_guard_data_without_llm(self) -> None:
        provider = FakeProvider([])
        context = (
            "【terry / 5MP~HK~HK (パワーダンク) — 統合フレームプロファイル】\n"
            "発生: 18F [採用: CAPCOM公式]\n"
            "持続: 3F→空白21F→6F（複数持続区間） [採用: CAPCOM公式]\n"
            "硬直: 33F [採用: CAPCOM公式]\n"
            "ガード時（攻撃側・ガードさせた側）: データなし [採用: なし]\n"
            "ガード時（防御側・ガードした側）: 算出不可 [攻撃側のなし値を符号反転]"
        )

        answer = asyncio.run(
            generate_answer("テリーのパワーダンクをガードしたら何F？", context, provider)
        )

        self.assertEqual(provider.calls, 0)
        self.assertIn("いずれにもデータがない", answer)
        self.assertIn("算出できません", answer)

    def test_attacker_guard_perspective_is_corrected(self) -> None:
        provider = FakeProvider([{
            TRANSCRIBED_KEY: ["+3F"],
            ANSWER_KEY: "ガードした側は +3F です。",
        }])
        context = (
            "【aki / 2HK (Crouching Heavy Kick)】\n"
            "ガード時: -3F (技を出した側が-3F / ガードした側は+3F)"
        )

        answer = asyncio.run(generate_answer("A.K.I.の2HKをガードさせたら何F？", context, provider))

        self.assertIn("技を出した側は -3F", answer)
        self.assertIn("3フレーム不利", answer)

    def test_signed_value_with_wrong_polarity_is_corrected(self) -> None:
        provider = FakeProvider([{
            TRANSCRIBED_KEY: ["-3F"],
            ANSWER_KEY: "技を出した側が -3F 有利です。",
        }])
        context = (
            "【aki / 3MP (Pu Lao)】\n"
            "ガード時: -3F (技を出した側が-3F / ガードした側は+3F)"
        )

        answer = asyncio.run(generate_answer("A.K.I.の3MPをガードさせたら何F？", context, provider))

        self.assertIn("技を出した側は -3F", answer)
        self.assertIn("3フレーム不利", answer)
        self.assertNotIn("-3F 有利", answer)

    def test_punish_suggestion_uses_deterministic_fast_path(self) -> None:
        provider = FakeProvider([])
        context = (
            "2HP（Xiu She） はガード時 -8F。ガードした側は +8F 有利 → 発生 8F 以内の技が確定反撃。\n"
            "【ryu の確定反撃候補】\n"
            "- 5LP / 立ち弱P（ジャブ）: 発生4F\n"
            "- 2LP / しゃがみ弱P（ジャブ）: 発生4F"
        )

        answer = asyncio.run(
            generate_answer("A.K.I.の2HPをリュウでガードした後、確定反撃に使える技を提案して", context, provider)
        )

        self.assertEqual(provider.calls, 0)
        self.assertIn("発生8F以内", answer)
        self.assertIn("5LP", answer)

    def test_timing_only_candidates_are_not_called_confirmed_punishes(self) -> None:
        provider = FakeProvider([])
        context = (
            "5HK は今回の条件でガード時 -5F。ガードした側は +5F なので、"
            "発生 5F 以内がフレーム上の候補です。確定反撃としての成立は未確定です。\n"
            "【ryu のフレーム上の反撃候補（到達未検証）】\n"
            "- 5LP / 立ち弱P（ジャブ）: 発生4F / リーチ未検証\n"
            "- 623LP / 弱 昇龍拳: 発生5F / リーチ未検証"
        )

        answer = asyncio.run(
            generate_answer("ケンの5HKをリュウでガードした後の確反は？", context, provider)
        )

        self.assertEqual(provider.calls, 0)
        self.assertIn("フレーム上の候補", answer)
        self.assertIn("確定反撃としては未確定", answer)
        self.assertNotIn("技が確定反撃です", answer)

    def test_conditional_punish_value_is_deferred_without_llm(self) -> None:
        provider = FakeProvider([])
        context = (
            "214P~LK（前突） の条件適用後ガード時硬直差は -5F（条件付き）。"
            "今回の条件で単一値を確定できないため反撃判定を保留します。"
        )

        answer = asyncio.run(
            generate_answer(
                "春麗の214P~LKをガードした後の確反は？", context, provider
            )
        )

        self.assertEqual(provider.calls, 0)
        self.assertIn("単一値に確定できない", answer)
        self.assertIn("確定反撃候補の提示を保留", answer)
        self.assertNotIn("F以内", answer)

    def test_unresolved_scenario_does_not_fall_back_to_reference_block_value(self) -> None:
        provider = FakeProvider([])
        context = (
            "【ken / 5HK (Standing Heavy Kick) — 統合フレームプロファイル】\n"
            "ガード時（攻撃側・ガードさせた側）: -5F [採用: CAPCOM公式]\n"
            "ガード時（防御側・ガードした側）: +5F [攻撃側のCAPCOM公式値を符号反転]\n"
            "質問条件: defender_burnout=True / interaction=block\n"
            "条件適用後ガード時（攻撃側）: 条件補正を適用する構造化ルールが未登録 "
            "[conditional_unresolved]\n"
            "条件適用後ガード時（防御側）: 条件補正を適用する構造化ルールが未登録 "
            "[conditional_unresolved]"
        )

        answer = asyncio.run(
            generate_answer(
                "ケンの5HKを相手がバーンアウト中にガードさせたら何F？",
                context,
                provider,
            )
        )

        self.assertEqual(provider.calls, 0)
        self.assertIn("単一値を確定できません", answer)
        self.assertNotIn("攻撃側）は-5F", answer)

    def test_perspective_only_scenario_keeps_conditional_reference_display(self) -> None:
        provider = FakeProvider([])
        context = (
            "【test / 5HK (Conditional Kick) — 統合フレームプロファイル】\n"
            "ガード時（攻撃側・ガードさせた側）: +1F / +11F（条件別） "
            "[採用: CAPCOM公式]\n"
            "ガード時（防御側・ガードした側）: -1F / -11F（条件別） "
            "[攻撃側のCAPCOM公式値を符号反転]\n"
            "質問条件: interaction=block / perspective=defender\n"
            "条件適用後ガード時（攻撃側）: +1F / +11F（条件別） "
            "[conditional_unresolved]\n"
            "条件適用後ガード時（防御側）: -1F / -11F（条件別） "
            "[conditional_unresolved]"
        )

        answer = asyncio.run(
            generate_answer(
                "Conditional Kickをガードした側は何F？", context, provider
            )
        )

        self.assertEqual(provider.calls, 0)
        self.assertIn("-1F / -11F（条件別）", answer)
        self.assertNotIn("単一値を確定できません", answer)

    def test_guard_question_can_use_check_punish_summary_fast_path(self) -> None:
        provider = FakeProvider([])
        context = "j.LK（Jumping Light Kick） はガード時 +0F。ガードした側は +0F 有利 → 確定反撃なし。"

        answer = asyncio.run(generate_answer("A.K.I.のj.LKをガードさせたら何F？", context, provider))

        self.assertEqual(provider.calls, 0)
        self.assertIn("技を出した側は +0F", answer)
        self.assertIn("五分", answer)


if __name__ == "__main__":
    unittest.main()
