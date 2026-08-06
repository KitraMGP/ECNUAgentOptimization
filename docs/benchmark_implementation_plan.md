# Benchmark 实现计划：面向智能体的内存管理系统

> 前置文档：`docs/benchmark_design_review.md`（设计评审，含源码级事实与行号索引）。
> 本文档将评审结论转化为**可执行的三阶段工程计划**：第一阶段纯 Python 增强、第二阶段 llama.cpp 可观测性、第三阶段优化验证。
> 当前阶段仅输出本计划，不编写代码、不创建 Python 文件、不修改任何源码；执行需经人工确认。

---

# Part 1. 项目背景分析

## 1.1 当前 `benchmark/agent_bench.py` 的功能

单一脚本（约 350 行）通过 llama-server 的 OpenAI 兼容 API 做黑盒评测：

- **请求层**：`chat()` 封装 `chat.completions`，固定 `enable_thinking: false` 保证可比；内置 400 兜底（超 ctx 时丢弃最早的非 system 消息重试，最多 3 次）。
- **观测层**：每次请求记录 prompt/completion/total tokens、`cached_tokens`（`usage.prompt_tokens_details.cached_tokens`）、wall-clock 延迟、请求后单点 RSS（psutil）与 GPU 显存（pynvml）。
- **聚合层**：`summarize()` 输出 token 总量、平均/最大延迟、峰值 RSS/GPU。
- **运行层**：`--scenario` 选择场景，结果落盘 `benchmark/results/bench_<时间戳>.json`，正式基线手工归档到 `benchmark/baseline/`。

## 1.2 当前已覆盖的 workload

| 场景 | 模拟对象 | 专测维度 | 任务判据 |
|---|---|---|---|
| multi_turn | 多轮对话（prompt 池循环累积） | 上下文增长、前缀缓存复用 | 无 |
| tool_call | 工具调用（文本 ACTION 协议 + JSON 回填） | 上下文冗余膨胀 | 无（有 ACTION 正则但无成功率汇总） |
| branch | 多路径决策（张家界公共前缀派生 A/B 分支） | 前缀缓存复用（串行） | 无 |
| long_life | 长生命周期 + 应用层截断 | KV 回收压力、缓存命中衰减 | 秘密数字召回（唯一有判据的场景） |

## 1.3 llama.cpp 当前 KV Cache 能力（源码级事实，详见评审文档）

| 能力 | 现状 | 关键位置 |
|---|---|---|
| 序列级 KV 操作 | 完备：`seq_rm` / `seq_keep` / `seq_cp` / `seq_add` / `seq_div` | `src/llama-kv-cache.cpp:379-668` |
| 前缀缓存 | `cache_prompt=true` 默认开，`get_common_prefix` 精确前缀匹配 | `server-context.cpp:3221` |
| 缓存复用扩展 | `--cache-reuse N`（chunk 级 KV 移位复用），**默认 0 禁用** | `common/arg.cpp:3453` |
| COW | **无真正 COW**，`TAG_KV_CACHE_SHARE_CELLS` TODO 遍布；`n_cmpl>1` 父→子槽为**真实复制**（天然对照基线） | `llama-kv-cache.cpp:305...`；`server-context.cpp:718-719` |
| KV 分配 | 启动时按 `n_ctx × layers × kv_heads × head_dim` **预分配全部 cell**，运行期固定 | `llama-kv-cache.cpp:289-302`（启动日志） |
| 对外指标 | `cached_tokens` 已暴露；**KV 内存/共享 cell 无任何运行时端点** | `server-task.cpp:398`；`/slots` 无 KV 字段 |
| 额外可用信号 | 响应 `timings` 对象含 `prompt_n`（实处理）/ `cache_n`（命中），且 **`prompt_n + cache_n == prompt_tokens`**；含 `prompt_ms` / `predicted_ms` / 吞吐 | `server-task.cpp:244-265`；`tests/unit/test_completion.py:661` |

## 1.4 当前 benchmark 存在的问题（按影响排序）

1. **指标与优化目标错配**：KV 预分配使进程级显存恒定（baseline 中 3220–3226 MB 几乎不变），KV 生命周期 / COW 的收益在峰值显存上**无法体现**。
2. **采样粗糙**：请求完成后单点采样，错过 prefill/解码峰值；无后台采样线程。
3. **无统计可信度**：单次运行、无预热（首轮冷启动 4519ms vs 后续 ~350ms）、无重复、无方差。
4. **任务成功率覆盖不足**：仅 long_life 有判据，无法回答"优化是否损害任务效果"。
5. **无并发/多任务场景**：全部串行；COW 的"并存共享"只能在并发下观测。
6. **branch 测的是前缀缓存复用，不是 COW**：A/B 串行请求，测不出存储共享。
7. **Context 压缩无评测路径**：无压缩开关、无前后对照、无保真度指标。
8. **配置易错**：`--ctx-size` 需手动与 server 一致，无校验。
9. **对比工具缺失**：baseline 靠人工翻 JSON。

## 1.5 后续 Benchmark 系统必须满足的目标

1. **可归因**：每类优化都有"能区分生效/未生效"的直接指标（KV 级）或代理指标（缓存/延迟级）。
2. **统计可信**：预热 + N≥5 重复 + mean/std/CI/p50/p95。
3. **保真约束**：任务成功率作为"优化未损害 agent 能力"的硬门槛，随每个配置报告。
4. **可消融**：三类优化独立开关，支持 5 配置矩阵（off / 单开 ×3 / all）。
5. **可复现**：完整 run config（模型哈希、llama.cpp commit、server 参数、ctx、seed、temperature）随结果落盘。
6. **可扩展**：workload 版本化、冻结制度，新增场景有规范流程。
7. **可观测**：llama.cpp 侧最终通过**内部接口**暴露 KV 统计（日志解析仅过渡）。

---

# Part 2. Benchmark 设计

## 2.1 Benchmark 总体架构

```
harness（控制层）
   │  解析 CLI / 读取 run config / 校验 server（/props）/
   │  编排场景×重复×预热 / 启动 probe / 调用 report / 落盘
   ▼
workload（场景定义层）
   │  确定性 prompt 序列生成器 + 规模参数 + evaluator 判据
   │  （纯数据描述，版本化 + hash）
   ▼
driver（请求层）
   │  消息序列 → server 请求（OpenAI 兼容 API）
   │  串行 / 并发（线程池多 slot）/ n_cmpl 对照 / 压缩前后双跑
   │  请求状态管理（历史累积、400 兜底且记录事件）
   ▼
probe（采样层）
   │  后台采样线程（RSS / GPU 周期采样，50–100ms）
   │  每请求快照：usage + timings + /slots；KV 端点低频采样（≥1s）
   ▼
metrics（指标层）
   │  四层指标计算：原始数据 → task / perf / cache / resource 指标
   │  曲线（cache hit rate over rounds）、汇总（mean/std/p50/p95）
   ▼
report（报告层）
   │  对比 baseline（自动 diff）、保真约束判定（pass/fail）
   │  生成 markdown 报告 → docs/；结果 JSON → results/ 或归档 baseline/
```

各层职责与边界：

| 层 | 职责 | 明确不做 |
|---|---|---|
| **harness** | 实验编排、配置管理、重复/预热循环、错误处理 | 不生成 prompt，不定义指标 |
| **workload** | 唯一 prompt/evaluator 来源；确定性（同 seed 同输出） | 不关心 server 细节 |
| **driver** | 协议翻译与并发调度；记录每请求原始响应 | 不做统计，不做采样 |
| **probe** | 时间对齐的资源采样与请求快照 | 不解释指标含义 |
| **metrics** | 纯函数：原始数据 → 指标；可单测 | 不发请求 |
| **report** | 统计聚合、diff、判定、归档 | 不改 workload |

## 2.2 Benchmark 定位约束

**评测对象是"推理系统的 KV / 内存 / 延迟效率"，不是"Agent 的智能能力"。**

- **主要评价**：KV Cache 利用效率（命中率、重算比、共享率、利用率）、内存占用（KV 级 + 进程级）、延迟（p50/p95、prefill/decode 拆分）、长生命周期推理性能（命中率随轮次衰减曲线、回收压力下的延迟稳定性）。
- **任务成功率仅作保真约束**：作为 pass/fail 门槛（如"优化后成功率 ≥ 基线 − 2pp"），进入每个配置的报告，但不作为优化之间的排名依据。
- **禁止**：
  - 主观人工评分；
  - LLM-as-a-Judge 作为主指标（若未来需要语义判据，只允许作为**辅助日志**，不计入主指标）；
  - 开放式问答评价（无确定答案的 prompt 不进入正式 workload）。
- **优先**：确定性任务（事实问答、数字/ID 召回、格式约束）、自动 evaluator（正则、集合匹配、数值比较）、可重复实验（temperature=0、seed 记录、同硬件）。

## 2.3 Baseline 设计（三级）

| 级别 | 定义 | server 侧配置 | 作用 |
|---|---|---|---|
| **Baseline-0** | 原始 llama.cpp server（默认参数） | `cache_prompt` 默认开（无法关闭）；`--cache-reuse 0`（默认）；不做任何调优 | 系统原始能力下限 |
| **Baseline-1** | 原始 llama.cpp + **已有**缓存机制全开 | 显式 `--cache-reuse N`（如 256）+ 合理的 slots/并行参数 | 已有机制收益上限；**本项目所有优化的比较对象** |
| **Baseline-2** | 本项目优化版本（KV lifecycle / COW / compression 之一或组合） | 对应优化开关 | 优化收益 = Baseline-2 − Baseline-1 |

**对照规则（每个实验必须声明比较对象）**：
- 已有机制收益：`Baseline-0 vs Baseline-1`；
- 本项目收益：`Baseline-1 vs Baseline-2`（消融各配置）；
- 端到端总收益：`Baseline-0 vs Baseline-2(all)`。
- 任何结论必须同时给出"比较对象"与"是否满足保真约束"，否则报告视为无效。

## 2.4 实验消融设计

三类优化必须独立可开关，benchmark CLI 提供（示例）：

```
--enable-kv-lifecycle     # E1: KV Cache 生命周期管理
--enable-cow              # E2: 分支 Copy-on-Write
--enable-compression      # E3: Context 压缩
```

- 开关在 benchmark 侧是**实验配置声明**（记入 run config、控制 server 启动参数与 workload 变体选择）；真正生效于 llama.cpp 侧（对应 server 启动参数或请求级参数，如 `--cache-reuse` 属 Baseline-1，COW/压缩为新增参数）。
- 消融矩阵（5 配置）：`none（Baseline-1）` / `kv-lifecycle only` / `cow only` / `compression only` / `all`。
- 每配置与 Baseline-0 并列执行（ABBA 顺序抵消环境漂移）。
- 每个优化配置除主指标外，必须报告"未触发优化路径"的回归项（如缓存全 miss 时的最坏延迟），防止优化以牺牲最坏情形为代价。

## 2.5 Workload 设计

设计原则：每个 workload = **确定性生成器（seed 参数化）+ 规模参数 + 自动 evaluator**；输出长度统一约束 `max_tokens`（默认 256，避免 completion 长度差异污染延迟对比）；规模参数在冻结前校准到"目标 ctx 下不触发 400 兜底"。

### 2.5.1 Long Horizon Agent（专测：KV 生命周期管理）

- **模拟**：长期 coding agent 生命周期——需求分析 → 代码设计 → debug → 测试 → 优化，各阶段固定轮数，阶段间插入大段中间产物（代码块、测试输出）模拟上下文累积。
- **规模参数**：`ctx ∈ {2K, 4K, 8K, 16K, 32K}`；`rounds` 随 ctx 缩放（保证发生 ≥1 次"接近/超过"窗口的事件）。
- **埋点**：早期轮次注入事实（如"项目代号 X-2026、端口 8899"），末期查询召回。
- **evaluator**：末期事实召回 + 每阶段格式断言（输出含指定结构）。
- **指标**：cached_tokens / cache hit rate 曲线 / recompute tokens / latency（p50/p95）/ KV 利用率。
- **注意**：32K 档的 KV 内存需在 8GB 显存内（必要时 `--cache-type-k/v q8_0` 或 CPU offload 部分层），E0 阶段先实测 `KV buffer size` 日志确认可行性。

### 2.5.2 Branch Reasoning / Multi Path Agent（专测：CoW）

- **模拟**：`Task → 分支 A/B/C`（同 system prompt + task description + 公共推理前缀，各分支独立续推）。
- **两种执行模式**：
  - 并发模式（多 slot 并行请求，测共享）：A/B/C 并行发出；
  - `n_cmpl` 对照模式（`n_cmpl=3`，走现有父→子槽**真实复制**）：作为"传统 KV 复制"基线，与优化后的 CoW 共享做直接对比。
- **evaluator**：分支一致性（各分支回答含各自约束关键词，且不串扰——A 的约束不出现在 B 的回答）。
- **指标**：fork latency（分支首 token 延迟）、shared KV ratio（`shared_cells / total_cells`，P1 端点）、peak KV memory、branch correctness。
- **对照**：`n_cmpl 复制基线 vs 并发 CoW 共享`，同一 workload 两种执行模式。

### 2.5.3 Tool-use Agent（专测：上下文膨胀 / 压缩收益输入）

- **模拟**：agent 依次调用 search → database → compiler → test 四类工具；工具返回规模参数化的 JSON（记录条数、字段数可调），回填上下文。
- **规模参数**：工具结果大小倍率（×1 / ×4 / ×16），把 prompt 占比拉高到 80%+，制造强冗余。
- **evaluator**：最终汇总必须覆盖全部关键条目（集合匹配），防"偷懒跳过"。
- **指标**：context 增长曲线、memory 压力（KV 利用率）、压缩场景下（2.5.4）的收益输入。
- **双用途**：单独跑 = 膨胀压力测试；与 compression 组合 = 压缩收益测试。

### 2.5.4 Context Compression（专测：Prompt 压缩）

- **比较**：`compression OFF vs compression ON`，同一 workload（推荐复用 2.5.1/2.5.3 的长上下文输入）。
- **压缩策略注入点**（实现阶段确定）：server 侧压缩（请求无感知，最干净）或请求侧压缩（workload 提供压缩前/后两套 prompt，作为 P2 前的过渡评测）。
- **埋点保真**：压缩前后都注入同一组关键事实，压缩版必须保留。
- **evaluator**：`task_fidelity = 压缩版成功率 / 未压缩版成功率`，要求 ≥ 0.95（可配置容差）。
- **指标**：prompt token 减少比例、prefill latency（`timings.prompt_ms`）、memory、task fidelity。

## 2.6 指标体系设计

每个指标标注：数据来源 / 计算方式 / 意义。

### Task Layer（保真约束，非排名指标）

| 指标 | 数据来源 | 计算方式 | 意义 |
|---|---|---|---|
| `task_success` | workload evaluator | 每场景确定性判定（正则/集合/数值），0/1 | 任务整体完成与否 |
| `secret_recall` | long-life 埋点 | 末期回答是否含注入事实 | 早期信息在生命周期/压缩后的存活率 |
| `tool_completion` | tool-use evaluator | 汇总覆盖条目数 / 总条目数 | 工具链路完整性 |
| `branch_consistency` | branch evaluator | 各分支约束关键词命中且无串扰 | 分支正确性与隔离性 |

### Performance Layer

| 指标 | 数据来源 | 计算方式 | 意义 |
|---|---|---|---|
| `p50 / p95 latency` | driver 每请求 wall-clock（排除预热） | 请求延迟分位数；p50 为主指标（对 LLM 波动鲁棒） | 用户感知延迟 |
| `prefill/decode 拆分` | 响应 `timings.prompt_ms / predicted_ms` | 直接读取（OAI 响应或 `timings_per_token: true`，E0 验证暴露方式） | 归因：缓存命中收益主要落在 prefill |
| `throughput` | `timings.prompt_per_second / predicted_per_second` | 直接读取；并发场景另算 `req/s` | 系统吞吐能力 |

### Cache Layer（生命周期 / COW 的核心代理层）

| 指标 | 数据来源 | 计算方式 | 意义 |
|---|---|---|---|
| `cached_tokens` | `usage.prompt_tokens_details.cached_tokens`（或 `timings.cache_n`，两者等价） | 每请求 + 累计 | 缓存复用绝对量 |
| `cache_hit_rate` | 上述 / `prompt_tokens` | `cached / prompt`；每轮 + 累计 | 复用效率 |
| `cache_hit_rate_curve` | 逐轮 `cache_hit_rate` | 按轮次画曲线，拟合衰减斜率 | **生命周期管理直接代理**：回收策略质量 = 曲线衰减速度 |
| `recompute_ratio` | `timings.prompt_n` / `prompt_tokens` | `prompt_n / (prompt_n + cache_n)`（= 1 − hit_rate） | 重算开销 |
| `shared_cells` / `shared_ratio` | **P1 新增 KV 端点** | `shared_cells / total_cells`（COW 生效时） | CoW 存储共享的直接证据 |

### Resource Layer

| 指标 | 数据来源 | 计算方式 | 意义 |
|---|---|---|---|
| `peak_rss_mb` | 后台采样线程（psutil，50–100ms 周期） | 采样序列最大值 | 进程级内存上限 |
| `peak_gpu_mb` | 后台采样线程（pynvml；容器内降级为 null 属正常） | 采样序列最大值 | 进程级显存上限（宏观参考） |
| `kv_total_bytes` | 启动日志 `KV buffer size`（过渡）→ **P1 KV 端点**（正式） | 直接读取 | KV 预分配上限；按需分配优化后本身下降 |
| `kv_used_cells / kv_total_cells` | P1 KV 端点 | 当前使用 / 预分配 | **KV 利用率**：同样显存服务更多上下文 |
| `kv_peak_used_bytes` | P1 KV 端点（请求边界采样） | 请求生命周期内 used_bytes 峰值 | 生命周期/COW 的直接内存收益 |

### 指标-优化映射（回归表）

| 优化 | 直接指标 | 代理指标 | 保真约束 |
|---|---|---|---|
| KV lifecycle | `kv_used_cells`、`kv_peak_used_bytes`、利用率 | `cache_hit_rate_curve` 衰减、`recompute_ratio`、p95 | `task_success` 不降 |
| CoW | `shared_ratio`、`kv_peak_used_bytes`（并发） | fork latency、并发峰值 GPU | `branch_consistency` 不降 |
| Compression | `prompt_tokens` 降幅、`kv_peak_used_bytes` | 总延迟、prefill 延迟 | `task_fidelity ≥ 0.95` |

---

# Part 3. 工程实现规划

## 3.1 第一阶段：纯 Python Benchmark 增强（不依赖 llama.cpp 改动）

目标：让当前脚本升级为统计可信、可消融、可对比的 harness。模块化规划（`benchmark/` 下新增包，**不重构** `agent_bench.py` 既有场景逻辑，以增量方式引入）：

| 模块 | 交付内容 |
|---|---|
| `config` | run config schema（模型/commit/seed/temperature/ctx/优化开关/重复次数/输出路径）；`/props` 探测与 ctx 校验；`nvidia-smi` 快照 |
| `harness` | 场景编排：预热（≥1 轮）→ 正式 N 次（默认 N=5）→ ABBA 顺序；错误恢复与超时 |
| `workloads` | 4 场景 + 规模参数 + evaluator 的定义与版本化（v1.0 + 内容 hash） |
| `driver` | 串行/并发（线程池）/ `n_cmpl` 对照；`timings_per_token` 请求参数；400 兜底参数化并记录事件 |
| `probe` | 后台采样线程（RSS/GPU 50–100ms）；每请求 usage+timings+`/slots` 快照；KV 端点低频采样（≥1s） |
| `metrics` | 四层指标纯函数（§2.6），含曲线计算；可单测 |
| `report` | 统计聚合（mean/std/CI/p50/p95）；`--compare <baseline> <result>` 自动 diff + 保真判定 + markdown 报告 |
| `tests` | 无 GPU 可跑的单元测试：workload 确定性、evaluator 判据、metrics 数值、report diff（用 mock OpenAI 兼容 server 返回固定 usage/timings） |

验证方式（无需 GPU）：`pytest` 全绿 + mock server 端到端冒烟；随后在真实 server 上以 0.8B CPU 跑通全套（开发回归集）。

## 3.2 第二阶段：llama.cpp 可观测性增强

目标：KV 级指标从"日志解析"升级为"内部接口"。

**新增公开 C API**（`include/llama.h` + `src/llama-context.cpp`，数据源 `llama-kv-cache.cpp` 既有 `size_k_bytes / size_v_bytes / cells / used_cells` 与 `llama_get_memory_breakdown`）：

```c
struct llama_kv_stats {
    size_t total_bytes;        // 全部 KV buffer 占用（预分配）
    size_t used_bytes;         // 当前实际使用（有效 cell）
    int64_t total_cells;       // 预分配 cell 数
    int64_t used_cells;        // 当前使用 cell 数
    int64_t shared_cells;      // 被多序列共享的 cell 数（COW 后）
    // per-layer: bytes 与 used_cells
};
void llama_kv_cache_get_stats(const struct llama_context * ctx, struct llama_kv_stats * stats);
```

**新增 HTTP 端点** `GET /kv-stats`（`server.cpp` 注册路由，handler 调上述 API，返回 JSON）：
- 字段：`total_bytes / used_bytes / total_cells / used_cells / shared_cells / per_layer[]`（每层 bytes、used_cells）。
- 约束：只读、低频（benchmark 侧采样 ≥1s），不引入锁竞争（读已提交状态即可）。
- **过渡方案**：P1 未交付前，benchmark 解析 server 启动日志 `KV buffer size = ... (C cells, ...)` 得到 `total_bytes/total_cells`；`used_cells` 用 `cached_tokens` 与 `/slots` 字段近似。日志解析仅作过渡，最终以端点为准。

交付验证：curl `/kv-stats` 字段正确；`tests/unit` 用例（加载小模型断言 total_cells 与启动日志一致、请求后 used_cells 单调、并发分支后 shared_cells 符合预期）。

## 3.3 第三阶段：优化验证（对应 E1 / E2 / E3）

| 实验 | 优化 | 比较对象 | 主指标 | 前置依赖 |
|---|---|---|---|---|
| **E1** | KV 生命周期管理 | Baseline-1 vs `--enable-kv-lifecycle` | `cache_hit_rate_curve` 衰减、`kv_used_cells`、`kv_peak_used_bytes`、p95 | P1 端点 |
| **E2** | 分支 CoW | `n_cmpl` 真实复制基线 vs `--enable-cow`（并发分支） | `shared_ratio`、fork latency、`kv_peak_used_bytes`、`branch_consistency` | P1 端点（shared_cells）+ 并发 driver |
| **E3** | Context 压缩 | `compression OFF vs ON` | prompt 降幅、prefill 延迟、`task_fidelity` | compression 实现（server 侧或请求侧） |

执行顺序与门禁：
1. E0 先跑通（第一阶段 harness + 全场景重测，重生成可信 baseline）；
2. 每项优化单独交付时即跑对应 E 实验，禁止一次性合入后再测；
3. 最终跑 `all` 配置（组合效应：协同或冲突都要报告）；
4. 全部结果归档 + 生成对比报告（docs/），映射赛题评分维度（显存/延迟/成功率）。

---

# Part 4. 工程约束（后续实现必须遵守）

## 4.1 workload 冻结制度（不允许修改 Benchmark 迎合优化结果）

- workload 定义**版本化**（`v1.0` + 内容 hash），随结果 JSON 落盘；任何运行都可回溯到确切 workload。
- workload 一旦冻结，**禁止因实验效果不好而修改**；确需新增/变更时走流程：
  1. 记录变更原因（docs/ 变更日志）；
  2. 更新 workload 版本号与本文档；
  3. **重新生成 baseline**（旧 baseline 归档标记 `superseded`，不删除）。
- 新增 workload 必须同样携带：确定性生成器、规模参数、自动 evaluator、版本与 hash。

## 4.2 实验可复现

每次实验的 run config 必须完整记录并随结果落盘：

- 模型路径 + **GGUF hash**；
- **llama.cpp commit**（含是否含本地补丁，本地补丁记录 diff 摘要）；
- server 启动参数（`--ctx-size`、`--cache-reuse`、slots、`--cache-type-k/v`、`-ngl` 等）；
- benchmark 参数（场景、规模参数、优化开关、重复次数）；
- **seed** 与**温度参数**（推理默认 `temperature=0`，作为全局默认值写入 config）；
- 环境快照（CPU 型号/频率策略、GPU 型号/驱动、`nvidia-smi` 输出、容器标记）。

## 4.3 性能实验规范

- 每次实验必须：`warmup ≥ 1` 轮（不计入统计）；
- 正式运行 `N ≥ 5`（GPU 时间不允许时最低 N=3，但必须在报告中声明）；
- 报告必须包含：`mean`、`std`、`p50`、`p95`（延迟类以 p50 为主指标）；
- 同硬件固定；ABBA 交叉执行抵消环境漂移；并发场景报告吞吐与成功率双指标。

## 4.4 代码规范

- 模块化：按 §3.1 分层，每层职责单一、接口清晰；
- 新模块必须有 README + 运行示例 + 单元测试（测试不得依赖 GPU/真实 server，用 mock）；
- **禁止大规模重构已有代码**（`agent_bench.py` 既有场景逻辑增量接入，不重写）；
- **禁止删除已有 baseline**（`benchmark/baseline/` 只增不改，失效基线标记 superseded）；
- 新增代码遵循根仓库 AGENTS.md 约定（沙盒内不自动运行 server/benchmark、提交需授权、commit message 格式）。

---

# 附录 A. 关键源码位置索引

| 内容 | 位置 |
|---|---|
| 当前 benchmark | `benchmark/agent_bench.py` |
| 设计评审（本计划前置） | `docs/benchmark_design_review.md` |
| KV 序列操作 API | `src/llama-kv-cache.cpp:379-668`；`include/llama.h:724-792` |
| KV buffer 预分配日志 | `src/llama-kv-cache.cpp:289-302` |
| KV 内部计量 | `src/llama-kv-cache.cpp:1806-1833`（size_k/v_bytes、total_size） |
| 前缀命中 | `tools/server/server-context.cpp:3221`（get_common_prefix） |
| timings 组装（prompt_n/cache_n/ms/吞吐） | `tools/server/server-context.cpp:546-566`；`server-task.cpp:244-265` |
| cached_tokens 报告 | `tools/server/server-task.cpp:398/620/731/753` |
| n_cmpl 父→子槽复制 | `tools/server/server-context.cpp:718-719, 3724, 4185` |
| COW 缺失 TODO | `src/llama-kv-cache.cpp:305/380/448/...`；`src/llama-kv-cache.h:274` |
| /props、/slots 路由 | `tools/server/server.cpp:236-237, 272-273` |
| memory breakdown API（P1 数据源） | `src/llama-context.cpp:4181`（`llama_get_memory_breakdown`） |
| `--cache-reuse` 默认 0 | `tools/server/../common/arg.cpp:3453-3459` |

# 附录 B. 开放问题（需在 E0 阶段确认）

1. **timings 在 OAI chat/completions 的暴露方式**：非流式最终响应是否含 `timings`？还是必须请求 `timings_per_token: true`？（有单测佐证流式路径，E0 实测确认非流式路径）
2. **32K ctx 可行性**：Qwen3.5-4B 在 8GB 显存下 32K ctx 的 KV 配置（是否需 `--cache-type-k/v q8_0` 或分层 offload），以启动日志实测为准。
3. **`--cache-reuse` 的合理默认值**：Baseline-1 的 N 取值（256？）需在 E0 用 0.8B 扫参确定，避免 Baseline-1 被低估/高估。
4. **并发 driver 的 slot 数**：server `--parallel` 与 workload 并发度的匹配关系，E0 用 0.8B 验证无请求串扰。
