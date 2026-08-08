# M0：真实智能体多路径决策 workload + 分支内存基线（技术调研与设计）

> 状态：**调研完成 + 详细设计（DESIGN ONLY）；未实现 workload 代码、未运行正式 benchmark**。
> 日期：2026-08-09 ｜ 对应路线：M0（阶段 0 通过后第一条后续路线）｜ 实施建议：**GO**（见 §8）。
> 修订：2026-08-09 v2（矩阵/预算/层数/架构/接口合同/回收观测）；v3（精确 token 计数、矩阵手算修正、driver 合同、门禁语义）；v4（driver `_retry` 位置参数兼容、thinking 字段生效实证、G-M0-1 与矩阵完全分离、RS 日志行号、CLI 临时目录）；v5（G-M0-1 口径统一、可提交证据、token parity 门禁、verdict 规则）；**v6（终审——INVALID 谓词 OR 语义三处统一、verdict/gates 完整值域与优先级映射表、add_special 幂等归因修正（add_bos_token=false 无 BOS）、parity 校准/清理协议、步骤编号与行号 Low 修正）**，见各节。
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

**memory_breakdown / position**：唯一输出点 `server.cpp:531` 退出时 `common_memory_breakdown_print`（`common/fit.cpp:817-940`），由 model 权重 + memory（KV/记忆）+ compute buffer 三部分构成；**hybrid 下 `context` 列 = attn+recr 合并，无法拆分**。运行期唯一 recurrent 细分 = 启动日志：`RS buffer size` 行（`llama-memory-recurrent.cpp:115`）+ `R/S (f32)` 分解行（`:123-126`）。

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

### 1.4 可提交探针证据（H2，benchmark/baseline/ 归档）

结构化证据：**`benchmark/baseline/m0_probe_thinking_token_parity_20260809.json`**（可复核，非 ignored tmp 日志）。内容摘要（2026-08-09，4B Q4_K_M / ctx4096 / unified / q8_0，无敏感公共消息）：

**thinking 渲染三路**（同一 messages + add_generation_prompt，`/apply-template`）：
| 配置 | prompt chars | tokens | 渲染尾部 | prompt sha256 |
|---|---|---|---|---|
| 不传（default） | 147 | 44 | `<|im_start|>assistant\n<think>\n` | `6fe4c9f6…b341ec7` |
| `enable_thinking=false` | 158 | 46 | `<think>\n\n</think>\n\n`（空 think 块，no-think） | `91c6bbac…7a70a5` |
| `enable_thinking=true` | 147 | 44 | 同 default | `6fe4c9f6…b341ec7` |

→ server 默认 thinking 开启；显式 false 实际生效（渲染空 think 块）。

**token parity**（no-think 配置，同一 messages）：
| 路径 | tokens |
|---|---|
| `/apply-template` → `/tokenize`（`add_special:false`） | 46 |
| `/apply-template` → `/tokenize`（`add_special:true`） | 46（**幂等归因（v6 修正）**：该 GGUF `tokenizer.ggml.add_bos_token=false` 且 `bos_token_id=None`（无 BOS token）→ add_special 无可加；**仅对本模型/配置成立，换模型必须重验**，GGUF 元数据实测见下） |
| 真实 `/v1/chat/completions`（max_tokens=1, temp=0, seed=42, no-think）`usage.prompt_tokens` | 46（finish_reason=length） |

→ **parity 成立、偏差 0**：M0 一致性门禁 = 比较 chat `usage.prompt_tokens` vs apply-template→tokenize(`add_special:false`)（§4.2 步骤 2/3）。**GGUF 元数据实测（2026-08-09）**：`tokenizer.ggml.add_bos_token=false`、`tokenizer.ggml.bos_token_id=None`、`eos_token_id=248046`、`tokenizer.ggml.model="qpt2"`——add_special 幂等的真实原因是无 BOS token 可加，**换模型必须重验**。

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
- **fallback 与判定（审查修订 v4：专用验证与正式矩阵分离）**：
  - **专用稳定性验证**（G-M0-1 数据源）：4B 决策点合法输出率 <100%（2 次独立会话 × ≥10 次，无 ACTION 或 `finish_reason=length` 均计 INVALID）→ 触发固定映射 fallback，该数据判定 **`NOT_VALIDATED`/HOLD**——**不称真实决策 PASS**；
  - **正式矩阵**：单 rep 决策失败**只记录该 rep INVALID**，并使用**固定路由 fallback**（脚本按固定规则选分支）**继续内存压力实验**（不中断、不重试），该 rep **不计入 G-M0-1**（G-M0-1 只用专用验证数据）；fallback 触发时在结果 schema `decision_fallback: true` 与顶层 `verdict` 显式标记（§5.1），报告不得宣称真实决策。
  - **INVALID 谓词（v6 定稿，可直接编码，三处同一）**：`decision_invalid ⇔ (finish_reason == "length") OR (输出不含合法 ACTION)` —— **任一条件即 INVALID**；即使截断文本（length）已含合法 ACTION 也判 INVALID（截断输出不可信）。专用验证与正式矩阵均用同一谓词。
- 分支语义稳定：分支 prompt = 公共上下文 + 决策点输出 + 分支专属任务 + 分支专属 canary；分支任务**不依赖模型继续决策**（后续轮次为固定后续推理/工具调用）。

**分支-工具-回收流**：
```
[公共上下文+决策点] → 模型输出 ACTION: branch(bX)
  → N 个分支请求并发发出（barrier；分支内容 = 公共前缀 + 各自任务 + canary）
  → 每分支 M 轮：工具结果注入（复用 tool_call 的 ACTION 协议或直接注入）→ 后续推理
  → 回收：全部分支完成后 erase（/slots/:id?action=erase，KVProbe.erase_slot）
```
- **identity 定义**：`branch`（b1..b8）是内容分支；`session` 是 workload 实例；`thread` 是并发出线程。canary 隔离：每分支专属 `CANARY-BX-<hex>` 注入分支 prompt 尾部，输出检测跨分支泄漏（复用 E15.1 G5 模式）。
- **工具注入（v5 定稿）**：**M0 不使用 OpenAI `tools` schema**（避免两条路径（apply-template vs chat）渲染差异与精确计数遗漏）；分支工具观测以**固定、无敏感、可审计的消息注入**（如 `<tool_response>` 文本块，复用 tool_call.py 协议）实现——精确计数请求因此不遗漏 tools；若未来改用 `tools` schema，则 /apply-template 与 /tokenize 两条路径必须携带**同一 tools schema**（并重新验证 token parity，§4.2 步骤 2）。

### 3.2 模块边界（**单一架构定稿**，审查修订）

**定稿：独立 M0 runner（与 E15 runner 约定一致），不注册第五场景**。理由：fanout 的决策/并发/门禁逻辑独立于现有四场景的"累积多轮对话"范式；注册场景需改 `config.py`/`runner.py`（场景发现、summary 处理、`extra` 语义），引入兼容性成本；独立 runner 与 `e15_branch_concurrent.py` 同为"专项 runner"先例，无第二套 config 约定（CLI 直接参数化，不走 BenchmarkConfig 扩展）。

- **复用**：`framework/driver.py`（OAI 封装/timings 提取）、`framework/kv_probe.py`、`framework/sampler.py`、`metrics/metrics.py summarize`、`e15_branch_concurrent.py` 的 server 生命周期（`start_server/stop_server`，`:548-599`）与 gate_verdict 纯函数模式。
- **新增（下一实现阶段，本阶段不写）**：
  - `benchmark/framework/fanout_prompts.py` —— 纯函数：决策点 prompt 构造（枚举约束）、分支扩展、canary 注入、`ACTION: branch(bX)` 解析、**token 精确计数（§4.2：/apply-template + /tokenize）**、KV 预算校验（§4 公式，fail-fast）。
  - `benchmark/runner/m0_fanout_runner.py` —— CLI + 矩阵编排 + server 生命周期 + 并发（barrier+ThreadPoolExecutor）+ gate + 落盘（唯一顶层 schema，§5.1）。
  - `benchmark/tests/test_fanout_prompts.py`、`benchmark/tests/test_m0_fanout_runner.py` —— 纯函数 + mock server e2e。
- **不改**：`config.py`、`runner.py`、`workload/__init__.py`、任何既有 workload（可回滚：删除新增文件即完全回滚，默认无行为改动）。
- **失败降级**：server 启动失败重试 3 次后跳过该配置并记 INVALID；`/metrics/kv` 不可用 → KVProbe 现有优雅降级（failures/last_error）；预算校验 fail-fast（§4）；决策点 fallback → NOT_VALIDATED（§3.1）。

### 3.3 请求接口合同（审查修订 v3）

| 项 | 合同 |
|---|---|
| 统一接口 | **OAI `/v1/chat/completions`**（`driver.chat`）——决策点与分支请求同一接口；**thinking 合同（v5 实证，§1.4 证据）**：server 默认 **thinking 开启**（apply-template 不传时渲染 `<|im_start|>assistant\n<think>\n`），必须显式 `chat_template_kwargs.enable_thinking=false` 才渲染空 think 块（`<think>\n\n</think>`）实现 no-think——该字段经 `oaicompat_chat_params_parse` 解析**实际生效**（`server-common.cpp:1095-1102` 覆盖默认；可复核证据 `benchmark/baseline/m0_probe_thinking_token_parity_20260809.json`）；M0 沿用 `driver._extra_body`（`driver.py:135-142`）显式 false，并以 token parity 一致性门禁（§4.2 步骤 2）执行验证；原生 `/completion` 仅为 e15 对照既有路径，M0 不用 |
| 确定性 | `temperature=0, seed=42`；`--n-predict`：**决策点 16（v3：由 8 上调，保证 `ACTION: branch(bX)` 输出完整且留边界）**、分支轮 64（CLI 可配 `--decision-n-predict/--branch-n-predict`） |
| 停止判定 | OAI `finish_reason`（stop/length）；**INVALID 谓词（v6）**：`(finish_reason=="length") OR (输出不含合法 ACTION)` → 任一即该 rep INVALID（length 截断即使含 ACTION 也不可信），走固定路由 fallback（§3.1）、**不计入 G-M0-1**（G-M0-1 仅用专用 2 会话×10 请求数据，§8） |
| 可比性 | 同 seed/temp 下输出 `tokens` 数组 + `content_sha256` 逐字节比较（e15 G1 模式）；决策点与分支均在请求级记录 prompt_tokens/completion_tokens/timings |
| slot 控制 | 默认不强制 `id_slot`（server 自动调度）；若需确定性 slot 映射（决策点 slot 0、分支 1..N），经 `extra_body["id_slot"]` 透传（见下方 driver 扩展，默认缺省保持旧行为） |
| timings | `driver._extract_timings`（`:160-180`）；TTFT 代理 = `timings.prompt_ms`（`server-context.cpp:571`），见 §5.1 |

**driver 最小兼容扩展（审查修订 v4 —— `_retry` 保持位置参数）**：当前 `driver.chat(messages, _retry=0)` **不支持 temperature/seed/max_tokens 透传**（`driver.py:62`）。设计签名：

```python
def chat(
    self, messages: List[dict], _retry: int = 0,   # _retry 保持位置参数（`*` 之前），兼容现有递归调用
    *,
    temperature: Optional[float] = None,   # → create(temperature=...)，None 不传（保持旧默认）
    seed: Optional[int] = None,            # → create(seed=...)
    max_tokens: Optional[int] = None,      # → create(max_tokens=...)
    extra_body: Optional[Dict[str, Any]] = None,  # 与 _extra_body() 合并（id_slot 等），None 不合并
) -> Dict[str, Any]:
```

- **兼容性（v4 修正）**：`_retry` 位于 `*` **之前**（位置参数区），现有调用全部不受影响——外部 `driver.chat(messages)` 与内部递归 `self.chat(messages, _retry + 1)`（`driver.py:132`，位置传参）均无需改动；新增参数全部 keyword-only 且默认 None → 不传时行为与旧版逐字节一致（`tests/test_driver.py` 回归用例锁定：旧签名调用方式全绿）；
- **实施文件**：`benchmark/framework/driver.py`（仅 `chat` 签名 + `_extra_body` 合并逻辑）；`benchmark/tests/test_driver.py` 新增用例（默认 None 旧行为、透传生效、**旧调用 `chat(msgs, 2)` 位置传 `_retry` 兼容**、keyword-only 参数拒绝位置传参、extra_body 合并优先级）；
- **OpenAI 参数映射**：`temperature/seed/max_tokens` 直接映射 `chat.completions.create` 同名参数；`extra_body` 与既有 `_extra_body()` 字典合并（调用方优先）。

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

### 4.2 unified KV 严格预算公式与 fail-fast（审查修订 v3：精确 token 计数）

**容量事实**：unified KV 池 `capacity_cells = ctx_size = 4096`（§1.3 实测），所有 slot 共享 cell 池；4B 上 C1 被 gate 拒绝 → **A/B 对照实际均为 full-prefill**（每分支从零 prefill 完整上下文）。

**token 精确计数（审查修订，不再以 chars/2.5 为唯一门禁）**：
- **预算函数**：`budget(P, B, N) = P + N×(P+B)`（P=决策点/公共前缀 token 数，B=每分支后缀 token 数，N=fan-out）；约束 `budget ≤ 4096×0.85 = 3481 cells`（安全余量 15%）。
- **P/B 的取值必须来自 chat 模板渲染后的真实 prompt 精确 token 计数**（实现时）：
  1. `POST /apply-template`（`server.cpp:263`；`server-context.cpp:5775-5781`）：body `{messages, add_generation_prompt: true, chat_template_kwargs: {enable_thinking: false}}`，经 `oaicompat_chat_params_parse`（与 /v1/chat/completions 同一解析路径，**thinking=false 模板一致**；字段生效证据：`server-common.cpp:1095-1102` 解析覆盖 + 探针证据 §1.4）→ 响应 `{"prompt": "<渲染后完整 prompt>"}`；
  2. `POST /tokenize`（`server.cpp:261`；`server-context.cpp:5828+`）：body `{content: <prompt>, add_special: false}` → 响应 `{"tokens": [id, ...]}`，`P/B = len(tokens)`；
  3. **一致性门禁（v6：token parity + M2 校准/清理协议）**：
     - **执行时机（独立校准）**：正式矩阵开始前、server 就绪后，对**每个桶的 messages 模板**独立校准一次：比较真实 chat 请求（`max_tokens=1, temp=0, seed=42`, no-think）的 `usage.prompt_tokens` 与 `/apply-template`→`/tokenize(add_special:false)` 的 token 数；**该 max_tokens=1 chat 请求不计入 replicates**（仅校准用途）；
     - **允许偏差 0**（实测成立，§1.4；add_special 幂等归因 = 该 GGUF 无 BOS token，**换模型必须重验**）；
     - **偏差非零 → 直接 `HOLD_NOT_VALIDATED` 并停止正式矩阵**，不做未经验证的自动补偿；若确需补偿，必须写入 `meta.parity_compensation`（含确定公式）并有对应单测；
     - **校准后状态清理**：校准完成后 `erase` 全部 slot 并确认 `/metrics/kv` `used_cells==0 && active_sequences==0`（attention 回基线），**之后才采集正式基线**；
     - **mock 测试只校验请求参数一致性**（messages、thinking=false、temperature、seed、max_tokens），不声称比较渲染结果；
  - 探针实测（2026-08-09，4B，中文为主）：chars/2.5 估算**低估**真实 token 数 **7.4%（short）→ 12.6%（medium）→ 16.3%（long）**（真实 chars/token ≈ 2.1–3.1，中文 1 字常 1–2 token）——**long 边界下 est=3400 实际可达 ~3944 > 3481 预算**，故 chars/2.5 不得作为门禁唯一依据。
- **失败降级**：`/apply-template` 或 `/tokenize` 不可用（HTTP 非 200/schema 不符）→ **保守 fallback = `chars/2.0`**（比实测最差 2.1 chars/token 更保守，高估 token → 安全侧）+ 记录 warning；**注意：chars/2.0 仅对该次实测语料（中文为主）相对保守，不宣称对任意文本绝对保守**——fallback 之后仍以 budget fail-fast 兜底（`budget > 3481` 拒绝），且 fallback 路径在结果 meta 标记（`token_count_method: "chars/2.0-fallback"`）；
- **运行前校准**：runner 在 server 就绪后、正式运行前，先对各桶 prompt 执行一次 apply-template+tokenize 得真实 P/B → 预算校验 → 合法才运行（预算表由函数计算，**单测断言合法/非法组合表，避免手工常量漂移**）。

**长度桶（token 目标值，实测校准；v3 收紧 long）**：
| 桶 | prefix P | branch B | 说明 |
|---|---|---|---|
| short | 150 | 150 | 决策点 + 短分支任务 |
| medium | 280 | 480 | 中等上下文 + 分支任务+工具注入 |
| long | 400 | 600 | 长公共上下文（v2 的 600/800 经探针证明余量不足，收紧） |

**合法矩阵（6 组合；parallel = fanout+2 动态；budget 值由公式计算，单测锁定）**：

| fanout N | parallel | short | medium | long |
|---|---|---|---|---|
| 2 | 4 | ✓ 750 | ✓ **1800** | ✓ 2400 |
| 4 | 6 | ✓ 1350 | ✓ **3320** | ✗ 4400 > 3481 |
| 8 | 10 | ✓ 2550 | ✗ **6360** | ✗ |

- 修正说明（v3）：medium×2 = 280+2×760 = **1800**、medium×8 = 280+8×760 = **6360**（v2 手算 1900/6700 有误，已由预算函数+单测取代手算）；long 桶收紧后 long×2 余量 = 3481−2400 = **1081 cells**（v2 的 81 cells 不足，因估算低估 16% 时 3400 est 实际 ~3944 超预算）。
- `parallel = N + 2`（+1 决策点 slot、+1 spare），前置校验 `parallel >= fanout+2`；
- 矩阵维度 × ctk{q8_0, f16} × 对照{A, B} → 6 组合 × 2 × 2 = **24 个运行单元**（每单元 warmup 2 + reps 5）；
- **RTX 4060 8GB 安全预算（§1.3 探针实测）**：最大 parallel 10 q8_0 = 3402 MiB（41.5%）；全部合法组合 GPU 峰值 < 3.5 GiB，余量 >4.6 GiB——**无需降级**。

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
           "decision_validated": true|false,
           "decision_fallback": false|true},   # v4：正式矩阵单 rep 决策失败触发固定路由时置 true
  "modes": {"off": {"start": ..., "replicates": [...], "stop": ...},
            "on":  {"start": ..., "replicates": [...], "stop": ...}},
  "gates": {"G-M0-1..7": {...}},
  "verdict": "PASS|NOT_VALIDATED|HOLD|INVALID",
  "notes": [...]
}
```

每 replicate 行字段（§5.2）：含 **`decision_fallback: bool`（v5：该 rep 是否触发固定路由 fallback）**；KV 观测经 `KVProbe.run_aggregate`（first/last/peak_used_cells）。

**verdict 确定规则（v6 定稿：完整值域与映射，无模糊词）**：

- **`gates.*.status` 值域**：`PASS | FAIL | NOT_APPLICABLE`（required gates = G-M0-1、G-M0-2、G-M0-3a、G-M0-4、G-M0-5、G-M0-6、G-M0-7；G-M0-3b 为 smoke 观测，status 恒 `NOT_APPLICABLE` 且不计入判定）。
- **顶层 `verdict` 值域**：`PASS | HOLD_NOT_VALIDATED | HOLD_UNSTABLE_MEASUREMENT | REJECT_CORRECTNESS_OR_ISOLATION | INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`（单一枚举值，不使用枚举外字符串）。
- **优先级映射表（高 → 低，命中即终值）**：

| # | 条件 | gates 状态 | verdict |
|---|---|---|---|
| 1 | 任一正式矩阵 rep `decision_fallback:true` | G-M0-1 = FAIL | `HOLD_NOT_VALIDATED` |
| 2 | 专用验证合法率 <100% | G-M0-1 = FAIL | `HOLD_NOT_VALIDATED` |
| 3 | G-M0-2 隔离失败（canary 泄漏 >0）或 G-M0-5 对照失效（shared_cells≠0 / 缺 rejected 日志） | 对应 gate = FAIL | `REJECT_CORRECTNESS_OR_ISOLATION` |
| 4 | G-M0-6 复现偏差 >10% | G-M0-6 = FAIL | `HOLD_UNSTABLE_MEASUREMENT` |
| 5 | G-M0-4 归因对账误差 >5% | G-M0-4 = FAIL | `HOLD_UNSTABLE_MEASUREMENT` |
| 6 | G-M0-7 落盘缺键 / parity 校准失败（§4.2 M2 协议） | G-M0-7 = FAIL | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| 7 | G-M0-3a 回收失败（erase 后 used_cells≠0） | G-M0-3a = FAIL | `REJECT_CORRECTNESS_OR_ISOLATION` |
| 8 | 全部 required gates = PASS 且无任何 fallback | 全 PASS | `PASS` |

- **PASS 的唯一充分条件**：`G-M0-1..7（required，G-M0-3b 除外）全部 PASS` 且 `无 decision_fallback`。
- **fallback rep 的归属**：带 `decision_fallback:true` 标记，**可继续参与内存归因 G-M0-4**（内存压力行为与决策来源无关），但**排除在真实决策质量声明之外**（不并入决策合法率、不参与 G-M0-1 统计）。
- `meta/modes/gates/verdict` 关系：`meta` 记录全局配置与决策验证结论（`decision_validated`/`decision_fallback` 聚合）；`modes.{off,on}.replicates[]` 为逐 rep 数据（含 rep 级 `decision_fallback`）；`gates` 为门禁判定明细（每 gate 一个 `status`）；`verdict` 为上述映射表的单值结论。

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
- M0 归因方案：**启动日志解析 `RS buffer size`（`llama-memory-recurrent.cpp:115`）与 `R/S (f32)`（`:123-126`）+ nvidia-smi 峰值差分**（全模型峰值 − 空载基线 = KV+RS+compute），配合 §1.3 精确公式（RS = 24×548864×4×N、KV = 8×8×128×2×elem×ctx）分离各成分。后续若需运行期 recurrent 计数，需 llama.cpp 最小扩展（`llama_kv_stats` 增加 recurrent 字段或 `/metrics/kv` 加 breakdown）——**列为未决问题 §9.1，不在 M0 承诺**。

### 5.3 指标语义区分

- **"预分配峰值不变"**：`capacity_bytes`/`capacity_cells` 由 ctx 决定（§1.3 实测 68 MiB@ctx4096），分支数不改变 capacity —— 这是预期，不是收益；
- **"used cells/容量改善"**：`used_cells` 随分支活跃/回收变化；改善指标 = 同 capacity 下承载更多活跃分支（used_cells 峰值占比）或回收后回落；
- **recurrent 是唯一随分支线性增长的显存项**（50.25 MiB/slot）→ 分支容量上限由 `(GPU 余量 − 固定 KV/权重) / 50.25` 决定，量化验收用此式与实测对账（G-M0-4）。

---

## 6. 实现文件清单与合同（下一阶段，审查修订）

| 文件 | 类型 | 内容 |
|---|---|---|
| `benchmark/framework/fanout_prompts.py` | 新增 | 纯函数：决策点 prompt（枚举约束）、分支扩展、canary 注入/检测、`ACTION: branch(bX)` 解析、**token 精确计数客户端（/apply-template + /tokenize，§4.2）与保守 fallback（chars/2.0）**、预算函数 `budget(P,B,N)` 与 fail-fast |
| `benchmark/runner/m0_fanout_runner.py` | 新增 | CLI + 矩阵编排 + server 生命周期（复用 e15 骨架）+ barrier 并发 + gate（G-M0-1..7）+ 唯一顶层 schema 落盘（§5.1） |
| `benchmark/framework/driver.py` | 修改 | **chat() keyword-only 扩展（temperature/seed/max_tokens/extra_body，§3.3）**，默认 None 保持旧行为 |
| `benchmark/tests/test_fanout_prompts.py` / `test_m0_fanout_runner.py` | 新增 | 纯函数（决策解析确定性/canary/**预算函数合法/非法组合表单测**/tokenize 客户端 mock/fail-fast/gate 判定/CLI）+ mock server e2e |
| `benchmark/tests/test_driver.py` | 修改 | 新增 chat 扩展用例（默认 None 旧行为、透传生效、keyword-only 兼容、extra_body 合并） |
| 文档 | 更新 | 本文件补实测结果 + AGENTS.md 状态更新 |

**不改**：`config.py`、`runner.py`、`workload/`。

CLI 合同（v4 收紧）：`--server-bin --model --port --ctx-size 4096 --fanout {2,4,8} --prefix-len {short,medium,long} --branch-len {short,medium,long} --ctk q8_0 --ctv q8_0 --decision-n-predict 16 --branch-n-predict 64 --tool-rounds M --warmup 2 --reps 5 --out <path> [--tmp-dir <dir>]`；`--parallel` 由 runner 按 `fanout+2` 计算（不接受手工覆盖）。**临时目录合同（对齐 E15 + M0 增强）**：`--tmp-dir` 为**可选用户参数**（缺省时 runner 用 `tempfile.mkdtemp()` 创建、退出时 `shutil.rmtree` 清理——**此"创建并主动清理"是 M0 相对 E15 的增强**，E15 runner 接受 `--tmp-dir` 但清理语义未承诺）；`--slot-save-path` **不作为独立用户参数**，由 runner 内部绑定为同一临时目录（`--slot-save-path <tmp-dir>`）传给 server——保证 `POST /slots/:id?action=erase` 可用（slot erase 依赖该路径，`e15_branch_concurrent.py:548-560` 先例）且生命周期随 runner 清理。预算 fail-fast：`budget(P,B,N) > 3481` → INVALID（§4.2，P/B 来自 tokenize 实测）。

测试计划：纯函数 pytest（决策点解析边界/确定性/canary/**预算函数合法与非法组合表**/tokenize 客户端 mock（HTTP 失败→chars/2.0 fallback）/gate_verdict 分支/CLI 校验/fail-fast）+ mock OpenAI server e2e（现有 `tests/mock_server.py` 模式）+ 可选 TinyLlama CPU smoke（**仅冒烟，不宣称 4B 结论**）。

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
| G-M0-1 决策确定性（v6：专用验证协议，与矩阵分离） | **专用稳定性验证，与正式矩阵数据完全分离**：**2 次独立 server 会话，每次 ≥10 请求（总数 ≥20）**，temp=0/seed=42 下 4B 决策点合法 `ACTION: branch(bX)` 输出率 **100%**；**INVALID 谓词（三处同一）**：`(finish_reason=="length") OR (输出不含合法 ACTION)` → 任一即 INVALID（length 截断即使含 ACTION 也不可信）；**<100% → `NOT_VALIDATED`/HOLD**；**正式矩阵单 rep 决策失败仅记录该 rep INVALID 并走固定路由 fallback 继续内存压力实验，不计入 G-M0-1，不宣称真实决策 PASS**；fallback 触发 → `decision_fallback: true` + verdict 标记（§5.1） |
| G-M0-2 隔离 | canary 跨分支泄漏率 **0**（全部 reps×branches 输出） |
| G-M0-3 回收（v3：不形成伪门禁） | **G-M0-3a（门禁）**：每 rep 末 `erase` 后 `/metrics/kv` `used_cells==0 && active_sequences==0` —— **仅证明 attention cells 回收**；**G-M0-3b（smoke，非门禁）**：全部 reps 完成后 nvidia-smi `used` 回落至空载基线（±50 MiB）—— **只做显存泄漏 smoke，不证明 recurrent state 回收**；recurrent 回收**保持不可观测缺口**（无运行期接口，§5.1），M0 不承诺、不验收 |
| G-M0-4 内存归因（v4：启动日志精确值门禁） | **主门禁**：启动日志 `RS buffer size`（`llama-memory-recurrent.cpp:115`）精确值 vs 公式 `24×548864×4×parallel` 对账 **误差 ≤5%**；attention KV 同理（`llama_kv_cache` 分配日志 68/128 MiB vs `8×8×128×2×elem×4096`）；**GPU used 差分只作总量 sanity**（阈值 ±10%，口径 = nvidia-smi `memory.used` 增量，探针观测 run-to-run 波动 <10%） |
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
