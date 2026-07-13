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


if __name__ == "__main__":
    unittest.main()
