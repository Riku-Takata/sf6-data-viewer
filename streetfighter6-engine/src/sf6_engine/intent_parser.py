"""Intent Parser: 自然言語クエリ → 構造化 JSON。

ユーザーの SF6 に関する質問を解析し、後続の Data Fetcher と RAG Builder が
使いやすい構造化 JSON に変換する。

出力スキーマ:
  intent_type  : "lookup_move" | "compare_moves" | "explain_concept" |
                 "punish_check" | "combo_info" | "general_question"
  chara        : SuperCombo の chara 値 (例: "Sagat", "Ryu")
  chara2       : 比較相手のキャラ (compare_moves 時)
  input        : numpad 表記の技入力 (例: "2HK", "5HP")
  input2       : 比較相手の技入力
  field        : 特定フィールドの指定 (例: "startup", "block_adv")
  concept      : ゲームシステムの概念名 (例: "Drive Impact", "Burnout")
  raw_query    : 元の質問文 (そのまま保持)
"""
from __future__ import annotations

import json
import logging
import re

from sf6_engine.frame_scenario import (
    merge_frame_scenarios,
    parse_frame_scenario,
    strip_scenario_phrases,
)
from sf6_engine.llm_provider import LLMProvider

# 通常技を示す単語パターン (これを含む技名は通常技と判定)
_NORMAL_MOVE_INDICATORS = re.compile(
    r'パンチ|キック'
    r'|(?:弱|中|強)[PpKk]'     # 弱P, 中K など
    r'|[LMH][KP](?:\s|$|~)'    # LP, MK, HK など
    r'|(?:立|屈|しゃがみ|ジャンプ|空中).*(?:弱|中|強)'
)

# 特定の必殺技キーワード (既知のものを明示的にマーク — フォールバック用に残す)
_SPECIAL_MOVE_KEYWORDS = re.compile(
    r'波動拳|昇竜拳|竜巻|ソニックブーム|サマーソルト|フラッシュキック'
    r'|タイガーショット|タイガーアッパー|タイガーニー|タイガーキャノン'
    r'|百裂拳|気功拳|鳳翼扇|気功掌|スピニングバードキック'
    r'|スクリューパイルドライバー|スパイラルアロー|キャノンストライク'
    r'|ガンスモーク|メテオストライク|クラッシュカウンター'
    r'|迅雷脚|疾風迅雷脚|龍尾脚|疾風|迅雷'
    r'|必殺技|スーパーアーツ|SA[123]'
)

# 日本語略称 → numpad 変換テーブル (LLM が変換できなかった場合のフォールバック)
_JP_ABBREV_TO_NUMPAD: dict[str, str] = {
    # しゃがみ系 (長い表記を先に)
    'しゃがみ弱P': '2LP', 'しゃがみ中P': '2MP', 'しゃがみ強P': '2HP',
    'しゃがみ弱K': '2LK', 'しゃがみ中K': '2MK', 'しゃがみ強K': '2HK',
    '屈弱P': '2LP',  '屈中P': '2MP',  '屈強P': '2HP',
    '屈弱K': '2LK',  '屈中K': '2MK',  '屈強K': '2HK',
    # 立ち系
    '立ち弱P': '5LP', '立ち中P': '5MP', '立ち強P': '5HP',
    '立ち弱K': '5LK', '立ち中K': '5MK', '立ち強K': '5HK',
    '立弱P': '5LP',  '立中P': '5MP',  '立強P': '5HP',
    '立弱K': '5LK',  '立中K': '5MK',  '立強K': '5HK',
    # ジャンプ系
    'ジャンプ弱P': 'j.LP', 'ジャンプ中P': 'j.MP', 'ジャンプ強P': 'j.HP',
    'ジャンプ弱K': 'j.LK', 'ジャンプ中K': 'j.MK', 'ジャンプ強K': 'j.HK',
    '空中弱P': 'j.LP',    '空中中P': 'j.MP',    '空中強P': 'j.HP',
    '空中弱K': 'j.LK',    '空中中K': 'j.MK',    '空中強K': 'j.HK',
}

# 方向 (前/後ろ/下) + 強度 (大/小 の別表記含む) の組み合わせを自動生成
# 例: '前大K' → 6HK, '後ろ強P' → 4HP。挿入順が照合優先順になるため、
# 方向付き → 立ち/しゃがみ/ジャンプの大小表記 → 素の大小表記 の順で追加する
_DIR_JP = {'前': '6', '後ろ': '4', '後': '4', '下': '2'}
_STRENGTH_JP = {'弱': 'L', '小': 'L', '中': 'M', '強': 'H', '大': 'H'}
for _d_jp, _d in _DIR_JP.items():
    for _s_jp, _s in _STRENGTH_JP.items():
        for _b in ('P', 'K'):
            _JP_ABBREV_TO_NUMPAD.setdefault(f'{_d_jp}{_s_jp}{_b}', f'{_d}{_s}{_b}')
for _p_jp, _p in (('立ち', '5'), ('立', '5'), ('しゃがみ', '2'), ('屈', '2')):
    for _s_jp, _s in (('大', 'H'), ('小', 'L')):
        for _b in ('P', 'K'):
            _JP_ABBREV_TO_NUMPAD.setdefault(f'{_p_jp}{_s_jp}{_b}', f'{_p}{_s}{_b}')
for _p_jp in ('ジャンプ', '空中'):
    for _s_jp, _s in (('大', 'H'), ('小', 'L')):
        for _b in ('P', 'K'):
            _JP_ABBREV_TO_NUMPAD.setdefault(f'{_p_jp}{_s_jp}{_b}', f'j.{_s}{_b}')
# 素の大小表記 (位置なし) は立ちとみなす。方向付き表記の後に置く
for _s_jp, _s in (('大', 'H'), ('小', 'L')):
    for _b in ('P', 'K'):
        _JP_ABBREV_TO_NUMPAD.setdefault(f'{_s_jp}{_b}', f'5{_s}{_b}')

# クエリ中に numpad 表記が明示的に書かれているかを検出するパターン
# 注: lookbehind/lookahead を英数字に拡張して「623HP」内の「3HP」誤マッチを防止
_NUMPAD_EXPLICIT = re.compile(
    r'(?<![A-Za-z0-9])'   # 前に英数字がない (例: 623HP 中の 3HP を防ぐ)
    r'('
    r'[1-9][LMH][PK]'     # 通常技: 5LP, 2MK, j.HP など
    r'|j\.[LMH][PK]'      # ジャンプ技: j.LP, j.HK
    r'|[2-9]{3,}[PK]'     # コマンド技 (強度なし): 236P, 623K, 214K
    r'|DI'
    r')(?![A-Za-z0-9])'   # 後に英数字がない
)

# コマンド技の明示表記: 623HP, 236LK, 236KK (OD), 236[LK] (ホールド), 22P, 6KK 等
# 単数字+単ボタン ('5P' 等) は誤マッチしやすいため対象外
_COMMAND_NUMPAD = re.compile(
    r'(?<![A-Za-z0-9])'
    r'('
    r'[1-9]{2,6}\[?(?:[LMH]?[PK]{1,3})\]?'   # 22P, 236LK, 236KK, 236[PP], 63214KK
    r'|[1-9](?:PP|KK|PPP|KKK)'               # 6KK, 4PP, 2KKK
    r')'
    r'(?![A-Za-z0-9])'
)

# combo_info / max_combo らしさの指標 (これが無いのに当該判定なら誤分類とみなす)
# 注: 「最大までためた」の「最大」だけで max_combo に誤分類される事例あり
_COMBO_INDICATORS = re.compile(
    r'コンボ|繋|つなが|つなげ|キャンセル|ルート|始動|派生|ラッシュ|ノックダウン|の後|火力'
)

logger = logging.getLogger(__name__)

_JP_TO_SC_CHARA: dict[str, str] = {
    "リュウ": "Ryu", "ケン": "Ken", "サガット": "Sagat", "ルーク": "Luke",
    "ガイル": "Guile", "春麗": "Chun-Li", "チュンリー": "Chun-Li",
    "キャミィ": "Cammy", "豪鬼": "Akuma", "アクマ": "Akuma",
    "ザンギエフ": "Zangief", "ブランカ": "Blanka", "ダルシム": "Dhalsim",
    "本田": "E.Honda", "エドモンド本田": "E.Honda", "エホンダ": "E.Honda",
    "ジュリ": "Juri", "マリーザ": "Marisa", "ジェイミー": "Jamie",
    "キンバリー": "Kimberly", "リリー": "Lily", "マノン": "Manon",
    "ラシード": "Rashid", "ディージェイ": "Dee_Jay", "エド": "Ed",
    "テリー": "Terry", "舞": "Mai", "エレナ": "Elena",
    "イングリッド": "Ingrid", "アレックス": "Alex", "JP": "JP",
    "A.K.I.": "A.K.I.", "AKI": "A.K.I.", "ベガ": "M.Bison",
    "M.バイソン": "M.Bison", "バイソン": "M.Bison",
    "C.ヴァイパー": "C.Viper", "ヴァイパー": "C.Viper",
}

_SC_CHARA_NAMES = {
    "Ryu", "Ken", "Sagat", "Luke", "Guile", "Chun-Li", "Cammy", "Akuma",
    "Zangief", "Blanka", "Dhalsim", "E.Honda", "Juri", "Marisa", "JP",
    "Jamie", "Kimberly", "Lily", "Manon", "Rashid", "Dee_Jay", "Ed",
    "A.K.I.", "Terry", "Mai", "Elena", "Ingrid", "Alex", "M.Bison",
    "C.Viper",
}


def _looks_like_sc_input_phrase(text: str) -> bool:
    """5HP~HP / j.HP~j.HP / [4]6LP / 22P~214P などの SC input 表記。"""
    if not text:
        return False
    if text == "-":
        return True
    if re.fullmatch(r'[LMH]?[PK]{1,3}(?:~[LMH]?[PK]{1,3})*', text):
        return True
    if re.fullmatch(r'[1-9](?:~[1-9])+', text):
        return True
    if re.fullmatch(r'[1-9]\[[1-9]\]', text):
        return True
    if re.fullmatch(r'~[LMH][PK]\s*\([A-Za-z ]+\)', text):
        return True
    return bool(
        re.fullmatch(r'[A-Za-z0-9jJ.\[\]{}()/,+~ \-]+', text)
        and (re.search(r'\d', text) or re.search(r'(?i)j\.', text))
        and re.search(r'[LPKMH]', text.upper())
    )


def _extract_simple_chara(query: str) -> tuple[str, str, str] | None:
    """先頭の「キャラ名の…」を検出して (表記, SC名, 残り) を返す。"""
    names = {**_JP_TO_SC_CHARA, **{name: name for name in _SC_CHARA_NAMES}}
    for name, sc_name in sorted(names.items(), key=lambda item: -len(item[0])):
        prefix = f"{name}の"
        if query.startswith(prefix):
            return name, sc_name, query[len(prefix):]
    return None


def _extract_simple_move(rest: str) -> str | None:
    """キャラ名を除いた質問から技名部分を抽出する。"""
    m = re.match(
        r'(.+?)(?:'
        r'の(?:発生|持続|硬直(?:差)?|全体|ガード|ヒット|ダメージ|性能|フレーム)'
        r'|を|について|は[？?]|$)',
        rest,
    )
    if not m:
        return None
    move = m.group(1).strip()
    return move or None


def _deterministic_simple_intent(query: str) -> dict | None:
    """定型のフレーム/ガード/確反質問を LLM なしで intent 化する。

    Discord bot の主要ユースケースと網羅評価の質問形は、
    「キャラの技の発生」「キャラの技をガードさせた/した」
    「キャラの技を○○でガードした後、確定反撃…」に収まる。
    ここを決定論で処理することで LLM の技名誤訳・input丸めを避ける。
    """
    detected = _extract_simple_chara(query)
    if not detected:
        return None
    _, chara, rest = detected
    move = strip_scenario_phrases(_extract_simple_move(rest))
    if not move:
        return None

    if re.search(r'確定反撃|確反|反撃|使える技|提案', query) and 'ガード' in query:
        intent_type = "punish_check"
    elif re.search(
        r'発生|持続|硬直|全体|ガード|ヒット|ダメージ|性能|何F|何フレーム|フレーム',
        query,
    ):
        intent_type = "lookup_move"
    else:
        return None

    intent: dict = {
        "intent_type": intent_type,
        "chara": chara,
        "raw_query": query,
    }

    punisher = None
    for name, sc_name in sorted(_JP_TO_SC_CHARA.items(), key=lambda item: -len(item[0])):
        if re.search(rf'{re.escape(name)}でガード', query):
            punisher = sc_name
            break
    if punisher:
        intent["chara2"] = punisher

    scenario = parse_frame_scenario(query)
    if scenario:
        intent["scenario"] = scenario

    if _looks_like_sc_input_phrase(move):
        intent["input"] = move
    else:
        for jp, numpad in _JP_ABBREV_TO_NUMPAD.items():
            if jp == move:
                intent["input"] = numpad
                break
        if "input" not in intent:
            intent["move_name"] = move

    return intent

# ============================================================
# Intent のスキーマ定義
# ============================================================

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent_type": {
            "type": "string",
            "enum": [
                "lookup_move",      # 技の情報照会
                "compare_moves",    # 技の比較
                "explain_concept",  # ゲームシステム説明
                "punish_check",     # 反撃確認
                "combo_info",       # コンボ情報 (キャンセル先・DR後フレーム)
                "max_combo",        # 最大コンボ計算 (ダメージ最大のコンボルート)
                "setplay_analysis", # セットプレイ・起き攻め分析 (KD後の択計算)
                "general_question", # その他
            ],
        },
        "chara":     {"type": "string", "description": "キャラ名 (SuperCombo 表記)"},
        "chara2":    {"type": "string", "description": "比較先キャラ"},
        "input":     {"type": "string", "description": "通常技の numpad 表記 (例: 2HK, 5HP)"},
        "input2":    {"type": "string", "description": "比較先技の numpad 表記"},
        "move_name":  {"type": "string", "description": "必殺技・SAの技名 (例: Tiger Shot, Shoryuken, 波動拳)"},
        "move_name2": {"type": "string", "description": "比較先の必殺技・SA名 (compare_moves 時)"},
        "field":     {"type": "string", "description": "特定フィールド (startup/block_adv/atk_range 等)"},
        "concept":   {"type": "string", "description": "ゲーム概念名"},
        "scenario": {
            "type": "object",
            "description": "質問に明示された距離・接触持続・状態・視点などの状況条件",
            "properties": {
                "distance": {
                    "type": "string",
                    "enum": ["point_blank", "close", "mid", "far", "tip", "max_range"],
                },
                "distance_value": {"type": "number"},
                "distance_unit": {"type": "string"},
                "contact_timing": {
                    "type": "string",
                    "enum": ["first_active", "late_active", "last_active", "active_frame"],
                },
                "active_frame": {"type": "integer"},
                "stage_index": {"type": "integer"},
                "opponent_state": {
                    "type": "string",
                    "enum": ["standing", "crouching", "airborne"],
                },
                "counter_state": {
                    "type": "string",
                    "enum": ["normal", "counter", "punish_counter"],
                },
                "defender_burnout": {"type": "boolean"},
                "drive_rush": {"type": "string", "enum": ["raw", "cancel"]},
                "corner": {"type": "boolean"},
                "interaction": {"type": "string", "enum": ["block", "hit"]},
                "perspective": {"type": "string", "enum": ["attacker", "defender"]},
            },
        },
        "raw_query": {"type": "string", "description": "元の質問文"},
    },
    "required": ["intent_type", "raw_query"],
}

# ============================================================
# システムプロンプト
# ============================================================

SYSTEM_PROMPT = """\
あなたはStreet Fighter 6 (SF6) の専門家アシスタントです。
ユーザーの質問を解析し、指定されたJSONスキーマに従って構造化データとして出力してください。

## キャラ名の正規化 (日本語 → SuperCombo 英語表記)
- リュウ → Ryu
- ケン → Ken
- サガット → Sagat
- ルーク → Luke
- ガイル → Guile
- 春麗 / チュンリー → Chun-Li
- キャミィ → Cammy
- 豪鬼 / アクマ → Akuma
- ザンギエフ → Zangief
- ブランカ → Blanka
- ダルシム → Dhalsim
- エドモンド本田 / エホンダ → E.Honda
- バルログ → Vega (SF6 では M.Bison)
- ジュリ → Juri
- マリーザ → Marisa
- JP → JP
- ジェイミー → Jamie
- キンバリー → Kimberly
- リリー → Lily
- マノン → Manon
- ラシード → Rashid
- ディージェイ → Dee_Jay
- エド → Ed
- A.K.I. → A.K.I.
- テリー → Terry
- M.バイソン → M.Bison
- 舞 → Mai
- エレナ → Elena
- イングリッド → Ingrid
- アレックス → Alex
- C.ヴァイパー → C.Viper

## 技入力の正規化 (日本語 → numpad 表記)
以下の表記はすべて同じ技を指す。略称・省略形もすべて正しく変換すること。

しゃがみ弱P / 屈弱P / 屈LP → 2LP
しゃがみ中P / 屈中P / 屈MP → 2MP
しゃがみ強P / 屈強P / 屈HP → 2HP
しゃがみ弱K / 屈弱K / 屈LK → 2LK
しゃがみ中K / 屈中K / 屈MK → 2MK
しゃがみ強K / 屈強K / 屈HK → 2HK

立ち弱P / 立弱P / 立LP → 5LP
立ち中P / 立中P / 立MP → 5MP
立ち強P / 立強P / 立HP → 5HP
立ち弱K / 立弱K / 立LK → 5LK
立ち中K / 立中K / 立MK → 5MK
立ち強K / 立強K / 立HK → 5HK

ジャンプ弱P / 空中弱P / J弱P → j.LP
ジャンプ中P / 空中中P / J中P → j.MP
ジャンプ強P / 空中強P / J強P → j.HP
ジャンプ弱K / 空中弱K / J弱K → j.LK
ジャンプ中K / 空中中K / J中K → j.MK
ジャンプ強K / 空中強K / J強K → j.HK

## インテント判定ルール
- 「〜の発生は?」「〜のフレームは?」「〜のリーチは?」→ lookup_move
- 「〜はキャンセルできる?」「〜のキャンセルは?」→ lookup_move (field="cancel")
- 「〜と〜どっちが速い?」「〜と〜を比べると?」→ compare_moves
- 「ドライブインパクトとは?」「バーンアウトって何?」→ explain_concept
- 「〜ガードして反撃できる?」「〜は確定反撃?」→ punish_check
- 「〜からコンボある?」「〜始動は?」「〜の後に何が繋がる?」「〜をDRキャンセルすると?」「〜をDRキャンセルすると何F?」「DRキャンセル後の有利は?」「コンボ後の有利は?」「ノックダウン後は?」→ combo_info
- 「〜からの最大コンボは?」「〜始動の最大ダメージは?」「最大コンボを教えて」「〜から何が最も繋がる?」「フルコンボは?」「BnB コンボは?」→ max_combo
- 「〜ガードして反撃できる?」「〜は確定反撃?」「〜の弱派生前は割り込める?」「〜の派生は真の連携?」「〜は割り込める?」「〜はブロックストリング?」→ punish_check
- 「〜のKD後は?」「〜ヒット後の起き攻めは?」「〜からセットプレイは?」「〜の後に前ステップしたら?」「〜の択は?」「ノックダウン後の攻め方は?」「〜でKDした後は?」→ setplay_analysis
- 上記に当てはまらない → general_question

## フィールド名のマッピング (field パラメータ)
- 発生 → startup
- ガード時 / ガード有利 → block_adv
- ヒット時 / ヒット有利 → hit_adv
- パニカン / パニッシュカウンター → punish_adv
- リーチ / 攻撃範囲 → atk_range
- ダメージ → damage
- 無敵 → invuln

## 技入力 (input フィールド) の設定ルール ★最重要★
- input フィールドに設定できるのは上記「通常技18パターン」に該当する技のみ
- 以下の場合に input を設定する:
  - 「5HP」「2HK」「j.MK」等の numpad 表記が質問文中に含まれる場合
  - 「屈弱P」「立中P」「屈強K」等の上記略称・省略形が含まれる場合
- 以下の技名は input フィールドに設定してはならない (省略すること):
  - 波動拳、昇竜拳、竜巻旋風脚、足刀蹴り → input 省略
  - タイガーショット、タイガーアッパーカット → input 省略
  - ソニックブーム、サマーソルトキック → input 省略
  - その他すべての必殺技・SA 名 → input 省略

## 必殺技・SA の技名 (move_name フィールドを使う)
- 必殺技・SA の技名は input ではなく move_name フィールドに設定する
- move_name には英語名を優先して設定する (日本語でも可)

## 良い例
- 「波動拳のガード硬直は?」→ {"intent_type": "lookup_move", "chara": "Ryu", "move_name": "Hadoken"}
- 「昇竜拳のガード硬直は?」→ {"intent_type": "lookup_move", "chara": "Ken", "move_name": "Shoryuken"}
- 「タイガーショットのデータは?」→ {"intent_type": "lookup_move", "chara": "Sagat", "move_name": "Tiger Shot"}
- 「竜巻旋風脚のフレームは?」→ {"intent_type": "lookup_move", "chara": "Ryu", "move_name": "Tatsumaki Senpu-kyaku"}
- 「サガットの5HPのリーチは?」→ {"intent_type": "lookup_move", "chara": "Sagat", "input": "5HP"}
- 「ケンの中迅雷脚のフレームは?」→ {"intent_type": "lookup_move", "chara": "Ken", "move_name": "Jinrai Kick"}
- 「ケンの迅雷脚の弱派生は?」→ {"intent_type": "combo_info", "chara": "Ken", "move_name": "Jinrai Kick"}
- 「ケンの迅雷脚の弱派生前は割り込める?」→ {"intent_type": "punish_check", "chara": "Ken", "move_name": "Jinrai Kick"}
- 「ルークの5MPをDRキャンセルすると何F?」→ {"intent_type": "combo_info", "chara": "Luke", "input": "5MP"}

## 悪い例 (絶対にやらないこと)
- 「波動拳のガード硬直は?」→ input: "5HP" ← これは間違い (波動拳≠5HP)
- 「昇竜拳のガード硬直は?」→ input: "5HP" ← これは間違い (昇竜拳≠5HP)

## 出力規則
- 必ず有効な JSON のみを出力すること
- キャラが特定できない場合は chara フィールドを省略
- 技が特定できない場合、または特殊技の場合は input フィールドを省略
- null や空文字は設定しないこと (フィールドごと省略)
"""

# ============================================================
# メイン関数
# ============================================================

async def parse_intent(query: str, provider: LLMProvider) -> dict:
    """自然言語クエリを構造化 Intent JSON に変換する。

    Args:
        query   : ユーザーの質問文。
        provider: LLMProvider インスタンス。

    Returns:
        dict: INTENT_SCHEMA に準拠した Intent。
              最低限 {"intent_type": "...", "raw_query": "..."} を含む。
    """
    deterministic = _deterministic_simple_intent(query)
    if deterministic:
        logger.debug(f"Intent parsed deterministically: {deterministic}")
        return deterministic

    prompt = f'次のSF6に関する質問を解析してください:\n\n{query}'

    try:
        from sf6_engine.token_usage import usage_label
        with usage_label("intent"):
            result = await provider.generate_structured(
                prompt=prompt,
                schema=INTENT_SCHEMA,
                system=SYSTEM_PROMPT,
            )
    except (ValueError, Exception) as e:
        logger.warning(f"Intent parse failed: {e}. Falling back to general_question.")
        fallback = {"intent_type": "general_question", "raw_query": query}
        scenario = parse_frame_scenario(query)
        if scenario:
            fallback["scenario"] = scenario
        return fallback

    # raw_query が欠けている場合は補完
    result.setdefault("raw_query", query)
    result.setdefault("intent_type", "general_question")

    # intent_type がスキーマ外の値 (例: "punish_adv") の場合は lookup_move にフォールバック
    _VALID_INTENTS = {
        "lookup_move", "compare_moves", "explain_concept",
        "punish_check", "combo_info", "max_combo",
        "setplay_analysis", "general_question",
    }
    if result.get("intent_type") not in _VALID_INTENTS:
        logger.warning(
            f"Invalid intent_type '{result['intent_type']}' — falling back to lookup_move"
        )
        result["intent_type"] = "lookup_move"

    # --- ポストプロセス検証 ---

    # (1) 必殺技と判定される場合に input の誤設定を除去
    # 判定基準 (いずれか):
    #   a) クエリに既知の必殺技キーワードが含まれる
    #   b) move_name が設定されており、通常技ワード (パンチ/キック等) を含まない
    #      → LLM が任意の必殺技名を move_name に入れた場合に汎用的に対応
    if result.get("input") and not _NUMPAD_EXPLICIT.search(query):
        move_name_val = result.get("move_name", "")
        is_special_context = (
            _SPECIAL_MOVE_KEYWORDS.search(query)
            or (
                move_name_val
                and not _NORMAL_MOVE_INDICATORS.search(move_name_val)
            )
        )
        if is_special_context:
            logger.info(
                f"Removed incorrect input '{result['input']}' "
                f"(special move context: move_name='{move_name_val}')"
            )
            result.pop("input", None)

    # (2) input が None なのに query に numpad 表記が含まれている場合は抽出
    if not result.get("input") and not _SPECIAL_MOVE_KEYWORDS.search(query):
        m = _NUMPAD_EXPLICIT.search(query)
        if m:
            extracted = m.group(1)
            result["input"] = extracted
            logger.info(f"Extracted input '{extracted}' from raw query")

    # (3) 日本語略称テーブルで確定変換 (LLM の誤変換を上書き)
    # 略称は一意に numpad に対応するので LLM の推測より優先する
    if not _SPECIAL_MOVE_KEYWORDS.search(query):
        for jp, numpad in _JP_ABBREV_TO_NUMPAD.items():
            if jp in query:
                if result.get("input") != numpad:
                    logger.info(
                        f"JP abbrev override: '{jp}' → '{numpad}'"
                        + (f" (LLM was: {result['input']})" if result.get("input") else "")
                    )
                    result["input"] = numpad
                break

    # (4a) _SPECIAL_MOVE_KEYWORDS に一致する日本語技名がクエリにあり move_name 未設定 → JP→EN変換して設定
    if not result.get("move_name") and not result.get("input"):
        # _JP_ABBREV_TO_NUMPAD と同様に, 既知の JP技名 → 英語技名マッピングを逆引き
        # import は _JP_MOVE_TO_EN を参照 (rag_builder の import は循環参照になるため直接定義)
        _JP_SPECIAL_NAMES = {
            '波動拳': 'Hadoken', '昇竜拳': 'Shoryuken', '竜巻旋風脚': 'Tatsumaki',
            '竜巻': 'Tatsumaki', 'ソニックブーム': 'Sonic Boom',
            'サマーソルトキック': 'Somersault Kick', 'サマーソルト': 'Somersault',
            'タイガーショット': 'Tiger Shot', 'タイガーアッパーカット': 'Tiger Uppercut',
            'タイガーアッパー': 'Tiger Uppercut', 'タイガーニークラッシュ': 'Tiger Knee Crush',
            'タイガーニー': 'Tiger Knee Crush', 'タイガーネクサス': 'Tiger Nexus',
            'タイガーキャノン': 'Tiger Cannon', 'サベージタイガー': 'Savage Tiger',
            'ノヴァ': 'Nova Tiger', 'グリード': 'Greedy Tiger', 'マイト': 'Mighty Tiger',
            'モノリス': 'Tiger Monolith', 'ステハイ': 'Step High Kick',
            'ステロー': 'Step Low Kick',
            '迅雷脚': 'Jinrai Kick', '疾風迅雷脚': 'Shippu Jinrai-kyaku',
            '龍尾脚': 'Dragonlash Kick',
            '百裂拳': 'Hyakuretsu', 'スピニングバードキック': 'Spinning Bird Kick',
            '気功掌': 'Kikosho', '鳳翼扇': 'Kikosho',
            'スクリューパイルドライバー': 'Screw Pile Driver',
            'スパイラルアロー': 'Spiral Arrow', 'キャノンスパイク': 'Cannon Spike',
            '瞬獄殺': 'Shun Goku Satsu',
            # ルーク
            'フラッシュナックル': 'Flash Knuckle', '強フラッシュナックル': 'Flash Knuckle',
            '中フラッシュナックル': 'Flash Knuckle', '弱フラッシュナックル': 'Flash Knuckle',
            'サンドブラスト': 'Sandblast', 'ライジングアッパー': 'Rising Uppercut',
        }
        for jp, en in _JP_SPECIAL_NAMES.items():
            if jp in query:
                result["move_name"] = en
                logger.info(f"JP special name extracted: '{jp}' → '{en}'")
                break

    # (4b) コマンド表記 (623HP, 236LK 等) が含まれる場合は move_name として抽出
    # 通常技パターン ([1-9][LMH][PK]) と混同しないよう、3桁以上の数字 + 強度修飾子付きのみ対象
    if not result.get("move_name") and not result.get("input"):
        m = _COMMAND_NUMPAD.search(query)
        if m:
            result["move_name"] = m.group(1)
            logger.info(f"Extracted command notation as move_name: '{m.group(1)}'")

    # (4c) クエリにコマンド表記 (236LK / 236[LK] / 623HP 等) が明示されている場合は
    # LLM の input 出力より常に優先する (LLM は '236LK' → '2LK' のように
    # トークンを壊すことがある — 明示表記の転記は決定論層の仕事)
    m_cmd = _COMMAND_NUMPAD.search(query)
    if m_cmd and result.get("input") != m_cmd.group(1):
        if result.get("input"):
            logger.info(
                f"Command notation override: input '{result['input']}' → '{m_cmd.group(1)}'"
            )
        result["input"] = m_cmd.group(1)

    # (5) combo_info / max_combo 誤分類の補正: コンボ関連語がクエリに無いのに
    # 当該判定の場合は lookup_move に降格 (例: 「236LKをためた時の性能は?」
    # 「最大までためた時の性能は?」)
    if (result.get("intent_type") in ("combo_info", "max_combo")
            and not _COMBO_INDICATORS.search(query)):
        logger.info(
            f"{result['intent_type']} without combo keywords — downgrading to lookup_move"
        )
        result["intent_type"] = "lookup_move"

    # (4) 英語の技名がクエリに含まれて move_name 未設定の場合は抽出
    # 「ルークのFlash Knuckleから…」のように日本語文中に英語技名が埋め込まれたケース
    if not result.get("move_name") and not result.get("input"):
        # 大文字始まりの英単語が2語以上連続 → 技名候補 (例: "Flash Knuckle", "Rising Uppercut")
        # ただしキャラ名は除外
        _CHAR_NAMES = {
            'Ryu', 'Ken', 'Guile', 'Luke', 'Sagat', 'Cammy', 'Chun', 'Li',
            'Zangief', 'Blanka', 'Dhalsim', 'Akuma', 'Juri', 'Marisa',
            'Jamie', 'Kimberly', 'Lily', 'Manon', 'Rashid', 'Dee', 'Jay',
            'Ed', 'Terry', 'Mai', 'Elena', 'Ingrid', 'Alex', 'Bison',
            'Viper', 'Honda', 'JP', 'Street', 'Fighter',
        }
        en_move_pattern = re.compile(r'(?<![A-Za-z])([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})+)(?![A-Za-z])')
        for m in en_move_pattern.finditer(query):
            words = m.group(1).split()
            if not any(w in _CHAR_NAMES for w in words):
                result["move_name"] = m.group(1)
                logger.info(f"Extracted English move name: '{m.group(1)}'")
                break

    # LLM が状況を省略・誤解しても、質問文に明示された条件を決定論抽出で上書きする。
    # 技名フィールドに条件語が混入した場合もここで除去する。
    scenario = merge_frame_scenarios(parse_frame_scenario(query), result.get("scenario"))
    if scenario:
        result["scenario"] = scenario
    else:
        result.pop("scenario", None)
    if result.get("move_name"):
        cleaned_move_name = strip_scenario_phrases(result["move_name"])
        if cleaned_move_name:
            result["move_name"] = cleaned_move_name

    logger.debug(f"Intent parsed: {result}")
    return result


# ============================================================
# CLI テスト用
# ============================================================

if __name__ == "__main__":
    import asyncio

    TEST_QUERIES = [
        "サガットの2HKの発生は?",
        "ドライブインパクトって何?",
        "ガイルのソニックブームガードして反撃できる?",
        "サガットの立ち強Pとルークの立ち強P、どっちがリーチ長い?",
        "バーンアウトってどうなるの?",
        "サガットの2HKでパニカン取ったら何F有利?",
    ]

    async def run_tests():
        from sf6_engine.factory import create_provider
        provider = create_provider()

        if not await provider.is_available():
            print("❌ Ollama が起動していません。`ollama serve` を実行してください。")
            return

        print(f"=== Intent Parser テスト ({provider.model}) ===\n")
        for q in TEST_QUERIES:
            print(f"Q: {q}")
            intent = await parse_intent(q, provider)
            print(f"  → {json.dumps(intent, ensure_ascii=False)}")
            print()

    asyncio.run(run_tests())
