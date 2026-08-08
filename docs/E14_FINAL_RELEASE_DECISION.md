# E14：最终发布决策（Final Release Decision）

- 复核完成时间：2026-08-08
- 范围：E14.0-E14.5（q8_0 operational profile 发布、灰度和运维交接）

## 发布状态

```text
RELEASE_STATUS: READY_FOR_DEPLOYMENT
PRODUCTION_ROLLOUT_STATUS: NOT_EXECUTED（无真实生产环境/部署窗口；本机 dry-run 完成）
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
```

## 冻结项目状态（不变）

```text
PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_CHECKPOINT_REUSE_STATUS: NO_GO_WITH_EVIDENCE
CHECKPOINT_PROTOTYPE_STATUS: RETAINED_EXPERIMENTAL_ATTENTION_ONLY
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 发布决策依据

| 项 | 结果 |
|---|---|
| 最终 build（afbf375c，SHA256 稳定）| PASS |
| E13.1 轻量回归重跑 | PASS（per-cell 17408、canary）|
| 生产配置断言 | PASS（E14.1：per-cell 17408、checkpoint 0）|
| 发布前验收（15 项）| **PASS**（E14.2：健康/tool JSON/canary/并发/shutdown/clone_from 501/重启恢复）|
| 灰度 | **NOT_EXECUTED**（无真实生产环境；本机 dry-run 通过：40 请求 100%、p95 685ms、无泄漏）|
| 回滚演练 | **PASS**（E14.4：q8_0→F16 配置切换，exit 0）|
| 测试退出码 | 全 0（llama 50+3、E1 5/5、根 177、C++ 18）|

## 工程决策

- **发布就绪**：q8_0 Qwen3.5-4B validated deployment profile（`--cache-type-k/v q8_0`；生产配置 `qwen35_4b_q8_production.yaml`；运维交接 `E14_5`）
- **未执行真实灰度**（无生产环境/窗口）——不虚构；获得部署窗口后按 E14.3 灰度协议执行
- **不改变**：NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS（NOT_ACHIEVED）、QWEN35_CHECKPOINT_REUSE_STATUS（NO_GO_WITH_EVIDENCE）、CHECKPOINT_PROTOTYPE_STATUS（RETAINED_EXPERIMENTAL_ATTENTION_ONLY）

## commit 与工作树

- 根仓库：`d81743a`(E14.0)、`653b1d2`(E14.1)、`5208258`(E14.2)、`fa4c97e`(E14.3)、`eca8722`(E14.4)、`4de52a7`(E14.5) + 本提交
- llama.cpp：`afbf375c6`（无 E14 修改）
- 工作树：两仓库 clean

## 唯一最终结论

**RELEASE_STATUS: READY_FOR_DEPLOYMENT**：Qwen3.5-4B q8_0 operational profile 发布候选通过全部验收（构建/配置/15 项/回滚演练），**生产配置固化**（`qwen35_4b_q8_production.yaml`）、**运维交接完成**（E14.5）；真实灰度未执行（无生产环境，不虚构），部署窗口获得后按 E14.3 协议执行。E14 后不再进入优化复核阶段——后续仅允许正常运维、缺陷修复，或在新模型/新上游机制/真实 LoRA 资产下建立全新独立项目。
