"""セットプレイ推論エンジン: KD後・有利フレーム後の起き攻め択を計算する。

セットプレイの核心計算:
  ① 技の hit_adv 生文字列 ('KD +27', '+4' 等) から有利Fを抽出
  ② プレイヤーがとる行動 (前ダッシュ等) のフレームコストを差し引く
  ③ 残り有利F 以内の発生を持つ技を sc_move_normalized から検索
  ④ 投げ (発生5F) が間合い内で出せるか判定

前ステップフレームは doc_chunks (SF6/Movement / Forward/Back Dashing) から
キャラ別に動的取得する。データがない場合は DEFAULT_DASH_F (20F) で補完。
※ 「前ステップ」= 66入力の通常前進。「ドライブラッシュ」とは別物。
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

# ============================================================
# SF6 共通定数
# ============================================================

DEFAULT_DASH_F  = 20   # キャラ別データが取得できない場合のフォールバック (前ステップ)
THROW_STARTUP_F = 5    # 投げ発生F (SF6全キャラ共通)

_HIT_PRESETS: list[tuple[str, int]] = [
    ('ヒット後そのまま', 0),
]

# ============================================================
# キャラ別前ダッシュフレーム取得
# ============================================================

def _normalize_char(name: str) -> str:
    """比較用にキャラ名を正規化 (空白・ピリオド・アンダースコア・括弧を除去して小文字化)。"""
    return re.sub(r'[\s._\-()\']', '', name.lower())


@lru_cache(maxsize=1)
def _load_dash_table() -> dict[str, int]:
    """doc_chunks の Forward/Back Dashing テーブルをパースして {正規化キャラ名: 前ダッシュF} を返す。

    lru_cache で初回取得後はメモリにキャッシュする。
    """
    from sf6_engine.db import get_client
    sb = get_client()
    res = sb.table('doc_chunks').select('content').eq(
        'page', 'SF6/Movement'
    ).eq('heading_h2', 'Forward/Back Dashing').execute()

    if not res.data:
        logger.warning("doc_chunks: Forward/Back Dashing チャンクが見つかりません")
        return {}

    content = res.data[0]['content']
    _HEADER_SKIP = {'Character', 'Forward Dash', 'Back Dash', 'Speed (f)', 'Distance'}
    lines = [l.strip() for l in content.split('\n') if l.strip()]

    result: dict[str, int] = {}
    i = 0
    while i < len(lines):
        if lines[i] in _HEADER_SKIP:
            i += 1
            continue
        char_name = lines[i]
        if i + 4 < len(lines):
            try:
                fwd_f    = int(lines[i + 1])
                float(lines[i + 2])   # 前ダッシュ距離 (検証のみ)
                int(lines[i + 3])     # 後ダッシュF    (検証のみ)
                float(lines[i + 4])   # 後ダッシュ距離 (検証のみ)
                # "(3K Hop)" 等のバリアント行は除外
                if '(' not in char_name:
                    result[_normalize_char(char_name)] = fwd_f
                i += 5
                continue
            except ValueError:
                pass
        i += 1

    logger.debug(f"Dash table loaded: {len(result)} chars")
    return result


def get_forward_dash_cost(chara: str) -> int:
    """capcom_slug または SC chara 名からキャラ固有の前ステップ (66入力) F を返す。

    doc_chunks のテーブルを参照し、見つからない場合は DEFAULT_DASH_F を返す。
    """
    sc_chara = _resolve_sc_chara(chara) or chara
    key = _normalize_char(sc_chara)
    table = _load_dash_table()
    cost = table.get(key, DEFAULT_DASH_F)
    if cost == DEFAULT_DASH_F and key not in table:
        logger.info(f"Dash cost not found for '{sc_chara}' (key='{key}'), using default {DEFAULT_DASH_F}F")
    return cost

# ============================================================
# パーサーユーティリティ
# ============================================================

_RE_NUM = re.compile(r'([+-]?\d+)')


def parse_kd_adv(raw: str | None) -> int | None:
    """'KD +27', 'KD +40~41 (KD +37~47)', 'HKD +23 Splat' → KD起き攻め有利F。

    括弧内の補足値は除去して最初の正数を返す。
    """
    if not raw:
        return None
    if 'KD' not in raw and 'HKD' not in raw:
        return None
    # 括弧内を除去してから最初の数値を取得
    stripped = re.sub(r'\(.*?\)', '', raw)
    nums = _RE_NUM.findall(stripped)
    # KD/HKD の後の数値 (最初の正の整数) を返す
    for n in nums:
        v = int(n)
        if v > 0:
            return v
    return None


def parse_hit_adv(raw: str | None) -> int | None:
    """'+3', '-2' → 通常ヒット有利F。KD/HKD を含む場合は None。"""
    if not raw:
        return None
    if 'KD' in raw or 'HKD' in raw:
        return None
    m = _RE_NUM.search(raw)
    return int(m.group(1)) if m else None


def is_kd(raw: str | None) -> bool:
    return bool(raw and ('KD' in raw or 'HKD' in raw))


# ============================================================
# データ構造
# ============================================================

@dataclass
class SetplayOption:
    """セットプレイで選択できる技1つ。"""
    input:       str
    name:        str
    startup_f:   int
    is_kd:       bool
    hit_adv_f:   int | None
    block_adv_f: int | None
    move_type:   str
    invuln:      str | None = None


@dataclass
class SetplayScenario:
    """1アクション後のセットプレイ状況。"""
    action_label:   str
    action_cost:    int
    base_adv:       int        # 技のKD/ヒット後有利F
    remaining:      int        # base_adv - action_cost
    options:        list[SetplayOption] = field(default_factory=list)
    throw_possible: bool = False


# ============================================================
# DB 検索
# ============================================================

_COMBO_TYPES = frozenset({'ground_normal', 'Special', 'special', 'Super', 'super'})


def _resolve_sc_chara(chara: str) -> str | None:
    from sf6_engine.db import get_client
    sb = get_client()
    res = sb.table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
    if res.data:
        return res.data[0]['sc_chara']
    res2 = sb.table('char_slug_map').select('sc_chara').ilike('sc_chara', f'%{chara}%').execute()
    return res2.data[0]['sc_chara'] if res2.data else None


def fetch_setplay_options(
    chara: str,
    max_startup: int,
    exclude_input: str | None = None,
) -> list[SetplayOption]:
    """max_startup F 以内の技を sc_move_normalized から取得して SetplayOption リストを返す。"""
    if max_startup <= 0:
        return []
    from sf6_engine.db import get_client
    sb = get_client()

    sc_chara = _resolve_sc_chara(chara)
    if not sc_chara:
        return []

    res = sb.table('sc_move_normalized').select(
        'input,name,move_type,startup_f,'
        'hit_adv_f,hit_is_knockdown,block_adv_f,invuln'
    ).eq('chara', sc_chara).lte('startup_f', max_startup).not_.is_('startup_f', 'null').execute()

    opts: list[SetplayOption] = []
    for r in res.data:
        if r.get('move_type') not in _COMBO_TYPES:
            continue
        if exclude_input and r.get('input') == exclude_input:
            continue
        opts.append(SetplayOption(
            input=r.get('input', '?'),
            name=r.get('name') or r.get('input', '?'),
            startup_f=int(r['startup_f']),
            is_kd=bool(r.get('hit_is_knockdown')),
            hit_adv_f=r.get('hit_adv_f'),
            block_adv_f=r.get('block_adv_f'),
            move_type=r.get('move_type', ''),
            invuln=r.get('invuln'),
        ))

    # KD技 → startup大きい順 (重い技優先)
    opts.sort(key=lambda o: (-(1 if o.is_kd else 0), -o.startup_f))
    return opts[:10]


# ============================================================
# セットプレイ計算
# ============================================================

def compute_setplay(
    chara: str,
    move_input: str,
    move_name: str,
    hit_adv_raw: str | None,
    action_presets: list[tuple[str, int]] | None = None,
) -> list[SetplayScenario]:
    """技ヒット/KD後に各アクションをとった場合のシナリオリストを返す。

    Args:
        chara:          capcom_slug または SC chara 名。
        move_input:     技の input ('623HP', '4HP' 等)。
        move_name:      技名 (表示用)。
        hit_adv_raw:    sc_moves.hit_adv の生文字列 ('KD +27', '+3' 等)。
        action_presets: [(ラベル, コストF), ...] None なら KD/ヒット種別に応じたプリセット。
                        KD時はキャラ固有前ステップF を自動取得して設定する。

    Returns:
        list[SetplayScenario]: 各アクション後のシナリオ。データ不足時は空リスト。
    """
    kd = is_kd(hit_adv_raw)
    base_adv = parse_kd_adv(hit_adv_raw) if kd else parse_hit_adv(hit_adv_raw)
    if base_adv is None:
        logger.info(f"setplay: {move_input} の有利F取得不可 (raw={hit_adv_raw!r})")
        return []

    if action_presets is None:
        if kd:
            dash_f = get_forward_dash_cost(chara)
            action_presets = [
                ('即攻め (距離調整なし)', 0),
                ('前ステップ',    dash_f),
                ('前ステップ×2',  dash_f * 2),
            ]
        else:
            action_presets = _HIT_PRESETS
    presets = action_presets

    scenarios: list[SetplayScenario] = []
    for action_label, action_cost in presets:
        remaining = base_adv - action_cost
        opts = fetch_setplay_options(chara, remaining, exclude_input=move_input)
        throw_ok = (remaining >= THROW_STARTUP_F)
        scenarios.append(SetplayScenario(
            action_label=action_label,
            action_cost=action_cost,
            base_adv=base_adv,
            remaining=remaining,
            options=opts,
            throw_possible=throw_ok,
        ))

    return scenarios


# ============================================================
# フォーマッター (LLM コンテキスト用)
# ============================================================

def _sign(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def format_setplay_context(
    move_input: str,
    move_name: str,
    hit_adv_raw: str | None,
    scenarios: list[SetplayScenario],
) -> str:
    """SetplayScenario リストを LLM コンテキスト用テキストに変換する。"""
    kd = is_kd(hit_adv_raw)
    adv_type = 'KD後有利' if kd else 'ヒット後有利'

    lines = [f"【セットプレイ分析: {move_input} ({move_name})】"]
    lines.append(f"  {adv_type}: {hit_adv_raw or '不明'}")
    lines.append("")

    for sc in scenarios:
        rem_str = _sign(sc.remaining)
        lines.append(f"▶ {sc.action_label}" +
                      (f" (コスト {sc.action_cost}F)" if sc.action_cost > 0 else "") + ":")
        if sc.action_cost > 0:
            lines.append(
                f"  {adv_type}{_sign(sc.base_adv)}F − {sc.action_label}{sc.action_cost}F = 残り有利 {rem_str}F"
            )
        else:
            lines.append(f"  残り有利: {rem_str}F ({adv_type}{_sign(sc.base_adv)}F そのまま)")

        if sc.remaining <= 0:
            lines.append("  ⚠ 残り有利なし → 確定択なし (択ゲー)")
            lines.append("")
            continue

        # 攻撃択
        ground   = [o for o in sc.options if o.move_type == 'ground_normal']
        specials = [o for o in sc.options if o.move_type in ('Special', 'special', 'Super', 'super')]

        def fmt_opt(o: SetplayOption) -> str:
            kd_tag  = ' (KD)' if o.is_kd else ''
            inv_tag = f' [無敵:{o.invuln}]' if o.invuln else ''
            blk_str = (f' ガード時{_sign(o.block_adv_f)}F' if o.block_adv_f is not None else '')
            return f"    ✅ {o.input:14s} 発生{o.startup_f}F  {o.name}{kd_tag}{inv_tag}{blk_str}"

        if ground:
            lines.append("  [通常技択]")
            for o in ground:
                lines.append(fmt_opt(o))
        if specials:
            lines.append("  [必殺技・SA択]")
            for o in specials:
                lines.append(fmt_opt(o))

        # 投げ択
        if sc.throw_possible:
            lines.append(
                f"  [投げ択] ✅ 発生5F ≤ {rem_str}F → 投げ間合いなら投げが確定"
            )
        else:
            lines.append(
                f"  [投げ択] ⚠ 発生5F > {rem_str}F → 投げ打ちになる (相手が暴れれば負け)"
            )

        lines.append(
            f"  ※ 推論根拠: {adv_type}{_sign(sc.base_adv)}F"
            + (f" − {sc.action_label}{sc.action_cost}F" if sc.action_cost else "")
            + f" = {rem_str}F → 発生{rem_str.lstrip('+')}F以内の技が確定"
        )
        lines.append("")

    return '\n'.join(lines)
