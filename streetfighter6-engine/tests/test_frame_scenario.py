"""Context-sensitive frame parsing and punish-safety tests."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.frame_data import lookup_frame_data  # noqa: E402
from sf6_engine.frame_scenario import parse_frame_scenario  # noqa: E402
from sf6_engine.intent_parser import parse_intent  # noqa: E402
from sf6_engine.punish import filter_timing_candidates  # noqa: E402
from sf6_engine.punish_service import check_punish_data  # noqa: E402
from tests.test_frame_data import CAPCOM_5HK, SC_5HK, UFD_5HK  # noqa: E402


class FailingProvider:
    async def generate_structured(self, *args, **kwargs) -> dict:
        raise AssertionError("deterministic fast path should not call provider")


class ScenarioParserTest(unittest.TestCase):
    def test_distance_is_removed_from_move_name_and_kept_as_condition(self) -> None:
        intent = asyncio.run(
            parse_intent("ケンの先端で大Kをガードしたら何F？", FailingProvider())
        )

        self.assertEqual(intent["input"], "5HK")
        self.assertEqual(intent["scenario"]["distance"], "tip")
        self.assertEqual(intent["scenario"]["interaction"], "block")
        self.assertEqual(intent["scenario"]["perspective"], "defender")

    def test_last_active_and_attacker_perspective_are_explicit(self) -> None:
        scenario = parse_frame_scenario(
            "ケンの大Kの最終持続をガードさせたら何F？"
        )

        self.assertEqual(scenario["contact_timing"], "last_active")
        self.assertEqual(scenario["perspective"], "attacker")

    def test_unspecified_burnout_actor_requires_clarification(self) -> None:
        scenario = parse_frame_scenario("バーンアウト中にケンの大Kをガードしたら？")

        self.assertNotIn("defender_burnout", scenario)
        self.assertEqual(scenario["ambiguities"][0]["field"], "burnout_actor")

    def test_parenthetical_stage_in_official_move_name_is_preserved(self) -> None:
        intent = asyncio.run(
            parse_intent("リュウの不破三連撃（2段目）の発生は？", FailingProvider())
        )

        self.assertEqual(intent["move_name"], "不破三連撃（2段目）")
        self.assertNotIn("stage_index", intent.get("scenario") or {})

    def test_distance_variant_in_official_move_name_is_not_a_scenario(self) -> None:
        intent = asyncio.run(
            parse_intent(
                "ザンギエフのシベリアンエクスプレス（遠距離版）の発生は？",
                FailingProvider(),
            )
        )

        self.assertEqual(
            intent["move_name"], "シベリアンエクスプレス（遠距離版）"
        )
        self.assertNotIn("distance", intent.get("scenario") or {})


class ContextualFrameTest(unittest.TestCase):
    def lookup(self, scenario: dict) -> dict:
        rows = {
            "capcom": [CAPCOM_5HK],
            "maps": [],
            "sc": [SC_5HK],
            "ufd": [UFD_5HK],
        }
        with (
            patch("sf6_engine.frame_data._resolve_character", return_value=("ken", "Ken")),
            patch("sf6_engine.frame_data._all_character_rows", return_value=rows),
        ):
            return lookup_frame_data("ken", "5HK", scenario=scenario, client=object())

    def test_last_active_derives_both_block_perspectives(self) -> None:
        result = self.lookup({
            "contact_timing": "last_active",
            "specified": ["contact_timing"],
        })
        evaluation = result["move"]["scenario_evaluation"]

        self.assertEqual(evaluation["block_perspectives"]["attacker"]["value"], -4)
        self.assertEqual(evaluation["block_perspectives"]["defender"]["value"], 4)
        self.assertEqual(
            evaluation["block_perspectives"]["attacker"]["status"],
            "derived_exact",
        )

    def test_tip_without_contact_frame_is_an_interval(self) -> None:
        result = self.lookup({"distance": "tip", "specified": ["distance"]})
        contextual = result["move"]["scenario_evaluation"]["contextual_facts"]["on_block"]

        self.assertEqual(contextual["status"], "derived_interval")
        self.assertEqual((contextual["min"], contextual["max"]), (-5, -4))
        self.assertFalse(contextual["usable_for_calculation"])

    def test_burnout_is_not_silently_ignored_without_structured_rule(self) -> None:
        result = self.lookup({
            "defender_burnout": True,
            "specified": ["defender_burnout"],
        })
        contextual = result["move"]["scenario_evaluation"]["contextual_facts"]["on_block"]

        self.assertEqual(contextual["status"], "conditional_unresolved")
        self.assertIsNone(contextual["value"])
        self.assertIn(
            "system_rule:defender_burnout_blockstun_modifier",
            contextual["required_data"],
        )

    def test_conditional_reference_values_are_inverted_but_not_calculated(self) -> None:
        rows = {
            "capcom": [{**CAPCOM_5HK, "on_block": "-1※-11"}],
            "maps": [],
            "sc": [SC_5HK],
            "ufd": [UFD_5HK],
        }
        scenario = {
            "interaction": "block",
            "perspective": "defender",
            "specified": ["interaction", "perspective"],
        }
        with (
            patch("sf6_engine.frame_data._resolve_character", return_value=("ken", "Ken")),
            patch("sf6_engine.frame_data._all_character_rows", return_value=rows),
        ):
            result = lookup_frame_data(
                "ken", "5HK", scenario=scenario, client=object()
            )

        evaluation = result["move"]["scenario_evaluation"]
        attacker = evaluation["block_perspectives"]["attacker"]
        defender = evaluation["block_perspectives"]["defender"]
        self.assertEqual(attacker["display"], "-1F / -11F（条件別）")
        self.assertEqual(defender["display"], "+1F / +11F（条件別）")
        self.assertFalse(attacker["usable_for_calculation"])
        self.assertIsNone(evaluation["punish_assessment"]["punish_window_f"])

    def test_family_name_with_multiple_strengths_is_not_used_for_calculation(self) -> None:
        rows = {
            "capcom": [
                {"section": "必殺技", "move_name": "弱 波動拳", "startup": "16", "active": "16-45", "recovery": "45", "on_block": "-5"},
                {"section": "必殺技", "move_name": "強 波動拳", "startup": "12", "active": "12-41", "recovery": "45", "on_block": "-5"},
            ],
            "maps": [
                {"capcom_move_name": "弱 波動拳", "sc_input": "236LP"},
                {"capcom_move_name": "強 波動拳", "sc_input": "236HP"},
            ],
            "sc": [
                {"input": "236LP", "name": "LP Hadoken", "move_type": "special", "startup": "16", "block_adv": "-5"},
                {"input": "236HP", "name": "HP Hadoken", "move_type": "special", "startup": "12", "block_adv": "-5"},
            ],
            "ufd": [],
        }
        with (
            patch("sf6_engine.frame_data._resolve_character", return_value=("ryu", "Ryu")),
            patch("sf6_engine.frame_data._all_character_rows", return_value=rows),
        ):
            result = lookup_frame_data("ryu", "波動拳", client=object())

        self.assertEqual(result["resolution"]["status"], "ambiguous")
        self.assertTrue(result["requires_clarification"])
        self.assertEqual(
            result["move"]["scenario_evaluation"]["contextual_facts"]["on_block"]["status"],
            "move_ambiguous",
        )


class PunishSafetyTest(unittest.TestCase):
    def test_non_neutral_moves_are_removed_from_timing_candidates(self) -> None:
        rows = [
            {"move_name": "Jump LP", "sc_input_key": "j.LP", "c_startup": 4, "section": "通常技"},
            {"move_name": "Chain end", "sc_input_key": "5LP~LP", "c_startup": 4, "section": "特殊技"},
            {"move_name": "Standing LP", "sc_input_key": "5LP", "c_startup": 4, "section": "通常技"},
            {"move_name": "Shoryuken", "sc_input_key": "623LP", "c_startup": 5, "section": "必殺技"},
        ]

        candidates = filter_timing_candidates(rows)

        self.assertEqual([candidate["input"] for candidate in candidates], ["5LP", "623LP"])
        self.assertTrue(all(candidate["confirmed_punish"] is None for candidate in candidates))

    def test_frame_window_does_not_claim_confirmed_punish_without_reach(self) -> None:
        profile = {
            "resolution": {"status": "resolved"},
            "facts": {"on_block": {"source_label": "CAPCOM公式"}},
            "block_perspectives": {},
            "scenario_evaluation": {
                "scenario": {},
                "block_perspectives": {
                    "attacker": {
                        "value": -5,
                        "display": "-5F",
                        "usable_for_calculation": True,
                    },
                    "defender": {"value": 5, "display": "+5F"},
                },
                "punish_assessment": {
                    "status": "timing_only_spatial_unverified",
                    "frame_punishable": True,
                    "punish_window_f": 5,
                    "confirmed_punishable": None,
                },
            },
        }
        lookup = {
            "found": True,
            "move": {
                "move_name": "Standing Heavy Kick",
                "on_block": -5,
                "frame_profile": profile,
            },
        }
        with patch("sf6_engine.punish_service.lookup_frame_data", return_value=lookup):
            result = check_punish_data("ken", "5HK")

        self.assertTrue(result["frame_punishable"])
        self.assertIsNone(result["confirmed_punishable"])
        self.assertIsNone(result["punishable"])
        self.assertIn("確定反撃としての成立は未確定", result["summary"])


if __name__ == "__main__":
    unittest.main()
