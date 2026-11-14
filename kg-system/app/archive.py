"""采集响应归档：把每次下载到的原始字节保存到本地或 MinIO，并记录来源信息。

归档的目标是断网后仍能查看已采集的数据，因此保存的是服务端返回的原始内容，
而不是解析后的正文。HTML、JSON、图片、PDF 等统一按内容校验和存放，天然去重。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MEDIA_EXTENSIONS = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/json": ".json",
    "text/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
}

URL_EXTENSIONS = {
    ".html", ".htm", ".json", ".xml", ".txt", ".csv", ".css", ".js",
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp",
    ".doc", ".docx", ".xls", ".xlsx", ".zip",
}


def guess_extension(content_type: str, url: str, content: bytes = b"") -> str:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media in MEDIA_EXTENSIONS:
        return MEDIA_EXTENSIONS[media]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in URL_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    if content[:5] == b"%PDF-":
        return ".pdf"
    if content[:2] == b"\xff\xd8":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ".bin"


def guess_media_type(extension: str) -> str:
    for media, suffix in MEDIA_EXTENSIONS.items():
        if suffix == extension:
            return media
    return "application/octet-stream"


class ResponseArchive:
    """保存采集响应字节，并在 manifest.jsonl 中记录来源信息。"""

    def __init__(self, store: Any, data_dir: Path):
        self.store = store
        self.data_dir = Path(data_dir)
        self.manifest_path = self.data_dir / "archive" / "manifest.jsonl"
        self._known: set[str] = set()
        self._load_manifest()

    def _load_manifest(self) -> None:
        if not self.manifest_path.is_file():
            return
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            checksum = record.get("checksum")
            if checksum:
                self._known.add(str(checksum))

    def save(self, url: str, content: bytes, content_type: str = "",
             doc_id: str | None = None, crawl_source_id: str | None = None) -> dict[str, Any]:
        checksum = hashlib.sha256(content).hexdigest()
        extension = guess_extension(content_type, url, content)
        media_type = (content_type or "").split(";", 1)[0].strip() or guess_media_type(extension)
        if checksum not in self._known:
            self.store.put_bytes(checksum, content, extension, media_type)
            self._append({
                "checksum": checksum,
                "extension": extension,
                "url": url,
                "contentType": media_type,
                "size": len(content),
                "docId": doc_id,
                "crawlSourceId": crawl_source_id,
            })
            self._known.add(checksum)
        return {
            "checksum": checksum,
            "extension": extension,
            "contentType": media_type,
            "name": f"{checksum}{extension}",
        }

    def _append(self, record: dict[str, Any]) -> None:
        from .db import utc_now

        record["fetchedAt"] = utc_now()
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get(self, checksum: str, extension: str) -> bytes | None:
        return self.store.get_asset(checksum, extension)

    @staticmethod
    def split_name(name: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"([0-9a-f]{64})(\.[a-z0-9]{1,8})", name)
        return (match.group(1), match.group(2)) if match else None
