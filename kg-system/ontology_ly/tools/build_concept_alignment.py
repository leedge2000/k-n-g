"""阶段 1 · 本体术语到应用主数据的 ID 对齐

为什么需要这一步
    alias_index.json 里的目标是本体对象（OBJ-PUB-CM-CORN、OBJ-PUB-EV-MINPRICE），
    而 app/rag.py 的实体词典用的是应用库主键（CMD0003、CON0003）。
    两套 ID 体系不打通，别名表接进检索链路后就会产出前端无法回查的悬空实体。
    本脚本把每个本体目标显式判到六类状态之一，不做隐式兜底。

六种对齐状态
    ALIGNED_APP    命中应用主数据行（Commodity -> CmdCommodity，CommodityForm -> Product 等）
    ALIGNED_ENUM   目标是封闭词表取值（PriceBasis: FOB），对齐到 kernel/enums.json
    NO_APP_TABLE   应用库没有对应主表（事件类型、计量单位、规则参数）
    NO_APP_RECORD  有表但无对应记录（机构 -> Enterprise 表为空）
    NO_TARGET_ID   术语种子未给 targetIdHint，没有稳定身份
    PSEUDO_ID      形如 grade=一级 的等级取值，不是对象 ID
    SCHEMA_ONLY    类型级结构词（品种、地区），供问句解析，不进检索实体词典

两条约束
1. 匹配只用「代码后缀等值」或「名称等值」，不做模糊相似度匹配；
   对不上就是对不上，宁可留空也不制造错误等价。
2. 不写生成时间戳，保证同一输入重跑逐字节一致（门 3 G3-2 幂等重跑）。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
ALIAS_PATH = ROOT / "terms" / "alias_index.json"
TYPES_PATH = ROOT / "kernel" / "object_types.json"
ENUMS_PATH = ROOT / "kernel" / "enums.json"
DB_PATH = ROOT.parent / "data" / "grain_oil.db"
OUT_PATH = ROOT / "terms" / "concept_alignment.json"

# 本体类型 -> 应用主表。code 为空表示该表只能按名称对齐。
APP_RULES: dict[str, dict] = {
    "Commodity": {
        "appType": "品种",
        "table": "CmdCommodity",
        "pk": "commodityId",
        "code": "commodityCode",
        "name": "commodityName",
        "bridge": ["conceptId"],
    },
    "CommodityForm": {
        "appType": "产品",
        "table": "Product",
        "pk": "productId",
        "code": "productCode",
        "name": "productName",
        "bridge": ["commodityId"],
    },
    "Organization": {
        "appType": "企业",
        "table": "Enterprise",
        "pk": "enterpriseId",
        "code": None,
        "name": "enterpriseName",
        "bridge": [],
    },
    "Indicator": {
        "appType": "指标定义",
        "table": "Indicator",
        "pk": "indicatorId",
        "code": "indicatorCode",
        "name": "indicatorName",
        "bridge": [],
    },
    "Region": {
        "appType": "地区",
        "table": "Region",
        "pk": "regionId",
        "code": "regionCode",
        "name": "regionName",
        "bridge": [],
    },
}

# 术语种子里沿用简写，词表里是带 Type 后缀的正式名
ENUM_ALIASES = {
    "CalendarScheme": "CalendarSchemeType",
    "StatisticalSource": "StatisticalSourceType",
}

# 形如 grade=一级 的不是对象 ID，是等级取值，没有主表可对齐
PSEUDO_ID_PREFIXES = ("grade=",)


def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_tables(specs: list[dict]) -> dict[str, list[dict]]:
    """一次连接读齐所有对齐候选行的代码与名称。"""
    tables: dict[str, list[dict]] = {}
    if not DB_PATH.exists():
        return tables
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        for spec in specs:
            table = spec["table"]
            if table in tables:
                continue
            columns = [spec["pk"], spec["name"], *spec.get("bridge", [])]
            if spec.get("code"):
                columns.append(spec["code"])
            select = ", ".join(f'"{column}"' for column in columns)
            try:
                tables[table] = [dict(row) for row in con.execute(f'SELECT {select} FROM "{table}"')]
            except sqlite3.Error:
                tables[table] = []
    finally:
        con.close()
    return tables


def align_by_app(target: dict, spec: dict, rows: list[dict]) -> dict:
    if not rows:
        return {"status": "NO_APP_RECORD", "reason": f'{spec["table"]} 表无记录'}
    hint = target["targetIdHint"]
    code_field = spec.get("code")
    for row in rows:
        code = row.get(code_field) if code_field else None
        if code and (hint == code or hint.endswith("-" + code)):
            return app_hit(target, spec, row, f"{code_field}={code}")
    for row in rows:
        name = row.get(spec["name"])
        if name and name == target["label"]:
            return app_hit(target, spec, row, f'{spec["name"]}={name}')
    return {"status": "NO_APP_RECORD", "reason": f'{spec["table"]} 无同码或同名记录'}


def app_hit(target: dict, spec: dict, row: dict, matched_by: str) -> dict:
    payload = {
        "status": "ALIGNED_APP",
        "appType": spec["appType"],
        "appTable": spec["table"],
        "appId": row[spec["pk"]],
        "appName": row[spec["name"]],
        "matchedBy": matched_by,
        # 本体标签与应用名称不一致时留痕，供命名评审，不影响对齐结论
        "labelMismatch": row[spec["name"]] != target["label"],
    }
    for field in spec.get("bridge", []):
        if row.get(field):
            payload["bridge" + field[:1].upper() + field[1:]] = row[field]
    return payload


def align(target: dict, tables: dict[str, list[dict]], enums: dict) -> dict:
    if target["kind"] == "type":
        return {"status": "SCHEMA_ONLY", "reason": "类型级结构词，不指向具体实例"}
    target_type = target.get("targetType")
    hint = target.get("targetIdHint")
    if not hint:
        return {"status": "NO_TARGET_ID", "reason": "术语种子未给 targetIdHint"}
    if hint.startswith(PSEUDO_ID_PREFIXES):
        return {"status": "PSEUDO_ID", "reason": "等级取值，非对象 ID"}

    enum_name = ENUM_ALIASES.get(target_type, target_type)
    if enum_name in enums:
        codes = {
            item["value"]: item.get("label")
            for item in enums[enum_name].get("values", [])
            if isinstance(item, dict) and item.get("value")
        }
        if hint in codes:
            return {
                "status": "ALIGNED_ENUM",
                "enumName": enum_name,
                "enumValue": hint,
                "enumLabel": codes[hint],
                "labelMatchesEnum": target["label"] == codes[hint],
            }
        return {"status": "ENUM_VALUE_MISSING", "reason": f"{enum_name} 无取值 {hint}"}

    spec = APP_RULES.get(target_type)
    if not spec:
        return {"status": "NO_APP_TABLE", "reason": f"应用库无 {target_type} 对应主表"}
    return align_by_app(target, spec, tables.get(spec["table"], []))


def collect_targets() -> list[dict]:
    """从别名索引里收集去重后的全部目标，保持稳定排序。"""
    index = load_json(ALIAS_PATH)
    seen: dict[str, dict] = {}
    for entry in index.get("entries", []):
        for target in entry.get("targets", []):
            seen.setdefault(target["targetKey"], target)
    return [seen[key] for key in sorted(seen)]


def main() -> None:
    enums = load_json(ENUMS_PATH)["enums"]
    types = load_json(TYPES_PATH)["types"]
    targets = collect_targets()
    tables = load_tables(list(APP_RULES.values()))

    records = []
    for target in targets:
        verdict = align(target, tables, enums)
        records.append(
            {
                "targetKey": target["targetKey"],
                "targetType": target.get("targetType"),
                "targetIdHint": target.get("targetIdHint"),
                "label": target["label"],
                "typeLabel": target.get("typeLabel"),
                "source": target.get("source"),
                **verdict,
            }
        )

    stats: dict[str, int] = {}
    for record in records:
        stats[record["status"]] = stats.get(record["status"], 0) + 1

    payload = {
        "schemaVersion": "0.1.0",
        "stage": "Stage 1",
        "sourceBaseline": "terms/alias_index.json + data/grain_oil.db + kernel/enums.json",
        "note": "由 tools/build_concept_alignment.py 生成，勿手改。每个本体目标只判一次，不做模糊匹配；"
                "ALIGNED_APP 用应用主键，ALIGNED_ENUM 用『枚举名:取值』，其余状态在检索层按本体 ID 展示。",
        "dbPath": "data/grain_oil.db",
        "appRules": {
            name: {key: value for key, value in spec.items()}
            for name, spec in sorted(APP_RULES.items())
        },
        "typeCount": len(types),
        "stats": {
            "targets": len(records),
            "byStatus": dict(sorted(stats.items())),
            "appTables": {table: len(rows) for table, rows in sorted(tables.items())},
        },
        "targets": records,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"targets={len(records)} -> {OUT_PATH}")
    for status, count in sorted(stats.items()):
        print(f"  {status:<20}{count}")
    for table, rows in sorted(tables.items()):
        print(f"  table {table:<16}{len(rows)}")


if __name__ == "__main__":
    main()
