from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text

from .catalog import REFERENCES, SPECS
from .db import Database, quote, utc_now


ENTITY_BY_TABLE = {spec.table: spec for spec in SPECS.values()}

# 谓词取自 catalog.REFERENCES，保证本体关系与既有图遍历语义一致
PREDICATES = {
    (entity_type, field): predicate
    for entity_type, references in REFERENCES.items()
    for field, _target, predicate in references
}

# Palantir 式动作类型：登记受控写入口，不复制业务实现
ACTIONS = [
    {
        "actionTypeId": "ACT-CREATE-KNOWLEDGE",
        "displayName": "创建知识记录",
        "targetType": "知识对象",
        "httpMethod": "POST",
        "path": "/api/knowledge/{entityType}",
        "requiredRole": "ROLE-02",
        "requiresApproval": 0,
        "description": "写入政策法规、指标数据、年鉴记录、农业情报或知识关系草稿",
    },
    {
        "actionTypeId": "ACT-SUBMIT-KNOWLEDGE",
        "displayName": "提交知识审核",
        "targetType": "知识对象",
        "httpMethod": "POST",
        "path": "/api/knowledge/{entityType}/{entityId}/submit",
        "requiredRole": "ROLE-02",
        "requiresApproval": 0,
        "description": "将草稿提交至审核队列",
    },
    {
        "actionTypeId": "ACT-REVIEW-KNOWLEDGE",
        "displayName": "审核知识记录",
        "targetType": "知识对象",
        "httpMethod": "POST",
        "path": "/api/knowledge/{entityType}/{entityId}/review",
        "requiredRole": "ROLE-03",
        "requiresApproval": 1,
        "description": "审核通过或驳回，审核通过后进入已发布状态",
    },
    {
        "actionTypeId": "ACT-CREATE-MASTER",
        "displayName": "登记主数据",
        "targetType": "主数据对象",
        "httpMethod": "POST",
        "path": "/api/master/{entityType}",
        "requiredRole": "ROLE-01",
        "requiresApproval": 0,
        "description": "登记品种、产品、地区、企业与本体概念",
    },
    {
        "actionTypeId": "ACT-UPLOAD-DOCUMENT",
        "displayName": "上传来源文档",
        "targetType": "来源文档",
        "httpMethod": "POST",
        "path": "/api/sources/upload",
        "requiredRole": "ROLE-02",
        "requiresApproval": 0,
        "description": "保存原件、切分段落并登记来源文档",
    },
    {
        "actionTypeId": "ACT-VOID-SOURCE",
        "displayName": "作废来源文档",
        "targetType": "来源文档",
        "httpMethod": "POST",
        "path": "/api/sources/{docId}/void",
        "requiredRole": "ROLE-01",
        "requiresApproval": 1,
        "description": "作废来源文档并移除其检索索引",
    },
    {
        "actionTypeId": "ACT-MERGE-MASTER",
        "displayName": "合并重复主数据",
        "targetType": "主数据对象",
        "httpMethod": "POST",
        "path": "/api/governance/merge",
        "requiredRole": "ROLE-01",
        "requiresApproval": 1,
        "description": "合并品种、地区、企业、产品与本体概念中的重复实体",
    },
]


class OntologyService:
    """Palantir 式操作本体：对象类型、属性、关系类型、动作类型。

    注册表由 seed/mapping.json 与 catalog 元数据编译而来，实例仍存放在原业务表，
    不复制数据、不改变既有读写路径。
    """

    def __init__(self, db: Database, seed_dir: Path):
        self.db = db
        self.seed_dir = Path(seed_dir)

    # ---------- 初始化与编译 ----------

    def initialize(self) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS "OntologyObjectType" ('
                '"objectTypeId" TEXT PRIMARY KEY, "displayName" TEXT NOT NULL, "entityType" TEXT, '
                '"description" TEXT, "aggregateRef" TEXT, "objectRole" TEXT NOT NULL, '
                '"primaryTable" TEXT NOT NULL, "primaryKey" TEXT NOT NULL, "domain" TEXT, '
                '"instanceCount" INTEGER DEFAULT 0, "status" TEXT DEFAULT \'ACTIVE\', "updatedAt" TEXT)'
            ))
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS "OntologyProperty" ('
                '"propertyId" TEXT PRIMARY KEY, "objectTypeId" TEXT NOT NULL, "column" TEXT NOT NULL, '
                '"attributeRef" TEXT, "displayName" TEXT NOT NULL, "dataType" TEXT, '
                '"requiredFlag" INTEGER DEFAULT 0, "uniqueFlag" INTEGER DEFAULT 0, '
                '"primaryKeyFlag" INTEGER DEFAULT 0, "linkFlag" INTEGER DEFAULT 0, '
                '"linkTypeId" TEXT, "systemFlag" INTEGER DEFAULT 0, "mappingNote" TEXT, '
                '"sortNo" INTEGER DEFAULT 0)'
            ))
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS "OntologyLinkType" ('
                '"linkTypeId" TEXT PRIMARY KEY, "displayName" TEXT NOT NULL, "sourceTypeId" TEXT NOT NULL, '
                '"targetTypeId" TEXT NOT NULL, "sourceProperty" TEXT, "targetProperty" TEXT, '
                '"cardinality" TEXT, "carrier" TEXT, "predicate" TEXT, "status" TEXT DEFAULT \'ACTIVE\')'
            ))
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS "OntologyActionType" ('
                '"actionTypeId" TEXT PRIMARY KEY, "displayName" TEXT NOT NULL, "targetType" TEXT, '
                '"httpMethod" TEXT, "path" TEXT, "requiredRole" TEXT, "requiresApproval" INTEGER DEFAULT 0, '
                '"description" TEXT, "status" TEXT DEFAULT \'ACTIVE\')'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_onto_property_type" ON "OntologyProperty" ("objectTypeId")'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_onto_link_source" ON "OntologyLinkType" ("sourceTypeId")'
            ))
        self.sync()

    def sync(self) -> dict[str, Any]:
        model = json.loads((self.seed_dir / "mapping.json").read_text(encoding="utf-8"))
        object_types: list[dict[str, Any]] = []
        properties: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []

        for mapping in model.get("mappings", []):
            for table_mapping in mapping.get("tableMappings", []):
                table = table_mapping["tableName"]
                spec = ENTITY_BY_TABLE.get(table)
                entity_type = spec.entity_type if spec else table
                is_master = table_mapping.get("tableRole") == "MASTER"
                domain = (spec.knowledge_type if spec and spec.knowledge_type else None) or (
                    "主数据" if is_master else mapping.get("objectName")
                )
                count = self.db.fetch_one(f'SELECT COUNT(*) AS total FROM {quote(table)}') or {}
                object_types.append({
                    "objectTypeId": table,
                    "displayName": entity_type,
                    "entityType": entity_type,
                    "description": f"{mapping.get('objectName', '')} · {table_mapping.get('tableRole', '')}",
                    "aggregateRef": mapping.get("objectRef"),
                    "objectRole": "AGGREGATE_ROOT" if is_master else "DETAIL",
                    "primaryTable": table,
                    "primaryKey": table_mapping.get("primaryKey") or (spec.pk if spec else ""),
                    "domain": domain,
                    "instanceCount": int(count.get("total") or 0),
                    "status": "ACTIVE",
                    "updatedAt": utc_now(),
                })

                for index, column in enumerate(table_mapping.get("columnMappings", [])):
                    properties.append({
                        "propertyId": f"{table}.{column['column']}",
                        "objectTypeId": table,
                        "column": column["column"],
                        "attributeRef": column.get("attributeRef"),
                        "displayName": column["column"],
                        "dataType": column.get("columnType"),
                        "requiredFlag": 0 if column.get("nullable", True) else 1,
                        "uniqueFlag": 1 if column.get("unique") else 0,
                        "primaryKeyFlag": 1 if column.get("isPrimaryKey") else 0,
                        "linkFlag": 1 if column.get("isForeignKey") else 0,
                        "linkTypeId": None,
                        "systemFlag": 0,
                        "mappingNote": column.get("mappingNote"),
                        "sortNo": index,
                    })
                    target_table = column.get("fkTargetTable")
                    if column.get("isForeignKey") and target_table:
                        links.append(self._link(
                            table, column["column"], target_table, column.get("fkTargetColumn", ""),
                            entity_type, column.get("unique") or column.get("isPrimaryKey"),
                        ))

                if is_master:
                    for offset, column in enumerate(["createdBy", "createdAt", "updatedBy", "updatedAt"]):
                        properties.append({
                            "propertyId": f"{table}.{column}",
                            "objectTypeId": table,
                            "column": column,
                            "attributeRef": None,
                            "displayName": column,
                            "dataType": "TEXT",
                            "requiredFlag": 0,
                            "uniqueFlag": 0,
                            "primaryKeyFlag": 0,
                            "linkFlag": 0,
                            "linkTypeId": None,
                            "systemFlag": 1,
                            "mappingNote": "审计列",
                            "sortNo": 1000 + offset,
                        })

                for foreign_key in table_mapping.get("foreignKeys", []):
                    links.append(self._link(
                        table, foreign_key.get("localColumn", ""), foreign_key.get("targetTable", ""),
                        foreign_key.get("targetColumn", ""), entity_type, False,
                    ))

        unique_links = {item["linkTypeId"]: item for item in links if item["linkTypeId"]}
        property_to_link = {
            f"{item['sourceTypeId']}.{item['sourceProperty']}": item["linkTypeId"]
            for item in unique_links.values()
        }
        for item in properties:
            item["linkTypeId"] = property_to_link.get(item["propertyId"])

        with self.db.engine.begin() as conn:
            conn.execute(text('DELETE FROM "OntologyProperty"'))
            conn.execute(text('DELETE FROM "OntologyLinkType"'))
            conn.execute(text('DELETE FROM "OntologyActionType"'))
            for item in object_types:
                conn.execute(text(
                    'INSERT INTO "OntologyObjectType" ("objectTypeId","displayName","entityType","description",'
                    '"aggregateRef","objectRole","primaryTable","primaryKey","domain","instanceCount","status","updatedAt") '
                    'VALUES (:objectTypeId,:displayName,:entityType,:description,:aggregateRef,:objectRole,'
                    ':primaryTable,:primaryKey,:domain,:instanceCount,:status,:updatedAt) '
                    'ON CONFLICT ("objectTypeId") DO UPDATE SET "displayName"=excluded."displayName",'
                    '"entityType"=excluded."entityType","description"=excluded."description",'
                    '"aggregateRef"=excluded."aggregateRef","objectRole"=excluded."objectRole",'
                    '"primaryTable"=excluded."primaryTable","primaryKey"=excluded."primaryKey",'
                    '"domain"=excluded."domain","instanceCount"=excluded."instanceCount",'
                    '"status"=excluded."status","updatedAt"=excluded."updatedAt"'
                ), item)
            for item in properties:
                conn.execute(text(
                    'INSERT INTO "OntologyProperty" ("propertyId","objectTypeId","column","attributeRef",'
                    '"displayName","dataType","requiredFlag","uniqueFlag","primaryKeyFlag","linkFlag",'
                    '"linkTypeId","systemFlag","mappingNote","sortNo") VALUES (:propertyId,:objectTypeId,:column,'
                    ':attributeRef,:displayName,:dataType,:requiredFlag,:uniqueFlag,:primaryKeyFlag,:linkFlag,'
                    ':linkTypeId,:systemFlag,:mappingNote,:sortNo)'
                ), item)
            for item in unique_links.values():
                conn.execute(text(
                    'INSERT INTO "OntologyLinkType" ("linkTypeId","displayName","sourceTypeId","targetTypeId",'
                    '"sourceProperty","targetProperty","cardinality","carrier","predicate","status") '
                    'VALUES (:linkTypeId,:displayName,:sourceTypeId,:targetTypeId,:sourceProperty,'
                    ':targetProperty,:cardinality,:carrier,:predicate,:status)'
                ), item)
            for item in ACTIONS:
                conn.execute(text(
                    'INSERT INTO "OntologyActionType" ("actionTypeId","displayName","targetType","httpMethod",'
                    '"path","requiredRole","requiresApproval","description","status") '
                    'VALUES (:actionTypeId,:displayName,:targetType,:httpMethod,:path,:requiredRole,'
                    ':requiresApproval,:description,:status)'
                ), {**item, "status": "ACTIVE"})

        self._replay_evolution_overrides()
        return self.stats()

    def _replay_evolution_overrides(self) -> None:
        """回放已发布的 ONTOLOGY_META 演进，避免元数据被定义层同步覆盖。"""
        try:
            rows = self.db.fetch_all(
                'SELECT "changeId","targetId","draftPayload" FROM "OpsEvolutionRequest" '
                'WHERE "targetKind"=\'ONTOLOGY_META\' AND "status"=\'PUBLISHED\' ORDER BY "publishedAt"'
            )
        except Exception:
            return
        for row in rows:
            if not row.get("targetId"):
                continue
            try:
                payload = json.loads(row["draftPayload"] or "{}")
            except ValueError:
                continue
            values = {
                key: payload[key]
                for key in ("displayName", "description", "status")
                if payload.get(key) is not None
            }
            if not values:
                continue
            assignments = ", ".join(f"{quote(key)}=:{key}" for key in values)
            with self.db.engine.begin() as conn:
                conn.execute(text(
                    f'UPDATE "OntologyObjectType" SET {assignments}, "updatedAt"=:moment '
                    'WHERE "objectTypeId"=:id'
                ), {**values, "moment": utc_now(), "id": row["targetId"]})

    def _link(self, source_table: str, source_column: str, target_table: str,
              target_column: str, entity_type: str, unique: Any) -> dict[str, Any]:
        if not source_column or not target_table:
            return {}
        predicate = PREDICATES.get((entity_type, source_column), source_column)
        return {
            "linkTypeId": f"LK-{source_table}-{source_column}",
            "displayName": predicate,
            "sourceTypeId": source_table,
            "targetTypeId": target_table,
            "sourceProperty": source_column,
            "targetProperty": target_column,
            "cardinality": "ONE_TO_ONE" if unique else "MANY_TO_ONE",
            "carrier": "FOREIGN_KEY",
            "predicate": predicate,
            "status": "ACTIVE",
        }

    # ---------- 查询 ----------

    def stats(self) -> dict[str, Any]:
        def total(table: str) -> int:
            row = self.db.fetch_one(f'SELECT COUNT(*) AS total FROM {quote(table)}') or {}
            return int(row.get("total") or 0)

        instances = self.db.fetch_one(
            'SELECT COALESCE(SUM("instanceCount"),0) AS total FROM "OntologyObjectType"'
        ) or {}
        return {
            "objectTypes": total("OntologyObjectType"),
            "properties": total("OntologyProperty"),
            "linkTypes": total("OntologyLinkType"),
            "actionTypes": total("OntologyActionType"),
            "instances": int(instances.get("total") or 0),
        }

    def object_types(self, keyword: str | None = None) -> list[dict[str, Any]]:
        sql = 'SELECT * FROM "OntologyObjectType"'
        params: dict[str, Any] = {}
        if keyword:
            sql += ' WHERE "displayName" LIKE :keyword OR "objectTypeId" LIKE :keyword OR "domain" LIKE :keyword'
            params["keyword"] = f"%{keyword}%"
        sql += ' ORDER BY CASE "objectRole" WHEN \'AGGREGATE_ROOT\' THEN 0 ELSE 1 END, "objectTypeId"'
        return self.db.fetch_all(sql, params)

    def object_type(self, object_type_id: str) -> dict[str, Any]:
        row = self.db.fetch_one(
            'SELECT * FROM "OntologyObjectType" WHERE "objectTypeId"=:id', {"id": object_type_id}
        )
        if not row:
            raise HTTPException(404, f"对象类型不存在：{object_type_id}")
        properties = self.db.fetch_all(
            'SELECT * FROM "OntologyProperty" WHERE "objectTypeId"=:id ORDER BY "sortNo"',
            {"id": object_type_id},
        )
        outgoing = self.db.fetch_all(
            'SELECT * FROM "OntologyLinkType" WHERE "sourceTypeId"=:id ORDER BY "linkTypeId"',
            {"id": object_type_id},
        )
        incoming = self.db.fetch_all(
            'SELECT * FROM "OntologyLinkType" WHERE "targetTypeId"=:id ORDER BY "linkTypeId"',
            {"id": object_type_id},
        )
        actions = self.db.fetch_all(
            'SELECT * FROM "OntologyActionType" WHERE "targetType"=:name OR "targetType" IN '
            '(\'知识对象\',\'主数据对象\') ORDER BY "actionTypeId"',
            {"name": row["displayName"]},
        )
        return {**row, "properties": properties, "outgoingLinks": outgoing,
                "incomingLinks": incoming, "actions": actions}

    def link_types(self) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            'SELECT l.*, s."displayName" AS "sourceName", t."displayName" AS "targetName" '
            'FROM "OntologyLinkType" l '
            'LEFT JOIN "OntologyObjectType" s ON s."objectTypeId"=l."sourceTypeId" '
            'LEFT JOIN "OntologyObjectType" t ON t."objectTypeId"=l."targetTypeId" '
            'ORDER BY l."sourceTypeId", l."linkTypeId"'
        )

    def action_types(self) -> list[dict[str, Any]]:
        return self.db.fetch_all('SELECT * FROM "OntologyActionType" ORDER BY "actionTypeId"')

    def graph(self) -> dict[str, Any]:
        nodes = []
        for row in self.object_types():
            nodes.append({
                "id": row["objectTypeId"],
                "label": row["displayName"],
                "role": row["objectRole"],
                "domain": row.get("domain"),
                "instanceCount": row.get("instanceCount") or 0,
                "size": 18 + min(42, int((row.get("instanceCount") or 0) ** 0.5)),
            })
        edges = [
            {
                "id": row["linkTypeId"],
                "source": row["sourceTypeId"],
                "target": row["targetTypeId"],
                "label": row["displayName"],
                "cardinality": row["cardinality"],
            }
            for row in self.link_types()
        ]
        return {"nodes": nodes, "edges": edges}

    def objects(self, object_type_id: str, keyword: str | None = None,
                page_no: int = 1, page_size: int = 20) -> dict[str, Any]:
        row = self.db.fetch_one(
            'SELECT * FROM "OntologyObjectType" WHERE "objectTypeId"=:id', {"id": object_type_id}
        )
        if not row:
            raise HTTPException(404, f"对象类型不存在：{object_type_id}")
        spec = ENTITY_BY_TABLE.get(row["primaryTable"])
        if not spec:
            raise HTTPException(422, f"对象类型缺少实体定义：{object_type_id}")
        table = quote(spec.table)
        pk = quote(spec.pk)
        where = ""
        params: dict[str, Any] = {}
        if keyword:
            field = spec.title_field or spec.pk
            where = f" WHERE {quote(field)} LIKE :keyword"
            params["keyword"] = f"%{keyword}%"
        count = self.db.fetch_one(f"SELECT COUNT(*) AS total FROM {table}{where}", params) or {}
        params.update({"limit": page_size, "offset": (page_no - 1) * page_size})
        items = self.db.fetch_all(
            f"SELECT * FROM {table}{where} ORDER BY {pk} LIMIT :limit OFFSET :offset", params
        )
        result = []
        for item in items:
            result.append({
                "objectId": item.get(spec.pk),
                "title": item.get(spec.title_field) if spec.title_field else item.get(spec.pk),
                "status": item.get(spec.status_field) if spec.status_field else None,
                "properties": item,
            })
        return {"objectTypeId": object_type_id, "total": int(count.get("total") or 0),
                "pageNo": page_no, "pageSize": page_size, "items": result}
