# -*- coding: utf-8 -*-
"""Stage 0 定义层校验器：门 0—门 4 静态校验，只读 JSON，不连库不联网。"""

import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = Path(__file__).resolve().parent / "stage0_report.json"

# 交付物清单（任务列出的 15 个落盘项）
FILES = [
    "kernel/object_types.json",
    "kernel/relation_types.json",
    "kernel/enums.json",
    "kernel/id_registry.json",
    "kernel/functions.json",
    "kernel/actions.json",
    "bridges/policy_event.json",
    "bridges/factor.json",
    "bridges/assertion.json",
    "capability/questions.json",
    "terms/ly_terms.json",
    "terms/skos_mapping.json",
    "gates/rules.json",
    "schema/kuzu_ddl.md",
    "packs/policy/clause_model.json",
]

ID_RE = re.compile(r"^(OBJ|LNK|DIM|SCP|FAC|AST)-[A-Z]{2,4}-[A-Z0-9-]+$")
SHORT_PREFIX_RE = re.compile(r"^(OBJ|LNK)-[A-Z]{2,4}$")
BARE_PREFIX_RE = re.compile(r"^(DIM|SCP|FAC|AST)$")
OPERAND_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Za-z]{2,})\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")
FIELD_TYPES = {
    "string", "string[]", "int", "float", "bool", "date", "datetime",
    "json", "array", "enum", "ref",
}

REQUIRED_AP = [
    "AP_SCOPE_LEAK", "AP_DIM_CONFLICT", "AP_ORPHAN_FACTOR", "AP_ASSERT_NO_EVIDENCE",
    "AP_DUP_ASSERT", "AP_UNIT_MISMATCH", "AP_TIME_OVERLAP", "AP_ID_REUSE",
    "AP_POLICY_MAGNITUDE", "AP_CRUD", "AP_COVERAGE",
]

rows = []
loaded = {}


def add(gate, rule, status, message):
    rows.append({"gate": gate, "rule": rule, "status": status, "message": message})


def load_json(rel):
    path = ROOT / rel
    if not path.is_file():
        raise FileNotFoundError(rel)
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def field_kind(field):
    """拆出字段类型类别与目标名：ref X / ref X[] / enum X 兼容写法。"""
    raw = str(field.get("type", "")).strip()
    base = "ref" if raw.startswith("ref") else ("enum" if raw.startswith("enum") else raw)
    target = field.get("ref") or field.get("enumRef")
    if target is None and " " in raw:
        target = raw.split(None, 1)[1].strip()
    if target:
        target = target.rstrip("[]").strip()
    return base, target


def flatten(value):
    """bridges 的 allowedWrites/allowedEdges 既可能是列表也可能是按包分组。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        out = []
        for item in value.values():
            if isinstance(item, list):
                out.extend(item)
        return out
    return []


def flatten_fields(container):
    """对象类型/关系实体/条款类型统一取字段列表。"""
    for t in container:
        yield t.get("typeName", "?"), t.get("fields", [])


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def pad(text, width):
    text = str(text)
    return text + " " * max(0, width - display_width(text))


def short(items, limit=4):
    items = list(items)
    head = "、".join(str(x) for x in items[:limit])
    return head + ("…" if len(items) > limit else "")


# ---------- 载入 ----------
missing = []
parse_error = []
for rel in FILES:
    path = ROOT / rel
    if not path.is_file():
        missing.append(rel)
        continue
    try:
        if path.suffix == ".json":
            loaded[rel] = load_json(rel)
    except Exception as exc:  # 解析失败也计入结构门
        parse_error.append(f"{rel}: {exc}")

if missing:
    add("G0", "G0-1", "FAIL", f"交付物缺失 {len(missing)} 个：{short(missing, 6)}")
if parse_error:
    add("G0", "G0-1", "FAIL", f"JSON 解析失败：{short(parse_error, 3)}")
if not missing and not parse_error:
    add("G0", "G0-1", "PASS", f"{len(FILES)} 个交付物全部存在且可解析")

obj = loaded.get("kernel/object_types.json", {})
rel = loaded.get("kernel/relation_types.json", {})
enums_doc = loaded.get("kernel/enums.json", {})
registry = loaded.get("kernel/id_registry.json", {})
questions = loaded.get("capability/questions.json", {})
rules_doc = loaded.get("gates/rules.json", {})
clause = loaded.get("packs/policy/clause_model.json", {})
terms = loaded.get("terms/ly_terms.json", {})
actions_doc = loaded.get("kernel/actions.json", {})
assertion_doc = loaded.get("bridges/assertion.json", {})

kernel_types = [t.get("typeName", "?") for t in obj.get("types", [])]
rel_types = [t.get("typeName", "?") for t in rel.get("relationEntities", [])]
clause_types = [t.get("typeName", "?") for t in clause.get("types", [])]
core29 = kernel_types + rel_types
all_types = core29 + clause_types
all_type_set = set(all_types)
enums = enums_doc.get("enums", {})
enum_names = set(enums)
common_fields = {f.get("name") for f in obj.get("commonFields", [])}
# 类型 -> 全部合法字段名（含公共字段）
type_fields = {t.get("typeName"): set(common_fields) | {f.get("name") for f in t.get("fields", [])}
               for t in obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", [])}

# 域码白名单
domain_codes = {d.get("code") for d in registry.get("domainCodes", [])}
enum_domain_codes = {v.get("value") for v in enums.get("DomainCode", {}).get("values", [])}

# ---------- 门 0 · 结构门 ----------
kb = len(kernel_types)
if 25 <= kb <= 29:
    add("G3", "G3-3a", "PASS", f"内核对象类型 {kb} 个，落在预算 25—29")
else:
    add("G3", "G3-3a", "FAIL", f"内核对象类型 {kb} 个，超出预算 25—29")

rb = len(rel_types)
if 4 <= rb <= 6:
    add("G3", "G3-3b", "PASS", f"关系实体化类型 {rb} 个，落在预算 4—6")
else:
    add("G3", "G3-3b", "FAIL", f"关系实体化类型 {rb} 个，超出预算 4—6")

# G0-7 字段三元组：类型标注 / 是否必填 / 说明
field_defects = []
unknown_types = set()
for tname, fields in flatten_fields(obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", [])):
    for f in fields:
        name = f.get("name", "<无名>")
        lack = [k for k in ("name", "type", "required") if k not in f]
        if "note" not in f and "desc" not in f:
            lack.append("note/desc")
        base, _ = field_kind(f)
        if base not in FIELD_TYPES:
            unknown_types.add(f"{tname}.{name}:{f.get('type')}")
        if lack:
            field_defects.append(f"{tname}.{name}(缺 {'/'.join(lack)})")
if field_defects:
    add("G0", "G0-7", "FAIL", f"{len(field_defects)} 个字段标注不全：{short(field_defects, 5)}")
elif unknown_types:
    add("G0", "G0-7", "FAIL", f"未知字段类型：{short(unknown_types, 5)}")
else:
    total_fields = sum(len(fs) for _, fs in flatten_fields(
        obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", [])))
    add("G0", "G0-7", "PASS", f"{total_fields} 个字段均有类型/必填/说明标注")

# G0-2a ID 前缀齐备且形态合规
reserved = set(registry.get("reservedFragments", []))
prefix_defects = []
for t in obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", []):
    tname = t.get("typeName", "?")
    prefix = t.get("idPrefix")
    if not prefix:
        prefix_defects.append(f"{tname}(无 idPrefix)")
        continue
    if not (SHORT_PREFIX_RE.match(prefix) or BARE_PREFIX_RE.match(prefix) or ID_RE.match(prefix)):
        prefix_defects.append(f"{tname}({prefix} 形态不合规)")
        continue
    if "-" in prefix:
        frag = prefix.split("-", 1)[1]
        if frag not in reserved:
            prefix_defects.append(f"{tname}(片段 {frag} 未登记 reservedFragments)")
if prefix_defects:
    add("G0", "G0-2a", "FAIL", f"{len(prefix_defects)} 个类型前缀异常：{short(prefix_defects, 5)}")
else:
    add("G0", "G0-2a", "PASS", f"{len(all_types)} 个类型均有合规 ID 前缀")

# G0-2b id_registry 覆盖全部类型
patterns = {p.get("typeName"): p for p in registry.get("typePatterns", [])}
uncovered = [t for t in all_types if t not in patterns]
prefix_mismatch = []
for t in obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", []):
    tname = t.get("typeName")
    pat = patterns.get(tname)
    if not pat:
        continue
    declared = str(t.get("idPrefix", "")).split("-")[0]
    if declared != str(pat.get("prefix", "")).split("-")[0]:
        prefix_mismatch.append(f"{tname}({t.get('idPrefix')} vs {pat.get('prefix')})")
if uncovered or prefix_mismatch:
    msg = []
    if uncovered:
        msg.append(f"未登记 {len(uncovered)} 个：{short(uncovered, 5)}")
    if prefix_mismatch:
        msg.append(f"前缀不一致：{short(prefix_mismatch, 4)}")
    add("G0", "G0-2b", "FAIL", "；".join(msg))
else:
    add("G0", "G0-2b", "PASS", f"id_registry 覆盖 {len(all_types)} 个类型，前缀一致")

# G0-5a 枚举字段引用的词表存在
enum_refs = []
for _, fields in flatten_fields(obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", [])):
    for f in fields:
        base, target = field_kind(f)
        if base.startswith("enum") and target:
            enum_refs.append((f.get("name"), target))
for dim in next((t for t in obj.get("types", []) if t.get("typeName") == "Dimension"), {}).get("seedInstances", []):
    if dim.get("inlineEnumRef"):
        enum_refs.append((dim.get("dimensionCode"), dim["inlineEnumRef"]))
bad_enum_refs = sorted({f"{n}->{t}" for n, t in enum_refs if t not in enum_names})
if bad_enum_refs:
    add("G0", "G0-5a", "FAIL", f"{len(bad_enum_refs)} 处引用不存在的词表：{short(bad_enum_refs, 5)}")
else:
    add("G0", "G0-5a", "PASS", f"{len(enum_refs)} 处枚举引用全部命中 enums.json")

# G0-6 词表无空值域、无 Other 兜底
empty_enums, catchall = [], []
for name, body in enums.items():
    values = body.get("values") or []
    if not values:
        empty_enums.append(name)
        continue
    if any(str(v.get("value", "")).strip() == "" for v in values):
        empty_enums.append(f"{name}(含空值)")
    for v in values:
        token = str(v.get("value", "")).strip().upper()
        if token in ("OTHER", "其他", "OTHER_VALUE") and not v.get("explicitOnly"):
            catchall.append(f"{name}.{v.get('value')}")
if empty_enums or catchall:
    msg = []
    if empty_enums:
        msg.append(f"空值域：{short(empty_enums, 4)}")
    if catchall:
        msg.append(f"未标 explicitOnly 的兜底项：{short(catchall, 4)}")
    add("G0", "G0-6", "FAIL", "；".join(msg))
else:
    add("G0", "G0-6", "PASS", f"{len(enums)} 个词表均非空，OTHER 类取值均标 explicitOnly")

# ---------- 门 0 · 引用可解析（G0-4） ----------
ref_defects = []
for tname, fields in flatten_fields(obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", [])):
    for f in fields:
        base, target = field_kind(f)
        if base.startswith("ref"):
            if not target:
                ref_defects.append(f"{tname}.{f.get('name')}(缺 ref 目标)")
            elif target not in all_type_set:
                ref_defects.append(f"{tname}.{f.get('name')}->{target}")
if ref_defects:
    add("G0", "G0-4", "FAIL", f"{len(ref_defects)} 处 ref 目标不存在：{short(ref_defects, 6)}")
else:
    ref_total = sum(1 for _, fs in flatten_fields(
        obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", []))
        for f in fs if field_kind(f)[0].startswith("ref"))
    add("G0", "G0-4", "PASS", f"{ref_total} 个 ref 目标均在内核 29 或政策包 9 类型内")

# G0-8 动作命名非 CRUD（AP_CRUD）
CRUD_WORDS = {"get", "create", "update", "delete", "insert", "remove", "set", "query", "list", "fetch", "add", "del"}
actions = actions_doc.get("actions", [])
crud_actions = []
for action in actions:
    tokens = {t for part in (action.get("actionId"), action.get("name")) if part
              for t in re.split(r"[^A-Za-z]+", str(part).lower()) if t}
    if tokens & CRUD_WORDS:
        crud_actions.append(action.get("actionId"))
crud_ratio = len(crud_actions) / len(actions) if actions else 0.0
if crud_ratio > 0.4:
    add("G0", "G0-8", "WARN",
        f"CRUD 式命名占比 {crud_ratio:.0%}（{len(crud_actions)}/{len(actions)}）超 40%：{short(crud_actions, 5)}")
elif not actions:
    add("G0", "G0-8", "FAIL", "actions.json 未登记任何动作，无法判定 CRUD 命名占比")
else:
    add("G0", "G0-8", "PASS",
        f"{len(actions)} 个动作中 CRUD 式命名 {len(crud_actions)} 个（{crud_ratio:.0%}），未超 40%")

# G2-7 边端点类型合法
edge_defects = []
edges = rel.get("edges", [])
edge_names = {e.get("name") for e in edges}
for e in edges:
    for side in ("from", "to"):
        for t in e.get(side, []) or []:
            if t not in set(core29):
                edge_defects.append(f"{e.get('name')}.{side}->{t}")
    if not e.get("from") or not e.get("to"):
        edge_defects.append(f"{e.get('name')}(端点为空)")
for e in clause.get("relations", []):
    for side in ("fromTypes", "toTypes"):
        for t in e.get(side, []) or []:
            if t not in all_type_set:
                edge_defects.append(f"{e.get('name')}.{side}->{t}")
if edge_defects:
    add("G2", "G2-7", "FAIL", f"{len(edge_defects)} 处边端点不存在：{short(edge_defects, 6)}")
else:
    add("G2", "G2-7", "PASS", f"{len(edges)} 条语义边 + {len(clause.get('relations', []))} 条条款关系端点均存在")

# G2-4 桥不越权：三座桥的写目标与边名必须在定义内
bridge_defects = []
bridge_files = ["bridges/policy_event.json", "bridges/factor.json", "bridges/assertion.json"]
for relpath in bridge_files:
    b = loaded.get(relpath, {})
    for w in flatten(b.get("allowedWrites")):
        target = w.get("targetType")
        if target not in all_type_set:
            bridge_defects.append(f"{relpath}:{target}(写目标)")
        for fname in w.get("allowedFields", []) or []:
            if fname not in type_fields.get(target, set()):
                bridge_defects.append(f"{relpath}:{target}.{fname}(字段不存在)")
    for e in flatten(b.get("allowedEdges")):
        name = e.get("name")
        if name not in edge_names:
            bridge_defects.append(f"{relpath}:{name}(边未定义)")
        for side in ("from", "to"):
            for t in e.get(side, []) or []:
                if t not in all_type_set:
                    bridge_defects.append(f"{relpath}:{name}.{side}->{t}")
    # forbid 文本中的 Type.field / EDGE.param 左侧必须是已定义类型或边
    for item in b.get("forbidden", []):
        for left in re.findall(r"\b([A-Z][A-Za-z]+)\s*\.", str(item.get("rule", ""))):
            if left not in all_type_set and left not in edge_names:
                bridge_defects.append(f"{relpath}:forbid {item.get('code')}({left} 未定义)")
if bridge_defects:
    add("G2", "G2-4", "FAIL", f"{len(bridge_defects)} 处桥引用不存在：{short(bridge_defects, 6)}")
else:
    add("G2", "G2-4", "PASS", "三座桥的写目标类型与边名均在定义内")

# ---------- 门 2 · 语义门 ----------
# G2-8 条款条件 leftOperand 可解析
operand_defects, operand_unknown, operand_pending = [], [], []


def scan_operands(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "leftOperand" and isinstance(v, str):
                operand_pending.append(v)
            else:
                scan_operands(v)
    elif isinstance(node, list):
        for v in node:
            scan_operands(v)
    elif isinstance(node, str):
        for left, right in OPERAND_RE.findall(node):
            operand_pending.append(f"{left}.{right}")


scan_operands(clause)
for operand in sorted(set(operand_pending)):
    left, _, right = operand.partition(".")
    if not right:
        continue
    if left not in all_type_set:
        operand_unknown.append(operand)
        continue
    if right in type_fields.get(left, set()):
        continue
    if right.upper() == right and re.match(r"^[A-Z][A-Z0-9_]+$", right):
        continue  # indicatorCode 形态，定义层无法核验注册表
    operand_defects.append(operand)
if operand_defects:
    add("G2", "G2-8", "FAIL", f"leftOperand 字段不存在：{short(operand_defects, 5)}")
elif operand_unknown:
    add("G2", "G2-8", "WARN", f"疑似属性路径但左侧类型不在 29 类型内：{short(operand_unknown, 5)}")
else:
    add("G2", "G2-8", "PASS", f"条款条件 leftOperand 共 {len(set(operand_pending))} 处，左侧类型与字段均可解析")

# G2-4a 政策包不得写 impactMagnitude
policy = loaded.get("bridges/policy_event.json", {})
policy_allowed_blob = json.dumps([policy.get("allowedWrites"), policy.get("allowedEdges")], ensure_ascii=False)
policy_forbid = policy.get("forbidden", [])
policy_ok = any(x.get("code") == "AP_POLICY_MAGNITUDE" and "impactMagnitude" in str(x.get("rule", ""))
                for x in policy_forbid) and "impactMagnitude" not in policy_allowed_blob
if policy_ok:
    add("G2", "G2-4a", "PASS", "政策包 forbid 含 AP_POLICY_MAGNITUDE/impactMagnitude，且允许区未出现该字段")
else:
    add("G2", "G2-4a", "FAIL", "政策包未有效禁止 impactMagnitude（forbid 缺失或允许区泄漏）")

# G2-4b 市场包不得写 ACTS_ON_FACTOR
factor = loaded.get("bridges/factor.json", {})
factor_forbid = factor.get("forbidden", [])
market_blob = json.dumps(factor.get("allowedEdges", {}).get("MARKET_PACK", []), ensure_ascii=False)
factor_ok = any("ACTS_ON_FACTOR" in str(x.get("rule", "")) for x in factor_forbid) and "ACTS_ON_FACTOR" not in market_blob
if factor_ok:
    add("G2", "G2-4b", "PASS", "市场包 forbid 含 ACTS_ON_FACTOR，且 MARKET_PACK 允许边未包含该边")
else:
    add("G2", "G2-4b", "FAIL", "市场包未有效禁止 ACTS_ON_FACTOR")

# G2-12 断言去重契约完整（AP_DUP_ASSERT）
dedup = assertion_doc.get("deduplication") or {}
expected_key = {"assertedById", "targetPeriodId", "subjectId", "predicate"}
dedup_defects = []
if set(dedup.get("key") or []) != expected_key or len(dedup.get("key") or []) != len(expected_key):
    dedup_defects.append(f"去重键={dedup.get('key')}")
try:
    threshold = float(dedup.get("similarityThreshold"))
except (TypeError, ValueError):
    threshold = -1.0
if abs(threshold - 0.85) > 1e-9:
    dedup_defects.append(f"相似度阈值={dedup.get('similarityThreshold')}")
if not dedup.get("neverMerge"):
    dedup_defects.append("neverMerge 为空")
if not dedup.get("onDuplicate"):
    dedup_defects.append("onDuplicate 为空")
register_action = next(
    (a for a in actions if a.get("actionId") == "ACT-PUB-REGISTER-ASSERTION"), None)
if not register_action:
    dedup_defects.append("缺 ACT-PUB-REGISTER-ASSERTION")
elif not any("去重键" in str(p) or "assertedById+targetPeriodId+subjectId+predicate" in str(p)
             for p in register_action.get("postconditions", []) or []):
    dedup_defects.append("登记动作后置断言未引用去重键")
if dedup_defects:
    add("G2", "G2-12", "FAIL", f"断言去重契约不完整：{short(dedup_defects, 5)}")
else:
    add("G2", "G2-12", "PASS",
        f"去重键 {len(expected_key)} 项、阈值 0.85、neverMerge {len(dedup['neverMerge'])} 组齐备，"
        "登记动作后置断言已引用该键")

# G3-4a 能力问题跳数上限
qs = questions.get("questions", [])
# 跳数上限由 capability/questions.json 的 hopPolicy 声明：Stage 0 交付按 stage0Limit，
# Stage 2 问题只在管理员开启 Stage 2 后按 stage2Limit 生效，避免把未启用的阶段算成告警。
hop_policy = questions.get("hopPolicy") or {}
stage0_limit = int(hop_policy.get("stage0Limit") or 5)
stage2_limit = int(hop_policy.get("stage2Limit") or stage0_limit)
hop_fail, hop_warn, hop_switch = [], [], []
for q in qs:
    hops = q.get("maxHops")
    if not isinstance(hops, int):
        hop_fail.append(f"{q.get('id')}(缺 maxHops)")
    elif hops < 1:
        hop_fail.append(f"{q.get('id')}(maxHops={hops})")
    elif q.get("stage") == "Stage 2":
        if hops > stage2_limit:
            hop_warn.append(f"{q.get('id')}={hops}")
        elif hops > stage0_limit:
            hop_switch.append(f"{q.get('id')}={hops}")
    elif hops > stage0_limit:
        hop_fail.append(f"{q.get('id')}={hops}")
if hop_fail:
    extra = f"；另有 Stage 2 的 {short(hop_warn, 3)} 超开关上限" if hop_warn else ""
    add("G3", "G3-4a", "FAIL", f"跳数超 Stage 0 上限 {stage0_limit}：{short(hop_fail, 6)}{extra}")
elif hop_warn:
    add("G3", "G3-4a", "WARN", f"Stage 2 问题跳数超开关上限 {stage2_limit}：{short(hop_warn, 4)}")
elif hop_switch:
    add("G3", "G3-4a", "PASS",
        f"{len(qs)} 条能力问题在 Stage 0 上限 {stage0_limit} 内；Stage 2 共 {len(hop_switch)} 条"
        f"使用管理员开关上限 {stage2_limit}：{short(hop_switch, 4)}")
else:
    add("G3", "G3-4a", "PASS", f"{len(qs)} 条能力问题 maxHops 均 ≤ {stage0_limit}")

# G3-4b 能力问题引用的类型/边存在
clause_edge_names = {e.get("name") for e in clause.get("relations", [])}
q_defects = []
for q in qs:
    for t in q.get("requiredTypes", []) or []:
        if t not in all_type_set:
            q_defects.append(f"{q.get('id')}.type:{t}")
    for e in q.get("requiredEdges", []) or []:
        if e not in edge_names and e not in clause_edge_names:
            q_defects.append(f"{q.get('id')}.edge:{e}")
if q_defects:
    add("G3", "G3-4b", "FAIL", f"{len(q_defects)} 处引用不存在：{short(q_defects, 6)}")
else:
    add("G3", "G3-4b", "PASS", f"{len(qs)} 条能力问题的类型与关系引用全部存在")

# G3-3c 能力问题条数预算
stage0_qs = [q for q in qs if q.get("stage") == "Stage 0"]
declared_enabled = (questions.get("coverage") or {}).get("stage0Enabled")
if 24 <= len(qs) <= 32:
    note = f"共 {len(qs)} 条（Stage 0 启用 {len(stage0_qs)} 条），落在 24—32"
    if declared_enabled is not None and declared_enabled != len(stage0_qs):
        add("G3", "G3-3c", "WARN", f"{note}；coverage.stage0Enabled={declared_enabled} 与实际不符")
    else:
        add("G3", "G3-3c", "PASS", note)
else:
    add("G3", "G3-3c", "FAIL", f"能力问题 {len(qs)} 条，超出预算 24—32")

# ---------- 门 3 · 全量门 ----------
# G3-6a 每个类型只声明一个域码，且与 id_registry 一致
domain_defects, seen_type = [], {}
for t in obj.get("types", []) + rel.get("relationEntities", []) + clause.get("types", []):
    tname = t.get("typeName")
    if tname in seen_type:
        domain_defects.append(f"{tname}(类型重名)")
    seen_type[tname] = True
    # 条款包 9 类型的域码在文件级声明，类型级可继承
    dc = t.get("domainCode") or clause.get("domainCode")
    if not isinstance(dc, str) or not dc:
        domain_defects.append(f"{tname}(缺 domainCode)")
        continue
    if dc not in enum_domain_codes or dc not in domain_codes:
        domain_defects.append(f"{tname}({dc} 未在 DomainCode 词表/域码表登记)")
    pat = patterns.get(tname)
    if pat and pat.get("defaultDomain") and pat["defaultDomain"] != dc:
        domain_defects.append(f"{tname}({dc} vs id_registry {pat['defaultDomain']})")
if domain_defects:
    add("G3", "G3-6a", "FAIL", f"{len(domain_defects)} 处域码归属异常：{short(domain_defects, 5)}")
else:
    add("G3", "G3-6a", "PASS", f"{len(seen_type)} 个类型各声明唯一 domainCode，且与 id_registry 一致")

# G3-6b 域码表本身唯一、归属单一
codes = [d.get("code") for d in registry.get("domainCodes", [])]
dup_codes = sorted({c for c in codes if codes.count(c) > 1})
owner_conflict = []
owner_map = {}
for d in registry.get("domainCodes", []):
    code, owner = d.get("code"), d.get("owner")
    if code in owner_map and owner_map[code] != owner:
        owner_conflict.append(f"{code}({owner_map[code]}/{owner})")
    owner_map[code] = owner
if dup_codes or owner_conflict:
    add("G3", "G3-6b", "FAIL", f"域码表异常：重复 {short(dup_codes, 4)}；归属冲突 {short(owner_conflict, 4)}")
else:
    add("G3", "G3-6b", "PASS", f"{len(codes)} 个域码各出现一次、归属唯一")

# G3-5a Commodity.commodityCode 全局唯一声明
commodity = next((t for t in obj.get("types", []) if t.get("typeName") == "Commodity"), {})
c_text = " ".join(commodity.get("constraints", []) + [
    f.get("note", "") + f.get("desc", "") for f in commodity.get("fields", [])])
if "commodityCode" in c_text and "全局唯一" in c_text:
    add("G3", "G3-5a", "PASS", "Commodity.commodityCode 已声明全局唯一（constraints + 字段 note）")
else:
    add("G3", "G3-5a", "FAIL", "Commodity.commodityCode 缺少全局唯一声明")

# G3-5b Factor.factorCode 全局唯一声明
factor_type = next((t for t in obj.get("types", []) if t.get("typeName") == "Factor"), {})
f_text = " ".join(factor_type.get("constraints", []) + [
    f.get("note", "") + f.get("desc", "") for f in factor_type.get("fields", [])])
if "factorCode" in f_text and "全局唯一" in f_text:
    add("G3", "G3-5b", "PASS", "Factor.factorCode 已声明全局唯一")
else:
    add("G3", "G3-5b", "FAIL", "Factor.factorCode 缺少全局唯一声明")

# ---------- 门 3 · 全量门（预算检查 G3-3d/e/f） ----------
dim_type = next((t for t in obj.get("types", []) if t.get("typeName") == "Dimension"), {})
dims = dim_type.get("seedInstances", [])
dim_codes = [d.get("dimensionCode") for d in dims]
enum_dim_codes = [v.get("value") for v in enums.get("DimensionCode", {}).get("values", [])]
dim_bad = [d.get("dimensionCode") for d in dims
           if not d.get("valueObjectType") and not d.get("inlineEnumRef") and not d.get("inlineAttribute")]
dim_bad += [d.get("dimensionCode") for d in dims
            if any(t not in all_type_set for t in d.get("valueObjectType", []) or [])]
dim_bad += [c for c in dim_codes if c not in enum_dim_codes]
dim_bad += sorted({c for c in dim_codes if dim_codes.count(c) > 1})
dim_bad += [c for c in enum_dim_codes if c not in dim_codes]
if len(dims) == 10 and not dim_bad:
    add("G3", "G3-3d", "PASS", "Dimension 固定 10 维，维度码与 DimensionCode 枚举一一对应，取值类型白名单合法")
elif len(dims) != 10:
    add("G3", "G3-3d", "FAIL", f"Dimension 为 {len(dims)} 维，应固定 10 维")
else:
    add("G3", "G3-3d", "FAIL", f"Dimension 维度定义异常：{short(dim_bad, 5)}")

if rb <= 6 and kb <= 29:
    add("G3", "G3-3e", "PASS", f"预算：内核 {kb}/上限 29，关系实体 {rb}/上限 6")
else:
    add("G3", "G3-3e", "FAIL", f"预算超限：内核 {kb}/29，关系实体 {rb}/6")

term_list = terms.get("terms", [])
if 100 <= len(term_list) <= 150:
    add("G3", "G3-3f", "PASS", f"术语种子 {len(term_list)} 条，落在 100—150")
else:
    add("G3", "G3-3f", "FAIL", f"术语种子 {len(term_list)} 条，超出预算 100—150")

# ---------- 门 4 · 评分门（反模式码两级登记） ----------
ap_entries = (rules_doc.get("antiPatterns") or {}).get("codes", [])
ap_codes = [c.get("code") for c in ap_entries]
core_codes = [c.get("code") for c in ap_entries if c.get("tier") == "CORE"]
ext_codes = [c.get("code") for c in ap_entries if c.get("tier") == "EXTENSION"]
missing_ap = [c for c in REQUIRED_AP if c not in core_codes]
extra_core = [c for c in core_codes if c not in REQUIRED_AP]
dup_ap = sorted({c for c in ap_codes if ap_codes.count(c) > 1})
untiered = [c for c in ap_entries if c.get("tier") not in ("CORE", "EXTENSION")]
if missing_ap or extra_core or dup_ap or untiered or not (11 <= len(ap_codes) <= 20):
    detail = []
    if missing_ap:
        detail.append(f"缺核心 {len(missing_ap)} 条：{short(missing_ap, 5)}")
    if extra_core:
        detail.append(f"多核心 {len(extra_core)} 条：{short(extra_core, 5)}")
    if dup_ap:
        detail.append(f"重复：{short(dup_ap, 3)}")
    if untiered:
        detail.append(f"未标 tier：{short([c.get('code') for c in untiered], 3)}")
    add("G4", "G4-2", "FAIL",
        f"两级码表不合规（CORE {len(core_codes)} / EXT {len(ext_codes)} / 合计 {len(ap_codes)}）：{'；'.join(detail)}")
else:
    add("G4", "G4-2", "PASS",
        f"CORE {len(core_codes)} 条与 §7.6 逐条一致，EXTENSION {len(ext_codes)} 条，合计 {len(ap_codes)} ≤ 20")

rule_ids = {r.get("ruleId") for g in rules_doc.get("gates", []) for r in g.get("rules", [])}
unwired = [c.get("code") for c in ap_entries if c.get("tier") == "CORE" and not c.get("detectedBy")]
dangling = sorted({
    f'{c.get("code")}->{rid}'
    for c in ap_entries
    for rid in (c.get("detectedBy") or [])
    if rid not in rule_ids
})
if dangling:
    add("G4", "G4-3", "FAIL", f"反模式码指向不存在的门规则：{short(dangling, 4)}")
elif unwired:
    add("G4", "G4-3", "WARN", f"核心码尚未接线到门规则（需实例层比对）：{short(unwired, 4)}")
else:
    add("G4", "G4-3", "PASS",
        f"全部 {len(core_codes)} 条核心码均已挂到现有门规则，共 {len(rule_ids)} 条规则可选")

# ---------- 汇总评分 ----------
n_err = sum(1 for r in rows if r["status"] == "FAIL")
n_warn = sum(1 for r in rows if r["status"] == "WARN")
n_pass = sum(1 for r in rows if r["status"] == "PASS")
score = max(0, min(100, 100 - n_err * 12 - n_warn * 2))
passed = n_err == 0 and score >= 80
summary_status = "PASS" if passed else "FAIL"
summary_msg = f"score={score}，error={n_err}，warn={n_warn}，通过线 ≥80 且 0 error"

# ---------- 输出 ----------
header = ("门", "规则ID", "结论", "说明")
widths = [max(display_width(header[0]), 4), max(display_width(header[1]), 8),
          max(display_width(header[2]), 6), 0]
print(" ".join([pad(header[0], widths[0]), pad(header[1], widths[1]), pad(header[2], widths[2]), header[3]]))
print("-" * 100)
for r in rows:
    print(" ".join([pad(r["gate"], widths[0]), pad(r["rule"], widths[1]),
                    pad(r["status"], widths[2]), r["message"]]))
print(" ".join([pad("G4", widths[0]), pad("G4-1", widths[1]), pad(summary_status, widths[2]), summary_msg]))
print("-" * 100)
print(f"score={score}  error={n_err}  warn={n_warn}  pass={n_pass}")
print(f"Stage 0 定义层校验：{'通过' if passed else '不通过'}")

report = {
    "generatedAt": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    "root": str(ROOT),
    "results": rows,
    "counts": {"error": n_err, "warn": n_warn, "pass": n_pass},
    "score": score,
    "passed": passed,
    "failedRules": [r["rule"] for r in rows if r["status"] == "FAIL"],
    "warnRules": [r["rule"] for r in rows if r["status"] == "WARN"],
}
REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

sys.exit(0 if n_err == 0 else 1)
