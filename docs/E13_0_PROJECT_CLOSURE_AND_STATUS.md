# E13.0：项目最终收口与状态冻结（Project Closure & Status）

- 复核时间：2026-08-08
- 目的：冻结最终事实；E12 报告如存在不一致仅以本报告勘误说明（不改历史）

## 1. 仓库与构建

| 项 | 值 |
|---|---|
| 根仓库 HEAD | `c7fc114`（clean）|
| llama.cpp HEAD | `f54930492`（clean）|
| E12 build commit | `f54930492`（build 15:17:39）|

## 2. E12.2 两次 hit-path 修复尝试及失败原因

| 尝试 | 失败原因 |
|---|---|
| skip-do_reset（restore 后保留 n_past=362，跳过 pos_min_thold do_reset）| **崩溃**：save 在生成后保存 → state 位置 364 > prefix 362 → KV 位置与调度 pos 冲突 |
| save 移到 prefill 完成点（post_decode）| **条件未触发**：server 的 prefill 完成检测时机与 post_decode 的 slot 状态不匹配；且 n_predict=0 的 token 序列差异（363 vs 362）|

## 3. 关键证据（E12）

- **4B restore 成功但全量 prefill**：restored checkpoint（362 tokens/64,659,376 bytes）→ `pos_min=364 ≥ pos_min_thold=362` → do_reset → prompt_n=384（全量）
- **save/restore 额外成本**：save（get_data 64.66MB）≈ 42ms、restore（set_data）≈ 8ms——on 路径 = save + restore + 全量 prefill（= off）→ **on 比 off 更慢（纯开销）**
- **checkpoint host memory**：64.66MB/checkpoint（4B P~362；recurrent state 大头；存 host、KV 池不变——不声称容量提升）

## 4. 明确表述（勘误性澄清）

1. **E12.4 的 p95 收益门槛未被命中路径实测**——不能写成通过（命中未发生，p95 无 hit 数据）
2. **E12.4 最终结论 = NO_OPTIMIZATION_GAIN**（restore 后 do_reset 全量，prefill 未减少）
3. **Qwen3.5-4B checkpoint 正确性来自安全 fallback**（全量 prefill 输出与 baseline 一致），**不是命中后的增量恢复**
4. **E10.4 API 探针仍只能称 API_LEVEL_PROTOTYPE**（llama API 层无损；非 server 集成路径；探针在 `llama.cpp/tmp/` 根忽略未入库）

## 5. q8_0 验证范围和限制

- 模型：Qwen3.5-4B-Q4_K_M.gguf（sha256 `de8e96cd0d0c3584...`）
- 验证：20×6 基础 workload + multi_turn/save_restore/long_prompt 补充（warmup 5 + formal 20 paired，temp=0/seed=42）
- **token-exact 仅限本验证矩阵**；decode 最大精确退化 **2.10%**；KV capacity **-47%**；GPU peak **-58MB**
- q4_0 **NOT_VALIDATED**（仅 1 场景对照）；**8192 context 未验证**；不得声称全局无损或普遍性能提升

## 6. 最终状态字段（E13 冻结）

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
