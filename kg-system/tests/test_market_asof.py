# -*- coding: utf-8 -*-
"""阶段 3 回溯：asOf 只回放当时可知的信息，不得泄露未来数据。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market import MarketService
from app.scope import ScopeService

BASE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def market_service(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'market-asof.db'}", BASE_DIR / "seed")
    db.initialize()
    market = MarketService(db, ScopeService(db))
    market.initialize()
    market.seed_market()
    yield market
    db.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-asof.db'}",
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
    return {"Authorization": f"Bearer {issue_token('asof-test', role, settings.jwt_secret)}"}


def test_series_detail_hides_unpublished_points_and_future_events(market_service):
    early = market_service.series_detail("OBJ-PUB-IN-0001", as_of="2024-02-01")
    assert early["asOf"] == "2024-02-01"
    assert early["points"] == []
    assert early["events"] == []

    mid = market_service.series_detail("OBJ-PUB-IN-0001", as_of="2024-03-15")
    assert [point["periodId"] for point in mid["points"]] == ["2024-01", "2024-02"]
    assert all(point["publishedAt"][:10] <= "2024-03-15" for point in mid["points"])
    assert [(event["eventId"], event["state"]) for event in mid["events"]] == [
        ("OBJ-PUB-EV-1001", "EFFECTIVE")
    ]
    assert mid["events"][0]["stateLabel"]
    assert mid["events"][0]["triggeredImpactCount"] == 1


def test_timeline_excludes_events_announced_after_as_of(market_service):
    assert market_service.timeline(as_of="2024-02-01")["items"] == []

    at_march = market_service.timeline(as_of="2024-03-15")
    assert [item["eventId"] for item in at_march["items"]] == [
        "OBJ-PUB-EV-1001", "OBJ-PUB-EV-1005"
    ]
    assert [item["state"] for item in at_march["items"]] == ["EFFECTIVE", "ANNOUNCED"]
    assert all(item["announceTime"][:10] <= "2024-03-15" for item in at_march["items"])


def test_overview_counts_respect_as_of(market_service):
    early = market_service.overview(as_of="2024-02-01")
    assert early["pointCount"] == 0
    assert early["eventCount"] == 0
    assert early["assertionCount"] == 0
    assert early["latestPeriod"] is None
    assert early["seriesCount"] == 8

    june = market_service.overview(as_of="2024-06-20")
    assert june["pointCount"] == 40
    assert june["eventCount"] == 4
    assert june["assertionCount"] == 0
    assert june["latestPeriod"] == "2024-05"

    full = market_service.overview()
    assert full["pointCount"] == 96
    assert full["eventCount"] == 6
    assert full["assertionCount"] == 6
    assert full["latestPeriod"] == "2024-12"


def test_assertions_respect_as_of(market_service):
    assert market_service.assertions(as_of="2024-09-01")["total"] == 0

    first = market_service.assertions(as_of="2024-09-11")
    assert first["total"] == 1
    assert first["items"][0]["items"][0]["assertionId"] == "AST-0001"

    late = market_service.assertions(as_of="2024-10-20")
    assert late["total"] == 6
    assert all(
        item["assertedAt"] <= "2024-10-20"
        for group in late["items"]
        for item in group["items"]
    )


def test_market_assertions_route_accepts_as_of(client, settings):
    response = client.get(
        "/api/market/assertions",
        params={"asOf": "2024-09-11"},
        headers=auth(settings),
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
