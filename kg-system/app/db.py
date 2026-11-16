from __future__ import annotations

import csv
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine

from app.ontology_mirror import initialize_mirror_schema

AUDIT_COLUMNS = {
    "createdBy": "TEXT", "createdAt": "TEXT", "updatedBy": "TEXT", "updatedAt": "TEXT"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"非法数据库标识符: {identifier}")
    return f'"{identifier}"'


class Database:
    def __init__(self, url: str, seed_dir: Path):
        self.url = url
        self.seed_dir = seed_dir
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        if url.startswith("sqlite"):
            Path(url.rsplit("/", 1)[-1]).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        self.tables: dict[str, dict[str, Any]] = {}
        self.columns: dict[str, list[str]] = {}
        self._load_mapping()

    def _load_mapping(self) -> None:
        model = json.loads((self.seed_dir / "mapping.json").read_text(encoding="utf-8"))
        for mapping in model["mappings"]:
            for table in mapping["tableMappings"]:
                name = table["tableName"]
                table["primaryKeys"] = [part.strip() for part in str(table["primaryKey"]).split(",") if part.strip()]
                self.tables[name] = table
                columns = [column["column"] for column in table["columnMappings"]]
                if table["tableRole"] == "MASTER":
                    columns.extend(column for column in AUDIT_COLUMNS if column not in columns)
                self.columns[name] = columns

    def initialize(self) -> None:
        with self.engine.begin() as conn:
            for name, table in self.tables.items():
                definitions = []
                primary_keys = table["primaryKeys"]
                for column in table["columnMappings"]:
                    definition = f"{quote(column['column'])} {column['columnType']}"
                    if column.get("isPrimaryKey"):
                        definition += " NOT NULL"
                        if len(primary_keys) == 1:
                            definition += " PRIMARY KEY"
                    elif not column.get("nullable", True):
                        definition += " NOT NULL"
                    if column.get("unique") and not column.get("isPrimaryKey"):
                        definition += " UNIQUE"
                    definitions.append(definition)
                if table["tableRole"] == "MASTER":
                    definitions.extend(f"{quote(column)} {kind}" for column, kind in AUDIT_COLUMNS.items())
                if len(primary_keys) > 1:
                    definitions.append(f"PRIMARY KEY ({', '.join(quote(column) for column in primary_keys)})")
                conn.execute(text(f"CREATE TABLE IF NOT EXISTS {quote(name)} ({', '.join(definitions)})"))
            self._backfill_columns(conn)
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS "ChangeLog" ('
                '"changeId" TEXT PRIMARY KEY, "entityType" TEXT NOT NULL, "entityId" TEXT NOT NULL, '
                '"changeType" TEXT NOT NULL, "updatedAt" TEXT NOT NULL, "payload" TEXT NOT NULL)'
            ))
            conn.execute(text('CREATE INDEX IF NOT EXISTS "idx_change_updated" ON "ChangeLog" ("updatedAt")'))
            for table, column in [("Policy", "reviewStatus"), ("IvdData", "status"), ("YrbYearbook", "status"), ("Intel", "status"), ("Relation", "status"), ("KtcCategory", "categoryCode")]:
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "idx_{table}_{column}" ON {quote(table)} ({quote(column)})'))
            initialize_mirror_schema(conn)

    def _backfill_columns(self, conn: Connection) -> None:
        """按 mapping.json 给已有库补齐缺失列。

        CREATE TABLE IF NOT EXISTS 对已存在的表不做任何改动，mapping 新增列后老库不会自动获得，
        这里逐列比对并 ALTER。只补可空列：已有行没有值，非空列必须连同默认值一起设计。
        """
        inspector = inspect(conn)
        for name, table in self.tables.items():
            if not inspector.has_table(name):
                continue
            existing = {column["name"] for column in inspector.get_columns(name)}
            for column in table["columnMappings"]:
                if column["column"] in existing:
                    continue
                if not column.get("nullable", True):
                    raise RuntimeError(f"{name}.{column['column']} 是非空列，无法对已有库直接补齐")
                conn.execute(text(
                    f"ALTER TABLE {quote(name)} ADD COLUMN {quote(column['column'])} {column['columnType']}"
                ))

    def fetch_all(self, sql: str, params: dict[str, Any] | None = None, conn: Connection | None = None) -> list[dict[str, Any]]:
        if conn is not None:
            return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text(sql), params or {}).mappings().all()]

    def fetch_one(self, sql: str, params: dict[str, Any] | None = None, conn: Connection | None = None) -> dict[str, Any] | None:
        rows = self.fetch_all(sql, params, conn)
        return rows[0] if rows else None

    def insert(self, table: str, values: dict[str, Any], conn: Connection, actor: str = "system", seed_time: str | None = None, upsert: bool = False) -> None:
        allowed = self.columns[table]
        row = {key: value for key, value in values.items() if key in allowed}
        if self.tables[table]["tableRole"] == "MASTER":
            now = seed_time or utc_now()
            row.setdefault("createdBy", actor)
            row.setdefault("createdAt", now)
            row.setdefault("updatedBy", actor)
            row.setdefault("updatedAt", now)
        names = list(row)
        placeholders = ", ".join(f":{name}" for name in names)
        sql = f"INSERT INTO {quote(table)} ({', '.join(quote(name) for name in names)}) VALUES ({placeholders})"
        if upsert:
            primary_keys = self.tables[table]["primaryKeys"]
            sql += f" ON CONFLICT ({', '.join(quote(column) for column in primary_keys)}) DO NOTHING"
        conn.execute(text(sql), row)

    def update(self, table: str, pk_value: str, values: dict[str, Any], conn: Connection, actor: str = "system", touch: bool = True) -> None:
        allowed = self.columns[table]
        row = {key: value for key, value in values.items() if key in allowed}
        if touch and self.tables[table]["tableRole"] == "MASTER":
            row["updatedBy"] = actor
            row["updatedAt"] = utc_now()
        if not row:
            return
        assignments = ", ".join(f"{quote(key)}=:{key}" for key in row)
        pk = self.tables[table]["primaryKey"]
        row["pk_value"] = pk_value
        conn.execute(text(f"UPDATE {quote(table)} SET {assignments} WHERE {quote(pk)}=:pk_value"), row)

    def next_id(self, table: str, prefix: str, conn: Connection | None = None) -> str:
        pk = self.tables[table]["primaryKey"]
        sql = f"SELECT {quote(pk)} AS pk FROM {quote(table)} WHERE {quote(pk)} LIKE :prefix"
        if conn is not None:
            rows = [dict(r._mapping) for r in conn.execute(text(sql), {"prefix": f"{prefix}%"})]
        else:
            rows = self.fetch_all(sql, {"prefix": f"{prefix}%"})
        numbers = [int(match.group(1)) for row in rows if (match := re.fullmatch(rf"{re.escape(prefix)}(\d+)", str(row["pk"])))]
        return f"{prefix}{max(numbers, default=0) + 1:04d}"

    def log_change(self, conn: Connection, entity_type: str, entity_id: str, change_type: str, payload: dict[str, Any], updated_at: str | None = None) -> None:
        conn.execute(text(
            'INSERT INTO "ChangeLog" ("changeId","entityType","entityId","changeType","updatedAt","payload") '
            'VALUES (:changeId,:entityType,:entityId,:changeType,:updatedAt,:payload)'
        ), {
            "changeId": uuid.uuid4().hex, "entityType": entity_type, "entityId": entity_id,
            "changeType": change_type, "updatedAt": updated_at or utc_now(),
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
        })

    def seed(self) -> None:
        seed_time = "2026-11-12T00:00:00Z"
        with self.engine.begin() as conn:
            with (self.seed_dir / "taxonomy.csv").open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    for key in ("level", "sortNo"):
                        row[key] = int(row[key])
                    self.insert("KtcCategory", row, conn, actor="seed", seed_time=seed_time, upsert=True)
            business = json.loads((self.seed_dir / "business.json").read_text(encoding="utf-8"))
            segments = business.get("SourceDoc_Segment", [])
            offsets: dict[str, int] = {}
            for row in sorted(segments, key=lambda item: (item["docId"], item["segmentNo"])):
                cursor = offsets.get(row["docId"], 0)
                row.setdefault("charStart", cursor)
                row.setdefault("charEnd", cursor + len(row["segmentText"]))
                offsets[row["docId"]] = row["charEnd"] + 1
            checksums = {
                doc_id: hashlib.sha256("\n".join(row["segmentText"] for row in segments if row["docId"] == doc_id).encode("utf-8")).hexdigest()
                for doc_id in offsets
            }
            for doc in business.get("SourceDoc", []):
                doc.setdefault("checksum", checksums.get(doc["docId"], ""))
            for table in self.tables:
                for row in business.get(table, []):
                    self.insert(table, row, conn, actor="seed", seed_time=seed_time, upsert=True)
            for entity_type, table, pk, status_field in [
                ("政策法规", "Policy", "policyId", "reviewStatus"), ("指标数据", "IvdData", "valueId", "status"),
                ("年鉴记录", "YrbYearbook", "recordId", "status"), ("农业情报", "Intel", "intelId", "status"),
                ("知识关系", "Relation", "relationId", "status"),
            ]:
                rows = self.fetch_all(f"SELECT * FROM {quote(table)}", conn=conn)
                for row in rows:
                    if row.get(status_field) == "已发布":
                        exists = self.fetch_one(
                            'SELECT "changeId" FROM "ChangeLog" WHERE "entityType"=:entity_type AND "entityId"=:entity_id',
                            {"entity_type": entity_type, "entity_id": row[pk]}, conn,
                        )
                        if not exists:
                            self.log_change(conn, entity_type, row[pk], "CREATED", row, row.get("updatedAt") or seed_time)

    def close(self) -> None:
        self.engine.dispose()
