# -*- coding: utf-8 -*-
"""阶段 4 联动与扩面隔离评测：逐情形副本库、只读路由、G4 正负门与深链。"""

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

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import issue_token
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market import MarketError, MarketService
from app.scope import ScopeService

CASES_PATH = ROOT / "ontology_ly" / "capability" / "stage4_linkage_smoke.json"
DATABASE_PATH = ROOT / "data" / "grain_oil.db"
REPORT_PATH = Path(__file__).resolve().parent / "stage4_report.json"
TECHNICAL_ACTOR = "technical-smoke"
JWT_SECRET = "stage4-technical-smoke-secret-with-32-bytes"

GROUP_METRICS = {
    "eventState": "eventStateAgreement",
    "impact": "impactAgreement",
    "scope": "scopeAgreement",
    "route": "routeShapeAgreement",
    "bridge": "bridgeIntegrity",
    "gate": "gateNegativeDetectionRate",
    "deepLink": "deepLinkAgreement",
    "noWrite": "noWriteRoutes",
}
METRIC_KEYS = ("casePassRate", *GROUP_METRICS.values())
HARD_METRICS = tuple(
    metric for metric in METRIC_KEYS if metric != "noWriteRoutes"
)


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
    no_write_passed: bool,
) -> dict[str, Any]:
    gates: dict[str, Any] = {
        "deterministicOutput": {"passed": deterministic is True},
        "formalDataUnchanged": {"passed": isolation_passed is True},
        "sourceHashesValid": {"passed": source_hashes_valid is True},
        "isolatedDataset": {"passed": isolation_passed is True},
        "noWriteRoutes": {"passed": no_write_passed is True},
    }
    for metric in HARD_METRICS:
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
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_field(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not part:
            continue
        index = None
        if "[" in part and part.endswith("]"):
            part, raw_index = part[:-1].split("[", 1)
            index = int(raw_index)
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)] if int(part) < len(current) else None
        else:
            return None
        if index is not None:
            current = current[index] if isinstance(current, list) and index < len(current) else None
    return current


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(TECHNICAL_ACTOR, 'ROLE-04', JWT_SECRET)}"}


def _services(database_path: Path) -> tuple[Database, MarketService]:
    db = Database(f"sqlite:///{database_path}", ROOT / "seed")
    db.initialize()
    scope = ScopeService(db)
    scope.initialize()
    service = MarketService(db, scope)
    service.initialize()
    return db, service


def _series_row(db: Database, series_id: str) -> dict[str, Any]:
    row = db.fetch_one('SELECT * FROM "IndicatorSeries" WHERE "seriesId"=:id', {"id": series_id})
    if not row:
        raise RuntimeError(f"序列不存在：{series_id}")
    return row


def _series_channel(db: Database, series: dict[str, Any]) -> str | None:
    scope = db.fetch_one('SELECT * FROM "OntologyScope" WHERE "scopeId"=:id', {"id": series.get("scopeId")})
    if not scope:
        return None
    for member in json.loads(scope.get("members") or "[]"):
        if str(member.get("dimensionCode") or "").upper() == "CHANNEL":
            return member.get("objectId")
    return None


def _check_service(check: dict[str, Any], db: Database, service: MarketService) -> dict[str, Any]:
    kind = check["type"]
    actual: Any
    if kind == "eventState":
        event = db.fetch_one('SELECT * FROM "Event" WHERE "eventId"=:id', {"id": check["eventId"]})
        actual = service.event_state(event, check["asOf"]) if event else None
    elif kind == "impactTriggered":
        link = db.fetch_one('SELECT * FROM "ImpactLink" WHERE "linkId"=:id', {"id": check["linkId"]})
        actual = service.impact_triggered(link, check["asOf"]) if link else None
    elif kind == "seriesField":
        series = _series_row(db, check["seriesId"])
        field = check["field"]
        if field == "unitCode":
            unit = db.fetch_one(
                'SELECT "unitCode" FROM "UnitOfMeasure" WHERE "unitId"=:id', {"id": series.get("unitId")}
            )
            actual = (unit or {}).get("unitCode")
        else:
            actual = series.get(field)
    elif kind == "seriesChannel":
        actual = _series_channel(db, _series_row(db, check["seriesId"]))
    elif kind == "tableCount":
        row = db.fetch_one(f'SELECT COUNT(*) AS total FROM "{check["table"]}"') or {}
        actual = int(row.get("total") or 0)
    elif kind == "impactEvidenceComplete":
        rows = db.fetch_all('SELECT "linkId" FROM "ImpactLink"')
        actual = all(
            int((db.fetch_one(
                'SELECT COUNT(*) AS total FROM "Evidence" WHERE "targetType"=\'ImpactLink\' AND "targetId"=:id',
                {"id": row["linkId"]},
            ) or {}).get("total") or 0) > 0
            for row in rows
        )
    elif kind == "impactNoSeriesTarget":
        row = db.fetch_one(
            'SELECT COUNT(*) AS total FROM "ImpactLink" WHERE "targetType"=\'SERIES\''
        ) or {}
        actual = int(row.get("total") or 0) == 0
    elif kind == "driveMetricComplete":
        rows = db.fetch_all('SELECT * FROM "FactorDriveLink"')
        actual = all(
            row.get("elasticity") is not None or row.get("lagDays") is not None for row in rows
        ) and all(
            db.fetch_one('SELECT 1 AS ok FROM "IndicatorSeries" WHERE "seriesId"=:id', {"id": row["seriesId"]})
            for row in rows
        )
    else:
        raise RuntimeError(f"未知 service 检查类型：{kind}")
    return {
        "group": check["group"],
        "field": check.get("field") or f"{kind}:{check.get('eventId') or check.get('linkId') or check.get('seriesId') or check.get('table')}",
        "expected": check.get("expected"),
        "actual": actual,
        "matched": actual == check.get("expected"),
    }


def _check_api(check: dict[str, Any], client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    kind = check["type"]
    response = client.get(check["path"], params=check.get("params") or {}, headers=headers)
    body = response.json() if response.status_code < 400 else None
    if kind == "apiStatus":
        actual: Any = response.status_code
        expected = check["expected"]
        matched = actual == expected
    elif kind == "apiField":
        actual = _resolve_field(body, check["field"])
        expected = check["expected"]
        matched = actual == expected
    elif kind == "apiLength":
        value = _resolve_field(body, check["field"])
        actual = len(value) if value is not None else None
        expected = check["expected"]
        matched = actual == expected
    elif kind == "apiEveryHas":
        items = _resolve_field(body, check["listField"]) or []
        actual = all(_resolve_field(item, check["field"]) is not None for item in items)
        expected = True
        matched = actual is True
    else:
        raise RuntimeError(f"未知 api 检查类型：{kind}")
    return {
        "group": check["group"],
        "field": f"{check['path']}:{check.get('field') or check.get('type')}",
        "expected": expected,
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
    db, service = _services(sub_snapshot)
    try:
        _run_mutations(db, check.get("mutations") or [])
        service.validate_stage4()
        actual = "未拦截"
        matched = False
    except MarketError as exc:
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


def _check_static(check: dict[str, Any], client: TestClient) -> dict[str, Any]:
    kind = check["type"]
    if kind == "htmlContains":
        response = client.get(check["path"])
        missing = [marker for marker in check["expected"] if marker not in response.text]
        actual = missing
        matched = response.status_code == 200 and not missing
    elif kind == "openapiGetOnly":
        schema = client.get("/openapi.json").json()
        paths = {
            path: set(methods)
            for path, methods in schema.get("paths", {}).items()
            if path.startswith(check["prefix"])
        }
        violations = {path: sorted(methods - {"get"}) for path, methods in paths.items() if methods - {"get"}}
        actual = violations or (bool(paths) is True)
        matched = bool(paths) and not violations
    else:
        raise RuntimeError(f"未知 static 检查类型：{kind}")
    return {
        "group": check["group"],
        "field": f"{kind}:{check.get('path') or check.get('prefix')}",
        "expected": check.get("expected", True),
        "actual": actual,
        "matched": matched,
    }


def _with_client(snapshot: Path, work_dir: Path):
    settings = Settings(
        database_url=f"sqlite:///{snapshot}",
        jwt_secret=JWT_SECRET,
        auto_seed=False,
        graph_backend="relational",
        data_dir=work_dir / "data",
        scheduler_enabled=False,
    )
    return TestClient(create_app(settings))


def _execute_once(case: dict[str, Any], database_path: Path, work_dir: Path) -> list[dict[str, Any]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    snapshot = work_dir / "case.db"
    shutil.copy2(database_path, snapshot)
    checks: list[dict[str, Any]] = []
    kind = case["kind"]

    if kind in ("service", "gate"):
        db, service = _services(snapshot)
        try:
            for check in case["checks"]:
                if check["type"] == "gatePositive":
                    result = service.validate_stage4()
                    actual_gates = result.get("gates") or {}
                    expected_gates = check["expectedGates"]
                    gates_ok = all(actual_gates.get(code) is True for code in expected_gates)
                    counts_ok = result.get("counts") == case.get("expectedCounts")
                    checks.append({
                        "group": check["group"],
                        "field": "gatePositive:counts+gates",
                        "expected": {"gates": expected_gates, "counts": case.get("expectedCounts")},
                        "actual": {"gates": actual_gates, "counts": result.get("counts")},
                        "matched": gates_ok and counts_ok,
                    })
                else:
                    checks.append(_check_service(check, db, service))
        finally:
            db.close()
    elif kind == "gate-negative":
        for index, check in enumerate(case["checks"]):
            checks.append(_check_gate_negative(check, snapshot, work_dir, index))
    elif kind in ("api", "static"):
        with _with_client(snapshot, work_dir) as client:
            headers = _auth_headers()
            for check in case["checks"]:
                if kind == "api":
                    checks.append(_check_api(check, client, headers))
                else:
                    checks.append(_check_static(check, client))
    else:
        raise RuntimeError(f"未知情形类型：{kind}")
    return checks


def _execute_case(case: dict[str, Any], database_path: Path, work_dir: Path) -> dict[str, Any]:
    cold_dir = work_dir / "cold"
    warm_dir = work_dir / "warm"
    errors: list[str] = []
    cold: list[dict[str, Any]] = []
    warm: list[dict[str, Any]] = []
    try:
        cold = _execute_once(case, database_path, cold_dir)
    except Exception as exc:  # noqa: BLE001 - 单情形失败不中断整轮
        errors.append(f"cold: {type(exc).__name__}: {exc}")
    try:
        warm = _execute_once(case, database_path, warm_dir)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"warm: {type(exc).__name__}: {exc}")
    deterministic = bool(cold) and _json_sha256(cold) == _json_sha256(warm)
    checks = cold
    failures = [
        check for check in checks
        if not _check_matched(check) and check.get("group") != "gate"
    ]
    gate_failures = [
        check for check in checks
        if not _check_matched(check) and check.get("group") == "gate"
    ]
    return {
        "caseId": case["caseId"],
        "title": case["title"],
        "kind": case["kind"],
        "deterministic": deterministic,
        "checks": checks,
        "failures": [check["field"] for check in failures],
        "gateFailures": [check["field"] for check in gate_failures],
        "errors": errors,
        "passed": deterministic and not errors and not failures and not gate_failures,
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
    no_write_passed = metrics.get("noWriteRoutes") == 1.0
    gates = build_hard_gates(
        metrics,
        deterministic=deterministic,
        isolation_passed=isolation_passed,
        source_hashes_valid=source_validation["passed"],
        no_write_passed=no_write_passed,
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
        "noWriteConstraints": {"passed": no_write_passed, "prefix": "/api/market/", "allowedMethods": ["GET"]},
        "metrics": metrics,
        "cases": rows,
        "hardGates": gates,
        "summary": (
            f"阶段 4 技术冒烟：{len(rows)} 个固定情形，"
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
