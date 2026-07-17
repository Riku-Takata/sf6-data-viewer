"""Tests for broad matchup interruption overviews without a live database."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.matchup_interrupt import analyze_matchup_interrupt_overview  # noqa: E402
from sf6_engine.sequence_analysis import MoveInteractionProfile  # noqa: E402


def _profile(
    input_: str,
    name: str,
    startup: int,
    *,
    character: str = "Kimberly",
    move_type: str = "ground_normal",
) -> MoveInteractionProfile:
    return MoveInteractionProfile(
        character=character,
        input=input_,
        name=name,
        move_type=move_type,
        startup_f=startup,
        active_f=None,
        recovery_f=None,
        on_block_f=None,
        on_hit_f=None,
        hitstun_f=None,
        blockstun_f=None,
        hitstop_f=None,
        atk_range=None,
        notes=None,
    )


def _timeline(opener: str, followup: str, gap: int) -> dict:
    return {
        "opener": _profile(opener, opener, 5, character="Ryu"),
        "followup": _profile(followup, followup, 9, character="Ryu", move_type="Special"),
        "timeline": {"actionable_gap_f": gap},
    }


class MatchupInterruptOverviewTest(unittest.TestCase):
    def test_mcp_tool_delegates_to_the_service_without_recursion(self) -> None:
        from sf6_engine.mcp_server import server

        expected = {"found": True, "summary": "ok"}
        with patch(
            "sf6_engine.mcp_server.server.analyze_matchup_interrupt_overview_data",
            return_value=expected,
        ):
            result = server.analyze_matchup_interrupt_overview("ryu", "kimberly")

        self.assertEqual(result, expected)

    def test_candidates_are_selected_from_every_discovered_cancel(self) -> None:
        profiles = [_profile("2LP", "Crouching Light Punch", 2), _profile("5LP", "Standing Light Punch", 3)]
        timelines = [_timeline("5LP", "214LP", 3), _timeline("2MK", "236LP", 0)]
        with patch("sf6_engine.matchup_interrupt.list_ground_normal_profiles", return_value=profiles), patch(
            "sf6_engine.matchup_interrupt.enumerate_special_cancel_timelines", return_value=timelines
        ):
            result = analyze_matchup_interrupt_overview("ryu", "kimberly")

        self.assertTrue(result["found"])
        self.assertEqual(result["scanned_pair_count"], 2)
        self.assertEqual(result["interruptible_count"], 1)
        self.assertEqual(result["sequences"][0]["timing_candidates"], [
            {"input": "2LP", "name": "Crouching Light Punch", "startup_f": 2},
        ])
        self.assertIn("2LP、発生2F", result["summary"])
        self.assertIn("全件比較", result["summary"])

    def test_fastest_normal_is_reported_when_no_candidate_exists(self) -> None:
        profiles = [_profile("2LP", "Crouching Light Punch", 4)]
        with patch("sf6_engine.matchup_interrupt.list_ground_normal_profiles", return_value=profiles), patch(
            "sf6_engine.matchup_interrupt.enumerate_special_cancel_timelines", return_value=[_timeline("5LP", "214LP", 3)]
        ):
            result = analyze_matchup_interrupt_overview("ryu", "kimberly")

        self.assertIn("いいえ", result["summary"])
        self.assertIn("間に合う連携はありません", result["summary"])
        self.assertEqual(result["selection_scope"], "all_scalar_ground_normal_to_standard_special_cancels")


if __name__ == "__main__":
    unittest.main()
