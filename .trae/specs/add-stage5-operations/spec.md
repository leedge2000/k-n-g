# 阶段 5「运营」Spec

## Why

阶段 1—4 已交付知识入库、政策规则、市场行情与联动，但运营侧仍是散点：
质量治理只有 `QisRecord` 与一次性 `detect`，没有指标趋势与告警；采集只有单次触发与运行表，没有源健康与失败重跑；
本体与规则口径的调整只能直接改库或改定义层文件，没有审批、版本与回滚。
阶段 5 把这三块收敛成一个可巡检、可追责、可回滚的运营闭环。

## What Changes

- 新增 4 张物理表：`OpsQualityMetric`、`OpsAlert`、`OpsEvolutionRequest`、`OpsEvolutionRelease`（映射 `M-MAP-OPS-017`—`020`，零新增本体类型）。
- 新增 `app/ops.py`：8 个固定指标口径、7 条告警规则、巡检服务、演进状态机（`DRAFT→PENDING_REVIEW→APPROVED→PUBLISHED→ROLLED_BACK`）。
- 新增 `OpsScheduler`：随 `KG_SCHEDULER_ENABLED` 启动，默认 30 分钟一轮巡检；间隔由 `KG_OPS_INSPECT_INTERVAL_MINUTES` 控制。
- 新增 `OpsService.validate_stage5()` 门禁 G5-1—G5-8，`initialize()` 末尾调用。
- `OntologyService.sync()` 末尾回放已发布的 `ONTOLOGY_META` 演进，避免被定义层覆盖。
- 新增 15 条 `/api/ops` 路由与 `GET /ops.html`。
- 新增 `web/ops.html`（四分区工作台），并在 `index.html`/`market.html`/`graph.html` 增加互链入口。

不做（已冻结）：定义层结构热更、外部告警推送、自定义指标公式、采集增量 diff、知识对象新写入口、既有 API 结构变更。

## Impact

- Affected specs：运营包（`ontology_ly/packs/ops/`）、本体内核映射（`seed/mapping.json`）、本体服务（`app/ontology.py`）。
- Affected code：
  - `seed/mapping.json`
  - `app/ops.py`（新增，主战场）
  - `app/config.py`、`app/main.py`
  - `app/ontology.py`
  - `web/ops.html`（新增）、`web/index.html`、`web/market.html`、`web/graph.html`
  - `tests/test_stage5.py`（新增）
- 依据：`ontology_ly/packs/ops/stage5_运营口径清单.md`（冻结稿）、`stage5_任务分解.md`。

## ADDED Requirements

### Requirement: 质量指标快照

系统 SHALL 定义 8 个固定质量指标，并在每次巡检时写入一行 `OpsQualityMetric` 快照。

#### Scenario: 指标快照落到库
- **WHEN** 调用 `POST /api/ops/inspect`
- **THEN** 8 个 `metricCode` 各新增一条快照，`status` 与阈值方向一致

#### Scenario: 阈值判定按方向
- **WHEN** `CONFLICT_PENDING`（方向 `LTE`，阈值 0）的值为 1
- **THEN** 该快照 `status='WARN'`

#### Scenario: 值为比例类保留 4 位
- **WHEN** `SRC_COVERAGE` 计算为 19/20
- **THEN** 快照值与接口返回均为 `0.95`

### Requirement: 告警生命周期

系统 SHALL 按 7 条告警规则开单，并保证同一 `alertCode` 同时最多一条 `OPEN`。

#### Scenario: 触发不重复开单
- **WHEN** 连续两轮巡检都命中 `ALT-CONFLICT`
- **THEN** `OpsAlert` 中该 `alertCode` 只有一条 `OPEN`

#### Scenario: 自动关闭
- **WHEN** 上轮命中的指标回到阈值内
- **THEN** 对应 `OPEN/ACK` 记录被置 `CLOSED` 并写入 `closedAt`

#### Scenario: 人工认领
- **WHEN** `ROLE-01` 对 `OPEN` 告警调用 `ack`
- **THEN** 状态变 `ACK`，记录 `ackedBy/ackedAt`，重复认领返回 409

### Requirement: 增量入库看板

系统 SHALL 聚合采集源健康、待审核积压与变更流游标，并提供受控失败重跑。

#### Scenario: 超期源标记
- **WHEN** 启用源的 `lastRunAt` 早于 `2 × minIntervalMinutes`
- **THEN** 该源在 `/api/ops/ingest` 中 `staleFlag=true`

#### Scenario: 重跑上限
- **WHEN** 某源上次成功后的连续失败次数已达 `maxRetry`
- **THEN** `POST /api/ops/ingest/retry` 返回 409，不触发采集

#### Scenario: 变更流游标
- **WHEN** 读取 `/api/ops/ingest`
- **THEN** 返回最近 20 条变更与最新 `updatedAt` 游标，可直接用于 `/api/kg/changes?since=`

### Requirement: 演进草稿—评审—发布

系统 SHALL 以状态机约束演进申请，评审人与提交人必须不同，发布后可回滚。

#### Scenario: 非法跳转被拒
- **WHEN** 对 `DRAFT` 申请直接调用 `publish`
- **THEN** 返回 409

#### Scenario: 职责分离
- **WHEN** 评审通过的 `reviewedBy` 与 `submittedBy` 相同
- **THEN** `publish` 返回 409

#### Scenario: 范围演进发布
- **WHEN** `targetKind='SCOPE'` 且 `changeType='UPDATE'` 的申请发布
- **THEN** `OntologyScope` 的 `members/normalizedKey/memberCount/version` 更新，`version+1`

#### Scenario: 规则演进发布
- **WHEN** `targetKind='RULE'` 的申请发布
- **THEN** `CompiledRule` 的目标字段更新，`previousPayload` 记录发布前值

#### Scenario: 元数据演进不被定义层覆盖
- **WHEN** 发布 `ONTOLOGY_META` 后调用 `POST /api/ontology/refresh`
- **THEN** 修改后的 `displayName/description/status` 仍生效

#### Scenario: 回滚还原
- **WHEN** 对 `PUBLISHED` 申请执行 `rollback`
- **THEN** 目标数据还原为 `previousPayload`，申请与版本均变为 `ROLLED_BACK`

### Requirement: 运营门禁

系统 SHALL 在启动时执行 G5-1—G5-8，覆盖表结构、指标一致性、告警唯一性、状态机与版本连续性。

#### Scenario: 门禁失败即启动失败
- **WHEN** 存在两条同 `alertCode` 的 `OPEN` 告警
- **THEN** `initialize()` 抛 `OpsError`，启动中止

#### Scenario: 版本号连续
- **WHEN** 发布版本 `versionNo` 出现跳号或重复
- **THEN** G5-6 失败并给出编号

### Requirement: 定时巡检

系统 SHALL 在 `KG_SCHEDULER_ENABLED=true` 时启动运营巡检线程，默认 30 分钟一轮。

#### Scenario: 启动即巡检
- **WHEN** 服务以 `scheduler_enabled=True` 启动
- **THEN** `OpsQualityMetric` 在启动后出现首轮数据

#### Scenario: 单轮异常不终止
- **WHEN** 某一轮巡检抛异常
- **THEN** 记录日志并继续下一轮，主服务不受影响
