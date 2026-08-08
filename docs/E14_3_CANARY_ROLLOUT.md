# E14.3：灰度发布（Canary Rollout）

- 复核时间：2026-08-08
- 状态：**无真实生产环境 → 不虚构灰度结果；执行本机 dry-run**

## 1. 灰度阶段

| 阶段 | 状态 |
|---|---|
| 预发布环境（完整 E14.2）| **PASS**（E14.2 15 项）|
| 5% 流量（≥30min）| **未执行**（无真实生产流量）|
| 25% 流量（≥60min）| **未执行**|
| 100% 流量 | **未执行**|

## 2. 本机 dry-run 结果（`raw/e14_canary_rollout.json`）

| 指标 | 值 |
|---|---|
| 请求数 / 成功率 | 40 / **100%**（0 失败）|
| HTTP 4xx / 5xx | 0 / 0 |
| p50 / p95 / p99 | 631 / 685 / 686 ms |
| canary 泄漏 | **无** |
| server crash/restart | **无** |
| checkpoint 日志 | **0** |
| KV occupancy | used 866 / capacity 4096 cells |
| tool JSON schema 失败 | 0（E14.2 chat 接口验证）|

## 3. 灰度监控项（本机 dry-run 采集口径，真实灰度沿用）

请求数/成功率、4xx/5xx、rejected/OOM、p50/p95/p99、tps、GPU used/peak、KV occupancy、队列长度、server restart/crash、schema 失败、canary 异常、与旧 profile 资源差异（E14.2/E14.1 已覆盖 per-cell/GPU）。

## 4. 回滚条件（预设，8 条）

任意 crash/hang/OOM；输出污染或 canary 泄漏；tool JSON schema 失败率高于基线；5xx 增 >1pp；p95 退化 >10%；GPU peak 超设备安全边界；q8_0 参数未生效；checkpoint experimental 意外启用——任一触发立即回滚（E14.4 runbook）。

## 5. 状态

```text
RELEASE_STATUS: READY_FOR_DEPLOYMENT
PRODUCTION_ROLLOUT_STATUS: NOT_EXECUTED（无真实生产环境/部署窗口；本机 dry-run 完成）
```
