-- ============================================================
-- Ultimate Frame Data (UFD) integration
--
-- UFD の実測フレーム・補足メモ・当たり判定 GIF のメタデータを保存する。
-- 公式/CAPCOM と SuperCombo の生データは変更しない。値の出所を明示したまま
-- 回答コンテキストへ追加するための、補完用テーブルである。
--
-- Apply: Supabase Studio -> SQL Editor
-- Rollback: DROP TABLE ufd_moves;
-- ============================================================

CREATE TABLE IF NOT EXISTS ufd_moves (
  id                  bigserial PRIMARY KEY,
  character_slug      text NOT NULL,
  source_move_key     text NOT NULL,
  category            text NOT NULL,
  move_name           text NOT NULL,
  sc_input            text,
  input_sequence      text,
  startup             text,
  total               text,
  damage              text,
  attack_type         text,
  cancellable         text,
  notes               text,
  hitbox_note         text,
  on_hit              text,
  on_block            text,
  active              text,
  recovery            text,
  hitbox_source_url   text,
  hitbox_storage_path text,
  hitbox_sha256       text,
  source_url          text NOT NULL,
  source_hash         text NOT NULL,
  scraped_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (character_slug, source_move_key)
);

CREATE INDEX IF NOT EXISTS idx_ufd_moves_character_input
  ON ufd_moves (character_slug, sc_input);
CREATE INDEX IF NOT EXISTS idx_ufd_moves_character_name
  ON ufd_moves (character_slug, move_name);

ALTER TABLE ufd_moves ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "public read" ON ufd_moves;
CREATE POLICY "public read" ON ufd_moves
  FOR SELECT TO anon, authenticated USING (true);

-- Storage bucket `sf6-ufd-hitboxes` is created by the importer with public=false.
-- The bot returns the original public UFD URL, while the archived binary stays private.
