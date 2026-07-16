"""intent_parser の定型質問 fast path テスト。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.intent_parser import parse_intent  # noqa: E402


class FailingProvider:
    async def generate_structured(self, *args, **kwargs) -> dict:
        raise AssertionError("deterministic fast path should not call provider")


class DeterministicIntentParserTest(unittest.TestCase):
    def parse(self, query: str) -> dict:
        return asyncio.run(parse_intent(query, FailingProvider()))

    def test_japanese_special_move_keeps_raw_move_name(self) -> None:
        intent = self.parse("ブランカのOD エレクトリックサンダーの発生は？")

        self.assertEqual(intent["intent_type"], "lookup_move")
        self.assertEqual(intent["chara"], "Blanka")
        self.assertEqual(intent["move_name"], "OD エレクトリックサンダー")

    def test_compound_sc_input_is_not_shortened(self) -> None:
        intent = self.parse("春麗のj.HP~j.HPの発生は？")

        self.assertEqual(intent["intent_type"], "lookup_move")
        self.assertEqual(intent["chara"], "Chun-Li")
        self.assertEqual(intent["input"], "j.HP~j.HP")

    def test_unusual_sc_inputs_are_kept_as_input(self) -> None:
        cases = [
            ("C.ヴァイパーの2~8の発生は？", "C.Viper", "2~8"),
            ("ディージェイの~HK (End)の発生は？", "Dee_Jay", "~HK (End)"),
            ("ケンのKK~MKをガードさせたら何F？", "Ken", "KK~MK"),
            ("ラシードの6[6]の発生は？", "Rashid", "6[6]"),
            ("M.バイソンの-の発生は？", "M.Bison", "-"),
        ]
        for query, chara, expected_input in cases:
            with self.subTest(query=query):
                intent = self.parse(query)
                self.assertEqual(intent["intent_type"], "lookup_move")
                self.assertEqual(intent["chara"], chara)
                self.assertEqual(intent["input"], expected_input)

    def test_punish_question_extracts_punisher(self) -> None:
        intent = self.parse("キャミィのリバースエッジをリュウでガードした後、確定反撃に使える技を提案して")

        self.assertEqual(intent["intent_type"], "punish_check")
        self.assertEqual(intent["chara"], "Cammy")
        self.assertEqual(intent["chara2"], "Ryu")
        self.assertEqual(intent["move_name"], "リバースエッジ")

    def test_japanese_normal_shorthand_becomes_input(self) -> None:
        intent = self.parse("ケンの大Kの発生は？")

        self.assertEqual(intent["intent_type"], "lookup_move")
        self.assertEqual(intent["chara"], "Ken")
        self.assertEqual(intent["input"], "5HK")

    def test_active_and_recovery_questions_extract_only_the_move(self) -> None:
        cases = [
            ("ケンの大Kの持続は？", "5HK"),
            ("ケンの大Kの硬直は？", "5HK"),
            ("ケンの大Kのフレームデータを教えて", "5HK"),
        ]
        for query, expected_input in cases:
            with self.subTest(query=query):
                intent = self.parse(query)
                self.assertEqual(intent["intent_type"], "lookup_move")
                self.assertEqual(intent["chara"], "Ken")
                self.assertEqual(intent["input"], expected_input)

    def test_guard_advantage_collection_becomes_typed_move_query(self) -> None:
        intent = self.parse("ラシードの技の中でガードさせて有利な技は？")

        self.assertEqual(intent["intent_type"], "query_moves")
        self.assertEqual(intent["chara"], "Rashid")
        self.assertNotIn("move_name", intent)
        self.assertNotIn("input", intent)
        self.assertEqual(
            intent["move_filter"],
            {
                "field": "on_block",
                "operator": "gt",
                "value": 0,
                "perspective": "attacker",
            },
        )
        self.assertEqual(intent["move_scope"], "all")

    def test_move_query_preserves_threshold_and_scope(self) -> None:
        intent = self.parse("ラシードの通常技でガードさせて+2F以上の技は？")

        self.assertEqual(intent["intent_type"], "query_moves")
        self.assertEqual(intent["move_scope"], "normal")
        self.assertEqual(intent["move_filter"]["operator"], "gte")
        self.assertEqual(intent["move_filter"]["value"], 2)

    def test_single_move_guard_question_does_not_become_collection_query(self) -> None:
        intent = self.parse("ラシードの5MPをガードさせたら何F有利？")

        self.assertEqual(intent["intent_type"], "lookup_move")
        self.assertEqual(intent["input"], "5MP")

    def test_ken_jinrai_blockstring_notation_is_structured_without_llm(self) -> None:
        intent = self.parse("ケンの2中K→中迅雷脚は連続ガード？")

        self.assertEqual(intent["intent_type"], "sequence_analysis")
        self.assertEqual(intent["chara"], "Ken")
        self.assertEqual(intent["attacker_sequence"], ["2MK", "中迅雷脚"])
        self.assertEqual(intent["query_targets"], ["blockstring", "timeline"])

    def test_ken_heavy_jinrai_four_frame_interrupt_is_structured_without_llm(self) -> None:
        intent = self.parse("ケンの2中K→大迅雷脚は発生4Fの技で割り込める？")

        self.assertEqual(intent["intent_type"], "sequence_analysis")
        self.assertEqual(intent["attacker_sequence"], ["2MK", "大迅雷脚"])
        self.assertEqual(intent["defender_action"]["startup_f"], 4)
        self.assertEqual(intent["query_targets"], ["interrupt", "timeline"])

    def test_character_specific_special_name_is_opaque_to_intent_parser(self) -> None:
        cases = [
            ("ブランカのしゃがみ中K→OD エレクトリックサンダーは連ガ？", "Blanka", "2MK", "OD エレクトリックサンダー"),
            ("春麗の立ち中P→弱 百裂脚は連続ガード？", "Chun-Li", "5MP", "弱 百裂脚"),
            ("リュウの立ち強P→SA3 真・昇龍拳は連続ガード？", "Ryu", "5HP", "SA3 真・昇龍拳"),
        ]

        for query, chara, opener, target in cases:
            with self.subTest(query=query):
                intent = self.parse(query)
                self.assertEqual(intent["intent_type"], "sequence_analysis")
                self.assertEqual(intent["chara"], chara)
                self.assertEqual(intent["attacker_sequence"], [opener, target])

    def test_natural_language_sequence_connectors_are_supported(self) -> None:
        cases = [
            ("リュウの立ち弱Pから弱 波掌撃は連続ガード？", "Ryu", "5LP", "弱 波掌撃"),
            ("春麗の立ち中Pの後に弱 百裂脚は連ガ？", "Chun-Li", "5MP", "弱 百裂脚"),
            (
                "ブランカの2MKをOD エレクトリックサンダーでキャンセルしたら連ガ？",
                "Blanka", "2MK", "OD エレクトリックサンダー",
            ),
            ("Ryuの5LP into 214LPはblockstring?", "Ryu", "5LP", "214LP"),
        ]

        for query, chara, opener, target in cases:
            with self.subTest(query=query):
                intent = self.parse(query)
                self.assertEqual(intent["intent_type"], "sequence_analysis")
                self.assertEqual(intent["chara"], chara)
                self.assertEqual(intent["attacker_sequence"], [opener, target])

    def test_supercombo_composite_target_is_preserved_for_sequence_analysis(self) -> None:
        intent = self.parse("A.K.I.の5LP→5LP~LPは発生4Fで割り込める？")

        self.assertEqual(intent["intent_type"], "sequence_analysis")
        self.assertEqual(intent["chara"], "A.K.I.")
        self.assertEqual(intent["attacker_sequence"], ["5LP", "5LP~LP"])
        self.assertEqual(intent["defender_action"]["startup_f"], 4)


if __name__ == "__main__":
    unittest.main()
