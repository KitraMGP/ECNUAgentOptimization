# E10：最终决策报告（Final Decision）

- 复核完成时间：2026-08-08
- 范围：E10.0-E10.4（Qwen3.5-4B 主模型优化定案与生产化验收）

## 最终状态

```text
PROJECT_STATUS: PASS_KV_CACHE_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: PROTOTYPE_PASS
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION（C1 语义保持）+ QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS（新路径）
QWEN35_Q8_KV_STATUS: PASS_DEPLOYABLE
QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS
PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE / RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 1. E10.0 勘误关闭

- E9.4 "KV type 不可用" 撤销（参数名笔误）；4B strict C1 off/on paired raw 补测完成（hash 全一致、capability rejected、shared=0）
- cleanup ≠ cancel（本 build 无 /cancel API → NOT_SUPPORTED）；LoRA skipped ≠ runtime 通过（NOT_VERIFIED 保持）
- H4 NO_GO 限定 end-position clone；C1 PASS 先 provisional 后确认

## 2. C1 验收关闭（E10.1）

- source-scan 直接测量（N=64 ≤130 us、token comparisons 线性）——非 e2e 推断
- N×match 矩阵全 200；save/restore 后 hash 与 baseline 一致；30-cycle endurance PASS（drift=0）
- prompt cache identity：代码级审计 + 空-空回归；不同 adapter/scale 构造 NOT_TESTED（资产缺失）
- **C1_ATTENTION_ONLY_PASS 确认**（E10.1 可执行项全完成）

## 3. clone API 决策（E10.2）

**INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE**：移除公共 `/slots/{id}?action=clone_from`（3 文件 0 残留）；保留底层 `llama_state_seq_get/set_data` 供 checkpoint 原型。

## 4. q8_0 生产化验收（E10.3）

- strict paired（warmup 5 + formal 20 × 6 workload）：**输出 hash 与 F16 完全一致**（含 tool_json/canary）
- KV capacity -47%（67→34MB）、GPU 总显存 2966→2936MB（-30MB 实际）、时延 ±3%、0 失败
- **QWEN35_Q8_KV_STATUS: PASS_DEPLOYABLE**（validated deployment profile：`--cache-type-k/v q8_0`，可复现脚本 e10_3_q8_validation.py）

## 5. prefix-aligned checkpoint 原型（E10.4）——主模型新优化

- llama API 探针（真实 4B hybrid 路径）：P 末尾完整 attn+recr state 保存 → 恢复到 target → P+X/P+Y
- **无损**：同一 checkpoint 分叉 2 target，token ID 与 baseline 完全一致（P+X、P+Y）
- **显著收益**：restore 7.86ms vs 重复 prefill 29.9ms（26%；长 P 场景 5-10× 潜力）
- 停止条件全未触发 → **QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS**
- 与 E9.6 end-position clone 的 NO_GO 对比：**prefix-aligned 是关键差异**（已验证）

## 6. 项目级判定（指令六规则）

| 规则 | 判定 |
|---|---|
| q8_0 过验收但无新算法 → PASS_OPERATIONAL + NOT_ACHIEVED | 不适用（有新算法实现）|
| **checkpoint-aligned reuse 无损+显著收益 → PASS_KV_CACHE_OPTIMIZATION + PROTOTYPE_PASS** | **适用（PROJECT_STATUS: PASS_KV_CACHE_OPTIMIZATION）** |
| q8_0 未过 + checkpoint NO_GO → PARTIAL | 不适用 |
| 错误 restore/污染/泄漏 → REJECT | 不适用（无损 + roundtrip 精确 + 无污染）|

## 7. 工程决策（保留/生产化/停止）

1. **保留**：C1（attention-only，完整验收）+ q8_0 profile（生产化部署配置，PASS_DEPLOYABLE）
2. **生产化候选**：prefix-aligned checkpoint reuse（prototype 通过）——下一工程 = server 集成（slot checkpoint 所有权、存储管理、多 target 分叉调度），**不作为本阶段承诺**
3. **停止**：end-position clone（已移除）、C2 容量估算（保持 estimator）、TinyLlama 收益不再作为主模型证据

## 8. 测试、commit、产物、工作树

- 测试：llama.cpp 50 passed + 3 skipped + E1 5/5 + 根 177；探针 4B 无损验证
- commit：llama.cpp `3a19afaa`（scan 计数）、`cb39a812`（clone 移除）；根 `7389e40`(E10.0)、`1a82b73`(E10.1)、`84a191f`(E10.2)、`a614fec`(E10.3)、`9a7f632`(E10.4)
- 产物：`docs/E10_0..E10_4 + E10_FINAL`、`raw/e10_c1_paired_4b.json`、`raw/e10_churn30.json`、`raw/e10_q8_validation.json`、`llama.cpp/tmp/e10_checkpoint_probe.cpp`（探针）
- 工作树：两仓库 clean（未跟踪 `docs/20260808_E10_INSTRUCTION.md`）

## 9. 唯一最终结论

**PASS_KV_CACHE_OPTIMIZATION**：主模型 Qwen3.5-4B 上获得**真实新优化**——prefix-aligned hybrid checkpoint reuse（无损 prototype 验证：同一 checkpoint 分叉多 target、restore 26% of prefill 成本）+ q8_0 KV 生产化 profile（PASS_DEPLOYABLE）。C1（attention-only）完整验收关闭。诚实标注：checkpoint 为 llama API 层原型（server 集成为后续工程）；LoRA/SWA/recurrent 运行时仍 NOT_VERIFIED（资产不可得，不虚标）。
