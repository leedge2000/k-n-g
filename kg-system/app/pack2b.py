"""阶段 2b · 条款结构化：从 Policy_Clause 抽取 NormativeStatement + 附属结构。

严格按冻结口径句清单（stage2b_口径句清单.md）执行：
  R1 条款切分（按分号/句号，引号/括号保护）
  R2 statementType 六类 + 冲突优先级
  R3 vaguenessFlag 10 词冻结字典
  R4 confirmationStatus 一律 DRAFT
  R5 ClauseCondition 扁平 LEAF，factSource=USER_INPUT
  R6 Exception/Consequence/Parameter 有则抽、Relation 本阶段不抽
  R7 actorScopeId / objectFormIds / regionalScopeId 一律 null
  R8 不编译规则、不升级结论层、不做定义展开
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import text as sa_text

from app.db import Database


class ProtectedAssetError(ValueError):
    """自动重抽会触及人工确认或测试资产时拒绝继续。"""

    def __init__(self, protected_ids: list[str]):
        self.protected_ids = sorted(set(protected_ids))
        super().__init__("存在受保护资产，禁止自动重抽：" + ", ".join(self.protected_ids))


# R3-1-1 冻结词表：10 词，不扩展
VAGUE_TERMS = (
    "适当",
    "必要时",
    "有关部门",
    "原则上",
    "及时",
    "视情况",
    "酌情",
    "合理",
    "相应",
    "必要",
)

# R2-1-1~2-1-6 模态词
MODAL_OBLIGATION = ("应", "须", "应当", "必须", "责成", "要求")
MODAL_PROHIBITION = ("不得", "禁止", "严禁")
MODAL_PERMISSION = ("可", "可以", "有权", "允许")
MODAL_AUTHORIZATION = ("由", "经", "需")  # 见 _is_authorization 判定

# R6-1-1 例外触发连词
EXCEPTION_CONNECTORS = ("但", "但是", "除外", "另有规定的除外", "除非")


def _split_clause(text: str) -> list[tuple[int, int, str]]:
    """按未包裹的分号/句号拆句，并返回可逐字回切的半开偏移。"""
    if not text:
        return []
    results: list[tuple[int, int, str]] = []
    start = 0
    bracket_depth = 0
    quote_closers: list[str] = []
    quote_pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}

    def append_segment(left: int, right: int) -> None:
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            results.append((left, right, text[left:right]))

    for i, ch in enumerate(text):
        if quote_closers:
            if ch == quote_closers[-1]:
                quote_closers.pop()
            elif ch in quote_pairs and quote_pairs[ch] != ch:
                quote_closers.append(quote_pairs[ch])
        elif ch in quote_pairs:
            quote_closers.append(quote_pairs[ch])
        elif ch in ("(", "（", "【"):
            bracket_depth += 1
        elif ch in (")", "）", "】"):
            bracket_depth = max(0, bracket_depth - 1)

        if bracket_depth == 0 and not quote_closers and ch in ("。", "；", ";"):
            append_segment(start, i)
            start = i + 1
    append_segment(start, len(text))
    return results


def _classify_statement(text: str) -> str:
    """R2-1-1~2-1-7：六类 + 冲突优先级。

    判定顺序（显式优先隐式）：
      DEFINITION（事实动词，非目标语气）→ TARGET → PROHIBITION → OBLIGATION
      → PERMISSION → AUTHORIZATION（兜底：缺模态）
    """
    # DEFINITION：含事实动词且无目标语气，避免把“产量为X”误作目标
    if _looks_like_definition(text):
        return "DEFINITION"
    # TARGET：显式目标语态 + 量值，且高于 OBLIGATION
    if _looks_like_target(text):
        return "TARGET"
    # PROHIBITION
    if any(m in text for m in MODAL_PROHIBITION):
        return "PROHIBITION"
    # OBLIGATION（含“要+处置动词”弱义务语态）
    if any(m in text for m in MODAL_OBLIGATION) or OBLIGATION_WEAK.search(text):
        return "OBLIGATION"
    # PERMISSION
    if any(m in text for m in MODAL_PERMISSION):
        return "PERMISSION"
    # AUTHORIZATION：含 "由…批准/经…核准/需…同意/授权…" 且主体是机构
    if _is_authorization(text):
        return "AUTHORIZATION"
    # R1-1-4：缺模态兜底 → DEFINITION
    return "DEFINITION"


# R2-1-1 v2：“要+处置动词”作为弱义务语态（如“要及时落实补建”）
OBLIGATION_WEAK = re.compile(
    r"要(?:及时|严格|切实|认真|按照|按|在|于|将|加强|落实|确保|建立|完善)"
)

# R2-1-6 v2：显式目标语气（“到XXXX年…力争/确保” + 量值），不依赖“目标”二字
TARGET_MARKERS = ("目标", "指标", "定额", "上限", "下限")


def _looks_like_definition(text: str) -> bool:
    """R1-1-4/R2-1-5 v2：纯事实陈述 → DEFINITION。

    v2 收紧：必须有显式事实动词（为/定为/规定为/是指），
    不再仅凭“含数字”判定——否则“到2030年力争建成13.5亿亩”会被误判为定义而非目标。
    """
    all_modals = MODAL_OBLIGATION + MODAL_PROHIBITION + MODAL_PERMISSION
    if any(m in text for m in all_modals):
        return False
    return bool(re.search(r"为|定为|规定为|是指", text))


def _looks_like_target(text: str) -> bool:
    """R2-1-6 v2：显式目标关键词，或“到XXXX年/力争/确保”+ 量值 → TARGET。"""
    if not re.search(r"\d", text):
        return False
    if any(marker in text for marker in TARGET_MARKERS):
        return True
    return bool(re.search(r"力争|确保|到\s*\d{4}\s*年", text))


def _is_authorization(text: str) -> bool:
    """R2-1-4：区分 AUTHORIZATION 与 PERMISSION。"""
    # 必须同时满足：含 由/经/需 + 含 批准/核准/同意/授权/备案
    has_marker = any(m in text for m in ("由", "经", "需"))
    has_auth_word = any(
        w in text
        for w in ("批准", "核准", "同意", "授权", "备案", "审批", "审核")
    )
    return has_marker and has_auth_word


def _vagueness(text: str) -> tuple[bool, str]:
    """R3-1-1/3-1-2：冻结 10 词命中检测。"""
    hits = [term for term in VAGUE_TERMS if term in text]
    return (bool(hits), ",".join(hits) if hits else "")


def _extract_condition(text: str, target_object: str) -> dict[str, Any] | None:
    """R5-1-1/5-1-4：抽扁平 LEAF 条件。

    v2 扩展三类可判定条件，按"语义最具体优先"顺序尝试：
      1. 空间禁限类：facts.landType ∈ 文中点名的区域类型
      2. 污染防治类：facts.wasteType ∈ 禁止排放/倾倒的废弃物
      3. 对象类：facts.targetObject = 条款目标对象（兜底）
    三类都不命中时返回 None——不再一律套 targetObject，避免语义错位。
    """
    land_types = _extract_land_types(text)
    if land_types:
        return {
            "operator": "LEAF",
            "leftOperand": "facts.landType",
            "comparator": "∈",
            "rightValue": ",".join(land_types),
            "factSource": "USER_INPUT",
            "regionalScopeId": None,
        }
    waste_types = _extract_waste_types(text)
    if waste_types:
        return {
            "operator": "LEAF",
            "leftOperand": "facts.wasteType",
            "comparator": "∈",
            "rightValue": ",".join(waste_types),
            "factSource": "USER_INPUT",
            "regionalScopeId": None,
        }
    if not target_object:
        return None
    return {
        "operator": "LEAF",
        "leftOperand": "facts.targetObject",
        "comparator": "=",
        "rightValue": target_object,
        "factSource": "USER_INPUT",
        "regionalScopeId": None,
    }


# R5-1-4 v2：空间禁限区域类型（点名词表，按出现顺序去重）
LAND_TYPE_TERMS = (
    "25度以上坡耕地",
    "严格管控类耕地",
    "生态保护红线",
    "退耕还林还草还湖还牧区域",
    "生态脆弱区",
    "滩涂",
)

# R5-1-4 v2：禁止排放/倾倒的废弃物枚举（须由禁止性模态引导）
WASTE_VERBS = ("排放", "倾倒", "存放", "堆放")
WASTE_PATTERN = re.compile(
    r"(?:严禁|禁止|不得)(?:将|把|向|在)?"
    r"(?P<items>[^，。；、]{1,20}(?:、[^，。；、]{1,20}){0,8})(?:等)?"
    r"(?:" + "|".join(WASTE_VERBS) + r")"
)


def _extract_land_types(text: str) -> list[str]:
    """R5-1-4 v2：识别空间禁限条款点名的区域类型；无禁止/限制模态时不抽。"""
    if not any(m in text for m in ("禁止", "不得", "严禁", "严格限制", "不能")):
        return []
    found: list[str] = []
    for term in LAND_TYPE_TERMS:
        if term in text and term not in found:
            found.append(term)
    return found


def _extract_waste_types(text: str) -> list[str]:
    """R5-1-4 v2：识别"严禁将X、Y排放/倾倒…"的废弃物枚举。"""
    match = WASTE_PATTERN.search(text)
    if not match:
        return []
    items = [part.strip() for part in match.group("items").split("、") if part.strip()]
    if items and items[-1].endswith("等"):
        items[-1] = items[-1][:-1]
    return [item for item in items if item]


def _protected_mask(text: str) -> list[bool]:
    """标记括号/引号内字符，供例外连词与切分判断复用。"""
    mask = [False] * len(text)
    depth = 0
    closers: list[str] = []
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
    for i, ch in enumerate(text):
        if closers:
            mask[i] = True
            if ch == closers[-1]:
                closers.pop()
            elif ch in pairs and pairs[ch] != ch:
                closers.append(pairs[ch])
            continue
        if ch in pairs:
            mask[i] = True
            closers.append(pairs[ch])
            continue
        if ch in ("(", "（", "【"):
            depth += 1
            mask[i] = True
            continue
        if ch in (")", "）", "】"):
            depth = max(0, depth - 1)
            mask[i] = True
            continue
        mask[i] = depth > 0
    return mask


def _extract_exception(text: str) -> str | None:
    """R6-1-1 v2：仅当例外连词出现在括号/引号**之外**时才算独立子句。

    "（…除外）"这类括注是限定语，不是例外分支，不能抽成 ClauseException。
    """
    mask = _protected_mask(text)
    for i, ch in enumerate(text):
        if mask[i]:
            continue
        for connector in EXCEPTION_CONNECTORS:
            if text.startswith(connector, i) and i > 0:
                return text[i: i + 200].strip()
    return None


def _extract_parameter(text: str) -> list[dict[str, Any]]:
    """R6-1-4：抽可解析的数值参数。

    种子里 POL0001 的三条 pattern 固定："最低收购价为每50公斤128元"
    → paramName="最低收购价", paramValue=128, unitId="元/50公斤"
    """
    results: list[dict[str, Any]] = []
    # 人民币金额 pattern：每X公斤Y元 / 每X吨Y元 / X元/50公斤
    price_match = re.search(
        r"最低收购价(?:标准)?为每?(?:(\d+)\s*(?:公斤|千克|吨))?\s*(\d+(?:\.\d+)?)\s*元",
        text,
    )
    if price_match:
        weight = price_match.group(1) or "50"
        value = float(price_match.group(2))
        results.append({
            "paramName": "最低收购价",
            "paramValue": value,
            "unitId": f"元/{weight}公斤" if weight != "50" else "元/50公斤",
        })
    return results


def _extract_consequence(text: str, statement_type: str) -> dict[str, Any] | None:
    """R6-1-3：抽动作+量值（statementType 须为 OBLIGATION/PERMISSION）。

    DEFINITION 类的量值进 RuleParameter（已处理），不进 Consequence。
    """
    if statement_type not in ("OBLIGATION", "PERMISSION"):
        return None
    parameters = _extract_parameter(text)
    has_action = re.search(r"收购|执行|建立|报送|销售|储存|采购|入仓|出库|检验|监测|公开|备案", text)
    if parameters and has_action:
        return {
            "consequenceType": "MEASURE",
            "description": "执行最低收购价政策",
            "magnitude": parameters[0]["paramValue"],
        }
    return None


# --------------------------------------------------------------------------- Pack2bService

class Pack2bService:
    """阶段 2b 抽取服务：条款切分 → 六类分类 → vaguenessFlag → 条件/例外/后果/参数抽取 → 入库。"""

    def __init__(self, db: Database):
        self.db = db

    def extract(self, policy_id: str | None = None) -> dict[str, Any]:
        """从 Policy_Clause 抽取并入库；返回抽取统计。"""
        clauses = self.db.fetch_all(
            'SELECT * FROM "Policy_Clause"'
            + (' WHERE "policyId"=:pid' if policy_id else ""),
            {"pid": policy_id} if policy_id else None,
        )
        if not clauses:
            return {"clauses": 0, "statements": 0, "conditions": 0}

        stats = {"clauses": 0, "statements": 0, "conditions": 0, "exceptions": 0,
                 "consequences": 0, "parameters": 0, "relations": 0}
        with self.db.engine.begin() as conn:
            # 清理旧数据（先删子表再删父表）——与插入同事务
            self._purge(conn, clauses)
            for clause in clauses:
                stats["clauses"] += 1
                self._extract_clause(conn, clause, stats)
        return stats

    def _purge(self, conn: Any, clauses: list[dict[str, Any]]) -> None:
        """先完整预检，再仅清理可自动重建的 DRAFT 资产。"""
        clause_ids = [c["clauseId"] for c in clauses]
        if not clause_ids:
            return
        cid_ph = ",".join(f":cid{i}" for i in range(len(clause_ids)))
        cid_params = {f"cid{i}": cid for i, cid in enumerate(clause_ids)}
        result = conn.execute(
            sa_text(f'SELECT "statementId" FROM "NormativeStatement" '
                    f'WHERE "clauseId" IN ({cid_ph})'),
            cid_params,
        )
        statement_ids = [row[0] for row in result.fetchall()]
        if not statement_ids:
            return

        sid_ph = ",".join(f":sid{i}" for i in range(len(statement_ids)))
        sid_params = {f"sid{i}": sid for i, sid in enumerate(statement_ids)}
        protected: list[str] = []
        for table, pk in (
            ("NormativeStatement", "statementId"),
            ("CompiledRule", "ruleId"),
        ):
            rows = conn.execute(
                sa_text(f'SELECT "{pk}" FROM "{table}" WHERE "statementId" IN ({sid_ph}) '
                        f'AND COALESCE("confirmationStatus", \'\') <> \'DRAFT\''),
                sid_params,
            ).fetchall()
            protected.extend(row[0] for row in rows)
        testcase_rows = conn.execute(
            sa_text(f'SELECT "testcaseId" FROM "RuleTestCase" WHERE "statementId" IN ({sid_ph})'),
            sid_params,
        ).fetchall()
        protected.extend(row[0] for row in testcase_rows)
        if protected:
            raise ProtectedAssetError(protected)

        conn.execute(
            sa_text(f'DELETE FROM "CompiledRule" WHERE "statementId" IN ({sid_ph})'),
            sid_params,
        )
        for table in ("RuleParameter", "ClauseConsequence", "ClauseException", "ClauseCondition"):
            conn.execute(
                sa_text(f'DELETE FROM "{table}" WHERE "statementId" IN ({sid_ph})'),
                sid_params,
            )
        conn.execute(
            sa_text(f'DELETE FROM "NormativeStatement" WHERE "statementId" IN ({sid_ph})'),
            sid_params,
        )
        # ClauseRelation 本阶段不抽，保留不动

    def _extract_clause(self, conn: Any, clause: dict[str, Any], stats: dict[str, int]) -> None:
        text = str(clause.get("clauseText") or "")
        clause_id = str(clause["clauseId"])
        clause_no_raw = clause.get("clauseNo")
        clause_no = str(clause_no_raw) if clause_no_raw is not None else ""
        target_object = str(clause.get("targetObject") or "")
        statements = _split_clause(text)
        if not statements:
            return

        for char_start, char_end, subtext in statements:
            stats["statements"] += 1
            identity = json.dumps({
                "clauseId": clause_id,
                "charStart": char_start,
                "charEnd": char_end,
                "text": subtext,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            sid = "STM-" + hashlib.sha256(identity).hexdigest()[:32]

            stmt_type = _classify_statement(subtext)
            vague, vague_note = _vagueness(subtext)
            # R5-1-4c v2：TARGET 是"目标达成度"，不构成规则适用条件，故不产条件
            condition = None if stmt_type == "TARGET" else _extract_condition(subtext, target_object)
            consequence = _extract_consequence(subtext, stmt_type)
            parameters = _extract_parameter(subtext)
            exception_text = _extract_exception(subtext)

            # 条件树入库（扁平 LEAF）
            cid = None
            if condition:
                stats["conditions"] += 1
                cid = self.db.next_id("ClauseCondition", "CND", conn)
                self.db.insert("ClauseCondition", {
                    "conditionId": cid,
                    "statementId": sid,
                    "parentConditionId": None,
                    **condition,
                }, conn)

            # 例外入库（若有）
            exception_ids: list[str] = []
            if exception_text:
                stats["exceptions"] += 1
                eid = self.db.next_id("ClauseException", "EXC", conn)
                ecid = self.db.next_id("ClauseCondition", "CND", conn)
                self.db.insert("ClauseCondition", {
                    "conditionId": ecid,
                    "statementId": sid,
                    "parentConditionId": None,
                    "operator": "LEAF",
                    "leftOperand": "facts.exception",
                    "comparator": "=",
                    "rightValue": exception_text[:60],
                    "factSource": "USER_INPUT",
                    "regionalScopeId": None,
                }, conn)
                exception_ids.append(eid)
                self.db.insert("ClauseException", {
                    "exceptionId": eid,
                    "statementId": sid,
                    "conditionId": ecid,
                    "exceptionType": "PROCEDURAL",
                    "note": exception_text,
                }, conn)

            # 后果入库（若有）
            consequence_id = None
            if consequence:
                stats["consequences"] += 1
                consequence_id = self.db.next_id("ClauseConsequence", "CON", conn)
                self.db.insert("ClauseConsequence", {
                    "consequenceId": consequence_id,
                    "statementId": sid,
                    **consequence,
                }, conn)

            # 参数入库（若有）
            param_ids: list[str] = []
            for p in parameters:
                stats["parameters"] += 1
                pid = self.db.next_id("RuleParameter", "PRM", conn)
                param_ids.append(pid)
                self.db.insert("RuleParameter", {
                    "parameterId": pid,
                    "statementId": sid,
                    "effectiveFrom": clause.get("clauseEffectiveDate"),
                    **p,
                }, conn)

            # Statement 入库
            self.db.insert("NormativeStatement", {
                "statementId": sid,
                "clauseId": clause_id,
                "clauseNo": clause_no,
                "text": subtext,
                "charStart": char_start,
                "charEnd": char_end,
                "statementType": stmt_type,
                "actorScopeId": None,
                "actionVerb": None,
                "objectFormIds": None,
                "conditionId": cid,
                "exceptionIds": json.dumps(exception_ids, ensure_ascii=False),
                "consequenceId": consequence_id,
                "parameterIds": json.dumps(param_ids, ensure_ascii=False),
                "vaguenessFlag": 1 if vague else 0,
                "vaguenessNote": vague_note,
                "confirmationStatus": "DRAFT",  # R4-1-1
            }, conn)
