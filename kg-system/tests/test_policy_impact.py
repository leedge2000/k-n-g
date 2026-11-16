import hashlib

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-policy-impact.db'}",
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
    return {"Authorization": f"Bearer {issue_token('impact-user', role, settings.jwt_secret)}"}


def insert(client, table, row):
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.insert(table, row, conn)


def insert_policy(client, policy_id, **overrides):
    row = {
        "policyId": policy_id,
        "policyTitle": f"测试政策{policy_id}",
        "docNumber": f"测试〔2026〕{policy_id[-1]}号",
        "policyLevel": "部门规范性文件",
        "issuer": "测试部门",
        "regionId": "REG0001",
        "sourceDocId": "SOU0001",
        "segmentId": "SOU0001-S001",
        "knowledgeCategoryId": "KTC0166",
        "industryStage": "收储",
        "factorDimensions": "价格",
        "policyStatus": "现行有效",
        "reviewStatus": "已发布",
    }
    row.update(overrides)
    insert(client, "Policy", row)


def insert_clause(client, clause_id, policy_id, target_object, measure_type="价格调控",
                  clause_text="测试条款原文。"):
    insert(client, "Policy_Clause", {
        "policyId": policy_id,
        "clauseId": clause_id,
        "clauseNo": int(clause_id.rsplit("-C", 1)[-1]),
        "clauseTitle": "测试条款",
        "clauseText": clause_text,
        "measureType": measure_type,
        "targetObject": target_object,
    })


def insert_link(client, link_id, policy_id, commodity_id, direction="POSITIVE",
                lag_days=0, status="有效", mechanism="测试影响机制。"):
    insert(client, "ImpactLink", {
        "linkId": link_id,
        "sourceType": "POLICY",
        "sourceId": policy_id,
        "targetType": "COMMODITY",
        "targetId": commodity_id,
        "impactDirection": direction,
        "impactMagnitude": None,
        "lagDays": lag_days,
        "mechanism": mechanism,
        "assertedByType": "TEST",
        "assertedById": "TEST",
        "version": 1,
        "status": status,
    })


def insert_evidence(client, evidence_id, link_id, quote, doc_id="SOU0001", segment_id=None):
    insert(client, "Evidence", {
        "evidenceId": evidence_id,
        "targetType": "ImpactLink",
        "targetId": link_id,
        "documentId": doc_id,
        "segmentId": segment_id,
        "quote": quote,
        "quoteHash": hashlib.sha1(quote.encode("utf-8")).hexdigest(),
        "evidenceType": "CITED",
        "strength": 0.0,
        "verifiedBy": "AI-Agent:DeepSeek-V4.1-Flash",
    })


def query_impacts(client, settings, **params):
    return client.get("/api/policies/impacts", params=params, headers=auth(settings))


def test_policy_link_appears_only_after_policy_takes_effect(client, settings):
    insert_policy(client, "POL9001", effectiveDate="2026-01-01")
    insert_link(client, "LNK9001", "POL9001", "CMD0005")
    insert_evidence(client, "ED9001", "LNK9001", "粮食产量稳定在1.4万亿斤左右。")

    before = query_impacts(client, settings, commodityId="CMD0005", asOf="2025-06-01")
    assert before.status_code == 200
    assert before.headers["cache-control"] == "no-store"
    assert before.json()["total"] == 0
    assert before.json()["summaryDirection"] == "依据不足"

    after = query_impacts(client, settings, commodityId="CMD0005", asOf="2026-06-01")
    body = after.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["policyId"] == "POL9001"
    assert item["direction"] == "正向"
    assert item["strength"] == "依据不足"
    assert item["quote"] == "粮食产量稳定在1.4万亿斤左右。"
    assert item["sourceLink"] == "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20487"
    assert item["evidenceCount"] == 1
    assert item["basis"] == "evidence"


def test_region_filter_follows_region_hierarchy(client, settings):
    insert_policy(client, "POL9002", regionId="REG0004", effectiveDate="2025-01-01")
    insert_link(client, "LNK9002", "POL9002", "CMD0003")
    insert_evidence(client, "ED9002", "LNK9002", "测试条款原文摘录。")

    # 河南政策不覆盖重庆
    chongqing = query_impacts(client, settings, commodityId="CMD0003", regionId="REG0002",
                              asOf="2026-06-01").json()
    assert chongqing["total"] == 0
    assert chongqing["regionName"] == "重庆市"

    henan = query_impacts(client, settings, commodityId="CMD0003", regionId="REG0004",
                          asOf="2026-06-01").json()
    assert [item["policyId"] for item in henan["items"]] == ["POL9002"]

    national = query_impacts(client, settings, commodityId="CMD0003", asOf="2026-06-01").json()
    assert [item["policyId"] for item in national["items"]] == ["POL9002"]


def test_clause_mention_without_link_stays_insufficient(client, settings):
    insert_policy(client, "POL9007", effectiveDate="2025-01-01")
    insert_clause(
        client, "POL9007-C001", "POL9007", "玉米", "价格调控",
        "合理确定玉米收购价格水平，稳定种粮收益预期。",
    )

    body = query_impacts(client, settings, commodityId="CMD0003", asOf="2026-06-01").json()
    items = [item for item in body["items"] if item["policyId"] == "POL9007"]
    assert len(items) == 1
    item = items[0]
    assert item["linkId"] is None
    assert item["direction"] == "依据不足"
    assert item["basis"] == "clause-only"
    assert item["evidenceCount"] == 0
    assert item["toolType"] == "价格调控"
    assert item["impactVariable"] == "价格"
    assert "玉米收购价格" in item["quote"]
    assert item["sourceLink"]


def test_link_without_evidence_is_downgraded_to_insufficient(client, settings):
    insert_policy(client, "POL9003", effectiveDate="2025-01-01")
    insert_link(client, "LNK9003", "POL9003", "CMD0003")

    body = query_impacts(client, settings, commodityId="CMD0003", asOf="2026-06-01").json()
    item = body["items"][0]
    assert item["direction"] == "依据不足"
    assert item["directionRaw"] == "POSITIVE"
    assert item["basis"] == "no-evidence"
    assert item["quote"] is None
    assert item["evidenceCount"] == 0


def test_inactive_link_status_is_ignored(client, settings):
    insert_policy(client, "POL9004", effectiveDate="2025-01-01")
    insert_link(client, "LNK9004", "POL9004", "CMD0003", status="失效")
    insert_evidence(client, "ED9004", "LNK9004", "测试条款原文摘录。")

    body = query_impacts(client, settings, commodityId="CMD0003", asOf="2026-06-01").json()
    assert body["total"] == 0


def test_policy_targets_lists_commodities_and_empty_enterprises(client, settings):
    insert_policy(client, "POL9005", effectiveDate="2025-01-01")
    insert_link(client, "LNK9005", "POL9005", "CMD0004", lag_days=None)
    insert_evidence(client, "ED9005", "LNK9005", "巩固提升大豆产能，做好产销衔接。")

    response = client.get("/api/policies/POL9005/targets", params={"asOf": "2026-06-01"},
                          headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["effective"] is True
    assert body["effectiveStatus"] == "CURRENT"
    assert [item["commodityName"] for item in body["targets"]["commodities"]] == ["大豆"]
    assert body["targets"]["commodities"][0]["direction"] == "正向"
    assert body["targets"]["regions"] == [{"regionId": "REG0001", "regionName": "中国"}]
    assert body["targets"]["industryStages"] == ["收储"]
    assert body["enterprises"] == []
    assert body["targets"]["enterpriseTypes"] == []
    assert any("企业主数据为空" in note for note in body["notes"])


def test_policy_targets_drops_out_when_policy_not_effective(client, settings):
    insert_policy(client, "POL9006", effectiveDate="2026-01-01")
    insert_link(client, "LNK9006", "POL9006", "CMD0003")
    insert_evidence(client, "ED9006", "LNK9006", "测试条款原文摘录。")

    body = client.get("/api/policies/POL9006/targets", params={"asOf": "2025-06-01"},
                      headers=auth(settings)).json()
    assert body["effective"] is False
    assert body["effectiveStatus"] == "NOT_YET_EFFECTIVE"
    assert body["items"] == []
    assert body["targets"]["commodities"] == []
    assert any("影响链同步失效" in note for note in body["notes"])


def test_policy_targets_reports_link_gap(client, settings):
    insert_policy(client, "POL9008", effectiveDate="2025-01-01")

    body = client.get("/api/policies/POL9008/targets", params={"asOf": "2026-06-01"},
                      headers=auth(settings)).json()
    assert body["effective"] is True
    assert body["total"] == 0
    assert any("尚未建立 ImpactLink" in note for note in body["notes"])


def test_impact_queries_reject_bad_input(client, settings):
    assert query_impacts(client, settings, asOf="2026-11-16").status_code == 400
    assert query_impacts(client, settings, commodityId="CMD9999", asOf="2026-11-16").status_code == 404
    assert query_impacts(client, settings, commodityId="CMD0005", regionId="REG9999",
                         asOf="2026-11-16").status_code == 404
    assert query_impacts(client, settings, commodityId="CMD0005", asOf="2026/11/16").status_code == 422
    assert client.get("/api/policies/POL9999/targets", headers=auth(settings)).status_code == 404
    assert client.get("/api/policies/POL0001/targets", params={"asOf": "bad"},
                      headers=auth(settings)).status_code == 422


def test_impact_endpoints_require_token(client):
    assert client.get("/api/policies/impacts", params={"commodityId": "CMD0005"}).status_code == 401
    assert client.get("/api/policies/POL0001/targets").status_code == 401
