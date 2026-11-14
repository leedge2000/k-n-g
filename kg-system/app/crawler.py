from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import socket
import threading
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException

from .archive import guess_extension, guess_media_type
from .db import Database, utc_now
from .extractors import get_extractor

IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(.*?)\2', re.I | re.S)


class CrawlSecurityError(ValueError):
    pass


def validate_crawl_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CrawlSecurityError("仅允许有效的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise CrawlSecurityError("采集地址不得包含用户凭据")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise CrawlSecurityError("禁止访问本机地址")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise CrawlSecurityError("采集域名无法解析") from exc
    if not addresses:
        raise CrawlSecurityError("采集域名无可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise CrawlSecurityError("禁止访问内网、回环、链路本地或保留地址")
    return url


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self.ignored = 0
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data.strip()
        elif not self.ignored and data.strip():
            self.parts.append(data.strip())


class Crawler:
    def __init__(self, db: Database, knowledge: Any = None, snapshot_store: Any = None,
                 search_index: Any = None, archive: Any = None):
        from .service import KnowledgeService

        self.db = db
        self.knowledge = knowledge or KnowledgeService(db)
        self.snapshot_store = snapshot_store
        self.search_index = search_index
        self.archive = archive
        self._active_source_id: str | None = None
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def trigger(self, source_id: str, actor: str) -> dict[str, Any]:
        with self._locks_guard:
            lock = self._locks.setdefault(source_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "该采集源已有任务正在执行")
        try:
            return self._execute(source_id, actor)
        finally:
            self._active_source_id = None
            lock.release()

    def _execute(self, source_id: str, actor: str) -> dict[str, Any]:
        source = self.db.fetch_one('SELECT * FROM "CrsSource" WHERE "crawlSourceId"=:id', {"id": source_id})
        if not source:
            raise HTTPException(404, "采集源不存在")
        if source["enabledFlag"] != "启用":
            raise HTTPException(409, "采集源处于暂停状态，启用后才可执行")
        self._active_source_id = source_id
        started_at = utc_now()
        started_clock = time.monotonic()
        run_id = self.db.next_id("CrsSource_CrawlRun", "RUN")
        try:
            charset = (source.get("charset") or "").strip().lower()
            if charset and charset not in {"utf-8", "utf8"}:
                status_code, raw = self.fetch_bytes(source["sourceUrl"])
                text = raw.decode(charset, "replace")
            else:
                status_code, text = self.fetch(source["sourceUrl"])
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            extractor = _TextExtractor()
            extractor.feed(text)
            parts = []
            content_length = 0
            for part in extractor.parts:
                remaining = 200000 - content_length
                if remaining <= 0:
                    break
                parts.append(part[:remaining])
                content_length += min(len(part), remaining) + 1
            content = "\n".join(parts).strip()
            if not content:
                raise ValueError("页面未提取到正文")

            doc_id = None
            new_doc_count = 0
            duplicate_count = 0
            run_status = "成功"
            error_message = None
            existing = self.db.fetch_one('SELECT "docId" FROM "SourceDoc" WHERE "checksum"=:checksum', {"checksum": checksum})
            if existing:
                duplicate_count = 1
                doc_id = existing["docId"]
            else:
                snapshot_html = self._localize_html(text, source["sourceUrl"])
                storage_uri = (
                    self.snapshot_store.put(checksum, snapshot_html)
                    if self.snapshot_store else None
                )
                registered = self.knowledge.register_source({
                    "title": extractor.title or source["sourceName"],
                    "docType": source["contentCategory"],
                    "issuer": source["sourceName"],
                    "fileFormat": "HTML",
                    "storageUri": storage_uri,
                    "checksum": checksum,
                    "language": "zh-CN",
                    "sourceUrl": source["sourceUrl"],
                    "crawlSourceId": source_id,
                    "knowledgeCategoryId": source.get("knowledgeCategoryId"),
                    "summary": content[:500],
                    "segments": [{"chapterPath": "自动采集正文", "segmentText": part} for part in parts],
                }, actor)
                doc_id = registered["docId"]
                new_doc_count = 1
                if self.search_index:
                    document = self.db.fetch_one('SELECT * FROM "SourceDoc" WHERE "docId"=:id', {"id": doc_id})
                    segments = self.db.fetch_all(
                        'SELECT * FROM "SourceDoc_Segment" WHERE "docId"=:id ORDER BY "segmentNo"', {"id": doc_id}
                    )
                    try:
                        self.search_index.index_document(document, segments)
                    except Exception as exc:
                        run_status = "部分成功"
                        error_message = f"来源文档已保存，但检索索引写入失败：{exc}"[:1000]

            extracted_count = 0
            structured_tables: list[str] = []
            extractor_type = source.get("extractorType")
            extractor = get_extractor(extractor_type)
            if extractor and doc_id:
                try:
                    extracted = self._run_extractor(extractor, source, text, doc_id, actor)
                    extracted_count = extracted["count"]
                    structured_tables = extracted["tables"]
                    if extracted["note"]:
                        error_message = (error_message + "；" if error_message else "") + extracted["note"]
                except Exception as exc:
                    run_status = "部分成功"
                    error_message = (error_message + "；" if error_message else "") + f"结构化抽取失败：{exc}"[:1000]

            duration_ms = int((time.monotonic() - started_clock) * 1000)
            run = {
                "runId": run_id, "crawlSourceId": source_id, "startedAt": started_at,
                "finishedAt": utc_now(), "runStatus": run_status, "httpStatus": status_code,
                "fetchedCount": 1, "savedCount": new_doc_count, "newDocCount": new_doc_count,
                "duplicateCount": duplicate_count, "durationMs": duration_ms,
                "errorMessage": error_message,
            }
            with self.db.engine.begin() as conn:
                self.db.insert("CrsSource_CrawlRun", run, conn, actor=actor)
                self.db.update("CrsSource", source_id, {"lastRunAt": started_at}, conn, actor=actor)
            return {
                "runId": run_id, "runStatus": run_status, "httpStatus": status_code,
                "fetchedCount": 1, "newDocCount": new_doc_count, "duplicateCount": duplicate_count,
                "docId": doc_id, "extractedCount": extracted_count,
                "structuredTables": structured_tables,
            }
        except Exception as exc:
            with self.db.engine.begin() as conn:
                self.db.insert("CrsSource_CrawlRun", {
                    "runId": run_id, "crawlSourceId": source_id, "startedAt": started_at,
                    "finishedAt": utc_now(), "runStatus": "失败", "fetchedCount": 0,
                    "savedCount": 0, "newDocCount": 0, "duplicateCount": 0,
                    "durationMs": int((time.monotonic() - started_clock) * 1000),
                    "errorMessage": str(exc)[:1000],
                }, conn, actor=actor)
            raise

    def _run_extractor(self, extractor, source: dict[str, Any], html: str,
                        doc_id: str, actor: str) -> dict[str, Any]:
        """执行域抽取器：存详情页 SourceDoc，写结构化行与 ChangeLog。"""
        result = extractor.extract(source, html, doc_id, actor, self.fetch, self.db,
                                   fetch_bytes=self.fetch_bytes)
        # 1) 存详情页 SourceDoc（按顺序，供 Policy/Intel 记录引用）
        sub_doc_ids: list[str] = []
        for payload in result.sub_documents:
            raw_html = payload.pop("_snapshotHtml", None)
            storage_uri = None
            if raw_html and self.snapshot_store:
                snapshot_html = self._localize_html(raw_html, payload.get("sourceUrl", ""))
                checksum = hashlib.sha256(snapshot_html.encode("utf-8")).hexdigest()
                storage_uri = self.snapshot_store.put(checksum, snapshot_html)
                payload["checksum"] = checksum
                payload["storageUri"] = storage_uri
            existing = self.db.fetch_one(
                'SELECT "docId","storageUri" FROM "SourceDoc" WHERE "sourceUrl"=:url',
                {"url": payload.get("sourceUrl", "")},
            )
            if existing:
                if storage_uri and not existing.get("storageUri"):
                    with self.db.engine.begin() as conn:
                        self.db.update(
                            "SourceDoc", existing["docId"],
                            {"checksum": payload["checksum"], "storageUri": storage_uri},
                            conn, actor=actor,
                        )
                sub_doc_ids.append(existing["docId"])
                continue
            checksum = hashlib.sha256((payload.get("summary") or "").encode("utf-8")).hexdigest()
            payload.setdefault("checksum", checksum)
            registered = self.knowledge.register_source(payload, actor)
            sub_doc_ids.append(registered["docId"])

        # 2) 写结构化行
        indicator_code_to_id: dict[str, str] = {}
        sub_doc_index = 0
        inserted_tables: set[str] = set()
        inserted_count = 0
        with self.db.engine.begin() as conn:
            for table, values in result.records:
                values = dict(values)
                # 为详情页来源的记录回填 sourceDocId
                if table in ("Policy", "Intel") and not values.get("sourceDocId"):
                    if sub_doc_index < len(sub_doc_ids):
                        values["sourceDocId"] = sub_doc_ids[sub_doc_index]
                        sub_doc_index += 1
                # 缺少 sourceDocId 时回退到列表页来源文档
                if not values.get("sourceDocId"):
                    values["sourceDocId"] = doc_id
                # segmentId 默认指向来源文档首个段落
                if not values.get("segmentId"):
                    values["segmentId"] = f"{values['sourceDocId']}-S001"
                # 解析指标编码 → indicatorId
                if table == "IvdData":
                    code = values.get("indicatorId")
                    if code and code in indicator_code_to_id:
                        values["indicatorId"] = indicator_code_to_id[code]
                # 幂等：按自然键查重，已存在则跳过
                natural = self._natural_key(table, values)
                if natural:
                    existing_id = self._find_by_natural_key(table, natural, conn)
                    if existing_id:
                        if table == "Indicator":
                            indicator_code_to_id[values.get("indicatorCode", "")] = existing_id
                        continue
                # 分配主键
                pk = self.db.tables[table]["primaryKey"]
                prefix = self._id_prefix(table)
                values[pk] = self.db.next_id(table, prefix, conn)
                self.db.insert(table, values, conn, actor=actor)
                self.db.log_change(conn, table, values[pk], "CREATED", values)
                if table == "Indicator":
                    indicator_code_to_id[values.get("indicatorCode", "")] = values[pk]
                inserted_tables.add(table)
                inserted_count += 1
        return {"count": inserted_count, "tables": sorted(inserted_tables), "note": result.note}

    @staticmethod
    def _id_prefix(table: str) -> str:
        return {
            "Policy": "POL", "Indicator": "IND", "IvdData": "IVD",
            "YrbYearbook": "YRB", "Intel": "INT", "Relation": "REL",
        }.get(table, "REC")

    @staticmethod
    def _natural_key(table: str, values: dict[str, Any]) -> dict[str, Any] | None:
        if table == "Policy":
            return {"policyTitle": values.get("policyTitle")}
        if table == "Indicator":
            return {"indicatorCode": values.get("indicatorCode")}
        if table == "IvdData":
            return {"indicatorId": values.get("indicatorId"), "period": values.get("period")}
        if table == "YrbYearbook":
            return {"yearbookName": values.get("yearbookName"),
                    "statYear": values.get("statYear"),
                    "tableName": values.get("tableName"),
                    "commodityId": values.get("commodityId")}
        if table == "Intel":
            return {"intelTitle": values.get("intelTitle")}
        if table == "Relation":
            return {"subjectEntityId": values.get("subjectEntityId"),
                    "predicate": values.get("predicate"),
                    "objectEntityId": values.get("objectEntityId")}
        return None

    def _find_by_natural_key(self, table: str, key: dict[str, Any], conn) -> str | None:
        clauses = " AND ".join(f'"{k}"=:{k}' for k in key)
        sql = f'SELECT "{self.db.tables[table]["primaryKey"]}" AS pk FROM "{table}" WHERE {clauses}'
        row = self.db.fetch_one(sql, key, conn)
        return row["pk"] if row else None

    def fetch(self, url: str) -> tuple[int, str]:
        status, response = self._get(url)
        return status, response.text

    def fetch_bytes(self, url: str) -> tuple[int, bytes]:
        """下载二进制资源（年鉴统计表为 JPG 图片）。"""
        status, response = self._get(url, timeout=40)
        return status, response.content

    def _get(self, url: str, timeout: float = 20) -> tuple[int, httpx.Response]:
        current = validate_crawl_url(url)
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(6):
                response = client.get(current, headers={"User-Agent": "GrainOilKnowledgeGraph/1.0"})
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("重定向缺少 Location")
                    current = validate_crawl_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                if len(response.content) > 10_000_000:
                    raise ValueError("采集资源超过 10MB 限制")
                self._archive_response(current, response)
                return response.status_code, response
        raise ValueError("重定向次数超过限制")

    def _archive_response(self, url: str, response: httpx.Response) -> None:
        """原样保存本次下载的字节，确保断网后仍可读取。"""
        if self.archive is None:
            return
        try:
            self.archive.save(
                url,
                response.content,
                response.headers.get("content-type", ""),
                crawl_source_id=self._active_source_id,
            )
        except Exception:
            # 归档失败不能阻断采集，离线兜底页仍可基于已入库正文生成
            pass

    def _localize_html(self, html: str, base_url: str,
                       max_images: int = 20, max_bytes: int = 6_000_000) -> str:
        """把图片内嵌为 data URI，使保存下来的网页脱离网络也能显示。"""
        if not html or "<img" not in html.lower() or not base_url:
            return html
        total = 0
        used = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal total, used
            if used >= max_images:
                return match.group(0)
            src = match.group(3).strip()
            if not src or src.lower().startswith(("data:", "#", "javascript:")):
                return match.group(0)
            absolute = urljoin(base_url, src)
            if not absolute.startswith(("http://", "https://")):
                return match.group(0)
            try:
                status, data = self.fetch_bytes(absolute)
            except Exception:
                return match.group(0)
            if status != 200 or not data or len(data) > 2_000_000 or total + len(data) > max_bytes:
                return match.group(0)
            extension = guess_extension("", absolute, data)
            media_type = guess_media_type(extension)
            encoded = base64.b64encode(data).decode("ascii")
            total += len(data)
            used += 1
            return f'{match.group(1)}{match.group(2)}data:{media_type};base64,{encoded}{match.group(2)}'

        return IMG_SRC_RE.sub(replace, html)
