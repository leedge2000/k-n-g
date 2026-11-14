"""为历史来源文档补抓原始网页并保存，保证断网后仍可离线打开。

用法（在 kg-system 目录下）：
    python scripts/backfill_snapshots.py
    python scripts/backfill_snapshots.py --force   # 覆盖已有快照
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.archive import ResponseArchive
from app.config import Settings
from app.crawler import Crawler
from app.db import Database
from app.storage import LocalSnapshotStore, MinioSnapshotStore


def build_store(settings: Settings):
    if settings.minio_endpoint:
        return MinioSnapshotStore(
            settings.minio_endpoint,
            settings.minio_access_key,
            settings.minio_secret_key,
            settings.minio_bucket,
        )
    return LocalSnapshotStore(settings.data_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="覆盖已存在的快照")
    args = parser.parse_args()

    settings = Settings.from_env()
    db = Database(settings.database_url, settings.seed_dir)
    db.initialize()
    store = build_store(settings)
    crawler = Crawler(db, snapshot_store=store,
                      archive=ResponseArchive(store, settings.data_dir))

    rows = db.fetch_all(
        'SELECT "docId","title","checksum","storageUri","sourceUrl" FROM "SourceDoc" '
        'ORDER BY "docId"'
    )
    saved = skipped = failed = 0
    for row in rows:
        doc_id = row["docId"]
        checksum = row.get("checksum") or ""
        has_snapshot = bool(checksum) and store.get(checksum) is not None
        if has_snapshot and not args.force:
            skipped += 1
            continue
        url = str(row.get("sourceUrl") or "")
        if not url.startswith(("http://", "https://")):
            skipped += 1
            continue
        try:
            status, text = crawler.fetch(url)
            if status != 200 or not text.strip():
                raise ValueError(f"HTTP {status} 或正文为空")
            snapshot_html = crawler._localize_html(text, url)
            if not checksum:
                import hashlib

                checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            storage_uri = store.put(checksum, snapshot_html)
            with db.engine.begin() as conn:
                db.update("SourceDoc", doc_id,
                          {"checksum": checksum, "storageUri": storage_uri},
                          conn, actor="backfill")
            print(f"OK  {doc_id} {url} -> {len(snapshot_html)} 字符")
            saved += 1
        except Exception as exc:
            print(f"FAIL {doc_id} {url}: {type(exc).__name__}: {exc}")
            failed += 1

    print(f"\n完成：新增 {saved}，跳过 {skipped}，失败 {failed}")
    print("失败文档仍可通过数据库正文离线页查看。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
