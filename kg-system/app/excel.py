from __future__ import annotations

import hashlib
import io
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zipfile import BadZipFile

from fastapi import HTTPException
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException

from .db import Database
from .service import KnowledgeService

MAX_BYTES = 10 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".xlsm"}

SHEET_KEYWORDS: dict[str, list[str]] = {
    "指标数据": ["指标数据", "指标", "统计数据", "数据表", "indicator"],
    "年鉴记录": ["年鉴记录", "年鉴", "yearbook"],
    "农业情报": ["农业情报", "情报", "资讯", "新闻", "intel"],
    "政策法规": ["政策法规", "政策", "法规", "policy"],
}

ALIASES: dict[str, dict[str, list[str]]] = {
    "指标数据": {
        "indicatorCode": ["指标编码", "指标代码", "指标编号", "indicatorcode"],
        "indicatorName": ["指标名称", "指标", "indicatorname"],
        "regionCode": ["地区编码", "地区代码", "regioncode"],
        "regionName": ["地区名称", "地区", "省份", "regionname"],
        "commodityCode": ["品种编码", "品种代码", "commoditycode"],
        "commodityName": ["品种名称", "品种", "commodityname"],
        "periodType": ["期间类型", "周期类型", "periodtype"],
        "period": ["期间", "统计期间", "统计年份", "年份", "period"],
        "value": ["数值", "指标值", "值", "value"],
        "unit": ["单位", "计量单位", "unit"],
        "industryStage": ["产业环节", "环节", "industrystage"],
        "categoryCode": ["分类编码", "知识分类编码", "分类代码", "categorycode"],
        "categoryName": ["分类名称", "知识分类", "categoryname"],
        "remark": ["备注", "说明", "remark"],
    },
    "年鉴记录": {
        "yearbookName": ["年鉴名称", "年鉴", "yearbookname"],
        "edition": ["版次", "版本", "edition"],
        "statYear": ["统计年份", "年份", "statyear"],
        "tableName": ["表名", "表格名称", "表名称", "tablename"],
        "indicatorCode": ["指标编码", "指标代码", "指标编号", "indicatorcode"],
        "indicatorName": ["指标名称", "指标", "indicatorname"],
        "regionCode": ["地区编码", "地区代码", "regioncode"],
        "regionName": ["地区名称", "地区", "省份", "regionname"],
        "commodityCode": ["品种编码", "品种代码", "commoditycode"],
        "commodityName": ["品种名称", "品种", "commodityname"],
        "value": ["数值", "指标值", "值", "value"],
        "unit": ["单位", "计量单位", "unit"],
        "caliberNote": ["口径说明", "口径", "calibernote"],
        "industryStage": ["产业环节", "环节", "industrystage"],
        "categoryCode": ["分类编码", "知识分类编码", "分类代码", "categorycode"],
        "categoryName": ["分类名称", "知识分类", "categoryname"],
    },
    "农业情报": {
        "intelTitle": ["标题", "情报标题", "主题", "inteltitle"],
        "intelType": ["情报类型", "类型", "inteltype"],
        "regionCode": ["地区编码", "地区代码", "regioncode"],
        "regionName": ["地区名称", "地区", "省份", "regionname"],
        "commodityCode": ["品种编码", "品种代码", "commoditycode"],
        "commodityName": ["品种名称", "品种", "commodityname"],
        "publishOrg": ["发布机构", "来源机构", "机构", "publishorg"],
        "publishDate": ["发布日期", "日期", "publishdate"],
        "keyPoints": ["要点", "关键信息", "内容", "keypoints"],
        "credibility": ["可信度", "credibility"],
        "industryStage": ["产业环节", "环节", "industrystage"],
        "categoryCode": ["分类编码", "知识分类编码", "分类代码", "categorycode"],
        "categoryName": ["分类名称", "知识分类", "categoryname"],
        "factorDimensions": ["要素维度", "因子维度", "factordimensions"],
    },
    "政策法规": {
        "policyTitle": ["政策标题", "标题", "政策名称", "policytitle"],
        "docNumber": ["文号", "发文字号", "docnumber"],
        "policyLevel": ["政策层级", "层级", "效力级别", "policylevel"],
        "issuer": ["发布机构", "发文机关", "发布单位", "issuer"],
        "effectiveDate": ["生效日期", "实施日期", "发布日期", "effectivedate"],
        "expireDate": ["失效日期", "截止日期", "expiredate"],
        "policyStatus": ["政策状态", "状态", "policystatus"],
        "regionCode": ["地区编码", "地区代码", "regioncode"],
        "regionName": ["地区名称", "地区", "省份", "regionname"],
        "industryStage": ["产业环节", "环节", "industrystage"],
        "categoryCode": ["分类编码", "知识分类编码", "分类代码", "categorycode"],
        "categoryName": ["分类名称", "知识分类", "categoryname"],
        "factorDimensions": ["要素维度", "因子维度", "factordimensions"],
        "summary": ["摘要", "内容摘要", "summary"],
    },
}

ROOT_CATEGORY_CODES = {
    "指标数据": "AE",
    "年鉴记录": "YB",
    "农业情报": "GI",
    "政策法规": "PL",
}

DEFAULT_REGION_CODE = "CN"
DEFAULT_STAGE = "生产"


@dataclass
class RowRecord:
    sheet: str
    row_no: int
    segment_index: int
    values: dict[str, object] = field(default_factory=dict)
    segment_id: str = ""


@dataclass
class ParsedSheet:
    title: str
    entity_type: str | None
    records: list[RowRecord] = field(default_factory=list)
    segment_count: int = 0


@dataclass
class ParsedWorkbook:
    sheets: list[ParsedSheet]
    segments: list[dict[str, object]]
    warnings: list[str]


def _normalize(text: object) -> str:
    return re.sub(r"[\s_\-—–（）()【】\[\]：:、/·.]+", "", str(text or "")).casefold()


def _alias_table(entity_type: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for field_name, words in ALIASES[entity_type].items():
        for word in words:
            table.setdefault(_normalize(word), field_name)
    return table


def _match_headers(headers: list[str], entity_type: str) -> list[str | None]:
    table = _alias_table(entity_type)
    return [table.get(_normalize(header)) for header in headers]


def _detect_entity_type(sheet_title: str, headers: list[str]) -> str | None:
    lowered = _normalize(sheet_title)
    for entity_type, words in SHEET_KEYWORDS.items():
        if any(_normalize(word) in lowered for word in words):
            return entity_type
    best_type: str | None = None
    best_fields: set[str] = set()
    for entity_type in ALIASES:
        mapped = {name for name in _match_headers(headers, entity_type) if name}
        if len(mapped) > len(best_fields):
            best_type, best_fields = entity_type, mapped
    if len(best_fields) < 2:
        return None
    return best_type


def _cell_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _to_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", "").replace("，", ""))
    return float(match.group(0)) if match else None


def _to_year(value: object) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", _cell_text(value))
    return int(match.group(0)) if match else None


def _to_date_text(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return _cell_text(value)
    text = _cell_text(value)
    match = re.search(r"((?:19|20)\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    year = re.search(r"(?:19|20)\d{2}", text)
    return f"{year.group(0)}-01-01" if year else text


def _period_type(value: object) -> str:
    text = _cell_text(value)
    if "季" in text or text.casefold().startswith("q"):
        return "季度"
    if "月" in text or text.casefold().startswith("m"):
        return "月度"
    return "年度"


def _frequency(value: object) -> str:
    text = _cell_text(value)
    if "季" in text:
        return "季"
    if "月" in text:
        return "月"
    return "年"


def parse_workbook(content: bytes) -> ParsedWorkbook:
    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except (InvalidFileException, BadZipFile, OSError, ValueError, KeyError) as exc:
        raise HTTPException(422, f"无法解析 Excel 文件：{exc}") from exc

    sheets: list[ParsedSheet] = []
    segments: list[dict[str, object]] = []
    warnings: list[str] = []
    try:
        for worksheet in workbook.worksheets:
            rows: list[tuple[int, list[object]]] = []
            for index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                values = ["" if cell is None else cell for cell in row]
                if any(_cell_text(value) for value in values):
                    rows.append((index, values))
            if not rows:
                continue
            header_pos = next(
                (pos for pos, (_, values) in enumerate(rows)
                 if sum(1 for value in values if _cell_text(value)) >= 2),
                0,
            )
            header_no, header_values = rows[header_pos]
            body = rows[header_pos + 1:]
            width = max([len(header_values)] + [len(values) for _, values in body])
            headers = [
                _cell_text(header_values[i]) if i < len(header_values) and _cell_text(header_values[i])
                else f"列{i + 1}"
                for i in range(width)
            ]
            entity_type = _detect_entity_type(worksheet.title, headers)
            mapped = _match_headers(headers, entity_type) if entity_type else [None] * width
            parsed = ParsedSheet(title=worksheet.title, entity_type=entity_type)
            for row_no, values in body:
                values = values + [""] * (width - len(values))
                text = "；".join(
                    f"{headers[i]}：{_cell_text(values[i])}"
                    for i in range(width) if _cell_text(values[i])
                )
                segment = {
                    "chapterPath": f"{worksheet.title} 第{row_no}行",
                    "pageNo": row_no,
                    "segmentText": text[:4000],
                    "segmentType": "表格行",
                }
                segments.append(segment)
                record = RowRecord(sheet=worksheet.title, row_no=row_no, segment_index=len(segments))
                for i in range(width):
                    field_name = mapped[i]
                    if field_name and field_name not in record.values and _cell_text(values[i]):
                        record.values[field_name] = values[i]
                parsed.records.append(record)
            parsed.segment_count = len(parsed.records)
            if entity_type is None:
                warnings.append(f"工作表「{worksheet.title}」未识别知识类型，仅登记为来源段落")
            sheets.append(parsed)
    finally:
        workbook.close()
    return ParsedWorkbook(sheets=sheets, segments=segments, warnings=warnings)


def build_template_workbook() -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F6F4A")
    samples = {
        "指标数据": {
            "指标名称": "稻谷产量", "地区名称": "中国", "品种名称": "稻谷",
            "期间类型": "年度", "期间": "2025", "数值": 21000000, "单位": "吨",
            "产业环节": "生产", "分类编码": "AE", "备注": "示例数据，可删除",
        },
        "年鉴记录": {
            "年鉴名称": "中国统计年鉴", "版次": "2025", "统计年份": 2024,
            "表名": "主要农产品产量", "指标名称": "粮食产量", "地区名称": "中国",
            "品种名称": "粮食", "数值": 70650, "单位": "万吨", "口径说明": "全国口径",
        },
        "农业情报": {
            "标题": "主产区秋粮收购进度加快", "情报类型": "市场动态", "地区名称": "中国",
            "品种名称": "玉米", "发布机构": "国家粮食和物资储备局", "发布日期": "2026-10-20",
            "要点": "东北地区玉米收购进度同比加快，贸易商建库意愿提升。", "可信度": "中",
            "产业环节": "收储",
        },
        "政策法规": {
            "政策标题": "关于做好秋粮收购工作的通知", "文号": "国粮粮〔2026〕1号",
            "政策层级": "国家级", "发布机构": "国家粮食和物资储备局", "生效日期": "2026-09-01",
            "失效日期": "", "政策状态": "现行有效", "地区名称": "中国",
            "产业环节": "收储", "摘要": "部署秋粮收购工作，保障农民售粮顺畅。",
        },
    }
    guide = workbook.create_sheet("使用说明")
    guide.append(["粮油知识图谱 Excel 导入说明"])
    guide.append([])
    guide.append(["1", "仅支持 .xlsx / .xlsm，单个文件不超过 10MB。"])
    guide.append(["2", "每个工作表首行（或首个含两列以上内容的行）为表头，每行生成一条来源段落。"])
    guide.append(["3", "工作表名称或表头命中知识类型时自动抽取知识：指标数据、年鉴记录、农业情报、政策法规。"])
    guide.append(["4", "指标、地区、品种、分类可按编码或名称匹配；未匹配的指标与地区会自动创建主数据。"])
    guide.append(["5", "缺少必填项的行会被跳过，并在导入结果中给出提示。"])
    guide.append(["6", "默认导入后直接发布；取消勾选则以草稿状态保存。"])
    guide.column_dimensions["A"].width = 10
    guide.column_dimensions["B"].width = 100
    guide["A1"].font = Font(bold=True, size=14)
    for sheet_name, row in samples.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.append(list(row.keys()))
        sheet.append(list(row.values()))
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")
        for index, header in enumerate(row.keys(), start=1):
            sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = max(12, len(header) * 2 + 4)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


class ExcelKnowledgeImporter:
    def __init__(self, db: Database, knowledge: KnowledgeService):
        self.db = db
        self.knowledge = knowledge
        self._category_cache: dict[str, dict[str, object]] = {}

    def import_workbook(
        self,
        filename: str,
        content: bytes,
        knowledge_category_id: str | None = None,
        auto_publish: bool = True,
        actor: str = "system",
        document_meta: dict[str, object] | None = None,
    ) -> dict[str, object]:
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(422, "仅支持 .xlsx 或 .xlsm 文件，请先将 .xls 另存为新格式")
        if not content:
            raise HTTPException(422, "上传文件为空")
        if len(content) > MAX_BYTES:
            raise HTTPException(422, "文件过大，Excel 导入上限为 10MB")

        checksum = hashlib.sha256(content).hexdigest()
        existing = self.db.fetch_one(
            'SELECT "docId" FROM "SourceDoc" WHERE "checksum" = :checksum', {"checksum": checksum}
        )
        if existing:
            raise HTTPException(409, f"相同内容的 Excel 已导入，来源文档为 {existing['docId']}")

        category_id = None
        if knowledge_category_id:
            category_id = self.knowledge.require("知识分类", knowledge_category_id)["categoryId"]

        parsed = parse_workbook(content)
        if not parsed.segments:
            raise HTTPException(422, "Excel 中没有可导入的数据行")

        title = os.path.splitext(os.path.basename(filename))[0].strip() or "Excel 导入"
        document = {
            "title": title,
            "docType": "Excel 数据表",
            "fileFormat": suffix.lstrip(".").upper(),
            "checksum": checksum,
            "language": "zh-CN",
            "pageCount": len(parsed.sheets),
            "knowledgeCategoryId": category_id,
            "summary": f"Excel 导入：{len(parsed.sheets)} 个工作表、{len(parsed.segments)} 行数据",
            "segments": parsed.segments,
        }
        if document_meta:
            for key in ("title", "docType", "issuer", "publishDate", "sourceUrl", "crawlSourceId", "summary"):
                value = document_meta.get(key)
                if value not in (None, ""):
                    document[key] = value
        registered = self.knowledge.register_source(document, actor)
        doc_id = str(registered["docId"])

        extracted: list[dict[str, object]] = []
        warnings = list(parsed.warnings)
        for sheet in parsed.sheets:
            if not sheet.entity_type:
                continue
            for record in sheet.records:
                record.segment_id = f"{doc_id}-S{record.segment_index:03d}"
                try:
                    result = self._import_record(
                        sheet.entity_type, record, doc_id, title, category_id, auto_publish, actor
                    )
                except HTTPException as exc:
                    warnings.append(f"{sheet.title} 第{record.row_no}行：{exc.detail}")
                    continue
                except Exception as exc:  # 单行失败不影响其余数据
                    warnings.append(f"{sheet.title} 第{record.row_no}行：{exc}")
                    continue
                if result:
                    extracted.append(result)

        return {
            "docId": doc_id,
            "title": title,
            "sheetCount": len(parsed.sheets),
            "segmentCount": len(parsed.segments),
            "imported": len(extracted),
            "autoPublished": auto_publish,
            "extracted": extracted,
            "unrecognizedSheets": [sheet.title for sheet in parsed.sheets if not sheet.entity_type],
            "warnings": warnings[:100],
        }

    def _import_record(
        self,
        entity_type: str,
        record: RowRecord,
        doc_id: str,
        doc_title: str,
        category_id: str | None,
        auto_publish: bool,
        actor: str,
    ) -> dict[str, object] | None:
        category = self._record_category(record, entity_type, category_id)
        stage = _cell_text(record.values.get("industryStage")) or DEFAULT_STAGE
        if entity_type == "指标数据":
            indicator = self._ensure_indicator(record, category, stage, actor)
            region = self._ensure_region(record, actor)
            commodity = self._find_commodity(record)
            value = _to_number(record.values.get("value"))
            if value is None:
                raise HTTPException(422, "数值为空或无法解析")
            period = _cell_text(record.values.get("period")) or str(datetime.now().year)
            payload = {
                "indicatorId": indicator["indicatorId"],
                "regionId": region["regionId"],
                "commodityId": commodity["commodityId"] if commodity else None,
                "periodType": _period_type(record.values.get("periodType")),
                "period": period,
                "value": value,
                "unit": _cell_text(record.values.get("unit")) or indicator.get("unit") or "",
                "qualityFlag": "待核验",
                "sourceDocId": doc_id,
                "segmentId": record.segment_id,
                "knowledgeCategoryId": category,
                "industryStage": stage,
                "remark": _cell_text(record.values.get("remark")) or "Excel 导入",
            }
            label = f"{indicator['indicatorName']} {period}"
        elif entity_type == "年鉴记录":
            indicator = self._ensure_indicator(record, category, stage, actor)
            region = self._ensure_region(record, actor)
            commodity = self._find_commodity(record)
            value = _to_number(record.values.get("value"))
            if value is None:
                raise HTTPException(422, "数值为空或无法解析")
            stat_year = _to_year(record.values.get("statYear")) or _to_year(record.values.get("period"))
            if stat_year is None:
                raise HTTPException(422, "统计年份缺失或无法解析")
            payload = {
                "yearbookName": _cell_text(record.values.get("yearbookName")) or doc_title,
                "edition": _cell_text(record.values.get("edition")) or str(stat_year),
                "statYear": stat_year,
                "tableName": _cell_text(record.values.get("tableName")) or None,
                "indicatorId": indicator["indicatorId"],
                "regionId": region["regionId"],
                "commodityId": commodity["commodityId"] if commodity else None,
                "value": value,
                "unit": _cell_text(record.values.get("unit")) or indicator.get("unit") or "",
                "caliberNote": _cell_text(record.values.get("caliberNote")) or None,
                "sourceDocId": doc_id,
                "segmentId": record.segment_id,
                "knowledgeCategoryId": category,
                "industryStage": stage,
            }
            label = f"{indicator['indicatorName']} {stat_year}"
        elif entity_type == "农业情报":
            title_value = _cell_text(record.values.get("intelTitle"))
            if not title_value:
                raise HTTPException(422, "缺少情报标题")
            region = self._find_region(record)
            commodity = self._find_commodity(record)
            payload = {
                "intelTitle": title_value,
                "intelType": _cell_text(record.values.get("intelType")) or "行业资讯",
                "regionId": region["regionId"] if region else None,
                "commodityId": commodity["commodityId"] if commodity else None,
                "publishOrg": _cell_text(record.values.get("publishOrg")) or None,
                "publishDate": _to_date_text(record.values.get("publishDate")) or None,
                "keyPoints": _cell_text(record.values.get("keyPoints")) or None,
                "credibility": _cell_text(record.values.get("credibility")) or "中",
                "industryStage": stage,
                "factorDimensions": _cell_text(record.values.get("factorDimensions")) or None,
                "sourceDocId": doc_id,
                "segmentId": record.segment_id,
                "knowledgeCategoryId": category,
            }
            label = title_value
        else:
            title_value = _cell_text(record.values.get("policyTitle"))
            if not title_value:
                raise HTTPException(422, "缺少政策标题")
            region = self._ensure_region(record, actor)
            payload = {
                "policyTitle": title_value,
                "docNumber": _cell_text(record.values.get("docNumber")) or None,
                "policyLevel": _cell_text(record.values.get("policyLevel")) or "其他",
                "issuer": _cell_text(record.values.get("issuer")) or "导入文件",
                "effectiveDate": _to_date_text(record.values.get("effectiveDate")) or None,
                "expireDate": _to_date_text(record.values.get("expireDate")) or None,
                "policyStatus": _cell_text(record.values.get("policyStatus")) or "现行有效",
                "regionId": region["regionId"],
                "industryStage": stage,
                "factorDimensions": _cell_text(record.values.get("factorDimensions")) or None,
                "summary": _cell_text(record.values.get("summary")) or None,
                "sourceDocId": doc_id,
                "segmentId": record.segment_id,
                "knowledgeCategoryId": category,
            }
            label = title_value

        created = self.knowledge.create_knowledge(entity_type, payload, actor)
        status = str(created["status"])
        if auto_publish:
            self.knowledge.submit(entity_type, str(created["entityId"]), actor)
            self.knowledge.review(entity_type, str(created["entityId"]), "通过", actor)
            status = "已发布"
        return {
            "entityType": entity_type,
            "entityId": created["entityId"],
            "label": label,
            "sheet": record.sheet,
            "rowNo": record.row_no,
            "status": status,
        }

    def _record_category(self, record: RowRecord, entity_type: str, fallback_id: str | None) -> str:
        for key in ("categoryCode", "categoryName"):
            value = _cell_text(record.values.get(key))
            if not value:
                continue
            row = self._category_by(value)
            if row:
                return str(row["categoryId"])
        if fallback_id:
            return fallback_id
        return self._root_category(entity_type)

    def _category_by(self, value: str) -> dict[str, object] | None:
        if value in self._category_cache:
            return self._category_cache[value]
        row = self.db.fetch_one(
            'SELECT * FROM "KtcCategory" WHERE "categoryId" = :value OR "categoryCode" = :value OR "categoryName" = :value',
            {"value": value},
        )
        if row:
            self._category_cache[value] = row
        return row

    def _root_category(self, entity_type: str) -> str:
        code = ROOT_CATEGORY_CODES.get(entity_type, "AE")
        row = self._category_by(code)
        if not row:
            row = self.db.fetch_one('SELECT * FROM "KtcCategory" ORDER BY "level", "sortNo" LIMIT 1')
        if not row:
            raise HTTPException(422, "知识分类未初始化，无法导入")
        return str(row["categoryId"])

    def _find_region(self, record: RowRecord) -> dict[str, object] | None:
        code = _cell_text(record.values.get("regionCode"))
        name = _cell_text(record.values.get("regionName"))
        if code:
            row = self.db.fetch_one('SELECT * FROM "Region" WHERE "regionCode" = :value', {"value": code})
            if row:
                return row
        if name:
            return self.db.fetch_one('SELECT * FROM "Region" WHERE "regionName" = :value', {"value": name})
        return None

    def _ensure_region(self, record: RowRecord, actor: str) -> dict[str, object]:
        row = self._find_region(record)
        if row:
            return row
        code = _cell_text(record.values.get("regionCode"))
        name = _cell_text(record.values.get("regionName"))
        if not code and not name:
            row = self.db.fetch_one('SELECT * FROM "Region" WHERE "regionCode" = :value',
                                    {"value": DEFAULT_REGION_CODE})
            if row:
                return row
            name = "中国"
            code = DEFAULT_REGION_CODE
        code = code or f"IMP-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8].upper()}"
        existing = self.db.fetch_one('SELECT * FROM "Region" WHERE "regionCode" = :value', {"value": code})
        if existing:
            return existing
        created = self.knowledge.create_master("地区", {
            "regionCode": code,
            "regionName": name or code,
            "regionLevel": "其他",
            "status": "有效",
        }, actor)
        return self.knowledge.require("地区", str(created["entityId"]))

    def _find_commodity(self, record: RowRecord) -> dict[str, object] | None:
        code = _cell_text(record.values.get("commodityCode"))
        name = _cell_text(record.values.get("commodityName"))
        if code:
            row = self.db.fetch_one('SELECT * FROM "CmdCommodity" WHERE "commodityCode" = :value', {"value": code})
            if row:
                return row
        if name:
            return self.db.fetch_one('SELECT * FROM "CmdCommodity" WHERE "commodityName" = :value', {"value": name})
        return None

    def _ensure_indicator(
        self, record: RowRecord, category: str, stage: str, actor: str
    ) -> dict[str, object]:
        code = _cell_text(record.values.get("indicatorCode"))
        name = _cell_text(record.values.get("indicatorName"))
        if not code and not name:
            raise HTTPException(422, "缺少指标名称或指标编码")
        row = None
        if code:
            row = self.db.fetch_one('SELECT * FROM "Indicator" WHERE "indicatorCode" = :value', {"value": code})
        if not row and name:
            row = self.db.fetch_one('SELECT * FROM "Indicator" WHERE "indicatorName" = :value', {"value": name})
        if row:
            return row
        if not name:
            raise HTTPException(422, f"指标编码 {code} 不存在，请补充指标名称以便自动创建")
        new_code = code or f"IMP-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8].upper()}"
        row = self.db.fetch_one('SELECT * FROM "Indicator" WHERE "indicatorCode" = :value', {"value": new_code})
        if row:
            return row
        created = self.knowledge.create_master("指标定义", {
            "indicatorCode": new_code,
            "indicatorName": name,
            "indicatorCategory": "Excel 导入指标",
            "unit": _cell_text(record.values.get("unit")) or "",
            "frequency": _frequency(record.values.get("periodType") or record.values.get("period")),
            "knowledgeCategoryId": category,
            "industryStage": stage,
            "status": "有效",
        }, actor)
        return self.knowledge.require("指标定义", str(created["entityId"]))
