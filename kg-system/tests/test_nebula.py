import os

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.config import Settings
from app.db import Database
from app.governance import GovernanceService
from app.main import create_app
from app.nebula import NebulaGraphStore
from app.service import KnowledgeService


class FakeResult:
    def __init__(self, rows=None, success=True):
        self._rows = rows or []
        self._success = success

    def is_succeeded(self):
        return self._success

    def error_msg(self):
        return "fake nebula error"

    def row_size(self):
        return len(self._rows)

    def row_values(self, index):
        return self._rows[index]


class Primitive:
    def __init__(self, value):
        self.value = value

    def as_primitive(self):
        return self.value


class FakeSession:
    def __init__(self, results=None):
        self.statements = []
        self.results = list(results or [])

    def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()

    def release(self):
        pass


@pytest.fixture()
def seeded_db(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'nebula-test.db'}", Settings().seed_dir)
    db.initialize()
    db.seed()
    yield db
    db.close()


def test_nebula_publish_excludes_unpublished_business_relation_edges(seeded_db):
    service = KnowledgeService(seeded_db)
    created = service.create_knowledge("知识关系", {
        "subjectEntityType": "品种", "subjectEntityId": "CMD0001", "predicate": "草稿关系",
        "objectEntityType": "地区", "objectEntityId": "REG0001", "confidence": 0.9,
        "sourceDocId": "SOU0002", "segmentId": "SOU0002-S002",
    }, "tester")
    session = FakeSession()
    store = NebulaGraphStore(seeded_db, Settings(), session=session)

    store.publish_all()

    statements = session.statements
    assert any("INSERT VERTEX knowledge_category" in statement for statement in statements)
    assert any("INSERT VERTEX indval" in statement for statement in statements)
    assert any("INSERT VERTEX relation_fact" in statement for statement in statements)
    assert not any("INSERT VERTEX kg_relation" in statement for statement in statements)
    relation_edges = "\n".join(statement for statement in statements if "INSERT EDGE kg_relation" in statement)
    assert "REL0001" in relation_edges
    assert created["entityId"] not in relation_edges
    indicator_edges = "\n".join(statement for statement in statements if "INSERT EDGE of_indicator" in statement)
    assert '"YRB0001"->"IND0002": ("2024")' in indicator_edges


def test_nebula_initialize_applies_configured_replica_factor(seeded_db):
    session = FakeSession()
    store = NebulaGraphStore(
        seeded_db,
        Settings(nebula_space="replica_test", nebula_replica_factor=3),
        session=session,
    )

    store.initialize()

    assert any(
        "CREATE SPACE IF NOT EXISTS replica_test" in statement
        and "replica_factor = 3" in statement
        for statement in session.statements
    )


def test_nebula_expand_executes_graph_match_and_normalizes_edges(seeded_db):
    row = [
        Primitive("CMD0001"), Primitive("REG0001"), Primitive("kg_relation"),
        Primitive({"predicate": "产于", "confidence": 0.99, "relationId": "REL0001"}),
    ]
    session = FakeSession([FakeResult([row])])
    store = NebulaGraphStore(seeded_db, Settings(), session=session)

    edges = store.expand_ids("CMD0001", depth=1)

    assert edges == [{
        "source": "CMD0001", "target": "REG0001", "edgeType": "kg_relation",
        "properties": {"predicate": "产于", "confidence": 0.99, "relationId": "REL0001"},
    }]
    assert "MATCH" in session.statements[0]
    assert 'id(v) == "CMD0001"' in session.statements[0]


def test_app_uses_nebula_for_lifecycle_publishing_and_graph_expand(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'nebula-app.db'}",
        jwt_secret="integration-test-secret-with-at-least-32-bytes",
        graph_backend="nebula",
    )
    events = []

    class Store:
        def __init__(self, db, settings):
            events.append("construct")

        def initialize(self):
            events.append("initialize")

        def publish_all(self):
            events.append("publish_all")

        def publish_entity(self, entity_type, entity_id):
            events.append(("publish_entity", entity_type, entity_id))

        def expand_ids(self, entity_id, depth):
            events.append(("expand", entity_id, depth))
            return [{
                "source": "CMD0001", "target": "REG0001", "edgeType": "kg_relation",
                "properties": {"predicate": "产于", "confidence": 0.99, "relationId": "REL0001"},
            }]

        def close(self):
            events.append("close")

    monkeypatch.setattr("app.main.NebulaGraphStore", Store)
    headers = {"Authorization": f"Bearer {issue_token('tester', 'ROLE-01', settings.jwt_secret)}"}
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/kg/graph/expand",
            params={"entityType": "品种", "entityId": "CMD0001", "depth": 1},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["edges"][0]["predicate"] == "产于"
        assert {node["id"] for node in response.json()["nodes"]} == {"CMD0001", "REG0001"}
        created = client.post("/api/knowledge/知识关系", headers=headers, json={
            "subjectEntityType": "品种", "subjectEntityId": "CMD0001", "predicate": "新关系",
            "objectEntityType": "地区", "objectEntityId": "REG0001", "confidence": 0.9,
            "sourceDocId": "SOU0002", "segmentId": "SOU0002-S002", "status": "已发布",
        })
        assert created.status_code == 201
        assert ("publish_entity", "知识关系", created.json()["entityId"]) in events

    assert events[:3] == ["construct", "initialize", "publish_all"]
    assert ("expand", "CMD0001", 1) in events
    assert events[-1] == "close"


def test_nebula_unpublishing_removes_relation_edges_and_document_children(seeded_db):
    with seeded_db.engine.begin() as conn:
        seeded_db.update("Relation", "REL0001", {"status": "已失效"}, conn, actor="tester")
        seeded_db.update("SourceDoc", "SOU0001", {"status": "已作废"}, conn, actor="tester")
    session = FakeSession()
    store = NebulaGraphStore(seeded_db, Settings(), session=session)

    store.publish_entity("知识关系", "REL0001")
    store.publish_entity("来源文档", "SOU0001")

    statements = session.statements
    assert 'DELETE EDGE kg_relation "CMD0001"->"REG0001"' in statements
    assert 'DELETE VERTEX "REL0001" WITH EDGE' in statements
    assert 'DELETE VERTEX "SOU0001-S001" WITH EDGE' in statements
    assert 'DELETE VERTEX "SOU0001" WITH EDGE' in statements


def test_lifecycle_operations_sync_nebula_after_commits(seeded_db):
    class Store:
        def __init__(self):
            self.published = []

        def publish_entity(self, entity_type, entity_id):
            self.published.append((entity_type, entity_id))

    store = Store()
    knowledge = KnowledgeService(seeded_db, store)
    governance = GovernanceService(seeded_db, knowledge)
    source = knowledge.register_source({
        "title": "图同步测试文档", "docType": "农业经济", "issuer": "测试机构",
        "segments": [{"segmentText": "用于验证文档与段落入图。"}],
    }, "tester")
    duplicate = knowledge.create_master("品种", {
        "commodityCode": "CMD-SYNC-DUP", "commodityName": "待融合稻谷",
        "commodityCategory": "谷物", "status": "有效",
    }, "tester")["entityId"]
    relation = knowledge.create_knowledge("知识关系", {
        "subjectEntityType": "品种", "subjectEntityId": duplicate, "predicate": "产于",
        "objectEntityType": "地区", "objectEntityId": "REG0001", "confidence": 0.9,
        "sourceDocId": "SOU0002", "segmentId": "SOU0002-S002", "status": "已发布",
    }, "tester")["entityId"]

    governance.merge("品种", duplicate, "CMD0001", 0.99, "tester")
    governance.void_source("SOU0001", "tester")

    assert ("来源文档", source["docId"]) in store.published
    assert ("品种", duplicate) in store.published
    assert ("品种", "CMD0001") in store.published
    assert ("知识关系", relation) in store.published
    assert ("来源文档", "SOU0001") in store.published
    assert ("政策法规", "POL0001") in store.published


@pytest.mark.skipif(os.getenv("KG_TEST_NEBULA") != "1", reason="需要可用的 NebulaGraph 集成环境")
def test_nebula_real_round_trip(seeded_db):
    settings = Settings.from_env()
    store = NebulaGraphStore(seeded_db, settings)
    try:
        store.initialize()
        store.publish_all()
        edges = store.expand_ids("CMD0001", depth=1)
        assert any(edge["properties"].get("predicate") == "产于" for edge in edges)

        knowledge = KnowledgeService(seeded_db, store)
        governance = GovernanceService(seeded_db, knowledge)
        duplicate = knowledge.create_master("品种", {
            "commodityCode": "CMD-REAL-DUP", "commodityName": "真实集成待融合稻谷",
            "commodityCategory": "谷物", "status": "有效",
        }, "tester")["entityId"]
        relation = knowledge.create_knowledge("知识关系", {
            "subjectEntityType": "品种", "subjectEntityId": duplicate, "predicate": "集成审核关系",
            "objectEntityType": "地区", "objectEntityId": "REG0003", "confidence": 0.9,
            "sourceDocId": "SOU0002", "segmentId": "SOU0002-S002",
        }, "tester")["entityId"]
        assert not any(
            edge["properties"].get("relationId") == relation
            for edge in store.expand_ids(duplicate, depth=1)
        )

        knowledge.submit("知识关系", relation, "tester")
        knowledge.review("知识关系", relation, "通过", "reviewer")
        assert any(
            edge["properties"].get("relationId") == relation
            for edge in store.expand_ids(duplicate, depth=1)
        )

        governance.merge("品种", duplicate, "CMD0001", 0.99, "tester")
        assert store.expand_ids(duplicate, depth=1) == []
        assert any(
            edge["properties"].get("relationId") == relation
            for edge in store.expand_ids("CMD0001", depth=1)
        )

        governance.void_source("SOU0001", "tester")
        assert store.expand_ids("SOU0001", depth=1) == []
        assert store.expand_ids("SOU0001-S001", depth=1) == []
        assert store.expand_ids("POL0001", depth=1) == []
    finally:
        store.close()
