# 任务分解：结论层引用编号与 policyId 打通

> 依赖 `spec.md`。审批通过后按序执行，每完成一项用 TodoWrite 勾选。

## T1 契约升级：证明链携带 policyId

- 文件：`ontology_ly/packs/policy/clause_model.json`
- 改动：
  - `schemaVersion` → `0.2.0`
  - `judgeAlgorithm.proofChainRequirement.eachStepShape` →
    `"{ step, ruleId, policyId, clauseId, citation, clausePath, clauseNo, factRef, quote }"`
  - 新增 `citationRule`：`P{n}` 由 `/api/policies/effective` 返回顺序确定，请求内唯一
  - 新增 `policyLinkRule`：`citation -> policyId -> clauseId` 三级可回溯；`quote` 必须是 `clauseText` 子串
- 验证：`python -c "import json;json.load(open('ontology_ly/packs/policy/clause_model.json',encoding='utf-8'))"` 无异常

## T2 政策引用编号：effective 增补 citation / citationIndex

- 文件：`app/policy.py`
- 改动：
  - 新增 `@classmethod def citation_index(cls, items) -> dict[str, str]`
  - `_item(row, citation=None)` 增加 `"citation"` 字段
  - `effective()` 在组装 `items[:limit]` 前按序赋 `P{i}`，返回值增加 `"citationIndex"`
- 验证：`pytest tests/test_policy.py -q`

## T3 结论层服务：ConclusionService

- 文件：`app/conclusion.py`（新增）
- 改动：
  - `VAGUE_TERMS = ("适当","必要时","有关部门","原则上","及时","视情况")`
  - `__init__(self, db, policy_service)`
  - `judge(facts, as_of=None, scope_id=None) -> dict`
  - `_clauses(policy_ids) -> list[dict]`：按 `policyId` 批量读 `Policy_Clause`
  - `_judge_clause(clause, citation, facts, target_day) -> dict`：六步顺序，返回 `outcome/reason/missing/notes/proofChain`
  - `_proof(...)`：生成 §5.1 结构，`quote` 取 `clauseText` 子串
- 约束：不做例外/优先级推断，`notes` 如实标注；证明链为空时 `APPLICABLE` 降级 `UNABLE`
- 验证：`pytest tests/test_conclusion.py -q`

## T4 端点与接线

- 文件：`app/main.py`
- 改动：
  - import `ConclusionService`
  - lifespan：`app.state.conclusion = ConclusionService(db, app.state.policy)`（在 `policy` 之后）
  - `POST /api/policies/judge`：body `{asOf?, scopeId?, facts}`；`ValueError -> 422`；显式 `asOf` 返回 `Cache-Control: no-store`
- 验证：`pytest tests/test_conclusion.py -q`

## T5 RAG 复用同一编号

- 文件：`app/rag.py`
- 改动：`_policy_text` 中 `[P{i}]` 改为优先 `item.get("citation")`
- 验证：`pytest tests/test_policy.py -q`（既有 `test_policy_text_feeds_answer_context` 仍通过）

## T6 用例

- 文件：`tests/test_conclusion.py`（新增）、`tests/test_policy.py`（补 1 条）
- 覆盖 §7 的 8 条
- 验证：`pytest tests/test_conclusion.py -q`、`pytest tests/test_policy.py -q`

## T7 收口

- 命令：
  ```
  cd c:\粮油知识图谱\kg-system
  python -m pytest -q
  python ontology_ly/tools/validate_stage0.py
  ```
- 通过线：pytest 全绿；`score=100 / error=0 / warn=0`
