# 阶段 5 任务分解

> 依据：`ontology_ly/packs/ops/stage5_运营口径清单.md`（冻结稿）。
> 执行顺序即下表顺序；每项完成需通过对应验证。

## T1 映射登记：4 张运营表

- `seed/mapping.json` 追加 `OpsQualityMetric`、`OpsAlert`、`OpsEvolutionRequest`、`OpsEvolutionRelease`（`M-MAP-OPS-017`—`020`）。
- 全部 `tableRole=MASTER`，主键分别为 `metricId`/`alertId`/`changeId`/`releaseId`。
- `Database.initialize()` 自然建表，无需改 `db.py`。

验证：初始化后 4 张表存在，审计列齐全。

## T2 `app/ops.py`：指标、告警、巡检

- `OpsError`、`METRICS`（8 项常量）、`ALERT_RULES`。
- `OpsService.initialize()` 建索引并调用 `validate_stage5()`。
- `compute_metrics()` 只读聚合，返回 `{code: {value, status, detail}}`。
- `inspect(actor)` 写快照 + 触发/关闭告警，返回本轮摘要。
- `overview()`、`ingest()`、`retry_failed()`、`metrics()`、`alerts()`、`ack()/close()`。

验证：`inspect()` 连跑两次，指标行数线性增长、告警不重复开单。

## T3 `app/ops.py`：演进状态机

- `create_draft()/submit()/review()/publish()/rollback()`、`list_changes()/list_releases()`。
- `publish()` 分派三类目标：`SCOPE`（`ScopeService`）、`RULE`（`CompiledRule`）、`ONTOLOGY_META`（`OntologyObjectType`）。
- 发布写 `previousPayload`、`releaseId`，并落 `OpsEvolutionRelease` 与 `ChangeLog`。
- 回滚按 `previousPayload` 反向还原。

验证：状态机非法迁移一律 409；提交人=评审人时发布被拒。

## T4 `OntologyService` 回放

- `sync()` 末尾回放所有 `PUBLISHED` 的 `ONTOLOGY_META` 申请，覆盖 `displayName/description/status`。
- 回放逻辑容错：目标不存在时跳过，不中断同步。

验证：发布元数据变更后手动 `POST /api/ontology/refresh`，值不丢。

## T5 `OpsScheduler` 与配置

- `app/config.py` 新增 `ops_inspect_interval_minutes`（`KG_OPS_INSPECT_INTERVAL_MINUTES`，默认 30）。
- `OpsScheduler` 线程：启动先跑一轮，之后按间隔巡检；异常只记日志。
- `main.py` lifespan 中随 `scheduler_enabled` 启动/停止，异常回退不影响主服务。

验证：`scheduler_enabled=True` 启动后 `OpsQualityMetric` 有数据。

## T6 路由接线

- `GET /ops.html` 返回静态页。
- 新增 15 条 `/api/ops` 路由（含 `retry`、`ack`、`close`、`inspect`），按冻结稿 §6 分配 `require_roles`。

验证：`/openapi.json` 中 `/api/ops` 路径数量与冻结稿一致。

## T7 `web/ops.html`

- 四分区 tab：总览 / 增量入库 / 质量监控 / 演进中心。
- 指标卡片色阶、告警认领/关闭、草稿新建与状态流转按钮、版本列表。
- 采用 `localStorage` 存 token，沿用既有页面交互习惯；不引第三方库。

验证：静态页 200，四分区可切换，各分区请求均返回 200。

## T8 导航互链

- `index.html`、`market.html`、`graph.html` 顶部导航加「运营工作台」链接。
- `ops.html` 回链三页。

验证：四页互相可跳转。

## T9 测试与收口

- 新增 `tests/test_stage5.py`：指标计算、告警生命周期、演进状态机、发布/回滚、G5 门禁、路由权限。
- 运行 `python -m pytest -q` 与 `python ontology_ly/tools/validate_stage0.py`。
- 清理临时脚本与调试输出。

验收：全量测试通过，定义层 `score=100 error=0 warn=0`。
