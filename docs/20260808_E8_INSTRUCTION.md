# E8 指令文件（2026-08-08，浓缩版）

用户于 2026-08-08 启动 E8：KV Cache 优化证据收敛、边界加固、性能验收和主模型路径决策。

## 冻结状态（不得自行提升）
```
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
C1_ATTENTION_ONLY_STATUS: C1_CONDITIONAL_PASS_RECOMPUTE_ONLY
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR
UPSTREAM_COMPARISON_STATUS: UPSTREAM_SNAPSHOT_COMPARISON_COMPLETE
LITERATURE_METADATA_STATUS: REQUIRES_CORRECTION
```

## 阶段与产物
- E8.0 证据完整性复核 → docs/E8_0_EVIDENCE_INTEGRITY_REPORT.md（每结论标 VERIFIED/PARTIALLY_VERIFIED/NOT_VERIFIED/CONTRADICTED；独立从 raw JSON 重算 E6/E7 汇总；不修改 raw）
- E8.1 文献二次核验（SGLang/ChunkAttention/InfiniGen/LM-Infinite/H2O/KIVI/SnapKV/ScissorHands/CacheBlend/DuoAttention/StreamingLLM/Quest）→ docs/E8_1_LITERATURE_CORRECTION_ADDENDUM.md（逐项：E7 声称/E8 核验/证据来源/修正/是否影响路线）
- E8.2 C1 正向能力边界 → docs/E8_2_C1_CAPABILITY_BOUNDARY_AUDIT.md：
  - 从负向黑名单（!hybrid&&!recurrent&&n_swa==0）升级为**正向能力判断函数**（默认不支持，只有审计过的标准 unified attention KV 实现返回支持）
  - 启用日志记录 capability/LCP/LoRA/tenant 判断
  - min-lcp 负值/零值明确拒绝或规定范围
  - 审计 n_past==n_tokens 回退 token 行为
- E8.3 tenant/session/identity 策略 → docs/E8_3_C1_TENANT_AND_IDENTITY_POLICY.md：
  - 区分 KV correctness/输出隔离/timing side-channel/tenant 策略
  - 无可信 tenant identity 时声明 trusted_single_tenant_deployment 限制；multi-tenant 标 NOT_VERIFIED
  - 不得从 prompt 文本/普通 header 猜 tenant
- E8.4 真实架构运行时验证（LoRA/SWA/recurrent/hybrid）→ docs/E8_4_C1_RUNTIME_ARCHITECTURE_VALIDATION.md：无模型则记录缺失资产+尝试来源+NOT_VERIFIED，不伪造
- E8.5 端到端性能和容量验收 → docs/E8_5_C1_PERFORMANCE_AND_CAPACITY_REPORT.md：
  - warmup≥5 + 正式≥30 次独立；paired on/off；CV>5% 标 NOISY；不删离群值
  - 指标：queue wait/prefill/decode/e2e latency/tps/p50/p95/CPU/GPU/shared_cells/logical KV/容量边界/failed/timeout/hash
  - 容量区分：预分配 bytes vs used cell vs 共享 headroom vs 最大 workload；不得声称物理分配降低
  - C1_ATTENTION_ONLY_PASS 升级条件（9 条全满足；任一未达保持 CONDITIONAL_PASS_RECOMPUTE_ONLY）
- E8.6 上游完整对比 → docs/E8_6_LLAMA_CPP_UPSTREAM_HISTORY_COMPARISON.md：
  - 获取足够历史（非 --depth 1）；检索 commit/issue/PR/discussion/release note
  - 关键词：prefix cache/shared prefix/seq_cp/cross-slot/cache_prompt/unified KV/prompt cache/LoRA cache identity/slot state copy/recurrent memory
  - 最终状态只能 UPSTREAM_HISTORY_AND_ISSUE_REVIEW_COMPLETE 或 UPSTREAM_SNAPSHOT_COMPARISON_COMPLETE
- E8.7 主模型路线决策 → docs/E8_7_QWEN35_HYBRID_PREFIX_REUSE_DECISION.md：
  - 路线 A：缩小为 attention-only 目标（需用户接受范围变化）
  - 路线 B：hybrid exact-prefix reuse 可行性研究（保存/复制/恢复 A 末尾 recurrent snapshot；量化成本；最小无损 prototype 或 NO_GO；无损=token ID 完全一致）
  - 不得把 feasibility note 写成已优化 4B
- E8_FINAL_REVIEW_REPORT.md

## 最终状态规则
默认保持 PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION。仅当：
1. 用户明确 attention-only 为目标范围且 C1 全验收过；或
2. Qwen3.5-4B 上实现新无损 exact-prefix reuse 并通过完整验收；才可升级。
发现错误共享/tenant 越界/LoRA identity 错误/SWA/recurrent 错误启用/泄漏/raw 不可重现/伪造 → 立即 REJECT_KV_CACHE_OPTIMIZATION。

## 禁止
- 删噪声数据；只展示共享命中；recompute 替代 latency；逻辑 headroom 当物理 bytes 降低；安全禁用当主模型优化；静态阅读替代运行时验证；"没发现 issue" 写成 "上游没有实现"；线性 LCP 称 radix tree；改 E6/E7 历史报告。

## 最终回复格式
PROJECT_STATUS / C1_ATTENTION_ONLY_STATUS / QWEN3.5_4B_STATUS / C2_STATUS / UPSTREAM_COMPARISON_STATUS / LITERATURE_METADATA_STATUS + 11 项说明 + 唯一最终结论。
