# -*- coding: utf-8 -*-
"""阶段 5 运营评测：指标分母、硬门与逐情形隔离执行。"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "ontology_ly" / "capability" / "stage5_ops_smoke.json"
EVALUATOR_PATH = ROOT / "ontology_ly" / "tools" / "evaluate_stage5.py"


def _load_evaluator():
    assert EVALUATOR_PATH.is_file(), "阶段 5 评测器尚未实现"
    spec = importlib.util.spec_from_file_location("evaluate_stage5", EVALUATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cases_file_fixes_eight_ops_sections():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert cases["datasetTier"] == "technical-smoke"
    assert [case["caseId"] for case in cases["cases"]] == [
        "S5-OPS-01", "S5-OPS-02", "S5-OPS-03", "S5-OPS-04",
        "S5-OPS-05", "S5-OPS-06", "S5-OPS-07", "S5-OPS-08",
    ]
    assert all(case["kind"] for case in cases["cases"])
    assert all(item["sha256"] for item in cases["source"]["files"])


def test_summarize_cases_keeps_explicit_denominators():
    evaluator = _load_evaluator()
    rows = [
        {
            "passed": True,
            "checks": [
                {"group": "metric", "matched": True},
                {"group": "alert", "matched": True},
                {"group": "ingest", "matched": True},
                {"group": "evolution", "matched": True},
                {"group": "rollback", "matched": True},
                {"group": "gate", "matched": True},
                {"group": "permission", "matched": True},
                {"group": "knowledge", "matched": True},
            ],
        },
        {
            "passed": False,
            "checks": [
                {"group": "metric", "matched": False},
                {"group": "alert", "matched": False},
                {"group": "ingest", "matched": False},
                {"group": "evolution", "matched": False},
                {"group": "rollback", "matched": False},
                {"group": "gate", "matched": False},
                {"group": "permission", "matched": False},
                {"group": "knowledge", "matched": False},
            ],
        },
    ]
    metrics = evaluator.summarize_cases(rows)
    assert metrics["caseCount"] == 2
    assert metrics["casePassRate"] == 0.5
    assert metrics["metricAgreement"] == 0.5
    assert metrics["alertLifecycleAgreement"] == 0.5
    assert metrics["ingestAgreement"] == 0.5
    assert metrics["evolutionAgreement"] == 0.5
    assert metrics["rollbackRestoreIntegrity"] == 0.5
    assert metrics["gateNegativeDetectionRate"] == 0.5
    assert metrics["permissionAgreement"] == 0.5
    assert metrics["knowledgeTablesUnchanged"] == 0.5


def test_summarize_cases_does_not_turn_empty_denominators_into_zero():
    evaluator = _load_evaluator()
    metrics = evaluator.summarize_cases([])
    for key in (
        "casePassRate", "metricAgreement", "alertLifecycleAgreement", "ingestAgreement",
        "evolutionAgreement", "rollbackRestoreIntegrity", "gateNegativeDetectionRate",
        "permissionAgreement", "knowledgeTablesUnchanged",
    ):
        assert metrics[key] is None, key
    assert metrics["caseCount"] == 0


def test_hard_gates_require_determinism_isolation_and_source_hashes():
    evaluator = _load_evaluator()
    perfect = {
        "caseCount": 8,
        "casePassRate": 1.0,
        "metricAgreement": 1.0,
        "alertLifecycleAgreement": 1.0,
        "ingestAgreement": 1.0,
        "evolutionAgreement": 1.0,
        "rollbackRestoreIntegrity": 1.0,
        "gateNegativeDetectionRate": 1.0,
        "permissionAgreement": 1.0,
        "knowledgeTablesUnchanged": 1.0,
    }
    gates = evaluator.build_hard_gates(
        perfect,
        deterministic=False,
        isolation_passed=False,
        source_hashes_valid=False,
    )
    assert not gates["deterministicOutput"]["passed"]
    assert not gates["formalDataUnchanged"]["passed"]
    assert not gates["sourceHashesValid"]["passed"]
    assert not gates["isolatedDataset"]["passed"]
    assert gates["casePassRate"]["passed"]
    assert not gates["overall"]["passed"]

    gates = evaluator.build_hard_gates(
        perfect,
        deterministic=True,
        isolation_passed=True,
        source_hashes_valid=True,
    )
    assert gates["overall"]["passed"]

    broken = dict(perfect, knowledgeTablesUnchanged=0.5)
    gates = evaluator.build_hard_gates(
        broken,
        deterministic=True,
        isolation_passed=True,
        source_hashes_valid=True,
    )
    assert not gates["overall"]["passed"]


def test_snapshot_evaluation_replays_all_cases_isolated(tmp_path):
    evaluator = _load_evaluator()
    assert hasattr(evaluator, "run_evaluation"), "阶段 5 快照执行器尚未实现"
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
    assert report["hardGates"]["overall"]["passed"]
    assert database_path.read_bytes() == before
