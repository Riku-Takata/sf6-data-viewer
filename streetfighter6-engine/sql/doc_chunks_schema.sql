-- ============================================================
-- doc_chunks_schema.sql
-- M2 Phase A: ゲームシステム文書のベクトル検索基盤
-- Target: Supabase (PostgreSQL 15+ with pgvector)
-- Created: 2026-05-15
--
-- 適用前提:
--   Supabase Dashboard → Database → Extensions で
--   "vector" 拡張が有効になっていること
-- ============================================================

-- pgvector 拡張を有効化 (既に有効なら何もしない)
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 1. doc_chunks: ゲームシステム文書のチャンク
--
-- SuperCombo Wiki の Game Mechanics ページを h2/h3 単位で分割したもの。
-- embedding は nomic-embed-text (768次元) で生成。
-- ============================================================
CREATE TABLE IF NOT EXISTS doc_chunks (
  id          TEXT PRIMARY KEY,          -- "gauges_drive-gauge_burnout" 等
  page        TEXT NOT NULL,             -- "SF6/Gauges"
  heading_h2  TEXT,                      -- "Drive Gauge"
  heading_h3  TEXT,                      -- "Burnout" (h3 がない場合は NULL)
  content     TEXT NOT NULL,             -- Markdown テキスト (HTMLストリップ済み)
  keywords    TEXT[],                    -- {"drive gauge", "burnout", "depleted"}
  embedding   vector(768),               -- nomic-embed-text の出力 (768次元)
  char_count  INT  GENERATED ALWAYS AS (length(content)) STORED,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ベクトル検索用インデックス (IVFFlat, チャンク数が少ないので lists=10)
-- チャンク数が 300 を超えたら lists を増やすこと
CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
  ON doc_chunks
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 10);

-- キーワード検索用インデックス
CREATE INDEX IF NOT EXISTS idx_doc_chunks_page
  ON doc_chunks(page);

-- ============================================================
-- 2. search_docs: コサイン類似度によるベクトル検索関数
--
-- RAG Builder から呼び出す。
-- match_threshold で最小類似度を設定 (0.0〜1.0)。
-- match_count で返す最大件数を設定。
-- ============================================================
CREATE OR REPLACE FUNCTION search_docs(
  query_embedding  vector(768),
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
SECURITY DEFINER   -- RLS をバイパスして anon key からも呼べるようにする
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
    1 - (dc.embedding <=> query_embedding) AS similarity
  FROM doc_chunks dc
  WHERE
    dc.embedding IS NOT NULL
    AND 1 - (dc.embedding <=> query_embedding) > match_threshold
  ORDER BY dc.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;

-- ============================================================
-- RLS ポリシー: public read (anon key で読み取り可能)
-- ============================================================
ALTER TABLE doc_chunks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "public read" ON doc_chunks;
CREATE POLICY "public read" ON doc_chunks
  FOR SELECT USING (true);

-- ============================================================
-- 動作確認クエリ (適用後に実行して確認)
-- ============================================================
-- SELECT * FROM doc_chunks LIMIT 3;
-- SELECT id, similarity FROM search_docs('[0.1, 0.2, ...]'::vector, 0.5, 3);
