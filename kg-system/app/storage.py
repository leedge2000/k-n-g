from __future__ import annotations

import hashlib
import math
import re
from io import BytesIO
from pathlib import Path
from typing import Any


class LocalSnapshotStore:
    def __init__(self, data_dir: Path):
        self.directory = data_dir / "snapshots"

    def put(self, checksum: str, content: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{checksum}.html"
        path.write_text(content, encoding="utf-8")
        return path.as_uri()


class MinioSnapshotStore:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str):
        from minio import Minio

        self.bucket = bucket
        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put(self, checksum: str, content: str) -> str:
        data = content.encode("utf-8")
        object_name = f"snapshots/{checksum}.html"
        self.client.put_object(
            self.bucket, object_name, BytesIO(data), len(data), content_type="text/html; charset=utf-8"
        )
        return f"s3://{self.bucket}/{object_name}"


class ElasticsearchSegmentIndex:
    def __init__(self, url: str, index_name: str = "grain-oil-segments"):
        from elasticsearch import Elasticsearch

        self.client = Elasticsearch(url, request_timeout=30)
        self.index_name = index_name
        if not self.client.indices.exists(index=index_name):
            self.client.indices.create(index=index_name, mappings={
                "properties": {
                    "docId": {"type": "keyword"},
                    "segmentId": {"type": "keyword"},
                    "title": {"type": "text"},
                    "segmentText": {"type": "text"},
                    "embedding": {"type": "dense_vector", "dims": 128, "index": True, "similarity": "cosine"},
                }
            })

    @staticmethod
    def embedding(text: str) -> list[float]:
        vector = [0.0] * 128
        normalized = re.sub(r"\s+", "", text).casefold()
        features = list(normalized) + [normalized[index:index + 3] for index in range(max(0, len(normalized) - 2))]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % len(vector)
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    def index_document(self, document: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        from elasticsearch.helpers import bulk

        actions = [{
            "_op_type": "index",
            "_index": self.index_name,
            "_id": segment["segmentId"],
            "_source": {
                "docId": document["docId"],
                "segmentId": segment["segmentId"],
                "title": document["title"],
                "segmentText": segment["segmentText"],
                "embedding": self.embedding(segment["segmentText"]),
            },
        } for segment in segments]
        if actions:
            bulk(self.client, actions, refresh="wait_for")

    def close(self) -> None:
        self.client.close()
