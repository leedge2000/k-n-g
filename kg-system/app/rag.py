from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in CJK_RANGES)


def tokenize(text: str) -> list[str]:
    """中文按单字＋二元组切分，拉丁字母与数字按整词切分。"""
    lowered = (text or "").casefold()
    tokens: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            tokens.append(run[0])
        else:
            tokens.extend(run[index] + run[index + 1] for index in range(len(run) - 1))
            tokens.extend(run)
        run.clear()

    word: list[str] = []
    for ch in lowered:
        if _is_cjk(ch):
            if word:
                tokens.append("".join(word))
                word.clear()
            run.append(ch)
            continue
        flush()
        if ch.isalnum():
            word.append(ch)
        elif word:
            tokens.append("".join(word))
            word.clear()
    flush()
    if word:
        tokens.append("".join(word))
    return [token for token in tokens if token.strip()]


class HashingEmbedder:
    """离线确定性向量化：哈希字符 n-gram + 次线性词频 + L2 归一化。"""

    name = "hash"

    def __init__(self, dim: int = 512):
        self.dim = dim

    def encode(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        counts: dict[str, int] = {}
        for token in tokenize(text):
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector


class OpenAIEmbedder:
    """调用 OpenAI 兼容 /embeddings 接口的向量后端，支持批量与维度自适应。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        batch_size: int = 16,
        dim: int = 0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "text-embedding-3-small"
        self.batch_size = max(1, int(batch_size or 16))
        self.dim = int(dim or 0)

    @property
    def name(self) -> str:
        return f"openai:{self.model}"

    def _post(self, texts: list[str]) -> list[list[float]]:
        import httpx

        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "input": texts, "encoding_format": "float"},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        return [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        vectors: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            for embedding in self._post(texts[start:start + self.batch_size]):
                vector = np.asarray(embedding, dtype=np.float32)
                norm = float(np.linalg.norm(vector))
                vectors.append(vector / norm if norm else vector)
        if not vectors:
            return np.zeros((0, self.dim or 1), dtype=np.float32)
        if not self.dim:
            self.dim = int(vectors[0].shape[0])
        return np.vstack(vectors)

    def encode(self, text: str) -> np.ndarray:
        return self.encode_batch([text])[0]

    def probe(self) -> int:
        """探测向量维度，顺带校验凭证与模型是否可用。"""
        self.dim = 0
        return int(self.encode("粮油产业知识检索维度探测").shape[0])


class BM25Index:
    """轻量 BM25，仅依赖倒排表，无需外部检索服务。"""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_count = len(documents)
        self.doc_len = np.array([len(doc) for doc in documents], dtype=np.float32)
        self.avg_len = float(self.doc_len.mean()) if self.doc_count else 0.0
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for index, doc in enumerate(documents):
            for token, count in self._frequencies(doc).items():
                self.postings.setdefault(token, []).append((index, count))
        self.idf = {
            token: math.log(1 + (self.doc_count - len(posts) + 0.5) / (len(posts) + 0.5))
            for token, posts in self.postings.items()
        }

    @staticmethod
    def _frequencies(tokens: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        return counts

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        scores = np.zeros(self.doc_count, dtype=np.float32)
        if not self.doc_count or not query_tokens:
            return scores
        for token in set(query_tokens):
            idf = self.idf.get(token)
            if idf is None:
                continue
            for index, tf in self.postings[token]:
                length = float(self.doc_len[index]) or 1.0
                denominator = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1.0))
                scores[index] += idf * tf * (self.k1 + 1) / denominator
        return scores


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}****{secret[-4:]}"


STOPWORDS = {
    "什么", "哪些", "怎么", "如何", "目前", "当前", "最新", "情况", "有关", "相关",
    "以及", "还有", "是否", "可以", "一个", "我们", "你们", "多少", "为什么", "哪些",
    "变化", "影响", "主要", "需要", "进行", "通过", "关于", "对于", "这个", "那个",
    "the", "and", "for", "with", "what", "how", "which", "from",
}

MAX_SEGMENT_ENTITIES = 24

# 阶段 1 别名表里只有这些本体类型算「有稳定身份的业务对象」，可以进实体图。
# 价格口径、加工工艺、计量单位、规则参数是属性取值，当作实体只会制造噪声共现。
ENTITY_TARGET_TYPES = frozenset(
    {
        "Commodity",
        "CommodityCategory",
        "CommodityForm",
        "Organization",
        "Enterprise",
        "Person",
        "Indicator",
        "EventType",
        "Region",
        "MarketPlace",
        "LogisticsNode",
    }
)

# 有稳定身份但应用库无对应记录的对齐状态，退到本体 ID 展示
ONTOLOGY_ID_STATUSES = frozenset({"NO_APP_TABLE", "NO_APP_RECORD"})

# 阶段 1 别名索引与对齐台账所在目录（由 ontology_ly/tools 下的脚本生成）
ONTOLOGY_TERMS_DIR = Path(__file__).resolve().parent.parent / "ontology_ly" / "terms"


class EntityGraph:
    """文档级实体/关系图：主数据词典命中段落，同段共现构成关系边。

    对应 LightRAG 的 chunk 级实体关系抽取，但使用确定性词典匹配，
    无需 LLM 也能离线运行，并可直接支撑 local / global 双级检索。
    """

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, Any]] = {}
        self.relations: dict[tuple[str, str], dict[str, Any]] = {}
        self.segments: dict[str, list[str]] = {}
        self.built_at: str | None = None

    @staticmethod
    def _names_in(text: str, names: set[str]) -> list[str]:
        if not text or not names:
            return []
        lengths = sorted({len(name) for name in names if len(name) >= 2})
        found: list[str] = []
        seen: set[str] = set()
        for length in lengths:
            for start in range(0, len(text) - length + 1):
                candidate = text[start:start + length]
                if candidate in names and candidate not in seen:
                    seen.add(candidate)
                    found.append(candidate)
        return found

    def build(
        self,
        records: list[dict[str, Any]],
        dictionary: dict[str, dict[str, Any]],
        extractions: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        extraction = extractions or {}
        extra_info: dict[str, dict[str, Any]] = {}
        for payload in extraction.values():
            for item in payload.get("entities", []):
                name = str(item.get("name") or "").strip()
                if len(name) >= 2 and name not in dictionary:
                    extra_info.setdefault(
                        name, {"type": item.get("type") or "抽取实体", "entityId": None}
                    )
        names = set(dictionary) | set(extra_info)
        entities: dict[str, dict[str, Any]] = {}
        relations: dict[tuple[str, str], dict[str, Any]] = {}
        segments: dict[str, list[str]] = {}
        for record in records:
            matched = self._names_in(record["segmentText"], names)
            payload = extraction.get(record["segmentId"]) or {}
            for item in payload.get("entities", []):
                name = str(item.get("name") or "").strip()
                if len(name) >= 2 and name not in matched:
                    matched.append(name)
            matched = matched[:MAX_SEGMENT_ENTITIES]
            segments[record["segmentId"]] = matched
            for name in matched:
                info = dictionary.get(name) or extra_info.get(name) or {"type": "抽取实体", "entityId": None}
                entity = entities.setdefault(
                    name,
                    {
                        "name": name,
                        "type": info.get("type"),
                        "entityId": info.get("entityId"),
                        "ontologyId": info.get("ontologyId"),
                        "ontologyType": info.get("ontologyType"),
                        "alignmentStatus": info.get("alignmentStatus"),
                        "segments": set(),
                        "docs": set(),
                        "count": 0,
                    },
                )
                entity["segments"].add(record["segmentId"])
                entity["docs"].add(record["docId"])
                entity["count"] += 1
            pairs: list[tuple[str, str]] = []
            for left in range(len(matched)):
                for right in range(left + 1, len(matched)):
                    pairs.append((matched[left], matched[right]))
            for item in payload.get("relations", []):
                source = str(item.get("source") or "").strip()
                target = str(item.get("target") or "").strip()
                if source in matched and target in matched and source != target:
                    pairs.append((source, target))
            for a, b in pairs:
                key = (a, b) if a <= b else (b, a)
                relation = relations.setdefault(
                    key, {"source": key[0], "target": key[1], "weight": 0, "segments": set()}
                )
                relation["weight"] += 1
                relation["segments"].add(record["segmentId"])
        self.entities = entities
        self.relations = relations
        self.segments = segments
        self.built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def match(self, text: str, limit: int = 8) -> list[str]:
        if not self.entities:
            return []
        found = [name for name in self._names_in(text, set(self.entities)) if name in self.entities]
        return found[:limit]

    def rank_segments(self, names: list[str], hops: int = 1) -> dict[str, float]:
        """local 检索：命中实体所在段落，按实体稀有度加权，并扩展一跳邻居。"""
        scores: dict[str, float] = {}
        seed = {name for name in names if name in self.entities}
        if not seed:
            return scores
        weights = {
            name: 1.0 / math.log(2 + len(self.entities[name]["segments"])) for name in seed
        }
        for name, weight in weights.items():
            for segment_id in self.entities[name]["segments"]:
                scores[segment_id] = scores.get(segment_id, 0.0) + weight
        if hops:
            neighbors: dict[str, float] = {}
            for (a, b), relation in self.relations.items():
                if a in seed and b not in seed:
                    neighbors[b] = max(neighbors.get(b, 0.0), relation["weight"] / (1 + relation["weight"]))
                elif b in seed and a not in seed:
                    neighbors[a] = max(neighbors.get(a, 0.0), relation["weight"] / (1 + relation["weight"]))
            for name, weight in neighbors.items():
                entity = self.entities.get(name)
                if not entity:
                    continue
                factor = 0.35 * weight / math.log(2 + len(entity["segments"]))
                for segment_id in entity["segments"]:
                    scores[segment_id] = scores.get(segment_id, 0.0) + factor
        return scores

    def relation_rank(self, names: list[str]) -> dict[str, float]:
        """global 检索：命中实体参与的关系边，按关系稀有度与共现强度加权。"""
        scores: dict[str, float] = {}
        seed = {name for name in names if name in self.entities}
        if not seed:
            return scores
        for (a, b), relation in self.relations.items():
            if a in seed or b in seed:
                rarity = 1.0 / math.log(2 + len(relation["segments"]))
                for segment_id in relation["segments"]:
                    scores[segment_id] = scores.get(segment_id, 0.0) + rarity * min(relation["weight"], 5)
        return scores

    def entity_rows(self, names: list[str], limit: int = 8) -> list[dict[str, Any]]:
        rows = []
        for name in names[:limit]:
            entity = self.entities.get(name)
            if not entity:
                continue
            rows.append(
                {
                    "name": name,
                    "type": entity["type"],
                    "entityId": entity["entityId"],
                    "ontologyId": entity.get("ontologyId"),
                    "ontologyType": entity.get("ontologyType"),
                    "alignmentStatus": entity.get("alignmentStatus"),
                    "segments": len(entity["segments"]),
                    "documents": len(entity["docs"]),
                }
            )
        return rows

    def top_relations(self, names: list[str], limit: int = 8) -> list[dict[str, Any]]:
        seed = set(names)
        rows = [
            relation
            for (a, b), relation in self.relations.items()
            if a in seed or b in seed
        ]
        rows.sort(key=lambda relation: relation["weight"], reverse=True)
        return [
            {
                "source": relation["source"],
                "target": relation["target"],
                "weight": relation["weight"],
                "segments": len(relation["segments"]),
            }
            for relation in rows[:limit]
        ]


class RagService:
    """面向来源文档段落的混合检索（向量 + BM25 + 图谱增强），LLM 可选。"""

    def __init__(self, db: Any, knowledge: Any, settings: Any):
        self.db = db
        self.knowledge = knowledge
        self.settings = settings
        self.directory = Path(settings.data_dir) / "rag"
        self.config_path = self.directory / "config.json"
        self.config = self._load_config(settings)
        self.embedder = self._build_embedder(self.config)
        self.backend = self.config["embeddingBackend"]
        self.dim = int(getattr(self.embedder, "dim", 0) or 0)
        self.lock = threading.RLock()
        self.records: list[dict[str, Any]] = []
        self.doc_tokens: list[list[str]] = []
        self.matrix = np.zeros((0, self.dim), dtype=np.float32)
        self.cached_ids: list[str] = []
        self.cached_matrix = np.zeros((0, self.dim), dtype=np.float32)
        self.bm25 = BM25Index([])
        self.signature: tuple[int, str] | None = None
        self.refreshed_at: str | None = None
        self.last_error: str | None = None
        self.graph = EntityGraph()
        self.dictionary: dict[str, dict[str, Any]] = {}
        self.extractions_path = self.directory / "entities.json"
        self.extractions: dict[str, dict[str, Any]] = {}
        self.extraction_pending = 0
        self._load_extractions()
        self._load()

    # ---------- 模型配置 ----------

    @staticmethod
    def _load_config(settings: Any) -> dict[str, str]:
        config = {
            "embeddingBackend": getattr(settings, "rag_embedding_backend", "hash") or "hash",
            "embeddingBaseUrl": getattr(settings, "rag_embedding_base_url", None) or "",
            "embeddingApiKey": getattr(settings, "rag_embedding_api_key", None) or "",
            "embeddingModel": getattr(settings, "rag_embedding_model", None) or "",
            "llmBaseUrl": getattr(settings, "rag_llm_base_url", None) or "",
            "llmApiKey": getattr(settings, "rag_llm_api_key", None) or "",
            "llmModel": getattr(settings, "rag_llm_model", None) or "gpt-4o-mini",
            "llmEntityExtraction": "false",
            "rerankMode": "lexical",
        }
        try:
            stored = json.loads(
                (Path(settings.data_dir) / "rag" / "config.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            for key in list(config):
                value = stored.get(key)
                if isinstance(value, str):
                    config[key] = value.strip()
        if config["embeddingBackend"] not in {"hash", "openai"}:
            config["embeddingBackend"] = "hash"
        if not config["embeddingModel"]:
            config["embeddingModel"] = "text-embedding-3-small"
        if config["rerankMode"] not in {"none", "lexical", "llm"}:
            config["rerankMode"] = "lexical"
        config["llmEntityExtraction"] = "true" if str(config["llmEntityExtraction"]).lower() in {"true", "1", "on", "yes"} else "false"
        return config

    def _build_embedder(self, config: dict[str, str]) -> Any:
        if config["embeddingBackend"] == "openai":
            return OpenAIEmbedder(
                config["embeddingBaseUrl"],
                config["embeddingApiKey"],
                config["embeddingModel"],
                int(getattr(self.settings, "rag_embedding_batch_size", 16) or 16),
            )
        return HashingEmbedder()

    @property
    def embedder_name(self) -> str:
        return getattr(self.embedder, "name", self.backend)

    def public_config(self) -> dict[str, Any]:
        config = self.config
        return {
            "embeddingBackend": config["embeddingBackend"],
            "embeddingBaseUrl": config["embeddingBaseUrl"],
            "embeddingModel": config["embeddingModel"],
            "embeddingApiKeyMasked": _mask(config["embeddingApiKey"]),
            "hasEmbeddingApiKey": bool(config["embeddingApiKey"]),
            "llmBaseUrl": config["llmBaseUrl"],
            "llmModel": config["llmModel"],
            "llmApiKeyMasked": _mask(config["llmApiKey"]),
            "hasLlmApiKey": bool(config["llmApiKey"]),
            "llmEntityExtraction": config["llmEntityExtraction"] == "true",
            "rerankMode": config["rerankMode"],
            "configFile": str(self.config_path),
        }

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "embeddingBackend",
            "embeddingBaseUrl",
            "embeddingApiKey",
            "embeddingModel",
            "llmBaseUrl",
            "llmApiKey",
            "llmModel",
        )
        candidate = dict(self.config)
        for field in fields:
            if field in payload:
                candidate[field] = str(payload.get(field) or "").strip()
        if "llmEntityExtraction" in payload:
            raw = payload.get("llmEntityExtraction")
            enabled = raw is True or str(raw).lower() in {"true", "1", "on", "yes"}
            candidate["llmEntityExtraction"] = "true" if enabled else "false"
        if "rerankMode" in payload:
            mode = str(payload.get("rerankMode") or "").strip().lower()
            if mode not in {"none", "lexical", "llm"}:
                raise ValueError("重排模式仅支持 none、lexical 或 llm")
            candidate["rerankMode"] = mode
        if candidate["embeddingBackend"] not in {"hash", "openai"}:
            raise ValueError("向量后端仅支持 hash 或 openai")
        if candidate["llmEntityExtraction"] == "true" and not candidate["llmBaseUrl"]:
            raise ValueError("启用 LLM 实体抽取需先填写归纳模型地址")
        if candidate["rerankMode"] == "llm" and not candidate["llmBaseUrl"]:
            raise ValueError("启用 LLM 重排需先填写归纳模型地址")
        if candidate["embeddingBackend"] == "openai":
            missing = [
                label
                for label, value in (
                    ("接口地址", candidate["embeddingBaseUrl"]),
                    ("API Key", candidate["embeddingApiKey"]),
                    ("模型名称", candidate["embeddingModel"]),
                )
                if not value
            ]
            if missing:
                raise ValueError("启用 OpenAI 兼容向量后端需填写：" + "、".join(missing))
        embedder = self._build_embedder(candidate)
        if candidate["embeddingBackend"] == "openai":
            try:
                embedder.probe()
            except Exception as exc:
                raise ValueError(f"向量接口校验失败：{exc}") from exc
        with self.lock:
            self.config = candidate
            self.embedder = embedder
            self.backend = candidate["embeddingBackend"]
            self.dim = int(getattr(embedder, "dim", 0) or 0)
            self.cached_ids = []
            self.cached_matrix = np.zeros((0, self.dim), dtype=np.float32)
            self.signature = None
            self.last_error = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as exc:
                raise ValueError(f"配置写入失败：{exc}") from exc
        try:
            self.refresh(force=True)
        except Exception as exc:
            self.last_error = str(exc)
        return {"status": self.status(), "config": self.public_config()}

    # ---------- 索引维护 ----------

    def _embed(self, text: str) -> np.ndarray:
        return self.embedder.encode(text)

    def _embed_many(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, max(self.dim, 1)), dtype=np.float32)
        if hasattr(self.embedder, "encode_batch"):
            return self.embedder.encode_batch(texts)
        return np.vstack([self.embedder.encode(text) for text in texts])

    def _rows(self) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT s."segmentId" AS segmentId, s."docId" AS docId, s."segmentNo" AS segmentNo,
                   s."segmentText" AS segmentText, s."pageNo" AS pageNo,
                   s."chapterPath" AS chapterPath,
                   d."title" AS docTitle, d."docType" AS docType, d."issuer" AS issuer,
                   d."publishDate" AS publishDate
            FROM "SourceDoc_Segment" s
            LEFT JOIN "SourceDoc" d ON d."docId" = s."docId"
            WHERE COALESCE(d."status", '有效') = '有效'
              AND LENGTH(TRIM(COALESCE(s."segmentText", ''))) > 0
            ORDER BY s."docId", s."segmentNo"
            """
        )

    def _build_dictionary(self) -> dict[str, dict[str, Any]]:
        """汇集主数据与同义词作为实体词典，供段落实体识别使用。"""
        dictionary: dict[str, dict[str, Any]] = {}
        for entity_type in ("品种", "产品", "地区", "企业", "本体概念", "指标定义"):
            try:
                items = self.knowledge.list_entities(entity_type, limit=2000)
            except Exception:
                continue
            for item in items:
                name = str(item.get("title") or "").strip()
                if len(name) >= 2:
                    dictionary.setdefault(
                        name, {"type": entity_type, "entityId": item.get("entityId")}
                    )
        try:
            rows = self.db.fetch_all(
                'SELECT "conceptId" AS conceptId, "synonymName" AS synonymName FROM "Concept_Synonym"'
            )
        except Exception:
            rows = []
        for row in rows:
            name = str(row.get("synonymName") or "").strip()
            if len(name) >= 2:
                dictionary.setdefault(
                    name, {"type": "本体概念", "entityId": row.get("conceptId")}
                )
        self._merge_ontology_aliases(dictionary)
        return dictionary

    def _merge_ontology_aliases(self, dictionary: dict[str, dict[str, Any]]) -> None:
        """把阶段 1 别名索引并入实体词典。

        只收有稳定身份的业务对象：类型在 ENTITY_TARGET_TYPES 内，对齐状态为
        ALIGNED_APP / NO_APP_TABLE / NO_APP_RECORD。类型级结构词、口径枚举、
        无 targetIdHint 与 grade= 取值一律跳过。同一写法指向多个对象的歧义项
        直接跳过，消歧失败不猜。已有主数据条目优先，别名只补空缺。
        """
        try:
            index = json.loads(
                (ONTOLOGY_TERMS_DIR / "alias_index.json").read_text(encoding="utf-8")
            )
            alignment = json.loads(
                (ONTOLOGY_TERMS_DIR / "concept_alignment.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return
        verdicts = {item["targetKey"]: item for item in alignment.get("targets", [])}
        for entry in index.get("entries", []):
            targets = entry.get("targets") or []
            if len(targets) != 1:
                continue
            target = targets[0]
            if target.get("targetType") not in ENTITY_TARGET_TYPES:
                continue
            verdict = verdicts.get(target.get("targetKey")) or {}
            status = verdict.get("status")
            identity = {
                "ontologyId": target.get("targetIdHint"),
                "ontologyType": target.get("targetType"),
                "alignmentStatus": status,
            }
            if status == "ALIGNED_APP":
                info = {"type": verdict["appType"], "entityId": verdict["appId"], **identity}
            elif status in ONTOLOGY_ID_STATUSES:
                info = {
                    "type": target.get("typeLabel") or target.get("targetType"),
                    "entityId": target.get("targetIdHint"),
                    **identity,
                }
            else:
                continue
            name = str(entry.get("surface") or "").strip()
            if len(name) < 2:
                continue
            if name not in dictionary:
                dictionary[name] = info
            elif status == "ALIGNED_APP" and dictionary[name].get("entityId") == verdict.get("appId"):
                # 主数据词典已占位且指向同一 app 记录时，只补本体身份，不覆盖主数据口径
                for key, value in identity.items():
                    dictionary[name].setdefault(key, value)

    def _dictionary_for_index(self, force: bool = False) -> dict[str, dict[str, Any]]:
        if force or not self.dictionary:
            self.dictionary = self._build_dictionary()
        return self.dictionary

    # ---------- LLM 段落实体抽取 ----------

    MAX_EXTRACTIONS_PER_REFRESH = 40

    def _load_extractions(self) -> None:
        try:
            data = json.loads(self.extractions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.extractions = {
                str(key): value for key, value in data.items() if isinstance(value, dict)
            }

    def _save_extractions(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.extractions_path.write_text(
                json.dumps(self.extractions, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _chat(self, prompt: str, timeout: float = 30, temperature: float = 0.1) -> str | None:
        base = (self.config["llmBaseUrl"] or "").rstrip("/")
        if not base:
            return None
        try:
            import httpx

            response = httpx.post(
                f"{base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.config['llmApiKey']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config["llmModel"],
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    def _llm_extract(self, text: str) -> dict[str, Any] | None:
        snippet = (text or "").strip()[:1500]
        if not snippet:
            return None
        prompt = (
            "你是粮油产业知识图谱的实体抽取器。从下面的文档段落中抽取实体与实体关系。\n"
            "要求：只抽取原文明确出现的机构、政策、项目、事件、品种、地区、指标、人物；"
            "不要臆造；实体名称保持原文；最多 12 个实体、10 条关系。\n"
            '只输出 JSON：{"entities":[{"name":"","type":"机构|政策|项目|事件|品种|地区|指标|人物|其他"}],'
            '"relations":[{"source":"实体名","target":"实体名"}]}\n段落：\n' + snippet
        )
        raw = self._chat(prompt, timeout=25, temperature=0)
        if not raw:
            return None
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        entities: list[dict[str, str]] = []
        for item in data.get("entities") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if len(name) >= 2:
                entities.append({"name": name[:40], "type": str(item.get("type") or "其他")[:10]})
        relations: list[dict[str, str]] = []
        for item in data.get("relations") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            if source and target and source != target:
                relations.append({"source": source[:40], "target": target[:40]})
        return {"entities": entities[:12], "relations": relations[:10]}

    def _extract_entities(self, records: list[dict[str, Any]]) -> None:
        pending: list[tuple[dict[str, Any], str]] = []
        for record in records:
            digest = hashlib.sha1(record["segmentText"].encode("utf-8")).hexdigest()[:16]
            cached = self.extractions.get(record["segmentId"])
            if cached and cached.get("hash") == digest:
                continue
            pending.append((record, digest))
        processed = 0
        for record, digest in pending[: self.MAX_EXTRACTIONS_PER_REFRESH]:
            data = self._llm_extract(record["segmentText"])
            if data is None:
                break
            self.extractions[record["segmentId"]] = {"hash": digest, **data}
            processed += 1
        valid = {record["segmentId"] for record in records}
        self.extractions = {key: value for key, value in self.extractions.items() if key in valid}
        self.extraction_pending = len(pending) - processed
        if processed:
            self._save_extractions()

    def refresh(self, force: bool = False) -> bool:
        with self.lock:
            rows = self._rows()
            ids = [row["segmentId"] for row in rows]
            signature = (len(ids), ids[-1] if ids else "")
            if not force and signature == self.signature:
                if self.config["llmEntityExtraction"] == "true" and self.config["llmBaseUrl"]:
                    before = len(self.extractions)
                    self._extract_entities(self.records)
                    if len(self.extractions) != before:
                        self.graph.build(self.records, self._dictionary_for_index(), self.extractions)
                return False

            existing = {} if force else {
                segment_id: index for index, segment_id in enumerate(self.cached_ids)
            }
            records: list[dict[str, Any]] = []
            vectors: list[np.ndarray | None] = []
            missing: list[int] = []
            for row in rows:
                position = existing.get(row["segmentId"])
                record = {
                    "segmentId": row["segmentId"],
                    "docId": row["docId"],
                    "segmentNo": row["segmentNo"],
                    "segmentText": row["segmentText"] or "",
                    "pageNo": row["pageNo"],
                    "chapterPath": row["chapterPath"],
                    "docTitle": row["docTitle"] or row["docId"],
                    "docType": row["docType"],
                    "issuer": row["issuer"],
                    "publishDate": row["publishDate"],
                }
                records.append(record)
                if position is not None and position < len(self.cached_matrix):
                    vectors.append(self.cached_matrix[position])
                else:
                    missing.append(len(vectors))
                    vectors.append(None)
            if missing:
                encoded = self._embed_many([records[index]["segmentText"] for index in missing])
                for offset, target in enumerate(missing):
                    vectors[target] = encoded[offset]
            if self.dim == 0 and vectors and vectors[0] is not None:
                self.dim = int(vectors[0].shape[0])
                if hasattr(self.embedder, "dim"):
                    self.embedder.dim = self.dim

            self.records = records
            self.matrix = (
                np.vstack([vector for vector in vectors if vector is not None]).astype(np.float32)
                if vectors
                else np.zeros((0, max(self.dim, 1)), dtype=np.float32)
            )
            self.cached_ids = [record["segmentId"] for record in records]
            self.cached_matrix = self.matrix
            self.doc_tokens = [tokenize(record["segmentText"]) for record in records]
            self.bm25 = BM25Index(self.doc_tokens)
            if self.config["llmEntityExtraction"] == "true" and self.config["llmBaseUrl"]:
                self._extract_entities(records)
            self.graph.build(records, self._dictionary_for_index(force), self.extractions)
            self.signature = signature
            self.refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save()
            return True

    def _save(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            np.save(self.directory / "vectors.npy", self.matrix)
            (self.directory / "meta.json").write_text(
                json.dumps(
                    {
                        "dim": self.dim,
                        "backend": self.backend,
                        "model": self.embedder_name,
                        "segmentIds": [record["segmentId"] for record in self.records],
                        "refreshedAt": self.refreshed_at,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _load(self) -> None:
        try:
            meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
            matrix = np.load(self.directory / "vectors.npy")
        except (OSError, ValueError):
            return
        if meta.get("backend") != self.backend or meta.get("model") != self.embedder_name:
            return
        dim = int(meta.get("dim") or 0)
        if self.dim and dim != self.dim:
            return
        if matrix.shape[0] != len(meta.get("segmentIds", [])) or not dim:
            return
        if not self.dim:
            self.dim = dim
            if hasattr(self.embedder, "dim"):
                self.embedder.dim = dim
        self.cached_ids = list(meta.get("segmentIds", []))
        self.cached_matrix = matrix.astype(np.float32)
        self.matrix = self.cached_matrix

    # ---------- 检索 ----------

    def status(self) -> dict[str, Any]:
        try:
            self.refresh()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
        return {
            "enabled": True,
            "segments": len(self.records),
            "documents": len({record["docId"] for record in self.records}),
            "dim": self.dim,
            "backend": self.backend,
            "embeddingModel": self.embedder_name,
            "llmEnabled": bool(self.config["llmBaseUrl"]),
            "llmModel": self.config["llmModel"],
            "entities": len(self.graph.entities),
            "relations": len(self.graph.relations),
            "graphBuiltAt": self.graph.built_at,
            "rerankMode": self.config["rerankMode"],
            "llmEntityExtraction": self.config["llmEntityExtraction"] == "true",
            "extractedSegments": len(self.extractions),
            "extractionPending": self.extraction_pending,
            "refreshedAt": self.refreshed_at,
            "lastError": self.last_error,
        }

    def rebuild(self) -> dict[str, Any]:
        self.dictionary = {}
        self.refresh(force=True)
        return self.status()

    def remove_document(self, doc_id: str) -> dict[str, Any]:
        """按文档增量删除：仅剔除该文档的段落与向量行，不重建其余段落向量。"""
        doc_id = (doc_id or "").strip()
        if not doc_id:
            return {"removed": 0}
        with self.lock:
            keep = [index for index, record in enumerate(self.records) if record["docId"] != doc_id]
            removed = len(self.records) - len(keep)
            if not removed:
                return {"removed": 0, "docId": doc_id, "status": self.status()}
            self.records = [self.records[index] for index in keep]
            self.matrix = (
                self.matrix[keep] if self.matrix.shape[0] else np.zeros((0, self.dim), dtype=np.float32)
            )
            self.cached_ids = [record["segmentId"] for record in self.records]
            self.cached_matrix = self.matrix
            self.doc_tokens = [self.doc_tokens[index] for index in keep] if self.doc_tokens else []
            self.bm25 = BM25Index(self.doc_tokens)
            valid = set(self.cached_ids)
            self.extractions = {
                key: value for key, value in self.extractions.items() if key in valid
            }
            self.graph.build(self.records, self.dictionary or {}, self.extractions)
            self.signature = (
                len(self.records),
                self.records[-1]["segmentId"] if self.records else "",
            )
            self.refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save()
            self._save_extractions()
        return {"removed": removed, "docId": doc_id, "status": self.status()}

    SUPPORTED_MODES = ("naive", "local", "global", "hybrid", "mix")

    # 政策清单只取前若干条进入答案上下文，避免把整张政策表灌给模型
    POLICY_CONTEXT_LIMIT = 5

    def search(
        self, query: str, mode: str = "hybrid", top_k: int = 10, answer: bool = True,
        policies: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = (query or "").strip()
        if not text:
            raise ValueError("检索问题不能为空")
        top_k = max(1, min(int(10 if top_k is None else top_k), 50))
        if mode == "graph":
            mode = "mix"
        if mode not in self.SUPPORTED_MODES:
            mode = "hybrid"
        policy_context = self._policy_summary(policies)
        try:
            self.refresh()
        except Exception as exc:
            self.last_error = str(exc)

        keywords = self._keywords(text)
        if not self.records:
            answer_text, answer_mode = self._answer(text, [], None, policies) if answer else ("", "disabled")
            return {
                "query": text,
                "mode": mode,
                "rerankMode": self.config["rerankMode"],
                "keywords": keywords,
                "matchedEntities": [],
                "relations": [],
                "items": [],
                "answer": answer_text,
                "answerMode": answer_mode,
                "policyContext": policy_context,
                "citations": [],
                "index": self.status(),
            }

        try:
            query_vector = self._embed(text)
        except Exception as exc:
            raise ValueError(f"查询向量化失败：{exc}") from exc
        vector_scores = self.matrix @ query_vector
        keyword_scores = self.bm25.scores(tokenize(text))
        high_scores = self.bm25.scores(tokenize(" ".join(keywords["high"])))
        local_scores = self._to_vector(self.graph.rank_segments(keywords["low"]))
        relation_scores = self._to_vector(self.graph.relation_rank(keywords["low"]))
        graph_boost = self._graph_boost(text) if mode == "mix" else {}

        weights: dict[str, float] = {}
        if mode == "naive":
            weights["vector"] = 1.0
        elif mode == "local":
            weights.update({"local": 1.6, "vector": 0.6})
        elif mode == "global":
            weights.update({"relation": 1.6, "keyword": 0.6, "high": 0.6})
        elif mode == "hybrid":
            weights.update({"local": 1.2, "relation": 1.0, "vector": 1.0, "keyword": 1.0})
        else:
            weights.update({"local": 1.2, "relation": 1.2, "vector": 1.0, "keyword": 1.0, "high": 0.5})

        rankings = {
            "vector": self._ranking(vector_scores),
            "keyword": self._ranking(keyword_scores),
            "high": self._ranking(high_scores),
            "local": self._ranking(local_scores),
            "relation": self._ranking(relation_scores),
        }
        fused: dict[int, float] = {}
        for name, weight in weights.items():
            for rank, index in enumerate(rankings.get(name, [])):
                fused[index] = fused.get(index, 0.0) + weight / (60 + rank + 1)
        if graph_boost:
            for index, record in enumerate(self.records):
                boost = graph_boost.get(record["segmentId"])
                if boost:
                    fused[index] = fused.get(index, 0.0) + 0.5 * boost

        ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)
        ordered, rerank_scores = self._rerank(text, keywords, ordered, top_k)
        best = ordered[0][1] if ordered else 1.0
        items = []
        for index, score in ordered:
            record = self.records[index]
            items.append(
                {
                    "segmentId": record["segmentId"],
                    "docId": record["docId"],
                    "docTitle": record["docTitle"],
                    "docType": record["docType"],
                    "issuer": record["issuer"],
                    "publishDate": record["publishDate"],
                    "pageNo": record["pageNo"],
                    "chapterPath": record["chapterPath"],
                    "segmentNo": record["segmentNo"],
                    "segmentText": record["segmentText"],
                    "score": round(score / best, 4) if best else 0.0,
                    "vectorScore": round(float(vector_scores[index]), 4),
                    "keywordScore": round(float(keyword_scores[index]), 4),
                    "localScore": round(float(local_scores[index]), 4),
                    "relationScore": round(float(relation_scores[index]), 4),
                    "rerankScore": round(float(rerank_scores.get(index, score)), 4),
                    "graphBoost": round(float(graph_boost.get(record["segmentId"], 0.0)), 4),
                    "matchedEntities": [
                        name
                        for name in self.graph.segments.get(record["segmentId"], [])
                        if name in keywords["low"]
                    ][:4],
                }
            )

        relations = self.graph.top_relations(keywords["low"])
        answer_text, answer_mode = (
            self._answer(text, items, relations, policies) if answer else ("", "disabled")
        )
        return {
            "query": text,
            "mode": mode,
            "rerankMode": self.config["rerankMode"],
            "keywords": keywords,
            "matchedEntities": self.graph.entity_rows(keywords["low"]),
            "relations": relations,
            "items": items,
            "answer": answer_text,
            "answerMode": answer_mode,
            "policyContext": policy_context,
            "citations": [
                {
                    "segmentId": item["segmentId"],
                    "docId": item["docId"],
                    "docTitle": item["docTitle"],
                    "pageNo": item["pageNo"],
                    "chapterPath": item.get("chapterPath"),
                    "segmentNo": item.get("segmentNo"),
                    "quote": (item.get("segmentText") or "")[:200],
                }
                for item in items[:5 if answer_mode == "llm" else 3]
            ],
            "index": self.status(),
        }

    def _rerank(
        self,
        text: str,
        keywords: dict[str, list[str]],
        ranked: list[tuple[int, float]],
        top_k: int,
    ) -> tuple[list[tuple[int, float]], dict[int, float]]:
        """对 RRF 融合后的候选做二次重排：lexical 为本地覆盖度，llm 为模型打分。"""
        candidates = ranked[: max(top_k * 3, 20)]
        mode = self.config.get("rerankMode", "lexical")
        if mode == "none" or len(candidates) <= 1:
            return candidates[:top_k], {index: score for index, score in candidates}
        if mode == "llm":
            order = self._llm_rerank(text, candidates)
            if order:
                used: set[int] = set()
                reordered: list[tuple[int, float]] = []
                for position in order:
                    index, score = candidates[position]
                    if index in used:
                        continue
                    used.add(index)
                    reordered.append((index, score))
                reordered.extend(item for item in candidates if item[0] not in used)
                return reordered[:top_k], {}
        scores = self._lexical_rerank(text, keywords, candidates)
        ordered = sorted(candidates, key=lambda item: scores.get(item[0], 0.0), reverse=True)
        return ordered[:top_k], scores

    def _lexical_rerank(
        self,
        text: str,
        keywords: dict[str, list[str]],
        candidates: list[tuple[int, float]],
    ) -> dict[int, float]:
        query_tokens = {token for token in tokenize(text) if len(token) >= 2}
        best = max((score for _, score in candidates), default=1.0) or 1.0
        high = keywords.get("high") or []
        low = set(keywords.get("low") or [])
        scores: dict[int, float] = {}
        for index, fused in candidates:
            record = self.records[index]
            doc_tokens = set(self.doc_tokens[index]) if index < len(self.doc_tokens) else set()
            coverage = len(query_tokens & doc_tokens) / len(query_tokens) if query_tokens else 0.0
            phrase = (
                sum(1 for word in high if word in record["segmentText"]) / len(high)
                if high
                else 0.0
            )
            matched = self.graph.segments.get(record["segmentId"], [])
            entity = sum(1 for name in matched if name in low) / len(low) if low else 0.0
            scores[index] = 0.5 * (fused / best) + 0.3 * coverage + 0.12 * phrase + 0.08 * entity
        return scores

    def _llm_rerank(
        self, text: str, candidates: list[tuple[int, float]]
    ) -> list[int] | None:
        lines = []
        for position, (index, _) in enumerate(candidates):
            snippet = self.records[index]["segmentText"][:180].replace("\n", " ")
            lines.append(f"[{position}] {snippet}")
        prompt = (
            "根据问题评估下列段落的相关度，只输出 JSON："
            '{"order":[最相关编号, ...]}，按相关度从高到低，必须覆盖全部编号且不重复。\n'
            f"问题：{text}\n段落：\n" + "\n".join(lines)
        )
        raw = self._chat(prompt, timeout=20, temperature=0)
        if not raw:
            return None
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        order: list[int] = []
        for value in data.get("order") or []:
            try:
                position = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= position < len(candidates) and position not in order:
                order.append(position)
        return order or None

    def _to_vector(self, scores: dict[str, float]) -> np.ndarray:
        vector = np.zeros(len(self.records), dtype=np.float32)
        for index, record in enumerate(self.records):
            value = scores.get(record["segmentId"])
            if value:
                vector[index] = value
        return vector

    def _keywords(self, text: str) -> dict[str, list[str]]:
        """对齐 LightRAG 的双级关键词：low 为具体实体，high 为上位主题词。"""
        low = self.graph.match(text, limit=8)
        # 查询别名可能从未出现在原文中，因而没有独立图谱节点；按稳定身份
        # 映射到同一对象的原文实体，避免别名只进词典却无法参与检索。
        for alias in self.graph._names_in(text, set(self.dictionary)):
            if alias in self.graph.entities:
                continue
            info = self.dictionary[alias]
            for name, entity in self.graph.entities.items():
                same_entity = bool(info.get("entityId")) and info.get("entityId") == entity.get("entityId")
                same_ontology = bool(info.get("ontologyId")) and info.get("ontologyId") == entity.get("ontologyId")
                if (same_entity or same_ontology) and name not in low:
                    low.append(name)
        high = [
            phrase for phrase in self._phrases(text)
            if phrase not in low and phrase not in STOPWORDS
        ][:8]
        if not high:
            high = [
                token for token in tokenize(text)
                if len(token) >= 2 and token not in STOPWORDS and token not in low
            ][:8]
        if self.config["llmBaseUrl"]:
            extracted = self._llm_keywords(text)
            if extracted:
                for name in extracted.get("low_level_keywords", []):
                    if name in self.graph.entities and name not in low:
                        low.append(name)
                for name in extracted.get("high_level_keywords", []):
                    if name and name not in high:
                        high.append(name)
        return {"high": high[:10], "low": low[:10]}

    def _phrases(self, text: str) -> list[str]:
        """从问题中抽 2-4 字词块，仅保留语料中出现过的，优先长词。"""
        if not self.bm25.postings:
            return []
        candidates: set[str] = set()
        for match in re.finditer(r"[\u4e00-\u9fff]{2,}", text or ""):
            run = match.group(0)
            for length in range(4, 1, -1):
                for start in range(0, len(run) - length + 1):
                    candidate = run[start:start + length]
                    if candidate in self.bm25.postings:
                        candidates.add(candidate)
        kept: list[str] = []
        for candidate in sorted(candidates, key=len, reverse=True):
            if any(candidate in longer for longer in kept):
                continue
            kept.append(candidate)
        return kept

    def _llm_keywords(self, question: str) -> dict[str, list[str]] | None:
        base_url = self.config["llmBaseUrl"]
        if not base_url:
            return None
        headers = {"Content-Type": "application/json"}
        if self.config["llmApiKey"]:
            headers["Authorization"] = f"Bearer {self.config['llmApiKey']}"
        instruction = (
            "从用户的粮油产业问题中抽取检索关键词。只输出 JSON，格式为 "
            '{"high_level_keywords": ["主题概念"], "low_level_keywords": ["具体实体"]}。'
            "high_level_keywords 放政策方向、趋势、主题等上位概念；"
            "low_level_keywords 放品种、地区、企业、指标等具体实体。各不超过 6 个。"
        )
        try:
            import httpx

            response = httpx.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": self.config["llmModel"],
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": instruction},
                        {"role": "user", "content": question},
                    ],
                },
                timeout=12,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            match = re.search(r"\{.*\}", content, re.S)
            data = json.loads(match.group(0)) if match else {}
        except Exception:
            return None
        return {
            "high_level_keywords": [str(item) for item in data.get("high_level_keywords") or []],
            "low_level_keywords": [str(item) for item in data.get("low_level_keywords") or []],
        }

    @staticmethod
    def _ranking(scores: np.ndarray | None) -> list[int]:
        if scores is None or not len(scores):
            return []
        positive = np.where(scores > 0)[0]
        return positive[np.argsort(-scores[positive])].tolist()

    def _graph_boost(self, text: str) -> dict[str, float]:
        try:
            result = self.knowledge.search(
                {
                    "keyword": text,
                    "categoryCode": None,
                    "knowledgeType": None,
                    "industryStage": None,
                    "factorDimension": None,
                    "commodityCode": None,
                    "regionCode": None,
                    "dateFrom": None,
                    "dateTo": None,
                    "pageNo": 1,
                    "pageSize": 50,
                }
            )
        except Exception:
            return {}
        document_boost: dict[str, float] = {}
        segment_boost: dict[str, float] = {}
        for item in result.get("items", []) or []:
            if item.get("segmentId"):
                segment_boost[item["segmentId"]] = 1.0
            elif item.get("sourceDocId"):
                document_boost[item["sourceDocId"]] = max(document_boost.get(item["sourceDocId"], 0.0), 0.6)
        boosts = dict(segment_boost)
        for index, record in enumerate(self.records):
            if record["segmentId"] in boosts:
                continue
            boost = document_boost.get(record["docId"])
            if boost:
                boosts[record["segmentId"]] = boost
        return boosts

    # ---------- 答案归纳 ----------

    @classmethod
    def _policy_summary(cls, policies: dict[str, Any] | None) -> dict[str, Any] | None:
        """对外只回显少量政策，完整清单由 /api/policies/effective 提供。"""
        if not policies:
            return None
        return {
            "asOf": policies.get("asOf"),
            "scopeId": policies.get("scopeId"),
            "total": policies.get("total", 0),
            "counts": policies.get("counts") or {},
            "items": (policies.get("items") or [])[: cls.POLICY_CONTEXT_LIMIT],
        }

    @classmethod
    def _policy_text(cls, policies: dict[str, Any] | None) -> str:
        summary = cls._policy_summary(policies)
        if not summary or not summary["items"]:
            return ""
        scope_note = f"，适用范围 {summary['scopeId']}" if summary["scopeId"] else ""
        lines = [
            f"现行有效政策（截至 {summary['asOf']}{scope_note}，共 {summary['total']} 条）："
        ]
        for index, item in enumerate(summary["items"], 1):
            effective = item.get("effectiveDate") or "长期有效"
            citation = str(item.get("citation") or f"P{index}")
            lines.append(
                f"[{citation}]《{item.get('policyTitle')}》"
                f"（{item.get('docNumber') or '无文号'}，{effective} 起施行）"
            )
        return "\n".join(lines)

    def _answer(
        self, question: str, items: list[dict[str, Any]], relations: list[dict[str, Any]] | None = None,
        policies: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        policy_text = self._policy_text(policies)
        if not items:
            return (policy_text, "policy") if policy_text else ("", "extractive")
        synthesized = self._synthesize(question, items[:5], relations or [], policy_text)
        if synthesized:
            return synthesized, "llm"
        body = "；".join(
            f"【{index + 1}】{item['segmentText'][:220]}"
            for index, item in enumerate(items[:3])
        )
        if policy_text:
            body = f"{policy_text}\n{body}"
        return body, "extractive"

    def _synthesize(
        self, question: str, items: list[dict[str, Any]], relations: list[dict[str, Any]],
        policy_text: str = "",
    ) -> str | None:
        base_url = self.config["llmBaseUrl"]
        if not base_url:
            return None
        try:
            import httpx
        except ImportError:
            return None
        context = ""
        if policy_text:
            context = policy_text + "\n\n"
        context += "\n\n".join(
            f"[{index + 1}] 《{item['docTitle']}》{item['segmentText'][:600]}"
            for index, item in enumerate(items)
        )
        if relations:
            context += "\n\n图谱关系：" + "；".join(
                f"{item['source']}—{item['target']}（共现 {item['weight']} 次）"
                for item in relations[:8]
            )
        headers = {"Content-Type": "application/json"}
        if self.config["llmApiKey"]:
            headers["Authorization"] = f"Bearer {self.config['llmApiKey']}"
        try:
            response = httpx.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": self.config["llmModel"],
                    "temperature": 0.2,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是粮油产业政策与情报分析助手。只依据给定资料、现行有效政策清单与图谱关系作答，"
                                "涉及政策时效时以清单中的截止日期为准；用中文先给出结论，再列要点，"
                                "并在结论后标注引用编号；资料不足时明确说明。"
                            ),
                        },
                        {"role": "user", "content": f"问题：{question}\n\n资料：\n{context}"},
                    ],
                },
                timeout=30,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            return None
