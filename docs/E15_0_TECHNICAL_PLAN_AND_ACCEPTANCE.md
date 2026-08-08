# E15：分阶段技术方案与验收计划（Technical Plan & Acceptance）

> 编制时间：2026-08-08
> v4 final（按 reviewer 实施前门禁最终修订）：system 合同统一（H1）；secret denylist/fail-closed/fake-secret 测试（H2）；显式按需加载协议 ACTION: resolve_tool_payload（H3）；淘汰统一为 LRU（M4）；E15.4 只做 12.12 安全质量子集、extractive_summary 确定性事实抽取（M5）；正式 lossless PASS 需 4B greedy paired 否则 HOLD_NOT_VALIDATED（M6）；登记已存在实现文件（M7）；Low 项（停止原因/venue/来源状态）。
> 输入：`docs/E15_1_REFERENCE_TABLE.md`（引用表）；`docs/E15_ARTICLE_MCP_LOG.json`（article-mcp 唯一 E15 证据日志）；**按 llama.cpp `afbf375c6` 代码基线复核**（证据清单见 §1.1）；E6 指令硬门禁（`docs/20260807_MAJOR_INSTRUCTION.md` §12.10–12.23）。
> 约束：**只修改 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md`、`docs/E15_1_REFERENCE_TABLE.md`、`docs/E15_ARTICLE_MCP_LOG.json`**；不修改 E6–E14 历史报告/raw；不修改 llama.cpp 代码；不提交代码；不运行长 benchmark（本文件是计划，不含实测数据）。
> 授权边界（用户新指令）：**E15 是 E14 发布基线之后的独立扩展阶段，不改变 E14 的发布基线与冻结结论**（`PASS_OPERATIONAL_KV_OPTIMIZATION`、q8_0 production profile、checkpoint NO_GO 等全部保持不变）。
> 设计原则：**所有候选默认关闭**；**Qwen3.5-4B（hybrid/recurrent）保持安全禁用**；无损优先、有损仅离线 oracle；不为技术炫技增加无收益复杂度。

---

## 0. 执行摘要

E14 冻结状态为 `PASS_OPERATIONAL_KV_OPTIMIZATION`，主模型 4B 为 `CORRECT_NO_OPTIMIZATION`。E15 作为 E14 后独立扩展，把四条技术线落成**最小、可落地**的分阶段计划：

- **技术线 A（分支推理内存共享）**：现有 C1（`--kv-prefix-share`）与 `seq_cp` 已具备跨 slot 前缀共享与零拷贝元数据共享；**seq bitset 已等价引用计数**，`server-context.cpp:1951-1958/2103-2108` 已有共享源覆盖/clear 保护。**A1 定位为不变量加固（测试/benchmark 扩展为主，缺口才做最小代码改动）**。A2 radix 索引按 E9.3（N≤64 全扫描 <1ms）**DEFER/REJECT**。
- **技术线 B（Prompt 压缩）**：**应用层确定性结构化压缩（B1）→ 有损摘要离线 oracle（B2）→ 达硬门禁才评估在线（B3）**；B1 允许输入 token 改变，以 CompressionManifest 可逆/确定性 + system/tool schema/关键事实字节级保留 + **greedy 输出 token 一致（仅取 E6 §12.10 的输出 token 不变条款）**为无损门禁；无法保证输出一致即降级有损走 12.12。**禁止 token/KV 有损压缩进 4B hybrid**。
- **技术线 C（上下文优化）**：LangGraph trim/remove/summarize 形态的四级保留策略 + thread-scoped state 隔离；prompt-cache identity 为**受 E9.1 启发的新设计**（不伪称已有复合 hash key）。
- **技术线 D（工具数据外置，ToolPayloadStore，E15 应用层实现）**：完整工具调用 payload **thread-scoped 外置**，prompt 放 deterministic projection + sha256/ref；模型需原文时输出内部动作 `ACTION: resolve_tool_payload(payload_ref=<ref>, expected_hash=<hash>)`，workload 拦截并**仅下一轮**注入完整 payload 后恢复 projection（H3）；写入前 **secret denylist 检查，命中默认 fail-closed**（不存 entry/projection/ref/snapshot）（H2）；淘汰为 **LRU**（成功读取刷新 last_used_tick，淘汰 min(last_used_tick)）（M4）；Driver 零改动。

实施顺序：**E15.1 分支并发 paired + C1 不变量 → E15.2 ToolPayloadStore + tool_call/long_life 集成 → E15.3 deterministic prompt preprocessor → E15.4 context full/trim/extractive_summary + thread isolation → E15.5 有损摘要 oracle → E15.6 完整验证收口**（含 4B 门禁）。

---

## 1. 冻结基线（E13/E14 已核验事实，E15 不修改）

### 1.1 代码基线复核清单（本方案证据来源；按 llama.cpp `afbf375c6` 复核）

| 证据项 | 代码位置（llama.cpp afbf375c6） | 复核内容 |
|---|---|---|
| C1 参数注册 | `common/arg.cpp:3694-3709` | `--kv-prefix-share` / `--kv-prefix-share-min-lcp`（默认 64） |
| C1 默认关 | `tools/server/server-context.cpp:1021-1022` | `kv_prefix_share = false` |
| C1 capability gate（hybrid 拒绝） | `tools/server/server-context.cpp:1497-1515` | memory null / 不支持共享 / hybrid / recurrent / SWA 依次拒绝并记日志 |
| C1 生效条件 | `tools/server/server-context.cpp:3851-3856` | `kv_prefix_share && kv_unified && supports_cross_slot_prefix_sharing && !hybrid && !recurrent && n_swa==0` |
| C1 共享执行 | `tools/server/server-context.cpp:3851-3907` | LCP 扫描 → `seq_rm` 清自身 → `seq_cp` 共享前缀 → prompt 同步 |
| longest LCP 选择 | `tools/server/server-context.cpp:3877-3887` | `best_lcp` 更新逻辑 |
| checkpoint hybrid guard（仅 checkpoint） | `tools/server/server-context.cpp:1483-1486` | `checkpoint_reuse` 在 hybrid/recurrent 上禁用（**与 C1 无关**） |
| 共享源保护 1 | `tools/server/server-context.cpp:1951-1958` | `sh>0` 且非 full prefix match → skip |
| 共享源保护 2 | `tools/server/server-context.cpp:2103-2108` | `sh>0` → skip clear |
| `seq_cp`（同 stream 零拷贝） | `src/llama-kv-cache.cpp:447-537` | bitset 元数据共享，`seq_add` 追加归属 |
| `seq_cp`（跨 stream 需拷贝） | `src/llama-kv-cache.cpp:490-504` | `is_full` 断言 + enqueue copy（**行号已按审查修正**） |
| cell bitset（等价引用计数） | `src/llama-kv-cells.h:238/301/309` | `seq_rm` / `seq_has` / `seq_add` |
| memory capability 默认 | `src/llama-memory.h:122`（默认 false）；`src/llama-kv-cache.h:156`（标准实现 true） | 仅标准 unified attention KV 可共享 |
| physical_sharing 契约 | `src/llama-kv-cache.cpp:797` | `stats.physical_sharing = false`（metadata sharing 不声明物理共享） |
| KV 统计 | `src/llama-kv-cache.cpp:698-770` | `seq_cell_stats`（shared_cells） |

### 1.2 冻结状态

| 项 | 值 | 证据 |
|---|---|---|
| 项目状态 | `PASS_OPERATIONAL_KV_OPTIMIZATION` | `docs/E13_0_*`、`docs/E14_FINAL_RELEASE_DECISION.md` |
| 主模型 4B | `CORRECT_NO_OPTIMIZATION`（hybrid 无 KV 优化收益） | E12/E13 |
| q8_0 KV 量化 | 生效（KV bytes -47%、GPU -58MB、per-cell 17408 B） | E10–E14，`benchmark/configs/qwen35_4b_q8_production.yaml` |
| C1 前缀共享 | 已实现+验收（TinyLlama：exact-prefix recompute 229→24，-89.52%；输出 token hash 一致；shared_cells 0→205），**默认关**、生产未启用 | `docs/E6_KV_OPTIMIZATION_FINAL_REPORT.md` §3；`server-context.cpp:1021` |
| checkpoint 复用 | `NO_GO_WITH_EVIDENCE`，hybrid guard 自动禁用（**仅作用于 checkpoint**） | `server-context.cpp:1483-1486` |

### 1.3 已存在实现登记（M7，本方案不再声称"只计划"）

以下文件已在本轮之前实现（未提交、未集成 workload、未跑真实模型 paired），E15 后续阶段在其基础上演进：

| 文件 | 内容 | 测试 |
|---|---|---|
| `benchmark/framework/context_policy.py` | C 线上下文策略核心（纯 Python stdlib）：`ContextPolicy`（full/trim/**extractive_summary**）、`ThreadState`（thread-scoped）、`CompressionManifest`、`DuplicateBlockCompressor`（完全重复块 dictionary/ref 去重，round-trip 可逆）、`ToolProjection`、snapshot/restore/purge、fail-fast 异常体系 | `benchmark/tests/test_context_policy.py`（68 例纯函数，通过；2026-08-08 阶段 0 实测 68 passed，与 E15.4 一致） |
| `benchmark/runner/e15_branch_concurrent.py` | E15.1 分支并发 paired runner：共同前缀 source 预热 + N 分支 target 并发（ThreadPoolExecutor + barrier）、门禁 G1–G9（输出一致/shared_cells>0/recompute≥25%/无污染/回基线/物理共享契约等） | `benchmark/tests/test_e15_branch_concurrent.py`（纯函数，通过） |

- **命名统一（M7）**：上下文策略第三档统一为 **`extractive_summary`**（确定性结构化事实抽取，不调用 LLM），不再使用 `summarize` 泛称；CLI 为 `--context-policy {full|trim|extractive_summary}`。
- 上述实现为 **DRAFT 状态**（`docs/E15_4_C_CONTEXT_POLICY_CORE_DRAFT.md`）：仅独立核心 + 纯函数测试，未集成 workload、未跑真实模型 paired、未验证 12.12；不视为正式验收证据。

---

## 2. 技术线 A：分支推理内存共享（能力边界 + A1 加固定位 + A2 处置）

### 2.1 现有能力边界（已验证事实，afbf375c6 复核）

| 机制 | 能力 | 边界（代码证据） |
|---|---|---|
| `seq_cp`（同 stream） | **纯 bitset 元数据共享，零数据拷贝**；`seq_add` 给 cell 追加归属 seq；**bitset 即等价引用计数** | `llama-kv-cache.cpp:447-537`；`llama-kv-cells.h:238/301/309` |
| `seq_cp`（跨 stream） | 需完整 buffer 数据拷贝（`is_full` 断言），非零拷贝 | `llama-kv-cache.cpp:490-504` |
| C1 `--kv-prefix-share` | 跨 slot 前缀共享：扫描 idle source → LCP → 清自身旧 KV → `seq_cp` 共享前缀 → 只计算剩余部分 | `server-context.cpp:3851-3907` |
| C1 生效条件 | `--kv-prefix-share` on；`--kv-unified`；memory `supports_cross_slot_prefix_sharing()==true`；非 hybrid/recurrent/SWA；source 非 processing；adapter/lora identity 一致；LCP ≥ min_lcp（默认 64） | `server-context.cpp:3851-3856`；`arg.cpp:3700-3709` |
| **C1 hybrid 拒绝（正确证据）** | ① capability gate：`server-context.cpp:1497-1515`；② 生效条件 `3851-3856`；③ `llama-memory.h:122` 基类默认 false（仅标准 `llama_kv_cache` 在 `llama-kv-cache.h:156` override 为 true） | 见 §1.1 清单 |
| 共享源覆盖/clear 保护（**已存在**） | ① prompt 不一致时跳过共享源（`1951-1958`）；② 共享源不清 KV（`2103-2108`） | `server-context.cpp:1951-1958`、`:2103-2108` |
| 物理共享统计 | `stats.physical_sharing = false`（**既有契约**：metadata sharing 不声明为物理共享/COW） | `llama-kv-cache.cpp:797` |

> **注意**：`server-context.cpp:1483-1486` 是 E13.2 **checkpoint 复用**的 hybrid guard，**不是** C1 的拒绝证据（§1.1 已区分）。

### 2.2 是否需要真实 COW / 新 refcount——结论与理由

**结论：不需要新的 refcount/guard 状态，不需要数据级 COW；A1 定位为对既有 bitset 引用与共享源保护的可观测性/不变量加固。**

| 问题 | 回答 | 理由 |
|---|---|---|
| 同 stream 分支共享需要数据 COW 吗？ | **不需要** | `seq_cp` 同 stream 已是零拷贝元数据共享；数据 COW 只会复制同一块 buffer 内容，无收益 |
| 需要新增独立 refcount 结构吗？ | **不需要** | cell 的 seq bitset 已等价引用计数；新增 refcount 是与 bitset 重复的状态 |
| 共享源覆盖/淘汰需要新保护吗？ | 现状已有（§2.1） | `1951-1958` 与 `2103-2108` 已是保护；A1 只负责**验证这些保护的行为不变量** |
| metadata sharing 是 COW 吗？ | **不是** | `physical_sharing=false` 是既有契约；metadata sharing 不得称为 paged COW |
| attention-only 与 hybrid 差异 | attention-only（TinyLlama）**可共享**；Qwen3.5-4B hybrid **永久禁用** | §2.1 表：capability gate（1497-1515）+ memory 默认 false（llama-memory.h:122）双重拒绝 |

### 2.3 A1 候选：既有 bitset 引用与共享源保护的可观测性/不变量加固（P0，默认关，以测试/benchmark 为主）

- **定位**：若审计无缺口 → **纯测试与 benchmark 扩展（不改 llama.cpp 代码）**；若审计发现缺口 → 最小代码改动（仅修复缺口，不新增重复状态）。
- **审计清单（第一步，先查后改）**：
  1. bitset 引用（`seq_has`/`seq_add`/`seq_rm`）在 C1 生命周期各点（共享建立、source 覆盖、slot purge、`seq_rm`）的行为是否满足 E6 §12.13 不变量 1/3/8/9；
  2. `1951-1958`/`2103-2108` 保护在并发/分支 workload 下是否被覆盖到（现有单测是否覆盖共享源 clear/skip 路径）；
  3. `physical_sharing=false` 契约与 `shared_cells` 统计在分支场景的语义是否清晰。
- **数据结构**：不新增 llama.cpp 状态；若需要观测，新增**独立 guard 事件日志**（如 `E15-A1: shared-source clear skipped`）或扩展现有 `seq_cell_stats` 输出，或在测试侧记录。
- **API/配置**：无新 llama.cpp 参数；沿用 `--kv-prefix-share`（默认关）+ `--lifecycle-trace`（debug-only，已有）。
- **生命周期**：不新增生命周期语义；验证既有语义。
- **正确性/安全/工具调用风险**：不改变输出 token（12.10）；风险只在测试不足导致未覆盖的边界（跨 seq 错误复用 12.13.5/10）。
- **回滚**：不引入新行为，无回滚需求；测试/日志可独立关闭。
- **最小复现**：`cd llama.cpp/tools/server/tests && LLAMA_SERVER_BIN_PATH=$ROOT/llama.cpp/build/bin/llama-server HF_ENDPOINT=https://hf-mirror.com LLAMA_CACHE=$ROOT/llama.cpp/tmp python3 -m pytest unit/test_kv_prefix_share.py --noconftest -v`（需 `LLAMA_SERVER_BIN_PATH` 指向实际 build 路径，`HF_ENDPOINT`/`LLAMA_CACHE` 用于离线模型缓存；耗时随环境变化，不以固定秒数承诺）；扩展用例：共享源 clear/skip 路径、purge 顺序、多分支并发。
- **原始指标与验收阈值**：输出 token ID 与 C1-off 完全一致（12.10）；12.13 十五条不变量测试全绿（含新增审计项）；`shared_cells>0` 且覆盖共享源不崩溃、不清空引用者 KV；exact-prefix recompute tokens 保持 ≤24（E6.4 基线）不退化。
- **判定**：审计发现缺口且修复失败 → `REJECT_CORRECTNESS_OR_ISOLATION`；无缺口（纯测试扩展）→ PASS（不变量全绿）；正确性过但收益不达 → `HOLD_NO_MEASURABLE_GAIN`。

### 2.4 A2：server 层 radix 前缀树索引——**DEFER/REJECT（不实现）**

- **决策依据（已核验数据）**：
  - **E9.3（实测）**：C1 线性 LCP 扫描在 N≤64（32 source 实测 on-off Δ = -0.29ms，噪声内；64/64 source 全成功）**CPU 开销可忽略（<1ms），无扩展性瓶颈**。
  - 候选集内 C1 已取 **longest LCP**（`server-context.cpp:3877-3887`），不存在"只找到次长前缀"的命中率损失。
  - 当前生产部署 `--parallel 4`，N 远小于 64；radix 索引的维护/淘汰/失效复杂度在当前规模是**纯额外复杂度，无收益**。
- **结论**：**DEFER/REJECT radix 索引**；除非未来出现明确查找瓶颈证据（如 N>64、source 数过百、实测 scan 时间进入 ms 级且占请求预算显著比例），不实现。**不得以"提高命中率"为由实现**。
- **保留**：RadixAttention（NeurIPS 2024）仅作思想对照（E15_1 §1.2/§2）。

---

## 3. 技术线 B：Prompt 压缩（应用层确定性结构化压缩 → 有损摘要 oracle → 在线决策）

### 3.0 硬性边界（用户要求 + E6/E12 既有结论）

- **禁止**把 token/KV 有损压缩直接写进 Qwen3.5-4B hybrid（E12 `NO_GO_WITH_EVIDENCE`；hybrid guard 永久禁用 KV 层改动）。
- 压缩分三步走：**B1 应用层确定性结构化压缩 → B2 有损摘要离线 oracle（12.12 门禁）→ B3 在线路径决策（默认关）**。
- B1/B2 均不修改 llama.cpp KV 路径；B3 若被批准，也只做 **prompt 前置改写 + thread-scoped state**，不触碰 server cache identity（除非明确必要性）。

### 3.1 B1 候选：应用层确定性结构化压缩（P1，默认关）

- **定位**：**输入 token 序列允许改变**——压缩的收益正是减少 token；"输入 token 序列相同且 token 数减少"是矛盾表述。**无损与否由输出决定，不由输入决定。**
- **无损候选门禁（定义）**：同时满足以下才可称 lossless：
  1. **CompressionManifest 可逆/确定性**：压缩前后映射可完整还原（原 prompt = f(压缩 prompt, manifest)，且同一输入 → 同一压缩输出、同一 manifest）；
  2. **字节级保留**：system prompt、tool schema、关键事实（用户约束/上下文事实）在压缩产物中字节级保留（不省略、不改写）；
  3. **greedy 输出一致 + 相同停止原因**：压缩前后（同 seed/temp=0）生成输出 token ID 完全一致 **且停止原因一致**——仅取 E6 §12.10 的"相同输入得到完全相同的 output token ID"与"相同停止原因"两条条款；**不适用** §12.10 的"相同 prompt token 数/相同输入 token 序列"条款（B1 允许输入 token 改变，这两条按定义不适用）。
  - 任一不满足 → **降级为有损候选，走 12.12 门禁，不得称 lossless**。
- **压缩内容（候选集，仅允许下列确定性变换；H1 system 合同）**：
  - **system 合同（统一，永不违反）**：禁止指令、安全约束、tool schema 本体**永不删改且字节级保留**；仅**完全重复且 manifest 可逆**的非安全模板片段允许 dictionary/ref 去重（`DuplicateBlockCompressor` 语义）；去重必须可 round-trip 还原，且**禁止指令 byte compare**（去重前后禁止指令片段逐字节一致）纳入验收。
  - 1) system prompt 模板规范化去重（仅限完全重复的非安全片段，manifest 可逆）；
  - 2) 工具描述固定模板化（schema 本体字节级保留，仅折叠重复字段说明）；
  - 3) 历史轮次结构化重排（内容全保留，仅调整呈现顺序；若 token 增加则无收益，不采用）。
  - **排除**：语义改写、摘要（属于 B2）、token 剪枝（KV 层，禁止）、任何对禁止指令/安全约束/tool schema 本体的删改。
- **数据结构**：`CompressionManifest`（原 prompt ↔ 压缩 prompt 可逆映射 + 版本 + 验证 hash）；`ThreadState`（见 C 线）承载压缩产物；**不修改 server cache identity**。
- **API/配置**：benchmark 层 `framework/` 增加 `prompt_preprocessor` 钩子（`--preprocessor structured-lossless`，默认 off）；server 层不新增参数。
- **生命周期**：预处理在请求构造时执行；压缩产物通过 thread-scoped state 传递；验证失败 → 回退原始 prompt。
- **正确性风险**：BPE 边界（文本级"看起来一样" ≠ token 级一致，E12 教训）——验证用 `tokenize` 后 diff + greedy 输出 diff；manifest 不可逆 → 立即降级。
- **回滚**：配置关 = 原始 prompt。
- **最小复现**：pytest 单元（无 GPU，0.5b 或 mock）为**开发信号**；**正式 lossless PASS 必须 4B greedy paired**（同 seed/temp=0，4B 模型，paired 输出 token/停止原因一致），否则 `HOLD_NOT_VALIDATED`（M6）。
- **原始指标与验收阈值**：压缩率（prompt tokens 减少 %，原始值记录）；**greedy 输出 token 完全一致 + 相同停止原因（硬门禁）**；不达 12.11 收益门槛只算 HOLD，不伪装收益。

### 3.2 B2 候选：摘要压缩离线 oracle（P2，有损，**仅离线**）

- **参考**：PartPrompt（ABSTRACT_ONLY）、R-KV（PARTIAL_TEXT_READ）、SnapKV（FULL_TEXT_READ）、LLMLingua（ABSTRACT_ONLY）——只借鉴应用层思想，不实现 token 剪枝内核。
- **内容（候选集）**：历史轮次滚动摘要（LangGraph `summarize` 模式）；长工具说明摘要；超长文档分块摘要。**仅离线评估，不进入在线路径，除非 12.12 全项达标。**
- **数据结构**：`SummaryCache`（thread_id → summary + 覆盖区间 + 版本）；摘要与原文**身份分离**（不同 prompt-cache key/thread-scoped key，绝不复用原始 KV）。
- **API/配置**：benchmark 层 `--preprocessor summary`（默认 off）；**不进入 server/KV**。
- **生命周期**：离线生成摘要 → 与同字节预算基线（full-KV oracle / uniform Q8 / uniform Q4）对比 → 达标才允许进入 B3 决策。
- **正确性/安全/工具调用风险**：tool JSON 失败（硬红线）、system prompt/禁止指令丢失（硬红线）、跨 session 泄漏（身份隔离）、needle 遗忘。
- **回滚**：off = 原始 prompt。
- **最小复现**：pytest + 0.5b/0.8b CPU 离线 oracle（短摘要样例），不跑 4B。
- **验收阈值（E6 §12.12 全项，硬门禁）**：实际 KV bytes -25% 或最大上下文/并发 +25%；aggregate task score 下降 ≤2%；单项普通任务 ≤5%；needle early/middle/late 每项 ≤2pp；**tool JSON schema 成功率 100%**；**system prompt retention 100%**；**禁止指令保持率 100%**；无新增跨 session 泄漏；code 可编译率 ≤-2pp；长输出事实一致性 ≤-3%；无 crash/hang/NaN。任一失败 → `REJECT_QUALITY_OR_SAFETY`（无平均容错）。

### 3.3 B3 在线路径决策（门禁判定，非实现承诺）

- 前置条件：B1 无损验证通过 **且** B2 离线 oracle 全项达标。
- 在线形式（若批准）：benchmark/前置层 prompt 改写 + thread-scoped state；**KV 层零改动**；默认关，显式开启。
- 不满足任一前置 → 保持关闭，记录 HOLD，**不得写进 4B hybrid**。

---

## 4. 技术线 C：上下文优化（LangGraph 式状态保留策略 + thread-scoped 隔离）

### 4.1 参考（官方文档已核实，见 E15_1 §2）

LangGraph v1.2.10：`trim_messages`（裁剪最旧消息）/ `RemoveMessage`（节点内删除）/ 手写 `summarize_conversation` 节点（摘要替换）；状态经 Checkpointer 增量持久化。概念参考：CoALA（记忆分层）、LongMemEval（长期记忆评测维度）、MemGPT（快慢内存分层）。

### 4.2 四级保留策略（数据结构 + 生命周期）

| 级 | 内容 | 保留策略 | 生命周期 |
|---|---|---|---|
| **L1 system** | system prompt + 禁止指令 + 安全约束 | **永不删改且字节级保留**（H1 system 合同；12.12 system retention 100%）；仅完全重复且 manifest 可逆的非安全模板片段允许 dictionary/ref 去重，**禁止指令 byte compare 验收** | 每请求前置；字节级保留（B1 门禁） |
| **L2 tool** | 工具 schema/描述 | **schema 本体永不删改且字节级保留**；仅允许确定性去重模板（B1-2）；**secret 永不外置、永不落盘、不进 store** | 每请求前置；tool JSON 100% 红线 |
| **L3 事实** | 用户提供的领域事实/约束 | 压缩候选需 **needle 每桶 ≤2pp** 门禁；默认全保留 | 会话内持久；摘要版本与原文版本身份分离 |
| **L4 历史** | 历史对话轮次 | 最近 N 轮原文保留；更旧轮次 **摘要替换（summarize）或删除（RemoveMessage 语义）**，默认保留原文（策略 off） | 滚动窗口；摘要进 L3 缓存，原文可丢弃 |

- **数据结构**：`ThreadState`（L1/L2 静态 + L3 事实列表 + L4 滚动窗口 + `SummaryBlock` 数组）；每 thread 独立（**thread-scoped state**）。
- **API/配置**：benchmark `workload/long_life` 与 `framework` 增加 `--context-policy {full|trim|extractive_summary}`（默认 `full` = 现状）。

### 4.3 身份隔离（防跨 session/thread 泄漏）

- **prompt-cache identity：受 E9.1 启发的新设计，不伪称已有复合 hash key**。现状（E9.1）只有 LoRA identity 门禁（`server_prompt_cache_state::lora` + `are_lora_equal`）；**不存在** model/system/tool hash 复合 key。E15 若需要缓存身份扩展，是**新设计**，需单独评审。
- **本阶段默认路径：应用层压缩优先用 thread-scoped state，避免修改 server cache identity**——压缩/摘要产物挂在应用层 `ThreadState` 上，不同 thread 天然隔离；不触碰 `server_prompt_cache` 的 key 逻辑，除非后续出现明确必要性再评审扩展。
- 每 thread 独立 slot（`--parallel` 下天然隔离）；tenant 限定 `TRUSTED_SINGLE_TENANT`（E8.3）。

### 4.4 正确性/安全/工具调用风险、回滚、复现、验收

- 风险：摘要丢失关键事实（needle 门禁）、工具调用失败（tool JSON 100% 红线）、跨 thread 泄漏（thread-scoped state 隔离测试）。
- 回滚：`--context-policy full` = 现状（无任何状态改写）。
- 最小复现：`uv run python agent_bench.py --scenario long_life --long-rounds 40`（对照 full vs trim/extractive_summary，同 seed/模型/配置）；pytest 单元覆盖保留策略纯函数（已存在 `test_context_policy.py`）。
- **验收阈值（M5：E15.4 只做 12.12 安全质量子集）**：tool JSON schema 成功率 100%、system prompt retention 100%、禁止指令保持率 100%、无新增跨 session 泄漏、无 crash/hang/NaN + 跨 thread 隔离 needle（A/B 两 thread 交叉请求无缓存复用证据：`/metrics/kv` shared_cells 与 prompt-cache 命中日志均无跨 thread 命中）+ 泄漏 needle（thread A 的事实不出现在 thread B 输出）。**完整同字节预算 oracle（12.12 的 bytes -25% / 并发 +25%、aggregate score 等数值项）留 E15.5（B2）执行**；E15.4 不跑完整 oracle。
- **extractive_summary 定位（M5）**：只做**确定性结构化事实抽取策略**（原文子串提取 + 工具投影 + summary block，不调用 LLM，已存在实现）；**论文候选质量 oracle（R-KV/PartPrompt/SnapKV 思想）留 E15.5（B2）**。

---

## 5. 技术线 D：工具调用数据外置（ToolPayloadStore，E15 应用层实现）

> 审查升级：工具调用中间数据从"排除项"升级为 **E15 应用层实现**（对应赛题"工具调用数据优化"方向）；分层/异构存储仍排除（§6）。

### 5.1 问题与依据

- **问题**：工具调用产生的大规模中间数据（检索结果、文件内容、长 JSON 响应）若完整进入 prompt，会推高 prefill token 数与 KV bytes，挤占上下文（Lost in the Middle：中段信息退化）且不必要。
- **依据（ABSTRACT/PARTIAL 级，未在本仓库复现）**：MemGPT（快慢内存分层、虚拟上下文管理）；CoALA（工作记忆 vs 长期记忆分层）；Lost in the Middle（长上下文中段退化）；LongMemEval（长期记忆评测维度）。见 `docs/E15_ARTICLE_MCP_LOG.json` seq 4/6/7/8 与 E15_1 §1.1。

### 5.2 方案（ToolPayloadStore）

- **核心思想**：完整 payload **thread-scoped 外置**（应用层慢存储），prompt 只放 **deterministic projection + payload_ref**——模型看到的是 payload 的确定性摘要/引用（而非原文），需要原文时通过**显式按需加载协议**（H3）取回。
- **数据结构**：
  - `ToolPayloadStore`（每 thread 一个实例，thread-scoped）：`dict[payload_ref → PayloadEntry{bytes, sha256, inserted_tick, last_used_tick}]`；
  - `PayloadRef`：`sha256:prefix`（确定性投影的短引用）；
  - `Projection`：确定性函数 `payload → (projection_text, sha256)`（如固定长度前缀 + 结构化字段抽取，**同输入必同输出**）。
- **API/配置**：benchmark 层 `--tool-payload-store on|off`（**默认 off**，可回滚）；`--tool-payload-max-bytes`（LRU/bytes 容量上限）；`--tool-payload-projection {head|structured}`；`--tool-payload-secret-denylist <regex>`（可配置扩展敏感字段模式）。**Driver 零改动**（OpenAI 兼容请求面不变）。
- **生命周期**：
  - 写入：工具调用返回 → **先过 secret denylist（H2）** → 通过才存 store，prompt 中放 projection + ref；
  - 读取：后续轮次引用 payload → 按 ref 取回或按 projection 注入；**成功读取刷新 `last_used_tick`（M4）**；
  - 容量：**LRU（M4）**：淘汰 `min(last_used_tick)` 的 entry；`inserted_tick` 仅作审计/tie-break，不参与淘汰主序；
  - snapshot/restore：thread 状态快照含 store（**进程内恢复结构，默认不落盘**；benchmark 结果只记录统计，不记录 payload/secret 明文）；purge：thread 结束或显式清理。
- **H2 secret 定义与过滤（写入前强制执行）**：
  - **denylist**：确定性敏感字段/模式列表，默认含 `token` / `api_key` / `apikey` / `password` / `passwd` / `secret` / `authorization` / `cookie` / `session` 等（大小写不敏感，可配置扩展）；
  - **检测到敏感字段 → 默认 fail-closed**：不存 entry / projection / ref / snapshot，拒绝该 payload 外置（记录统计事件，不含明文）；
  - 可选 redaction 模式**也不得保留原文**（redacted 版本同样过 denylist 校验）；
  - **long_life 的 secret 是 protected fact（L3），不进入 store**（永远留在上下文/ThreadState 的 protected 区，§4.2）；
  - **fake-secret 测试**：新增测试验证 store / snapshot / prompt / log **均无明文**（以 fake secret 如 `fake_token_xyz` 注入，断言四类输出中无该字符串）。
- **H3 显式按需加载协议**：
  - 第 1 轮：projection + `payload_ref` 进 prompt（**不得每轮自动注入全部 payload**）；
  - 模型需要原文时，输出**内部动作**：`ACTION: resolve_tool_payload(payload_ref=<ref>, expected_hash=<hash>)`；
  - workload 拦截该内部动作，校验 ref/hash（跨 thread / missing / hash mismatch → **fail-fast**），**仅下一轮**注入完整 payload；使用后**恢复 projection**（再次换回引用形态）；
  - **普通工具 action 不变**（只有 resolve_tool_payload 是内部动作）；
  - **search_orders 投影约束**：`search_orders` 类工具的 projection **必须包含 order_ids**（或等价的定位字段），以支持现有链路在未注入完整 payload 时仍可完成流程。
- **正确性/安全/工具调用风险（fail-fast 设计）**：
  - **跨 thread 访问 → fail-fast**（拒绝并报错，绝不静默降级）；
  - **ref 缺失（被淘汰/从未写入）→ fail-fast**（宁可失败，不让模型在缺 payload 下继续）；
  - **hash mismatch（投影与 payload 不一致）→ fail-fast**；
  - **secret 红线**：H2 全项（denylist fail-closed、不进 store/snapshot、fake-secret 无明文）；
  - **工具 JSON/system/secret 100%**：projection 不得改变 tool schema、system prompt、禁止指令、secret 语义。
- **回滚**：`--tool-payload-store off` = 原始 prompt（完整 payload 直接进 prompt，现状行为）。
- **最小复现**：pytest 单元（无 GPU）：store 读写/LRU 容量淘汰（含访问刷新）/snapshot-restore/purge/跨 thread 拒绝/hash mismatch 拒绝/**secret denylist fail-closed**/**fake-secret 无明文**；随后 `agent_bench.py --scenario tool_call` 与 `long_life` 集成对照。
- **原始指标与验收阈值（E15.2 阶段）**：
  - projection 确定性（同输入 → 同 projection + 同 sha256，100% 用例）；
  - 防跨 thread（A 的 ref 在 B 中访问 → 拒绝，100%）；
  - missing fail-fast（ref 缺失 → 显式错误，0 静默降级）；
  - **LRU/bytes 容量（M4）**：淘汰顺序 = `min(last_used_tick)`（插入序仅 tie-break），字节上限生效；**访问刷新测试**（读取后 last_used_tick 更新，不被误淘汰）；
  - snapshot/restore 正确（restore 后 ref 可解析）；
  - purge 后 ref 不可解析（fail-fast）；
  - **H2 全项**：denylist 命中 → fail-closed（0 entry 落盘）；fake-secret 在 store/snapshot/prompt/log 中 0 明文；
  - **H3 协议**：resolve_tool_payload 动作被拦截、仅下一轮注入、使用后恢复 projection；普通工具 action 不变；search_orders projection 含 order_ids；
  - **tool JSON schema 成功率 100%、system prompt retention 100%、禁止指令保持率 100%、secret 不外泄 100%**；
  - paired 指标：prompt tokens 与 KV bytes 在 store on vs off 的对比（原始值记录；收益达 12.11/12.12 门槛才可 PASS，否则 HOLD）。

---

## 6. 赛题范围覆盖与排除（工具数据升级为 D 线；分层/异构存储仍排除）

| 赛题方向 | 本阶段处理 | 理由 |
|---|---|---|
| KV Cache 生命周期管理 | 既有：q8_0 生产化（E10–E14）+ C1 前缀共享（E15.1 加固） | 已固化 |
| **分支推理内存共享** | **E15.1–E15.2 主线**：C1 不变量加固 + 分支并发 paired benchmark | 本方案核心 |
| **Prompt 与上下文压缩** | **E15.3–E15.5 主线**：B1 结构化无损 + C 保留策略 + B2 摘要 oracle | 本方案核心 |
| **工具调用数据优化**（中间数据结构化存储/按需加载，避免完全进 KV） | **E15.2 应用层实现：ToolPayloadStore**（完整 payload thread-scoped 外置 + deterministic projection + sha256/ref；**H2 secret denylist fail-closed**；**H3 显式按需加载协议**；**LRU 淘汰**；默认 off 可回滚） | 对应赛题方向；应用层实现（Driver 零改动），不触碰 KV/不修改 server cache identity |
| **分层内存与异构存储**（GPU/主存/外部存储冷热分离、迁移） | **排除** | E6 已证 vAttention 类方案依赖 CUDA VMM/驱动（本机 RTX 4060 8GB 不支持，`REJECT_UNSUPPORTED_HARDWARE`）；分层迁移需要新的存储后端与数据面协议，超出 E15 范围；作为 E14 后独立候选记录，不在本方案实施 |
| 异构 AI 加速硬件（DTK/CANN 等） | **排除** | 无国产加速硬件环境；不影响 llama.cpp 路径正确性 |

---

## 7. 所有候选默认关闭与 4B hybrid 安全禁用（总表）

| 候选 | 开关 | 默认 | 4B hybrid | attention-only（TinyLlama） |
|---|---|---|---|---|
| C1 `--kv-prefix-share`（现状） | `--kv-prefix-share` | 关 | **禁用**（capability gate 1497-1515 + memory 默认 false） | 可用（已验收） |
| A1 bitset/共享源保护加固 | 无新参数（测试/日志） | 关 | 禁用（同 C1 门禁） | 可用 |
| A2 radix 索引 | — | **DEFER/REJECT** | — | — |
| D ToolPayloadStore | `--tool-payload-store` | 关 | 可用（应用层，不触 KV） | 可用 |
| B1 结构化确定性压缩 | `--preprocessor structured-lossless` | 关 | 可用（应用层，不触 KV） | 可用 |
| B2 摘要（离线） | `--preprocessor summary`（仅离线 oracle） | 关 | **不进在线路径** | 可离线验证 |
| C 上下文策略 | `--context-policy {full\|trim\|extractive_summary}`（默认 full） | full（=关闭） | 可用（应用层） | 可用 |

---

## 8. 阶段顺序、产物、测试与提交边界（最终实施顺序）

> 提交规则遵循 E6 §12.20（llama.cpp 与根仓库各自独立提交；每阶段完成后提交；不提交模型/build/日志）。本计划为计划文本，实际实现与提交需用户授权后进行。

**实施顺序（最终定稿）**：
1. **E15.1 分支并发 paired benchmark + C1 不变量加固**——零/极小代码改动，直接产出分支共享行为证据（共同前缀 source 预热后 target 并发）；
2. **E15.2 ToolPayloadStore + tool_call/long_life 集成**——工具数据外置应用层实现（H2/H3/LRU）；
3. **E15.3 deterministic prompt preprocessor（B1）**——应用层结构化压缩；
4. **E15.4 context full/trim/extractive_summary + thread isolation（C）**——上下文保留策略（12.12 安全质量子集）；
5. **E15.5 有损摘要 oracle（B2）**——12.12 完整同字节 oracle，达标才评估上线；
6. **E15.6 完整验证收口（含 4B 门禁）**——全量验收 + 12.22 唯一状态。

| 阶段 | 内容 | 产物（docs） | 测试/验收 | PASS 条件 | HOLD 条件 | REJECT 条件 |
|---|---|---|---|---|---|---|
| **E15.0** | 本方案 + 引用表 + article-mcp 证据日志定稿 | `E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md`、`E15_1_REFERENCE_TABLE.md`、`E15_ARTICLE_MCP_LOG.json` | 引用可核验；venue 与 E8.1 一致；读取状态分级正确；计数表述与 manifest 一致 | 评审通过 | 引用不可核验（网络/工具不可用，12.15） | — |
| **E15.1** | A1 不变量加固（审计 → 测试/benchmark 扩展 → 仅缺口时最小修复）+ 分支并发 paired workload | `E15_1_A1_SHARED_CELL_INVARIANTS.md`、`E15_1B_BRANCH_PAIRED.md` | 12.10 全项 + 12.13 十五条 + `test_kv_prefix_share.py`（新增共享源 clear/skip、purge、多分支并发用例）+ `e6_c1_bench.py --reps 5`；**分支并发 runner（已存在 `benchmark/runner/e15_branch_concurrent.py` + 门禁 G1–G9）**：共同前缀 **source 预热并 idle 后**，N 分支 target 并发（warmup≥2 + 正式≥5 中位数）；`/metrics/kv` shared_cells/recompute；输出 hash 一致 | 输出 token 一致 + 不变量全绿 + recompute ≤24 不退化 + 分支场景 shared_cells>0（G2）且 recompute 降 ≥25%（G3） | 审计无缺口且全绿 → PASS（纯测试扩展）；正确性过、收益不达 → `HOLD_NO_MEASURABLE_GAIN` | bitset 语义破坏/崩溃/污染 → `REJECT_CORRECTNESS_OR_ISOLATION` |
| **E15.2** | D：ToolPayloadStore 应用层实现 + tool_call/long_life 集成 | `E15_2_TOOL_PAYLOAD_STORE.md` | pytest 单元（projection 确定性/hash/防跨 thread/missing fail-fast/**LRU 容量（含访问刷新）**/snapshot-restore/purge/**H2 secret denylist fail-closed + fake-secret 无明文**/**H3 resolve_tool_payload 协议**/tool JSON-system-禁止指令-secret 100%）+ tool_call/long_life 对照 | §5.2 验收全项（含 H2/H3/M4）+ paired prompt token/KV 指标记录；tool JSON/system/禁止指令/secret 100% | 收益不达 12.11/12.12 → HOLD | fail-fast 失效/secret 泄漏/跨 thread 放行 → `REJECT_QUALITY_OR_SAFETY` |
| **E15.3** | B1：deterministic prompt preprocessor | `E15_3_B1_STRUCTURED_LOSSLESS.md` | pytest 单元（manifest 可逆 round-trip + tokenize 后 diff + **禁止指令 byte compare**）；**正式 lossless PASS 需 4B greedy paired**（同 seed/temp=0：输出 token 一致 + 相同停止原因），mock/0.5b 仅开发信号 | **4B paired greedy 输出 token 完全一致 + 相同停止原因（12.10 条款）+ manifest 可逆/确定性 + 字节级保留** + 压缩率记录 | 无损未验证（仅 mock/0.5b）→ `HOLD_NOT_VALIDATED`；无损不成立 → **降级为有损，转 E15.5 走 12.12** | manifest 不可逆/输出不一致且拒绝降级 → REJECT |
| **E15.4** | C：context full/trim/extractive_summary + thread isolation | `E15_4_C_CONTEXT_POLICY.md` | pytest 纯函数（已存在 `test_context_policy.py` 68 例，阶段 0 实测 68 passed）+ long_life 对照 + 跨 thread 隔离 needle；**12.12 安全质量子集**（tool JSON 100%/system retention 100%/禁止指令 100%/无泄漏/无 crash-hang） | 安全质量子集全过 + 隔离测试过 | 策略无收益 → HOLD | 泄漏/工具失败 → REJECT |
| **E15.5** | B2：有损摘要离线 oracle（论文候选质量 oracle） | `E15_5_B2_SUMMARY_ORACLE.md` | **12.12 完整同字节 oracle**（tool JSON 100%、needle ≤2pp、retention 100%、bytes -25% 或 ctx/并发 +25%、aggregate score ≤2%） | 12.12 全项达标 | 收益不达标 → HOLD（保持离线） | 任一红线失败 → `REJECT_QUALITY_OR_SAFETY` |
| **E15.6** | 收口：完整验证 + 复现命令 + 提交 | `E15_FINAL_REPORT.md` | **根全量 pytest 全绿**（`cd benchmark && uv run pytest -q`，2026-08-08 阶段 0 实测 **413 passed**；以实测数为准）+ llama.cpp 相关测试全绿；两仓库 clean；**4B 门禁（M6）**：B1 lossless 若声明 PASS 必须已有 4B greedy paired 证据；12.22 唯一状态 | 按 12.22 判定 | 外部阻塞（12.15） | 按 12.22 REJECT 清单 |

> **E15.1B 与 E2.5 A2 的区分**：E2.5 的 A2 是 **prefix-branch 路由协议**（独立 RAM cache 语义），已在 E2.5 判定 **REJECT**（revisit p50 +938ms、prompt_processed ≈2×、退化与容量无关，`docs/E2_5_A2_DECISION_GATE_REPORT.md`）。**E15.1B 不重开 A2 路由**：只新增并发/paired workload 与直接指标（shared_cells/recompute/输出一致性），验证**现有 C1 前缀共享**在真实分支推理内存共享中的行为。

---

## 9. 判定规则（PASS / HOLD / REJECT 汇总）

- **PASS**：12.22 的 `PASS_KV_CACHE_OPTIMIZATION` 或阶段级 PASS 条件全部满足（含无损输出完全一致、12.13 不变量、12.12 有损门禁、收益达 12.11/12.12 数值门槛、原始数据完整、复现命令可执行）。
- **HOLD**：`HOLD_NO_MEASURABLE_GAIN`（正确性过收益不足）／`HOLD_THEORETICAL_GAIN_ONLY`（仅理论收益无实际证据）／`HOLD_KV_OPTIMIZATION_INCOMPLETE`（仅 12.15 外部阻塞）／**`HOLD_NOT_VALIDATED`（M6：无损候选仅 mock/0.5b 开发信号、未做 4B greedy paired 时，不得 PASS）**。
- **REJECT**：`REJECT_CORRECTNESS_OR_ISOLATION`（无损正确性/隔离失败）／`REJECT_QUALITY_OR_SAFETY`（有损红线：tool JSON、system prompt、禁止指令、泄漏、crash/hang）／`REJECT_KV_CACHE_OPTIMIZATION`（12.22 REJECT 清单）。
- **PARTIAL**：`PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION`（真实实现+正确性过、无正式收益）——E15 与 E14 一致时维持该语义，不把"正确但无收益"写成成功。
- **E15 与 E14 的关系**：E15 的判定不影响 E14 已冻结的发布基线与结论；E15 收口时在最终报告中显式声明两者状态。

---

## 10. 为何选该方案（决策依据）

1. **复用已验证基座、拒绝无收益复杂度**：C1/`seq_cp`/q8_0 是 E6–E14 唯一经真实路径验收的能力；A 线在既有 bitset 引用与共享源保护之上做**不变量验证**（不新增重复状态），radix 索引按 E9.3 数据 DEFER/REJECT。
2. **无损定义由输出决定**：B1 允许输入 token 改变，以 manifest 可逆/确定性 + 字节级保留 + greedy 输出一致（仅取 12.10 输出 token 条款）定义 lossless；无法保证即降级有损走 12.12。
3. **分支验证优先**：E15.1 以测试与 paired benchmark 先行，直接产出分支共享行为证据，成本最低、风险最小。
4. **工具数据外置走应用层**：ToolPayloadStore（确定性 projection + sha256/ref + thread-scoped + fail-fast）对应赛题"工具调用数据优化"方向，Driver 零改动、默认 off、可回滚；避免大 payload 进 prompt/KV。
5. **有损隔离**：B2 摘要只做离线 oracle，达 12.12 硬门禁才评估上线；R-KV/PartPrompt/SnapKV/LLMLingua 只作参考与对照，不直接实现。
6. **上下文优化走应用层**：LangGraph trim/remove/summarize 天然在状态层（不触 KV）；thread-scoped state 优先、不修改 server cache identity。
7. **默认关 + 可回滚**：每个候选独立开关、默认关闭；4B hybrid 由 capability gate + memory 默认 false 双重禁用；失败路径全部可回到现状行为。
8. **验收硬门禁沿用 E6**：不新建宽松标准；PASS/HOLD/REJECT 语义与 12.22 对齐，保证与 E7–E14 结论可比较。

---

## 11. 改动文件清单（本计划 final）

- 修改：`docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md`（本文，v4 final）
- 修改：`docs/E15_1_REFERENCE_TABLE.md`（引用表，v4 final：Lost in the Middle venue 标注 + SGLang/vLLM 来源状态）
- 修改：`docs/E15_ARTICLE_MCP_LOG.json`（article-mcp 唯一 E15 证据日志：v3）
- 明确不改：`docs/E6_*`~`docs/E14_*`（历史报告/raw）、`llama.cpp/`（代码）、`benchmark/results/` 其他既有数据、README/AGENTS.md（文档同步待后续授权）。
