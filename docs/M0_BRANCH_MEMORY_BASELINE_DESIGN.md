# M0：真实智能体多路径决策 workload + 分支内存基线（技术调研与设计）

> 状态：**调研完成 + 详细设计（DESIGN ONLY）；未实现 workload 代码、未运行正式 benchmark**。
> 日期：2026-08-09 ｜ 对应路线：M0（阶段 0 通过后第一条后续路线）｜ 实施建议：**GO**（见 §8）。
> 修订：2026-08-09 v2（审查修复）——矩阵/预算/层数/架构/接口合同/回收观测修正，见各节。
> 约束：本阶段只调研与设计，不实现代码、不运行长 GPU benchmark；不改 E6–E15 历史 raw/结论。
> 探针（§1.3/§7）：仅"启动 server → 读取内存分配日志与 /metrics/kv → 退出"，未做任何推理请求。

---

## 1. 现状调查（证据）

### 1.1 benchmark 侧（根仓库 benchmark/）

**Workload 抽象**（`benchmark/framework/workload.py`）：
- `WorkloadSpec`（`name/params/prompts/expected/meta` + `fingerprint()`，`:25-42`）；
- `Workload` 抽象基类：`generate(params) -> WorkloadSpec`、`run(driver, spec) -> {"rows":[...], "meta":{...}}`、`evaluate(results, spec)`（`:44-80`）；
- 注册表 `_REGISTRY` + `@register`（实例化注册，重名 ValueError），`get_workload/all_workloads`（`:83-103`）。

**四场景**（`benchmark/workload/__init__.py:5` 导入 branch/long_life/multi_turn/tool_call）：
- `multi_turn.py`：固定 8 条中文 prompt 池循环 `rounds` 次累积 history（`:17-61`）；
- `tool_call.py`：文本 ACTION 协议 + MOCK_TOOLS 大段 JSON 回填；E15.2 `ToolPayloadRun` 集成（externalized put→projection→resolve 拦截；off 默认旧行为）（`:33-68, 258-301`）；
- `branch.py`（见 §2 详细分析）：固定 A/B 分支问题 + 固定"继续完善方案X"循环（`:15-65`）；
- `long_life.py`：多轮对话+工具+应用层截断，`est>budget` 丢早期消息，主指标 `state_retention_rate`（`:48-157`）。
- `branch_pressure.py`（E2.5）：双分支 X/Y、长共享前缀、固定交替 8 请求 `SEQUENCE`、JSON evaluator（`:319-405`）；**不在 `workload/__init__.py` 注册列表**，仅显式 `import workload.branch_pressure` 时才触发 `@register`（`tests/test_branch_pressure.py` 中 `get_workload("branch_pressure")` 前必须先 import 该模块）。

**runner**（`benchmark/runner/e15_branch_concurrent.py`，E15.1）：
- CLI：`--server-bin --model --port(8091) --ctx-size(2048) --parallel(5) --cache-ram(0) --min-lcp(64) --branches(2|3) --warmup(2) --reps(5) --out --tmp-dir`，前置校验 `parallel>=branches+2`、off 模式 KV 峰值<ctx（`:838-873`）；固定 `SEED=42/TEMPERATURE=0.0/N_PREDICT=8`（`:66-70`）；
- prompt：`PREFIX_CORE`（英文童话）+ `BRANCHES[b0..b3]` 固定后缀 + 专属 canary，**纯字符串拼接**，分支首 token 互异是硬编码设计（`:74-114`，测试 `test_branch_first_tokens_differ`）；
- 并发：`threading.Barrier` + `ThreadPoolExecutor`，target 各自 `id_slot=i+1`（`:686-703`）；source 预热 slot 0 后 idle（`:672-675`）；
- 门禁 `gate_verdict` G0–G9（纯函数，`:292-515`）：G0 数据完整 / G1 on/off 输出一致 / G2 shared_cells>0 / G3 recompute 降≥25% / G4 off 无共享 / G5 canary 无污染 / G6 部分前缀保护 / G7 清空回基线 / G8 shutdown clean / G9 physical_sharing==false 契约；
- server 生命周期：`start_server` Popen + `/health` 轮询、`stop_server` SIGTERM（`:548-599`）；
- 请求走**原生 `/completion`**：`{prompt, n_predict, temperature, seed, cache_prompt, id_slot, return_tokens}`（`:602-608`）；
- 落盘 JSON：`{"meta", "modes":{"off"/"on":{"start","replicates","stop"}}, "gates", "verdict", "notes"}`（`:795-831`）。

**driver**（`benchmark/framework/driver.py`）：OpenAI SDK 封装走 `/v1/chat/completions`（`:58`）；`chat()` 请求前 `preprocessor.process(messages)`（默认 off）、`kv_probe` 前后快照（`:75-103`）；返回行 `text/prompt_tokens/completion_tokens/total_tokens/cached_tokens/latency_ms/rss_mb/gpu_mb/timings`（`:89-99`）；`_extract_timings` 从 `timings` 属性 / `model_extra` / `__pydantic_extra__` 提取（`:160-180`）；**`_extra_body` 固定 `chat_template_kwargs.enable_thinking=False`（`:134-141`，no-think 保证可比）**；**400 超 ctx 重试**：`exceed_context_size_error` 且 `_retry<max_retry(3)` 时丢弃最早约 1/3 非 system 消息，若候选丢失全部真实 user query 则找回最早一条（Qwen3.5 multi_step_tool 模板要求，`:107-131`；`_is_real_user_query` 定义 `:147-158`）。**原生 `/completion` 未在 driver 封装**（e15_branch_concurrent 直接可用）。

**sampler / KVProbe**：`framework/sampler.py` —— `find_server_pid`（ss -ltnp + psutil 兜底）、`find_server_rss_mb`（psutil rss）、`find_server_gpu_mb`（pynvml 按 pid 匹配 usedGpuMemory，容器内降级 None）（`:21-75`）；`framework/kv_probe.py` —— `KVProbe` GET `/metrics/kv`（自动剥 /v1，`:25-29`）、`snapshot(tag)`、`kv_state/list_slots/erase_slot/clean_all_slots`（`:95-144`）、后台周期采样（`:147-158`）、`run_aggregate`（first/last/peak_used_cells，`:177-196`）。

**结果 schema**（`benchmark/runner/runner.py`）：`{"config": config.to_dict(), "summary": {...}, "scenarios": {...}}`（`:249-254`）落盘 `results/bench_<ts>.json`（`:257-264`）；`scenarios[name]` repeat=1 为 rows 数组、repeat>1 为 `{"runs", "protocol"}`（`:143-147`）；`--kv-probe` 追加 `kv_observations`。`metrics/metrics.py summarize`：p50/p95/mean/std/throughput/decode_tps/peak_rss/gpu/cache_hit_rate/recompute_tokens（`:93-107`）。

**config**（`framework/config.py:28-64`）：`ctx_size(2048) parallel(0=探测) repeat(1) warmup(0) seed(42) temperature(0.0) rounds(20) tool_steps(6) branch_rounds(5) long_rounds(40) kv_probe_enabled(False) replicate_mode("auto") kv_clean("erase") extra({})` 等；`configs/qwen35_4b_q8_production.yaml`（E14.1：ctx 4096、parallel 4、q8_0、kv_unified、-ngl 99、slot_save_path，q4_0 禁止）。

### 1.2 llama.cpp 侧（HEAD 4a699aaad）

**`/metrics/kv`**：路由 `tools/server/server.cpp:236`；handler 投递 `SERVER_TASK_TYPE_METRICS` 到推理线程（`server-context.cpp:5328-5364`）；组装点 `server-context.cpp:3104-3135`（`llama_memory_get_kv_stats` → 字段 `schema_version/capacity_bytes/used_bytes/used_bytes_valid/capacity_cells/used_cells/active_sequences/shared_cells/physical_sharing/shared_cells_semantics`）；`llama_kv_stats` 结构 `include/llama.h:727-751`；标准实现 `src/llama-kv-cache.cpp:734-800`（`capacity_bytes=total_size()`、cells 跨 stream 累加、`shared_cells`=引用数>1 的 cell、`physical_sharing=false` 恒 false）。

**hybrid 结构与统计**（`src/llama-memory-hybrid.h:19-94`）：`llama_memory_hybrid` 内部 `mem_attn`（llama_kv_cache）+ `mem_recr`（llama_memory_recurrent）双成员；**`get_kv_stats` 只委托 attention 部分**（`llama-memory-hybrid.cpp:190-194`，注释 "recurrent state is not part of the KV cache statistics"）；`seq_cell_stats` 同样只委托 attention（`:196-199`）；`memory_breakdown` 把 attn+recr 字节合并返回（`:182-188`，无法拆分）。

**recurrent 内存**（`src/llama-memory-recurrent.cpp:99-126`）：经 `filter` 仅对 **recurrent 层**分配 `cache_r_l{i}`（`[n_embd_r, n_rows]`）/`cache_s_l{i}`（`[n_embd_s, n_rows]`），`n_rows = mem_size × (1+n_rs_seq)`，**类型固定 F32**；`mem_size = max(1, n_seq_max)`（每 slot 一格，**与 n_ctx 无关**）；日志 `RS buffer size` 与 `size = ... (cells, layers, seqs rs_seq), R/S (f32)` 的 `layers` 打印为**总层数**（`n_layer=hparams.n_layer()`，`:29`），实际分配仅 recurrent 层（filter skip，`:76-82`）；`size_r/s_bytes()` 累加非空 tensor（`:709-716`）。

**capability gate（C1 前缀共享）**：启动期日志 `server-context.cpp:1497-1515`（memory null / 不支持共享 / **hybrid** / recurrent / SWA 依次 rejected，仅日志不 throw）；运行时生效门 `server-context.cpp:3851-3856`（`kv_prefix_share && kv_unified && supports_cross_slot_prefix_sharing && !hybrid && !recurrent && n_swa==0`）；默认实现 `src/llama-memory.h:122-124` 返回 false，**唯一 override true 为** `llama_kv_cache::supports_cross_slot_prefix_sharing`（`src/llama-kv-cache.h:156`）；hybrid 不 override → **Qwen3.5-4B 无条件被拒**。

**`/completion` 与 OAI**：`handle_completions_impl` 定义 `server-context.cpp:4890`；`post_completions`（/completion、/completions）委托 `:5621-5625`，OAI 端点（/v1/completions、/chat/completions、/v1/chat/completions）同一 handler（`server.cpp:242-246` 路由；`server-context.cpp:5633-5762`）；timings 填充 `server-context.cpp:568-576`（`prompt_n/cache_n/predicted_n/prompt_ms/predicted_ms` 等）；`t_prompt_processing` 定义 `:342` → `timings.prompt_ms`（`:571`，**TTFT 代理**，见 §5.1）。**并发模型**：`server_queue::start_loop`（`server-queue.cpp:125-215`）主线程阻塞循环 = 队首 task → `update_slots()` 单次遍历所有 slots 完成 pre_decode/decode/sample（`server-context.cpp:3398+`）→ 即 cont-batching 是**单线程调度**，HTTP 线程独立（`server.cpp:504/519`）；并行只存在于 ggml 内部线程与多 slot 批处理。

**memory_breakdown / position**：唯一输出点 `server.cpp:531` 退出时 `common_memory_breakdown_print`（`common/fit.cpp:817-940`），由 model 权重 + memory（KV/记忆）+ compute buffer 三部分构成；**hybrid 下 `context` 列 = attn+recr 合并，无法拆分**。运行期唯一 recurrent 细分 = 启动日志（`llama-memory-recurrent.cpp:123-126`）。

**checkpoint / COW**：`--checkpoint-reuse`（`arg.cpp:3684-3690`）在 hybrid/recurrent 上启动强制禁用（`server-context.cpp:1479-1487`，E12 实证 restore 后仍全量 prefill）；请求级 `checkpoint_save/restore`（`server-task.h:81-83`）；slot 级 `POST /slots/:id?action=save|restore|erase`（`server.cpp:275`；`server-context.cpp:5446-5477`）需 `--slot-save-path`；**无 COW**（physical_sharing 恒 false，共享是 metadata/bitset 级）。

**Qwen3.5-4B**：`llama_arch_is_hybrid` 含 `LLM_ARCH_QWEN35/QWEN35MOE`（`src/llama-arch.cpp:958-977`）；**层结构**（`src/models/qwen35.cpp:21-27`）：`is_recr_impl[i] = (i+1) % full_attn_interval != 0`（interval=4，GGUF 无 `LLM_KV_ATTENTION_RECURRENT_LAYERS` 时 fallback）→ **32 层中 8 attention（i=3,7,...,31）、24 recurrent**；4B 档 = `n_layer==32 && n_embd==2560`（`qwen35.cpp:31-34`）；memory 类型 `llama_memory_hybrid`（`llama-model.cpp:2256-2303`）；**`--cache-type-k/v` 只作用于 attention 部分**（`llama-model.cpp:2288-2296`），recurrent 固定 F32。

**recurrent 状态维度**（`src/llama-hparams.cpp:183-231`，KDA 分支）：
- `n_embd_r() = 3×(ssm_d_conv−1)×n_head×n_embd_head_kda`；4B 实测闭合值 = `3×(3−1)×32×128 = 24576`（ssm_d_conv=3、n_head=32、head_dim=128）；
- `n_embd_s() = n_embd_head_kda² × n_head = 128×128×32 = 524288`。
- 每 slot RS = `24 层 × (24576+524288) × 4 B = 52,690,944 B = 50.25 MiB` —— 与探针实测（§1.3）精确闭合。

**attention KV per-cell**（8 attention 层）：`n_kv_heads×head_dim×2×(每元素字节)`；q8_0（每 32 值 + 2B scale → 34/32 系数）= `8×8×128×2×34/32 = 17408 B/cell`；f16 = `8×8×128×2×2 = 32768 B/cell`。ctx 4096 → q8 68 MiB / f16 128 MiB —— 与探针实测精确闭合。

### 1.3 M0 探针实测（2026-08-09，RTX 4060 Laptop 8GB / driver 610.43.03）

方法：`build-cuda/bin/llama-server`（**version 8569/afbf375c6**；4a699aaad 仅新增测试文件、未改核心代码，故**核心代码与 HEAD 等价**；正式实验前仍重建到 4a699aaad 保证版本号一致，见 §9.5）加载 `models/qwen3-5-4B-Q4_K_M.gguf`，`-ngl 99 --ctx-size 4096 --kv-unified`，轮询 `/health` 就绪后抓 nvidia-smi 与启动日志，立即 SIGTERM（**零推理请求**）。日志留于忽略目录 `llama.cpp/tmp/m0_probe2_*.log`、`m0_metrics_probe*.log`（不入库）。

| parallel | ctk/ctv | KV buffer（attention，unified） | RS buffer（recurrent） | R/S 分解 | GPU used |
|---|---|---|---|---|---|
| 2 | q8_0 | 68.00 MiB | 100.50 MiB | R 4.50 + S 96.00 | 2974 MiB |
| 4 | q8_0 | 68.00 MiB | 201.00 MiB | R 9.00 + S 192.00 | 3074 MiB |
| 6 | q8_0 | 68.00 MiB | 301.50 MiB（= 6×50.25） | — | 3186 MiB |
| 8 | q8_0 | 68.00 MiB | 402.00 MiB | R 18.00 + S 384.00 | 3290 MiB |
| 10 | q8_0 | 68.00 MiB | 502.50 MiB（= 10×50.25） | — | 3402 MiB |
| 4 | f16 | 128.00 MiB | 201.00 MiB | R 9.00 + S 192.00 | 3132 MiB |

模型权重实测：`CUDA0 model buffer size = 2571.63 MiB`（+ `CPU_Mapped 497.31 MiB`，mmap 页面；GGUF 文件 2707514144 B = 2.52 GiB）。

`/metrics/kv` 实测（p4/q8_0/unified/ctx4096，无请求）：`capacity_cells=4096, capacity_bytes=71303168（=68 MiB，与 KV buffer 精确吻合）, used_cells=0, active_sequences=0, shared_cells=0, physical_sharing=false`。

**GPU 口径说明**：`nvidia-smi memory.total = 8188 MiB`（空闲 free=7795、used=40）；探针"GPU used"为 nvidia-smi `memory.used` 绝对值（含系统约 40 MiB）；模型+KV+RS 实际增量 ≈ used − 40。

**关键量化事实**：
1. **attention KV（unified）与分支数无关**：ctx 4096 时 q8_0=68 MiB、f16=128 MiB 固定池——"KV 预分配固定"在 hybrid 上同样成立（`capacity_bytes` 由 ctx 决定，§5.2）。
2. **recurrent RS buffer 随 parallel 线性增长：50.25 MiB/slot**（= 24 层 × 548864 × 4B；R 2.25 + S 48.0）——**hybrid fan-out 唯一随活跃分支数线性增长的显存项**。
3. **GPU 峰值完全在 8GB 预算内**：parallel 10 q8_0 = 3402 MiB（8188 的 41.5%），余量约 4.8 GiB。
4. `/metrics/kv` **只反映 attention 部分**；recurrent 内存无运行期接口（观测缺口见 §5.1）。

---

## 2. 现有场景为何不是"真实多路径决策"

| 维度 | 现状（证据） | 与真实 Agent fan-out 的差距 |
|---|---|---|
| 分支来源 | `branch.py:15-18` 固定 `BRANCH_QUESTIONS`（A/B 两问）+ `:53-65` 固定"继续完善方案X"循环；`e15_branch_concurrent.py:99-114` 脚本拼接 `PREFIX_CORE+BRANCHES[bX]`；`branch_pressure.py` 固定 `SEQUENCE` | **模型从不决策分叉**，只是固定 prompt 上的续写；无"决策点→分支"语义 |
| 分支首 token | 人为设计互异（保证 LCP 精确终止） | 真实分支由内容决定，LCP 自然可变 |
| 并发形态 | HTTP 并发（barrier+ThreadPoolExecutor），但 server 单线程调度（`server_queue::start_loop`+`update_slots` 单循环，§1.2） | "并发"是队列化并发，非并行推理；fan-out 测量的是调度/批处理行为而非并行计算 |
| 模型域 | E15.1 用 TinyLlama stories260K attention-only（`e15_branch_concurrent.py` 目标模型） | **不可外推 4B**：4B 是 hybrid，C1 前缀共享被 capability gate 永久拒绝（`server-context.cpp:1497-1515/3851-3856`），无 shared_cells>0 可言 |
| 内存语义 | E15.1 测的是 metadata 前缀共享（shared_cells/recompute），且无 COW（physical_sharing 恒 false） | hybrid 下 fan-out 内存主成分是 **recurrent state**（§1.3 实测 50.25 MiB/slot 线性），与 attention KV 共享机制**不同量级、不同机制** |
| 4B gate 覆盖 | benchmark 侧无 4B 决策 workload；`e15_3_b1_paired.py` 是 preprocessor 无损门禁（无关） | 缺少"4B 上真实分支语义 + 内存归因"的基线 |

结论：**现有 branch 场景全部是"脚本模板分支"，不是"模型决策多路径"**；TinyLlama E15.1 的共享收益证据不能外推到 4B hybrid（capability gate + 无 COW + recurrent 主导）。M0 需要新的 workload 语义。

---

## 3. M0 workload 设计（DESIGN）

### 3.1 语义（真实多路径决策，temp=0 可稳定复现）

**决策点协议（确定性）**：公共上下文末尾要求模型输出唯一合法分支指令，枚举受限：
```
ACTION: branch(b1|b2|b3|b4|b5|b6|b7|b8)
```
- 决策点 prompt 采用枚举式约束（"只能输出上述 ACTION 之一，不得输出其他内容"），规避自由文本指令漂移；
- `temperature=0, seed=42` 固定 → 分支选择在**给定公共上下文下确定性**；用 0.8B（CPU）离线校准决策点 prompt 的合法输出率，4B（GPU）正式；
- **fallback 与判定（审查修订）**：若 4B 决策点合法输出率 <100%（经 ≥20 次复现），触发固定映射 fallback（脚本按固定规则选分支），该数据判定为 **`NOT_VALIDATED`/HOLD**——**不称真实决策 PASS**；固定映射数据仅可作内存压力 smoke（§8 G-M0-1）。
- 分支语义稳定：分支 prompt = 公共上下文 + 决策点输出 + 分支专属任务 + 分支专属 canary；分支任务**不依赖模型继续决策**（后续轮次为固定后续推理/工具调用）。

**分支-工具-回收流**：
```
[公共上下文+决策点] → 模型输出 ACTION: branch(bX)
  → N 个分支请求并发发出（barrier；分支内容 = 公共前缀 + 各自任务 + canary）
  → 每分支 M 轮：工具结果注入（复用 tool_call 的 ACTION 协议或直接注入）→ 后续推理
  → 回收：全部分支完成后 erase（/slots/:id?action=erase，KVProbe.erase_slot）
```
- **identity 定义**：`branch`（b1..b8）是内容分支；`session` 是 workload 实例；`thread` 是并发出线程。canary 隔离：每分支专属 `CANARY-BX-<hex>` 注入分支 prompt 尾部，输出检测跨分支泄漏（复用 E15.1 G5 模式）。

### 3.2 模块边界（**单一架构定稿**，审查修订）

**定稿：独立 M0 runner（与 E15 runner 约定一致），不注册第五场景**。理由：fanout 的决策/并发/门禁逻辑独立于现有四场景的"累积多轮对话"范式；注册场景需改 `config.py`/`runner.py`（场景发现、summary 处理、`extra` 语义），引入兼容性成本；独立 runner 与 `e15_branch_concurrent.py` 同为"专项 runner"先例，无第二套 config 约定（CLI 直接参数化，不走 BenchmarkConfig 扩展）。

- **复用**：`framework/driver.py`（OAI 封装/timings 提取）、`framework/kv_probe.py`、`framework/sampler.py`、`metrics/metrics.py summarize`、`e15_branch_concurrent.py` 的 server 生命周期（`start_server/stop_server`，`:548-599`）与 gate_verdict 纯函数模式。
- **新增（下一实现阶段，本阶段不写）**：
  - `benchmark/framework/fanout_prompts.py` —— 纯函数：决策点 prompt 构造（枚举约束）、分支扩展、canary 注入、`ACTION: branch(bX)` 解析、token 估算（2.5 chars/token，同 e15 `est_tokens` 惯例）、KV 预算校验（§4 公式，fail-fast）。
  - `benchmark/runner/m0_fanout_runner.py` —— CLI + 矩阵编排 + server 生命周期 + 并发（barrier+ThreadPoolExecutor）+ gate + 落盘（唯一顶层 schema，§5.1）。
  - `benchmark/tests/test_fanout_prompts.py`、`benchmark/tests/test_m0_fanout_runner.py` —— 纯函数 + mock server e2e。
- **不改**：`config.py`、`runner.py`、`workload/__init__.py`、任何既有 workload（可回滚：删除新增文件即完全回滚，默认无行为改动）。
- **失败降级**：server 启动失败重试 3 次后跳过该配置并记 INVALID；`/metrics/kv` 不可用 → KVProbe 现有优雅降级（failures/last_error）；预算校验 fail-fast（§4）；决策点 fallback → NOT_VALIDATED（§3.1）。

### 3.3 请求接口合同（审查修订）

| 项 | 合同 |
|---|---|
| 统一接口 | **OAI `/v1/chat/completions`**（`driver.chat`）——决策点与分支请求同一接口；`chat_template_kwargs.enable_thinking=False`（`driver.py:134-141`）保证 no-think 一致；原生 `/completion` 仅为 e15 对照既有路径，M0 不用（模板/thinking 控制不一致风险） |
| 确定性 | `temperature=0, seed=42`（经 `extra_body`）；`--n-predict`：决策点 8、分支轮 64（CLI 可配 `--decision-n-predict/--branch-n-predict`） |
| 停止判定 | OAI `finish_reason`（stop/length）；记录到行级 |
| 可比性 | 同 seed/temp 下输出 `tokens` 数组 + `content_sha256` 逐字节比较（e15 G1 模式）；决策点与分支均在请求级记录 prompt_tokens/completion_tokens/timings |
| slot 控制 | 默认不强制 `id_slot`（server 自动调度）；若需确定性 slot 映射（决策点 slot 0、分支 1..N），M0 实现时在 `driver.chat` 的 `extra_body` 增加可选 `id_slot` 透传（一行，兼容旧行为，默认缺省） |
| timings | `driver._extract_timings`（`:160-180`）；TTFT 代理 = `timings.prompt_ms`（`server-context.cpp:571`），见 §5.1 |

### 3.4 请求序列与计时

每 replicate：决策点请求（slot 0）→（可选 erase 决策点 slot，见 §4 预算）→ N 分支并发（barrier，slot 1..N）→ 每分支 M 轮 → `erase` 全部 → 断言回基线。warmup≥1、reps≥5（与 `benchmark/README.md:145` 约定一致），中位数报告。

---

## 4. 实验矩阵与 KV 预算（审查修订）

### 4.1 对照

| 对照 | 配置 | 目的 |
|---|---|---|
| **A. full-prefill baseline** | 无 `--kv-prefix-share`，每分支全量 prefill（`cache_prompt` 生效但无跨 slot 共享） | 分支内存/延迟基线（used_cells 峰值 = 决策 + 各分支完整占用，见 §4.2 公式） |
| **B. 当前 C1 请求** | `--kv-prefix-share --kv-prefix-share-min-lcp 64`；**4B 预期 capability rejected**（`server-context.cpp:1497-1515` 日志 + shared_cells 恒 0） | 安全对照：验证 gate 行为契约（日志出现、共享不生效），不宣称收益 |
| **C. q8_0 可复用接口** | `--cache-type-k/v q8_0`（= `qwen35_4b_q8_production.yaml` 的 KV 形态） | M0 后 q8_0 分支容量实验的同一接口；本阶段仅记录接口与容量数字（68 MiB @ctx4096，§1.3） |

**明确不做**：checkpoint/COW 实现（`--checkpoint-reuse` 在 hybrid 上已禁用，slot save/restore 为 E12 原型范畴；M0 只做内存归因基线）。

### 4.2 unified KV 严格预算公式与 fail-fast（审查修订）

**容量事实**：unified KV 池 `capacity_cells = ctx_size = 4096`（§1.3 实测），所有 slot 共享 cell 池；4B 上 C1 被 gate 拒绝 → **A/B 对照实际均为 full-prefill**（每分支从零 prefill 完整上下文）。

**预算公式（保守上界，沿用 e15 前置估算模式）**：
```
used_cells ≈ P + N×(P+B)
P = 决策点/公共前缀 token 数；B = 每分支后缀 token 数；N = fan-out 分支数
约束：P + N×(P+B) ≤ capacity_cells × 0.85 = 3481   （安全余量 15%）
```
- 含义：决策点请求占 P cells；N 个分支各自 full-prefill 完整 `(P+B)`（无跨 slot 共享）；
- 运行时优化（不影响公式）：分支并发前可 `erase` 决策点 slot 释放 P cells → 实际占用 `N×(P+B)`；公式仍按保守上界 fail-fast；
- **fail-fast**：runner 启动前置校验用 `est = P + N×(P+B)`（token 估算 2.5 chars/token，同 e15 `est_tokens`，`e15_branch_concurrent.py:867-873`），`est > 3481` → 拒绝该组合并记 INVALID，不启动 server。

**长度桶收敛（token 估算，2.5 chars/token）**：
| 桶 | prefix P | branch B | 说明 |
|---|---|---|---|
| short | 150 | 150 | 决策点 + 短分支任务 |
| medium | 280 | 480 | 中等上下文 + 分支任务+工具注入 |
| long | 600 | 800 | 长公共上下文（仅配小 fanout，见下） |

**合法矩阵（9 候选 → 6 合法；parallel = fanout+2 动态，审查修订）**：

| fanout N | parallel | short (750) | medium (1900/3320) | long (3400/…) |
|---|---|---|---|---|
| 2 | 4 | ✓ used=750 | ✓ used=1900 | ✓ used=3400 |
| 4 | 6 | ✓ used=1350 | ✓ used=3320 | ✗ used=6200 > 3481 |
| 8 | 10 | ✓ used=2550 | ✗ used=6700 | ✗ |

- `parallel = N + 2`（+1 决策点 slot、+1 spare），前置校验 `parallel >= fanout+2`（沿用 e15 `parallel>=branches+2` 模式）；
- **不可运行组合（long×4、medium/long×8）在 runner 中由 §4.2 公式拒绝，不进入实验**；
- 矩阵维度 × ctk{q8_0, f16} × 对照{A, B} → 6 组合 × 2 × 2 = **24 个运行单元**（每单元 warmup 2 + reps 5）；
- **RTX 4060 8GB 安全预算（§1.3 探针实测）**：最大 parallel 10 q8_0 = 3402 MiB（41.5%）；全部合法组合 GPU 峰值 < 3.5 GiB，余量 >4.6 GiB 供 compute buffer 与抖动——**无需降级**。

---

## 5. 落盘 schema 与硬门禁

### 5.1 唯一顶层 JSON schema（审查修订：单一架构）

独立 runner 落盘 `results/m0_fanout_<ts>.json`，结构对齐 e15_branch_smoke（`e15_branch_concurrent.py:795-831`）：
```json
{
  "meta": {"workload": "fanout", "design_ref": "M0_BRANCH_MEMORY_BASELINE_DESIGN.md",
           "model": "...", "binary_version": "8570/4a699aaad", "ctx_size": 4096,
           "fanout": N, "prefix_len": "...", "branch_len": "...", "ctk/ctv": "...",
           "protocol": "OAI /v1/chat/completions, temp=0, seed=42, no-think",
           "decision_validated": true|false},
  "modes": {"off": {"start": ..., "replicates": [...], "stop": ...},
            "on":  {"start": ..., "replicates": [...], "stop": ...}},
  "gates": {"G-M0-1..7": {...}},
  "verdict": "PASS|NOT_VALIDATED|HOLD|INVALID",
  "notes": [...]
}
```

每 replicate 行字段（§5.2），KV 观测经 `KVProbe.run_aggregate`（first/last/peak_used_cells）。

### 5.2 指标字段

| 类别 | 字段 | 来源 |
|---|---|---|
| GPU/RSS 峰值 | `peak_gpu_mb` / `peak_rss_mb` | `sampler.find_server_gpu_mb/rss_mb`（行级 max，`metrics.py:104-105`）；GPU 口径 = nvidia-smi `memory.used`（total 8188、空闲 used≈40，增量 = used−40） |
| KV（attention） | `capacity_cells/used_cells/shared_cells/active_sequences/capacity_bytes/used_bytes` | `/metrics/kv`（KVProbe 快照 + `run_aggregate`） |
| token 分解 | `timings.prompt_n/cache_n/predicted_n`（`prompt_n+cache_n==prompt_tokens`） | OAI timings（`server-context.cpp:568-576`） |
| 延迟 | `latency_ms`；**TTFT 代理 = `timings.prompt_ms`**（= `t_prompt_processing`，`server-context.cpp:342/571`；llama-server 无独立 TTFT 字段） | driver 行 / timings |
| 输出 | `tokens`（数组）+ `content_sha256` + `finish_reason` | driver/runner 提取（e15 模式） |
| 分支质量 | `task_success`、canary 泄漏标志、决策点合法率、回收断言 | workload gate |

**recurrent/attention 细分 —— 最小可观测性缺口（如实记录，本阶段不实现 llama.cpp 改动）**：
- `/metrics/kv` 只统计 attention（`llama-memory-hybrid.cpp:190-194`）；recurrent 内存**无运行期接口**（仅启动日志 `RS buffer size` + 退出时 `common_memory_breakdown_print` 的合并 `context` 列）。
- M0 归因方案：**启动日志解析 `RS buffer size`/`R/S (f32)`（`llama-memory-recurrent.cpp:123-126`）+ nvidia-smi 峰值差分**（全模型峰值 − 空载基线 = KV+RS+compute），配合 §1.3 精确公式（RS = 24×548864×4×N、KV = 8×8×128×2×elem×ctx）分离各成分。后续若需运行期 recurrent 计数，需 llama.cpp 最小扩展（`llama_kv_stats` 增加 recurrent 字段或 `/metrics/kv` 加 breakdown）——**列为未决问题 §9.1，不在 M0 承诺**。

### 5.3 指标语义区分

- **"预分配峰值不变"**：`capacity_bytes`/`capacity_cells` 由 ctx 决定（§1.3 实测 68 MiB@ctx4096），分支数不改变 capacity —— 这是预期，不是收益；
- **"used cells/容量改善"**：`used_cells` 随分支活跃/回收变化；改善指标 = 同 capacity 下承载更多活跃分支（used_cells 峰值占比）或回收后回落；
- **recurrent 是唯一随分支线性增长的显存项**（50.25 MiB/slot）→ 分支容量上限由 `(GPU 余量 − 固定 KV/权重) / 50.25` 决定，量化验收用此式与实测对账（G-M0-4）。

---

## 6. 实现文件清单与合同（下一阶段，审查修订）

| 文件 | 类型 | 内容 |
|---|---|---|
| `benchmark/framework/fanout_prompts.py` | 新增 | 纯函数：决策点 prompt（枚举约束）、分支扩展、canary 注入/检测、`ACTION: branch(bX)` 解析、token 估算、KV 预算校验（§4.2 fail-fast） |
| `benchmark/runner/m0_fanout_runner.py` | 新增 | CLI + 矩阵编排 + server 生命周期（复用 e15 骨架）+ barrier 并发 + gate（G-M0-1..7）+ 唯一顶层 schema 落盘（§5.1） |
| `benchmark/tests/test_fanout_prompts.py` / `test_m0_fanout_runner.py` | 新增 | 纯函数（决策解析确定性/canary/预算 fail-fast/gate 判定/CLI）+ mock server e2e |
| 文档 | 更新 | 本文件补实测结果 + AGENTS.md 状态更新 |

**不改**：`config.py`、`runner.py`、`workload/`、`driver.py`（除 §3.3 可选 `id_slot` 透传一行，缺省行为不变）。

CLI 合同：`--server-bin --model --port --ctx-size 4096 --fanout {2,4,8} --prefix-len {short,medium,long} --branch-len {short,medium,long} --ctk q8_0 --ctv q8_0 --decision-n-predict 8 --branch-n-predict 64 --tool-rounds M --warmup 2 --reps 5 --out <path>`；`--parallel` 由 runner 按 `fanout+2` 计算（不接受手工覆盖，避免非法组合）。预算 fail-fast：`est > 3481` → INVALID（§4.2）。

测试计划：纯函数 pytest（决策点解析边界/确定性/canary/预算公式/gate_verdict 分支/CLI 校验/fail-fast）+ mock OpenAI server e2e（现有 `tests/mock_server.py` 模式）+ 可选 TinyLlama CPU smoke（**仅冒烟，不宣称 4B 结论**）。

---

## 7. 8GB 可行性（探针实证，§1.3）

- 4B Q4_K_M + ctx4096 + unified + parallel 10（矩阵最大）+ q8_0：**GPU 峰值 3402 MiB / 8188（41.5%）**，全部合法组合 < 3.5 GiB；
- 权重 CUDA0 2571.63 MiB（+ CPU_Mapped 497.31）；attention KV 固定 68（q8）/128（f16）MiB；recurrent 50.25 MiB/slot 线性；
- 分支容量理论上限：`(8188 − 2572 − 68 − 系统/compute ~400) / 50.25 ≈ 102 slot`，但 decode 吞吐/批处理预算先于显存成为限制 → 矩阵取 fanout 2/4/8 是安全的。
- **结论：矩阵无显存阻塞；无需降级配置**。

---

## 8. 实施建议与量化验收标准

**建议：GO** —— 调研充分（全部关键接口有 file:line 证据）、可行性实证（§1.3/§7 探针）、矩阵合法（§4.2 公式收敛）、纯新增可回滚、风险可控。

可量化验收标准（M0 实现阶段）：
| 门禁 | 标准 |
|---|---|
| G-M0-1 决策确定性 | temp=0/seed=42 下 4B 决策点合法 `ACTION: branch(bX)` 输出率 **100%**（≥20 次，跨 2 次独立 server 会话）；**<100% → `NOT_VALIDATED`/HOLD**（固定映射数据仅作内存压力 smoke，不称真实决策 PASS） |
| G-M0-2 隔离 | canary 跨分支泄漏率 **0**（全部 reps×branches 输出） |
| G-M0-3 回收（审查修订） | 每 rep 末 `erase` 后 `/metrics/kv` `used_cells==0 && active_sequences==0`（**仅证明 attention cells 回收**）；recurrent 回收用 **GPU 差分观测**：nvidia-smi `used` 回落至空载基线（±50 MiB）；**局限**：总量粒度、受波动影响、不能证明 per-slot/完整状态回收——M0 只承诺"attention 回收 + GPU 总量回落"，不承诺完整状态回收证明 |
| G-M0-4 内存归因 | 各成分可分离：`attention KV`（=8×8×128×2×elem×4096：q8 68 / f16 128 MiB）、`recurrent`（=24×548864×4×parallel）、`权重+compute`（=GPU used − KV − RS − 系统 40）—— 与探针数字对账误差 ≤5% |
| G-M0-5 对照有效性 | 4B 上 off vs `--kv-prefix-share`：两者 `shared_cells` 恒 0，且 on 会话日志含 `E8-C1: capability rejected: hybrid`（100%） |
| G-M0-6 复现 | 同配置两次独立 run：TTFT（`timings.prompt_ms`）/latency 中位数偏差 ≤10%，决策分支归属一致 |
| G-M0-7 数据落盘 | 每 rep 完整记录 §5.2 字段，无缺键；`results/m0_fanout_*.json` 可独立复算 summarize |

**输出（M0 实现交付）**：一份 `branch memory baseline` 报告，回答"fan-out 内存/延迟主成分"（预期：recurrent state 线性项主导显存，attention KV 固定池，重复 prefill 主导延迟；以实测为准，不预设结论）。

---

## 9. 未决问题

1. **recurrent 内存运行期观测缺口**：需 llama.cpp 最小扩展（`llama_kv_stats` 加 recurrent 字段）或接受"启动日志 + 差分"近似——M0 用后者，列为后续优化候选。
2. **4B 决策点提示工程**：需 0.8B 离线校准（CPU，可跑）→ 4B 验证；若枚举约束下 4B 仍不稳定，fallback = 固定决策映射且判定 **NOT_VALIDATED/HOLD**（§3.1/§8）。
3. **temp=0 下分支"选择/回收"轮语义**：模型 `ACTION: select(bX)` 的稳定性需实测；失败则降级为固定回收（`erase`），不阻塞内存归因目标。
4. **parallel 10 + long 桶的 decode 吞吐**：单线程调度下 latency 可能高，M0 只报告、不优化（且 long×大 fanout 已被预算公式排除）。
5. **build-cuda 二进制版本**：探针用 8569/afbf375c6（核心代码与 HEAD 等价，因 4a699aaad 仅新增测试文件）；正式 M0 实验前重建到 4a699aaad（version 8570）保证版本号一致。

## 10. 风险

- Laptop GPU 抖动（E3 门禁先例）→ warmup + 中位数 + 同硬件 off/on paired。
- 0.8B 指令遵循不稳定 → 决策点枚举约束 + 离线校准（§9.2 fallback → NOT_VALIDATED）。
- 模型可用性：4B GGUF 已就位（2.7 GB，`models/qwen3-5-4B-Q4_K_M.gguf`）；0.8B 需 `download_models.sh 0.8b`（M0 实现时按需拉取）。

## 11. 实施清单（下一步，不在本阶段执行）

1. 新增 `benchmark/framework/fanout_prompts.py` + 纯函数测试（决策点解析/确定性/canary/预算公式 fail-fast）；
2. 新增 `benchmark/runner/m0_fanout_runner.py`（复用 e15 生命周期/gate 骨架）+ mock e2e 测试；
3. `cmake --build build-cuda` 重建 4B 实验二进制到 HEAD（4a699aaad）；
4. 0.8B 决策点校准 → 4B 决策确定性验证（G-M0-1，<100% → NOT_VALIDATED）；
5. 跑合法矩阵（§4.2，6 组合 × 2 ctk × 2 对照 = 24 单元）→ 落盘 → 归因报告（§8 输出）→ 对照 G-M0-2..7；
6. 结果归档 `benchmark/baseline/` + 报告文档 + AGENTS.md 状态更新。
