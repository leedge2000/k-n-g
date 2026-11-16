import json

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-rule-runtime.db'}",
        jwt_secret="test-secret-with-at-least-32-bytes-for-hs256",
        auto_seed=True,
        graph_backend="relational",
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def auth(settings, role="ROLE-04"):
    return {"Authorization": f"Bearer {issue_token('policy-user', role, settings.jwt_secret)}"}


def _prepare_rules(client, settings, clause_ids=None):
    client.post("/api/policies/extract-2b", json={"policyId": "POL0001"}, headers=auth(settings))
    client.post("/api/policies/compile-2c", json={"policyId": "POL0001"}, headers=auth(settings))
    db = client.app.state.db
    statements = db.fetch_all('SELECT * FROM "NormativeStatement" ORDER BY "statementId"')
    with db.engine.begin() as conn:
        for statement in statements:
            if clause_ids is not None and statement["clauseId"] not in clause_ids:
                continue
            statement_id = statement["statementId"]
            testcase_id = "RTC-RT-" + statement_id
            db.insert("RuleTestCase", {
                "testcaseId": testcase_id,
                "statementId": statement_id,
                "scenarioName": "rt-smoke",
                "facts": json.dumps({"targetObject": "早籼稻"}, ensure_ascii=False),
                "expectedOutcome": "UNABLE",
                "expectedReason": "runtime smoke",
                "authoredBy": "rt-smoke",
            }, conn, actor="rt-smoke")
            db.update("NormativeStatement", statement_id, {
                "confirmationStatus": "CONFIRMED",
                "confirmedBy": "rt-smoke",
                "confirmedAt": "2026-11-14T00:00:00Z",
            }, conn, actor="rt-smoke")
            rule = db.fetch_one(
                'SELECT * FROM "CompiledRule" WHERE "statementId"=:sid', {"sid": statement_id}, conn
            )
            db.update("CompiledRule", rule["ruleId"], {
                "confirmationStatus": "CONFIRMED",
                "backTranslationMatch": 1,
                "testcaseIds": json.dumps([testcase_id]),
            }, conn, actor="rt-smoke")


def test_rule_alert_raises_on_applicable(client, settings):
    rule_alert = client.app.state.rule_alert
    result = rule_alert.run(facts={"targetObject": "早籼稻"}, as_of="2025-06-01")
    assert result["totalVerdicts"] >= 1
    assert any(code.startswith("RULE-POL0001-C001") for code in result["raised"])

    db = client.app.state.db
    alert = db.fetch_one(
        'SELECT * FROM "OpsAlert" WHERE "alertCode"=:code ORDER BY "raisedAt" DESC LIMIT 1',
        {"code": "RULE-POL0001-C001"},
    )
    assert alert is not None
    assert alert["status"] == "OPEN"
    assert alert["severity"] == "P2"


def test_rule_alert_idempotent_on_repeated_applicable(client, settings):
    rule_alert = client.app.state.rule_alert
    rule_alert.run(facts={"targetObject": "早籼稻"}, as_of="2025-06-01")
    result = rule_alert.run(facts={"targetObject": "早籼稻"}, as_of="2025-06-01")
    # 第二次应该不重复开告警
    assert not any(code.startswith("RULE-POL0001-C001") for code in result["raised"])

    db = client.app.state.db
    open_alerts = db.fetch_all(
        'SELECT "alertId" FROM "OpsAlert" WHERE "alertCode"=:code AND "status"=\'OPEN\'',
        {"code": "RULE-POL0001-C001"},
    )
    assert len(open_alerts) == 1


def test_rule_alert_closes_on_not_applicable(client, settings):
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.insert("OpsAlert", {
            "alertId": db.next_id("OpsAlert", "ALT", conn),
            "alertCode": "RULE-POL0001-C001",
            "severity": "P2",
            "title": "pre-existing open alert",
            "status": "OPEN",
            "raisedAt": "2026-11-14T00:00:00Z",
        }, conn, actor="test")

    rule_alert = client.app.state.rule_alert
    # 中晚籼稻不匹配 C001（早籼稻）→ NOT_APPLICABLE
    result = rule_alert.run(facts={"targetObject": "中晚籼稻"}, as_of="2025-06-01")
    assert "RULE-POL0001-C001" in result["closed"]

    alert = db.fetch_one(
        'SELECT * FROM "OpsAlert" WHERE "alertCode"=:code ORDER BY "raisedAt" DESC LIMIT 1',
        {"code": "RULE-POL0001-C001"},
    )
    assert alert["status"] == "CLOSED"
    assert alert["closedAt"] is not None


def test_rule_test_runner_runs_all(client, settings):
    _prepare_rules(client, settings)
    runner = client.app.state.rule_runner
    report = runner.run_all()
    assert report["total"] == 3
    assert report["passed"] + report["failed"] == 3
    # 早籼稻命中 C001 → APPLICABLE，而测试用例期望 UNABLE，所以 C001 应为 failed
    by_id = {r["testcaseId"]: r for r in report["results"]}
    c001 = by_id["RTC-RT-" + str(
        client.app.state.db.fetch_one(
            'SELECT "statementId" FROM "NormativeStatement" WHERE "clauseId"=:cid',
            {"cid": "POL0001-C001"},
        )["statementId"]
    )]
    assert c001["actual"] == "APPLICABLE"
    assert c001["passed"] is False


def test_rule_test_runner_missing_testcase(client, settings):
    runner = client.app.state.rule_runner
    result = runner.run_testcase("RTC-NOT-EXIST")
    assert result["status"] == "MISSING"
    assert result["passed"] is False
