# E7 复核任务指令（2026-08-08）

用户于 2026-08-08 启动 KV Cache 优化项目复核任务。本文件是任务需求（完整 805 行指令的浓缩版，用于上下文压缩后自动读取恢复）。

## 目标
复核 E6.0-E6.6（最终状态 PASS_KV_CACHE_OPTIMIZATION 被判定为 provisional，不能直接接受），审计 C1 真实实现、上游对比、cache identity、扩展 benchmark、修正 C2 范围与文献 metadata，最终给出唯一诚实状态。

## 核心质疑点
1. C1 跨 slot exact-prefix KV sharing 确实进真实 KV 路径（seq_cp）
2. TinyLlama 上 recompute 229→24
3. 但主模型 Qwen3.5-4B 是 hybrid，C1 自动禁用 → 主模型无收益
4. C1 是线性 idle-slot LCP 扫描 + seq_cp，不是完整 radix tree
5. cache identity、LoRA/adapter 隔离、state restore、性能退化、p95、非共享 workload 需独立验证
6. C2 只是容量估算，不是 SnapKV oracle
7. 文献 venue/发表状态需重新核验

## 阶段（每阶段独立提交）
- E7.0 项目复核和环境确认（docs/E7_0_PROJECT_REVIEW_BASELINE.md；提交 docs: reassess KV optimization completion status）
- E7.1 C1 cache identity 和生命周期审计（提交 test: audit cross-slot KV sharing isolation）
- E7.2 C1 必要代码修正（提交 fix: harden cross-slot KV cache identity）
- E7.3 上游实现对比（docs/E7_1_LLAMA_CPP_UPSTREAM_COMPARISON.md；提交 docs: compare KV sharing with upstream llama.cpp）
- E7.4 扩展 paired benchmark（提交 test: expand cross-slot KV benchmark matrix）
- E7.5 C2 范围和文献修正（docs/E7_2_LITERATURE_METADATA_CORRECTION.md + 修正 manifest/bib/notes；提交 docs: correct KV compression scope and literature metadata）
- E7.6 最终项目状态复核（docs/E7_3..E7_6 + E7_FINAL_REVIEW_REPORT.md；提交 docs: finalize KV optimization reassessment）

## 关键审计点（C1）
- seq_cp 在 unified pool 实际语义（同 stream 纯 bitset 元数据）
- 共享源保护（get_available_slot / try_clear_idle_slots 跳过 shared>0）
- 共享 cell 覆盖时所有引用处理（apply_ubatch cells.rm 清全部关联 = 覆盖即失效）
- cache identity：model/adapter/adapter scale/RoPE/KV type/context config/backend/tenant 隔离
- hybrid 检测（llama_model_is_hybrid）是否覆盖所有 recurrent/hybrid 架构
- 生命周期：slot 清理/retry/cancel/resume/state restore/generation 变化
- 关闭时是否完全回基线

## C1 名称修正
若仍为线性 LCP 扫描 + seq_cp（无 radix tree/节点分裂/LRU），统一改名为：
"Cross-slot exact-prefix KV metadata sharing"（可注明 Inspired by RadixAttention）
不得称 RadixAttention implementation。

## 验收门槛
- C1_ATTENTION_ONLY_PASS：16 条件全满足（含 identity 隔离、生命周期、性能退化门禁、recompute ≥25% 下降、非共享 workload ≤5% 退化、prefill/decode median ≤5% 下降、p95 ≤10% 增加、原始数据可复现）
- 只满足 1-7 → C1_CONDITIONAL_PASS_RECOMPUTE_ONLY
- identity/生命周期失败 → C1_REJECT_CORRECTNESS_OR_ISOLATION
- 正确性过但无端到端收益 → C1_HOLD_NO_END_TO_END_GAIN

## 主模型状态
- Qwen3.5-4B 上 C1 安全禁用无收益 → QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
- 错误共享/污染 → REJECT
- 不得声称优化了 Qwen3.5-4B

## C2 状态
仅容量估算 → C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR（不得称 SnapKV oracle）
方向 A：降级为离线容量估算；方向 B：补成真 oracle（真实 attention score/压缩 KV/decode/质量）

## 最终状态规则（5 选 1）
1. PASS_KV_CACHE_OPTIMIZATION：仅当主模型有收益或明确把 attention-only 定义为目标范围 + 全部门禁过 + 上游对比完成 + 文献 metadata 无重大错误
2. PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION：真实实现 + correctness 过 + attention-only 有收益 + 主模型无收益（**主模型仍为 Qwen3.5-4B 时默认优先**）
3. PARTIAL_KV_RESEARCH_NO_IMPLEMENTATION：无真实代码实现（C1 存在则不可用）
4. HOLD_KV_OPTIMIZATION_INCOMPLETE：仅外部阻塞（GPU/网络/凭据/用户暂停）
5. REJECT_KV_CACHE_OPTIMIZATION：错误复用/污染/泄漏/伪造

## 交付
- docs/E7_*.md 系列报告（区分历史声明/复核证据/确认事实/无法证明结论/修正/最终状态）
- 性能表必须展示 baseline/candidate 原始值 + delta + 相对变化 + reps + median + p95 + 是否达门槛
- 最终回复格式：
  PROJECT_STATUS / C1_ATTENTION_ONLY_STATUS / QWEN3.5_4B_STATUS / C2_STATUS / UPSTREAM_COMPARISON_STATUS + 13 项说明
- 结论必须用 PASS/PARTIAL/HOLD/REJECT/NOT_APPLICABLE/NOT_VERIFIED，禁用模糊词
- 第一条工作更新必须报告：仓库 HEAD/工作树/E6 一致性/C1 代码入口/model/GPU/remote/初步报告矛盾
