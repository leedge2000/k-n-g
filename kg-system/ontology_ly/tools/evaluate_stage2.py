# -*- coding: utf-8 -*-
"""阶段 2 技术冒烟评测：三态指标、解释完整性与隔离硬门。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import shutil
import sqlite3
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import issue_token
from app.config import Settings
from app.main import create_app

SMOKE_PATH = ROOT / "ontology_ly" / "capability" / "stage2_reasoning_smoke.json"
DATABASE_PATH = ROOT / "data" / "grain_oil.db"
REPORT_PATH = Path(__file__).resolve().parent / "stage2_report.json"
OUTCOMES = ("APPLICABLE", "NOT_APPLICABLE", "UNABLE")
TECHNICAL_ACTOR = "technical-smoke"
DISCLAIMER = "辅助研究，不构成法律意见或官方解释"


def _ratio(numerator: int, denominator: int) -> float | None:
    """空分母保留为 None，避免把未评测误报为零或满分。"""
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def summarize_suite(rows: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(rows)
    expected_unable = [row for row in rows if row.get("expectedOutcome") == "UNABLE"]
    actual_applicable = [row for row in rows if row.get("actualOutcome") == "APPLICABLE"]
    confusion_matrix = {
        expected: {actual: 0 for actual in OUTCOMES}
        for expected in OUTCOMES
    }
    for row in rows:
        expected = row.get("expectedOutcome")
        actual = row.get("actualOutcome")
        if expected in confusion_matrix and actual in OUTCOMES:
            confusion_matrix[expected][actual] += 1

    return {
        "caseCount": case_count,
        "threeStateAgreement": _ratio(
            sum(row.get("expectedOutcome") == row.get("actualOutcome") for row in rows),
            case_count,
        ),
        "unableRecognitionRate": _ratio(
            sum(row.get("actualOutcome") == "UNABLE" for row in expected_unable),
            len(expected_unable),
        ),
        "fatalMisjudgementRate": _ratio(
            sum(row.get("actualOutcome") == "APPLICABLE" for row in expected_unable),
            len(expected_unable),
        ),
        "explanationUsabilityRate": _ratio(
            sum(row.get("explanationUsable") is True for row in rows), case_count
        ),
        "scopeLabelCompletenessRate": _ratio(
            sum(row.get("scopeLabelled") is True for row in rows), case_count
        ),
        "applicableProofCompletenessRate": _ratio(
            sum(row.get("applicableProofComplete") is True for row in actual_applicable),
            len(actual_applicable),
        ),
        "expectedReasonsMatched": bool(rows)
        and all(row.get("expectedReasonMatched") is True for row in rows),
        "outputShapeMatched": bool(rows)
        and all(row.get("outputShapeMatched", True) is True for row in rows),
        "confusionMatrix": confusion_matrix,
    }


def _metric_gate(suites: dict[str, dict[str, Any]], metric: str, expected: float) -> dict[str, Any]:
    values = {name: metrics.get(metric) for name, metrics in suites.items()}
    return {
        "passed": bool(values) and all(value == expected for value in values.values()),
        "expected": expected,
        "actualBySuite": values,
    }


def build_hard_gates(
    suite_metrics: dict[str, dict[str, Any]],
    deterministic: bool,
    isolation_passed: bool,
    nemo_execution: dict[str, Any],
    source_hashes_valid: bool,
    activation_boundary: bool,
) -> dict[str, Any]:
    gates = {
        "deterministicOutput": {"passed": deterministic is True},
        "formalDataUnchanged": {"passed": isolation_passed is True},
        "realNemoExecution": {
            "passed": (
                nemo_execution.get("version") == "0.10.1"
                and int(nemo_execution.get("executed") or 0) > 0
                and int(nemo_execution.get("successful") or 0) > 0
            ),
            "actual": dict(nemo_execution),
        },
        "sourceHashesValid": {"passed": source_hashes_valid is True},
        "activationBoundary": {"passed": activation_boundary is True},
        "threeStateAgreement": _metric_gate(suite_metrics, "threeStateAgreement", 1.0),
        "unableRecognition": _metric_gate(suite_metrics, "unableRecognitionRate", 1.0),
        "fatalMisjudgement": _metric_gate(suite_metrics, "fatalMisjudgementRate", 0.0),
        "explanationUsability": _metric_gate(suite_metrics, "explanationUsabilityRate", 1.0),
        "scopeLabelCompleteness": _metric_gate(suite_metrics, "scopeLabelCompletenessRate", 1.0),
        "applicableProofCompleteness": _metric_gate(
            suite_metrics, "applicableProofCompletenessRate", 1.0
        ),
        "expectedReasons": {
            "passed": bool(suite_metrics)
            and all(metrics.get("expectedReasonsMatched") is True for metrics in suite_metrics.values()),
            "actualBySuite": {
                name: metrics.get("expectedReasonsMatched")
                for name, metrics in suite_metrics.items()
            },
        },
        "statementOutputShape": {
            "passed": bool(suite_metrics)
            and all(metrics.get("outputShapeMatched") is True for metrics in suite_metrics.values()),
            "actualBySuite": {
                name: metrics.get("outputShapeMatched")
                for name, metrics in suite_metrics.items()
            },
        },
    }
    gates["overall"] = {"passed": all(gate["passed"] for gate in gates.values())}
    return gates


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


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "p50Ms": None, "p95Ms": None, "maxMs": None}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = max(1, math.ceil(fraction * len(ordered)))
        return round(ordered[position - 1], 3)

    return {
        "samples": len(values),
        "p50Ms": percentile(0.50),
        "p95Ms": percentile(0.95),
        "maxMs": round(max(values), 3),
    }


def _auth(settings: Settings) -> dict[str, str]:
    token = issue_token("technical-smoke", "ROLE-04", settings.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


def _require_ok(response: Any, operation: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise RuntimeError(f"{operation} 返回 {response.status_code}: {response.text}")
    return response.json()


def _statement_and_condition(db: Any, clause_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    statement = db.fetch_one(
        'SELECT * FROM "NormativeStatement" WHERE "clauseId"=:cid', {"cid": clause_id}
    )
    if not statement:
        raise RuntimeError(f"技术情形缺少 statement：{clause_id}")
    condition = db.fetch_one(
        'SELECT * FROM "ClauseCondition" WHERE "conditionId"=:cid AND "statementId"=:sid',
        {"cid": statement["conditionId"], "sid": statement["statementId"]},
    )
    if not condition:
        raise RuntimeError(f"技术情形缺少主条件：{statement['statementId']}")
    return statement, condition


def _mutate_case(db: Any, setup: str) -> None:
    """在一次性快照内构造技术故障；不向正式库写入任何状态。"""
    if setup in {"confirmed", "draft", "engine_failure", "missing_proof_chain"}:
        return

    statement, condition = _statement_and_condition(db, "POL0001-C001")
    with db.engine.begin() as conn:
        if setup == "collection_missing":
            db.update("ClauseCondition", condition["conditionId"], {
                "leftOperand": "facts.tags",
                "comparator": "∈",
                "rightValue": "早籼稻,中晚籼稻",
            }, conn, actor=TECHNICAL_ACTOR)
            return
        if setup == "invalid_numeric":
            db.update("ClauseCondition", condition["conditionId"], {
                "leftOperand": "facts.price",
                "comparator": ">",
                "rightValue": "0",
                "rightUnitId": "元/50公斤",
            }, conn, actor=TECHNICAL_ACTOR)
            return
        if setup == "unknown_comparator":
            db.update("ClauseCondition", condition["conditionId"], {
                "comparator": "≈",
            }, conn, actor=TECHNICAL_ACTOR)
            return
        if setup == "exception_unknown":
            condition_id = "CND-TECH-EX-UNKNOWN"
            db.insert("ClauseCondition", {
                "conditionId": condition_id,
                "statementId": statement["statementId"],
                "parentConditionId": None,
                "operator": "LEAF",
                "leftOperand": "facts.localReserveActive",
                "comparator": "=",
                "rightValue": "True",
                "factSource": "USER_INPUT",
            }, conn, actor=TECHNICAL_ACTOR)
            db.insert("ClauseException", {
                "exceptionId": "EXC-TECH-UNKNOWN",
                "statementId": statement["statementId"],
                "conditionId": condition_id,
                "exceptionType": "技术例外",
                "note": "地方收储启动时除外",
            }, conn, actor=TECHNICAL_ACTOR)
            return
        if setup == "multi_statement":
            duplicate_statement = dict(statement)
            duplicate_statement.update({
                "statementId": "STM-TECH-MULTI-C001",
                "conditionId": "CND-TECH-MULTI-C001",
                "confirmationStatus": "DRAFT",
                "confirmedBy": None,
                "confirmedAt": None,
            })
            duplicate_condition = dict(condition)
            duplicate_condition.update({
                "conditionId": "CND-TECH-MULTI-C001",
                "statementId": "STM-TECH-MULTI-C001",
            })
            db.insert("NormativeStatement", duplicate_statement, conn, actor=TECHNICAL_ACTOR)
            db.insert("ClauseCondition", duplicate_condition, conn, actor=TECHNICAL_ACTOR)
            return
        if setup in {"priority_override", "priority_cycle"}:
            _, higher_condition = _statement_and_condition(db, "POL0001-C002")
            db.update("ClauseCondition", higher_condition["conditionId"], {
                "rightValue": "早籼稻",
            }, conn, actor=TECHNICAL_ACTOR)
            db.insert("ClauseRelation", {
                "relationId": "REL-TECH-C002-C001",
                "fromClauseId": "POL0001-C002",
                "toClauseId": "POL0001-C001",
                "relationType": "PREVAILS_OVER",
                "priorityBasis": "NEWER",
                "resolved": 0,
            }, conn, actor=TECHNICAL_ACTOR)
            if setup == "priority_cycle":
                db.insert("ClauseRelation", {
                    "relationId": "REL-TECH-C001-C002",
                    "fromClauseId": "POL0001-C001",
                    "toClauseId": "POL0001-C002",
                    "relationType": "PREVAILS_OVER",
                    "priorityBasis": "SPECIAL",
                    "resolved": 0,
                }, conn, actor=TECHNICAL_ACTOR)
            return
    raise ValueError(f"未知技术情形 setup：{setup}")


def _confirm_compiled(db: Any) -> None:
    """仅修改临时快照；CONFIRMED 是技术开关，不是专家确认或业务金标。"""
    statements = db.fetch_all(
        'SELECT ns.* FROM "NormativeStatement" ns '
        'JOIN "Policy_Clause" pc ON ns."clauseId"=pc."clauseId" '
        'WHERE pc."policyId"=\'POL0001\' ORDER BY ns."statementId"'
    )
    with db.engine.begin() as conn:
        for statement in statements:
            statement_id = str(statement["statementId"])
            testcase_id = "RTC-TECH-" + statement_id
            db.insert("RuleTestCase", {
                "testcaseId": testcase_id,
                "statementId": statement_id,
                "scenarioName": "technical-smoke",
                "facts": json.dumps({"targetObject": "早籼稻"}, ensure_ascii=False),
                "expectedOutcome": "UNABLE",
                "expectedReason": "technical-smoke，不是专家金标",
                "authoredBy": TECHNICAL_ACTOR,
            }, conn, actor=TECHNICAL_ACTOR)
            db.update("NormativeStatement", statement_id, {
                "confirmationStatus": "CONFIRMED",
                "confirmedBy": TECHNICAL_ACTOR,
                "confirmedAt": "2026-11-14T00:00:00Z",
            }, conn, actor=TECHNICAL_ACTOR)
            rule = db.fetch_one(
                'SELECT * FROM "CompiledRule" WHERE "statementId"=:sid',
                {"sid": statement_id}, conn,
            )
            if not rule:
                raise RuntimeError(f"编译结果缺失：{statement_id}")
            db.update("CompiledRule", rule["ruleId"], {
                "confirmationStatus": "CONFIRMED",
                "backTranslationMatch": 1,
                "testcaseIds": json.dumps([testcase_id]),
            }, conn, actor=TECHNICAL_ACTOR)


def _prepare_case(client: TestClient, settings: Settings, setup: str) -> dict[str, Any]:
    headers = _auth(settings)
    extracted = _require_ok(client.post(
        "/api/policies/extract-2b", json={"policyId": "POL0001"}, headers=headers
    ), "extract-2b")
    _mutate_case(client.app.state.db, setup)
    compiled = _require_ok(client.post(
        "/api/policies/compile-2c", json={"policyId": "POL0001"}, headers=headers
    ), "compile-2c")
    if setup != "draft":
        _confirm_compiled(client.app.state.db)
    return {
        "extract": extracted,
        "compile": compiled,
        "temporaryConfirmation": setup != "draft",
        "confirmationActor": TECHNICAL_ACTOR if setup != "draft" else None,
        "expertConfirmed": False,
    }


def _proof_complete(outcome: dict[str, Any], clause_texts: dict[str, str]) -> bool:
    chain = outcome.get("chain") or []
    required = {
        "step", "ruleId", "statementId", "policyId", "clauseId", "citation",
        "clausePath", "clauseNo", "factRef", "quote", "engine",
    }
    return (
        outcome.get("proofChain") == chain
        and [step.get("step") for step in chain] == [1, 2, 3, 4, 5, 6]
        and all(
            required.issubset(step)
            and step.get("statementId") == outcome.get("statementId")
            and step.get("ruleId") == outcome.get("ruleId")
            and step.get("citation")
            and step.get("quote")
            and step.get("quote") == clause_texts.get(str(step.get("clauseId")))
            for step in chain
        )
    )


def _reset_stage2_assets(snapshot_path: Path) -> None:
    """把副本库还原到 2b 抽取前的派生资产状态。

    正式库中的 POL0001 已有 CONFIRMED 陈述/规则与测试用例，会触发受保护资产拦截；
    评测对象是「从条款重新抽取并编译」，因此在副本上清空这些派生行后重跑。
    只作用于 case 快照，不触碰正式库。
    """
    with sqlite3.connect(snapshot_path) as conn:
        statement_ids = [
            row[0] for row in conn.execute(
                'SELECT s."statementId" FROM "NormativeStatement" s '
                'JOIN "Policy_Clause" c ON c."clauseId"=s."clauseId" '
                'WHERE c."policyId"=\'POL0001\''
            )
        ]
        if statement_ids:
            placeholders = ",".join("?" for _ in statement_ids)
            conn.execute(
                f'DELETE FROM "RuleTestCase" WHERE "statementId" IN ({placeholders})',
                statement_ids,
            )
            for table in ("CompiledRule", "RuleParameter", "ClauseConsequence",
                          "ClauseException", "ClauseCondition"):
                conn.execute(
                    f'DELETE FROM "{table}" WHERE "statementId" IN ({placeholders})',
                    statement_ids,
                )
        conn.execute(
            'DELETE FROM "NormativeStatement" WHERE "clauseId" IN ('
            'SELECT "clauseId" FROM "Policy_Clause" WHERE "policyId"=\'POL0001\')'
        )
        conn.commit()


def _execute_case(
    suite_name: str,
    case: dict[str, Any],
    database_path: Path,
    case_dir: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    case_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = case_dir / "snapshot.db"
    shutil.copy2(database_path, snapshot_path)
    snapshot_hash = sha256(snapshot_path)
    _reset_stage2_assets(snapshot_path)
    settings = Settings(
        database_url=f"sqlite:///{snapshot_path.as_posix()}",
        jwt_secret="technical-smoke-secret-with-at-least-32-bytes",
        auto_seed=False,
        graph_backend="relational",
        data_dir=case_dir / "data",
    )
    telemetry = {"requests": 0, "executed": 0, "successful": 0, "failed": 0}
    responses: dict[str, dict[str, Any] | None] = {"cold": None, "warm": None}
    elapsed: dict[str, float] = {}
    errors: list[dict[str, Any]] = []

    with TestClient(create_app(settings)) as client:
        preparation = _prepare_case(client, settings, str(case["setup"]))
        service = client.app.state.conclusion
        original_run = service._builder.run

        def measured_run(derived: Any, rules_text: str) -> Any:
            telemetry["executed"] += 1
            if case["setup"] == "engine_failure":
                telemetry["failed"] += 1
                raise RuntimeError("technical-smoke engine failure")
            try:
                result = original_run(derived, rules_text)
            except Exception:
                telemetry["failed"] += 1
                raise
            telemetry["successful"] += 1
            return result

        service._builder.run = measured_run
        if case["setup"] == "missing_proof_chain":
            original_verdict = service._verdict

            def remove_chain(*args: Any, **kwargs: Any) -> dict[str, Any]:
                result = original_verdict(*args, **kwargs)
                result["proofChain"] = []
                result["chain"] = []
                return result

            service._verdict = remove_chain

        headers = _auth(settings)
        for phase in ("cold", "warm"):
            telemetry["requests"] += 1
            started = perf_counter()
            response = client.post("/api/policy/judge", json={
                "asOf": case["asOf"],
                "facts": case["facts"],
                "maxHops": 3,
                "question": "条款是否适用",
            }, headers=headers)
            elapsed[phase] = (perf_counter() - started) * 1000
            if response.status_code == 200:
                responses[phase] = response.json()
            else:
                errors.append({
                    "phase": phase,
                    "statusCode": response.status_code,
                    "detail": response.text,
                })

        clause_texts = {
            str(row["clauseId"]): str(row["clauseText"])
            for row in client.app.state.db.fetch_all(
                'SELECT "clauseId","clauseText" FROM "Policy_Clause"'
            )
        }
        effective_total = client.app.state.policy.effective(
            str(case["asOf"]), limit=1000
        )["total"]

    cold = responses["cold"]
    warm = responses["warm"]
    outcomes = list(cold.get("outcomes") or []) if cold else []
    matching = [item for item in outcomes if item.get("clauseId") == case["targetClauseId"]]
    target = matching[0] if matching else {}
    actual_outcome = target.get("outcome")
    reason = str(target.get("reason") or "")
    expected_count = int(case.get("expectedStatementCount") or 1)
    applicable_outputs = [item for item in outcomes if item.get("outcome") == "APPLICABLE"]
    deterministic = cold is not None and warm is not None and cold == warm
    activation_violations = [
        {
            "phase": phase,
            "statementId": item.get("statementId"),
            "officiallyActivated": item.get("officiallyActivated"),
            "activationEvidenceId": item.get("activationEvidenceId"),
        }
        for phase, body in responses.items()
        for item in ((body or {}).get("outcomes") or [])
        if item.get("officiallyActivated") != "UNKNOWN" or item.get("activationEvidenceId") is not None
    ]
    row = {
        "suite": suite_name,
        "caseId": case["caseId"],
        "title": case["title"],
        "setup": case["setup"],
        "targetClauseId": case["targetClauseId"],
        "expectedOutcome": case["expectedOutcome"],
        "actualOutcome": actual_outcome,
        "expectedReasonContains": case["expectedReasonContains"],
        "expectedReasonMatched": str(case["expectedReasonContains"]) in reason,
        "outputShapeMatched": len(matching) == expected_count,
        "outputCount": len(matching),
        "expectedStatementCount": expected_count,
        "statementId": target.get("statementId"),
        "ruleId": target.get("ruleId"),
        "reason": reason,
        "explanationUsable": bool(
            target
            and reason
            and target.get("citation")
            and target.get("disclaimer") == DISCLAIMER
            and target.get("scopeNote")
            and (actual_outcome == "UNABLE" or target.get("chain"))
        ),
        "scopeLabelled": bool(
            target.get("scopeNote")
            and f"本次实际检索到 {effective_total} 份" in str(target.get("scopeNote"))
            and "查询范围" in str(target.get("scopeNote"))
            and "不代表官方完整政策清单" in str(target.get("scopeNote"))
        ),
        "applicableProofComplete": (
            all(_proof_complete(item, clause_texts) for item in applicable_outputs)
            if actual_outcome == "APPLICABLE" else None
        ),
        "activationBoundaryKept": not activation_violations,
        "activationCheckedOutcomes": sum(
            len((body or {}).get("outcomes") or []) for body in responses.values()
        ),
        "activationViolations": activation_violations,
        "deterministic": deterministic,
        "coldResponseSha256": _json_sha256(cold) if cold is not None else None,
        "warmResponseSha256": _json_sha256(warm) if warm is not None else None,
        "coldElapsedMs": round(elapsed.get("cold", 0.0), 3),
        "warmElapsedMs": round(elapsed.get("warm", 0.0), 3),
        "snapshotBeforeInitialization": {"sha256": snapshot_hash},
        "preparation": preparation,
        "errors": errors,
        "outcomes": outcomes,
    }
    return row, telemetry


def _validate_sources(smoke: dict[str, Any]) -> dict[str, Any]:
    files = []
    for item in smoke["source"]["files"]:
        path = ROOT / item["path"]
        actual = sha256(path) if path.is_file() else None
        files.append({
            "path": item["path"],
            "expectedSha256": item["sha256"],
            "actualSha256": actual,
            "matched": actual == item["sha256"],
        })
    return {"passed": bool(files) and all(item["matched"] for item in files), "files": files}


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def run_evaluation(
    smoke_path: str | Path = SMOKE_PATH,
    database_path: str | Path = DATABASE_PATH,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    smoke_path = Path(smoke_path)
    database_path = Path(database_path)
    if not smoke_path.is_file() or not database_path.is_file():
        raise FileNotFoundError("阶段 2 固定情形或正式数据库不存在")
    smoke_bytes = smoke_path.read_bytes()
    smoke = json.loads(smoke_bytes.decode("utf-8"))
    if smoke.get("datasetTier") != "technical-smoke":
        raise ValueError("阶段 2 评测只接受 technical-smoke 固定输入")

    source_validation = _validate_sources(smoke)
    protected_paths = [database_path, smoke_path]
    protected_paths.extend(ROOT / item["path"] for item in smoke["source"]["files"])
    protected_paths = list(dict.fromkeys(protected_paths))
    before = {path: file_state(path) for path in protected_paths}

    with ExitStack() as stack:
        if workspace is None:
            workspace_path = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="kg-stage2-eval-")))
        else:
            workspace_path = Path(workspace)
            workspace_path.mkdir(parents=True, exist_ok=True)

        suites: dict[str, Any] = {}
        total_telemetry = {"requests": 0, "executed": 0, "successful": 0, "failed": 0}
        for suite_name, suite in smoke["suites"].items():
            rows = []
            for case in suite["cases"]:
                row, telemetry = _execute_case(
                    suite_name, case, database_path, workspace_path / suite_name / case["caseId"]
                )
                rows.append(row)
                for key in total_telemetry:
                    total_telemetry[key] += telemetry[key]
            suites[suite_name] = {
                "title": suite["title"],
                "metrics": summarize_suite(rows),
                "latency": {
                    "method": "nearest-rank",
                    "productionAssessment": "NOT_ASSESSED",
                    "cold": _latency_summary([row["coldElapsedMs"] for row in rows]),
                    "warm": _latency_summary([row["warmElapsedMs"] for row in rows]),
                },
                "cases": rows,
            }

    after = {path: file_state(path) for path in protected_paths}
    isolation_files = {
        _display_path(path): {
            "before": before[path],
            "after": after[path],
            "unchanged": before[path] == after[path],
        }
        for path in protected_paths
    }
    isolation_passed = all(item["unchanged"] for item in isolation_files.values())
    rows = [row for suite in suites.values() for row in suite["cases"]]
    deterministic = bool(rows) and all(row["deterministic"] for row in rows)
    activation_violations = [
        {"caseId": row["caseId"], **violation}
        for row in rows for violation in row["activationViolations"]
    ]
    activation_boundary = {
        "passed": not activation_violations,
        "checkedOutcomes": sum(row["activationCheckedOutcomes"] for row in rows),
        "violations": activation_violations,
        "rule": "officiallyActivated 必须为 UNKNOWN，activationEvidenceId 必须为 null",
    }
    try:
        nemo_version = importlib.metadata.version("nemo-python")
    except importlib.metadata.PackageNotFoundError:
        nemo_version = None
    nemo_execution = {
        "version": nemo_version,
        **total_telemetry,
        "skipped": total_telemetry["requests"] - total_telemetry["executed"],
    }
    suite_metrics = {name: suite["metrics"] for name, suite in suites.items()}
    hard_gates = build_hard_gates(
        suite_metrics,
        deterministic=deterministic,
        isolation_passed=isolation_passed,
        nemo_execution=nemo_execution,
        source_hashes_valid=source_validation["passed"],
        activation_boundary=activation_boundary["passed"],
    )
    all_rows = rows
    report = {
        "schemaVersion": "1.0.0",
        "stage": "Stage 2",
        "datasetTier": "technical-smoke",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "businessQualityGate": "NOT_ASSESSED",
        "productionPerformanceGate": "NOT_ASSESSED",
        "boundary": "技术冒烟只验证推理契约与保护边界；临时 CONFIRMED 不是专家确认，预期不是独立业务金标，耗时不是生产性能验收。",
        "environment": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "nemo": nemo_version,
        },
        "configuration": {
            "graphBackend": "relational",
            "autoSeed": False,
            "api": "/api/policy/judge",
            "formalOnly": True,
            "maxHops": 3,
            "metricsSource": "cold",
            "warmRounds": 1,
            "snapshotPerCase": True,
        },
        "temporaryConfirmation": {
            "actor": TECHNICAL_ACTOR,
            "scope": "一次性 SQLite 快照",
            "expertConfirmed": False,
            "businessGold": False,
        },
        "inputs": {
            "database": {
                "path": _display_path(database_path),
                "sha256": before[database_path]["sha256"],
            },
            "smoke": {
                "path": _display_path(smoke_path),
                "sha256": hashlib.sha256(smoke_bytes).hexdigest(),
            },
            "snapshotsBeforeInitialization": [
                {"caseId": row["caseId"], **row["snapshotBeforeInitialization"]}
                for row in rows
            ],
        },
        "sourceValidation": source_validation,
        "suites": suites,
        "deterministicOutput": deterministic,
        "nemoExecution": nemo_execution,
        "activationBoundary": activation_boundary,
        "latency": {
            "method": "nearest-rank",
            "productionAssessment": "NOT_ASSESSED",
            "cold": _latency_summary([row["coldElapsedMs"] for row in all_rows]),
            "warm": _latency_summary([row["warmElapsedMs"] for row in all_rows]),
        },
        "evaluationIsolation": {"passed": isolation_passed, "files": isolation_files},
        "hardGates": hard_gates,
        "allHardGatesPassed": hard_gates["overall"]["passed"],
    }
    report["passed"] = report["allHardGatesPassed"] and isolation_passed
    return report


def main() -> int:
    report = run_evaluation()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"报告：{REPORT_PATH}")
    for name, suite in report["suites"].items():
        metrics = suite["metrics"]
        print(
            f"{name}: 三态一致率={metrics['threeStateAgreement']}, "
            f"UNABLE识别率={metrics['unableRecognitionRate']}, "
            f"致命误判率={metrics['fatalMisjudgementRate']}"
        )
    print(
        f"确定性={report['deterministicOutput']}，隔离={report['evaluationIsolation']['passed']}，"
        f"硬门禁={report['allHardGatesPassed']}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
