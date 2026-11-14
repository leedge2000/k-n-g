# -*- coding: utf-8 -*-
"""阶段 1 硬要求：Scope + Dimension 权威注册表与 Kùzu 投影的回归测试。"""

import importlib.util
from pathlib import Path

import pytest

from app.db import Database
from app.scope import ScopeError, ScopeService, normalize_members, normalized_key

BASE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def scope_service(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'scope.db'}", BASE_DIR / "seed")
    service = ScopeService(db)
    service.initialize()
    yield service
    db.close()


def _members(*items):
    return [
        {"dimensionCode": dimension, "objectType": object_type, "objectId": object_id}
        for dimension, object_type, object_id in items
    ]


def test_dimensions_are_seeded_from_kernel(scope_service):
    items = scope_service.dimensions()
    assert len(items) == 10
    assert [item["dimensionCode"] for item in items] == [
        "COMMODITY", "GEOGRAPHY", "ACTOR", "CHANNEL", "TIME_WINDOW",
        "UNIT_BASIS", "POLICY_TOOL", "TAX_BASIS", "QUALITY_GRADE", "TRADE_FLOW",
    ]
    tax_basis = next(item for item in items if item["dimensionCode"] == "TAX_BASIS")
    assert tax_basis["inlineEnumRef"] == "TaxBasis"
    assert tax_basis["valueObjectType"] == []
    grade = next(item for item in items if item["dimensionCode"] == "QUALITY_GRADE")
    assert grade["inlineAttribute"] == "CommodityForm.grade"
    assert scope_service.stats() == {"dimensionCount": 10, "scopeCount": 0, "expectedDimensionCount": 10}


def test_repeated_initialize_does_not_duplicate_dimensions(scope_service):
    scope_service.initialize()
    scope_service.initialize()
    assert scope_service.stats()["dimensionCount"] == 10


def test_members_are_sorted_into_one_normalized_key():
    ordered = normalize_members(_members(("COMMODITY", "Commodity", "CMD0003"), ("GEOGRAPHY", "Region", "REG0001")))
    reversed_input = normalize_members(_members(("GEOGRAPHY", "Region", "REG0001"), ("COMMODITY", "Commodity", "CMD0003")))
    assert ordered == reversed_input
    assert normalized_key("POLICY_SCOPE", ordered) == normalized_key("POLICY_SCOPE", reversed_input)
    assert normalized_key("POLICY_SCOPE", ordered) != normalized_key("SERIES_SCOPE", ordered)


def test_upsert_scope_dedupes_by_normalized_key(scope_service):
    first = scope_service.upsert_scope(
        "POLICY_SCOPE",
        _members(("COMMODITY", "Commodity", "CMD0003"), ("GEOGRAPHY", "Region", "REG0001")),
        "POL-MINPRICE-CORN-NATIONAL",
    )
    assert first["created"] is True
    assert first["scopeId"].startswith("SCP-PUB-")
    assert [member["dimensionCode"] for member in first["members"]] == ["COMMODITY", "GEOGRAPHY"]
    again = scope_service.upsert_scope(
        "POLICY_SCOPE",
        _members(("GEOGRAPHY", "Region", "REG0001"), ("COMMODITY", "Commodity", "CMD0003")),
        "POL-MINPRICE-CORN-NATIONAL",
    )
    assert again["created"] is False
    assert again["scopeId"] == first["scopeId"]
    assert scope_service.stats()["scopeCount"] == 1


def test_member_object_type_outside_whitelist_is_rejected(scope_service):
    with pytest.raises(ScopeError, match="不接受对象类型"):
        scope_service.upsert_scope(
            "SERIES_SCOPE", _members(("COMMODITY", "Region", "REG0001")), "SER-BAD-DIM"
        )


def test_inline_enum_dimension_validates_member_value(scope_service):
    accepted = scope_service.upsert_scope(
        "ASSERTION_SCOPE", _members(("TAX_BASIS", "TaxBasis", "INCLUSIVE")), "ASR-TAX-INCLUSIVE"
    )
    assert accepted["created"] is True
    with pytest.raises(ScopeError, match="不合法"):
        scope_service.upsert_scope(
            "ASSERTION_SCOPE", _members(("TAX_BASIS", "TaxBasis", "UNKNOWN")), "ASR-TAX-UNKNOWN"
        )
    with pytest.raises(ScopeError, match="成员类型必须是 TaxBasis"):
        scope_service.upsert_scope(
            "ASSERTION_SCOPE", _members(("TAX_BASIS", "Commodity", "INCLUSIVE")), "ASR-TAX-WRONG"
        )


def test_single_valued_dimension_rejects_second_member(scope_service):
    with pytest.raises(ScopeError, match="单值维度"):
        scope_service.upsert_scope(
            "SERIES_SCOPE",
            _members(
                ("TIME_WINDOW", "TimePeriod", "2024"),
                ("TIME_WINDOW", "TimePeriod", "2025"),
            ),
            "SER-TWO-WINDOWS",
        )


def test_scope_code_conflict_is_rejected(scope_service):
    scope_service.upsert_scope(
        "SERIES_SCOPE", _members(("GEOGRAPHY", "Region", "REG0001")), "SER-CODE-CLASH"
    )
    with pytest.raises(ScopeError, match="已被"):
        scope_service.upsert_scope(
            "SERIES_SCOPE", _members(("GEOGRAPHY", "Region", "REG0002")), "SER-CODE-CLASH"
        )


def test_is_superset_follows_declared_chain(scope_service):
    national = scope_service.upsert_scope(
        "POLICY_SCOPE", _members(("COMMODITY", "Commodity", "CMD0003")), "POL-CORN-NATIONAL",
        is_open=True,
    )
    henan = scope_service.upsert_scope(
        "SERIES_SCOPE",
        _members(("COMMODITY", "Commodity", "CMD0003"), ("GEOGRAPHY", "Region", "REG0001")),
        "SER-CORN-HENAN",
        superset_of=[national["scopeId"]],
    )
    county = scope_service.upsert_scope(
        "SERIES_SCOPE",
        _members(("COMMODITY", "Commodity", "CMD0003"), ("GEOGRAPHY", "Region", "REG0002")),
        "SER-CORN-HENAN-COUNTY",
        superset_of=[henan["scopeId"]],
    )
    assert henan["supersetOf"] == [national["scopeId"]]
    assert scope_service.is_superset(national["scopeId"], henan["scopeId"]) is True
    assert scope_service.is_superset(national["scopeId"], county["scopeId"]) is True
    assert scope_service.is_superset(county["scopeId"], national["scopeId"]) is False


def _load_builder():
    path = BASE_DIR / "ontology_ly" / "tools" / "build_kuzu.py"
    spec = importlib.util.spec_from_file_location("build_kuzu", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kuzu_projection_matches_sqlite(tmp_path, scope_service):
    pytest.importorskip("kuzu")
    builder = _load_builder()
    kernel = builder.load_kernel()
    scope_service.upsert_scope(
        "POLICY_SCOPE",
        _members(("COMMODITY", "Commodity", "CMD0003")),
        "POL-CORN-PROJECTION",
    )
    sqlite_path = Path(scope_service.db.engine.url.database)
    database, connection = builder.connect(tmp_path / "ly.kuzu")
    builder.ensure_schema(connection, kernel)
    assert builder.seed_dimensions(connection, kernel) == 10
    scopes = builder.read_scopes(sqlite_path)
    assert len(scopes) == 1
    assert builder.project_scopes(connection, scopes) == 1
    assert builder.count_rows(connection, "Dimension") == 10
    assert builder.count_rows(connection, "Scope") == 1
    database.close()
