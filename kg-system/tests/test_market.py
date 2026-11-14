# -*- coding: utf-8 -*-
"""阶段 3 市场包：G3 校验门、种子幂等与只读工作台接口的回归测试。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market import MarketError, MarketService, similarity
from app.scope import ScopeService

BASE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def market_service(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'market.db'}", BASE_DIR / "seed")
    db.initialize()
    scope = ScopeService(db)
    service = MarketService(db, scope)
    service.initialize()
    service.seed_market()
    yield service
    db.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-test.db'}",
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
    return {"Authorization": f"Bearer {issue_token('test-user', role, settings.jwt_secret)}"}


def _count(db, table):
    return db.fetch_one(f'SELECT COUNT(*) AS total FROM "{table}"')["total"]


def test_seed_lands_expected_rows_and_is_idempotent(market_service):
    db = market_service.db
    assert _count(db, "IndicatorSeries") == 8
    assert _count(db, "PriceDataPoint") == 96
    assert _count(db, "Event") == 6
    assert _count(db, "Factor") == 8
    assert _count(db, "Assertion") == 6
    assert _count(db, "PriceTransmissionLink") == 4

    market_service.seed_market()
    assert _count(db, "IndicatorSeries") == 8
    assert _count(db, "PriceDataPoint") == 96
    assert _count(db, "Assertion") == 6


def test_assertion_dedupe_merges_similar_same_org_proposition(market_service):
    rows = market_service.db.fetch_all('SELECT * FROM "Assertion" ORDER BY assertionId')
    ids = [row["assertionId"] for row in rows]
    assert ids == ["AST-0001", "AST-0002", "AST-0003", "AST-0005", "AST-0006", "AST-0007"]

    first = next(row for row in rows if row["assertionId"] == "AST-0003")
    duplicate = {
        "assertionType": "FORECAST",
        "subjectType": "IndicatorSeries",
        "subjectId": "OBJ-PUB-IN-0004",
        "predicate": "WILL_RISE",
        "objectValue": 30.0,
        "objectUnitId": first["objectUnitId"],
        "targetPeriodId": "2024-12",
        "scopeId": first["scopeId"],
        "assertedByType": "ORGANIZATION",
        "assertedById": first["assertedById"],
        "assertedAt": "2024-10-20",
        "horizon": "SHORT",
        "rationale": "示例观点：猪肉零售价格将温和上行，幅度有限。",
        "stanceStrength": "WEAK",
    }
    result = market_service.upsert_assertion(duplicate)
    assert result["merged"] is True
    assert result["assertionId"] == "AST-0003"
    assert _count(market_service.db, "Assertion") == 6


def test_cross_institution_opposite_views_are_kept_parallel(market_service):
    rows = market_service.db.fetch_all(
        'SELECT * FROM "Assertion" WHERE subjectId=:id ORDER BY predicate',
        {"id": "OBJ-PUB-IN-0001"},
    )
    assert [row["predicate"] for row in rows] == ["WILL_FALL", "WILL_RISE"]
    assert market_service.assertions("OBJ-PUB-IN-0001")["total"] == 2


def test_similarity_reaches_threshold_for_seed_duplicate():
    assert similarity("示例观点：猪肉零售价格将温和上行，幅度有限。", "示例观点 猪肉零售价格将温和上行 幅度有限") >= 0.85


def _series_payload(market_service):
    row = market_service.db.fetch_one(
        'SELECT * FROM "IndicatorSeries" WHERE seriesId=:id', {"id": "OBJ-PUB-IN-0001"}
    )
    return {
        "indicatorId": row["indicatorId"],
        "scopeId": row["scopeId"],
        "frequency": "MONTH",
        "statisticalSourceId": row["statisticalSourceId"],
        "priceBasis": row["priceBasis"],
        "taxBasis": row["taxBasis"],
        "currencyCode": row["currencyCode"],
        "unitId": row["unitId"],
        "startPeriodId": "2024-01",
        "endPeriodId": "2024-12",
    }


def test_series_duplicate_on_indicator_and_scope_is_rejected(market_service):
    payload = _series_payload(market_service)
    with pytest.raises(MarketError) as error:
        market_service.upsert_series(payload)
    assert error.value.status == 409


def test_series_tax_basis_must_mirror_scope(market_service):
    payload = _series_payload(market_service)
    payload["taxBasis"] = "EXCLUSIVE"
    with pytest.raises(MarketError) as error:
        market_service.upsert_series(payload)
    assert "口径不一致" in str(error.value)


def test_point_unit_must_match_series(market_service):
    with pytest.raises(MarketError) as error:
        market_service.upsert_point({
            "seriesId": "OBJ-PUB-IN-0001",
            "periodId": "2024-01",
            "value": 2600,
            "unitId": "OBJ-PUB-UN-0002",
            "publishedAt": "2024-02-05T00:00:00Z",
        })
    assert error.value.status == 400


def test_duplicate_point_period_is_rejected(market_service):
    with pytest.raises(MarketError) as error:
        market_service.upsert_point({
            "seriesId": "OBJ-PUB-IN-0001",
            "periodId": "2024-01",
            "value": 2600,
            "publishedAt": "2024-02-05T00:00:00Z",
        })
    assert error.value.status == 409


def test_transmission_rejects_self_loop(market_service):
    with pytest.raises(MarketError) as error:
        market_service.upsert_link({
            "fromSeriesId": "OBJ-PUB-IN-0001",
            "toSeriesId": "OBJ-PUB-IN-0001",
            "transmissionType": "COST_PUSH",
            "samplePeriodId": "2024-12",
            "regimeNote": "自环不应被接受",
        })
    assert error.value.status == 409


def test_policy_event_requires_scope(market_service):
    with pytest.raises(MarketError) as error:
        market_service.upsert_event({
            "eventTypeId": "OBJ-PUB-EV-0001",
            "eventTitle": "政策事件缺范围",
            "eventTime": "2024-09-01",
            "eventScale": "NATIONAL",
        })
    assert "scopeId" in str(error.value)


def test_market_routes_require_token(client):
    assert client.get("/api/market/overview").status_code == 401


def test_market_overview_and_series(client, settings):
    response = client.get("/api/market/overview", headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["seriesCount"] == 8
    assert body["pointCount"] == 96
    assert body["assertionCount"] == 6
    assert body["eventCount"] == 6
    assert body["transmissionCount"] == 4
    assert body["scopeCount"] == 8
    assert len(body["scopeDimensions"]) == 5
    assert "CHANNEL" in set(body["scopeDimensions"])
    assert len(body["periods"]) == 12
    assert body["counts"]["commodityForms"] == 8
    assert body["counts"]["places"] == 3
    assert body["counts"]["impacts"] == 4

    response = client.get("/api/market/series", headers=auth(settings))
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 8
    rice = next(item for item in items if item["seriesId"] == "OBJ-PUB-IN-0001")
    assert rice["indicatorName"] == "稻谷河南收购价"
    assert rice["regionLabel"] == "河南省"
    assert rice["latestPeriod"] == "2024-12"
    assert rice["latestValue"] == 2580


def test_market_series_detail_carries_scope_points_events_and_assertions(client, settings):
    response = client.get("/api/market/series/OBJ-PUB-IN-0001", headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["series"]["priceBasis"] == "EX_FARM"
    members = body["scope"]["members"]
    assert {member["dimensionCode"] for member in members} == {
        "CHANNEL", "COMMODITY", "GEOGRAPHY", "TAX_BASIS", "UNIT_BASIS"
    }
    channels = [member for member in members if member["dimensionCode"] == "CHANNEL"]
    assert len(channels) == 1
    commodity = next(member for member in members if member["dimensionCode"] == "COMMODITY")
    assert commodity["objectId"] == "OBJ-PUB-CM-0001"
    assert len(body["points"]) == 12
    assert len(body["events"]) == 1
    assert len(body["assertions"]) == 2
    assert all(item["evidenceIds"] for group in body["assertions"] for item in group["items"])
    assert body["commodityForm"]["formId"] == "OBJ-PUB-CM-0001"
    assert body["marketPlace"]["placeId"] == "OBJ-PUB-MK-0001"

    missing = client.get("/api/market/series/OBJ-PUB-IN-9999", headers=auth(settings))
    assert missing.status_code == 404


def test_market_timeline_filters_by_period_and_category(client, settings):
    response = client.get(
        "/api/market/timeline",
        params={"from": "2024-07", "to": "2024-12", "category": "SUPPLY"},
        headers=auth(settings),
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["eventTitle"] for item in items] == ["主产区干旱预警"]
    assert items[0]["eventCategory"] == "SUPPLY"


def test_market_assertions_group_bull_and_bear_views(client, settings):
    response = client.get(
        "/api/market/assertions",
        params={"subjectId": "OBJ-PUB-IN-0001"},
        headers=auth(settings),
    )
    assert response.status_code == 200
    groups = response.json()["items"]
    assert {group["stance"] for group in groups} == {"看多", "看空"}
    assert all(len(group["items"]) == 1 for group in groups)


def test_market_transmission_graph(client, settings):
    response = client.get("/api/market/transmissions", headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert len(body["edges"]) == 4
    assert len(body["nodes"]) == 6
    assert all(edge["regimeNote"] for edge in body["edges"])
    assert any(
        edge["from"] == "OBJ-PUB-IN-0006" and edge["to"] == "OBJ-PUB-IN-0007"
        for edge in body["edges"]
    )


def test_market_entities_are_registered_in_catalog(client, settings):
    response = client.get("/api/entities/价格数据点", params={"limit": 5}, headers=auth(settings))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 5
