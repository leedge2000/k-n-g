import json

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-conclusion.db'}",
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


def _insert_clause(client, **overrides):
    row = {
        "policyId": "POL0001",
        "clauseId": "POL0001-C999",
        "clauseNo": 99,
        "clauseTitle": "模糊用语条款",
        "clauseText": "必要时由中储粮开展收购。",
        "targetObject": "早籼稻",
        "measureType": "最低收购价",
        "clauseEffectiveDate": "2025-01-01",
    }
    row.update(overrides)
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.insert("Policy_Clause", row, conn)


def test_judge_applicable_and_not_applicable(client, settings):
    response = client.post(
        "/api/policies/judge",
        json={
            "asOf": "2025-06-01",
            "facts": {"commodity": "早籼稻", "price": 126},
        },
        headers=auth(settings),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["asOf"] == "2025-06-01"
    assert body["scopeId"] is None

    by_clause = {v["clauseId"]: v for v in body["verdicts"]}
    assert by_clause["POL0001-C001"]["outcome"] == "APPLICABLE"
    assert by_clause["POL0001-C002"]["outcome"] == "NOT_APPLICABLE"
    assert by_clause["POL0001-C003"]["outcome"] == "NOT_APPLICABLE"
    assert "品种不匹配" in by_clause["POL0001-C002"]["reason"]

    # APPLICABLE 必须带 6 步完整证明链
    chain = by_clause["POL0001-C001"]["proofChain"]
    assert [step["step"] for step in chain] == [1, 2, 3, 4, 5, 6]
    assert chain[5]["quote"] == "2025年生产的早籼稻最低收购价为每50公斤128元。"
    assert chain[0]["citation"] == "P1"
    assert chain[0]["policyId"] == "POL0001"
    assert chain[0]["clauseId"] == "POL0001-C001"
    assert chain[0]["clauseNo"] == "1"


def test_judge_citation_index_backwards(client, settings):
    response = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    body = response.json()
    idx = body["citationIndex"]
    assert idx["P1"] == "POL0001"
    for verdict in body["verdicts"]:
        if verdict["policyId"] == "POL0001":
            assert verdict["citation"] == "P1"
            for step in verdict["proofChain"]:
                assert idx[step["citation"]] == step["policyId"]


def test_judge_quote_is_clause_text_substring(client, settings):
    response = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    body = response.json()
    # 从 DB 原文取 clauseText
    db = client.app.state.db
    texts = {
        row["clauseId"]: str(row["clauseText"])
        for row in db.fetch_all('SELECT "clauseId","clauseText" FROM "Policy_Clause"')
    }
    for verdict in body["verdicts"]:
        for step in verdict["proofChain"]:
            clause_text = texts.get(step["clauseId"], "")
            assert step["quote"] in clause_text


def test_judge_unable_when_policy_not_current(client, settings):
    response = client.post(
        "/api/policies/judge",
        json={"asOf": "2024-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    body = response.json()
    # POL0001 在 2024-06-01 未生效 → 现行政策列表空 → 无 verdicts
    assert body["verdicts"] == []


def test_judge_unable_when_missing_fact(client, settings):
    response = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {}},
        headers=auth(settings),
    )
    body = response.json()
    by_clause = {v["clauseId"]: v for v in body["verdicts"]}
    for cid, v in by_clause.items():
        assert v["outcome"] == "UNABLE"
        assert "缺事实：品种" in v["reason"]
        assert v["missing"] == ["品种"]
        # 证明链应停在第 3 步，不进入步骤 4-6
        assert [s["step"] for s in v["proofChain"]] == [1, 2, 3]


def test_judge_unable_on_vague_terms(client, settings):
    _insert_clause(client, clauseText="必要时由有关部门采取适当措施。", targetObject="早籼稻")
    response = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    body = response.json()
    vague = next(v for v in body["verdicts"] if v["clauseId"] == "POL0001-C999")
    assert vague["outcome"] == "UNABLE"
    assert "模糊用语" in vague["reason"]
    assert [s["step"] for s in vague["proofChain"]] == [1, 2]


def test_judge_rejects_unknown_scope(client, settings):
    response = client.post(
        "/api/policies/judge",
        json={"scopeId": "SCP-PUB-UNKNOWN", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    assert response.status_code == 422
    assert "范围不存在" in response.json()["detail"]


def test_judge_citation_matches_effective(client, settings):
    judge_body = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    ).json()
    eff_body = client.get(
        "/api/policies/effective",
        params={"asOf": "2025-06-01"},
        headers=auth(settings),
    ).json()
    assert judge_body["citationIndex"] == eff_body["citationIndex"]
    # POL0001 在两边必须同 citation
    judge_p1 = judge_body["citationIndex"]["P1"]
    eff_p1 = eff_body["citationIndex"]["P1"]
    assert judge_p1 == eff_p1 == "POL0001"


# ==================================================================== 阶段 2 statement 级正式推理

def _extract(client, settings):
    response = client.post(
        "/api/policies/extract-2b", json={"policyId": "POL0001"}, headers=auth(settings)
    )
    assert response.status_code == 200


def _compile(client, settings):
    response = client.post(
        "/api/policies/compile-2c", json={"policyId": "POL0001"}, headers=auth(settings)
    )
    assert response.status_code == 200


def _confirm_compiled(client, clause_ids=None):
    """临时 technical-smoke 状态，只验证正式路径开关，不代表专家确认。"""
    db = client.app.state.db
    statements = db.fetch_all('SELECT * FROM "NormativeStatement" ORDER BY "statementId"')
    with db.engine.begin() as conn:
        for statement in statements:
            if clause_ids is not None and statement["clauseId"] not in clause_ids:
                continue
            statement_id = statement["statementId"]
            testcase_id = "RTC-TECH-" + statement_id
            db.insert("RuleTestCase", {
                "testcaseId": testcase_id,
                "statementId": statement_id,
                "scenarioName": "technical-smoke",
                "facts": json.dumps({"targetObject": "早籼稻"}, ensure_ascii=False),
                "expectedOutcome": "UNABLE",
                "expectedReason": "technical-smoke，不是专家金标",
                "authoredBy": "technical-smoke",
            }, conn, actor="technical-smoke")
            db.update("NormativeStatement", statement_id, {
                "confirmationStatus": "CONFIRMED",
                "confirmedBy": "technical-smoke",
                "confirmedAt": "2026-11-14T00:00:00Z",
            }, conn, actor="technical-smoke")
            rule = db.fetch_one(
                'SELECT * FROM "CompiledRule" WHERE "statementId"=:sid', {"sid": statement_id}, conn
            )
            db.update("CompiledRule", rule["ruleId"], {
                "confirmationStatus": "CONFIRMED",
                "backTranslationMatch": 1,
                "testcaseIds": json.dumps([testcase_id]),
            }, conn, actor="technical-smoke")


def _prepare_rules(client, settings, clause_ids=None):
    _extract(client, settings)
    _compile(client, settings)
    _confirm_compiled(client, clause_ids)


def _single_judge(client, settings, facts):
    return client.post(
        "/api/policy/judge",
        json={"asOf": "2025-06-01", "facts": facts, "question": "条款是否适用"},
        headers=auth(settings),
    )


def _by_clause(body, clause_id, key="outcomes"):
    return [item for item in body[key] if item["clauseId"] == clause_id]


def test_formal_nemo_preserves_statement_id_and_complete_proof(client, settings):
    _prepare_rules(client, settings)
    response = _single_judge(client, settings, {"targetObject": "早籼稻"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"outcomes", "asOf"}
    assert len(body["outcomes"]) == 3
    assert len({item["statementId"] for item in body["outcomes"]}) == 3

    applicable = _by_clause(body, "POL0001-C001")[0]
    assert applicable["outcome"] == "APPLICABLE"
    assert applicable["conditionMet"] is True
    assert applicable["engine"] == "nemo"
    assert applicable["formalReasoning"] is True
    assert applicable["officiallyActivated"] == "UNKNOWN"
    assert applicable["activationEvidenceId"] is None
    assert applicable["ruleId"].startswith("RULE-")
    assert [step["step"] for step in applicable["chain"]] == [1, 2, 3, 4, 5, 6]
    assert applicable["proofChain"] == applicable["chain"]
    assert all(step["quote"] in "2025年生产的早籼稻最低收购价为每50公斤128元。" for step in applicable["chain"])
    assert all(step["citation"] == "P1" for step in applicable["chain"])
    assert applicable["evidenceIds"] == ["SOU0001"]
    assert "1 份" in applicable["scopeNote"]
    assert "37 份" not in applicable["scopeNote"]
    assert applicable["disclaimer"] == "辅助研究，不构成法律意见或官方解释"


def test_singular_api_returns_unable_for_draft_instead_of_fallback(client, settings):
    _extract(client, settings)
    _compile(client, settings)
    response = _single_judge(client, settings, {"targetObject": "早籼稻"})
    assert response.status_code == 200
    outcomes = response.json()["outcomes"]
    assert len(outcomes) == 3
    assert all(item["outcome"] == "UNABLE" for item in outcomes)
    assert all(item["engine"] == "nemo" for item in outcomes)
    assert all(item["formalReasoning"] is False for item in outcomes)
    assert all("确认" in item["reason"] or "测试" in item["reason"] for item in outcomes)


def test_plural_api_marks_mixed_engines_per_statement(client, settings):
    _prepare_rules(client, settings, {"POL0001-C001"})
    response = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    body = response.json()
    assert body["engine"] == "mixed"
    assert _by_clause(body, "POL0001-C001", "verdicts")[0]["engine"] == "nemo"
    for clause_id in ("POL0001-C002", "POL0001-C003"):
        item = _by_clause(body, clause_id, "verdicts")[0]
        assert item["engine"] == "fallback-v1"
        assert item["formalReasoning"] is False


def test_exception_unknown_is_unable_even_when_main_condition_is_true(client, settings):
    _extract(client, settings)
    db = client.app.state.db
    statement = db.fetch_one('SELECT * FROM "NormativeStatement" WHERE "clauseId"="POL0001-C001"')
    with db.engine.begin() as conn:
        db.insert("ClauseCondition", {
            "conditionId": "CND-EX-UNKNOWN",
            "statementId": statement["statementId"],
            "parentConditionId": None,
            "operator": "LEAF",
            "leftOperand": "facts.localReserveActive",
            "comparator": "=",
            "rightValue": "True",
            "factSource": "USER_INPUT",
        }, conn, actor="technical-smoke")
        db.insert("ClauseException", {
            "exceptionId": "EXC-UNKNOWN",
            "statementId": statement["statementId"],
            "conditionId": "CND-EX-UNKNOWN",
            "exceptionType": "技术例外",
            "note": "地方收储启动时除外",
        }, conn, actor="technical-smoke")
    _compile(client, settings)
    _confirm_compiled(client)

    outcome = _by_clause(_single_judge(client, settings, {"targetObject": "早籼稻"}).json(), "POL0001-C001")[0]
    assert outcome["outcome"] == "UNABLE"
    assert outcome["conditionMet"] is True
    assert "例外" in outcome["reason"]
    assert [step["step"] for step in outcome["chain"]] == [1, 2, 3, 4]


def test_multiple_statements_in_one_clause_are_not_folded(client, settings):
    _extract(client, settings)
    db = client.app.state.db
    statement = db.fetch_one('SELECT * FROM "NormativeStatement" WHERE "clauseId"="POL0001-C001"')
    condition = db.fetch_one('SELECT * FROM "ClauseCondition" WHERE "conditionId"=:cid', {"cid": statement["conditionId"]})
    duplicate = dict(statement)
    duplicate.update({"statementId": "STM-MULTI-C001", "conditionId": "CND-MULTI-C001", "confirmationStatus": "DRAFT"})
    condition = dict(condition)
    condition.update({"conditionId": "CND-MULTI-C001", "statementId": "STM-MULTI-C001"})
    with db.engine.begin() as conn:
        db.insert("NormativeStatement", duplicate, conn, actor="technical-smoke")
        db.insert("ClauseCondition", condition, conn, actor="technical-smoke")
    _compile(client, settings)
    _confirm_compiled(client)

    response = _single_judge(client, settings, {"targetObject": "早籼稻"})
    items = _by_clause(response.json(), "POL0001-C001")
    assert len(items) == 2
    assert {item["statementId"] for item in items} == {statement["statementId"], "STM-MULTI-C001"}
    assert len({item["ruleId"] for item in items}) == 2


def test_priority_override_and_cycle_are_explicit(client, settings):
    _prepare_rules(client, settings)
    db = client.app.state.db
    condition = db.fetch_one(
        'SELECT cc.* FROM "ClauseCondition" cc JOIN "NormativeStatement" ns '
        'ON cc."conditionId"=ns."conditionId" WHERE ns."clauseId"="POL0001-C002"'
    )
    with db.engine.begin() as conn:
        db.update("ClauseCondition", condition["conditionId"], {"rightValue": "早籼稻"}, conn)
        db.insert("ClauseRelation", {
            "relationId": "REL-C002-C001",
            "fromClauseId": "POL0001-C002",
            "toClauseId": "POL0001-C001",
            "relationType": "PREVAILS_OVER",
            "priorityBasis": "NEWER",
            "resolved": 0,
        }, conn)
    outcomes = _single_judge(client, settings, {"targetObject": "早籼稻"}).json()["outcomes"]
    lower = _by_clause({"outcomes": outcomes}, "POL0001-C001")[0]
    higher = _by_clause({"outcomes": outcomes}, "POL0001-C002")[0]
    assert lower["outcome"] == "NOT_APPLICABLE"
    assert "覆盖" in lower["reason"]
    assert [step["step"] for step in lower["chain"]] == [1, 2, 3, 4, 5]
    assert higher["outcome"] == "APPLICABLE"

    with db.engine.begin() as conn:
        db.insert("ClauseRelation", {
            "relationId": "REL-C001-C002",
            "fromClauseId": "POL0001-C001",
            "toClauseId": "POL0001-C002",
            "relationType": "PREVAILS_OVER",
            "priorityBasis": "SPECIAL",
            "resolved": 0,
        }, conn)
    outcomes = _single_judge(client, settings, {"targetObject": "早籼稻"}).json()["outcomes"]
    assert _by_clause({"outcomes": outcomes}, "POL0001-C001")[0]["outcome"] == "UNABLE"
    assert _by_clause({"outcomes": outcomes}, "POL0001-C002")[0]["outcome"] == "UNABLE"
    assert "成环" in _by_clause({"outcomes": outcomes}, "POL0001-C001")[0]["reason"]


def test_nemo_failure_never_falls_back(client, settings, monkeypatch):
    _prepare_rules(client, settings)
    def fail(*args, **kwargs):
        raise RuntimeError("technical-smoke engine failure")
    monkeypatch.setattr(client.app.state.conclusion._builder, "run", fail)
    outcome = _by_clause(_single_judge(client, settings, {"targetObject": "早籼稻"}).json(), "POL0001-C001")[0]
    assert outcome["outcome"] == "UNABLE"
    assert outcome["engine"] == "nemo"
    assert "Nemo" in outcome["reason"]
    assert "engine failure" in outcome["reason"]


def test_applicable_without_complete_chain_is_downgraded(client, settings, monkeypatch):
    _prepare_rules(client, settings)
    service = client.app.state.conclusion
    original = service._verdict
    def remove_chain(*args, **kwargs):
        result = original(*args, **kwargs)
        result["proofChain"] = []
        result["chain"] = []
        return result
    monkeypatch.setattr(service, "_verdict", remove_chain)
    outcome = _by_clause(_single_judge(client, settings, {"targetObject": "早籼稻"}).json(), "POL0001-C001")[0]
    assert outcome["outcome"] == "UNABLE"
    assert "证明链不完整" in outcome["reason"]


def test_nemo_engine_is_closed_after_request_and_with_application(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        _prepare_rules(test_client, settings)
        response = _single_judge(test_client, settings, {"targetObject": "早籼稻"})
        assert response.status_code == 200
        assert response.json()["outcomes"][0]["engine"] == "nemo"
        assert test_client.app.state.conclusion._engine._engine is None
    assert app.state.conclusion._engine._engine is None
