from __future__ import annotations

import hashlib
import io
import os
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException

MAX_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".docx", ".txt", ".md", ".csv", ".pdf"}

DOC_TYPE_BY_SUFFIX = {
    ".xlsx": "Excel 数据表",
    ".xlsm": "Excel 数据表",
    ".docx": "Word 文档",
    ".pdf": "PDF 文档",
    ".txt": "文本文件",
    ".md": "Markdown 文档",
    ".csv": "CSV 数据表",
}


class DocumentIngestor:
    """把上传文档登记为来源文档，并切分为可回溯的文档段落。"""

    def __init__(self, db: Any, knowledge: Any, excel_importer: Any, upload_dir: str | os.PathLike | None = None):
        self.db = db
        self.knowledge = knowledge
        self.excel_importer = excel_importer
        self.upload_dir = Path(upload_dir) if upload_dir else None

    def ingest(
        self,
        filename: str,
        content: bytes,
        meta: dict[str, Any] | None = None,
        auto_publish: bool = False,
        actor: str = "system",
    ) -> dict[str, Any]:
        meta = meta or {}
        name = os.path.basename(filename or "upload")
        suffix = os.path.splitext(name)[1].lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(422, f"不支持的文件类型：{suffix or '未知'}，支持 " + "、".join(sorted(ALLOWED_SUFFIXES)))
        if not content:
            raise HTTPException(422, "上传文件为空")
        if len(content) > MAX_BYTES:
            raise HTTPException(422, "文件过大，上传上限为 20MB")

        checksum = hashlib.sha256(content).hexdigest()
        self._store_original(checksum, suffix, content)

        if suffix in {".xlsx", ".xlsm"}:
            result = self.excel_importer.import_workbook(
                name,
                content,
                meta.get("knowledgeCategoryId") or None,
                bool(auto_publish),
                actor,
                document_meta=meta,
            )
            result.setdefault("fileFormat", suffix.lstrip(".").upper())
            result["extractedCount"] = len(result.get("extracted") or [])
            return result

        existing = self.db.fetch_one(
            'SELECT "docId" FROM "SourceDoc" WHERE "checksum"=:checksum', {"checksum": checksum}
        )
        if existing:
            raise HTTPException(409, f"相同内容的文档已登记，来源文档为 {existing['docId']}")

        segments, page_count = self._extract(suffix, content)
        if not segments:
            raise HTTPException(422, "未能从文档中提取到正文内容")

        title = str(meta.get("title") or "").strip() or os.path.splitext(name)[0].strip() or "上传文档"
        document = {
            "title": title,
            "docType": str(meta.get("docType") or DOC_TYPE_BY_SUFFIX.get(suffix, "文档")),
            "issuer": meta.get("issuer") or None,
            "publishDate": meta.get("publishDate") or None,
            "fileFormat": suffix.lstrip(".").upper(),
            "checksum": checksum,
            "language": "zh-CN",
            "pageCount": page_count,
            "sourceUrl": meta.get("sourceUrl") or None,
            "crawlSourceId": meta.get("crawlSourceId") or None,
            "knowledgeCategoryId": meta.get("knowledgeCategoryId") or None,
            "summary": str(meta.get("summary") or f"上传抽取：{len(segments)} 个段落"),
            "segments": segments,
        }
        registered = self.knowledge.register_source(document, actor)
        return {
            "docId": registered["docId"],
            "title": title,
            "fileFormat": document["fileFormat"],
            "segmentCount": registered["segments"],
            "pageCount": page_count,
            "imported": 0,
            "autoPublished": 0,
            "extracted": [],
            "extractedCount": 0,
            "warnings": [],
        }

    # ---------- 原件保存 ----------

    def _store_original(self, checksum: str, suffix: str, content: bytes) -> None:
        """按内容哈希保存上传原件，供来源回溯时打开原文档。"""
        if not self.upload_dir:
            return
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        target = self.upload_dir / f"{checksum}{suffix}"
        if not target.exists():
            target.write_bytes(content)

    # ---------- 文本抽取 ----------

    def _extract(self, suffix: str, content: bytes) -> tuple[list[dict[str, Any]], int]:
        if suffix == ".docx":
            return self._extract_docx(content)
        if suffix == ".pdf":
            return self._extract_pdf(content)
        text = self._decode(content)
        return self._split_text(text), 1

    @staticmethod
    def _decode(content: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="ignore")

    @staticmethod
    def _chunk_text(text: str, limit: int = 700) -> list[str]:
        text = re.sub(r"\r\n?", "\n", text).strip()
        if not text:
            return []
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
        chunks: list[str] = []
        buffer = ""
        for paragraph in paragraphs:
            paragraph = re.sub(r"[ \t]+", " ", paragraph)
            if len(paragraph) > limit:
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                for start in range(0, len(paragraph), limit):
                    chunks.append(paragraph[start:start + limit])
                continue
            separator_length = 2 if buffer else 0
            if len(buffer) + len(paragraph) + separator_length > limit:
                if buffer:
                    chunks.append(buffer)
                buffer = paragraph
            else:
                buffer = f"{buffer}\n\n{paragraph}".strip()
        if buffer:
            chunks.append(buffer)
        return chunks

    def _split_text(self, text: str) -> list[dict[str, Any]]:
        chapter = None
        segments: list[dict[str, Any]] = []
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            block = "\n".join(buffer).strip()
            buffer.clear()
            for chunk in self._chunk_text(block):
                segments.append({"segmentText": chunk, "chapterPath": chapter, "segmentType": "正文"})

        for line in re.split(r"\r?\n", text):
            stripped = line.strip()
            if re.match(r"^#{1,6}\s+", stripped):
                flush()
                chapter = re.sub(r"^#{1,6}\s+", "", stripped)
            buffer.append(line)
        flush()
        return segments

    def _extract_docx(self, content: bytes) -> tuple[list[dict[str, Any]], int]:
        try:
            from docx import Document as DocxDocument
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(500, "服务器缺少 python-docx 依赖，无法解析 Word 文档") from exc
        document = DocxDocument(io.BytesIO(content))
        segments: list[dict[str, Any]] = []
        chapter = None
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            text = "\n\n".join(buffer).strip()
            buffer.clear()
            for chunk in self._chunk_text(text):
                segments.append({"segmentText": chunk, "chapterPath": chapter, "segmentType": "正文"})

        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower() if paragraph.style else ""
            if style.startswith("heading") or style.startswith("标题"):
                flush()
                chapter = text
                continue
            buffer.append(text)
            if sum(len(item) for item in buffer) >= 700:
                flush()
        flush()
        return segments, len(document.sections) or 1

    def _extract_pdf(self, content: bytes) -> tuple[list[dict[str, Any]], int]:
        try:
            import pymupdf as fitz
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(500, "服务器缺少 PyMuPDF 依赖，无法解析 PDF 文档") from exc
        segments: list[dict[str, Any]] = []
        with fitz.open(stream=content, filetype="pdf") as document:
            page_count = document.page_count
            for index, page in enumerate(document, 1):
                text = page.get_text("text") or ""
                for chunk in self._chunk_text(text):
                    segments.append({
                        "segmentText": chunk,
                        "pageNo": index,
                        "segmentType": "正文",
                    })
        return segments, page_count
