# E13：最终决策报告（Final Decision）

- 复核完成时间：2026-08-08
- 范围：E13.0-E13.3（项目最终收口、q8_0 部署固化与 checkpoint 原型隔离）

## 最终状态

```text
PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
QWEN35_CHECKPOINT_REUSE_STATUS: NO_GO_WITH_EVIDENCE
CHECKPOINT_PROTOTYPE_STATUS: RETAINED_EXPERIMENTAL_ATTENTION_ONLY
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 1. 最终工程决策

| 决策 | 内容 |
|---|---|
| **生产化** | q8_0 Qwen3.5-4B validated deployment profile（`--cache-type-k/v q8_0`；E13.1 固化配置 + 轻量回归）|
| **保留** | C1（attention-only 完整验收）；checkpoint attention-only 实验原型（默认关、experimental、hybrid 已防护）；identity C++ 单元测试 |
| **停止** | Qwen3.5-4B hybrid checkpoint hit-path（E12 NO_GO_WITH_EVIDENCE + E13.2 hybrid 保护）；end-position clone（已移除）；C2 扩展；TinyLlama 主模型收益外推 |
| **不宣称** | 新的主模型算法优化已实现（q8_0 为上游既有参数组合，非新算法；checkpoint 主模型路径 NO_GO）|

## 2. 阶段收口

- **E13.0**：事实冻结（E12.2 两次修复失败、4B restore 成功但全量、save 42ms+restore 8ms 开销、checkpoint 64.66MB、E12.4 p95 未实测不写成通过、NO_OPTIMIZATION_GAIN）
- **E13.1**：q8_0 profile 固化（唯一配置 + configs/qwen35_4b_q8_validated.yaml + 轻量回归：server/per-cell 17408/canary 全 OK）
- **E13.2**：checkpoint 隔离（hybrid 启动降级 + `checkpoint_reuse_unsupported_hybrid` 日志；TinyLlama 保留 experimental）
- **E13.3**：默认路径回归（4B 输出与 E12 baseline 一致、checkpoint off 无 save/restore、clone_from 0 残留、测试退出码全 0）

## 3. commit 与工作树

- llama.cpp：`afbf375c`（E13.2 hybrid 保护）
- 根：`2f04a90`(E13.0)、`018365c`(E13.1)、`3e41b17`(E13.2)、`8b596cb`(E13.3)
- 工作树：两仓库 clean

## 4. 唯一最终结论

**PASS_OPERATIONAL_KV_OPTIMIZATION**：Qwen3.5-4B 上 **q8_0 生产化部署 profile 固化完成**（validated deployment profile，可部署、可复现、有回归）；**主模型新算法优化 NOT_ACHIEVED**（novel hybrid checkpoint server 路径 NO_GO_WITH_EVIDENCE，已加 hybrid 防护隔离）。C1（attention-only）完整验收；checkpoint 原型保留为 experimental（attention-only）；LoRA runtime 未验证（资产不可得）。**项目正式收口**——E13 后不再启动新的 Qwen3.5-4B checkpoint 纯复核阶段。
