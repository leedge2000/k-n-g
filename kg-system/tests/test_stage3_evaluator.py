# -*- coding: utf-8 -*-
"""阶段 3 回溯评测：指标分母、硬门与快照只读执行。"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "ontology_ly" / "capability" / "stage3_backtest_smoke.json"
EVALUATOR_PATH = ROOT / "ontology_ly" / "tools" / "evaluate_stage3.py"


def _load_evaluator():
    assert EVALUATOR_PATH.is_file(), "阶段 3 评测器尚未实现"
    spec = importlib.util.spec_from_file_location("evaluate_stage3", EVALUATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cases_file_fixes_six_backtest_sections():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert cases["datasetTier"] == "technical-smoke"
    assert [case["caseId"] for case in cases["cases"]] == [
        "S3-BT-01", "S3-BT-02", "S3-BT-03",
        "S3-BT-04", "S3-BT-05", "S3-BT-06",
    ]
    assert [case["asOf"] for case in cases["cases"]] == [
        "2024-02-01", "2024-03-15", "2024-06-20",
        "2024-08-15", "2024-10-15", "2025-01-15",
    ]
    assert all(case["seriesId"] == "OBJ-PUB-IN-0001" for case in cases["cases"])
    assert all(item["sha256"] for item in cases["source"]["files"])


def test_summarize_cases_keeps_explicit_denominators():
    evaluator = _load_evaluator()
    rows = [
        {
            "passed": True,
            "lookaheadLeaks": 0,
            "eventStateChecks": [{"expected": "EFFECTIVE", "actual": "EFFECTIVE"}],
            "impactChecks": [{"expected": 1, "actual": 1}],
            "overviewChecks": [{"field": "pointCount", "expected": 16, "actual": 16}],
            "assertionChecks": [{"expected": 0, "actual": 0}],
            "transmissionChecks": [{"field": "edges", "expected": 4, "actual": 4}],
        },
        {
            "passed": False,
            "lookaheadLeaks": 1,
            "eventStateChecks": [{"expected": "ANNOUNCED", "actual": "EFFECTIVE"}],
            "impactChecks": [{"expected": 1, "actual": 0}],
            "overviewChecks": [{"field": "pointCount", "expected": 0, "actual": 6}],
            "assertionChecks": [],
            "transmissionChecks": [],
        },
    ]
    metrics = evaluator.summarize_cases(rows)
    assert metrics["caseCount"] == 2
    assert metrics["casePassRate"] == 0.5
    assert metrics["eventStateAgreement"] == 0.5
    assert metrics["impactAgreement"] == 0.5
    assert metrics["overviewAgreement"] == 0.5
    assert metrics["assertionGroupAgreement"] == 1.0
    assert metrics["transmissionIntegrity"] == 1.0
    assert metrics["lookaheadLeaks"] == 1


def test_summarize_cases_does_not_turn_empty_denominators_into_zero():
    evaluator = _load_evaluator()
    metrics = evaluator.summarize_cases([])
    assert metrics["caseCount"] == 0
    assert metrics["casePassRate"] is None
    assert metrics["eventStateAgreement"] is None
    assert metrics["impactAgreement"] is None
    assert metrics["overviewAgreement"] is None
    assert metrics["assertionGroupAgreement"] is None
    assert metrics["transmissionIntegrity"] is None
    assert metrics["lookaheadLeaks"] == 0


def test_hard_gates_require_determinism_isolation_and_read_only():
    evaluator = _load_evaluator()
    perfect = {
        "caseCount": 6,
        "casePassRate": 1.0,
        "eventStateAgreement": 1.0,
        "impactAgreement": 1.0,
        "overviewAgreement": 1.0,
        "assertionGroupAgreement": 1.0,
        "transmissionIntegrity": 1.0,
        "lookaheadLeaks": 0,
    }
    gates = evaluator.build_hard_gates(
        perfect,
        deterministic=False,
        isolation_passed=False,
        read_only_passed=False,
        source_hashes_valid=False,
    )
    assert not gates["deterministicOutput"]["passed"]
    assert not gates["formalDataUnchanged"]["passed"]
    assert not gates["readOnlyAccess"]["passed"]
    assert not gates["sourceHashesValid"]["passed"]
    assert gates["noLookahead"]["passed"]
    assert gates["casePassRate"]["passed"]
    assert not gates["overall"]["passed"]

    passed = evaluator.build_hard_gates(
        perfect,
        deterministic=True,
        isolation_passed=True,
        read_only_passed=True,
        source_hashes_valid=True,
    )
    assert passed["overall"]["passed"]


def test_lookahead_leak_fails_hard_gates():
    evaluator = _load_evaluator()
    metrics = {
        "caseCount": 6,
        "casePassRate": 1.0,
        "eventStateAgreement": 1.0,
        "impactAgreement": 1.0,
        "overviewAgreement": 1.0,
        "assertionGroupAgreement": 1.0,
        "transmissionIntegrity": 1.0,
        "lookaheadLeaks": 3,
    }
    gates = evaluator.build_hard_gates(
        metrics,
        deterministic=True,
        isolation_passed=True,
        read_only_passed=True,
        source_hashes_valid=True,
    )
    assert not gates["noLookahead"]["passed"]
    assert not gates["overall"]["passed"]


def test_snapshot_evaluation_replays_all_cases_read_only(tmp_path):
    evaluator = _load_evaluator()
    assert hasattr(evaluator, "run_evaluation"), "阶段 3 快照执行器尚未实现"
    database_path = ROOT / "data" / "grain_oil.db"
    before = database_path.read_bytes()

    report = evaluator.run_evaluation(
        cases_path=CASES_PATH,
        database_path=database_path,
        workspace=tmp_path,
    )

    assert len(report["cases"]) == 6
    assert all(case["deterministic"] for case in report["cases"])
    assert all(case["lookaheadLeaks"] == 0 for case in report["cases"])
    assert report["datasetTier"] == "technical-smoke"
    assert report["businessQualityGate"] == "NOT_ASSESSED"
    assert report["productionPerformanceGate"] == "NOT_ASSESSED"
    assert report["sourceValidation"]["passed"]
    assert report["evaluationIsolation"]["passed"]
    assert report["readOnlyAccess"]["passed"]
    assert report["hardGates"]["overall"]["passed"]
    assert database_path.read_bytes() == before
