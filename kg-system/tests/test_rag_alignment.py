# -*- coding: utf-8 -*-
"""阶段 1 检索链路收尾：本体别名合并与实体身份透出的回归测试。"""

import json

import app.rag as rag_module
from app.rag import EntityGraph, RagService


def _service_without_index():
    # _merge_ontology_aliases 只读模块常量，不依赖实例状态与数据库
    return object.__new__(RagService)


def test_merge_ontology_aliases_keeps_app_identity_priority():
    dictionary = {"玉米": {"type": "品种", "entityId": "CMD0003"}}
    _service_without_index()._merge_ontology_aliases(dictionary)
    assert dictionary["玉米"]["entityId"] == "CMD0003"
    assert dictionary["玉米"]["type"] == "品种"
    assert dictionary["玉米"]["ontologyId"] == "OBJ-PUB-CM-CORN"
    assert dictionary["玉米"]["alignmentStatus"] == "ALIGNED_APP"


def test_merge_ontology_aliases_skips_mismatched_app_record():
    dictionary = {"玉米": {"type": "品种", "entityId": "CMD9999"}}
    _service_without_index()._merge_ontology_aliases(dictionary)
    assert dictionary["玉米"]["entityId"] == "CMD9999"
    assert "ontologyId" not in dictionary["玉米"]


def test_merge_ontology_aliases_exposes_ontology_identity():
    dictionary = {}
    _service_without_index()._merge_ontology_aliases(dictionary)
    with_ontology = [info for info in dictionary.values() if info.get("ontologyId")]
    assert with_ontology
    for info in with_ontology:
        assert info["ontologyType"]
        assert info["alignmentStatus"]
        assert info["type"] and info["entityId"]


def test_entity_rows_expose_ontology_identity():
    graph = EntityGraph()
    dictionary = {
        "玉米": {
            "type": "品种",
            "entityId": "CMD0003",
            "ontologyId": "OBJ-PUB-CM-CORN",
            "ontologyType": "Commodity",
            "alignmentStatus": "ALIGNED_APP",
        }
    }
    graph.build(
        [
            {
                "segmentId": "SEG-1",
                "docId": "DOC-1",
                "segmentText": "玉米收购价格小幅上调。",
            }
        ],
        dictionary,
    )
    rows = graph.entity_rows(["玉米"])
    assert rows and rows[0]["ontologyId"] == "OBJ-PUB-CM-CORN"
    assert rows[0]["ontologyType"] == "Commodity"
    assert rows[0]["alignmentStatus"] == "ALIGNED_APP"


def _target(key, hint=None):
    return {
        "targetKey": key,
        "targetType": "Commodity",
        "targetIdHint": hint,
        "typeLabel": "品种",
    }


def _install_alignment_data(monkeypatch, tmp_path, entries, verdicts):
    (tmp_path / "alias_index.json").write_text(
        json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "concept_alignment.json").write_text(
        json.dumps({"targets": verdicts}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(rag_module, "ONTOLOGY_TERMS_DIR", tmp_path)


def test_merge_ontology_aliases_skips_whole_ambiguous_surface(monkeypatch, tmp_path):
    first = _target("term:Commodity:OBJ-A", "OBJ-A")
    second = _target("term:Commodity:OBJ-B", "OBJ-B")
    verdicts = [
        {"targetKey": first["targetKey"], "status": "ALIGNED_APP", "appType": "品种", "appId": "CMD-A"},
        {"targetKey": second["targetKey"], "status": "ALIGNED_APP", "appType": "品种", "appId": "CMD-B"},
    ]
    _install_alignment_data(
        monkeypatch, tmp_path, [{"surface": "歧义词", "targets": [first, second]}], verdicts
    )
    dictionary = {}
    _service_without_index()._merge_ontology_aliases(dictionary)
    assert "歧义词" not in dictionary


def test_merge_ontology_aliases_rejects_non_entity_or_unstable_verdicts(monkeypatch, tmp_path):
    cases = [
        ("结构词", "SCHEMA_ONLY", None),
        ("枚举词", "ALIGNED_ENUM", "GRAIN"),
        ("无身份", "NO_TARGET_ID", None),
        ("伪身份", "PSEUDO_ID", "grade=一级"),
    ]
    entries = []
    verdicts = []
    for surface, status, hint in cases:
        target = _target(f"term:Commodity:{surface}", hint)
        entries.append({"surface": surface, "targets": [target]})
        verdicts.append({"targetKey": target["targetKey"], "status": status})
    _install_alignment_data(monkeypatch, tmp_path, entries, verdicts)

    dictionary = {}
    _service_without_index()._merge_ontology_aliases(dictionary)
    assert dictionary == {}


def test_merge_ontology_aliases_keeps_stable_ontology_identity_without_app_record(monkeypatch, tmp_path):
    entries = []
    verdicts = []
    for surface, status, hint in (
        ("无应用表", "NO_APP_TABLE", "OBJ-NO-TABLE"),
        ("无应用记录", "NO_APP_RECORD", "OBJ-NO-RECORD"),
    ):
        target = _target(f"term:Commodity:{hint}", hint)
        entries.append({"surface": surface, "targets": [target]})
        verdicts.append({"targetKey": target["targetKey"], "status": status})
    _install_alignment_data(monkeypatch, tmp_path, entries, verdicts)

    dictionary = {"无应用表": {"type": "品种", "entityId": "CMD-KEEP"}}
    _service_without_index()._merge_ontology_aliases(dictionary)
    assert dictionary["无应用表"]["entityId"] == "CMD-KEEP"
    assert dictionary["无应用记录"] == {
        "type": "品种",
        "entityId": "OBJ-NO-RECORD",
        "ontologyId": "OBJ-NO-RECORD",
        "ontologyType": "Commodity",
        "alignmentStatus": "NO_APP_RECORD",
    }
