-- ============================================================
-- sf6_engine_schema_v2.sql
-- Layer 3 M1: SuperCombo データ統合スキーマ
-- Target: Supabase (PostgreSQL 15+)
-- Created: 2026-05-15
--
-- 適用順:
--   1. char_slug_map          (キャラ対応表)
--   2. sc_moves               (SuperCombo 生データ)
--   3. sc_move_normalized     (正規化ビュー)
--   4. capcom_to_numpad()     (通常技変換関数)
--   5. unified_moves          (CAPCOM + SuperCombo 統合ビュー)
-- ============================================================

-- ============================================================
-- 0. 既存オブジェクトのクリーンアップ (冪等実行のため)
--    sc_moves は空テーブルのため DROP して再作成する
-- ============================================================
DROP VIEW  IF EXISTS unified_moves        CASCADE;
DROP VIEW  IF EXISTS sc_move_normalized   CASCADE;
DROP TABLE IF EXISTS sc_moves             CASCADE;
DROP TABLE IF EXISTS char_slug_map        CASCADE;
DROP FUNCTION IF EXISTS capcom_to_numpad(TEXT);

-- ============================================================
-- 1. char_slug_map: CAPCOM ↔ SuperCombo キャラスラッグ対応表
--
-- capcom_slug: Layer 1 の characters.slug と一致する値。
--              CAPCOM 公式 URL から抽出: /character/{slug}/frame
-- sc_chara:    SuperCombo の Cargo API が返す chara フィールドの値
-- ============================================================
CREATE TABLE IF NOT EXISTS char_slug_map (
  id              SERIAL PRIMARY KEY,
  capcom_slug     TEXT NOT NULL UNIQUE,  -- characters.slug と対応
  sc_chara        TEXT NOT NULL UNIQUE,  -- SuperCombo の chara 値
  display_name_ja TEXT NOT NULL          -- 日本語表示名
);

-- 30キャラ分の初期データ
-- capcom_slug は characters テーブルの実際の slug 値と照合済み (2026-05-15確認)
INSERT INTO char_slug_map (capcom_slug, sc_chara, display_name_ja) VALUES
  ('aki',          'A.K.I.',    'A.K.I.'),
  ('gouki_akuma',  'Akuma',     '豪鬼'),
  ('alex',         'Alex',      'アレックス'),
  ('blanka',       'Blanka',    'ブランカ'),
  ('cammy',        'Cammy',     'キャミィ'),
  ('chunli',       'Chun-Li',   '春麗'),
  ('cviper',       'C.Viper',   'C.ヴァイパー'),
  ('deejay',       'Dee_Jay',   'ディージェイ'),
  ('dhalsim',      'Dhalsim',   'ダルシム'),
  ('ed',           'Ed',        'エド'),
  ('ehonda',       'E.Honda',   'エドモンド本田'),
  ('elena',        'Elena',     'エレナ'),
  ('guile',        'Guile',     'ガイル'),
  ('ingrid',       'Ingrid',    'イングリッド'),
  ('jamie',        'Jamie',     'ジェイミー'),
  ('jp',           'JP',        'JP'),
  ('juri',         'Juri',      'ジュリ'),
  ('ken',          'Ken',       'ケン'),
  ('kimberly',     'Kimberly',  'キンバリー'),
  ('lily',         'Lily',      'リリー'),
  ('luke',         'Luke',      'ルーク'),
  ('mai',          'Mai',       '舞'),
  ('manon',        'Manon',     'マノン'),
  ('marisa',       'Marisa',    'マリーザ'),
  ('vega_mbison',  'M.Bison',   'M.バイソン'),
  ('rashid',       'Rashid',    'ラシード'),
  ('ryu',          'Ryu',       'リュウ'),
  ('sagat',        'Sagat',     'サガット'),
  ('terry',        'Terry',     'テリー'),
  ('zangief',      'Zangief',   'ザンギエフ')
ON CONFLICT (capcom_slug) DO NOTHING;

-- ============================================================
-- 2. sc_moves: SuperCombo Cargo API のフレームデータ (生データ)
--
-- フィールド名はスネークケースに変換。
-- blockAdv / hitAdv は block_adv / hit_adv に統一。
-- HTML / MediaWiki マークアップはインポータ (Python) が除去済み。
-- {{{fieldname}}} プレースホルダは NULL として保存。
-- ============================================================
CREATE TABLE IF NOT EXISTS sc_moves (
  id              BIGSERIAL PRIMARY KEY,
  chara           TEXT NOT NULL,   -- 'Sagat' (SuperCombo の chara 値)
  move_id         TEXT NOT NULL,   -- 'sagat_2hk' (SuperCombo の moveId)
  input           TEXT NOT NULL,   -- '5HP', '236P', '214214K', 'j.HK'
  name            TEXT,            -- 'Tiger Kick', 'Tiger Shot'
  move_type       TEXT,            -- 'ground_normal', 'air_normal', 'Special', 'Super', 'throw', 'drive', 'taunt'
  guard           TEXT,            -- 'LH', 'Mid', 'Low', 'High', 'Throw'
  damage          TEXT,            -- '800', '600,400', '1700~3000'
  startup         TEXT,            -- '8', '10~12'
  active          TEXT,            -- '3', '2(5)3', '53(28)12'
  recovery        TEXT,            -- '22', '73'
  total           TEXT,            -- 総フレーム数 '33'
  cancel          TEXT,            -- '-', 'sa1 sa2 sa3'
  hit_adv         TEXT,            -- ヒット時有利 (元: hitAdv) '-2', 'KD +29', 'HKD ~'
  block_adv       TEXT,            -- ガード時有利 (元: blockAdv) '-6', '+2'
  punish_adv      TEXT,            -- パニカン時有利 '+4', 'KD +30'
  perf_parry_adv  TEXT,            -- パーフェクトパリィ後有利 '+26'
  dr_cancel_hit   TEXT,            -- DRcancelHit
  dr_cancel_blk   TEXT,            -- DRcancelBlk
  after_dr_hit    TEXT,            -- afterDRHit
  after_dr_blk    TEXT,            -- afterDRBlk
  hitstun         TEXT,
  blockstun       TEXT,
  hitstop         TEXT,
  drive_dmg_blk   TEXT,            -- driveDmgBlk: ガード時のドライブゲージ削り量
  drive_dmg_hit   TEXT,            -- driveDmgHit: ヒット時のドライブゲージ削り量
  drive_gain      TEXT,            -- driveGain
  super_gain_hit  TEXT,            -- superGainHit
  super_gain_blk  TEXT,            -- superGainBlk
  invuln          TEXT,            -- '1-14 Full', '1-5 Strike'
  armor           TEXT,            -- 'Break', 'Super'
  airborne        TEXT,
  jug_start       TEXT,            -- jugStart
  jug_increase    TEXT,            -- jugIncrease
  jug_limit       TEXT,            -- jugLimit
  proj_speed      TEXT,            -- projSpeed
  atk_range       TEXT,            -- '1.40', '2.30 (1.87)'
  notes           TEXT,            -- HTMLストリップ済み解説テキスト
  notes_raw       TEXT,            -- 元の生データ (デバッグ用)
  raw_json        JSONB,           -- 元レコードまるごと保存 (将来の再パース用)
  imported_at     TIMESTAMPTZ DEFAULT NOW(),

  UNIQUE(chara, input)             -- 同キャラ同入力は1行に集約
);

CREATE INDEX IF NOT EXISTS idx_sc_moves_chara
  ON sc_moves(chara);
CREATE INDEX IF NOT EXISTS idx_sc_moves_move_type
  ON sc_moves(move_type);

-- ============================================================
-- 3. sc_move_normalized: sc_moves の正規化ビュー
--
-- text 型フィールドから数値を抽出する。
-- HTML / MediaWiki マークアップはインポータが処理済みのため、
-- ここでは正規表現による数値抽出のみ行う。
-- ============================================================
CREATE OR REPLACE VIEW sc_move_normalized
WITH (security_invoker = true) AS
SELECT
  id,
  chara,
  move_id,
  input,
  name,
  move_type,
  guard,
  damage,
  startup,
  active,
  recovery,
  total,
  hit_adv,
  block_adv,
  punish_adv,
  perf_parry_adv,
  atk_range,
  invuln,
  armor,
  notes,

  -- 発生: '8' → 8, '10~12' → 10 (最小値を採用)
  (regexp_match(startup, '(\d+)'))[1]::int        AS startup_f,

  -- 持続: '3' → 3, '2(5)3' → 2 (最初の数値)
  (regexp_match(active, '(\d+)'))[1]::int         AS active_f,

  -- 硬直: '22' → 22
  (regexp_match(recovery, '(\d+)'))[1]::int       AS recovery_f,

  -- 総フレーム: '33' → 33
  (regexp_match(total, '(\d+)'))[1]::int          AS total_f,

  -- ガード有利: '-6' → -6, '+2' → 2, '-19~' → -19
  (regexp_match(block_adv, '([+-]?\d+)'))[1]::int AS block_adv_f,

  -- ヒット有利: '-2' → -2, 'KD +29' → 29, 'HKD ~' → NULL
  (regexp_match(hit_adv, '([+-]?\d+)'))[1]::int   AS hit_adv_f,

  -- パニカン有利: '+4' → 4
  (regexp_match(punish_adv, '([+-]?\d+)'))[1]::int AS punish_adv_f,

  -- パーフェクトパリィ後有利: '+26' → 26
  (regexp_match(perf_parry_adv, '([+-]?\d+)'))[1]::int AS perf_parry_adv_f,

  -- リーチ: '1.40' → 1.40, '2.30 (1.87)' → 2.30 (最初の値)
  (regexp_match(atk_range, '(\d+\.\d+)'))[1]::numeric AS atk_range_n,

  -- ノックダウン判定: 'KD' or 'HKD' を含む場合 true
  (hit_adv ILIKE '%KD%' OR hit_adv ILIKE '%HKD%') AS hit_is_knockdown,

  -- 通常技フラグ (ジャンプ含む)
  (move_type IN ('ground_normal', 'air_normal', 'air_normal8')) AS is_normal,

  -- 生データ (デバッグ用)
  startup   AS startup_raw,
  active    AS active_raw,
  recovery  AS recovery_raw,
  hit_adv   AS hit_adv_raw,
  block_adv AS block_adv_raw,
  damage    AS damage_raw

FROM sc_moves;

-- ============================================================
-- 4. capcom_to_numpad: CAPCOM日本語技名 → SuperCombo numpad 表記
--
-- 通常技 18 パターンのみ対応 (必殺技・SA は M2 で追加)。
-- CAPCOM の move_name には「立ち弱P（タイガージャブ）」のように
-- 技の通称が括弧内に付くため、先頭部分で前方一致する。
-- ============================================================
CREATE OR REPLACE FUNCTION capcom_to_numpad(jp_name TEXT)
RETURNS TEXT AS $$
BEGIN
  IF jp_name IS NULL THEN RETURN NULL; END IF;

  -- 地上通常技 (12 パターン)
  IF jp_name LIKE '立ち弱P%'     THEN RETURN '5LP'; END IF;
  IF jp_name LIKE '立ち弱K%'     THEN RETURN '5LK'; END IF;
  IF jp_name LIKE '立ち中P%'     THEN RETURN '5MP'; END IF;
  IF jp_name LIKE '立ち中K%'     THEN RETURN '5MK'; END IF;
  IF jp_name LIKE '立ち強P%'     THEN RETURN '5HP'; END IF;
  IF jp_name LIKE '立ち強K%'     THEN RETURN '5HK'; END IF;
  IF jp_name LIKE 'しゃがみ弱P%' THEN RETURN '2LP'; END IF;
  IF jp_name LIKE 'しゃがみ弱K%' THEN RETURN '2LK'; END IF;
  IF jp_name LIKE 'しゃがみ中P%' THEN RETURN '2MP'; END IF;
  IF jp_name LIKE 'しゃがみ中K%' THEN RETURN '2MK'; END IF;
  IF jp_name LIKE 'しゃがみ強P%' THEN RETURN '2HP'; END IF;
  IF jp_name LIKE 'しゃがみ強K%' THEN RETURN '2HK'; END IF;

  -- ジャンプ通常技 (6 パターン)
  IF jp_name LIKE 'ジャンプ弱P%'  THEN RETURN 'j.LP'; END IF;
  IF jp_name LIKE 'ジャンプ弱K%'  THEN RETURN 'j.LK'; END IF;
  IF jp_name LIKE 'ジャンプ中P%'  THEN RETURN 'j.MP'; END IF;
  IF jp_name LIKE 'ジャンプ中K%'  THEN RETURN 'j.MK'; END IF;
  IF jp_name LIKE 'ジャンプ強P%'  THEN RETURN 'j.HP'; END IF;
  IF jp_name LIKE 'ジャンプ強K%'  THEN RETURN 'j.HK'; END IF;

  -- 必殺技・SA は M2 まで未対応
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================
-- 5. unified_moves: CAPCOM + SuperCombo 統合ビュー
--
-- CAPCOM の move_normalized を基準に SuperCombo を LEFT JOIN。
-- 通常技のみ SuperCombo データが結合される (capcom_to_numpad が非 NULL の行)。
-- 必殺技・SA は CAPCOM 側のみ (sc_* カラムは NULL)。
--
-- 前提: move_normalized ビューが Layer 1 で作成済みであること。
-- ============================================================
CREATE OR REPLACE VIEW unified_moves
WITH (security_invoker = true) AS
SELECT
  -- === 識別キー ===
  c.character_slug,
  c.move_name,
  c.section,
  capcom_to_numpad(c.move_name)  AS sc_input_key,  -- SC側の検索キー (通常技のみ非NULL)

  -- === CAPCOM フレームデータ ===
  c.startup_int          AS c_startup,
  c.active_first         AS c_active_first,
  c.active_last          AS c_active_last,
  c.recovery_int         AS c_recovery,
  c.on_block_int         AS c_on_block,
  c.on_block_is_range    AS c_on_block_is_range,
  c.on_hit_int           AS c_on_hit,
  c.on_hit_is_knockdown  AS c_on_hit_is_knockdown,
  c.damage_int           AS c_damage,
  c.cancellable_chain,
  c.cancellable_sa1,
  c.cancellable_sa2,
  c.cancellable_sa3,
  c.has_conditional_note,
  c.raw_startup          AS c_raw_startup,
  c.raw_on_block         AS c_raw_on_block,
  c.raw_on_hit           AS c_raw_on_hit,
  c.raw_damage           AS c_raw_damage,
  c.raw_note             AS c_raw_note,
  c.patch_date,

  -- === SuperCombo フレームデータ (通常技のみ結合、それ以外は NULL) ===
  sc.startup_f           AS sc_startup,
  sc.active_f            AS sc_active,
  sc.recovery_f          AS sc_recovery,
  sc.block_adv_f         AS sc_block_adv,
  sc.hit_adv_f           AS sc_hit_adv,
  sc.hit_is_knockdown    AS sc_hit_is_knockdown,
  sc.punish_adv_f        AS sc_punish_adv,
  sc.perf_parry_adv_f    AS sc_perf_parry_adv,
  sc.atk_range_n         AS sc_atk_range,
  sc.invuln              AS sc_invuln,
  sc.damage              AS sc_damage_raw,
  sc.notes               AS sc_notes,

  -- === データ存在フラグ ===
  (sc.id IS NOT NULL)    AS has_sc_data

FROM move_normalized c
LEFT JOIN char_slug_map csm
  ON csm.capcom_slug = c.character_slug
LEFT JOIN sc_move_normalized sc
  ON sc.chara = csm.sc_chara
  AND sc.input = capcom_to_numpad(c.move_name);

-- ============================================================
-- RLS ポリシー: public read (個人利用のため)
-- ============================================================
ALTER TABLE char_slug_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE sc_moves       ENABLE ROW LEVEL SECURITY;

-- 既存ポリシーを一度削除してから再作成 (冪等実行のため)
DROP POLICY IF EXISTS "public read" ON char_slug_map;
DROP POLICY IF EXISTS "public read" ON sc_moves;

CREATE POLICY "public read" ON char_slug_map
  FOR SELECT USING (true);

CREATE POLICY "public read" ON sc_moves
  FOR SELECT USING (true);
