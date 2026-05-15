#!/usr/bin/env python3
"""SuperCombo Wiki データの HTML/MediaWiki マークアップを除去するユーティリティ。

SuperCombo の Cargo API から取得したフレームデータには、以下のようなゴミが混入する:
  - HTML タグ: <span style="color: #b70c0b;">'''-5'''</span>
  - MediaWiki 太字: '''テキスト'''
  - MediaWiki リンク: [[ページ名|表示テキスト]]
  - MediaWiki テンプレート: {{テンプレート名|引数}}
  - HTML エンティティ: &amp; &lt; &gt;
  - <br> タグ: <br>, <br/>, <br />

このスクリプトを直接実行すると、stdin から JSON を読み込んでクリーニングした JSON を出力する。
ライブラリとしてインポートすると strip_html() 関数が使える。

Usage:
  # ライブラリとして
  from sf6_engine.scripts.html_strip import strip_html
  clean = strip_html('<span style="color: #b70c0b;">\'\'\'−5\'\'\'</span>')
  # → "-5"

  # コマンドラインで
  cat supercombo_sf6_2026-04-26.json | python -m sf6_engine.scripts.html_strip > cleaned.json
"""

import html
import json
import re
import sys


# --- 正規表現パターン (コンパイル済み) ---

# HTML タグ除去 (selfclosing含む)
RE_HTML_TAG = re.compile(r"<[^>]+>")

# MediaWiki 太字 '''テキスト'''
RE_MW_BOLD = re.compile(r"'{2,3}")

# MediaWiki リンク [[ページ名|表示テキスト]] → 表示テキスト
# [[ページ名]] → ページ名
RE_MW_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")

# MediaWiki テンプレート {{名前|引数1|引数2...}} → 引数1 (最初の引数)
RE_MW_TEMPLATE = re.compile(r"\{\{[^|}]+\|([^|}]+)(?:\|[^}]*)?\}\}")

# MediaWiki テンプレート (引数なし) {{名前}} → 除去
RE_MW_TEMPLATE_BARE = re.compile(r"\{\{[^}]*\}\}")

# 全角マイナス → 半角マイナス
RE_FULLWIDTH_MINUS = re.compile(r"[−﹣]")

# 連続空白 → 単一空白
RE_MULTI_SPACE = re.compile(r"[ \t]+")

# <br> タグ → 改行
RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def strip_html(text: str) -> str:
    """HTML/MediaWiki マークアップを除去してプレーンテキストを返す。

    Args:
        text: クリーニング対象の文字列。None や空文字はそのまま返す。

    Returns:
        クリーニングされた文字列。

    Examples:
        >>> strip_html('<span style="color: #b70c0b;">\\'\\'\\'-5\\'\\'\\'</span>')
        '-5'
        >>> strip_html('Long range poke.<br>Good for whiff punish.')
        'Long range poke. Good for whiff punish.'
        >>> strip_html("[[SF6/Sagat|Sagat]]'s 5HP")
        "Sagat's 5HP"
        >>> strip_html("")
        ''
        >>> strip_html(None)
        ''
    """
    if not text:
        return ""

    s = text

    # Step 1: <br> → スペース (改行の代わり。1行テキストにするため)
    s = RE_BR.sub(" ", s)

    # Step 2: HTML タグ除去
    s = RE_HTML_TAG.sub("", s)

    # Step 3: HTML エンティティをデコード
    s = html.unescape(s)

    # Step 4: MediaWiki リンク
    s = RE_MW_LINK.sub(r"\1", s)

    # Step 5: MediaWiki テンプレート (引数あり → 最後の引数を残す)
    s = RE_MW_TEMPLATE.sub(r"\1", s)

    # Step 6: MediaWiki テンプレート (引数なし → 除去)
    s = RE_MW_TEMPLATE_BARE.sub("", s)

    # Step 7: MediaWiki 太字マーカー除去
    s = RE_MW_BOLD.sub("", s)

    # Step 8: 全角マイナス → 半角
    s = RE_FULLWIDTH_MINUS.sub("-", s)

    # Step 9: 連続空白を正規化
    s = RE_MULTI_SPACE.sub(" ", s)

    # Step 10: 前後の空白除去
    s = s.strip()

    return s


def strip_move_fields(move: dict) -> dict:
    """SuperCombo の1技レコードに対して、対象フィールドを一括クリーニングする。

    元データ (raw_json) はそのまま保持し、表示用フィールドをクリーニングする。

    Args:
        move: SuperCombo Cargo API のレコード (dict)

    Returns:
        クリーニング済みの新しい dict
    """
    # クリーニング対象フィールド
    STRIP_FIELDS = [
        "damage", "startup", "active", "recovery",
        "onBlock", "onHit", "punishAdv", "perfParryAdv",
        "atkRange", "cancel", "guard",
    ]

    cleaned = dict(move)  # shallow copy

    for field in STRIP_FIELDS:
        if field in cleaned and cleaned[field]:
            cleaned[field] = strip_html(cleaned[field])

    # notes は別途保持 (notes_raw = 元データ, notes = クリーン版)
    if "notes" in cleaned:
        cleaned["notes_raw"] = move.get("notes", "")
        cleaned["notes"] = strip_html(move.get("notes", ""))

    return cleaned


def clean_json_file(input_stream, output_stream):
    """JSON ファイル全体をクリーニングして出力する。"""
    data = json.load(input_stream)

    if isinstance(data, list):
        cleaned = [strip_move_fields(move) for move in data]
    elif isinstance(data, dict):
        # 単一レコード
        cleaned = strip_move_fields(data)
    else:
        raise ValueError(f"Unexpected JSON structure: {type(data)}")

    json.dump(cleaned, output_stream, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    clean_json_file(sys.stdin, sys.stdout)
