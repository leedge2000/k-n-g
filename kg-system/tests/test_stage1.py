# -*- coding: utf-8 -*-
"""阶段 1：解析分块、混合检索、范围上下文与引用回溯回归。"""

import io

import pymupdf as fitz
import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import issue_token
from app.config import Settings
from app.documents import DocumentIngestor
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'kg-stage1-test.db'}",
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
    return {"Authorization": f"Bearer {issue_token('stage1-test', role, settings.jwt_secret)}"}


def _ingestor():
    return object.__new__(DocumentIngestor)


def test_chunk_text_handles_empty_crlf_and_length_boundaries():
    ingestor = _ingestor()
    assert ingestor._chunk_text(" \r\n ") == []
    assert ingestor._chunk_text("甲\r\n\r\n乙") == ["甲\n\n乙"]

    exact = "粮" * 700
    assert ingestor._chunk_text(exact) == [exact]
    assert [len(item) for item in ingestor._chunk_text(exact + "油")] == [700, 1]
    assert [len(item) for item in ingestor._chunk_text("谷" * 1500)] == [700, 700, 100]

    chunks = ingestor._chunk_text("甲" * 690 + "\n\n" + "乙" * 20)
    assert [len(item) for item in chunks] == [690, 20]
    assert all(len(item) <= 700 for item in chunks)


def test_markdown_split_keeps_each_chapter_with_its_own_content():
    segments = _ingestor()._split_text(
        "导言。\n\n# 第一章\n第一章正文。\n\n## 第二节\n第二节正文。"
    )
    assert [item["chapterPath"] for item in segments] == [None, "第一章", "第二节"]
    assert "导言" in segments[0]["segmentText"]
    assert "第一章正文" in segments[1]["segmentText"]
    assert "第二节正文" in segments[2]["segmentText"]
    assert _ingestor()._split_text("无标题正文。")[0]["chapterPath"] is None


def test_docx_extraction_keeps_heading_paths():
    document = Document()
    document.add_heading("第一章 总则", level=1)
    document.add_paragraph("玉米最低收购价政策正文。")
    document.add_heading("第二章 附则", level=1)
    document.add_paragraph("监督检查要求正文。")
    content = io.BytesIO()
    document.save(content)

    segments, _ = _ingestor()._extract_docx(content.getvalue())
    assert [item["chapterPath"] for item in segments] == ["第一章 总则", "第二章 附则"]
    assert segments[0]["segmentText"] == "玉米最低收购价政策正文。"
    assert segments[1]["segmentText"] == "监督检查要求正文。"


def test_pdf_extraction_keeps_page_numbers():
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "Page one: grain output.")
    second = document.new_page()
    second.insert_text((72, 72), "Page two: rice stocks.")
    content = document.tobytes()
    document.close()

    segments, page_count = _ingestor()._extract_pdf(content)
    assert page_count == 2
    assert [item["pageNo"] for item in segments] == [1, 2]
    assert "grain output" in segments[0]["segmentText"]
    assert "rice stocks" in segments[1]["segmentText"]


def test_rag_supports_five_modes_and_invalid_mode_fallback(client):
    rag = client.app.state.rag
    for mode in rag.SUPPORTED_MODES:
        result = rag.search("玉米产量是多少？", mode=mode, top_k=5, answer=False)
        assert result["mode"] == mode
        assert result["items"]
        assert all(item["segmentId"] for item in result["items"])
    fallback = rag.search("玉米产量是多少？", mode="not-a-mode", top_k=5, answer=False)
    assert fallback["mode"] == "hybrid"


def test_query_only_alias_resolves_to_canonical_entity(client):
    result = client.app.state.rag.search("苞米产量是多少？", mode="local", top_k=5, answer=False)
    matched = {
        (item.get("name"), item.get("entityId"), item.get("ontologyId"))
        for item in result["matchedEntities"]
    }
    assert ("玉米", "CMD0003", "OBJ-PUB-CM-CORN") in matched
    assert any(item["segmentId"] == "SOU0002-S002" and item["localScore"] > 0 for item in result["items"])


def test_rag_top_k_is_clamped_and_empty_query_is_rejected(client, monkeypatch):
    rag = client.app.state.rag
    seen = []
    original = rag._rerank

    def capture_top_k(text_value, keywords, ranked, top_k):
        seen.append(top_k)
        return original(text_value, keywords, ranked, top_k)

    monkeypatch.setattr(rag, "_rerank", capture_top_k)
    rag.search("玉米产量", top_k=0, answer=False)
    rag.search("玉米产量", top_k=-3, answer=False)
    rag.search("玉米产量", top_k=51, answer=False)
    assert seen == [1, 1, 50]
    with pytest.raises(ValueError, match="不能为空"):
        rag.search("  ", answer=False)


def test_hash_ranking_is_repeatable_and_void_documents_are_excluded(client):
    rag = client.app.state.rag
    first = rag.search("China rice production", top_k=8, answer=False)
    second = rag.search("China rice production", top_k=8, answer=False)
    assert [item["segmentId"] for item in first["items"]] == [
        item["segmentId"] for item in second["items"]
    ]

    with client.app.state.db.engine.begin() as connection:
        connection.execute(
            text('UPDATE "SourceDoc" SET "status"=:status WHERE "docId"=:doc_id'),
            {"status": "作废", "doc_id": "SOU0004"},
        )
    rag.refresh(force=True)
    assert all(row["docId"] != "SOU0004" for row in rag._rows())
    result = rag.search("China rice production", top_k=10, answer=False)
    assert all(item["docId"] != "SOU0004" for item in result["items"])


def test_rag_api_validates_empty_query_and_top_k_boundaries(client, settings):
    headers = auth(settings)
    empty = client.post("/api/rag/query", json={"query": " "}, headers=headers)
    assert empty.status_code == 422

    for top_k in (0, -3):
        response = client.post(
            "/api/rag/query", json={"query": "玉米产量", "topK": top_k}, headers=headers
        )
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1

    large = client.post(
        "/api/rag/query", json={"query": "玉米产量", "topK": 51}, headers=headers
    )
    assert large.status_code == 200
    assert len(large.json()["items"]) <= 50
    invalid = client.post(
        "/api/rag/query", json={"query": "玉米产量", "topK": "bad"}, headers=headers
    )
    assert invalid.status_code == 422


def test_rag_api_echoes_policy_context_and_rejects_invalid_scope(client, settings):
    scope = client.app.state.scope.upsert_scope(
        "POLICY_SCOPE",
        [{"dimensionCode": "COMMODITY", "objectType": "Commodity", "objectId": "CMD0001"}],
        "STAGE1-RICE-SCOPE",
    )
    headers = auth(settings)
    response = client.post(
        "/api/rag/query",
        json={"query": "稻谷最低收购价", "asOf": "2025-06-01", "scopeId": scope["scopeId"]},
        headers=headers,
    )
    assert response.status_code == 200
    context = response.json()["policyContext"]
    assert context["asOf"] == "2025-06-01"
    assert context["scopeId"] == scope["scopeId"]

    bad_date = client.post(
        "/api/rag/query", json={"query": "稻谷", "asOf": "2025-13-40"}, headers=headers
    )
    assert bad_date.status_code == 422
    bad_scope = client.post(
        "/api/rag/query", json={"query": "稻谷", "scopeId": "SCP-NOT-FOUND"}, headers=headers
    )
    assert bad_scope.status_code == 422


def test_extractive_answer_citations_are_traceable_to_source_segments(client, settings):
    response = client.post(
        "/api/rag/query",
        json={"query": "2024年稻谷、小麦、玉米产量分别是多少？", "topK": 5, "answer": True},
        headers=auth(settings),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answerMode"] == "extractive"
    assert body["citations"]

    for index, citation in enumerate(body["citations"], 1):
        item = body["items"][index - 1]
        assert f"【{index}】" in body["answer"]
        assert citation["segmentId"] == item["segmentId"]
        source = client.app.state.db.fetch_one(
            'SELECT s."segmentText" AS segmentText, s."pageNo" AS pageNo, '
            's."chapterPath" AS chapterPath, s."segmentNo" AS segmentNo '
            'FROM "SourceDoc_Segment" s WHERE s."segmentId"=:segment_id',
            {"segment_id": citation["segmentId"]},
        )
        assert source is not None
        assert citation["quote"] == source["segmentText"][:200]
        assert citation["quote"] in source["segmentText"]
        assert citation["pageNo"] == source["pageNo"]
        assert citation["chapterPath"] == source["chapterPath"]
        assert citation["segmentNo"] == source["segmentNo"]
        assert source["segmentText"][:220] in body["answer"]
