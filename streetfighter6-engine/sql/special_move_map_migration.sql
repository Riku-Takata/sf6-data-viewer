-- ============================================================
-- special_move_map / move_aliases マイグレーション (ADR-018)
--
-- 目的:
--   1. special_move_map: CAPCOM 日本語必殺技/SA名 ⇔ SC input の対応表。
--      unified_moves が通常技しか結合していなかったギャップを埋め、
--      日本語公式名から直接 SC フレームデータを引けるようにする。
--      シード投入: scripts/load_special_move_map.py (883件 / match_specials.py 生成)
--   2. move_aliases: コミュニティ略称 (アパカ, フリッカー等) の学習テーブル。
--      Discord bot の聞き返しループが MCP ツール register_move_alias 経由で追記。
--
-- 適用: Supabase Studio の SQL Editor で実行 (DDL は手動運用)
-- ロールバック: DROP TABLE special_move_map; DROP TABLE move_aliases;
-- ============================================================

-- 1. CAPCOM ⇔ SC 必殺技マッピング
CREATE TABLE IF NOT EXISTS special_move_map (
  id                bigserial PRIMARY KEY,
  capcom_slug       text NOT NULL,             -- move_normalized.character_slug
  capcom_move_name  text NOT NULL,             -- move_normalized.move_name (完全一致)
  sc_chara          text NOT NULL,             -- sc_move_normalized.chara
  sc_input          text NOT NULL,             -- sc_move_normalized.input (完全一致)
  sc_name           text,                      -- 同一inputに複数行がある場合の判別用
  match_method      text NOT NULL DEFAULT 'manual',  -- auto-sigN / auto-loose / manual
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (capcom_slug, capcom_move_name)
);

CREATE INDEX IF NOT EXISTS idx_special_move_map_slug
  ON special_move_map (capcom_slug);

-- 2. 学習エイリアス (技名略称 → SC技ファミリー)
--    alias は強度prefix (弱/中/強/OD) を剥がした「ファミリー名」で保存し、
--    強度の解決は既存の _pick_variant ロジックに委ねる
CREATE TABLE IF NOT EXISTS move_aliases (
  id              bigserial PRIMARY KEY,
  sc_chara        text NOT NULL,               -- sc_move_normalized.chara
  alias           text NOT NULL,               -- 例: 'フリッカー' (強度なし)
  sc_name_family  text NOT NULL,               -- 例: 'Psycho Flicker' (強度prefixなし)
  sc_input        text,                        -- 代表input (参考情報)
  source          text NOT NULL DEFAULT 'discord',
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sc_chara, alias)
);

CREATE INDEX IF NOT EXISTS idx_move_aliases_chara
  ON move_aliases (sc_chara);

-- 3. RLS: 読み取りは public、書き込みは service_role のみ
--    (move_aliases への INSERT は MCP サーバの register_move_alias 経由。
--     MCP Lambda は SSM から service key を取得する)
ALTER TABLE special_move_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE move_aliases     ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public read" ON special_move_map
  FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY "public read" ON move_aliases
  FOR SELECT TO anon, authenticated USING (true);
-- service_role は RLS をバイパスするため書き込みポリシーは不要
