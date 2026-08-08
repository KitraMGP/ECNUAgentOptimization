# E11：最终决策报告（Final Decision）

- 复核完成时间：2026-08-08
- 范围：E11.0-E11.6（Qwen3.5-4B prefix-aligned checkpoint reuse server 集成与最终生产化决策）

## 最终状态

```text
PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION（C1 语义）+ checkpoint 主模型未命中
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS（API 无损 + server 集成 PARTIAL：TinyLlama 生效、4B 未命中）
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 1. 工程决策（集成 / 保留 prototype / 停止）

**决策：保留 checkpoint 为 PROTOTYPE_PASS（主模型集成未达成收益），q8_0 生产化部署 profile 验收通过。**

| 决策项 | 结论 |
|---|---|
| 集成 | **已实现**（`--checkpoint-reuse` + checkpoint_save/restore，默认 off）：TinyLlama 完整生效（restore 命中、无损、生命周期过、e2e -46~-84%）；**4B hybrid 未命中 cache**（recurrent 位置语义）→ 主模型收益未达成 |
| 保留 | checkpoint 保留为 prototype（API 层无损证据 + server 集成 TinyLlama 证据）——**生产化候选**（需 4B cache 命中适配）|
| 生产化 | **q8_0 profile**（PASS_VALIDATED_DEPLOYMENT_PROFILE）：本矩阵内 token-exact、decode ≤2.10%、KV -47%、GPU -58MB——**可部署**（上游既有开关 + 验收数据，非新算法）|
| 停止 | end-position clone（已移除）、C2 容量估算（保持 estimator）、TinyLlama 收益不作为主模型证据 |

## 2. 各阶段结论

- **E11.0**：E10 勘误（E10.4=API_LEVEL_PROTOTYPE、E10.3=VALIDATED_DEPLOYMENT_PROFILE；decode 精确退化 max 2.10%；save 成本未计 → E11.5 纳入）
- **E11.1**：q8_0 边界确认（9 类 workload 矩阵 token-exact、GPU 多次 peak -58MB、q4_0 NOT_VALIDATED）
- **E11.2**：checkpoint 结构 + 所有权 + identity（C++ 18 断言）
- **E11.3**：server 集成（TinyLlama 生效；**BPE 前缀边界发现**——P+X 整体 tokenize 时 P 末尾空格合并 → 前缀不一致 → 安全拒绝）
- **E11.4**：生命周期（P×target 矩阵 match=True、source 删除后 restore、prefix mismatch 拒绝、restart 池清空；CANCEL NOT_SUPPORTED）
- **E11.5**：e2e（TinyLlama 多 target -46~-84%、长 P -81%；4B 无收益）
- **E11.6**：identity C++ 结构化验证 + 开关组合（8 组合全过）

## 3. 关键诚实声明

1. **主模型 Qwen3.5-4B 无新算法收益**：checkpoint server 集成在 hybrid 上 restore 执行但 cache_prompt 未命中（recurrent 位置语义）→ 输出无损但无性能收益 → `NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED`
2. **q8_0 是上游既有开关**（validated deployment profile，非新实现）——不冒充新算法
3. **BPE tokenizer 前缀边界**：checkpoint 前缀与请求前缀的 token 序列可能不一致（P 末尾空格合并）→ 安全拒绝（降级全量）——真实限制，E11.3 记录
4. **LoRA/identity runtime 未验证**（资产不可得）——不隐藏
5. **API prototype 通过 ≠ 生产就绪**（E11.0 明确分层）

## 4. 测试、commit、产物、工作树

- 测试：llama.cpp 50 passed + 3 skipped、E1 5/5、根 177、C++ identity 18 断言、开关组合 8 组合
- commit：llama.cpp `f60b4710`（checkpoint 池）、`8d1b2b1e`（server 集成）；根 `ad70305`(E11.0)、`8ab6dac`(E11.1)、`bb17318`(E11.2)、`9bbbb6c`(E11.3)、`467e11b`(E11.4)、`2cecd87`(E11.5)、`a7d9310`(E11.6)
- 产物：`docs/E11_0..E11_6 + E11_FINAL`、`raw/e11_*.json`、`benchmark/scripts/e11_{1,4,5}_*.py`、`llama.cpp/tools/server/server-checkpoint.h` + `test_checkpoint_identity.cpp`
- 工作树：两仓库 clean（未跟踪 `docs/20260808_E11_INSTRUCTION.md`）

## 5. 唯一最终结论

**PASS_OPERATIONAL_KV_OPTIMIZATION**：Qwen3.5-4B 上 q8_0 生产化部署 profile 验收完成（PASS_VALIDATED_DEPLOYMENT_PROFILE——本矩阵内 token-exact、-47% KV、-58MB 实际显存），可部署；**主模型新算法优化 NOT_ACHIEVED**（checkpoint server 集成 TinyLlama 完整生效但 4B hybrid 未命中 cache——需 recurrent 位置语义适配，保留为生产化候选 prototype）。C1（attention-only）完整验收；LoRA runtime 与 checkpoint 主模型收益如实标注未达成。不虚报生产级 PASS。
