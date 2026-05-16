"""SuperCombo ゲームシステム文書のチャンク分割 + Supabase pgvector 格納。

処理フロー:
  1. supercombo_docs_*.json を読み込む
  2. 各ページの HTML を h2/h3 単位でチャンク分割
  3. チャンクごとに nomic-embed-text で埋め込みベクトルを生成
  4. doc_chunks テーブルに upsert

Usage:
  python -m sf6_engine.importers.docs path/to/supercombo_docs_YYYY-MM-DD.json
  python -m sf6_engine.importers.docs docs/supercombo_docs_2026-05-15.json --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================
# HTMLストリップ (シンプル版: html.parser 使用)
# ============================================================

_RE_TAG      = re.compile(r'<[^>]+>')
_RE_ENTITIES = re.compile(r'&[a-z#0-9]+;')
_RE_SPACES   = re.compile(r'[ \t]+')
_RE_LINES    = re.compile(r'\n{3,}')

# スキップする見出しキーワード (ナビゲーション等)
_SKIP_HEADINGS = {
    'sf6 navigation', 'navigation', 'contents', 'references',
    'see also', 'external links', 'categories',
}


def _strip_html(raw: str) -> str:
    """HTML タグと実体参照を除去してプレーンテキストを返す。"""
    s = _RE_TAG.sub(' ', raw)
    s = html.unescape(s)
    s = _RE_SPACES.sub(' ', s)
    s = _RE_LINES.sub('\n\n', s)
    return s.strip()


# ============================================================
# DocChunk データクラス
# ============================================================

@dataclass
class DocChunk:
    id: str               # "gauges_drive-overview"
    page: str             # "SF6/Gauges"
    heading_h2: str       # "Drive Overview"
    heading_h3: str | None
    content: str          # プレーンテキスト
    keywords: list[str] = field(default_factory=list)

    def to_db_row(self, embedding: list[float]) -> dict:
        return {
            "id": self.id,
            "page": self.page,
            "heading_h2": self.heading_h2,
            "heading_h3": self.heading_h3,
            "content": self.content,
            "keywords": self.keywords,
            "embedding": embedding,
        }


# ============================================================
# チャンク分割
# ============================================================

def _slug(text: str) -> str:
    """見出しテキストを URL セーフな slug に変換。"""
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _extract_keywords(content: str, heading_h2: str, heading_h3: str | None) -> list[str]:
    """チャンク内容からキーワードを自動抽出。"""
    # 見出し自体はキーワードとして必ず含める
    keywords = set()
    keywords.add(heading_h2.lower())
    if heading_h3:
        keywords.add(heading_h3.lower())

    # SF6 専門用語を検出
    terms = [
        'drive gauge', 'drive impact', 'drive reversal', 'drive parry',
        'drive rush', 'burnout', 'overdrive', 'super gauge', 'super art',
        'sa1', 'sa2', 'sa3',
        'counter hit', 'punish counter', 'perfect parry',
        'chip damage', 'chip ko',
        'block', 'pushback', 'frame advantage',
        'armor', 'invulnerability', 'cancel',
        'juggle', 'combo',
    ]
    content_lower = content.lower()
    for term in terms:
        if term in content_lower:
            keywords.add(term)

    return sorted(keywords)


def split_into_chunks(page_key: str, html_content: str) -> list[DocChunk]:
    """HTML ページを h2/h3 単位のチャンクに分割する。

    Returns:
        list[DocChunk]: チャンクのリスト。短すぎるチャンク (100文字未満) は
                        直前の h2 チャンクにマージする。
    """
    chunks: list[DocChunk] = []

    # h2 / h3 タグで分割
    # パターン: <h2> / <h3> タグとその内容
    pattern = re.compile(
        r'<(h2|h3)[^>]*>(.*?)</\1>(.*?)(?=<h2|<h3|$)',
        re.DOTALL | re.IGNORECASE,
    )

    current_h2 = ''
    page_slug = _slug(page_key.replace('SF6/', ''))

    for m in pattern.finditer(html_content):
        level    = m.group(1).lower()   # 'h2' or 'h3'
        heading  = _strip_html(m.group(2)).strip()
        content  = _strip_html(m.group(3)).strip()

        # ナビゲーション等はスキップ
        if heading.lower() in _SKIP_HEADINGS or len(content) < 30:
            continue

        if level == 'h2':
            current_h2 = heading
            heading_h3 = None
            chunk_id = f"{page_slug}_{_slug(heading)}"
        else:
            heading_h3 = heading
            chunk_id = f"{page_slug}_{_slug(current_h2)}_{_slug(heading)}"

        chunk = DocChunk(
            id=chunk_id,
            page=page_key,
            heading_h2=current_h2 if current_h2 else heading,
            heading_h3=heading_h3,
            content=content,
            keywords=_extract_keywords(content, current_h2 or heading, heading_h3),
        )
        chunks.append(chunk)

    # 短いチャンク (100文字未満) を前のチャンクにマージ
    merged: list[DocChunk] = []
    for chunk in chunks:
        if merged and len(chunk.content) < 100 and chunk.heading_h3:
            prev = merged[-1]
            prev.content += f'\n\n### {chunk.heading_h3}\n{chunk.content}'
            prev.keywords = sorted(set(prev.keywords + chunk.keywords))
        else:
            merged.append(chunk)

    return merged


# ============================================================
# メイン: インポート処理
# ============================================================

async def import_docs(
    json_path: str | Path,
    *,
    dry_run: bool = False,
    pages_filter: list[str] | None = None,
) -> dict[str, int]:
    """ゲームシステム文書 JSON をチャンク分割して doc_chunks に格納する。

    Args:
        json_path    : fetch_supercombo_docs.js が出力した JSON ファイルのパス。
        dry_run      : True の場合、DB 書き込みを行わず内容確認のみ。
        pages_filter : 指定がある場合はそのページのみ処理 (例: ['SF6/Gauges'])。

    Returns:
        {'total_chunks': N, 'upserted': N, 'skipped': N, 'errors': N}
    """
    from sf6_engine.db import get_write_client
    from sf6_engine.factory import create_provider

    path = Path(json_path)
    logger.info(f"Loading {path} ...")

    with open(path, encoding='utf-8') as f:
        data = json.load(f)

    pages: dict = data.get('pages', {})
    if pages_filter:
        pages = {k: v for k, v in pages.items() if k in pages_filter}

    # --- チャンク生成 ---
    all_chunks: list[DocChunk] = []
    for page_key, page_data in pages.items():
        html_content = page_data.get('html', '')
        if not html_content or page_data.get('error'):
            logger.warning(f"Skipping {page_key}: no HTML or error")
            continue

        chunks = split_into_chunks(page_key, html_content)
        logger.info(f"  {page_key}: {len(chunks)} chunks")
        for c in chunks:
            logger.info(f"    [{c.heading_h2}] {c.heading_h3 or ''} — {len(c.content)} chars")
        all_chunks.extend(chunks)

    total = len(all_chunks)
    logger.info(f"\nTotal chunks: {total}")

    if dry_run:
        logger.info("Dry run — skipping DB write")
        return {'total_chunks': total, 'upserted': 0, 'skipped': total, 'errors': 0}

    # --- 埋め込み生成 + DB 書き込み ---
    provider = create_provider()
    if not await provider.is_available():
        raise RuntimeError("Ollama が起動していません。`ollama serve` を実行してください。")

    sb = get_write_client()
    upserted = 0
    errors = 0

    for i, chunk in enumerate(all_chunks, 1):
        try:
            # 埋め込みベクトルを生成
            emb_text = f"{chunk.heading_h2}"
            if chunk.heading_h3:
                emb_text += f" / {chunk.heading_h3}"
            emb_text += f"\n\n{chunk.content[:500]}"  # 先頭500文字を使用

            embedding = await provider.embed(emb_text)

            # doc_chunks に upsert
            row = chunk.to_db_row(embedding)
            sb.table('doc_chunks').upsert(row, on_conflict='id').execute()
            upserted += 1
            logger.info(f"  [{i:03d}/{total}] upserted: {chunk.id}")

        except Exception as e:
            logger.error(f"  [{i:03d}/{total}] ERROR {chunk.id}: {e}")
            errors += 1

    logger.info(f"\nDone. upserted={upserted}, errors={errors}")
    return {'total_chunks': total, 'upserted': upserted, 'skipped': 0, 'errors': errors}


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    parser = argparse.ArgumentParser(description='SuperCombo ゲームシステム文書を doc_chunks に格納する')
    parser.add_argument('json_path', help='supercombo_docs_YYYY-MM-DD.json のパス')
    parser.add_argument('--dry-run', action='store_true', help='DB 書き込みをせずチャンク内容を確認')
    parser.add_argument('--pages', nargs='+', default=None,
                        help='処理するページキー (例: SF6/Gauges SF6/Offense)')
    args = parser.parse_args()

    result = asyncio.run(import_docs(
        args.json_path,
        dry_run=args.dry_run,
        pages_filter=args.pages,
    ))
    print(f'\n結果: {result}')
