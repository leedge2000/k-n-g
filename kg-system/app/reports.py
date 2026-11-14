from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from .catalog import SPECS
from .db import quote

KNOWLEDGE_TYPES = ("政策法规", "指标数据", "年鉴记录", "农业情报", "知识关系")

REPORT_TITLES = {
    "REP-01": "知识总量与状态总览",
    "REP-02": "来源文档覆盖情况",
    "REP-03": "审核时效统计",
    "REP-04": "数据质量治理统计",
    "REP-05": "采集任务统计",
    "REP-06": "增量变更导出",
    "REP-07": "分类体系分布",
}


class ReportService:
    def __init__(self, db: Any):
        self.db = db

    def get(self, code: str, since: str | None = None) -> dict[str, Any]:
        builders = {
            "REP-01": self.overview,
            "REP-02": self.source_coverage,
            "REP-03": self.review_latency,
            "REP-04": self.quality,
            "REP-05": self.crawl,
            "REP-06": lambda: self.incremental(since),
            "REP-07": self.taxonomy,
        }
        builder = builders.get(code.upper())
        if not builder:
            raise HTTPException(404, f"未知报表：{code}")
        return {
            "code": code.upper(),
            "title": REPORT_TITLES[code.upper()],
            "generatedAt": self.db.fetch_one("SELECT datetime('now') AS now")["now"],
            "data": builder(),
        }

    def _count(self, table: str, where: str = "", params: dict[str, Any] | None = None) -> int:
        sql = f"SELECT COUNT(*) AS count FROM {quote(table)}" + (f" WHERE {where}" if where else "")
        return self.db.fetch_one(sql, params or {})["count"]

    def overview(self) -> dict[str, Any]:
        by_type = []
        for entity_type in KNOWLEDGE_TYPES:
            spec = SPECS[entity_type]
            status_field = quote(spec.status_field or "status")
            rows = self.db.fetch_all(
                f"SELECT {status_field} AS status, COUNT(*) AS count FROM {quote(spec.table)} GROUP BY {status_field}"
            )
            buckets = {row["status"]: row["count"] for row in rows}
            by_type.append({
                "entityType": entity_type,
                "total": sum(buckets.values()),
                "draft": buckets.get("草稿", 0),
                "pending": buckets.get("待审核", 0),
                "published": buckets.get("已发布", 0),
                "rejected": buckets.get("已退回", 0),
            })
        return {
            "byType": by_type,
            "sourceDocuments": self._count("SourceDoc", '"status"=\'有效\''),
            "publishedRelations": self._count("Relation", '"status"=\'已发布\''),
            "categories": self._count("KtcCategory"),
        }

    def source_coverage(self) -> dict[str, Any]:
        by_doc_type = self.db.fetch_all(
            'SELECT "docType" AS docType, COUNT(*) AS count FROM "SourceDoc" GROUP BY "docType" ORDER BY count DESC'
        )
        by_category = self.db.fetch_all(
            'SELECT "knowledgeCategoryId" AS categoryId, COUNT(*) AS count FROM "SourceDoc" '
            'WHERE "knowledgeCategoryId" IS NOT NULL GROUP BY "knowledgeCategoryId" ORDER BY count DESC LIMIT 10'
        )
        return {
            "documents": self._count("SourceDoc"),
            "segments": self._count("SourceDoc_Segment"),
            "byDocType": by_doc_type,
            "topCategories": by_category,
            "documentsWithoutCategory": self._count("SourceDoc", '"knowledgeCategoryId" IS NULL'),
        }

    def review_latency(self) -> dict[str, Any]:
        rows = []
        for entity_type in KNOWLEDGE_TYPES:
            spec = SPECS[entity_type]
            status_field = quote(spec.status_field or "status")
            row = self.db.fetch_one(
                f'SELECT COUNT(*) AS published, '
                f'AVG((julianday("updatedAt") - julianday("createdAt")) * 24.0) AS avgHours '
                f'FROM {quote(spec.table)} WHERE {status_field}=\'已发布\''
            )
            pending = self.db.fetch_one(
                f"SELECT COUNT(*) AS count FROM {quote(spec.table)} WHERE {status_field}='待审核'"
            )["count"]
            rows.append({
                "entityType": entity_type,
                "published": row["published"],
                "pending": pending,
                "avgHours": round(row["avgHours"], 1) if row["avgHours"] is not None else None,
            })
        return {"byType": rows}

    def quality(self) -> dict[str, Any]:
        by_issue = self.db.fetch_all(
            'SELECT "issueType" AS issueType, COUNT(*) AS count FROM "QisRecord" GROUP BY "issueType" ORDER BY count DESC'
        )
        by_status = self.db.fetch_all(
            'SELECT "handleStatus" AS handleStatus, COUNT(*) AS count FROM "QisRecord" GROUP BY "handleStatus" ORDER BY count DESC'
        )
        return {
            "total": self._count("QisRecord"),
            "byIssueType": by_issue,
            "byHandleStatus": by_status,
            "unresolved": self._count("QisRecord", '"handleStatus" NOT IN (\'已处理\',\'已关闭\')'),
        }

    def crawl(self) -> dict[str, Any]:
        by_status = self.db.fetch_all(
            'SELECT "runStatus" AS runStatus, COUNT(*) AS count FROM "CrsSource_CrawlRun" GROUP BY "runStatus" ORDER BY count DESC'
        )
        recent = self.db.fetch_all(
            'SELECT "runId","crawlSourceId","startedAt","finishedAt","runStatus","newDocCount","duplicateCount","errorMessage" '
            'FROM "CrsSource_CrawlRun" ORDER BY "startedAt" DESC LIMIT 10'
        )
        return {
            "sources": self._count("CrsSource"),
            "enabledSources": self._count("CrsSource", '"enabledFlag"=\'启用\''),
            "runs": self._count("CrsSource_CrawlRun"),
            "byRunStatus": by_status,
            "recentRuns": recent,
        }

    def incremental(self, since: str | None) -> dict[str, Any]:
        since = since or "1970-01-01T00:00:00Z"
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", since):
            raise HTTPException(400, "since 必须为 UTC ISO 8601 时间")
        by_type = self.db.fetch_all(
            'SELECT "entityType" AS entityType, COUNT(*) AS count FROM "ChangeLog" WHERE "updatedAt">:since '
            'GROUP BY "entityType" ORDER BY count DESC',
            {"since": since},
        )
        recent = self.db.fetch_all(
            'SELECT "changeId","entityType","entityId","changeType","updatedAt" FROM "ChangeLog" '
            'WHERE "updatedAt">:since ORDER BY "updatedAt" DESC LIMIT 50',
            {"since": since},
        )
        return {"since": since, "byEntityType": by_type, "recentChanges": recent}

    def taxonomy(self) -> dict[str, Any]:
        by_root = self.db.fetch_all(
            'SELECT "rootCategory" AS rootCategory, COUNT(*) AS count FROM "KtcCategory" '
            'GROUP BY "rootCategory" ORDER BY count DESC'
        )
        by_stage = self.db.fetch_all(
            'SELECT "industryStage" AS industryStage, COUNT(*) AS count FROM "KtcCategory" '
            'GROUP BY "industryStage" ORDER BY count DESC'
        )
        used = self.db.fetch_all(
            'SELECT "knowledgeCategoryId" AS categoryId, COUNT(*) AS count FROM ('
            ' SELECT "knowledgeCategoryId" FROM "Policy" WHERE "knowledgeCategoryId" IS NOT NULL'
            ' UNION ALL SELECT "knowledgeCategoryId" FROM "IvdData" WHERE "knowledgeCategoryId" IS NOT NULL'
            ' UNION ALL SELECT "knowledgeCategoryId" FROM "YrbYearbook" WHERE "knowledgeCategoryId" IS NOT NULL'
            ' UNION ALL SELECT "knowledgeCategoryId" FROM "Intel" WHERE "knowledgeCategoryId" IS NOT NULL'
            ') GROUP BY "knowledgeCategoryId" ORDER BY count DESC LIMIT 10'
        )
        return {
            "totalCategories": self._count("KtcCategory"),
            "byRootCategory": by_root,
            "byIndustryStage": by_stage,
            "topUsedCategories": used,
        }
