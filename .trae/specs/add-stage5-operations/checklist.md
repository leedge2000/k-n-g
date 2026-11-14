# 阶段 5 验收清单

## 数据层

- [ ] `seed/mapping.json` 存在 `M-MAP-OPS-017`—`020` 四条映射，`tableRole` 均为 `MASTER`
- [ ] 4 张运营表初始化后存在，`createdBy/createdAt/updatedBy/updatedAt` 齐全
- [ ] `ops_inspect_interval_minutes` 可由 `KG_OPS_INSPECT_INTERVAL_MINUTES` 覆盖，默认 30

## 质量监控

- [ ] 8 个 `metricCode` 全部有快照，值域与方向一致
- [ ] `LTE`/`GTE` 阈值判定正确，`WARN`/`BREACH` 分级符合冻结稿 §3.1
- [ ] 同一 `alertCode` 同时最多一条 `OPEN`
- [ ] 指标回落后既有 `OPEN/ACK` 告警自动 `CLOSED`
- [ ] `ack` 幂等：重复认领返回 409

## 增量入库

- [ ] 源健康表含 `staleFlag`，阈值 `2 × minIntervalMinutes`
- [ ] 近 7 天成功率与失败率口径一致
- [ ] 待审核积压覆盖五类知识表
- [ ] 变更流返回最近 20 条与最新游标
- [ ] 连续失败达 `maxRetry` 时重跑返回 409

## 演进中心

- [ ] 状态机六个状态齐全，非法迁移 409
- [ ] `publish` 要求 `APPROVED` + 评审人非空 + 评审人≠提交人
- [ ] `SCOPE` 发布就地更新且 `version+1`
- [ ] `RULE` 发布只改白名单字段
- [ ] `ONTOLOGY_META` 发布后 `ontology/refresh` 不覆盖
- [ ] 回滚还原目标数据，申请与版本同为 `ROLLED_BACK`
- [ ] `versionNo` 从 1 连续递增

## 门禁

- [ ] G5-1—G5-8 全部实现，失败消息含编号与对象 ID
- [ ] `initialize()` 末尾调用 `validate_stage5()`
- [ ] 人为制造重复 `OPEN` 告警时启动失败

## 前端与路由

- [ ] `GET /ops.html` 返回 200
- [ ] 15 条 `/api/ops` 路由的权限与冻结稿 §6 一致
- [ ] 四分区均可加载数据，无控制台报错
- [ ] 四页导航互链可用

## 收口

- [ ] `python -m pytest -q` 全绿
- [ ] `python ontology_ly/tools/validate_stage0.py` 输出 `score=100 error=0 warn=0`
- [ ] 无临时脚本、调试输出残留
