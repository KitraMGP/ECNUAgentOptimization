# E8：最终复核报告（Final Review Report）

- 复核完成时间：2026-08-08
- 范围：E8.0-E8.7（证据收敛、边界加固、性能验收、主模型路线决策）

## 最终状态

```text
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
C1_ATTENTION_ONLY_STATUS: C1_CONDITIONAL_PASS_RECOMPUTE_ONLY
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR
UPSTREAM_COMPARISON_STATUS: UPSTREAM_HISTORY_AND_ISSUE_REVIEW_COMPLETE
LITERATURE_METADATA_STATUS: CORRECTED
```

## 1. E7 结论复核（E8.0 证据完整性）

- **VERIFIED**：E7 矩阵 8 场景 median/delta/reduction 与 raw 完全一致；E6.4 recompute 229→24（-89.52%）；E6.5 endurance 12 周期；测试数量（47+5+177）与退出码
- **PARTIALLY_VERIFIED**：4B probe off/on 非同一 commit（E8.4 已在同一 build 下重验）；raw 缺 seed/temp/时间戳显式字段（E8.5 已补齐）
- **CONTRADICTED（E7 内已修）**：C1 名称（RADIX→metadata sharing）、C2 命名（SnapKV→estimator）
- E7 的 E6-PASS 降级结论：**保留**（主模型无收益仍成立）

## 2. C1 实际支持范围（E8.2/E8.3）

- **正向能力声明**（非负向黑名单）：`llama_memory_supports_cross_slot_prefix_sharing()` 默认 false，仅标准 unified attention KV（llama_kv_cache）返回 true；msa/dsa/iswa/dsv4/hybrid/recurrent 全部默认拒绝
- 支持范围：**TRUSTED_SINGLE_TENANT_DEPLOYMENT**（server 无 per-request tenant auth → 无可信 tenant identity；multi-tenant NOT_VERIFIED，参数帮助+日志+文档三处声明）
- min-lcp 负值启动明确失败；n_past==n_tokens 回退审计（≤1 token，server 既有行为）
- 新测试：capability 6 例 + LoRA 3 例（skipif）+ E7.1 审计 10 例

## 3. tenant 与 cache identity 策略（E8.3）

- 无 tenant identity（仅全局 --api-key）→ trusted_single_tenant_deployment；不从 prompt/header 猜 tenant
- identity = LCP ≥ min-lcp + adapter(ptr+scale) + 架构能力；canary 内容隔离已证 ≠ tenant 安全边界

## 4. LoRA/SWA/recurrent/hybrid 实测（E8.4）

- **VERIFIED**：hybrid（4B，sha256 de8e96cd...）capability rejected + 共享 0 + 独立运行；attention-only（含 MOE）capability accepted + 共享
- **NOT_VERIFIED**：LoRA（moe_shakespeare15M.gguf 网络不可达，测试就绪 skipif）、SWA（无模型）、recurrent（无模型）——**不因代码静态判断升级状态**

## 5. 性能、p95、容量、recompute（E8.5，30 reps + 5 warmup）

| 场景 | recompute | prefill med | decode med | e2e med | 输出一致 | 失败 |
|---|---|---|---|---|---|---|
| shared | 1560→14（-99.1%）| 304.5→5.45ms | 6.83→4.60 | 311.6→10.1ms | ✓ | 0/30 |
| partial | 814→41（-94.96%）| 88.2→8.06ms | 5.07→3.83 | 93.5→11.9ms | ✓ | 0/30 |
| noshare | 376→376（0%）| 22.66→21.77ms（-3.92%）| 3.64→3.58（-1.70%）| 26.3→25.5（p95 +0.81%）| ✓ | 0/30 |
| capacity | 1560→14（-99.1%）| 312.2→5.48ms | 6.68→4.59 | 319.2→10.1ms | ✓ | 0/30 |

- 门禁数值全达标（recompute≥25%、noshare prefill/decode ≤5%、p95 ≤10%）
- **CV 检查**：noshare 6.2/6.9%、on 模式 5-14%（CPU ms 噪声）→ NOISY 标注 → 按指令保守保持 CONDITIONAL
- 容量：used_cells 3137→1591（-49.3% 逻辑 headroom）；**capacity_bytes 预分配不变（物理分配未降低，不虚报）**

## 6. 上游历史和 issue/PR 对比（E8.6）

- 8584 commits + GitHub API：**无 merged 跨 slot 前缀共享**；最近似 = PR #26204 clone_to（KV 数据克隆，open 未合并，机制不同）→ C1 = **independent feature**
- issue #26207：上游承认 lora-cache-identity 缺陷（per-request lora 复用 prompt cache）→ 印证本地 C1 identity 修复；本地继承的 server prompt cache 路径仍带该上游缺陷（记录）
- issue #25913：hybrid/recurrent 上 prompt reuse 丢失 → 与本地 hybrid 禁用同向
- 无可直接移植修复

## 7. 文献 metadata 修正（E8.1）

- **SGLang/RadixAttention：SOSP 2024 → NeurIPS 2024**（ACM DL + stanford theory + NeurIPS poster 94872）
- ChunkAttention：UNVERIFIED → **ACL 2024 Long Papers**（ACL Anthology 2024.acl-long.623）
- InfiniGen：UNVERIFIED → **OSDI 2024**（USENIX osdi24-lee.pdf）
- LM-Infinite：保持 UNVERIFIED（OpenReview decision 不可读）；其余 8 篇确认
- manifest + references.bib 已修正；E7.2 历史保留

## 8. Qwen3.5-4B 路线决策（E8.7）

- hybrid seq_cp（recurrent 侧）只共享 tail 位置（顺序状态不可回退）→ C1 元数据共享路径**机制性不可行**
- 唯一理论路径 = checkpoint 恢复（state_write/read 覆盖 attn+recr），但 4 个必要条件缺失 → **路线 B NO_GO（当前实现）**
- 路线 A（attention-only 目标）务实但**需用户确认范围变化**
- **不声称优化了 Qwen3.5-4B**

## 9. C2 状态

`KV_BUDGET_FEASIBILITY_ESTIMATOR`（容量估算；无 score/压缩/decode/质量 → 不作为 PASS 证据；SnapKV 为 attention-only future prototype）

## 10. commits、产物路径、工作树

**llama.cpp（clean）**：
- `4d6e9877`（fix: capability boundary，E8.2）
- `e8484452`（docs: tenant scope，E8.3）
- `94e88846`（test: LoRA identity，E8.4）

**根仓库（clean）**：
- `5855858`（E8.0）、`fea4575`（E8.1）、`58d3f76`（E8.2 报告）、`9bd9500`（E8.3 报告）、`4b4a1c8`（E8.4 报告）、`439cfa2`（E8.5 实验+报告）、`59787b4`（E8.6）、`3a0c211`（E8.7）

**产物**：`docs/E8_0..E8_7 + E8_FINAL`、`benchmark/scripts/e8_c1_perf.py`、`raw/e8_c1_perf_*.json`、`papers/kv-cache/{manifest,references.bib}`（修正）

**工作树**：两仓库 clean（未跟踪：`docs/20260808_E8_INSTRUCTION.md`）

## 11. 唯一最终结论

**PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION**：C1 是真实、正确、隔离的 attention-only 优化（正向能力声明、identity/lifecycle/性能/容量验收通过、TinyLlama 上 recompute -99.1%/e2e 311→10ms/容量 headroom -49.3%）；但主模型 Qwen3.5-4B（hybrid）上机制性不可用（路线 B NO_GO），主模型优化目标未达成。E6 的 PASS 不恢复；若用户确认 attention-only 为目标范围，可基于 E8.5 完整验收重新评估。
