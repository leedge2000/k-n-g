# -*- coding: utf-8 -*-
"""阶段 5 运营包：指标口径、告警生命周期、增量入库看板、演进状态机与 G5 门禁的回归测试。"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.ontology import OntologyService
from app.ops import (
    ALERT_RULES,
    METRICS,
    OpsError,
    OpsScheduler,
    OpsService,
    judge_status,
)
from app.scope import ScopeService

BASE_DIR = Path(__file__).resolve().parents[1]
OPS_TABLES = ("OpsQualityMetric", "OpsAlert", "OpsEvolutionRequest", "OpsEvolutionRelease")


def build_ops(tmp_path: Path) -> tuple[Database, ScopeService, OntologyService, OpsService]:
    db = Database(f"sqlite:///{tmp_path / 'stage5.db'}", BASE_DIR / "seed")
    db.initialize()
    db.seed()
    scope = ScopeService(db)
    scope.initialize()
    ontology = OntologyService(db, BASE_DIR / "seed")
    ontology.initialize()
    ops = OpsService(db, scope)
    ops.initialize()
    return db, scope, ontology, ops


@pytest.fixture()
def stage5(tmp_path):
    db, scope, ontology, ops = build_ops(tmp_path)
    yield db, scope, ontology, ops
    db.close()


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-stage5.db'}",
        jwt_secret="test-secret-with-at-least-32-bytes-for-hs256",
        auto_seed=True,
        graph_backend="relational",
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def auth(settings, role="ROLE-04", subject="stage5-user"):
    return {"Authorization": f"Bearer {issue_token(subject, role, settings.jwt_secret)}"}


def _insert(db: Database, table: str, values: dict) -> None:
    with db.engine.begin() as conn:
        db.insert(table, values, conn, actor="stage5-test")


def _count(db: Database, sql: str, params: dict | None = None) -> int:
    return int((db.fetch_one(sql, params) or {}).get("total") or 0)


def _seed_scope(scope: ScopeService) -> dict:
    return scope.upsert_scope(
        scope_type="SERIES_SCOPE",
        members=[{"dimensionCode": "GEOGRAPHY", "objectType": "Region", "objectId": "REG0001"}],
        scope_code="SCOPE-TEST-REGION",
        label="初始标签",
        actor="stage5-test",
    )


def _approve(ops: OpsService, change_id: str, submitter="submitter", reviewer="reviewer"):
    ops.submit(change_id, submitter)
    ops.review(change_id, True, "同意", reviewer)
    return ops.publish(change_id, "publisher")


def _conflict_issue(issue_id: str) -> dict:
    return {
        "issueId": issue_id, "issueType": "冲突", "primaryEntityType": "指标数据",
        "primaryEntityId": "IVD0001", "duplicateEntityId": "IVD0002",
        "similarity": 1.0, "conflictField": "value", "conflictValueA": "1", "conflictValueB": "2",
        "handleStatus": "待处理",
    }


def test_ops_tables_registered_with_audit_columns(stage5):
    db, _, _, _ = stage5
    mapping = json.loads((BASE_DIR / "seed" / "mapping.json").read_text(encoding="utf-8"))
    ids = {item["id"] for item in mapping["mappings"]}
    assert {"M-MAP-OPS-017", "M-MAP-OPS-018", "M-MAP-OPS-019", "M-MAP-OPS-020"} <= ids
    for table in OPS_TABLES:
        assert table in db.tables
        columns = set(db.columns[table])
        assert {"createdBy", "createdAt", "updatedBy", "updatedAt"} <= columns


def test_compute_metrics_covers_frozen_codes(stage5):
    _, _, _, ops = stage5
    metrics = ops.compute_metrics()
    assert list(metrics) == list(METRICS)
    for code, metric in metrics.items():
        definition = METRICS[code]
        assert metric["direction"] == definition["direction"]
        assert metric["status"] == judge_status(
            metric["metricValue"], definition["direction"], definition["threshold"]
        )


def test_judge_status_boundaries():
    assert judge_status(0.96, "GTE", 0.95) == "OK"
    assert judge_status(0.9, "GTE", 0.95) == "WARN"
    assert judge_status(0.7, "GTE", 0.95) == "BREACH"
    assert judge_status(0.2, "LTE", 0.2) == "OK"
    assert judge_status(0.3, "LTE", 0.2) == "WARN"
    assert judge_status(0.5, "LTE", 0.2) == "BREACH"
    # 阈值为 0 的计数类指标不做 BREACH 分级，避免任何非零值都直接判死
    assert judge_status(1, "LTE", 0) == "WARN"


def test_inspect_writes_snapshots_and_dedupes_open_alerts(stage5):
    db, _, _, ops = stage5
    before = _count(db, 'SELECT COUNT(*) AS total FROM "OpsQualityMetric"')
    ops.inspect("tester")
    ops.inspect("tester")
    after = _count(db, 'SELECT COUNT(*) AS total FROM "OpsQualityMetric"')
    assert after - before == len(METRICS) * 2
    duplicates = db.fetch_all(
        'SELECT "alertCode", COUNT(*) AS total FROM "OpsAlert" WHERE "status"=\'OPEN\' '
        'GROUP BY "alertCode" HAVING COUNT(*)>1'
    )
    assert duplicates == []


def test_conflict_alert_lifecycle(stage5):
    db, _, _, ops = stage5
    _insert(db, "QisRecord", _conflict_issue("QIS-TEST-1"))
    ops.inspect("tester")
    alert = db.fetch_one(
        'SELECT * FROM "OpsAlert" WHERE "alertCode"=\'ALT-CONFLICT\' AND "status"=\'OPEN\''
    )
    assert alert and alert["severity"] == "P2"

    acked = ops.ack(alert["alertId"], "admin")
    assert acked["status"] == "ACK"
    with pytest.raises(HTTPException) as excinfo:
        ops.ack(alert["alertId"], "admin")
    assert excinfo.value.status_code == 409

    closed = ops.close(alert["alertId"], "admin")
    assert closed["status"] == "CLOSED"
    with pytest.raises(HTTPException) as excinfo:
        ops.close(alert["alertId"], "admin")
    assert excinfo.value.status_code == 409


def test_alert_auto_closes_when_metric_recovers(stage5):
    db, _, _, ops = stage5
    _insert(db, "QisRecord", _conflict_issue("QIS-TEST-2"))
    ops.inspect("tester")
    assert _count(
        db, 'SELECT COUNT(*) AS total FROM "OpsAlert" WHERE "alertCode"=\'ALT-CONFLICT\' AND "status"=\'OPEN\''
    ) == 1
    with db.engine.begin() as conn:
        conn.execute(__import__("sqlalchemy").text('DELETE FROM "QisRecord" WHERE "issueId"=\'QIS-TEST-2\''))
    ops.inspect("tester")
    assert _count(
        db, 'SELECT COUNT(*) AS total FROM "OpsAlert" WHERE "alertCode"=\'ALT-CONFLICT\' AND "status" IN (\'OPEN\',\'ACK\')'
    ) == 0


def test_ingest_reports_stale_source_and_backlog(stage5):
    db, _, _, ops = stage5
    category = db.fetch_one('SELECT "categoryId" FROM "KtcCategory" LIMIT 1')["categoryId"]
    _insert(db, "CrsSource", {
        "crawlSourceId": "CRS-TEST-1", "sourceName": "测试采集源",
        "sourceUrl": "https://example.com/news", "sourceType": "网页",
        "contentCategory": "政策", "crawlMode": "定时", "minIntervalMinutes": 60,
        "maxRetry": 2, "knowledgeCategoryId": category, "enabledFlag": "启用",
        "lastRunAt": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    })
    report = ops.ingest()
    source = next(item for item in report["sources"] if item["crawlSourceId"] == "CRS-TEST-1")
    assert source["staleFlag"] is True
    assert report["backlog"]["total"] == sum(report["backlog"]["byType"].values())
    assert "cursor" in report["changes"]


def test_retry_blocked_after_max_failures(stage5):
    db, _, _, ops = stage5
    category = db.fetch_one('SELECT "categoryId" FROM "KtcCategory" LIMIT 1')["categoryId"]
    _insert(db, "CrsSource", {
        "crawlSourceId": "CRS-TEST-2", "sourceName": "失败源",
        "sourceUrl": "https://example.com/fail", "sourceType": "网页",
        "contentCategory": "政策", "crawlMode": "定时", "minIntervalMinutes": 60,
        "maxRetry": 2, "knowledgeCategoryId": category, "enabledFlag": "启用",
    })
    for index in range(2):
        _insert(db, "CrsSource_CrawlRun", {
            "runId": f"RUN-TEST-{index}", "crawlSourceId": "CRS-TEST-2",
            "startedAt": datetime.now(timezone.utc).isoformat(), "runStatus": "失败",
            "newDocCount": 0, "duplicateCount": 0,
        })
    with pytest.raises(HTTPException) as excinfo:
        ops.retry_failed("CRS-TEST-2", "editor")
    assert excinfo.value.status_code == 409


def test_evolution_rejects_illegal_transitions(stage5):
    _, _, _, ops = stage5
    change = ops.create_draft({
        "targetKind": "ONTOLOGY_META", "changeType": "UPDATE", "targetId": "OT-TEST",
        "draftPayload": {"displayName": "改名"},
    }, "admin")
    with pytest.raises(HTTPException) as excinfo:
        ops.publish(change["changeId"], "admin")
    assert excinfo.value.status_code == 409
    with pytest.raises(HTTPException) as excinfo:
        ops.review(change["changeId"], True, "ok", "reviewer")
    assert excinfo.value.status_code == 409


def test_publish_requires_separation_of_duties(stage5):
    _, _, _, ops = stage5
    change = ops.create_draft({
        "targetKind": "ONTOLOGY_META", "changeType": "UPDATE", "targetId": "OT-TEST",
        "draftPayload": {"displayName": "改名"},
    }, "admin")
    ops.submit(change["changeId"], "same-user")
    ops.review(change["changeId"], True, "ok", "same-user")
    with pytest.raises(HTTPException) as excinfo:
        ops.publish(change["changeId"], "same-user")
    assert excinfo.value.status_code == 409


def test_scope_evolution_publish_and_rollback(stage5):
    db, scope, _, ops = stage5
    created = _seed_scope(scope)
    change = ops.create_draft({
        "targetKind": "SCOPE", "changeType": "UPDATE", "targetId": created["scopeId"],
        "draftPayload": {
            "scopeCode": "SCOPE-TEST-REGION", "scopeType": "SERIES_SCOPE", "label": "变更后的标签",
            "members": [{"dimensionCode": "GEOGRAPHY", "objectType": "Region", "objectId": "REG0002"}],
        },
    }, "admin")
    result = _approve(ops, change["changeId"])
    assert result["status"] == "PUBLISHED"

    row = db.fetch_one('SELECT * FROM "OntologyScope" WHERE "scopeId"=:id', {"id": created["scopeId"]})
    assert row["label"] == "变更后的标签"
    assert row["memberCount"] == 1
    assert int(row["version"]) == 2

    ops.rollback(change["changeId"], "admin")
    row = db.fetch_one('SELECT * FROM "OntologyScope" WHERE "scopeId"=:id', {"id": created["scopeId"]})
    assert row["label"] == "初始标签"
    assert int(row["version"]) == 1
    release = db.fetch_one('SELECT * FROM "OpsEvolutionRelease" ORDER BY "versionNo" DESC LIMIT 1')
    assert release["status"] == "ROLLED_BACK"


def test_meta_evolution_survives_ontology_sync(stage5):
    db, _, ontology, ops = stage5
    target = db.fetch_one('SELECT * FROM "OntologyObjectType" ORDER BY "objectTypeId" LIMIT 1')
    original = target["displayName"]
    change = ops.create_draft({
        "targetKind": "ONTOLOGY_META", "changeType": "UPDATE", "targetId": target["objectTypeId"],
        "draftPayload": {"displayName": "运营改名验证"},
    }, "admin")
    _approve(ops, change["changeId"])
    assert db.fetch_one(
        'SELECT "displayName" FROM "OntologyObjectType" WHERE "objectTypeId"=:id',
        {"id": target["objectTypeId"]},
    )["displayName"] == "运营改名验证"

    ontology.sync()
    assert db.fetch_one(
        'SELECT "displayName" FROM "OntologyObjectType" WHERE "objectTypeId"=:id',
        {"id": target["objectTypeId"]},
    )["displayName"] == "运营改名验证"

    ops.rollback(change["changeId"], "admin")
    assert db.fetch_one(
        'SELECT "displayName" FROM "OntologyObjectType" WHERE "objectTypeId"=:id',
        {"id": target["objectTypeId"]},
    )["displayName"] == original


def test_rule_evolution_only_touches_whitelisted_fields(stage5):
    db, _, _, ops = stage5
    _insert(db, "CompiledRule", {
        "ruleId": "CR-TEST", "statementId": "NS-TEST", "ruleExpression": "1=1",
        "compilerVersion": "v1", "ontologyVersion": "v1", "confirmationStatus": "DRAFT",
        "testcaseIds": "[]", "backTranslationMatch": 0,
    })
    change = ops.create_draft({
        "targetKind": "RULE", "changeType": "UPDATE", "targetId": "CR-TEST",
        "draftPayload": {"confirmationStatus": "CONFIRMED", "testcaseIds": ["TC1", "TC2"]},
    }, "admin")
    result = _approve(ops, change["changeId"])
    assert "规则 CR-TEST" in result["impactSummary"]
    row = db.fetch_one('SELECT * FROM "CompiledRule" WHERE "ruleId"=\'CR-TEST\'')
    assert row["confirmationStatus"] == "CONFIRMED"
    assert json.loads(row["testcaseIds"]) == ["TC1", "TC2"]
    assert row["ruleExpression"] == "1=1"

    ops.rollback(change["changeId"], "admin")
    row = db.fetch_one('SELECT * FROM "CompiledRule" WHERE "ruleId"=\'CR-TEST\'')
    assert row["confirmationStatus"] == "DRAFT"
    assert json.loads(row["testcaseIds"]) == []


def test_gate_rejects_duplicate_open_alert(stage5):
    db, _, _, ops = stage5
    for index in range(2):
        _insert(db, "OpsAlert", {
            "alertId": f"ALT-DUP-{index}", "alertCode": "ALT-TEST", "severity": "P3",
            "title": "重复告警", "status": "OPEN", "raisedAt": datetime.now(timezone.utc).isoformat(),
        })
    with pytest.raises(OpsError) as excinfo:
        ops.validate_stage5()
    assert "G5-3" in str(excinfo.value)


def test_gate_rejects_version_gap(stage5):
    db, _, _, ops = stage5
    _insert(db, "OpsEvolutionRelease", {
        "releaseId": "RVW-GAP", "versionNo": 5, "changeIds": "[]", "snapshot": "{}",
        "status": "ACTIVE", "publishedAt": datetime.now(timezone.utc).isoformat(),
    })
    with pytest.raises(OpsError) as excinfo:
        ops.validate_stage5()
    assert "G5-6" in str(excinfo.value)


def test_scheduler_runs_once_per_interval(stage5):
    _, _, _, ops = stage5
    scheduler = OpsScheduler(ops, interval_minutes=30)
    moment = datetime.now(timezone.utc)
    assert scheduler.run_due(moment)["metrics"]
    assert scheduler.run_due(moment + timedelta(minutes=1)) is None
    assert scheduler.run_due(moment + timedelta(minutes=31))["metrics"]


def test_ops_routes_and_permissions(client, settings):
    assert client.get("/ops.html").status_code == 200
    analyst = auth(settings, "ROLE-04")

    overview = client.get("/api/ops/overview", headers=analyst)
    assert overview.status_code == 200
    assert len(overview.json()["metrics"]) == len(METRICS)

    assert client.get("/api/ops/ingest", headers=analyst).status_code == 403
    assert client.post("/api/ops/inspect", headers=analyst).status_code == 403
    assert client.get("/api/ops/evolution", headers=analyst).status_code == 200
    assert client.get("/api/ops/releases", headers=analyst).status_code == 200
    assert client.get("/api/ops/metrics?limit=5", headers=analyst).status_code == 200
    assert client.get("/api/ops/alerts?status=OPEN", headers=analyst).status_code == 200
    assert client.post("/api/ops/alerts/ALT-NONE/ack", headers=auth(settings, "ROLE-01")).status_code == 404


def test_ops_ingest_route_for_editor(client, settings):
    editor = auth(settings, "ROLE-02")
    response = client.get("/api/ops/ingest", headers=editor)
    assert response.status_code == 200
    assert "sources" in response.json()
    retry = client.post("/api/ops/ingest/retry", json={}, headers=editor)
    assert retry.status_code == 422


def test_evolution_draft_validation(client, settings):
    admin = auth(settings, "ROLE-01")
    bad_kind = client.post("/api/ops/evolution", json={
        "targetKind": "UNKNOWN", "changeType": "UPDATE", "draftPayload": {"a": 1},
    }, headers=admin)
    assert bad_kind.status_code == 422
    missing_target = client.post("/api/ops/evolution", json={
        "targetKind": "RULE", "changeType": "UPDATE", "draftPayload": {"confirmationStatus": "CONFIRMED"},
    }, headers=admin)
    assert missing_target.status_code == 422
    created = client.post("/api/ops/evolution", json={
        "targetKind": "ONTOLOGY_META", "changeType": "UPDATE", "targetId": "OT-TEST",
        "draftPayload": {"description": "描述"},
    }, headers=admin)
    assert created.status_code == 200 and created.json()["status"] == "DRAFT"


def test_alert_rules_cover_known_metrics():
    codes = {rule["metricCode"] for rule in ALERT_RULES}
    assert codes <= set(METRICS)
    assert len({rule["alertCode"] for rule in ALERT_RULES}) == len(ALERT_RULES)
