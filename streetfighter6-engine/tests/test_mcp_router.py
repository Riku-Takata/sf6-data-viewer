"""Discord MCP router move-identifier tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discord_bot.mcp_router import map_intent  # noqa: E402


class McpRouterTest(unittest.TestCase):
    def test_japanese_normal_shorthand_uses_normalized_input_for_all_fields(self) -> None:
        for field in ("発生", "持続", "硬直", "全体", "性能"):
            with self.subTest(field=field):
                calls = map_intent({
                    "intent_type": "lookup_move",
                    "chara": "Ken",
                    "input": "5HK",
                    "raw_query": f"ケンの大Kの{field}は？",
                })
                self.assertEqual(
                    calls,
                    [("lookup_move", {"character": "ken", "move_name": "5HK"})],
                )

    def test_japanese_special_name_passes_only_the_move_phrase(self) -> None:
        calls = map_intent({
            "intent_type": "lookup_move",
            "chara": "Cammy",
            "move_name": "強 スパイラルアロー",
            "raw_query": "キャミィの強 スパイラルアローの持続は？",
        })

        self.assertEqual(
            calls,
            [(
                "lookup_move",
                {"character": "cammy", "move_name": "強 スパイラルアロー"},
            )],
        )

    def test_guard_question_keeps_normalized_normal_input(self) -> None:
        calls = map_intent({
            "intent_type": "lookup_move",
            "chara": "Ken",
            "input": "5HK",
            "raw_query": "ケンの大Kをガードしたら何F？",
        })

        self.assertEqual(
            calls,
            [("lookup_move", {"character": "ken", "move_name": "5HK"})],
        )

    def test_scenario_is_forwarded_without_becoming_part_of_move_name(self) -> None:
        scenario = {
            "schema_version": 1,
            "specified": ["distance", "interaction", "perspective"],
            "distance": "tip",
            "interaction": "block",
            "perspective": "defender",
            "evidence": {},
            "ambiguities": [],
        }
        calls = map_intent({
            "intent_type": "punish_check",
            "chara": "Ken",
            "input": "5HK",
            "chara2": "Ryu",
            "scenario": scenario,
            "raw_query": "ケンの先端で大Kをリュウでガードした後、反撃できる？",
        })

        self.assertEqual(calls[0][0], "check_punish")
        self.assertEqual(calls[0][1]["move_name"], "5HK")
        self.assertEqual(calls[0][1]["scenario"], scenario)


if __name__ == "__main__":
    unittest.main()
