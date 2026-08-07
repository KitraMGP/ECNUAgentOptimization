你将在当前仓库中完成一次端到端的 KV Cache 优化研究、实现与验收任务。

当前日期为 2026-08-07。项目目标不是简单修复 KV Cache 准入、淘汰或并发错误，
而是在保持正确性和质量门禁的前提下，对 llama.cpp 的 KV Cache 实现产生可测量、
可复现、可提交的容量、内存、复用或计算效率收益。

你必须自主完成代码审计、论文研究、基线冻结、候选筛选、实现、测试、基准、报告和
分阶段提交。不得只给方案、TODO、伪代码或研究报告。

一、硬性目标

最终至少交付：

1. 使用 article-mcp 检索、下载并阅读近期权威 KV Cache 论文。
2. 审计当前 llama.cpp KV allocator、unified KV、prefix reuse、slot lifecycle、
   quantization 和 attention kernel。
3. 建立未优化基线和可复现 workload。
4. 至少选择两个候选路线进行可行性验证。
5. 至少一个候选必须修改 llama.cpp 的实际 KV Cache 代码。
6. 至少一个无损优化候选必须完成 paired benchmark。
7. 至少建立一个量化、压缩或异构存储候选的可运行原型或离线 oracle。
8. 证明收益不是通过增加显存、降低并发、减少输出或改变输入制造的。
9. 运行正确性、容量、质量、耐久和资源回收门禁。
10. 输出原始数据、复现命令、论文证据、失败候选和最终判定。

若没有任何实现达到预注册收益门槛，不得宣称完成 KV Cache 优化。

二、执行约束

- 开始前读取仓库结构、README、构建脚本、测试框架和已有实验报告。
- 使用 rg 搜索代码；遵循仓库已有风格，不进行无关重构。
- 不覆盖、回滚或清理用户已有修改。
- 所有人工代码修改使用 apply_patch。
- GPU、llama-server 和显存实验必须由主代理通过独占锁串行执行。
- CPU 代码审计、论文阅读和离线统计可以并行。
- 同一时间最多运行一个 llama-server。
- 检测已有 GPU/server 进程；不能确认归属时停止实验并记录 blocker。
- 每个实验必须记录 commit、配置、模型 hash、GPU、驱动、命令、环境变量和退出码。
- 正式指标不得只依赖 wall-time；wall-time 只能作为辅助指标。
- 每个阶段独立提交；不得用一个最终提交掩盖中间过程。
- 不得为了通过测试删除失败样本、放宽阈值或静默截断。
- 不得伪造 article-mcp 调用、论文下载、测试结果或性能数据。

三、论文检索与下载

优先调用 article-mcp。先枚举实际可用的 MCP 工具和参数，不得猜测接口名称。
如果 article-mcp 未挂载或调用失败：

1. 保存准确错误信息；
2. 在报告中标记 ARTICLE_MCP_UNAVAILABLE；
3. 改用会议官网、出版社、作者主页或 arXiv 等一手来源；
4. 不得把搜索摘要标记为“已阅读全文”。

检索范围以 2023-01-01 至 2026-08-07 为主，必要时纳入更早的奠基论文。
优先级：

- SOSP、OSDI、NSDI、USENIX ATC、FAST、EuroSys、ASPLOS；
- NeurIPS、ICML、ICLR、MLSys；
- ACL、EMNLP、NAACL 及其 Findings；
- 正式版本优先于 arXiv；只有预印本时必须明确标记。

重点检索：

- paged/block-based KV allocation；
- virtual-memory-backed KV allocation；
- exact prefix sharing、radix cache、copy-on-write；
- cache-aware scheduling 和 reuse-aware eviction；
- KV quantization、mixed precision、per-layer/per-head precision；
- token/head/layer eviction；
- sink/recent/heavy-hitter retention；
- query-aware sparse KV access；
- CPU/GPU/SSD KV offload 和 prefetch；
- low-rank KV；
- RAG chunk/non-prefix KV reuse；
- KV compression 对正确性、幻觉、安全和工具调用的影响。

至少核验以下研究方向及其正式出处，不要盲信标题、年份或既有报告：
PagedAttention、vAttention、ChunkAttention/RadixAttention、KIVI、KVQuant、
H2O、SnapKV、Quest、InfiniGen、CacheBlend，以及 2025-2026 年新的
adaptive quantization、layer/head budget、offload 和安全评测工作。

为每篇纳入论文保存：

- 标题、作者、年份、venue、DOI/arXiv ID、正式来源；
- PDF 或可访问正文；
- SHA256；
- BibTeX；
- 下载日期；
- 阅读状态：FULL_TEXT_READ、PARTIAL_TEXT_READ 或 ABSTRACT_ONLY；
- 方法、关键数据结构、算法伪代码、实验配置、消融、限制；
- 是否需要训练、模型修改、CUDA kernel 或离线校准；
- 与当前 llama.cpp 的对应代码位置；
- 可实现候选和不可实现原因。

生成：

- papers/kv-cache/manifest.json
- papers/kv-cache/references.bib
- papers/kv-cache/notes/
- docs/KV_CACHE_LITERATURE_REVIEW.md
- benchmark/results/kv_optimization/paper_route_matrix.json

不得大段复制论文正文。使用摘要、结构化笔记和短引用，并保留来源定位。

四、代码审计

定位并记录：

- KV cell/block 的结构、分配、查找、移动、释放和清空；
- unified KV pool 的容量计算和固定预分配；
- slot、sequence、session、generation 的所有权关系；
- active、idle、protected、cancelled 状态；
- prefix/cache reuse 的 key、匹配粒度和失效规则；
- purge、defrag、LRU、victim selection；
- sequence copy/remove/keep 和 copy-on-write 能力；
- RoPE position、KV type、adapter/model identity；
- CPU/GPU buffer 分配和实际 KV bytes；
- attention kernel 是否要求连续物理 KV；
- F16、Q8、Q4 等已有 KV 类型及量化粒度；
- state save/load、retry、resume、cancel 和 server shutdown；
- 当前指标缺口及需要添加的 instrumentation。

先证明当前代码不已经具备候选方案的等价能力。若已有部分实现，应补足缺口并测量，
不得仅重命名或重新包装已有行为。

生成 docs/KV_CACHE_CURRENT_ARCHITECTURE.md。

五、候选路线

按以下顺序评估，不允许一开始只选择高风险有损压缩。

P0 无损系统优化：

A. 固定大小 paged/block pool：
- 测试 8、16、32、64 tokens/block；
- logical-to-physical block table；
- 按需 allocate/free；
- refcount、generation、prefix sharing 和 partial-block COW；
- 统计内部碎片、共享率、COW 和最大并发。

B. exact prefix block sharing：
- 只允许 exact token ID prefix；
- cache identity 至少包含 model、adapter、RoPE、KV type 和关键推理配置；
- 防止跨 session、generation、tenant 或模型错误复用；
- 最后一个不完整块必须复制或 COW。

C. reuse/cost-aware idle eviction：
- LRU 作为 control；
- 比较 LFU、offset-aware、reuse-probability 和 recompute-cost score；
- active/protected KV 永远不能成为 victim；
- 无可靠历史数据时回退 LRU。

D. virtual-memory-backed allocation：
- 评估连续虚拟地址加按需物理映射是否能降低 attention kernel 改造成本；
- 若平台或后端不支持，记录具体 API、驱动或架构 blocker。

P1 数值压缩：

E. per-layer/per-KV mixed precision：
- 分离 K type 和 V type；
- 比较 F16、Q8/Q8、Q4/Q8、Q4/Q4 和 layer-adaptive；
- recent residual window 可保留高精度；
- 正式容量单位必须为 bytes。

F. quantization + retention 联合预算：
- sink、recent、heavy-hitter、historical quantized 四类预算；
- 与“全部 Q4”和“更少 token、全部 Q8”做同字节预算对照。

P2 有损结构优化：

G. per-layer/per-head/token budget；
H. Key geometry 或 attention score 驱动的 eviction；
I. prefill/decode 分阶段压缩；
J. query-aware page selection。

P3 异构和研究路线：

K. GPU + pinned CPU KV offload/prefetch；
L. low-rank historical KV；
M. RAG chunk 或 non-prefix KV reuse。

P2/P3 必须先实现离线 oracle 或最小原型。只有质量和收益上界成立后，才能修改生产路径。

六、假设冻结与路线选择

建立 benchmark/results/kv_optimization/optimization_hypotheses.json。

每个候选必须包含：

- paper_evidence；
- current_code_locations；
- bottleneck；
- proposed_change；
- invariants；
- implementation_steps；
- expected_metrics；
- expected_gain_threshold；
- quality_risks；
- performance_risks；
- minimal_reproducer；
- rollback_condition；
- selected/rejected/deferred 状态及理由。

至少注册两个可实现候选。主实现优先选择 P0；第二候选优先选择 P1。
若首个候选不可行，记录证据并转入第二候选，不得停止在调研阶段。

七、基线与测试矩阵

优化前冻结 commit、模型、模型 hash、ctx、KV 类型、GPU、sampling、seed、
输入、输出预算、并发和 capacity。

至少记录：

- capacity_cells、used_cells、free_cells；
- allocated/active/idle/shared/stranded cells；
- physical blocks、logical blocks、refcount、COW；
- internal fragmentation；
- prefix/cache hit cells；
- recompute tokens；
- purge 次数、victim 和释放量；
- actual CPU/GPU KV bytes；
- prefill/decode tokens；
- completed/rejected/failed sessions；
- 最大可完成并发；
- 最终空闲基线和泄漏指标。

paired workload 至少包含：

- 并发 1/2/4/6/8；
- 短、长和长短混合 session；
- exact common prefix、partial prefix、不同 prefix；
- idle reuse、LRU pressure、active pressure；
- cancel/retry/resume；
- generation 和 slot reuse；
- 超容量和 no-victim；
- 12 个以上 churn 周期；
- 清空后的资源回收；
- 长代码、长摘要、needle retrieval；
- 多指令和工具调用 JSON。

优化前后必须使用相同输入、seed、token budget、capacity 和配置。
每项保留原始值，不能只报告百分比。

八、正式验收门槛

正确性：

- active/protected 请求不被淘汰；
- 无跨 session/generation/slot/model/adapter 错误复用；
- 无静默截断、错误 2xx、crash、hang、deadlock；
- capacity error 与 sequence ctx error 可区分；
- trace-off lifecycle event 为 0；
- full-KV 无损候选 greedy 输出必须完全一致；
- 清空后资源返回预注册基线。

无损收益必须至少满足一项预注册指标，并且其他核心指标不明显退化：

- 相同 KV bytes 下最大可完成 session 数提高；
- internal fragmentation 或 stranded cells 明显下降；
- exact prefix 场景 recompute tokens 明显下降；
- cache-hit physical cells 增加；
- 相同逻辑状态下实际 KV bytes 下降；
- purge 后释放空间可立即复用。

有损候选必须额外通过：

- perplexity 或正式任务质量阈值；
- LongBench/RULER/needle 的位置分桶；
- long-form factual consistency；
- code generation；
- tool-call JSON schema；
- system prompt retention；
- jailbreak/context conflict；
- full-KV greedy token divergence；
- 不得用平均分掩盖安全或工具调用失败。

性能收益不得来自：

- 增加显存或 capacity；
- 减小 ctx、输入、输出或并发；
- 更换模型或降低质量配置；
- 删除失败请求；
- 减少测试周期；
- 只展示最佳运行；
- 仅依赖 wall-time 抖动。

九、分阶段交付

E6.0 文献、架构审计和实验冻结
提交：docs: freeze KV optimization research and baseline

E6.1 基线、instrumentation 和最小复现
提交：test: establish KV cache optimization baseline

E6.2 第一个 P0 实际实现
提交：perf: optimize unified KV cache utilization

E6.3 第二候选或 P1 原型
提交：perf: prototype adaptive KV cache compression

E6.4 paired capacity、reuse 和 quality benchmark
提交：test: measure KV cache optimization gains

E6.5 至少 12 周期 endurance；资源允许时运行 30 周期
提交：test: validate optimized KV cache endurance

E6.6 最终报告
提交：docs: report KV cache optimization results

每阶段提交前运行对应测试。不要提交模型、巨型日志、构建目录或无关生成文件。

十、最终产物

生成：

- docs/E6_0_KV_RESEARCH_AND_BASELINE_REPORT.md
- docs/E6_1_KV_INSTRUMENTATION_REPORT.md
- docs/E6_2_LOSSLESS_KV_OPTIMIZATION_REPORT.md
- docs/E6_3_KV_COMPRESSION_PROTOTYPE_REPORT.md
- docs/E6_4_KV_OPTIMIZATION_GAIN_REPORT.md
- docs/E6_5_KV_OPTIMIZATION_ENDURANCE_REPORT.md
- docs/E6_KV_OPTIMIZATION_FINAL_REPORT.md
- benchmark/results/kv_optimization/raw/
- benchmark/results/kv_optimization/summary.json
- benchmark/results/kv_optimization/reproduction_commands.sh

最终报告必须列出：

- article-mcp 调用及下载记录；
- FULL_TEXT_READ/PARTIAL/ABSTRACT_ONLY 状态；
- 论文到代码候选的映射；
- 所有修改文件和 commit；
- 基线与优化后的原始数据；
- 正式门槛和逐项 PASS/HOLD/REJECT；
- 未采用路线和具体原因；
- 已知限制、回退方式和下一步；
- 工作树状态及未提交文件；
- 所有复现命令。

十一、最终状态

只能选择一个：

PASS_KV_CACHE_OPTIMIZATION
- 有实际 llama.cpp KV 代码优化；
- 正确性全部通过；
- 至少一个预注册容量、内存、复用或重算指标达到门槛；
- paired benchmark 和原始数据完整。

PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
- 实现和正确性通过，但没有达到正式收益门槛。

PARTIAL_KV_RESEARCH_NO_IMPLEMENTATION
- 完成研究或原型，但没有可验收的 llama.cpp 实际优化。

HOLD_KV_OPTIMIZATION_INCOMPLETE
- 环境、GPU、工具或时间阻塞导致关键矩阵未完成。

REJECT_KV_CACHE_OPTIMIZATION
- 出现错误 KV 复用、active 误淘汰、静默截断、质量红线失败、
  资源泄漏、错误成功响应或不可恢复回归。

立即开始执行。先检查仓库、git 状态、article-mcp 可用性、GPU/server 状态和已有
KV 实现，再冻结研究与基线。除非遇到无法自行解决的外部阻塞，不要停在计划阶段。

是。上一版已经覆盖了研究、实现和交付流程，但仍存在“明显下降”“质量阈值”“资源允许时”等模糊表述。建议在提示词末尾追加下面这份**强制执行与验收附录**。它将实验数量、统计方式、收益阈值、质量红线、阻塞条件和最终状态全部固定，避免智能体自行降低标准或只完成调研。


十二、强制执行规范与歧义消除

本节优先级高于前文。若前文与本节冲突，以本节为准。

12.1 任务性质

这是“研究 + 实际代码实现 + 可重复实验 + 验收”任务，不是以下任何一种任务：

- 仅进行论文调研；
- 仅提出架构设计；
- 仅生成伪代码；
- 仅增加监控指标；
- 仅修复已有正确性问题；
- 仅执行一次性能测试；
- 仅修改 benchmark；
- 仅实现不进入真实 KV 路径的演示代码；
- 仅报告理论内存压缩率；
- 仅把现有功能重命名为新的论文方法。

除非出现第 12.15 节定义的外部阻塞，否则必须持续执行到：

1. 至少一个真实 KV Cache 实现候选已经完成；
2. 对该候选完成基线与候选 paired benchmark；
3. 完成正确性和质量验收；
4. 生成原始数据和最终报告；
5. 给出唯一最终状态。

不得在完成调研或制定计划后停止。

12.2 优先级

发生目标冲突时，按以下顺序决策：

1. KV 内容和请求隔离正确性；
2. active 请求和生命周期安全；
3. 无损候选的输出等价性；
4. 有损候选的安全、工具调用和指令遵循；
5. 资源回收和长期稳定性；
6. 实际 KV bytes、容量或重算收益；
7. 吞吐和延迟；
8. 实现复杂度；
9. 论文方法复现完整度。

任何低优先级收益都不能补偿高优先级失败。

12.3 术语定义

必须按以下定义使用术语：

- physical KV bytes：
  由 KV Cache 实际分配器或后端分配的 CPU/GPU 内存字节数。
  不得用理论 tensor 大小代替实际分配量。

- logical KV tokens：
  所有 sequence 从逻辑上引用的 KV token 数之和。
  共享 prefix 被多个 sequence 引用时需要重复计数。

- unique physical KV tokens：
  实际只存储一次的 KV token 数。
  共享 prefix 只能计数一次。

- internal fragmentation：
  已分配 block 中没有保存有效 KV、也不能被其他请求使用的空间。

- stranded capacity：
  理论空闲但由于连续性、所有权、状态或 allocator 限制而不能满足新请求的空间。

- exact prefix reuse：
  在模型身份、adapter、token ID、position、RoPE、KV 类型和其他影响 KV 的配置均
  相同的条件下，直接共享已有 KV，不重新计算。

- lossless optimization：
  在相同模型、输入、sampling、seed、backend 和浮点配置下，输出 token ID 序列与
  基线完全相同。

- lossy optimization：
  可能因量化、token/head/layer 淘汰、稀疏访问或低秩表示改变输出的优化。

- paired benchmark：
  基线和候选使用同一个 workload manifest，只允许改变被测优化开关及其直接参数。

- completed session：
  请求返回协议层成功状态，生成达到预期停止条件，输出未静默截断，且没有内部错误。

- active/protected：
  正在 prefill、decode、恢复、复制或被显式标记为不可淘汰的 KV。

12.4 article-mcp 验收

开始时必须列举当前会话实际暴露的 MCP server 和 tool。不得根据提示词猜测
article-mcp 的函数名。

如果 article-mcp 可用，必须通过它完成：

1. 至少 4 组检索查询；
2. 至少收集 15 篇候选论文；
3. 至少下载 10 篇可合法获取的全文；
4. 至少完整阅读 8 篇；
5. 至少精读 5 篇与最终候选直接相关的论文；
6. 为每篇论文生成结构化笔记；
7. 记录每次检索、下载和正文读取的 MCP 调用结果。

“完整阅读”至少包括：

- abstract；
- introduction；
- method/design；
- implementation；
- evaluation；
- ablation；
- limitations 或 failure cases；
- conclusion。

“精读”还必须提取：

- 数据结构；
- 核心算法；
- 默认参数；
- 参数敏感度；
- 实验硬件和模型；
- 基线；
- 论文报告的原始指标；
- 方法不适用的 workload；
- 复现所需 kernel 或训练条件；
- 对 llama.cpp 的实现映射。

如果 article-mcp 不可用：

- 将探测命令、错误类型和错误文本写入报告；
- 设置 `article_mcp_status=UNAVAILABLE`；
- 使用论文正式页面和 PDF 继续任务；
- 不得因此停止；
- 最终状态不能仅因 article-mcp 不可用而变为 HOLD。

论文数量不足时，允许的最低标准为：

- 12 篇候选；
- 8 篇全文；
- 6 篇完整阅读；
- 4 篇精读。

低于该最低标准时，研究阶段不得标记完成。

12.5 权威来源判定

论文来源按以下优先级处理：

1. 正式会议或期刊页面；
2. ACL Anthology、USENIX、ACM DL、IEEE Xplore、PMLR、
   NeurIPS Proceedings、OpenReview 正式录用页；
3. 作者公开的正式版本；
4. arXiv；
5. 项目博客、公司博客和 GitHub，仅可作为实现补充材料；
6. 搜索摘要，不得作为论文结论证据。

同一工作存在多个版本时：

- 优先使用正式发表版本；
- 记录 arXiv 和正式版本的对应关系；
- 不重复计算论文数量；
- 若版本结论不同，明确记录差异。

不得把截至 2026-08-07 尚未正式发表的论文写成已正式录用。
无法确认 venue 时标记为 `PREPRINT` 或 `VENUE_UNVERIFIED`。

12.6 实现路线选择规则

完成论文和代码审计后，为每个候选计算以下评分：

```text
candidate_score =
    0.30 * expected_measurable_gain
  + 0.25 * compatibility_with_current_architecture
  + 0.20 * implementation_feasibility
  + 0.15 * correctness_confidence
  + 0.10 * paper_evidence_strength
```

每项使用 0 至 5 分，并在报告中解释评分。

主候选必须满足：

- 属于 P0 无损路线；
- 总分不低于 3.5；
- 没有已知 correctness blocker；
- 可以作用于真实推理路径；
- 可以在现有环境中测量。

第二候选优先选择 P1。若 P1 总分均低于 3.0，可选择另一个 P0。

不允许以“实现复杂”为唯一理由跳过所有 P0。
不允许在没有完成至少一个 P0 实现之前，把 P2/P3 作为唯一交付成果。

12.7 环境冻结

第一次正式构建或 benchmark 前，创建：

`benchmark/results/kv_optimization/environment.json`

必须包含：

- UTC 和本地时间；
- git commit；
- git branch；
- `git status --short`；
- OS；
- CPU；
- RAM；
- GPU 型号和数量；
- GPU driver；
- CUDA、ROCm、Metal 或其他 backend 版本；
- compiler 和版本；
- CMake/build flags；
- llama.cpp build type；
- 模型路径；
- 模型文件大小；
- 模型 SHA256；
- tokenizer/config identity；
- KV 类型；
- context size；
- batch 和 micro-batch；
- flash attention 状态；
- offload layer 数；
- sampling 参数；
- seed；
- 环境变量；
- 已存在的相关进程；
- 可用磁盘空间。

基线冻结后，除被测优化参数外不得修改上述条件。
必须修改时，基线和候选都重新运行，并创建新的 experiment ID。

12.8 Workload Manifest

在运行正式实验前创建不可变的：

`benchmark/results/kv_optimization/workload_manifest.json`

每个 workload 必须包含：

- workload_id；
- category；
- 输入文件或输入 SHA256；
- tokenized input length；
- expected max output tokens；
- concurrency；
- number of sessions；
- prefix sharing pattern；
- cancel/retry/resume pattern；
- seed；
- warmup count；
- measured repetition count；
- timeout；
- expected completion count；
- quality evaluator；
- 是否属于 correctness gate；
- 是否属于 performance gate。

最低 workload 集合：

- `W01_single_short`
- `W02_single_long`
- `W03_concurrency_2`
- `W04_concurrency_4`
- `W05_concurrency_6`
- `W06_concurrency_8`
- `W07_exact_prefix_4_sessions`
- `W08_partial_prefix`
- `W09_no_shared_prefix`
- `W10_idle_reuse`
- `W11_active_pressure`
- `W12_cancel_and_slot_reuse`
- `W13_retry_resume`
- `W14_over_capacity`
- `W15_no_victim`
- `W16_churn_12_cycles`
- `W17_long_code_generation`
- `W18_long_summary`
- `W19_needle_early`
- `W20_needle_middle`
- `W21_needle_late`
- `W22_multi_instruction`
- `W23_tool_call_json`
- `W24_system_prompt_retention`

如果硬件无法运行某个并发级别，不得删除 workload。
应记录基线和候选在该级别是否完成，以及准确失败原因。

12.9 重复次数和统计规则

正确性测试：

- 每个确定性 workload 至少运行 1 次；
- 涉及取消、竞争、并发或生命周期时至少运行 3 次；
- 任何一次失败都视为该 gate 失败。

性能和容量测试：

- 预热至少 2 次；
- 正式测量至少 5 次；
- 报告全部原始值；
- 中位数作为主值；
- 同时报告 min、max、mean、median 和 p95；
- 不得删除异常值；
- 若变异系数超过 5%，增加到 10 次；
- 10 次后仍超过 5%，标记 `NOISY`，不得仅据此宣称性能通过。

容量极限：

- 使用固定步长或二分搜索；
- 基线和候选使用相同搜索算法；
- 边界点至少重复 3 次；
- 只有 3 次全部成功才能算作可支持容量。

12.10 无损候选正确性门槛

无损候选必须全部满足：

- 相同输入得到完全相同的 output token ID；
- 相同停止原因；
- 相同 prompt token 数；
- 相同 generated token 数；
- 不出现 NaN/Inf；
- 不出现跨请求 KV 污染；
- 不出现跨 generation 错误复用；
- 不出现 active/protected victim；
- 不出现 refcount 下溢、悬空引用或 double free；
- server 退出后 KV 资源回到基线；
- AddressSanitizer 或项目等价内存检查中无新增错误；
- 所有相关单元和集成测试通过。

浮点日志、时间戳和物理 block ID 可以不同，不要求日志逐字一致。
模型输出 token 不允许不同。

任一项失败，候选状态立即为：

`REJECT_CORRECTNESS_OR_ISOLATION`

不得继续用性能收益覆盖该失败。

12.11 无损收益门槛

主候选至少满足下列一项，并且没有触发退化红线：

- 相同 physical KV bytes 下最大完成并发提高至少 15%；
- internal fragmentation 相对下降至少 20%，且绝对下降至少 2 个百分点；
- exact-prefix workload 的 recompute tokens 下降至少 25%；
- exact-prefix workload 的 physical KV bytes 下降至少 15%；
- stranded capacity 相对下降至少 25%；
- 在相同 workload 下 KV Cache 峰值实际分配 bytes 下降至少 15%。

同时必须满足：

- completed sessions 不减少；
- failed/rejected sessions 不增加；
- median prefill throughput 不下降超过 5%；
- median decode throughput 不下降超过 5%；
- p95 request latency 不增加超过 10%；
- 空闲后资源回收量不得减少；
- 非共享 prefix workload 不得出现超过 5% 的持续退化。

若只达到理论容量收益而没有实际分配或实际可完成 workload 证据，判定为：

`HOLD_THEORETICAL_GAIN_ONLY`

若正确性通过但所有正式收益均未达到，判定为：

`HOLD_NO_MEASURABLE_GAIN`

12.12 有损候选门槛

有损候选必须与同字节预算基线比较，不能只与 full precision/full cache 比较。

至少包含：

- full-KV oracle；
- uniform Q8；
- uniform Q4；
- 候选 mixed precision 或 compression；
- 如适用，保留更少 token 的高精度对照。

有损候选必须满足：

- 实际 KV bytes 至少下降 25%，或最大上下文/并发至少提高 25%；
- aggregate task score 相对下降不超过 2%；
- 任一单项普通任务相对下降不超过 5%；
- needle early/middle/late 每项准确率下降不超过 2 个百分点；
- 工具调用 JSON schema 成功率必须为 100%；
- system prompt retention 必须为 100%；
- 禁止指令保持率必须为 100%；
- 不得新增跨 session 信息泄漏；
- code generation 可编译率下降不超过 2 个百分点；
- 长输出事实一致性指标下降不超过 3%；
- 不得出现 crash、hang、NaN 或非法内存访问。

工具调用、安全、隔离和 system prompt 测试没有平均容错。
任何一次失败都判定为：

`REJECT_QUALITY_OR_SAFETY`

12.13 Paged/Shared Block 特定不变量

实现 paged allocation、prefix sharing 或 COW 时，必须建立自动测试验证：

1. block refcount 等于所有逻辑引用数；
2. free block 的 refcount 必须为 0；
3. active block 不得出现在 free list；
4. 不同模型或 adapter 不得共享 block；
5. 不同 token 序列不得因 hash collision 共享；
6. hash 命中后仍需验证完整 identity；
7. partial block 在追加写入前必须具有唯一所有权或执行 COW；
8. sequence 删除后只释放该 sequence 的引用；
9. shared block 只有最后一个引用删除后才能回收；
10. generation 变化后旧引用不得重新有效；
11. state save/load 后引用关系正确恢复，或明确禁用并给出错误；
12. defrag/move 后逻辑 block table 全部更新；
13. allocator 失败不得产生部分提交的 sequence 状态；
14. server shutdown 后所有 block 可回收；
15. 64 位计数器溢出和 refcount 上限有明确处理。

这些测试缺失时，paged/shared block 候选不得判定 PASS。

12.14 量化和淘汰特定不变量

KV 量化必须验证：

- K 和 V 的 layout、scale、zero point 与 kernel 一致；
- group 尾部不足一个 group 时正确处理；
- 不同 layer/head 的类型表不会错位；
- recent residual window 边界正确；
- save/load 明确支持或明确拒绝；
- CPU 与 GPU 路径结果差异被测量；
- 实际 allocated bytes 与理论 bytes 的差异被报告。

Token/head/layer 淘汰必须验证：

- 原始 RoPE position 不因物理压缩重新编号；
- sink 和 recent window 不越界；
- 每层预算总和不超过配置；
- 被淘汰 token 不再被 kernel 读取；
- top-k 相同分数时有确定性 tie-break；
- attention metadata 与 KV 内容同步更新；
- 空预算和极小预算返回明确错误或受支持行为；
- `compression_budget=100%` 与 full-KV 输出完全一致。

12.15 外部阻塞判定

只有以下情况可以停止实现并报告 HOLD：

- article-mcp、网络和所有权威论文来源均不可访问，导致无法达到最低阅读数量；
- 必需模型文件不存在且本机没有可用替代模型；
- 编译工具链缺失且无法在当前权限下安装或使用已有环境；
- GPU 不可用，而候选必须依赖 GPU 且 CPU 路径不能验证；
- GPU 被无法确认归属的外部进程长期占用；
- 磁盘空间不足且不能清理本任务生成物；
- 当前代码无法构建，且失败由任务开始前的用户修改造成；
- 必需的第三方服务需要用户凭据；
- 候选需要当前硬件或驱动不支持的虚拟内存/API；
- 用户明确要求暂停。

以下不构成外部阻塞：

- 候选实现复杂；
- 第一条路线失败；
- 论文结果不能直接复现；
- benchmark 收益不足；
- 测试失败；
- article-mcp 不可用但正式论文网页可访问；
- 没有现成脚本；
- 需要增加 instrumentation；
- 需要缩小原型范围。

遇到非外部阻塞时，记录原因并转入下一个已注册候选。

12.16 时间或资源不足时的降级顺序

如果运行环境存在实际时间或资源限制，按以下顺序缩减：

1. 保留所有 correctness 和 isolation 测试；
2. 保留主 P0 候选；
3. 保留 paired benchmark；
4. 保留 W07 exact prefix、W11 active pressure、W12 cancel/slot reuse、
   W16 churn、W23 tool JSON 和 W24 system retention；
5. 性能重复次数从 10 次降到 5 次，但不能低于 5 次；
6. endurance 从 30 周期降到 12 周期；
7. 第二候选只完成离线 oracle；
8. 延后 P2/P3；
9. 不得删除原始数据和失败结果。

不得通过缩减 correctness、隔离、安全或资源回收测试来节省时间。

12.17 原始数据格式

每次运行必须生成一条 JSON 记录，至少包含：

```json
{
  "experiment_id": "",
  "candidate_id": "",
  "variant": "baseline_or_candidate",
  "workload_id": "",
  "repetition": 0,
  "git_commit": "",
  "model_sha256": "",
  "config_sha256": "",
  "start_time_utc": "",
  "end_time_utc": "",
  "exit_code": 0,
  "timeout": false,
  "completed_sessions": 0,
  "failed_sessions": 0,
  "rejected_sessions": 0,
  "prompt_tokens": 0,
  "generated_tokens": 0,
  "output_token_sha256": "",
  "peak_physical_kv_bytes": 0,
  "final_physical_kv_bytes": 0,
  "logical_kv_tokens": 0,
  "unique_physical_kv_tokens": 0,
  "internal_fragmentation_bytes": 0,
  "stranded_capacity_bytes": 0,
  "shared_blocks": 0,
  "cow_operations": 0,
  "prefix_hit_tokens": 0,
  "recompute_tokens": 0,
  "purge_count": 0,
  "active_victim_count": 0,
  "cross_session_mismatch_count": 0,
  "prefill_tokens_per_second": 0,
  "decode_tokens_per_second": 0,
  "request_latency_ms": 0,
  "quality_score": null,
  "quality_gate": "PASS_HOLD_REJECT_NOT_APPLICABLE",
  "notes": []
}
```

不能获取的字段使用 `null`，并在 instrumentation gap 中解释。
不得用 0 代替未知值。

12.18 候选判定状态

每个候选只能取一个状态：

- `SELECTED_FOR_IMPLEMENTATION`
- `DEFERRED_AFTER_SCORING`
- `REJECT_INCOMPATIBLE_ARCHITECTURE`
- `REJECT_UNSUPPORTED_HARDWARE`
- `REJECT_CORRECTNESS_OR_ISOLATION`
- `REJECT_QUALITY_OR_SAFETY`
- `HOLD_THEORETICAL_GAIN_ONLY`
- `HOLD_NO_MEASURABLE_GAIN`
- `PASS_OPTIMIZATION_GAIN`

`PASS_OPTIMIZATION_GAIN` 必须同时满足：

1. 实际代码进入真实 KV 路径；
2. 相关测试通过；
3. paired benchmark 完整；
4. 达到第 12.11 或 12.12 节的数值门槛；
5. 原始数据完整；
6. 不触发任何 correctness、quality、safety 或 isolation 红线。

12.19 阶段退出条件

E6.0 只有在以下条件全部满足后完成：

- article-mcp 已探测；
- 达到最低论文阅读数量；
- 当前架构文档完成；
- 至少两个候选完成评分；
- environment 和 workload manifest 已冻结；
- optimization hypotheses 已写入文件。

E6.1 只有在以下条件全部满足后完成：

- 未优化基线可复现；
- 必需 instrumentation 可采集；
- 所有最小 correctness reproducer 可运行；
- 原始 JSON 能被汇总脚本读取；
- 基线结果已保存。

E6.2 只有在以下条件全部满足后完成：

- P0 候选进入真实 KV 路径；
- 单元测试和集成测试通过；
- 无损输出完全一致；
- allocator/lifecycle 不变量通过；
- 候选可以通过配置关闭并回到原始行为。

E6.3 只有在以下条件全部满足后完成：

- 第二候选至少有可运行原型或离线 oracle；
- 有实际输出数据；
- 明确判断是否值得进入在线路径；
- 未伪装成正式优化收益。

E6.4 只有在以下条件全部满足后完成：

- 基线和候选使用相同 manifest；
- 正式重复次数满足要求；
- 全部原始数据存在；
- 汇总数据可从原始数据重新生成；
- 候选获得 PASS/HOLD/REJECT 判定。

E6.5 只有在以下条件全部满足后完成：

- 至少完成 12 个完整 churn 周期；
- 每周期结束状态被记录；
- 没有累计泄漏；
- 没有 active victim；
- 没有跨 session 污染；
- 最终资源返回基线。

E6.6 只有在以下条件全部满足后完成：

- 最终报告列出所有事实和失败结果；
- reproduction commands 可执行；
- 所有 commit 和文件已列出；
- git status 已记录；
- 给出唯一最终状态。

12.20 提交规则

只有在仓库可正常提交且用户没有禁止提交时，执行阶段提交。

每次提交前：

- 检查 `git status --short`；
- 只暂存本阶段文件；
- 不提交用户无关修改；
- 运行本阶段测试；
- 在报告中记录测试命令和退出码。

如果工作树包含用户对同一文件的修改：

- 保留这些修改；
- 在其基础上进行最小编辑；
- 不得 checkout、reset 或恢复文件；
- 无法区分修改归属时不提交该文件，但继续生成补丁和报告。

禁止：

- `git reset --hard`；
- `git checkout -- <file>`；
- 强制清理工作树；
- 修改历史；
- 强制推送；
- 自动创建远程 PR；
- 提交模型、PDF 大文件、build 目录或完整运行日志。

12.21 最终报告必须回答的问题

最终报告不得只列文件和命令，必须明确回答：

1. 当前 KV Cache 的主要实际瓶颈是什么？
2. 该判断由哪些代码和基线数据支持？
3. 检索并完整阅读了哪些论文？
4. 哪些论文结论可以迁移到当前实现？
5. 哪些论文方法不适用，为什么？
6. 最终选择了哪两个候选？
7. 主候选修改了哪些真实数据结构和执行路径？
8. 候选保持了哪些不变量？
9. 基线与候选在完全相同条件下的原始值是什么？
10. 收益来自更低 physical bytes、更少 fragmentation、更高 reuse、
    更少 recompute，还是更高可完成容量？
11. 是否存在 throughput 或 latency 代价？
12. 输出是否与 full-KV 基线完全一致？
13. 是否发现 active victim、错误共享、泄漏或 stale generation？
14. 有损候选是否通过工具调用、安全和 system prompt 门禁？
15. 哪些结果具有统计噪声或环境限制？
16. 如何关闭或回滚优化？
17. 如何使用一条命令重现最关键结果？
18. 最终状态为什么是 PASS、PARTIAL、HOLD 或 REJECT？

12.22 最终状态的精确定义

最终只能输出以下一个状态：

`PASS_KV_CACHE_OPTIMIZATION`

必须全部满足：

- 至少完整阅读 6 篇且精读 4 篇权威论文；
- 至少两个候选完成可行性验证；
- 至少一个 P0 候选进入真实 KV 路径；
- 所有无损正确性门禁通过；
- 至少一项正式收益达到第 12.11 节门槛；
- paired benchmark、原始数据和复现命令完整；
- endurance 通过；
- 没有 safety、isolation 或 lifecycle 红线。

`PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION`

适用于：

- 有真实代码实现；
- 正确性通过；
- 但没有任何正式收益达到门槛。

不得把该状态描述成优化成功。

`PARTIAL_KV_RESEARCH_NO_IMPLEMENTATION`

适用于：

- 论文和架构研究完成；
- 但没有候选进入真实 KV 路径。

不得把离线脚本或伪代码描述成 KV 实现。

`HOLD_KV_OPTIMIZATION_INCOMPLETE`

仅适用于第 12.15 节定义的外部阻塞，或关键正式矩阵确实无法完成。
必须附带 blocker 证据、已完成工作和解除阻塞后的第一条命令。

`REJECT_KV_CACHE_OPTIMIZATION`

适用于任何以下情况：

- 错误 KV 复用；
- 跨 session、generation、model、adapter 或 tenant 污染；
- active/protected 请求被淘汰；
- 静默截断；
- 错误成功响应；
- refcount、COW、allocator 或生命周期错误；
- 不可恢复泄漏；
- 有损候选违反安全、工具调用或 system prompt 红线；
- 为制造收益而改变 workload；
- 原始数据与报告不一致。

12.23 最终回复格式

最终回复严格按以下顺序：

1. `FINAL_STATUS: <唯一状态>`
2. 一段不超过 150 字的执行结论
3. 主候选及实际修改
4. 基线与候选关键原始值表格
5. 正确性、质量、资源回收门禁
6. 论文阅读数量和最关键证据
7. 未采用候选及原因
8. 测试和 benchmark 命令
9. commit 列表
10. 产物路径
11. 已知限制或 blocker
12. 工作树状态

不得：

- 使用“看起来有效”“大概提升”“理论上可以”等代替数值；
- 只报告百分比而不报告原始值；
- 隐藏失败实验；
- 把 HOLD 或 PARTIAL 写成成功；
- 在没有达到数值门槛时输出 PASS；
- 要求用户自行完成尚未执行的核心实现或正式验收。

现在开始执行。

第一条工作更新必须报告：

- 当前仓库路径；
- git branch、commit 和工作树状态；
- 可用 MCP server/tool；
- article-mcp 状态；
- 构建系统；
- KV Cache 主要代码入口；
- GPU/server 进程状态；
- 下一步将冻结的文件。

此后直接进行论文检索、代码审计和基线冻结。
除第 12.15 节外，不要等待用户确认，不要停在计划阶段。

