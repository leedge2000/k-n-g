# 阶段 5 运营口径清单（冻结稿）

> 依据：`ontology_ly/方案定稿.md` L354 ——「5 运营 | 增量入库、本体与规则演进、质量监控 | 不变」。
> 本稿冻结阶段 5 的口径边界，实现不得越界；未列明项一律视为「不做」。

## 0 结论摘要

| 项 | 冻结结论 |
|---|---|
| 范围 | 三块全做：增量入库、质量监控、本体与规则演进 |
| 入口 | 新增 `web/ops.html` 运营工作台，导航与既有三页互链 |
| 持久化 | 新增 4 张表：`OpsQualityMetric`、`OpsAlert`、`OpsEvolutionRequest`、`OpsEvolutionRelease` |
| 巡检 | 新增 `OpsScheduler`，随 `KG_SCHEDULER_ENABLED` 启动，默认 30 分钟一轮 |
| 演进 | 草稿 → 提交评审 → 评审 → 发布 → 回滚；评审人与提交人必须不同 |
| 门禁 | `OpsService.validate_stage5()` 的 G5-1—G5-8 |

## 1 范围边界

### 1.1 做

1. **增量入库**：采集源健康、运行成功率、失败重跑、待审核积压、变更流游标，聚合成一块看板。
2. **质量监控**：8 个指标快照落库 + 阈值判定 + 告警生命周期（OPEN/ACK/CLOSED）+ 定时巡检。
3. **本体与规则演进**：`SCOPE`（维度与范围口径）、`RULE`（编译规则）、`ONTOLOGY_META`（对象类型非结构元数据）三类目标的版本化变更流。

### 1.2 不做（冻结）

- 不改 `seed/mapping.json` 结构的热更新：对象/关系/动作类型仍由定义层编译，运行时只允许改非结构元数据。
- 不做外部消息推送（邮件/飞书/短信），告警只在系统内可见。
- 不做指标的自定义公式编辑（指标集合在代码常量 `METRICS` 中固定）。
- 不做采集源页面的多页抓取、登录态抓取、增量 diff 抽取；沿用现有「整页正文 + checksum 去重」。
- 不新增任何知识对象的写入口；演进发布只写本体/规则侧的表。
- 不改动阶段 1—4 既有 API 的请求/响应结构。

## 2 数据表口径

新增 4 张表，`tableRole=MASTER`（自动带 `createdBy/createdAt/updatedBy/updatedAt` 审计列）。
映射编号 `M-MAP-OPS-017`—`M-MAP-OPS-020`。

### 2.1 `OpsQualityMetric`（质量指标快照）

| 列 | 类型 | 说明 |
|---|---|---|
| metricId | TEXT PK | `MQM0001` 递增 |
| metricCode | TEXT NOT NULL | 指标编码，取值见 §3.1 |
| metricName | TEXT NOT NULL | 指标中文名 |
| metricValue | REAL NOT NULL | 指标值 |
| unit | TEXT | 单位（`RATIO` / `COUNT`） |
| threshold | REAL | 判定阈值 |
| direction | TEXT NOT NULL | `GTE`（越大越好）/ `LTE`（越小越好） |
| status | TEXT NOT NULL | `OK` / `WARN` / `BREACH` |
| measuredAt | TEXT NOT NULL | 采集时刻（UTC） |
| detail | TEXT | 明细 JSON（分组计数等） |

索引：`("metricCode","measuredAt")`、`("measuredAt")`。

### 2.2 `OpsAlert`（运营告警）

| 列 | 类型 | 说明 |
|---|---|---|
| alertId | TEXT PK | `ALT0001` 递增 |
| alertCode | TEXT NOT NULL | 告警编码，见 §3.2 |
| severity | TEXT NOT NULL | `P1` / `P2` / `P3` |
| title | TEXT NOT NULL | 标题 |
| detail | TEXT | 明细 |
| metricCode | TEXT | 关联指标 |
| metricValue | REAL | 触发时指标值 |
| status | TEXT NOT NULL | `OPEN` / `ACK` / `CLOSED` |
| raisedAt | TEXT NOT NULL | 触发时刻 |
| ackedBy / ackedAt | TEXT | 认领信息 |
| closedAt | TEXT | 关闭时刻 |

约束：同一 `alertCode` 同时最多一条 `OPEN`（服务层保证，G5-3 校验）。

### 2.3 `OpsEvolutionRequest`（演进申请）

| 列 | 类型 | 说明 |
|---|---|---|
| changeId | TEXT PK | `OCK0001` 递增 |
| targetKind | TEXT NOT NULL | `SCOPE` / `RULE` / `ONTOLOGY_META` |
| targetId | TEXT | 目标主键；新建类变更可空 |
| changeType | TEXT NOT NULL | `CREATE` / `UPDATE` / `ARCHIVE` |
| draftPayload | TEXT NOT NULL | 申请载荷 JSON |
| previousPayload | TEXT | 发布前快照 JSON（发布时写入） |
| status | TEXT NOT NULL | `DRAFT`/`PENDING_REVIEW`/`APPROVED`/`REJECTED`/`PUBLISHED`/`ROLLED_BACK` |
| impactSummary | TEXT | 影响面说明 |
| submittedBy / submittedAt | TEXT | 提交评审 |
| reviewedBy / reviewedAt / reviewRemark | TEXT | 评审 |
| publishedAt | TEXT | 发布 |
| releaseId | TEXT | 归属发布版本 |
| updatedAt | TEXT | 最后变更 |

### 2.4 `OpsEvolutionRelease`（发布版本）

| 列 | 类型 | 说明 |
|---|---|---|
| releaseId | TEXT PK | `RVW0001` 递增 |
| versionNo | INTEGER NOT NULL UNIQUE | 从 1 连续递增 |
| summary | TEXT | 版本说明 |
| changeIds | TEXT NOT NULL | 本次发布包含的 changeId 数组 JSON |
| snapshot | TEXT NOT NULL | 发布后各目标值快照 JSON |
| publishedBy / publishedAt | TEXT | 发布人/时刻 |
| status | TEXT NOT NULL | `ACTIVE` / `ROLLED_BACK` |
| rolledBackAt | TEXT | 回滚时刻 |

## 3 指标与告警口径

### 3.1 指标集合（固定 8 个）

| metricCode | 名称 | 口径 | 方向 | 阈值 |
|---|---|---|---|---|
| `SRC_COVERAGE` | 来源覆盖率 | 已发布知识对象中有 `sourceDocId` 且文档存在的比例 | GTE | 0.95 |
| `ORPHAN_RATE` | 孤儿知识率 | 已发布知识对象中 `sourceDocId` 为空或文档不存在的比例 | LTE | 0.02 |
| `DUP_PENDING` | 待处理重复数 | `QisRecord` 中 `issueType='重复'` 且 `handleStatus='待处理'` | LTE | 10 |
| `CONFLICT_PENDING` | 待处理冲突数 | `QisRecord` 中 `issueType='冲突'` 且 `handleStatus='待处理'` | LTE | 0 |
| `STALE_SOURCE` | 超期采集源数 | 启用源中 `lastRunAt` 为空或早于 `2 × minIntervalMinutes` | LTE | 0 |
| `CRAWL_FAIL_RATE` | 采集失败率 | 近 7 天 `CrsSource_CrawlRun` 失败数 / 总数 | LTE | 0.2 |
| `REVIEW_BACKLOG` | 待审核积压 | 五类知识表中 `status='待审核'` 合计 | LTE | 50 |
| `RULE_COVERAGE` | 规则覆盖率 | 已发布政策中至少存在一条 `confirmationStatus='CONFIRMED'` 规则的比例 | GTE | 0.5 |

- `status` 判定：`GTE` 时 `value < threshold → WARN`、`value < threshold * 0.8 → BREACH`；`LTE` 时 `value > threshold → WARN`、`value > threshold * 2 → BREACH`；阈值为 0 的 `LTE` 指标不做 `BREACH` 分级（任何非零值只判 `WARN`）。
- 值为比例类时保留 4 位小数；计数类为整数。

### 3.2 告警规则

| alertCode | 触发条件 | 级别 |
|---|---|---|
| `ALT-DUP` | `DUP_PENDING > 10` | P3 |
| `ALT-CONFLICT` | `CONFLICT_PENDING > 0` | P2 |
| `ALT-ORPHAN` | `ORPHAN_RATE > 0.02` | P2 |
| `ALT-SOURCE-STALE` | `STALE_SOURCE > 0` | P3 |
| `ALT-CRAWL-FAIL` | `CRAWL_FAIL_RATE > 0.2` | P2 |
| `ALT-REVIEW-BACKLOG` | `REVIEW_BACKLOG > 50` | P3 |
| `ALT-RULE-COVERAGE` | `RULE_COVERAGE < 0.5` | P3 |

生命周期：巡检发现触发 → 无 `OPEN` 记录则新建；指标回到阈值内 → 把该 `alertCode` 的 `OPEN/ACK` 记录置 `CLOSED` 并写 `closedAt`。人工 `ACK` 只改状态，不阻止后续关闭。

## 4 增量入库口径

`OpsService.ingest()` 返回三块：

1. **源健康**：每个 `CrsSource` 的 `lastRunAt`、`staleFlag`、近 7 天运行数/成功数/成功率、最近一次运行状态。
2. **待审核积压**：五类知识表按 `status='待审核'` 分组计数。
3. **变更流**：最近 20 条 `ChangeLog` + 最新游标（`updatedAt`），供增量消费方 `GET /api/kg/changes?since=` 接续。

失败重跑：`POST /api/ops/ingest/retry` 按 `crawlSourceId` 重跑；若「上次成功之后的连续失败次数」已达该源 `maxRetry`，返回 409 并提示人工处理，避免无限重试。

## 5 演进口径

### 5.1 状态机

```
DRAFT --submit--> PENDING_REVIEW --review(通过)--> APPROVED --publish--> PUBLISHED --rollback--> ROLLED_BACK
                                   --review(驳回)--> REJECTED --submit--> PENDING_REVIEW
```

- `publish` 前置：`status=APPROVED` 且 `reviewedBy` 非空且 `reviewedBy != submittedBy`。
- `rollback` 前置：`status=PUBLISHED`。
- 每次迁移写 `ChangeLog`：`OPS_EVOLUTION_*`。

### 5.2 三类目标的发布语义

| targetKind | 发布动作 | 回滚动作 | 影响面 |
|---|---|---|---|
| `SCOPE` | 新建走 `ScopeService.upsert_scope`；更新就地改 `members/normalizedKey/memberCount/isOpen/label`、`version+1` | 还原 `members/normalizedKey/memberCount/isOpen/label/version` | 命中该范围成员数 + 引用该 `scopeCode` 的规则数 |
| `RULE` | 更新 `CompiledRule` 的 `confirmationStatus`/`backTranslationMatch`/`testcaseIds`/`ruleExpression` | 还原上述字段 | 该规则所属政策的条款数与用例数 |
| `ONTOLOGY_META` | 更新 `OntologyObjectType` 的 `displayName`/`description`/`status` | 还原上述字段 | 该对象类型实例数 |

`OntologyService.sync()` 在重建注册表后回放所有 `status='PUBLISHED'` 的 `ONTOLOGY_META` 申请，避免被定义层覆盖。

### 5.3 冲突处理

- `SCOPE` 更新后 `normalizedKey` 与其它范围重复 → 409，要求先合并。
- `RULE` 目标不存在 → 404。
- `ONTOLOGY_META` 目标不存在 → 404。

## 6 API 契约（`/api/ops`）

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/ops.html` | 登录 | 运营工作台页面 |
| GET | `/api/ops/overview` | 登录 | 三块汇总：指标最新值、未闭环告警数、源健康、演进在途数 |
| GET | `/api/ops/ingest` | ROLE-02 | 增量入库看板 |
| POST | `/api/ops/ingest/retry` | ROLE-02 | 失败重跑，body `{crawlSourceId}` |
| POST | `/api/ops/inspect` | ROLE-01 | 立即巡检一次 |
| GET | `/api/ops/metrics` | 登录 | 指标快照，参数 `metricCode`、`limit` |
| GET | `/api/ops/alerts` | 登录 | 告警列表，参数 `status`、`limit` |
| POST | `/api/ops/alerts/{alertId}/ack` | ROLE-01 | 认领 |
| POST | `/api/ops/alerts/{alertId}/close` | ROLE-01 | 关闭 |
| GET | `/api/ops/evolution` | 登录 | 演进申请列表，参数 `status`、`targetKind` |
| POST | `/api/ops/evolution` | ROLE-01 | 建草稿 |
| POST | `/api/ops/evolution/{changeId}/submit` | ROLE-01 | 提交评审 |
| POST | `/api/ops/evolution/{changeId}/review` | ROLE-03 | 评审，body `{approved, remark}` |
| POST | `/api/ops/evolution/{changeId}/publish` | ROLE-01 | 发布 |
| POST | `/api/ops/evolution/{changeId}/rollback` | ROLE-01 | 回滚 |
| GET | `/api/ops/releases` | 登录 | 发布版本列表 |

统一错误：参数非法 422、状态不满足 409、目标不存在 404、越权 403。

## 7 前端 `web/ops.html`

四个分区（单页 tab，无框架，沿用既有内联样式风格）：

1. **总览**：指标卡片（8 个，含状态色）、未闭环告警数、源健康摘要、演进在途数。
2. **增量入库**：源健康表（含「重跑」按钮）、待审核积压、最近运行、变更流游标。
3. **质量监控**：指标趋势表（按 metricCode 分组的最近 N 条）、告警列表（认领/关闭）、「立即巡检」按钮。
4. **演进中心**：申请列表（状态徽章）、新建草稿表单、提交/评审/发布/回滚按钮、发布版本列表。

导航：`index.html` / `market.html` / `graph.html` 增加「运营工作台」入口；`ops.html` 反向互链。

## 8 定时巡检

- `OpsScheduler(ops, interval_minutes)`，线程名 `grain-oil-ops-scheduler`，启动时先跑一轮，其后按间隔轮询。
- 开关沿用 `KG_SCHEDULER_ENABLED`；间隔由 `KG_OPS_INSPECT_INTERVAL_MINUTES`（默认 30）控制。
- 单轮异常只记日志，不终止线程；`stop()` 与 `CrawlScheduler` 同样在 lifespan 收尾。

## 9 门禁 G5-1—G5-8

`OpsService.validate_stage5()` 聚合，`initialize()` 末尾调用；失败抛 `OpsError`，消息含编号与对象 ID。

| 门 | 校验 |
|---|---|
| G5-1 | 4 张运营表存在且审计列齐全 |
| G5-2 | `METRICS` 的 8 个指标编码与最近一轮快照一致（缺项即失败） |
| G5-3 | 同一 `alertCode` 最多一条 `OPEN` |
| G5-4 | 申请 `status` 在枚举内；`PUBLISHED` 必须有 `releaseId`、`reviewedBy`、`publishedAt` |
| G5-5 | `ROLLED_BACK` 申请所属版本的 `status` 必须为 `ROLLED_BACK` |
| G5-6 | 发布版本 `versionNo` 从 1 起连续、无重复 |
| G5-7 | 指标快照值非空，且 `status` 与阈值判定一致 |
| G5-8 | 每个 `PUBLISHED` 申请在 `ChangeLog` 中存在 `OPS_EVOLUTION_PUBLISH` 记录 |

## 10 与既有能力的边界

- 复用：`Crawler`、`CrawlScheduler`、`GovernanceService`、`ScopeService`、`OntologyService`、`ChangeLog`。
- 不复制：不重写爬取、不去重、不改治理算法；质量指标只做**聚合读取**。
- 无写冲突：演进发布不触碰五类知识对象的业务表。
