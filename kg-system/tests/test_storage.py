import os

import pytest

from app.storage import ElasticsearchSegmentIndex, MinioSnapshotStore


@pytest.mark.skipif(
    os.getenv("KG_TEST_ELASTICSEARCH") != "1",
    reason="需要可用的 Elasticsearch 集成环境",
)
def test_elasticsearch_segment_and_vector_index_round_trip():
    index = ElasticsearchSegmentIndex(
        os.getenv("KG_ELASTICSEARCH_URL", "http://127.0.0.1:9200"),
        "grain-oil-segments-test",
    )
    try:
        index.index_document(
            {"docId": "DOC-TEST", "title": "稻谷生产快讯"},
            [{"segmentId": "DOC-TEST-S001", "segmentNo": 1, "segmentText": "2024年稻谷产量保持稳定。"}],
        )
        result = index.client.search(
            index="grain-oil-segments-test",
            query={"term": {"docId": "DOC-TEST"}},
            size=1,
        )
        assert result["hits"]["total"]["value"] == 1
        assert result["hits"]["hits"][0]["_source"]["segmentText"] == "2024年稻谷产量保持稳定。"
        vector_result = index.client.search(
            index="grain-oil-segments-test",
            knn={
                "field": "embedding",
                "query_vector": index.embedding("稻谷产量稳定"),
                "k": 1,
                "num_candidates": 10,
            },
            size=1,
        )
        assert vector_result["hits"]["hits"][0]["_id"] == "DOC-TEST-S001"
    finally:
        index.client.indices.delete(index="grain-oil-segments-test", ignore_unavailable=True)
        index.close()


@pytest.mark.skipif(os.getenv("KG_TEST_MINIO") != "1", reason="需要可用的 MinIO 集成环境")
def test_minio_snapshot_round_trip():
    store = MinioSnapshotStore(
        os.getenv("KG_MINIO_ENDPOINT", "127.0.0.1:19000"),
        os.getenv("KG_MINIO_ACCESS_KEY", "grainoil"),
        os.getenv("KG_MINIO_SECRET_KEY", "grainoil-secret"),
        "grain-oil-test",
    )
    checksum = "integration-snapshot"
    object_name = f"snapshots/{checksum}.html"
    try:
        uri = store.put(checksum, "<html><body>稻谷生产快照</body></html>")
        response = store.client.get_object(store.bucket, object_name)
        try:
            assert response.read().decode("utf-8") == "<html><body>稻谷生产快照</body></html>"
        finally:
            response.close()
            response.release_conn()
        assert uri == "s3://grain-oil-test/snapshots/integration-snapshot.html"
    finally:
        store.client.remove_object(store.bucket, object_name)
