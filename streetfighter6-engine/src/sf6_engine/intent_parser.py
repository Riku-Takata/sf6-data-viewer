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

from sf6_engine.llm_provider import LLMProvider

# 特殊技・必殺技の日本語キーワード。
# クエリにこれらが含まれているのに numpad 表記が明示されていない場合は input を除去する。
_SPECIAL_MOVE_KEYWORDS = re.compile(
    r'波動拳|昇竜拳|竜巻|ソニックブーム|サマーソルト|フラッシュキック'
    r'|タイガーショット|タイガーアッパー|タイガーニー|タイガーキャノン'
    r'|百裂拳|気功拳|鳳翼扇|気功掌|スピニングバードキック'
    r'|スクリューパイルドライバー|スパイラルアロー|キャノンストライク'
    r'|ガンスモーク|メテオストライク|クラッシュカウンター'
    r'|必殺技|スーパーアーツ|SA[123]'
)

# クエリ中に numpad 表記が明示的に書かれているかを検出するパターン
_NUMPAD_EXPLICIT = re.compile(
    r'(?<![A-Za-z])'   # 前に英字がない (誤マッチ防止)
    r'('
    r'[1-9][LMH][PK]'  # 通常技: 5LP, 2MK, j.HP など
    r'|j\.[LMH][PK]'   # ジャンプ技: j.LP, j.HK
    r'|[2-9]{3,}[PK]'  # コマンド技: 236P, 623K, 214K
    r'|DI'
    r')(?![A-Za-z])'   # 後に英字がない
)

logger = logging.getLogger(__name__)

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
- 立ち弱P → 5LP, 立ち中P → 5MP, 立ち強P → 5HP
- 立ち弱K → 5LK, 立ち中K → 5MK, 立ち強K → 5HK
- しゃがみ弱P → 2LP, しゃがみ中P → 2MP, しゃがみ強P → 2HP
- しゃがみ弱K → 2LK, しゃがみ中K → 2MK, しゃがみ強K → 2HK
- ジャンプ弱P → j.LP, ジャンプ中P → j.MP, ジャンプ強P → j.HP
- ジャンプ弱K → j.LK, ジャンプ中K → j.MK, ジャンプ強K → j.HK

## インテント判定ルール
- 「〜の発生は?」「〜のフレームは?」「〜のリーチは?」→ lookup_move
- 「〜はキャンセルできる?」「〜のキャンセルは?」→ lookup_move (field="cancel")
- 「〜と〜どっちが速い?」「〜と〜を比べると?」→ compare_moves
- 「ドライブインパクトとは?」「バーンアウトって何?」→ explain_concept
- 「〜ガードして反撃できる?」「〜は確定反撃?」→ punish_check
- 「〜からコンボある?」「〜始動は?」「〜の後に何が繋がる?」「〜をDRキャンセルすると?」「コンボ後の有利は?」「ノックダウン後は?」→ combo_info
- 「〜からの最大コンボは?」「〜始動の最大ダメージは?」「最大コンボを教えて」「〜から何が最も繋がる?」「フルコンボは?」「BnB コンボは?」→ max_combo
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
- 以下の技名は input フィールドに設定してはならない (省略すること):
  - 波動拳、昇竜拳、竜巻旋風脚、足刀蹴り → input 省略
  - タイガーショット、タイガーアッパーカット → input 省略
  - ソニックブーム、サマーソルトキック → input 省略
  - その他すべての必殺技・SA 名 → input 省略
- 「5HP」「2HK」「j.MK」等の numpad 表記が質問文中に明示されている場合のみ input に設定する

## 必殺技・SA の技名 (move_name フィールドを使う)
- 必殺技・SA の技名は input ではなく move_name フィールドに設定する
- move_name には英語名を優先して設定する (日本語でも可)

## 良い例
- 「波動拳のガード硬直は?」→ {"intent_type": "lookup_move", "chara": "Ryu", "move_name": "Hadoken"}
- 「昇竜拳のガード硬直は?」→ {"intent_type": "lookup_move", "chara": "Ken", "move_name": "Shoryuken"}
- 「タイガーショットのデータは?」→ {"intent_type": "lookup_move", "chara": "Sagat", "move_name": "Tiger Shot"}
- 「竜巻旋風脚のフレームは?」→ {"intent_type": "lookup_move", "chara": "Ryu", "move_name": "Tatsumaki Senpu-kyaku"}
- 「サガットの5HPのリーチは?」→ {"intent_type": "lookup_move", "chara": "Sagat", "input": "5HP"}

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
    prompt = f'次のSF6に関する質問を解析してください:\n\n{query}'

    try:
        result = await provider.generate_structured(
            prompt=prompt,
            schema=INTENT_SCHEMA,
            system=SYSTEM_PROMPT,
        )
    except (ValueError, Exception) as e:
        logger.warning(f"Intent parse failed: {e}. Falling back to general_question.")
        return {"intent_type": "general_question", "raw_query": query}

    # raw_query が欠けている場合は補完
    result.setdefault("raw_query", query)
    result.setdefault("intent_type", "general_question")

    # --- ポストプロセス検証 ---

    # (1) 特殊技名があるのに numpad が設定されている場合は除去
    if result.get("input") and _SPECIAL_MOVE_KEYWORDS.search(query):
        if not _NUMPAD_EXPLICIT.search(query):
            logger.info(
                f"Removed incorrect input mapping '{result['input']}' "
                f"(special move keyword detected in query, no explicit numpad notation)"
            )
            result.pop("input", None)

    # (2) input が None なのに query に numpad 表記が含まれている場合は抽出
    if not result.get("input") and not _SPECIAL_MOVE_KEYWORDS.search(query):
        m = _NUMPAD_EXPLICIT.search(query)
        if m:
            extracted = m.group(1)
            result["input"] = extracted
            logger.info(f"Extracted input '{extracted}' from raw query")

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
