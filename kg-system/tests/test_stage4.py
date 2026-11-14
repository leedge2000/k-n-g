# -*- coding: utf-8 -*-
"""阶段 4 市场包：新增物理表、G4 门禁、事件四态与影响触发、只读路由与静态页的回归测试。"""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import issue_token
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market import SERIES_SCOPE, MarketError, MarketService
from app.scope import ScopeService

BASE_DIR = Path(__file__).resolve().parents[1]

STAGE4_TABLES = (
    "CommodityForm",
    "MarketPlace",
    "Evidence",
    "ImpactLink",
    "FactorDriveLink",
    "EventFactorLink",
)


def build_service(tmp_path: Path) -> tuple[Database, MarketService]:
    """函数级独立库：业务种子 + 市场种子，破坏性门禁测试互不影响。"""
    db = Database(f"sqlite:///{tmp_path / 'stage4.db'}", BASE_DIR / "seed")
    db.initialize()
    db.seed()
    scope = ScopeService(db)
    service = MarketService(db, scope)
    service.initialize()
    service.seed_market()
    return db, service


@pytest.fixture()
def stage4(tmp_path):
    db, service = build_service(tmp_path)
    yield db, service
    db.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-stage4.db'}",
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
    return {"Authorization": f"Bearer {issue_token('stage4-user', role, settings.jwt_secret)}"}


def _count(db: Database, table: str) -> int:
    return db.fetch_one(f'SELECT COUNT(*) AS total FROM "{table}"')["total"]


def _row(db: Database, table: str, key: str, value: str) -> dict:
    return db.fetch_one(f'SELECT * FROM "{table}" WHERE "{key}"=:value', {"value": value})


def _execute(db: Database, sql: str, params: dict | None = None) -> None:
    with db.engine.begin() as conn:
        conn.execute(text(sql), params or {})


def _insert(db: Database, table: str, values: dict) -> None:
    with db.engine.begin() as conn:
        db.insert(table, values, conn, actor="stage4-test")


def _quote_hash(quote: str) -> str:
    return hashlib.sha1(quote.encode("utf-8")).hexdigest()


def test_stage4_tables_exist_with_composite_primary_keys(stage4):
    db, _ = stage4
    ddl = {}
    for table in STAGE4_TABLES:
        row = db.fetch_one(
            'SELECT "sql" AS ddl FROM "sqlite_master" WHERE "type"=\'table\' AND "name"=:name',
            {"name": table},
        )
        assert row, f"缺少物理表：{table}"
        ddl[table] = row["ddl"]

    assert 'PRIMARY KEY ("factorId", "seriesId")' in ddl["FactorDriveLink"]
    assert 'PRIMARY KEY ("eventId", "factorId")' in ddl["EventFactorLink"]


def test_stage4_tables_carry_audit_columns(stage4):
    db, _ = stage4
    audit = {"createdBy", "createdAt", "updatedBy", "updatedAt"}
    for table in STAGE4_TABLES:
        columns = {row["name"] for row in db.fetch_all(f'PRAGMA table_info("{table}")')}
        assert audit <= columns, f"{table} 缺少审计列：{audit - columns}"


def test_seed_lands_stage4_counts(stage4):
    db, _ = stage4
    assert _count(db, "CommodityForm") == 8
    assert _count(db, "MarketPlace") == 3
    assert _count(db, "Evidence") == 10
    assert _count(db, "ImpactLink") == 4
    assert _count(db, "FactorDriveLink") == 5
    assert _count(db, "EventFactorLink") == 4


def test_series_form_backfill_matches_seed_expectations(stage4):
    db, _ = stage4
    expected = {
        "OBJ-PUB-IN-0001": "OBJ-PUB-CM-0001",
        "OBJ-PUB-IN-0002": "OBJ-PUB-CM-0003",
        "OBJ-PUB-IN-0003": "CMD0006",
        "OBJ-PUB-IN-0004": "CMD0007",
    }
    for series_id, form_id in expected.items():
        row = _row(db, "IndicatorSeries", "seriesId", series_id)
        assert row["commodityFormId"] == form_id


def test_each_series_scope_has_single_channel_market_place(stage4):
    db, _ = stage4
    scopes = db.fetch_all(
        'SELECT "scopeId","scopeCode","members" FROM "OntologyScope" WHERE "scopeType"=:type',
        {"type": SERIES_SCOPE},
    )
    assert len(scopes) == 8
    for scope in scopes:
        members = json.loads(scope["members"])
        channels = [
            member for member in members
            if str(member.get("dimensionCode") or "").upper() == "CHANNEL"
        ]
        assert len(channels) == 1, scope["scopeCode"]
        place = db.fetch_one(
            'SELECT "placeId" FROM "MarketPlace" WHERE "placeId"=:id',
            {"id": channels[0]["objectId"]},
        )
        assert place, f"{scope['scopeCode']} 的渠道市场不存在"


def test_validate_stage4_passes_on_seed(stage4):
    _, service = stage4
    result = service.validate_stage4()
    assert set(result["gates"]) == {
        "G4-1", "G4-2", "G4-3", "G4-4", "G4-5", "G4-6", "G4-7", "G4-8",
    }
    assert all(result["gates"].values())
    assert result["counts"] == {
        "series": 8,
        "commodityForms": 8,
        "places": 3,
        "evidence": 10,
        "impactLinks": 4,
        "factorDrives": 5,
        "eventFactors": 4,
        "assertions": 6,
    }


def test_g4_1_rejects_series_with_missing_form(stage4):
    db, service = stage4
    row = dict(_row(db, "IndicatorSeries", "seriesId", "OBJ-PUB-IN-0005"))
    row["seriesId"] = "OBJ-PUB-IN-9999"
    row["commodityFormId"] = "OBJ-PUB-CM-9999"
    _insert(db, "IndicatorSeries", row)

    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-1" in message
    assert "OBJ-PUB-IN-9999" in message


def test_g4_3_rejects_impact_link_without_evidence(stage4):
    db, service = stage4
    _execute(
        db,
        'DELETE FROM "Evidence" WHERE "targetType"=\'ImpactLink\' AND "targetId"=\'LNK-IM-0001\'',
    )
    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-3" in message
    assert "LNK-IM-0001" in message


def test_g4_3_rejects_policy_link_pointing_to_series(stage4):
    db, service = stage4
    link = dict(_row(db, "ImpactLink", "linkId", "LNK-IM-0001"))
    link.update({
        "linkId": "LNK-IM-9999",
        "sourceType": "POLICY",
        "sourceId": "POL0001",
        "targetType": "SERIES",
        "targetId": "OBJ-PUB-IN-0001",
        "lagDays": 0,
    })
    _insert(db, "ImpactLink", link)
    quote = "测试引用：政策影响边不得直接指向指标序列。"
    _insert(db, "Evidence", {
        "evidenceId": "OBJ-PUB-ED-9999",
        "targetType": "ImpactLink",
        "targetId": "LNK-IM-9999",
        "documentId": "SOU0001",
        "quote": quote,
        "quoteHash": _quote_hash(quote),
        "pageNo": 1,
        "evidenceType": "CITED",
        "strength": 0.5,
        "verifiedBy": "system",
        "verifiedAt": "2024-11-01",
    })

    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-3" in message
    assert "LNK-IM-9999" in message


def test_g4_3_rejects_magnitude_without_market_pack_assertion(stage4):
    db, service = stage4
    _execute(
        db,
        'UPDATE "ImpactLink" SET "impactMagnitude"=\'STRONG\', "assertedByType"=\'ORGANIZATION\' '
        'WHERE "linkId"=\'LNK-IM-0001\'',
    )
    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-3" in message
    assert "LNK-IM-0001" in message


def test_g4_4_rejects_drive_without_elasticity_and_lag(stage4):
    db, service = stage4
    _execute(
        db,
        'UPDATE "FactorDriveLink" SET "elasticity"=NULL, "lagDays"=NULL '
        'WHERE "factorId"=\'FAC0001\' AND "seriesId"=\'OBJ-PUB-IN-0005\'',
    )
    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    assert "G4-4" in str(error.value)


def test_g4_5_rejects_effective_assertion_without_evidence(stage4):
    db, service = stage4
    _execute(
        db,
        'DELETE FROM "Evidence" WHERE "targetType"=\'Assertion\' AND "targetId"=\'AST-0006\'',
    )
    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-5" in message
    assert "AST-0006" in message


def test_g4_6_rejects_inverted_event_window(stage4):
    db, service = stage4
    _execute(
        db,
        'UPDATE "Event" SET "effectiveFrom"=\'2024-10-01\', "effectiveTo"=\'2024-06-01\' '
        'WHERE "eventId"=\'OBJ-PUB-EV-1004\'',
    )
    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-6" in message
    assert "OBJ-PUB-EV-1004" in message


def test_g4_7_rejects_tampered_quote_hash(stage4):
    db, service = stage4
    _execute(
        db,
        'UPDATE "Evidence" SET "quoteHash"=\'deadbeef\' WHERE "evidenceId"=\'OBJ-PUB-ED-0001\'',
    )
    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-7" in message
    assert "OBJ-PUB-ED-0001" in message


def test_g4_8_rejects_missing_month_in_series(stage4):
    db, service = stage4
    _execute(
        db,
        'DELETE FROM "PriceDataPoint" WHERE "seriesId"=\'OBJ-PUB-IN-0005\' '
        'AND "periodId"=\'2024-06\'',
    )
    with pytest.raises(MarketError) as error:
        service.validate_stage4()
    message = str(error.value)
    assert "G4-8" in message
    assert "OBJ-PUB-IN-0005" in message


def test_event_state_follows_four_phase_lifecycle(stage4):
    db, service = stage4
    policy_event = _row(db, "Event", "eventId", "OBJ-PUB-EV-1001")
    assert service.event_state(policy_event, "2024-02-01") == "PENDING"
    assert service.event_state(policy_event, "2024-06-15") == "EFFECTIVE"
    assert service.event_state(policy_event, "2024-08-15") == "EXPIRED"
    assert service.event_state(policy_event, "2025-01-31") == "EXPIRED"

    announced_event = _row(db, "Event", "eventId", "OBJ-PUB-EV-1004")
    assert service.event_state(announced_event, "2024-05-25") == "ANNOUNCED"


def test_impact_trigger_boundary_respects_lag_and_policy(stage4):
    db, service = stage4
    lagged = _row(db, "ImpactLink", "linkId", "LNK-IM-0003")
    assert service.impact_triggered(lagged, "2024-04-14") is False
    assert service.impact_triggered(lagged, "2024-04-15") is True

    policy = _row(db, "ImpactLink", "linkId", "LNK-IM-0004")
    assert service.impact_triggered(policy, "2023-01-01") is True


def test_seed_market_is_idempotent_for_stage4_tables(stage4):
    db, service = stage4
    tables = (
        "CommodityForm", "MarketPlace", "Evidence", "ImpactLink", "FactorDriveLink",
        "EventFactorLink", "IndicatorSeries", "PriceDataPoint", "Event", "Factor",
        "Assertion", "PriceTransmissionLink",
    )
    before = {table: _count(db, table) for table in tables}

    service.seed_market()
    service.seed_market()

    assert {table: _count(db, table) for table in tables} == before
    assert all(service.validate_stage4()["gates"].values())


def test_stage4_routes_expose_readonly_payloads(client, settings):
    headers = auth(settings)

    overview = client.get(
        "/api/market/overview", params={"asOf": "2024-06-30"}, headers=headers
    )
    assert overview.status_code == 200
    body = overview.json()
    assert body["asOf"] == "2024-06-30"
    assert len(body["scopeDimensions"]) == 5

    detail = client.get(
        "/api/market/series/OBJ-PUB-IN-0001", params={"asOf": "2024-06-30"}, headers=headers
    )
    assert detail.status_code == 200
    impacts = detail.json()["impacts"]
    assert impacts
    assert all("triggered" in item and "evidenceCount" in item for item in impacts)

    timeline = client.get(
        "/api/market/timeline", params={"asOf": "2024-06-30"}, headers=headers
    )
    assert timeline.status_code == 200
    assert timeline.json()["asOf"] == "2024-06-30"

    forms = client.get(
        "/api/market/commodity-forms", params={"commodityId": "CMD0001"}, headers=headers
    )
    assert forms.status_code == 200
    assert forms.json()["total"] == 2
    assert {item["formId"] for item in forms.json()["items"]} == {
        "OBJ-PUB-CM-0001", "OBJ-PUB-CM-0005"
    }

    places = client.get(
        "/api/market/places", params={"marketType": "SPOT"}, headers=headers
    )
    assert places.status_code == 200
    assert {item["marketType"] for item in places.json()["items"]} == {"SPOT"}

    links = client.get(
        "/api/market/impacts", params={"asOf": "2024-04-15"}, headers=headers
    )
    assert links.status_code == 200
    assert links.json()["asOf"] == "2024-04-15"
    assert links.json()["total"] == 4
    assert all(
        item["triggered"] is not None and "evidenceCount" in item
        for item in links.json()["items"]
    )

    evidence = client.get(
        "/api/market/evidence",
        params={"targetType": "ImpactLink", "targetId": "LNK-IM-0001"},
        headers=headers,
    )
    assert evidence.status_code == 200
    assert evidence.json()["total"] == 1

    missing = client.get("/api/market/evidence", headers=headers)
    assert missing.status_code == 400

    factor = client.get(
        "/api/market/factors/FAC0001", params={"asOf": "2024-06-30"}, headers=headers
    )
    assert factor.status_code == 200
    assert factor.json()["drives"][0]["seriesId"] == "OBJ-PUB-IN-0005"

    absent = client.get("/api/market/factors/FAC9999", headers=headers)
    assert absent.status_code == 404


def test_market_routes_expose_no_write_methods(client):
    schema = client.get("/openapi.json").json()
    paths = {path: methods for path, methods in schema["paths"].items() if path.startswith("/api/market/")}
    assert paths
    for path, methods in paths.items():
        assert set(methods) <= {"get"}, f"{path} 暴露了写方法：{set(methods) - {'get'}}"


def test_market_static_pages_are_served(client):
    page = client.get("/market.html")
    assert page.status_code == 200
    html = page.text
    for marker in ("asof-range", "form-chips", "drive-panel", "impact-panel", "badge-effective"):
        assert marker in html, f"market.html 缺少标记：{marker}"

    index = client.get("/index.html")
    assert index.status_code == 200
    assert "entityType" in index.text
    assert "market.html?commodity=" in index.text
