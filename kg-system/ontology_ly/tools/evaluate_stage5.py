# -*- coding: utf-8 -*-
"""阶段 5 运营隔离评测：指标口径、告警生命周期、增量入库、演进状态机与 G5 正负门。"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import issue_token
from app.catalog import KNOWLEDGE_TYPES, SPECS
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.ontology import OntologyService
from app.ops import METRICS, OpsError, OpsService, judge_status
from app.scope import ScopeService

CASES_PATH = ROOT / "ontology_ly" / "capability" / "stage5_ops_smoke.json"
DATABASE_PATH = ROOT / "data" / "grain_oil.db"
REPORT_PATH = Path(__file__).resolve().parent / "stage5_report.json"
TECHNICAL_ACTOR = "technical-smoke"
JWT_SECRET = "stage5-technical-smoke-secret-with-32-bytes"

GROUP_METRICS = {
    "metric": "metricAgreement",
    "alert": "alertLifecycleAgreement",
    "ingest": "ingestAgreement",
    "evolution": "evolutionAgreement",
    "rollback": "rollbackRestoreIntegrity",
    "gate": "gateNegativeDetectionRate",
    "permission": "permissionAgreement",
    "knowledge": "knowledgeTablesUnchanged",
}
METRIC_KEYS = ("casePassRate", *GROUP_METRICS.values())


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _check_matched(check: dict[str, Any]) -> bool:
    if "matched" in check:
        return check["matched"] is True
    return check.get("expected") == check.get("actual")


def summarize_cases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "caseCount": len(rows),
        "casePassRate": _ratio(sum(1 for row in rows if row.get("passed") is True), len(rows)),
    }
    for group, metric in GROUP_METRICS.items():
        checks = [check for row in rows for check in (row.get("checks") or []) if check.get("group") == group]
        metrics[metric] = _ratio(sum(1 for check in checks if _check_matched(check)), len(checks))
    return metrics


def build_hard_gates(
    metrics: dict[str, Any],
    deterministic: bool,
    isolation_passed: bool,
    source_hashes_valid: bool,
) -> dict[str, Any]:
    gates: dict[str, Any] = {
        "deterministicOutput": {"passed": deterministic is True},
        "formalDataUnchanged": {"passed": isolation_passed is True},
        "sourceHashesValid": {"passed": source_hashes_valid is True},
        "isolatedDataset": {"passed": isolation_passed is True},
    }
    for metric in METRIC_KEYS:
        gates[metric] = {"passed": metrics.get(metric) == 1.0, "expected": 1.0, "actual": metrics.get(metric)}
    gates["overall"] = {"passed": all(gate["passed"] for gate in gates.values())}
    return gates


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _services(database_path: Path) -> tuple[Database, ScopeService, OntologyService, OpsService]:
    db = Database(f"sqlite:///{database_path}", ROOT / "seed")
    db.initialize()
    scope = ScopeService(db)
    scope.initialize()
    ontology = OntologyService(db, ROOT / "seed")
    ontology.initialize()
    ops = OpsService(db, scope)
    ops.initialize()
    return db, scope, ontology, ops


def _apply_setup(db: Database, items: list[dict[str, Any]]) -> None:
    for item in items:
        values = dict(item["values"])
        for field, selector in (item.get("fill") or {}).items():
            row = db.fetch_one(
                f'SELECT "{selector["column"]}" AS value FROM "{selector["table"]}" LIMIT 1'
            ) or {}
            values[field] = row.get("value")
        with db.engine.begin() as conn:
            db.insert(item["table"], values, conn, actor=TECHNICAL_ACTOR)


def _cleanup(db: Database, item: dict[str, Any]) -> None:
    clauses = " AND ".join(f'"{key}"=:{key}' for key in item["where"])
    with db.engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{item["table"]}" WHERE {clauses}'), item["where"])


def _knowledge_counts(db: Database) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entity_type in KNOWLEDGE_TYPES:
        row = db.fetch_one(f'SELECT COUNT(*) AS total FROM "{SPECS[entity_type].table}"') or {}
        counts[entity_type] = int(row.get("total") or 0)
    return counts


def _alert(db: Database, code: str, active_only: bool = False) -> dict[str, Any] | None:
    status = ' AND "status" IN (\'OPEN\',\'ACK\')' if active_only else ""
    return db.fetch_one(
        f'SELECT * FROM "OpsAlert" WHERE "alertCode"=:code{status} '
        'ORDER BY "raisedAt" DESC, "alertId" DESC LIMIT 1',
        {"code": code},
    )


def _transition_status(ops: OpsService, alert_id: str, action: str) -> Any:
    try:
        result = ops.ack(alert_id, TECHNICAL_ACTOR) if action == "ack" else ops.close(alert_id, TECHNICAL_ACTOR)
        return result.get("status")
    except HTTPException as exc:
        return exc.status_code


def _approve(ops: OpsService, change_id: str) -> dict[str, Any]:
    ops.submit(change_id, "s5-submitter")
    ops.review(change_id, True, "技术验收通过", "s5-reviewer")
    return ops.publish(change_id, "s5-publisher")


def _target_id(db: Database, check: dict[str, Any]) -> str:
    selector = check["targetIdFrom"]
    row = db.fetch_one(
        f'SELECT "{selector["column"]}" AS value FROM "{selector["table"]}" '
        f'ORDER BY "{selector["column"]}" LIMIT 1'
    ) or {}
    if not row.get("value"):
        raise RuntimeError(f"无法解析目标：{selector}")
    return str(row["value"])


def _check_metric(check: dict[str, Any], ops: OpsService) -> dict[str, Any]:
    computed = ops.compute_metrics()
    if check["type"] == "metricDefinitions":
        actual = {
            code: {"direction": item["direction"], "threshold": float(item["threshold"])}
            for code, item in computed.items()
        }
        expected = check["expected"]
        matched = actual == expected
        field = "metricDefinitions"
    elif check["type"] == "metricStatusConsistent":
        actual = all(
            item["status"] == judge_status(item["metricValue"], item["direction"], float(item["threshold"]))
            for item in computed.values()
        )
        expected = True
        matched = actual is True
        field = "metricStatusConsistent"
    else:
        raise RuntimeError(f"未知 metric 检查：{check['type']}")
    return {"group": check["group"], "field": field, "expected": expected, "actual": actual, "matched": matched}


def _check_alert(check: dict[str, Any], db: Database, ops: OpsService, ctx: dict[str, Any]) -> dict[str, Any]:
    kind = check["type"]
    code = check.get("alertCode")
    if kind == "inspectRaisesAlert":
        ops.inspect(TECHNICAL_ACTOR)
        row = _alert(db, code, active_only=True) or {}
        ctx.setdefault("alertIds", {})[code] = row.get("alertId")
        actual = {"status": row.get("status"), "severity": row.get("severity")}
        matched = actual == check["expected"]
    elif kind == "alertTransition":
        alert_id = ctx.get("alertIds", {}).get(code) or (_alert(db, code) or {}).get("alertId")
        actual = _transition_status(ops, alert_id, check["action"])
        matched = actual == check["expected"]
    elif kind == "alertDuplicateTransition":
        alert_id = ctx.get("alertIds", {}).get(code) or (_alert(db, code) or {}).get("alertId")
        actual = _transition_status(ops, alert_id, check["action"])
        matched = actual == check["expected"]
    elif kind == "inspectTwice":
        ops.inspect(TECHNICAL_ACTOR)
        ops.inspect(TECHNICAL_ACTOR)
        actual = True
        matched = True
    elif kind == "openAlertUniqueness":
        duplicates = db.fetch_all(
            'SELECT "alertCode", COUNT(*) AS total FROM "OpsAlert" WHERE "status"=\'OPEN\' '
            'GROUP BY "alertCode" HAVING COUNT(*)>1'
        )
        actual = len(duplicates) == 0
        matched = actual is True
    elif kind == "alertSeverity":
        row = _alert(db, code) or {}
        actual = row.get("severity")
        matched = actual == check["expected"]
    elif kind == "autoCloseRecovery":
        _apply_setup(db, check.get("setup") or [])
        ops.inspect(TECHNICAL_ACTOR)
        raised = _alert(db, code, active_only=True)
        cleanup = check["cleanup"]
        for item in cleanup if isinstance(cleanup, list) else [cleanup]:
            _cleanup(db, item)
        ops.inspect(TECHNICAL_ACTOR)
        remaining = _alert(db, code, active_only=True)
        actual = bool(raised) and remaining is None
        matched = actual is check["expected"]
    else:
        raise RuntimeError(f"未知 alert 检查：{kind}")
    return {
        "group": check["group"],
        "field": f"{kind}:{code}",
        "expected": check.get("expected"),
        "actual": actual,
        "matched": matched,
    }


def _check_ingest(check: dict[str, Any], db: Database, ops: OpsService) -> dict[str, Any]:
    kind = check["type"]
    if kind == "retryBlocked":
        try:
            ops.retry_failed(check["crawlSourceId"], TECHNICAL_ACTOR)
            actual = 200
        except HTTPException as exc:
            actual = exc.status_code
        matched = actual == check["expected"]
    else:
        report = ops.ingest()
        if kind == "ingestSourceStale":
            row = next(
                (item for item in report["sources"] if item["crawlSourceId"] == check["crawlSourceId"]),
                {},
            )
            actual = row.get("staleFlag")
        elif kind == "ingestBacklogConsistent":
            backlog = report["backlog"]
            actual = backlog["total"] == sum(backlog["byType"].values())
        elif kind == "ingestCursorPresent":
            actual = report["changes"]["cursor"] is not None
        else:
            raise RuntimeError(f"未知 ingest 检查：{kind}")
        matched = actual == check["expected"]
    return {
        "group": check["group"],
        "field": kind,
        "expected": check.get("expected"),
        "actual": actual,
        "matched": matched,
    }


def _check_evolution(check: dict[str, Any], db: Database, ops: OpsService) -> dict[str, Any]:
    kind = check["type"]
    payload = {
        "targetKind": check["targetKind"],
        "changeType": check["changeType"],
        "draftPayload": check["draftPayload"],
    }
    if check.get("targetIdFrom"):
        payload["targetId"] = _target_id(db, check)
    if kind == "evolutionFlow":
        created = ops.create_draft(payload, "s5-submitter")
        statuses = [created["status"]]
        statuses.append(ops.submit(created["changeId"], "s5-submitter")["status"])
        statuses.append(ops.review(created["changeId"], True, "同意", "s5-reviewer")["status"])
        statuses.append(ops.publish(created["changeId"], "s5-publisher")["status"])
        statuses.append(ops.rollback(created["changeId"], "s5-rollback")["status"])
        actual = statuses
        matched = actual == check["expectedStatuses"]
    elif kind == "illegalTransition":
        created = ops.create_draft(payload, "s5-submitter")
        try:
            ops.publish(created["changeId"], "s5-publisher")
            actual = 200
        except HTTPException as exc:
            actual = exc.status_code
        matched = actual == check["expected"]
    elif kind == "separationOfDuties":
        created = ops.create_draft(payload, "s5-same")
        ops.submit(created["changeId"], "s5-same")
        ops.review(created["changeId"], True, "同意", "s5-same")
        try:
            ops.publish(created["changeId"], "s5-publisher")
            actual = 200
        except HTTPException as exc:
            actual = exc.status_code
        matched = actual == check["expected"]
    else:
        raise RuntimeError(f"未知 evolution 检查：{kind}")
    return {
        "group": check["group"],
        "field": kind,
        "expected": check.get("expectedStatuses") or check.get("expected"),
        "actual": actual,
        "matched": matched,
    }


def _check_rollback(
    check: dict[str, Any], db: Database, scope: ScopeService, ontology: OntologyService, ops: OpsService
) -> dict[str, Any]:
    kind = check["type"]
    if kind == "scopeEvolutionRoundTrip":
        created_scope = scope.upsert_scope(
            scope_type="SERIES_SCOPE",
            members=[{"dimensionCode": "GEOGRAPHY", "objectType": "Region", "objectId": "REG0001"}],
            scope_code="SCOPE-S5-ROUNDTRIP",
            label="初始标签",
            actor=TECHNICAL_ACTOR,
        )
        payload = {
            "targetKind": "SCOPE", "changeType": "UPDATE", "targetId": created_scope["scopeId"],
            "draftPayload": {
                "scopeCode": "SCOPE-S5-ROUNDTRIP", "scopeType": "SERIES_SCOPE",
                "label": "变更后的标签",
            },
        }
        change = ops.create_draft(payload, "s5-submitter")
        _approve(ops, change["changeId"])
        updated = db.fetch_one(
            'SELECT * FROM "OntologyScope" WHERE "scopeId"=:id', {"id": created_scope["scopeId"]}
        ) or {}
        ops.rollback(change["changeId"], "s5-rollback")
        restored = db.fetch_one(
            'SELECT * FROM "OntologyScope" WHERE "scopeId"=:id', {"id": created_scope["scopeId"]}
        ) or {}
        actual = (
            updated.get("label") == "变更后的标签" and int(updated.get("version") or 0) == 2
            and restored.get("label") == "初始标签" and int(restored.get("version") or 0) == 1
        )
        matched = actual is True
    elif kind == "ruleEvolutionWhitelist":
        change = ops.create_draft({
            "targetKind": "RULE", "changeType": "UPDATE", "targetId": check["ruleId"],
            "draftPayload": {"confirmationStatus": "CONFIRMED", "testcaseIds": ["TC1", "TC2"]},
        }, "s5-submitter")
        _approve(ops, change["changeId"])
        updated = db.fetch_one(
            'SELECT * FROM "CompiledRule" WHERE "ruleId"=:id', {"id": check["ruleId"]}
        ) or {}
        ops.rollback(change["changeId"], "s5-rollback")
        restored = db.fetch_one(
            'SELECT * FROM "CompiledRule" WHERE "ruleId"=:id', {"id": check["ruleId"]}
        ) or {}
        actual = (
            updated.get("confirmationStatus") == "CONFIRMED"
            and json.loads(updated.get("testcaseIds") or "[]") == ["TC1", "TC2"]
            and updated.get("ruleExpression") == "1=1"
            and restored.get("confirmationStatus") == "DRAFT"
            and json.loads(restored.get("testcaseIds") or "[]") == []
        )
        matched = actual is True
    elif kind == "metaEvolutionSurvivesSync":
        target_id = _target_id(db, check)
        original = db.fetch_one(
            'SELECT "displayName" FROM "OntologyObjectType" WHERE "objectTypeId"=:id', {"id": target_id}
        ) or {}
        change = ops.create_draft({
            "targetKind": "ONTOLOGY_META", "changeType": "UPDATE", "targetId": target_id,
            "draftPayload": {"displayName": "阶段5改名验证"},
        }, "s5-submitter")
        _approve(ops, change["changeId"])
        ontology.sync()
        after_sync = db.fetch_one(
            'SELECT "displayName" FROM "OntologyObjectType" WHERE "objectTypeId"=:id', {"id": target_id}
        ) or {}
        ops.rollback(change["changeId"], "s5-rollback")
        restored = db.fetch_one(
            'SELECT "displayName" FROM "OntologyObjectType" WHERE "objectTypeId"=:id', {"id": target_id}
        ) or {}
        actual = (
            after_sync.get("displayName") == "阶段5改名验证"
            and restored.get("displayName") == original.get("displayName")
        )
        matched = actual is True
    else:
        raise RuntimeError(f"未知 rollback 检查：{kind}")
    return {
        "group": check["group"],
        "field": kind,
        "expected": check.get("expected", True),
        "actual": actual,
        "matched": matched,
    }


def _run_mutations(db: Database, mutations: list[dict[str, Any]]) -> None:
    from sqlalchemy import text

    with db.engine.begin() as conn:
        for mutation in mutations:
            conn.execute(text(mutation["sql"]), mutation.get("params") or {})


def _check_gate_negative(
    check: dict[str, Any], snapshot: Path, work_dir: Path, index: int
) -> dict[str, Any]:
    sub_snapshot = work_dir / f"negative-{index}.db"
    shutil.copy2(snapshot, sub_snapshot)
    db, _, _, ops = _services(sub_snapshot)
    try:
        _run_mutations(db, check.get("mutations") or [])
        ops.validate_stage5()
        actual = "未拦截"
        matched = False
    except OpsError as exc:
        actual = str(exc)
        matched = all(token in actual for token in check.get("expectContains") or [])
    finally:
        db.close()
    return {
        "group": check["group"],
        "field": f"gateNegative:{check['gate']}",
        "expected": check.get("expectContains"),
        "actual": actual,
        "matched": matched,
    }


def _with_client(snapshot: Path, work_dir: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{snapshot}",
        jwt_secret=JWT_SECRET,
        auto_seed=False,
        graph_backend="relational",
        data_dir=work_dir / "data",
        scheduler_enabled=False,
    )
    return TestClient(create_app(settings))


def _check_permission(check: dict[str, Any], client: TestClient) -> dict[str, Any]:
    token = issue_token(TECHNICAL_ACTOR, check["role"], JWT_SECRET)
    response = client.request(
        check["method"], check["path"], headers={"Authorization": f"Bearer {token}"}
    )
    actual = response.status_code
    return {
        "group": check["group"],
        "field": f"{check['method']} {check['path']} [{check['role']}]",
        "expected": check["expected"],
        "actual": actual,
        "matched": actual == check["expected"],
    }


def _execute_once(case: dict[str, Any], database_path: Path, work_dir: Path) -> list[dict[str, Any]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    snapshot = work_dir / "case.db"
    shutil.copy2(database_path, snapshot)
    kind = case["kind"]
    checks: list[dict[str, Any]] = []

    if kind in ("service", "mixed"):
        db, scope, ontology, ops = _services(snapshot)
        try:
            before = _knowledge_counts(db)
            _apply_setup(db, case.get("setup") or [])
            ctx: dict[str, Any] = {}
            for check in case["checks"]:
                if check["type"] in ("metricDefinitions", "metricStatusConsistent"):
                    checks.append(_check_metric(check, ops))
                elif check["group"] == "alert":
                    checks.append(_check_alert(check, db, ops, ctx))
                elif check["group"] == "ingest":
                    checks.append(_check_ingest(check, db, ops))
                elif check["group"] == "evolution":
                    checks.append(_check_evolution(check, db, ops))
                elif check["group"] == "rollback":
                    checks.append(_check_rollback(check, db, scope, ontology, ops))
                elif check["group"] == "permission":
                    continue
                elif check["type"] == "gateNegative":
                    checks.append(_check_gate_negative(check, snapshot, work_dir, len(checks)))
                else:
                    raise RuntimeError(f"未知 service 检查：{check['type']}")
        finally:
            db.close()

        if kind == "mixed":
            with _with_client(snapshot, work_dir) as client:
                for check in case["checks"]:
                    if check["group"] == "permission":
                        checks.append(_check_permission(check, client))

        db = Database(f"sqlite:///{snapshot}", ROOT / "seed")
        db.initialize()
        try:
            after = _knowledge_counts(db)
        finally:
            db.close()
        checks.append({
            "group": "knowledge",
            "field": "knowledgeTablesUnchanged",
            "expected": before,
            "actual": after,
            "matched": before == after,
        })
    else:
        raise RuntimeError(f"未知情形类型：{kind}")
    return checks


def _execute_case(case: dict[str, Any], database_path: Path, work_dir: Path) -> dict[str, Any]:
    cold: list[dict[str, Any]] = []
    warm: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        cold = _execute_once(case, database_path, work_dir / "cold")
    except Exception as exc:  # noqa: BLE001 - 单情形失败不中断整轮
        errors.append(f"cold: {type(exc).__name__}: {exc}")
    try:
        warm = _execute_once(case, database_path, work_dir / "warm")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"warm: {type(exc).__name__}: {exc}")
    deterministic = bool(cold) and _json_sha256(cold) == _json_sha256(warm)
    failures = [check for check in cold if not _check_matched(check)]
    return {
        "caseId": case["caseId"],
        "title": case["title"],
        "kind": case["kind"],
        "deterministic": deterministic,
        "checks": cold,
        "failures": [check["field"] for check in failures],
        "errors": errors,
        "passed": deterministic and not errors and not failures,
    }


def _validate_sources(cases: dict[str, Any]) -> dict[str, Any]:
    results = []
    for item in cases.get("source", {}).get("files", []):
        path = ROOT / item["path"]
        actual = sha256(path) if path.exists() else None
        results.append({
            "path": item["path"],
            "expected": item["sha256"],
            "actual": actual,
            "matched": actual == item["sha256"],
        })
    return {"passed": bool(results) and all(item["matched"] for item in results), "files": results}


def run_evaluation(cases_path: Path, database_path: Path, workspace: Path) -> dict[str, Any]:
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    before = sha256(database_path)
    source_validation = _validate_sources(cases)

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(dir=workspace) as temp:
        temp_dir = Path(temp)
        for case in cases["cases"]:
            rows.append(_execute_case(case, database_path, temp_dir / case["caseId"]))

    after = sha256(database_path)
    metrics = summarize_cases(rows)
    deterministic = all(row["deterministic"] for row in rows)
    isolation_passed = before == after
    gates = build_hard_gates(
        metrics,
        deterministic=deterministic,
        isolation_passed=isolation_passed,
        source_hashes_valid=source_validation["passed"],
    )
    return {
        "schemaVersion": "1.0.0",
        "stage": cases["stage"],
        "datasetTier": cases["datasetTier"],
        "businessQualityGate": "NOT_ASSESSED",
        "productionPerformanceGate": "NOT_ASSESSED",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "source": {"files": cases["source"]["files"]},
        "sourceValidation": source_validation,
        "formalDatabase": {"path": str(database_path), "before": before, "after": after, "unchanged": isolation_passed},
        "evaluationIsolation": {
            "passed": isolation_passed,
            "strategy": "每个情形冷/热两次独立副本库；正式库只读",
        },
        "metrics": metrics,
        "cases": rows,
        "hardGates": gates,
        "summary": (
            f"阶段 5 技术冒烟：{len(rows)} 个固定情形，"
            f"通过 {sum(1 for row in rows if row['passed'])} 个；"
            "业务质量与生产性能未评估。"
        ),
    }


def main() -> int:
    report = run_evaluation(CASES_PATH, DATABASE_PATH, REPORT_PATH.parent)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    gates = report["hardGates"]
    print(json.dumps({
        "report": str(REPORT_PATH),
        "metrics": report["metrics"],
        "overallPassed": gates["overall"]["passed"],
        "failedGates": [name for name, gate in gates.items() if name != "overall" and not gate["passed"]],
    }, ensure_ascii=False, indent=2))
    return 0 if gates["overall"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
