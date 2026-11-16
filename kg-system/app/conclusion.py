"""结论层：statement 级 Nemo 正式推理与复数 API 的 v1 兼容 fallback。

Python 负责叶子条件三态求值、正式资格与证明链；Nemo 负责规则组合、
优先级传递闭包、覆盖和成环检测。引擎异常绝不静默转入 fallback。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.db import Database
from app.pack2c import load_tier1_registry, predicate_suffix, shared_nemo_rules
from app.policy import PolicyService, parse_date
from app.rule_engine import NemoEngine, ProgramBuilder, eval_condition


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
DISCLAIMER = "辅助研究，不构成法律意见或官方解释"


def _fact_name(group: str, status: str, suffix: str, index: int) -> str:
    return f"{group}_{status}_{suffix}_{index}"


class ConclusionService:
    def __init__(self, db: Database, policy_service: PolicyService):
        self.db = db
        self.policy = policy_service
        self._engine = NemoEngine()
        self._builder = ProgramBuilder(self._engine)
        registry = load_tier1_registry()
        self.tier1_policies = {entry["policyId"] for entry in registry["policies"]}

    def close(self) -> None:
        self._engine.close()

    def judge(
        self,
        facts: dict[str, Any],
        as_of: str | None = None,
        scope_id: str | None = None,
        max_hops: int = 3,
        formal_only: bool = False,
    ) -> dict[str, Any]:
        """按现行政策逐 statement 判定；复数接口可对非正式资产使用 v1。"""
        del max_hops  # 请求契约保留；当前规则闭包由 Nemo 完整求值，不做图遍历截断。
        facts = dict(facts or {})
        effective = self.policy.effective(as_of, limit=1000, scope_id=scope_id)
        target_day = parse_date(effective["asOf"])
        assert target_day is not None

        policy_ids = [item["policyId"] for item in effective["items"]]
        citation_by_policy = {item["policyId"]: item["citation"] for item in effective["items"]}
        policies_by_id = {item["policyId"]: item for item in effective["items"]}
        evidence_by_policy = {
            policy_id: self._evidence_ids(policies_by_id[policy_id]) for policy_id in policy_ids
        }
        scope_note = self._scope_note(effective)

        clauses = self._clauses(policy_ids)
        statements = self._statements_for(policy_ids)
        rules = self._compiled_rules_for(policy_ids)
        statements_by_clause: dict[str, list[dict[str, Any]]] = {}
        for statement in statements:
            statements_by_clause.setdefault(str(statement["clauseId"]), []).append(statement)

        pending: list[tuple[str, Any]] = []
        formal_contexts: list[dict[str, Any]] = []
        for clause in clauses:
            clause_statements = statements_by_clause.get(str(clause["clauseId"]), [])
            if not clause_statements:
                if not formal_only:
                    pending.append(("done", self._fallback_verdict(
                        clause=clause,
                        policy=policies_by_id.get(clause["policyId"], {}),
                        citation=citation_by_policy.get(clause["policyId"]),
                        facts=facts,
                        target_day=target_day,
                        statement=None,
                        evidence_ids=evidence_by_policy.get(clause["policyId"], []),
                        scope_note=scope_note,
                    )))
                continue

            for statement in clause_statements:
                statement_id = str(statement["statementId"])
                rule = rules.get(statement_id)
                eligible, qualification_reason = self._formal_qualification(statement, rule)
                common = {
                    "statement": statement,
                    "rule": rule,
                    "clause": clause,
                    "policy": policies_by_id.get(clause["policyId"], {}),
                    "citation": citation_by_policy.get(clause["policyId"]),
                    "evidence_ids": evidence_by_policy.get(clause["policyId"], []),
                    "scope_note": scope_note,
                }
                if not eligible:
                    if str(statement.get("statementType") or "") == "TARGET":
                        pending.append(("done", self._unqualified_verdict(
                            **common,
                            reason="目标类陈述（TARGET）仅描述建设目标，不作为适用性规则判定",
                        )))
                    elif formal_only:
                        pending.append(("done", self._unqualified_verdict(
                            **common, reason=qualification_reason
                        )))
                    else:
                        pending.append(("done", self._fallback_verdict(
                            clause=clause,
                            policy=common["policy"],
                            citation=common["citation"],
                            facts=facts,
                            target_day=target_day,
                            statement=statement,
                            evidence_ids=common["evidence_ids"],
                            scope_note=scope_note,
                        )))
                    continue
                context = self._formal_context(**common, facts=facts)
                formal_contexts.append(context)
                pending.append(("formal", context))

        engine_result = self._execute_nemo(formal_contexts)
        verdicts = [
            self._formal_verdict(item, engine_result) if kind == "formal" else item
            for kind, item in pending
        ]
        self._enforce_proof_gate(verdicts)

        engines = {str(verdict.get("engine") or "fallback-v1") for verdict in verdicts}
        if engines == {"nemo"}:
            top_engine = "nemo"
        elif engines and engines != {"fallback-v1"}:
            top_engine = "mixed"
        else:
            top_engine = "fallback-v1"
        return {
            "asOf": effective["asOf"],
            "scopeId": effective["scopeId"],
            "citationIndex": effective["citationIndex"],
            "facts": facts,
            "verdicts": verdicts,
            "engine": top_engine,
        }

    # ------------------------------------------------------------------ 数据读取

    def _clauses(self, policy_ids: list[str]) -> list[dict[str, Any]]:
        if not policy_ids:
            return []
        placeholders = ", ".join(f":p{i}" for i in range(len(policy_ids)))
        params = {f"p{i}": pid for i, pid in enumerate(policy_ids)}
        return self.db.fetch_all(
            'SELECT * FROM "Policy_Clause" WHERE "policyId" IN ('
            + placeholders + ') ORDER BY "policyId", "clauseNo", "clauseId"',
            params,
        )

    def _compiled_rules_for(self, policy_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not policy_ids:
            return {}
        placeholders = ",".join(f":pid{i}" for i in range(len(policy_ids)))
        params = {f"pid{i}": pid for i, pid in enumerate(policy_ids)}
        rows = self.db.fetch_all(
            'SELECT cr.* FROM "CompiledRule" cr '
            'JOIN "NormativeStatement" ns ON cr."statementId"=ns."statementId" '
            'JOIN "Policy_Clause" pc ON ns."clauseId"=pc."clauseId" '
            f'WHERE pc."policyId" IN ({placeholders}) '
            'ORDER BY cr."statementId", cr."ruleId"',
            params,
        )
        return {str(row["statementId"]): row for row in rows}

    def _statements_for(self, policy_ids: list[str]) -> list[dict[str, Any]]:
        if not policy_ids:
            return []
        placeholders = ",".join(f":pid{i}" for i in range(len(policy_ids)))
        params = {f"pid{i}": pid for i, pid in enumerate(policy_ids)}
        return self.db.fetch_all(
            'SELECT ns.*, pc."policyId" as "policyId", pc."targetObject" as "targetObject" '
            'FROM "NormativeStatement" ns '
            'JOIN "Policy_Clause" pc ON ns."clauseId"=pc."clauseId" '
            f'WHERE pc."policyId" IN ({placeholders}) '
            'ORDER BY pc."policyId", pc."clauseNo", ns."statementId"',
            params,
        )

    def _main_conditions(self, statement: dict[str, Any]) -> list[dict[str, Any]]:
        condition_id = statement.get("conditionId")
        if not condition_id:
            return []
        row = self.db.fetch_one(
            'SELECT * FROM "ClauseCondition" WHERE "conditionId"=:cid AND "statementId"=:sid',
            {"cid": condition_id, "sid": statement["statementId"]},
        )
        return [row] if row else []

    def _exception_for_statement(self, statement_id: str) -> dict[str, Any] | None:
        return self.db.fetch_one(
            'SELECT * FROM "ClauseException" WHERE "statementId"=:sid '
            'ORDER BY "exceptionId" LIMIT 1',
            {"sid": statement_id},
        )

    def _exception_condition(self, exception: dict[str, Any] | None) -> dict[str, Any] | None:
        if not exception:
            return None
        return self.db.fetch_one(
            'SELECT * FROM "ClauseCondition" WHERE "conditionId"=:cid AND "statementId"=:sid',
            {"cid": exception.get("conditionId"), "sid": exception["statementId"]},
        )

    # ------------------------------------------------------------------ 正式资格

    def _formal_qualification(
        self, statement: dict[str, Any], rule: dict[str, Any] | None
    ) -> tuple[bool, str]:
        problems: list[str] = []
        if statement["policyId"] not in self.tier1_policies:
            problems.append("政策不在一级清单")
        if (
            statement.get("confirmationStatus") != "CONFIRMED"
            or not statement.get("confirmedBy")
            or not statement.get("confirmedAt")
        ):
            problems.append("statement 未确认或缺确认人/确认时间")
        if not rule:
            problems.append("CompiledRule 缺失")
            return False, "；".join(problems)
        if rule.get("confirmationStatus") != "CONFIRMED":
            problems.append("rule 未确认")
        if rule.get("backTranslationMatch") is not True and rule.get("backTranslationMatch") != 1:
            problems.append("回译未确认一致")

        testcase_ids = self._testcase_ids(rule.get("testcaseIds"))
        if not testcase_ids:
            problems.append("测试用例缺失或无效")
        else:
            for testcase_id in testcase_ids:
                testcase = self.db.fetch_one(
                    'SELECT * FROM "RuleTestCase" WHERE "testcaseId"=:tid AND "statementId"=:sid',
                    {"tid": testcase_id, "sid": statement["statementId"]},
                )
                if not self._valid_testcase(testcase):
                    problems.append(f"测试用例缺失或无效：{testcase_id}")
        return not problems, "；".join(problems)

    @staticmethod
    def _testcase_ids(raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        try:
            value = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    @staticmethod
    def _valid_testcase(testcase: dict[str, Any] | None) -> bool:
        if not testcase or not testcase.get("authoredBy"):
            return False
        raw_facts = testcase.get("facts")
        if raw_facts in (None, ""):
            return False
        try:
            facts = json.loads(raw_facts) if isinstance(raw_facts, str) else raw_facts
        except json.JSONDecodeError:
            return False
        if not isinstance(facts, dict):
            return False
        expected = testcase.get("expectedOutcome")
        if expected not in {"APPLICABLE", "NOT_APPLICABLE", "UNABLE"}:
            return False
        if expected == "UNABLE" and not testcase.get("expectedReason"):
            return False
        return True

    # ------------------------------------------------------------------ Nemo 执行

    def _formal_context(
        self,
        statement: dict[str, Any],
        rule: dict[str, Any],
        clause: dict[str, Any],
        policy: dict[str, Any],
        citation: str | None,
        evidence_ids: list[str],
        scope_note: str,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        statement_id = str(statement["statementId"])
        suffix = predicate_suffix(statement_id)
        conditions = self._main_conditions(statement)
        condition_results = [eval_condition(condition, facts) for condition in conditions]
        exception = self._exception_for_statement(statement_id)
        exception_condition = self._exception_condition(exception)
        exception_result = (
            eval_condition(exception_condition, facts) if exception_condition is not None else None
        )

        derived: dict[str, list[tuple[Any, ...]]] = {}
        for index, result in enumerate(condition_results):
            status = "true" if result is True else "false" if result is False else "unknown"
            derived.setdefault(_fact_name("main_cond", status, suffix, index), []).append(("case0",))
        if exception_condition is not None:
            status = (
                "true" if exception_result is True
                else "false" if exception_result is False else "unknown"
            )
            derived.setdefault(_fact_name("exception_cond", status, suffix, 0), []).append(("case0",))

        return {
            "statement": statement,
            "rule": rule,
            "clause": clause,
            "policy": policy,
            "citation": citation,
            "evidence_ids": evidence_ids,
            "scope_note": scope_note,
            "conditions": conditions,
            "condition_results": condition_results,
            "exception": exception,
            "exception_condition": exception_condition,
            "exception_result": exception_result,
            "derived": derived,
        }

    def _execute_nemo(self, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        empty = {
            "error": None,
            "applicable": set(),
            "overridden": set(),
            "overridden_by": {},
            "cycles": set(),
        }
        if not contexts:
            return empty
        derived: dict[str, list[tuple[Any, ...]]] = {"case_exists": [("case0",)]}
        for context in contexts:
            for predicate, rows in context["derived"].items():
                derived.setdefault(predicate, []).extend(rows)

        statement_to_clause = {
            str(context["statement"]["statementId"]): str(context["clause"]["clauseId"])
            for context in contexts
        }
        clause_to_statements: dict[str, list[str]] = {}
        for statement_id, clause_id in statement_to_clause.items():
            clause_to_statements.setdefault(clause_id, []).append(statement_id)
        clause_ids = sorted(set(statement_to_clause.values()))
        relations: list[tuple[str, str]] = []
        if clause_ids:
            placeholders = ",".join(f":cid{i}" for i in range(len(clause_ids)))
            params = {f"cid{i}": clause_id for i, clause_id in enumerate(clause_ids)}
            rows = self.db.fetch_all(
                'SELECT * FROM "ClauseRelation" WHERE "relationType"=\'PREVAILS_OVER\' '
                f'AND "fromClauseId" IN ({placeholders}) AND "toClauseId" IN ({placeholders}) '
                'ORDER BY "relationId"',
                params,
            )
            for relation in rows:
                for high in clause_to_statements.get(str(relation["fromClauseId"]), []):
                    for low in clause_to_statements.get(str(relation["toClauseId"]), []):
                        relations.append((high, low))
        # prevails 是二元谓词；空关系也必须保持二元，避免 Nemo arity 冲突。
        derived["prevails"] = sorted(set(relations)) or [("__NONE__", "__NONE__")]
        rules_text = shared_nemo_rules().rstrip() + "\n" + "\n".join(
            str(context["rule"]["ruleExpression"]).strip() for context in contexts
        )
        try:
            output_rows = self._builder.run(derived, rules_text)
            overridden_rows = self._engine.result("overridden")
            overridden_by_rows = self._engine.result("overridden_by")
            cycle_rows = self._engine.result("cycle")
        except Exception as exc:
            return {**empty, "error": str(exc)}
        finally:
            # FastAPI 同步端点在工作线程执行；句柄必须在同一线程即时释放。
            self._engine.close()

        overridden_by: dict[str, list[str]] = {}
        for row in overridden_by_rows:
            overridden_by.setdefault(str(row.get("c1")), []).append(str(row.get("c2")))
        return {
            "error": None,
            "applicable": {
                str(row.get("c1")) for row in output_rows
                if row.get("c2") in (None, "APPLICABLE")
            },
            "overridden": {str(row.get("c1")) for row in overridden_rows},
            "overridden_by": overridden_by,
            "cycles": {str(row.get("c0")) for row in cycle_rows},
        }

    # ------------------------------------------------------------------ 正式证明链

    def _formal_verdict(
        self, context: dict[str, Any], engine_result: dict[str, Any]
    ) -> dict[str, Any]:
        statement = context["statement"]
        statement_id = str(statement["statementId"])
        proof: list[dict[str, Any]] = []
        missing = self._missing_facts(context["conditions"], context["condition_results"])
        notes: list[str] = []

        def verdict(outcome: str, reason: str, condition_met: bool | None):
            return self._verdict(
                str(context["clause"]["clauseId"]),
                str(context["clause"]["policyId"]),
                context["citation"],
                outcome,
                reason,
                missing,
                notes,
                proof,
                rule_id=str(context["rule"]["ruleId"]),
                statement_id=statement_id,
                engine="nemo",
                formal_reasoning=True,
                condition_met=condition_met,
                evidence_ids=context["evidence_ids"],
                scope_note=context["scope_note"],
            )

        self._step(context, proof, 1, "确认与现行有效")
        statement_text = str(statement.get("text") or context["clause"].get("clauseText") or "")
        hits = [term for term in VAGUE_TERMS if term in statement_text]
        if hits:
            self._step(context, proof, 2, "模糊性")
            return verdict("UNABLE", f"含模糊用语：{','.join(hits)}", None)
        self._step(context, proof, 2, "模糊性")

        results = context["condition_results"]
        condition_met = self._condition_met(results)
        fact_ref = self._first_fact_ref(context["conditions"])
        if any(result is None for result in results):
            self._step(context, proof, 3, "主条件", fact_ref)
            unknown = self._unknown_condition_labels(context["conditions"], results)
            return verdict("UNABLE", "缺事实或条件不可求值：" + "、".join(unknown), None)
        if any(result is False for result in results):
            self._step(context, proof, 3, "主条件", fact_ref)
            return verdict("NOT_APPLICABLE", "至少一个主条件不成立", False)
        self._step(context, proof, 3, "主条件", fact_ref)

        if context["exception"] is not None:
            exception_ref = self._first_fact_ref(
                [context["exception_condition"]] if context["exception_condition"] else []
            )
            self._step(context, proof, 4, "例外", exception_ref)
            if context["exception_result"] is None:
                return verdict("UNABLE", "例外条件缺事实或不可求值，无法排除例外", condition_met)
            if context["exception_result"] is True:
                return verdict("NOT_APPLICABLE", "例外条款命中", condition_met)
        else:
            self._step(context, proof, 4, "例外")

        if engine_result["error"]:
            self._step(context, proof, 5, "优先级")
            return verdict("UNABLE", f"Nemo 执行失败：{engine_result['error']}", condition_met)
        if statement_id in engine_result["cycles"]:
            self._step(context, proof, 5, "优先级")
            return verdict("UNABLE", "优先级关系成环，无法裁决", condition_met)
        if statement_id in engine_result["overridden"]:
            self._step(context, proof, 5, "优先级")
            high = "、".join(engine_result["overridden_by"].get(statement_id, []))
            suffix = f"（覆盖方：{high}）" if high else ""
            return verdict("NOT_APPLICABLE", "被更高优先级规则覆盖" + suffix, condition_met)
        if statement_id not in engine_result["applicable"]:
            self._step(context, proof, 5, "优先级")
            return verdict("UNABLE", "Nemo 未产生有效输出，无法确认规则结论", condition_met)
        self._step(context, proof, 5, "优先级")
        self._step(context, proof, 6, "结论")
        return verdict("APPLICABLE", "主条件全部成立、无例外触发且未被覆盖", True)

    def _unqualified_verdict(
        self,
        statement: dict[str, Any],
        rule: dict[str, Any] | None,
        clause: dict[str, Any],
        policy: dict[str, Any],
        citation: str | None,
        evidence_ids: list[str],
        scope_note: str,
        reason: str,
    ) -> dict[str, Any]:
        context = {
            "statement": statement,
            "rule": rule or {},
            "clause": clause,
            "policy": policy,
            "citation": citation,
        }
        proof: list[dict[str, Any]] = []
        self._step(context, proof, 1, "正式资格")
        return self._verdict(
            str(clause["clauseId"]), str(clause["policyId"]), citation,
            "UNABLE", reason, [], [], proof,
            rule_id=str(rule["ruleId"]) if rule else None,
            statement_id=str(statement["statementId"]),
            engine="nemo", formal_reasoning=False, condition_met=None,
            evidence_ids=evidence_ids, scope_note=scope_note,
        )

    @staticmethod
    def _condition_met(results: list[bool | None]) -> bool | None:
        if not results or any(result is None for result in results):
            return None
        return all(results)

    @staticmethod
    def _first_fact_ref(conditions: list[dict[str, Any]]) -> str | None:
        for condition in conditions:
            if condition and condition.get("leftOperand"):
                return str(condition["leftOperand"])
        return None

    @staticmethod
    def _fact_key(condition: dict[str, Any]) -> str:
        operand = str(condition.get("leftOperand") or "未知条件")
        return operand[len("facts."):] if operand.startswith("facts.") else operand

    def _missing_facts(
        self, conditions: list[dict[str, Any]], results: list[bool | None]
    ) -> list[str]:
        return [
            self._fact_key(condition)
            for condition, result in zip(conditions, results)
            if result is None
        ]

    def _unknown_condition_labels(
        self, conditions: list[dict[str, Any]], results: list[bool | None]
    ) -> list[str]:
        return [
            self._fact_key(condition) or f"条件 #{index}"
            for index, (condition, result) in enumerate(zip(conditions, results))
            if result is None
        ]

    @staticmethod
    def _step(
        context: dict[str, Any],
        proof: list[dict[str, Any]],
        step_no: int,
        label: str,
        fact_ref: str | None = None,
    ) -> None:
        clause = context["clause"]
        clause_no = str(clause.get("clauseNo") or "")
        rule = context.get("rule") or {}
        proof.append({
            "step": step_no,
            "ruleId": rule.get("ruleId"),
            "statementId": context["statement"].get("statementId"),
            "policyId": str(clause["policyId"]),
            "clauseId": str(clause["clauseId"]),
            "citation": context.get("citation"),
            "clausePath": f"第{clause_no}条" if clause_no else str(clause["clauseId"]),
            "clauseNo": clause_no,
            "factRef": fact_ref,
            "quote": str(clause.get("clauseText") or ""),
            "_label": label,
            "engine": "nemo",
        })

    # ------------------------------------------------------------------ v1 fallback

    def _fallback_verdict(
        self,
        clause: dict[str, Any],
        policy: dict[str, Any],
        citation: str | None,
        facts: dict[str, Any],
        target_day: date,
        statement: dict[str, Any] | None,
        evidence_ids: list[str],
        scope_note: str,
    ) -> dict[str, Any]:
        result = self._judge_clause_v1(clause, policy, citation, facts, target_day)
        condition_met = (
            True if result["outcome"] == "APPLICABLE"
            else False if result["outcome"] == "NOT_APPLICABLE" else None
        )
        result.update({
            "statementId": str(statement["statementId"]) if statement else None,
            "engine": "fallback-v1",
            "formalReasoning": False,
            "conditionMet": condition_met,
            "evidenceIds": evidence_ids,
            "scopeNote": scope_note,
            "chain": result["proofChain"],
        })
        return result

    def _judge_clause_v1(
        self,
        clause: dict[str, Any],
        policy: dict[str, Any],
        citation: str | None,
        facts: dict[str, Any],
        target_day: date,
    ) -> dict[str, Any]:
        """原 Policy_Clause 粒度六步判定，仅供复数 API 兼容。"""
        policy_id = str(clause["policyId"])
        clause_id = str(clause["clauseId"])
        clause_no = str(clause.get("clauseNo") or "")
        clause_text = str(clause.get("clauseText") or "")
        clause_title = str(clause.get("clauseTitle") or "")
        target_object = str(clause.get("targetObject") or "")
        clause_effective = parse_date(clause.get("clauseEffectiveDate"))

        missing: list[str] = []
        notes: list[str] = []
        proof: list[dict[str, Any]] = []

        def step(step_no: int, label: str, fact_ref: str | None) -> None:
            proof.append({
                "step": step_no,
                "ruleId": clause_id,
                "policyId": policy_id,
                "clauseId": clause_id,
                "citation": citation,
                "clausePath": f"第{clause_no}条" if clause_no else clause_id,
                "clauseNo": clause_no,
                "factRef": fact_ref,
                "quote": clause_text,
                "_label": label,
                "engine": "fallback-v1",
            })

        if not policy or policy.get("policyId") != policy_id:
            step(1, "规则可用", None)
            return self._verdict(clause_id, policy_id, citation, "UNABLE",
                                 "政策在 asOf 非现行有效", missing, notes, proof)
        step(1, "规则可用", None)
        hits = [term for term in VAGUE_TERMS if term in clause_text]
        if hits:
            step(2, "模糊用语", None)
            return self._verdict(clause_id, policy_id, citation, "UNABLE",
                                 f"含模糊用语：{','.join(hits)}", missing, notes, proof)
        step(2, "模糊用语", None)

        fact_commodity = facts.get("targetObject") or facts.get("commodity") or facts.get("commodityCode")
        if not fact_commodity:
            missing.append("品种")
            step(3, "前提", "facts.commodity")
            return self._verdict(clause_id, policy_id, citation, "UNABLE",
                                 "缺事实：品种", missing, notes, proof)
        if target_object and str(fact_commodity).strip() != target_object:
            step(3, "前提", "facts.commodity")
            return self._verdict(clause_id, policy_id, citation, "NOT_APPLICABLE",
                                 f"前提不满足：品种不匹配（{fact_commodity} vs {target_object}）",
                                 missing, notes, proof)
        if clause_effective is not None and target_day < clause_effective:
            step(3, "前提", "facts.asOf")
            return self._verdict(clause_id, policy_id, citation, "NOT_APPLICABLE",
                                 f"前提不满足：条款未生效（生效日 {clause_effective.isoformat()}）",
                                 missing, notes, proof)
        step(3, "前提", "facts.commodity")
        notes.append("未登记例外，未做例外消解")
        step(4, "例外", None)
        notes.append("未登记优先级关系，未做冲突裁决")
        step(5, "优先级", None)
        step(6, "结论", None)
        reason = f"条件全部成立且无例外触发（目标对象={target_object or '未限定'}，{clause_title or clause_text[:30]}）"
        return self._verdict(clause_id, policy_id, citation, "APPLICABLE",
                             reason, missing, notes, proof)

    # ------------------------------------------------------------------ 公共输出与最终硬门

    @staticmethod
    def _verdict(
        clause_id: str,
        policy_id: str,
        citation: str | None,
        outcome: str,
        reason: str,
        missing: list[str],
        notes: list[str],
        proof_chain: list[dict[str, Any]],
        rule_id: str | None = None,
        statement_id: str | None = None,
        engine: str = "fallback-v1",
        formal_reasoning: bool = False,
        condition_met: bool | None = None,
        evidence_ids: list[str] | None = None,
        scope_note: str = "",
    ) -> dict[str, Any]:
        cleaned = [
            {key: value for key, value in step.items() if not key.startswith("_")}
            for step in proof_chain
        ]
        return {
            "statementId": statement_id,
            "ruleId": rule_id or clause_id,
            "clauseId": clause_id,
            "policyId": policy_id,
            "citation": citation,
            "outcome": outcome,
            "conditionMet": condition_met,
            "officiallyActivated": "UNKNOWN",
            "activationEvidenceId": None,
            "reason": reason,
            "missing": missing,
            "notes": notes,
            "chain": cleaned,
            "proofChain": cleaned,
            "evidenceIds": list(evidence_ids or []),
            "scopeNote": scope_note,
            "disclaimer": DISCLAIMER,
            "engine": engine,
            "formalReasoning": formal_reasoning,
        }

    @staticmethod
    def _enforce_proof_gate(verdicts: list[dict[str, Any]]) -> None:
        required = {
            "step", "ruleId", "policyId", "clauseId", "citation",
            "clausePath", "clauseNo", "factRef", "quote",
        }
        for verdict in verdicts:
            chain = verdict.get("chain")
            if chain is None:
                chain = verdict.get("proofChain") or []
                verdict["chain"] = chain
            verdict["proofChain"] = chain
            if verdict.get("outcome") != "APPLICABLE":
                continue
            complete = [step.get("step") for step in chain] == [1, 2, 3, 4, 5, 6]
            complete = complete and all(
                required.issubset(step) and step.get("citation") and step.get("quote")
                for step in chain
            )
            if not complete:
                verdict["outcome"] = "UNABLE"
                verdict["reason"] = "证明链不完整，APPLICABLE 已降级为 UNABLE"

    def _evidence_ids(self, policy: dict[str, Any]) -> list[str]:
        source_id = policy.get("sourceDocId")
        if not source_id:
            return []
        row = self.db.fetch_one(
            'SELECT "docId" FROM "SourceDoc" WHERE "docId"=:doc_id', {"doc_id": source_id}
        )
        return [str(row["docId"])] if row else []

    @staticmethod
    def _scope_note(effective: dict[str, Any]) -> str:
        scope = effective.get("scopeId") or "未限定"
        return (
            f"本次实际检索到 {effective['total']} 份已收录且现行有效的政策；"
            f"查询范围：{scope}；未收录或未成现行有效的文件不参与本次推理，"
            "不代表官方完整政策清单。"
        )
