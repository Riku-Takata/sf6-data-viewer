"""コンボエンジン: フレームデータからの最大コンボ計算。

アルゴリズム: ビームサーチ (beam_width=8, max_depth=7)
  - 各ステップで「技を使う」または「技→DRキャンセル」を選択
  - ドライブゲージ (最大6本) を追跡: DRキャンセル = 3本消費
  - 目的: 全コンボルート中でダメージが最大になるものを探索

  コンボが繋がる条件:
    startup_f(次の技) <= current_adv (現在の有利フレーム)

  DRキャンセルの有利:
    技ヒット → DRキャンセル (3本) → after_dr_hit_f の有利で次の技が繋がる

  コンボ終了条件:
    - KD (ノックダウン) → 通常終了
    - 有利フレームが0以下 → 次の技が繋がらない
    - ドライブゲージ不足でDR不可 & ヒット有利が小さい
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ドライブゲージコスト
DRIVE_COST_DR = 3


# ============================================================
# データ構造
# ============================================================

@dataclass(frozen=True)
class MoveNode:
    """コンボ計算に使用する技データ (イミュータブル)。"""
    input: str
    name: str
    move_type: str
    startup_f: int
    hit_adv_f: Optional[int]   # None = ノックダウン
    hit_is_knockdown: bool
    damage: int                 # 総ダメージ (多段は合算)
    dr_cancelable: bool         # DRキャンセル可能か
    after_dr_hit_f: Optional[int]  # DRキャンセル後の有利F (None = KD or 不明)
    chain_cancelable: bool = False  # チェーンキャンセル可能か ('Chn' in cancel)

    # チェーンキャンセルは発生フレームに関係なく繋がる仮想有利F
    CHAIN_ADV: int = 99  # チェーン時の仮想有利フレーム (常に繋がる)


@dataclass
class ComboStep:
    """コンボの1ステップ。"""
    move: MoveNode
    adv_before: int            # この技を使う時点での有利F
    used_dr: bool = False      # この技の後にDRキャンセルしたか
    adv_after: Optional[int] = None  # この技後の有利F (DR込み)
    drive_spent: int = 0       # このステップで消費したドライブゲージ


@dataclass
class ComboResult:
    """最大コンボの結果。"""
    steps: list[ComboStep]
    total_damage: int
    total_drive_spent: int
    ends_in_kd: bool
    initial_adv: int

    def route_str(self) -> str:
        """コンボルートを文字列で表現。例: '2MP[DR]→4HP[KD]'"""
        parts = []
        for s in self.steps:
            label = s.move.input
            if s.move.hit_is_knockdown and not s.used_dr:
                label += '[KD]'
            elif s.used_dr:
                label += '[DR]'
            parts.append(label)
        return ' → '.join(parts)

    def format_context(self) -> str:
        """LLM へのコンテキスト用テキストを生成する。"""
        lines = [
            "【最大コンボ計算結果】",
            f"  コンボルート: {self.route_str()}",
            f"  総ダメージ:   {self.total_damage}",
            f"  ドライブ消費: {self.total_drive_spent}/6本",
            f"  {'ノックダウン終わり' if self.ends_in_kd else '継続可能'}",
            "",
            "  ステップ詳細:",
        ]
        for i, s in enumerate(self.steps, 1):
            mv   = s.move
            name = mv.name or mv.input
            hit  = ('KD' if mv.hit_is_knockdown else
                    (f'+{s.adv_after}F' if s.adv_after is not None else '?'))
            dr_note = f' → DRキャンセル (3本消費、以降+{s.adv_after}F)' if s.used_dr else ''
            lines.append(
                f"  {i}. {mv.input} ({name})"
                f" 発生{mv.startup_f}F / ダメージ{mv.damage}"
                f" / ヒット後: {hit}{dr_note}"
            )
            if s.adv_before == -1:
                lines.append(f"     └ 始動技 (コンボの起点)")
            elif s.adv_before == MoveNode.CHAIN_ADV:
                lines.append(f"     └ チェーンキャンセル (発生フレーム無関係で繋がる)")
            else:
                lines.append(
                    f"     └ 繋がる根拠: 直前の有利{s.adv_before}F ≥ 発生{mv.startup_f}F"
                )

        lines.append("")
        lines.append(f"  ※ 探索条件: 初期有利{self.initial_adv}F / ドライブ上限6本")
        return '\n'.join(lines)


# ============================================================
# ユーティリティ
# ============================================================

_RE_NUM = re.compile(r'([+-]?\d+)')


def _parse_adv(val) -> Optional[int]:
    """'+8', 'KD +29', 8 などを int に変換。KD/HKD を含む場合は None。"""
    if val is None:
        return None
    s = str(val)
    if 'KD' in s or 'HKD' in s:
        return None
    m = _RE_NUM.search(s)
    return int(m.group(1)) if m else None


def _parse_damage(damage_str: Optional[str]) -> int:
    """'800' → 800 / '200,600' → 800 (合算) / '1700~3000' → 1700 (最小)"""
    if not damage_str:
        return 0
    # 括弧内の合計値を優先: '300,200x6,1000 (2500)' → 2500
    paren = re.search(r'\((\d+)\)', damage_str)
    if paren:
        return int(paren.group(1))
    # カンマ区切りの合算
    base = damage_str.split('(')[0]
    nums = re.findall(r'\d+', base)
    if not nums:
        return 0
    # 'x N' 掛け算を考慮: '200x6' → 1200
    total = 0
    for part in base.split(','):
        part = part.strip()
        m_mult = re.match(r'(\d+)x(\d+)', part)
        if m_mult:
            total += int(m_mult.group(1)) * int(m_mult.group(2))
        else:
            nums_in_part = re.findall(r'\d+', part)
            if nums_in_part:
                total += int(nums_in_part[0])
    return total


def _parse_multi_adv(val) -> Optional[int]:
    """'+20/+16' などの複数値から最大値を返す。KD含む場合は None。"""
    if val is None:
        return None
    s = str(val)
    if 'KD' in s or 'HKD' in s:
        return None
    nums = [int(m) for m in _RE_NUM.findall(s)]
    return max(nums) if nums else None


# ============================================================
# キャラクター技データ取得
# ============================================================

# コンボに使用できる move_type
_COMBO_TYPES = frozenset({
    'ground_normal', 'Special', 'special', 'Super', 'super',
})


def fetch_combo_moves(chara: str) -> list[MoveNode]:
    """コンボ計算用のキャラクター技データをまとめて取得する。

    sc_moves と sc_move_normalized を結合し、MoveNode のリストを返す。

    Args:
        chara: capcom_slug ('sagat') または SuperCombo chara 名 ('Sagat')。

    Returns:
        list[MoveNode]: コンボ計算に使用できる技の一覧。
    """
    from sf6_engine.db import get_client
    sb = get_client()

    # capcom_slug → sc_chara
    map_res = sb.table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
    if not map_res.data:
        sc_chara = chara  # SC chara 名をそのまま試す
    else:
        sc_chara = map_res.data[0]['sc_chara']

    # sc_moves (生データ: damage, dr_cancel_hit, after_dr_hit)
    raw_res = sb.table('sc_moves').select(
        'input,name,move_type,damage,dr_cancel_hit,after_dr_hit'
    ).eq('chara', sc_chara).execute()
    raw_by_input = {r['input']: r for r in raw_res.data}

    # sc_move_normalized (整数化済み: startup_f, hit_adv_f, hit_is_knockdown)
    norm_res = sb.table('sc_move_normalized').select(
        'input,startup_f,hit_adv_f,hit_is_knockdown,move_type'
    ).eq('chara', sc_chara).not_.is_('startup_f', 'null').execute()

    nodes: list[MoveNode] = []
    for norm in norm_res.data:
        inp = norm['input']
        mt  = norm.get('move_type', '')
        if mt not in _COMBO_TYPES:
            continue

        raw = raw_by_input.get(inp, {})
        startup_f = norm.get('startup_f')
        if startup_f is None or startup_f <= 0:
            continue

        dr_cancel_raw = raw.get('dr_cancel_hit')
        after_dr_raw  = raw.get('after_dr_hit')

        # after_dr_hit が None や KD の場合も考慮
        after_dr_f = _parse_multi_adv(after_dr_raw)
        dr_cancelable = (dr_cancel_raw is not None and
                         dr_cancel_raw not in ('', '-') and
                         after_dr_f is not None and
                         after_dr_f > 0)

        cancel_raw = raw.get('cancel', '')
        chain_cancelable = 'Chn' in str(cancel_raw)

        nodes.append(MoveNode(
            input=inp,
            name=raw.get('name') or inp,
            move_type=mt,
            startup_f=int(startup_f),
            hit_adv_f=norm.get('hit_adv_f'),
            hit_is_knockdown=bool(norm.get('hit_is_knockdown', False)),
            damage=_parse_damage(raw.get('damage')),
            dr_cancelable=dr_cancelable,
            after_dr_hit_f=after_dr_f,
            chain_cancelable=chain_cancelable,
        ))

    return nodes


# ============================================================
# ビームサーチ コンボ計算
# ============================================================

def find_max_combo(
    moves: list[MoveNode],
    initial_adv: int,
    starter_input: Optional[str] = None,
    initial_drive: int = 6,
    max_depth: int = 7,
    beam_width: int = 8,
    first_step_chain: bool = False,
) -> Optional[ComboResult]:
    """ビームサーチで最大ダメージコンボを探索する。

    Args:
        moves:         キャラクターの全技リスト (fetch_combo_moves の返り値)。
        initial_adv:   コンボ開始時の有利フレーム。
        starter_input: 既に使った技のinput (繰り返し禁止用)。None = 始動技なし。
        initial_drive: 開始時のドライブゲージ本数 (デフォルト: 6)。
        max_depth:     最大コンボ深さ。
        beam_width:    ビーム幅 (各ステップで保持する候補数)。

    Returns:
        最大ダメージの ComboResult、見つからない場合は None。
    """
    # State: (adv, drive, steps_tuple, damage, ended)
    # steps_tuple: ComboStep のタプル (イミュータブル)
    initial_state = (initial_adv, initial_drive, (), 0, False)
    beam: list[tuple] = [initial_state]

    best: Optional[ComboResult] = None
    best_damage = 0

    def update_best(steps, damage, drive_spent, ends_kd):
        nonlocal best, best_damage
        if damage > best_damage and steps:
            best_damage = damage
            best = ComboResult(
                steps=list(steps),
                total_damage=damage,
                total_drive_spent=initial_drive - drive_spent,
                ends_in_kd=ends_kd,
                initial_adv=initial_adv,
            )

    for depth in range(max_depth):
        next_beam: list[tuple] = []

        for adv, drive, steps, dmg, ended in beam:
            if ended:
                # コンボ終了済み (KD等) → このまま保持
                update_best(steps, dmg, drive, True)
                continue

            last_input = steps[-1].move.input if steps else starter_input
            expanded = False

            # --- チェーンキャンセルの処理 ---
            # 直前の技がチェーン可能、または始動技がチェーン可能な場合
            last_mv_node = steps[-1].move if steps else None
            chain_mode = (
                (last_mv_node is not None and last_mv_node.chain_cancelable)
                or (not steps and first_step_chain)  # 始動技のチェーン
            )
            chain_target_types = frozenset({'ground_normal'})  # チェーン対象は地上通常技

            for mv in moves:
                # チェーンモード: 軽量技なら startup チェック不要
                is_chain = (chain_mode and mv.move_type in chain_target_types
                            and mv.startup_f <= 6)  # 軽量技はおおむね発生6F以下
                if not is_chain:
                    # 通常: フレームが繋がらない技はスキップ
                    if mv.startup_f > adv:
                        continue
                # 直前と同じinputの繰り返しはスキップ
                if mv.input == last_input and mv.move_type == 'ground_normal':
                    continue

                mv_dmg = dmg + mv.damage
                # チェーン時は仮想有利を使用してコンテキストに表示
                effective_adv = MoveNode.CHAIN_ADV if is_chain else adv

                # --- 選択肢A: 通常ヒット (DR なし) ---
                step_no_dr = ComboStep(
                    move=mv,
                    adv_before=effective_adv,
                    used_dr=False,
                    adv_after=mv.hit_adv_f,
                    drive_spent=0,
                )
                new_steps = steps + (step_no_dr,)

                if mv.hit_is_knockdown or mv.hit_adv_f is None:
                    # KD → コンボ終了
                    update_best(new_steps, mv_dmg, drive, True)
                    next_beam.append((0, drive, new_steps, mv_dmg, True))
                    expanded = True
                elif mv.hit_adv_f > 0:
                    next_beam.append((mv.hit_adv_f, drive, new_steps, mv_dmg, False))
                    expanded = True

                # --- 選択肢B: DRキャンセル ---
                if mv.dr_cancelable and drive >= DRIVE_COST_DR and mv.after_dr_hit_f:
                    dr_adv = mv.after_dr_hit_f
                    new_drive = drive - DRIVE_COST_DR
                    step_dr = ComboStep(
                        move=mv,
                        adv_before=effective_adv,  # チェーン時は99を表示
                        used_dr=True,
                        adv_after=dr_adv,
                        drive_spent=DRIVE_COST_DR,
                    )
                    new_steps_dr = steps + (step_dr,)
                    next_beam.append((dr_adv, new_drive, new_steps_dr, mv_dmg, False))
                    expanded = True

            if not expanded:
                # これ以上繋がらない → コンボ終了
                update_best(steps, dmg, drive, False)

        if not next_beam:
            break

        # ビーム幅でトリム (ダメージ降順)
        next_beam.sort(key=lambda x: x[3], reverse=True)
        beam = next_beam[:beam_width]

    # 最終ビームから最善を確定
    for adv, drive, steps, dmg, ended in beam:
        update_best(steps, dmg, drive, ended)

    return best


# ============================================================
# キャラ + 始動技を指定して最大コンボを計算
# ============================================================

def compute_max_combo(
    chara: str,
    starter_input: str,
    use_dr: bool = True,
    drive_bars: int = 6,
) -> Optional[ComboResult]:
    """指定した始動技から最大コンボを計算する。

    Args:
        chara:         capcom_slug or SC chara 名。
        starter_input: 始動技の input (例: '2MP')。
        use_dr:        DRキャンセルを許可するか。
        drive_bars:    使用可能なドライブゲージ本数。

    Returns:
        ComboResult: 最大ダメージのコンボルート。見つからない場合は None。
    """
    from sf6_engine.db import get_client
    sb = get_client()

    # capcom_slug → sc_chara 変換
    map_res = sb.table('char_slug_map').select('sc_chara').eq('capcom_slug', chara.lower()).execute()
    sc_chara = map_res.data[0]['sc_chara'] if map_res.data else chara

    # 始動技のデータを取得
    starter_raw = sb.table('sc_moves').select(
        'input,name,damage,hit_adv,dr_cancel_hit,after_dr_hit,cancel'
    ).eq('chara', sc_chara).eq('input', starter_input).execute()

    if not starter_raw.data:
        logger.warning(f"始動技 {starter_input} が見つかりません")
        return None

    starter_data = starter_raw.data[0]

    # 始動技ヒット後の有利フレームを取得
    starter_norm = sb.table('sc_move_normalized').select(
        'hit_adv_f,hit_is_knockdown'
    ).eq('chara', sc_chara).eq('input', starter_input).execute()

    if not starter_norm.data:
        return None

    norm = starter_norm.data[0]
    starter_hit_adv = norm.get('hit_adv_f')
    starter_kd = norm.get('hit_is_knockdown', False)

    # 始動技がKDなら続きのコンボなし
    if starter_kd or starter_hit_adv is None or starter_hit_adv <= 0:
        logger.info(f"{starter_input} は KD または有利なし → コンボなし")
        return None

    # 始動技のDRキャンセル判定
    dr_raw = starter_data.get('dr_cancel_hit')
    after_dr_raw = starter_data.get('after_dr_hit')
    after_dr_f = _parse_multi_adv(after_dr_raw)
    starter_dr = (dr_raw and dr_raw not in ('', '-') and
                  after_dr_f is not None and after_dr_f > 0)

    # 始動技のチェーンキャンセル判定
    starter_cancel = str(starter_data.get('cancel', ''))
    starter_chain = 'Chn' in starter_cancel

    # 全技データ取得
    all_moves = fetch_combo_moves(chara)

    results: list[ComboResult] = []

    # 始動技のスタートアップ
    starter_norm_r = sb.table('sc_move_normalized').select('startup_f').eq('chara', sc_chara).eq('input', starter_input).execute()
    starter_startup = starter_norm_r.data[0]['startup_f'] if starter_norm_r.data else 0
    starter_dmg = _parse_damage(starter_data.get('damage'))

    def _make_starter_step(used_dr=False, adv_after=None, drive_spent=0) -> ComboStep:
        return ComboStep(
            move=MoveNode(
                input=starter_input,
                name=starter_data.get('name') or starter_input,
                move_type='ground_normal',
                startup_f=starter_startup,
                hit_adv_f=starter_hit_adv,
                hit_is_knockdown=False,
                damage=starter_dmg,
                dr_cancelable=starter_dr,
                after_dr_hit_f=after_dr_f,
                chain_cancelable=starter_chain,
            ),
            adv_before=-1,   # 始動技
            used_dr=used_dr,
            adv_after=adv_after or starter_hit_adv,
            drive_spent=drive_spent,
        )

    # Case 1: 始動技ヒット → 続きをビームサーチ (チェーン or 通常リンク)
    r1 = find_max_combo(
        all_moves, starter_hit_adv, starter_input, drive_bars,
        max_depth=6, first_step_chain=starter_chain
    )
    if r1:
        # 始動技のステップを先頭に追加
        full_steps = [_make_starter_step()] + r1.steps
        r1_full = ComboResult(
            steps=full_steps,
            total_damage=starter_dmg + r1.total_damage,
            total_drive_spent=r1.total_drive_spent,
            ends_in_kd=r1.ends_in_kd,
            initial_adv=starter_hit_adv,
        )
        results.append(r1_full)

    # Case 2: 始動技 → DRキャンセル → 続きをビームサーチ
    if use_dr and starter_dr and drive_bars >= DRIVE_COST_DR and after_dr_f:
        r2 = find_max_combo(
            all_moves, after_dr_f, starter_input,
            drive_bars - DRIVE_COST_DR, max_depth=6
        )
        if r2:
            # 始動技のスタートアップを取得
            starter_norm_row = sb.table('sc_move_normalized').select('startup_f').eq('chara', sc_chara).eq('input', starter_input).execute()
            starter_startup = starter_norm_row.data[0]['startup_f'] if starter_norm_row.data else 0

            # 始動技のステップを先頭に追加
            starter_step = ComboStep(
                move=MoveNode(
                    input=starter_input,
                    name=starter_data.get('name') or starter_input,
                    move_type='ground_normal',
                    startup_f=starter_startup,
                    hit_adv_f=starter_hit_adv,
                    hit_is_knockdown=False,
                    damage=_parse_damage(starter_data.get('damage')),
                    dr_cancelable=True,
                    after_dr_hit_f=after_dr_f,
                ),
                adv_before=-1,   # 始動技は自由に使える (-1 = 「始動」)
                used_dr=True,
                adv_after=after_dr_f,
                drive_spent=DRIVE_COST_DR,
            )
            full_steps = [starter_step] + r2.steps
            full_dmg   = _parse_damage(starter_data.get('damage')) + r2.total_damage
            r2_full = ComboResult(
                steps=full_steps,
                total_damage=full_dmg,
                total_drive_spent=r2.total_drive_spent + DRIVE_COST_DR,
                ends_in_kd=r2.ends_in_kd,
                initial_adv=starter_hit_adv,
            )
            results.append(r2_full)

    if not results:
        return None

    return max(results, key=lambda r: r.total_damage)
