"""第六层（P2）规则运行时：测试用例执行与政策结论→预警联动。

- ``RuleTestRunner``：逐条运行 ``RuleTestCase``，与期望结果比对，输出可用于 CI 的报告。
- ``RuleAlertService``：把 ``ConclusionService`` 的判定结果写入 ``OpsAlert``：
  APPLICABLE → 打开告警；NOT_APPLICABLE → 关闭已有告警；UNABLE → 不动。

两个服务都只读取 CONFIRMED 的 CompiledRule，不写 CompiledRule；预警只写 OpsAlert。
"""
from __future__ import annotations

import json
from typing import Any

from app.conclusion import ConclusionService
from app.db import Database, utc_now


class RuleTestRunner:
    """运行 ``RuleTestCase`` 并比对期望结果。"""

    def __init__(self, db: Database, conclusion: ConclusionService) -> None:
        self.db = db
        self.conclusion = conclusion

    def _policy_info(self, statement_id: str) -> dict[str, Any] | None:
        return self.db.fetch_one(
            'SELECT pc."policyId", p."effectiveDate" FROM "NormativeStatement" ns '
            'JOIN "Policy_Clause" pc ON ns."clauseId"=pc."clauseId" '
            'JOIN "Policy" p ON pc."policyId"=p."policyId" '
            'WHERE ns."statementId"=:sid',
            {"sid": statement_id},
        )

    @staticmethod
    def _parse_facts(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if raw in (None, ""):
            return {}
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def run_testcase(self, testcase_id: str) -> dict[str, Any]:
        testcase = self.db.fetch_one(
            'SELECT * FROM "RuleTestCase" WHERE "testcaseId"=:tid', {"tid": testcase_id}
        )
        if not testcase:
            return {"testcaseId": testcase_id, "status": "MISSING", "passed": False}

        statement_id = str(testcase["statementId"])
        facts = self._parse_facts(testcase.get("facts"))
        info = self._policy_info(statement_id)
        if info is None:
            return {
                "testcaseId": testcase_id,
                "statementId": statement_id,
                "status": "NO_POLICY",
                "passed": False,
                "expected": testcase.get("expectedOutcome"),
                "actual": None,
            }

        as_of = info.get("effectiveDate") or "2025-06-01"
        try:
            result = self.conclusion.judge(facts=facts, as_of=as_of)
        except Exception as exc:  # noqa: BLE001 — 测试用例需捕获引擎异常并记录
            return {
                "testcaseId": testcase_id,
                "statementId": statement_id,
                "scenarioName": testcase.get("scenarioName"),
                "expected": testcase.get("expectedOutcome"),
                "actual": "ERROR",
                "passed": False,
                "error": str(exc),
            }

        verdict = next(
            (v for v in result.get("verdicts", []) if str(v.get("statementId")) == statement_id),
            None,
        )
        actual = verdict["outcome"] if verdict else "NO_VERDICT"
        expected = testcase.get("expectedOutcome")
        passed = actual == expected
        return {
            "testcaseId": testcase_id,
            "statementId": statement_id,
            "scenarioName": testcase.get("scenarioName"),
            "expected": expected,
            "actual": actual,
            "passed": passed,
            "reason": verdict.get("reason") if verdict else None,
        }

    def run_all(self) -> dict[str, Any]:
        rows = self.db.fetch_all('SELECT "testcaseId" FROM "RuleTestCase" ORDER BY "testcaseId"')
        results = [self.run_testcase(str(row["testcaseId"])) for row in rows]
        passed = sum(1 for r in results if r.get("passed"))
        return {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "results": results,
        }


class RuleAlertService:
    """把政策判定结果映射为 OpsAlert。"""

    def __init__(self, db: Database, conclusion: ConclusionService) -> None:
        self.db = db
        self.conclusion = conclusion

    @staticmethod
    def _alert_code(rule_id: str) -> str:
        return f"RULE-{rule_id}"

    def run(
        self,
        facts: dict[str, Any],
        as_of: str | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        verdict_result = self.conclusion.judge(facts=facts or {}, as_of=as_of, scope_id=scope_id)
        verdicts = verdict_result.get("verdicts", [])
        raised: list[str] = []
        closed: list[str] = []
        unchanged: list[str] = []
        moment = utc_now()

        with self.db.engine.begin() as conn:
            for verdict in verdicts:
                rule_id = verdict.get("ruleId")
                if not rule_id:
                    continue
                alert_code = self._alert_code(str(rule_id))
                outcome = verdict.get("outcome")
                active = self.db.fetch_one(
                    'SELECT * FROM "OpsAlert" WHERE "alertCode"=:code '
                    "AND \"status\" IN ('OPEN','ACK') ORDER BY \"raisedAt\" DESC LIMIT 1",
                    {"code": alert_code},
                    conn,
                )
                if outcome == "APPLICABLE":
                    if active is None:
                        self.db.insert(
                            "OpsAlert",
                            {
                                "alertId": self.db.next_id("OpsAlert", "ALT", conn),
                                "alertCode": alert_code,
                                "severity": "P2",
                                "title": f"政策触发：{verdict.get('clauseId')}",
                                "detail": json.dumps(
                                    {
                                        "ruleId": rule_id,
                                        "statementId": verdict.get("statementId"),
                                        "clauseId": verdict.get("clauseId"),
                                        "policyId": verdict.get("policyId"),
                                        "citation": verdict.get("citation"),
                                        "reason": verdict.get("reason"),
                                        "engine": verdict.get("engine"),
                                        "formalReasoning": verdict.get("formalReasoning"),
                                        "conditionMet": verdict.get("conditionMet"),
                                        "evidenceIds": verdict.get("evidenceIds"),
                                        "scopeNote": verdict.get("scopeNote"),
                                    },
                                    ensure_ascii=False,
                                ),
                                "metricCode": alert_code,
                                "metricValue": 1.0,
                                "status": "OPEN",
                                "raisedAt": moment,
                            },
                            conn,
                            actor="rule-alert",
                        )
                        raised.append(alert_code)
                    else:
                        unchanged.append(alert_code)
                elif outcome == "NOT_APPLICABLE":
                    if active is not None:
                        self.db.update(
                            "OpsAlert",
                            active["alertId"],
                            {"status": "CLOSED", "closedAt": moment},
                            conn,
                            actor="rule-alert",
                        )
                        closed.append(alert_code)
                    else:
                        unchanged.append(alert_code)
                else:  # UNABLE
                    unchanged.append(alert_code)

        return {
            "asOf": verdict_result.get("asOf"),
            "scopeId": verdict_result.get("scopeId"),
            "totalVerdicts": len(verdicts),
            "raised": raised,
            "closed": closed,
            "unchanged": unchanged,
        }
