import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-policy.db'}",
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


def insert_policy(client, **overrides):
    row = {
        "policyId": "POL9001",
        "policyTitle": "测试政策",
        "docNumber": "测试〔2026〕1号",
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
    db = client.app.state.db
    with db.engine.begin() as conn:
        db.insert("Policy", row, conn)


def test_effective_policies_respect_as_of_window(client, settings):
    insert_policy(client, policyId="POL9002", policyTitle="无生效日期的通知")

    before = client.get("/api/policies/effective", params={"asOf": "2024-06-01"}, headers=auth(settings))
    assert before.status_code == 200
    assert before.headers["cache-control"] == "no-store"
    body = before.json()
    assert body["asOf"] == "2024-06-01"
    assert body["counts"]["NOT_YET_EFFECTIVE"] == 1   # POL0001 2025-01-01 生效
    assert body["counts"]["UNKNOWN_DATE"] == 0
    # 缺生效日期视为长期有效，早于其他政策生效的时点上也算现行
    assert body["total"] == 1
    assert [item["policyId"] for item in body["items"]] == ["POL9002"]
    assert body["items"][0]["effectiveDate"] is None

    after = client.get("/api/policies/effective", params={"asOf": "2025-06-01"}, headers=auth(settings))
    assert after.status_code == 200
    body = after.json()
    assert body["total"] == 2
    assert [item["policyId"] for item in body["items"]] == ["POL0001", "POL9002"]
    assert body["items"][0]["effectiveDate"] == "2025-01-01"


def test_effective_policies_default_to_today_and_stay_cacheable(client, settings):
    response = client.get("/api/policies/effective", headers=auth(settings))
    assert response.status_code == 200
    assert "cache-control" not in response.headers
    body = response.json()
    assert body["asOf"] >= "2025-01-01"
    assert [item["policyId"] for item in body["items"]] == ["POL0001"]


def test_effective_policies_reject_malformed_as_of(client, settings):
    for value in ("2025-13-01", "2025/06/01", "12345"):
        response = client.get("/api/policies/effective", params={"asOf": value}, headers=auth(settings))
        assert response.status_code == 422, value


def test_superseded_version_drops_out_of_effective_list(client, settings):
    insert_policy(
        client,
        policyId="POL0003",
        policyTitle="关于公布2026年稻谷最低收购价格的通知",
        effectiveDate="2026-01-01",
        version=2,
        supersedesPolicyId="POL0001",
    )

    # 后继版本尚未生效时，旧版本仍为现行有效
    old = client.get("/api/policies/effective", params={"asOf": "2025-06-01"}, headers=auth(settings)).json()
    assert [item["policyId"] for item in old["items"]] == ["POL0001"]

    # 后继版本生效后，旧版本退出，后继版本带着版本链信息出现
    new = client.get("/api/policies/effective", params={"asOf": "2026-06-01"}, headers=auth(settings)).json()
    assert new["counts"]["SUPERSEDED"] == 1
    assert [item["policyId"] for item in new["items"]] == ["POL0003"]
    assert new["items"][0]["version"] == 2
    assert new["items"][0]["supersedesPolicyId"] == "POL0001"


def _member(dimension, object_type, object_id):
    return {"dimensionCode": dimension, "objectType": object_type, "objectId": object_id}


def _scopes(client):
    scope = client.app.state.scope
    national = scope.upsert_scope(
        "POLICY_SCOPE", [_member("COMMODITY", "Commodity", "CMD0003")], "POL-CORN-NATIONAL",
        is_open=True,
    )
    henan = scope.upsert_scope(
        "POLICY_SCOPE",
        [_member("COMMODITY", "Commodity", "CMD0003"), _member("GEOGRAPHY", "Region", "REG0001")],
        "POL-CORN-HENAN", superset_of=[national["scopeId"]],
    )
    return national, henan


def test_effective_policies_filter_by_scope(client, settings):
    national, henan = _scopes(client)
    insert_policy(client, policyId="POL9101", scopeId=national["scopeId"], effectiveDate="2025-01-01")
    insert_policy(client, policyId="POL9102", scopeId=henan["scopeId"], effectiveDate="2025-01-02")

    params = {"asOf": "2025-06-01"}
    unscoped = client.get("/api/policies/effective", params=params, headers=auth(settings)).json()
    assert unscoped["scopeId"] is None
    assert unscoped["total"] == 3

    # 全国范围：省级政策不命中；未填范围的政策（POL0001）始终保留
    by_national = client.get(
        "/api/policies/effective", params={**params, "scopeId": national["scopeId"]}, headers=auth(settings)
    ).json()
    assert by_national["scopeId"] == national["scopeId"]
    assert [item["policyId"] for item in by_national["items"]] == ["POL9101", "POL0001"]

    # 省级范围：全国范围政策与省级政策都命中
    by_henan = client.get(
        "/api/policies/effective", params={**params, "scopeId": henan["scopeId"]}, headers=auth(settings)
    ).json()
    assert [item["policyId"] for item in by_henan["items"]] == ["POL9102", "POL9101", "POL0001"]
    assert by_henan["items"][0]["scopeId"] == henan["scopeId"]


def test_effective_policies_reject_unknown_scope(client, settings):
    response = client.get(
        "/api/policies/effective", params={"scopeId": "SCP-PUB-UNKNOWN"}, headers=auth(settings)
    )
    assert response.status_code == 422
    assert "范围不存在" in response.json()["detail"]


def test_rag_query_carries_effective_policies(client, settings):
    national, henan = _scopes(client)
    insert_policy(
        client,
        policyId="POL9201",
        policyTitle="稻谷最低收购价执行预案",
        scopeId=national["scopeId"],
        effectiveDate="2025-01-01",
    )

    response = client.post(
        "/api/rag/query",
        json={
            "query": "稻谷最低收购价政策现在还有效吗",
            "asOf": "2025-06-01",
            "scopeId": henan["scopeId"],
            "answer": False,
        },
        headers=auth(settings),
    )
    assert response.status_code == 200
    context = response.json()["policyContext"]
    assert context["asOf"] == "2025-06-01"
    assert context["scopeId"] == henan["scopeId"]
    assert [item["policyId"] for item in context["items"]] == ["POL9201", "POL0001"]


def test_rag_query_rejects_unknown_scope(client, settings):
    response = client.post(
        "/api/rag/query",
        json={"query": "今年还有哪些现行有效的政策", "scopeId": "SCP-PUB-UNKNOWN"},
        headers=auth(settings),
    )
    assert response.status_code == 422
    assert "范围不存在" in response.json()["detail"]


def test_policy_text_feeds_answer_context(client):
    text = client.app.state.rag._policy_text({
        "asOf": "2025-06-01",
        "scopeId": "SCP-PUB-X",
        "total": 1,
        "counts": {},
        "items": [{
            "policyId": "POL0001",
            "policyTitle": "关于公布2025年稻谷最低收购价格的通知",
            "docNumber": "发改价格〔2025〕1号",
            "effectiveDate": "2025-01-01",
        }],
    })
    assert "现行有效政策（截至 2025-06-01，适用范围 SCP-PUB-X，共 1 条）" in text
    assert "[P1]《关于公布2025年稻谷最低收购价格的通知》（发改价格〔2025〕1号，2025-01-01 起施行）" in text


def test_effective_policies_carry_citation_and_index(client, settings):
    response = client.get("/api/policies/effective", params={"asOf": "2025-06-01"}, headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    for item in body["items"]:
        assert item.get("citation") is not None
    index = body["citationIndex"]
    assert isinstance(index, dict)
    for item in body["items"]:
        assert index[item["citation"]] == item["policyId"]


def test_unpublished_successor_does_not_supersede_current_version(client, settings):
    insert_policy(
        client, policyId="POL0003", effectiveDate="2025-01-01",
        supersedesPolicyId="POL0001", reviewStatus="待审核",
    )
    body = client.get(
        "/api/policies/effective", params={"asOf": "2025-06-01"}, headers=auth(settings)
    ).json()
    assert [item["policyId"] for item in body["items"]] == ["POL0001"]
    assert body["counts"]["SUPERSEDED"] == 0


def test_successor_with_invalid_effective_date_does_not_supersede(client, settings):
    insert_policy(
        client, policyId="POL0003", effectiveDate="not-a-date",
        supersedesPolicyId="POL0001",
    )
    body = client.get(
        "/api/policies/effective", params={"asOf": "2025-06-01"}, headers=auth(settings)
    ).json()
    assert [item["policyId"] for item in body["items"]] == ["POL0001"]
    assert body["counts"]["UNKNOWN_DATE"] == 1


def test_expired_successor_keeps_historical_supersession_without_resurrecting_old_version(client, settings):
    insert_policy(
        client, policyId="POL0003", effectiveDate="2025-06-01", expireDate="2025-12-31",
        supersedesPolicyId="POL0001",
    )
    during = client.get(
        "/api/policies/effective", params={"asOf": "2025-07-01"}, headers=auth(settings)
    ).json()
    assert [item["policyId"] for item in during["items"]] == ["POL0003"]

    after = client.get(
        "/api/policies/effective", params={"asOf": "2026-01-01"}, headers=auth(settings)
    ).json()
    assert after["items"] == []
    assert after["counts"]["SUPERSEDED"] == 1
    assert after["counts"]["EXPIRED"] == 1


def test_withdrawn_successor_keeps_historical_supersession(client, settings):
    insert_policy(
        client, policyId="POL0003", effectiveDate="2025-06-01", policyStatus="已废止",
        supersedesPolicyId="POL0001",
    )
    body = client.get(
        "/api/policies/effective", params={"asOf": "2025-07-01"}, headers=auth(settings)
    ).json()
    assert body["items"] == []
    assert body["counts"]["SUPERSEDED"] == 1
    assert body["counts"]["WITHDRAWN"] == 1


def test_multihop_chain_ignores_unpublished_intermediate_version(client, settings):
    insert_policy(
        client, policyId="POL0003", effectiveDate="2025-02-01",
        supersedesPolicyId="POL0001", reviewStatus="待审核",
    )
    insert_policy(
        client, policyId="POL0004", effectiveDate="2025-03-01",
        supersedesPolicyId="POL0003",
    )
    body = client.get(
        "/api/policies/effective", params={"asOf": "2025-06-01"}, headers=auth(settings)
    ).json()
    assert {item["policyId"] for item in body["items"]} == {"POL0001", "POL0004"}
    assert body["counts"]["SUPERSEDED"] == 0
