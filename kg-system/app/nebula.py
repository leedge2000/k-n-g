from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from typing import Any

from .catalog import REFERENCES, SPECS, TAG_BY_TABLE
from .config import Settings
from .db import Database, quote


EDGE_PROPERTIES = {
    "has_segment": ("sortNo",), "has_clause": ("clauseNo",),
    "has_synonym": ("synonymType",), "has_run": ("startedAt",),
    "produced_by_crawl": ("confidence",), "from_doc": ("segmentId",),
    "in_category": ("isPrimary",), "in_region": ("relationType",),
    "applies_to_region": (), "of_indicator": ("period",),
    "of_commodity": ("relationType",), "processed_from": ("yieldRate",),
    "made_by": (), "located_in": (), "parent_of": ("level",),
    "maps_to_concept": ("matchType",),
}
CHILD_TABLES = {
    "SourceDoc": ("SourceDoc_Segment", "docId"),
    "Policy": ("Policy_Clause", "policyId"),
    "Concept": ("Concept_Synonym", "conceptId"),
    "CrsSource": ("CrsSource_CrawlRun", "crawlSourceId"),
}


class NebulaGraphStore:
    """Publish relational knowledge to NebulaGraph and execute graph traversals."""

    def __init__(self, db: Database, settings: Settings, session: Any = None):
        self.db = db
        self.settings = settings
        self._session = session
        self._pool = None
        self._lock = threading.RLock()

    def _get_session(self) -> Any:
        if self._session is None:
            from nebula3.gclient.net import ConnectionPool

            addresses = []
            for item in self.settings.nebula_hosts.split(","):
                host, separator, port = item.strip().rpartition(":")
                if not separator or not host or not port.isdigit():
                    raise ValueError(f"NebulaGraph 地址无效：{item}")
                addresses.append((host, int(port)))
            self._pool = ConnectionPool()
            if not self._pool.init(addresses):
                raise RuntimeError("NebulaGraph 连接池初始化失败")
            self._session = self._pool.get_session(
                self.settings.nebula_user, self.settings.nebula_password
            )
        return self._session

    def _execute(self, statement: str) -> Any:
        for _ in range(40):
            with self._lock:
                result = self._get_session().execute(statement)
            if result.is_succeeded():
                return result
            message = result.error_msg()
            if "No schema found" not in message:
                break
            time.sleep(0.5)
        raise RuntimeError(f"NebulaGraph 执行失败：{message}；语句：{statement}")

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"非法 NebulaGraph 标识符：{value}")
        return value

    @staticmethod
    def _literal(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        text = text.replace("\r", "\\r").replace("\n", "\\n")
        return f'"{text}"'

    def initialize(self) -> None:
        schema_path = self.settings.seed_dir.parent / "deploy" / "schema.ngql"
        schema = "\n".join(
            line for line in schema_path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--")
        )
        space = self._identifier(self.settings.nebula_space)
        if self.settings.nebula_replica_factor < 1:
            raise ValueError("NebulaGraph 副本数必须大于 0")
        schema = schema.replace("grain_oil_kg", space)
        schema = re.sub(
            r"replica_factor\s*=\s*\d+",
            f"replica_factor = {self.settings.nebula_replica_factor}",
            schema,
            count=1,
        )
        statements = [
            statement.strip()
            for statement in schema.split(";")
            if statement.strip()
        ]
        for statement in statements:
            if statement.upper().startswith("USE "):
                last_error = None
                for _ in range(20):
                    try:
                        self._execute(statement)
                        last_error = None
                        break
                    except RuntimeError as error:
                        last_error = error
                        time.sleep(0.5)
                if last_error:
                    raise last_error
            else:
                self._execute(statement)
        last_error = None
        for _ in range(40):
            try:
                self._execute("DESCRIBE TAG source_doc")
                self._execute("DESCRIBE EDGE kg_relation")
                last_error = None
                break
            except RuntimeError as error:
                last_error = error
                time.sleep(0.5)
        if last_error:
            raise RuntimeError("NebulaGraph Schema 尚未就绪") from last_error

    def _publishable(self, table: str, row: dict[str, Any]) -> bool:
        if table == "Policy":
            return row.get("reviewStatus") == "已发布"
        if table in {"IvdData", "YrbYearbook", "Intel", "Relation"}:
            return row.get("status") == "已发布"
        if table == "SourceDoc":
            return row.get("status") not in {"已作废", "已失效"}
        return row.get("status") not in {"已作废", "已失效", "已融合"}

    def _insert_vertices(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        tag = self._identifier(TAG_BY_TABLE[table])
        columns = self.db.columns[table]
        primary_key = self.db.tables[table]["primaryKey"]
        values = []
        for row in rows:
            properties = ", ".join(self._literal(row.get(column)) for column in columns)
            values.append(f"{self._literal(row[primary_key])}: ({properties})")
        for start in range(0, len(values), 100):
            batch = ", ".join(values[start:start + 100])
            self._execute(
                f"INSERT VERTEX {tag} ({', '.join(columns)}) VALUES {batch}"
            )

    def _insert_edges(self, edge_type: str, rows: list[tuple[str, str, dict[str, Any]]],
                      columns: tuple[str, ...]) -> None:
        if not rows:
            return
        edge_type = self._identifier(edge_type)
        values = []
        for source, target, properties in rows:
            literal_properties = ", ".join(self._literal(properties.get(column)) for column in columns)
            values.append(
                f"{self._literal(source)}->{self._literal(target)}: ({literal_properties})"
            )
        for start in range(0, len(values), 100):
            batch = ", ".join(values[start:start + 100])
            suffix = f" ({', '.join(columns)})" if columns else ""
            self._execute(f"INSERT EDGE {edge_type}{suffix} VALUES {batch}")

    def _structural_edges(self) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
        edges: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}

        def add(edge_type: str, source: str | None, target: str | None,
                **properties: Any) -> None:
            if source and target:
                edges.setdefault(edge_type, []).append((source, target, properties))

        for segment in self.db.fetch_all('SELECT * FROM "SourceDoc_Segment"'):
            add("has_segment", segment.get("docId"), segment.get("segmentId"),
                sortNo=segment.get("segmentNo"))
        for clause in self.db.fetch_all('SELECT * FROM "Policy_Clause"'):
            add("has_clause", clause.get("policyId"), clause.get("clauseId"),
                clauseNo=clause.get("clauseNo"))
        for synonym in self.db.fetch_all('SELECT * FROM "Concept_Synonym"'):
            add("has_synonym", synonym.get("conceptId"), synonym.get("synonymId"),
                synonymType=synonym.get("synonymType"))
        for run in self.db.fetch_all('SELECT * FROM "CrsSource_CrawlRun"'):
            add("has_run", run.get("crawlSourceId"), run.get("runId"),
                startedAt=run.get("startedAt"))

        for entity_type, references in REFERENCES.items():
            spec = SPECS[entity_type]
            rows = self.db.fetch_all(f"SELECT * FROM {quote(spec.table)}")
            for row in rows:
                if not self._publishable(spec.table, row):
                    continue
                source_id = row[spec.pk]
                for field, target_type, predicate in references:
                    target_id = row.get(field)
                    if not target_id:
                        continue
                    if field == "sourceDocId":
                        add("from_doc", source_id, target_id, segmentId=row.get("segmentId"))
                    elif field == "knowledgeCategoryId":
                        add("in_category", source_id, target_id, isPrimary=True)
                    elif field == "crawlSourceId":
                        add("produced_by_crawl", source_id, target_id, confidence=1.0)
                    elif field == "indicatorId":
                        period = row.get("period")
                        if period is None and row.get("statYear") is not None:
                            period = str(row["statYear"])
                        add("of_indicator", source_id, target_id, period=period)
                    elif field == "commodityId":
                        if spec.table == "Product":
                            add("processed_from", source_id, target_id, yieldRate=None)
                        else:
                            add("of_commodity", source_id, target_id,
                                relationType="计量" if spec.table in {"IvdData", "YrbYearbook"} else "涉及")
                    elif field == "regionId":
                        if spec.table == "Policy":
                            add("applies_to_region", source_id, target_id)
                        elif spec.table == "Enterprise":
                            add("located_in", source_id, target_id)
                        else:
                            add("in_region", source_id, target_id,
                                relationType="统计" if spec.table in {"IvdData", "YrbYearbook"} else "涉及")
                    elif field in {"parentCommodityId", "parentRegionId", "parentConceptId", "parentCategoryId"}:
                        add("parent_of", target_id, source_id, level=row.get("level"))
                    elif field == "conceptId":
                        add("maps_to_concept", source_id, target_id, matchType="精确")
                    elif field == "enterpriseId":
                        add("made_by", source_id, target_id)
        return edges

    def publish_entity(self, entity_type: str, entity_id: str) -> None:
        spec = SPECS[entity_type]
        row = self.db.fetch_one(
            f"SELECT * FROM {quote(spec.table)} WHERE {quote(spec.pk)}=:id", {"id": entity_id}
        )
        if row is None:
            self._execute(f"DELETE VERTEX {self._literal(entity_id)} WITH EDGE")
            return
        if not self._publishable(spec.table, row):
            if spec.table == "Relation":
                self._execute(
                    f"DELETE EDGE kg_relation {self._literal(row['subjectEntityId'])}"
                    f"->{self._literal(row['objectEntityId'])}"
                )
            child = CHILD_TABLES.get(spec.table)
            if child:
                child_table, foreign_key = child
                children = self.db.fetch_all(
                    f"SELECT * FROM {quote(child_table)} WHERE {quote(foreign_key)}=:id", {"id": entity_id}
                )
                child_pk = self.db.tables[child_table]["primaryKey"]
                for child_row in children:
                    self._execute(f"DELETE VERTEX {self._literal(child_row[child_pk])} WITH EDGE")
            self._execute(f"DELETE VERTEX {self._literal(entity_id)} WITH EDGE")
            return

        self._insert_vertices(spec.table, [row])
        entity_ids = {entity_id}
        child = CHILD_TABLES.get(spec.table)
        if child:
            child_table, foreign_key = child
            children = self.db.fetch_all(
                f"SELECT * FROM {quote(child_table)} WHERE {quote(foreign_key)}=:id", {"id": entity_id}
            )
            self._insert_vertices(child_table, children)
            child_pk = self.db.tables[child_table]["primaryKey"]
            entity_ids.update(child_row[child_pk] for child_row in children)

        structural_edges = self._structural_edges()
        for edge_type, columns in EDGE_PROPERTIES.items():
            related = [
                edge for edge in structural_edges.get(edge_type, [])
                if edge[0] in entity_ids or edge[1] in entity_ids
            ]
            self._insert_edges(edge_type, related, columns)
        if spec.table == "Relation":
            self._insert_edges(
                "kg_relation",
                [(row["subjectEntityId"], row["objectEntityId"], row)],
                ("relationId", "predicate", "confidence"),
            )

    def publish_all(self) -> None:
        for table in TAG_BY_TABLE:
            rows = self.db.fetch_all(f"SELECT * FROM {quote(table)}")
            self._insert_vertices(table, [row for row in rows if self._publishable(table, row)])

        structural_edges = self._structural_edges()
        for edge_type, columns in EDGE_PROPERTIES.items():
            self._insert_edges(edge_type, structural_edges.get(edge_type, []), columns)

        relations = self.db.fetch_all('SELECT * FROM "Relation" WHERE "status"=:status', {"status": "已发布"})
        self._insert_edges(
            "kg_relation",
            [
                (row["subjectEntityId"], row["objectEntityId"], row)
                for row in relations
            ],
            ("relationId", "predicate", "confidence"),
        )

    @classmethod
    def _primitive(cls, value: Any) -> Any:
        if hasattr(value, "as_primitive"):
            return cls._primitive(value.as_primitive())
        if isinstance(value, dict):
            return {key: cls._primitive(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._primitive(item) for item in value]
        for predicate, converter in (
            ("is_bool", "as_bool"), ("is_int", "as_int"),
            ("is_double", "as_double"), ("is_string", "as_string"),
        ):
            if hasattr(value, predicate) and getattr(value, predicate)():
                return getattr(value, converter)()
        if hasattr(value, "is_map") and value.is_map():
            return {key: cls._primitive(item) for key, item in value.as_map().items()}
        if hasattr(value, "is_list") and value.is_list():
            return [cls._primitive(item) for item in value.as_list()]
        if hasattr(value, "is_set") and value.is_set():
            return [cls._primitive(item) for item in value.as_set()]
        if hasattr(value, "is_empty") and value.is_empty():
            return None
        if hasattr(value, "is_null") and value.is_null():
            return None
        return value

    def expand_ids(self, entity_id: str, depth: int = 1) -> list[dict[str, Any]]:
        if not 1 <= depth <= 3:
            raise ValueError("图展开深度必须为 1～3")
        statement = (
            f"MATCH (v)-[e*1..{depth}]-(neighbor) "
            f"WHERE id(v) == {self._literal(entity_id)} "
            "UNWIND e AS edge_item "
            "RETURN src(edge_item) AS source, dst(edge_item) AS target, "
            "type(edge_item) AS edgeType, properties(edge_item) AS properties"
        )
        result = self._execute(statement)
        edges = []
        for index in range(result.row_size()):
            source, target, edge_type, properties = result.row_values(index)[:4]
            edges.append({
                "source": self._primitive(source),
                "target": self._primitive(target),
                "edgeType": self._primitive(edge_type),
                "properties": self._primitive(properties),
            })
        return edges

    def close(self) -> None:
        if self._session is not None and hasattr(self._session, "release"):
            self._session.release()
        if self._pool is not None:
            self._pool.close()
        self._session = None
        self._pool = None
