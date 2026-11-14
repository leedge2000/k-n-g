"""阶段 2c：将条款陈述编译为可审计的 Nemo 规则。

Python 负责叶子条件三态求值，Nemo 负责规则组合、例外、优先级闭包和成环
检测。自动编译结果始终为 DRAFT；本模块不执行专家确认。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import text as sa_text

from app.db import Database
from app.pack2b import ProtectedAssetError


COMPILER_VERSION = "pack2c-nemo-v1"
ONTOLOGY_VERSION = "stage0-final-v1"
POLICY_PACK_DIR = Path(__file__).resolve().parents[1] / "ontology_ly" / "packs" / "policy"
TIER1_REGISTRY_PATH = POLICY_PACK_DIR / "tier1_policy_registry.json"
SHARED_RULE_ASSET = POLICY_PACK_DIR / "rules" / "stage2_nemo.rls"

# 已支持的扁平事实属性；indicatorCode 另外从 Indicator 表动态核验。
SUPPORTED_FACT_PATHS = {
    "facts.targetObject",
    "facts.price",
    "facts.region",
    "facts.date",
    "facts.grade",
    "facts.localReserveActive",
    "facts.provinceRulePublished",
    "facts.emergencyLevel",
    "facts.tags",
}


def load_tier1_registry() -> dict[str, Any]:
    return json.loads(TIER1_REGISTRY_PATH.read_text(encoding="utf-8"))


def shared_nemo_rules() -> str:
    return SHARED_RULE_ASSET.read_text(encoding="utf-8")


def rule_id_for(statement_id: str) -> str:
    return "RULE-" + hashlib.sha256(statement_id.encode("utf-8")).hexdigest()[:32]


def predicate_suffix(statement_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", statement_id.lower())


def _fact_name(group: str, status: str, suffix: str, index: int | None = None) -> str:
    base = f"{group}_{status}_{suffix}"
    return base if index is None else f"{base}_{index}"


def _statement_rules(
    statement_id: str,
    conditions: list[dict[str, Any]],
    exception_condition: dict[str, Any] | None,
    parameters: list[dict[str, Any]],
) -> str:
    """生成 statement 私有谓词及 fires 规则，不写入任何代码内政策阈值。"""
    suffix = predicate_suffix(statement_id)
    sid = json.dumps(statement_id, ensure_ascii=False)
    lines: list[str] = []

    true_parts = [
        f"{_fact_name('main_cond', 'true', suffix, i)}(?c)"
        for i in range(len(conditions))
    ]
    lines.append(f"{_fact_name('main', 'true', suffix)}(?c) :- " + ", ".join(true_parts) + " .")
    for status in ("false", "unknown"):
        for i in range(len(conditions)):
            body = f"{_fact_name('main_cond', status, suffix, i)}(?c)"
            if status == "unknown":
                body += f", ~{_fact_name('main', 'false', suffix)}(?c)"
            lines.append(f"{_fact_name('main', status, suffix)}(?c) :- {body} .")

    if exception_condition is None:
        lines.append(f"{_fact_name('exception', 'false', suffix)}(?c) :- case_exists(?c) .")
    else:
        lines.append(
            f"{_fact_name('exception', 'true', suffix)}(?c) :- "
            f"{_fact_name('exception_cond', 'true', suffix, 0)}(?c) ."
        )
        lines.append(
            f"{_fact_name('exception', 'false', suffix)}(?c) :- "
            f"{_fact_name('exception_cond', 'false', suffix, 0)}(?c) ."
        )
        lines.append(
            f"{_fact_name('exception', 'unknown', suffix)}(?c) :- "
            f"{_fact_name('exception_cond', 'unknown', suffix, 0)}(?c), "
            f"~{_fact_name('exception', 'true', suffix)}(?c) ."
        )

    lines.append(
        f"fires(?c, {sid}) :- case_exists(?c), "
        f"{_fact_name('main', 'true', suffix)}(?c), "
        f"{_fact_name('exception', 'false', suffix)}(?c) ."
    )
    for parameter in parameters:
        values = [
            statement_id,
            str(parameter.get("paramName") or ""),
            str(parameter.get("paramValue") or ""),
            str(parameter.get("unitId") or ""),
        ]
        lines.append(
            "rule_parameter(" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + ") ."
        )
    return "\n".join(lines)


class Pack2cService:
    """NormativeStatement → Nemo ruleExpression → DRAFT CompiledRule。"""

    def __init__(self, db: Database):
        self.db = db
        registry = load_tier1_registry()
        self.registry_version = registry["registryVersion"]
        self.tier1_policies = {entry["policyId"]: entry for entry in registry["policies"]}

    def compile(self, policy_id: str | None = None) -> dict[str, Any]:
        statements = self._fetch_statements(policy_id)
        stats: dict[str, Any] = {
            "statements": len(statements),
            "compiled": 0,
            "skipped_no_conditions": 0,
            "skipped_not_tier1": 0,
            "rejections": [],
            "registryVersion": self.registry_version,
        }
        if not statements:
            return stats

        with self.db.engine.begin() as conn:
            self._assert_no_protected_assets(conn, statements)
            for statement in statements:
                statement_id = str(statement["statementId"])
                if statement["policyId"] not in self.tier1_policies:
                    stats["skipped_not_tier1"] += 1
                    self._delete_stale_drafts(conn, statement_id)
                    continue

                rule, rejection = self._compile_one(statement)
                if rejection == "NO_CONDITIONS":
                    stats["skipped_no_conditions"] += 1
                    self._delete_stale_drafts(conn, statement_id)
                    continue
                if rejection is not None:
                    stats["rejections"].append({"statementId": statement_id, "reason": rejection})
                    self._delete_stale_drafts(conn, statement_id)
                    continue

                assert rule is not None
                self._delete_stale_drafts(conn, statement_id, keep_rule_id=rule["ruleId"])
                existing = self.db.fetch_one(
                    'SELECT "ruleId" FROM "CompiledRule" WHERE "ruleId"=:rid',
                    {"rid": rule["ruleId"]},
                    conn,
                )
                if existing:
                    self.db.update(
                        "CompiledRule", rule["ruleId"], rule, conn, actor="pack2c-compiler"
                    )
                else:
                    self.db.insert("CompiledRule", rule, conn, actor="pack2c-compiler")
                stats["compiled"] += 1
        return stats

    def _fetch_statements(self, policy_id: str | None) -> list[dict[str, Any]]:
        sql = (
            'SELECT ns.*, pc."policyId" as "policyId", pc."targetObject" as "targetObject" '
            'FROM "NormativeStatement" ns '
            'JOIN "Policy_Clause" pc ON ns."clauseId" = pc."clauseId"'
        )
        params: dict[str, Any] = {}
        if policy_id:
            sql += ' WHERE pc."policyId"=:pid'
            params["pid"] = policy_id
        sql += ' ORDER BY ns."statementId"'
        return self.db.fetch_all(sql, params)

    def _assert_no_protected_assets(self, conn: Any, statements: list[dict[str, Any]]) -> None:
        ids = [str(statement["statementId"]) for statement in statements]
        placeholders = ",".join(f":sid{i}" for i in range(len(ids)))
        params = {f"sid{i}": sid for i, sid in enumerate(ids)}
        protected = [
            str(statement["statementId"])
            for statement in statements
            if statement.get("confirmationStatus") != "DRAFT"
        ]
        rule_rows = conn.execute(
            sa_text(
                f'SELECT "ruleId", "confirmationStatus" FROM "CompiledRule" '
                f'WHERE "statementId" IN ({placeholders})'
            ),
            params,
        ).mappings().all()
        protected.extend(
            str(row["ruleId"]) for row in rule_rows if row["confirmationStatus"] != "DRAFT"
        )
        testcase_rows = conn.execute(
            sa_text(f'SELECT "testcaseId" FROM "RuleTestCase" WHERE "statementId" IN ({placeholders})'),
            params,
        ).all()
        protected.extend(str(row[0]) for row in testcase_rows)
        if protected:
            raise ProtectedAssetError(protected)

    def _main_conditions(self, statement: dict[str, Any]) -> list[dict[str, Any]]:
        condition_id = statement.get("conditionId")
        if not condition_id:
            return []
        row = self.db.fetch_one(
            'SELECT * FROM "ClauseCondition" WHERE "conditionId"=:cid AND "statementId"=:sid',
            {"cid": condition_id, "sid": statement["statementId"]},
        )
        return [row] if row else []

    def _parameters_for(self, statement_id: str) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            'SELECT * FROM "RuleParameter" WHERE "statementId"=:sid ORDER BY "parameterId"',
            {"sid": statement_id},
        )

    def _exception_for(self, statement_id: str) -> dict[str, Any] | None:
        return self.db.fetch_one(
            'SELECT * FROM "ClauseException" WHERE "statementId"=:sid ORDER BY "exceptionId" LIMIT 1',
            {"sid": statement_id},
        )

    def _exception_condition(self, exception: dict[str, Any] | None) -> dict[str, Any] | None:
        if not exception:
            return None
        return self.db.fetch_one(
            'SELECT * FROM "ClauseCondition" WHERE "conditionId"=:cid AND "statementId"=:sid',
            {"cid": exception["conditionId"], "sid": exception["statementId"]},
        )

    def _registered_indicator_codes(self) -> set[str]:
        return {
            str(row["indicatorCode"])
            for row in self.db.fetch_all('SELECT "indicatorCode" FROM "Indicator"')
        }

    def _invalid_operand(
        self, conditions: list[dict[str, Any]], indicator_codes: set[str]
    ) -> str | None:
        for condition in conditions:
            operand = str(condition.get("leftOperand") or "")
            if operand not in SUPPORTED_FACT_PATHS and operand not in indicator_codes:
                return operand or "<empty>"
        return None

    def _compile_one(
        self, statement: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        statement_id = str(statement["statementId"])
        conditions = self._main_conditions(statement)
        if not conditions:
            return None, "NO_CONDITIONS"
        parameters = self._parameters_for(statement_id)
        exception = self._exception_for(statement_id)
        exception_condition = self._exception_condition(exception)
        check_conditions = conditions + ([exception_condition] if exception_condition else [])
        invalid = self._invalid_operand(check_conditions, self._registered_indicator_codes())
        if invalid is not None:
            return None, f"未登记的 leftOperand：{invalid}"
        if exception and exception_condition is None:
            return None, "例外条件根不存在或不可读取"

        rule_expression = _statement_rules(
            statement_id, conditions, exception_condition, parameters
        )
        return {
            "ruleId": rule_id_for(statement_id),
            "statementId": statement_id,
            "ruleExpression": rule_expression,
            "compilerVersion": COMPILER_VERSION,
            "ontologyVersion": ONTOLOGY_VERSION,
            "backTranslation": self._generate_back_translation(
                statement, conditions, parameters, exception, exception_condition
            ),
            "backTranslationMatch": None,
            "testcaseIds": json.dumps([], ensure_ascii=False),
            "confirmationStatus": "DRAFT",
            "dependsOnClauseIds": json.dumps([statement["clauseId"]], ensure_ascii=False),
        }, None

    def _generate_back_translation(
        self,
        statement: dict[str, Any],
        conditions: list[dict[str, Any]],
        parameters: list[dict[str, Any]],
        exception: dict[str, Any] | None,
        exception_condition: dict[str, Any] | None,
    ) -> str:
        text = str(statement["text"])
        parts = [f"[{statement['statementType']}] {text[:80]}{'…' if len(text) > 80 else ''}"]
        if conditions:
            parts.append("触发条件：")
            for index, condition in enumerate(conditions):
                parts.append(
                    f"  #{index} {condition.get('leftOperand') or '?'} "
                    f"{condition.get('comparator') or '='} {condition.get('rightValue') or '?'}"
                )
        if parameters:
            parts.append("参数：")
            for parameter in parameters:
                parts.append(
                    f"  {parameter['paramName']} = {parameter['paramValue']} "
                    f"{parameter.get('unitId') or ''}".rstrip()
                )
        if exception:
            parts.append(f"例外：{exception.get('note') or exception.get('exceptionType') or '未登记'}")
            if exception_condition:
                parts.append(
                    f"  {exception_condition.get('leftOperand') or '?'} "
                    f"{exception_condition.get('comparator') or '='} "
                    f"{exception_condition.get('rightValue') or '?'}"
                )
        return "\n".join(parts)

    def _delete_stale_drafts(
        self, conn: Any, statement_id: str, keep_rule_id: str | None = None
    ) -> None:
        sql = (
            'DELETE FROM "CompiledRule" WHERE "statementId"=:sid '
            'AND "confirmationStatus"=\'DRAFT\''
        )
        params: dict[str, Any] = {"sid": statement_id}
        if keep_rule_id:
            sql += ' AND "ruleId"<>:keep_id'
            params["keep_id"] = keep_rule_id
        conn.execute(sa_text(sql), params)
