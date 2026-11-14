"""阶段 2b 条款结构化 · 测试
严格按冻结口径句清单（stage2b_口径句清单.md）逐条可测规则覆盖。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-pack2b.db'}",
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


# --------------------------------------------------------------------------- helpers

def _insert_clause(client, **overrides):
    """插入一条 Policy_Clause，默认是种子 POL0001-C001 的克隆，可覆盖。"""
    row = {
        "policyId": "POL0001",
        "clauseId": "POL0001-C999",
        "clauseNo": 99,
        "clauseTitle": "测试条款",
        "clauseText": "2025年生产的早籼稻最低收购价为每50公斤128元。",
        "targetObject": "早籼稻",
        "measureType": "最低收购价",
        "clauseEffectiveDate": "2025-01-01",
    }
    row.update(overrides)
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.insert("Policy_Clause", row, conn)
    return row["clauseId"]


def _extract(client, settings, policy_id=None):
    """调阶段 2b 抽取端点。"""
    return client.post(
        "/api/policies/extract-2b",
        json={"policyId": policy_id} if policy_id else {},
        headers=auth(settings),
    )


def _query(client, table, where=None, params=None):
    """查整张表或带 WHERE，返回 list[dict]。"""
    db = client.app.state.db
    sql = f'SELECT * FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    return db.fetch_all(sql, params)


# =========================================================================== R1 条款切分

def test_r1_split_no_semicolon(client, settings):
    """R1-1-1/1-2：无分号条款 → 1 条 NormativeStatement。"""
    resp = _extract(client, settings, "POL0001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["clauses"] >= 3  # 种子三条
    statements = _query(client, "NormativeStatement", '"clauseId" LIKE :p', {"p": "POL0001-C00%"})
    # POL0001-C001/C002/C003 每条都只产出 1 条
    assert len(statements) == 3
    for s in statements:
        assert s["charStart"] is not None
        assert s["charEnd"] is not None
        assert s["text"]  # 非空


def test_r1_split_with_semicolon(client, settings):
    """R1-1-1/1-2：含分号条款 → 切 2 条。"""
    _insert_clause(client,
                   clauseId="POL0001-C-SEMI",
                   clauseText="应当保持干燥；不得露天堆放。")
    resp = _extract(client, settings, "POL0001")
    assert resp.status_code == 200
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-SEMI"})
    assert len(statements) == 2
    texts = {s["text"] for s in statements}
    assert texts == {"应当保持干燥", "不得露天堆放"}


def test_r1_split_quote_protection(client, settings):
    """R1-1-3：引号/括号内的分号不切。"""
    _insert_clause(client,
                   clauseId="POL0001-C-QUOTE",
                   clauseText='应当使用"一级;二级"标准执行。')
    resp = _extract(client, settings, "POL0001")
    assert resp.status_code == 200
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-QUOTE"})
    assert len(statements) == 1  # 引号内的分号不切


# =========================================================================== R2 statementType 六类

def test_r2_definition_no_modal(client, settings):
    """R2-1-5：缺模态 + 数字 → DEFINITION。种子三条最低收购价都应判 DEFINITION。"""
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId" LIKE :p', {"p": "POL0001-C00%"})
    for s in statements:
        assert s["statementType"] == "DEFINITION"


def test_r2_obligation(client, settings):
    """R2-1-1：含'应当' → OBLIGATION。"""
    _insert_clause(client,
                   clauseId="POL0001-C-OBL",
                   clauseText="粮食经营者应当建立购销台账。",
                   targetObject="粮食")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-OBL"})
    assert len(statements) == 1
    assert statements[0]["statementType"] == "OBLIGATION"


def test_r2_prohibition(client, settings):
    """R2-1-2：含'不得' → PROHIBITION。"""
    _insert_clause(client,
                   clauseId="POL0001-C-PRO",
                   clauseText="严禁在粮食储备库内吸烟。",
                   targetObject="粮食储备库")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-PRO"})
    assert statements[0]["statementType"] == "PROHIBITION"


def test_r2_permission(client, settings):
    """R2-1-3：含'可以' → PERMISSION（且不是 AUTHORIZATION）。"""
    _insert_clause(client,
                   clauseId="POL0001-C-PERM",
                   clauseText="企业可以自主选择收购点。",
                   targetObject="企业")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-PERM"})
    assert statements[0]["statementType"] == "PERMISSION"


def test_r2_authorization(client, settings):
    """R2-1-4：含'经…批准' → AUTHORIZATION。"""
    _insert_clause(client,
                   clauseId="POL0001-C-AUTH",
                   clauseText="进口需要经国务院批准。",
                   targetObject="进口")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-AUTH"})
    assert statements[0]["statementType"] == "AUTHORIZATION"


def test_r2_target(client, settings):
    """R2-1-6：显式含'目标/指标' + 数字 → TARGET。"""
    _insert_clause(client,
                   clauseId="POL0001-C-TGT",
                   clauseText="应当实现2025年粮食产量7000万吨的目标。",
                   targetObject="粮食")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-TGT"})
    assert statements[0]["statementType"] == "TARGET"


def test_r2_priority_prohibition_over_obligation(client, settings):
    """R2-1-7：冲突优先级——PROHIBITION > OBLIGATION。同时含'不得'和'应当' → PROHIBITION。"""
    _insert_clause(client,
                   clauseId="POL0001-C-CONFL",
                   clauseText="应当入库但不得露天堆放。",
                   targetObject="粮食")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-CONFL"})
    # 这条会被拆成两句（按句号），后一句 "应当入库但不得露天堆放" 是一句完整的
    # 实际上按分号/句号拆，没分号句号所以只 1 条。里面同时含应当+不得。
    assert len(statements) == 1
    assert statements[0]["statementType"] == "PROHIBITION"


# =========================================================================== R3 vaguenessFlag 10 词冻结

def test_r3_vagueness_hit(client, settings):
    """R3-1-1：含冻结词'必要时' → vaguenessFlag=1。"""
    _insert_clause(client,
                   clauseId="POL0001-C-VAGUE",
                   clauseText="必要时由有关部门组织收购。",
                   targetObject="粮食")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-VAGUE"})
    assert len(statements) == 1
    assert statements[0]["vaguenessFlag"] == 1
    # vaguenessNote 必须列命中词
    note = statements[0]["vaguenessNote"]
    assert "必要时" in note
    assert "有关部门" in note


def test_r3_vagueness_clean(client, settings):
    """R3-1-2：不含冻结词 → vaguenessFlag=0。"""
    _insert_clause(client,
                   clauseId="POL0001-C-CLEAN",
                   clauseText="2025年生产的早籼稻最低收购价为每50公斤128元。",
                   targetObject="早籼稻")
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-CLEAN"})
    assert statements[0]["vaguenessFlag"] == 0
    assert statements[0]["vaguenessNote"] in ("", None)


# =========================================================================== R4 confirmationStatus 一律 DRAFT

def test_r4_confirmation_status_draft(client, settings):
    """R4-1-1：所有新抽 NormativeStatement 一律 DRAFT，不自动 CONFIRMED。"""
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement")
    assert len(statements) > 0
    for s in statements:
        assert s["confirmationStatus"] == "DRAFT"


# =========================================================================== R5 ClauseCondition 扁平 LEAF

def test_r5_condition_leaf(client, settings):
    """R5-1-1/5-1-4：Condition.operator=LEAF, factSource=USER_INPUT。"""
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C001"})
    assert len(statements) == 1
    sid = statements[0]["statementId"]
    conditions = _query(client, "ClauseCondition", '"statementId"=:sid', {"sid": sid})
    assert len(conditions) >= 1
    main_cond = conditions[0]
    assert main_cond["operator"] == "LEAF"
    assert main_cond["factSource"] == "USER_INPUT"
    assert main_cond["leftOperand"] == "facts.targetObject"
    assert main_cond["rightValue"] == "早籼稻"


# =========================================================================== R6 RuleParameter 数值抽取

def test_r6_parameter_extraction(client, settings):
    """R6-1-4：最低收购价 pattern 抽 paramValue=128/129/131。"""
    _extract(client, settings, "POL0001")
    for cid, expected_value in [("POL0001-C001", 128.0), ("POL0001-C002", 129.0), ("POL0001-C003", 131.0)]:
        statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": cid})
        assert len(statements) == 1, f"{cid} 应产出 1 条 statement"
        sid = statements[0]["statementId"]
        params = _query(client, "RuleParameter", '"statementId"=:sid', {"sid": sid})
        assert len(params) >= 1, f"{cid} 应至少抽 1 个 parameter"
        price_param = [p for p in params if p["paramName"] == "最低收购价"]
        assert len(price_param) == 1
        assert price_param[0]["paramValue"] == expected_value


# =========================================================================== R7 Scope/Form 字段一律 null

def test_r7_scope_fields_null(client, settings):
    """R7-1-2/3/4：actorScopeId / objectFormIds / regionalScopeId 一律 null。"""
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement")
    assert len(statements) > 0
    for s in statements:
        assert s["actorScopeId"] is None, f"actorScopeId 应为 null: {s['statementId']}"
        # objectFormIds 是 JSON 数组字符串；空列表也可以
        assert s["objectFormIds"] is None or json.loads(s["objectFormIds"]) == []
        # regionalScopeId 不在 NormativeStatement 表里——是 Scope 表里的字段，略


# =========================================================================== R8 本阶段不编译规则/不抽 Relation

def test_r8_no_clause_relation(client, settings):
    """R6-1-5/R8-0-1：ClauseRelation 本阶段不抽，表应为空。"""
    _extract(client, settings, "POL0001")
    relations = _query(client, "ClauseRelation")
    assert len(relations) == 0


def test_r8_purge_and_reextract_idempotent(client, settings):
    """purge 后重抽应幂等——结果条数一致，无重复。"""
    _extract(client, settings, "POL0001")
    first = _query(client, "NormativeStatement", '"clauseId" LIKE :p', {"p": "POL0001-C00%"})
    _extract(client, settings, "POL0001")
    second = _query(client, "NormativeStatement", '"clauseId" LIKE :p', {"p": "POL0001-C00%"})
    assert len(first) == len(second) == 3  # 三条种子条款
    ids_first = {s["statementId"] for s in first}
    ids_second = {s["statementId"] for s in second}
    assert ids_first == ids_second


def test_r2_no_modal_quantity_statement_is_definition(client, settings):
    _insert_clause(
        client, clauseId="POL0001-C-DEF-PRIORITY",
        clauseText="2025年粮食产量为7000万吨。", targetObject="粮食",
    )
    _extract(client, settings, "POL0001")
    statement = _query(
        client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C-DEF-PRIORITY"}
    )[0]
    assert statement["statementType"] == "DEFINITION"


def test_statement_offsets_and_stable_id_match_source_text(client, settings):
    clause_id = _insert_clause(
        client, clauseId="POL0001-C-OFFSET",
        clauseText="  早籼稻最低收购价为每50公斤128元。\n  中晚籼稻最低收购价为每50公斤129元；  ",
        targetObject="早籼稻",
    )
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": clause_id})
    assert len(statements) == 2
    clause = _query(client, "Policy_Clause", '"clauseId"=:cid', {"cid": clause_id})[0]
    for statement in statements:
        assert statement["text"] == clause["clauseText"][statement["charStart"]:statement["charEnd"]]
        identity = json.dumps({
            "clauseId": clause_id,
            "charStart": statement["charStart"],
            "charEnd": statement["charEnd"],
            "text": statement["text"],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert statement["statementId"] == "STM-" + hashlib.sha256(identity).hexdigest()[:32]


def test_chinese_quotes_and_punctuation_split_correctly(client, settings):
    clause_id = _insert_clause(
        client, clauseId="POL0001-C-CN-QUOTE",
        clauseText="应当使用“一级；二级”标准；不得露天堆放。", targetObject="粮食",
    )
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": clause_id})
    assert [s["text"] for s in statements] == ["应当使用“一级；二级”标准", "不得露天堆放"]


def test_main_and_exception_conditions_have_separate_roots(client, settings):
    clause_id = _insert_clause(
        client, clauseId="POL0001-C-EXCEPTION",
        clauseText="早籼稻最低收购价为每50公斤128元，但经批准除外。", targetObject="早籼稻",
    )
    _extract(client, settings, "POL0001")
    statement = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": clause_id})[0]
    exception = _query(client, "ClauseException", '"statementId"=:sid', {"sid": statement["statementId"]})[0]
    assert statement["conditionId"] != exception["conditionId"]
    main = _query(client, "ClauseCondition", '"conditionId"=:cid', {"cid": statement["conditionId"]})[0]
    exceptional = _query(client, "ClauseCondition", '"conditionId"=:cid', {"cid": exception["conditionId"]})[0]
    assert main["leftOperand"] == "facts.targetObject"
    assert exceptional["leftOperand"] == "facts.exception"


def test_consequence_requires_explicit_action_and_quantity(client, settings):
    clause_id = _insert_clause(
        client, clauseId="POL0001-C-CONSEQUENCE",
        clauseText="应当按照最低收购价为每50公斤128元的标准收购早籼稻；不得执行最低收购价政策；应当执行最低收购价政策。",
        targetObject="早籼稻",
    )
    _extract(client, settings, "POL0001")
    statements = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": clause_id})
    consequences = {
        s["text"]: _query(client, "ClauseConsequence", '"statementId"=:sid', {"sid": s["statementId"]})
        for s in statements
    }
    positive = consequences["应当按照最低收购价为每50公斤128元的标准收购早籼稻"]
    assert len(positive) == 1
    assert positive[0]["magnitude"] == 128.0
    assert consequences["不得执行最低收购价政策"] == []
    assert consequences["应当执行最低收购价政策"] == []


def test_reextract_rejects_confirmed_statement_without_deleting_assets(client, settings):
    _extract(client, settings, "POL0001")
    statement = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C001"})[0]
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.update(
            "NormativeStatement", statement["statementId"],
            {"confirmationStatus": "CONFIRMED", "confirmedBy": "tester", "confirmedAt": "2026-01-01T00:00:00Z"},
            conn,
        )
    response = _extract(client, settings, "POL0001")
    assert response.status_code == 409
    assert statement["statementId"] in response.text
    assert _query(client, "NormativeStatement", '"statementId"=:sid', {"sid": statement["statementId"]})


def test_reextract_rejects_rule_testcase(client, settings):
    _extract(client, settings, "POL0001")
    statement = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C001"})[0]
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.insert("RuleTestCase", {
            "testcaseId": "RTC-PROTECTED", "statementId": statement["statementId"],
            "scenarioName": "保护资产", "facts": "{}",
            "expectedOutcome": "UNABLE", "expectedReason": "缺事实", "authoredBy": "tester",
        }, conn)
    response = _extract(client, settings, "POL0001")
    assert response.status_code == 409
    assert "RTC-PROTECTED" in response.text
    assert _query(client, "RuleTestCase", '"testcaseId"=:tid', {"tid": "RTC-PROTECTED"})


def test_reextract_rejects_non_draft_compiled_rule(client, settings):
    _extract(client, settings, "POL0001")
    statement = _query(client, "NormativeStatement", '"clauseId"=:cid', {"cid": "POL0001-C001"})[0]
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.insert("CompiledRule", {
            "ruleId": "RULE-PROTECTED", "statementId": statement["statementId"],
            "ruleExpression": "% protected", "compilerVersion": "test", "ontologyVersion": "test",
            "confirmationStatus": "CONFIRMED", "testcaseIds": "[]",
        }, conn)
    response = _extract(client, settings, "POL0001")
    assert response.status_code == 409
    assert "RULE-PROTECTED" in response.text
    assert _query(client, "CompiledRule", '"ruleId"=:rid', {"rid": "RULE-PROTECTED"})


# =========================================================================== 边界

def test_extract_no_clauses(client, settings):
    """无 Policy_Clause 的 policyId → 返回空统计。"""
    resp = _extract(client, settings, "POL9999")
    assert resp.status_code == 200
    body = resp.json()
    assert body["clauses"] == 0
    assert body["statements"] == 0


def test_extract_manual_trigger_requires_auth(client):
    """端点需认证。"""
    resp = client.post("/api/policies/extract-2b", json={})
    assert resp.status_code == 401
