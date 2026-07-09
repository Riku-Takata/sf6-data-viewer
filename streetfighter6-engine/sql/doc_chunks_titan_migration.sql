-- ============================================================
-- doc_chunks_titan_migration.sql
-- M4 ステップ2: 埋め込みを Bedrock Titan Text Embeddings V2 へ移行
-- Target: Supabase (PostgreSQL 15+ with pgvector)
-- Created: 2026-06-08  (ADR-017)
--
-- 背景:
--   既存の embedding カラムは nomic-embed-text (768次元)。
--   AWS リモート MCP (サーバレス) では Ollama を起動できないため、
--   Bedrock Titan V2 (1024次元) に寄せる。次元が異なるため
--   既存カラムは壊さず embedding_titan を新設し、72チャンクを再埋め込みする。
--
-- 適用手順:
--   1. Supabase Studio → SQL Editor でこのファイルを実行 (STEP 1〜2)
--   2. `PYTHONPATH=src python scripts/reembed_titan.py` で embedding_titan を埋める
--   3. このファイルの STEP 3 (索引) を実行 (データ投入後の方が IVFFlat の精度が良い)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- STEP 1: embedding_titan カラム (1024次元) を追加
-- 既存の embedding (768次元) はそのまま残す (ロールバック容易・並行検証可能)
-- ============================================================
ALTER TABLE doc_chunks
  ADD COLUMN IF NOT EXISTS embedding_titan vector(1024);

-- ============================================================
-- STEP 2: search_docs_titan — Titan 埋め込みでのコサイン類似度検索
-- 既存 search_docs(768) は温存し、別関数として並存させる。
-- ============================================================
CREATE OR REPLACE FUNCTION search_docs_titan(
  query_embedding  vector(1024),
  match_threshold  float  DEFAULT 0.5,
  match_count      int    DEFAULT 5
)
RETURNS TABLE (
  id          TEXT,
  page        TEXT,
  heading_h2  TEXT,
  heading_h3  TEXT,
  content     TEXT,
  keywords    TEXT[],
  similarity  float
)
LANGUAGE plpgsql
SECURITY DEFINER   -- RLS をバイパスして anon key からも呼べる
AS $$
BEGIN
  RETURN QUERY
  SELECT
    dc.id,
    dc.page,
    dc.heading_h2,
    dc.heading_h3,
    dc.content,
    dc.keywords,
    1 - (dc.embedding_titan <=> query_embedding) AS similarity
  FROM doc_chunks dc
  WHERE
    dc.embedding_titan IS NOT NULL
    AND 1 - (dc.embedding_titan <=> query_embedding) > match_threshold
  ORDER BY dc.embedding_titan <=> query_embedding
  LIMIT match_count;
END;
$$;

-- ============================================================
-- STEP 3: ベクトル索引 (★ reembed_titan.py で投入した後に実行すること)
-- IVFFlat は投入済みデータからクラスタ重心を学習するため、
-- 空の状態で作ると精度が落ちる。72件と少量なので未作成でも全件スキャンで動作する。
-- ============================================================
-- CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding_titan
--   ON doc_chunks
--   USING ivfflat (embedding_titan vector_cosine_ops)
--   WITH (lists = 10);

-- ============================================================
-- 動作確認 (再埋め込み後)
-- ============================================================
-- SELECT count(*) FROM doc_chunks WHERE embedding_titan IS NOT NULL;  -- 期待: 72
-- SELECT id, similarity FROM search_docs_titan('[...1024次元...]'::vector, 0.4, 3);
