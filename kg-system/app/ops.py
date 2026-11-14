from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text

from .catalog import KNOWLEDGE_TYPES, SPECS
from .db import Database, quote, utc_now
from .scope import ScopeError, ScopeService

logger = logging.getLogger(__name__)


class OpsError(RuntimeError):
    pass


# 指标口径冻结在代码里：方向 GTE 越大越好、LTE 越小越好，阈值见阶段 5 冻结稿 §3.1
METRICS: dict[str, dict[str, Any]] = {
    "SRC_COVERAGE": {"name": "来源覆盖率", "unit": "RATIO", "direction": "GTE", "threshold": 0.95},
    "ORPHAN_RATE": {"name": "孤儿知识率", "unit": "RATIO", "direction": "LTE", "threshold": 0.02},
    "DUP_PENDING": {"name": "待处理重复数", "unit": "COUNT", "direction": "LTE", "threshold": 10},
    "CONFLICT_PENDING": {"name": "待处理冲突数", "unit": "COUNT", "direction": "LTE", "threshold": 0},
    "STALE_SOURCE": {"name": "超期采集源数", "unit": "COUNT", "direction": "LTE", "threshold": 0},
    "CRAWL_FAIL_RATE": {"name": "采集失败率", "unit": "RATIO", "direction": "LTE", "threshold": 0.2},
    "REVIEW_BACKLOG": {"name": "待审核积压", "unit": "COUNT", "direction": "LTE", "threshold": 50},
    "RULE_COVERAGE": {"name": "规则覆盖率", "unit": "RATIO", "direction": "GTE", "threshold": 0.5},
}

# 告警规则：与指标状态解耦，按显式条件判定，便于单测覆盖边界
ALERT_RULES: tuple[dict[str, Any], ...] = (
    {"alertCode": "ALT-DUP", "metricCode": "DUP_PENDING", "severity": "P3",
     "operator": "gt", "threshold": 10, "title": "待处理重复数据超过阈值"},
    {"alertCode": "ALT-CONFLICT", "metricCode": "CONFLICT_PENDING", "severity": "P2",
     "operator": "gt", "threshold": 0, "title": "存在未处理的指标冲突"},
    {"alertCode": "ALT-ORPHAN", "metricCode": "ORPHAN_RATE", "severity": "P2",
     "operator": "gt", "threshold": 0.02, "title": "孤儿知识率超过阈值"},
    {"alertCode": "ALT-SOURCE-STALE", "metricCode": "STALE_SOURCE", "severity": "P3",
     "operator": "gt", "threshold": 0, "title": "存在超期未采集的采集源"},
    {"alertCode": "ALT-CRAWL-FAIL", "metricCode": "CRAWL_FAIL_RATE", "severity": "P2",
     "operator": "gt", "threshold": 0.2, "title": "近 7 天采集失败率超过阈值"},
    {"alertCode": "ALT-REVIEW-BACKLOG", "metricCode": "REVIEW_BACKLOG", "severity": "P3",
     "operator": "gt", "threshold": 50, "title": "待审核积压超过阈值"},
    {"alertCode": "ALT-RULE-COVERAGE", "metricCode": "RULE_COVERAGE", "severity": "P3",
     "operator": "lt", "threshold": 0.5, "title": "规则覆盖率低于阈值"},
)

TARGET_KINDS = ("SCOPE", "RULE", "ONTOLOGY_META")
CHANGE_TYPES = {
    "SCOPE": ("CREATE", "UPDATE"),
    "RULE": ("UPDATE",),
    "ONTOLOGY_META": ("UPDATE", "ARCHIVE"),
}
REQUEST_STATUSES = ("DRAFT", "PENDING_REVIEW", "APPROVED", "REJECTED", "PUBLISHED", "ROLLED_BACK")
RULE_FIELDS = ("confirmationStatus", "backTranslationMatch", "testcaseIds", "ruleExpression")
META_FIELDS = ("displayName", "description", "status")


def judge_status(value: float, direction: str, threshold: float) -> str:
    """按方向判定指标状态：threshold=0 的 LTE 指标不做 BREACH 分级。"""
    if direction == "GTE":
        if value < threshold * 0.8:
            return "BREACH"
        if value < threshold:
            return "WARN"
        return "OK"
    if threshold > 0 and value > threshold * 2:
        return "BREACH"
    if value > threshold:
        return "WARN"
    return "OK"


class OpsService:
    def __init__(self, db: Database, scope: ScopeService, crawler: Any = None):
        self.db = db
        self.scope = scope
        self.crawler = crawler

    # ---------- 初始化与门禁 ----------

    def initialize(self) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_metric_code_time" ON "OpsQualityMetric" ("metricCode","measuredAt")'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_metric_time" ON "OpsQualityMetric" ("measuredAt")'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_alert_status" ON "OpsAlert" ("status")'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_evo_status" ON "OpsEvolutionRequest" ("status")'
            ))
        self.inspect("system")
        self.validate_stage5()

    def validate_stage5(self) -> dict[str, Any]:
        checks: dict[str, str] = {}

        # G5-1 四张运营表与审计列齐备
        for table in ("OpsQualityMetric", "OpsAlert", "OpsEvolutionRequest", "OpsEvolutionRelease"):
            columns = set(self.db.columns.get(table, []))
            missing = {"createdBy", "createdAt", "updatedBy", "updatedAt"} - columns
            if not columns or missing:
                raise OpsError(f"G5-1 运营表 {table} 缺列：{sorted(missing) or '表未登记'}")
        checks["G5-1"] = "4 张运营表与审计列齐备"

        # G5-2 最近一轮快照覆盖全部指标
        latest = self.db.fetch_one('SELECT MAX("measuredAt") AS moment FROM "OpsQualityMetric"') or {}
        moment = latest.get("moment")
        if not moment:
            raise OpsError("G5-2 尚无质量指标快照，巡检未执行")
        rows = self.db.fetch_all(
            'SELECT "metricCode" FROM "OpsQualityMetric" WHERE "measuredAt"=:moment', {"moment": moment}
        )
        codes = {row["metricCode"] for row in rows}
        absent = set(METRICS) - codes
        if absent:
            raise OpsError(f"G5-2 最近一轮（{moment}）缺少指标：{sorted(absent)}")
        checks["G5-2"] = f"最近一轮覆盖 {len(METRICS)} 个指标"

        # G5-3 同一告警编码最多一条 OPEN
        duplicates = self.db.fetch_all(
            'SELECT "alertCode", COUNT(*) AS total FROM "OpsAlert" WHERE "status"=\'OPEN\' '
            'GROUP BY "alertCode" HAVING COUNT(*)>1'
        )
        if duplicates:
            raise OpsError(f"G5-3 告警重复 OPEN：{[(r['alertCode'], r['total']) for r in duplicates]}")
        checks["G5-3"] = "告警编码在 OPEN 状态唯一"

        # G5-4/G5-5 状态机与回滚一致性
        requests = self.db.fetch_all(
            'SELECT "changeId","status","releaseId","reviewedBy","publishedAt" FROM "OpsEvolutionRequest"'
        )
        releases = {
            row["releaseId"]: row["status"]
            for row in self.db.fetch_all('SELECT "releaseId","status" FROM "OpsEvolutionRelease"')
        }
        for row in requests:
            if row["status"] not in REQUEST_STATUSES:
                raise OpsError(f"G5-4 申请 {row['changeId']} 状态非法：{row['status']}")
            if row["status"] == "PUBLISHED":
                missing = [key for key in ("releaseId", "reviewedBy", "publishedAt") if not row.get(key)]
                if missing:
                    raise OpsError(f"G5-4 已发布申请 {row['changeId']} 缺 {missing}")
            if row["status"] == "ROLLED_BACK" and row.get("releaseId"):
                if releases.get(row["releaseId"]) != "ROLLED_BACK":
                    raise OpsError(f"G5-5 申请 {row['changeId']} 已回滚，但版本 {row['releaseId']} 未标记回滚")
        checks["G5-4"] = f"{len(requests)} 条申请状态合法"
        checks["G5-5"] = "回滚申请与版本状态一致"

        # G5-6 版本号连续
        versions = sorted(
            int(row["versionNo"] or 0)
            for row in self.db.fetch_all('SELECT "versionNo" FROM "OpsEvolutionRelease"')
        )
        if versions and versions != list(range(1, len(versions) + 1)):
            raise OpsError(f"G5-6 发布版本号不连续：{versions}")
        checks["G5-6"] = f"{len(versions)} 个发布版本号连续"

        # G5-7 快照状态与阈值判定一致
        snapshots = self.db.fetch_all(
            'SELECT * FROM "OpsQualityMetric" WHERE "measuredAt"=:moment', {"moment": moment}
        )
        for row in snapshots:
            expected = judge_status(float(row["metricValue"]), row["direction"], float(row["threshold"] or 0))
            if expected != row["status"]:
                raise OpsError(
                    f"G5-7 指标 {row['metricCode']}（{row['metricId']}）状态 {row['status']} 与阈值判定 {expected} 不一致"
                )
        checks["G5-7"] = f"{len(snapshots)} 条快照状态与阈值一致"

        # G5-8 已发布申请可回溯到变更流
        published = [row["changeId"] for row in requests if row["status"] == "PUBLISHED"]
        if published:
            logged = {
                row["entityId"]
                for row in self.db.fetch_all(
                    'SELECT "entityId" FROM "ChangeLog" WHERE "entityType"=\'运营演进\' '
                    'AND "changeType"=\'OPS_EVOLUTION_PUBLISH\''
                )
            }
            absent = [change_id for change_id in published if change_id not in logged]
            if absent:
                raise OpsError(f"G5-8 已发布申请缺变更流记录：{absent}")
        checks["G5-8"] = f"{len(published)} 条已发布申请均有变更流记录"

        return {"score": 100, "checks": checks}

    # ---------- 指标计算 ----------

    def _published_knowledge_rows(self) -> list[tuple[str, dict[str, Any]]]:
        rows: list[tuple[str, dict[str, Any]]] = []
        for entity_type in KNOWLEDGE_TYPES:
            spec = SPECS[entity_type]
            for row in self.db.fetch_all(
                f'SELECT * FROM {quote(spec.table)} WHERE {quote(spec.status_field)}=\'已发布\''
            ):
                rows.append((entity_type, row))
        return rows

    def _source_doc_ids(self) -> set[str]:
        return {
            row["docId"]
            for row in self.db.fetch_all('SELECT "docId" FROM "SourceDoc" WHERE "status"<>\'已作废\'')
        }

    def compute_metrics(self) -> dict[str, dict[str, Any]]:
        knowledge = self._published_knowledge_rows()
        docs = self._source_doc_ids()
        total = len(knowledge)
        covered = sum(1 for _, row in knowledge if row.get("sourceDocId") and row["sourceDocId"] in docs)
        orphan = total - covered
        coverage = round(covered / total, 4) if total else 1.0
        orphan_rate = round(orphan / total, 4) if total else 0.0

        issue_rows = self.db.fetch_all('SELECT "issueType","handleStatus" FROM "QisRecord"')
        duplicate = sum(1 for row in issue_rows if row["issueType"] == "重复" and row["handleStatus"] == "待处理")
        conflict = sum(1 for row in issue_rows if row["issueType"] == "冲突" and row["handleStatus"] == "待处理")

        now = datetime.now(timezone.utc)
        sources = self.db.fetch_all('SELECT * FROM "CrsSource" WHERE "enabledFlag"=\'启用\'')
        stale = 0
        for source in sources:
            window = int(source.get("minIntervalMinutes") or 60) * 2
            last_run_at = source.get("lastRunAt")
            if not last_run_at:
                stale += 1
                continue
            try:
                last_run = datetime.fromisoformat(str(last_run_at).replace("Z", "+00:00"))
            except ValueError:
                stale += 1
                continue
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            if now - last_run > timedelta(minutes=window):
                stale += 1

        since = (now - timedelta(days=7)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        runs = self.db.fetch_all(
            'SELECT "runStatus" FROM "CrsSource_CrawlRun" WHERE "startedAt">=:since', {"since": since}
        )
        fail_rate = round(
            sum(1 for row in runs if row["runStatus"] == "失败") / len(runs), 4
        ) if runs else 0.0

        backlog = 0
        backlog_detail: dict[str, int] = {}
        for entity_type in KNOWLEDGE_TYPES:
            spec = SPECS[entity_type]
            row = self.db.fetch_one(
                f'SELECT COUNT(*) AS total FROM {quote(spec.table)} WHERE {quote(spec.status_field)}=\'待审核\''
            ) or {}
            count = int(row.get("total") or 0)
            backlog += count
            backlog_detail[entity_type] = count

        policies = self.db.fetch_one(
            'SELECT COUNT(*) AS total FROM "Policy" WHERE "reviewStatus"=\'已发布\''
        ) or {}
        policy_total = int(policies.get("total") or 0)
        covered_rule = self.db.fetch_one(
            'SELECT COUNT(DISTINCT pc."policyId") AS total FROM "CompiledRule" cr '
            'JOIN "NormativeStatement" ns ON ns."statementId"=cr."statementId" '
            'JOIN "Policy_Clause" pc ON pc."clauseId"=ns."clauseId" '
            'JOIN "Policy" p ON p."policyId"=pc."policyId" '
            'WHERE p."reviewStatus"=\'已发布\' AND cr."confirmationStatus"=\'CONFIRMED\''
        ) or {}
        rule_coverage = round(int(covered_rule.get("total") or 0) / policy_total, 4) if policy_total else 1.0

        values = {
            "SRC_COVERAGE": (coverage, {"covered": covered, "total": total}),
            "ORPHAN_RATE": (orphan_rate, {"orphan": orphan, "total": total}),
            "DUP_PENDING": (float(duplicate), {"pending": duplicate}),
            "CONFLICT_PENDING": (float(conflict), {"pending": conflict}),
            "STALE_SOURCE": (float(stale), {"enabledSources": len(sources)}),
            "CRAWL_FAIL_RATE": (fail_rate, {"runs7d": len(runs)}),
            "REVIEW_BACKLOG": (float(backlog), backlog_detail),
            "RULE_COVERAGE": (rule_coverage, {"policies": policy_total}),
        }
        result: dict[str, dict[str, Any]] = {}
        for code, definition in METRICS.items():
            value, detail = values[code]
            result[code] = {
                "metricCode": code,
                "metricName": definition["name"],
                "unit": definition["unit"],
                "direction": definition["direction"],
                "threshold": float(definition["threshold"]),
                "metricValue": value,
                "status": judge_status(value, definition["direction"], float(definition["threshold"])),
                "detail": detail,
            }
        return result

    # ---------- 巡检 ----------

    def inspect(self, actor: str) -> dict[str, Any]:
        metrics = self.compute_metrics()
        moment = utc_now()
        raised: list[str] = []
        closed: list[str] = []
        with self.db.engine.begin() as conn:
            for metric in metrics.values():
                self.db.insert("OpsQualityMetric", {
                    "metricId": self.db.next_id("OpsQualityMetric", "MQM", conn),
                    "metricCode": metric["metricCode"],
                    "metricName": metric["metricName"],
                    "metricValue": metric["metricValue"],
                    "unit": metric["unit"],
                    "threshold": metric["threshold"],
                    "direction": metric["direction"],
                    "status": metric["status"],
                    "measuredAt": moment,
                    "detail": json.dumps(metric["detail"], ensure_ascii=False),
                }, conn, actor=actor)

            for rule in ALERT_RULES:
                metric = metrics[rule["metricCode"]]
                triggered = (
                    metric["metricValue"] > rule["threshold"]
                    if rule["operator"] == "gt"
                    else metric["metricValue"] < rule["threshold"]
                )
                active = self.db.fetch_one(
                    'SELECT * FROM "OpsAlert" WHERE "alertCode"=:code AND "status" IN (\'OPEN\',\'ACK\') '
                    'ORDER BY "raisedAt" DESC LIMIT 1', {"code": rule["alertCode"]}, conn,
                )
                if triggered and not active:
                    self.db.insert("OpsAlert", {
                        "alertId": self.db.next_id("OpsAlert", "ALT", conn),
                        "alertCode": rule["alertCode"],
                        "severity": rule["severity"],
                        "title": rule["title"],
                        "detail": json.dumps({
                            "metricCode": rule["metricCode"],
                            "metricValue": metric["metricValue"],
                            "threshold": rule["threshold"],
                            **metric["detail"],
                        }, ensure_ascii=False),
                        "metricCode": rule["metricCode"],
                        "metricValue": metric["metricValue"],
                        "status": "OPEN",
                        "raisedAt": moment,
                    }, conn, actor=actor)
                    raised.append(rule["alertCode"])
                elif not triggered and active:
                    self.db.update("OpsAlert", active["alertId"], {
                        "status": "CLOSED", "closedAt": moment,
                    }, conn, actor=actor)
                    closed.append(rule["alertCode"])
        return {
            "measuredAt": moment,
            "metrics": list(metrics.values()),
            "raised": raised,
            "closed": closed,
        }

    # ---------- 查询 ----------

    def latest_metrics(self) -> list[dict[str, Any]]:
        rows = self.db.fetch_all('SELECT * FROM "OpsQualityMetric" ORDER BY "measuredAt" DESC, "metricCode"')
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(row["metricCode"], row)
        return [latest[code] for code in METRICS if code in latest]

    def overview(self) -> dict[str, Any]:
        alerts = self.db.fetch_all('SELECT "alertCode","severity","status","title","raisedAt" FROM "OpsAlert"')
        pending_status = ("OPEN", "ACK")
        active_alerts = [row for row in alerts if row["status"] in pending_status]
        by_severity = {"P1": 0, "P2": 0, "P3": 0}
        for row in active_alerts:
            by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + 1
        evolution: dict[str, int] = {}
        for row in self.db.fetch_all('SELECT "status" FROM "OpsEvolutionRequest"'):
            evolution[row["status"]] = evolution.get(row["status"], 0) + 1
        sources = self.db.fetch_all('SELECT "enabledFlag","lastRunAt" FROM "CrsSource"')
        latest = self.latest_metrics()
        return {
            "lastInspectedAt": latest[0]["measuredAt"] if latest else None,
            "metrics": latest,
            "alerts": {
                "active": len(active_alerts),
                "open": sum(1 for row in alerts if row["status"] == "OPEN"),
                "ack": sum(1 for row in alerts if row["status"] == "ACK"),
                "bySeverity": by_severity,
                "items": active_alerts[:10],
            },
            "ingest": {
                "sources": len(sources),
                "enabled": sum(1 for row in sources if row["enabledFlag"] == "启用"),
            },
            "evolution": evolution,
        }

    def metrics(self, metric_code: str | None = None, limit: int = 100) -> dict[str, Any]:
        if metric_code:
            rows = self.db.fetch_all(
                'SELECT * FROM "OpsQualityMetric" WHERE "metricCode"=:code ORDER BY "measuredAt" DESC LIMIT :limit',
                {"code": metric_code, "limit": limit},
            )
        else:
            rows = self.db.fetch_all(
                'SELECT * FROM "OpsQualityMetric" ORDER BY "measuredAt" DESC LIMIT :limit', {"limit": limit}
            )
        return {"total": len(rows), "items": rows}

    def alerts(self, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        if status:
            rows = self.db.fetch_all(
                'SELECT * FROM "OpsAlert" WHERE "status"=:status ORDER BY "raisedAt" DESC LIMIT :limit',
                {"status": status, "limit": limit},
            )
        else:
            rows = self.db.fetch_all(
                'SELECT * FROM "OpsAlert" ORDER BY "raisedAt" DESC LIMIT :limit', {"limit": limit}
            )
        return {"total": len(rows), "items": rows}

    def ack(self, alert_id: str, actor: str) -> dict[str, Any]:
        alert = self.db.fetch_one('SELECT * FROM "OpsAlert" WHERE "alertId"=:id', {"id": alert_id})
        if not alert:
            raise HTTPException(404, "告警不存在")
        if alert["status"] != "OPEN":
            raise HTTPException(409, f"仅 OPEN 告警可认领，当前状态 {alert['status']}")
        moment = utc_now()
        with self.db.engine.begin() as conn:
            self.db.update("OpsAlert", alert_id, {
                "status": "ACK", "ackedBy": actor, "ackedAt": moment,
            }, conn, actor=actor)
        return {"alertId": alert_id, "status": "ACK", "ackedAt": moment}

    def close(self, alert_id: str, actor: str) -> dict[str, Any]:
        alert = self.db.fetch_one('SELECT * FROM "OpsAlert" WHERE "alertId"=:id', {"id": alert_id})
        if not alert:
            raise HTTPException(404, "告警不存在")
        if alert["status"] == "CLOSED":
            raise HTTPException(409, "告警已关闭")
        moment = utc_now()
        with self.db.engine.begin() as conn:
            self.db.update("OpsAlert", alert_id, {
                "status": "CLOSED", "closedAt": moment,
            }, conn, actor=actor)
        return {"alertId": alert_id, "status": "CLOSED", "closedAt": moment}

    # ---------- 增量入库 ----------

    def ingest(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=7)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        sources = []
        for source in self.db.fetch_all('SELECT * FROM "CrsSource" ORDER BY "crawlSourceId"'):
            runs = self.db.fetch_all(
                'SELECT "runId","runStatus","startedAt","newDocCount","errorMessage" '
                'FROM "CrsSource_CrawlRun" WHERE "crawlSourceId"=:id AND "startedAt">=:since '
                'ORDER BY "startedAt" DESC',
                {"id": source["crawlSourceId"], "since": since},
            )
            success = sum(1 for row in runs if row["runStatus"] == "成功")
            window = int(source.get("minIntervalMinutes") or 60) * 2
            stale = True
            if source.get("lastRunAt"):
                try:
                    last_run = datetime.fromisoformat(str(source["lastRunAt"]).replace("Z", "+00:00"))
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=timezone.utc)
                    stale = now - last_run > timedelta(minutes=window)
                except ValueError:
                    stale = True
            sources.append({
                "crawlSourceId": source["crawlSourceId"],
                "sourceName": source["sourceName"],
                "enabledFlag": source["enabledFlag"],
                "lastRunAt": source.get("lastRunAt"),
                "staleFlag": stale and source["enabledFlag"] == "启用",
                "runs7d": len(runs),
                "success7d": success,
                "successRate": round(success / len(runs), 4) if runs else None,
                "lastRunStatus": runs[0]["runStatus"] if runs else None,
                "lastError": runs[0]["errorMessage"] if runs else None,
            })

        backlog = {}
        for entity_type in KNOWLEDGE_TYPES:
            spec = SPECS[entity_type]
            row = self.db.fetch_one(
                f'SELECT COUNT(*) AS total FROM {quote(spec.table)} WHERE {quote(spec.status_field)}=\'待审核\''
            ) or {}
            backlog[entity_type] = int(row.get("total") or 0)

        changes = self.db.fetch_all(
            'SELECT * FROM "ChangeLog" ORDER BY "updatedAt" DESC LIMIT 20'
        )
        return {
            "sources": sources,
            "backlog": {"total": sum(backlog.values()), "byType": backlog},
            "changes": {
                "cursor": changes[0]["updatedAt"] if changes else None,
                "items": changes,
            },
        }

    def retry_failed(self, source_id: str, actor: str) -> dict[str, Any]:
        source = self.db.fetch_one(
            'SELECT * FROM "CrsSource" WHERE "crawlSourceId"=:id', {"id": source_id}
        )
        if not source:
            raise HTTPException(404, "采集源不存在")
        if source["enabledFlag"] != "启用":
            raise HTTPException(409, "采集源处于暂停状态，启用后才可重跑")
        runs = self.db.fetch_all(
            'SELECT "runStatus" FROM "CrsSource_CrawlRun" WHERE "crawlSourceId"=:id '
            'ORDER BY "startedAt" DESC LIMIT 20', {"id": source_id},
        )
        failures = 0
        for row in runs:
            if row["runStatus"] == "失败":
                failures += 1
            else:
                break
        limit = int(source.get("maxRetry") or 3)
        if failures >= limit:
            raise HTTPException(409, f"连续失败 {failures} 次已达重试上限 {limit}，请人工排查")
        if self.crawler is None:
            raise HTTPException(503, "采集器未装配")
        return self.crawler.trigger(source_id, actor)

    # ---------- 演进 ----------

    def _request(self, change_id: str) -> dict[str, Any]:
        row = self.db.fetch_one(
            'SELECT * FROM "OpsEvolutionRequest" WHERE "changeId"=:id', {"id": change_id}
        )
        if not row:
            raise HTTPException(404, "演进申请不存在")
        return row

    def _log(self, conn: Any, change_id: str, change_type: str, payload: dict[str, Any], actor: str) -> None:
        self.db.log_change(conn, "运营演进", change_id, change_type, payload)

    def create_draft(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        target_kind = str(payload.get("targetKind") or "").upper()
        change_type = str(payload.get("changeType") or "").upper()
        if target_kind not in TARGET_KINDS:
            raise HTTPException(422, f"targetKind 必须是 {TARGET_KINDS} 之一")
        if change_type not in CHANGE_TYPES[target_kind]:
            raise HTTPException(422, f"{target_kind} 仅支持 {CHANGE_TYPES[target_kind]}")
        draft = payload.get("draftPayload")
        if not isinstance(draft, dict) or not draft:
            raise HTTPException(422, "draftPayload 必须是非空对象")
        target_id = payload.get("targetId")
        if change_type != "CREATE" and not target_id:
            raise HTTPException(422, "更新类变更必须指定 targetId")
        with self.db.engine.begin() as conn:
            change_id = self.db.next_id("OpsEvolutionRequest", "OCK", conn)
            self.db.insert("OpsEvolutionRequest", {
                "changeId": change_id,
                "targetKind": target_kind,
                "targetId": target_id,
                "changeType": change_type,
                "draftPayload": json.dumps(draft, ensure_ascii=False),
                "status": "DRAFT",
            }, conn, actor=actor)
            self._log(conn, change_id, "OPS_EVOLUTION_CREATE", {
                "status": "DRAFT", "targetKind": target_kind, "changeType": change_type,
            }, actor)
        return {"changeId": change_id, "status": "DRAFT"}

    def submit(self, change_id: str, actor: str) -> dict[str, Any]:
        request = self._request(change_id)
        if request["status"] not in ("DRAFT", "REJECTED"):
            raise HTTPException(409, f"{request['status']} 状态不可提交评审")
        moment = utc_now()
        with self.db.engine.begin() as conn:
            self.db.update("OpsEvolutionRequest", change_id, {
                "status": "PENDING_REVIEW", "submittedBy": actor, "submittedAt": moment,
            }, conn, actor=actor)
            self._log(conn, change_id, "OPS_EVOLUTION_SUBMIT", {
                "status": "PENDING_REVIEW", "submittedBy": actor,
            }, actor)
        return {"changeId": change_id, "status": "PENDING_REVIEW"}

    def review(self, change_id: str, approved: bool, remark: str | None, actor: str) -> dict[str, Any]:
        request = self._request(change_id)
        if request["status"] != "PENDING_REVIEW":
            raise HTTPException(409, f"{request['status']} 状态不可评审")
        status = "APPROVED" if approved else "REJECTED"
        moment = utc_now()
        with self.db.engine.begin() as conn:
            self.db.update("OpsEvolutionRequest", change_id, {
                "status": status, "reviewedBy": actor, "reviewedAt": moment, "reviewRemark": remark,
            }, conn, actor=actor)
            self._log(conn, change_id, "OPS_EVOLUTION_REVIEW", {
                "status": status, "reviewedBy": actor, "remark": remark,
            }, actor)
        return {"changeId": change_id, "status": status, "reviewedBy": actor}

    def publish(self, change_id: str, actor: str) -> dict[str, Any]:
        request = self._request(change_id)
        if request["status"] != "APPROVED":
            raise HTTPException(409, f"{request['status']} 状态不可发布，需先评审通过")
        if not request.get("reviewedBy"):
            raise HTTPException(409, "缺少评审人，不可发布")
        if request["reviewedBy"] == request.get("submittedBy"):
            raise HTTPException(409, "评审人与提交人不能为同一人")
        draft = json.loads(request["draftPayload"])
        target_kind = request["targetKind"]
        applier = {
            "SCOPE": self._apply_scope,
            "RULE": self._apply_rule,
            "ONTOLOGY_META": self._apply_meta,
        }[target_kind]
        previous, impact, applied = applier(request, draft, actor)

        moment = utc_now()
        with self.db.engine.begin() as conn:
            version_row = self.db.fetch_one(
                'SELECT COALESCE(MAX("versionNo"),0) AS total FROM "OpsEvolutionRelease"', conn=conn
            ) or {}
            version_no = int(version_row.get("total") or 0) + 1
            release_id = self.db.next_id("OpsEvolutionRelease", "RVW", conn)
            self.db.insert("OpsEvolutionRelease", {
                "releaseId": release_id,
                "versionNo": version_no,
                "summary": f"{target_kind} {request['changeId']}",
                "changeIds": json.dumps([change_id], ensure_ascii=False),
                "snapshot": json.dumps({"previous": previous, "applied": applied}, ensure_ascii=False, default=str),
                "publishedBy": actor,
                "publishedAt": moment,
                "status": "ACTIVE",
            }, conn, actor=actor)
            self.db.update("OpsEvolutionRequest", change_id, {
                "status": "PUBLISHED",
                "previousPayload": json.dumps(previous, ensure_ascii=False, default=str),
                "impactSummary": impact,
                "publishedAt": moment,
                "releaseId": release_id,
            }, conn, actor=actor)
            self._log(conn, change_id, "OPS_EVOLUTION_PUBLISH", {
                "status": "PUBLISHED", "releaseId": release_id, "versionNo": version_no,
                "impactSummary": impact,
            }, actor)
        return {
            "changeId": change_id, "status": "PUBLISHED", "releaseId": release_id,
            "versionNo": version_no, "impactSummary": impact,
        }

    def rollback(self, change_id: str, actor: str) -> dict[str, Any]:
        request = self._request(change_id)
        if request["status"] != "PUBLISHED":
            raise HTTPException(409, f"{request['status']} 状态不可回滚")
        previous = json.loads(request["previousPayload"] or "{}")
        target_kind = request["targetKind"]
        restorer = {
            "SCOPE": self._restore_scope,
            "RULE": self._restore_rule,
            "ONTOLOGY_META": self._restore_meta,
        }[target_kind]
        restorer(request, previous)
        moment = utc_now()
        with self.db.engine.begin() as conn:
            self.db.update("OpsEvolutionRequest", change_id, {"status": "ROLLED_BACK"}, conn, actor=actor)
            if request.get("releaseId"):
                release = self.db.fetch_one(
                    'SELECT * FROM "OpsEvolutionRelease" WHERE "releaseId"=:id', {"id": request["releaseId"]}, conn
                )
                if release and release["status"] != "ROLLED_BACK":
                    self.db.update("OpsEvolutionRelease", request["releaseId"], {
                        "status": "ROLLED_BACK", "rolledBackAt": moment,
                    }, conn, actor=actor)
            self._log(conn, change_id, "OPS_EVOLUTION_ROLLBACK", {
                "status": "ROLLED_BACK", "releaseId": request.get("releaseId"),
            }, actor)
        return {"changeId": change_id, "status": "ROLLED_BACK", "releaseId": request.get("releaseId")}

    def list_changes(self, status: str | None = None, target_kind: str | None = None) -> dict[str, Any]:
        sql = 'SELECT * FROM "OpsEvolutionRequest"'
        clauses, params = [], {}
        if status:
            clauses.append('"status"=:status')
            params["status"] = status
        if target_kind:
            clauses.append('"targetKind"=:kind')
            params["kind"] = target_kind
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += ' ORDER BY "createdAt" DESC, "changeId" DESC'
        rows = self.db.fetch_all(sql, params)
        return {"total": len(rows), "items": rows}

    def list_releases(self) -> dict[str, Any]:
        rows = self.db.fetch_all('SELECT * FROM "OpsEvolutionRelease" ORDER BY "versionNo" DESC')
        return {"total": len(rows), "items": rows}

    # ---- 三类目标：发布 ----

    def _apply_scope(self, request: dict[str, Any], draft: dict[str, Any], actor: str) -> tuple[dict, str, dict]:
        scope_code = str(draft.get("scopeCode") or "").strip().upper()
        scope_type = str(draft.get("scopeType") or "").strip().upper()
        members = draft.get("members") or []
        if not scope_code or not scope_type:
            raise HTTPException(422, "SCOPE 变更需提供 scopeCode 与 scopeType")
        if request["changeType"] == "CREATE":
            try:
                result = self.scope.upsert_scope(
                    scope_type=scope_type, members=members, scope_code=scope_code,
                    label=draft.get("label"), is_open=bool(draft.get("isOpen")),
                    superset_of=draft.get("supersetOf") or [], actor=actor,
                )
            except ScopeError as exc:
                raise HTTPException(422, str(exc)) from exc
            impact = f"新建范围 {result['scopeId']}，成员 {result.get('memberCount')} 个"
            return {}, impact, {"scopeId": result["scopeId"], "members": members}

        target = self.db.fetch_one(
            'SELECT * FROM "OntologyScope" WHERE "scopeId"=:id', {"id": request["targetId"]}
        )
        if not target:
            raise HTTPException(404, "范围不存在")
        normalized = json.loads(target["members"])
        key = target["normalizedKey"]
        if members:
            try:
                normalized, key = self.scope.prepare_members(scope_type or target["scopeType"], members)
            except ScopeError as exc:
                raise HTTPException(422, str(exc)) from exc
            conflict = self.db.fetch_one(
                'SELECT "scopeId" FROM "OntologyScope" WHERE "normalizedKey"=:key AND "scopeId"<>:id',
                {"key": key, "id": request["targetId"]},
            )
            if conflict:
                raise HTTPException(409, f"成员集合与范围 {conflict['scopeId']} 重复，请先合并")
        previous = {
            "members": json.loads(target["members"]),
            "normalizedKey": target["normalizedKey"],
            "memberCount": target["memberCount"],
            "isOpen": target["isOpen"],
            "label": target["label"],
            "version": target["version"],
        }
        version = int(target["version"] or 1) + 1
        is_open = int(bool(draft.get("isOpen", bool(target["isOpen"]))))
        label = draft.get("label") or target["label"]
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'UPDATE "OntologyScope" SET "members"=:members,"normalizedKey"=:key,"memberCount"=:count,'
                '"isOpen"=:is_open,"label"=:label,"version"=:version,"updatedBy"=:actor,"updatedAt"=:moment '
                'WHERE "scopeId"=:id'
            ), {
                "members": json.dumps(normalized, ensure_ascii=False), "key": key,
                "count": len(normalized), "is_open": is_open,
                "label": label, "version": version,
                "actor": actor, "moment": utc_now(), "id": request["targetId"],
            })
        rules = self.db.fetch_one(
            'SELECT COUNT(*) AS total FROM "CompiledRule" WHERE "ruleExpression" LIKE :code',
            {"code": f"%{target['scopeCode']}%"},
        ) or {}
        impact = f"范围 {target['scopeCode']} 成员 {len(normalized)} 个，被 {int(rules.get('total') or 0)} 条规则引用"
        return previous, impact, {"scopeId": request["targetId"], "members": normalized, "version": version}

    def _apply_rule(self, request: dict[str, Any], draft: dict[str, Any], actor: str) -> tuple[dict, str, dict]:
        target = self.db.fetch_one(
            'SELECT * FROM "CompiledRule" WHERE "ruleId"=:id', {"id": request["targetId"]}
        )
        if not target:
            raise HTTPException(404, "规则不存在")
        values = {key: draft[key] for key in RULE_FIELDS if key in draft}
        if not values:
            raise HTTPException(422, f"规则变更仅支持 {RULE_FIELDS}")
        if isinstance(values.get("testcaseIds"), (list, tuple)):
            values["testcaseIds"] = json.dumps(list(values["testcaseIds"]), ensure_ascii=False)
        previous = {key: target.get(key) for key in RULE_FIELDS}
        with self.db.engine.begin() as conn:
            self.db.update("CompiledRule", request["targetId"], values, conn, actor=actor)
        cases = len(json.loads(values.get("testcaseIds") or target.get("testcaseIds") or "[]") or [])
        impact = f"规则 {request['targetId']} 更新字段 {sorted(values)}，测试用例 {cases} 条"
        return previous, impact, values

    def _apply_meta(self, request: dict[str, Any], draft: dict[str, Any], actor: str) -> tuple[dict, str, dict]:
        target = self.db.fetch_one(
            'SELECT * FROM "OntologyObjectType" WHERE "objectTypeId"=:id', {"id": request["targetId"]}
        )
        if not target:
            raise HTTPException(404, "对象类型不存在")
        values = {key: draft[key] for key in META_FIELDS if draft.get(key) is not None}
        if request["changeType"] == "ARCHIVE":
            values["status"] = "ARCHIVED"
        if not values:
            raise HTTPException(422, f"元数据变更仅支持 {META_FIELDS}")
        previous = {key: target.get(key) for key in META_FIELDS}
        assignments = ", ".join(f"{quote(key)}=:{key}" for key in values)
        with self.db.engine.begin() as conn:
            conn.execute(text(
                f'UPDATE "OntologyObjectType" SET {assignments}, "updatedAt"=:moment WHERE "objectTypeId"=:id'
            ), {**values, "moment": utc_now(), "id": request["targetId"]})
        impact = f"对象类型 {request['targetId']} 实例 {target.get('instanceCount') or 0} 条"
        return previous, impact, values

    # ---- 三类目标：回滚 ----

    def _restore_scope(self, request: dict[str, Any], previous: dict[str, Any]) -> None:
        if not previous or not request.get("targetId"):
            return
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'UPDATE "OntologyScope" SET "members"=:members,"normalizedKey"=:key,"memberCount"=:count,'
                '"isOpen"=:is_open,"label"=:label,"version"=:version,"updatedAt"=:moment WHERE "scopeId"=:id'
            ), {
                "members": json.dumps(previous.get("members") or [], ensure_ascii=False),
                "key": previous.get("normalizedKey"), "count": previous.get("memberCount") or 0,
                "is_open": int(previous.get("isOpen") or 0), "label": previous.get("label"),
                "version": previous.get("version") or 1, "moment": utc_now(), "id": request["targetId"],
            })

    def _restore_rule(self, request: dict[str, Any], previous: dict[str, Any]) -> None:
        if not previous:
            return
        values = {key: previous.get(key) for key in RULE_FIELDS if key in previous}
        if not values:
            return
        with self.db.engine.begin() as conn:
            self.db.update("CompiledRule", request["targetId"], values, conn, actor="rollback")

    def _restore_meta(self, request: dict[str, Any], previous: dict[str, Any]) -> None:
        if not previous:
            return
        values = {key: previous.get(key) for key in META_FIELDS if key in previous}
        if not values:
            return
        assignments = ", ".join(f"{quote(key)}=:{key}" for key in values)
        with self.db.engine.begin() as conn:
            conn.execute(text(
                f'UPDATE "OntologyObjectType" SET {assignments}, "updatedAt"=:moment WHERE "objectTypeId"=:id'
            ), {**values, "moment": utc_now(), "id": request["targetId"]})


class OpsScheduler:
    """运营巡检线程：启动即巡检一轮，之后按间隔轮询；单轮异常只记日志。"""

    def __init__(self, ops: OpsService, interval_minutes: int = 30):
        self.ops = ops
        self.interval_seconds = max(60, int(interval_minutes) * 60)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_checked: datetime | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="grain-oil-ops-scheduler", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception:
                logger.exception("运营巡检失败")
            self._stop_event.wait(60)

    def run_due(self, now: datetime | None = None) -> dict[str, Any] | None:
        current = now or datetime.now(timezone.utc)
        if self._last_checked and (current - self._last_checked).total_seconds() < self.interval_seconds:
            return None
        self._last_checked = current
        return self.ops.inspect("scheduler")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
