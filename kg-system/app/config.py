from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'grain_oil.db').as_posix()}"
    jwt_secret: str = "grain-oil-local-development-secret"
    jwt_expire_minutes: int = 480
    auto_seed: bool = True
    graph_backend: str = "relational"
    nebula_hosts: str = "127.0.0.1:9669"
    nebula_user: str = "root"
    nebula_password: str = "nebula"
    nebula_space: str = "grain_oil_kg"
    nebula_replica_factor: int = 1
    elasticsearch_url: str | None = None
    minio_endpoint: str | None = None
    minio_access_key: str = "grainoil"
    minio_secret_key: str = "grainoil-secret"
    minio_bucket: str = "grain-oil-docs"
    scheduler_enabled: bool = False
    scheduler_timezone: str = "Asia/Shanghai"
    ops_inspect_interval_minutes: int = 30
    rag_embedding_backend: str = "hash"
    rag_embedding_base_url: str | None = None
    rag_embedding_api_key: str | None = None
    rag_embedding_model: str = "text-embedding-3-small"
    rag_embedding_batch_size: int = 16
    rag_llm_base_url: str | None = None
    rag_llm_api_key: str | None = None
    rag_llm_model: str = "gpt-4o-mini"
    cors_origins: str = "http://127.0.0.1:8000,http://localhost:8000"
    seed_dir: Path = BASE_DIR / "seed"
    web_dir: Path = BASE_DIR / "web"
    data_dir: Path = BASE_DIR / "data"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("KG_DATABASE_URL", cls.database_url),
            jwt_secret=os.getenv("KG_JWT_SECRET", cls.jwt_secret),
            jwt_expire_minutes=int(os.getenv("KG_JWT_EXPIRE_MINUTES", cls.jwt_expire_minutes)),
            auto_seed=_bool(os.getenv("KG_AUTO_SEED"), True),
            graph_backend=os.getenv("KG_GRAPH_BACKEND", cls.graph_backend),
            nebula_hosts=os.getenv("KG_NEBULA_HOSTS", cls.nebula_hosts),
            nebula_user=os.getenv("KG_NEBULA_USER", cls.nebula_user),
            nebula_password=os.getenv("KG_NEBULA_PASSWORD", cls.nebula_password),
            nebula_space=os.getenv("KG_NEBULA_SPACE", cls.nebula_space),
            nebula_replica_factor=int(os.getenv("KG_NEBULA_REPLICA_FACTOR", cls.nebula_replica_factor)),
            elasticsearch_url=os.getenv("KG_ELASTICSEARCH_URL") or None,
            minio_endpoint=os.getenv("KG_MINIO_ENDPOINT") or None,
            minio_access_key=os.getenv("KG_MINIO_ACCESS_KEY", cls.minio_access_key),
            minio_secret_key=os.getenv("KG_MINIO_SECRET_KEY", cls.minio_secret_key),
            minio_bucket=os.getenv("KG_MINIO_BUCKET", cls.minio_bucket),
            scheduler_enabled=_bool(os.getenv("KG_SCHEDULER_ENABLED"), False),
            scheduler_timezone=os.getenv("KG_SCHEDULER_TIMEZONE", cls.scheduler_timezone),
            ops_inspect_interval_minutes=int(
                os.getenv("KG_OPS_INSPECT_INTERVAL_MINUTES", cls.ops_inspect_interval_minutes)
            ),
            rag_embedding_backend=os.getenv("KG_RAG_EMBEDDING_BACKEND", cls.rag_embedding_backend),
            rag_embedding_base_url=os.getenv("KG_RAG_EMBEDDING_BASE_URL") or None,
            rag_embedding_api_key=os.getenv("KG_RAG_EMBEDDING_API_KEY") or None,
            rag_embedding_model=os.getenv("KG_RAG_EMBEDDING_MODEL", cls.rag_embedding_model),
            rag_embedding_batch_size=int(
                os.getenv("KG_RAG_EMBEDDING_BATCH_SIZE", cls.rag_embedding_batch_size)
            ),
            rag_llm_base_url=os.getenv("KG_RAG_LLM_BASE_URL") or None,
            rag_llm_api_key=os.getenv("KG_RAG_LLM_API_KEY") or None,
            rag_llm_model=os.getenv("KG_RAG_LLM_MODEL", cls.rag_llm_model),
            cors_origins=os.getenv("KG_CORS_ORIGINS", cls.cors_origins),
        )
