"""RAG Context Builder: Intent → Supabase クエリ → コンテキスト文字列。

Intent Parser の出力に応じて unified_moves / sc_move_normalized から
フレームデータを取得し、LLM への最終プロンプトに埋め込むコンテキストを組み立てる。

Phase 2 (文書取込) 完了後は doc_chunks からもコンテキストを追加できるよう
設計している。現時点では Phase 1 データ (フレームデータ) のみ対応。
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from sf6_engine.db import get_client
from sf6_engine.token_usage import usage_label
from sf6_engine.ufd import fetch_ufd_details, format_ufd_details

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
    '強フラッシュナックル': 'Flash Knuckle',
    '中フラッシュナックル': 'Flash Knuckle',
    '弱フラッシュナックル': 'Flash Knuckle',
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

# CAPCOM 技名の強度/SA prefix と条件タグ (special_move_map のファミリー抽出用)
_CAPCOM_PREFIX_RE = re.compile(r'(?:^|\s)(弱|中|強|OD|SA1|SA2|SA3|CA)\s*')
_CAPCOM_COND_RE = re.compile(r'(【[^】]*】|\[[^\]]*\]|（[^）]*）)')
# ファミリー選択の優先順 (強度ヒントがない場合は無印 → 弱 → … の順で採用)
_CAPCOM_PREFIX_RANK = {None: 0, '弱': 1, '中': 2, '強': 3, 'OD': 4,
                       'SA1': 5, 'SA2': 6, 'SA3': 7, 'CA': 8}


def _capcom_family(move_name: str) -> str:
    """CAPCOM 技名から強度prefix・条件タグ・注釈を除いたファミリー名。"""
    name = _CAPCOM_COND_RE.sub('', move_name)
    name = _CAPCOM_PREFIX_RE.sub(' ', name)
    return re.sub(r'\s+', ' ', name).strip()


def _capcom_prefix(move_name: str) -> str | None:
    """CAPCOM 技名の強度/SA prefix (OD 優先)。"""
    found = _CAPCOM_PREFIX_RE.findall(_CAPCOM_COND_RE.sub('', move_name))
    if not found:
        return None
    return 'OD' if 'OD' in found else found[0]


@lru_cache(maxsize=64)
def _get_special_map(capcom_slug: str) -> tuple:
    """special_move_map のキャラ分を取得 (プロセス内キャッシュ)。

    テーブル未作成 (migration 未適用) の場合は空を返して既存経路にフォールバック。
    """
    try:
        res = get_client().table('special_move_map').select(
            'capcom_move_name,sc_chara,sc_input,sc_name'
        ).eq('capcom_slug', capcom_slug).execute()
        return tuple(tuple(sorted(r.items())) for r in (res.data or []))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"special_move_map 取得失敗 (未適用?): {e}")
        return ()


def _fetch_sc_by_input(sc_chara: str, sc_input: str, sc_name: str | None = None) -> dict | None:
    """sc_move_normalized から (chara, input[, name]) 完全一致で1件取得。"""
    q = get_client().table('sc_move_normalized').select(_SC_MOVE_SELECT).eq(
        'chara', sc_chara).eq('input', sc_input)
    if sc_name:
        q = q.eq('name', sc_name)
    res = q.execute()
    return res.data[0] if res.data else None


# --- 入力表記の正規化とバリアントグルーピング ---
# SuperCombo はひとつの技の「条件違い」を別行として収録する:
#   ボタンホールド:   236[LK] / 5[HP]        部分ため:       236{P} / 214{K}
#   スタンス prefix:  W.623MP (Windclad)     パーフェクト:   pf.214[MP]
#   入力欄への注釈:   22P (hold) / 214HK (Air Current)
#   代替表記:         5/6KK / MPMK or 66
# これらを [] {} 注釈 prefix を剥がした「正規化キー」で同一グループに束ねる。
# 注: [4]6P / [2]8K / 6[6] のような数字の [] はチャージ"コマンド"なので剥がさない。

_INPUT_ANNOT_RE = re.compile(r'\s*\([^)]*\)')                 # '22P (hold)' → '22P'
_BTN_HOLD_RE = re.compile(r'[\[{]([LMH]?[PK]{1,2})[\]}]')     # 236[LK]/236{P} → 236LK/236P
_STANCE_PREFIX_RE = re.compile(r'^(?:W\.|pf\.)')              # W.623MP / pf.214MP


def _canon_input_keys(inp: str) -> tuple[str, ...]:
    """入力表記から正規化キーの組を生成する ('5/6KK' → ('5KK', '6KK'))。"""
    s = _INPUT_ANNOT_RE.sub('', inp or '').strip()
    s = _BTN_HOLD_RE.sub(r'\1', s)
    s = _STANCE_PREFIX_RE.sub('', s)
    if not s:
        return ()
    parts = [p.strip() for p in s.split(' or ')] if ' or ' in s else [s]
    keys: list[str] = []
    for p in parts:
        m = re.match(r'^(\d)/(\d)(.*)$', p)   # '5/6KK' → 5KK と 6KK
        if m:
            keys += [m.group(1) + m.group(3), m.group(2) + m.group(3)]
        else:
            keys.append(p)
    # 1文字キー ('4 or 6 + PPP/KKK' の '4' 等) は照合キーとして弱すぎるため除外
    return tuple(dict.fromkeys(k for k in keys if len(k) >= 2))


def _variant_label(row: dict) -> str:
    """バリアント行の条件ラベルを技名の括弧修飾と入力表記から決定論的に導出。"""
    name = row.get('name') or ''
    inp = row.get('input') or ''
    quals = re.findall(r'\(([^)]+)\)', name)
    for q in quals:
        ql = q.lower()
        if 'hold' in ql and 'partial' not in ql:
            return 'ため (ホールド) 版'
        if 'partial' in ql:
            return '部分ため版'
        if 'charge' in ql:
            return f'ため版 ({q})'
        if ql.startswith('lv'):
            return f'{q} 版'
        if ql.startswith('dl'):
            return f'飲酒レベル {q}'
        if ql == 'windclad':
            return 'ウィンドクラッド (風まとい) 版'
        if ql == 'air current':
            return 'エアカレント (風あり) 版'
        if ql == 'enhanced':
            return '強化版'
        if ql == 'perfect':
            return 'パーフェクト (ジャスト入力) 版'
        if ql == 'flame':
            return '炎まとい版'
        if ql == 'mine':
            return 'マイン設置時'
    if _BTN_HOLD_RE.search(inp) and '{' not in inp:
        return 'ため (ホールド) 版'
    if '{' in inp:
        return '部分ため版'
    if inp.startswith('W.'):
        return 'ウィンドクラッド (風まとい) 版'
    if inp.startswith('pf.'):
        return 'パーフェクト (ジャスト入力) 版'
    if '(hold' in inp.lower():
        return 'ため (ホールド) 版'
    if _INPUT_ANNOT_RE.search(inp):
        return f'{_INPUT_ANNOT_RE.search(inp).group(0).strip()} 版'
    if quals:
        return f'{quals[-1]} 版'
    return '通常版'


# バリアントとして束ねない move_type (Drive Parry と Drive Rush Cancel が
# 'MPMK' キーで誤って対になる等を防ぐ)
_VARIANT_EXCLUDE_TYPES = {'drive', 'taunt'}


def _fetch_variant_group(sc_chara: str, sc_input: str,
                         exclude_name: str | None = None) -> list[dict]:
    """同キャラで正規化キーが交差する別バリアント行を取得。

    Args:
        sc_chara:     SuperCombo キャラ名 ("Ed" 等)。大文字小文字は無視。
        sc_input:     基準となる入力 ("236LK" / "236[LK]" / "6KK" 等)。
        exclude_name: 基準行の技名。同一入力で名前違いの行 (Jamie の飲酒レベル等)
                      をバリアントとして残すために使う。None なら同一入力は全て除外。

    Returns:
        list[dict]: バリアント行 (基準行自身は含まない)。なければ空リスト。
    """
    keys = set(_canon_input_keys(sc_input))
    if not sc_chara or not keys:
        return []
    # セカンダリキー: 強度修飾子を落としたキー ('236LP' → '236P')。
    # 通常版が強度別 (236LP/MP/HP)、ため版が強度なし (236{P}/[P]) で
    # 収録される非対称ケース (Akuma 波動拳等) を拾う。強度違いの通常版同士が
    # 誤って束にならないよう、バリアントラベル付きの行にのみ適用する。
    generic_keys = {re.sub(r'(\d)[LMH]([PK])', r'\1\2', k) for k in keys} - keys
    try:
        res = get_client().table('sc_move_normalized').select(_SC_MOVE_SELECT).ilike(
            'chara', sc_chara).execute()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"バリアント検索失敗: {e}")
        return []
    out = []
    for r in res.data or []:
        if (r.get('move_type') or '').lower() in _VARIANT_EXCLUDE_TYPES:
            continue
        inp = r.get('input') or ''
        if inp == sc_input and (exclude_name is None or r.get('name') == exclude_name):
            continue
        rkeys = set(_canon_input_keys(inp))
        if keys & rkeys:
            out.append(r)
        elif generic_keys & rkeys and _variant_label(r) != '通常版':
            out.append(r)
    return out


def _fmt_variant_section(picked_input: str, variants: list[dict]) -> str:
    """バリアント行のコンテキストセクションを生成。"""
    lines = [
        f"【バリアントあり】{picked_input} には条件・操作によって性能が変わる"
        f"別バージョンがあります ([] はボタンホールド=ためる、{{}} は部分ための意):"
    ]
    for v in variants:
        lines.append("")
        lines.append(f"▼ {_variant_label(v)}:")
        lines.append(_fmt_sc_move(v))
    return '\n'.join(lines)


def _fetch_special_by_jp(capcom_slug: str, move_name: str, raw_query: str) -> dict | None:
    """CAPCOM 公式日本語名で special_move_map を引き、SC 行を返す (ステップ0)。

    move_name / raw_query のどちらかに CAPCOM ファミリー名 (例: ロン・ポワン,
    タイガーアッパーカット) が含まれていれば、強度ヒント (弱/中/強/OD/SA1-3)
    を prefix と照合して該当バリアントの SC フレームデータを取得する。
    """
    map_rows = [dict(r) for r in _get_special_map(capcom_slug.lower())]
    if not map_rows:
        return None
    text = f"{move_name} {raw_query}"

    # ファミリー名が text に含まれる行を候補に (最長一致を優先)
    cands: list[tuple[int, dict]] = []
    for r in map_rows:
        fam = _capcom_family(r['capcom_move_name'])
        if len(fam) >= 2 and fam in text:
            cands.append((len(fam), r))
    if not cands:
        return None
    max_len = max(l for l, _ in cands)
    cands2 = [r for l, r in cands if l == max_len]

    # 強度ヒント: text 中の 弱/中/強/OD/SA1-3/CA を prefix と照合
    hint = None
    m = re.search(r'(?<![A-Za-z0-9])(OD|SA1|SA2|SA3|CA)(?![A-Za-z0-9])|オーバードライブ|[弱中強]', text)
    if m:
        hint = 'OD' if m.group(0) == 'オーバードライブ' else m.group(0)
    if hint:
        hinted = [r for r in cands2 if _capcom_prefix(r['capcom_move_name']) == hint]
        if hinted:
            cands2 = hinted

    cands2.sort(key=lambda r: _CAPCOM_PREFIX_RANK.get(
        _capcom_prefix(r['capcom_move_name']), 9))
    for r in cands2:
        row = _fetch_sc_by_input(r['sc_chara'], r['sc_input'], r.get('sc_name'))
        if row:
            logger.info(
                f"special_move_map hit: '{r['capcom_move_name']}' → {r['sc_input']}")
            return row
    return None


def _fetch_by_alias(capcom_slug: str, sc_chara: str, move_name: str, raw_query: str) -> dict | None:
    """学習済みエイリアス (move_aliases) で検索する (ステップ0.5)。

    alias は強度なしのファミリー名で保存されているため、ヒット後は
    sc_name_family の ILIKE 検索 + _pick_variant で強度を解決する。
    """
    try:
        res = get_client().table('move_aliases').select(
            'alias,sc_name_family').eq('sc_chara', sc_chara).execute()
        aliases = res.data or []
    except Exception as e:  # noqa: BLE001
        logger.debug(f"move_aliases 取得失敗 (未適用?): {e}")
        return None
    text = f"{move_name} {raw_query}"
    hits = [a for a in aliases if a['alias'] and a['alias'] in text]
    if not hits:
        return None
    hits.sort(key=lambda a: -len(a['alias']))  # 最長エイリアス優先
    family = hits[0]['sc_name_family']
    res = get_client().table('sc_move_normalized').select(_SC_MOVE_SELECT).eq(
        'chara', sc_chara).ilike('name', f'%{family}%').execute()
    if not res.data:
        return None
    logger.info(f"move_aliases hit: '{hits[0]['alias']}' → '{family}'")
    return _pick_variant(_sort_by_type(res.data), raw_query, family)


def _fetch_move_by_name(chara: str, move_name: str, raw_query: str = '') -> dict | None:
    """必殺技・SA 名で sc_move_normalized を検索する。

    検索順序 (ハードコーディングを排除して汎用化):
    0. special_move_map (CAPCOM 公式日本語名 → SC input) — 全キャラ対応の主経路
    0.5. move_aliases (Discord bot 等で学習したコミュニティ略称)
    1. Special/Super タイプに絞って move_name を直接 ILIKE 検索 (英語名向け)
    2. move_name を単語分割して各単語で Special/Super 検索
    3. _JP_MOVE_TO_EN マッピング経由 (LLM誤訳のリカバリー、最終フォールバック)
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

    # 0. CAPCOM 公式日本語名 (special_move_map) — LLM の翻訳を経由しない主経路
    row = _fetch_special_by_jp(chara, move_name, raw_query)
    if row:
        return row

    # 0.5. 学習済みエイリアス (コミュニティ略称)
    row = _fetch_by_alias(chara, sc_chara, move_name, raw_query)
    if row:
        return row

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
    # 複数の単語が残っている場合は他の単語で絞り込みを行う (例: "Tiger" → Tiger Shot/Knee/Uppercut が混在するためさらに絞る)
    for word in move_name.split():
        if len(word) >= 3:
            rows = search_special(word)
            if rows:
                # 残り単語でさらに絞り込む
                other_words = [w for w in move_name.split() if w != word and len(w) >= 3]
                if other_words:
                    narrowed = [
                        r for r in rows
                        if any(w.lower() in (r.get('name', '') or '').lower() for w in other_words)
                    ]
                    if narrowed:
                        rows = narrowed
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


def _block_adv_line(v: int) -> str:
    """ガード時有利を両視点で明示する行 (視点取り違え防止)。"""
    return (f"ガード時: {_sign(v)}F (技を出した側が{_sign(v)}F / "
            f"ガードした側は{_sign(-v)}F)")


def _hit_adv_line(v: int) -> str:
    """ヒット時有利を両視点で明示する行。"""
    return (f"ヒット時: {_sign(v)}F (技を当てた側が{_sign(v)}F / "
            f"食らった側は{_sign(-v)}F)")


def _fmt_sc_move(row: dict) -> str:
    """sc_move_normalized の行 (必殺技用) をコンテキスト用テキストに変換。"""
    name = row.get('name', '不明')
    inp  = row.get('input', '?')
    chara = row.get('chara', '?')
    lines = [f"【{chara} / {name} ({inp})】"]

    if row.get('startup_f') is not None:
        lines.append(f"発生: {row['startup_f']}F")
    if row.get('block_adv_f') is not None:
        lines.append(_block_adv_line(row['block_adv_f']))
    if row.get('hit_adv_f') is not None:
        if row.get('hit_is_knockdown'):
            lines.append(f"ヒット時: KD (ノックダウン)")
        else:
            lines.append(_hit_adv_line(row['hit_adv_f']))
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
        notes = row['notes']
        lines.append(f"解説: {notes[:400]}" + ("…" if len(notes) > 400 else ""))

    ufd = fetch_ufd_details(
        _ufd_slug_for_sc_chara(chara), sc_input=inp, move_name=name
    )
    if ufd:
        lines.append(format_ufd_details(ufd))

    return "\n".join(lines)


@lru_cache(maxsize=64)
def _ufd_slug_for_sc_chara(chara: str) -> str:
    """SuperComboキャラ名をUFDの保存キー(CAPCOM slug)へ解決する。"""
    try:
        result = (
            get_client().table("char_slug_map").select("capcom_slug")
            .eq("sc_chara", chara).limit(1).execute()
        )
        if result.data:
            return result.data[0]["capcom_slug"]
    except Exception as exc:  # pragma: no cover - DB一時障害時の安全なフォールバック
        logger.debug("UFD character mapping unavailable: %s", exc)
    return chara.lower().replace(".", "").replace("_", "")


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
            lines.append(f"ヒット時: {hit} (ノックダウン — 食らった側はダウンする)")
            lines.append(f"  → 相手が起き上がるまで {hit} の有利があります")
        else:
            hv = _parse_frame_value(hit)
            if hv is not None:
                lines.append(_hit_adv_line(hv))
            else:
                lines.append(f"ヒット時有利: {_sign_str(hit)} (技を当てた側の視点)")

    # パニッシュカウンター有利
    pc = row.get('punish_adv', '')
    if pc and str(pc).strip() not in ('', '-'):
        if 'KD' in str(pc) or 'HKD' in str(pc):
            lines.append(f"パニッシュカウンター時: {pc} (ハードノックダウン)")
        else:
            lines.append(f"パニッシュカウンター時有利: {_sign_str(pc)} (技を当てた側の視点)")

    # ガード有利
    blk = row.get('block_adv', '')
    if blk:
        bv = _parse_frame_value(blk)
        if bv is not None:
            lines.append(_block_adv_line(bv))
        else:
            lines.append(f"ガード時有利: {_sign_str(blk)} (技を出した側の視点)")

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

    # ガード有利 (両視点を明示: 視点取り違え防止)
    c_blk = row.get("c_on_block")
    sc_blk = row.get("sc_block_adv")
    if c_blk is not None:
        lines.append(_block_adv_line(c_blk) + (f" (SC: {_sign(sc_blk)}F)" if sc_blk and sc_blk != c_blk else ""))
    elif sc_blk is not None:
        lines.append(_block_adv_line(sc_blk) + " (SCデータ)")

    # ヒット有利
    sc_hit = row.get("sc_hit_adv")
    c_hit  = row.get("c_on_hit")
    if sc_hit is not None:
        lines.append(_hit_adv_line(sc_hit))
    elif c_hit is not None:
        lines.append(_hit_adv_line(c_hit))

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

    ufd = fetch_ufd_details(
        chara_slug, sc_input=sc_input, move_name=move_name
    )
    if ufd:
        lines.append(format_ufd_details(ufd))

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

        with usage_label("embed"):
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

    # LLM が move_name を出力しなかった場合のフォールバック:
    # raw_query をそのまま渡すと、_fetch_move_by_name のステップ0
    # (special_move_map の日本語ファミリー containment 検索) が
    # クエリ文中の技名 (例: 「…弱ロン・ポワンを食らった時…」) を拾える。
    _MOVE_INTENTS = ("lookup_move", "combo_info", "punish_check",
                     "sequence_analysis", "setplay_analysis", "max_combo")
    if chara and not inp and not move_name and intent_type in _MOVE_INTENTS:
        rq = intent.get("raw_query", "")
        if rq:
            move_name = rq
            logger.info("move_name 欠落 → raw_query でファミリー検索を試行")

    sections: list[str] = []

    # --- sequence_analysis: 連携を共通タイムラインで決定論評価 ---
    if intent_type == "sequence_analysis":
        if not chara:
            return "⚠ 連携を解析するキャラクターが特定できませんでした。"
        sequence = intent.get("attacker_sequence") or []
        defender = intent.get("defender_action") or {}
        from sf6_engine.sequence_analysis import analyze_sequence

        result = analyze_sequence(
            chara,
            sequence,
            initial_interaction=intent.get("initial_interaction") or "block",
            defender_startup_f=defender.get("startup_f"),
            defender_character=defender.get("character"),
            defender_move=defender.get("move"),
            expected_outcome=intent.get("expected_outcome"),
            attacker_delay_f=(intent.get("attacker_timing") or {}).get("delay_f", 0),
            defender_delay_f=defender.get("delay_f", 0),
            query_targets=intent.get("query_targets"),
            terminal_interaction=(intent.get("terminal_state") or {}).get("interaction"),
            terminal_perspective=(intent.get("terminal_state") or {}).get("perspective", "both"),
        )
        if not result.get("found"):
            return f"⚠ {result.get('message') or '連携解析に必要なデータが不足しています。'}"
        return result["summary"]

    # --- query_moves: 条件を満たす技の集合を統合プロファイルで決定論検索 ---
    if intent_type == "query_moves":
        if not chara:
            return "⚠ 技を検索するキャラクターが特定できませんでした。"
        from sf6_engine.frame_data import query_frame_data

        move_filter = intent.get("move_filter") or {}
        result = query_frame_data(
            chara,
            field=move_filter.get("field") or "on_block",
            operator=move_filter.get("operator") or "gt",
            value=move_filter.get("value", 0),
            perspective=move_filter.get("perspective") or "attacker",
            scope=intent.get("move_scope") or "all",
            scenario=intent.get("scenario"),
        )
        return result.get("summary") or "⚠ 技条件検索の結果を取得できませんでした。"

    # Core frame-data questions use the same deterministic multi-source
    # profile as MCP/Discord.  This keeps CLI and bot source selection
    # identical and bypasses the legacy query-shape-dependent lookup paths.
    raw_query = intent.get("raw_query", "")
    core_frame_query = bool(re.search(
        r'発生|持続|硬直|ガード|ヒット|全体|性能|フレーム', raw_query
    ))
    typed_frame_path = bool(
        intent_type == "punish_check"
        or core_frame_query
        or intent.get("scenario")
    )
    if (intent_type in ("lookup_move", "punish_check") and chara
            and typed_frame_path and (inp or move_name)):
        from sf6_engine.frame_data import format_frame_profile_context, lookup_frame_data

        identifier = inp or move_name or raw_query
        lookup = lookup_frame_data(
            chara,
            identifier,
            scenario=intent.get("scenario"),
        )
        if lookup.get("found") and lookup.get("move"):
            move = lookup["move"]
            profile = move.get("frame_profile") or {}
            sections.append(format_frame_profile_context(profile))
            resolution = profile.get("resolution") or {}
            if resolution.get("status") != "resolved":
                sections.append(
                    "【回答制約】技が一意に解決されていません。数値を断定せず、"
                    f"次を確認してください: {resolution.get('clarification') or '正式名またはコマンド'}"
                )
                return "\n\n".join(sections)
            ufd = move.get("ufd") or {}
            if ufd.get("hitbox_source_url"):
                sections.append(f"当たり判定GIF (UFD): {ufd['hitbox_source_url']}")
            if intent_type == "punish_check":
                evaluation = profile.get("scenario_evaluation") or {}
                assessment = evaluation.get("punish_assessment") or {}
                contextual_block = (
                    evaluation.get("block_perspectives", {}).get("attacker") or {}
                )
                block_adv = (
                    contextual_block.get("value")
                    if contextual_block.get("usable_for_calculation") else None
                )
                if isinstance(block_adv, int) and assessment.get("frame_punishable"):
                    sections.append(
                        f"【反撃判定】攻撃側はガード時 {block_adv:+d}F、"
                        f"防御側は {-block_adv:+d}F → "
                        f"発生 {assessment.get('punish_window_f')}F 以内がフレーム上の候補です。"
                        "ガード後距離・押し戻し・技の到達は未検証なので、"
                        "確定反撃としては断定できません。"
                    )
                elif isinstance(block_adv, int):
                    sections.append(
                        f"【反撃判定】攻撃側はガード時 {block_adv:+d}Fのため、"
                        "防御側に確定反撃のフレームはありません。"
                    )
                else:
                    sections.append(
                        "【反撃判定】今回の条件で硬直差が単一値に確定しないため判定保留。"
                    )
            return "\n\n".join(sections)
        sections.append(
            f"⚠ {chara} の {identifier} の統合フレームデータが見つかりませんでした。"
        )
        return "\n\n".join(sections)

    # --- setplay_analysis: KD後・有利フレーム後の起き攻め択を計算 ---
    if intent_type == "setplay_analysis" and chara:
        raw_row: dict | None = None
        actual_inp = inp

        if inp:
            raw_row = _fetch_combo_data(chara, inp)
        elif move_name:
            # コマンド表記 (623HP, 236LK 等) の場合は sc_moves.input で直接検索
            if re.match(r'^[2-9]{3,}[LMH]?[PK]$', move_name):
                raw_row = _fetch_combo_data(chara, move_name)
                actual_inp = move_name
            if not raw_row:
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

                # 派生技の単体フレームデータ。キャンセル連携の割り込み可否は
                # 親技の on-block ではなくキャンセル開始と blockstun を使うため、
                # ここで単体値から隙間を逆算しない。
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
                        heading = "【派生技フレームデータ】"
                        lines = [heading]
                        if _show_gap:
                            lines.append(
                                "割り込み判定には起点技→派生技の連携指定とキャンセル窓が必要です。"
                                "単体の硬直差では判定しません。"
                            )
                        for fr in follow_res.data:
                            lines.append(
                                f"  {fr['input']} ({fr.get('name','?')}): "
                                f"発生{fr.get('startup','?')}F "
                                f"ヒット時{fr.get('hit_adv','?')} "
                                f"ガード時{fr.get('block_adv','?')}"
                            )
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
            variants = _fetch_variant_group(
                row.get('chara', ''), row.get('input', ''), exclude_name=row.get('name'))
            if variants:
                sections.append(_fmt_variant_section(row.get('input', ''), variants))
            if intent_type == "punish_check":
                blk = row.get("block_adv_f")
                if blk is not None:
                    if blk <= -1:
                        sections.append(
                            f"【反撃判定】ガード時 {_sign(blk)}F → "
                            f"発生 {abs(blk)}F 以内がフレーム上の候補です。"
                            "到達距離は未検証です。"
                        )
                    else:
                        sections.append(
                            f"【反撃判定】ガード時 {_sign(blk)}F → "
                            f"ガードした側が不利のため反撃はできません。"
                        )
                # 派生技の追加フレームデータ。単体技の硬直差だけではキャンセル
                # 連携の割り込み判定はできないため、誤った隙間計算を行わない。
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
                            lines = ["【派生技フレームデータ】"]
                            lines.append(
                                "割り込み判定には起点技→派生技の連携指定とキャンセル窓が必要です。"
                                "単体の硬直差では判定しません。"
                            )
                            for fr in follow_res.data:
                                lines.append(
                                    f"  {fr['input']} ({fr.get('name','?')}): "
                                    f"発生{fr.get('startup','?')}F "
                                    f"ヒット時{fr.get('hit_adv','?')} "
                                    f"ガード時{fr.get('block_adv','?')}"
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
            # バリアント (ため版等) があれば添付 (例: Ed 5HP → 5[HP] Psycho Knuckle)
            variants = _fetch_variant_group(chara, inp)
            if variants:
                sections.append(_fmt_variant_section(inp, variants))
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
                            f"発生 {abs(blk)}F 以内がフレーム上の候補です。"
                            "到達距離は未検証です。"
                        )
                    else:
                        sections.append(
                            f"【反撃判定】ガード時 {_sign(blk)}F → "
                            f"ガードした側が不利のため反撃はできません。"
                        )
        else:
            # unified_moves は通常技のみ → 必殺技入力 (236LK / 236[LK] 等) は
            # sc_move_normalized を直接検索するフォールバック
            row_s = None
            try:
                res_sc = get_client().table('sc_move_normalized').select(
                    _SC_MOVE_SELECT).ilike('chara', chara).eq('input', inp).execute()
                if res_sc.data:
                    row_s = _sort_by_type(res_sc.data)[0]
            except Exception as e:  # noqa: BLE001
                logger.debug(f"sc_move_normalized input検索失敗: {e}")
            if row_s is None:
                # 完全一致ミス → 正規化キー一致で再検索
                # ('6KK' → '5/6KK' Kill Rush, '22P' → '22P (hold)' 等)
                cands = _fetch_variant_group(chara, inp)
                if cands:
                    base = [r for r in cands if _variant_label(r) == '通常版']
                    row_s = _sort_by_type(base or cands)[0]
                    logger.info(
                        f"Canonical input match: '{inp}' → '{row_s.get('input')}'"
                    )
            if row_s:
                sections.append(_fmt_sc_move(row_s))
                variants = _fetch_variant_group(
                    row_s.get('chara', ''), row_s.get('input', ''),
                    exclude_name=row_s.get('name'))
                if variants:
                    sections.append(_fmt_variant_section(row_s.get('input', ''), variants))
                if intent_type == "punish_check":
                    blk = row_s.get("block_adv_f")
                    if blk is not None:
                        if blk <= -1:
                            sections.append(
                                f"【反撃判定】ガード時 {_sign(blk)}F → "
                                f"発生 {abs(blk)}F 以内がフレーム上の候補です。"
                                "到達距離は未検証です。"
                            )
                        else:
                            sections.append(
                                f"【反撃判定】ガード時 {_sign(blk)}F → "
                                f"ガードした側が不利のため反撃はできません。"
                            )
            else:
                sections.append(
                    f"⚠ {chara} の {inp} のデータが見つかりません。"
                    f"入力表記 (例: 5HP, 2MK, 236LK) または技名をご確認ください。"
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
12. 【派生技フレームデータ】だけからは、キャンセル連携の割り込み可否を計算・断定しないこと:
    - 親技の on-block / on-hit と派生技の発生を引き算して「隙間」を作ってはいけない
    - 割り込み判定は【連携解析】のキャンセル開始基準・blockstun/hitstun・派生技発生を優先する
    - 連携解析が無い場合は、起点技→派生技・ガード/ヒット・防御側の発生を確認して判定保留とする
    - notes の counter-hit 等は補足情報として引用してよいが、単体値から割り込み可否を推測しない
13. 「無敵:」フィールドが参照データにある場合、必ずその値 (例: '1-21 Air') を回答に含めること。
    「データに含まれていません」と言ってはいけない — 無敵フレームの行が記述そのものである。
14. 【セットプレイ分析】セクションがある場合:
    - 各アクション (前ステップ等) ごとの残り有利Fと択を箇条書きで示すこと
    - 「推論根拠」の計算式 (KD有利F − アクションF = 残りF) を説明に含めること
    - 投げ択と打撃択の両方について言及し、起き攻めの「択」として整理すること
    - KD後と通常ヒット後で異なる場合は区別して回答すること
15. 【最重要: フレーム有利の視点】「ガード時: +N」「ヒット時: +N」は常に
    **技を出した側 (攻撃側) の視点**の数値である。参照データには両視点が
    括弧書きされているので、質問に該当する側の数値を**そのまま引用**する
    (自分で符号を計算し直さないこと)。視点の判別ルール:
    - 質問者が技を**受ける側** (「Xをガードした時」「Xをガードしたら」「Xを食らった時」):
      → 「ガードした側は−NF」「食らった側は−NF」の数値で答える
      例: ガード時+2Fの技に「ガードした時何F有利?」→「ガードした側は -2F (2フレーム不利) です」
    - 質問者が技を**出す側** (「Xはガードされた時」「Xがガードされると」「Xをガードさせた時」
      「Xのガード硬直差は」):
      → 技を出した側の数値で答える
      例: ガード時+2Fの技に「ガードされた時何F?」→「技を出した側が +2F 有利です」
      例: ガード時+2Fの技に「ガードさせた時何F有利?」→「技を出した側が +2F 有利です」
      注意: 「ガードさせた」(使役形) は攻撃側、「ガードした」は防御側 — 混同しないこと
    - 「XをガードしたA側」「ガードさせたB側」のように主語が明示されていれば主語に従う
    - 「## 視点判定」セクションがプロンプトにある場合は、その判定に**無条件で従う**こと
    どちらの視点で答えたかを回答の冒頭で必ず明記すること
16. 【バリアントあり】セクションがある場合 (ため版・レベル別・スタンス条件等):
    - 「## バリアント判定」セクションがプロンプトにあれば、指定された「▼ ラベル」の
      行の数値で答えること
    - 判定セクションが無ければ通常版のデータで答えた上で、バリアントが存在すること
      (と主な違い、例: ガード時有利の変化) に必ず一言触れる
    - 入力表記の [] はボタンを押し続ける (ためる)、{} は部分ための意味である
17. 【質問条件】または「条件適用後」の行がある場合:
    - 通常の表値ではなく、条件適用後の値と status を優先する
    - conditional_unresolved / invalid_condition / move_ambiguous は数値を推測せず、確認事項を返す
    - 「フレーム上の反撃候補」は距離・押し戻し・到達を証明していないため、
      「確定反撃」と断定せず、到達未検証の候補として説明する
18. 【連携解析】がある場合:
    - 単発技の通常フレームではなく、共通タイムライン、相打ち後有利、確認済み追撃を引用する
    - 攻撃側+N Fなら防御側-N Fという両視点を併記する
    - 「確認済み追撃」と単なる「フレーム上の候補」を混同しない
    - 相手技未指定の注意書きや距離未検証の条件を省略しない
"""


# 視点の決定論的判定 (ADR/メモ: gemma4 は「ガードさせた」(使役=攻撃側) と
# 「ガードした」(防御側) をプロンプトのルールだけでは安定して区別できない)
_ATTACKER_VIEW_RE = re.compile(
    r"ガードさせ|ガードされ|ヒットさせ|当てさせ|当てた(?:時|とき|場合|側|ら)|硬直差"
)
_DEFENDER_VIEW_RE = re.compile(
    r"ガードした|ガードして|[食喰く]らった|受けた(?:時|とき|場合|側|ら)"
)


# 質問文中の「バリアント条件語」→ バリアントラベルの対応 (決定論判定)
_VARIANT_COND_TO_LABEL: list[tuple[re.Pattern, str]] = [
    (re.compile(r'部分ため|部分タメ'), '部分ため版'),
    # 注: 「勝つために」等の目的の「ため」に誤マッチしないよう活用形を限定
    (re.compile(r'ため[たるて]|(?:最大|フル)ため|ため版|タメ|溜め|ホールド|長押し|押しっぱ'),
     'ため (ホールド) 版'),
    (re.compile(r'ウィンドクラッド|風まと'), 'ウィンドクラッド (風まとい) 版'),
    (re.compile(r'エアカレント'), 'エアカレント (風あり) 版'),
    (re.compile(r'飲酒|ドリンク|お酒|酔[いっ]|DL[0-9]'), '飲酒レベル'),
    (re.compile(r'パーフェクト|ジャスト入力'), 'パーフェクト (ジャスト入力) 版'),
    (re.compile(r'炎まと|フレイム'), '炎まとい版'),
    (re.compile(r'マイン'), 'マイン設置時'),
    (re.compile(r'(?:Lv\.?|レベル)\s*([0-9])'), 'Lv 指定版'),
]


# 質問フィールド判定: 質問語 → (表示名, コンテキストからの値抽出パターン群)
# 値抽出はテキスト形式 (build_context) と JSON 形式 (MCP move dict) の両対応
_FIELD_SPECS: list[tuple[re.Pattern, str, list[re.Pattern], bool]] = [
    # (質問語, 表示名, 抽出パターン, 数値検証するか)
    (re.compile(r'発生'), '発生',
     [re.compile(r'発生[:：]?\s*([0-9]+(?:\+[0-9]+)?)'),
      re.compile(r'"startup[^"]*"\s*:\s*"?([0-9]+(?:\+[0-9]+)?)')], True),
    (re.compile(r'持続'), '持続',
     [re.compile(r'持続[:：]?\s*([0-9~〜\-]+)'),
      re.compile(r'"active[^"]*"\s*:\s*"?([0-9~〜\-]+)')], True),
    (re.compile(r'硬直(?!差)'), '硬直',
     [re.compile(r'硬直[:：]?\s*([0-9]+)'),
      re.compile(r'"recovery[^"]*"\s*:\s*"?([0-9]+)')], True),
    (re.compile(r'ダメージ|威力'), 'ダメージ',
     [re.compile(r'ダメージ[:：]?\s*([0-9,x×]+)'),
      re.compile(r'"damage[^"]*"\s*:\s*"?([0-9,x×]+)')], True),
    (re.compile(r'無敵'), '無敵',
     [re.compile(r'無敵[:：]?\s*([^\n]+)'),
      re.compile(r'"invuln[^"]*"\s*:\s*"?([^",]+)')], False),
    (re.compile(r'リーチ|間合い|射程'), 'リーチ',
     [re.compile(r'リーチ[:：]?\s*([0-9.]+)'),
      re.compile(r'"(?:atk_)?range[^"]*"\s*:\s*"?([0-9.]+)')], False),
]
# 有利フレーム系の質問語 (これがある場合はフィールド専念の注意書きを付けない)
_ADV_QUERY_RE = re.compile(r'ガード|ヒット|有利|不利|硬直差|反撃|パニ')


def _detect_fields(query: str) -> list[tuple[str, list[re.Pattern], bool]]:
    """質問文が聞いているフィールドを列挙する。"""
    return [(label, pats, strict) for q_re, label, pats, strict in _FIELD_SPECS
            if q_re.search(query)]


def _field_directive(query: str) -> str:
    """質問フィールドの判定指示をプロンプト冒頭に置く (決定論判定)。

    「発生を聞かれたのにガード有利を答える」誤りを防ぐ。
    """
    fields = [label for label, _, _ in _detect_fields(query)]
    if not fields:
        return ''
    names = '」「'.join(fields)
    text = (
        "## 質問フィールド判定 (システムによる自動判定)\n"
        f"質問が聞いているのは「{names}」の値である。"
        f"参照データのその項目の値を転記して端的に答えること。"
    )
    if not _ADV_QUERY_RE.search(query):
        text += "\nガード時・ヒット時の有利フレームは質問されていないので回答の主役にしないこと。"
    return text


def _field_expected_values(query: str, context: str) -> dict[str, set[str]]:
    """質問フィールドごとに、コンテキストから抽出した正解候補値を返す。

    数値検証対象 (strict=True) のフィールドのみ。候補が取れないフィールドは含めない。
    """
    out: dict[str, set[str]] = {}
    for label, pats, strict in _detect_fields(query):
        if not strict:
            continue
        vals: set[str] = set()
        for p in pats:
            vals.update(m.group(1) for m in p.finditer(context))
        if vals:
            out[label] = vals
    return out


def _detect_variant_cond(query: str) -> tuple[str, str] | None:
    """質問文からバリアント条件語を検出する。(マッチ語, ラベル) か None。"""
    for pat, label in _VARIANT_COND_TO_LABEL:
        m = pat.search(query)
        if m:
            return m.group(0), label
    return None


def _variant_directive(query: str) -> str:
    """質問文からバリアント条件語を検出し、プロンプト冒頭に置く指示行を返す。

    ラベル対応はデータ側の _variant_label と揃えてあり、LLM は指定された
    「▼ ラベル」の行を引用するだけでよい。
    """
    cond = _detect_variant_cond(query)
    if cond:
        word, label = cond
        return (
            "## バリアント判定 (システムによる自動判定)\n"
            f"質問は「{word}」のバージョンについて聞いている。\n"
            f"参照データの【バリアントあり】セクションに"
            f"「▼ {label}」に該当する行があれば、**その行の数値**で答えること。\n"
            "該当する行が無い場合のみ通常版のデータで答え、その旨を明記すること。"
        )
    return ""


def _recap_lines(query: str, context: str) -> str:
    """回答に使うべき行を質問の直前に再掲する (Lost in the Middle 対策)。

    LLM は生成直前のコンテキストを最も強く参照するため、視点判定・
    バリアント判定に該当する重要行をコンテキスト末尾へ決定論で再配置する。
    """
    lines: list[str] = []
    # バリアント判定該当ブロックの再掲
    cond = _detect_variant_cond(query)
    if cond and '【バリアントあり】' in context:
        _, label = cond
        m = re.search(
            rf'^▼ {re.escape(label)}.*?:\n(.*?)(?=\n▼ |\n\n---|\Z)',
            context, re.DOTALL | re.MULTILINE)
        if m:
            lines.append(f"▼ {label} (質問に該当するバージョン):")
            lines.append(m.group(1).strip())
    # 視点付きフレーム行の再掲
    if _ATTACKER_VIEW_RE.search(query) or _DEFENDER_VIEW_RE.search(query):
        seen = 0
        for ln in context.split('\n'):
            if _CTX_ATTACKER_VAL_RE.search(ln) or _CTX_DEFENDER_VAL_RE.search(ln):
                lines.append(ln.strip())
                seen += 1
                if seen >= 4:
                    break
    # 質問フィールドの値の再掲 (候補が一意に取れた場合のみ — 複数技の混在を防ぐ)
    for label, vals in _field_expected_values(query, context).items():
        if len(vals) == 1:
            lines.append(f"{label}: {next(iter(vals))}")
    if not lines:
        return ''
    return "\n\n## 回答に必ず使う参照行 (再掲)\n" + '\n'.join(dict.fromkeys(lines))


def _perspective_directive(query: str) -> str:
    """質問文から視点 (攻撃側/防御側) を判定し、プロンプト冒頭に置く指示行を返す。

    Args:
        query: 元のユーザー質問。

    Returns:
        str: 視点指示セクション。判定できない場合は空文字列。
    """
    if _ATTACKER_VIEW_RE.search(query):
        return (
            "## 視点判定 (システムによる自動判定)\n"
            "この質問は**技を出した側 (攻撃側) の視点**である。\n"
            "参照データの括弧内「技を出した側が○F」「技を当てた側が○F」に書かれた\n"
            "数値を符号ごと一字一句そのまま転記して答えること。\n"
            "作業例: データが「ガード時: +2F (技を出した側が+2F / ガードした側は-2F)」\n"
            "        → 答えは「技を出した側が +2F 有利」(「+2F」をそのまま転記)\n"
            "自分で符号を付け直す・反転することは絶対に禁止。"
        )
    if _DEFENDER_VIEW_RE.search(query):
        return (
            "## 視点判定 (システムによる自動判定)\n"
            "この質問は**技を受けた側 (ガード/被弾側) の視点**である。\n"
            "参照データの括弧内「ガードした側は○F」「食らった側は○F」に書かれた\n"
            "数値を符号ごと一字一句そのまま転記して答えること。\n"
            "作業例: データが「ガード時: +2F (技を出した側が+2F / ガードした側は-2F)」\n"
            "        → 答えは「ガードした側は -2F (2フレーム不利)」(「-2F」をそのまま転記)\n"
            "自分で符号を付け直す・反転することは絶対に禁止。"
        )
    return ""

ANSWER_TEMPLATE = """\
## 参照データ
{context}

## 質問
{query}
"""

# 回答の構造化出力スキーマ。プロパティ名自体に指示を埋め込む (プロパティ名CoT):
# gemma4 は自由文だと転記指示を忘れるが、プロパティ名は生成の直前に必ず「見る」ため
# 追従性が維持される。生成順も CoT になるよう「転記 → 視点 → 回答文」の順に定義。
ANSWER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "参照した技または文書の名前": {"type": "string"},
        "参照データから符号ごと一字一句転記したフレーム数値のリスト": {
            "type": "array",
            "items": {"type": "string"},
            "description": "回答で使う数値。例: ['+4F', '26F']。参照データに無い数値を書いてはいけない",
        },
        "回答の視点_出した側か受けた側か視点なしか": {"type": "string"},
        "回答文_上で転記した数値だけを使い日本語で簡潔に": {"type": "string"},
    },
    "required": [
        "参照データから符号ごと一字一句転記したフレーム数値のリスト",
        "回答文_上で転記した数値だけを使い日本語で簡潔に",
    ],
}

_ANSWER_TEXT_KEY = "回答文_上で転記した数値だけを使い日本語で簡潔に"

# --- 決定論検証: 回答の数値がコンテキストに実在するか / 視点の結び付けが正しいか ---

_FRAME_TOKEN_RE = re.compile(r'[+-]?\d+(?:\.\d+)?F')
# コンテキスト内の両視点ラベル付き数値 (自前フォーマッタの出力形式に依存)
_CTX_ATTACKER_VAL_RE = re.compile(r'技を(?:出した|当てた)側が([+-]\d+)F')
_CTX_DEFENDER_VAL_RE = re.compile(r'(?:ガードした|食らった)側は([+-]\d+)F')
# 回答内の「視点語 と 数値」の結び付き
_ANS_SIDE_VAL_RE = re.compile(
    r'(出した側|攻撃側|当てた側|ガードした側|食らった側|受けた側|防御側)'
    r'[^\d+\-]{0,14}([+-]\d+)F'
)
_ATTACKER_WORDS = ('出した側', '攻撃側', '当てた側')


def _phantom_frame_tokens(answer: str, context: str) -> list[str]:
    """回答中のフレーム数値のうち、参照データに存在しないものを列挙する。"""
    bad = []
    for tok in set(_FRAME_TOKEN_RE.findall(answer)):
        if tok in context:
            continue
        num = tok[:-1]
        # 'F' なし表記 (DRキャンセル行の '+12' 等) も許容
        if num.startswith(('+', '-')):
            if re.search(rf'{re.escape(num)}(?![\d.])', context):
                continue
        else:
            # 符号なしは部分文字列誤判定 (800 の '80' 等) を境界チェックで防ぐ
            if re.search(rf'(?<![\d.+\-]){re.escape(num)}(?![\d.])', context):
                continue
        bad.append(tok)
    return bad


def _perspective_violations(answer: str, context: str) -> list[str]:
    """回答が視点語に逆側の数値を結び付けていないか検査する。

    コンテキストは両視点を「技を出した側が+2F / ガードした側は-2F」形式で
    併記しているので、そこから正解の集合を抽出して照合する。
    """
    atk = set(_CTX_ATTACKER_VAL_RE.findall(context))
    dfn = set(_CTX_DEFENDER_VAL_RE.findall(context))
    if not atk and not dfn:
        return []
    problems = []
    for m in _ANS_SIDE_VAL_RE.finditer(answer):
        side, val = m.group(1), m.group(2)
        want = atk if side in _ATTACKER_WORDS else dfn
        if want and val not in want:
            correct = ' / '.join(f'{v}F' for v in sorted(want))
            problems.append(
                f"「{side}」に {val}F を結び付けているが、参照データでは {correct}"
            )
    return problems


# 幻覚検出用の既知キャラ名 (英語表記 + 日本語表記)
_KNOWN_CHARA_NAMES = (
    'Ryu', 'Ken', 'Guile', 'Luke', 'Sagat', 'Cammy', 'Chun-Li', 'Zangief',
    'Blanka', 'Dhalsim', 'Akuma', 'Juri', 'Marisa', 'Jamie', 'Kimberly',
    'Lily', 'Manon', 'Rashid', 'Dee Jay', 'Dee_Jay', 'Ed', 'Terry', 'Mai',
    'Elena', 'Ingrid', 'Alex', 'M.Bison', 'C.Viper', 'E.Honda', 'JP',
    'リュウ', 'ケン', 'ガイル', 'ルーク', 'サガット', 'キャミィ', '春麗',
    'ザンギエフ', 'ブランカ', 'ダルシム', '豪鬼', 'ジュリ', 'マリーザ',
    'ジェイミー', 'キンバリー', 'リリー', 'マノン', 'ラシード', 'ディージェイ',
    'エド', 'テリー', 'エレナ', 'イングリッド', 'アレックス', 'ベガ',
    'バイパー',
    # 注: '舞' '本田' は一般語 (振る舞い等) に誤マッチするため対象外
)


def _foreign_chara_mentions(answer: str, context: str, query: str) -> list[str]:
    """回答に登場するのに参照データにも質問にも居ないキャラ名を列挙する。

    gemma4 が JSON モードで無関係なキャラの学習知識を幻覚する事故
    (Ed の質問に Manon の解説を返す等) を検出する。
    """
    source = context + query
    bad = []
    for name in _KNOWN_CHARA_NAMES:
        if name in answer and name not in source:
            # 'Ed' が 'Edmond' 等の一部でないか英字名のみ境界チェック
            if name.isascii() and not re.search(
                    rf'(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])', answer):
                continue
            bad.append(name)
    return bad


def _ensure_move_reference(answer: str, context: str) -> str:
    """回答がどの技のデータかを名乗っていない場合、先頭に技ヘッダを付ける。

    「硬直は31Fです。」のような主語のない回答を防ぐ。参照データの
    【chara / name (input)】ヘッダは決定論生成なのでそのまま転用できる。
    """
    headers = re.findall(r'【([^】]+)】', context)
    if not headers:
        return answer
    for h in headers:
        parts = [p.strip() for p in re.split(r'[/()]', h) if p.strip()]
        if any(p in answer for p in parts if len(p) >= 2):
            return answer
    return f"【{headers[0]}】\n{answer}"


def _variant_mention_note(answer: str, context: str) -> str:
    """バリアント存在への言及 (ANSWER_SYSTEM ルール16) を決定論で補完する。

    参照データに【バリアントあり】があるのに回答が触れていない場合、
    ▼ ラベルを列挙した補足行を返す。LLM の言及忘れに依存しない。
    """
    if '【バリアントあり】' not in context:
        return ''
    if re.search(r'ため|ホールド|バリアント|ウィンドクラッド|飲酒|エアカレント|Lv|版', answer):
        return ''
    labels = re.findall(r'^▼ (.+):$', context, re.MULTILINE)
    if not labels:
        return ''
    return f"\n\n※ この技には条件で性能が変わるバージョンがあります: {' / '.join(dict.fromkeys(labels))}"


def _answer_has_frame_value(answer: str, value: int) -> bool:
    """回答が指定フレーム値を表現しているかを緩めに判定する。"""
    signed = f"{value:+d}"
    abs_value = str(abs(value))
    if re.search(rf'(?<![\d.]){re.escape(signed)}\s*(?:F|フレーム)?(?![\d.])', answer):
        return True
    if value == 0 and re.search(r'(?<![\d.])0\s*(?:F|フレーム)?(?![\d.])|五分', answer):
        return True
    word = '有利' if value > 0 else '不利'
    return bool(
        re.search(rf'(?<![\d.]){abs_value}\s*(?:F|フレーム)?[^。\n]{{0,10}}{word}', answer)
        or re.search(rf'{word}[^。\n]{{0,10}}(?<![\d.]){abs_value}\s*(?:F|フレーム)?', answer)
    )


def _answer_has_signed_polarity_conflict(answer: str, value: int) -> bool:
    """符号付き数値の近くで有利/不利が逆に書かれていないか判定する。"""
    if value == 0:
        return False
    signed = f"{value:+d}"
    wrong_word = '有利' if value < 0 else '不利'
    return bool(
        re.search(rf'{re.escape(signed)}\s*(?:F|フレーム)?[^。\n]{{0,10}}{wrong_word}', answer)
        or re.search(rf'{wrong_word}[^。\n]{{0,10}}{re.escape(signed)}\s*(?:F|フレーム)?', answer)
    )


def _requested_perspective_value(query: str, context: str) -> tuple[str, int] | None:
    """質問が要求する視点のフレーム値をコンテキストから決定論で取る。"""
    if _has_material_scenario(context):
        if "ガード" in query:
            if _ATTACKER_VIEW_RE.search(query):
                label = "技を出した側"
                contextual = _contextual_profile_fact(context, "ガード時（攻撃側）")
            elif _DEFENDER_VIEW_RE.search(query):
                label = "ガードした側"
                contextual = _contextual_profile_fact(context, "ガード時（防御側）")
            else:
                contextual = None
                label = ""
        elif re.search(r"ヒット|当て|食ら|喰ら|受け", query):
            contextual = _contextual_profile_fact(context, "ヒット時（攻撃側）")
            if _ATTACKER_VIEW_RE.search(query):
                label = "技を当てた側"
            elif _DEFENDER_VIEW_RE.search(query):
                label = "食らった側"
                if contextual and re.fullmatch(r"[+-]?\d+F", contextual[0]):
                    value = -int(contextual[0][:-1])
                    return label, value
            else:
                contextual = None
                label = ""
        else:
            contextual = None
            label = ""
        if contextual:
            display, status = contextual
            if status in {"source_exact", "derived_exact", "condition_selected"}:
                match = re.fullmatch(r"([+-]?\d+)F", display)
                if match:
                    return label, int(match.group(1))
            return None

    if 'ガード' in query:
        if _ATTACKER_VIEW_RE.search(query):
            label = '技を出した側'
            pattern = r'ガード時[^\n]*技を出した側が([+-]\d+)F'
        elif _DEFENDER_VIEW_RE.search(query):
            label = 'ガードした側'
            pattern = r'ガード時[^\n]*ガードした側は([+-]\d+)F'
        else:
            return None
    elif re.search(r'ヒット|当て|食ら|喰ら|受け', query):
        if _ATTACKER_VIEW_RE.search(query):
            label = '技を当てた側'
            pattern = r'ヒット時[^\n]*技を当てた側が([+-]\d+)F'
        elif _DEFENDER_VIEW_RE.search(query):
            label = '食らった側'
            pattern = r'ヒット時[^\n]*食らった側は([+-]\d+)F'
        else:
            return None
    else:
        return None
    m = re.search(pattern, context)
    if not m and 'ガード' in query:
        if _ATTACKER_VIEW_RE.search(query):
            m = re.search(r'ガード時\s*([+-]\d+)F', context)
        elif _DEFENDER_VIEW_RE.search(query):
            m = re.search(r'ガードした側は\s*([+-]\d+)F', context)
    if not m:
        return None
    return label, int(m.group(1))


def _perspective_corrected_answer(answer: str, query: str, context: str) -> str:
    """要求視点の値が回答に無い場合は、決定論で短く正答へ置き換える。"""
    expected = _requested_perspective_value(query, context)
    if not expected:
        return answer
    label, value = expected
    if _answer_has_frame_value(answer, value) and not _answer_has_signed_polarity_conflict(answer, value):
        return answer
    if value > 0:
        desc = f"{abs(value)}フレーム有利"
    elif value < 0:
        desc = f"{abs(value)}フレーム不利"
    else:
        desc = "五分"
    return _ensure_move_reference(f"{label}は {value:+d}F ({desc})です。", context)


def _frame_desc(value: int) -> str:
    if value > 0:
        return f"{abs(value)}フレーム有利"
    if value < 0:
        return f"{abs(value)}フレーム不利"
    return "五分"


def _profile_fact(context: str, label: str) -> tuple[str, str] | None:
    """統合フレームプロファイルの採用値とソースを取得する。"""
    match = re.search(
        rf'^{re.escape(label)}:\s*(.+?)\s+'
        rf'\[(?:採用:\s*([^\]]+)|攻撃側の(.+?)値を符号反転)\]$',
        context,
        re.MULTILINE,
    )
    if not match:
        return None
    source = match.group(2) or match.group(3)
    return match.group(1).strip(), source.strip()


def _contextual_profile_fact(context: str, label: str) -> tuple[str, str] | None:
    """Read a condition-applied display and its calculation status."""
    match = re.search(
        rf'^条件適用後{re.escape(label)}:\s*(.+?)\s+\[([^\]]+)\]$',
        context,
        re.MULTILINE,
    )
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _has_material_scenario(context: str) -> bool:
    """Return whether the question adds a condition that can change frames."""
    if "条件の確認事項:" in context:
        return True
    match = re.search(r"^質問条件:\s*(.+)$", context, re.MULTILINE)
    if not match:
        return False
    fields = set(re.findall(r"([a-z_][a-z0-9_]*)=", match.group(1)))
    return bool(fields - {"interaction", "perspective"})


def _profile_difference(context: str, label: str) -> str:
    match = re.search(
        rf'^【ソース差異:{re.escape(label)}】(.+)$', context, re.MULTILINE
    )
    return match.group(1).strip() if match else ''


def _profile_capcom_note(context: str) -> str:
    match = re.search(r'^CAPCOM公式注記:\s*(.+)$', context, re.MULTILINE)
    return match.group(1).strip() if match else ''


def _advantage_display(display: str) -> str:
    """'+4F' を '+4F (4フレーム有利)' のように補足する。"""
    match = re.fullmatch(r'([+-]?\d+)F', display.strip())
    if not match:
        return display
    value = int(match.group(1))
    return f"{value:+d}F ({_frame_desc(value)})"


def _profile_field_answer(query: str, context: str) -> str | None:
    """主要フレーム項目を統合プロファイルから決定論的に回答する。"""
    requested: list[tuple[str, str]] = []
    for pattern, label in (
        (r'発生|何Fで出|何フレームで出', '発生'),
        (r'持続', '持続'),
        (r'硬直(?!差)', '硬直'),
    ):
        if re.search(pattern, query):
            if (
                label == "持続"
                and "質問条件:" in context
                and "ガード" in query
                and re.search(r"持続当て|最終持続|持続\s*\d+\s*F?目", query)
            ):
                continue
            requested.append((label, label))

    attacker = _profile_fact(context, 'ガード時（攻撃側・ガードさせた側）')
    defender = _profile_fact(context, 'ガード時（防御側・ガードした側）')
    asks_guard = bool(re.search(r'ガード|硬直差', query))
    lines: list[str] = []
    contextual_attacker = _contextual_profile_fact(context, "ガード時（攻撃側）")
    contextual_defender = _contextual_profile_fact(context, "ガード時（防御側）")
    if asks_guard and _has_material_scenario(context):
        selected_contextual = (
            contextual_defender if _DEFENDER_VIEW_RE.search(query)
            else contextual_attacker
        )
        if selected_contextual and selected_contextual[1] not in {
            "source_exact", "derived_exact", "condition_selected",
        }:
            lines.append(
                f"条件適用後のガード時硬直差は{selected_contextual[0]}"
                f"（{selected_contextual[1]}）のため、単一値を確定できません。"
            )
            attacker = None
            defender = None
        elif contextual_attacker:
            attacker = (contextual_attacker[0], f"条件評価:{contextual_attacker[1]}")
            if contextual_defender:
                defender = (
                    contextual_defender[0],
                    f"条件評価:{contextual_defender[1]}",
                )

    for _, label in requested:
        fact = _profile_fact(context, label)
        if not fact:
            continue
        display, source = fact
        if source == 'なし' or display == 'データなし':
            lines.append(
                f"{label}はCAPCOM公式・UFD・SuperComboのいずれにも"
                "データがありません。"
            )
        else:
            lines.append(f"{label}は{display}です（{source}）。")
        difference = _profile_difference(context, label)
        if difference:
            lines.append(f"ソース別の記録: {difference}。")
        note = _profile_capcom_note(context)
        if note and any(token in display for token in (
            '条件', '※', '複数持続', '着地', '硬直単独値なし'
        )):
            lines.append(f"CAPCOM公式注記: {note}")

    if asks_guard and attacker:
        attacker_display, source = attacker
        defender_display = defender[0] if defender else '算出不可'
        if source == 'なし' or attacker_display == 'データなし':
            lines.append(
                "ガード時硬直差はCAPCOM公式・UFD・SuperComboのいずれにも"
                "データがないため、攻撃側・防御側とも算出できません。"
            )
        elif _ATTACKER_VIEW_RE.search(query):
            lines.append(
                "ガードさせた側（攻撃側）は"
                f"{_advantage_display(attacker_display)}です（{source}）。"
            )
        elif _DEFENDER_VIEW_RE.search(query):
            lines.append(
                "ガードした側（防御側）は"
                f"{_advantage_display(defender_display)}です"
                f"（{source}の攻撃側硬直差を符号反転）。"
            )
        else:
            lines.append(
                "ガード時は、"
                f"攻撃側が{_advantage_display(attacker_display)}、"
                f"防御側が{_advantage_display(defender_display)}です（{source}）。"
            )
        difference = _profile_difference(context, 'ガード時')
        if difference:
            lines.append(f"ソース別の記録: {difference}。")

    generic_profile_query = bool(
        re.search(r'性能|フレームデータ|フレーム情報|詳しく|教えて|データ', query)
    )
    if not lines and generic_profile_query and '統合フレームプロファイル' in context:
        for label in ('発生', '持続', '硬直'):
            fact = _profile_fact(context, label)
            if fact:
                lines.append(f"{label}: {fact[0]}（{fact[1]}）")
        if attacker:
            defender_display = defender[0] if defender else '算出不可'
            lines.append(
                "ガード時: "
                f"攻撃側{_advantage_display(attacker[0])} / "
                f"防御側{_advantage_display(defender_display)}（{attacker[1]}）"
            )

    if not lines:
        return None
    body = lines[0] if len(lines) == 1 else '\n'.join(f"- {line}" for line in lines)
    return _ensure_move_reference(body, context)


def _deterministic_frame_answer(query: str, context: str) -> str | None:
    """単純なフレーム質問は参照データから決定論で即答する。

    発生・ガード視点・確定反撃候補は MCP/DB コンテキストに正規化済みの
    数値が含まれるため、LLM 生成を介さない方が速く、視点取り違えも起きない。
    """
    profile_answer = _profile_field_answer(query, context)
    if profile_answer:
        return profile_answer

    if re.search(r'発生|何Fで出|何フレームで出', query):
        m = re.search(r'発生:\s*([+-]?\d+)\s*F?', context)
        if m:
            return _ensure_move_reference(f"発生は{int(m.group(1))}Fです。", context)

    if (
        re.search(r'確定反撃|確反|反撃|提案|使える技|何で返', query)
        and re.search(r'単一値を確定できないため反撃判定を保留', context)
    ):
        display_match = re.search(
            r'条件適用後ガード時硬直差は\s*(.+?)。', context
        )
        display = display_match.group(1) if display_match else "条件別データ"
        return (
            f"攻撃側のガード時参照値は{display}ですが、適用条件が未指定です。"
            "今回の条件では硬直差を単一値に確定できないため、"
            "確定反撃候補の提示を保留します。"
        )

    if (
        'フレーム上の反撃候補（到達未検証）' in context
        and re.search(r'確定反撃|確反|反撃|提案|使える技|何で返', query)
    ):
        options: list[tuple[str, str, str]] = []
        for m in re.finditer(r'^- ([^/\n]+) / ([^:\n]+): 発生(\d+)F', context, re.MULTILINE):
            options.append((m.group(1).strip(), m.group(2).strip(), m.group(3)))
        if not options:
            return None
        window_match = re.search(r'発生\s*(\d+)F\s*以内', context)
        window = window_match.group(1) if window_match else options[-1][2]
        parts = [
            f"{inp}（{name}、発生{startup}F）" if inp != '-' else f"{name}（発生{startup}F）"
            for inp, name, startup in options[:5]
        ]
        return (
            f"ガードした側は +{window}F で、発生{window}F以内がフレーム上の候補です。"
            f"候補: {'、'.join(parts)}。"
            "ただしリーチとガード後距離が未検証なので、確定反撃としては未確定です。"
        )

    if '確定反撃候補' in context and re.search(r'確定反撃|確反|反撃|提案|使える技|何で返', query):
        options: list[tuple[str, str, str]] = []
        for m in re.finditer(r'^- ([^/\n]+) / ([^:\n]+): 発生(\d+)F', context, re.MULTILINE):
            options.append((m.group(1).strip(), m.group(2).strip(), m.group(3)))
        if not options:
            return None
        window_match = re.search(r'発生\s*(\d+)F\s*以内', context)
        window = window_match.group(1) if window_match else options[-1][2]
        parts = [
            f"{inp}（{name}、発生{startup}F）" if inp != '-' else f"{name}（発生{startup}F）"
            for inp, name, startup in options[:5]
        ]
        return (
            f"ガードした側は +{window}F 有利なので、発生{window}F以内の技が確定反撃です。"
            f"候補: {'、'.join(parts)}。"
        )

    expected = _requested_perspective_value(query, context)
    if expected:
        label, value = expected
        return _ensure_move_reference(f"{label}は {value:+d}F ({_frame_desc(value)})です。", context)

    return None


def _punish_option_note(answer: str, query: str, context: str) -> str:
    """確定反撃候補がコンテキストにあるのに回答が落とした場合に補う。"""
    timing_only = 'フレーム上の反撃候補（到達未検証）' in context
    if '確定反撃候補' not in context and not timing_only:
        return ''
    if not re.search(r'提案|候補|使える技|どの技|何で|確反|反撃', query):
        return ''
    opts = []
    for m in re.finditer(r'^- ([^/\n]+) / ([^:\n]+): 発生(\d+)F', context, re.MULTILINE):
        inp, name, startup = (m.group(1).strip(), m.group(2).strip(), m.group(3))
        if (inp != '-' and inp in answer) or name in answer:
            return ''
        opts.append((inp, name, startup))
    if not opts:
        return ''
    parts = [
        f"{inp}（{name}、発生{startup}F）" if inp != '-' else f"{name}（発生{startup}F）"
        for inp, name, startup in opts[:3]
    ]
    label = "フレーム上の反撃候補（到達未検証）" if timing_only else "確定反撃候補"
    return f"\n\n{label}: " + "、".join(parts)


def _postprocess_answer(answer: str, query: str, context: str) -> str:
    """LLM後の決定論的な補足をまとめて適用する。"""
    answer = _perspective_corrected_answer(answer, query, context)
    return answer + _variant_mention_note(answer, context) + _punish_option_note(answer, query, context)


def _context_frame_excerpt(context: str, limit: int = 6) -> str:
    """検証失敗時にユーザーへ添える、コンテキストのフレーム数値行の抜粋。"""
    lines = [ln.strip() for ln in context.split('\n')
             if _FRAME_TOKEN_RE.search(ln) and not ln.startswith('⚠')]
    return '\n'.join(lines[:limit])


def _answer_mentions_transcribed_values(answer: str, transcribed: object) -> bool:
    """構造化出力が転記した値を、回答本文が使っているか緩めに判定する。

    発生などの単純なフレーム値は「12F」ではなく「12です」と返っても
    ユーザー向けには自然なので、符号なし値は単位なし表記も許容する。
    """
    if not transcribed:
        return False
    values = transcribed if isinstance(transcribed, list) else [transcribed]
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        numeric_like = re.fullmatch(r'[+-]?\d+(?:\.\d+)?\s*(?:F|フレーム)?', text)
        if not numeric_like and text in answer:
            return True
        for m in re.finditer(r'([+-]?)(\d+(?:\.\d+)?)\s*(?:F|フレーム)?', text):
            sign, num = m.group(1), m.group(2)
            prefix = re.escape(sign + num) if sign else rf'[+-]?{re.escape(num)}'
            if re.search(rf'(?<![\d.]){prefix}\s*(?:F|フレーム)?(?![\d.])', answer):
                return True
    return False


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
    # build_context() が生成する連携解析 summary は、共通タイムラインと
    # レビュー済み観測から既に完成文として決定論生成されている。
    # LLM に再要約させず、値・視点・確度ラベルを保存する。
    stripped_context = context.strip()
    if (
        re.match(r"^【[^\n]+連携[^\n]*解析】(?:\n|$)", stripped_context)
        or stripped_context.startswith("【技条件検索】")
    ):
        return stripped_context

    deterministic = _deterministic_frame_answer(query, context)
    if deterministic:
        return _postprocess_answer(deterministic, query, context)

    # 重要行の再掲を参照データ末尾 (=質問の直前) に配置する (Lost in the Middle 対策)
    prompt = ANSWER_TEMPLATE.format(
        context=context + _recap_lines(query, context), query=query)
    directives = [d for d in (_field_directive(query), _variant_directive(query),
                              _perspective_directive(query)) if d]
    if directives:
        prompt = '\n\n'.join(directives) + f"\n\n{prompt}"

    # 構造化出力 (プロパティ名CoT) + 決定論検証。失敗時は検証エラーを
    # フィードバックして1回だけ再生成する (再帰修正)。
    answer = ""
    if hasattr(provider, 'generate_structured'):
        attempt_prompt = prompt
        for attempt in (1, 2):
            try:
                with usage_label("answer" if attempt == 1 else "answer_retry"):
                    data = await provider.generate_structured(
                        prompt=attempt_prompt, schema=ANSWER_JSON_SCHEMA,
                        system=ANSWER_SYSTEM)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"構造化回答生成失敗 (attempt {attempt}): {e}")
                break
            answer = str(data.get(_ANSWER_TEXT_KEY) or '').strip()
            if not answer:
                logger.warning(f"構造化回答が空 (attempt {attempt}): keys={list(data)}")
                continue
            problems = _phantom_frame_tokens(answer, context)
            problems = [f"数値 {t} は参照データに存在しない" for t in problems]
            problems += _perspective_violations(answer, context)
            problems += [
                f"参照データに無いキャラ「{n}」に言及している (質問と無関係な内容)"
                for n in _foreign_chara_mentions(answer, context, query)
            ]
            # フレームデータがあるのに回答が数値を1つも使っていない場合も疑う
            # ('4フレーム' 表記も数値使用とみなす)
            transcribed = data.get(
                "参照データから符号ごと一字一句転記したフレーム数値のリスト") or []
            if (transcribed and _FRAME_TOKEN_RE.search(context)
                    and not _answer_mentions_transcribed_values(answer, transcribed)):
                problems.append("回答文が転記した数値を1つも使っていない")
            # 質問されたフィールドの値が回答に含まれているか
            for label, vals in _field_expected_values(query, context).items():
                if not any(v in answer for v in vals):
                    cand = ' / '.join(sorted(vals))
                    problems.append(
                        f"質問された「{label}」の値 ({cand}) が回答に含まれていない"
                    )
            if not problems:
                answer = _ensure_move_reference(answer, context)
                return _postprocess_answer(answer, query, context)
            logger.warning(f"回答検証NG (attempt {attempt}): {problems}")
            attempt_prompt = (
                f"{prompt}\n\n## 前回回答の検証エラー (参照データの数値をそのまま転記して修正すること)\n- "
                + "\n- ".join(problems)
            )
        if answer:
            # 2回とも検証NG → 誤った回答は返さず、安全な決定論フォールバックへ。
            excerpt = _context_frame_excerpt(context)
            if excerpt:
                logger.warning("回答検証NG。参照データ抜粋:\n%s", excerpt)
            deterministic_retry = _deterministic_frame_answer(query, context)
            if deterministic_retry:
                return _postprocess_answer(deterministic_retry, query, context)
            return (
                "参照データは取得できましたが、矛盾のない回答文を生成できませんでした。"
                "技名と確認したい項目（発生・持続・硬直・ガード時など）を指定してください。"
            )

    # フォールバック: 従来の自由文生成 (構造化非対応プロバイダ / JSON失敗時)
    with usage_label("answer_fallback"):
        resp = await provider.generate(prompt=prompt, system=ANSWER_SYSTEM)
    answer = resp.text.strip()
    return _postprocess_answer(answer, query, context)
