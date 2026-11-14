from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from fastapi import HTTPException

from .catalog import KNOWLEDGE_TYPES, MERGE_REFERENCES, MERGEABLE_TYPES, SPECS
from .db import Database, quote, utc_now
from .service import KnowledgeService


class GovernanceService:
    def __init__(self, db: Database, knowledge: KnowledgeService):
        self.db = db
        self.knowledge = knowledge

    def detect(self, actor: str) -> dict[str, int]:
        rows = self.db.fetch_all(
            'SELECT * FROM "IvdData" WHERE "status" NOT IN (\'已失效\',\'已融合\') '
            'ORDER BY "valueId"'
        )
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            key = (row["indicatorId"], row["regionId"], row.get("commodityId"), row["periodType"], row["period"])
            groups[key].append(row)
        issues: list[dict[str, Any]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            canonical = next((row for row in group if row["status"] == "已发布"), group[0])
            for duplicate in group:
                if duplicate["valueId"] == canonical["valueId"]:
                    continue
                issues.append({
                    "issueType": "重复", "primaryEntityType": "指标数据",
                    "primaryEntityId": canonical["valueId"], "duplicateEntityId": duplicate["valueId"],
                    "similarity": 1.0, "handleStatus": "待处理",
                    "handleRemark": "指标、地区、品种和统计期间完全一致",
                })
                if duplicate["value"] != canonical["value"] or duplicate.get("unit") != canonical.get("unit"):
                    issues.append({
                        "issueType": "冲突", "primaryEntityType": "指标数据",
                        "primaryEntityId": canonical["valueId"], "duplicateEntityId": duplicate["valueId"],
                        "similarity": 1.0, "conflictField": "value/unit",
                        "conflictValueA": f"{canonical['value']} {canonical.get('unit') or ''}".strip(),
                        "conflictValueB": f"{duplicate['value']} {duplicate.get('unit') or ''}".strip(),
                        "handleStatus": "待处理", "handleRemark": "同一统计键对应不同数值或单位",
                    })
        existing = self.db.fetch_all('SELECT "issueId" FROM "QisRecord"')
        numbers = [int(match.group(1)) for row in existing if (match := re.fullmatch(r"QIS(\d+)", row["issueId"]))]
        next_number = max(numbers, default=0) + 1
        duplicates = 0
        conflicts = 0
        with self.db.engine.begin() as conn:
            for issue in issues:
                found = self.db.fetch_one(
                    'SELECT "issueId" FROM "QisRecord" WHERE "issueType"=:issue_type AND '
                    '"primaryEntityId"=:primary_id AND "duplicateEntityId"=:duplicate_id',
                    {"issue_type": issue["issueType"], "primary_id": issue["primaryEntityId"],
                     "duplicate_id": issue["duplicateEntityId"]}, conn,
                )
                if found:
                    continue
                issue["issueId"] = f"QIS{next_number:04d}"
                next_number += 1
                self.db.insert("QisRecord", issue, conn, actor=actor)
                duplicates += issue["issueType"] == "重复"
                conflicts += issue["issueType"] == "冲突"
        return {"duplicates": duplicates, "conflicts": conflicts, "scanned": len(rows)}

    def records(self, issue_type: str | None = None) -> dict[str, Any]:
        if issue_type:
            rows = self.db.fetch_all(
                'SELECT * FROM "QisRecord" WHERE "issueType"=:issue_type ORDER BY "createdAt" DESC',
                {"issue_type": issue_type},
            )
        else:
            rows = self.db.fetch_all('SELECT * FROM "QisRecord" ORDER BY "createdAt" DESC')
        return {"total": len(rows), "items": rows}

    def merge(self, entity_type: str, source_id: str, target_id: str, similarity: float, actor: str) -> dict[str, Any]:
        if entity_type not in MERGEABLE_TYPES:
            raise HTTPException(400, "该实体类型不支持融合")
        if source_id == target_id:
            raise HTTPException(422, "融合源与目标不能相同")
        if not 0 <= similarity <= 1:
            raise HTTPException(422, "相似度必须在 0～1 之间")
        spec = SPECS[entity_type]
        self.knowledge.require(entity_type, source_id)
        self.knowledge.require(entity_type, target_id)
        affected: list[tuple[str, str]] = []
        with self.db.engine.begin() as conn:
            for table, field in MERGE_REFERENCES.get(entity_type, []):
                referenced_spec = next((item for item in SPECS.values() if item.table == table), None)
                if referenced_spec:
                    referenced_rows = self.db.fetch_all(
                        f"SELECT * FROM {quote(table)} WHERE {quote(field)}=:source_id",
                        {"source_id": source_id}, conn,
                    )
                    affected.extend(
                        (referenced_spec.entity_type, row[referenced_spec.pk]) for row in referenced_rows
                    )
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"UPDATE {quote(table)} SET {quote(field)}=:target_id WHERE {quote(field)}=:source_id"
                    ),
                    {"target_id": target_id, "source_id": source_id},
                )
            relations = self.db.fetch_all(
                'SELECT * FROM "Relation" WHERE ("subjectEntityType"=:entity_type AND "subjectEntityId"=:source_id) '
                'OR ("objectEntityType"=:entity_type AND "objectEntityId"=:source_id)',
                {"entity_type": entity_type, "source_id": source_id}, conn,
            )
            affected.extend(("知识关系", relation["relationId"]) for relation in relations)
            for relation in relations:
                values = {}
                if relation["subjectEntityType"] == entity_type and relation["subjectEntityId"] == source_id:
                    values["subjectEntityId"] = target_id
                if relation["objectEntityType"] == entity_type and relation["objectEntityId"] == source_id:
                    values["objectEntityId"] = target_id
                self.db.update("Relation", relation["relationId"], values, conn, actor=actor)
            self.db.update(spec.table, source_id, {"status": "已融合"}, conn, actor=actor)
            source = self.db.fetch_one(
                f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.pk)}=:id", {"id": source_id}, conn
            )
            self.db.log_change(conn, entity_type, source_id, "MERGED", {
                "mergedIntoId": target_id, "similarity": similarity, "status": "已融合",
            }, source["updatedAt"])
        self.knowledge._sync_graph(entity_type, source_id)
        for affected_type, affected_id in dict.fromkeys(affected):
            self.knowledge._sync_graph(affected_type, affected_id)
        self.knowledge._sync_graph(entity_type, target_id)
        return {"sourceEntityId": source_id, "targetEntityId": target_id, "status": "已融合"}

    def void_source(self, doc_id: str, actor: str) -> dict[str, Any]:
        document = self.knowledge.require("来源文档", doc_id)
        if document["status"] == "已作废":
            return {"docId": doc_id, "status": "已作废", "invalidated": 0}
        invalidated = 0
        invalidated_entities: list[tuple[str, str]] = []
        with self.db.engine.begin() as conn:
            self.db.update("SourceDoc", doc_id, {"status": "已作废"}, conn, actor=actor)
            source = self.db.fetch_one('SELECT * FROM "SourceDoc" WHERE "docId"=:id', {"id": doc_id}, conn)
            self.db.log_change(conn, "来源文档", doc_id, "VOID", {
                "status": "已作废", "previousStatus": document["status"],
            }, source["updatedAt"])
            for entity_type in KNOWLEDGE_TYPES:
                spec = SPECS[entity_type]
                rows = self.db.fetch_all(
                    f"SELECT * FROM {quote(spec.table)} WHERE \"sourceDocId\"=:doc_id "
                    f"AND {quote(spec.status_field)}<>'已失效'",
                    {"doc_id": doc_id}, conn,
                )
                for row in rows:
                    entity_id = row[spec.pk]
                    self.db.update(spec.table, entity_id, {spec.status_field: "已失效"}, conn, actor=actor)
                    saved = self.db.fetch_one(
                        f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.pk)}=:id", {"id": entity_id}, conn
                    )
                    self.db.log_change(conn, entity_type, entity_id, "INVALIDATE", {
                        "status": "已失效", "previousStatus": row[spec.status_field],
                        "sourceDocId": doc_id,
                    }, saved["updatedAt"])
                    invalidated += 1
                    invalidated_entities.append((entity_type, entity_id))
        self.knowledge._sync_graph("来源文档", doc_id)
        for entity_type, entity_id in invalidated_entities:
            self.knowledge._sync_graph(entity_type, entity_id)
        return {"docId": doc_id, "status": "已作废", "invalidated": invalidated}

    def restore_source(self, doc_id: str, actor: str) -> dict[str, Any]:
        """撤销作废：恢复来源文档及其派生的失效实体，并回写变更日志。"""
        document = self.knowledge.require("来源文档", doc_id)
        if document["status"] != "已作废":
            return {"docId": doc_id, "status": document["status"], "restored": 0}
        previous_status = self._previous_status("来源文档", doc_id) or "有效"
        restored = 0
        restored_entities: list[tuple[str, str]] = []
        with self.db.engine.begin() as conn:
            self.db.update("SourceDoc", doc_id, {"status": previous_status}, conn, actor=actor)
            source = self.db.fetch_one('SELECT * FROM "SourceDoc" WHERE "docId"=:id', {"id": doc_id}, conn)
            self.db.log_change(conn, "来源文档", doc_id, "RESTORE", {
                "status": previous_status, "previousStatus": "已作废",
            }, source["updatedAt"])
            for entity_type in KNOWLEDGE_TYPES:
                spec = SPECS[entity_type]
                rows = self.db.fetch_all(
                    f"SELECT * FROM {quote(spec.table)} WHERE \"sourceDocId\"=:doc_id "
                    f"AND {quote(spec.status_field)}='已失效'",
                    {"doc_id": doc_id}, conn,
                )
                for row in rows:
                    entity_id = row[spec.pk]
                    target = self._previous_status(entity_type, entity_id, conn) or "已发布"
                    self.db.update(spec.table, entity_id, {spec.status_field: target}, conn, actor=actor)
                    saved = self.db.fetch_one(
                        f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.pk)}=:id", {"id": entity_id}, conn
                    )
                    self.db.log_change(conn, entity_type, entity_id, "RESTORE", {
                        "status": target, "previousStatus": "已失效", "sourceDocId": doc_id,
                    }, saved["updatedAt"])
                    restored += 1
                    restored_entities.append((entity_type, entity_id))
        self.knowledge._sync_graph("来源文档", doc_id)
        for entity_type, entity_id in restored_entities:
            self.knowledge._sync_graph(entity_type, entity_id)
        return {"docId": doc_id, "status": previous_status, "restored": restored}

    def _previous_status(self, entity_type: str, entity_id: str, conn: Any = None) -> str | None:
        """从最近一次 INVALIDATE/VOID 日志中取回变更前状态。"""
        row = self.db.fetch_one(
            'SELECT "payload" FROM "ChangeLog" WHERE "entityType"=:t AND "entityId"=:i '
            'AND "changeType" IN (\'INVALIDATE\',\'VOID\') ORDER BY "updatedAt" DESC LIMIT 1',
            {"t": entity_type, "i": entity_id}, conn,
        )
        if not row:
            return None
        try:
            return json.loads(row["payload"]).get("previousStatus")
        except (TypeError, ValueError):
            return None
