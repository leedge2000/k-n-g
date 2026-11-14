# -*- coding: utf-8 -*-
"""阶段 3 技术冒烟评测：as-of 回放一致性、前视泄漏与只读快照执行。"""

from __future__ import annotations

import hashlib
import json
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

CASES_PATH = ROOT / "ontology_ly" / "capability" / "stage3_backtest_smoke.json"
DATABASE_PATH = ROOT / "data" / "grain_oil.db"
REPORT_PATH = Path(__file__).resolve().parent / "stage3_report.json"
TECHNICAL_ACTOR = "technical-smoke"


def _ratio(numerator: int, denominator: int) -> float | None:
    """空分母保留为 None，避免把未评测误报为零或满分。"""
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _check_matched(check: dict[str, Any]) -> bool:
    if "matched" in check:
        return check["matched"] is True
    return check.get("expected") == check.get("actual")


_CHECK_KEYS = {
    "eventStateAgreement": "eventStateChecks",
    "impactAgreement": "impactChecks",
    "overviewAgreement": "overviewChecks",
    "assertionGroupAgreement": "assertionChecks",
    "transmissionIntegrity": "transmissionChecks",
}


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "caseCount": len(cases),
        "casePassRate": _ratio(
            sum(1 for case in cases if case.get("passed") is True), len(cases)
        ),
        "lookaheadLeaks": sum(int(case.get("lookaheadLeaks") or 0) for case in cases),
    }
    for metric, key in _CHECK_KEYS.items():
        checks = [check for case in cases for check in (case.get(key) or [])]
        metrics[metric] = _ratio(
            sum(1 for check in checks if _check_matched(check)), len(checks)
        )
    return metrics


def build_hard_gates(
    metrics: dict[str, Any],
    deterministic: bool,
    isolation_passed: bool,
    read_only_passed: bool,
    source_hashes_valid: bool,
) -> dict[str, Any]:
    gates: dict[str, Any] = {
        "deterministicOutput": {"passed": deterministic is True},
        "formalDataUnchanged": {"passed": isolation_passed is True},
        "readOnlyAccess": {"passed": read_only_passed is True},
        "sourceHashesValid": {"passed": source_hashes_valid is True},
        "noLookahead": {
            "passed": metrics.get("lookaheadLeaks") == 0,
            "actual": metrics.get("lookaheadLeaks"),
        },
    }
    for metric, expected in (
        ("casePassRate", 1.0),
        ("eventStateAgreement", 1.0),
        ("impactAgreement", 1.0),
        ("overviewAgreement", 1.0),
        ("assertionGroupAgreement", 1.0),
        ("transmissionIntegrity", 1.0),
    ):
        gates[metric] = {
            "passed": metrics.get(metric) == expected,
            "expected": expected,
            "actual": metrics.get(metric),
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


def _auth(settings: Settings) -> dict[str, str]:
    token = issue_token(TECHNICAL_ACTOR, "ROLE-04", settings.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


def _fetch_snapshot(
    client: TestClient, headers: dict[str, str], case: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """只读调用公开 GET API；方法列表用于 readOnlyAccess 硬门。"""
    as_of = case["asOf"]
    series_id = case["seriesId"]
    methods: list[str] = []

    def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        methods.append("GET")
        response = client.get(path, params=params or {}, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"{path} 返回 {response.status_code}: {response.text}")
        return response.json()

    payload = {
        "overview": get("/api/market/overview", {"asOf": as_of}),
        "series": get(f"/api/market/series/{series_id}", {"asOf": as_of}),
        "timeline": get("/api/market/timeline", {"asOf": as_of}),
        "assertions": get("/api/market/assertions", {"asOf": as_of}),
        "transmissions": get("/api/market/transmissions"),
    }
    return payload, methods


def _build_checks(
    case: dict[str, Any], payload: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    expected = case["expected"]
    as_of = case["asOf"]
    overview = payload["overview"]
    series_body = payload["series"]
    points = series_body.get("points") or []
    series_events = series_body.get("events") or []
    timeline_items = payload["timeline"].get("items") or []
    assertions = payload["assertions"]
    transmissions = payload["transmissions"]

    overview_checks = [
        {"field": field, "expected": value, "actual": overview.get(field)}
        for field, value in expected["overview"].items()
    ]
    expected_series = expected["series"]
    overview_checks.extend([
        {
            "field": "series.pointCount",
            "expected": expected_series["pointCount"],
            "actual": len(points),
        },
        {
            "field": "series.lastPeriodId",
            "expected": expected_series["lastPeriodId"],
            "actual": points[-1]["periodId"] if points else None,
        },
        {
            "field": "series.unitCode",
            "expected": expected_series["unitCode"],
            "actual": (series_body.get("series") or {}).get("unitCode"),
        },
        {
            "field": "series.priceBasis",
            "expected": expected_series["priceBasis"],
            "actual": (series_body.get("series") or {}).get("priceBasis"),
        },
    ])

    event_checks: list[dict[str, Any]] = [
        {
            "field": "timeline.eventIds",
            "expected": [item["eventId"] for item in expected["timeline"]],
            "actual": [item.get("eventId") for item in timeline_items],
        },
        {
            "field": "series.eventIds",
            "expected": [item["eventId"] for item in expected["events"]],
            "actual": [item.get("eventId") for item in series_events],
        },
    ]
    for expected_item in expected["timeline"]:
        actual_item = next(
            (item for item in timeline_items if item.get("eventId") == expected_item["eventId"]),
            {},
        )
        event_checks.append({
            "field": f"timeline.{expected_item['eventId']}.state",
            "expected": expected_item["state"],
            "actual": actual_item.get("state"),
        })
    for expected_item in expected["events"]:
        actual_item = next(
            (item for item in series_events if item.get("eventId") == expected_item["eventId"]),
            {},
        )
        event_checks.append({
            "field": f"series.{expected_item['eventId']}.state",
            "expected": expected_item["state"],
            "actual": actual_item.get("state"),
        })

    impact_checks = []
    for expected_item in expected["timeline"]:
        actual_item = next(
            (item for item in timeline_items if item.get("eventId") == expected_item["eventId"]),
            {},
        )
        impact_checks.append({
            "field": f"timeline.{expected_item['eventId']}.triggeredImpactCount",
            "expected": expected_item["triggeredImpactCount"],
            "actual": actual_item.get("triggeredImpactCount"),
        })

    assertion_checks = [{
        "field": "assertions.total",
        "expected": expected["assertionTotal"],
        "actual": assertions.get("total"),
    }]

    transmission_checks = [
        {
            "field": "transmissions.nodes",
            "expected": expected["transmission"]["nodes"],
            "actual": len(transmissions.get("nodes") or []),
        },
        {
            "field": "transmissions.edges",
            "expected": expected["transmission"]["edges"],
            "actual": len(transmissions.get("edges") or []),
        },
    ]

    leaks: list[dict[str, Any]] = []
    for point in points:
        published = str(point.get("publishedAt") or "")[:10]
        if published > as_of:
            leaks.append({
                "type": "point",
                "periodId": point.get("periodId"),
                "publishedAt": published,
            })
    for event in series_events:
        announced = str(event.get("announceTime") or "")[:10]
        if announced > as_of:
            leaks.append({
                "type": "seriesEvent",
                "eventId": event.get("eventId"),
                "announceTime": announced,
            })
    for item in timeline_items:
        announced = str(item.get("announceTime") or "")[:10]
        if announced > as_of:
            leaks.append({
                "type": "timelineEvent",
                "eventId": item.get("eventId"),
                "announceTime": announced,
            })

    checks = {
        "overviewChecks": overview_checks,
        "eventStateChecks": event_checks,
        "impactChecks": impact_checks,
        "assertionChecks": assertion_checks,
        "transmissionChecks": transmission_checks,
    }
    return checks, leaks


def _execute_case(
    case: dict[str, Any], database_path: Path, case_dir: Path
) -> dict[str, Any]:
    case_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = case_dir / "snapshot.db"
    shutil.copy2(database_path, snapshot_path)
    settings = Settings(
        database_url=f"sqlite:///{snapshot_path.as_posix()}",
        jwt_secret="technical-smoke-secret-with-at-least-32-bytes",
        auto_seed=False,
        graph_backend="relational",
        data_dir=case_dir / "data",
    )
    errors: list[dict[str, Any]] = []
    elapsed: dict[str, float] = {}
    responses: dict[str, dict[str, Any] | None] = {"cold": None, "warm": None}
    methods: list[str] = []
    row: dict[str, Any] = {
        "caseId": case["caseId"],
        "title": case["title"],
        "asOf": case["asOf"],
        "seriesId": case["seriesId"],
        "snapshotBeforeInitialization": {"sha256": sha256(snapshot_path)},
    }
    try:
        with TestClient(create_app(settings)) as client:
            headers = _auth(settings)
            for phase in ("cold", "warm"):
                started = perf_counter()
                payload, phase_methods = _fetch_snapshot(client, headers, case)
                elapsed[phase] = (perf_counter() - started) * 1000
                methods.extend(phase_methods)
                responses[phase] = payload
    except Exception as error:  # 评测器必须把失败转成证据，而不是中断全部情形
        errors.append({"type": type(error).__name__, "detail": str(error)})

    cold = responses["cold"]
    warm = responses["warm"]
    deterministic = cold is not None and warm is not None and cold == warm
    checks, leaks = _build_checks(case, cold) if cold is not None else (
        {key: [] for key in _CHECK_KEYS.values()}, []
    )
    all_checks = [check for group in checks.values() for check in group]
    failures = [check for check in all_checks if not _check_matched(check)]
    row.update({
        **checks,
        "failures": failures,
        "lookaheadLeaks": len(leaks),
        "lookaheadDetail": leaks,
        "deterministic": deterministic,
        "coldResponseSha256": _json_sha256(cold) if cold is not None else None,
        "warmResponseSha256": _json_sha256(warm) if warm is not None else None,
        "coldElapsedMs": round(elapsed.get("cold", 0.0), 3),
        "warmElapsedMs": round(elapsed.get("warm", 0.0), 3),
        "requestMethods": methods,
        "errors": errors,
    })
    row["passed"] = (
        deterministic
        and not errors
        and not failures
        and not leaks
        and bool(all_checks)
    )
    return row


def _validate_sources(cases_doc: dict[str, Any]) -> dict[str, Any]:
    files = []
    for item in cases_doc["source"]["files"]:
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
    cases_path: str | Path = CASES_PATH,
    database_path: str | Path = DATABASE_PATH,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    cases_path = Path(cases_path)
    database_path = Path(database_path)
    if not cases_path.is_file() or not database_path.is_file():
        raise FileNotFoundError("阶段 3 固定情形或正式数据库不存在")
    cases_bytes = cases_path.read_bytes()
    cases_doc = json.loads(cases_bytes.decode("utf-8"))
    if cases_doc.get("datasetTier") != "technical-smoke":
        raise ValueError("阶段 3 评测只接受 technical-smoke 固定输入")

    source_validation = _validate_sources(cases_doc)
    protected_paths = [database_path, cases_path]
    protected_paths.extend(ROOT / item["path"] for item in cases_doc["source"]["files"])
    protected_paths = list(dict.fromkeys(protected_paths))
    before = {path: file_state(path) for path in protected_paths}

    with ExitStack() as stack:
        if workspace is None:
            workspace_path = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="kg-stage3-eval-"))
            )
        else:
            workspace_path = Path(workspace)
            workspace_path.mkdir(parents=True, exist_ok=True)

        rows = [
            _execute_case(case, database_path, workspace_path / case["caseId"])
            for case in cases_doc["cases"]
        ]

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

    requests = [method for row in rows for method in row["requestMethods"]]
    non_get = [method for method in requests if method != "GET"]
    read_only_access = {
        "passed": bool(requests) and not non_get,
        "requestCount": len(requests),
        "nonGetMethods": non_get,
    }
    deterministic = bool(rows) and all(row["deterministic"] for row in rows)
    metrics = summarize_cases(rows)
    hard_gates = build_hard_gates(
        metrics,
        deterministic=deterministic,
        isolation_passed=isolation_passed,
        read_only_passed=read_only_access["passed"],
        source_hashes_valid=source_validation["passed"],
    )

    report = {
        "schemaVersion": "1.0.0",
        "stage": "Stage 3",
        "datasetTier": "technical-smoke",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "businessQualityGate": "NOT_ASSESSED",
        "productionPerformanceGate": "NOT_ASSESSED",
        "boundary": (
            "技术冒烟只验证 as-of 回放契约与前视边界；期望值全部来自合成种子，"
            "不是真实行情回溯，也不是业务金标或生产性能验收。"
        ),
        "environment": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
        },
        "configuration": {
            "graphBackend": "relational",
            "autoSeed": False,
            "api": "/api/market/*",
            "readOnly": True,
            "warmRounds": 1,
            "snapshotPerCase": True,
        },
        "inputs": {
            "database": {
                "path": _display_path(database_path),
                "sha256": before[database_path]["sha256"],
            },
            "cases": {
                "path": _display_path(cases_path),
                "sha256": hashlib.sha256(cases_bytes).hexdigest(),
            },
        },
        "sourceValidation": source_validation,
        "cases": rows,
        "metrics": metrics,
        "readOnlyAccess": read_only_access,
        "deterministicOutput": deterministic,
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
    for case in report["cases"]:
        print(
            f"{case['caseId']} {case['asOf']}: 通过={case['passed']}，"
            f"前视泄漏={case['lookaheadLeaks']}，确定性={case['deterministic']}"
        )
    metrics = report["metrics"]
    print(
        f"情形通过率={metrics['casePassRate']}，状态一致率={metrics['eventStateAgreement']}，"
        f"影响一致率={metrics['impactAgreement']}，前视泄漏={metrics['lookaheadLeaks']}"
    )
    print(
        f"隔离={report['evaluationIsolation']['passed']}，只读={report['readOnlyAccess']['passed']}，"
        f"硬门禁={report['allHardGatesPassed']}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
