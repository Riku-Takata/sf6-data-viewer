"""CAPCOM⇔SC 必殺技マッピング作成用のファミリーダンプ。

全キャラについて、CAPCOM側 (move_normalized) の必殺技/SA名から強度prefixを
除去した「技ファミリー」と、SC側 (sc_move_normalized) の Special/Super の
name から強度prefixを除去したファミリー (+input) を並べて JSON 出力する。

出力はマッピングレビュー用の中間ファイル:
  scripts/out/special_families.json

使い方 (engine ルートから):
  PYTHONPATH=src ./.venv312/bin/python scripts/dump_special_families.py
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from sf6_engine.db import get_client

OUT_PATH = Path(__file__).parent / "out" / "special_families.json"

# CAPCOM 名の強度/SA prefix (例: "弱 ロン・ポワン", "SA1 アラベスク", "OD マネージュ・ドレ")
_CAPCOM_PREFIX = re.compile(r'^(弱|中|強|OD|SA1|SA2|SA3|CA)\s*')
# CAPCOM 名の注釈 (例: "（メダルLvに応じて動作が変化）", "（1段目）")
_CAPCOM_NOTE = re.compile(r'（[^）]*）')

# SC name の強度prefix (例: "LK Rond-point", "OD Dégagé", "HP Renversé")
_SC_PREFIX = re.compile(r'^(LP|MP|HP|LK|MK|HK|OD|EX)\s+')

_TARGET_SECTIONS = ('必殺技', 'スーパーアーツ')
_SC_TYPES = ('Special', 'special', 'Super', 'super')


def capcom_family(move_name: str) -> str:
    """強度prefix・注釈を除去してファミリー名を返す。"""
    name = _CAPCOM_PREFIX.sub('', move_name)
    name = _CAPCOM_NOTE.sub('', name)
    return name.strip()


def sc_family(name: str) -> str:
    return _SC_PREFIX.sub('', name or '').strip()


def main() -> None:
    sb = get_client()

    slug_map = {
        r['capcom_slug']: r['sc_chara']
        for r in sb.table('char_slug_map').select('capcom_slug,sc_chara').execute().data
    }

    result: dict[str, dict] = {}
    for capcom_slug, sc_chara in sorted(slug_map.items()):
        # CAPCOM 側: 必殺技 + SA の全行
        cres = sb.table('move_normalized').select('move_name,section').eq(
            'character_slug', capcom_slug
        ).in_('section', list(_TARGET_SECTIONS)).execute()
        capcom_fams: dict[str, list[str]] = defaultdict(list)
        for row in cres.data or []:
            capcom_fams[capcom_family(row['move_name'])].append(row['move_name'])

        # SC 側: Special/Super の全行
        sres = sb.table('sc_move_normalized').select('input,name,move_type').eq(
            'chara', sc_chara
        ).in_('move_type', list(_SC_TYPES)).execute()
        sc_fams: dict[str, list[dict]] = defaultdict(list)
        for row in sres.data or []:
            sc_fams[sc_family(row['name'])].append(
                {'input': row['input'], 'name': row['name'], 'type': row['move_type']}
            )

        result[capcom_slug] = {
            'sc_chara': sc_chara,
            'capcom_families': {k: v for k, v in sorted(capcom_fams.items())},
            'sc_families': {k: v for k, v in sorted(sc_fams.items())},
        }
        print(f"{capcom_slug:16s} capcom={len(capcom_fams):3d} families / sc={len(sc_fams):3d} families")

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nwrote {OUT_PATH}")


if __name__ == '__main__':
    main()
