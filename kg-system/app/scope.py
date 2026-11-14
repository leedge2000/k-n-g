from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from app.db import Database, utc_now

KERNEL_DIR = Path(__file__).resolve().parent.parent / "ontology_ly" / "kernel"

# 阶段 0 定死的 10 个维度，任何包都不得增删（门 3 AP_DIM_CONFLICT）
DIMENSION_SEED_TIME = "2026-11-13T00:00:00Z"
EXPECTED_DIMENSION_COUNT = 10

SCOPE_CODE_RE = r"^[A-Z0-9][A-Z0-9-]{2,63}$"


class ScopeError(ValueError):
    """维度或范围定义不合法（对应门 2 G2-5 / 门 3 AP_DIM_CONFLICT / AP_SCOPE_LEAK）。"""


def normalize_members(members: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """按 dimensionCode 升序归一化成员，同一集合必须得到同一序列。"""
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for member in members or []:
        dimension = str(member.get("dimensionCode") or "").strip().upper()
        object_type = str(member.get("objectType") or "").strip()
        object_id = str(member.get("objectId") or "").strip()
        if not dimension or not object_type or not object_id:
            raise ScopeError("members 每项都必须同时给出 dimensionCode、objectType、objectId")
        key = (dimension, object_id)
        if key in seen:
            raise ScopeError(f"members 出现重复成员 {dimension}:{object_id}")
        seen.add(key)
        normalized.append(
            {"dimensionCode": dimension, "objectType": object_type, "objectId": object_id}
        )
    if not normalized:
        raise ScopeError("members 不能为空")
    normalized.sort(key=lambda item: (item["dimensionCode"], item["objectId"]))
    return normalized


def normalized_key(scope_type: str, members: list[dict[str, str]]) -> str:
    """sha1(scopeType + 排序后的 dimensionCode:objectId 列表)，相同即同一 Scope。"""
    canonical = "|".join(
        [scope_type, *[f"{item['dimensionCode']}:{item['objectId']}" for item in members]]
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def scope_id(normalized: str) -> str:
    """SCP + 域码 + 归一化键前 16 位，既满足 ID 规则又保证同集合同 ID。"""
    return f"SCP-PUB-{normalized[:16].upper()}"


class ScopeService:
    """维度与范围的权威注册表。

    政策包与市场包都只能向这里取维度、建范围，成员类型不合法直接拒绝，
    从而避免两个包各造一套维度（阶段 1 硬要求）。
    """

    def __init__(self, db: Database, kernel_dir: Path | None = None):
        self.db = db
        self.kernel_dir = Path(kernel_dir or KERNEL_DIR)
        self._object_types: dict[str, dict[str, Any]] | None = None
        self._enums: dict[str, dict[str, Any]] | None = None

    # ---------- 内核定义 ----------

    @property
    def object_types(self) -> dict[str, dict[str, Any]]:
        if self._object_types is None:
            payload = json.loads(
                (self.kernel_dir / "object_types.json").read_text(encoding="utf-8")
            )
            rows = payload.get("types") or payload
            self._object_types = {row["typeName"]: row for row in rows}
        return self._object_types

    @property
    def enums(self) -> dict[str, dict[str, Any]]:
        if self._enums is None:
            payload = json.loads((self.kernel_dir / "enums.json").read_text(encoding="utf-8"))
            self._enums = payload.get("enums") or payload
        return self._enums

    def _enum_values(self, name: str) -> list[str]:
        node = self.enums.get(name) or {}
        return [str(item["value"]) for item in node.get("values") or []]

    # ---------- 初始化与装载 ----------

    def initialize(self) -> None:
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS "OntologyDimension" ('
                '"dimensionId" TEXT PRIMARY KEY, "dimensionCode" TEXT NOT NULL UNIQUE, '
                '"label" TEXT NOT NULL, "valueObjectType" TEXT NOT NULL DEFAULT \'[]\', '
                '"multiValued" INTEGER NOT NULL DEFAULT 1, "hierarchical" INTEGER NOT NULL DEFAULT 0, '
                '"inlineEnumRef" TEXT, "inlineAttribute" TEXT, "domainCode" TEXT, '
                '"status" TEXT DEFAULT \'ACTIVE\', "version" INTEGER DEFAULT 1, "updatedAt" TEXT)'
            ))
            conn.execute(text(
                'CREATE TABLE IF NOT EXISTS "OntologyScope" ('
                '"scopeId" TEXT PRIMARY KEY, "scopeCode" TEXT NOT NULL UNIQUE, "label" TEXT, '
                '"scopeType" TEXT NOT NULL, "members" TEXT NOT NULL, '
                '"normalizedKey" TEXT NOT NULL UNIQUE, "isOpen" INTEGER NOT NULL DEFAULT 0, '
                '"supersetOf" TEXT NOT NULL DEFAULT \'[]\', "memberCount" INTEGER NOT NULL DEFAULT 0, '
                '"domainCode" TEXT DEFAULT \'PUB\', "status" TEXT DEFAULT \'ACTIVE\', '
                '"version" INTEGER DEFAULT 1, "createdBy" TEXT, "createdAt" TEXT, '
                '"updatedBy" TEXT, "updatedAt" TEXT)'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_scope_type" ON "OntologyScope" ("scopeType")'
            ))
        self.sync_dimensions()

    def dimension_seeds(self) -> list[dict[str, Any]]:
        """读取内核 Dimension.seedInstances，并校验它与枚举、白名单一致。"""
        definition = self.object_types.get("Dimension")
        if not definition:
            raise ScopeError("object_types.json 缺少 Dimension 类型定义")
        seeds = definition.get("seedInstances") or []
        if len(seeds) != EXPECTED_DIMENSION_COUNT:
            raise ScopeError(
                f"Dimension 必须固定为 {EXPECTED_DIMENSION_COUNT} 维，当前 {len(seeds)} 维"
            )
        allowed_codes = self._enum_values("DimensionCode")
        allowed_types = set(self._enum_values("ObjectTypeName"))
        seen: set[str] = set()
        for seed in seeds:
            code = str(seed.get("dimensionCode") or "").upper()
            if code not in allowed_codes:
                raise ScopeError(f"维度码 {code} 不在 DimensionCode 枚举内")
            if code in seen:
                raise ScopeError(f"维度码 {code} 重复")
            seen.add(code)
            for object_type in seed.get("valueObjectType") or []:
                if object_type not in allowed_types:
                    raise ScopeError(f"维度 {code} 的白名单类型 {object_type} 不在 ObjectTypeName 内")
            inline_enum = seed.get("inlineEnumRef")
            if inline_enum and inline_enum not in self.enums:
                raise ScopeError(f"维度 {code} 的内联枚举 {inline_enum} 未定义")
            inline_attribute = seed.get("inlineAttribute") or ""
            if inline_attribute:
                owner = inline_attribute.split(".", 1)[0]
                if owner not in allowed_types:
                    raise ScopeError(f"维度 {code} 的内联属性宿主 {owner} 不在 ObjectTypeName 内")
        return sorted(seeds, key=lambda item: allowed_codes.index(str(item["dimensionCode"]).upper()))

    def sync_dimensions(self) -> dict[str, Any]:
        """把内核种子写入权威表；重复执行不产生新行，内核改名会同步覆盖。"""
        seeds = self.dimension_seeds()
        with self.db.engine.begin() as conn:
            for seed in seeds:
                conn.execute(text(
                    'INSERT INTO "OntologyDimension" ("dimensionId","dimensionCode","label",'
                    '"valueObjectType","multiValued","hierarchical","inlineEnumRef","inlineAttribute",'
                    '"domainCode","status","version","updatedAt") '
                    'VALUES (:dimensionId,:dimensionCode,:label,:valueObjectType,:multiValued,'
                    ':hierarchical,:inlineEnumRef,:inlineAttribute,:domainCode,\'ACTIVE\',1,:updatedAt) '
                    'ON CONFLICT("dimensionId") DO UPDATE SET "dimensionCode"=excluded."dimensionCode", '
                    '"label"=excluded."label", "valueObjectType"=excluded."valueObjectType", '
                    '"multiValued"=excluded."multiValued", "hierarchical"=excluded."hierarchical", '
                    '"inlineEnumRef"=excluded."inlineEnumRef", '
                    '"inlineAttribute"=excluded."inlineAttribute", "updatedAt"=excluded."updatedAt"'
                ), {
                    "dimensionId": seed["id"],
                    "dimensionCode": str(seed["dimensionCode"]).upper(),
                    "label": seed.get("label") or seed["dimensionCode"],
                    "valueObjectType": json.dumps(seed.get("valueObjectType") or [], ensure_ascii=False),
                    "multiValued": 1 if seed.get("multiValued") else 0,
                    "hierarchical": 1 if seed.get("hierarchical") else 0,
                    "inlineEnumRef": seed.get("inlineEnumRef"),
                    "inlineAttribute": seed.get("inlineAttribute"),
                    "domainCode": seed.get("domainCode") or "PUB",
                    "updatedAt": DIMENSION_SEED_TIME,
                })
        return {"dimensionCount": len(seeds), "syncedAt": DIMENSION_SEED_TIME}

    # ---------- 维度查询 ----------

    def dimensions(self) -> list[dict[str, Any]]:
        rows = self.db.fetch_all('SELECT * FROM "OntologyDimension" ORDER BY "dimensionCode"')
        order = {code: index for index, code in enumerate(self._enum_values("DimensionCode"))}
        rows.sort(key=lambda row: order.get(row["dimensionCode"], len(order)))
        return [self._dimension_row(row) for row in rows]

    def dimension(self, dimension_code: str) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            'SELECT * FROM "OntologyDimension" WHERE "dimensionCode"=:code',
            {"code": str(dimension_code or "").upper()},
        )
        return self._dimension_row(row) if row else None

    @staticmethod
    def _dimension_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "dimensionId": row["dimensionId"],
            "dimensionCode": row["dimensionCode"],
            "label": row["label"],
            "valueObjectType": json.loads(row["valueObjectType"] or "[]"),
            "multiValued": bool(row["multiValued"]),
            "hierarchical": bool(row["hierarchical"]),
            "inlineEnumRef": row.get("inlineEnumRef"),
            "inlineAttribute": row.get("inlineAttribute"),
            "domainCode": row.get("domainCode"),
            "status": row.get("status"),
            "version": row.get("version"),
            "updatedAt": row.get("updatedAt"),
        }

    # ---------- 范围校验与建立 ----------

    def _validate_member(self, member: dict[str, str]) -> None:
        dimension = self.dimension(member["dimensionCode"])
        if not dimension:
            raise ScopeError(f"维度 {member['dimensionCode']} 未注册")
        object_type = member["objectType"]
        whitelist = dimension["valueObjectType"]
        if whitelist:
            if object_type not in whitelist:
                raise ScopeError(
                    f"维度 {dimension['dimensionCode']} 不接受对象类型 {object_type}，"
                    f"允许：{'/'.join(whitelist)}"
                )
            return
        inline_enum = dimension.get("inlineEnumRef")
        if inline_enum:
            if object_type != inline_enum:
                raise ScopeError(f"维度 {dimension['dimensionCode']} 的成员类型必须是 {inline_enum}")
            values = self._enum_values(inline_enum)
            if member["objectId"] not in values:
                raise ScopeError(
                    f"{inline_enum} 取值 {member['objectId']} 不合法，允许：{'/'.join(values)}"
                )
            return
        inline_attribute = dimension.get("inlineAttribute") or ""
        if inline_attribute:
            owner = inline_attribute.split(".", 1)[0]
            if object_type != owner:
                raise ScopeError(f"维度 {dimension['dimensionCode']} 的成员类型必须是 {owner}")
            return
        raise ScopeError(f"维度 {dimension['dimensionCode']} 既无白名单也无内联取值定义")

    def prepare_members(
        self, scope_type: str, members: Iterable[dict[str, Any]]
    ) -> tuple[list[dict[str, str]], str]:
        """归一化并校验成员集合，返回 (归一化成员, 归一化键)。"""
        scope_type = str(scope_type or "").upper()
        if scope_type not in self._enum_values("ScopeType"):
            raise ScopeError(f"scopeType {scope_type} 不在 ScopeType 枚举内")
        normalized = normalize_members(members)
        for member in normalized:
            self._validate_member(member)
        self._validate_multivalue(normalized)
        return normalized, normalized_key(scope_type, normalized)

    def upsert_scope(
        self,
        scope_type: str,
        members: Iterable[dict[str, Any]],
        scope_code: str,
        label: str | None = None,
        is_open: bool = False,
        superset_of: Iterable[str] | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """建立或复用范围。同一成员集合重复提交会返回既有记录，不会产生新 Scope。"""
        scope_type = str(scope_type or "").upper()
        if scope_type not in self._enum_values("ScopeType"):
            raise ScopeError(f"scopeType {scope_type} 不在 ScopeType 枚举内")
        scope_code = str(scope_code or "").strip().upper()
        if not re.fullmatch(SCOPE_CODE_RE, scope_code):
            raise ScopeError("scopeCode 只能使用大写字母、数字与连字符，长度 3—64")
        normalized, key = self.prepare_members(scope_type, members)
        superset = [str(item) for item in (superset_of or [])]
        existing = self.find_scope_by_key(key)
        if existing:
            return {**existing, "created": False}
        conflicting = self.db.fetch_one(
            'SELECT "scopeId" FROM "OntologyScope" WHERE "scopeCode"=:code', {"code": scope_code}
        )
        if conflicting:
            raise ScopeError(f"scopeCode {scope_code} 已被 {conflicting['scopeId']} 占用")
        new_id = scope_id(key)
        now = utc_now()
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'INSERT INTO "OntologyScope" ("scopeId","scopeCode","label","scopeType","members",'
                '"normalizedKey","isOpen","supersetOf","memberCount","domainCode","status",'
                '"version","createdBy","createdAt","updatedBy","updatedAt") '
                'VALUES (:scopeId,:scopeCode,:label,:scopeType,:members,:normalizedKey,:isOpen,'
                ':supersetOf,:memberCount,\'PUB\',\'ACTIVE\',1,:actor,:now,:actor,:now)'
            ), {
                "scopeId": new_id,
                "scopeCode": scope_code,
                "label": label or scope_code,
                "scopeType": scope_type,
                "members": json.dumps(normalized, ensure_ascii=False),
                "normalizedKey": key,
                "isOpen": 1 if is_open else 0,
                "supersetOf": json.dumps(superset, ensure_ascii=False),
                "memberCount": len(normalized),
                "actor": actor,
                "now": now,
            })
        return {**self.scope(new_id), "created": True}

    def _validate_multivalue(self, members: list[dict[str, str]]) -> None:
        counts: dict[str, int] = {}
        for member in members:
            counts[member["dimensionCode"]] = counts.get(member["dimensionCode"], 0) + 1
        for code, count in counts.items():
            dimension = self.dimension(code)
            if dimension and not dimension["multiValued"] and count > 1:
                raise ScopeError(f"维度 {code} 为单值维度，同一 Scope 内最多 1 个成员")

    def scope(self, scope_id_value: str) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            'SELECT * FROM "OntologyScope" WHERE "scopeId"=:id', {"id": scope_id_value}
        )
        return self._scope_row(row) if row else None

    def find_scope_by_key(self, key: str) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            'SELECT * FROM "OntologyScope" WHERE "normalizedKey"=:key', {"key": key}
        )
        return self._scope_row(row) if row else None

    def scopes(self, scope_type: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if scope_type:
            rows = self.db.fetch_all(
                'SELECT * FROM "OntologyScope" WHERE "scopeType"=:type '
                'ORDER BY "createdAt" DESC LIMIT :limit',
                {"type": str(scope_type).upper(), "limit": int(limit)},
            )
        else:
            rows = self.db.fetch_all(
                'SELECT * FROM "OntologyScope" ORDER BY "createdAt" DESC LIMIT :limit',
                {"limit": int(limit)},
            )
        return [self._scope_row(row) for row in rows]

    @staticmethod
    def _scope_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "scopeId": row["scopeId"],
            "scopeCode": row["scopeCode"],
            "label": row.get("label"),
            "scopeType": row["scopeType"],
            "members": json.loads(row["members"] or "[]"),
            "normalizedKey": row["normalizedKey"],
            "isOpen": bool(row["isOpen"]),
            "supersetOf": json.loads(row.get("supersetOf") or "[]"),
            "memberCount": row.get("memberCount"),
            "domainCode": row.get("domainCode"),
            "status": row.get("status"),
            "version": row.get("version"),
            "createdAt": row.get("createdAt"),
            "updatedAt": row.get("updatedAt"),
        }

    def is_superset(self, outer_id: str, inner_id: str) -> bool:
        """判断政策范围是否覆盖序列范围：同集合，或沿「显式上位范围」向上可达。

        supersetOf 记录的是「我的上位范围」，因此从内层出发逐级向上查找。
        """
        if outer_id == inner_id:
            return True
        pending = [inner_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            row = self.scope(current)
            if not row:
                continue
            for parent in row["supersetOf"]:
                if parent == outer_id:
                    return True
                pending.append(parent)
        return False

    def stats(self) -> dict[str, Any]:
        dimensions = self.db.fetch_one('SELECT COUNT(*) AS total FROM "OntologyDimension"') or {}
        scopes = self.db.fetch_one('SELECT COUNT(*) AS total FROM "OntologyScope"') or {}
        return {
            "dimensionCount": int(dimensions.get("total") or 0),
            "scopeCount": int(scopes.get("total") or 0),
            "expectedDimensionCount": EXPECTED_DIMENSION_COUNT,
        }
