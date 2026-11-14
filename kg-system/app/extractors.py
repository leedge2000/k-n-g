"""域抽取器：把采集到的 HTML 转换为结构化知识行。

每个抽取器对应一个知识域，由 Crawler 根据 CrsSource.extractorType 调度。
抽取失败时抛出异常，由 Crawler 记入 CrsSource_CrawlRun.errorMessage；
单条记录解析失败应被抽取器内部捕获并跳过，避免整批回滚。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

FetchFn = Callable[[str], tuple[int, str]]


@dataclass
class ExtractionResult:
    records: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sub_documents: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


class _LinkParser(HTMLParser):
    """提取页面中的超链接与文本。"""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = "".join(self._text).strip()
            if self._href and text:
                self.links.append((self._href, text))
            self._href = None
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)


class _DetailTextParser(HTMLParser):
    """提取详情页标题与正文文本。"""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._in_title = False
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored:
            self._ignored -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        elif not self._ignored:
            self.parts.append(text)


class BaseExtractor:
    extractor_type: str = ""

    def extract(self, source: dict[str, Any], html: str, doc_id: str,
                actor: str, fetch: FetchFn, db: Any = None,
                fetch_bytes: Callable[[str], tuple[int, bytes]] | None = None) -> ExtractionResult:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 政策法规：国务院政策文件库
# --------------------------------------------------------------------------- #
POLICY_KEYWORDS = ("粮食", "粮油", "大豆", "玉米", "小麦", "稻谷", "食用油",
                   "农业", "收购", "补贴", "种业", "耕地", "农田")


class PolicyExtractor(BaseExtractor):
    extractor_type = "policy_gov"

    def extract(self, source, html, doc_id, actor, fetch, db=None, fetch_bytes=None):
        result = ExtractionResult()
        import json as _json
        # 政策列表由 ZUIXINZHENGCE.json 加载
        base = source["sourceUrl"].rstrip("/")
        api_url = base + "/ZUIXINZHENGCE.json"
        try:
            _, api_text = fetch(api_url)
            items = _json.loads(api_text)
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        # 过滤粮油相关标题
        matched = [
            it for it in items
            if isinstance(it, dict) and any(kw in str(it.get("TITLE", "")) for kw in POLICY_KEYWORDS)
        ][:10]
        for item in matched:
            url = item.get("URL", "")
            title = str(item.get("TITLE", "")).strip()
            if not url or not title:
                continue
            try:
                _, detail_html = fetch(url)
            except Exception:
                continue
            dp = _DetailTextParser()
            try:
                dp.feed(detail_html)
            except Exception:
                continue
            body = "\n".join(dp.parts)
            doc_number = self._extract_doc_number(body)
            issuer = self._extract_issuer(body)
            effective_date = item.get("DOCRELPUBTIME") or self._extract_date(body)
            sub_doc = {
                "title": title, "docType": "政策详情", "issuer": issuer or source["sourceName"],
                "fileFormat": "HTML", "language": "zh-CN", "sourceUrl": url,
                "crawlSourceId": source["crawlSourceId"],
                "knowledgeCategoryId": source.get("knowledgeCategoryId") or "KTC0146",
                "summary": body[:500],
                "segments": [{"chapterPath": "政策正文", "segmentText": p}
                             for p in dp.parts if p.strip()][:50],
                "_snapshotHtml": detail_html,
            }
            result.sub_documents.append(sub_doc)
            policy = {
                "policyId": None,
                "policyTitle": title, "docNumber": doc_number,
                "policyLevel": "国家级" if "gov.cn" in url else "其他",
                "policyStatus": "现行有效",
                "issuer": issuer or source["sourceName"],
                "effectiveDate": effective_date,
                "regionId": "REG0001",
                "knowledgeCategoryId": source.get("knowledgeCategoryId") or "KTC0146",
                "industryStage": "政策",
                "reviewStatus": "已发布",
                "summary": body[:500],
            }
            result.records.append(("Policy", policy))
        result.note = f"政策详情页 {len(result.records)} 条"
        return result

    @staticmethod
    def _extract_doc_number(text: str) -> str:
        patterns = [
            r"[\u4e00-\u9fa5A-Za-z]{2,10}[〔\[（(]\d{4}[〕\]）)]\d+号",
            r"\d{4}年[\u4e00-\u9fa5A-Za-z]{1,8}\d+号",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(0)
        return ""

    @staticmethod
    def _extract_issuer(text: str) -> str:
        m = re.search(r"(国务院|农业农村部|国家发展改革委|财政部|国家粮食和物资储备局|商务部|海关总署|水利部|自然资源部)", text)
        return m.group(1) if m else ""

    @staticmethod
    def _extract_date(text: str) -> str:
        m = re.search(r"(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.search(r"(20\d{2})", text)
        return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# 市场价格指标：农业农村部数据平台 200zs.jsp
# --------------------------------------------------------------------------- #
class MarketIndexExtractor(BaseExtractor):
    extractor_type = "market_index_moa"

    # 指数名称 → (indicatorCode, commodityId, unit)
    INDEX_MAP = {
        "粮油产品批发价格指数": ("MKT-GRAIN-OIL-INDEX", "CMD0005", "指数"),
        "粮食价格指数": ("MKT-GRAIN-INDEX", "CMD0005", "指数"),
        "食用油价格指数": ("MKT-EDIBLE-OIL-INDEX", "CMD0008", "指数"),
    }

    def extract(self, source, html, doc_id, actor, fetch, db=None, fetch_bytes=None):
        result = ExtractionResult()
        # 数据由页面内 AJAX 从 /nyb/200zs 加载，直接取 JSON
        base = source["sourceUrl"]
        api_url = base.rsplit("/pc/", 1)[0] + "/200zs" if "/pc/" in base else base
        try:
            _, api_text = fetch(api_url)
            import json as _json
            data = _json.loads(api_text)
        except Exception:
            # 回退到 HTML 解析
            data = None
        if data and isinstance(data, list) and len(data) >= 3:
            dates, names, values = data[0], data[1], data[2]
        else:
            # HTML 回退解析
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)
            dates, names, values = [], [], []
            for name in self.INDEX_MAP:
                idx = text.find(name)
                if idx < 0:
                    continue
                window = text[idx:idx + 120]
                m = re.search(r"(-?\d+(?:\.\d+)?)", window[len(name):])
                if m:
                    dates.append("")
                    names.append(name)
                    values.append(m.group(1))
        period = ""
        seen_indicators: set[str] = set()
        for i, name in enumerate(names):
            if name not in self.INDEX_MAP:
                continue
            code, commodity_id, unit = self.INDEX_MAP[name]
            try:
                value = float(values[i])
            except (ValueError, IndexError, TypeError):
                continue
            if not period and i < len(dates):
                period = self._normalize_date(dates[i])
            if code not in seen_indicators:
                result.records.append(("Indicator", {
                    "indicatorId": None,
                    "indicatorCode": code, "indicatorName": name,
                    "indicatorCategory": "市场价格", "unit": unit,
                    "frequency": "日", "sourceOrg": "农业农村部",
                    "knowledgeCategoryId": source.get("knowledgeCategoryId") or "KTC0034",
                    "industryStage": "流通",
                    "status": "有效",
                }))
                seen_indicators.add(code)
            result.records.append(("IvdData", {
                "valueId": None,
                "indicatorId": code,
                "regionId": "REG0001", "commodityId": commodity_id,
                "periodType": "日", "period": period,
                "value": value, "unit": unit, "qualityFlag": "官方发布",
                "knowledgeCategoryId": source.get("knowledgeCategoryId") or "KTC0034",
                "industryStage": "流通",
                "status": "已发布",
            }))
        result.note = f"市场指数 {len([r for r in result.records if r[0]=='IvdData'])} 条"
        return result

    @staticmethod
    def _normalize_date(text: str) -> str:
        m = re.search(r"(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return text


# --------------------------------------------------------------------------- #
# 年鉴记录
# --------------------------------------------------------------------------- #
# 年鉴图像表列名 -> (CmdCommodity.commodityId, Indicator.indicatorId)
YEARBOOK_COLUMNS: dict[str, tuple[str, str]] = {
    "粮食": ("CMD0005", "IND0003"),
    "稻谷": ("CMD0001", "IND0002"),
    "小麦": ("CMD0002", "IND0004"),
    "玉米": ("CMD0003", "IND0005"),
    "大豆": ("CMD0004", "IND0006"),
}
YEARBOOK_IMAGE_DIR = Path(__file__).resolve().parents[1] / "data" / "yearbook_imgs"
YEARBOOK_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "yearbook_parsed"


class YearbookExtractor(BaseExtractor):
    extractor_type = "yearbook_stats"

    def extract(self, source, html, doc_id, actor, fetch, db=None, fetch_bytes=None):
        # 国家统计局年鉴统计表以 JPG 图片发布，走图像识别路径
        if "/ndsj/" in source.get("sourceUrl", "") and fetch_bytes is not None:
            return self._extract_image_tables(source, html, doc_id, fetch_bytes)
        return self._extract_html_tables(source, html, db)

    def _extract_image_tables(self, source, html, doc_id, fetch_bytes):
        from . import yearbook as yb

        result = ExtractionResult()
        catalog_url = source["sourceUrl"]
        base = catalog_url.rsplit("/", 1)[0] + "/"
        match = re.search(r"/ndsj/(\d{4})/", catalog_url)
        edition = match.group(1) if match else ""
        catalog = yb.parse_catalog(html)
        if not any(re.search(r"[\u4e00-\u9fff]", entry.title) for entry in catalog):
            # 目录页为 GBK 编码，按 UTF-8 解码会得到乱码表名，改为按字节重读
            _, raw = fetch_bytes(catalog_url)
            catalog = yb.parse_catalog(raw.decode("gb18030", "ignore"))
        YEARBOOK_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        tables: list[str] = []
        notes: list[str] = []
        count = 0
        for entry in catalog:
            spec = yb.TABLE_SPECS.get(entry.table_no)
            if not spec:
                continue
            image_url = urljoin(base, entry.image_url)
            image_path = YEARBOOK_IMAGE_DIR / Path(entry.image_url).name
            try:
                status, data = fetch_bytes(image_url)
                if status != 200 or not data:
                    raise ValueError(f"HTTP {status}")
                image_path.write_bytes(data)
            except Exception as exc:  # 直链被防盗链拦截时改走浏览器截图
                try:
                    yb.capture_image(image_url, image_path)
                except Exception as shot_exc:
                    notes.append(f"{entry.table_no} 图片不可用"
                                 f"（{type(exc).__name__}/{type(shot_exc).__name__}）")
                    continue
                notes.append(f"{entry.table_no} 改由浏览器截图（{type(exc).__name__}）")
            parsed = yb.parse_image(image_path, entry.table_no, YEARBOOK_CACHE_DIR)
            parsed.image_url = image_url
            tables.append(entry.table_no)
            if not parsed.rows:
                notes.append(f"{entry.table_no} 未解析出数据行")
                continue
            for row in parsed.rows:
                for column, (commodity_id, indicator_id) in YEARBOOK_COLUMNS.items():
                    value = row["values"].get(column)
                    if value is None:
                        continue
                    result.records.append(("YrbYearbook", {
                        "recordId": None,
                        "yearbookName": f"中国统计年鉴{edition}" if edition else "中国统计年鉴",
                        "edition": edition,
                        "statYear": row["year"],
                        "tableName": entry.title,
                        "indicatorId": indicator_id,
                        "regionId": "REG0001",
                        "commodityId": commodity_id,
                        "value": value,
                        "unit": parsed.unit,
                        "caliberNote": f"{entry.table_no} 表图像识别（图片 {parsed.image_sha256[:12]}）",
                        "sourceDocId": doc_id,
                        "knowledgeCategoryId": source.get("knowledgeCategoryId") or spec["categoryId"],
                        "industryStage": "生产",
                        "status": "已发布",
                    }))
                    count += 1
            if parsed.rejected:
                notes.append(f"{entry.table_no} {len(parsed.rejected)} 处未识别")
        summary = f"年鉴图像表 {len(tables)} 张，记录 {count} 条"
        if notes:
            summary += "；" + "；".join(notes[:5])
        result.note = summary
        return result

    def _extract_html_tables(self, source, html, db):
        result = ExtractionResult()
        indicator_id = "IND0001"
        if db is not None:
            try:
                row = db.fetch_one('SELECT "indicatorId" FROM "Indicator" LIMIT 1')
                if row:
                    indicator_id = row["indicatorId"]
            except Exception:
                pass
        # 尝试从 HTML 表格中解析年鉴数据
        table_text = re.sub(r"</tr>", "\n", html, flags=re.I)
        table_text = re.sub(r"</t[dh]>", "\t", table_text, flags=re.I)
        table_text = re.sub(r"<[^>]+>", "", table_text)
        rows = [r.strip() for r in table_text.split("\n") if r.strip()]
        count = 0
        for row in rows:
            cells = [c.strip() for c in row.split("\t") if c.strip()]
            if len(cells) < 3:
                continue
            year = None
            value = None
            for cell in cells:
                if re.fullmatch(r"20\d{2}", cell):
                    year = int(cell)
                elif re.fullmatch(r"-?\d+(?:\.\d+)?", cell):
                    value = float(cell)
            if year is None or value is None:
                continue
            result.records.append(("YrbYearbook", {
                "recordId": None,
                "yearbookName": source["sourceName"],
                "statYear": year,
                "tableName": cells[0][:80],
                "indicatorId": indicator_id,
                "regionId": "REG0001",
                "value": value,
                "unit": "",
                "knowledgeCategoryId": source.get("knowledgeCategoryId") or "KTC0201",
                "status": "已发布",
            }))
            count += 1
            if count >= 50:
                break
        result.note = f"年鉴记录 {count} 条"
        return result


# --------------------------------------------------------------------------- #
# 农业情报
# --------------------------------------------------------------------------- #
INTEL_KEYWORDS = POLICY_KEYWORDS + ("市场", "行情", "价格", "供需", "进口", "出口", "产量")


class IntelExtractor(BaseExtractor):
    extractor_type = "intel_agri"

    def extract(self, source, html, doc_id, actor, fetch, db=None, fetch_bytes=None):
        result = ExtractionResult()
        parser = _LinkParser()
        try:
            parser.feed(html)
        except Exception:
            return result
        base = source["sourceUrl"]
        seen: set[str] = set()
        count = 0
        for href, text in parser.links:
            if not text or len(text) < 6:
                continue
            if not any(kw in text for kw in INTEL_KEYWORDS):
                continue
            full = urljoin(base, href)
            if full in seen:
                continue
            seen.add(full)
            try:
                _, detail_html = fetch(full)
            except Exception:
                continue
            dp = _DetailTextParser()
            try:
                dp.feed(detail_html)
            except Exception:
                continue
            title = (dp.title or text).strip()
            if not title:
                continue
            body = "\n".join(dp.parts)
            result.sub_documents.append({
                "title": title, "docType": "情报详情", "issuer": source["sourceName"],
                "fileFormat": "HTML", "language": "zh-CN", "sourceUrl": full,
                "crawlSourceId": source["crawlSourceId"],
                "knowledgeCategoryId": source.get("knowledgeCategoryId") or "KTC0246",
                "summary": body[:500],
                "segments": [{"chapterPath": "情报正文", "segmentText": p}
                             for p in dp.parts if p.strip()][:30],
                "_snapshotHtml": detail_html,
            })
            result.records.append(("Intel", {
                "intelId": None,
                "intelTitle": title, "intelType": "市场分析",
                "regionId": "REG0001",
                "publishOrg": source["sourceName"],
                "publishDate": self._extract_date(body),
                "keyPoints": body[:300],
                "credibility": "中",
                "knowledgeCategoryId": source.get("knowledgeCategoryId") or "KTC0246",
                "industryStage": "流通",
                "status": "已发布",
            }))
            count += 1
            if count >= 10:
                break
        result.note = f"情报 {count} 条"
        return result

    @staticmethod
    def _extract_date(text: str) -> str:
        m = re.search(r"(20\d{2})[年\-/\.](\d{1,2})[月\-/\.](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""


# --------------------------------------------------------------------------- #
# 知识关系派生（不抓取，从已入库实体派生）
# --------------------------------------------------------------------------- #
class RelationDeriver(BaseExtractor):
    extractor_type = "relation_derive"

    def extract(self, source, html, doc_id, actor, fetch, db=None, fetch_bytes=None):
        result = ExtractionResult()
        if db is None:
            result.note = "缺少 db 连接，无法派生关系"
            return result
        pairs: set[tuple[str, str]] = set()
        for table in ("Policy", "IvdData", "YrbYearbook", "Intel"):
            try:
                rows = db.fetch_all(
                    f'SELECT DISTINCT "commodityId","regionId" FROM "{table}" '
                    f'WHERE "commodityId" IS NOT NULL AND "regionId" IS NOT NULL'
                )
            except Exception:
                continue
            for r in rows:
                cid, rid = r.get("commodityId"), r.get("regionId")
                if cid and rid:
                    pairs.add((cid, rid))
        for commodity_id, region_id in pairs:
            result.records.append(("Relation", {
                "relationId": None,
                "subjectEntityType": "品种", "subjectEntityId": commodity_id,
                "predicate": "主产于",
                "objectEntityType": "地区", "objectEntityId": region_id,
                "confidence": 1.0,
                "sourceDocId": doc_id,
                "extractedBy": "relation_derive",
                "status": "有效",
            }))
        result.note = f"派生关系 {len(result.records)} 条"
        return result


EXTRACTORS: dict[str, BaseExtractor] = {
    cls.extractor_type: cls()
    for cls in (PolicyExtractor, MarketIndexExtractor, YearbookExtractor,
                IntelExtractor, RelationDeriver)
    if cls.extractor_type
}


def get_extractor(extractor_type: str | None) -> BaseExtractor | None:
    if not extractor_type:
        return None
    return EXTRACTORS.get(extractor_type.strip())
