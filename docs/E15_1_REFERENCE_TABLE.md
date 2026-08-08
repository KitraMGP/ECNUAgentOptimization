# E15.1：调研来源引用表（Reference Table）

- 生成时间：2026-08-08（v3 final：计数表述修正 + 工具数据专项调研来源）
- 用途：E15 三条技术线（分支内存共享 / Prompt 压缩 / 上下文优化 / 工具数据外置）的调研来源落表；供 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` 引用。
- 来源分类约定：
  - **FULL_TEXT_READ** = 已读全文（本仓库 `papers/kv-cache/notes/*.md` 有笔记 + manifest SHA256）；
  - **PARTIAL_TEXT_READ / SECTIONS_READ** = 本次经 article-mcp `get_article_details` 读取部分章节（须注明章节名）；**abstract+intro+conclusion 不得标 FULL_TEXT_READ**；
  - **ABSTRACT_ONLY** = 仅经 article-mcp `search_literature` 或 arXiv 官方页面获得摘要（不得引用其具体数字）；
  - **OFFICIAL_DOCS** = 框架官方文档/仓库（web_fetch 核实）。
- article-mcp 唯一 E15 权威日志：`docs/E15_ARTICLE_MCP_LOG.json`（seq 1–8：R-KV / PartPrompt / Lost in the Middle / LLMLingua / CoALA / LongMemEval / MemGPT）；E6 旧日志 `benchmark/results/kv_optimization/article_mcp_log.json` 仅作历史对照。

---

## 1. 论文（article-mcp 检索/读取 + 仓库既有）

### 1.0 E6 manifest 计数澄清（reviewer 修正）

`papers/kv-cache/manifest.json`（E6）的 **counts 字段**记录：`candidates_collected=21, pdf_downloaded=21, fulltext_read=19, deep_read=8, abstract_only=2`；而 **papers 数组**实际逐项状态为：`FULL_TEXT_READ × 20 + ABSTRACT_ONLY × 1`（vpid）。数组与 counts 字段之间存在**既有差异**（E6 时代的统计口径与逐项标注不完全一致）。本表按**逐项数组实际状态**为准：

- E6 21 条：20 FULL_TEXT_READ（pagedattention … lminfinite）+ 1 ABSTRACT_ONLY（**vpid**，candidate=**NOT_APPLICABLE**（external system），非本项目候选）。
- **E15 新增文献不混入 E6 统计**：R-KV（PARTIAL_TEXT_READ）、PartPrompt（ABSTRACT_ONLY）、工具数据专项 5 篇（全部 ABSTRACT_ONLY）——均为 E15 自己的读取，与 E6 manifest 计数无关。

### 1.1 工具数据专项调研（ToolPayloadStore 依据，全部 ABSTRACT/PARTIAL，无 FULL）

| # | 论文 | 出处/ID | 读取状态 | 摘要级结论 | 在本方案中的用法 |
|---|---|---|---|---|---|
| T1 | **Lost in the Middle: How Language Models Use Long Contexts**（Liu N F et al.） | arXiv 2307.03172；**venue 标注：VENUE_UNVERIFIED**（arXiv 页标注 TACL 2023，但本仓库无一级来源（ACM DL/官方页）确认；不标为正式 venue） | **ABSTRACT_ONLY**（article-mcp 未命中，经 arXiv 官方页面核实；log seq 4） | 长上下文中相关信息位于中段时性能显著退化；开头/结尾最优 | **工具数据外置的位置依据**：prompt 中只放 payload 摘要/引用，避免大 payload 挤占中间位置 |
| T2 | **LLMLingua: Compressing Prompts for Accelerated Inference of LLMs**（Jiang H et al.） | arXiv 2310.05736；EMNLP 2023 | **ABSTRACT_ONLY**（log seq 5） | coarse-to-fine prompt 压缩 + budget controller，最高 20x 压缩少量性能损失 | **B2 摘要 oracle 参考**：token 级压缩思想（本项目只做应用层，不实现其 token 剪枝内核） |
| T3 | **Cognitive Architectures for Language Agents（CoALA）**（Sumers T R et al.） | arXiv 2309.02427；TMLR 2024 | **ABSTRACT_ONLY**（log seq 6） | 语言 agent 模块化记忆（working/episodic/semantic）+ 结构化动作空间 + 决策循环 | **C 线记忆分层的概念参考**：区分工作记忆（上下文内）与长期记忆（外置），支撑 ToolPayloadStore 外置设计 |
| T4 | **LongMemEval**（Wu D et al.） | arXiv 2410.10813；ICLR 2025 | **ABSTRACT_ONLY**（log seq 7） | 五类长期记忆能力评测（信息抽取/多会话推理/时间推理/知识更新/abstention）；500 问、长上下文助手掉 30% | **上下文策略验收的评测维度参考**：多会话推理/abstention 可纳入 C 线 needle 类测试设计 |
| T5 | **MemGPT: Towards LLMs as Operating Systems**（Packer C et al.） | arXiv 2310.08560 | **ABSTRACT_ONLY**（log seq 8） | virtual context management：快慢内存分层、数据搬移、interrupts 控制流 | **ToolPayloadStore 的直接概念来源**：完整 payload 外置到"慢存储"，prompt 只留引用（deterministic projection + hash） |

### 1.2 三条技术线既有论文

| # | 论文 | 出处/ID | 读取状态 | 论文结论（引用原文数据） | 在本方案中的用法 |
|---|---|---|---|---|---|
| 1 | **R-KV: Redundancy-aware KV Cache Compression for Reasoning Models**（Cai Z, Xiao W, et al.） | NeurIPS 2025；PMCID **PMC13183371**（PMID 42158289）；https://pmc.ncbi.nlm.nih.gov/articles/PMC13183371/ | **PARTIAL_TEXT_READ / SECTIONS_READ**（article-mcp 实读 abstract+introduction+conclusion；log seq 3） | 摘要：10–34% KV 保留 ≈ full-KV 性能、16% KV 达 105% full-KV、90% 内存节省、6.6× throughput；引言：reasoning 模型长 CoT >50% token 冗余、attention 简单剪枝对重复段失效；结论：decoding-time 联合 importance+redundancy 打分 | **参考不直接采用**：R-KV 是 **KV 层有损压缩**，面向 CoT 超长输出的 reasoning 模型；本项目 4B 为 Qwen3.5 hybrid（recurrent），E6/E12 已证 hybrid 上 KV 层有损无收益/不安全。仅作"冗余 token 识别"思想参考（B2 离线 oracle 可选对照） |
| 2 | **PartPrompt: Parse Trees Guided LLM Prompt Compression**（Mao W, Hou C, et al.） | IEEE TPAMI 2026；DOI 10.1109/TPAMI.2025.3609956；https://pubmed.ncbi.nlm.nih.gov/40953431/ | **ABSTRACT_ONLY**（article-mcp 检索；log seq 2） | 摘要：parse-tree + 信息熵 + 全局树剪枝，selective 压缩 SOTA（非生成式、避免 hallucination） | **B1/B2 prompt 压缩参考**：selective 保留/剪枝思想可用于应用层结构化压缩；因 ABSTRACT_ONLY，**不引用其具体数字**，不承诺复现 |
| 3 | **SnapKV: LLM Knows What You are Looking for Before Generation** | arXiv 2404.14469；**NeurIPS 2024**（poster 93531，正文为 preprint 版；E7 复核确认） | **FULL_TEXT_READ**（仓库 `papers/kv-cache/notes/snapkv.md`） | prompt 侧按 attention 选择重要 KV（observation window），推理时压缩 prompt KV，加速而无质量损失 | **B2 离线 oracle 对照候选**：prompt KV 选择与"历史压缩"语义接近；但仍属 KV 层有损，**不写入 4B hybrid**，仅用于 attention-only 或应用层等价验证 |
| 4 | **RadixAttention（SGLang）** | arXiv 2312.07104；**NeurIPS 2024**（ACM DL 10.5555/3737916.3739916；poster 94872）——**venue 按 E8.1 一级来源纠正：此前 E7.2 曾误标 SOSP 2024，无一级来源支持** | **FULL_TEXT_READ**（notes/radixattention_sglang.md） | radix tree（CPU）+ LRU 淘汰（ref=0 叶子优先）+ cache-aware scheduling；命中率 50–99%、overhead <0.3%（论文结论） | **仅作思想参考**：E15.0 §2.4 已按 E9.3 评估 **DEFER/REJECT（不实现 radix 索引）** |
| 5 | **PagedAttention** | arXiv 2309.06180；SOSP 2023 | **FULL_TEXT_READ**（notes/pagedattention.md；`REJECT_INCOMPATIBLE_ARCHITECTURE`） | block 级管理 + refcount + COW（copy-on-write 仅分页架构） | **不实现完整方案**（需 kernel 重写）；metadata sharing 不是 paged COW（E15.0 §2.2） |
| 6 | E6 其余 20 篇（vattention/h2o/kivi/kvquant/quest/chunkattention/infinigen/cacheblend/pyramidkv/duoattention/palu/scissorhands/memserve/leankv_diffkv/streamingllm/tova/lminfinite + pagedattention/radixattention/snapkv 已列） | 见 `papers/kv-cache/manifest.json`（数组 21 条：20 FULL + 1 ABSTRACT） | **FULL_TEXT_READ**（vpid 除外，vpid=ABSTRACT_ONLY 且 candidate=**NOT_APPLICABLE**） | 各论文结论记录于 `papers/kv-cache/notes/*.md` | E6 已判定 REJECT/DEFERRED 的（vAttention=硬件不支持、H2O/TOVA 等=per-head 淘汰与 cell 架构不符）E15 不复活 |

**article-mcp 状态**：本次可用（`search_literature` / `get_article_details` 均成功）；Europe PMC 对 arXiv 系论文覆盖不足（Lost in the Middle 等 5 篇均未命中，改用 arXiv 官方页面核实）；详见 `docs/E15_ARTICLE_MCP_LOG.json`。

---

## 2. Agent / 推理框架官方实现链接（web_fetch 核实）

| 框架 | 版本（核实时） | 官方来源链接 | 核心机制（官方文档记载） | 与本项目适配性 |
|---|---|---|---|---|
| **LangGraph** | v1.2.10（2026-07-28） | 文档：https://docs.langchain.com/oss/python/langgraph/persistence ；PyPI：https://pypi.org/pypi/langgraph/json ；仓库：https://github.com/langchain-ai/langgraph | StateGraph / Node / Edge / Checkpointer；super-step 增量 checkpoint；`trim_messages` / `RemoveMessage` / 手写 `summarize_conversation` 节点（无内置自动压缩）；**无 COW/内存级共享**（状态拷贝 + reducer 合并） | **C 线直接依据**：trim/remove/summarize 三件套即本方案"历史保留策略"的 API 形态；应用层状态管理，不触碰 KV |
| **SGLang** | v0.5.17（2026-08） | radix cache 文档：https://docs.sglang.ai/docs/advanced_features/session_radix_cache ；实现：https://raw.githubusercontent.com/sgl-project/sglang/main/python/sglang/srt/mem_cache/radix_cache.py ；论文：https://arxiv.org/abs/2312.07104 | **来源状态：OFFICIAL_DOCS（radix cache 文档 + 源码文件）＋论文 FULL_TEXT_READ（E6 manifest）**；RadixAttention 前缀树，radix cache **默认启用**，`--disable-radix-cache` / `--radix-eviction-policy` / session radix cache；命中段 `_split_node` 拆节点（克隆索引不复制 KV） | **对照参考**：与 llama.cpp `seq_cp` 元数据共享思想一致（C1 受其启发，E8.1 确认）；本项目按 E9.3 不实现 radix 树 |
| **vLLM** | v0.26.0（2026-07） | prefix caching 设计文档：https://docs.vllm.ai/en/latest/design/prefix_caching/ ；PyPI：https://pypi.org/project/vllm/ | **来源状态：OFFICIAL_DOCS（prefix caching 设计文档 + PyPI）**；hash-based 块级 automatic prefix caching（`block_hash = hash(parent_hash, tokens, extra)`，仅全块缓存）；`ref_cnt` + touch 防淘汰 | **对照参考**：block-table/分页/COW 依赖分页架构，与 llama.cpp 连续 KV buffer **不适用**（不改 kernel） |
| **LlamaIndex** | stable（docs 当前版） | 文档：https://docs.llamaindex.ai/en/stable/ ；仓库：https://github.com/run-llama/llama_index | agents / workflows（事件驱动）/ chat engines / memory 模块 / RAG pipelines；无内置 KV 层优化 | **工具数据外置的框架参考**：其 agents + memory 分层可用于 benchmark 层 ToolPayloadStore 的编排形态 |
| **AutoGen** | stable（AgentChat/Core） | 文档：https://microsoft.github.io/autogen/stable/ ；仓库：https://github.com/microsoft/autogen | AgentChat（对话式多 agent）/ Core（事件驱动、可扩展、分布式运行时）/ Extensions（MCP、代码执行器等） | **对照参考**：事件驱动 agent 状态管理思路可借鉴；与本项目 KV 优化无直接接口 |

---

## 3. 论文结论 vs 工程推断 区分

**论文结论（已核验来源，未在本仓库复现）**：
- R-KV：10% KV ≈100% 性能、90% 内存节省、6.6× throughput、13× batch / 9× 长序列加速（PARTIAL_TEXT_READ，abstract+intro+conclusion）。
- PartPrompt：selective prompt 压缩 SOTA、避免生成式 hallucination（ABSTRACT_ONLY，不引用具体数字）。
- SnapKV：prompt KV 选择可无损加速（仓库笔记全文）。
- RadixAttention：命中率 50–99%、overhead <0.3%（NeurIPS 2024，全文）。
- Lost in the Middle：中段信息性能显著退化（ABSTRACT_ONLY，arXiv 官方页；**venue VENUE_UNVERIFIED**）。
- LLMLingua：最高 20x 压缩少量性能损失（ABSTRACT_ONLY）。
- LongMemEval：长上下文助手长期记忆掉 30%（ABSTRACT_ONLY）。
- MemGPT：虚拟上下文管理（快慢内存分层）有效（ABSTRACT_ONLY）。

**工程推断（本项目实现者的推演，未验证）**：
- "seq bitset 已等价引用计数、现有覆盖/clear 保护已覆盖共享源"——基于 `llama-kv-cells.h` bitset 与 `server-context.cpp:1951-1958/2103-2108` 的代码级核验，**由 E15.1 不变量测试验证**。
- "C1 线性扫描无瓶颈（N≤64 <1ms）故 radix 索引无必要"——**E9.3 已有实测数据**（32 source：on-off Δ = -0.29ms），非纯推断。
- "ToolPayloadStore（确定性 projection + hash + thread-scoped 外置）可无损保留工具 JSON/system/secret 语义"——受 MemGPT/CoALA/Lost in the Middle 摘要级思想启发，**未验证**，必须由 E15.2 验收测试证明。
- "LangGraph 式 trim/remove/summarize 可在 server 前置/benchmark 层无损落地"——基于官方文档 API 形态的推演。

> 本表所有"已验证事实"（本项目代码行为）见 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` §3 与 E15.0 的代码基线复核清单（llama.cpp afbf375c6）。已存在实现（`benchmark/framework/context_policy.py`、`benchmark/runner/e15_branch_concurrent.py` 及其测试）登记于 E15.0 §1.3。
