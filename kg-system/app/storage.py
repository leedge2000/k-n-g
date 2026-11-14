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
        return self.put_bytes(
            checksum, content.encode("utf-8"), ".html", "text/html; charset=utf-8"
        )

    def put_bytes(self, checksum: str, content: bytes, extension: str,
                  media_type: str = "application/octet-stream") -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{checksum}{extension}"
        path.write_bytes(content)
        return path.as_uri()

    def get(self, checksum: str) -> bytes | None:
        return self.get_asset(checksum, ".html")

    def get_asset(self, checksum: str, extension: str) -> bytes | None:
        path = self.directory / f"{checksum}{extension}"
        return path.read_bytes() if path.is_file() else None


class MinioSnapshotStore:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str):
        from minio import Minio

        self.bucket = bucket
        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put(self, checksum: str, content: str) -> str:
        return self.put_bytes(
            checksum, content.encode("utf-8"), ".html", "text/html; charset=utf-8"
        )

    def put_bytes(self, checksum: str, content: bytes, extension: str,
                  media_type: str = "application/octet-stream") -> str:
        object_name = f"snapshots/{checksum}{extension}"
        self.client.put_object(
            self.bucket, object_name, BytesIO(content), len(content), content_type=media_type
        )
        return f"s3://{self.bucket}/{object_name}"

    def get(self, checksum: str) -> bytes | None:
        return self.get_asset(checksum, ".html")

    def get_asset(self, checksum: str, extension: str) -> bytes | None:
        from minio.error import S3Error

        response = None
        try:
            response = self.client.get_object(
                self.bucket, f"snapshots/{checksum}{extension}"
            )
            return response.read()
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return None
            raise
        finally:
            if response is not None:
                response.close()
                response.release_conn()


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
