# E12：最终决策报告（Final Decision）

- 复核完成时间：2026-08-08
- 范围：E12.0-E12.4（Qwen3.5-4B hybrid checkpoint 命中路径最终可行性验证与工程收口）

## 最终状态

```text
PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
QWEN35_CHECKPOINT_REUSE_STATUS: NO_GO_WITH_EVIDENCE（server 集成路径）
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 1. hybrid cache miss 根因（E12.1，诊断证据）

**根因链**：restore 成功（set_data 64.66MB）→ `llama_memory_seq_pos_min` = **364**（hybrid = max(attn, recr)，recurrent 位置语义）≥ `pos_min_thold` = 362 → cache_prompt 的 context checkpoint 搜索（restore 未创建）→ **do_reset → n_past=0 → 全量 prefill**（prompt_n=384）。
- TinyLlama 对照：pos_min=0 < 362 → 不进入 do_reset → 命中（prompt_n 127→17）
- 辅助因素：BPE tokenizer 前缀边界（P+X 整体 tokenize 时 P 末尾空格合并 → find_prefix 拒绝，安全 fallback）

## 2. 是否修改 hit-path（E12.2，NO_GO_WITH_EVIDENCE）

**尝试了两处最小修复，均未能达成可靠 hit，停止代码修改**：
1. **skip do_reset**（restore 后保留 n_past=362）→ **崩溃**（save 边界错位：生成后保存的 state 位置 364 > prefix 362 → KV 位置与调度 pos 冲突）
2. **save 移到 prefill 完成点**（post_decode）→ **条件未触发**（server 的 prefill 完成检测时机与 post_decode 的 slot 状态不匹配）；且 n_predict=0 的 token 序列有差异（363 vs 362）

**证据结论**：**server 层无法在精确 P 边界可靠获得 hybrid state**（save 时机 + recurrent pos_min 双重障碍）→ **无法证明 server 层 restore 后 recurrent position 正确** → 按 E12.2 判定：**NO_GO_WITH_EVIDENCE**。E10.4 llama API 探针（绕过 server cache_prompt）无损成立——但非 server 集成路径。

## 3. Qwen3.5-4B token-exact 结果（E12.3，fallback 正确性）

- 所有场景（P 116/362/440 × target 1/2/4 × P 末尾 4 类 × on/off × C1 拒绝）：**输出与独立 baseline 完全一致**（安全 fallback 全量 prefill）
- prefix mismatch 安全拒绝；restore 失败回滚；refcount=0；无 crash/hang/OOM/污染
- **正确性 TRUE（fallback）**；**命中 FALSE；优化 NOT_ACHIEVED**

## 4. 收益（E12.4，NO_OPTIMIZATION_GAIN）

- restore 后 do_reset 全量 prefill（prompt_n=384=全量）→ **prefill 未减少** → 无优化收益
- on 路径 = save（~42ms/64.66MB）+ restore（~8ms）+ 全量 = **比 off 更慢**（纯开销）
- checkpoint host memory：**64.66MB/checkpoint**（明确报告；不声称容量提升——state 存 host、KV 池不变）

## 5. 保留与停止（E12.5 情况 B）

**保留**：
- C1（attention-only，完整验收）
- q8_0 validated deployment profile（本矩阵内 token-exact、KV -47%、GPU -58MB）
- checkpoint 实验代码（默认关闭；API 探针 + server 原型）——E10.4 llama API 层无损证据保留

**停止**：
- Qwen3.5-4B checkpoint 索引/LRU/分布式/生产调度（不再扩展）
- TinyLlama checkpoint 收益作为主模型证据
- q8_0 作为新算法

## 6. commit、产物、工作树

- commit：llama.cpp `f5493049`（E12.2 尝试）；根 `93783a0`(E12.0)、`40ee318`(E12.1)、`ac8db34`(E12.3/12.4)
- 产物：`docs/E12_0..E12_4 + E12_FINAL`、`llama.cpp/tmp/e12_diag.log`（诊断 raw）、`e12_hit*.log`
- 工作树：根 clean；llama.cpp clean（`f5493049` 后）

## 7. 唯一最终结论

**PASS_OPERATIONAL_KV_OPTIMIZATION + NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED**：Qwen3.5-4B 上 **q8_0 生产化部署 profile 验收完成**（可部署）；**novel hybrid checkpoint server 集成路径 NO_GO_WITH_EVIDENCE**（根因：recurrent pos_min 触发 do_reset + save 边界无法在 P 边界可靠获得——E12.1 诊断 + E12.2 两处修复尝试证据）。项目接受"q8_0 operational profile + novel checkpoint path NO_GO_WITH_EVIDENCE"最终结论。E12 后不再启动新的纯复核阶段。
