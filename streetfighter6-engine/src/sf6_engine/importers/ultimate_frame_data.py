"""Ultimate Frame Data (UFD) のキャラ別ページと当たり判定GIFを取り込む。

UFDは公開ページのHTMLを1キャラずつ、控えめな速度で取得する。公式/CAPCOMや
SuperComboの値を上書きせず、``ufd_moves`` に出所付きの補完データとして保存する。
GIF本体はSupabase Storageのprivate bucketへ、DBには対応技とメタデータだけを保存する。

Usage:
  PYTHONPATH=src python -m sf6_engine.importers.ultimate_frame_data --character ken
  PYTHONPATH=src python -m sf6_engine.importers.ultimate_frame_data --all --delay 1.0
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from sf6_engine.db import create_write_client, get_client, get_write_client

logger = logging.getLogger(__name__)

UFD_BASE_URL = "https://ultimateframedata.com/sf6/"
UFD_HITBOX_BUCKET = "sf6-ufd-hitboxes"
BATCH_SIZE = 100

# UFDのURLはCAPCOM slugとほぼ一致する。ページに存在する全キャラを明示し、
# --all の対象をデータベースの旧いキャラ対応表に縛らない。
UFD_CHARACTER_SLUGS = (
    "aki", "akuma", "alex", "blanka", "cammy", "chunli", "cviper", "deejay",
    "dhalsim", "ed", "ehonda", "elena", "guile", "ingrid", "jamie", "jp", "juri",
    "ken", "kimberly", "lily", "luke", "mbison", "mai", "manon", "marisa", "rashid",
    "ryu", "sagat", "terry", "zangief",
)

# 既存のCAPCOM slugとUFD URLとの差異。
_UFD_TO_CHARACTER_SLUG = {"akuma": "gouki_akuma", "mbison": "vega_mbison"}

_NORMAL_INPUTS = {
    "Standing Light Punch": "5LP", "Standing Medium Punch": "5MP",
    "Standing Heavy Punch": "5HP", "Standing Light Kick": "5LK",
    "Standing Medium Kick": "5MK", "Standing Heavy Kick": "5HK",
    "Crouching Light Punch": "2LP", "Crouching Medium Punch": "2MP",
    "Crouching Heavy Punch": "2HP", "Crouching Light Kick": "2LK",
    "Crouching Medium Kick": "2MK", "Crouching Heavy Kick": "2HK",
    "Jump Light Punch": "j.LP", "Jump Medium Punch": "j.MP",
    "Jump Heavy Punch": "j.HP", "Jump Light Kick": "j.LK",
    "Jump Medium Kick": "j.MK", "Jump Heavy Kick": "j.HK",
    "Neutral Jump Light Punch": "nj.LP", "Neutral Jump Medium Punch": "nj.MP",
    "Neutral Jump Heavy Punch": "nj.HP", "Neutral Jump Light Kick": "nj.LK",
    "Neutral Jump Medium Kick": "nj.MK", "Neutral Jump Heavy Kick": "nj.HK",
}

_INPUT_TOKENS = {
    "Down-Back": "1", "Down": "2", "Down-Forward": "3", "Back": "4",
    "Forward": "6", "Up-Back": "7", "Up": "8", "Up-Forward": "9",
    "Light Punch": "LP", "Medium Punch": "MP", "Heavy Punch": "HP",
    "Light Kick": "LK", "Medium Kick": "MK", "Heavy Kick": "HK",
    "Punch": "P", "Kick": "K", "PP": "PP", "KK": "KK", "P": "P", "K": "K",
}

_TARGET_DIRECTION = {
    "Standing": "5", "Crouching": "2", "Forward": "6", "Back": "4",
    "Down": "2", "Down-Forward": "3", "Down-Back": "1",
}
_TARGET_ATTACK_RE = re.compile(
    r"^(?:(Standing|Crouching|Forward|Back|Down|Down-Forward|Down-Back)"
    r"\s*(?:\+\s*)?)?(Light|Medium|Heavy)\s+(Punch|Kick)$"
)


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.split())
    return None if text in {"", "--"} else text


@dataclass(frozen=True)
class UfdMove:
    category: str
    move_name: str
    startup: str | None = None
    input_sequence: str | None = None
    total: str | None = None
    damage: str | None = None
    attack_type: str | None = None
    cancellable: str | None = None
    notes: str | None = None
    hitbox_note: str | None = None
    on_hit: str | None = None
    on_block: str | None = None
    active: str | None = None
    recovery: str | None = None
    hitbox_url: str | None = None


class _UfdPageParser(HTMLParser):
    """UFDの静的HTMLを、外部HTMLパーサ依存なしでmovecontainer単位に読む。"""

    _FIELDS = {
        "movename": "move_name", "startup": "startup", "inputsequence": "input_sequence",
        "totalframes": "total", "basedamage": "damage", "attacktype": "attack_type",
        "cancellable": "cancellable", "notes": "notes", "whichhitbox": "hitbox_note",
        "onhit": "on_hit", "onblock": "on_block", "activeframes": "active",
        "recovery": "recovery",
    }

    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.category = "Uncategorized"
        self.moves: list[UfdMove] = []
        self._current: dict[str, str] | None = None
        self._field_stack: list[str | None] = []
        self._capture_category = False
        self._category_parts: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "h2" and "movecategory" in classes:
            self._capture_category = True
            self._category_parts = []
            return
        if tag == "div" and "movecontainer" in classes:
            self._current = {}
            self._field_stack = []
            return
        if self._current is None:
            return
        if tag == "a" and "hitboximg" in classes:
            href = dict(attrs).get("data-featherlight") or dict(attrs).get("href")
            if href:
                self._current["hitbox_url"] = urljoin(self.source_url, href)
        if tag == "div":
            field = next((self._FIELDS[c] for c in classes if c in self._FIELDS), None)
            self._field_stack.append(field)

    def handle_data(self, data: str) -> None:
        if self._capture_category:
            self._category_parts.append(data)
        if self._current is not None and self._field_stack and self._field_stack[-1]:
            field = self._field_stack[-1]
            self._current[field] = self._current.get(field, "") + data

    def handle_comment(self, data: str) -> None:
        del data

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag == "h2" and self._capture_category:
            self.category = _clean("".join(self._category_parts)) or "Uncategorized"
            self._capture_category = False
            return
        if self._current is None or tag != "div":
            return
        if self._field_stack:
            self._field_stack.pop()
            return
        move_name = _clean(self._current.get("move_name"))
        if move_name:
            values = {key: _clean(value) for key, value in self._current.items()}
            self.moves.append(UfdMove(category=self.category, move_name=move_name, **{
                key: value for key, value in values.items() if key != "move_name"
            }))
        self._current = None


def parse_ufd_html(html: str, source_url: str) -> list[UfdMove]:
    """UFDキャラページのHTMLを技行リストに変換する。"""
    parser = _UfdPageParser(source_url)
    parser.feed(html)
    parser.close()
    return parser.moves


def _target_combo_input(move_name: str) -> str | None:
    """Convert target-combo notation embedded in a UFD move name."""
    candidates: list[str] = []
    without_label = re.sub(r"\s*\([^()]+\)\s*$", "", move_name).strip()
    if "," in without_label:
        candidates.append(without_label)
    candidates.extend(
        value for value in re.findall(r"\(([^()]*)\)", move_name) if "," in value
    )
    for candidate in candidates:
        pieces = [piece.strip() for piece in candidate.split(",")]
        if len(pieces) < 2:
            continue
        converted: list[str] = []
        for index, piece in enumerate(pieces):
            match = _TARGET_ATTACK_RE.fullmatch(piece)
            if not match:
                converted = []
                break
            direction, strength, button = match.groups()
            prefix = _TARGET_DIRECTION.get(direction or "", "5" if index == 0 else "")
            strength_code = {"Light": "L", "Medium": "M", "Heavy": "H"}[strength]
            converted.append(prefix + strength_code + ("P" if button == "Punch" else "K"))
        if converted:
            return "~".join(converted)
    return None


def _sequence_to_sc_input(sequence: str | None, move_name: str) -> str | None:
    """UFDの英語コマンド表記をSC numpad表記へ変換できる場合だけ返す。"""
    if move_name in _NORMAL_INPUTS:
        return _NORMAL_INPUTS[move_name]
    target_combo = _target_combo_input(move_name)
    if target_combo:
        return target_combo
    # UFD固有技には "Forward + Heavy Punch (Name)" のように入力欄が無い行がある。
    # 強度まで一意な単発の方向通常技だけは安全にSC inputへ結べる。
    m = re.match(
        r"^(Forward|Back|Down) \+ (Light|Medium|Heavy) (Punch|Kick)(?: \([^)]*\))?$",
        move_name,
    )
    if m:
        direction = {"Forward": "6", "Back": "4", "Down": "2"}[m.group(1)]
        strength = {"Light": "L", "Medium": "M", "Heavy": "H"}[m.group(2)]
        button = "P" if m.group(3) == "Punch" else "K"
        return f"{direction}{strength}{button}"
    if not sequence:
        m = re.search(r"\(([A-Z]{1,3})\)$", move_name)
        return m.group(1) if m else None
    hold = bool(re.search(r"\(Hold\)", sequence, re.IGNORECASE))
    text = re.sub(r"\s*\(Hold\)\s*", "", sequence, flags=re.IGNORECASE)
    text = text.replace("(Forward Jump Only)", "").strip()
    # UFD occasionally repeats the OD button as ``KK (KK)``.
    text = re.sub(r"\s*\((PP|KK)\)\s*$", "", text)
    # "Down, Down-Forward, Forward + Light Punch" -> 236LP
    pieces = [part.strip() for part in re.split(r"\s*,\s*|\s*\+\s*", text) if part.strip()]
    if not pieces:
        return None
    converted = [_INPUT_TOKENS.get(piece) for piece in pieces]
    if any(value is None for value in converted):
        return None
    values = [value for value in converted if value]
    if hold and values and re.fullmatch(r"[LMH]?[PK]{1,3}", values[-1]):
        values[-1] = f"[{values[-1]}]"
    return "".join(values)


def _valid_sc_inputs(character_slug: str) -> set[str]:
    """Return SC inputs used to validate conservatively derived OD commands."""
    try:
        mapped = (
            get_client().table("char_slug_map").select("sc_chara")
            .eq("capcom_slug", character_slug).limit(1).execute().data or []
        )
        if not mapped:
            return set()
        rows = (
            get_client().table("sc_moves").select("input")
            .eq("chara", mapped[0]["sc_chara"]).limit(500).execute().data or []
        )
        return {row["input"] for row in rows if row.get("input")}
    except Exception as exc:  # dry-run fixture and migration-safe fallback
        logger.debug("SC input validation unavailable: %s", exc)
        return set()


def _derive_page_sc_inputs(
    moves: list[UfdMove],
    valid_sc_inputs: set[str],
) -> list[str | None]:
    """Resolve direct inputs, then infer OD inputs from same-family siblings."""
    resolved = [_sequence_to_sc_input(move.input_sequence, move.move_name) for move in moves]
    for index, move in enumerate(moves):
        if resolved[index] or not (
            move.move_name.endswith("(Overdrive)")
            or "Overdrive" in (move.input_sequence or "")
        ):
            continue
        family = re.sub(r"\s*\(Overdrive\)\s*$", "", move.move_name).strip()
        candidates: set[str] = set()
        for sibling, sibling_input in zip(moves, resolved):
            if not sibling_input or not sibling.move_name.startswith(family + " ("):
                continue
            match = re.fullmatch(r"(.*?)(?:[LMH])?([PK]+)", sibling_input)
            if not match:
                continue
            candidate = match.group(1) + match.group(2)[0] * 2
            if not valid_sc_inputs or candidate in valid_sc_inputs:
                candidates.add(candidate)
        if len(candidates) == 1:
            resolved[index] = candidates.pop()
    return resolved


def _source_move_key(move: UfdMove, sc_input: str | None) -> str:
    raw = "|".join((move.category, move.move_name, move.input_sequence or "", sc_input or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _source_identity(category: str, move_name: str, input_sequence: str | None) -> str:
    """Stable UFD identity independent of our derived SC mapping."""
    raw = "|".join((category, move_name, input_sequence or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _fetch_url(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "sf6-engine/1.0 (+personal data archive)"})
    with urlopen(request, timeout=30) as response:  # noqa: S310 -- fixed public source URL
        return response.read()


def _ensure_hitbox_bucket() -> None:
    storage = get_write_client().storage
    try:
        storage.get_bucket(UFD_HITBOX_BUCKET)
    except Exception:
        storage.create_bucket(UFD_HITBOX_BUCKET, options={"public": False})


def _store_hitbox(character_slug: str, move_key: str, source_url: str) -> tuple[str, str]:
    """GIFをprivate Storageへ保存し、(path, sha256)を返す。"""
    content = _fetch_url(source_url)
    digest = hashlib.sha256(content).hexdigest()
    path = f"{character_slug}/{move_key}-{digest[:12]}.gif"
    # storage3の同期クライアントはスレッド共有に向かないため、ワーカーごとに作る。
    # 一時的なStorage応答エラーもあるため、短いリトライを行う。
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            create_write_client().storage.from_(UFD_HITBOX_BUCKET).upload(
                path,
                content,
                file_options={"content-type": "image/gif", "upsert": "true"},
            )
            return path, digest
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GIF upload failed: {source_url}") from last_error


def _to_row(
    character_slug: str,
    source_url: str,
    move: UfdMove,
    *,
    sc_input: str | None,
    download_gifs: bool,
    existing_gifs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    move_key = _source_move_key(move, sc_input)
    storage_path = None
    gif_hash = None
    existing = existing_gifs.get(f"key:{move_key}") or existing_gifs.get(
        "identity:" + _source_identity(move.category, move.move_name, move.input_sequence)
    )
    if existing:
        # A corrected derived sc_input must update the existing source row,
        # rather than create a duplicate with a new key.
        move_key = existing["source_move_key"]
    if (
        existing
        and existing.get("hitbox_source_url") == move.hitbox_url
        and existing.get("hitbox_storage_path")
    ):
        storage_path = existing.get("hitbox_storage_path")
        gif_hash = existing.get("hitbox_sha256")
    elif download_gifs and move.hitbox_url:
        try:
            storage_path, gif_hash = _store_hitbox(character_slug, move_key, move.hitbox_url)
        except Exception as exc:  # noqa: BLE001
            # UFD側に古い/欠損GIFリンクがあっても、フレームデータ本文は価値がある。
            # 元URLを残し、次回再取得時にだけGIF保存を再試行する。
            logger.warning("Skipping unavailable hitbox GIF %s: %s", move.hitbox_url, exc)
    payload = asdict(move)
    payload["hitbox_source_url"] = payload.pop("hitbox_url")
    payload.update({
        "character_slug": character_slug,
        "source_move_key": move_key,
        "sc_input": sc_input,
        "hitbox_storage_path": storage_path,
        "hitbox_sha256": gif_hash,
        "source_url": source_url,
        "source_hash": hashlib.sha256(repr(payload).encode("utf-8")).hexdigest(),
    })
    return payload


def _existing_gif_rows(character_slug: str) -> dict[str, dict[str, Any]]:
    """再実行時に同一GIFをStorageへ再アップロードしないための既存情報。"""
    try:
        result = (
            get_client().table("ufd_moves").select(
                "source_move_key,category,move_name,input_sequence,hitbox_source_url,"
                "hitbox_storage_path,hitbox_sha256"
            ).eq("character_slug", character_slug).execute()
        )
        indexed: dict[str, dict[str, Any]] = {}
        for row in result.data or []:
            indexed[f"key:{row['source_move_key']}"] = row
            identity = _source_identity(
                row["category"], row["move_name"], row.get("input_sequence")
            )
            indexed[f"identity:{identity}"] = row
        return indexed
    except Exception as exc:
        logger.debug("existing UFD GIF lookup skipped: %s", exc)
        return {}


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一キャラ・同一UFD技キーは後出しを正として統合する。"""
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        deduped[(row["character_slug"], row["source_move_key"])] = row
    return list(deduped.values())


def _upsert_rows(rows: list[dict[str, Any]]) -> None:
    """UFD行をバッチupsertする。呼び出し元はキャラ単位で使う。"""
    sb = get_write_client()
    for start in range(0, len(rows), BATCH_SIZE):
        sb.table("ufd_moves").upsert(
            rows[start:start + BATCH_SIZE], on_conflict="character_slug,source_move_key"
        ).execute()


def _delete_stale_rows(character_slug: str, retained_keys: set[str]) -> int:
    """Delete rows removed from the current UFD character page."""
    sb = get_write_client()
    existing = (
        sb.table("ufd_moves").select("id,source_move_key")
        .eq("character_slug", character_slug).limit(500).execute().data or []
    )
    stale_ids = [row["id"] for row in existing if row["source_move_key"] not in retained_keys]
    for start in range(0, len(stale_ids), BATCH_SIZE):
        sb.table("ufd_moves").delete().in_(
            "id", stale_ids[start:start + BATCH_SIZE]
        ).execute()
    return len(stale_ids)


def import_ultimate_frame_data(
    *,
    characters: list[str],
    download_gifs: bool = True,
    dry_run: bool = False,
    delay: float = 1.0,
    html_path: str | Path | None = None,
    gif_workers: int = 4,
) -> dict[str, int]:
    """UFDのキャラ別ページを取得し、DBとStorageへ保存する。"""
    if html_path and len(characters) != 1:
        raise ValueError("--html-path はキャラを1人だけ指定した場合に使えます")
    if not dry_run and download_gifs:
        _ensure_hitbox_bucket()

    rows: list[dict[str, Any]] = []
    upserted = 0
    deleted = 0
    for index, ufd_slug in enumerate(characters):
        character_slug = _UFD_TO_CHARACTER_SLUG.get(ufd_slug, ufd_slug)
        source_url = urljoin(UFD_BASE_URL, ufd_slug)
        if html_path:
            html = Path(html_path).read_text(encoding="utf-8")
        else:
            html = _fetch_url(source_url).decode("utf-8", errors="replace")
        moves = parse_ufd_html(html, source_url)
        if not moves:
            raise RuntimeError(f"{source_url} から技データを抽出できませんでした")
        logger.info("%s: %d moves", character_slug, len(moves))
        derived_inputs = _derive_page_sc_inputs(moves, _valid_sc_inputs(character_slug))
        existing_gifs = {} if dry_run else _existing_gif_rows(character_slug)
        should_store_gifs = download_gifs and not dry_run
        # GIFは独立した公開リソースなので、少数並列で保存する。直列だとGIFが多い
        # キャラで1キャラ分の処理が長くなり、失敗時の再開性も悪くなる。
        character_rows: list[dict[str, Any]] = []
        if should_store_gifs and gif_workers > 1:
            with ThreadPoolExecutor(max_workers=gif_workers) as executor:
                futures = [
                    executor.submit(
                        _to_row, character_slug, source_url, move,
                        sc_input=sc_input,
                        download_gifs=True, existing_gifs=existing_gifs,
                    )
                    for move, sc_input in zip(moves, derived_inputs)
                ]
                character_rows.extend(future.result() for future in futures)
        else:
            for move, sc_input in zip(moves, derived_inputs):
                character_rows.append(_to_row(
                    character_slug, source_url, move,
                    sc_input=sc_input,
                    download_gifs=should_store_gifs,
                    existing_gifs=existing_gifs,
                ))
        character_rows = _dedupe_rows(character_rows)
        rows.extend(character_rows)
        if not dry_run:
            _upsert_rows(character_rows)
            upserted += len(character_rows)
            deleted += _delete_stale_rows(
                character_slug,
                {row["source_move_key"] for row in character_rows},
            )
        if index + 1 < len(characters) and not html_path:
            time.sleep(delay)

    if dry_run:
        return {
            "characters": len(characters), "moves": len(rows), "upserted": 0,
            "deleted": 0,
            "mapped_inputs": sum(1 for row in rows if row.get("sc_input")),
            "gifs": 0,
        }

    return {
        "characters": len(characters), "moves": len(rows), "upserted": upserted,
        "deleted": deleted,
        "mapped_inputs": sum(1 for row in rows if row.get("sc_input")),
        "gifs": sum(1 for row in rows if row.get("hitbox_storage_path")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Ultimate Frame Data into Supabase")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--character", choices=UFD_CHARACTER_SLUGS)
    group.add_argument("--all", action="store_true")
    parser.add_argument("--no-gifs", action="store_true", help="GIF本体をStorageへ保存しない")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0, help="キャラページ間の待機秒数")
    parser.add_argument(
        "--gif-workers", type=int, default=4,
        help="GIF保存の最大並列数（デフォルト: 4）",
    )
    parser.add_argument("--html-path", help="ネットワーク不要のHTMLテスト用")
    args = parser.parse_args()
    characters = list(UFD_CHARACTER_SLUGS if args.all else (args.character,))
    result = import_ultimate_frame_data(
        characters=characters,
        download_gifs=not args.no_gifs,
        dry_run=args.dry_run,
        delay=args.delay,
        html_path=args.html_path,
        gif_workers=max(1, args.gif_workers),
    )
    print(result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
