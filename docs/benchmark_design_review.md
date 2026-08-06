# Benchmark 设计评审：面向智能体的内存管理系统

> 评审对象：`benchmark/agent_bench.py`（基线 & 优化对比共用脚本）与 llama.cpp 侧现有 KV Cache 能力。
> 评审目标：回答"当前 benchmark 能否评价 KV Cache 生命周期管理 / 分支 CoW / Context 压缩"三个优化方向，并给出可落地的 Benchmark 架构、workload、指标与实验方案。
> 本文件为科研设计分析，不包含代码实现；所有结论基于静态代码阅读（沙盒内禁止运行 server / benchmark）。

---

## 1. 项目理解

### 1.1 项目定位

面向赛题 14（面向智能体的内存管理系统，高校赛题）：在 llama.cpp 基础上扩展，针对智能体长生命周期推理（多轮对话、工具调用、多路径决策）优化 KV Cache 生命周期管理、分支共享（COW）、Prompt/Context 压缩，并用可复现的 Agent 工作流 Benchmark 做优化前后同硬件对比。

### 1.2 当前技术栈与版本

| 组件 | 版本/说明 |
|---|---|
| llama.cpp | 独立 git 仓库，worktree 分支，HEAD `b06aa774c`（约 2026-08 上游 master）；`llama-kv-cache-dsv4`、`server-mcp` 等新结构已合入 |
| 模型 | Qwen3.5-4B-Q4_K_M（GPU 正式）/ Qwen3.5-0.8B-Q4_K_M（CPU 开发）/ Qwen2.5-0.5B（最小验证）；GGUF 由 `models/download_models.sh` 拉取 |
| 交互方式 | llama-server 的 OpenAI 兼容 API（`http://127.0.0.1:8080/v1`），benchmark 侧为纯黑盒客户端 |
| benchmark | `benchmark/agent_bench.py`（Python 3.13 + uv），psutil 采样 RSS、pynvml 采样 GPU 显存 |

### 1.3 当前 benchmark 结构（agent_bench.py）

- **场景 1 multi_turn**：固定 prompt 池循环累积历史，测上下文逐轮增长下的缓存复用（cached_tokens）与延迟。
- **场景 2 tool_call**：文本 ACTION 协议模拟工具调用（规避小模型工具参数不稳定的问题），工具返回大段 JSON 回填上下文，模拟"上下文冗余膨胀"。
- **场景 3 branch**：同一公共前缀（张家界旅游方案）派生 A/B 两个分支，各分支续推多轮。
- **场景 4 long_life**：长生命周期 + 应用层截断（模拟真实 agent 的上下文窗口管理），用"秘密数字召回"作为任务成功率判据。
- 每次 `chat()` 返回 prompt/completion/total tokens、`cached_tokens`（`usage.prompt_tokens_details.cached_tokens`）、延迟、RSS、GPU 显存；`summarize()` 聚合成均值/峰值。
- 基线已归档：`benchmark/baseline/qwen3.5-4b_gpu_all_baseline.json`（GPU 三场景）、`qwen3.5-4b_longlife_40r_baseline.json`、`qwen3.5-0.8b_gpu_all_baseline.json`。

### 1.4 llama.cpp 侧现状（与三类优化直接相关）

基于对 `llama.cpp/src/` 与 `llama.cpp/tools/server/` 的静态调查（关键位置见括号）：

**KV Cache 生命周期管理 —— 底层能力已存在，server 侧部分使用**
- 序列操作 API 完备：`llama_memory_seq_rm`（删序列段）、`seq_keep`（只保留某序列）、`seq_cp`（复制）、`seq_add`/`seq_div`（位置平移/整除，KV shift 用）、`seq_pos_min/max`（`src/llama-kv-cache.cpp:379/447/539/566/616`）。
- server 内部已使用：slot 复用清理（`server-context.cpp:3444/3963`）、`n_keep`/`n_discard` 与 chunk 移位（`2955/3272`）、checkpoint 恢复（`3351-3387`）。
- `--cache-reuse N`：允许非前缀 chunk 的 KV 复用（slot 内移位重用），**默认 0 禁用**（`common/arg.cpp:3453`，部分预设 256）。
- defrag 机制已废弃（`--defrag-thold` DEPRECATED，`common/arg.cpp:2466`），新机制为每轮 `find_slot()/update()` 自动整理。
- **关键事实：llama.cpp 的 KV buffer 在启动时按 `n_ctx × n_layers × n_heads × head_dim` 预分配全部 cell**（`llama-kv-cache.cpp:289-302` 启动日志输出 `KV buffer size = N MiB (C cells, L layers, ...)`），运行期 cell 数量固定。**因此"淘汰/回收策略"在进程级显存上不体现**——显存收益只能来自"分配策略"（按需分配、分层存储、更小 KV 类型），或转为"缓存复用率/重算延迟"等代理收益。

**分支共享 / COW —— 没有真正的 COW**
- `TAG_KV_CACHE_SHARE_CELLS` TODO 标记遍布 `llama-kv-cache.cpp`（305、380、448、540、567、617、656、669、814、1094…），`llama-kv-cache.h:274` 注释明确"临时方案，待重构以支持两个 KV cache 共享 cell"。
- 现有近似能力：`ctx_other` / `shared_ptr<v_cells_impl>` 的元数据级共享（`llama-kv-cache.cpp:84-85`）；`seq_cp` 同 stream 内只复制 cell 元数据、跨 stream 必须真实复制 buffer（459-506）。
- server 的 `n_cmpl > 1`（一次请求生成 N 个补全）通过父槽→子槽 `copy_state_to`（`server-context.cpp:718-719`，内部 `seq_rm`+`seq_cp`）**真实复制** KV —— 这是优化前"分支代价"的天然对照基线，也是未来 COW 收益的直接度量对象。

**前缀缓存（Prompt caching）—— 已具备并对外报告**
- `cache_prompt = true` 默认开启（`server-task.h:53`）；`n_past = get_common_prefix(input_tokens)`（`server-context.cpp:3221`），即**精确前缀 token 匹配**。
- 通过 OpenAI 兼容 usage 报告：completion 的 `prompt_tokens_details.cached_tokens`（`server-task.cpp:398`）、chat 的 `input_tokens_details.cached_tokens`（`620/731/753`）。
- 这是当前 benchmark 唯一能拿到的"缓存复用"观测信号。

**可观测性 —— 运行时 KV 指标缺失（最大短板）**
- `GET /slots`：`server_slot::to_json`（`server-context.cpp:679-712`）含 `n_prompt_tokens` / `n_prompt_tokens_processed` / `n_prompt_tokens_cache` 等，**无 KV 内存字段**。
- `GET /metrics`（`server-context.cpp:4400-4497`）：仅 prompt/predicted tokens 速率、请求数、busy slot 数，**无任何 KV 占用指标**。
- 公开 API `llama_get_memory_breakdown(ctx)`（`llama-context.cpp:4181`）可返回模型+KV 按 buffer type 的内存分解，但 **server 未暴露到任何端点**。

---

## 2. Benchmark 目标定义

### 2.1 总目标

在**同硬件、同模型、同 workload** 前提下，为三类优化提供"优化前 vs 优化后"的**可复现、统计可信、能归因**的对比证据，并满足赛题评分维度（应用效果 40%：显存占用降低 + 延迟优化 + 任务成功率不下降）。

### 2.2 每类优化需要回答的评测问题

| 优化方向 | 评测问题（必须能被数据回答） | 保真约束 |
|---|---|---|
| KV Cache 生命周期管理 | 回收/淘汰/复用策略是否提高缓存复用率、降低重算 token 数与重算延迟、提高 KV 利用率（实际占用/预分配上限）、支撑更长的有效会话？ | 任务成功率不下降 |
| 分支 CoW | 同前缀多分支并存时，KV 存储共享率（共享 cell / 总 cell）、峰值 KV 内存、分支派生开销（对比 `n_cmpl` 真实复制）是否有收益？ | 分支内容正确性不下降 |
| Context 压缩 | 压缩后 prompt 规模、总处理 token、延迟、内存下降多少？压缩对关键信息（任务约束、记忆）的保真度如何？ | 压缩后任务成功率 ≥ 阈值 |

### 2.3 设计原则

1. **可归因**：每个指标必须能区分"优化生效/未生效"。当前进程级显存采样做不到（KV 预分配恒定），必须引入 KV 级观测。
2. **代理指标与直接指标分层**：直接指标（KV 占用、共享 cell 数）依赖 llama.cpp 侧新增可观测性；代理指标（cached_tokens、延迟、重算比例）今天就能采，先立起来。
3. **确定性优先**：prompt 模板、轮数、秘密数字全部参数化；重复实验取分布而非单点。
4. **与赛题对齐**：显存 / 延迟 / 任务成功率三类主指标缺一不可，最终报告直接映射到评分细则。

---

## 3. 当前 benchmark 的作用与不足

### 3.1 作用（保留价值）

- 覆盖赛题点名的三类典型 agent 场景 + 长生命周期场景，workload 设计（工具 JSON 膨胀、公共前缀分叉、秘密数字记忆）与赛题特征贴合。
- 已具备缓存命中观测（cached_tokens）与任务成功率判据雏形（long_life 秘密数字）。
- 固定 `enable_thinking: false` 保证可比；`chat()` 有 400 兜底，鲁棒。
- 基线已归档且命名可读，为前后对比提供了起点。

### 3.2 不足（按影响排序）

| # | 不足 | 证据/影响 | 优化后如何解决 |
|---|---|---|---|
| 1 | **指标与优化目标错配：进程级显存恒定** | baseline 中 GPU 显存全程 3220–3226 MB 几乎不变（KV 预分配），**KV 生命周期/COW 优化无法通过峰值显存体现** | KV 级指标（新端点）+ 代理指标（命中率/重算延迟） |
| 2 | **采样方式粗糙**：每轮请求完成后单点采样一次，错过 prefill/解码过程中的峰值；psutil 与 NVML 不同步；容器内 pynvml 降级为 null（已知正常） | 峰值可能被低估 | 后台采样线程（50–100ms 周期），采样全生命周期 |
| 3 | **无统计可信度**：单次运行、无预热、无重复、无方差 | multi_turn 首轮 4519ms（冷启动）拖高均值；branch 延迟 11–16.6s 波动大 | 预热轮 + N 次重复 + mean/std/CI 报告 |
| 4 | **任务成功率覆盖不足**：仅 long_life 有成功判据；tool_call 有 ACTION 正则但无成功率汇总；branch 无答案校验；multi_turn 纯闲聊 | 无法回答"优化是否损害任务效果"（赛题 40% 权重） | 每场景内置确定性判据（见 §5.1） |
| 5 | **无并发/多任务场景**：全部串行单请求 | 赛题明确"资源受限或多任务并发"；**COW 的"并存共享"只能在并发下观测** | 并发驱动（多 slot 并行请求 + n_cmpl 对照） |
| 6 | **branch 场景测的是前缀缓存复用，不是 COW**：A/B 串行请求，A 的 KV 先被复用后 B 再算 | 即使实现 COW，当前场景也测不出共享收益 | 并发分支 + 共享 cell 观测 |
| 7 | **Context 压缩无评测路径**：无压缩开关、无"压缩前/压缩后"对照、无保真度指标 | 压缩优化无法归因 | 新增 compression 场景（双跑对比） |
| 8 | **配置易错**：`--ctx-size` 需手动与 server 一致，无校验 | 误配导致结果作废 | 启动时探测 server 实际 ctx（`/props`），写入并校验 |
| 9 | **对比工具缺失**：baseline 靠人工翻 JSON | 效率低、易错 | 自动 diff 报告（§4.4） |

### 3.3 结论：当前 benchmark 能否评价三类优化？

| 优化方向 | 能否评价 | 说明 |
|---|---|---|
| KV Cache 生命周期管理 | **部分可评（间接）** | 可用 cached_tokens / 重算延迟 / 延迟曲线作代理指标（long_life 截断场景已能触发）；但**显存维度不可评**（预分配恒定、无 KV 级指标），且缺命中率随轮次衰减曲线 |
| 分支 CoW | **基本不可评** | 串行请求测不到存储共享；无共享 cell 观测；llama.cpp 本身尚无 COW 实现（只有 `n_cmpl` 真实复制可作对照基线） |
| Context 压缩 | **不可评** | 无压缩路径与对照；tool_call 场景提供了"冗余膨胀"workload 基础，可复用为压缩输入 |

**一句话总结**：当前 benchmark 是合格的"延迟 + token 计量"工具，但作为"内存优化"的评测工具存在结构性缺失——缺 KV 级可观测性、缺并发、缺统计、缺成功率判据、缺压缩对照。

---

## 4. 建议的 benchmark 架构

### 4.1 总体分层

```
┌─────────────────────────────────────────────────────────┐
│  harness（控制层）：场景编排、重复/预热、ctx 校验、配置落盘 │
├─────────────────────────────────────────────────────────┤
│  workload（场景生成器）：确定性 prompt 模板 + 规模参数 +    │
│        任务成功判据（每场景内置 evaluator）               │
├─────────────────────────────────────────────────────────┤
│  driver（请求层）：串行 / 并发（线程池，多 slot）、         │
│        n_cmpl 对照、压缩前/压缩后双跑                     │
├─────────────────────────────────────────────────────────┤
│  probe（采样层）：后台采样线程（RSS/GPU/KV 端点），         │
│        每请求记录 usage + timings + /slots 快照           │
├─────────────────────────────────────────────────────────┤
│  report（指标层）：四层指标聚合、baseline diff、           │
│        统计检验（mean±std / CI / 效应量）                 │
└─────────────────────────────────────────────────────────┘
```

### 4.2 与 llama.cpp 的接口约定

| 接口 | 用途 | 现状 |
|---|---|---|
| OpenAI 兼容 API（chat.completions） | 主 workload 驱动；`cached_tokens` 主缓存信号 | 已有 |
| `GET /props` | 探测 server 实际 `ctx-size`、模型名、backend | 已有（用于 ctx 校验） |
| `GET /slots` | 请求前后快照：`n_prompt_tokens_cache` 等（细粒度缓存状态） | 已有，字段名随版本变化需脚本兼容 |
| **KV 统计端点（新增，P1）** | 返回：KV 总占用、cell 总数/已用、共享 cell 数（COW 后）、per-layer 分解；数据源 `llama_get_memory_breakdown` + cell 状态 | **缺失，需在 llama.cpp 侧加**（属优化工作一部分，benchmark 预留字段契约） |
| 启动日志 | 解析 `llama_kv_cache: ... KV buffer size = N MiB (C cells, ...)`，拿到预分配上限 | 已有（可作为过渡方案） |

### 4.3 配置与可复现性

- 单文件 run config（JSON）记录：模型、GGUF 哈希、server 启动参数（`--ctx-size`/`--cache-reuse`/slots/`-ngl`）、benchmark 参数、重复次数、seed、环境（CPU/GPU、nvidia-smi 快照）。
- 输出物：`benchmark/results/` 临时结果 + 归档 `benchmark/baseline/<模型>_<环境>_<场景>_<说明>.json`（沿用现有命名规范）。
- 优化开关（如 `--cache-reuse 256`、COW on/off、compression on/off）必须是 run config 的一等字段，保证 A/B 仅差一个开关。

### 4.4 对比报告

- `--compare baseline.json results.json`：自动 diff 每个场景的四层指标，输出变化百分比 + 是否满足保真约束（成功率下降 ≤ 阈值），生成 markdown 报告直接进 `docs/`。

---

## 5. workload 设计

### 5.1 场景清单（保留 + 增强 + 新增）

每个场景必须携带：**规模参数、确定性、任务成功判据（evaluator）、专测优化维度**。

| 场景 | 状态 | 专测维度 | 成功判据（evaluator） |
|---|---|---|---|
| multi_turn | 增强 | KV 生命周期（复用率、延迟曲线） | 间隔抽查事实型问答（如"15+27=?"），回答含预期数字 |
| tool_call | 增强 | 生命周期 + 压缩输入 | 最终汇总覆盖全部 8 个订单 ID（正则/集合匹配） |
| branch（串行） | 保留 | 前缀缓存复用（对照） | 分支 A/B 回答含各自方案关键词（如"索道""袁家界"） |
| **branch_parallel（新增）** | 新增 | **分支 COW** | 同串行版；额外记录并发正确性（响应未串扰） |
| **n_cmpl 对照（新增）** | 新增 | **COW 对照基线** | 一次请求 `n_cmpl=2` 生成两个候选，成功判据同 branch |
| long_life | 增强 | 生命周期（回收压力） | 秘密数字召回（已有）+ 截断后关键信息存活率 |
| **compression（新增）** | 新增 | Context 压缩 | 压缩版任务成功率 ≥ 未压缩版 − 容差 |
| **concurrency/mixed（新增）** | 新增 | 多任务资源竞争 | 各客户端成功率 + 无 OOM |

### 5.2 关键 workload 设计点

1. **长生命周期压力变体（KV 回收）**：小 ctx（如 2048）+ 长时间运行，让 server 侧真正发生 KV 淘汰/移位（`--cache-reuse` 参与），记录每轮 `cached_tokens` 形成"命中率随轮次衰减曲线"——这是 KV 生命周期管理最直接的代理指标。
2. **并发分支（COW 场景）**：同一公共前缀，A/B/C 分支请求**并行**发送（多 slot）；COW 收益 = 这些槽位公共前缀部分的 cell 是否共享。需 server 侧新增"共享 cell 数"统计才能直接度量；过渡期用"并发下峰值 KV 占用 + 分支首 token 延迟"作代理。
3. **n_cmpl 对照**：`n_cmpl>1` 走父槽→子槽真实复制（`copy_state_to`），与未来 COW 实现构成天然前后对照——同一 workload 在优化前是"复制"，优化后是"共享+写时复制"。
4. **工具调用数据膨胀**：工具返回 JSON 规模参数化（订单数、SKU 数、物流条目数），为 Context 压缩提供可调冗余度输入；大结果变体把 prompt 占比拉到 80%+，放大压缩收益。
5. **压缩保真**：压缩场景用"秘密数字/关键约束"埋点，验证压缩不丢关键信息（与赛题"保证推理效果"直接对应）。
6. **规模-ctx 矩阵**：`(ctx 2048 / 4096 / 8192) × (rounds 20 / 40 / 80)`，选取能触发回收与不触发的两端，避免"ctx 太大永远不回收"导致场景失效（现有 README 已提示需小 ctx 触发）。

---

## 6. 指标设计

### 6.1 四层指标

**L1 任务层（保真约束，赛题 40% 中"任务成功率不下降"）**
- `task_success`（每场景 evaluator）、`tool_completion_rate`（工具链完整度）、`branch_consistency`、`secret_recall`（long_life）、`compression_fidelity`（压缩后成功率相对差）。

**L2 性能层**
- 延迟：`avg / p50 / p95 / max`（排除预热轮）；建议按请求内阶段拆分（prompt 处理 vs 解码），数据源 `timings`（`server-task.cpp:244-265` 的 prompt/predicted 字段）。
- 吞吐：`tokens/s`（解码吞吐）、`req/s`（并发场景）。

**L3 缓存层（生命周期/COW 的代理指标）**
- `cache_hit_rate = cached_tokens / prompt_tokens`（每轮 + 累计）。
- `cache_hit_rate_curve`：命中率随轮次变化（长生命周期衰减速度 = 回收策略质量）。
- `recompute_tokens` / `recompute_ratio`：未命中重算的 token 量（生命周期管理直接收益）。
- 分支场景：`branch_prefix_hit_rate`（公共前缀在分支请求中的命中）。
- COW 直接指标（P1 后）：`shared_cells / total_cells`（共享率）、`kv_peak_shared_bytes`。

**L4 资源层（显存，赛题 40% 中"显存占用降低"）**
- 进程级（宏观，保留）：`peak_rss_mb`、`peak_gpu_mb`（后台采样线程，非请求后单点）。
- **KV 级（关键新增）**：`kv_total_bytes`（启动日志/新端点）、`kv_used_cells / kv_total_cells`（利用率）、`kv_peak_used_bytes`；优化前后对比利用率上升 = 同样的显存服务更多上下文。
- 预分配上限（`KV buffer size`）与实现策略挂钩：若优化引入按需分配/分层存储，则 `kv_total_bytes` 本身下降。

### 6.2 指标-优化映射

| 优化 | 主指标（直接） | 代理指标（先行） | 保真约束 |
|---|---|---|---|
| KV 生命周期管理 | `kv_used_cells`、`kv_peak_used_bytes`、利用率 | `cache_hit_rate_curve`、`recompute_ratio`、p95 延迟 | `task_success` 不降 |
| 分支 COW | `shared_cells` 共享率、`kv_peak_used_bytes`（并发） | 分支首 token 延迟、并发峰值 GPU | `branch_consistency` 不降 |
| Context 压缩 | `prompt_tokens` 降幅、`kv_peak_used_bytes` | 总延迟、总处理 token | `compression_fidelity` ≥ 容差 |

### 6.3 统计规范

- 每配置重复 **N ≥ 3**（GPU 时间允许时 N=5），报告 `mean ± std` 与 95% CI。
- 预热 ≥ 1 轮（首个请求冷启动明显：baseline 首轮 4519ms vs 后续 ~350ms）。
- 同硬件固定：记录 CPU 型号/频率策略、GPU 型号/驱动/`nvidia-smi` 快照；容器内 GPU 显存降级为 null 属正常，不参与统计（沿用 AGENTS.md 约定）。
- 延迟类指标建议用中位数为主指标（对 LLM 随机波动鲁棒），均值仅作参考。

---

## 7. 实验方案

### 7.1 阶段划分

| 阶段 | 内容 | 产出 |
|---|---|---|
| **E0 基线重测** | 当前脚本 + 预热 + 重复 + 采样线程 + evaluator 补齐，全场景重跑，重做可信基线 | 新 baseline（4B GPU + 0.8B CPU） |
| **E1 KV 生命周期** | `--cache-reuse` 开/关 + long_life 压力变体 + KV 统计端点（P1 交付后） | 命中率衰减曲线、重算比、利用率对比 |
| **E2 分支 COW** | `n_cmpl` 对照 → 并发分支 → COW 实现后重跑；共享率/峰值 KV 对比 | COW 收益量化（对比真实复制的基线） |
| **E3 Context 压缩** | compression 场景双跑（压缩开/关），工具膨胀 workload 做输入 | 压缩率-保真度曲线 |

### 7.2 对照设计

- 同一台机器、同一 GGUF（记录哈希）、同一 workload、同一 seed 序列，**仅切换优化开关**；每轮对比 A/B 交叉执行两次（ABBA）以抵消环境漂移（温度漂移/后台负载）。
- 每类优化至少两个对照：`baseline(off) vs optimized(on)`，且 `optimized` 需同时报告"未触发优化路径"的回归项（如缓存全 miss 时的最坏延迟），防止优化以牺牲最坏情形为代价。

### 7.3 评测矩阵（正式 GPU 版）

| 模型 | ctx | 场景 | 重复 | 说明 |
|---|---|---|---|---|
| Qwen3.5-4B（GPU） | 2048 | long_life ×3 变体 + branch_parallel + n_cmpl | 3 | 正式对比主矩阵（回收压力） |
| Qwen3.5-4B（GPU） | 8192 | multi_turn + tool_call + compression | 3 | 常态长上下文（无回收） |
| Qwen3.5-0.8B（CPU） | 2048 | 同上一行关键场景子集 | 2 | 开发期快速回归 |

### 7.4 数据管理

- 运行期输出 `benchmark/results/`（不入库）；正式结果归档 `benchmark/baseline/<模型>_<环境>_<场景>_<说明>.json`；对比报告进 `docs/`（README 已预留 docs/ 目录）。
- 每份结果 JSON 自带完整 run config（§4.3），保证任何人可复现。

---

## 8. 后续开发计划

### P0 —— benchmark 增强（纯 Python，不依赖 llama.cpp 改动，先行）

1. 预热 + N 次重复 + 统计输出（mean/std/CI）。
2. 后台采样线程（RSS/GPU 周期采样，替代请求后单点）。
3. 每场景 evaluator（成功率判据）补齐。
4. 并发驱动（线程池多 slot）与 `n_cmpl` 对照场景。
5. `GET /props` ctx 校验 + run config 落盘。
6. `--compare` 自动 diff 报告工具。

### P1 —— llama.cpp 可观测性（为三类优化提供直接指标）

7. server 新增 KV 统计端点（或扩展 `/slots`）：cell 总数/已用/共享数、KV 占用字节（复用 `llama_get_memory_breakdown`）、per-layer 分解。
8. 过渡期：脚本解析 server 启动日志 `KV buffer size ... (C cells, ...)` 获取预分配上限。

### P2 —— 优化实现与配套评测

9. KV 生命周期管理：`--cache-reuse` 策略调优、动态回收/淘汰、按需分配或分层存储（使 `kv_total_bytes` 可降）→ E1。
10. 分支 COW：在 `TAG_KV_CACHE_SHARE_CELLS` 预留点实现 cell 引用计数/写时复制（或先做 cell 级共享）→ E2。
11. Context 压缩：system prompt/工具描述去重精简、长历史裁剪/摘要、压缩保真评测 → E3。

### P3 —— 正式评测与文档

12. 4B GPU 全套 E0–E3 正式跑测，归档 baseline 与对比报告。
13. 输出赛题要求的测试报告（优化前后同硬件对比表：显存/延迟/成功率），更新 `docs/` 与 README。

### 里程碑对应

- P0 完成 → benchmark 可信（对应赛题"可复现性/测试覆盖"）。
- P1 完成 → 内存收益可归因（对应赛题"显存占用降低"证据链）。
- P2 完成 → 三类优化均有量化收益（对应赛题"应用效果"）。
- P3 完成 → 文档/报告闭环（对应赛题"文档质量"）。

---

## 附：关键代码位置索引（评审依据）

| 内容 | 位置 |
|---|---|
| 当前 benchmark 脚本 | `benchmark/agent_bench.py` |
| 基线数据 | `benchmark/baseline/qwen3.5-4b_gpu_all_baseline.json` 等 |
| KV 序列操作 API | `src/llama-kv-cache.cpp:379-668`，`include/llama.h:724-792` |
| cache_prompt / 前缀命中 | `tools/server/server-context.cpp:3221`；`tools/server/server-task.h:53` |
| cached_tokens 报告 | `tools/server/server-task.cpp:398/620/731/753` |
| n_cmpl 父/子槽复制 | `tools/server/server-context.cpp:718-719, 3724, 4185` |
| COW 缺失（TODO） | `src/llama-kv-cache.cpp:305/380/448/...`，`src/llama-kv-cache.h:274` |
| KV buffer 预分配日志 | `src/llama-kv-cache.cpp:289-302` |
| /slots 字段 | `tools/server/server-context.cpp:679-712` |
| /metrics 无 KV 指标 | `tools/server/server-context.cpp:4400-4497` |
| --cache-reuse 默认 0 | `tools/server/../common/arg.cpp:3453-3459` |
