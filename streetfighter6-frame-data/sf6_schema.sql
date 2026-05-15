-- ============================================================
-- SF6 Frame Data DB - Initial Schema (Layer 1)
-- Target: Supabase (PostgreSQL 15+)
-- ============================================================

-- ------------------------------------------------------------
-- 1. characters: キャラクタの静的メタ
-- ------------------------------------------------------------
create table if not exists characters (
  slug              text primary key,             -- URL slug: 'sagat', 'ryu' 等
  display_name_ja   text not null,                -- 'サガット'
  display_name_en   text,                         -- 'Sagat'
  added_at          timestamptz not null default now()
);

-- ------------------------------------------------------------
-- 2. patches: 検知したバランスパッチ一件=一行
-- ------------------------------------------------------------
create table if not exists patches (
  id                    bigserial primary key,
  capcom_updated_date   date not null unique,     -- CAPCOM側の 'Updated 2026.03.27'
  detected_at           timestamptz not null default now(),
  notes_url             text,                     -- パッチノートURL (任意)
  summary               text                      -- 変更件数など (任意)
);

-- ------------------------------------------------------------
-- 3. moves: 技の論理ID (パッチをまたいでも同じ技として扱う)
-- ------------------------------------------------------------
create table if not exists moves (
  id                       bigserial primary key,
  character_slug           text not null references characters(slug) on delete cascade,
  section                  text not null,         -- '通常技','特殊技','必殺技','スーパーアーツ','通常投げ','共通システム'
  move_name                text not null,         -- '立ち弱P（タイガージャブ）'
  first_seen_patch_id      bigint references patches(id),
  retired_at_patch_id      bigint references patches(id),
  unique (character_slug, move_name)
);
create index if not exists idx_moves_character on moves(character_slug);

-- ------------------------------------------------------------
-- 4. move_snapshots: 各パッチ時点のフレーム数値 (本体)
--    値はすべて text: '5-7' や '全体 42' や 'D' (ダウン) 等の非数値表現が混在するため
-- ------------------------------------------------------------
create table if not exists move_snapshots (
  id                  bigserial primary key,
  move_id             bigint not null references moves(id) on delete cascade,
  patch_id            bigint not null references patches(id) on delete cascade,

  -- フレーム数値
  startup             text,
  active              text,
  recovery            text,
  on_hit              text,
  on_block            text,
  cancel              text,
  damage              text,
  combo_scaling       text,
  drive_gain_hit      text,
  drive_lose_guard    text,
  drive_lose_pc       text,
  sa_gain             text,
  attribute           text,
  note                text,

  -- キャラ単位のメタ (このパッチ時点での体力)
  vitality            integer,

  -- 出所 (生HTMLのS3パス等)
  raw_html_uri        text,
  scraped_at          timestamptz not null default now(),

  unique (move_id, patch_id)
);
create index if not exists idx_move_snapshots_patch on move_snapshots(patch_id);
create index if not exists idx_move_snapshots_move  on move_snapshots(move_id);

-- ------------------------------------------------------------
-- 5. scrape_runs: 運用監視用 (いつ何が起きたか)
-- ------------------------------------------------------------
create table if not exists scrape_runs (
  id                  bigserial primary key,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,
  status              text not null,              -- 'no_change' | 'scraping' | 'success' | 'error'
  detected_date       date,                       -- 今回検知したUpdated日付
  patch_id            bigint references patches(id),
  characters_scraped  integer,
  moves_changed       integer,
  error_message       text
);

-- ------------------------------------------------------------
-- 6. 便利ビュー: 各技の最新スナップショット
--    security_invoker=true: 呼び出しユーザーの権限で実行 (RLSを尊重)
-- ------------------------------------------------------------
create or replace view move_latest
with (security_invoker = true) as
select distinct on (m.id)
  m.id                  as move_id,
  m.character_slug,
  m.section,
  m.move_name,
  s.startup, s.active, s.recovery,
  s.on_hit, s.on_block, s.cancel,
  s.damage, s.combo_scaling,
  s.drive_gain_hit, s.drive_lose_guard, s.drive_lose_pc,
  s.sa_gain, s.attribute, s.note, s.vitality,
  p.capcom_updated_date as patch_date,
  s.scraped_at
from moves m
join move_snapshots s on s.move_id = m.id
join patches        p on p.id = s.patch_id
order by m.id, p.capcom_updated_date desc;

-- ------------------------------------------------------------
-- 7. 便利ビュー: 直近2パッチ間の差分 (何が変わったか)
--    security_invoker=true: 呼び出しユーザーの権限で実行 (RLSを尊重)
-- ------------------------------------------------------------
create or replace view move_diff_recent
with (security_invoker = true) as
with ranked as (
  select
    s.*,
    row_number() over (partition by s.move_id order by p.capcom_updated_date desc) as rn
  from move_snapshots s
  join patches p on p.id = s.patch_id
),
latest as (select * from ranked where rn = 1),
previous as (select * from ranked where rn = 2)
select
  m.character_slug,
  m.move_name,
  l.startup  as startup_new,   pr.startup  as startup_old,
  l.active   as active_new,    pr.active   as active_old,
  l.recovery as recovery_new,  pr.recovery as recovery_old,
  l.on_hit   as on_hit_new,    pr.on_hit   as on_hit_old,
  l.on_block as on_block_new,  pr.on_block as on_block_old,
  l.damage   as damage_new,    pr.damage   as damage_old,
  l.note     as note_new,      pr.note     as note_old
from moves m
join latest  l  on l.move_id = m.id
join previous pr on pr.move_id = m.id
where (l.startup,l.active,l.recovery,l.on_hit,l.on_block,l.damage,l.note)
   is distinct from
      (pr.startup,pr.active,pr.recovery,pr.on_hit,pr.on_block,pr.damage,pr.note);
