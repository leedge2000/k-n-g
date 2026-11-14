# 规格：结论层引用编号与政策 policyId 打通

## 1. 现状核实（决定实现路径的事实）

| 事实 | 证据 |
|---|---|
| 应用层**没有**结论层实现 | `kg-system/app/` 无 conclusion / judge 模块；`grep 六步\|证明链` 只命中 `rule_trial/judge_pydatalog.py`、`rule_trial/REPORT.md`、`packs/policy/clause_model.json` |
| 结论层契约已冻结但未落地 | `clause_model.json` 的 `judgeAlgorithm`：输入 `{statementId, facts, asOf}`，三态 `APPLICABLE / NOT_APPLICABLE / UNABLE`，六步顺序固定 |
| 证明链每步只有 `{step, ruleId, factRef, clausePath, quote}` | `clause_model.json` `proofChainRequirement.eachStepShape`，**没有 policyId** |
| 应用库有可引用的政策与条款 | `seed/business.json`：`Policy`（POL0001）、`Policy_Clause`（POL0001-C001..C003，含 `clauseNo/clauseText/targetObject/measureType/clauseEffectiveDate`） |
| 应用库**没有** `NormativeStatement / ClauseCondition / ClauseException / ClauseRelation` 表 | `seed/mapping.json` 无对应表；阶段 2b 尚未落地 |
| 现有“引用编号”只有 RAG 答案里的 `[P1]`（政策）与 `[1]`（片段） | `app/rag.py::_policy_text` 生成 `[P{i}]`；`citations` 仅含片段，`[P1]` 无结构化映射到 `policyId` |

结论：要把“引用编号 ↔ policyId”打通，必须在应用层新增结论层 v1，并统一政策引用编号。

## 2. 目标

1. 新增应用层结论层 `ConclusionService`，按冻结的**六步顺序**给出三态结论与证明链。
2. 证明链每一步带 `policyId`（并带 `clauseId`），使 `clausePath / quote` 可回溯到具体政策记录。
3. 统一“政策引用编号” `P1..Pn`：同一套编号同时出现在 `/api/policies/effective`、RAG 答案、结论层证明链，且 `P{n} -> policyId` 映射唯一、可校验。
4. `quote` 必须是 `clauseText` 的逐字子串，可定位。

## 3. 非目标（本规格明确不做）

- 不做 `NormativeStatement / ClauseCondition / ClauseException / ClauseRelation` 的建表与抽取（属阶段 2b）。
- 不在应用层嵌入 Nemo/pyDatalog 推理引擎（属阶段 2c）；v1 用确定性条件树实现同等六步顺序。
- 不推断 `PREVAILS_OVER` 优先级与例外消解：数据未登记时**如实标注**并降级，不伪造结论。
- 不改 `PolicyService.effective` 的 asOf 与 scope 口径（沿用 §8.1 与“政策范围覆盖查询范围”）。

## 4. 口径决定（默认值，可改）

| 项 | 默认口径 |
|---|---|
| 结论粒度 | **条款级**（`Policy_Clause`）作为最小可引用单元。原因：`NormativeStatement` 表尚未存在；条款已带 `policyId/clauseNo/clauseText`，是当前唯一能稳定回溯的引用单元 |
| 引用编号 | 按 `/api/policies/effective` 返回顺序，对 `items` 依次编号 `P1..Pn`；同一响应内 `policyId` 与 `P{n}` 一一对应；编号不进库，请求内稳定 |
| 第 1 步“规则可用” | 只取 `effective(asOf, scopeId)` 命中的政策条款；政策非现行有效 → 该条款不参与，且结论 `UNABLE(reason="政策在 asOf 非现行有效")` |
| 第 2 步“模糊用语” | 条款正文命中模糊词表 `适当/必要时/有关部门/原则上/及时/视情况` → `UNABLE`，`vaguenessNote` 标出命中词 |
| 第 3 步“前提” | 事实键 `commodity` 缺失 → `UNABLE(缺事实：品种)`；`facts.commodity != clause.targetObject` → `NOT_APPLICABLE(前提不满足：品种)`；`asOf < clauseEffectiveDate` → `NOT_APPLICABLE(前提不满足：条款未生效)` |
| 第 4 步“例外” | 未登记 `ClauseException` → 跳过，`notes` 标注“未登记例外，未做例外消解” |
| 第 5 步“优先级冲突” | 未登记 `ClauseRelation(PREVAILS_OVER)` → 跳过，`notes` 标注“未登记优先级关系，未做冲突裁决” |
| 第 6 步“结论” | 前五步均通过 → `APPLICABLE`；**证明链为空时一律降级 `UNABLE`**（`noChainNoAnswer`） |
| `scopeId` 缺省 | 不限范围（沿用既有口径） |
| `asOf` 缺省 | 东八区当天（复用 `PolicyService`） |

## 5. 接口与数据结构

### 5.1 证明链步骤

```
{
  "step": 1..6,
  "ruleId": "POL0001-C001",        // 与 clauseId 同值，保留冻结字段名
  "policyId": "POL0001",           // 新增：政策主键
  "clauseId": "POL0001-C001",      // 新增：条款主键
  "citation": "P1",                // 新增：政策引用编号，P{n} ↔ policyId
  "clausePath": "POL0001 第1条",   // 条款路径，由 policyTitle + clauseNo 组成
  "clauseNo": "第1条",             // 新增：原始编号
  "factRef": "facts.commodity",    // 该步依据的事实；非事实步骤为 null
  "quote": "2025年生产的早籼稻最低收购价为每50公斤128元。"  // 必须是 clauseText 逐字子串
}
```

### 5.2 `POST /api/policies/judge`

请求：

```json
{
  "asOf": "2025-06-01",
  "scopeId": "SCP-PUB-...",
  "facts": { "commodity": "早籼稻", "price": 126 }
}
```

响应：

```json
{
  "asOf": "2025-06-01",
  "scopeId": null,
  "citationIndex": { "P1": "POL0001" },
  "verdicts": [
    {
      "clauseId": "POL0001-C001",
      "policyId": "POL0001",
      "citation": "P1",
      "outcome": "APPLICABLE",
      "reason": "条件全部成立且无例外触发",
      "missing": [],
      "notes": ["未登记例外，未做例外消解", "未登记优先级关系，未做冲突裁决"],
      "proofChain": [ ... 5.1 ... ]
    }
  ]
}
```

`asOf` 显式传入时响应头 `Cache-Control: no-store`（与 effective 一致）。

### 5.3 `GET /api/policies/effective` 增补

每个 `items[i]` 增加 `"citation": "P{i+1}"`；响应增加 `"citationIndex": {"P1": "POL0001", ...}`。其余字段与口径不变。

### 5.4 RAG 复用同一编号

`RagService._policy_text` 优先取 `item["citation"]`，缺失时回退 `[P{i}]`；`policyContext` 原样带出 `citation`。保证答案里的 `P{n}` 与 `citationIndex` 一致。

## 6. 改动清单（精确到文件与函数）

| 文件 | 改动 |
|---|---|
| `ontology_ly/packs/policy/clause_model.json` | `schemaVersion` 0.1.0→0.2.0；`proofChainRequirement.eachStepShape` 改为含 `policyId/clauseId/citation/clauseNo`；新增 `citationRule` 与 `policyLinkRule` 说明；`frozen` 保持 true |
| `app/policy.py` | `_item()` 增加 `citation`（由 `effective()` 传入序号）；`effective()` 返回值增加 `citationIndex`；新增静态方法 `citation_index(items)` |
| `app/conclusion.py`（新增） | `ConclusionService(db, policy_service)`；`judge(facts, as_of=None, scope_id=None)`；`VAGUE_TERMS`；`_clauses(policy_ids)`；`_judge_clause(...)`；`_proof(...)` |
| `app/main.py` | lifespan 增加 `app.state.conclusion = ConclusionService(db, app.state.policy)`；新增 `POST /api/policies/judge`（`ValueError -> 422`，显式 `asOf` 加 `no-store`） |
| `app/rag.py` | `_policy_text` 使用 `item.get("citation")`；其余不变 |
| `tests/test_conclusion.py`（新增） | 见 §7 |
| `tests/test_policy.py` | 增加 1 条断言：`effective` 的 `items` 带 `citation` 且 `citationIndex` 反向可查 |

## 7. 验收与测试

单测（`pytest tests/test_conclusion.py -q`）：

1. `APPLICABLE`：POL0001-C001 + `commodity=早籼稻` + `asOf=2025-06-01` → 三态为 `APPLICABLE`，证明链非空。
2. 引用打通：证明链第 1 步 `policyId == "POL0001"` 且 `citationIndex[citation] == policyId`。
3. `quote` 可定位：每步 `quote` 是 `Policy_Clause.clauseText` 的逐字子串。
4. `NOT_APPLICABLE`：`commodity=小麦` → 原因含“前提不满足：品种”。
5. `UNABLE`：缺 `commodity` → `missing` 含“品种”；`asOf=2024-01-01` → `UNABLE` 或条款未生效的 `NOT_APPLICABLE`（以第 1 步政策时效优先，断言为 `UNABLE`）。
6. 模糊用语：插入含“必要时”的条款 → `UNABLE` 且 `reason` 含“含模糊用语”。
7. 未知 `scopeId` → HTTP 422，`detail` 含“范围不存在”。
8. `citation` 与 `effective` 一致：同一 policy 在 `effective` 与 `judge` 中 `citation` 相同。

回归（收口必跑）：

```
cd c:\粮油知识图谱\kg-system
python -m pytest -q
python ontology_ly/tools/validate_stage0.py
```

通过线：`pytest` 全绿（当前基线 53 passed / 3 skipped，新增用例后同比例增长）；`validate_stage0.py` `score=100 / error=0 / warn=0`。

## 8. 风险与回滚

- **冻结契约变更**：`clause_model.json` 升到 0.2.0，属加法式扩展（只增字段），不删改既有字段；若需保持 0.1.0，可改为在响应中平铺 `policyId` 而不动契约。
- **条款级 ≠ 陈述级**：v1 判定不到 `NormativeStatement` 粒度，结论会偏粗；在响应 `notes` 中显式声明，待 2b 落地后替换数据源，接口不变。
- **引用编号不稳定**：`P{n}` 依赖政策列表顺序；已在响应内附 `citationIndex`，前端应以映射为准，不缓存跨请求编号。

## 9. 需确认事项

1. 是否接受“先做条款级结论层 v1”，而不是等 2b 的 `NormativeStatement` 表（默认：接受）。
2. 是否接受把 `clause_model.json` 从 0.1.0 升到 0.2.0 以写入 `policyId/clauseId/citation`（默认：接受；若拒绝则改为响应层平铺）。
3. 是否同意 `P{n}` 采用“请求内顺序编号”而非持久化编号（默认：同意）。
