"""Ultimate Frame Data のHTML抽出・SC入力変換・回答用整形テスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sf6_engine.importers.ultimate_frame_data import (  # noqa: E402
    UfdMove,
    _derive_page_sc_inputs,
    _sequence_to_sc_input,
    _source_identity,
    _to_row,
    parse_ufd_html,
)
from sf6_engine.ufd import format_ufd_details  # noqa: E402


_HTML = """
<h2 class="movecategory">Normal Attacks</h2><div class="moves">
  <div class="movecontainer">
    <div class="hitbox"><a class="hitboximg" data-featherlight="hitboxes/Ken/ken-st-hk.gif"></a></div>
    <div class="movename">Standing Heavy Kick</div><div class="startup">12</div>
    <div class="totalframes">38</div><div class="basedamage">800</div>
    <div class="attacktype">High</div><div class="cancellable">--</div>
    <div class="notes">--</div><div class="whichhitbox">--</div>
    <div class="onhit">+1</div><div class="onblock">-5</div>
    <div class="activeframes">2</div><div class="recovery">25</div>
  </div>
</div>
<h2 class="movecategory">Special Moves</h2><div class="moves">
  <div class="movecontainer">
    <div class="movename">Shoryuken (Light Punch)</div><div class="startup">5</div>
    <div class="inputsequence">Forward, Down, Down-Forward + Light Punch</div>
    <div class="totalframes">47</div><div class="onblock">-23</div>
    <div class="activeframes">10</div><div class="recovery">33</div>
  </div>
</div>
"""


class UltimateFrameDataParserTest(unittest.TestCase):
    def test_parses_fields_categories_and_gif_url(self) -> None:
        moves = parse_ufd_html(_HTML, "https://ultimateframedata.com/sf6/ken")

        self.assertEqual(len(moves), 2)
        normal, special = moves
        self.assertEqual(normal.category, "Normal Attacks")
        self.assertEqual(normal.move_name, "Standing Heavy Kick")
        self.assertEqual(normal.startup, "12")
        self.assertEqual(normal.active, "2")
        self.assertEqual(normal.recovery, "25")
        self.assertEqual(normal.on_block, "-5")
        self.assertEqual(
            normal.hitbox_url,
            "https://ultimateframedata.com/sf6/hitboxes/Ken/ken-st-hk.gif",
        )
        self.assertEqual(special.category, "Special Moves")
        self.assertEqual(special.input_sequence, "Forward, Down, Down-Forward + Light Punch")

    def test_normal_and_special_inputs_are_normalized(self) -> None:
        self.assertEqual(_sequence_to_sc_input(None, "Standing Heavy Kick"), "5HK")
        self.assertEqual(
            _sequence_to_sc_input(None, "Forward + Medium Punch (Skull Splitter)"), "6MP"
        )
        self.assertEqual(
            _sequence_to_sc_input(
                "Forward, Down, Down-Forward + Light Punch", "Shoryuken (Light Punch)"
            ),
            "623LP",
        )
        self.assertEqual(
            _sequence_to_sc_input(
                "Forward, Down, Down-Forward + Heavy Kick (Hold)",
                "Cannon Spike (Heavy Kick, Full Charge)",
            ),
            "623[HK]",
        )
        self.assertEqual(
            _sequence_to_sc_input(
                "Down, Down-Forward, Forward + KK (KK)",
                "Hundred Lightning Kicks (Overdrive)",
            ),
            "236KK",
        )

    def test_target_combo_inputs_are_normalized_from_move_names(self) -> None:
        self.assertEqual(
            _sequence_to_sc_input(
                None,
                "Forward + Heavy Punch, Forward + Heavy Punch, Heavy Kick "
                "(Kikoku Combination)",
            ),
            "6HP~6HP~HK",
        )
        self.assertEqual(
            _sequence_to_sc_input(
                None, "Fluttering Lark (Crouching Medium Kick, Heavy Kick)"
            ),
            "2MK~HK",
        )
        self.assertEqual(
            _sequence_to_sc_input(
                None, "Power Dunk (Medium Punch, Heavy Kick, Heavy Kick)"
            ),
            "5MP~HK~HK",
        )

    def test_overdrive_input_is_derived_from_valid_family_input(self) -> None:
        moves = [
            UfdMove(
                category="Special Moves",
                move_name="Hazanshu (Light Kick)",
                input_sequence="Down, Down-Back, Back + Light Kick",
            ),
            UfdMove(
                category="Special Moves",
                move_name="Hazanshu (Heavy Kick)",
                input_sequence="Down, Down-Back, Back + Heavy Kick",
            ),
            UfdMove(
                category="Special Moves",
                move_name="Hazanshu (Overdrive)",
                input_sequence="Down, Down-Back, Back + Overdrive",
            ),
        ]

        resolved = _derive_page_sc_inputs(moves, {"214LK", "214HK", "214KK"})

        self.assertEqual(resolved, ["214LK", "214HK", "214KK"])

    def test_missing_existing_gif_path_is_retried(self) -> None:
        move = UfdMove(
            category="Normal Attacks",
            move_name="Standing Medium Punch",
            hitbox_url="https://example.test/ryu-st-mp.gif",
        )
        identity = _source_identity(move.category, move.move_name, move.input_sequence)
        existing = {
            f"identity:{identity}": {
                "source_move_key": "stable-key",
                "hitbox_source_url": move.hitbox_url,
                "hitbox_storage_path": None,
                "hitbox_sha256": None,
            }
        }

        with patch(
            "sf6_engine.importers.ultimate_frame_data._store_hitbox",
            return_value=("ryu/new.gif", "abc123"),
        ) as store:
            row = _to_row(
                "ryu",
                "https://ultimateframedata.com/sf6/ryu",
                move,
                sc_input="5MP",
                download_gifs=True,
                existing_gifs=existing,
            )

        store.assert_called_once()
        self.assertEqual(row["source_move_key"], "stable-key")
        self.assertEqual(row["hitbox_storage_path"], "ryu/new.gif")

    def test_ufd_context_keeps_source_and_gif_reference(self) -> None:
        context = format_ufd_details({
            "category": "Normal Attacks",
            "startup": "12",
            "active": "2",
            "recovery": "25",
            "total": "38",
            "on_block": "-5",
            "notes": "Test note",
            "hitbox_source_url": "https://ultimateframedata.com/sf6/hitboxes/ken/ken-st-hk.gif",
        })

        self.assertIn("Ultimate Frame Data 実測補足", context)
        self.assertIn("当たり判定GIF", context)
        self.assertIn("公式/SuperComboと差がある場合", context)


if __name__ == "__main__":
    unittest.main()
