# E7：最终复核报告（Final Review Report）

- 复核完成时间：2026-08-08
- 复核对象：E6.0-E6.6（原 `PASS_KV_CACHE_OPTIMIZATION`，provisional）

## 最终状态

```text
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
C1_ATTENTION_ONLY_STATUS: C1_CONDITIONAL_PASS_RECOMPUTE_ONLY
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR
UPSTREAM_COMPARISON_STATUS: UPSTREAM_COMPARISON_COMPLETE
```

## 1. 项目完成评价

**PARTIAL**：C1 是真实、正确、隔离的 attention-only KV 优化（TinyLlama 上 recompute 下降 67-94% 且无损）；但主模型 Qwen3.5-4B（hybrid）上 C1 自动禁用、无收益——项目级"优化主模型"目标未达成。诚实结论：**correctness 与 attention-only 收益成立，主模型优化不成立**。

## 2. 上次 PASS 结论是否保留

**不保留**。E6 的 `PASS_KV_CACHE_OPTIMIZATION` 降级为 `PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION`，理由：
- 主模型无收益（C1 hybrid 禁用，实证）
- 收益仅存在于 attention-only 特定 workload（TinyLlama）
- 项目未将 attention-only 定义为目标范围（主模型仍为 Qwen3.5-4B）

## 3. C1 实际实现和审计结论

- 实现：`update_slots → cache_prompt 分支`，idle slot 线性 LCP 扫描（`get_common_prefix`）+ `llama_memory_seq_cp(0, lcp)` **纯元数据共享**（bitset 位，不碰数据 buffer）+ 共享源保护（`seq_cell_stats.shared>0` 跳过）+ `--kv-prefix-share`（默认 off）
- **非 radix tree**：无前缀树/节点分裂/LRU/cache-aware scheduling
- 审计 20 问：机制正确（unified 单 stream、无并发竞态、无悬空引用、覆盖免疫、off 回基线）
- **E7.2 修复 3 个高危缺陷**：lora/adapter identity（are_lora_equal）、SWA/iswa purge 破坏（n_swa==0 禁用）、recurrent 漏检（is_recurrent 禁用）
- 名称修正：**Cross-slot exact-prefix KV metadata sharing**（Inspired by RadixAttention）

## 4. cache identity 和隔离结论

- identity 判断 = token LCP ≥ min_lcp + adapter(ptr+scale) 相同 + 模型非 hybrid/recurrent/SWA + kv_unified
- model/revision/RoPE/KV type/context config：同 ctx_tgt 隐含固定（server 单模型实例）
- grammar/tool/sampler：不参与（不影响已存 KV 内容）；tenant/session：共享设计意图，canary 测试证明无泄漏
- 10 项审计测试全过（不同 prefix 禁共享/min-lcp 边界/多 target/最长 LCP/source 删除/target 删除/双向清理/canary）
- **NOT_TESTED**（无模型文件）：lora 隔离实测、SWA 禁用实测、recurrent 禁用实测（代码级保护已实现）

## 5. 上游对比结论

- 上游 ggml-org/llama.cpp master `3653e6d6d` **无 `--kv-prefix-share`** → C1 非重复实现
- 上游唯一跨 slot seq_cp 是 `copy_state_to` 全量复制（`-1,-1`，fork 语义），与 C1 部分前缀共享不同
- 无可直接移植修复；推荐保留本地 C1
- 限制：`--depth 1` 浅克隆，未遍历完整历史/issue/PR

## 6. 扩展实验结果（TinyLlama，5 reps）

| 场景 | off recompute | on recompute | 相对变化 | 无损 |
|---|---|---|---|---|
| M01 2-slot 共享 | 209 | 14 | -93.3% | ✓ |
| M02 3-slot 多 target | 208.5 | 13 | -93.76% | ✓ |
| M03 partial prefix | 125 | 31 | -75.2% | ✓ |
| M04 无共享（退化门禁）| 135 | 135 | 0.0% | ✓ |
| M05 长代码 | 227 | 75 | -66.96% | ✓ |
| M06 长摘要 | 425 | 43 | -89.88% | ✓ |
| M07 tool JSON | 319 | 52 | -83.7% | ✓ |
| M08 canary | 235 | 24 | -89.79% | ✓ 无泄漏 |

原始值全量存 `benchmark/results/kv_optimization/raw/e7_c1_matrix.json`；每 rep slot erase 独立、seed 42/temp 0 固定。

## 7. C2 修正结论

`KV_BUDGET_FEASIBILITY_ESTIMATOR`：仅容量估算（1024 budget 下 attention KV 理论上界 -68.6%）；无 attention score/压缩 KV/decode/质量 → 不作为 PASS 依据；SnapKV 归为 attention-only future prototype。

## 8. 文献修正结论

12 篇正式收录 venue 全部核验确认（H2O/ScissorHands=NeurIPS 2023、KIVI=ICML 2024、SnapKV=NeurIPS 2024 poster+preprint 正文、CacheBlend=EuroSys 2025 等）；8 篇 preprint 保持 VENUE_UNVERIFIED；manifest/bib 已更新。

## 9. 代码和测试修改

- llama.cpp `e7e08552`（test: audit，10 例）+ `f5837427`（fix: harden identity/SWA/recurrent）
- 根仓库 `0d4bcbb`（audit 报告）+ `d4b7d04`（上游对比）+ `d620c13`（扩展 benchmark）+ `fbacf8d`（4B 复验数据）+ `f800802`（C2/文献修正）

## 10. 已知限制

1. wall-time 性能门禁（prefill/decode median/p95）在消费级 GPU + TinyLlama ms 级噪声下无可靠证据 → C1 为 CONDITIONAL_PASS（recompute-only）
2. lora/SWA/recurrent 隔离无对应模型实测（代码级保护 + 条件已实现）
3. 上游对比为浅克隆快照
4. 主模型 hybrid 上 C1 不可用（架构限制，非实现缺陷）

## 11. 下一步方向

1. **主模型收益路径**：需实现 recurrent state 的安全共享/复用（如 hybrid seq_cp 语义补全）或选择 attention-only 模型作为项目目标范围（需用户确认）
2. C1 升级完整 PASS：补 wall-time 性能门禁（稳定环境）或正式接受 recompute 代理
3. C2 方向 B（真 SnapKV oracle）：限 attention-only future prototype
4. lora/SWA/recurrent 实测：需对应模型文件

## 12. commit 与产物路径

- llama.cpp：`e7e08552`、`f5837427`（工作树 clean）
- 根仓库：`0d4bcbb`、`d4b7d04`、`d620c13`、`fbacf8d`、`f800802` + E7 系列报告
- 产物：`docs/E7_0/E7_1/E7_2/E7_3/E7_4/E7_5/E7_6/E7_FINAL`、`benchmark/scripts/e7_c1_bench_matrix.py`、`raw/e7_c1_matrix.json`

## 13. 工作树状态

两仓库 clean（除未跟踪 `docs/20260808_REVIEW_INSTRUCTION.md`、`docs/20260807_MAJOR_INSTRUCTION.md`——指令文件，建议提交）
