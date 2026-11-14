from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from fastapi import HTTPException

from .catalog import KNOWLEDGE_TYPES, REFERENCES, SPECS, EntitySpec
from .db import Database, quote, utc_now


class KnowledgeService:
    def __init__(self, db: Database, graph_store: Any = None):
        self.db = db
        self.graph_store = graph_store

    def _sync_graph(self, entity_type: str, entity_id: str) -> None:
        if self.graph_store is not None:
            self.graph_store.publish_entity(entity_type, entity_id)

    def spec(self, entity_type: str) -> EntitySpec:
        spec = SPECS.get(entity_type)
        if not spec:
            raise HTTPException(404, f"未知实体类型：{entity_type}")
        return spec

    def get(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        spec = self.spec(entity_type)
        return self.db.fetch_one(
            f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.pk)}=:entity_id",
            {"entity_id": entity_id},
        )

    def require(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        row = self.get(entity_type, entity_id)
        if not row:
            raise HTTPException(404, f"实体不存在：{entity_type}/{entity_id}")
        return row

    def title(self, entity_type: str, row: dict[str, Any]) -> str:
        spec = self.spec(entity_type)
        if spec.title_field and row.get(spec.title_field):
            return str(row[spec.title_field])
        if entity_type in {"指标数据", "年鉴记录"}:
            indicator = self.get("指标定义", row["indicatorId"]) or {}
            region = self.get("地区", row["regionId"]) or {}
            period = str(row.get("period") or row.get("statYear") or "")
            if period.isdigit():
                period += "年"
            return f"{period}{region.get('regionName', '')}{indicator.get('indicatorName', '')}"
        if entity_type == "知识关系":
            subject = self.get(row["subjectEntityType"], row["subjectEntityId"])
            object_row = self.get(row["objectEntityType"], row["objectEntityId"])
            subject_title = self.title(row["subjectEntityType"], subject) if subject else row["subjectEntityId"]
            object_title = self.title(row["objectEntityType"], object_row) if object_row else row["objectEntityId"]
            return f"{subject_title} —{row['predicate']}→ {object_title}"
        return str(row.get(spec.pk, ""))

    def status(self, entity_type: str, row: dict[str, Any]) -> str | None:
        field = self.spec(entity_type).status_field
        return row.get(field) if field else None

    def category(self, row: dict[str, Any]) -> dict[str, Any]:
        category_id = row.get("knowledgeCategoryId")
        if not category_id:
            return {}
        return self.db.fetch_one(
            'SELECT * FROM "KtcCategory" WHERE "categoryId"=:id', {"id": category_id}
        ) or {}

    def sources(self, entity_type: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        if entity_type == "来源文档":
            doc_id = row["docId"]
            segment = self.db.fetch_one(
                'SELECT * FROM "SourceDoc_Segment" WHERE "docId"=:id ORDER BY "segmentNo" LIMIT 1',
                {"id": doc_id},
            )
        else:
            doc_id = row.get("sourceDocId")
            segment_id = row.get("segmentId")
            segment = None
            if segment_id:
                segment = self.db.fetch_one(
                    'SELECT * FROM "SourceDoc_Segment" WHERE "segmentId"=:id', {"id": segment_id}
                )
            if not segment and doc_id:
                segment = self.db.fetch_one(
                    'SELECT * FROM "SourceDoc_Segment" WHERE "docId"=:id ORDER BY "segmentNo" LIMIT 1',
                    {"id": doc_id},
                )
        if not doc_id or not segment:
            return []
        document = self.get("来源文档", doc_id)
        if not document:
            return []
        return [{
            "sourceDocId": doc_id,
            "docTitle": document["title"],
            "sourceUrl": document.get("sourceUrl"),
            "issuer": document.get("issuer"),
            "segmentId": segment["segmentId"],
            "pageNo": segment.get("pageNo"),
            "chapterPath": segment.get("chapterPath"),
            "charStart": segment["charStart"],
            "charEnd": segment["charEnd"],
            "segmentText": segment["segmentText"],
        }]

    def detail(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        row = self.require(entity_type, entity_id)
        return {
            "entityType": entity_type,
            "entityId": entity_id,
            "title": self.title(entity_type, row),
            "status": self.status(entity_type, row),
            "properties": row,
            "category": self.category(row),
            "relations": self.one_hop(entity_type, entity_id),
            "sources": self.sources(entity_type, row),
        }

    def _visible(self, entity_type: str, row: dict[str, Any] | None) -> bool:
        if not row:
            return False
        if entity_type in KNOWLEDGE_TYPES:
            return self.status(entity_type, row) == "已发布"
        return self.status(entity_type, row) not in {"已作废", "已失效", "已融合"}

    def one_hop(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        current = self.require(entity_type, entity_id)
        if not self._visible(entity_type, current) and entity_type in KNOWLEDGE_TYPES:
            # 草稿和失效知识仍可在详情中查看，但不向图遍历泄露其关系。
            return []
        relations: list[dict[str, Any]] = []

        def add(source_type: str, source_id: str, predicate: str, target_type: str,
                target_id: str, kind: str, relation_id: str, confidence: float | None = None) -> None:
            target = self.get(target_type, target_id)
            source = self.get(source_type, source_id)
            if not self._visible(target_type, target) or not self._visible(source_type, source):
                return
            relations.append({
                "relationId": relation_id,
                "sourceEntityType": source_type,
                "sourceEntityId": source_id,
                "targetEntityType": target_type,
                "targetEntityId": target_id,
                "predicate": predicate,
                "kind": kind,
                "confidence": confidence,
            })

        if entity_type != "知识关系":
            for field, target_type, predicate in REFERENCES.get(entity_type, []):
                target_id = current.get(field)
                if target_id:
                    add(entity_type, entity_id, predicate, target_type, target_id,
                        "结构关系", f"STRUCT-{entity_type}-{entity_id}-{field}")

        for other_type, references in REFERENCES.items():
            if other_type == "知识关系":
                continue
            other_spec = self.spec(other_type)
            for field, target_type, predicate in references:
                if target_type != entity_type:
                    continue
                rows = self.db.fetch_all(
                    f"SELECT * FROM {quote(other_spec.table)} WHERE {quote(field)}=:id",
                    {"id": entity_id},
                )
                for row in rows:
                    add(other_type, row[other_spec.pk], predicate, entity_type, entity_id,
                        "结构关系", f"STRUCT-{other_type}-{row[other_spec.pk]}-{field}")

        relation_rows = self.db.fetch_all(
            'SELECT * FROM "Relation" WHERE "status"=:published AND '
            '(("subjectEntityType"=:entity_type AND "subjectEntityId"=:entity_id) OR '
            '("objectEntityType"=:entity_type AND "objectEntityId"=:entity_id))',
            {"published": "已发布", "entity_type": entity_type, "entity_id": entity_id},
        )
        for relation in relation_rows:
            add(relation["subjectEntityType"], relation["subjectEntityId"], relation["predicate"],
                relation["objectEntityType"], relation["objectEntityId"], "知识三元组",
                relation["relationId"], relation.get("confidence"))
        unique = {item["relationId"]: item for item in relations}
        return list(unique.values())

    def _locate_entity(self, entity_id: str) -> tuple[str, dict[str, Any]] | None:
        for candidate_type, candidate_spec in SPECS.items():
            if candidate_spec.id_prefix and entity_id.startswith(candidate_spec.id_prefix):
                row = self.get(candidate_type, entity_id)
                if row:
                    return candidate_type, row
        for candidate_type in SPECS:
            row = self.get(candidate_type, entity_id)
            if row:
                return candidate_type, row
        return None

    def _nebula_graph(self, entity_type: str, entity_id: str, depth: int,
                      start: dict[str, Any]) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {
            entity_id: {
                "id": entity_id, "entityType": entity_type,
                "label": self.title(entity_type, start), "status": self.status(entity_type, start),
            }
        }
        edges: dict[str, dict[str, Any]] = {}
        predicates = {
            "has_segment": "包含段落", "has_clause": "包含条款", "has_synonym": "包含同义词",
            "has_run": "采集执行", "produced_by_crawl": "采集来源", "from_doc": "来源文档",
            "in_category": "所属分类", "in_region": "涉及地区", "applies_to_region": "适用地区",
            "of_indicator": "对应指标", "of_commodity": "涉及品种", "processed_from": "原料品种",
            "made_by": "生产企业", "located_in": "所在地区", "parent_of": "上级实体",
            "maps_to_concept": "映射概念",
        }
        for raw_edge in self.graph_store.expand_ids(entity_id, depth):
            source_id = str(raw_edge["source"])
            target_id = str(raw_edge["target"])
            for node_id in (source_id, target_id):
                if node_id in nodes:
                    continue
                located = self._locate_entity(node_id)
                if located:
                    node_type, row = located
                    nodes[node_id] = {
                        "id": node_id, "entityType": node_type, "label": self.title(node_type, row),
                        "status": self.status(node_type, row),
                    }
            if source_id not in nodes or target_id not in nodes:
                continue
            edge_type = str(raw_edge["edgeType"])
            properties = raw_edge.get("properties") or {}
            relation_id = str(properties.get("relationId") or f"NEBULA-{source_id}-{edge_type}-{target_id}")
            edges[relation_id] = {
                "id": relation_id, "source": source_id, "target": target_id,
                "predicate": properties.get("predicate") or predicates.get(edge_type, edge_type),
                "kind": "知识三元组" if edge_type == "kg_relation" else "结构关系",
                "confidence": properties.get("confidence"),
            }
        return {"nodes": list(nodes.values()), "edges": list(edges.values()), "depth": depth}

    def graph(self, entity_type: str, entity_id: str, depth: int) -> dict[str, Any]:
        start = self.require(entity_type, entity_id)
        if self.graph_store is not None:
            return self._nebula_graph(entity_type, entity_id, depth, start)
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}

        def add_node(node_type: str, node_id: str) -> None:
            if node_id in nodes:
                return
            row = self.get(node_type, node_id)
            if row:
                nodes[node_id] = {
                    "id": node_id,
                    "entityType": node_type,
                    "label": self.title(node_type, row),
                    "status": self.status(node_type, row),
                }

        add_node(entity_type, entity_id)
        frontier = [(entity_type, entity_id)]
        for _ in range(depth):
            next_frontier = []
            for current_type, current_id in frontier:
                for relation in self.one_hop(current_type, current_id):
                    source_id = relation["sourceEntityId"]
                    target_id = relation["targetEntityId"]
                    add_node(relation["sourceEntityType"], source_id)
                    add_node(relation["targetEntityType"], target_id)
                    edges[relation["relationId"]] = {
                        "id": relation["relationId"],
                        "source": source_id,
                        "target": target_id,
                        "predicate": relation["predicate"],
                        "kind": relation["kind"],
                        "confidence": relation.get("confidence"),
                    }
                    neighbor_type = relation["targetEntityType"] if source_id == current_id else relation["sourceEntityType"]
                    neighbor_id = target_id if source_id == current_id else source_id
                    if neighbor_id not in {item[1] for item in next_frontier}:
                        next_frontier.append((neighbor_type, neighbor_id))
            frontier = next_frontier
        return {"nodes": list(nodes.values()), "edges": list(edges.values()), "depth": depth}

    def taxonomy_descendants(self, category_code: str) -> set[str]:
        categories = self.db.fetch_all('SELECT "categoryId","categoryCode","parentCategoryId" FROM "KtcCategory"')
        root = next((row for row in categories if row["categoryCode"] == category_code), None)
        if not root:
            raise HTTPException(400, f"未知分类编码：{category_code}")
        children: dict[str, list[str]] = {}
        for row in categories:
            if row["parentCategoryId"]:
                children.setdefault(row["parentCategoryId"], []).append(row["categoryId"])
        result = {root["categoryId"]}
        frontier = [root["categoryId"]]
        while frontier:
            current = frontier.pop()
            for child in children.get(current, []):
                if child not in result:
                    result.add(child)
                    frontier.append(child)
        return result

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        keyword = str(params.get("keyword") or "").strip().casefold()
        category_ids = self.taxonomy_descendants(params["categoryCode"]) if params.get("categoryCode") else None
        knowledge_type = params.get("knowledgeType")
        expected_types = []
        if knowledge_type:
            expected_types = [name for name in KNOWLEDGE_TYPES if name == knowledge_type or self.spec(name).knowledge_type == knowledge_type]
            if not expected_types:
                raise HTTPException(400, "知识类别无效")
        candidates = []
        for entity_type in expected_types or list(KNOWLEDGE_TYPES):
            spec = self.spec(entity_type)
            rows = self.db.fetch_all(
                f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.status_field)}=:status",
                {"status": "已发布"},
            )
            for row in rows:
                if category_ids is not None and row.get("knowledgeCategoryId") not in category_ids:
                    continue
                if params.get("industryStage") and row.get("industryStage") != params["industryStage"]:
                    continue
                factors = set(filter(None, str(row.get("factorDimensions") or "").split(",")))
                requested_factors = set(filter(None, str(params.get("factorDimension") or "").split(",")))
                if requested_factors and not requested_factors.issubset(factors):
                    continue
                if params.get("commodityCode"):
                    commodity = self.db.fetch_one(
                        'SELECT "commodityId" FROM "CmdCommodity" WHERE "commodityCode"=:code',
                        {"code": params["commodityCode"]},
                    )
                    if not commodity or row.get("commodityId") != commodity["commodityId"]:
                        continue
                if params.get("regionCode"):
                    region = self.db.fetch_one(
                        'SELECT "regionId" FROM "Region" WHERE "regionCode"=:code',
                        {"code": params["regionCode"]},
                    )
                    if not region or row.get("regionId") != region["regionId"]:
                        continue
                title = self.title(entity_type, row)
                haystack = (title + " " + json.dumps(row, ensure_ascii=False, default=str)).casefold()
                if keyword and keyword not in haystack:
                    continue
                category = self.category(row)
                date_value = str(row.get("period") or row.get("effectiveDate") or row.get("publishDate") or row.get("statYear") or "")
                if params.get("dateFrom") and date_value < params["dateFrom"]:
                    continue
                if params.get("dateTo") and date_value > params["dateTo"]:
                    continue
                score = 1.0 if keyword and keyword in title.casefold() else (0.7 if keyword else 0.5)
                candidates.append({
                    "entityType": entity_type,
                    "entityId": row[spec.pk],
                    "title": title,
                    "knowledgeType": spec.knowledge_type or entity_type,
                    "status": "已发布",
                    "score": score,
                    "categoryId": category.get("categoryId"),
                    "categoryCode": category.get("categoryCode"),
                    "categoryName": category.get("categoryName"),
                    "categoryPath": category.get("categoryPath") or "",
                    "industryStage": row.get("industryStage"),
                    "factorDimensions": row.get("factorDimensions") or category.get("factorDimensions") or "",
                    "period": row.get("period"),
                    "regionId": row.get("regionId"),
                    "commodityId": row.get("commodityId"),
                })
        candidates.sort(key=lambda item: (-item["score"], item["entityType"], item["entityId"]))
        category_counts = Counter((item["categoryCode"], item["categoryName"]) for item in candidates if item["categoryCode"])
        industry_counts = Counter(item["industryStage"] for item in candidates if item["industryStage"])
        factor_counts = Counter(
            factor for item in candidates for factor in item["factorDimensions"].split(",") if factor
        )
        page_no = params.get("pageNo", 1)
        page_size = params.get("pageSize", 20)
        start = (page_no - 1) * page_size
        return {
            "total": len(candidates),
            "pageNo": page_no,
            "pageSize": page_size,
            "items": candidates[start:start + page_size],
            "facets": {
                "category": [{"value": key[0], "label": key[1], "count": count} for key, count in category_counts.most_common()],
                "industryStage": [{"value": key, "label": key, "count": count} for key, count in industry_counts.most_common()],
                "factorDimension": [{"value": key, "label": key, "count": count} for key, count in factor_counts.most_common()],
            },
        }

    def taxonomy(self, page_no: int, page_size: int) -> dict[str, Any]:
        rows = self.db.fetch_all('SELECT * FROM "KtcCategory" ORDER BY "level","sortNo","categoryId"')
        start = (page_no - 1) * page_size
        return {"total": len(rows), "items": rows[start:start + page_size]}

    def stats(self) -> dict[str, Any]:
        entities = 0
        for entity_type, spec in SPECS.items():
            if entity_type in {"文档段落", "政策条款", "采集执行记录", "质量治理记录", "知识关系"}:
                continue
            entities += self.db.fetch_one(f"SELECT COUNT(*) AS count FROM {quote(spec.table)}")["count"]
        return {
            "entities": entities,
            "relations": self.db.fetch_one('SELECT COUNT(*) AS count FROM "Relation" WHERE "status"=\'已发布\'')["count"],
            "documents": self.db.fetch_one('SELECT COUNT(*) AS count FROM "SourceDoc"')["count"],
            "categories": self.db.fetch_one('SELECT COUNT(*) AS count FROM "KtcCategory"')["count"],
        }

    def changes(self, since: str, limit: int, entity_type: str | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", since):
            raise HTTPException(400, "since 必须为 UTC ISO 8601 时间")
        sql = 'SELECT * FROM "ChangeLog" WHERE "updatedAt">:since'
        params: dict[str, Any] = {"since": since, "limit": limit}
        if entity_type:
            self.spec(entity_type)
            sql += ' AND "entityType"=:entity_type'
            params["entity_type"] = entity_type
        sql += ' ORDER BY "updatedAt","changeId" LIMIT :limit'
        rows = self.db.fetch_all(sql, params)
        changes = [{
            "changeId": row["changeId"],
            "entityType": row["entityType"],
            "entityId": row["entityId"],
            "changeType": row["changeType"],
            "updatedAt": row["updatedAt"],
            "payload": json.loads(row["payload"]),
        } for row in rows]
        return {"changes": changes, "nextSince": max((row["updatedAt"] for row in rows), default=since)}

    def list_knowledge(self, entity_type: str, status: str | None = None,
                       keyword: str | None = None, page_no: int = 1,
                       page_size: int = 20) -> dict[str, Any]:
        if entity_type not in KNOWLEDGE_TYPES:
            raise HTTPException(400, "仅支持四类知识与知识关系")
        spec = self.spec(entity_type)
        sql = f"SELECT * FROM {quote(spec.table)}"
        params: dict[str, Any] = {}
        if status:
            sql += f" WHERE {quote(spec.status_field)}=:status"
            params["status"] = status
        sql += f' ORDER BY "updatedAt" DESC'
        haystack_keyword = str(keyword or "").strip().casefold()
        items: list[dict[str, Any]] = []
        for row in self.db.fetch_all(sql, params):
            title = self.title(entity_type, row)
            if haystack_keyword and haystack_keyword not in f"{title} {row[spec.pk]}".casefold():
                continue
            items.append({
                "entityType": entity_type,
                "entityId": row[spec.pk],
                "title": title,
                "status": self.status(entity_type, row),
                "sourceDocId": row.get("sourceDocId"),
                "segmentId": row.get("segmentId"),
                "updatedAt": row.get("updatedAt"),
            })
        start = (page_no - 1) * page_size
        return {"total": len(items), "items": items[start:start + page_size]}

    def list_tasks(self, status: str = "待审核", entity_type: str | None = None,
                   keyword: str | None = None, page_no: int = 1,
                   page_size: int = 50) -> dict[str, Any]:
        types = [entity_type] if entity_type else list(KNOWLEDGE_TYPES)
        items: list[dict[str, Any]] = []
        for candidate in types:
            if candidate not in KNOWLEDGE_TYPES:
                continue
            items.extend(self.list_knowledge(candidate, status, keyword, 1, 100000)["items"])
        items.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
        start = (page_no - 1) * page_size
        return {"total": len(items), "items": items[start:start + page_size]}

    def list_entities(self, entity_type: str, keyword: str | None = None,
                      limit: int = 500) -> list[dict[str, Any]]:
        spec = self.spec(entity_type)
        rows = self.db.fetch_all(f"SELECT * FROM {quote(spec.table)}")
        if entity_type in KNOWLEDGE_TYPES:
            rows = [row for row in rows if self.status(entity_type, row) not in {None, "已作废", "已失效", "已融合"}]
        keyword_text = str(keyword or "").strip().casefold()
        items: list[dict[str, Any]] = []
        for row in rows:
            title = self.title(entity_type, row)
            if keyword_text and keyword_text not in f"{title} {row[spec.pk]}".casefold():
                continue
            items.append({
                "entityId": row[spec.pk],
                "title": title,
                "status": self.status(entity_type, row),
            })
            if len(items) >= limit:
                break
        return items

    def create_knowledge(self, entity_type: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        if entity_type not in KNOWLEDGE_TYPES:
            raise HTTPException(400, "仅支持四类知识与知识关系登记")
        spec = self.spec(entity_type)
        row = dict(payload)
        entity_id = self.db.next_id(spec.table, spec.id_prefix)
        row[spec.pk] = entity_id
        if entity_type == "知识关系":
            row.setdefault("status", "草稿")
            row.setdefault("extractedBy", "人工登记")
            self.require(row["subjectEntityType"], row["subjectEntityId"])
            self.require(row["objectEntityType"], row["objectEntityId"])
        else:
            row["status" if entity_type != "政策法规" else "reviewStatus"] = "草稿"
        if entity_type == "指标数据":
            row.setdefault("qualityFlag", "待核验")
        self._validate_knowledge(entity_type, row)
        with self.db.engine.begin() as conn:
            self.db.insert(spec.table, row, conn, actor=actor)
            if row.get(spec.status_field) == "已发布":
                saved = self.db.fetch_one(
                    f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.pk)}=:id", {"id": entity_id}, conn
                )
                self.db.log_change(conn, entity_type, entity_id, "CREATED", saved)
        if row.get(spec.status_field) == "已发布":
            self._sync_graph(entity_type, entity_id)
        return {"entityId": entity_id, "status": row[spec.status_field]}

    def create_master(self, entity_type: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        if entity_type not in {"品种", "产品", "地区", "企业", "本体概念", "指标定义"}:
            raise HTTPException(400, "不支持的主数据类型")
        spec = self.spec(entity_type)
        row = dict(payload)
        entity_id = self.db.next_id(spec.table, spec.id_prefix)
        row[spec.pk] = entity_id
        row.setdefault("status", "有效")
        self._validate_required(spec, row)
        with self.db.engine.begin() as conn:
            self.db.insert(spec.table, row, conn, actor=actor)
        self._sync_graph(entity_type, entity_id)
        return {"entityId": entity_id, "status": row["status"]}

    def _validate_required(self, spec: EntitySpec, row: dict[str, Any]) -> None:
        missing = []
        for column in self.db.tables[spec.table]["columnMappings"]:
            if not column.get("nullable", True) and row.get(column["column"]) in {None, ""}:
                missing.append(column["column"])
        if missing:
            raise HTTPException(422, f"缺少必填字段：{', '.join(missing)}")

    def _validate_knowledge(self, entity_type: str, row: dict[str, Any]) -> None:
        spec = self.spec(entity_type)
        self._validate_required(spec, row)
        if row.get("sourceDocId"):
            document = self.require("来源文档", row["sourceDocId"])
            if document["status"] != "有效":
                raise HTTPException(422, "来源文档无效")
        if row.get("segmentId"):
            segment = self.get("文档段落", row["segmentId"])
            if not segment or segment["docId"] != row.get("sourceDocId"):
                raise HTTPException(422, "来源段落与文档不匹配")
        if row.get("knowledgeCategoryId"):
            self.require("知识分类", row["knowledgeCategoryId"])
        if entity_type == "知识关系" and not 0 <= float(row["confidence"]) <= 1:
            raise HTTPException(422, "置信度必须在 0～1 之间")

    def submit(self, entity_type: str, entity_id: str, actor: str) -> dict[str, Any]:
        if entity_type not in KNOWLEDGE_TYPES:
            raise HTTPException(400, "该类型不需要审核")
        spec = self.spec(entity_type)
        row = self.require(entity_type, entity_id)
        if row[spec.status_field] not in {"草稿", "已退回"}:
            raise HTTPException(409, "仅草稿或已退回知识可提交审核")
        with self.db.engine.begin() as conn:
            self.db.update(spec.table, entity_id, {spec.status_field: "待审核"}, conn, actor=actor)
        return {"entityId": entity_id, "status": "待审核"}

    def review(self, entity_type: str, entity_id: str, decision: str, actor: str) -> dict[str, Any]:
        if entity_type not in KNOWLEDGE_TYPES or decision not in {"通过", "退回"}:
            raise HTTPException(400, "审核类型或决定无效")
        spec = self.spec(entity_type)
        row = self.require(entity_type, entity_id)
        if row[spec.status_field] != "待审核":
            raise HTTPException(409, "仅待审核知识可执行审核")
        if decision == "通过" and entity_type == "知识关系" and float(row["confidence"]) < 0.6:
            raise HTTPException(422, "置信度低于 0.6 的关系不得发布")
        new_status = "已发布" if decision == "通过" else "已退回"
        with self.db.engine.begin() as conn:
            self.db.update(spec.table, entity_id, {spec.status_field: new_status}, conn, actor=actor)
            if new_status == "已发布":
                saved = self.db.fetch_one(
                    f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.pk)}=:id", {"id": entity_id}, conn
                )
                self.db.log_change(conn, entity_type, entity_id, "CREATED", saved, saved["updatedAt"])
        if new_status == "已发布":
            self._sync_graph(entity_type, entity_id)
        return {"entityId": entity_id, "status": new_status}

    def register_source(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        segments = payload.get("segments") or []
        if not segments:
            raise HTTPException(422, "来源文档至少需要一个正文段落")
        doc_id = self.db.next_id("SourceDoc", "SOU")
        document = {key: value for key, value in payload.items() if key != "segments"}
        document.update({"docId": doc_id, "status": "有效"})
        self._validate_required(self.spec("来源文档"), document)
        offset = 0
        segment_rows = []
        for index, segment in enumerate(segments, 1):
            text = str(segment.get("segmentText") or "").strip()
            if not text:
                raise HTTPException(422, "段落正文不能为空")
            segment_rows.append({
                **segment,
                "docId": doc_id,
                "segmentId": f"{doc_id}-S{index:03d}",
                "segmentNo": index,
                "segmentText": text,
                "charStart": offset,
                "charEnd": offset + len(text),
            })
            offset += len(text) + 1
        with self.db.engine.begin() as conn:
            self.db.insert("SourceDoc", document, conn, actor=actor)
            for segment in segment_rows:
                self.db.insert("SourceDoc_Segment", segment, conn, actor=actor)
        self._sync_graph("来源文档", doc_id)
        return {"docId": doc_id, "segments": len(segment_rows), "status": "有效"}
