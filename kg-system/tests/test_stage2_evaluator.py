import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_PATH = ROOT / "ontology_ly" / "capability" / "stage2_reasoning_smoke.json"
EVALUATOR_PATH = ROOT / "ontology_ly" / "tools" / "evaluate_stage2.py"


def _load_evaluator():
    assert EVALUATOR_PATH.is_file(), "阶段 2 评测器尚未实现"
    spec = importlib.util.spec_from_file_location("evaluate_stage2", EVALUATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_separates_policy_and_synthetic_suites():
    smoke = json.loads(SMOKE_PATH.read_text(encoding="utf-8"))
    assert smoke["datasetTier"] == "technical-smoke"
    assert set(smoke["suites"]) == {"pol0001", "syntheticSafety"}
    cases = [case for suite in smoke["suites"].values() for case in suite["cases"]]
    assert len(cases) == 15
    assert len({case["caseId"] for case in cases}) == len(cases)
    assert all(case["expectedOutcome"] in {"APPLICABLE", "NOT_APPLICABLE", "UNABLE"} for case in cases)
    assert all(case["expectedReasonContains"] for case in cases)
    assert all(item["sha256"] for item in smoke["source"]["files"])


def test_metrics_keep_three_state_and_explanation_denominators():
    evaluator = _load_evaluator()
    rows = [
        {
            "expectedOutcome": "APPLICABLE", "actualOutcome": "APPLICABLE",
            "explanationUsable": True, "scopeLabelled": True,
            "applicableProofComplete": True, "outputCount": 1,
        },
        {
            "expectedOutcome": "UNABLE", "actualOutcome": "UNABLE",
            "explanationUsable": True, "scopeLabelled": True,
            "applicableProofComplete": None, "outputCount": 1,
        },
        {
            "expectedOutcome": "UNABLE", "actualOutcome": "NOT_APPLICABLE",
            "explanationUsable": False, "scopeLabelled": False,
            "applicableProofComplete": None, "outputCount": 1,
        },
        {
            "expectedOutcome": "NOT_APPLICABLE", "actualOutcome": "NOT_APPLICABLE",
            "explanationUsable": True, "scopeLabelled": True,
            "applicableProofComplete": None, "outputCount": 1,
        },
    ]
    metrics = evaluator.summarize_suite(rows)
    assert metrics["threeStateAgreement"] == 0.75
    assert metrics["unableRecognitionRate"] == 0.5
    assert metrics["fatalMisjudgementRate"] == 0.0
    assert metrics["explanationUsabilityRate"] == 0.75
    assert metrics["scopeLabelCompletenessRate"] == 0.75
    assert metrics["applicableProofCompletenessRate"] == 1.0
    assert metrics["confusionMatrix"]["UNABLE"]["NOT_APPLICABLE"] == 1


def test_metrics_do_not_turn_empty_denominators_into_zero():
    evaluator = _load_evaluator()
    metrics = evaluator.summarize_suite([])
    assert metrics["threeStateAgreement"] is None
    assert metrics["unableRecognitionRate"] is None
    assert metrics["fatalMisjudgementRate"] is None
    assert metrics["applicableProofCompletenessRate"] is None


def test_hard_gates_require_determinism_isolation_and_real_nemo():
    evaluator = _load_evaluator()
    perfect = {
        "threeStateAgreement": 1.0,
        "unableRecognitionRate": 1.0,
        "fatalMisjudgementRate": 0.0,
        "explanationUsabilityRate": 1.0,
        "scopeLabelCompletenessRate": 1.0,
        "applicableProofCompletenessRate": 1.0,
        "expectedReasonsMatched": True,
    }
    gates = evaluator.build_hard_gates(
        {"pol0001": perfect, "syntheticSafety": perfect},
        deterministic=False,
        isolation_passed=False,
        nemo_execution={"version": "0.10.1", "executed": 0, "successful": 0, "skipped": 15},
        source_hashes_valid=False,
        activation_boundary=False,
    )
    assert not gates["deterministicOutput"]["passed"]
    assert not gates["formalDataUnchanged"]["passed"]
    assert not gates["realNemoExecution"]["passed"]
    assert not gates["sourceHashesValid"]["passed"]
    assert not gates["activationBoundary"]["passed"]
    assert not gates["threeStateAgreement"]["passed"] is False  # 指标本身仍应通过
    assert not all(gate["passed"] for gate in gates.values())


def test_snapshot_evaluation_runs_all_cases_without_touching_formal_data(tmp_path):
    evaluator = _load_evaluator()
    assert hasattr(evaluator, "run_evaluation"), "阶段 2 快照执行器尚未实现"
    database_path = ROOT / "data" / "grain_oil.db"
    before = database_path.read_bytes()

    report = evaluator.run_evaluation(
        smoke_path=SMOKE_PATH,
        database_path=database_path,
        workspace=tmp_path,
    )

    rows = [row for suite in report["suites"].values() for row in suite["cases"]]
    assert len(rows) == 15
    assert all(row["expectedReasonMatched"] for row in rows)
    assert all(row["outputShapeMatched"] for row in rows)
    assert next(row for row in rows if row["caseId"] == "S2-SAF-007")["outputCount"] == 2
    assert all(row["deterministic"] for row in rows)
    assert report["datasetTier"] == "technical-smoke"
    assert report["businessQualityGate"] == "NOT_ASSESSED"
    assert report["productionPerformanceGate"] == "NOT_ASSESSED"
    assert report["nemoExecution"]["executed"] > 0
    assert report["nemoExecution"]["successful"] > 0
    assert report["sourceValidation"]["passed"]
    assert report["activationBoundary"]["passed"]
    assert report["evaluationIsolation"]["passed"]
    assert report["hardGates"]["overall"]["passed"]
    assert database_path.read_bytes() == before
