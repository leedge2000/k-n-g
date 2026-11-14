import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.main import create_app


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


def test_health_is_public_and_reports_graph_counts(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["counts"]["knowledgeCategory"] == 280
    assert body["counts"]["publishedRelations"] >= 3


def test_search_requires_bearer_token(client):
    assert client.get("/api/kg/search", params={"keyword": "稻谷"}).status_code == 401


def test_search_returns_published_knowledge_and_facets(client, settings):
    response = client.get("/api/kg/search", params={"keyword": "稻谷"}, headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 4
    assert any(item["entityType"] == "政策法规" for item in body["items"])
    assert any(item["entityType"] == "指标数据" for item in body["items"])
    assert all(item["status"] == "已发布" for item in body["items"])
    assert set(body["facets"]) == {"category", "industryStage", "factorDimension"}


def test_search_expands_category_to_all_descendants(client, settings):
    response = client.get(
        "/api/kg/search",
        params={"categoryCode": "AE-02", "pageSize": 100},
        headers=auth(settings),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 8
    assert all(item["categoryPath"].startswith("农业经济/粮食生产经济") for item in body["items"])


def test_entity_detail_contains_one_hop_relations_and_sources(client, settings):
    response = client.get("/api/kg/entities/指标数据/IVD0002", headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "2024年中国稻谷产量"
    assert body["properties"]["value"] == 20753
    predicates = {relation["predicate"] for relation in body["relations"]}
    assert {"对应指标", "统计地区", "涉及品种", "来源文档"}.issubset(predicates)
    assert body["sources"][0]["sourceDocId"] == "SOU0002"
    assert body["sources"][0]["segmentId"] == "SOU0002-S002"


def test_missing_entity_returns_404(client, settings):
    response = client.get("/api/kg/entities/品种/NOT-FOUND", headers=auth(settings))
    assert response.status_code == 404


def test_traceability_returns_verified_paragraph_location(client, settings):
    response = client.get("/api/kg/traceability/指标数据/IVD0002", headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["docTitle"] == "中华人民共和国2024年国民经济和社会发展统计公报"
    assert "稻谷产量20753万吨" in body["segmentText"]
    assert body["charEnd"] > body["charStart"]
    assert body["sourceUrl"].startswith("https://www.stats.gov.cn/")


def test_graph_expands_business_triples_and_structural_edges(client, settings):
    response = client.get(
        "/api/kg/graph/expand",
        params={"entityType": "品种", "entityId": "CMD0001", "depth": 2},
        headers=auth(settings),
    )
    assert response.status_code == 200
    body = response.json()
    assert any(node["id"] == "CMD0001" for node in body["nodes"])
    assert any(edge["predicate"] == "产于" for edge in body["edges"])
    assert any(edge["predicate"] == "涉及品种" for edge in body["edges"])
    assert len({node["id"] for node in body["nodes"]}) == len(body["nodes"])


def test_taxonomy_contains_complete_280_node_tree(client, settings):
    response = client.get("/api/kg/taxonomy", params={"pageSize": 400}, headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 280
    assert sum(item["level"] == 1 for item in body["items"]) == 4
    assert sum(item["level"] == 2 for item in body["items"]) == 31


def test_source_registration_creates_paragraph_level_evidence(client, settings):
    payload = {
        "title": "测试来源文档",
        "docType": "农业经济",
        "issuer": "测试机构",
        "sourceUrl": "https://example.com/source",
        "knowledgeCategoryId": "KTC0011",
        "segments": [{"chapterPath": "一、生产", "pageNo": 1, "segmentText": "第一段。"}],
    }
    response = client.post("/api/sources", json=payload, headers=auth(settings, "ROLE-02"))
    assert response.status_code == 201
    body = response.json()
    assert body["docId"].startswith("SOU")
    trace = client.get(
        f"/api/kg/traceability/来源文档/{body['docId']}", headers=auth(settings)
    )
    assert trace.status_code == 200
    assert trace.json()["segmentText"] == "第一段。"


def test_source_file_serves_saved_crawler_snapshot(client, settings):
    checksum = "a" * 64
    html = "<html><body>已保存的稻谷来源快照</body></html>"
    snapshot = settings.data_dir / "snapshots" / f"{checksum}.html"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(html, encoding="utf-8")
    created = client.post("/api/sources", json={
        "title": "稻谷来源快照",
        "docType": "自动采集",
        "fileFormat": "HTML",
        "sourceUrl": "https://example.com/source",
        "knowledgeCategoryId": "KTC0011",
        "checksum": checksum,
        "storageUri": snapshot.as_uri(),
        "segments": [{"segmentText": "已保存的稻谷来源快照"}],
    }, headers=auth(settings, "ROLE-02"))
    assert created.status_code == 201

    response = client.get(
        f"/api/sources/{created.json()['docId']}/file", headers=auth(settings)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == html


def test_source_file_serves_offline_database_body_when_snapshot_missing(client, settings):
    source_url = "https://example.com/original-policy"
    created = client.post("/api/sources", json={
        "title": "仅保存正文的旧文档",
        "docType": "政策详情",
        "fileFormat": "HTML",
        "sourceUrl": source_url,
        "knowledgeCategoryId": "KTC0011",
        "summary": "旧文档摘要",
        "segments": [{"segmentText": "数据库中保存的正文"}],
    }, headers=auth(settings, "ROLE-02"))
    assert created.status_code == 201

    response = client.get(
        f"/api/sources/{created.json()['docId']}/file",
        headers=auth(settings),
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "仅保存正文的旧文档" in response.text
    assert "数据库中保存的正文" in response.text
    assert "旧文档摘要" in response.text
    assert "location" not in response.headers


def test_archive_serves_saved_response_bytes(client, settings):
    content = b'{"TITLE":"\xe7\xb2\xae\xe9\xa3\x9f\xe6\x94\xbf\xe7\xad\x96"}'
    record = client.app.state.archive.save(
        "https://example.com/ZUIXINZHENGCE.json", content, "application/json"
    )

    response = client.get(
        f"/api/archive/{record['name']}", headers=auth(settings)
    )

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].startswith("application/json")
    manifest = settings.data_dir / "archive" / "manifest.jsonl"
    assert record["checksum"] in manifest.read_text(encoding="utf-8")


def test_knowledge_entry_goes_through_review_before_searchable(client, settings):
    payload = {
        "indicatorId": "IND0002",
        "regionId": "REG0001",
        "commodityId": "CMD0001",
        "periodType": "年度",
        "period": "2023",
        "value": 20660,
        "unit": "万吨",
        "sourceDocId": "SOU0002",
        "segmentId": "SOU0002-S002",
        "knowledgeCategoryId": "KTC0012",
        "industryStage": "生产",
    }
    created = client.post("/api/knowledge/指标数据", json=payload, headers=auth(settings, "ROLE-02"))
    assert created.status_code == 201
    entity_id = created.json()["entityId"]
    assert created.json()["status"] == "草稿"
    submitted = client.post(
        f"/api/knowledge/指标数据/{entity_id}/submit", headers=auth(settings, "ROLE-02")
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "待审核"
    reviewed = client.post(
        f"/api/knowledge/指标数据/{entity_id}/review",
        json={"decision": "通过", "comment": "来源一致"},
        headers=auth(settings, "ROLE-03"),
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "已发布"
    found = client.get(
        "/api/kg/search", params={"keyword": entity_id}, headers=auth(settings)
    )
    assert found.json()["total"] == 1


def test_low_confidence_relation_cannot_be_published(client, settings):
    payload = {
        "subjectEntityType": "品种",
        "subjectEntityId": "CMD0001",
        "predicate": "测试关系",
        "objectEntityType": "地区",
        "objectEntityId": "REG0001",
        "confidence": 0.4,
        "sourceDocId": "SOU0002",
        "segmentId": "SOU0002-S002",
    }
    created = client.post("/api/knowledge/知识关系", json=payload, headers=auth(settings, "ROLE-02"))
    relation_id = created.json()["entityId"]
    client.post(f"/api/knowledge/知识关系/{relation_id}/submit", headers=auth(settings, "ROLE-02"))
    response = client.post(
        f"/api/knowledge/知识关系/{relation_id}/review",
        json={"decision": "通过"},
        headers=auth(settings, "ROLE-03"),
    )
    assert response.status_code == 422


def test_duplicate_and_conflict_detection_create_governance_records(client, settings):
    payload = {
        "indicatorId": "IND0002",
        "regionId": "REG0001",
        "commodityId": "CMD0001",
        "periodType": "年度",
        "period": "2024",
        "value": 99999,
        "unit": "万吨",
        "sourceDocId": "SOU0002",
        "segmentId": "SOU0002-S002",
        "knowledgeCategoryId": "KTC0012",
        "industryStage": "生产",
    }
    created = client.post("/api/knowledge/指标数据", json=payload, headers=auth(settings, "ROLE-02"))
    assert created.status_code == 201
    detected = client.post("/api/governance/detect", headers=auth(settings, "ROLE-01"))
    assert detected.status_code == 200
    assert detected.json()["duplicates"] >= 1
    assert detected.json()["conflicts"] >= 1
    records = client.get("/api/governance/records", headers=auth(settings, "ROLE-01"))
    issue_types = {item["issueType"] for item in records.json()["items"]}
    assert {"重复", "冲突"}.issubset(issue_types)


def test_entity_merge_rewires_relations_and_emits_merged_change(client, settings):
    duplicate = {
        "commodityCode": "CMD-RICE-DUP",
        "commodityName": "稻谷（待融合）",
        "commodityCategory": "谷物",
        "conceptId": "CON0001",
        "status": "有效",
    }
    created = client.post("/api/master/品种", json=duplicate, headers=auth(settings, "ROLE-01"))
    duplicate_id = created.json()["entityId"]
    relation = {
        "subjectEntityType": "品种",
        "subjectEntityId": duplicate_id,
        "predicate": "产于",
        "objectEntityType": "地区",
        "objectEntityId": "REG0001",
        "confidence": 0.95,
        "sourceDocId": "SOU0002",
        "segmentId": "SOU0002-S002",
        "status": "已发布",
    }
    relation_created = client.post("/api/knowledge/知识关系", json=relation, headers=auth(settings, "ROLE-02"))
    relation_id = relation_created.json()["entityId"]
    merged = client.post(
        "/api/governance/merge",
        json={"entityType": "品种", "sourceEntityId": duplicate_id, "targetEntityId": "CMD0001", "similarity": 0.96},
        headers=auth(settings, "ROLE-01"),
    )
    assert merged.status_code == 200
    detail = client.get(f"/api/kg/entities/知识关系/{relation_id}", headers=auth(settings)).json()
    assert detail["properties"]["subjectEntityId"] == "CMD0001"
    changes = client.get("/api/kg/changes", params={"since": "2020-01-01T00:00:00Z", "limit": 1000}, headers=auth(settings)).json()
    merge_changes = [item for item in changes["changes"] if item["changeType"] == "MERGED"]
    assert merge_changes[-1]["payload"]["mergedIntoId"] == "CMD0001"


def test_void_source_marks_downstream_invalid_without_deleting_it(client, settings):
    response = client.post("/api/sources/SOU0001/void", headers=auth(settings, "ROLE-01"))
    assert response.status_code == 200
    detail = client.get("/api/kg/entities/政策法规/POL0001", headers=auth(settings)).json()
    assert detail["status"] == "已失效"
    changes = client.get("/api/kg/changes", params={"since": "2020-01-01T00:00:00Z", "limit": 1000}, headers=auth(settings)).json()["changes"]
    invalidated = [item for item in changes if item["changeType"] == "INVALIDATE" and item["entityId"] == "POL0001"]
    assert invalidated[0]["payload"]["status"] == "已失效"


def test_changes_are_read_only_and_empty_result_keeps_since(client, settings):
    since = "2099-01-01T00:00:00Z"
    first = client.get("/api/kg/changes", params={"since": since}, headers=auth(settings))
    second = client.get("/api/kg/changes", params={"since": since}, headers=auth(settings))
    assert first.status_code == 200
    assert first.json() == {"changes": [], "nextSince": since}
    assert second.json() == first.json()


def test_crawl_registers_snapshot_document_segments_and_deduplicates(client, settings, monkeypatch):
    with client.app.state.db.engine.begin() as conn:
        client.app.state.db.update("CrsSource", "CRS0001", {"enabledFlag": "启用"}, conn, actor="tester")
    html = "<html><head><title>稻谷生产快讯</title></head><body><p>2024年稻谷产量保持稳定。</p><p>数据来源于公开统计。</p></body></html>"
    monkeypatch.setattr(client.app.state.crawler, "fetch", lambda url: (200, html))

    first = client.post(
        "/api/crawl/trigger", json={"crawlSourceId": "CRS0001"}, headers=auth(settings, "ROLE-01")
    )
    assert first.status_code == 200
    assert first.json()["newDocCount"] == 1
    document = client.app.state.db.fetch_one(
        'SELECT * FROM "SourceDoc" WHERE "docId"=:id', {"id": first.json()["docId"]}
    )
    assert document["title"] == "稻谷生产快讯"
    assert document["checksum"]
    assert document["storageUri"]
    segments = client.app.state.db.fetch_all(
        'SELECT * FROM "SourceDoc_Segment" WHERE "docId"=:id ORDER BY "segmentNo"',
        {"id": document["docId"]},
    )
    assert [row["segmentText"] for row in segments] == ["2024年稻谷产量保持稳定。", "数据来源于公开统计。"]
    snapshot = settings.data_dir / "snapshots" / f"{document['checksum']}.html"
    assert snapshot.read_text(encoding="utf-8") == html

    second = client.post(
        "/api/crawl/trigger", json={"crawlSourceId": "CRS0001"}, headers=auth(settings, "ROLE-01")
    )
    assert second.status_code == 200
    assert second.json()["newDocCount"] == 0
    assert second.json()["duplicateCount"] == 1
    assert client.app.state.db.fetch_one('SELECT COUNT(*) AS count FROM "SourceDoc"')["count"] == 5


def test_scheduler_triggers_due_source_once_and_honors_minimum_interval(client, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.scheduler import CrawlScheduler

    with client.app.state.db.engine.begin() as conn:
        client.app.state.db.update(
            "CrsSource", "CRS0001", {"enabledFlag": "启用"}, conn, actor="tester"
        )
    calls = []
    monkeypatch.setattr(
        client.app.state.crawler,
        "trigger",
        lambda source_id, actor: calls.append((source_id, actor)) or {"newDocCount": 0},
    )
    scheduler = CrawlScheduler(client.app.state.crawler)
    due = datetime(2026, 11, 13, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert len(scheduler.run_due(due)) == 1
    assert scheduler.run_due(due) == []
    assert scheduler.run_due(due.replace(minute=1)) == []
    assert calls == [("CRS0001", "scheduler")]

    with client.app.state.db.engine.begin() as conn:
        client.app.state.db.update(
            "CrsSource", "CRS0001", {"lastRunAt": "2026-11-12T13:00:00Z"}, conn, actor="tester"
        )
    assert CrawlScheduler(client.app.state.crawler).run_due(due) == []
    assert calls == [("CRS0001", "scheduler")]


def test_crawl_security_rejects_loopback_before_network_access():
    from app.crawler import validate_crawl_url, CrawlSecurityError

    with pytest.raises(CrawlSecurityError):
        validate_crawl_url("http://127.0.0.1/internal")


def test_crawl_trigger_enforces_rbac_and_paused_sources(client, settings):
    forbidden = client.post(
        "/api/crawl/trigger", json={"crawlSourceId": "CRS0001", "mode": "now"}, headers=auth(settings, "ROLE-04")
    )
    assert forbidden.status_code == 403
    paused = client.post(
        "/api/crawl/trigger", json={"crawlSourceId": "CRS0001", "mode": "now"}, headers=auth(settings, "ROLE-01")
    )
    assert paused.status_code == 409


def test_static_business_graph_application_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "粮油产业多维知识图谱" in response.text
    assert "graph-canvas" in response.text


def test_dashboard_stats_use_real_business_records(client, settings):
    response = client.get("/api/kg/stats", headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["entities"] >= 20
    assert body["relations"] >= 3
    assert body["documents"] == 4
    assert body["categories"] == 280


def test_ontology_dimensions_are_served_from_kernel(client, settings):
    response = client.get("/api/ontology/dimensions", headers=auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 10
    codes = [item["dimensionCode"] for item in body["items"]]
    assert codes[0] == "COMMODITY" and codes[-1] == "TRADE_FLOW"


def test_scope_api_is_idempotent_and_role_guarded(client, settings):
    payload = {
        "scopeType": "POLICY_SCOPE",
        "members": [{"dimensionCode": "COMMODITY", "objectType": "Commodity", "objectId": "CMD0003"}],
        "scopeCode": "POL-CORN-API",
    }
    assert client.post("/api/ontology/scopes", json=payload, headers=auth(settings, "ROLE-04")).status_code == 403
    first = client.post("/api/ontology/scopes", json=payload, headers=auth(settings, "ROLE-02"))
    assert first.status_code == 201
    assert first.json()["created"] is True
    again = client.post("/api/ontology/scopes", json=payload, headers=auth(settings, "ROLE-02"))
    assert again.status_code == 201
    assert again.json()["created"] is False
    assert again.json()["scopeId"] == first.json()["scopeId"]
    listed = client.get("/api/ontology/scopes", params={"scopeType": "POLICY_SCOPE"}, headers=auth(settings))
    assert listed.json()["total"] == 1
    assert client.get(f"/api/ontology/scopes/{first.json()['scopeId']}", headers=auth(settings)).status_code == 200
    bad = client.post(
        "/api/ontology/scopes",
        json={**payload, "members": [{"dimensionCode": "COMMODITY", "objectType": "Region", "objectId": "REG0001"}]},
        headers=auth(settings, "ROLE-02"),
    )
    assert bad.status_code == 400
