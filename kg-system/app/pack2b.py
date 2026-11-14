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
      TARGET（显式目标关键词+数字）→ PROHIBITION → OBLIGATION → PERMISSION
      → AUTHORIZATION → DEFINITION（兜底：缺模态）
    """
    # DEFINITION：无其他模态的事实/数量陈述优先，避免把“产量为X”误作目标
    if _looks_like_definition(text):
        return "DEFINITION"
    # TARGET：显式目标模态 + 量值，且高于 OBLIGATION
    if re.search(r"目标|指标|定额|上限|下限", text) and re.search(r"\d", text):
        return "TARGET"
    # PROHIBITION
    if any(m in text for m in MODAL_PROHIBITION):
        return "PROHIBITION"
    # OBLIGATION
    if any(m in text for m in MODAL_OBLIGATION):
        return "OBLIGATION"
    # PERMISSION
    if any(m in text for m in MODAL_PERMISSION):
        return "PERMISSION"
    # AUTHORIZATION：含 "由…批准/经…核准/需…同意/授权…" 且主体是机构
    if _is_authorization(text):
        return "AUTHORIZATION"
    # R1-1-4：缺模态兜底 → DEFINITION
    return "DEFINITION"


def _looks_like_definition(text: str) -> bool:
    """R1-1-4/R2-1-5：纯事实陈述 → DEFINITION。"""
    # 不含任何模态词 且 含数字（价格/数量表）
    all_modals = MODAL_OBLIGATION + MODAL_PROHIBITION + MODAL_PERMISSION
    if not any(m in text for m in all_modals):
        # 含数字 或 显式含"为/定为/规定为"等事实性动词
        if re.search(r"\d", text) or re.search(r"为|定为|规定为", text):
            return True
    return False


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
    """R5-1-1/5-1-4：抽扁平 LEAF 条件（品种类）。

    种子里 POL0001 的三条都是"2025年生产的早籼稻/中晚籼稻/粳稻最低收购价为X"，
    最显式的条件是 targetObject 匹配。
    """
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


def _extract_exception(text: str) -> str | None:
    """R6-1-1：显式含例外连词的子句。

    简化实现：返回文本中第一个例外连词之后的部分。
    """
    for connector in EXCEPTION_CONNECTORS:
        idx = text.find(connector)
        if idx > 0:
            # 取连词后 200 字符以内的子句
            return text[idx: idx + 200].strip()
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
            condition = _extract_condition(subtext, target_object)
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
