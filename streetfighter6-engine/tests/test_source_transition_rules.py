"""Tests for the all-character SuperCombo transition-rule review queue."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.importers.source_transition_rules import (  # noqa: E402
    build_transition_candidates,
    reviewed_rule_candidates,
)


class SourceTransitionRuleImporterTest(unittest.TestCase):
    def test_direct_gap_note_becomes_one_unreviewed_review_candidate(self) -> None:
        payload = {
            "data": {
                "A.K.I.": [
                    {"input": "5LP", "moveType": "ground_normal", "cancel": "Chn"},
                    {
                        "input": "5LP~LP",
                        "moveType": "ground_normal",
                        "notes": "3f blockstring gap between hits",
                    },
                ],
            },
        }

        candidates = build_transition_candidates(payload)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["source_input"], "5LP")
        self.assertEqual(candidate["transition_type"], "chain")
        self.assertEqual(candidate["candidate_status"], "direct_evidence_ready_for_review")
        self.assertEqual(candidate["gap_min_f"], 3)
        rows = reviewed_rule_candidates(
            candidates,
            character_slug_by_sc_name={"a.k.i.": "a_ki"},
            patch_version="2026-04-26",
        )
        self.assertEqual(rows[0]["timing_basis"], "direct_block_gap")
        self.assertFalse(rows[0]["reviewed"])

    def test_strength_specific_note_does_not_leak_to_other_strengths(self) -> None:
        payload = {
            "data": {
                "Ken": [
                    {"input": "236MK", "moveType": "Special", "cancel": "-"},
                    {"input": "236HK", "moveType": "Special", "cancel": "-"},
                    {
                        "input": "236K~6HK",
                        "moveType": "Special",
                        "notes": "True blockstring from 236HK.",
                    },
                ],
            },
        }

        candidates = build_transition_candidates(payload)
        by_source = {candidate["source_input"]: candidate for candidate in candidates}

        self.assertEqual(
            by_source["236HK"]["candidate_status"],
            "direct_evidence_ready_for_review",
        )
        self.assertEqual(by_source["236HK"]["gap_max_f"], 0)
        self.assertEqual(by_source["236MK"]["candidate_status"], "needs_timing_review")

    def test_branch_without_direct_note_stays_in_review_queue(self) -> None:
        payload = {
            "data": {
                "Ken": [
                    {"input": "236MK", "moveType": "Special", "cancel": "-"},
                    {
                        "input": "236K~6LK",
                        "moveType": "Special",
                        "notes": "Follow-up can be input on hit/block/whiff.",
                    },
                ],
            },
        }

        candidate = build_transition_candidates(payload)[0]

        self.assertEqual(candidate["candidate_status"], "needs_timing_review")
        self.assertIn("composite_transition_timing_rule_missing", candidate["reason_codes"])

    def test_conflicting_duplicate_rows_are_not_auto_staged(self) -> None:
        payload = {
            "data": {
                "A.K.I.": [
                    {"input": "5LP", "moveType": "ground_normal", "cancel": "Chn"},
                    {
                        "input": "5LP~LP",
                        "moveType": "ground_normal",
                        "notes": "3f blockstring gap between hits.",
                    },
                    {
                        "input": "5LP~LP",
                        "moveType": "ground_normal",
                        "notes": "4f blockstring gap between hits.",
                    },
                ],
            },
        }

        candidate = build_transition_candidates(payload)[0]

        self.assertEqual(candidate["candidate_status"], "conflicting_source_evidence")
        self.assertEqual(candidate["reason_codes"], ["conflicting_direct_gap_values"])

    def test_state_conditional_gap_is_left_for_review(self) -> None:
        payload = {
            "data": {
                "A.K.I.": [
                    {"input": "5LP", "moveType": "ground_normal", "cancel": "Chn"},
                    {
                        "input": "5LP~LP",
                        "moveType": "ground_normal",
                        "notes": "2f blockstring gap on block if the opponent is crouching.",
                    },
                ],
            },
        }

        candidate = build_transition_candidates(payload)[0]

        self.assertEqual(candidate["candidate_status"], "needs_timing_review")


if __name__ == "__main__":
    unittest.main()
