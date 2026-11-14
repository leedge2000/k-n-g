# -*- coding: utf-8 -*-
"""阶段 1 离线检索基线：固定快照、五模式指标、引用回查与确定性验证。"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.db import Database
from app.rag import RagService
from app.scope import EXPECTED_DIMENSION_COUNT, ScopeService
from app.service import KnowledgeService

TOOLS_DIR = Path(__file__).resolve().parent
GOLD_PATH = ROOT / "ontology_ly" / "capability" / "stage1_retrieval_gold.json"
DATABASE_PATH = ROOT / "data" / "grain_oil.db"
REPORT_PATH = TOOLS_DIR / "stage1_report.json"
PROTECTED_PATHS = (
    DATABASE_PATH,
    ROOT / "data" / "rag" / "config.json",
    ROOT / "data" / "rag" / "vectors.npy",
    ROOT / "data" / "rag" / "meta.json",
    ROOT / "data" / "rag" / "entities.json",
)
METRIC_NAMES = ("hit@5", "hit@10", "recall@5", "recall@10", "mrr@10", "ndcg@10")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "sha256": None, "sizeBytes": None}
    return {"exists": True, "sha256": sha256(path), "sizeBytes": path.stat().st_size}


def percentile(values: list[float], fraction: float) -> float | None:
    """ nearest-rank 百分位；空样本不伪造为 0。"""
    if not values:
        return None
    ordered = sorted(values)
    position = max(1, math.ceil(fraction * len(ordered)))
    return ordered[position - 1]


def latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "p50Ms": round(percentile(values, 0.50), 3) if values else None,
        "p95Ms": round(percentile(values, 0.95), 3) if values else None,
        "maxMs": round(max(values), 3) if values else None,
    }


def ranking_metrics(ranked_ids: list[str], relevant_ids: list[str]) -> dict[str, Any]:
    relevant = set(relevant_ids)
    first_rank = next(
        (rank for rank, segment_id in enumerate(ranked_ids, 1) if segment_id in relevant),
        None,
    )

    def hits_at(k: int) -> int:
        return len({segment_id for segment_id in ranked_ids[:k] if segment_id in relevant})

    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, segment_id in enumerate(ranked_ids[:10], 1)
        if segment_id in relevant
    )
    ideal_count = min(len(relevant), 10)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return {
        "hit@5": float(hits_at(5) > 0),
        "hit@10": float(hits_at(10) > 0),
        "recall@5": hits_at(5) / len(relevant) if relevant else 0.0,
        "recall@10": hits_at(10) / len(relevant) if relevant else 0.0,
        "mrr@10": 1.0 / first_rank if first_rank is not None and first_rank <= 10 else 0.0,
        "ndcg@10": dcg / idcg if idcg else 0.0,
        "firstRelevantRank": first_rank,
    }


def evaluate_retrieval(service: RagService, gold: dict[str, Any]) -> dict[str, Any]:
    """复用生产检索入口；冷轮用于指标，热轮用于延迟与第二/三轮确定性比较。"""
    queries = gold["queries"]
    modes: dict[str, Any] = {}
    runs: list[dict[str, Any]] = []

    for mode in service.SUPPORTED_MODES:
        mode_runs: list[dict[str, Any]] = []
        for phase, rounds in (("cold", (0,)), ("warm", (1, 2, 3))):
            for round_no in rounds:
                for query in queries:
                    started = perf_counter()
                    result = None
                    error = None
                    try:
                        result = service.search(
                            query["query"], mode=mode, top_k=10, answer=True
                        )
                    except Exception as exc:  # 单条失败也必须留痕，并继续评测其余模式
                        error = f"{type(exc).__name__}: {exc}"
                    elapsed_ms = (perf_counter() - started) * 1000
                    run = {
                        "mode": mode,
                        "phase": phase,
                        "round": round_no,
                        "queryId": query["queryId"],
                        "elapsedMs": elapsed_ms,
                        "result": result,
                        "error": error,
                    }
                    mode_runs.append(run)
                    runs.append(run)

        cold_runs = [run for run in mode_runs if run["phase"] == "cold"]
        warm_runs = [run for run in mode_runs if run["phase"] == "warm"]
        by_round = {
            round_no: {
                run["queryId"]: run
                for run in mode_runs
                if run["phase"] == "warm" and run["round"] == round_no
            }
            for round_no in (1, 2, 3)
        }
        deterministic = True
        determinism_comparisons = []
        for query in queries:
            query_id = query["queryId"]
            second = by_round[2][query_id]
            third = by_round[3][query_id]
            second_ids = (
                [item["segmentId"] for item in second["result"]["items"]]
                if second["result"] is not None
                else None
            )
            third_ids = (
                [item["segmentId"] for item in third["result"]["items"]]
                if third["result"] is not None
                else None
            )
            same = (
                second["error"] is None
                and third["error"] is None
                and second_ids == third_ids
            )
            deterministic = deterministic and same
            determinism_comparisons.append(
                {
                    "queryId": query_id,
                    "equal": same,
                    "round2SegmentIds": second_ids,
                    "round3SegmentIds": third_ids,
                }
            )

        question_rows = []
        for query, run in zip(queries, cold_runs, strict=True):
            result = run["result"]
            items = result["items"] if result is not None else []
            ranked_ids = [item["segmentId"] for item in items]
            matched_entities = (
                [
                    {
                        "name": entity.get("name"),
                        "entityId": entity.get("entityId"),
                        "ontologyId": entity.get("ontologyId"),
                    }
                    for entity in result.get("matchedEntities", [])
                ]
                if result is not None
                else []
            )
            question_rows.append(
                {
                    "queryId": query["queryId"],
                    "query": query["query"],
                    "relevantSegmentIds": query["relevantSegmentIds"],
                    "expectedEntityIds": query.get("expectedEntityIds", []),
                    "expectedOntologyIds": query.get("expectedOntologyIds", []),
                    "success": run["error"] is None,
                    "error": run["error"],
                    "rankedSegmentIds": ranked_ids,
                    "matchedEntities": matched_entities,
                    **ranking_metrics(ranked_ids, query["relevantSegmentIds"]),
                }
            )

        metrics = {
            name: round(sum(row[name] for row in question_rows) / len(question_rows), 6)
            for name in METRIC_NAMES
        }
        metrics.update(
            {
                "zeroResultRate": round(
                    sum(not row["rankedSegmentIds"] for row in question_rows) / len(question_rows),
                    6,
                ),
                "zeroHitRate": round(
                    sum(row["firstRelevantRank"] is None for row in question_rows)
                    / len(question_rows),
                    6,
                ),
            }
        )
        modes[mode] = {
            "success": all(run["error"] is None for run in mode_runs),
            "queryCount": len(queries),
            "metricsSource": "cold",
            "metrics": metrics,
            "questions": question_rows,
            "latency": {
                "method": "nearest-rank",
                "coldDefinition": "该模式下每个问题的第一次查询",
                "warmDefinition": "同一实例后续三轮全部问题",
                "cold": latency_summary([run["elapsedMs"] for run in cold_runs]),
                "warm": latency_summary([run["elapsedMs"] for run in warm_runs]),
                "warmRounds": {
                    str(round_no): latency_summary(
                        [
                            run["elapsedMs"]
                            for run in warm_runs
                            if run["round"] == round_no
                        ]
                    )
                    for round_no in (1, 2, 3)
                },
            },
            "deterministicRanking": deterministic,
            "determinismComparisons": determinism_comparisons,
            "errors": [
                {
                    "phase": run["phase"],
                    "round": run["round"],
                    "queryId": run["queryId"],
                    "error": run["error"],
                }
                for run in mode_runs
                if run["error"] is not None
            ],
        }

    return {"modes": modes, "runs": runs}


def verify_citations(result: dict[str, Any], db: Database) -> list[str]:
    """校验单次响应的引用顺序、原文定位、元数据和离线答案编号。"""
    failures: list[str] = []
    items = result.get("items") or []
    citations = result.get("citations") or []
    answer = result.get("answer") or ""
    answer_mode = result.get("answerMode")
    expected_count = min(5 if answer_mode == "llm" else 3, len(items))
    if len(citations) != expected_count:
        failures.append(f"引用数量 {len(citations)}，应为 {expected_count}")

    for index, citation in enumerate(citations):
        label = f"引用[{index + 1}]"
        if index >= len(items):
            failures.append(f"{label} 没有对应检索 item")
            continue
        item = items[index]
        if citation.get("segmentId") != item.get("segmentId"):
            failures.append(f"{label} segmentId 与 item 顺序不一致")
        if citation.get("docId") != item.get("docId"):
            failures.append(f"{label} docId 与 item 顺序不一致")
        source = db.fetch_one(
            """
            SELECT s."segmentText" AS segmentText, s."pageNo" AS pageNo,
                   s."chapterPath" AS chapterPath, s."segmentNo" AS segmentNo,
                   d."status" AS docStatus
            FROM "SourceDoc_Segment" s
            LEFT JOIN "SourceDoc" d ON d."docId" = s."docId"
            WHERE s."segmentId"=:segment_id AND s."docId"=:doc_id
            """,
            {"segment_id": citation.get("segmentId"), "doc_id": citation.get("docId")},
        )
        if source is None:
            failures.append(f"{label} docId/segmentId 在数据库中不存在")
            continue
        if (source.get("docStatus") or "有效") != "有效":
            failures.append(f"{label} 来源文档不是有效状态")
        quote = citation.get("quote") or ""
        if not quote:
            failures.append(f"{label} quote 为空")
        elif len(quote) > 200:
            failures.append(f"{label} quote 超过 200 字")
        elif quote not in (source.get("segmentText") or ""):
            failures.append(f"{label} quote 不是原段落逐字子串")
        for field in ("pageNo", "chapterPath", "segmentNo"):
            if citation.get(field) != source.get(field):
                failures.append(f"{label} {field} 与数据库不一致")

    if answer_mode == "extractive" and items:
        references = [int(value) for value in re.findall(r"【(\d+)】", answer)]
        expected_references = list(range(1, len(citations) + 1))
        if references != expected_references:
            failures.append(f"答案编号 {references}，应为 {expected_references}")
        for index, citation in enumerate(citations, 1):
            if f"【{index}】" not in answer:
                failures.append(f"答案缺少【{index}】")
            if citation.get("quote") and citation["quote"] not in answer:
                failures.append(f"引用[{index}]摘句未出现在答案中")
    return failures


def validate_gold(gold: dict[str, Any], db: Database) -> dict[str, Any]:
    queries = gold.get("queries") or []
    query_ids = [query.get("queryId") for query in queries]
    relevant_ids = {
        segment_id
        for query in queries
        for segment_id in query.get("relevantSegmentIds") or []
    }
    invalid_segments = []
    for segment_id in sorted(relevant_ids):
        row = db.fetch_one(
            """
            SELECT s."segmentText" AS segmentText, d."status" AS docStatus
            FROM "SourceDoc_Segment" s
            LEFT JOIN "SourceDoc" d ON d."docId" = s."docId"
            WHERE s."segmentId"=:segment_id
            """,
            {"segment_id": segment_id},
        )
        if row is None:
            invalid_segments.append({"segmentId": segment_id, "reason": "段落不存在"})
        elif (row.get("docStatus") or "有效") != "有效":
            invalid_segments.append({"segmentId": segment_id, "reason": "来源文档非有效"})
        elif not (row.get("segmentText") or "").strip():
            invalid_segments.append({"segmentId": segment_id, "reason": "段落原文为空"})
    structural_errors = []
    if not queries:
        structural_errors.append("金标没有问题")
    if len(query_ids) != len(set(query_ids)) or any(not value for value in query_ids):
        structural_errors.append("queryId 为空或重复")
    for query in queries:
        relevant = query.get("relevantSegmentIds") or []
        if not query.get("query") or not relevant or len(relevant) != len(set(relevant)):
            structural_errors.append(f"{query.get('queryId')} 的问题或相关段落标注无效")
    return {
        "queryCount": len(queries),
        "uniqueRelevantSegmentCount": len(relevant_ids),
        "structuralErrors": structural_errors,
        "invalidSegments": invalid_segments,
        "passed": not structural_errors and not invalid_segments,
    }


def main() -> int:
    if not DATABASE_PATH.is_file():
        raise FileNotFoundError(f"缺少固定评测数据库：{DATABASE_PATH}")
    gold_bytes = GOLD_PATH.read_bytes()
    gold = json.loads(gold_bytes.decode("utf-8"))
    before = {path: file_state(path) for path in PROTECTED_PATHS}

    with tempfile.TemporaryDirectory(prefix="kg-stage1-eval-") as temporary:
        temp_dir = Path(temporary)
        snapshot_path = temp_dir / "grain_oil.db"
        shutil.copy2(DATABASE_PATH, snapshot_path)
        snapshot_hash = sha256(snapshot_path)
        settings = Settings(
            database_url=f"sqlite:///{snapshot_path.as_posix()}",
            auto_seed=False,
            graph_backend="relational",
            data_dir=temp_dir / "data",
            rag_embedding_backend="hash",
            rag_embedding_base_url=None,
            rag_embedding_api_key=None,
            rag_llm_base_url=None,
            rag_llm_api_key=None,
        )
        db = Database(settings.database_url, settings.seed_dir)
        db.initialize()
        scope = ScopeService(db)
        scope.initialize()
        scope_stats = scope.stats()
        gold_validation = validate_gold(gold, db)
        service = RagService(db, KnowledgeService(db), settings)
        expected_config = {
            "embeddingBackend": "hash",
            "rerankMode": "lexical",
            "llmBaseUrl": "",
            "llmApiKey": "",
            "llmEntityExtraction": "false",
        }
        for key, expected in expected_config.items():
            if service.config.get(key) != expected:
                raise RuntimeError(
                    f"临时 RAG 配置不符合隔离口径：{key}={service.config.get(key)!r}"
                )
        service.refresh(force=True)
        evaluation = evaluate_retrieval(service, gold)
        modes = evaluation["modes"]

        citation_failures = []
        checked_responses = 0
        checked_citations = 0
        for run in evaluation["runs"]:
            if run["result"] is None:
                continue
            checked_responses += 1
            checked_citations += len(run["result"].get("citations") or [])
            errors = verify_citations(run["result"], db)
            if errors:
                citation_failures.append(
                    {
                        "mode": run["mode"],
                        "phase": run["phase"],
                        "round": run["round"],
                        "queryId": run["queryId"],
                        "errors": errors,
                    }
                )
        passed_responses = checked_responses - len(citation_failures)
        citation_rate = passed_responses / checked_responses if checked_responses else 0.0
        deterministic = all(mode["deterministicRanking"] for mode in modes.values())
        db.engine.dispose()

    after = {path: file_state(path) for path in PROTECTED_PATHS}
    isolation_files = {
        str(path.relative_to(ROOT)).replace("\\", "/"): {
            "before": before[path],
            "after": after[path],
            "unchanged": before[path] == after[path],
        }
        for path in PROTECTED_PATHS
    }
    isolation_passed = all(item["unchanged"] for item in isolation_files.values())
    all_modes_succeeded = set(modes) == set(RagService.SUPPORTED_MODES) and all(
        mode["success"] for mode in modes.values()
    )
    all_hit_at_10 = bool(modes) and all(
        mode["metrics"]["hit@10"] == 1.0 for mode in modes.values()
    )
    hard_gates = {
        "fiveModesSucceeded": {
            "passed": all_modes_succeeded,
            "expected": sorted(RagService.SUPPORTED_MODES),
            "actual": {mode: detail["success"] for mode, detail in modes.items()},
        },
        "seedSmokeHitAt10": {
            "passed": all_hit_at_10,
            "expected": 1.0,
            "actual": {mode: detail["metrics"]["hit@10"] for mode, detail in modes.items()},
        },
        "citationVerificationRate": {
            "passed": checked_responses > 0 and citation_rate == 1.0,
            "expected": 1.0,
            "actual": round(citation_rate, 6),
        },
        "deterministicRanking": {
            "passed": deterministic,
            "expected": True,
            "actual": {mode: detail["deterministicRanking"] for mode, detail in modes.items()},
        },
        "goldSegmentsValid": {
            "passed": gold_validation["passed"],
            "expected": True,
            "actual": gold_validation["passed"],
        },
        "dimensionCount": {
            "passed": scope_stats["dimensionCount"] == EXPECTED_DIMENSION_COUNT,
            "expected": EXPECTED_DIMENSION_COUNT,
            "actual": scope_stats["dimensionCount"],
        },
    }
    report = {
        "schemaVersion": "1.0.0",
        "stage": "Stage 1",
        "datasetTier": "seed-smoke",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "businessQualityGate": "NOT_ASSESSED",
        "productionPerformanceGate": "NOT_ASSESSED",
        "boundary": "种子冒烟基线不代表真实业务召回质量，也不代表并发 50 的生产性能验收。",
        "productionTargetsForLaterAssessment": {
            "commonQuery": {"p95Ms": 3000, "concurrency": 50},
            "fullTextSearch": {"p95Ms": 1000},
        },
        "environment": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
        },
        "configuration": {
            "graphBackend": settings.graph_backend,
            "embeddingBackend": service.config["embeddingBackend"],
            "embeddingModel": service.embedder_name,
            "rerankMode": service.config["rerankMode"],
            "llmEnabled": False,
            "llmEntityExtraction": False,
            "topK": 10,
            "answer": True,
            "warmRounds": 3,
            "metricsSource": "cold",
        },
        "inputs": {
            "database": {
                "path": "data/grain_oil.db",
                "sha256": before[DATABASE_PATH]["sha256"],
            },
            "snapshotBeforeInitialization": {"sha256": snapshot_hash},
            "gold": {
                "path": "ontology_ly/capability/stage1_retrieval_gold.json",
                "sha256": hashlib.sha256(gold_bytes).hexdigest(),
            },
        },
        "scope": scope_stats,
        "goldValidation": gold_validation,
        "modes": modes,
        "citations": {
            "level": "response",
            "checkedResponses": checked_responses,
            "passedResponses": passed_responses,
            "failedResponses": len(citation_failures),
            "checkedCitations": checked_citations,
            "verificationRate": round(citation_rate, 6),
            "failures": citation_failures,
        },
        "deterministicRanking": deterministic,
        "evaluationIsolation": {"passed": isolation_passed, "files": isolation_files},
        "hardGates": hard_gates,
        "allHardGatesPassed": all(gate["passed"] for gate in hard_gates.values()),
    }
    report["passed"] = report["allHardGatesPassed"] and isolation_passed
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"报告：{REPORT_PATH}")
    for mode, detail in modes.items():
        metrics = detail["metrics"]
        cold = detail["latency"]["cold"]
        warm = detail["latency"]["warm"]
        print(
            f"{mode}: Hit@10={metrics['hit@10']:.3f}, Recall@10={metrics['recall@10']:.3f}, "
            f"MRR@10={metrics['mrr@10']:.3f}, nDCG@10={metrics['ndcg@10']:.3f}, "
            f"cold P95={cold['p95Ms']}ms, warm P95={warm['p95Ms']}ms"
        )
    print(
        f"引用核验率={citation_rate:.3f}，确定性={deterministic}，"
        f"硬门禁={report['allHardGatesPassed']}，隔离={isolation_passed}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
