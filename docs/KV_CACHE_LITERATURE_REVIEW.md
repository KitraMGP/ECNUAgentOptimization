# KV Cache 文献综述（KV Cache Literature Review）

- 阶段：E6.0（文献、架构审计和实验冻结）
- 生成日期：2026-08-07
- article-mcp 状态：`AVAILABLE_BUT_LIMITED`（详见 `benchmark/results/kv_optimization/article_mcp_log.json`；Europe PMC/PubMed 对系统类论文覆盖不足，arXiv 源返回时间排序噪音；已改用 arXiv/官方页面获取全文）

## 1. 检索与论文统计

- 候选论文：**21 篇**（要求 ≥15）
- 全文下载（PDF + SHA256 + 文本提取）：**21 篇**（要求 ≥10）
- 完整阅读（FULL_TEXT_READ）：**19 篇**（要求 ≥8）
- 精读（deep read，含数据结构/算法/参数/消融/限制/llama.cpp 映射）：**8 篇**（要求 ≥5）
- 摘要级（ABSTRACT_ONLY）：2 篇（vPID 通过 article-mcp 获取）
- 每篇笔记：`papers/kv-cache/notes/*.md`
- BibTeX：`papers/kv-cache/references.bib`
- 下载日期：2026-08-07（全部）

### 检索方向覆盖（指令三重点清单）

| 指令方向 | 覆盖论文 |
|---|---|
| paged/block-based KV allocation | PagedAttention、vAttention、LeanKV/DiffKV |
| virtual-memory-backed KV allocation | vAttention |
| exact prefix sharing、radix cache、COW | SGLang/RadixAttention、ChunkAttention、MemServe |
| cache-aware scheduling、reuse-aware eviction | SGLang/RadixAttention |
| KV quantization、mixed precision、per-layer/per-head | KIVI、KVQuant、LeanKV/DiffKV、Palu |
| token/head/layer eviction | H2O、SnapKV、ScissorHands、PyramidKV、DuoAttention、TOVA |
| sink/recent/heavy-hitter retention | StreamingLLM、LM-Infinite、H2O |
| query-aware sparse KV access | Quest |
| CPU/GPU/SSD KV offload 和 prefetch | InfiniGen、CacheBlend、vPID |
| low-rank KV | Palu |
| RAG chunk/non-prefix KV reuse | CacheBlend |
| KV compression 正确性/工具调用影响 | 见 12.12 质量门禁（本任务 workload W17-W24 覆盖） |

## 2. 核心论文精读结论（8 篇 deep read）

### 2.1 PagedAttention（vLLM, SOSP 2023）
固定大小 KV block + block table 映射非连续物理内存；refcount + COW + 抢占（swap/recompute）。吞吐 2–4×；共享 prefix 5-shot 3.58×；kernel 开销 20–26%。**对 llama.cpp：读取侧需为全部 ggml 后端重写 attention kernel，与图执行架构根本冲突 → 完整方案不采用**；但 cell 级按需填充与跨序列共享（`seq_cp` bitset）已原生存在。

### 2.2 vAttention（ASPLOS 2025）
CUDA VMM 解耦虚拟/物理分配：虚拟连续（kernel 兼容）+ 物理按需映射。decode 吞吐最高 1.99×、端到端 1.23×。**对 llama.cpp：依赖 CUDA VMM + NVIDIA 驱动改动，仅 CUDA 后端 → 不采用**；调度思想（分配重叠、延迟回收）可借鉴。

### 2.3 H2O（NeurIPS 2023）
Heavy-Hitter 识别：累计注意力分数 power-law；budget 对半（H² + recent）。5× 内存缩减无精度损失；20% budget 与 full 相当。**对 llama.cpp：per-head 独立淘汰与 token 共享 cell 冲突 → 原样不采用**；token 级共享分数简化版有条件可行。

### 2.4 SnapKV（NeurIPS 2024）
observation window 投票 + TopK + pooling 压缩 prompt KV；生成阶段零开销。3.6× 解码加速、8.2× 内存效率；NIAH 380× 压缩。**对 llama.cpp：与 prefill/decode 两阶段天然匹配；主要工作 = prefill 注意力分数输出通道 + KV 紧凑重排 → 列为 P1 有损候选**。

### 2.5 KIVI（ICML 2024）
K per-channel + V per-token 非对称 2bit；grouped + residual(fp16) 混合。2bit 精度损失 ≤2%；2.6× 峰值内存降低、2.35–3.47× 吞吐。**对 llama.cpp：缺 2bit 类型/per-channel 布局/混合精度窗口 → 精确复现不采用**；现有 `-ctk q4_0 -ctv q4_1` 可作降级近似。

### 2.6 KVQuant（NeurIPS 2024）
5 组件（per-channel K、pre-RoPE、nuqX 非均匀、dense+sparse、attention sink）。3bit <0.1 PPL 退化、4.8× 压缩。**对 llama.cpp：需离线校准管线 + 新 kernel → 不采用**；`IQ4_NL` 可作非均匀 4bit 近似。

### 2.7 SGLang/RadixAttention（SOSP 2024）
radix tree（CPU 维护）+ LRU 淘汰（ref=0 叶子优先）+ cache-aware scheduling。吞吐 6.4×、命中率 50–99%、overhead <0.3%。**对 llama.cpp：在 unified KV pool 之上加 CPU 侧 radix tree（边 = cell 区间），复用现有 `get_common_prefix`/`k_idxs`/prompt cache → 无需新 CUDA kernel，工程量在 server/context 层 → 列为 P0 无损主候选**。

### 2.8 Quest（ICML 2024）
页级 min/max 元数据 + query 点积上界 + Top-K 页加载。7.03× self-attention 加速。**对 llama.cpp：Stage 1 可用现有 ggml 算子，但 Stage 2 需稀疏页加载 CUDA kernel → 不采用**（且不省内存）。

## 3. 其他论文（FULL_TEXT_READ，概要）

| 论文 | 机制 | llama.cpp 可实现性 |
|---|---|---|
| ChunkAttention | 前缀树 chunk 共享 + TPP kernel | 简化版（前缀去重）可行；完整版 kernel 高成本 |
| InfiniGen | 相邻层注意力推测 + 关键 KV 预取（offload） | 长期候选；SVD 变换可独立 |
| CacheBlend | selective KV recompute（RAG 非前缀复用） | 中期候选；`cpy_k/cpy_v`+K-shift 已具备 |
| PyramidKV | 层间金字塔预算 + instruction token 投票 | 可实现（P2 有损）；保留原始 rotary 位置天然兼容 |
| DuoAttention | retrieval/streaming head 分类，streaming 常数 KV | 可实现但需 head 分类训练工具链 |
| Palu | 低秩投影压缩 hidden dim | 可实现但架构级改动 + 新算子 |
| ScissorHands | 持久重要性假说 + 固定预算驱逐 | 可实现（驱逐+mask，中等复杂度） |
| MemServe | disaggregated serving + MemPool | 架构异构，不采用 |
| DiffKV/LeanKV | 差异化精度 + 并行 KV compaction | 需 paged 重构，不采用 |
| StreamingLLM | attention sink（4 token）+ rolling window | 可实现（保留头部 cell + 环形滚动） |
| TOVA | 每步 attention 分数驱动 token 丢弃 | 高难度（需位置重映射） |
| LM-Infinite | Λ-shaped mask + distance ceiling | 可实现（mask + RoPE 钳制） |

## 4. 论文 → 候选映射结论

综合 21 篇论文与 llama.cpp 架构审计，可落地的方向收敛为：

1. **P0 无损主候选：RadixAttention 式前缀共享**（SGLang/RadixAttention SOSP'24 为最直接证据；ChunkAttention、MemServe、CacheBlend 的 prefix/radix 思想佐证）——在 llama.cpp server 层加 CPU 侧 radix tree，复用现有 unified KV pool 的 cell/seq 基础设施，不重写 kernel。收益：exact-prefix workload 下 recompute tokens 下降（指令 12.11 门槛之一：exact-prefix recompute tokens -25% 或 physical KV bytes -15%）。
2. **P1 有损第二候选：SnapKV 式 prompt KV 压缩**（SnapKV NeurIPS'24 为主证据；PyramidKV、ScissorHands、StreamingLLM 佐证）——prefill 后一次性压缩 prompt KV，生成阶段零开销。收益：相同逻辑状态下实际 KV bytes 下降（12.11 门槛）。
3. 已评估并排除：PagedAttention 完整方案（kernel 重写）、vAttention（驱动依赖）、Quest（kernel 依赖）、KIVI/KVQuant 精确复现（缺类型/校准管线）、MemServe（架构异构）、DiffKV（paged 重构）。

详见 `benchmark/results/kv_optimization/paper_route_matrix.json` 与 `optimization_hypotheses.json`。
