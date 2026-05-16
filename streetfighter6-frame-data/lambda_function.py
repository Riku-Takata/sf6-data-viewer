"""
SF6 Frame Data Scraper - AWS Lambda Handler
==============================================
EventBridgeから日次起動される。

処理フロー:
  1. CAPCOM公式 battle_change ページから最新パッチ日付を取得
  2. patches テーブルと比較し、未知なら全キャラスクレイプを起動
  3. 各キャラ frame ページを取得・パース → moves / move_snapshots に upsert
  4. 生HTMLは Supabase Storage に current/ → previous/ ローテーションで保存
  5. scrape_runs テーブルに実行ログを残す

Env vars (本番Lambda):
  SUPABASE_SECRET_NAME  : Secrets Managerのシークレット名 (default: sf6-frame-scraper/supabase)
  SUPABASE_BUCKET       : 生HTML保管用バケット名 (default: sf6-html-archive)

Env vars (ローカル開発時): Secrets Managerが使えない場合のフォールバック
  SUPABASE_URL          : https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  : service_role key
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Iterable

import requests
from bs4 import BeautifulSoup, Tag
from supabase import Client, create_client
from supabase.client import ClientOptions

# --- 設定 -----------------------------------------------------------------

CAPCOM_BASE = "https://www.streetfighter.com/6/ja-jp/character"
BATTLE_CHANGE_URL = "https://www.streetfighter.com/6/buckler/ja-jp/battle_change"
# CAPCOMサイトはUser-Agentでボットを弾くため、実ブラウザを偽装する.
# 礼儀的スリープと回数制限で負荷を最小化することで誠実な利用を担保する.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
SCRAPE_DELAY_SEC = 3.0
HTTP_TIMEOUT = 30
BUCKET = os.environ.get("SUPABASE_BUCKET", "sf6-html-archive")

# 全キャラのスラッグを明示的に保持する。
# discover_character_slugs() がHTMLから取得できなかった場合のフォールバック用。
# EventBridge以外からの手動実行時に force_slugs 指定がない場合にも使用する。
ALL_KNOWN_SLUGS: list[str] = [
    "aki", "gouki_akuma", "alex", "blanka", "cammy", "chunli", "cviper",
    "deejay", "dhalsim", "ed", "ehonda", "elena", "guile", "ingrid",
    "jamie", "jp", "juri", "ken", "kimberly", "lily", "luke", "mai",
    "manon", "marisa", "vega_mbison", "rashid", "ryu", "sagat", "terry", "zangief",
]

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# --- 列マッピング: CAPCOM公式テーブルのCSSクラスprefix → DB列 ----------------
COLUMN_SPEC: list[tuple[str, str]] = [
    ("move_name",        "frame_skill"),
    ("startup",          "frame_startup_frame"),
    ("active",           "frame_active_frame"),
    ("recovery",         "frame_recovery_frame"),
    ("on_hit",           "frame_hit_frame"),
    ("on_block",         "frame_block_frame"),
    ("cancel",           "frame_cancel"),
    ("damage",           "frame_damage"),
    ("combo_scaling",    "frame_combo_correct"),
    ("drive_gain_hit",   "frame_drive_gauge_gain_hit"),
    ("drive_lose_guard", "frame_drive_gauge_lose_dguard"),
    ("drive_lose_pc",    "frame_drive_gauge_lose_punish"),
    ("sa_gain",          "frame_sa_gauge_gain"),
    ("attribute",        "frame_attribute"),
    ("note",             "frame_note"),
]


@dataclass
class MoveRow:
    section: str
    move_name: str
    fields: dict[str, str]


# --- HTTPフェッチ (リトライ付き) -----------------------------------------

def fetch(url: str, *, session: requests.Session,
          max_retries: int = 3, backoff: float = 2.0) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(url, timeout=HTTP_TIMEOUT, headers=DEFAULT_HEADERS)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last_exc = e
            wait = backoff ** attempt
            logger.warning(f"fetch retry {attempt}/{max_retries} after {wait:.1f}s: {e}")
            time.sleep(wait)
    raise RuntimeError(f"failed to fetch {url}: {last_exc}")


# --- ① 検知: battle_change ページから最新パッチ日付を取り出す ------------

PATCH_DATE_PATTERNS = [
    # 日本語ページ: 'バトル変更リスト 2026.04.15 update' / '2025.12.16'
    re.compile(r'(\d{4})\.(\d{2})\.(\d{2})'),
    # 英語ページ: '04.15.2026 update'
    re.compile(r'(\d{2})\.(\d{2})\.(\d{4})\s*update', re.IGNORECASE),
    # ISO風: '2026/04/15'
    re.compile(r'(\d{4})/(\d{2})/(\d{2})'),
]


def extract_latest_patch_date(html: str) -> date | None:
    """battle_change ページHTMLから最新パッチ日付を取得.

    ページ本文には現行および過去のパッチ日付が複数並んでいるため、
    全ての日付を抽出して最大値（=最新）を返す.
    """
    candidates: list[date] = []
    for pat in PATCH_DATE_PATTERNS:
        for m in pat.finditer(html):
            try:
                # パターンの先頭が \d{4} か \d{2} かで年月日順を判定
                if pat.pattern.startswith(r'(\d{4})\.') or pat.pattern.startswith(r'(\d{4})/'):
                    y, mo, d = (int(m.group(i)) for i in (1, 2, 3))
                else:  # MM.DD.YYYY
                    mo, d, y = (int(m.group(i)) for i in (1, 2, 3))
                # 妥当性チェック (SF6リリース2023年6月以降, 未来は2年以内)
                if 2023 <= y <= datetime.now().year + 2:
                    candidates.append(date(y, mo, d))
            except ValueError:
                continue
    return max(candidates) if candidates else None


# --- ② スクレイプ: キャラ一覧と frame ページのパース ----------------------

def discover_character_slugs(html: str) -> list[str]:
    """任意の frame ページHTMLから全キャラのslugを抽出。

    HTMLナビゲーションから見つかったスラッグに ALL_KNOWN_SLUGS をマージする。
    JavaScriptレンダリングや動的ロードでナビゲーションに現れないキャラを補完する。
    """
    soup = BeautifulSoup(html, "html.parser")
    slugs: list[str] = []
    for a in soup.select('a[href*="/character/"][href$="/frame"]'):
        href = a.get("href", "")
        m = re.search(r"/character/([^/]+)/frame", href)
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))

    # HTML から取得できなかったキャラを既知リストで補完
    for known in ALL_KNOWN_SLUGS:
        if known not in slugs:
            slugs.append(known)
            logger.info(f"discover: added '{known}' from ALL_KNOWN_SLUGS (not found in HTML nav)")

    return slugs


def _cell_text(cell: Tag | None) -> str:
    """ulは行区切り, それ以外は空白で結合, 余白を正規化."""
    if cell is None:
        return ""
    lines: list[str] = []
    for ul in cell.find_all("ul", recursive=False):
        for li in ul.find_all("li", recursive=False):
            t = li.get_text(" ", strip=True)
            if t:
                lines.append(t)
    text_parts: list[str] = []
    for desc in cell.descendants:
        if isinstance(desc, Tag):
            if desc.name in ("ul", "li"):
                continue
            if desc.name == "label":
                text_parts.append(desc.get_text(" ", strip=True))
        elif desc.parent and desc.parent.name not in ("ul", "li", "label"):
            s = str(desc).strip()
            if s:
                text_parts.append(s)
    main = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
    if lines:
        return (main + "\n" + "\n".join(lines)).strip() if main else "\n".join(lines)
    return main


def _find_cell_by_class_prefix(row: Tag, prefix: str) -> Tag | None:
    for td in row.find_all(["td", "th"], recursive=False):
        for c in td.get("class") or []:
            if c.startswith(prefix):
                return td
    return None


def parse_frame_page(html: str) -> tuple[list[MoveRow], int | None]:
    """frame ページHTMLからフレームデータ行と体力を抽出."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("frame table not found")

    vital: int | None = None
    vital_tag = soup.select_one('[class*="frame_attention"] span')
    if vital_tag and vital_tag.text.strip().isdigit():
        vital = int(vital_tag.text.strip())

    tbody = table.find("tbody") or table
    rows = tbody.find_all("tr", recursive=False)

    current_section = "不明"
    out: list[MoveRow] = []
    for tr in rows:
        cls = tr.get("class") or []
        if any("frame_heading" in c for c in cls):
            span = tr.find("span")
            if span:
                current_section = span.get_text(strip=True)
            continue

        if len(tr.find_all(["td", "th"], recursive=False)) < 10:
            continue

        values: dict[str, str] = {}
        for field, prefix in COLUMN_SPEC:
            cell = _find_cell_by_class_prefix(tr, prefix)
            values[field] = _cell_text(cell) if cell else ""

        # 技名は frame_arts の中身を優先
        name_cell = _find_cell_by_class_prefix(tr, "frame_skill")
        if name_cell:
            arts = name_cell.find(class_=lambda c: c and any("frame_arts" in x for x in (c if isinstance(c, list) else [c])))
            if arts:
                values["move_name"] = arts.get_text(" ", strip=True)

        if not values["move_name"]:
            continue

        move_name = values.pop("move_name")
        out.append(MoveRow(section=current_section, move_name=move_name, fields=values))
    return out, vital


# --- ③ Supabase クライアント ----------------------------------------------

@lru_cache(maxsize=1)
def _load_supabase_credentials() -> tuple[str, str]:
    """SupabaseのURLとservice_keyを取得.

    優先順位:
      1. 環境変数 SUPABASE_URL & SUPABASE_SERVICE_KEY (ローカル開発時)
      2. AWS Secrets Manager (本番Lambda時)

    Lambda cold start で1回だけSecrets Managerを叩き、warmな間はキャッシュする.
    """
    env_url = os.environ.get("SUPABASE_URL")
    env_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if env_url and env_key:
        logger.info("using Supabase credentials from environment variables")
        return env_url, env_key

    secret_name = os.environ.get("SUPABASE_SECRET_NAME", "sf6-frame-scraper/supabase")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    logger.info(f"loading Supabase credentials from Secrets Manager: {secret_name}")

    # boto3はLambda環境にプリインストール済み. ローカル開発ではboto3が
    # インストールされていなくても環境変数経由で動かせるよう遅延import.
    import boto3  # noqa: PLC0415

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    secret = json.loads(response["SecretString"])
    return secret["url"], secret["service_key"]


def supabase_client() -> Client:
    url, key = _load_supabase_credentials()
    options = ClientOptions(
        postgrest_client_timeout=30,
        storage_client_timeout=60,
    )
    return create_client(url, key, options=options)


def upsert_patch(sb: Client, patch_date: date) -> int:
    """patches に UPSERT し、id を返す."""
    res = sb.table("patches").upsert(
        {"capcom_updated_date": patch_date.isoformat()},
        on_conflict="capcom_updated_date",
    ).execute()
    return res.data[0]["id"]


def get_known_patch_dates(sb: Client) -> set[date]:
    res = sb.table("patches").select("capcom_updated_date").execute()
    return {date.fromisoformat(r["capcom_updated_date"]) for r in res.data}


def upsert_character(sb: Client, slug: str, display_name_ja: str) -> None:
    sb.table("characters").upsert(
        {"slug": slug, "display_name_ja": display_name_ja},
        on_conflict="slug",
    ).execute()


def bulk_upsert_character_data(
    sb: Client,
    character_slug: str,
    patch_id: int,
    moves: list[MoveRow],
    vitality: int | None,
    raw_html_uri: str,
) -> None:
    """1キャラ分のmoves/snapshotsをまとめてDBに書き込む.

    リクエスト数を 130-195 → 最大3 (existing取得, 新規moves挿入, snapshots upsert) に圧縮.
    """
    if not moves:
        return

    # ① 既存movesを1回で取得
    res = sb.table("moves").select("id, move_name")\
        .eq("character_slug", character_slug).execute()
    name_to_id: dict[str, int] = {r["move_name"]: r["id"] for r in res.data}

    # ② moves を UPSERT (初回は INSERT、再スクレイプ時は DO NOTHING)
    # INSERT ではなく UPSERT を使うことで、部分的な前回スクレイプ残骸があっても安全に実行できる。
    # 同一 move_name が複数パースされる場合があるため、後出し優先で重複排除する。
    seen_names: dict[str, MoveRow] = {}
    for mv in moves:
        seen_names[mv.move_name] = mv  # 後出し優先
    unique_moves = list(seen_names.values())

    all_move_rows = [
        {
            "character_slug": character_slug,
            "section": mv.section,
            "move_name": mv.move_name,
            "first_seen_patch_id": patch_id,
        }
        for mv in unique_moves
    ]
    if all_move_rows:
        ins = sb.table("moves").upsert(
            all_move_rows,
            on_conflict="character_slug,move_name",
        ).execute()
        for r in ins.data:
            name_to_id[r["move_name"]] = r["id"]

    # snapshot rows も重複排除済みの unique_moves を使う
    moves = unique_moves

    # ③ snapshotsをバルクUPSERT
    snapshot_rows = [
        {
            "move_id": name_to_id[mv.move_name],
            "patch_id": patch_id,
            "vitality": vitality,
            "raw_html_uri": raw_html_uri,
            **mv.fields,
        }
        for mv in moves
    ]
    sb.table("move_snapshots").upsert(
        snapshot_rows, on_conflict="move_id,patch_id"
    ).execute()


def insert_scrape_run(sb: Client, status: str, **kwargs) -> int:
    res = sb.table("scrape_runs").insert({"status": status, **kwargs}).execute()
    return res.data[0]["id"]


def update_scrape_run(sb: Client, run_id: int, **kwargs) -> None:
    sb.table("scrape_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }).eq("id", run_id).execute()


# --- ④ Supabase Storage: 生HTMLのローテーション保管 ----------------------

def rotate_and_store_html(sb: Client, slug: str, html: str) -> str:
    """current/{slug}.html → previous/{slug}.html にローテーションし、新しいHTMLを保存.

    戻り値: ストレージ内パス (move_snapshots.raw_html_uri に格納)
    """
    storage = sb.storage.from_(BUCKET)
    current_path = f"current/{slug}.html"
    previous_path = f"previous/{slug}.html"

    # 古い previous は捨てる (失敗は無視: 初回は存在しない)
    try:
        storage.remove([previous_path])
    except Exception:
        pass

    # current → previous にコピーしてから削除
    try:
        existing = storage.download(current_path)
        if existing:
            storage.upload(previous_path, existing,
                           file_options={"content-type": "text/html",
                                         "cache-control": "no-cache"})
            storage.remove([current_path])
    except Exception:
        pass  # current が無いのは初回時。問題なし

    # 新しい current を保存
    storage.upload(
        current_path,
        html.encode("utf-8"),
        file_options={"content-type": "text/html; charset=utf-8",
                      "cache-control": "no-cache"},
    )
    return f"{BUCKET}/{current_path}"


# --- ⑤ オーケストレーション ----------------------------------------------

def scrape_all_characters(sb: Client, patch_id: int) -> tuple[int, list[str]]:
    """全キャラを取得・保存. (キャラ数, エラーキャラlist) を返す."""
    session = requests.Session()
    # 1キャラ取って全slugを発見
    logger.info("seed fetch: ryu (for slug discovery)")
    seed_html = fetch(f"{CAPCOM_BASE}/ryu/frame", session=session)
    slugs = discover_character_slugs(seed_html)
    logger.info(f"discovered {len(slugs)} characters: {slugs}")

    succeeded = 0
    errors: list[str] = []
    seed_processed = False
    overall_start = time.monotonic()

    for i, slug in enumerate(slugs, start=1):
        char_start = time.monotonic()
        try:
            # 1. HTML取得 (ryuはseed_htmlを再利用して通信1回節約)
            if slug == "ryu" and not seed_processed:
                html = seed_html
                seed_processed = True
                logger.info(f"  [{i}/{len(slugs)}] {slug}: reusing seed html")
            else:
                t0 = time.monotonic()
                html = fetch(f"{CAPCOM_BASE}/{slug}/frame", session=session)
                logger.info(f"  [{i}/{len(slugs)}] {slug}: fetched ({time.monotonic()-t0:.1f}s, {len(html)//1024}KB)")

            # 2. character upsert
            upsert_character(sb, slug, display_name_ja=slug)

            # 3. HTML保管 (Storage)
            t0 = time.monotonic()
            raw_uri = rotate_and_store_html(sb, slug, html)
            logger.info(f"      storage rotation done ({time.monotonic()-t0:.1f}s)")

            # 4. パース
            moves, vitality = parse_frame_page(html)
            logger.info(f"      parsed: {len(moves)} moves, HP={vitality}")

            # 5. DB書き込み (バルク)
            t0 = time.monotonic()
            bulk_upsert_character_data(sb, slug, patch_id, moves, vitality, raw_uri)
            logger.info(f"      db upsert done ({time.monotonic()-t0:.1f}s)")

            succeeded += 1
            logger.info(f"  [{i}/{len(slugs)}] {slug}: OK (total {time.monotonic()-char_start:.1f}s)")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"  [{i}/{len(slugs)}] {slug} FAILED: {e}")
            errors.append(slug)

        time.sleep(SCRAPE_DELAY_SEC)

    logger.info(f"all done in {time.monotonic()-overall_start:.1f}s "
                f"(succeeded={succeeded}, failed={len(errors)})")
    return succeeded, errors


def scrape_specific_characters(sb: Client, patch_id: int, slugs: list[str]) -> tuple[int, list[str]]:
    """指定したスラッグのみスクレイプする。

    force_slugs イベントや補完スクレイプ時に使用。
    既存の patch_id に対して upsert するため、既存データは上書きされる。
    """
    session = requests.Session()
    succeeded = 0
    errors: list[str] = []

    for i, slug in enumerate(slugs, start=1):
        char_start = time.monotonic()
        try:
            t0 = time.monotonic()
            html = fetch(f"{CAPCOM_BASE}/{slug}/frame", session=session)
            logger.info(f"  [{i}/{len(slugs)}] {slug}: fetched ({time.monotonic()-t0:.1f}s, {len(html)//1024}KB)")

            upsert_character(sb, slug, display_name_ja=slug)
            raw_uri = rotate_and_store_html(sb, slug, html)
            moves, vitality = parse_frame_page(html)
            logger.info(f"      parsed: {len(moves)} moves, HP={vitality}")
            bulk_upsert_character_data(sb, slug, patch_id, moves, vitality, raw_uri)

            succeeded += 1
            logger.info(f"  [{i}/{len(slugs)}] {slug}: OK ({time.monotonic()-char_start:.1f}s)")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"  [{i}/{len(slugs)}] {slug} FAILED: {e}")
            errors.append(slug)

        if i < len(slugs):
            time.sleep(SCRAPE_DELAY_SEC)

    return succeeded, errors


def lambda_handler(event, context):  # noqa: ARG001
    sb = supabase_client()

    # force_slugs: 指定スラッグを強制スクレイプ (パッチ変更なしでも実行)
    # 使用例: {"force_slugs": ["cammy", "guile", "ingrid", "ken"]}
    force_slugs: list[str] | None = event.get("force_slugs") if isinstance(event, dict) else None
    if force_slugs:
        logger.info(f"force_slugs mode: {force_slugs}")
        # 最新の既知 patch_id を使用
        known_dates = get_known_patch_dates(sb)
        if not known_dates:
            return {"status": "error", "error": "no known patches in DB"}
        latest_date = max(known_dates)
        patch_res = sb.table("patches").select("id").eq(
            "capcom_updated_date", latest_date.isoformat()
        ).limit(1).execute()
        patch_id = patch_res.data[0]["id"]
        logger.info(f"using patch_id={patch_id} (date={latest_date})")

        run_id = insert_scrape_run(sb, "scraping",
                                   detected_date=latest_date.isoformat(),
                                   error_message=f"force_slugs: {force_slugs}")
        try:
            succeeded, errors = scrape_specific_characters(sb, patch_id, force_slugs)
            status = "success"
            update_scrape_run(sb, run_id, status=status, patch_id=patch_id,
                              characters_scraped=succeeded,
                              error_message=("partial errors: " + ",".join(errors)) if errors else None)
            return {
                "status": status,
                "mode": "force_slugs",
                "patch_date": latest_date.isoformat(),
                "characters_scraped": succeeded,
                "errors": errors,
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("force_slugs scrape failed")
            update_scrape_run(sb, run_id, status="error", error_message=str(e))
            return {"status": "error", "run_id": run_id, "error": str(e)}

    # ① 通常フロー: パッチ検知
    session = requests.Session()
    try:
        bc_html = fetch(BATTLE_CHANGE_URL, session=session)
        latest = extract_latest_patch_date(bc_html)
    except Exception as e:  # noqa: BLE001
        logger.exception("detection failed")
        run_id = insert_scrape_run(sb, "error", error_message=f"detect: {e}")
        return {"status": "error", "run_id": run_id}

    if latest is None:
        run_id = insert_scrape_run(sb, "error",
                                   error_message="could not parse patch date from battle_change")
        return {"status": "error", "run_id": run_id}

    logger.info(f"latest patch on CAPCOM: {latest}")

    known = get_known_patch_dates(sb)
    if latest in known:
        run_id = insert_scrape_run(sb, "no_change", detected_date=latest.isoformat())
        update_scrape_run(sb, run_id, status="no_change")
        return {"status": "no_change", "patch_date": latest.isoformat()}

    # ② 新パッチ → 全キャラスクレイプ
    run_id = insert_scrape_run(sb, "scraping", detected_date=latest.isoformat())
    try:
        patch_id = upsert_patch(sb, latest)
        succeeded, errors = scrape_all_characters(sb, patch_id)
        status = "success"
        update_scrape_run(sb, run_id,
                          status=status,
                          patch_id=patch_id,
                          characters_scraped=succeeded,
                          error_message=("partial errors: " + ",".join(errors)) if errors else None)
        return {
            "status": status,
            "patch_date": latest.isoformat(),
            "characters_scraped": succeeded,
            "errors": errors,
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("scrape failed")
        update_scrape_run(sb, run_id, status="error", error_message=str(e))
        return {"status": "error", "run_id": run_id, "error": str(e)}


# --- ローカル実行用 (デバッグ) -------------------------------------------

if __name__ == "__main__":
    # ローカルではlogger.info等が標準出力に出るようにbasicConfig.
    # Lambda環境ではAWS側がハンドラを設定するため、この分岐は影響しない.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    print(json.dumps(lambda_handler({}, None), indent=2, ensure_ascii=False))