"""RAG Context Builder: Intent → Supabase クエリ → コンテキスト文字列。

Intent Parser の出力に応じて unified_moves / sc_move_normalized から
フレームデータを取得し、LLM への最終プロンプトに埋め込むコンテキストを組み立てる。

Phase 2 (文書取込) 完了後は doc_chunks からもコンテキストを追加できるよう
設計している。現時点では Phase 1 データ (フレームデータ) のみ対応。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sf6_engine.db import get_client

logger = logging.getLogger(__name__)

# ============================================================
# フレームデータ取得
# ============================================================

# ============================================================
# 必殺技: 日本語技名 → 英語検索キーワードのマッピング
# sc_moves.name (英語) を ILIKE 検索するために使用
# ============================================================
_JP_MOVE_TO_EN: dict[str, str] = {
    # サガット
    'タイガーショット': 'Tiger Shot',
    'タイガーアッパー': 'Tiger Uppercut',
    'タイガーニー': 'Tiger Knee',
    'タイガーニークラッシュ': 'Tiger Knee Crush',
    'タイガーキャノン': 'Tiger Cannon',
    'タイガーバニッシャー': 'Tiger Vanquisher',
    'タイガーネクサス': 'Tiger Nexus',
    'サベージタイガー': 'Savage Tiger',
    'タイガーモノリス': 'Tiger Monolith',
    'モノリス': 'Tiger Monolith',
    'ノヴァタイガー': 'Nova Tiger',
    'ノヴァ': 'Nova Tiger',
    'グリーディタイガー': 'Greedy Tiger',
    'グリード': 'Greedy Tiger',
    'マイティタイガー': 'Mighty Tiger',
    'マイト': 'Mighty Tiger',
    'ステップハイキック': 'Step High Kick',
    'ステハイ': 'Step High Kick',
    'ステップローキック': 'Step Low Kick',
    'ステロー': 'Step Low Kick',
    # リュウ
    '波動拳': 'Hadoken',
    '昇竜拳': 'Shoryuken',
    '竜巻旋風脚': 'Tatsumaki',
    '電刃錬気': 'Denjin',
    # ケン
    '竜巻旋風脚ケン': 'Tatsumaki',
    '疾風迅雷脚': 'Shippu',
    '迅雷脚': 'Jinrai',
    '龍尾脚': 'Dragonlash',
    # ガイル
    'ソニックブーム': 'Sonic Boom',
    'サマーソルト': 'Somersault',
    'サマーソルトキック': 'Somersault',
    # 春麗
    '気功掌': 'Kikosho',
    '百裂脚': 'Hyakuretsu',
    'スピニングバードキック': 'Spinning Bird Kick',
    # キャミィ
    'キャノンストライク': 'Cannon Strike',
    'スパイラルアロー': 'Spiral Arrow',
    'キャノンスパイク': 'Cannon Spike',
    # ザンギエフ
    'スクリューパイルドライバー': 'Screw Pile Driver',
    'ダブルラリアット': 'Lariat',
    # 豪鬼
    '瞬獄殺': 'Shun Goku Satsu',
    '豪波動拳': 'Hadoken',
    '豪昇竜拳': 'Shoryuken',
    # ルーク
    'サンドブラスト': 'Sandblast',
    'フラッシュナックル': 'Flash Knuckle',
    'ライジングアッパー': 'Rising Uppercut',
}


_SC_MOVE_SELECT = (
    'id,chara,input,name,move_type,'
    'startup_f,active_f,recovery_f,'
    'block_adv_f,hit_adv_f,hit_is_knockdown,'
    'punish_adv_f,perf_parry_adv_f,atk_range_n,'
    'invuln,armor,notes,damage'
)

# 特殊技 (Super) より一般技 (Special) を優先して返すためのソート順
_MOVE_TYPE_PRIORITY = ['Special', 'special', 'throw', 'Super', 'super']


def _sort_by_type(rows: list[dict]) -> list[dict]:
    """move_type 順でソート: Special → throw → Super の優先度。"""
    def priority(r):
        t = r.get('move_type', '')
        try:
            return _MOVE_TYPE_PRIORITY.index(t)
        except ValueError:
            return 99
    return sorted(rows, key=priority)


# 強度語 → 入力キー候補 (K系とP系の両方を持つ)
_STRENGTH_TO_KEYS: dict[str, tuple[str, ...]] = {
    '弱':    ('LK', 'LP'),
    '中':    ('MK', 'MP'),
    '強':    ('HK', 'HP'),
    'weak':  ('LK', 'LP'),
    'light': ('LK', 'LP'),
    'medium':('MK', 'MP'),
    'mid':   ('MK', 'MP'),
    'heavy': ('HK', 'HP'),
    'hard':  ('HK', 'HP'),
}
# 後方互換用 (単一キー版)
_STRENGTH_INPUT_KEY = {k: v[0] for k, v in _STRENGTH_TO_KEYS.items()}


def _pick_variant(rows: list[dict], raw_query: str, move_name: str = '') -> dict:
    """複数の同名バリアント (弱/中/強) から raw_query のヒントで最適なものを返す。

    「中迅雷脚の弱派生」のように別の強度語が混在する場合、技名の直前にある
    強度語を優先する。技名の直前に見つからない場合は全文検索へフォールバック。
    """
    # JP技名キーワードを raw_query から生成 (JP_MOVE_TO_EN の逆引き)
    # move_name が英語の場合、対応する日本語名を探す
    jp_keywords: list[str] = []
    for jp, en in _JP_MOVE_TO_EN.items():
        if en.lower() in move_name.lower() or move_name.lower() in en.lower():
            jp_keywords.append(jp)
    # move_name 自体が日本語の場合も追加
    if move_name and not jp_keywords:
        jp_keywords.append(move_name)

    # OD (オーバードライブ) 検索: ボタン2個押し (KK/PP/LPMP/LPHP 等)
    # 注: Python3では CJK文字が \w とみなされるため \bOD\b は日本語境界でマッチしない
    #     → ASCII境界専用パターンを使用
    if re.search(r'(?<![A-Za-z0-9])OD(?![A-Za-z0-9])|オーバードライブ', raw_query):
        for r in rows:
            inp = r.get('input', '')
            if any(x in inp for x in ('KK', 'PP', 'LPMP', 'LPHP', 'MPHP')):
                return r

    def _match_strength(input_str: str, jp_strength: str) -> bool:
        """強度語に対応する入力キー候補 (K系+P系) のいずれかが input に含まれるか。"""
        keys = _STRENGTH_TO_KEYS.get(jp_strength, ())
        return any(k in input_str.upper() for k in keys)

    # 技名直前の強度語を優先検索
    for kw in jp_keywords:
        idx = raw_query.find(kw)
        if idx > 0:
            preceding = raw_query[max(0, idx - 2):idx]  # 直前2文字
            for jp_s in _STRENGTH_TO_KEYS:
                if jp_s in preceding:
                    for r in rows:
                        if _match_strength(r.get('input', ''), jp_s):
                            return r

    # フォールバック: クエリ全文から強度語を検索
    for jp_s in _STRENGTH_TO_KEYS:
        if jp_s in raw_query:
            for r in rows:
                if _match_strength(r.get('input', ''), jp_s):
                    return r

    return rows[0]


_SPECIAL_MOVE_TYPES = ['Special', 'special', 'Super', 'super']


def _fetch_move_by_name(chara: str, move_name: str, raw_query: str = '') -> dict | None:
    """必殺技・SA 名で sc_move_normalized を検索する。

    検索順序 (ハードコーディングを排除して汎用化):
    1. Special/Super タイプに絞って move_name を直接 ILIKE 検索
    2. move_name を単語分割して各単語で Special/Super 検索
    3. _JP_MOVE_TO_EN マッピング経由 (日本語→英語フォールバック)
    4. タイプ不問で全移動技から検索 (コマンド通常技・throw なども拾う)

    複数ヒット時は raw_query の弱/中/強ヒントで最適なバリアントを選択する。
    """
    sb = get_client()

    map_res = sb.table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
    if not map_res.data:
        map_res = sb.table('char_slug_map').select('sc_chara').ilike('sc_chara', f'%{chara}%').execute()
    if not map_res.data:
        logger.warning(f"char_slug_map に {chara} が見つかりません")
        return None
    sc_chara = map_res.data[0]['sc_chara']

    def search_special(keyword: str) -> list[dict]:
        """Special/Super タイプのみを name ILIKE で検索。"""
        if not keyword or len(keyword) < 2:
            return []
        res = sb.table('sc_move_normalized').select(_SC_MOVE_SELECT).eq(
            'chara', sc_chara
        ).ilike('name', f'%{keyword}%').in_('move_type', _SPECIAL_MOVE_TYPES).execute()
        return res.data or []

    def search_all(keyword: str) -> list[dict]:
        """タイプ不問で name ILIKE 検索 (コマンド通常技・throw など向け)。"""
        if not keyword or len(keyword) < 2:
            return []
        res = sb.table('sc_move_normalized').select(_SC_MOVE_SELECT).eq(
            'chara', sc_chara
        ).ilike('name', f'%{keyword}%').execute()
        return res.data or []

    # 1. 直接検索 (LLM が英語名を出力した場合はここで解決)
    rows = search_special(move_name)
    if rows:
        return _pick_variant(_sort_by_type(rows), raw_query, move_name)

    # 2. 単語分割検索 ("Tiger Uppercut" → ["Tiger","Uppercut"] で各単語を試す)
    for word in move_name.split():
        if len(word) >= 3:
            rows = search_special(word)
            if rows:
                return _pick_variant(_sort_by_type(rows), raw_query, move_name)

    # 3. JP→EN マッピング (LLM が日本語名を出力したときのフォールバック)
    en_keyword = _JP_MOVE_TO_EN.get(move_name)
    if en_keyword:
        rows = search_special(en_keyword)
        if rows:
            return _pick_variant(_sort_by_type(rows), raw_query, move_name)
    for jp, en in _JP_MOVE_TO_EN.items():
        if jp in move_name or move_name in jp:
            rows = search_special(en)
            if rows:
                return _pick_variant(_sort_by_type(rows), raw_query, move_name)

    # 4. タイプ不問 (コマンド通常技・Tiger Monolith 等も拾う)
    rows = search_all(move_name)
    if rows:
        return _pick_variant(_sort_by_type(rows), raw_query, move_name)
    for word in move_name.split():
        if len(word) >= 3:
            rows = search_all(word)
            if rows:
                return _pick_variant(_sort_by_type(rows), raw_query, move_name)

    # 5. raw_query からの JP→EN 逆引き (LLM が誤訳した場合のリカバリー)
    #    例: 「瞬獄殺」を「Instant Kill」と訳した場合、クエリ中の「瞬獄殺」→「Shun Goku Satsu」で再検索
    if raw_query:
        for jp, en in _JP_MOVE_TO_EN.items():
            if jp in raw_query:
                rows = search_special(en)
                if rows:
                    logger.info(f"JP→EN recovery: '{jp}' → '{en}' for move_name='{move_name}'")
                    return _pick_variant(_sort_by_type(rows), raw_query, en)

    logger.info(f"{sc_chara} の '{move_name}' にヒットなし")
    return None


def _fmt_sc_move(row: dict) -> str:
    """sc_move_normalized の行 (必殺技用) をコンテキスト用テキストに変換。"""
    name = row.get('name', '不明')
    inp  = row.get('input', '?')
    chara = row.get('chara', '?')
    lines = [f"【{chara} / {name} ({inp})】"]

    if row.get('startup_f') is not None:
        lines.append(f"発生: {row['startup_f']}F")
    if row.get('block_adv_f') is not None:
        lines.append(f"ガード時: {_sign(row['block_adv_f'])}F")
    if row.get('hit_adv_f') is not None:
        if row.get('hit_is_knockdown'):
            lines.append(f"ヒット時: KD (ノックダウン)")
        else:
            lines.append(f"ヒット時: {_sign(row['hit_adv_f'])}F")
    if row.get('punish_adv_f') is not None:
        lines.append(f"パニッシュカウンター時: {_sign(row['punish_adv_f'])}F")
    if row.get('atk_range_n') is not None:
        lines.append(f"リーチ: {row['atk_range_n']}")
    if row.get('invuln'):
        lines.append(f"無敵: {row['invuln']}")
    if row.get('armor'):
        lines.append(f"アーマー: {row['armor']}")
    if row.get('damage'):
        lines.append(f"ダメージ: {row['damage']}")
    if row.get('recovery_f') is not None:
        lines.append(f"硬直: {row['recovery_f']}F")
    if row.get('notes'):
        lines.append(f"解説: {row['notes']}")

    return "\n".join(lines)


# ============================================================
# コンボ・キャンセル情報取得
# ============================================================

# cancel フィールドの略称を日本語に展開
_CANCEL_ABBR: dict[str, str] = {
    'Chn': 'チェーン(連打)',
    'Sp':  '必殺技キャンセル',
    'SA':  'SAキャンセル',
    'TC':  'ターゲットコンボ',
    'SA1': 'SA1キャンセル',
    'SA2': 'SA2キャンセル',
    'SA3': 'SA3キャンセル',
    '-':   'なし',
}


def _parse_cancel(cancel_raw: str | None) -> str:
    """cancel フィールドを日本語の解説文に変換。"""
    if not cancel_raw or cancel_raw == '-':
        return 'なし'
    parts = [c.strip() for c in cancel_raw.replace(',', ' ').split()]
    labels = [_CANCEL_ABBR.get(p, p) for p in parts if p and p != '-']
    return ' / '.join(labels) if labels else 'なし'


def _fetch_combo_data(chara: str, sc_input: str) -> dict | None:
    """sc_moves からコンボ・キャンセル関連フィールドをまとめて取得する。

    unified_moves / sc_move_normalized では省略されているフィールド
    (cancel, dr_cancel_hit, after_dr_hit 等) を含む完全な行を返す。
    """
    sb = get_client()

    # char_slug_map 経由で sc_chara を解決
    map_res = sb.table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
    if not map_res.data:
        map_res = sb.table('char_slug_map').select('sc_chara').ilike('sc_chara', f'%{chara}%').execute()
    if not map_res.data:
        return None
    sc_chara = map_res.data[0]['sc_chara']

    res = sb.table('sc_moves').select(
        'input,name,move_type,'
        'startup,active,recovery,'
        'hit_adv,block_adv,punish_adv,'
        'cancel,'
        'dr_cancel_hit,dr_cancel_blk,'
        'after_dr_hit,after_dr_blk,'
        'damage,notes'
    ).eq('chara', sc_chara).eq('input', sc_input).execute()

    return res.data[0] if res.data else None


def _fmt_combo_context(chara: str, row: dict) -> str:
    """コンボ・キャンセル情報をコンテキスト用テキストに変換。"""
    inp  = row.get('input', '?')
    name = row.get('name', '不明')

    lines = [f"【{chara} / {inp} ({name}) — コンボ・キャンセル情報】"]

    # ヒット有利 (ノックダウン判定)
    hit = row.get('hit_adv', '')
    if hit:
        if 'KD' in str(hit) or 'HKD' in str(hit):
            lines.append(f"ヒット時: {hit} (ノックダウン)")
            lines.append(f"  → 相手が起き上がるまで {hit} の有利があります")
        else:
            lines.append(f"ヒット時有利: {_sign_str(hit)}")

    # ガード有利
    blk = row.get('block_adv', '')
    if blk:
        lines.append(f"ガード時有利: {_sign_str(blk)}")

    # キャンセル可否
    cancel_raw = row.get('cancel', '-')
    cancel_str = _parse_cancel(cancel_raw)
    lines.append(f"キャンセル: {cancel_str} (生値: {cancel_raw or '-'})")

    # DRキャンセル (ヒット時)
    dr_hit  = row.get('dr_cancel_hit')
    adr_hit = row.get('after_dr_hit')
    if dr_hit or adr_hit:
        lines.append(f"DRキャンセル (ヒット時):")
        if dr_hit:
            lines.append(f"  キャンセル直後: {dr_hit}")
        if adr_hit:
            lines.append(f"  ラッシュ到着後: {adr_hit} (この有利で次の攻撃が繋がる)")

    # DRキャンセル (ガード時)
    dr_blk  = row.get('dr_cancel_blk')
    adr_blk = row.get('after_dr_blk')
    if dr_blk or adr_blk:
        lines.append(f"DRキャンセル (ガード時):")
        if dr_blk:
            lines.append(f"  キャンセル直後: {dr_blk}")
        if adr_blk:
            lines.append(f"  ラッシュ到着後: {adr_blk}")

    # 解説 (チェーン・コンボ情報が含まれる)
    notes = row.get('notes', '')
    if notes:
        lines.append(f"コンボ解説: {notes}")

    return '\n'.join(lines)


def _sign_str(val) -> str:
    """文字列または数値を符号付き表示。例: '-5' → '-5', '4' → '+4'"""
    s = str(val).strip()
    if s.lstrip('-').isdigit() and not s.startswith('-') and not s.startswith('+'):
        return f'+{s}'
    return s


def _parse_frame_value(val) -> int | None:
    """'+8', '+8F', '8', 8 などを int に変換。KD/HKD を含む場合は None。"""
    if val is None:
        return None
    s = str(val)
    if 'KD' in s or 'HKD' in s:
        return None
    m = re.search(r'([+-]?\d+)', s)
    return int(m.group(1)) if m else None


# コンボ繋がり検索に含める move_type
_COMBO_USEFUL_TYPES = frozenset({
    'ground_normal', 'Special', 'special', 'Super', 'super', 'throw',
})


def _find_combo_follow_ups(
    chara: str,
    max_startup: int,
    exclude_input: str | None = None,
    include_special: bool = True,
) -> list[dict]:
    """指定フレーム以内の発生を持つ技を検索してコンボ候補を返す。

    Args:
        chara:          capcom_slug ('sagat') または SC chara 名 ('Sagat')。
        max_startup:    この発生F 以内の技を返す (コンボの有利F = このvalueのはず)。
        exclude_input:  除外するinput (始動技自身を除く)。
        include_special: 必殺技・SAを含めるか。

    Returns:
        list[dict]: startup_f の大きい順 (高ダメージ技を優先) にソートされた技リスト。
    """
    sb = get_client()

    # capcom_slug → sc_chara
    map_res = sb.table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
    if not map_res.data:
        # SC chara 名でそのまま試みる
        sc_chara = chara
    else:
        sc_chara = map_res.data[0]['sc_chara']

    res = sb.table('sc_move_normalized').select(
        'input,name,move_type,startup_f,'
        'block_adv_f,hit_adv_f,hit_is_knockdown,'
        'punish_adv_f,atk_range_n,damage,notes'
    ).eq('chara', sc_chara).lte('startup_f', max_startup).not_.is_('startup_f', 'null').execute()

    results = []
    for r in res.data:
        mt = r.get('move_type', '')
        if mt not in _COMBO_USEFUL_TYPES:
            continue
        if r.get('input') == exclude_input:
            continue
        if not include_special and mt in ('Special', 'special', 'Super', 'super'):
            continue
        results.append(r)

    # ソート: KD優先 → startup大きい順 (重い技ほど威力が高い)
    def sort_key(r):
        is_kd = 1 if r.get('hit_is_knockdown') else 0
        st    = r.get('startup_f', 0) or 0
        return (-is_kd, -st)

    return sorted(results, key=sort_key)[:10]


def _fmt_combo_route(
    source_input: str,
    source_name: str,
    adv_f: int,
    follow_ups: list[dict],
    scenario_label: str,
) -> str:
    """コンボルートを LLM 用コンテキストテキストに変換する。

    「なぜ繋がるか」の推論根拠 (adv_f ≥ startup_f) を明示することで
    LLM がコンテキストを正しく解釈できるようにする。
    """
    lines = [
        f"【コンボルート分析: {source_input} ({source_name})】",
        f"  {scenario_label}後の有利フレーム: +{adv_f}F",
        f"  ← 発生 {adv_f}F 以内の技は確定でコンボになります",
        "",
        "  繋がる技 (発生順):",
    ]

    ground  = [r for r in follow_ups if r.get('move_type') in ('ground_normal',)]
    special = [r for r in follow_ups if r.get('move_type') in ('Special','special','Super','super')]
    throws  = [r for r in follow_ups if r.get('move_type') == 'throw']

    def fmt_row(r, prefix=''):
        inp    = r.get('input', '?')
        name   = r.get('name') or '不明'
        st     = r.get('startup_f', '?')
        is_kd  = r.get('hit_is_knockdown', False)
        hit    = r.get('hit_adv_f')
        hit_str = ('KD' if is_kd else (_sign_str(hit) + 'F' if hit is not None else '?'))
        pa     = r.get('punish_adv_f')
        pa_str = f' (パニカン時: +{pa}F)' if pa else ''
        return f"  {prefix}✅ {inp:10s} 発生{st}F │ {name} │ ヒット時: {hit_str}{pa_str}"

    if ground:
        lines.append("  [通常技]")
        for r in ground:
            lines.append(fmt_row(r))
    if special:
        lines.append("  [必殺技・SA]")
        for r in special:
            lines.append(fmt_row(r))
    if throws:
        lines.append("  [投げ]")
        for r in throws:
            lines.append(fmt_row(r))

    if not (ground or special or throws):
        lines.append("  (該当技なし)")

    lines.append("")
    lines.append(f"  ※ 推論根拠: {scenario_label}後の有利{adv_f}F ≥ 各技の発生F → コンボ確定")

    return '\n'.join(lines)


def _fetch_move(chara: str, sc_input: str) -> dict | None:
    """unified_moves から指定キャラの指定技を取得。

    引数の chara は SuperCombo chara 値 ("Sagat" 等)。
    sc_input は numpad 表記 ("2HK", "5HP" 等)。
    """
    sb = get_client()
    res = (
        sb.table("unified_moves")
        .select(
            "character_slug,move_name,section,sc_input_key,"
            "c_startup,c_on_block,c_on_hit,c_damage,"
            "sc_startup,sc_active,sc_recovery,"
            "sc_block_adv,sc_hit_adv,sc_punish_adv,sc_perf_parry_adv,"
            "sc_atk_range,sc_invuln,sc_damage_raw,sc_notes,has_sc_data"
        )
        .eq("sc_input_key", sc_input)
        .execute()
    )
    # キャラ名で絞り込み (大文字小文字を無視)
    chara_lower = chara.lower().replace(" ", "").replace(".", "").replace("_", "")
    for row in res.data:
        slug = row.get("character_slug", "")
        if slug.lower().replace("_", "").replace(".", "") == chara_lower:
            return row

    # char_slug_map 経由で capcom_slug から逆引き
    map_res = (
        sb.table("char_slug_map")
        .select("capcom_slug")
        .eq("sc_chara", chara)
        .limit(1)
        .execute()
    )
    if map_res.data:
        capcom_slug = map_res.data[0]["capcom_slug"]
        for row in res.data:
            if row.get("character_slug") == capcom_slug:
                return row

    return None


def _fmt_move(row: dict) -> str:
    """フレームデータ行を人間可読なテキストブロックに変換。"""
    sc_input = row.get("sc_input_key", "?")
    move_name = row.get("move_name", "不明")
    chara_slug = row.get("character_slug", "?")
    lines = [
        f"【{chara_slug} / {sc_input} ({move_name})】",
    ]

    # 発生
    c_st = row.get("c_startup")
    sc_st = row.get("sc_startup")
    if c_st is not None:
        lines.append(f"発生: {c_st}F" + (f" (SC: {sc_st}F)" if sc_st and sc_st != c_st else ""))
    elif sc_st is not None:
        lines.append(f"発生: {sc_st}F (SCデータ)")

    # ガード有利
    c_blk = row.get("c_on_block")
    sc_blk = row.get("sc_block_adv")
    if c_blk is not None:
        lines.append(f"ガード時: {_sign(c_blk)}F" + (f" (SC: {_sign(sc_blk)}F)" if sc_blk and sc_blk != c_blk else ""))
    elif sc_blk is not None:
        lines.append(f"ガード時: {_sign(sc_blk)}F (SCデータ)")

    # ヒット有利
    sc_hit = row.get("sc_hit_adv")
    c_hit  = row.get("c_on_hit")
    if sc_hit is not None:
        lines.append(f"ヒット時: {_sign(sc_hit)}F")
    elif c_hit is not None:
        lines.append(f"ヒット時: {_sign(c_hit)}F")

    # パニッシュカウンター有利
    pa = row.get("sc_punish_adv")
    if pa is not None:
        lines.append(f"パニッシュカウンター(パニカン)時: {_sign(pa)}F")

    # パーフェクトパリィ
    ppa = row.get("sc_perf_parry_adv")
    if ppa is not None:
        lines.append(f"パーフェクトパリィ(完全パリィ)後: {_sign(ppa)}F")

    # リーチ
    ar = row.get("sc_atk_range")
    if ar is not None:
        lines.append(f"リーチ: {ar}")

    # 無敵
    inv = row.get("sc_invuln")
    if inv:
        lines.append(f"無敵: {inv}")

    # ダメージ
    c_dmg  = row.get("c_damage")
    sc_dmg = row.get("sc_damage_raw")
    if c_dmg is not None:
        lines.append(f"ダメージ: {c_dmg}")
    elif sc_dmg:
        lines.append(f"ダメージ: {sc_dmg}")

    # キャンセル (CAPCOM side boolean flags)
    cancels = []
    if row.get("cancellable_chain"): cancels.append("チェーン")
    if row.get("cancellable_sa1"):   cancels.append("SA1")
    if row.get("cancellable_sa2"):   cancels.append("SA2")
    if row.get("cancellable_sa3"):   cancels.append("SA3")
    if cancels:
        lines.append(f"キャンセル可: {' / '.join(cancels)}")

    # 解説テキスト
    notes = row.get("sc_notes")
    if notes:
        lines.append(f"解説: {notes}")

    return "\n".join(lines)


def _sign(n: int | float | None) -> str:
    """数値を符号付き文字列に変換。例: -6 → '-6', 4 → '+4'"""
    if n is None:
        return "?"
    return f"+{n}" if n >= 0 else str(n)


# ============================================================
# コンテキスト構築
# ============================================================

# SF6ゲーム用語の日英マッピング (Intent Parser から渡された概念名を英語に変換)
_JP_TO_EN_CONCEPT: dict[str, list[str]] = {
    'ドライブインパクト': ['Drive Impact', 'DI'],
    'ドライブパリィ': ['Drive Parry', 'parry'],
    'ドライブリバーサル': ['Drive Reversal'],
    'ドライブラッシュ': ['Drive Rush', 'DR'],
    'ドライブラッシュキャンセル': ['Drive Rush', 'DR cancel'],
    'キャンセルドライブラッシュ': ['Drive Rush', 'DR cancel'],
    'ドライブラッシュのキャンセル': ['Drive Rush'],
    'オーバードライブ': ['Overdrive', 'OD'],
    'バーンアウト': ['Burnout'],
    'ドライブゲージ': ['Drive Gauge'],
    'パーフェクトパリィ': ['Perfect Parry'],
    '完全パリィ': ['Perfect Parry'],
    'パニッシュカウンター': ['Punish Counter', 'Punish Counters'],
    'パニカン': ['Punish Counter', 'Punish Counters'],
    'カウンターヒット': ['Counter Hit', 'Counter-hit', 'counter-hits'],
    'スーパーアーツ': ['Super Art'],
    'スーパーゲージ': ['Super Gauge'],
    'ジャグル': ['Juggle'],
    'ダメージスケーリング': ['Damage Scaling'],
    'コンボ': ['Combo', 'Combos'],
    'キャンセル': ['Cancel'],
    'ブロック': ['Blocking', 'Block'],
    'ガード': ['Blocking', 'Guard'],
    'ガードバック': ['Blocking', 'pushback'],
    '起き攻め': ['Wake-up'],
    '無敵': ['invulnerability', 'Reversals'],
    '対空': ['Anti-Airs'],
    '暴れ': ['Reversals'],
}


def _keyword_search(query_text: str, count: int = 3) -> list[dict]:
    """キーワードベースのフォールバック検索。

    日英マッピングでクエリを変換し、heading_h2/keywords フィールドで ILIKE 検索する。
    """
    sb = get_client()

    # 日本語概念を英語に変換
    search_terms: list[str] = []
    for jp, en_list in _JP_TO_EN_CONCEPT.items():
        if jp in query_text:
            search_terms.extend(en_list)
    # 元のクエリも追加
    search_terms.append(query_text)

    results = []
    seen_ids: set[str] = set()

    for term in search_terms:
        try:
            # heading_h2 で検索
            res = sb.table('doc_chunks').select(
                'id,page,heading_h2,heading_h3,content,keywords'
            ).ilike('heading_h2', f'%{term}%').limit(count).execute()
            for r in res.data:
                if r['id'] not in seen_ids:
                    r['similarity'] = 0.92
                    results.append(r)
                    seen_ids.add(r['id'])

            # heading_h3 でも検索
            res3 = sb.table('doc_chunks').select(
                'id,page,heading_h2,heading_h3,content,keywords'
            ).ilike('heading_h3', f'%{term}%').limit(count).execute()
            for r in res3.data:
                if r['id'] not in seen_ids:
                    r['similarity'] = 0.88
                    results.append(r)
                    seen_ids.add(r['id'])
        except Exception as e:
            logger.debug(f"keyword search error for {term!r}: {e}")

    return results[:count]


async def _search_docs(query_text: str, provider, threshold: float = 0.45, count: int = 3) -> list[dict]:
    """doc_chunks をハイブリッド検索 (キーワード優先 + ベクトル補完) して関連チャンクを返す。

    Args:
        query_text: 検索クエリ文字列 (日本語・英語どちらも可)。
        provider  : LLMProvider インスタンス (embed に使用)。
        threshold : ベクトル検索のコサイン類似度最小値 (0〜1)。
        count     : 返す最大チャンク数。

    Returns:
        list[dict]: 検索結果のリスト。similarity フィールド付き。
    """
    results: list[dict] = []
    seen_ids: set[str] = set()

    # --- 1. キーワード検索 (日英マッピング + heading_h2 ILIKE) ---
    kw_results = _keyword_search(query_text, count)
    for r in kw_results:
        results.append(r)
        seen_ids.add(r['id'])

    # --- 2. ベクトル検索 (補完) ---
    try:
        # 日本語概念を英語に変換してから埋め込み
        en_terms = []
        for jp, en_list in _JP_TO_EN_CONCEPT.items():
            if jp in query_text:
                en_terms.extend(en_list)
        embed_text = ' '.join(en_terms) if en_terms else query_text

        embedding = await provider.embed(embed_text)
        sb = get_client()
        vec_res = sb.rpc('search_docs', {
            'query_embedding': embedding,
            'match_threshold': threshold,
            'match_count': count,
        }).execute()

        for r in (vec_res.data or []):
            if r['id'] not in seen_ids:
                results.append(r)
                seen_ids.add(r['id'])

    except Exception as e:
        logger.warning(f"vector search failed: {e}")

    # similarity 降順でソート
    results.sort(key=lambda x: x.get('similarity', 0), reverse=True)
    return results[:count]


def _fmt_doc_chunk(chunk: dict) -> str:
    """doc_chunks の1行をコンテキスト用テキストブロックに変換。"""
    h2 = chunk.get('heading_h2', '')
    h3 = chunk.get('heading_h3')
    page = chunk.get('page', '')
    content = chunk.get('content', '')
    sim = chunk.get('similarity', 0)

    header = f"【{page} / {h2}"
    if h3:
        header += f" / {h3}"
    header += f"】 (類似度: {sim:.2f})"

    return f"{header}\n{content[:800]}"  # 長すぎるチャンクは先頭800文字に制限


async def build_context(intent: dict, provider=None) -> str:
    """Intent に応じて Supabase からデータを取得し、RAG コンテキストを組み立てる。

    Args:
        intent: parse_intent() が返す dict。

    Returns:
        str: LLM のプロンプトに埋め込む参照データ文字列。
             データが見つからない場合は「データなし」を明示する文字列を返す。
    """
    intent_type = intent.get("intent_type", "general_question")
    chara      = intent.get("chara")
    chara2     = intent.get("chara2")
    inp        = intent.get("input")
    inp2       = intent.get("input2")
    move_name  = intent.get("move_name")   # 必殺技・SA の技名
    move_name2 = intent.get("move_name2") # 比較先の必殺技・SA 名
    concept    = intent.get("concept")

    sections: list[str] = []

    # --- setplay_analysis: KD後・有利フレーム後の起き攻め択を計算 ---
    if intent_type == "setplay_analysis" and chara:
        raw_row: dict | None = None
        actual_inp = inp

        if inp:
            raw_row = _fetch_combo_data(chara, inp)
        elif move_name:
            norm_row = _fetch_move_by_name(chara, move_name, raw_query=intent.get("raw_query",""))
            if norm_row:
                actual_inp = norm_row.get('input', '')
                raw_row = _fetch_combo_data(chara, actual_inp)

        if raw_row and actual_inp:
            hit_adv_raw    = raw_row.get('hit_adv')
            punish_adv_raw = raw_row.get('punish_adv')
            mv_name        = raw_row.get('name') or actual_inp

            # 技の基本データもコンテキストに追加
            sections.append(_fmt_combo_context(chara, raw_row))

            # パニカン時のセットプレイ (punish_adv が KD でヒットと異なる場合)
            from sf6_engine.setplay_engine import compute_setplay, format_setplay_context

            # 通常ヒット後セットプレイ
            scenarios_hit = compute_setplay(chara, actual_inp, mv_name, hit_adv_raw)
            if scenarios_hit:
                sections.append(format_setplay_context(actual_inp, mv_name, hit_adv_raw, scenarios_hit))

            # パニカン時が通常ヒットと異なる場合は別途表示
            if punish_adv_raw and punish_adv_raw != hit_adv_raw:
                scenarios_pc = compute_setplay(chara, actual_inp, mv_name, punish_adv_raw)
                if scenarios_pc:
                    pc_ctx = format_setplay_context(actual_inp, mv_name, punish_adv_raw, scenarios_pc)
                    sections.append(f"【パニッシュカウンター時のセットプレイ】\n{pc_ctx}")
        else:
            name_disp = inp or move_name or '(技名不明)'
            sections.append(
                f"⚠ {chara} の {name_disp} のセットプレイデータが見つかりませんでした。"
                f"通常技は numpad 表記 (例: 623HP)、必殺技は正式名で入力してください。"
            )

    # --- max_combo: ビームサーチで最大ダメージコンボを計算 ---
    if intent_type == "max_combo" and chara and inp:
        try:
            from sf6_engine.combo_engine import compute_max_combo
            result = compute_max_combo(chara, inp, use_dr=True, drive_bars=6)
            if result:
                sections.append(result.format_context())
                # doc_chunks からDrive Rush のゲームシステム情報も追加
                if provider:
                    dr_chunks = await _search_docs("Drive Rush cancel mechanics cost", provider, threshold=0.5, count=1)
                    if dr_chunks:
                        sections.append(_fmt_doc_chunk(dr_chunks[0]))
            else:
                sections.append(f"⚠ {chara} の {inp} からのコンボルートが見つかりませんでした。")
        except Exception as e:
            logger.error(f"max_combo error: {e}")
            sections.append(f"⚠ コンボ計算中にエラーが発生しました: {e}")

    # --- combo_info: キャンセル・コンボ情報 + 繋がり技の自動計算 ---
    if intent_type == "combo_info" and chara and inp:
        combo_row = _fetch_combo_data(chara, inp)
        if combo_row:
            # 1. 基本コンボ情報
            sections.append(_fmt_combo_context(chara, combo_row))

            # 2. DRキャンセル後のコンボルート計算
            after_dr = _parse_frame_value(combo_row.get('after_dr_hit'))
            if after_dr and after_dr > 0:
                follow_ups_dr = _find_combo_follow_ups(chara, after_dr, exclude_input=inp)
                if follow_ups_dr:
                    sections.append(_fmt_combo_route(
                        inp, combo_row.get('name', ''), after_dr, follow_ups_dr,
                        'DRキャンセル'
                    ))

            # 3. 通常ヒット後のリンクコンボ (有利が大きいものだけ)
            hit_adv = _parse_frame_value(combo_row.get('hit_adv'))
            if hit_adv and hit_adv >= 3:
                # hit_adv - 1 で厳密なリンク計算 (フレームの猶予)
                link_threshold = hit_adv - 1
                follow_ups_hit = _find_combo_follow_ups(
                    chara, link_threshold, exclude_input=inp, include_special=False
                )
                if follow_ups_hit:
                    sections.append(_fmt_combo_route(
                        inp, combo_row.get('name', ''), hit_adv, follow_ups_hit,
                        '通常ヒット'
                    ))

            # 4. パニカン時のコンボルート (有利が大幅に増える場合)
            pc_adv_raw = combo_row.get('hit_adv', '')
            # パニカン時は hit_adv + 4F (SF6 のルール)
            base_adv = _parse_frame_value(pc_adv_raw)
            if base_adv is not None:
                pc_adv = base_adv + 4
                if pc_adv >= 6:  # パニカンで繋がる範囲が広がる場合のみ表示
                    pc_ups = _find_combo_follow_ups(chara, pc_adv - 1, exclude_input=inp)
                    if pc_ups and len(pc_ups) > len(follow_ups_dr if after_dr else []):
                        sections.append(_fmt_combo_route(
                            inp, combo_row.get('name', ''), pc_adv, pc_ups,
                            'パニッシュカウンター時'
                        ))
        else:
            sections.append(f"⚠ {chara} の {inp} のコンボデータが見つかりません。")
    elif intent_type == "combo_info" and chara and move_name:
        sc_row = _fetch_move_by_name(chara, move_name, raw_query=intent.get("raw_query",""))
        if sc_row:
            actual_input = sc_row.get('input', '')
            combo_row = _fetch_combo_data(chara, actual_input)
            if combo_row:
                sections.append(_fmt_combo_context(chara, combo_row))

                # DRキャンセル後のコンボ
                after_dr = _parse_frame_value(combo_row.get('after_dr_hit'))
                if after_dr and after_dr > 0:
                    follow_ups = _find_combo_follow_ups(chara, after_dr, exclude_input=actual_input)
                    if follow_ups:
                        sections.append(_fmt_combo_route(
                            actual_input, combo_row.get('name', ''), after_dr,
                            follow_ups, 'DRキャンセル'
                        ))

                # 通常ヒット後のリンク
                hit_adv_val = _parse_frame_value(combo_row.get('hit_adv'))
                if hit_adv_val and hit_adv_val >= 3:
                    follow_ups_hit = _find_combo_follow_ups(
                        chara, hit_adv_val - 1, exclude_input=actual_input, include_special=True
                    )
                    if follow_ups_hit:
                        sections.append(_fmt_combo_route(
                            actual_input, combo_row.get('name', ''), hit_adv_val,
                            follow_ups_hit, '通常ヒット'
                        ))

                # 派生技の表示 (常に表示。割り込み判定は派生キーワードがある場合のみ計算)
                rq = intent.get("raw_query", "")
                _show_gap = any(kw in rq for kw in ['派生', '割り込', '連携', 'follow', 'blockstring', 'block string'])
                if True:  # 常に派生技を確認する
                    import re as _re
                    base_inp = _re.sub(r'[LMH]([KP])', r'\1', actual_input)
                    map_r = get_client().table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
                    sc_chara_val = map_r.data[0]['sc_chara'] if map_r.data else chara
                    follow_res = get_client().table('sc_moves').select(
                        'input,name,startup,hit_adv,block_adv,notes'
                    ).eq('chara', sc_chara_val).ilike('input', f'{base_inp}~%').execute()
                    if follow_res.data:
                        blk_adv_f = sc_row.get('block_adv_f')
                        hit_adv_f_v = sc_row.get('hit_adv_f')
                        heading = "【派生技フレームデータと割り込み判定】" if _show_gap else "【派生技フレームデータ】"
                        lines = [heading]
                        for fr in follow_res.data:
                            raw_st = fr.get('startup', '')
                            st_m = re.search(r'\d+', str(raw_st))
                            st_f = int(st_m.group()) if st_m else None
                            lines.append(
                                f"  {fr['input']} ({fr.get('name','?')}): "
                                f"発生{fr.get('startup','?')}F "
                                f"ヒット時{fr.get('hit_adv','?')} "
                                f"ガード時{fr.get('block_adv','?')}"
                            )
                            if _show_gap and st_f is not None:
                                if hit_adv_f_v is not None and hit_adv_f_v > 0:
                                    gap_hit = st_f - hit_adv_f_v
                                    lines.append(f"    → ヒット後の隙間: {gap_hit}F" + (
                                        " (確定連携)" if gap_hit <= 0 else
                                        f" (発生{gap_hit}F以下で割り込み可 / カウンターヒットに注意)" if gap_hit <= 4 else
                                        f" (発生{gap_hit}F以下で割り込み可)"
                                    ))
                                if blk_adv_f is not None and blk_adv_f < 0:
                                    gap_blk = abs(blk_adv_f) - st_f
                                    lines.append(f"    → ガード後の隙間: {gap_blk}F" + (
                                        " (真の連携 / 割り込み不可)" if gap_blk <= 0 else
                                        " (3F以下のため事実上割り込み不可)" if gap_blk <= 3 else
                                        f" (発生{gap_blk}F以下で割り込み可)"
                                    ))
                            if fr.get('notes'):
                                lines.append(f"    解説: {fr['notes'][:150]}")
                        sections.append('\n'.join(lines))
    elif intent_type == "combo_info" and not chara:
        sections.append("[コンボ情報: キャラ名と技名を指定してください。]")

    # --- lookup_move / punish_check: 必殺技名検索 (input が None の場合) ---
    if intent_type in ("lookup_move", "punish_check") and chara and not inp and move_name:
        row = _fetch_move_by_name(chara, move_name, raw_query=intent.get("raw_query",""))
        if row:
            sections.append(_fmt_sc_move(row))
            if intent_type == "punish_check":
                blk = row.get("block_adv_f")
                if blk is not None:
                    if blk <= -1:
                        sections.append(
                            f"【反撃判定】ガード時 {_sign(blk)}F → "
                            f"発生 {abs(blk)}F 以内の技なら確定反撃が入ります。"
                        )
                    else:
                        sections.append(
                            f"【反撃判定】ガード時 {_sign(blk)}F → "
                            f"ガードした側が不利のため反撃はできません。"
                        )
                # 派生技の追加フレームデータ (割り込み判断に必要)
                rq = intent.get("raw_query", "")
                if any(kw in rq for kw in ['派生', 'フォロー', '割り込', '繋ぎ', 'follow']):
                    sc_inp = row.get('input', '')
                    if sc_inp:
                        import re as _re
                        # '236MK' → '236K' のように強度修飾子を除去してベース入力を取得
                        base_inp = _re.sub(r'[LMH]([KP])', r'\1', sc_inp)
                        map_r = get_client().table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
                        sc_chara_val = map_r.data[0]['sc_chara'] if map_r.data else chara
                        # ベース入力の派生技を検索 (236K~% で 236K~6LK/MK/HK をキャッチ)
                        follow_res = get_client().table('sc_moves').select(
                            'input,name,startup,hit_adv,block_adv,notes'
                        ).eq('chara', sc_chara_val).ilike('input', f'{base_inp}~%').execute()
                        if follow_res.data:
                            hit_adv_f  = row.get('hit_adv_f')
                            block_adv_f = row.get('block_adv_f')
                            lines = ["【派生技フレームデータと割り込み判定】"]
                            for fr in follow_res.data:
                                raw_st = fr.get('startup', '')
                                st_m = re.search(r'\d+', str(raw_st))
                                st_f = int(st_m.group()) if st_m else None
                                lines.append(
                                    f"  {fr['input']} ({fr.get('name','?')}): "
                                    f"発生{fr.get('startup','?')}F "
                                    f"ヒット時{fr.get('hit_adv','?')} "
                                    f"ガード時{fr.get('block_adv','?')}"
                                )
                                if st_f is not None:
                                    # ヒット後の隙間計算
                                    if hit_adv_f is not None and hit_adv_f > 0:
                                        gap_hit = st_f - hit_adv_f
                                        if gap_hit <= 0:
                                            lines.append(f"    → ヒット後: 相手は行動できない (確定連携)")
                                        elif gap_hit <= 3:
                                            lines.append(
                                                f"    → ヒット後の隙間: {gap_hit}F "
                                                f"(最速技でも当てられない or カウンターヒット確定)"
                                            )
                                        else:
                                            lines.append(
                                                f"    → ヒット後の隙間: {gap_hit}F "
                                                f"(発生{gap_hit}F以下の技で割り込み可能)"
                                            )
                                    # ガード後の隙間計算
                                    if block_adv_f is not None and block_adv_f < 0:
                                        gap_blk = abs(block_adv_f) - st_f
                                        if gap_blk <= 0:
                                            lines.append(f"    → ガード後: 真の連携 (割り込み不可)")
                                        elif gap_blk <= 3:
                                            lines.append(
                                                f"    → ガード後の隙間: {gap_blk}F "
                                                f"(3F以下のため事実上割り込み不可)"
                                            )
                                        else:
                                            lines.append(
                                                f"    → ガード後の隙間: {gap_blk}F "
                                                f"(発生{gap_blk}F以下の技で割り込み可能)"
                                            )
                                if fr.get('notes'):
                                    lines.append(f"    解説: {fr['notes'][:150]}")
                            sections.append('\n'.join(lines))
        else:
            sections.append(
                f"⚠ {chara} の '{move_name}' のデータが見つかりませんでした。"
                f"技名のスペルや表記をご確認ください。"
            )

    # --- lookup_move / punish_check / compare_moves: フレームデータ取得 (通常技) ---
    if intent_type in ("lookup_move", "punish_check", "compare_moves") and chara and inp:
        row = _fetch_move(chara, inp)
        if row:
            sections.append(_fmt_move(row))
            # cancel・DR情報も追加 (キャンセル・コンボに関する質問への対応)
            combo_row = _fetch_combo_data(chara, inp)
            if combo_row and (combo_row.get('cancel') or combo_row.get('dr_cancel_hit')):
                cancel_raw = combo_row.get('cancel', '-')
                cancel_str = _parse_cancel(cancel_raw)
                extra = [f"【キャンセル情報】"]
                extra.append(f"キャンセル可: {cancel_str}")
                dr_hit  = combo_row.get('dr_cancel_hit')
                adr_hit = combo_row.get('after_dr_hit')
                if dr_hit:
                    extra.append(f"DRキャンセル(ヒット時) → ラッシュ到着後: {adr_hit or '?'}")
                dr_blk  = combo_row.get('dr_cancel_blk')
                adr_blk = combo_row.get('after_dr_blk')
                if dr_blk:
                    extra.append(f"DRキャンセル(ガード時) → ラッシュ到着後: {adr_blk or '?'}")
                sections.append('\n'.join(extra))
            # punish_check の場合: ガード有利Fから「反撃できるか」を明示
            if intent_type == "punish_check":
                blk = row.get("sc_block_adv") or row.get("c_on_block")
                if blk is not None:
                    if blk <= -1:
                        sections.append(
                            f"【反撃判定】ガード時 {_sign(blk)}F → "
                            f"発生 {abs(blk)}F 以内の技なら確定反撃が入ります。"
                        )
                    else:
                        sections.append(
                            f"【反撃判定】ガード時 {_sign(blk)}F → "
                            f"ガードした側が不利のため反撃はできません。"
                        )
        else:
            sections.append(
                f"⚠ {chara} の {inp} のデータが見つかりません。"
                f"通常技 (立ち/しゃがみ/ジャンプ + 弱/中/強 + P/K) のみ対応しています。"
                f"必殺技・SAのデータは M2 以降で対応予定です。"
            )
    elif intent_type == "punish_check" and chara and not inp and not move_name:
        # 技名も input も特定できない場合のみエラー表示
        sections.append(
            f"⚠ {chara} の指定された技のデータが見つかりませんでした。"
            f"技名または numpad 表記 (例: 2HK, 623HP) で指定してください。"
        )

    # --- compare_moves: 2つ目の技 (通常技) ---
    if intent_type == "compare_moves" and chara2 and inp2:
        row2 = _fetch_move(chara2, inp2)
        if row2:
            sections.append(_fmt_move(row2))
        else:
            sections.append(f"⚠ {chara2} の {inp2} のデータが見つかりません。")

    # --- compare_moves: 必殺技同士の比較 ---
    if intent_type == "compare_moves" and move_name and not inp:
        chara_for_name = chara or chara2
        if chara_for_name and move_name:
            row_s = _fetch_move_by_name(chara_for_name, move_name)
            if row_s:
                sections.append(_fmt_sc_move(row_s))
    if intent_type == "compare_moves" and move_name2:
        chara_for_name2 = chara2 or chara
        if chara_for_name2:
            row_s2 = _fetch_move_by_name(chara_for_name2, move_name2)
            if row_s2:
                sections.append(_fmt_sc_move(row_s2))

    # --- explain_concept / general_question: doc_chunks をベクトル検索 ---
    if intent_type in ("explain_concept", "general_question") and provider is not None:
        # concept と raw_query の両方でキーワード抽出できるよう、raw_query を優先使用
        # (例: "パニッシュカウンターとカウンターヒットの違いは?" → 両方を検索)
        search_query = intent.get("raw_query") or concept or ""
        chunks = await _search_docs(search_query, provider, count=4)
        if chunks:
            for chunk in chunks:
                sections.append(_fmt_doc_chunk(chunk))
        else:
            sections.append(
                f"[ゲームシステム文書に '{search_query}' の関連情報が見つかりませんでした。"
                f"あなた自身の SF6 知識で補足してください。]"
            )

    # --- lookup_move / punish_check でも関連ゲーム概念文書を追加 ---
    # (例: パニカン → Punish Counter の文書も参照)
    if intent_type in ("lookup_move", "punish_check") and provider is not None:
        field = intent.get("field")
        extra_query = None
        if field == "punish_adv" or intent_type == "punish_check":
            extra_query = "Punish Counter frame advantage"
        if extra_query:
            extra_chunks = await _search_docs(extra_query, provider, threshold=0.55, count=1)
            if extra_chunks:
                sections.append(_fmt_doc_chunk(extra_chunks[0]))

    # --- データが何もない場合 ---
    if not sections:
        sections.append("[参照データなし: あなた自身の知識で回答してください。]")

    return "\n\n---\n\n".join(sections)


# ============================================================
# 最終回答生成プロンプト
# ============================================================

ANSWER_SYSTEM = """\
あなたはStreet Fighter 6の対戦アシスタントです。
以下のルールを厳守してください:

1. 「参照データ」に含まれる情報のみに基づいて回答する
2. データにない情報は「このデータには含まれていません」と明示する
3. 数値を引用する際は具体的な数字を必ず含める (例: 「11F」「-12F」)
4. 「⚠」で始まる行はデータが見つからないことを示す — その場合は正直に伝える
5. 「【反撃判定】」セクションがある場合は、そこに書かれた判定を直接引用して回答すること。
   「直接的な記述がありません」と言ってはいけない — 反撃判定の行が記述そのものである。
6. 「コンボ解説:」や「解説:」フィールドが英語であっても、その内容に基づいて日本語で回答すること。
   英語で書かれているからといって「データに含まれていません」と言ってはいけない。
7. 「キャンセル:」フィールドは技のキャンセル可否を示す — 直接引用して回答すること。
8. 【コンボルート分析】セクションがある場合:
   - 「繋がる技」リストの ✅ 項目を実際のコンボ候補として回答すること
   - 「※ 推論根拠」の内容を説明に含め、なぜ繋がるかを明示すること
   - 特にKD (ノックダウン) する技があれば、それを最も推奨として挙げること
9. 実戦的なアドバイスは、解説テキスト (解説:) がある場合のみ行う
10. ハルシネーション厳禁: 根拠のない数値や技名を生成しない
11. 回答は構造的に (コンボルートには番号や箇条書きを使う)
12. 【派生技フレームデータ】セクションと「反撃判定」が両方ある場合 (割り込み・フレームトラップ判定):
    - 親技のヒット有利 (+N) と派生技の発生F を使って「隙間」を計算する
    - 隙間 = 派生発生F − ヒット有利 (正数なら相手が行動できるフレーム数)
    - 派生技の notes に "counter-hit any opponent button" があれば「割り込めばカウンターヒットを食らう」と回答する
    - ガード時の計算: 隙間 = |ガード不利| − 派生発生F (0以下なら真の連携、割り込み不可)
    - ヒット時・ガード時の両方について言及し、「押せるが〜」「割り込み不可」を明確に述べること
13. 【セットプレイ分析】セクションがある場合:
    - 各アクション (前ステップ等) ごとの残り有利Fと択を箇条書きで示すこと
    - 「推論根拠」の計算式 (KD有利F − アクションF = 残りF) を説明に含めること
    - 投げ択と打撃択の両方について言及し、起き攻めの「択」として整理すること
    - KD後と通常ヒット後で異なる場合は区別して回答すること
"""

ANSWER_TEMPLATE = """\
## 参照データ
{context}

## 質問
{query}
"""


async def generate_answer(
    query: str,
    context: str,
    provider,
) -> str:
    """参照データとユーザー質問から最終回答を生成。

    Args:
        query   : 元のユーザー質問。
        context : build_context() が返したコンテキスト文字列。
        provider: LLMProvider インスタンス。

    Returns:
        str: ユーザーへの回答文字列。
    """
    prompt = ANSWER_TEMPLATE.format(context=context, query=query)
    resp = await provider.generate(prompt=prompt, system=ANSWER_SYSTEM)
    return resp.text.strip()
