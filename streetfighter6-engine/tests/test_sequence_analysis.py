"""Multi-move pressure, trade and confirmed-followup tests."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discord_bot.mcp_router import map_intent  # noqa: E402
from sf6_engine.importers.sequence_observations import (  # noqa: E402
    load_observations,
    validate_observation,
)
from sf6_engine.intent_parser import parse_intent  # noqa: E402
from sf6_engine.rag_builder import generate_answer  # noqa: E402
from sf6_engine.sequence_analysis import (  # noqa: E402
    MoveInteractionProfile,
    _apply_integrated_frame_profile,
    analyze_sequence,
    calculate_trade_advantage_from_hitstun,
    evaluate_sequence,
    load_bundled_sequence_observations,
    make_sequence_key,
)


class FailingProvider:
    async def generate_structured(self, *args, **kwargs) -> dict:
        raise AssertionError("sequence fast path must not call the LLM")

    async def generate(self, *args, **kwargs):
        raise AssertionError("sequence answer path must not call the LLM")


def move(
    character: str,
    input_: str,
    *,
    startup: int,
    block: int | None = None,
    hit: int | None = None,
    hitstun: int | None = None,
    hitstop: int | None = None,
    notes: str | None = None,
) -> MoveInteractionProfile:
    return MoveInteractionProfile(
        character=character,
        input=input_,
        name=input_,
        move_type="ground_normal",
        startup_f=startup,
        active_f=2,
        recovery_f=10,
        on_block_f=block,
        on_hit_f=hit,
        hitstun_f=hitstun,
        blockstun_f=None,
        hitstop_f=hitstop,
        atk_range=None,
        notes=notes,
    )


SAGAT_5MP = move(
    "Sagat",
    "5MP",
    startup=6,
    block=2,
    hit=6,
    hitstun=25,
    hitstop=11,
    notes="5MPx2 sets up a trade combo vs. most reversal 4f normals",
)
SAGAT_2MP = move("Sagat", "2MP", startup=7, hitstun=23, hitstop=11)
SAGAT_5LP = move("Sagat", "5LP", startup=5, hitstun=16, hitstop=9)
DUMMY_4F_17 = move("Dummy", "2LP", startup=4, hitstun=17, hitstop=9)
DUMMY_4F_18 = move("Dummy", "5LK", startup=4, hitstun=18, hitstop=9)
RYU_2LP_15 = move("Ryu", "2LP", startup=4, hitstun=15, hitstop=9)


class SequenceIntentTest(unittest.TestCase):
    QUERY = (
        "サガットの連携で立ち中P→立ち中Pっていう連携があるんだけど、"
        "この連携は最速暴れ（発生4Fの技をガード後に最速で出す）すると"
        "相打ちになってどっちが有利なの？"
    )

    def test_golden_query_is_structured_without_llm(self) -> None:
        intent = asyncio.run(parse_intent(self.QUERY, FailingProvider()))

        self.assertEqual(intent["intent_type"], "sequence_analysis")
        self.assertEqual(intent["chara"], "Sagat")
        self.assertEqual(intent["attacker_sequence"], ["5MP", "5MP"])
        self.assertEqual(intent["attacker_timing"]["delay_f"], 0)
        self.assertEqual(intent["initial_interaction"], "block")
        self.assertEqual(intent["defender_action"]["startup_f"], 4)
        self.assertEqual(intent["defender_action"]["delay_f"], 0)
        self.assertEqual(intent["expected_outcome"], "trade")

    def test_sequence_intent_routes_to_dedicated_mcp_tool(self) -> None:
        intent = asyncio.run(parse_intent(self.QUERY, FailingProvider()))

        self.assertEqual(map_intent(intent), [(
            "analyze_sequence",
            {
                "character": "sagat",
                "attacker_sequence": ["5MP", "5MP"],
                "initial_interaction": "block",
                "attacker_delay_f": 0,
                "defender_startup_f": 4,
                "defender_delay_f": 0,
                "expected_outcome": "trade",
            },
        )])

    def test_attacker_and_exact_defender_are_resolved_from_positions(self) -> None:
        query = (
            "リュウ相手にサガットの5MP→5MPを連携にして、"
            "リュウの2LPで最速暴れした相打ち後はどっちが有利？"
        )

        intent = asyncio.run(parse_intent(query, FailingProvider()))

        self.assertEqual(intent["chara"], "Sagat")
        self.assertEqual(intent["attacker_sequence"], ["5MP", "5MP"])
        self.assertEqual(intent["initial_interaction"], "block")
        self.assertEqual(intent["defender_action"]["character"], "Ryu")
        self.assertEqual(intent["defender_action"]["move"], "2LP")
        self.assertEqual(map_intent(intent)[0][1]["defender_character"], "ryu")
        self.assertEqual(map_intent(intent)[0][1]["defender_move"], "2LP")

    def test_numeric_and_unspecified_delays_are_not_flattened_to_earliest(self) -> None:
        numeric = asyncio.run(parse_intent(
            "サガットの5MP→3F遅らせ5MPの連携に、発生4Fで最速暴れしたら？",
            FailingProvider(),
        ))
        unspecified = asyncio.run(parse_intent(
            "サガットの5MP→ディレイ5MPの連携に、発生4Fで最速暴れしたら？",
            FailingProvider(),
        ))

        self.assertEqual(numeric["attacker_timing"]["delay_f"], 3)
        self.assertEqual(map_intent(numeric)[0][1]["attacker_delay_f"], 3)
        self.assertIsNone(unspecified["attacker_timing"]["delay_f"])
        self.assertIsNone(map_intent(unspecified)[0][1]["attacker_delay_f"])

        unresolved = analyze_sequence(
            "sagat",
            ["5MP", "5MP"],
            defender_startup_f=4,
            attacker_delay_f=None,
        )
        self.assertEqual(unresolved["status"], "delay_unspecified")

    def test_defender_delay_is_separate_from_attacker_delay(self) -> None:
        intent = asyncio.run(parse_intent(
            "サガットの5MP→5MPの連携に、3F遅らせて発生4Fで暴れたら？",
            FailingProvider(),
        ))

        self.assertEqual(intent["attacker_timing"]["delay_f"], 0)
        self.assertEqual(intent["defender_action"]["delay_f"], 3)

    def test_defender_move_requires_character(self) -> None:
        result = analyze_sequence(
            "sagat",
            ["5MP", "5MP"],
            defender_move="2LP",
        )

        self.assertEqual(result["status"], "defender_character_required")


class TradeTimelineTest(unittest.TestCase):
    def test_integrated_profile_overrides_sc_core_but_keeps_sc_hitstun(self) -> None:
        resolved = {
            "found": True,
            "resolution": {"usable_for_calculation": True},
            "move": {
                "input": "5MP",
                "move_name": "立ち中P（タイガーソーク）",
                "move_type": "ground_normal",
                "startup": 6,
                "active": 3,
                "recovery": 14,
                "on_block": 2,
                "on_hit": 6,
                "frame_profile": {
                    "facts": {
                        "startup": {"source_label": "CAPCOM公式"},
                        "active": {"source_label": "CAPCOM公式"},
                        "recovery": {"source_label": "UFD"},
                        "on_block": {"source_label": "CAPCOM公式"},
                        "on_hit": {"source_label": "CAPCOM公式"},
                    }
                },
            },
        }

        merged = _apply_integrated_frame_profile(SAGAT_5MP, resolved)

        self.assertEqual(merged.active_f, 3)
        self.assertEqual(merged.recovery_f, 14)
        self.assertEqual(merged.hitstun_f, 25)
        self.assertEqual(merged.hitstop_f, 11)
        self.assertEqual(merged.frame_sources["startup"], "CAPCOM公式")

    def test_plus_two_six_frame_move_ties_four_frame_reversal(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17],
            followup_profiles=[SAGAT_2MP, SAGAT_5LP],
            expected_outcome="trade",
            observations=[],
        )

        timeline = result["timeline"]
        self.assertEqual(timeline["attacker_first_active_frame"], 6)
        self.assertEqual(timeline["defender_first_active_frame"], 6)
        self.assertEqual(timeline["timing_outcome"], "simultaneous")
        self.assertEqual(result["collision"]["outcome"], "trade_if_both_reach")

    def test_one_frame_attacker_delay_makes_four_frame_reversal_win_timing(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17],
            followup_profiles=[SAGAT_2MP],
            expected_outcome=None,
            observations=[],
            attacker_delay_f=1,
            defender_delay_f=0,
        )

        self.assertEqual(result["timeline"]["attacker_first_active_frame"], 7)
        self.assertEqual(result["timeline"]["defender_first_active_frame"], 6)
        self.assertEqual(result["timeline"]["timing_outcome"], "defender_first")
        self.assertIn("攻撃側は7F目、防御側は6F目", result["summary"])
        self.assertIn("相打ち後の有利差は算出しません", result["summary"])

    def test_trade_advantage_uses_hitstun_difference_and_frame_convention(self) -> None:
        self.assertEqual(
            calculate_trade_advantage_from_hitstun(SAGAT_5MP, DUMMY_4F_17),
            7,
        )
        self.assertEqual(
            calculate_trade_advantage_from_hitstun(SAGAT_5MP, RYU_2LP_15),
            9,
        )

    def test_exact_defender_profile_derives_plus_seven(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17],
            followup_profiles=[SAGAT_2MP],
            expected_outcome="trade",
            observations=[],
            exact_defender_requested=True,
        )

        post = result["post_interaction"]
        self.assertEqual(post["status"], "derived_exact")
        self.assertEqual(post["attacker_advantage_f"], 7)
        self.assertEqual(post["defender_advantage_f"], -7)
        self.assertIn("フレーム上の追撃候補は2MP(発生7F、猶予0F)", result["summary"])
        self.assertIn("連続ヒット確定とは扱いません", result["summary"])

    def test_exact_ryu_2lp_derives_plus_nine_and_two_frame_2mp_window(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[RYU_2LP_15],
            followup_profiles=[SAGAT_2MP],
            expected_outcome="trade",
            observations=[],
            exact_defender_requested=True,
            defender_character_slug="ryu",
            defender_move_input="2LP",
        )

        post = result["post_interaction"]
        self.assertEqual(post["attacker_advantage_f"], 9)
        self.assertEqual(post["defender_advantage_f"], -9)
        self.assertEqual(
            post["derived_profiles"][0]["calculation_expression"],
            "25 - 15 - 1 = 9",
        )
        candidate = result["followups"]["timing_candidates"][0]
        self.assertEqual(candidate["input"], "2MP")
        self.assertEqual(candidate["leniency_f"], 2)
        self.assertFalse(candidate["combo_confirmed"])
        self.assertIn("攻撃側が+9F、防御側が-9F", result["summary"])
        self.assertIn("5MPのhitstun 25", result["summary"])
        self.assertIn("Ryu 2LPのhitstun 15", result["summary"])
        self.assertIn("= +9", result["summary"])
        self.assertIn("2MP(発生7F、猶予2F)", result["summary"])

    def test_unspecified_four_frame_move_preserves_result_range(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17, DUMMY_4F_18, RYU_2LP_15],
            followup_profiles=[SAGAT_2MP, SAGAT_5LP],
            expected_outcome="trade",
            observations=[],
        )

        post = result["post_interaction"]
        self.assertEqual(post["status"], "derived_interval")
        self.assertEqual((post["min_f"], post["max_f"]), (6, 9))
        self.assertIsNone(post["attacker_advantage_f"])
        candidate = next(
            item for item in result["followups"]["timing_candidates"]
            if item["input"] == "2MP"
        )
        self.assertEqual(candidate["timing_status"], "timing_connected_for_some_profiles")
        self.assertEqual(candidate["timing_connected_profile_count"], 2)
        self.assertEqual(candidate["timing_total_profile_count"], 3)
        self.assertFalse(candidate["combo_confirmed"])
        self.assertIn("相手の技が未指定のため単一値にはできません", result["summary"])
        self.assertIn("+9F: 1技（Ryu 2LP）", result["summary"])
        self.assertIn("2MP(発生7F: 2/3技)", result["summary"])

    def test_unspecified_single_profile_still_does_not_claim_exact_move(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17],
            followup_profiles=[SAGAT_2MP],
            expected_outcome="trade",
            observations=[],
        )

        post = result["post_interaction"]
        self.assertEqual(post["status"], "derived_profile_set")
        self.assertIsNone(post["attacker_advantage_f"])
        self.assertEqual((post["min_f"], post["max_f"]), (7, 7))

    def test_stale_observation_is_rejected_when_frame_fingerprint_changes(self) -> None:
        changed_opener = move(
            "Sagat",
            "5MP",
            startup=6,
            block=1,
            hit=6,
            hitstun=25,
            hitstop=11,
        )

        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[changed_opener, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17],
            followup_profiles=[SAGAT_2MP],
            expected_outcome="trade",
            observations=[{
                **load_bundled_sequence_observations()[0],
                "reviewed": True,
            }],
        )

        self.assertEqual(result["timeline"]["timing_outcome"], "defender_first")
        self.assertEqual(result["post_interaction"]["status"], "unresolved")
        self.assertIsNone(result["evidence"]["reviewed_observation"])

    def test_character_specific_request_does_not_reuse_generic_observation(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17, DUMMY_4F_18],
            followup_profiles=[SAGAT_2MP],
            expected_outcome="trade",
            observations=load_bundled_sequence_observations(),
            defender_character_slug="ryu",
        )

        self.assertEqual(result["post_interaction"]["status"], "derived_interval")
        self.assertIsNone(result["evidence"]["reviewed_observation"])


class GoldenObservationTest(unittest.TestCase):
    def test_bundled_observation_is_retained_as_incomplete_import_data(self) -> None:
        loaded = load_observations()

        self.assertEqual(len(loaded), 1)
        self.assertIsNone(loaded[0]["attacker_advantage_f"])
        self.assertFalse(loaded[0]["reviewed"])
        self.assertEqual(
            loaded[0]["result_state"]["reported_attacker_advantage_f"],
            7,
        )

    def test_reviewed_exact_result_requires_defender_identity(self) -> None:
        raw = dict(load_bundled_sequence_observations()[0])
        raw.update({
            "reviewed": True,
            "attacker_advantage_f": 7,
            "defender_advantage_f": -7,
        })

        with self.assertRaisesRegex(ValueError, "require exact defender"):
            validate_observation(raw)

    def test_incomplete_generic_observation_never_overrides_derived_values(self) -> None:
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[DUMMY_4F_17, DUMMY_4F_18],
            followup_profiles=[SAGAT_2MP, SAGAT_5LP],
            expected_outcome="trade",
            observations=load_bundled_sequence_observations(),
        )

        post = result["post_interaction"]
        self.assertEqual(post["status"], "derived_interval")
        self.assertEqual((post["min_f"], post["max_f"]), (6, 7))
        self.assertIsNone(post["attacker_advantage_f"])
        self.assertEqual(result["followups"]["confirmed"], [])
        self.assertIsNone(result["evidence"]["reviewed_observation"])

    def test_exact_reviewed_result_applies_only_to_matching_defender_move(self) -> None:
        observation = {
            "observation_key": make_sequence_key(
                "sagat",
                ["5MP", "5MP"],
                "block",
                4,
                "trade",
                defender_character_slug="ryu",
                defender_move_input="2LP",
            ),
            "attacker_character_slug": "sagat",
            "attacker_sequence": [
                {"input": "5MP", "interaction": "block"},
                {"input": "5MP", "timing": "earliest", "delay_f": 0},
            ],
            "initial_interaction": "block",
            "defender_character_slug": "ryu",
            "defender_move_input": "2LP",
            "defender_profile": {
                "startup_f": 4,
                "timing": "earliest",
                "delay_f": 0,
            },
            "outcome": "trade",
            "attacker_advantage_f": 9,
            "defender_advantage_f": -9,
            "confirmed_followups": [{
                "input": "2MP",
                "combo_confirmed": True,
                "spatial_connected": True,
                "state_connected": True,
                "evidence": "unit-test fixture",
            }],
            "conditions": {},
            "source": "unit_test",
            "patch_version": "test",
            "confidence": 1.0,
            "reviewed": True,
        }
        result = evaluate_sequence(
            character_slug="sagat",
            sc_character="Sagat",
            attacker_moves=[SAGAT_5MP, SAGAT_5MP],
            initial_interaction="block",
            defender_startup_f=4,
            defender_profiles=[RYU_2LP_15],
            followup_profiles=[SAGAT_2MP],
            expected_outcome="trade",
            observations=[observation],
            exact_defender_requested=True,
            defender_character_slug="ryu",
            defender_move_input="2LP",
        )

        self.assertEqual(result["post_interaction"]["status"], "observed_exact")
        self.assertEqual(result["post_interaction"]["attacker_advantage_f"], 9)
        self.assertTrue(result["followups"]["confirmed"][0]["combo_confirmed"])

    def test_sequence_summary_bypasses_answer_llm(self) -> None:
        context = (
            "【Sagat / 5MP -> 5MP 連携解析】\n"
            "検証済み観測では相打ち後、攻撃側が+7F、防御側が-7Fです。\n"
            "確認済み追撃は2MP（発生7F）です。"
        )

        answer = asyncio.run(generate_answer("5MP→5MPの相打ち後は？", context, FailingProvider()))

        self.assertEqual(answer, context)


if __name__ == "__main__":
    unittest.main()
