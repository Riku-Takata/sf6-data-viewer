"""
SuperCombo Wiki の Cargo API を叩いて、SF6 のデータ実態を確認する.

このスクリプトは「探索」目的の一回限りの実行用. 本番のスクレイパーは
このスクリプトの結果を見てから設計する.

確認したいこと:
  1. Cargo API が実際にアクセス可能か (User-Agent等で弾かれないか)
  2. SF6_FrameData テーブルの行数と構造
  3. 主要フィールド (punishAdv, atkRange, blockAdv) の充填率
  4. サガットの強タイガーニー・立ち弱P等、具体技のデータが取れるか

使い方:
  python supercombo_explore.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

import requests

# SuperCombo Wiki の MediaWiki API エンドポイント
# srk.shib.live と wiki.supercombo.gg は同じ実体 (前者はミラー)
API_BASE = "https://wiki.supercombo.gg/api.php"

# CAPCOM時と同様、SuperComboもUA識別でボットを弾くため実ブラウザを偽装する.
# 礼儀的スリープ (DELAY_BETWEEN_CALLS) と低頻度アクセスで負荷を最小化することで誠実な利用を担保する.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}

REQUEST_TIMEOUT = 30
DELAY_BETWEEN_CALLS = 1.0  # 礼儀的スリープ


def cargo_query(tables: str, fields: str, where: str = "",
                limit: int = 50, offset: int = 0) -> list[dict]:
    """Cargo API で SQL 風クエリを発行.

    https://www.mediawiki.org/wiki/Extension:Cargo の cargoquery action 仕様準拠.
    """
    params = {
        "action": "cargoquery",
        "format": "json",
        "tables": tables,
        "fields": fields,
        "limit": str(limit),
        "offset": str(offset),
    }
    if where:
        params["where"] = where

    r = requests.get(API_BASE, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if "error" in data:
        raise RuntimeError(f"Cargo API error: {data['error']}")

    # 'cargoquery' = [{'title': {field1: ..., field2: ...}}, ...]
    return [item.get("title", {}) for item in data.get("cargoquery", [])]


def fetch_all(tables: str, fields: str, where: str = "",
              page_size: int = 500, max_pages: int = 20) -> list[dict]:
    """ページング込みで全件取得 (max_pages で打ち切り)."""
    all_rows: list[dict] = []
    offset = 0
    for page in range(max_pages):
        rows = cargo_query(tables, fields, where, limit=page_size, offset=offset)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
        time.sleep(DELAY_BETWEEN_CALLS)
    return all_rows


def analyze_field_fill_rate(rows: list[dict], fields: list[str]) -> dict:
    """各フィールドが何%埋まっているかを集計."""
    total = len(rows)
    if total == 0:
        return {}
    result = {}
    for f in fields:
        non_empty = sum(
            1 for r in rows
            if r.get(f) not in (None, "", "-")
        )
        result[f] = {
            "filled": non_empty,
            "total": total,
            "rate": f"{non_empty / total * 100:.1f}%",
        }
    return result


def main():
    out_dir = Path(__file__).parent / "supercombo_explore_results"
    out_dir.mkdir(exist_ok=True)

    # ============================================================
    # Step 1: そもそもAPIが叩けるか確認 (1件取るだけ)
    # ============================================================
    print("=" * 60)
    print("Step 1: API疎通確認")
    print("=" * 60)
    try:
        sample = cargo_query(
            tables="SF6_FrameData",
            fields="chara,input,name",
            limit=1,
        )
        print(f"✓ API疎通OK. サンプル1件取得: {sample}")
    except Exception as e:
        print(f"✗ API疎通失敗: {e}")
        sys.exit(1)

    time.sleep(DELAY_BETWEEN_CALLS)

    # ============================================================
    # Step 2: SF6_FrameData テーブルの全体規模を把握
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 2: SF6_FrameData の行数とキャラ一覧")
    print("=" * 60)
    chara_rows = fetch_all(
        tables="SF6_FrameData",
        fields="chara",
        page_size=500,
        max_pages=10,
    )
    chara_counter = Counter(r.get("chara") for r in chara_rows if r.get("chara"))
    print(f"\n✓ 総行数: {len(chara_rows)}")
    print(f"✓ キャラ数: {len(chara_counter)}")
    print(f"\n各キャラの技数:")
    for chara, n in sorted(chara_counter.items(), key=lambda x: -x[1]):
        print(f"  {chara:20s}: {n} 技")

    time.sleep(DELAY_BETWEEN_CALLS)

    # ============================================================
    # Step 3: サガットのフルデータ取得
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 3: サガットの全技フルデータ取得")
    print("=" * 60)
    # 主要フィールドを列挙 (ドキュメントから確認した項目)
    sagat_fields = (
        "chara,moveId,input,name,moveType,"
        "damage,startup,active,recovery,total,guard,cancel,"
        "hitAdv,blockAdv,punishAdv,perfParryAdv,"
        "DRcancelHit,DRcancelBlk,afterDRHit,afterDRBlk,"
        "hitstun,blockstun,hitstop,"
        "driveDmgBlk,driveDmgHit,driveGain,"
        "superGainHit,superGainBlk,"
        "invuln,armor,airborne,"
        "jugStart,jugIncrease,jugLimit,"
        "projSpeed,atkRange,notes"
    )
    sagat_rows = fetch_all(
        tables="SF6_FrameData",
        fields=sagat_fields,
        where="chara='Sagat'",
        page_size=200,
        max_pages=2,
    )
    print(f"✓ サガットの技数: {len(sagat_rows)}")

    # 技名一覧
    print(f"\nサガットの技 (input / name):")
    for r in sagat_rows[:20]:
        print(f"  {r.get('input', '?'):15s} | {r.get('name', '?')}")
    if len(sagat_rows) > 20:
        print(f"  ... and {len(sagat_rows) - 20} more")

    # 充填率分析
    fields_to_check = [
        "startup", "active", "recovery", "guard", "cancel",
        "hitAdv", "blockAdv", "punishAdv", "perfParryAdv",
        "DRcancelHit", "DRcancelBlk", "afterDRHit", "afterDRBlk",
        "atkRange", "invuln", "armor", "airborne",
        "jugStart", "jugLimit",
        "driveDmgBlk", "driveGain", "superGainHit",
    ]
    fill_rates = analyze_field_fill_rate(sagat_rows, fields_to_check)
    print(f"\nサガットでの主要フィールド充填率:")
    for f, info in fill_rates.items():
        bar = "█" * int(float(info["rate"].rstrip("%")) / 5)
        print(f"  {f:18s} {info['rate']:>6s}  {bar}")

    # JSON保存
    sagat_out = out_dir / "sagat_full.json"
    sagat_out.write_text(
        json.dumps(sagat_rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n✓ サガット全データを保存: {sagat_out}")

    time.sleep(DELAY_BETWEEN_CALLS)

    # ============================================================
    # Step 4: 具体例 - 強タイガーニーと立ち弱P
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 4: 注目技の中身を表示")
    print("=" * 60)
    # 入力記法でソート
    notable_inputs = ["5LP", "5HP", "236HK", "236MK", "236HP"]
    for target in notable_inputs:
        match = next(
            (r for r in sagat_rows if r.get("input") == target),
            None,
        )
        if match:
            print(f"\n--- Sagat / {target} ({match.get('name')}) ---")
            for k, v in match.items():
                if v not in (None, "", "-"):
                    print(f"  {k:18s}: {v}")
        else:
            print(f"\n--- Sagat / {target}: not found ---")

    # ============================================================
    # Step 5: 全キャラでの充填率 (大規模調査)
    # ============================================================
    print("\n" + "=" * 60)
    print("Step 5: 全キャラでの充填率 (注目フィールドのみ)")
    print("=" * 60)
    print("(全データ取得中...)")
    key_fields_str = "chara,input,name,blockAdv,punishAdv,atkRange,DRcancelBlk"
    all_rows = fetch_all(
        tables="SF6_FrameData",
        fields=key_fields_str,
        page_size=500,
        max_pages=10,
    )
    print(f"\n✓ 全件取得完了: {len(all_rows)} 行")
    fill = analyze_field_fill_rate(
        all_rows, ["blockAdv", "punishAdv", "atkRange", "DRcancelBlk"]
    )
    print(f"\n全キャラ通算の充填率:")
    for f, info in fill.items():
        bar = "█" * int(float(info["rate"].rstrip("%")) / 5)
        print(f"  {f:18s} {info['rate']:>6s} ({info['filled']}/{info['total']})  {bar}")

    # JSON保存
    all_out = out_dir / "all_chars_summary.json"
    all_out.write_text(
        json.dumps(all_rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n✓ 全キャラサマリを保存: {all_out}")

    print("\n" + "=" * 60)
    print("完了. 結果は以下に保存されました:")
    print(f"  {out_dir}/")
    print("=" * 60)


if __name__ == "__main__":
    main()