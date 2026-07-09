"""CAPCOM⇔SC 必殺技/SA の自動マッチング (フレームシグネチャ方式)。

CAPCOM (move_normalized) の必殺技/SA 各行を、同キャラの SC (sc_move_normalized)
Special/Super 行と突き合わせる。強度prefix (弱/中/強/OD) と input の強度キーで
候補を絞り、(発生, ガード硬直, ヒット硬直/KD, 持続, 全体) の一致数スコアで判定。

- 一意な最良候補 → matched
- 同点複数 → ambiguous (レビュー対象)
- 候補なし/低スコア → unmatched (ログのみ、結合なし許容)

MANUAL_OVERRIDES で ambiguous/unmatched を確定できる。

出力:
  scripts/out/special_match_report.txt  (レビュー用)
  scripts/out/special_move_map_seed.json (シードデータ)

使い方:
  PYTHONPATH=src ./.venv312/bin/python scripts/match_specials.py
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from sf6_engine.db import get_client

OUT_DIR = Path(__file__).parent / "out"

_CAPCOM_STRENGTH = re.compile(r'(?:^|\s)(弱|中|強|OD|SA1|SA2|SA3|CA)\s*')
_COND_TAG = re.compile(r'^(【[^】]*】|\[[^\]]*\])\s*')

# input の強度キー判定 (SC 側)
_OD_KEYS = ('PP', 'KK', 'LPMP', 'MPHP', 'LPHP', 'LKMK', 'MKHK', 'LKHK')


def parse_capcom_name(move_name: str) -> tuple[str, str | None, list[str]]:
    """CAPCOM 技名 → (ファミリー名, 強度, 条件タグ列)。

    例: '[風纏い]OD 弱 コンドルスパイア' → ('コンドルスパイア', 'OD', ['[風纏い]'])
        'SA1 ソニックハリケーン （上）' → ('ソニックハリケーン （上）', 'SA1', [])
    """
    name = move_name
    conds: list[str] = []
    while (m := _COND_TAG.match(name)):
        conds.append(m.group(1))
        name = name[m.end():]
    strengths = _CAPCOM_STRENGTH.findall(name)
    # OD 弱 のような複合は「OD」を強度として優先 (SC input が PP/KK になるため)
    strength = None
    if strengths:
        strength = 'OD' if 'OD' in strengths else strengths[0]
    family = _CAPCOM_STRENGTH.sub(' ', name).strip()
    family = re.sub(r'\s+', ' ', family)
    return family, strength, conds


def sc_strength(input_str: str, name: str) -> str:
    """SC input/name から強度クラスを判定: L/M/H/OD/NA。"""
    s = (input_str or '').upper().replace(' ', '')
    if any(k in s for k in _OD_KEYS):
        return 'OD'
    n = (name or '')
    if re.match(r'^OD\s', n):
        return 'OD'
    for cls, keys in (('L', ('LP', 'LK')), ('M', ('MP', 'MK')), ('H', ('HP', 'HK'))):
        if any(k in s for k in keys):
            return cls
    m = re.match(r'^(LP|LK|MP|MK|HP|HK)\s', n)
    if m:
        return m.group(1)[0]
    return 'NA'


_STRENGTH_TO_CLS = {'弱': 'L', '中': 'M', '強': 'H', 'OD': 'OD'}

# SC name の強度prefix除去 (ファミリー整合ボーナス用)
_SC_FAMILY_RE = re.compile(r'^(LP|MP|HP|LK|MK|HK|OD|EX)\s+')


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def signature_score(c: dict, s: dict) -> tuple[int, int]:
    """(一致数, 比較可能数)。発生/ガード/ヒット(KD含む)/持続/全体を比較。"""
    # recovery は除外 (空中技の着地硬直などカウント方法が CAPCOM/SC で異なる)
    pairs = [
        (_int_or_none(c.get('startup_int')), _int_or_none(s.get('startup_f'))),
        (_int_or_none(c.get('on_block_int')), _int_or_none(s.get('block_adv_f'))),
    ]
    score = comparable = 0
    for cv, sv in pairs:
        if cv is not None and sv is not None:
            comparable += 1
            if cv == sv:
                score += 1
    # ヒット: KD フラグ or 数値
    c_kd, s_kd = c.get('on_hit_is_knockdown'), s.get('hit_is_knockdown')
    c_hit, s_hit = _int_or_none(c.get('on_hit_int')), _int_or_none(s.get('hit_adv_f'))
    if c_kd is not None and s_kd is not None and (c_kd or s_kd):
        comparable += 1
        if bool(c_kd) == bool(s_kd):
            score += 1
    elif c_hit is not None and s_hit is not None:
        comparable += 1
        if c_hit == s_hit:
            score += 1
    return score, comparable


# ==================================================================
# 手動確定 (レビュー後に追記): (capcom_slug, capcom_move_name) → sc_input
# sc_input が None の場合は「対応なし」として確定 (unmatched 扱いで抑制)
# ==================================================================
MANUAL_OVERRIDES: dict[tuple[str, str], str | None] = {
    # --- AMBIG の解決 (技名の意味対応で確定) ---
    ('alex', 'OD パワーボム'): '63214PP',
    ('alex', 'パワードロップ'): '63214P Backturn',
    ('alex', 'OD パワードロップ'): '63214PP Backturn',
    ('blanka', 'ブランカちゃん爆弾（射出）'): '22P~214P',
    ('chunli', 'SA1 空中気功掌'): 'j.236236P',
    ('deejay', 'SA2 サンライズフェスティバル・ライト'): '236236LP',
    ('deejay', 'SA2 サンライズフェスティバル・マーベラス'): '236236MP',
    ('deejay', 'SA2 サンライズフェスティバル・マキシマム'): '236236HP',
    ('ehonda', 'OD 大銀杏投げ'): '63214KK',
    ('elena', 'SA2リヴァイブダンス'): '236236P',
    ('gouki_akuma', 'OD 斬空波動拳'): 'j.236PP',
    ('gouki_akuma', '空中竜巻斬空脚'): 'j.214K',
    ('gouki_akuma', '百鬼豪斬空'): '236KK~j.236P',
    ('guile', 'ソニックブレイク （単発）'): 'PP',
    ('guile', 'ソニックブレイク （派生）'): '[4]6P~P',
    ('guile', 'SA1 ソニックハリケーン （横）'): '[4]646P',
    ('jamie', '弱 流酔拳（2段目）'): '236LP~6P',
    ('jamie', '中 流酔拳（2段目）'): '236MP~6P',
    ('jamie', '強 流酔拳（2段目）'): '236HP~6P',
    ('jamie', 'OD 流酔拳（2段目）'): '236PP~6P',
    ('jamie', '[酔いレベル4]流酔拳（2段目）'): None,
    ('jamie', '[酔いレベル4]流酔拳（3段目）'): None,
    ('juri', 'SA1 殺界風破斬'): '236236K',
    ('ken', '空中竜巻旋風脚'): 'j.214K',
    ('ken', 'OD 空中竜巻旋風脚'): 'j.214KK',
    ('kimberly', 'SA1 武神乱拍子'): '236236K',
    ('kimberly', 'SA1 武神乱拍子・雷譜'): '236236[K]',
    ('kimberly', 'SA2 空中武神天翔亢竜'): 'j.214214P',
    ('lily', '強 コンドルウィンド'): '214HP (HOLD OK)',
    ('lily', '強 コンドルスパイア'): '236HK',
    ('lily', 'コンドルダイブ'): 'j.PP',
    ('lily', 'OD コンドルダイブ'): 'j.PPP',
    ('lily', 'SA2 スカイサンダーバード'): 'j.236236K',
    ('lily', '[風纏い]SA2 スカイサンダーバード'): 'j.236236K',
    ('luke', '弱 フラッシュナックル（ホールド）'): '214[LP]',
    ('luke', '中 フラッシュナックル（ホールド）'): '214[MP]',
    ('luke', '強 フラッシュナックル（ホールド）'): '214[HP]',
    ('luke', '弱 フラッシュナックル（ジャスト）'): 'pf.214[LP]',
    ('luke', '中 フラッシュナックル（ジャスト）'): 'pf.214[MP]',
    ('luke', '強 フラッシュナックル（ジャスト）'): 'pf.214[HP]',
    ('luke', 'スラムダンク'): '623PP~PP',
    ('manon', 'OD マネージュ・ドレ（メダルLvに応じて動作が変化）'): '63214PP',
    ('zangief', 'SA2 サイクロンラリアット（ホールド）'): '236236[P]',
    # --- LOOSE 誤マッチの修正 ---
    ('aki', '紫煙追'): '214LP~6P',
    ('aki', 'OD 紫煙追'): '214PP~6P',
    ('aki', '紫煙追 （炸裂）'): '214LP~6P',
    ('aki', 'OD 紫煙追 （炸裂）'): '214PP~6P',
    ('cammy', 'キャノンストライク'): 'j.214K',
    ('deejay', 'OD ワニングムーン'): '214KK~MK',
    ('dhalsim', '弱 ヨガファイア'): '236P',
    ('dhalsim', '中 ヨガファイア'): '236P',
    ('dhalsim', '強 ヨガファイア'): '236P',
    ('dhalsim', 'OD ヨガファイア'): '236PP',
    ('dhalsim', '強 ヨガファイア（ホールド）'): '236[P]',
    ('dhalsim', '強 ヨガアーチ'): '236LK',
    ('dhalsim', 'OD ヨガアーチ'): '236KK',
    ('ed', '弱 サイコシュート'): '236P~6P',
    ('ed', '中 サイコシュート'): '236P~6P',
    ('ed', '強 サイコシュート'): '236P~6P',
    ('ed', 'OD サイコシュート'): '236PP~6P',
    ('jp', '弱 トリグラフ'): '22P',
    ('jp', '中 トリグラフ'): '22P',
    ('jp', '強 トリグラフ'): '22P',
    ('jp', 'OD トリグラフ'): '22PP',
    ('kimberly', '空中武神旋風脚'): 'j.214K',
    ('kimberly', 'OD 空中武神旋風脚'): 'j.214KK',
    ('mai', '乱れ花蝶扇'): '236[PP]~6P',
    ('mai', '[強化版]乱れ花蝶扇'): '236[PP]~6P',
    ('mai', '弱 花蝶扇（ホールド）'): '236[P]',
    ('mai', '中 花蝶扇（ホールド）'): '236[P]',
    ('mai', '強 花蝶扇（ホールド）'): '236[P]',
    ('mai', 'OD 花蝶扇（ホールド）'): '236[PP]',
    ('vega_mbison', 'OD ヘッドプレス'): '[2]8KK~K or [2]8K~KK',
    ('vega_mbison', 'OD デビルリバース'): '[2]8KK~P or [2]8K~PP',
    # --- NO-HIT の高頻度技 (投げ/移動技はガード硬直がなく自動判定不可) ---
    ('alex', 'ハイパーボム'): '63214PP~6 Backturn',
    ('alex', 'SA2 オメガウィングバスター'): 'PP (SA2)',
    ('aki', '弱 蛇軽功'): '236LK',
    ('aki', '中 蛇軽功'): '236MK',
    ('aki', '強 蛇軽功'): '236HK',
    ('aki', 'OD 蛇軽功'): '236KK',
    ('aki', '悪鬼蛇行'): '2PP',
    ('aki', '雁字搦'): '2PP~LPLK',
    ('blanka', 'ブランカちゃん爆弾'): '22P',
    ('blanka', 'ローリングキャノン'): 'Any Direction + P (during SA2)',
    ('blanka', 'SA2 ライトニングビースト'): '214214P',
    ('cammy', 'OD キャノンストライク'): 'j.214KK',
    ('cammy', 'フーリガンコンビネーション'): '236P',
    ('cammy', 'フーリガンコンビネーション（ホールド）'): '236[HP]',
    ('cammy', 'OD フーリガンコンビネーション'): '236PP',
    ('cammy', 'レイザーエッジスライサー'): '236P~No Input',
    ('cammy', 'レイザーエッジスライサー（ホールド）'): '236[HP]~No Input',
    ('cammy', 'キャノンストライク（ホールド）'): None,
    ('cammy', 'リバースエッジ'): '236P~2K',
    ('cammy', 'リバースエッジ（ホールド）'): '236[HP]~2K',
    ('cammy', 'フェイタルレッグツイスター'): '236P~LPLK',
    ('cammy', 'フェイタルレッグツイスター（ホールド）'): '236[HP]~LPLK',
    ('cammy', 'OD フェイタルレッグツイスター'): '236PP~LPLK',
    ('cammy', 'サイレントステップ'): '236P~P',
    ('cammy', 'サイレントステップ（ホールド）'): '236[HP]~P',
    ('cammy', 'OD サイレントステップ'): '236PP~P',
    ('chunli', 'OD 空中百裂脚'): 'j.236KK',
    ('cviper', '弱 セイスモハンマー'): '623P',
    ('cviper', '中 セイスモハンマー'): '623P',
    ('cviper', '強 セイスモハンマー'): '623P',
    ('cviper', 'OD セイスモハンマー'): '623PP',
    ('cviper', 'セービングフォース(Lv1)'): '214K',
    ('cviper', 'セービングフォース(Lv2)'): '214{K}',
    ('cviper', 'セービングフォース(Lv3)'): '214[K]',
    ('cviper', '弱 トレースコンビネーション'): '214LP~6PP',
    ('cviper', '中 トレースコンビネーション'): '214MP~6PP',
    ('cviper', '強 トレースコンビネーション'): '214HP~6PP',
}


def main() -> None:
    sb = get_client()
    slug_map = {
        r['capcom_slug']: r['sc_chara']
        for r in sb.table('char_slug_map').select('capcom_slug,sc_chara').execute().data
    }

    matched: list[dict] = []
    ambiguous: list[str] = []
    unmatched: list[str] = []
    report: list[str] = []

    for capcom_slug, sc_chara in sorted(slug_map.items()):
        cres = sb.table('move_normalized').select(
            'move_name,section,startup_int,recovery_int,on_block_int,'
            'on_hit_int,on_hit_is_knockdown,damage_int'
        ).eq('character_slug', capcom_slug).in_('section', ['必殺技', 'スーパーアーツ']).execute()
        sres = sb.table('sc_move_normalized').select(
            'input,name,move_type,startup_f,recovery_f,block_adv_f,'
            'hit_adv_f,hit_is_knockdown'
        ).eq('chara', sc_chara).in_('move_type', ['Special', 'special', 'Super', 'super']).execute()
        sc_rows = sres.data or []
        report.append(f"\n{'='*70}\n{capcom_slug} ({sc_chara})\n{'='*70}")

        pending: list[tuple] = []
        for c in cres.data or []:
            mn = c['move_name']
            key = (capcom_slug, mn)
            if key in MANUAL_OVERRIDES:
                inp = MANUAL_OVERRIDES[key]
                if inp is not None:
                    sc_row = next((s for s in sc_rows if s['input'] == inp), None)
                    if sc_row is None:
                        report.append(f"  [BAD-OVR] {mn}  ->  {inp} (input が SC に存在しない!)")
                        unmatched.append(f"{capcom_slug}: {mn} (bad override)")
                        continue
                    matched.append({'capcom_slug': capcom_slug, 'capcom_move_name': mn,
                                    'sc_chara': sc_chara, 'sc_input': inp,
                                    'sc_name': sc_row['name'], 'match_method': 'manual'})
                    report.append(f"  [MANUAL ] {mn}  ->  {inp} ({sc_row['name']})")
                else:
                    report.append(f"  [SKIP   ] {mn}  (対応なし確定)")
                continue

            family, strength, conds = parse_capcom_name(mn)
            is_sa = c['section'] == 'スーパーアーツ' or (strength or '').startswith(('SA', 'CA'))

            # 候補: SA は super のみ / 必殺技は special のみ
            cands = [s for s in sc_rows
                     if (s['move_type'].lower() == 'super') == is_sa]
            # 強度クラスで絞り込み (弱/中/強/OD のみ)
            cls = _STRENGTH_TO_CLS.get(strength or '')
            if cls:
                narrowed = [s for s in cands if sc_strength(s['input'], s['name']) == cls]
                if narrowed:
                    cands = narrowed

            pending.append((c, mn, family, cands))

        # --- パス1: 厳格 (比較可能項目がすべて一致) ---
        fam_to_sc: dict[str, set[str]] = defaultdict(set)  # capcomファミリー → SCファミリー名
        leftover = []
        for c, mn, family, cands in pending:
            scored = [(sc, s) for s in cands
                      for sc, comp in [signature_score(c, s)]
                      if comp >= 2 and sc == comp]
            if scored:
                best = max(sc for sc, _ in scored)
                top = [s for sc, s in scored if sc == best]
                if len(top) == 1 or len({t['name'] for t in top}) == 1:
                    s = top[0]
                    matched.append({'capcom_slug': capcom_slug, 'capcom_move_name': mn,
                                    'sc_chara': sc_chara, 'sc_input': s['input'],
                                    'sc_name': s['name'], 'match_method': f'auto-sig{best}'})
                    fam_to_sc[family].add(_SC_FAMILY_RE.sub('', s['name']).strip())
                    report.append(f"  [OK sig{best}] {mn}  ->  {s['input']} ({s['name']})")
                    continue
                ambiguous.append(f"{capcom_slug}: {mn}")
                names = ' / '.join(f"{t['input']}({t['name']})" for t in top)
                report.append(f"  [AMBIG  ] {mn}  ->  {names}")
                continue
            leftover.append((c, mn, family, cands))

        # --- パス2: 緩和 (一意な最良候補 + 同ファミリー整合ボーナス) ---
        # SC 側が旧パッチ数値の場合に厳格一致が落ちるため、部分一致でも
        # 「他候補より明確に良い」場合は採用する。全件レポートで目視レビュー。
        for c, mn, family, cands in leftover:
            known_fams = fam_to_sc.get(family, set())
            scored2 = []
            for s in cands:
                sc, comp = signature_score(c, s)
                if comp == 0:
                    continue
                bonus = 0.5 if _SC_FAMILY_RE.sub('', s['name']).strip() in known_fams else 0.0
                scored2.append((sc + bonus, sc, s))
            scored2.sort(key=lambda t: -t[0])
            if scored2 and scored2[0][1] >= 1 and (
                    len(scored2) == 1 or scored2[0][0] > scored2[1][0]):
                s = scored2[0][2]
                matched.append({'capcom_slug': capcom_slug, 'capcom_move_name': mn,
                                'sc_chara': sc_chara, 'sc_input': s['input'],
                                'sc_name': s['name'], 'match_method': 'auto-loose'})
                report.append(f"  [LOOSE  ] {mn}  ->  {s['input']} ({s['name']})")
            else:
                unmatched.append(f"{capcom_slug}: {mn}")
                report.append(f"  [NO-HIT ] {mn}")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / 'special_match_report.txt').write_text('\n'.join(report))
    (OUT_DIR / 'special_move_map_seed.json').write_text(
        json.dumps(matched, ensure_ascii=False, indent=1))
    print(f"matched={len(matched)} ambiguous={len(ambiguous)} unmatched={len(unmatched)}")
    print(f"report: {OUT_DIR / 'special_match_report.txt'}")


if __name__ == '__main__':
    main()
