# E6.0：KV Cache 优化研究、架构审计与实验冻结报告

状态：**E6.0 COMPLETE**（阶段退出条件 12.19 全部满足）

## 1. 环境快照

- 根仓库：`benchmark-enhance` @ `023e925`（工作树仅未跟踪的指令文件）
- llama.cpp：`llama.cpp` @ `69711a2d6`（E4.2 修复后，干净）
- GPU：RTX 4060 Laptop 8GB（8188 MiB，40 MiB 已用），驱动 610.43.03，CUDA 13.3，arch 89
- 模型：`models/qwen3-5-4B-Q4_K_M.gguf`（2.7GB，sha256 `de8e96cd…`）
- 构建：CPU `build/` + CUDA `build-cuda/` 均就绪
- 磁盘：127G 可用
- 已有进程：无 llama-server，8080 空闲
- 完整冻结：`benchmark/results/kv_optimization/environment.json`

## 2. article-mcp 探测结果

- **状态：`AVAILABLE_BUT_LIMITED`**（`benchmark/results/kv_optimization/article_mcp_log.json`）
- 可用工具：`search_literature` / `get_article_details` / `get_references` / `get_literature_relations` / `get_journal_quality`（mcp__article-mcp__* 全部注册且调用成功）
- 限制：Europe PMC/PubMed/OpenAlex 对计算机系统类论文覆盖不足（精确标题检索 4 组中 3 组返回 0 结果）；arXiv 源返回按时间排序的最新条目而非相关性排序
- 处置（按指令 12.4）：记录错误/限制 → 改用 arXiv 官方页面 + 正式会议页（SOSP/ASPLOS/NeurIPS/ICML/ICLR/EuroSys/ACM DL）获取全文 → 继续任务
- article-mcp 实际贡献：vPID 论文（Europe PMC 命中，RTX 4060 同款硬件的 KV 持久化系统，摘要级）

## 3. 论文检索与阅读统计

| 指标 | 要求 | 实际 |
|---|---|---|
| 检索查询 | ≥4 组 | 9 组（article-mcp）+ 15 组（web 核验）|
| 候选论文 | ≥15 | **21** |
| 全文下载（PDF+SHA256+文本）| ≥10 | **21** |
| 完整阅读（FULL_TEXT_READ）| ≥8 | **19** |
| 精读 | ≥5 | **8** |
| 摘要级 | — | 2（vPID）|

- 笔记：`papers/kv-cache/notes/`（21 篇）
- BibTeX：`papers/kv-cache/references.bib`
- manifest（含 SHA256、阅读状态、候选映射）：`papers/kv-cache/manifest.json`
- PDF 文件存 `papers/kv-cache/pdf/`（git 忽略），SHA256 记录于 manifest
- 综述：`docs/KV_CACHE_LITERATURE_REVIEW.md`

### 已核验正式出处（指令三核验清单）

| 论文 | 核验结果 |
|---|---|
| PagedAttention | SOSP 2023 ✓（arXiv 2309.06180，DOI 10.1145/3600006.3613165）|
| vAttention | ASPLOS 2025 ✓（arXiv 2405.04437，DOI 10.1145/3669940.3707256）|
| ChunkAttention | arXiv preprint（VENUE_UNVERIFIED）|
| RadixAttention/SGLang | SOSP 2024 ✓（arXiv 2312.07104）|
| KIVI | ICML 2024 ✓（PMLR 235，正文页脚；文件备注 ICLR 2024 与正文矛盾，以正文为准）|
| KVQuant | NeurIPS 2024 ✓（GitHub + OpenReview 确认）|
| H2O | NeurIPS 2023 ✓（正文标注；ICML 页为旧链接）|
| SnapKV | NeurIPS 2024 ✓（neurips.cc poster 93531）|
| Quest | ICML 2024 ✓（GitHub + PMLR 235）|
| InfiniGen | arXiv preprint（VENUE_UNVERIFIED）|
| CacheBlend | EuroSys 2025 ✓（DOI 10.1145/3689031.3696098）|
| StreamingLLM | ICLR 2024 ✓（正文标注）|
| DuoAttention | ICLR 2025 ✓（proceedings 确认）|
| ScissorHands | NeurIPS 2023（正文 preprint；自引 NeurIPS 36）|
| MemServe | arXiv preprint（VENUE_UNVERIFIED）|
| 2025-2026 新工作 | PyramidKV/DiffKV/Palu/LM-Infinite/TOVA 均为 preprint（VENUE_UNVERIFIED 标记）|

### 下载纠错记录

首次批量下载 6 篇 arXiv ID 错配（2406.01142→中微子、2405.05331→凝聚态、2407.03618→BM25、2501.11092→数学、2504.07836→CV、2405.08518→控制理论），已用正确 ID 重下并标注于 manifest（DuoAttention=2410.10819、Palu=2407.21118、ScissorHands=2305.17118、MemServe=2406.17565、LeanKV/DiffKV=2412.03131）。

## 4. 代码审计结论（docs/KV_CACHE_CURRENT_ARCHITECTURE.md）

13 项审计全覆盖，关键结论：
1. **cell 结构**：`llama_kv_cells`（token 粒度元数据：pos/ext/shift/seq bitset），数据在连续 tensor；无 defrag（`mv()` 注释禁用）
2. **unified pool**：`kv_size = n_ctx`（unified）固定预分配 3D tensor `[n_embd_gqa, kv_size, n_stream]`，运行时不可扩展
3. **所有权**：slot.id = seq id；cell seq bitset 支持多 seq 元数据共享（非 COW）
4. **状态**：7 态 slot_state，无 CANCELLED/protected；purge 只选 idle slot
5. **prefix reuse**：token 级 LCP（`get_common_prefix`）+ `n_cache_reuse` 位移复用；无 hash/radix
6. **淘汰**：`try_clear_idle_slots`（default/lru），整 slot 粒度
7. **seq_cp**：同流纯元数据共享（零复制）；无物理 COW（`physical_sharing=false` 显式声明）
8. **RoPE**：pos 存 cell；量化 KV 经 Hadamard 旋转解决；adapter 不参与 cache identity
9. **buffer**：按层 buft 一次性预分配；used_bytes = used_cells×(size_k+size_v)/capacity_cells
10. **kernel**：flash attention 吃连续 view，空洞由 kq_mask 屏蔽；无 paged
11. **KV 类型**：F16/F32/BF16/Q8_0/Q4_0/Q4_1/IQ4_NL/Q5_0/Q5_1；per-token 行内 block（QK=32）
12. **save/load**：state_write/read + RAM prompt cache + checkpoint + 磁盘 slot
13. **指标缺口**：internal fragmentation、recompute tokens、physical blocks/refcount、eviction 统计

## 5. 候选路线评分与选择（12.6 公式）

candidate_score = 0.30×gain + 0.25×compat + 0.20×feas + 0.15×corr + 0.10×paper（0-5 分）

| 候选 | 路线 | gain | compat | feas | corr | paper | 总分 | 状态 |
|---|---|---|---|---|---|---|---|---|
| **C1 RadixAttention 式前缀共享** | P0-B | 4 | 4 | 3 | 4 | 5 | **3.9** | **SELECTED（主候选）** |
| **C2 SnapKV 式 prompt KV 压缩** | P1-G | 3 | 3 | 3 | 3 | 4 | **3.1** | **SELECTED（第二候选）** |
| C3 Paged block pool | P0-A | 2 | 1 | 1 | 2 | 5 | 1.9 | REJECT_INCOMPATIBLE_ARCHITECTURE |
| C4 reuse-aware eviction | P0-C | 2 | 4 | 4 | 4 | 2 | 3.2 | DEFERRED（lru 已有，收益稀释）|
| C5 KV 量化组合 | P1-E | 3 | 4 | 4 | 3 | 4 | 3.55 | DEFERRED（已有功能重测，作对照）|
| C6 vAttention VMM | P0-D | 3 | 1 | 1 | 2 | 4 | 2.2 | REJECT_UNSUPPORTED_HARDWARE |

### C1（主候选）技术要点
- 在 unified KV pool 之上加 **CPU 侧 radix tree**（边 = 连续 cell 区间）；复用现有 `get_common_prefix`、`k_idxs/v_idxs` 非连续写入、`seq_cp` 元数据共享、RAM prompt cache
- 节点分裂/合并 + refcount（cell bitset 可数）+ LRU（叶子优先）+ cache-aware 批内排序（近似 DFS）
- **无需改 attention kernel**（tree 逻辑在 CPU，KV 复用利用现有 cell 布局）
- 收益：exact-prefix workload recompute tokens 下降 ≥25% 或 physical KV bytes 下降 ≥15%（12.11 门槛）
- 风险：slot 与 seq 绑定粒度改造、LLAMA_MAX_SEQ bitset 上限、cache-aware 排序触碰 fairness

### C2（第二候选）技术要点
- prefill 后一次性压缩 prompt KV：observation window（Lobs）query 对 prefix key voting → TopK + pooling → 选中 prefix KV + window 拼成压缩 KV；生成阶段零开销
- 需新增：prefill 注意力分数输出通道（FA 下不物化，需额外实现）+ KV 紧凑/重排（`ggml_set_rows` 索引 copy + cell 元数据迁移）+ server 层触发与记账
- per-head 选择在共享 cell 下降级为 token 级（可实验验证精度差异）
- 收益：相同逻辑状态实际 KV bytes 下降 ≥25%（12.12 有损门槛）+ 质量门禁（12.12 全项）
- 对照基线：full-KV、uniform Q8、uniform Q4、候选混合（同字节预算）

## 6. 假设冻结（optimization_hypotheses.json）

每个候选含 13 字段（paper_evidence/current_code_locations/bottleneck/proposed_change/invariants/implementation_steps/expected_metrics/expected_gain_threshold/quality_risks/performance_risks/minimal_reproducer/rollback_condition/status+reason）。公共不变量：
- active/protected KV 永不成为 victim
- 无跨 session/generation/model/adapter 错误复用（cache identity：model+adapter+RoPE+KV type+推理配置）
- 无损候选输出 token ID 与基线完全一致
- 清空后资源返回预注册基线；trace-off lifecycle event 为 0

## 7. 基线与测试矩阵冻结

- **environment.json**：commit/GPU/driver/CUDA/compiler/build flags/模型 sha256/KV 类型/ctx 8192/seed 42/环境变量/已有进程/磁盘（29 项）
- **workload_manifest.json**：24 workload（W01-W24）全量注册（12.8 最低集合）；并发 1/2/4/6/8、短/长/混合、exact/partial/no prefix、idle reuse/active pressure/cancel/retry/churn/超容量、长代码/长摘要/needle/多指令/tool JSON/system retention
- 重复规则（12.9）：correctness 确定性 ≥1 次、并发/生命周期 ≥3 次；性能 warmup≥2 + 正式≥5 次，中位数主值 + min/max/mean/p95，CV>5% 增至 10 次并标记 NOISY
- wall-time 仅辅助指标（正式指标 = 容量/reuse/bytes/重算/碎片）
- 容量极限：固定步长/二分搜索 + 边界点 3 次全成功

## 8. 阶段退出条件核对（12.19 E6.0）

| 条件 | 状态 |
|---|---|
| article-mcp 已探测 | ✓（AVAILABLE_BUT_LIMITED，日志完整）|
| 达到最低论文阅读数量 | ✓（21/19/19/8 vs 12/8/6/4）|
| 当前架构文档完成 | ✓（docs/KV_CACHE_CURRENT_ARCHITECTURE.md）|
| 至少两个候选完成评分 | ✓（C1=3.9、C2=3.1、C3-C6 已评分）|
| environment 和 workload manifest 已冻结 | ✓ |
| optimization hypotheses 已写入文件 | ✓ |

**E6.0 COMPLETE**

## 9. 产物与提交

产物：
- `papers/kv-cache/manifest.json`、`references.bib`、`notes/`（21 篇）、`pdf/`（git 忽略）
- `docs/KV_CACHE_LITERATURE_REVIEW.md`
- `docs/KV_CACHE_CURRENT_ARCHITECTURE.md`
- `benchmark/results/kv_optimization/`：environment.json、workload_manifest.json、optimization_hypotheses.json、paper_route_matrix.json、article_mcp_log.json、raw/

提交：`docs: freeze KV optimization research and baseline`（根仓库，本阶段无 llama.cpp 改动）
