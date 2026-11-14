# -*- coding: utf-8 -*-
"""阶段 4 联动评测：指标分母、硬门与逐情形隔离执行。"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "ontology_ly" / "capability" / "stage4_linkage_smoke.json"
EVALUATOR_PATH = ROOT / "ontology_ly" / "tools" / "evaluate_stage4.py"


def _load_evaluator():
    assert EVALUATOR_PATH.is_file(), "阶段 4 评测器尚未实现"
    spec = importlib.util.spec_from_file_location("evaluate_stage4", EVALUATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cases_file_fixes_eight_linkage_sections():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert cases["datasetTier"] == "technical-smoke"
    assert [case["caseId"] for case in cases["cases"]] == [
        "S4-LK-01", "S4-LK-02", "S4-LK-03", "S4-LK-04",
        "S4-LK-05", "S4-LK-06", "S4-LK-07", "S4-LK-08",
    ]
    assert all(case["kind"] for case in cases["cases"])
    assert all(item["sha256"] for item in cases["source"]["files"])


def test_summarize_cases_keeps_explicit_denominators():
    evaluator = _load_evaluator()
    rows = [
        {
            "passed": True,
            "checks": [
                {"group": "eventState", "matched": True},
                {"group": "impact", "matched": True},
                {"group": "scope", "matched": True},
                {"group": "route", "matched": True},
                {"group": "bridge", "matched": True},
                {"group": "gate", "matched": True},
                {"group": "deepLink", "matched": True},
                {"group": "noWrite", "matched": True},
            ],
        },
        {
            "passed": False,
            "checks": [
                {"group": "eventState", "matched": False},
                {"group": "impact", "matched": False},
                {"group": "scope", "matched": False},
                {"group": "route", "matched": False},
                {"group": "bridge", "matched": False},
                {"group": "gate", "matched": False},
                {"group": "deepLink", "matched": False},
                {"group": "noWrite", "matched": False},
            ],
        },
    ]
    metrics = evaluator.summarize_cases(rows)
    assert metrics["caseCount"] == 2
    assert metrics["casePassRate"] == 0.5
    assert metrics["eventStateAgreement"] == 0.5
    assert metrics["impactAgreement"] == 0.5
    assert metrics["scopeAgreement"] == 0.5
    assert metrics["routeShapeAgreement"] == 0.5
    assert metrics["bridgeIntegrity"] == 0.5
    assert metrics["gateNegativeDetectionRate"] == 0.5
    assert metrics["deepLinkAgreement"] == 0.5
    assert metrics["noWriteRoutes"] == 0.5


def test_summarize_cases_does_not_turn_empty_denominators_into_zero():
    evaluator = _load_evaluator()
    metrics = evaluator.summarize_cases([])
    for key in (
        "casePassRate", "eventStateAgreement", "impactAgreement", "scopeAgreement",
        "routeShapeAgreement", "bridgeIntegrity", "gateNegativeDetectionRate",
        "deepLinkAgreement", "noWriteRoutes",
    ):
        assert metrics[key] is None, key
    assert metrics["caseCount"] == 0


def test_hard_gates_require_determinism_isolation_and_no_write():
    evaluator = _load_evaluator()
    perfect = {
        "caseCount": 8,
        "casePassRate": 1.0,
        "eventStateAgreement": 1.0,
        "impactAgreement": 1.0,
        "scopeAgreement": 1.0,
        "routeShapeAgreement": 1.0,
        "bridgeIntegrity": 1.0,
        "gateNegativeDetectionRate": 1.0,
        "deepLinkAgreement": 1.0,
        "noWriteRoutes": 1.0,
    }
    gates = evaluator.build_hard_gates(
        perfect,
        deterministic=False,
        isolation_passed=False,
        source_hashes_valid=False,
        no_write_passed=False,
    )
    assert not gates["deterministicOutput"]["passed"]
    assert not gates["formalDataUnchanged"]["passed"]
    assert not gates["sourceHashesValid"]["passed"]
    assert not gates["isolatedDataset"]["passed"]
    assert not gates["noWriteRoutes"]["passed"]
    assert gates["casePassRate"]["passed"]
    assert not gates["overall"]["passed"]

    gates = evaluator.build_hard_gates(
        perfect,
        deterministic=True,
        isolation_passed=True,
        source_hashes_valid=True,
        no_write_passed=True,
    )
    assert gates["overall"]["passed"]

    broken = dict(perfect, bridgeIntegrity=0.5)
    gates = evaluator.build_hard_gates(
        broken,
        deterministic=True,
        isolation_passed=True,
        source_hashes_valid=True,
        no_write_passed=True,
    )
    assert not gates["overall"]["passed"]


def test_snapshot_evaluation_replays_all_cases_isolated(tmp_path):
    evaluator = _load_evaluator()
    assert hasattr(evaluator, "run_evaluation"), "阶段 4 快照执行器尚未实现"
    database_path = ROOT / "data" / "grain_oil.db"
    before = database_path.read_bytes()

    report = evaluator.run_evaluation(
        cases_path=CASES_PATH,
        database_path=database_path,
        workspace=tmp_path,
    )

    assert len(report["cases"]) == 8
    assert all(case["deterministic"] for case in report["cases"])
    assert report["datasetTier"] == "technical-smoke"
    assert report["businessQualityGate"] == "NOT_ASSESSED"
    assert report["productionPerformanceGate"] == "NOT_ASSESSED"
    assert report["sourceValidation"]["passed"]
    assert report["evaluationIsolation"]["passed"]
    assert report["noWriteConstraints"]["passed"]
    assert report["hardGates"]["overall"]["passed"]
    assert database_path.read_bytes() == before
