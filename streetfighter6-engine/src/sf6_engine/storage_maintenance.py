"""Supabase Storage assets that are optional for the SF6 runtime."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sf6_engine.db import get_write_client
from sf6_engine.importers.ultimate_frame_data import BATCH_SIZE, UFD_HITBOX_BUCKET

HTML_ARCHIVE_BUCKET = "sf6-html-archive"


def _list_bucket_objects(bucket_name: str) -> list[dict[str, Any]]:
    """Recursively list a bucket with enough metadata for a purge manifest."""
    bucket = get_write_client().storage.from_(bucket_name)
    objects: list[dict[str, Any]] = []
    pending_paths = [""]
    visited_paths: set[str] = set()
    while pending_paths:
        path = pending_paths.pop(0)
        if path in visited_paths:
            continue
        visited_paths.add(path)
        offset = 0
        while True:
            page = bucket.list(path=path, options={"limit": 1000, "offset": offset}) or []
            if not page:
                break
            for item in page:
                name = str(item.get("name") or "")
                full_path = f"{path}/{name}" if path else name
                if item.get("id"):
                    metadata = item.get("metadata") or {}
                    objects.append({
                        "path": full_path,
                        "size": int(metadata.get("size") or 0),
                        "etag": metadata.get("eTag") or metadata.get("etag"),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                    })
                elif name:
                    pending_paths.append(full_path)
            if len(page) < 1000:
                break
            offset += len(page)
    return objects


def _list_ufd_objects() -> list[dict[str, Any]]:
    return _list_bucket_objects(UFD_HITBOX_BUCKET)


def purge_ufd_gifs(*, manifest_path: Path, execute: bool = False) -> dict[str, Any]:
    """Write a recovery manifest and optionally remove every archived UFD GIF."""
    sb = get_write_client()
    objects = _list_ufd_objects()
    rows = (
        sb.table("ufd_moves")
        .select(
            "character_slug,source_move_key,move_name,hitbox_source_url,"
            "hitbox_storage_path,hitbox_sha256"
        )
        .not_.is_("hitbox_storage_path", "null")
        .limit(5000)
        .execute()
        .data
        or []
    )
    row_by_path = {row["hitbox_storage_path"]: row for row in rows}
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bucket": UFD_HITBOX_BUCKET,
        "object_count": len(objects),
        "total_bytes": sum(item["size"] for item in objects),
        "objects": [
            {**item, "ufd_move": row_by_path.get(item["path"])} for item in objects
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if execute:
        bucket = sb.storage.from_(UFD_HITBOX_BUCKET)
        paths = [item["path"] for item in objects]
        for start in range(0, len(paths), BATCH_SIZE):
            bucket.remove(paths[start:start + BATCH_SIZE])
        sb.table("ufd_moves").update({
            "hitbox_storage_path": None,
            "hitbox_sha256": None,
        }).not_.is_("hitbox_storage_path", "null").execute()

    return {
        "execute": execute,
        "manifest": str(manifest_path),
        "objects": len(objects),
        "bytes": manifest["total_bytes"],
    }


def purge_html_archive(*, manifest_path: Path, execute: bool = False) -> dict[str, Any]:
    """Remove the optional CAPCOM raw-HTML archive and clear its DB URIs.

    The current frame-data Bot reads normalized PostgreSQL rows; it does not
    need these raw HTML copies at runtime.  Clearing ``raw_html_uri`` first
    prevents dangling database links if a later Storage delete partially
    fails.  The next source-scraper run can recreate fresh current snapshots.
    """
    sb = get_write_client()
    objects = _list_bucket_objects(HTML_ARCHIVE_BUCKET)
    reference_response = (
        sb.table("move_snapshots")
        .select("id", count="exact")
        .like("raw_html_uri", f"{HTML_ARCHIVE_BUCKET}/%")
        .limit(1)
        .execute()
    )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bucket": HTML_ARCHIVE_BUCKET,
        "object_count": len(objects),
        "total_bytes": sum(item["size"] for item in objects),
        "move_snapshot_reference_count": reference_response.count or 0,
        "objects": objects,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if execute:
        sb.table("move_snapshots").update({"raw_html_uri": None}).like(
            "raw_html_uri", f"{HTML_ARCHIVE_BUCKET}/%"
        ).execute()
        bucket = sb.storage.from_(HTML_ARCHIVE_BUCKET)
        paths = [item["path"] for item in objects]
        for start in range(0, len(paths), BATCH_SIZE):
            bucket.remove(paths[start:start + BATCH_SIZE])

    return {
        "execute": execute,
        "manifest": str(manifest_path),
        "objects": len(objects),
        "bytes": manifest["total_bytes"],
        "move_snapshot_references": manifest["move_snapshot_reference_count"],
    }


def relink_html_archive_references(*, manifest_path: Path, execute: bool = False) -> dict[str, Any]:
    """Restore NULL snapshot URIs after an HTML archive has been rebuilt.

    ``move_snapshots`` stores the source page at character granularity.  The
    archive is intentionally only a current/previous debugging copy, so a
    restored historical snapshot points to the current page for the same
    character rather than pretending to recreate its deleted historical HTML.
    """
    sb = get_write_client()
    grouped_ids: dict[str, list[int]] = {}
    offset = 0
    while True:
        page = (
            sb.table("move_snapshots")
            .select("id,moves!inner(character_slug)")
            .is_("raw_html_uri", "null")
            .range(offset, offset + 999)
            .execute()
            .data
            or []
        )
        if not page:
            break
        for row in page:
            move = row.get("moves") or {}
            slug = move.get("character_slug")
            if slug:
                grouped_ids.setdefault(str(slug), []).append(int(row["id"]))
        if len(page) < 1000:
            break
        offset += len(page)

    current_paths = {
        item["path"] for item in _list_bucket_objects(HTML_ARCHIVE_BUCKET)
        if item["path"].startswith("current/")
    }
    missing_archives = sorted(
        slug for slug in grouped_ids if f"current/{slug}.html" not in current_paths
    )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bucket": HTML_ARCHIVE_BUCKET,
        "null_raw_html_uri_count": sum(len(ids) for ids in grouped_ids.values()),
        "character_counts": {slug: len(ids) for slug, ids in sorted(grouped_ids.items())},
        "missing_current_archives": missing_archives,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if execute:
        if missing_archives:
            raise RuntimeError(
                "Cannot restore raw_html_uri without current HTML archives: "
                + ", ".join(missing_archives)
            )
        for slug, ids in grouped_ids.items():
            uri = f"{HTML_ARCHIVE_BUCKET}/current/{slug}.html"
            for start in range(0, len(ids), BATCH_SIZE):
                sb.table("move_snapshots").update({"raw_html_uri": uri}).in_(
                    "id", ids[start:start + BATCH_SIZE]
                ).execute()

    return {
        "execute": execute,
        "manifest": str(manifest_path),
        "relinked_references": manifest["null_raw_html_uri_count"] if execute else 0,
        "pending_references": manifest["null_raw_html_uri_count"],
        "missing_current_archives": missing_archives,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Optional SF6 Storage asset maintenance")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--purge-ufd-gifs", action="store_true")
    action.add_argument("--purge-html-archive", action="store_true")
    action.add_argument("--relink-html-archive", action="store_true")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="manifest作成後に選択したStorage保守操作を実行する",
    )
    args = parser.parse_args()
    if args.purge_ufd_gifs:
        result = purge_ufd_gifs(manifest_path=args.manifest, execute=args.execute)
    elif args.purge_html_archive:
        result = purge_html_archive(manifest_path=args.manifest, execute=args.execute)
    else:
        result = relink_html_archive_references(
            manifest_path=args.manifest, execute=args.execute
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
