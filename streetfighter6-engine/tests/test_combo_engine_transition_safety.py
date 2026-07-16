"""Regression tests preventing generic chain flags from creating fake routes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.combo_engine import MoveNode, find_max_combo  # noqa: E402


def node(input_: str, startup: int) -> MoveNode:
    return MoveNode(
        input=input_,
        name=input_,
        move_type="ground_normal",
        startup_f=startup,
        hit_adv_f=None,
        hit_is_knockdown=True,
        damage=1000,
        dr_cancelable=False,
        after_dr_hit_f=None,
    )


class ComboEngineTransitionSafetyTest(unittest.TestCase):
    def test_chain_flag_does_not_make_any_fast_normal_a_free_followup(self) -> None:
        result = find_max_combo(
            [node("5LP", 4)],
            initial_adv=1,
            starter_input="2LP",
            first_step_chain=True,
        )

        self.assertIsNone(result)

    def test_normal_frame_link_still_works(self) -> None:
        result = find_max_combo(
            [node("5LP", 4)],
            initial_adv=4,
            starter_input="2LP",
            first_step_chain=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.steps[0].adv_before, 4)


if __name__ == "__main__":
    unittest.main()
