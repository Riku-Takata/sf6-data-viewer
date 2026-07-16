"""Tests for family-level pressure analysis without a live database."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.pressure_family import analyze_pressure_family  # noqa: E402


def _analysis(target: str, startup: int, gap: int) -> dict:
    return {
        "found": True,
        "attacker_sequence": [
            {"input": "2MK", "startup_f": 7},
            {"input": target, "startup_f": startup},
        ],
        "timeline": {"actionable_gap_f": gap},
        "transition": {"status": "resolved"},
    }


class PressureFamilyAnalysisTest(unittest.TestCase):
    def test_reviewed_default_enumerates_normal_variants(self) -> None:
        lookup = {
            "found": True,
            "resolution": {
                "status": "ambiguous",
                "candidates": [
                    {"input": "236HK", "names": ["強 迅雷脚"]},
                    {"input": "236KK", "names": ["OD 迅雷脚"]},
                    {"input": "236LK", "names": ["弱 迅雷脚"]},
                    {"input": "236MK", "names": ["中 迅雷脚"]},
                ],
            },
        }
        outcomes = {
            "236LK": _analysis("236LK", 12, -4),
            "236MK": _analysis("236MK", 16, 0),
            "236HK": _analysis("236HK", 25, 9),
        }

        with patch("sf6_engine.pressure_family.lookup_frame_data", return_value=lookup), patch(
            "sf6_engine.pressure_family.analyze_sequence",
            side_effect=lambda _character, sequence, **_kwargs: outcomes[sequence[1]],
        ) as analyze:
            result = analyze_pressure_family("ken", "Jinrai Kick")

        self.assertTrue(result["found"])
        self.assertEqual(result["opener"], "2MK")
        self.assertEqual([item["input"] for item in result["variants"]], [
            "236LK", "236MK", "236HK",
        ])
        self.assertEqual(analyze.call_count, 3)
        self.assertIn("2MKから最速キャンセル", result["summary"])
        self.assertIn("連続ガード", result["summary"])
        self.assertIn("発生8F以下なら割り込め", result["summary"])

    def test_no_reviewed_default_asks_for_the_opener(self) -> None:
        result = analyze_pressure_family("ryu", "Hadoken")

        self.assertFalse(result["found"])
        self.assertEqual(result["status"], "opener_unspecified")
        self.assertIn("どの技から", result["message"])

    def test_all_scope_retains_overdrive(self) -> None:
        lookup = {
            "found": True,
            "resolution": {
                "candidates": [
                    {"input": "236LK", "names": ["弱 迅雷脚"]},
                    {"input": "236KK", "names": ["OD 迅雷脚"]},
                ],
            },
        }
        outcomes = {
            "236LK": _analysis("236LK", 12, -4),
            "236KK": _analysis("236KK", 13, -3),
        }
        with patch("sf6_engine.pressure_family.lookup_frame_data", return_value=lookup), patch(
            "sf6_engine.pressure_family.analyze_sequence",
            side_effect=lambda _character, sequence, **_kwargs: outcomes[sequence[1]],
        ):
            result = analyze_pressure_family(
                "ken", "Jinrai Kick", variant_scope="all"
            )

        self.assertEqual([item["input"] for item in result["variants"]], [
            "236LK", "236KK",
        ])


if __name__ == "__main__":
    unittest.main()
