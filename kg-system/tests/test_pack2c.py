"""阶段 2c · 规则引擎 + 编译器 + Datalog 集成 测试"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app
import app.rule_engine as rule_engine
from app.rule_engine import (
    CozoEngine, ProgramBuilder, eval_condition, final_judge, _compare,
)


# ==================================================================== 规则引擎底层（不依赖应用层建表）

def test_nemo_engine_executes_real_rules_without_skip():
    engine = rule_engine.NemoEngine()
    results = engine.run(
        'case_exists("case0") .\n'
        'main_true_s1("case0") .\n'
        'exception_false_s1("case0") .\n'
        'fires(?c, "s1") :- case_exists(?c), main_true_s1(?c), exception_false_s1(?c) .\n'
        'output(?c, ?s) :- fires(?c, ?s) .'
    )
    assert results == [{"c0": "case0", "c1": "s1"}]
    assert list(engine.result("fires"))


def test_program_builder_emits_nemo_facts_and_empty_sentinel():
    builder = ProgramBuilder(rule_engine.NemoEngine())
    program = builder.build({"case_exists": [("case0",)], "prevails": []}, "")
    assert 'case_exists("case0") .' in program
    assert 'prevails("__NONE__") .' in program
    assert "<-" not in program


class TestCozoEngine:
    def test_cozo_available(self):
        engine = CozoEngine()
        # 最简单的 Cozo program：空事实 + 查询
        program = "facts_exists[c] <- [[0]]\napplicable[c] := facts_exists[c]\n?[c] := applicable[c]"
        results = engine.run(program)
        assert len(results) > 0

    def test_program_builder(self):
        engine = CozoEngine()
        builder = ProgramBuilder(engine)

        # 派生事实表
        derived = {
            "case_exists": [("case0",)],
            "cond_0": [("case0",)],
        }
        rules = """
applicable[c] := case_exists[c], cond_0[c]
?[c] := applicable[c]
"""
        program = builder.build(derived, rules)
        results = builder.run(derived, rules)
        assert len(results) > 0

    def test_empty_const_rel_not_undefined(self):
        """Cozo 常量规则空表需要占位值，否则否定会报未定义。"""
        engine = CozoEngine()
        builder = ProgramBuilder(engine)
        # 空表：应该被占位符填充（不是真的空）
        const = builder.const_rel("empty_pred", arity=1, rows=[])
        assert "__NONE__" in const


# ==================================================================== 叶子条件求值

class TestEvalCondition:
    def test_equal_match(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.targetObject",
                "comparator": "=", "rightValue": "早籼稻", "factSource": "USER_INPUT"}
        assert eval_condition(cond, {"targetObject": "早籼稻"}) is True

    def test_equal_mismatch(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.targetObject",
                "comparator": "=", "rightValue": "早籼稻", "factSource": "USER_INPUT"}
        assert eval_condition(cond, {"targetObject": "中晚籼稻"}) is False

    def test_missing_fact(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.targetObject",
                "comparator": "=", "rightValue": "早籼稻", "factSource": "USER_INPUT"}
        assert eval_condition(cond, {}) is None  # 缺事实 → UNABLE 候选

    def test_in_comparator(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.region",
                "comparator": "∈", "rightValue": "河北,山东,河南", "factSource": "USER_INPUT"}
        assert eval_condition(cond, {"region": "山东"}) is True
        assert eval_condition(cond, {"region": "广东"}) is False

    def test_numeric_comparator(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.price",
                "comparator": "≤", "rightValue": 1.19, "rightUnitId": "元/公斤", "factSource": "USER_INPUT"}
        assert eval_condition(cond, {"price": 1.10}) is True
        assert eval_condition(cond, {"price": 1.25}) is False

    def test_final_judge_all_true(self):
        outcome, reason = final_judge(
            condition_eval_results=[True], rule_fired=True)
        assert outcome == "APPLICABLE"

    def test_final_judge_has_none(self):
        outcome, reason = final_judge(
            condition_eval_results=[None], rule_fired=False)
        assert outcome == "UNABLE"

    def test_final_judge_all_true_but_no_fire(self):
        outcome, reason = final_judge(
            condition_eval_results=[True], rule_fired=False)
        assert outcome == "UNABLE"

    def test_final_judge_unknown_exception(self):
        outcome, reason = final_judge(
            condition_eval_results=[True], exception_eval_result=None,
            exception_present=True, rule_fired=False)
        assert outcome == "UNABLE"

    def test_missing_collection_fact_is_unknown(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.tags",
                "comparator": "contains", "rightValue": "稻谷", "factSource": "USER_INPUT"}
        assert eval_condition(cond, {}) is None

    def test_invalid_numeric_and_missing_unit_are_unknown(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.price",
                "comparator": "≤", "rightValue": 1.19, "rightUnitId": "元/公斤", "factSource": "USER_INPUT"}
        assert eval_condition(cond, {"price": "not-a-number"}) is None
        assert eval_condition(cond | {"rightUnitId": None}, {"price": 1.1}) is None

    def test_unknown_comparator_is_unknown(self):
        cond = {"operator": "LEAF", "leftOperand": "facts.price",
                "comparator": "≈", "rightValue": 1.19, "factSource": "USER_INPUT"}
        assert eval_condition(cond, {"price": 1.19}) is None

    def test_final_judge_exception(self):
        outcome, reason = final_judge(
            condition_eval_results=[True], exception_eval_result=True, rule_fired=True)
        assert outcome == "NOT_APPLICABLE"


# ==================================================================== 应用层 fixture

@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-pack2c.db'}",
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


# ==================================================================== Pack2c 编译器

def _extract_2b(client, settings, policy_id="POL0001"):
    resp = client.post("/api/policies/extract-2b", json={"policyId": policy_id},
                       headers=auth(settings))
    assert resp.status_code == 200


def _compile_2c(client, settings, policy_id="POL0001"):
    resp = client.post("/api/policies/compile-2c", json={"policyId": policy_id},
                       headers=auth(settings))
    return resp


def test_compile_generates_rules(client, settings):
    """种子 POL0001 三条 statement → 生成 3 条 CompiledRule（DRAFT）。"""
    _extract_2b(client, settings)
    resp = _compile_2c(client, settings)
    assert resp.status_code == 200
    body = resp.json()
    assert body["compiled"] == 3  # 三条种子 statement

    rules = client.app.state.db.fetch_all('SELECT * FROM "CompiledRule"')
    assert len(rules) == 3
    for r in rules:
        assert r["confirmationStatus"] == "DRAFT"
        assert r["ruleExpression"]  # 非空
        assert r["compilerVersion"] == "pack2c-nemo-v1"
        assert ":-" in r["ruleExpression"]
        assert "main_true_" in r["ruleExpression"]
        assert "exception_false_" in r["ruleExpression"]
        assert ":=" not in r["ruleExpression"]
        assert "<-" not in r["ruleExpression"]


def test_compile_no_duplicates_on_reextract(client, settings):
    """抽 2b → 编译 → 再抽 2b → 再编译，结果条数应一致（purge 生效）。"""
    _extract_2b(client, settings)
    r1 = _compile_2c(client, settings).json()
    _extract_2b(client, settings)
    r2 = _compile_2c(client, settings).json()
    assert r1["compiled"] == r2["compiled"]


def test_draft_rules_blocked_from_inference(client, settings):
    """confirmationStatus=DRAFT → 不参与推理，conclusion 仍走 v1。"""
    _extract_2b(client, settings)
    _compile_2c(client, settings)

    # 先不 confirm，跑 judge 应仍走 v1
    resp = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("engine") == "fallback-v1"


def test_rule_confirmation_alone_does_not_switch_to_formal_nemo(client, settings):
    """仅确认规则、缺少陈述确认和有效测试时，兼容接口仍明确走 fallback。"""
    db = client.app.state.db
    _extract_2b(client, settings)
    _compile_2c(client, settings)

    # 手动 confirm 所有种子 CompiledRule
    rules = db.fetch_all('SELECT "ruleId" FROM "CompiledRule"')
    with db.engine.begin() as conn:
        for r in rules:
            db.update("CompiledRule", r["ruleId"],
                      {"confirmationStatus": "CONFIRMED",
                       "backTranslationMatch": 1},
                      conn,
                      actor="tester")

    resp = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻"}},
        headers=auth(settings),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("engine") == "fallback-v1"
    assert all(item["engine"] == "fallback-v1" for item in body["verdicts"])


def test_compiled_rule_has_back_translation(client, settings):
    """backTranslation 应包含 statementType + 条件信息。"""
    _extract_2b(client, settings)
    _compile_2c(client, settings)
    rules = client.app.state.db.fetch_all('SELECT * FROM "CompiledRule"')
    for r in rules:
        bt = r["backTranslation"] or ""
        assert "DEFINITION" in bt or "OBLIGATION" in bt or "PROHIBITION" in bt or \
               "PERMISSION" in bt or "AUTHORIZATION" in bt or "TARGET" in bt


# ==================================================================== 无编译规则时 v1 不受影响

def test_v1_fallback_preserved(client, settings):
    """不跑 compile-2c，直接 judge → 全部走 v1，基线行为不变。"""
    resp = client.post(
        "/api/policies/judge",
        json={"asOf": "2025-06-01", "facts": {"targetObject": "早籼稻", "price": 126}},
        headers=auth(settings),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("engine") == "fallback-v1"
    # 原有三态判定仍应正确
    by_clause = {v["clauseId"]: v for v in body["verdicts"]}
    assert by_clause["POL0001-C001"]["outcome"] == "APPLICABLE"
    assert by_clause["POL0001-C002"]["outcome"] == "NOT_APPLICABLE"


# ==================================================================== 阶段 2 Nemo 编译契约

POLICY_PACK = Path(__file__).resolve().parents[1] / "ontology_ly" / "packs" / "policy"


def _rule_snapshot(db):
    fields = (
        "ruleId", "statementId", "ruleExpression", "compilerVersion",
        "backTranslation", "backTranslationMatch", "testcaseIds", "confirmationStatus",
    )
    return [
        {field: row[field] for field in fields}
        for row in sorted(
            db.fetch_all('SELECT * FROM "CompiledRule"'),
            key=lambda item: item["statementId"],
        )
    ]


def test_compile_uses_stable_rule_ids_and_is_idempotent(client, settings):
    _extract_2b(client, settings)
    assert _compile_2c(client, settings).status_code == 200
    db = client.app.state.db
    before = _rule_snapshot(db)
    assert len(before) == 3
    for rule in before:
        expected = "RULE-" + hashlib.sha256(rule["statementId"].encode("utf-8")).hexdigest()[:32]
        assert rule["ruleId"] == expected
        assert rule["backTranslationMatch"] is None
        assert json.loads(rule["testcaseIds"]) == []

    assert _compile_2c(client, settings).status_code == 200
    assert _rule_snapshot(db) == before
    assert db.fetch_one('SELECT COUNT(*) AS n FROM "CompiledRule"')["n"] == 3


def test_compile_rejects_policy_outside_tier1_registry(client, settings):
    db = client.app.state.db
    policy = db.fetch_one('SELECT * FROM "Policy" WHERE "policyId"="POL0001"')
    clause = db.fetch_one('SELECT * FROM "Policy_Clause" WHERE "clauseId"="POL0001-C001"')
    policy.update({"policyId": "POL9999", "docNumber": "TEST〔2025〕999号"})
    clause.update({"policyId": "POL9999", "clauseId": "POL9999-C001"})
    with db.engine.begin() as conn:
        db.insert("Policy", policy, conn, actor="tester")
        db.insert("Policy_Clause", clause, conn, actor="tester")
    _extract_2b(client, settings, "POL9999")

    resp = _compile_2c(client, settings, "POL9999")
    assert resp.status_code == 200
    assert resp.json()["compiled"] == 0
    assert resp.json()["skipped_not_tier1"] == 1
    assert db.fetch_all('SELECT * FROM "CompiledRule"') == []


def test_compile_rejects_unregistered_left_operand(client, settings):
    _extract_2b(client, settings)
    db = client.app.state.db
    statement = db.fetch_one('SELECT * FROM "NormativeStatement" ORDER BY "statementId" LIMIT 1')
    condition = db.fetch_one(
        'SELECT * FROM "ClauseCondition" WHERE "statementId"=:sid AND "parentConditionId" IS NULL',
        {"sid": statement["statementId"]},
    )
    with db.engine.begin() as conn:
        db.update("ClauseCondition", condition["conditionId"],
                  {"leftOperand": "facts.notRegistered"}, conn, actor="tester")

    resp = _compile_2c(client, settings)
    assert resp.status_code == 200
    assert resp.json()["compiled"] == 2
    assert resp.json()["rejections"] == [{
        "statementId": statement["statementId"],
        "reason": "未登记的 leftOperand：facts.notRegistered",
    }]
    assert db.fetch_one(
        'SELECT * FROM "CompiledRule" WHERE "statementId"=:sid',
        {"sid": statement["statementId"]},
    ) is None


def test_recompile_protects_non_draft_statement(client, settings):
    _extract_2b(client, settings)
    _compile_2c(client, settings)
    db = client.app.state.db
    statement = db.fetch_one('SELECT * FROM "NormativeStatement" LIMIT 1')
    with db.engine.begin() as conn:
        db.update("NormativeStatement", statement["statementId"],
                  {"confirmationStatus": "CONFIRMED"}, conn, actor="tester")
    before = _rule_snapshot(db)
    client.raise_server_exceptions = False

    resp = _compile_2c(client, settings)
    assert resp.status_code == 409
    assert statement["statementId"] in resp.json()["detail"]["protectedIds"]
    assert _rule_snapshot(db) == before


def test_recompile_protects_non_draft_rule(client, settings):
    _extract_2b(client, settings)
    _compile_2c(client, settings)
    db = client.app.state.db
    rule = db.fetch_one('SELECT * FROM "CompiledRule" LIMIT 1')
    with db.engine.begin() as conn:
        db.update("CompiledRule", rule["ruleId"],
                  {"confirmationStatus": "CONFIRMED"}, conn, actor="tester")
    before = _rule_snapshot(db)
    client.raise_server_exceptions = False

    resp = _compile_2c(client, settings)
    assert resp.status_code == 409
    assert rule["ruleId"] in resp.json()["detail"]["protectedIds"]
    assert _rule_snapshot(db) == before


def test_recompile_protects_rule_testcases(client, settings):
    _extract_2b(client, settings)
    _compile_2c(client, settings)
    db = client.app.state.db
    statement = db.fetch_one('SELECT * FROM "NormativeStatement" LIMIT 1')
    testcase = {
        "testcaseId": "RTC-PROTECTED",
        "statementId": statement["statementId"],
        "scenarioName": "专家资产保护回归",
        "facts": "{}",
        "expectedOutcome": "UNABLE",
        "expectedReason": "技术回归",
        "authoredBy": "tester",
    }
    with db.engine.begin() as conn:
        db.insert("RuleTestCase", testcase, conn, actor="tester")
    before = _rule_snapshot(db)
    client.raise_server_exceptions = False

    resp = _compile_2c(client, settings)
    assert resp.status_code == 409
    assert "RTC-PROTECTED" in resp.json()["detail"]["protectedIds"]
    assert _rule_snapshot(db) == before
    assert db.fetch_one('SELECT * FROM "RuleTestCase" WHERE "testcaseId"="RTC-PROTECTED"')


def test_registry_and_versioned_nemo_assets_match_compiled_rules(client, settings):
    registry = json.loads((POLICY_PACK / "tier1_policy_registry.json").read_text(encoding="utf-8"))
    assert registry["registryVersion"] == "stage2-tier1-v1"
    assert [p["policyId"] for p in registry["policies"]] == ["POL0001"]
    entry = registry["policies"][0]
    assert entry["docNumber"] == "发改价格〔2025〕308号"
    assert len(entry["qualificationEvidence"]) == 3
    assert entry["quantitativeClauseIds"] == ["POL0001-C001", "POL0001-C002", "POL0001-C003"]

    _extract_2b(client, settings)
    _compile_2c(client, settings)
    rules = sorted(
        client.app.state.db.fetch_all('SELECT * FROM "CompiledRule"'),
        key=lambda item: item["statementId"],
    )
    common = (POLICY_PACK / "rules" / "stage2_nemo.rls").read_text(encoding="utf-8")
    sample = (POLICY_PACK / "rules" / "rice_minprice_2025.rls").read_text(encoding="utf-8")
    expected = "\n".join([common.rstrip(), *(r["ruleExpression"].strip() for r in rules)]).rstrip() + "\n"
    assert sample == expected
    assert rule_engine.NemoEngine().run('case_exists("case0") .\n' + sample) == []
