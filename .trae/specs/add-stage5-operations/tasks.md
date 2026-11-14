# 阶段 5 任务清单

> 依据 `spec.md` 与 `ontology_ly/packs/ops/stage5_运营口径清单.md`（冻结稿）。

- [ ] 1. 映射登记 4 张运营表

  验收：`OpsQualityMetric`、`OpsAlert`、`OpsEvolutionRequest`、`OpsEvolutionRelease` 建表且审计列齐全（`M-MAP-OPS-017`—`020`）。

- [ ] 2. `app/ops.py` 指标与巡检

  验收：`METRICS` 8 项、`ALERT_RULES` 7 条；`inspect()` 连续两轮指标行数线性增长、告警不重复开单。

- [ ] 3. `app/ops.py` 演进状态机

  验收：`DRAFT→PENDING_REVIEW→APPROVED→PUBLISHED→ROLLED_BACK` 全链路可跑；非法迁移 409；提交人=评审人时发布被拒。

- [ ] 4. `OntologyService` 回放 `ONTOLOGY_META`

  验收：发布元数据后 `POST /api/ontology/refresh` 仍保留修改。

- [ ] 5. `OpsScheduler` 与配置

  验收：`ops_inspect_interval_minutes` 可配置；`scheduler_enabled=True` 启动后出现首轮快照；异常不终止线程。

- [ ] 6. 路由接线

  验收：15 条 `/api/ops` 路由 + `GET /ops.html`；角色权限符合冻结稿 §6。

- [ ] 7. `web/ops.html`

  验收：四分区可切换，指标卡、告警、演进操作、版本列表均可用。

- [ ] 8. 导航互链

  验收：`index.html`/`market.html`/`graph.html`/`ops.html` 四页互跳。

- [ ] 9. 测试与收口

  验收：`python -m pytest -q` 全绿；`python ontology_ly/tools/validate_stage0.py` 输出 `score=100 error=0 warn=0`。
