"""Discord MCP router move-identifier tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discord_bot.mcp_router import is_alias_learnable_result, map_intent  # noqa: E402


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

    def test_direction_number_japanese_strength_keeps_normalized_input(self) -> None:
        calls = map_intent({
            "intent_type": "lookup_move",
            "chara": "Sagat",
            "input": "2MP",
            "raw_query": "サガットの2中pは発生何フレ？",
        })

        self.assertEqual(
            calls,
            [("lookup_move", {"character": "sagat", "move_name": "2MP"})],
        )

    def test_pressure_family_routes_without_overriding_the_resolved_family_name(self) -> None:
        calls = map_intent({
            "intent_type": "pressure_family_analysis",
            "chara": "Ken",
            "family_move": "Jinrai Kick",
            "variant_scope": "normal",
            "raw_query": "ケンの迅雷って割り込める？",
        })

        self.assertEqual(calls, [(
            "analyze_sequence_family",
            {
                "character": "ken",
                "family_move": "Jinrai Kick",
                "initial_interaction": "block",
                "variant_scope": "normal",
            },
        )])

    def test_matchup_interrupt_overview_routes_attacker_and_defender(self) -> None:
        calls = map_intent({
            "intent_type": "matchup_interrupt_overview",
            "chara": "Ryu",
            "chara2": "Kimberly",
            "raw_query": "リュウの主な技に対してキンバリーが割り込める技を教えてください",
        })

        self.assertEqual(calls, [(
            "analyze_matchup_interrupt_overview",
            {"attacker": "ryu", "defender": "kimberly"},
        )])

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

    def test_move_query_maps_typed_filter_without_move_name(self) -> None:
        calls = map_intent({
            "intent_type": "query_moves",
            "chara": "Rashid",
            "move_filter": {
                "field": "on_block",
                "operator": "gt",
                "value": 0,
                "perspective": "attacker",
            },
            "move_scope": "all",
            "raw_query": "ラシードの技の中でガードさせて有利な技は？",
        })

        self.assertEqual(
            calls,
            [(
                "query_moves",
                {
                    "character": "rashid",
                    "field": "on_block",
                    "operator": "gt",
                    "value": 0,
                    "perspective": "attacker",
                    "scope": "all",
                },
            )],
        )

    def test_only_explicit_single_move_not_found_is_alias_learnable(self) -> None:
        self.assertTrue(is_alias_learnable_result(
            "lookup_move",
            {
                "found": False,
                "resolution": {
                    "status": "not_found",
                    "reason": "move_not_found",
                },
            },
        ))
        self.assertFalse(is_alias_learnable_result(
            "query_moves",
            {"found": True, "count": 0},
        ))
        self.assertFalse(is_alias_learnable_result(
            "lookup_move",
            {"found": False, "error": "database unavailable"},
        ))
        self.assertFalse(is_alias_learnable_result(
            "lookup_move",
            {
                "found": False,
                "resolution": {
                    "status": "not_found",
                    "reason": "character_not_found",
                },
            },
        ))


if __name__ == "__main__":
    unittest.main()
