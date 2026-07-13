"""Multi-source frame profile and perspective tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.frame_data import (  # noqa: E402
    _best_named_row,
    _inputs_match,
    _invert_advantage,
    _parse_capcom_active,
    _parse_frame_value,
    _parse_recovery,
    _parse_stage_sequence,
    _resolve_sc_variant_from_family,
    _section_compatible,
    format_frame_profile_context,
    lookup_frame_data,
)


CAPCOM_5HK = {
    "character_slug": "ken",
    "section": "通常技",
    "move_name": "立ち強K （一文字蹴り）",
    "startup": "12",
    "active": "12-13",
    "recovery": "※23",
    "on_hit": "1",
    "on_block": "-5",
    "cancel": "",
    "damage": "800",
    "note": "※空振り/アーマーヒット時硬直2F増加",
    "patch_date": "2026-05-28",
}

SC_5HK = {
    "input": "5HK",
    "name": "Standing Heavy Kick",
    "move_type": "ground_normal",
    "guard": "LH",
    "startup": "12",
    "active": "2",
    "recovery": "23(25)",
    "total": "36(38)",
    "hit_adv": "+1",
    "block_adv": "-5",
    "punish_adv": "KD +56 Spin",
    "perf_parry_adv": "-25",
    "damage": "800",
    "atk_range": "1.921 (1.898)",
    "invuln": None,
    "notes": "2 extra recovery frames on whiff",
    "imported_at": "2026-05-15T03:53:09+00:00",
}

UFD_5HK = {
    "category": "Normal Attacks",
    "move_name": "Standing Heavy Kick",
    "sc_input": "5HK",
    "input_sequence": None,
    "startup": "12",
    "total": "38",
    "damage": "800",
    "attack_type": "High",
    "cancellable": None,
    "notes": None,
    "hitbox_note": None,
    "on_hit": "+1",
    "on_block": "-5",
    "active": "2",
    "recovery": "25",
    "hitbox_source_url": "https://example.test/ken-5hk.gif",
    "hitbox_storage_path": "ken/5hk.gif",
    "source_url": "https://example.test/ken",
    "scraped_at": "2026-07-10T01:58:26+00:00",
}


class FrameParsingTest(unittest.TestCase):
    def test_neutral_jump_input_notation_is_equivalent(self) -> None:
        self.assertTrue(_inputs_match("nj.HK", "8HK"))
        self.assertTrue(_inputs_match("NJ.hp", "8HP"))
        self.assertFalse(_inputs_match("j.HK", "8HK"))

    def test_guard_not_applicable_is_not_a_missing_value(self) -> None:
        parsed = _parse_frame_value("N/A", advantage=True)
        self.assertEqual(parsed["semantic"], "not_applicable")
        self.assertEqual(parsed["display"], "対象外（ガード不成立）")
        self.assertEqual(_invert_advantage(parsed)["display"], "対象外（ガード不成立）")

    def test_variable_guard_value_is_preserved_for_both_perspectives(self) -> None:
        parsed = _parse_frame_value("varies", advantage=True)
        self.assertEqual(parsed["semantic"], "variable")
        self.assertEqual(parsed["display"], "状況依存（固定値なし）")
        self.assertEqual(_invert_advantage(parsed)["display"], "状況依存（固定値なし）")

    def test_missing_strength_variant_uses_mapped_family(self) -> None:
        capcom = {
            "section": "必殺技",
            "move_name": "強 サイコフリッカー",
            "startup": "17",
            "active": "17-24",
            "recovery": "18",
        }
        maps = [{
            "capcom_move_name": "弱 サイコフリッカー",
            "sc_input": "236LK",
            "sc_name": "Psycho Flicker",
        }]
        rows = [
            {"move_type": "Special", "input": "236LK", "name": "Psycho Flicker"},
            {"move_type": "Special", "input": "236HK", "name": "Psycho Flicker"},
        ]
        resolved = _resolve_sc_variant_from_family(capcom, maps, rows)
        self.assertEqual((resolved or {}).get("input"), "236HK")

    def test_exact_base_name_beats_parenthetical_variant(self) -> None:
        rows = [
            {"move_name": "強 スパイラルアロー（ホールド）"},
            {"move_name": "強 スパイラルアロー"},
        ]

        selected = _best_named_row(rows, "強 スパイラルアロー")

        self.assertEqual(selected["move_name"], "強 スパイラルアロー")

    def test_capcom_absolute_active_window_becomes_duration(self) -> None:
        parsed = _parse_capcom_active("12-13")

        self.assertEqual(parsed["value"], 2)
        self.assertEqual(parsed["display"], "2F")
        self.assertEqual(parsed["raw"], "12-13")

    def test_capcom_scalar_active_window_is_one_frame(self) -> None:
        parsed = _parse_capcom_active("7")

        self.assertEqual(parsed["value"], 1)
        self.assertEqual(parsed["display"], "1F")

    def test_capcom_summary_active_span_is_not_double_counted(self) -> None:
        parsed = _parse_capcom_active("13-38 13-15, 30-38")

        self.assertIsNone(parsed["value"])
        self.assertEqual(parsed["active_segments"], [3, 9])
        self.assertEqual(parsed["inactive_gaps"], [14])
        self.assertEqual(
            parsed["display"], "3F→空白14F→9F（複数持続区間）"
        )

    def test_composite_landing_recovery_is_not_flattened(self) -> None:
        parsed = _parse_recovery("24+着地後16")

        self.assertIsNone(parsed["value"])
        self.assertEqual(parsed["semantic"], "composite_recovery")
        self.assertEqual(parsed["display"], "24F＋着地後16F")

    def test_total_duration_in_recovery_column_is_not_recovery(self) -> None:
        parsed = _parse_recovery("全体 52")

        self.assertIsNone(parsed["value"])
        self.assertFalse(parsed["usable"])
        self.assertEqual(parsed["semantic"], "total_only")
        self.assertIn("全体52F", parsed["display"])

    def test_advantage_range_is_inverted_in_reverse_order(self) -> None:
        attacker = _parse_frame_value("-6~-4", advantage=True)
        defender = _invert_advantage(attacker)

        self.assertEqual(attacker["display"], "-6～-4F")
        self.assertEqual(defender["display"], "+4～+6F")

    def test_ufd_ellipsis_range_is_preserved_and_inverted(self) -> None:
        attacker = _parse_frame_value("-12...-2", advantage=True)
        defender = _invert_advantage(attacker)

        self.assertEqual(attacker["display"], "-12～-2F")
        self.assertEqual(defender["display"], "+2～+12F")

    def test_conditional_advantages_are_not_flattened(self) -> None:
        attacker = _parse_frame_value("-60※-93", advantage=True)
        defender = _invert_advantage(attacker)

        self.assertIsNone(attacker["value"])
        self.assertEqual(attacker["alternatives"], [-60, -93])
        self.assertEqual(attacker["display"], "-60F / -93F（条件別）")
        self.assertEqual(defender["display"], "+60F / +93F（条件別）")

    def test_target_combo_advantages_are_labeled_per_stage(self) -> None:
        attacker = _parse_stage_sequence("-3, -14, --", field="on_block")
        defender = _invert_advantage(attacker)

        self.assertEqual(attacker["semantic"], "stage_sequence")
        self.assertEqual(
            attacker["display"],
            "1段目: -3F / 2段目: -14F / 3段目: 対象外（ガード不成立）",
        )
        self.assertEqual(
            defender["display"],
            "1段目: +3F / 2段目: +14F / 3段目: 対象外（ガード不成立）",
        )

    def test_signature_category_rejects_throw_to_target_combo(self) -> None:
        capcom_throw = {"section": "通常投げ"}
        target_combo = {"move_type": "ground_normal"}
        sc_throw = {"move_type": "throw"}

        self.assertFalse(_section_compatible(capcom_throw, target_combo))
        self.assertTrue(_section_compatible(capcom_throw, sc_throw))


class FrameProfileTest(unittest.TestCase):
    def lookup(self, query: str) -> dict:
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
            return lookup_frame_data("ken", query, client=object())

    def test_official_name_and_input_share_the_same_selected_profile(self) -> None:
        by_name = self.lookup("立ち強K （一文字蹴り）")
        by_input = self.lookup("5HK")

        for result in (by_name, by_input):
            move = result["move"]
            self.assertEqual(move["startup"], 12)
            self.assertEqual(move["active"], 2)
            self.assertEqual(move["recovery"], 23)
            self.assertEqual(move["on_block"], -5)
            self.assertEqual(move["on_block_defender"], 5)
            self.assertEqual(
                move["frame_profile"]["facts"]["recovery"]["source"], "capcom"
            )
            self.assertTrue(move["frame_profile"]["facts"]["recovery"]["conflict"])
            block_sources = {
                observation["source"]
                for observation in move["frame_profile"]["facts"]["on_block"]["observations"]
            }
            hit_sources = {
                observation["source"]
                for observation in move["frame_profile"]["facts"]["on_hit"]["observations"]
            }
            self.assertEqual(block_sources, {"capcom", "ufd", "supercombo"})
            self.assertEqual(hit_sources, {"capcom", "ufd", "supercombo"})

        self.assertEqual(
            by_name["move"]["frame_profile"]["facts"],
            by_input["move"]["frame_profile"]["facts"],
        )

    def test_context_exposes_both_guard_perspectives_and_source_difference(self) -> None:
        result = self.lookup("5HK")
        context = format_frame_profile_context(result["move"]["frame_profile"])

        self.assertIn("発生: 12F [採用: CAPCOM公式]", context)
        self.assertIn("持続: 2F [採用: CAPCOM公式]", context)
        self.assertIn("硬直: 23F（条件付き） [採用: CAPCOM公式]", context)
        self.assertIn("ガード時（攻撃側・ガードさせた側）: -5F", context)
        self.assertIn("ガード時（防御側・ガードした側）: +5F", context)
        self.assertIn("【ソース差異:硬直】", context)
        self.assertIn("CAPCOM公式注記", context)


if __name__ == "__main__":
    unittest.main()
