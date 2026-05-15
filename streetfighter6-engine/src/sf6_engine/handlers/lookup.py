"""
カテゴリ1: 単一技の性能照会 (lookup_move).

ユーザーが指定した「キャラ + 技名」から技の詳細データを返す.
技名の曖昧性 (例: '2HK' → 'しゃがみ強K（タイガースラストキック）') は
後続のLLMレイヤー(Layer A)が解決する前提. ただし、本ハンドラ内でも
部分一致による候補提示を行い、完全一致が無かった場合はユーザーに
選択肢を提示できるようにする.
"""
from __future__ import annotations

from sf6_engine.db import get_client
from sf6_engine.models import LookupResult, MoveDetail


def lookup_move(character: str, move_name: str) -> LookupResult:
    """指定キャラの指定技の性能データを返す.

    戦略:
      1. 完全一致でmoveを検索
      2. ヒットしたら MoveDetail を返す
      3. ヒットしなかったら 部分一致(ILIKE) で候補を3件まで拾う
      4. 部分一致も無ければ found=False + 近そうな技名なしで返す

    Args:
        character: キャラslug (例: 'sagat', 'ryu')
        move_name: 技名. 正式名 (例: '立ち弱P（タイガージャブ）') または
                   部分 (例: 'タイガージャブ') を受け付ける.

    Returns:
        LookupResult: 検索結果. found=True なら move に詳細が入る.
    """
    sb = get_client()

    # --- Step 1: 完全一致 ---
    exact = sb.table("move_normalized").select("*")\
        .eq("character_slug", character)\
        .eq("move_name", move_name)\
        .limit(1)\
        .execute()

    if exact.data:
        return LookupResult(
            found=True,
            move=MoveDetail.from_view_row(exact.data[0]),
            character=character,
            query_move_name=move_name,
        )

    # --- Step 2: 部分一致で候補を探す ---
    # PostgREST の ilike.*keyword*  がワイルドカードマッチ
    partial = sb.table("move_normalized").select("move_name")\
        .eq("character_slug", character)\
        .ilike("move_name", f"%{move_name}%")\
        .limit(10)\
        .execute()

    candidate_names = [r["move_name"] for r in partial.data] if partial.data else []

    # --- Step 3: 部分一致が1件だけなら、それを「見つけた」扱いにする ---
    # (技名の曖昧記法を吸収するため)
    if len(candidate_names) == 1:
        disambiguated = sb.table("move_normalized").select("*")\
            .eq("character_slug", character)\
            .eq("move_name", candidate_names[0])\
            .limit(1)\
            .execute()
        if disambiguated.data:
            return LookupResult(
                found=True,
                move=MoveDetail.from_view_row(disambiguated.data[0]),
                character=character,
                query_move_name=move_name,
                message=f"完全一致はありませんでしたが、'{candidate_names[0]}' が唯一該当したため返します.",
            )

    # --- Step 4: 候補を提示して終わる ---
    if candidate_names:
        return LookupResult(
            found=False,
            character=character,
            query_move_name=move_name,
            candidate_names=candidate_names,
            message=f"完全一致なし. 近そうな技名が{len(candidate_names)}件見つかりました.",
        )

    # --- Step 5: 完全に該当なし ---
    return LookupResult(
        found=False,
        character=character,
        query_move_name=move_name,
        candidate_names=[],
        message=f"キャラ '{character}' の技 '{move_name}' は見つかりませんでした.",
    )


if __name__ == "__main__":
    # 簡易動作確認: サガットの立ち弱Pを引いてみる
    import json
    result = lookup_move("sagat", "立ち弱P（タイガージャブ）")
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
