"""从内核定义生成可执行的 Kùzu DDL，并装载 Dimension / Scope 投影。

权威记录在 SQLite（OntologyDimension / OntologyScope），Kùzu 只做只读图投影：
删掉 --out 目录后重跑本脚本即可全量重建。DDL 由 kernel/object_types.json 派生，
内核字段一旦变化而本脚本未跟上，会直接报错而不是静默漂移。

用法（需先 pip install kuzu）：
    python ontology_ly/tools/build_kuzu.py --db data/grain_oil.db --out data/ly.kuzu
    python ontology_ly/tools/build_kuzu.py --out data/ly.kuzu --verify
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
KERNEL_DIR = ROOT / "ontology_ly" / "kernel"
EXPECTED_DIMENSION_COUNT = 10
PROJECTED_TYPES = ("Dimension", "Scope")

# 内核公共字段 → 投影列（None 表示本投影不落地该字段）
# evidenceIds / confidence / extractedBy / authority / mergedIntoId 属治理字段，
# 真值留在 SQLite，阶段 1 只把双时间、域、状态投影到图侧。
COMMON_COLUMNS: dict[str, tuple[str, str] | None] = {
    "id": ("id", "STRING"),
    "name": ("label", "STRING"),
    "alias": None,
    "code": None,
    "description": None,
    "domainCode": ("domainCode", "STRING"),
    "validFrom": ("validFrom", "DATE"),
    "validTo": ("validTo", "DATE"),
    "recordedAt": ("recordedAt", "TIMESTAMP"),
    "supersededAt": ("supersededAt", "TIMESTAMP"),
    "version": ("version", "INT64"),
    "status": ("status", "STRING"),
    "evidenceIds": None,
    "confidence": None,
    "extractedBy": None,
    "authority": None,
    "mergedIntoId": ("mergedIntoId", "STRING"),
}

# 类型差异字段 → 投影列；数组与 JSON 在此处显式降维，不做隐式推断
FIELD_COLUMNS: dict[tuple[str, str], tuple[str, str]] = {
    ("Dimension", "dimensionCode"): ("dimensionCode", "STRING"),
    ("Dimension", "valueObjectType"): ("valueObjectType", "STRING[]"),
    ("Dimension", "multiValued"): ("multiValued", "BOOLEAN"),
    ("Dimension", "hierarchical"): ("hierarchical", "BOOLEAN"),
    ("Scope", "scopeCode"): ("scopeCode", "STRING"),
    ("Scope", "scopeType"): ("scopeType", "STRING"),
    ("Scope", "members"): ("membersJson", "STRING"),
    ("Scope", "isOpen"): ("isOpen", "BOOLEAN"),
    ("Scope", "normalizedKey"): ("normalizedKey", "STRING"),
    ("Scope", "supersetOf"): ("supersetOfIds", "STRING[]"),
}

# 投影特有字段（内核未声明，写入授权表时才产生）
EXTRA_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "Dimension": [("inlineEnumRef", "STRING"), ("inlineAttribute", "STRING")],
    "Scope": [("memberCount", "INT64")],
}

TAIL_COLUMNS: list[tuple[str, str]] = [("updatedAt", "TIMESTAMP")]


def load_kernel() -> dict[str, Any]:
    object_types = json.loads((KERNEL_DIR / "object_types.json").read_text(encoding="utf-8"))
    enums = json.loads((KERNEL_DIR / "enums.json").read_text(encoding="utf-8"))
    return {"types": object_types, "enums": enums}


def _type_definition(kernel: dict[str, Any], type_name: str) -> dict[str, Any]:
    for row in kernel["types"]["types"]:
        if row["typeName"] == type_name:
            return row
    raise SystemExit(f"内核缺少 {type_name} 类型定义")


def columns_for(kernel: dict[str, Any], type_name: str) -> list[tuple[str, str]]:
    """把内核字段翻译成投影列；出现未登记字段立即失败，避免 DDL 与内核脱节。"""
    definition = _type_definition(kernel, type_name)
    columns: list[tuple[str, str]] = []
    unmapped: list[str] = []
    common = {field["name"] for field in kernel["types"]["commonFields"]}
    for field in kernel["types"]["commonFields"]:
        if field["name"] not in COMMON_COLUMNS:
            unmapped.append(field["name"])
            continue
        mapped = COMMON_COLUMNS[field["name"]]
        if mapped:
            columns.append(mapped)
    for field in definition.get("fields", []):
        mapped = FIELD_COLUMNS.get((type_name, field["name"]))
        if mapped is None:
            unmapped.append(field["name"])
            continue
        columns.append(mapped)
    if unmapped:
        raise SystemExit(
            f"{type_name} 有未映射到 Kùzu 投影的字段：{', '.join(sorted(unmapped))}；"
            "请先在 ontology_ly/tools/build_kuzu.py 的 FIELD_COLUMNS 中登记"
        )
    columns.extend(EXTRA_COLUMNS.get(type_name, []))
    columns.extend(TAIL_COLUMNS)
    return columns


def ddl_statements(kernel: dict[str, Any]) -> list[str]:
    statements = []
    for type_name in PROJECTED_TYPES:
        columns = columns_for(kernel, type_name)
        body = ", ".join(f"{name} {kind}" for name, kind in columns)
        statements.append(
            f"CREATE NODE TABLE IF NOT EXISTS {type_name}({body}, PRIMARY KEY(id))"
        )
    return statements


def connect(out_dir: Path):
    try:
        import kuzu
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给明确指引
        raise SystemExit("缺少 kuzu 依赖，请先执行 pip install kuzu") from exc
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    database = kuzu.Database(str(out_dir))
    return database, kuzu.Connection(database)


def ensure_schema(connection, kernel: dict[str, Any]) -> None:
    for statement in ddl_statements(kernel):
        connection.execute(statement)


def seed_dimensions(connection, kernel: dict[str, Any]) -> int:
    seeds = _type_definition(kernel, "Dimension").get("seedInstances") or []
    if len(seeds) != EXPECTED_DIMENSION_COUNT:
        raise SystemExit(f"Dimension 必须固定 {EXPECTED_DIMENSION_COUNT} 维，当前 {len(seeds)}")
    statement = (
        "MERGE (d:Dimension {id: $id}) "
        "SET d.label=$label, d.domainCode=$domainCode, d.dimensionCode=$dimensionCode, "
        "d.valueObjectType=$valueObjectType, d.multiValued=$multiValued, "
        "d.hierarchical=$hierarchical, d.inlineEnumRef=$inlineEnumRef, "
        "d.inlineAttribute=$inlineAttribute, d.version=1, d.status='ACTIVE', "
        "d.validFrom=date($validFrom), d.recordedAt=timestamp($recordedAt), "
        "d.updatedAt=timestamp($recordedAt)"
    )
    for seed in seeds:
        connection.execute(statement, {
            "id": seed["id"],
            "label": seed.get("label") or seed["dimensionCode"],
            "domainCode": seed.get("domainCode") or "PUB",
            "dimensionCode": str(seed["dimensionCode"]).upper(),
            "valueObjectType": list(seed.get("valueObjectType") or []),
            "multiValued": bool(seed.get("multiValued")),
            "hierarchical": bool(seed.get("hierarchical")),
            "inlineEnumRef": seed.get("inlineEnumRef"),
            "inlineAttribute": seed.get("inlineAttribute"),
            "validFrom": "2026-11-13",
            "recordedAt": "2026-11-13T00:00:00Z",
        })
    return len(seeds)


def read_scopes(sqlite_path: Path) -> list[dict[str, Any]]:
    if not sqlite_path.exists():
        return []
    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='OntologyScope'"
        ).fetchone()
        if not exists:
            return []
        rows = connection.execute(
            'SELECT * FROM "OntologyScope" WHERE "status"=\'ACTIVE\' ORDER BY "scopeId"'
        ).fetchall()
    return [dict(row) for row in rows]


def project_scopes(connection, scopes: list[dict[str, Any]]) -> int:
    statement = (
        "MERGE (s:Scope {id: $id}) "
        "SET s.label=$label, s.domainCode=$domainCode, s.scopeCode=$scopeCode, "
        "s.scopeType=$scopeType, s.membersJson=$membersJson, s.normalizedKey=$normalizedKey, "
        "s.isOpen=$isOpen, s.supersetOfIds=$supersetOfIds, s.memberCount=$memberCount, "
        "s.version=$version, s.status=$status, s.validFrom=date($validFrom), "
        "s.recordedAt=timestamp($recordedAt), s.updatedAt=timestamp($updatedAt)"
    )
    for row in scopes:
        recorded = row.get("createdAt") or row.get("updatedAt") or "2026-11-13T00:00:00Z"
        connection.execute(statement, {
            "id": row["scopeId"],
            "label": row.get("label") or row.get("scopeCode"),
            "domainCode": row.get("domainCode") or "PUB",
            "scopeCode": row["scopeCode"],
            "scopeType": row["scopeType"],
            "membersJson": row["members"],
            "normalizedKey": row["normalizedKey"],
            "isOpen": bool(row["isOpen"]),
            "supersetOfIds": list(json.loads(row.get("supersetOf") or "[]")),
            "memberCount": int(row.get("memberCount") or 0),
            "version": int(row.get("version") or 1),
            "status": row.get("status") or "ACTIVE",
            "validFrom": recorded[:10],
            "recordedAt": recorded,
            "updatedAt": row.get("updatedAt") or recorded,
        })
    return len(scopes)


def count_rows(connection, table: str) -> int:
    result = connection.execute(f"MATCH (n:{table}) RETURN count(*) AS total").get_all()
    return int(result[0][0]) if result else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并校验 Dimension / Scope 的 Kùzu 投影")
    parser.add_argument("--db", default=str(ROOT / "data" / "grain_oil.db"), help="SQLite 权威库路径")
    parser.add_argument("--out", default=str(ROOT / "data" / "ly.kuzu"), help="Kùzu 数据库目录")
    parser.add_argument("--verify", action="store_true", help="只读校验，不写投影")
    args = parser.parse_args()

    kernel = load_kernel()
    database, connection = connect(Path(args.out))
    ensure_schema(connection, kernel)
    if not args.verify:
        seeded = seed_dimensions(connection, kernel)
        scopes = read_scopes(Path(args.db))
        projected = project_scopes(connection, scopes)
        print(f"DDL 已应用：{len(ddl_statements(kernel))} 张节点表")
        print(f"Dimension 装载：{seeded} 条")
        print(f"Scope 投影：{projected} 条")
    dimensions = count_rows(connection, "Dimension")
    scopes = count_rows(connection, "Scope")
    print(f"校验：Dimension={dimensions} Scope={scopes}")
    if dimensions != EXPECTED_DIMENSION_COUNT:
        print(f"维度数量异常，应为 {EXPECTED_DIMENSION_COUNT}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
