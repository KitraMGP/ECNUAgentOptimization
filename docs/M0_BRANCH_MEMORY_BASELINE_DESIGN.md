# M0：真实智能体多路径决策 workload + 分支内存基线（技术调研与设计）

> 状态：**调研完成 + 详细设计（DESIGN ONLY）；未实现 workload 代码、未运行正式 benchmark**。
> 日期：2026-08-09 ｜ 对应路线：M0（阶段 0 通过后第一条后续路线）｜ 实施建议：**GO**（见 §8）。
> 修订：2026-08-09 v2（矩阵/预算/层数/架构/接口合同/回收观测）；v3（精确 token 计数、矩阵手算修正、driver 合同、门禁语义）；v4（driver `_retry` 位置参数兼容、thinking 字段生效实证、G-M0-1 与矩阵完全分离、RS 日志行号、CLI 临时目录）；v5（G-M0-1 口径统一、可提交证据、token parity 门禁、verdict 规则）；v6（INVALID 谓词 OR 语义、verdict/gates 值域映射、add_special 归因、parity 校准协议）；v7（gate/verdict 分层、parity 前置、schema 完整化、聚合定稿）；v8（preflight/schema 闭环）；v9（状态机/schema 终审）；v10（验证/聚合路径收口）；v11（跨章节同步定稿）；v12（运行边界合同）；v13（实现阻断收口）；v14（validator/测试合同终审）；v15（落盘/测试盲区终审）；v16（结构化错误/schema/测试残留终审）；v17（最后 Medium/Low 文档残留）；v18（终审阻断与规范残留）；v19（示例不变量终审）；**v20（决策统计不变量终审——INVALID_DECISION 两来源统计（session 增 `invalid_length`/`invalid_no_action`，`invalid == invalid_length + invalid_no_action`、`invalid_length == finish_reasons.get("length",0)`，删除 v19 的 `length==invalid` 总约束）、finish_reasons 不变量保留 `sum(values)==requests−error_count`（ERROR 请求一律不写 finish_reasons，畸形 200 携带 finish_reason 也不写）、output_hashes 只记录 OK/INVALID_DECISION 且长度 `== valid+invalid`（ERROR 不写 hash）、preflight meta 统一 `matrix_complete=false`（formal 正常 true、中断 false）、formal server 启动失败 code 固定（spawn 失败/早退=`server_crash`、health 超时=`health_failed`、端点缺失=`endpoint_unavailable`）、**算术/不变量人工与审查复核（实现阶段 validator pytest 为正式可复核证据，本调研阶段不创建针对文档源码文本的脆弱测试、不虚构已入库脚本）**、示例（partial/formal/⑱a/⑱b/in-flight）全部闭合、测试㉖ 唯一键继续 (code, stage ?? null, bucket ?? null)）**，见各节。
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

**driver**（`benchmark/framework/driver.py`）：OpenAI SDK 封装走 `/v1/chat/completions`（`:58`）；`chat()` 请求前 `preprocessor.process(messages)`（默认 off）、`kv_probe` 前后快照（`:75-103`）；返回行 `text/prompt_tokens/completion_tokens/total_tokens/cached_tokens/latency_ms/rss_mb/gpu_mb/timings`（`:89-99`）；`_extract_timings` 从 `timings` 属性 / `model_extra` / `__pydantic_extra__` 提取（`:160-180`）；**`_extra_body` 固定 `chat_template_kwargs.enable_thinking=False`（`:135-142`，no-think 保证可比）**；**400 超 ctx 重试**：`exceed_context_size_error` 且 `_retry<max_retry(3)` 时丢弃最早约 1/3 非 system 消息，若候选丢失全部真实 user query 则找回最早一条（Qwen3.5 multi_step_tool 模板要求，`:107-131`；`_is_real_user_query` 定义 `:147-158`）。**原生 `/completion` 未在 driver 封装**（e15_branch_concurrent 直接可用）。

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
| `/apply-template` → `/tokenize`（`add_special:true`） | 46（**幂等归因（v7 完整措辞）**：该 GGUF `tokenizer.ggml.add_bos_token=false`、`bos_token_id` 缺失、`add_eos_token` 字段缺失（默认 false）→ 无 BOS token 可加，add_special 无可加；**仅对本模型/配置成立，换模型必须重验**，GGUF 元数据实测见下） |
| 真实 `/v1/chat/completions`（max_tokens=1, temp=0, seed=42, no-think）`usage.prompt_tokens` | 46（finish_reason=length） |

→ **parity 成立、偏差 0**：M0 一致性门禁 = 比较 chat `usage.prompt_tokens` vs apply-template→tokenize(`add_special:false`)（§4.2 步骤 2/3）。**GGUF 元数据实测（2026-08-09）**：`tokenizer.ggml.add_bos_token=false`、`tokenizer.ggml.bos_token_id` 缺失（None）、`tokenizer.ggml.add_eos_token` 字段缺失（默认 false）、`eos_token_id=248046`、`tokenizer.ggml.model="gpt2"`——**add_special 幂等的真实原因是无 BOS token 可加（add_bos_token=false 且无 bos_token_id）**；**结论仅限本 GGUF/配置，换模型必须重验**。

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
  - **专用稳定性验证**（G-M0-1 数据源）：4B 决策点合法输出率 <100%（2 次独立会话 × ≥10 次，无 ACTION 或 `finish_reason=length` 均计 INVALID_DECISION）→ **G-M0-1 = FAIL → 顶层 `HOLD_NOT_VALIDATED`**（规则 `G1_FAIL`）——**不称真实决策 PASS**；
  - **正式矩阵**：单 rep 决策失败**只记录该 rep `status=INVALID_DECISION`**（rep 级字段，**不是顶层 verdict**，顶层 verdict 仅用 §5.1 五值枚举），并使用**固定路由 fallback**（脚本按固定规则选分支）**继续内存压力实验**（不中断、不重试），该 rep **不计入 G-M0-1**（G-M0-1 只用专用验证数据）；fallback 触发时在结果 schema `decision_fallback: true` 与顶层 `verdict` 显式标记（§5.1），报告不得宣称真实决策。
  - **INVALID_DECISION 谓词（v8 定稿，可直接编码，三处同一）**：`decision_invalid ⇔ (finish_reason == "length") OR (输出不含合法 ACTION)` —— **任一条件即 rep 判定 `INVALID_DECISION`**（rep 级，非顶层 verdict）；即使截断文本（length）已含合法 ACTION 也判 INVALID_DECISION（截断输出不可信）。专用验证与正式矩阵均用同一谓词。
- 分支语义稳定：分支 prompt = 公共上下文 + 决策点输出 + 分支专属任务 + 分支专属 canary；分支任务**不依赖模型继续决策**（后续轮次为固定后续推理/工具调用）。

**分支-工具-回收流**：
```
[公共上下文+决策点] → 模型输出 ACTION: branch(bX)
  → N 个分支请求并发发出（barrier；分支内容 = 公共前缀 + 各自任务 + canary）
  → 每分支 M 轮：工具结果注入（复用 tool_call 的 ACTION 协议或直接注入）→ 后续推理
  → 回收：全部分支完成后 erase（/slots/:id?action=erase，KVProbe.erase_slot）
```
- **identity 定义**：`branch`（b1..b8）是内容分支；`session` 是 workload 实例；`thread` 是并发出线程。canary 隔离：每分支专属 `CANARY-BX-<hex>` 注入分支 prompt 尾部，输出检测跨分支泄漏（复用 E15.1 G5 模式）。
- **工具注入（v5 定稿）**：**M0 不使用 OpenAI `tools` schema**（避免两条路径（apply-template vs chat）渲染差异与精确计数遗漏）；分支工具观测以**固定、无敏感、可审计的消息注入**（如 `<tool_response>` 文本块，复用 tool_call.py 协议）实现——精确计数请求因此不遗漏 tools；若未来改用 `tools` schema，则 /apply-template 与 /tokenize 两条路径必须携带**同一 tools schema**（并重新验证 token parity，§4.2 步骤 3）。

### 3.2 模块边界（**单一架构定稿**，审查修订）

**定稿：独立 M0 runner（与 E15 runner 约定一致），不注册第五场景**。理由：fanout 的决策/并发/门禁逻辑独立于现有四场景的"累积多轮对话"范式；注册场景需改 `config.py`/`runner.py`（场景发现、summary 处理、`extra` 语义），引入兼容性成本；独立 runner 与 `e15_branch_concurrent.py` 同为"专项 runner"先例，无第二套 config 约定（CLI 直接参数化，不走 BenchmarkConfig 扩展）。

- **复用**：`framework/driver.py`（OAI 封装/timings 提取）、`framework/kv_probe.py`、`framework/sampler.py`、`metrics/metrics.py summarize`、`e15_branch_concurrent.py` 的 server 生命周期（`start_server/stop_server`，`:548-599`）与 gate_verdict 纯函数模式。
- **新增（下一实现阶段，本阶段不写）**：
  - `benchmark/framework/fanout_prompts.py` —— 纯函数：决策点 prompt 构造（枚举约束）、分支扩展、canary 注入、`ACTION: branch(bX)` 解析、**token 精确计数（§4.2：/apply-template + /tokenize）**、KV 预算校验（§4 公式，fail-fast）。
  - `benchmark/runner/m0_fanout_runner.py` —— CLI + 矩阵编排 + server 生命周期 + 并发（barrier+ThreadPoolExecutor）+ gate + 落盘（唯一顶层 schema，§5.1）。
  - `benchmark/tests/test_fanout_prompts.py`、`benchmark/tests/test_m0_fanout_runner.py` —— 纯函数 + mock server e2e。
- **不改**：`config.py`、`runner.py`、`workload/__init__.py`、任何既有 workload（可回滚：删除新增文件即完全回滚，默认无行为改动）。
- **失败降级（v12 定稿）**：server 启动/health 失败（重试 3 次后）或 `/metrics/kv` 不可用等**基础设施失败 → 落盘 preflight 五键结果（`phase=preflight`、`preflight_status=FAILED`、verdict 按规则 `PREFLIGHT_INFRA` = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`）并停止**——**不使用"run 级 INVALID"措辞**（rep 级 status 仅 OK/INVALID_DECISION/ERROR，见 §5.1）；预算逐 unit（§4/§6，超限 unit 排除、全部被拒 → preflight `ALL_BUDGET_REJECTED` = `HOLD_NOT_VALIDATED`）；决策点 fallback → **`HOLD_NOT_VALIDATED`**（规则 `DECISION_FALLBACK`/`G1_FAIL`）。

### 3.3 请求接口合同（审查修订 v3）

| 项 | 合同 |
|---|---|
| 统一接口 | **OAI `/v1/chat/completions`**（`driver.chat`）——决策点与分支请求同一接口；**thinking 合同（v5 实证，§1.4 证据）**：server 默认 **thinking 开启**（apply-template 不传时渲染 `<|im_start|>assistant\n<think>\n`），必须显式 `chat_template_kwargs.enable_thinking=false` 才渲染空 think 块（`<think>\n\n</think>`）实现 no-think——该字段经 `oaicompat_chat_params_parse` 解析**实际生效**（`server-common.cpp:1095-1102` 覆盖默认；可复核证据 `benchmark/baseline/m0_probe_thinking_token_parity_20260809.json`）；M0 沿用 `driver._extra_body`（`driver.py:135-142`）显式 false，并以 token parity 一致性门禁（§4.2 步骤 3）执行验证；原生 `/completion` 仅为 e15 对照既有路径，M0 不用 |
| 确定性 | `temperature=0, seed=42`；`--n-predict`：**决策点 16（v3：由 8 上调，保证 `ACTION: branch(bX)` 输出完整且留边界）**、分支轮 64（CLI 可配 `--decision-n-predict/--branch-n-predict`） |
| 停止判定 | OAI `finish_reason`（stop/length）；**INVALID_DECISION 谓词（v8）**：`(finish_reason=="length") OR (输出不含合法 ACTION)` → 任一即该 rep `status=INVALID_DECISION`（rep 级，非顶层 verdict；length 截断即使含 ACTION 也不可信），走固定路由 fallback（§3.1）、**不计入 G-M0-1**（G-M0-1 仅用专用 2 次独立会话 × 每次 ≥10 请求（总数 ≥20）数据，§8） |
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
- **retry 构造合同（v12，任务 2）**：当前 `Driver.__init__` 的 `max_retry=3`（`driver.py:36`）与 `OpenAI(base_url, api_key)`（`:58`）——OpenAI SDK **隐式默认 `max_retries=2`**（对连接错误/429/5xx 重试），须显式可控。设计：
  - `Driver.__init__` 新增可选 **`sdk_max_retries: Optional[int] = None`**（None 时保持既有 SDK 默认行为，**不改变其他 workload**）；M0 显式 `sdk_max_retries=0`（SDK 层不重试，统一由外层 transient wrapper 控制，§3.8）；
  - M0 实例化 **`Driver(max_retry=1, sdk_max_retries=0)`**：内层 context400 最多 2 次 wire（首 + 内部重试 1），外层 transient 3 次逻辑调用 → **理论最大 6 wire**（§3.8 上限不变）；
  - 实施文件：`driver.py`（`__init__` 加 `sdk_max_retries` → 传给 `OpenAI(max_retries=...)`）+ `tests/test_driver.py`（默认 None 保持 SDK 行为、显式 0 禁用、max_retry=1 实例化语义）；§3.8 retry 分层引用本构造。
  - **context400 递归透传（v13，任务 4）**：`chat()` 内部递归 `self.chat(messages, _retry + 1)`（`driver.py:132`）**必须透传 `temperature`/`seed`/`max_tokens`/`extra_body`**（签名改为 `self.chat(messages, _retry + 1, temperature=..., seed=..., max_tokens=..., extra_body=...)`）；测试验证**重试前后参数完全保持**（mock 记录两次 wire 请求参数一致）；旧调用（单参数/位置 `_retry`）兼容仍保留。

### 3.4 请求序列与计时

每 replicate：决策点请求（slot 0）→（可选 erase 决策点 slot，见 §4 预算）→ N 分支并发（barrier，slot 1..N）→ 每分支 M 轮 → `erase` 全部 → 断言回基线。warmup≥1、reps≥5（与 `benchmark/README.md:145` 约定一致），中位数报告。

### 3.5 执行顺序（v12 定稿，H1/M2 收口）

```
1. server 就绪（--health 200）且 /metrics/kv、/apply-template、/tokenize 端点可用
   —— 启动/health/端点探测失败 → 基础设施失败 → 落盘 preflight INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE（M1）
2. 每长度桶分别校准：P 模板与 B/工具观测模板 apply-template+tokenize → parity 对比（max_tokens=1 chat，豁免 INVALID_DECISION 谓词）
   —— 校准期间请求异常/端点消失 → 基础设施失败 → preflight INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE（M1）
   —— 仅端点正常响应但 parity 数值非零 → preflight HOLD_NOT_VALIDATED（M1）
3. 精确 budget 校准（全部候选 unit 逐一 tokenize 实测）
   —— 单 unit 超预算 → 记入 meta.preflight_rejections[] 并排除该 unit；其余合法 unit 继续（M3-8）
   —— 全部 unit 被拒 → 落盘 phase=preflight HOLD_NOT_VALIDATED
4. erase 全部 slot → /metrics/kv used_cells==0 && active_sequences==0（attention 回基线）
5. G-M0-1 专用稳定性验证：2 次**独立 server 启动** × 每次 ≥10 请求（总数 ≥20）
   —— 证据落盘 meta.decision_validation（M2）；**<100% → G-M0-1=FAIL，但不停止**（v11 语义保留，v12 补失败路径）：
      formal 仍运行且**仍请求模型**，只有满足 INVALID_DECISION 谓词的 rep 才走 fixed fallback（`decision_fallback=true`），
      **合法 rep 保持 `status=OK`、`decision_fallback=false`**——不强制全部 rep fallback；
      G-M0-1 gate 已 FAIL → 顶层为 `HOLD_NOT_VALIDATED` 或更高优先级的 INVALID 类（规则 `G1_FAIL`/`DECISION_FALLBACK`，若并存 `REP_ERROR`/`FORMAL_INCOMPLETE` 则取 INVALID 类）
   —— **验证阶段失败路径（v12，任务 5）**：
      a) 验证中普通请求 ERROR/invalid：**如实记入 meta.decision_validation**（含结构化 error_summary），G-M0-1=FAIL；
         **验证请求 in-flight 时 server 崩溃（v18）**：该请求**计入 `requests` 与 `error_count`**，`error_summary` 加 `{code: "server_crash", stage: "validation", count: 1}`，**保证等式 `requests == valid + invalid + error_count` 成立**；测试覆盖；
         若**两次验证 server 均能完成**（health 正常、会话完整）→ **继续 formal**；
      b) 验证 server 启动失败/崩溃/证据无法落盘 → **phase=preflight 基础设施失败**（规则 `PREFLIGHT_INFRA` = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`）；
         `decision_validation` 可为 `null` 或 partial（validator 有则验证结构，**不强制 2 sessions**）；
      c) **仅 formal 才强制完整 2 sessions × 每次 ≥10**（§5.1 phase 约束）
6. formal 矩阵（6 合法组合 × 2 ctk × 2 对照 = 24 单元，每单元 warmup 2 + reps 5）：
   —— **使用新的独立 server 启动**（与验证会话分离），先确认 attention 基线（used_cells==0）再开始（M2 防污染）；
   —— **formal server 启动失败（v20）** → 落盘 `phase=preflight`、规则 `PREFLIGHT_INFRA`、`executed_units=0`、**`matrix_complete=false`**、**保留完整 decision_validation（验证证据不丢）**、`modes={}`、`gates={}`（§3.7 生命周期同步）；**启动失败 code 固定（v20）**：进程 spawn 失败/早退 → `server_crash`；进程存活但 health 超时 → `health_failed`；健康但必需端点缺失 → `endpoint_unavailable`
```

**前置失败分类（v10）**：步骤 1–4 的失败 → 落盘 preflight 五键结果并停止（verdict：基础设施 → `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`；parity 数值非零/全部 unit 预算被拒 → `HOLD_NOT_VALIDATED`）；步骤 5 的 G-M0-1 <100% **不是前置失败**（formal 继续，H1）。

### 3.6 transient retry 与端点归因（v12 定稿，M1/M3）

- **transient retry（M3）**：formal 与校准请求中，**连接错误 / 请求超时 / HTTP 5xx → 最多重试 2 次（总尝试 3 次），确定性退避 0.25s / 0.5s**；**HTTP 4xx 与"畸形成功响应"（200 但缺字段/JSON 畸形）不重试**——ctx 超限由预算保证（§4.2），driver 既有 400 context fallback 保持原行为，但重试/回退后仍失败即 `status=ERROR`（→ `any_rep_error`）；
- **重试耗尽 → rep ERROR → 规则 `REP_ERROR`（any_rep_error）**；校准请求重试耗尽 → 按 M1 归因（见下）；
- **端点归因唯一化（M1）**：
  - server 启动失败 / `/health` 失败 / 端点（/metrics/kv、/apply-template、/tokenize）探测失败、**校准期间请求异常或端点消失** → **基础设施失败 → preflight `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`**；
  - **仅当端点正常响应但 token parity 数值非零** → `HOLD_NOT_VALIDATED`（preflight）；
- **测试覆盖**：5xx 后重试恢复（总尝试 ≤3）、重试耗尽 → ERROR、4xx 不重试、畸形 200 不重试、校准端点消失 → 基础设施归因。

### 3.7 server 生命周期与崩溃处理（v12 定稿，任务 6/8）

- **生命周期（任务 8）**：
  1. **校准 server**：启动 1 次（步骤 1–4 全在此 server 完成），`finally` 停止；
  2. **G-M0-1 验证 server**：**两次独立启动/停止**（每次 ≥10 请求），每次 `finally` 停止；
  3. **formal server**：G-M0-1 验证会话结束后**新启动一次**（防污染），开始前确认 attention 基线（`used_cells==0`），全部 unit 跑完后 `finally` 停止；
     - **formal server 启动失败（v20）**：health 探测失败/进程启动失败 → 落盘 `phase=preflight`、规则 `PREFLIGHT_INFRA`、`executed_units=0`、**`matrix_complete=false`**、**保留完整 decision_validation**（验证证据不丢）、`modes={}`、`gates={}`，合法持久化（测试㉖）；**code 固定（v20）**：进程 spawn 失败/早退 → `server_crash`；进程存活但 health 超时 → `health_failed`；健康但必需端点（/metrics/kv、/apply-template、/tokenize）缺失 → `endpoint_unavailable`
  - **端口策略**：由 runner 分配可用端口（`socket` 绑定探测或端口池），不硬编码单一端口；启动前后做 health 检查与进程退出检查（`pgrep -x llama-server` 确认无残留，复用 e15 生命周期骨架 `e15_branch_concurrent.py:548-599`）。
- **formal server 崩溃/端点消失（任务 6，v12）**：**立即停止后续矩阵**（不再启动新 unit）；**保留已完成 rep**；当前进行中 rep 写 `status=ERROR`（`error_type="server_crash"`、可选 `error_stage`/`error_bucket`；**不保存原始异常/路径/URL/prompt**）；**`meta.matrix_complete=false`**、`executed_units < planned_units`、`notes` 记录中断；落盘 formal 五键结果；顶层 → 规则 **`FORMAL_INCOMPLETE`** = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`（**绝不 PASS**；依赖完整矩阵的 gates（G-M0-6 复现、G-M0-4 归因）置 `NOT_APPLICABLE`）。
- **单 rep ERROR 继续（任务 6，v12）**：**server 仍健康时**，单个 rep ERROR 后**继续剩余 matrix**（不立即停止）；ERROR rep 保留在落盘数据、不参与 G-M0-4 归因；矩阵正常跑完后 `matrix_complete=true`，顶层由规则 `REP_ERROR` → `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`；**仅 server 崩溃/端点消失才立即停止并置 `FORMAL_INCOMPLETE`**。

### 3.8 retry 分层（v12 定稿，任务 9）

- **外层 transient wrapper**：只处理**连接错误 / 请求超时 / HTTP 5xx**，**最多 3 次逻辑调用**（首次 + 重试 2 次，确定性退避 0.25s / 0.5s）；
- **内层 driver**：每次逻辑调用内，driver 对 **context 400**（`exceed_context_size_error`）最多**内部重试 1 次**（丢弃最早消息后重试，`driver.py:107-131` 既有逻辑）；**理论最大 wire request = 3 × 2 = 6 次**；
- **不外层重试**：HTTP 4xx（除 context 400 内层处理）与"畸形 200"（200 但缺字段/JSON 畸形）→ 直接 `status=ERROR`；
- **精确预算使 context 400 仅为安全兜底**（P/B 由 tokenize 实测，§4.2）；重试耗尽 → rep ERROR → `any_rep_error`（规则 `REP_ERROR`）。

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

**token 精确计数（审查修订，不再以 chars 估算为门禁；chars/2.5 仅为旧估算对照）**：
- **预算函数**：`budget(P, B, N) = P + N×(P+B)`（P=决策点/公共前缀 token 数，B=每分支后缀 token 数，N=fan-out）；约束 `budget ≤ 4096×0.85 = 3481 cells`（安全余量 15%）。
- **P/B 的取值必须来自 chat 模板渲染后的真实 prompt 精确 token 计数**（实现时）：
  1. `POST /apply-template`（`server.cpp:263`；`server-context.cpp:5775-5781`）：body `{messages, add_generation_prompt: true, chat_template_kwargs: {enable_thinking: false}}`，经 `oaicompat_chat_params_parse`（与 /v1/chat/completions 同一解析路径，**thinking=false 模板一致**；字段生效证据：`server-common.cpp:1095-1102` 解析覆盖 + 探针证据 §1.4）→ 响应 `{"prompt": "<渲染后完整 prompt>"}`；
  2. `POST /tokenize`（`server.cpp:261`；`server-context.cpp:5828+`）：body `{content: <prompt>, add_special: false}` → 响应 `{"tokens": [id, ...]}`，`P/B = len(tokens)`；
  3. **一致性门禁（v7：token parity + 校准/清理协议）**：
     - **执行时机（独立前置校准）**：正式矩阵开始前、server 就绪后，**对每个长度桶分别校准两个模板**：公共决策点 P 模板与分支 B/工具观测模板（各自 messages 构造）；比较真实 chat 请求（`max_tokens=1, temp=0, seed=42`, no-think）的 `usage.prompt_tokens` 与 `/apply-template`→`/tokenize(add_special:false)` 的 token 数；
     - **max_tokens=1 校准请求豁免决策 INVALID_DECISION 谓词（v8）**：校准请求仅用于 parity 计数，**不进入任何 rep/gate、不触发决策 INVALID_DECISION 判定**（其 `finish_reason=length` 是预期的）；
     - **允许偏差 0**（实测成立，§1.4；add_special 幂等归因 = 该 GGUF 无 BOS token，**换模型必须重验**）；
     - **偏差非零 → 前置 `HOLD_NOT_VALIDATED`（规则 `PARITY_MISMATCH`）并停止正式矩阵**（落盘 preflight，不产生矩阵结果），不做未经验证的自动补偿；若确需补偿，必须写入 `meta.parity_compensation`（含确定公式）并有对应单测（当前策略 `parity_compensation=null`）；
     - **校准后状态清理**：校准完成后 `erase` 全部 slot 并确认 `/metrics/kv` `used_cells==0 && active_sequences==0`（attention 回基线），**之后才采集正式基线**；
     - **mock 测试只校验请求参数一致性**（messages、thinking=false、temperature、seed、max_tokens），不声称比较渲染结果；
  - 探针实测（2026-08-09，4B，中文为主）：**旧估算对照 chars/2.5**（历史 v2 使用，仅作对照，不作为当前方法名/门禁）低估真实 token 数 **7.4%（short）→ 12.6%（medium）→ 16.3%（long）**（真实 chars/token ≈ 2.1–3.1，中文 1 字常 1–2 token）——**long 边界下 est=3400 实际可达 ~3944 > 3481 预算**；**正式诊断 fallback 使用 `chars/2.0`（更保守，v15）**，仅写入 notes 诊断字段，不作为正式 token_count_method 主值。
- **失败降级（v18 定稿，端点归因全文同步）**：`/apply-template` 或 `/tokenize` **探测失败、请求异常、端点消失或 schema 无法解析**（HTTP 非 200 / JSON 畸形 / 缺必需字段）→ **基础设施失败 → 落盘 preflight（规则 `PREFLIGHT_INFRA` = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`）并停止**；**仅当端点正常响应但 token parity 数值非零** → 落盘 preflight（规则 `PARITY_MISMATCH` = `HOLD_NOT_VALIDATED`）并停止；**预算超限同样落盘 preflight 五键结果（规则 `ALL_BUDGET_REJECTED` = `HOLD_NOT_VALIDATED`）——已取消 v8 的"不生成文件"行为**（保证证据不丢）；**`token_count_method` 映射穷尽（v18）**：formal / `PARITY_MISMATCH` / `ALL_BUDGET_REJECTED` → `"apply-template+tokenize"`；`PREFLIGHT_INFRA` → **恒 `null`**（计数前或部分完成后端点消失，进度记入 `parity_progress`，不再有"或记录已完成方法"分支）；`chars/2.0` 仅作为 notes/meta **诊断字段的估算方法**（`notes["est_method"]="chars/2.0-diagnostic"`），**不作为正式 `token_count_method` 主值**，**不允许绕过 parity 开始正式矩阵**。
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
           "phase": "preflight|formal",           # v9：唯一 phase 字段（必含）
           "preflight_status": "FAILED|N/A",      # v10：仅两值（preflight 时 FAILED，formal 时 N/A）
           "preflight_reason": null,              # v16：固定 error code 或 code 列表（无自由文本）；phase=preflight 时必填
           "decision_validation": {"sessions": [  # v13（M2/G-M0-1 证据）：两次独立 server 启动；formal 强制 2 sessions×≥10，preflight 允许 null/partial
               {"server_start": 1, "requests": 10, "valid": 10, "invalid": 0,
                "invalid_length": 0, "invalid_no_action": 0,   # v20：invalid = invalid_length + invalid_no_action；invalid_length = finish_reasons["length"]
                "error_count": 0,
                "finish_reasons": {"stop": 10, "length": 0},
                "output_hashes": ["<sha256...>"], "error_summary": []}],   # v16：无错误时空数组 []；有错误时 [{code, stage?, bucket?, count}]（count 必需正整数）；requests = valid + invalid + error_count
               "total_valid_rate": 1.0},          # v15：decision_validation 级聚合 = sum(valid)/sum(requests)（总 requests=0 时 null）；error_count>0 或 invalid>0 → G-M0-1=FAIL
               # partial 示例（v20，preflight 合法；全部不变量闭合）：{"sessions": [{"server_start": 1, "requests": 5, "valid": 4,
               #   "invalid": 1, "invalid_length": 1, "invalid_no_action": 0, "error_count": 0,
               #   "finish_reasons": {"stop": 4, "length": 1}, "output_hashes": ["h1","h2","h3","h4","h5"],   # v20：length 型 INVALID_DECISION（有响应可解析 → 有 hash）；len(output_hashes)=5=valid+invalid
               #   "error_summary": []}],
               #   "total_valid_rate": 0.8}   # requests=5=4+1+0（等式）；invalid=1=invalid_length(1)+invalid_no_action(0)（两来源）；finish_reasons[length]=1=invalid_length；sum(finish_reasons.values())=5=5-0；len(output_hashes)=5=valid+invalid；sum(valid)/sum(requests)=4/5
           "preflight_rejections": [],            # v10（M3-8）：[{unit, prefix_len, branch_len, fanout, budget, threshold}]，空数组=无排除
           "planned_units": 24,                   # v14：理论预算筛选后的合法矩阵候选数（6 合法组合 × 2 ctk × 2 control = 24）
           "executed_units": 24,                  # v14：实际执行的 unit 数；= planned_units − |preflight_rejections| 仅当 matrix_complete=true 且无崩溃中断时成立；preflight_rejections 只记录精确 tokenizer 复核后进一步被拒的 unit
           "matrix_complete": true,               # v12：formal 是否完整跑完（崩溃/提前终止 → false → FORMAL_INCOMPLETE）
           "decision_validated": true|false,
           "decision_fallback": false|true,        # 任一正式矩阵 rep fallback 时 true（聚合）
           "parity_ok": true|false|null,           # v13：三值（true=parity 通过；false=PARITY_MISMATCH；null=PREFLIGHT_INFRA 未完整执行）
           "parity_progress": {"completed": ["short-P", "short-B"], "errors": []},  # v18：{completed: list[str], errors: list[{code, stage?, bucket?}]}；preflight 可为空结构；PREFLIGHT_INFRA 部分完成时保存；formal 记录完整进度；不改变 verdict
           "parity_compensation": null,            # v7：当前策略不补偿，恒 null；如未来启用须为确定公式对象
           "token_count_method": "apply-template+tokenize"|null},  # v14：仅两值——正式主值 apply-template+tokenize（formal/PARITY_MISMATCH/ALL_BUDGET_REJECTED）或 null（PREFLIGHT_INFRA，计数未完整建立）；chars/2.0 仅作为 notes/诊断字段的估算方法，不作为正式主值
  "modes": {"off": {"start": ..., "replicates": [...], "stop": ...},
            "on":  {"start": ..., "replicates": [...], "stop": ...}},
  "gates": {"G-M0-1": {"status": "PASS|FAIL|NOT_APPLICABLE"}, "...": {...}},
  "verdict": "PASS|HOLD_NOT_VALIDATED|HOLD_UNSTABLE_MEASUREMENT|REJECT_CORRECTNESS_OR_ISOLATION|INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE",
  "notes": [...]
}
```

每 replicate 行字段（§5.2）：含 **`decision_fallback: bool`（v5：该 rep 是否触发固定路由 fallback）** 与 **`status`（rep 级，取值 `OK | INVALID_DECISION | ERROR`；`INVALID_DECISION` 不是顶层 verdict）**；KV 观测经 `KVProbe.run_aggregate`（first/last/peak_used_cells）。

**两个 phase 的合法 schema 变体（v8 定稿）**——唯一顶层键集始终为 `{meta, modes, gates, verdict, notes}`：

| phase | 触发 | meta 关键字段 | modes | gates | verdict | G-M0-7 |
|---|---|---|---|---|---|---|
| `preflight` | parity 失败（数值非零 / 端点异常 / schema 无法解析）或预算全部被拒或 server/health/基础设施失败**或 formal server 启动失败（验证证据完整，测试㉖，v18）** | `phase="preflight"`, `preflight_status="FAILED"`, `preflight_reason=<结构化错误码（规则映射见 §5.1）>`, **`parity_ok` 三值**：`ALL_BUDGET_REJECTED` → `true`；`PARITY_MISMATCH` → `false`；`PREFLIGHT_INFRA` → `null`；**`token_count_method`（v18 彻底唯一）**：`ALL_BUDGET_REJECTED`/`PARITY_MISMATCH` → `"apply-template+tokenize"`；**`PREFLIGHT_INFRA` → 恒 `null`**（不再有"或记录已完成方法"分支）；**`parity_progress`（v18）**：仅记录已完成 P/B 桶与结构化错误码（`{completed: [...], errors: [{code, stage?, bucket?}]}`），**不改变 verdict** | `{}`（无矩阵运行） | `{}`（不评估任何 gate） | `HOLD_NOT_VALIDATED`（`PARITY_MISMATCH`/`ALL_BUDGET_REJECTED`）或 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`（`PREFLIGHT_INFRA`） | **不评估**（仅 formal 评估） |
| `formal` | parity 通过且预算通过 | `phase="formal"`, `parity_ok=true`, `token_count_method="apply-template+tokenize"` | `{off:{...}, on:{...}}` | `G-M0-1..7`（G-M0-3b 恒 NOT_APPLICABLE） | §5.1 verdict 规则五值 | 评估（缺键 → FAIL → INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE） |

- **schema 判定规则（v9 可编码）**：**validator 在 phase 分支前先验证顶层键集精确等于 `{meta, modes, gates, verdict, notes}`**（preflight 与 formal 均强制，缺任一键即非法）；`phase=preflight` ⇔ `modes={} ∧ gates={}`（verdict 为 `HOLD_NOT_VALIDATED` 或 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` 之一，由前置失败类型决定）；`phase=formal` ⇔ `modes 非空 ∧ gates 非空`；任何文件 `phase=preflight` 且 `gates≠{}`、或顶层键集不精确 → 非法 schema；
- **G-M0-7 评估范围（v9）**：仅 `phase=formal` 时评估；**formal 缺 `gates["G-M0-7"]` 键 → 完整性检查先置该 gate FAIL**（聚合用 `get(default)` 不 KeyError，§伪代码）；preflight 不评估任何 gate。
- **phase 字段约束（v12，validator 校验必需字段与类型）**：
  - `phase=preflight`：`decision_validation` **三种形态均合法（v20）：`null` / partial / 完整 2 sessions×≥10**——partial 或完整时 validator 校验已有 session 结构（`requests/valid/invalid/error_count/finish_reasons/output_hashes/error_summary` 类型正确、**等式 `requests == valid + invalid + error_count` 对 preflight partial 与 formal 均强制（v18）**、**INVALID_DECISION 两来源（v20）**：session 增 `invalid_length` 与 `invalid_no_action`，**`invalid == invalid_length + invalid_no_action`**、**`invalid_length == finish_reasons.get("length", 0)`**（length 型 INVALID_DECISION）、`invalid_no_action` = finish_reason 非 length 但输出无合法 ACTION 的请求数（**删除 v19 的 `length==invalid` 总约束**，改为上述两来源分解）、**finish_reasons 不变量（v20）**：`sum(finish_reasons.values()) == requests - error_count`（error 请求无响应/无 finish_reason；**任何 status=ERROR 请求一律不写入 finish_reasons，即使畸形 200 携带 finish_reason**）、所有 count（valid/invalid/error_count/invalid_length/invalid_no_action/finish_reasons 值）为非负整数、**`output_hashes` 约束（v20）**：只记录有可解析响应的 OK/INVALID_DECISION 请求，**长度必须 `== valid + invalid`**、ERROR 不写 hash、`total_valid_rate` 为 decision_validation 级聚合 `= sum(valid)/sum(requests)`（总 requests=0 时 null））但**不强制 2 sessions / 每次 ≥10**（仅 formal 强制）；**formal server 启动失败（验证证据完整时）→ `PREFLIGHT_INFRA` 落盘：保留完整 decision_validation、`modes={}`、`gates={}`、`executed_units=0`、**`matrix_complete=false`（v20：所有 preflight 文件统一 false）**，为合法持久化（测试㉖）**；`preflight_rejections` **必须为数组**（基础设施/parity 失败时为 `[]`；全部 unit 预算被拒时为含全部排除项的数组）；`modes={}`、`gates={}`；**`parity_ok` 三值语义**：`ALL_BUDGET_REJECTED` 发生在 parity 通过后 → **必须 `parity_ok=true`**；`PARITY_MISMATCH` → **必须 `parity_ok=false`**；`PREFLIGHT_INFRA`（含 parity 部分完成后端点消失）→ **统一 `parity_ok=null`**（parity 未完整通过，`parity_progress` 仅记录已完成桶与结构化错误码，不改变 verdict）；违反 → `SCHEMA_INVALID`；
  - `phase=formal`：`decision_validation` **必须含两次独立 server 会话证据**（`sessions` 长度 = 2、每会话 `requests ≥ 10`、`valid/invalid/error_count` 为非负整数且**等式 `requests == valid + invalid + error_count` 成立（v16，preflight partial 与 formal 均强制）**、**INVALID_DECISION 两来源（v20，同 preflight）**：`invalid == invalid_length + invalid_no_action`、`invalid_length == finish_reasons.get("length", 0)`、**finish_reasons 不变量（v20）**：`sum(finish_reasons.values()) == requests - error_count`（ERROR 请求一律不写 finish_reasons，畸形 200 携带 finish_reason 也不写）、所有 count 非负整数、**`output_hashes` 长度 `== valid + invalid`（v20，ERROR 不写 hash）**、`finish_reasons`/`output_hashes`/`error_summary`（结构化错误码，无错误 []）类型正确；`total_valid_rate` = **decision_validation 级聚合 `sum(valid)/sum(requests)`**，**总 requests=0 时必须 `null`（v16）**）；`preflight_rejections` 必须为数组（可为空）；`planned_units`/`executed_units` 为正整数且 `executed_units ≤ planned_units`；**`matrix_complete`（v12）必须为 bool，且 `matrix_complete=true` 时 `executed_units == planned_units − |preflight_rejections|`**；`parity_ok` 必须 `true`；`modes` 非空、`gates` 非空；
  - 违反任一约束 → `SCHEMA_INVALID`。
- **rep status 状态机（v10 定稿，H3 + 逻辑约束）**：每个正式矩阵 replicate 的 `status` 取值：
  - `OK`：请求成功（HTTP 200）、响应字段完整、决策合法（`finish_reason=stop` 且含合法 ACTION）；**必须 `decision_fallback=false`**；
  - `INVALID_DECISION`：`(finish_reason=="length") OR (输出不含合法 ACTION)`（谓词三处同一）→ **走固定路由 fallback 继续**（§3.1），**`decision_fallback=true`（双向 ⇔：status=INVALID_DECISION ⇔ decision_fallback=true）**，不计入 G-M0-1，**可参与 G-M0-4 内存归因**，但排除真实决策质量声明；
  - `ERROR`：HTTP 最终非 200（按 §3.6 transient retry 后仍失败）、请求超时、响应缺字段/畸形、请求异常 → **必须 `decision_fallback=false` 且 `error_type` 为固定 error code（v16，可选 `error_stage`/`error_bucket`；不保存原始异常/路径/URL/prompt）**；**任何 formal rep ERROR → 顶层 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`**（规则 `REP_ERROR`），**ERROR rep 不参与 G-M0-4 数值归因**；
  - **server 启动/health/endpoint 失败不属于 rep status**——它们是 preflight 基础设施失败（§3.2/§3.6 M1），落盘 preflight 五键结果。
- **G-M0-7 对 formal 每 rep 校验**：`status`（三值）、`decision_fallback`（bool）、`error_type`（固定 error code，ERROR 时必填）、可选 `error_stage`/`error_bucket` 等必需键（§5.2）；缺键 → G-M0-7 = FAIL。
- **结构化错误码（v16，任务 5/10）**：**不保存原始异常字符串/路径/URL/prompt**；`error_type`、`preflight_reason`、`error_summary`、`parity_progress.errors` 一律使用固定枚举 code（可选 `stage`/`bucket` 字段）：
  - **枚举**：`connection_error`、`timeout`、`http_4xx`、`http_5xx`、`malformed_response`、`server_crash`、`health_failed`、`endpoint_unavailable`、`parity_mismatch`、`budget_rejected`、`validation_incomplete`；
  - **表示形式（v16）**：`error_summary` = **无错误时统一 `[]`，有错误时 `[{code, stage?, bucket?, count}]`（count 必需正整数）**；`parity_progress.errors` = `[{code, stage?, bucket?}]` 固定对象；`preflight_reason` = 单个固定 code 或 code 列表（无自由文本）；
  - **validator 只接受上述枚举与值类型**（`stage ∈ {calibration, validation, formal}`、`bucket ∈ {short, medium, long}`、`count` 为正整数）；**测试禁止任意 raw message**（任何非枚举字符串 → `SCHEMA_INVALID`）。
  - **`error_stage`/`error_bucket` 语义（v18）**：`error_stage` 标识错误发生阶段（calibration/validation/formal）；**`error_bucket` 为当前 unit 的长度桶（short/medium/long）**；**非 unit 级错误（如 server 启动/health/端点探测）可省略 `bucket`**。
  - **`error_summary` 约束（v18）**：每 session **`sum(error_summary[].count) == error_count`**；**唯一性（v18）**：省略的 `stage`/`bucket` 视为 `null`，**比较键 = `(code, stage ?? null, bucket ?? null)`**；相同键必须聚合为唯一项（不重复条目）；`count` 为正整数；违反任一 → `SCHEMA_INVALID`。
  - **`preflight_reason` 规则映射（v18）**：
    | 前置失败类型 | 允许的 code 集合 |
    |---|---|
    | `PARITY_MISMATCH` | 仅 `parity_mismatch` |
    | `ALL_BUDGET_REJECTED` | 仅 `budget_rejected` |
    | `PREFLIGHT_INFRA` | 基础设施类：`connection_error` / `timeout` / `http_4xx` / `http_5xx` / `malformed_response` / `server_crash` / `health_failed` / `endpoint_unavailable` / `validation_incomplete`（按实际阶段） |
    支持 code 列表，但**每项必须属于对应集合**；错配（如 PARITY_MISMATCH 带 `budget_rejected`）→ `SCHEMA_INVALID`（测试覆盖）。
- **测试要求（v12 更新，规则名称引用）**：① preflight 变体单测（`PREFLIGHT_INFRA`/`PARITY_MISMATCH`/`ALL_BUDGET_REJECTED` 三路径 → 五键精确匹配，verdict 分别为 INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE / HOLD_NOT_VALIDATED / HOLD_NOT_VALIDATED）；② formal 变体单测（全 gate status 三值枚举、verdict 五值枚举、`gates["G-M0-7"].status` 仅在 formal 存在）；③ 非法 schema 检测单测（顶层键集多/缺键、preflight 携带 gates、**phase 字段约束违反（v15）**：**preflight 的 decision_validation 三种形态（null / partial / 完整 2 sessions×≥10）均合法**（只要结构合法——仅表示 formal modes 尚未产生，不因验证证据完整而非法）；**formal 缺完整 2 sessions×≥10 证据非法**；formal 缺两会话证据、executed>planned、matrix_complete=true 但 executed≠planned−|rejections|、session 等式 requests≠valid+invalid+error_count）→ `SCHEMA_INVALID`；补正反单测）；④ **verdict 持久化证据**：preflight 文件即持久化输出（含 `preflight_status=FAILED`/`preflight_reason`/`parity_ok`）；⑤ rep 状态机单测（OK/INVALID_DECISION/ERROR 判定与逻辑约束：INVALID_DECISION⇔fallback、OK/ERROR 必须 fallback=false、**ERROR 必填 `error_type`（固定 error code，v18）且 `error_stage`/`error_bucket` 可选，不保存任何自由文本**）；⑥ **并存优先级单测**（`REP_ERROR` + `G1_FAIL` + fallback 并存 → 顶层仍 INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE；`G7_SCHEMA_FAIL` 高于 `DECISION_FALLBACK`；`FORMAL_INCOMPLETE` 高于一切 HOLD 类）；⑦ **aggregate_verdict 签名与 validator 前置单测**（`aggregate_verdict(doc, any_fallback, any_rep_error, any_preflight_rejection)`，schema 非法 → `SCHEMA_INVALID` 直接返回，不进入聚合；preflight 分支返回 `doc["verdict"]`）；⑧ **preflight_rejections/planned/executed 单测**（单 unit 被拒排除、`executed_units=planned−|rejections|`、其余继续、`PARTIAL_REJECTION` → HOLD_NOT_VALIDATED、全部被拒 → `ALL_BUDGET_REJECTED` preflight HOLD）；⑨ **decision_validation 落盘单测**（两次独立 server 启动、字段完整、formal 新 server 启动防污染）；⑩ **transient retry 分层单测（v20）**（外层 3 次逻辑调用处理连接/超时/5xx、内层 context 400 内部重试 1 次、理论最大 6 wire、4xx/畸形 200 不外层重试、耗尽→ERROR；**畸形 200 正反用例（v20）**：畸形 200（缺字段/JSON 畸形）即使携带 finish_reason → `status=ERROR` 且**不写入 finish_reasons**（反例：写入则 SCHEMA_INVALID）；正常 200 → 正常计数）；⑪ **formal server 崩溃单测**（崩溃 → 停止后续、保留已完成 rep、当前 rep ERROR、`matrix_complete=false`、`FORMAL_INCOMPLETE`）；⑫ **FORMAL_INCOMPLETE 单测**（`matrix_complete=false` → 顶层 INVALID、**绝不 PASS**、依赖完整矩阵 gates（G-M0-4/6）置 NOT_APPLICABLE）；⑬ **验证 server 失败路径单测**（两次验证 server 均完成但含 ERROR/invalid → G-M0-1=FAIL 且继续 formal；验证 server 启动失败/崩溃 → preflight `PREFLIGHT_INFRA`、`decision_validation` null/partial 合法；仅 formal 强制 2 sessions×≥10）；⑭ **rep ERROR 继续单测**（server 健康单 rep ERROR → 继续剩余 matrix、`matrix_complete=true`、`REP_ERROR`）；⑮ **SDK 重试禁用单测**（`Driver(sdk_max_retries=0)` → `OpenAI(max_retries=0)`；默认 None 保持 SDK 行为；`Driver(max_retry=1, sdk_max_retries=0)` 的 6-wire 上限成立）；⑯ **parity_ok 三值 validator 单测**（`ALL_BUDGET_REJECTED` → `parity_ok=true`；`PARITY_MISMATCH` → `false`；`PREFLIGHT_INFRA`（含 parity 部分完成后端点消失）→ `null`；违反 → `SCHEMA_INVALID`）；⑰ **端口生命周期单测**（`--port-base` 占用时向上探测、未给时动态分配、校准/验证×2/formal 四次进程均 finally 停止且 `pgrep` 无遗留）；⑱a **decision_validation 聚合纯函数单测（v20）**（**独立 fixture：纯聚合函数**用非对称 sessions（s1={requests:10, valid:10, invalid:0, invalid_length:0, invalid_no_action:0, error_count:0}、s2={requests:5, valid:4, invalid:1, invalid_length:1, invalid_no_action:0, error_count:0}）验证 `total_valid_rate = sum(valid)/sum(requests) = 14/15`，总 requests=0 时 null；**不得用 s2 requests=5 的 doc 作为 formal validator fixture**）；⑱b **formal validator 判定单测（v20）**（**合法 formal doc（每 session requests≥10）**验证 `invalid>0` 或 `error_count>0` → G-M0-1=FAIL；**INVALID_DECISION 两来源三型用例（v20）**：① length 型（`invalid_length>0, invalid_no_action=0`，`invalid_length==finish_reasons["length"]`）；② stop+无 ACTION 型（`invalid_no_action>0, invalid_length=0`，finish_reason 非 length 但输出无合法 ACTION）；③ 混合型（两者均 >0，`invalid==invalid_length+invalid_no_action`）；**output_hashes 长度 `== valid+invalid`**（ERROR 不写 hash）；`error_summary` 为结构化枚举对象校验（非自由文本"去敏"）；preflight partial 校验结构但不强制 2 sessions×≥10）；⑲ **Driver context400 递归透传单测**（首次与重试 wire 的 temperature/seed/max_tokens/extra_body 完全一致；旧 `chat(msgs, 2)` 位置传参兼容）；⑳ **FORMAL_INCOMPLETE 优先单测**（`matrix_complete=false && any_rep_error=true` → 顶层 FORMAL_INCOMPLETE 而非 REP_ERROR）；㉑ **parity_progress 单测（v18）**（类型 `{completed: list[str], errors: list[{code, stage?, bucket?}]}`，**errors 元素必须为结构化枚举对象，非枚举对象（自由文本/任意字符串）→ `SCHEMA_INVALID`**；preflight 空/缺进度用空结构、PREFLIGHT_INFRA 部分完成保存、**不改变 PREFLIGHT_INFRA verdict**、validator 校验类型与枚举）；㉒ **决策 ACTION 解析/确定性/canary 单测**（`ACTION: branch(bX)` 解析边界（缺失/畸形/多行/大小写）、决策点 prompt 枚举约束下 temp=0/seed=42 确定性、canary 注入与跨分支检测）；㉓ **apply-template+tokenize mock 与端点失败单测**（正常响应 → token 数正确；HTTP 非 200/JSON 畸形/缺字段 → `PREFLIGHT_INFRA` 落盘、不降级继续）；㉔ **budget 函数组合表单测**（6 合法组合（§4.2 表）全部通过、3 非法组合（long×4、medium×8、long×8）全部拒绝；精确 tokenizer 复核后 rejection 记录到 `preflight_rejections[]`）；㉕ **CLI 参数/port-base/fail-fast 单测**（`--fanout`/`--prefix-len`/`--branch-len` 组合校验、`--port-base` 占用向上探测、预算超限 fail-fast 报错且落盘 preflight）；㉖ **formal server 启动失败落盘单测（v19）**（验证证据完整时 formal server 启动失败 → `phase=preflight`、规则 `PREFLIGHT_INFRA`、**保留完整 decision_validation（2 sessions×≥10）**、`modes={}`、`gates={}`、`executed_units=0`、合法持久化；phase 约束明确该值；另含 **preflight_reason 错配 code → SCHEMA_INVALID**（如 PARITY_MISMATCH 带 `budget_rejected`）与 **error_summary 约束**（sum(count)==error_count、**唯一性键 `(code, stage ?? null, bucket ?? null)`（v19）：省略字段与显式 null 视为同键，未聚合 → SCHEMA_INVALID**）与 **finish_reasons 不变量**（sum(values)==requests−error_count、length==invalid，正反用例）。

**verdict 确定规则（v7 定稿：gate 与顶层分层，确定性聚合）**：

- **`gates.*.status` 值域**：`PASS | FAIL | NOT_APPLICABLE`（required gates = G-M0-1、G-M0-2、G-M0-3a、G-M0-4、G-M0-5、G-M0-6、G-M0-7；G-M0-3b 为 smoke 观测，status 恒 `NOT_APPLICABLE` 且不计入判定）。
- **顶层 `verdict` 值域**：`PASS | HOLD_NOT_VALIDATED | HOLD_UNSTABLE_MEASUREMENT | REJECT_CORRECTNESS_OR_ISOLATION | INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`（单一枚举值，不使用枚举外字符串）。
- **分层原则**：`gates` 只反映**各自测量域**的 PASS/FAIL（G-M0-1 只由专用 2×≥10 验证决定，**正式矩阵 fallback 不改变 G-M0-1 status**）；fallback / parity / 预算超限等**顶层聚合条件**作为独立判定项，在映射表中显式给出 gate 状态与顶层 verdict 的组合。
- **verdict 确定规则（v12 定稿：稳定规则名称，跨章节唯一引用）**：

- **稳定规则名称表（全文唯一引用，不再用易漂移的编号）**：

| 规则名 | 条件 | gates 状态 | verdict |
|---|---|---|---|
| `SCHEMA_INVALID` | schema validator 前置失败（顶层键集不精确 / phase 非法 / 值域越界） | （validator 拒绝，不评估 gates） | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `PREFLIGHT_INFRA` | 前置：基础设施失败（server 启动/health/端点探测失败、校准期间请求异常/端点消失，M1）**或 formal server 启动失败（验证证据完整，测试㉖，v18）**→ 落盘 preflight | gates = `{}` | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `PARITY_MISMATCH` | 前置：端点正常但 token parity 数值非零（M1）→ 落盘 preflight | gates = `{}` | `HOLD_NOT_VALIDATED` |
| `ALL_BUDGET_REJECTED` | 前置：全部 unit 预算被拒（§3.5 步骤 3）→ 落盘 preflight | gates = `{}` | `HOLD_NOT_VALIDATED` |
| `FORMAL_INCOMPLETE` | formal：`matrix_complete=false`（server 崩溃/提前终止，含 unit 间隙，§3.7）→ 落盘已完成 rep，**依赖完整矩阵的 gates 置 NOT_APPLICABLE**（**优先于 REP_ERROR**：崩溃期间写入的 ERROR rep 不改变归属） | 受影响 gate = NOT_APPLICABLE | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `REP_ERROR` | formal：`matrix_complete=true` 且任一 rep `status=ERROR`（`any_rep_error=true`；server 健康时单 rep ERROR 后**继续剩余 matrix**，§3.7） | 各 gate 保持原值（ERROR rep 不参与 G-M0-4 归因） | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `G7_SCHEMA_FAIL` | formal：G-M0-7 FAIL（落盘缺键/逻辑约束违反，缺键先置 FAIL） | G-M0-7 = FAIL | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `PARTIAL_REJECTION` | formal：`meta.preflight_rejections[]` 非空（≥1 unit 被拒，其余执行） | 各 gate 正常评估 | `HOLD_NOT_VALIDATED` |
| `DECISION_FALLBACK` | formal：任一 rep `decision_fallback=true`（`any_fallback=true`） | **G-M0-1 保持专用验证原值** | `HOLD_NOT_VALIDATED` |
| `G1_FAIL` | formal：专用验证合法率 <100%（G-M0-1=FAIL） | G-M0-1 = FAIL | `HOLD_NOT_VALIDATED` |
| `CORRECTNESS_FAIL` | formal：G-M0-2 隔离 / G-M0-5 对照 / G-M0-3a 回收任一 FAIL | 对应 gate = FAIL | `REJECT_CORRECTNESS_OR_ISOLATION` |
| `STABILITY_FAIL` | formal：G-M0-6 复现 / G-M0-4 归因任一 FAIL | 对应 gate = FAIL | `HOLD_UNSTABLE_MEASUREMENT` |
| `ALL_PASS` | formal：全部 required gates PASS 且无 fallback / rep ERROR / rejection | 全 PASS | `PASS` |

- **优先级（高→低，命中即终值）**：`SCHEMA_INVALID` = `PREFLIGHT_INFRA` = `FORMAL_INCOMPLETE` = `REP_ERROR` = `G7_SCHEMA_FAIL`（INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE 类；**`FORMAL_INCOMPLETE` 在 `REP_ERROR` 之前**——matrix_complete=false 先命中）> `PARITY_MISMATCH` = `ALL_BUDGET_REJECTED` = `PARTIAL_REJECTION` = `DECISION_FALLBACK` = `G1_FAIL`（HOLD_NOT_VALIDATED 类）> `CORRECTNESS_FAIL` > `STABILITY_FAIL` > `ALL_PASS`；**INVALID 类恒高于 HOLD 类**（即使并存，如 REP_ERROR + G1_FAIL → REP_ERROR 生效）。
- **gates status 值域**：`PASS | FAIL | NOT_APPLICABLE`（G-M0-3b 恒 NOT_APPLICABLE）。
- **顶层 verdict 值域**：`PASS | HOLD_NOT_VALIDATED | HOLD_UNSTABLE_MEASUREMENT | REJECT_CORRECTNESS_OR_ISOLATION | INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`。

> 注：G-M0-7 只负责**落盘 schema/必需键**完整性；parity 失败已由 `PARITY_MISMATCH` 前置处理（不再挂 G-M0-7）。

**确定性聚合伪代码（v12 定稿，规则名称化）**：
```
# 前置：schema_validator(doc) 先执行（顶层键集精确五键 + phase 合法 + 值域 + 必需字段类型），
#       非法 → 返回 SCHEMA_INVALID（INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE），不进入聚合
def aggregate_verdict(doc, any_fallback, any_rep_error, any_preflight_rejection):
    g = doc["gates"]                                   # formal gates，一律 .get(default)，不 KeyError
    if doc["meta"]["phase"] == "preflight":
        return doc["verdict"]                          # 前置失败已由落盘函数确定（PARITY_MISMATCH/ALL_BUDGET_REJECTED/PREFLIGHT_INFRA），不重算
    # ---- formal（优先级高→低）----
    if not doc["meta"].get("matrix_complete", False):
                                 return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # FORMAL_INCOMPLETE（优先于 REP_ERROR；绝不 PASS）
    if any_rep_error:            return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # REP_ERROR（matrix_complete=true 才命中）
    if g.get("G-M0-7","FAIL")=="FAIL": return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # G7_SCHEMA_FAIL
    if any_preflight_rejection:  return "HOLD_NOT_VALIDATED"                        # PARTIAL_REJECTION
    if any_fallback:             return "HOLD_NOT_VALIDATED"                        # DECISION_FALLBACK（G-M0-1 保持原值）
    if g.get("G-M0-1","FAIL")=="FAIL":   return "HOLD_NOT_VALIDATED"                # G1_FAIL
    if g.get("G-M0-2","FAIL")=="FAIL" or g.get("G-M0-5","FAIL")=="FAIL" or g.get("G-M0-3a","FAIL")=="FAIL":
                                 return "REJECT_CORRECTNESS_OR_ISOLATION"           # CORRECTNESS_FAIL
    if g.get("G-M0-6","FAIL")=="FAIL" or g.get("G-M0-4","FAIL")=="FAIL":
                                 return "HOLD_UNSTABLE_MEASUREMENT"                 # STABILITY_FAIL
    return "PASS"                                                                    # ALL_PASS
```
- **优先级原则**：`REP_ERROR`/`G7_SCHEMA_FAIL`（INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE 类）**恒高于** `PARTIAL_REJECTION`/`DECISION_FALLBACK`/`G1_FAIL`（HOLD_NOT_VALIDATED 类）——并存时 INVALID 类生效（如 REP_ERROR + G1_FAIL + fallback 并存 → REP_ERROR）。
- **H3 边界**：聚合只读 `doc["meta"]["phase"]` 与顶层 `doc["verdict"]`（preflight 分支），formal 只读 `doc["gates"]`（get/default）；**绝不把 meta 子对象与顶层 doc 混淆**；schema validator 在任何聚合逻辑之前执行。
- **PASS 的唯一充分条件（ALL_PASS）**：`phase=formal` 且 `G-M0-1..7（required，G-M0-3b 除外）全部 PASS` 且 `parity_ok=true` 且 `any_fallback=false` 且 `any_rep_error=false` 且 `any_preflight_rejection=false`。
- **fallback rep 的归属**：带 `decision_fallback:true` 标记，**可继续参与内存归因 G-M0-4**（内存压力行为与决策来源无关），但**排除在真实决策质量声明之外**（不并入决策合法率、不参与 G-M0-1 统计）；**ERROR rep 不参与 G-M0-4 数值归因**（其数据不可信）。
- `meta/modes/gates/verdict` 关系：`meta` 记录全局配置与决策验证结论（`phase`/`preflight_status`/`preflight_reason`、`decision_validation`、`planned_units`/`executed_units`/`preflight_rejections[]`、`decision_validated`/`decision_fallback` 聚合、`parity_ok`/`parity_compensation`/`token_count_method`）；`modes.{off,on}.replicates[]` 为逐 rep 数据（含 rep 级 `status`/`decision_fallback`/`error_type`/`error_stage`/`error_bucket`）；`gates` 为门禁判定明细（每 gate 一个 `status`）；`verdict` 为上述聚合伪代码的单值结论。

### 5.2 指标字段

| 类别 | 字段 | 来源 |
|---|---|---|
| GPU/RSS 峰值 | `peak_gpu_mb` / `peak_rss_mb` | `sampler.find_server_gpu_mb/rss_mb`（行级 max，`metrics.py:104-105`）；GPU 口径 = nvidia-smi `memory.used`（total 8188、空闲 used≈40，增量 = used−40） |
| KV（attention） | `capacity_cells/used_cells/shared_cells/active_sequences/capacity_bytes/used_bytes` | `/metrics/kv`（KVProbe 快照 + `run_aggregate`） |
| token 分解 | `timings.prompt_n/cache_n/predicted_n`（`prompt_n+cache_n==prompt_tokens`） | OAI timings（`server-context.cpp:568-576`） |
| 延迟 | `latency_ms`；**TTFT 代理 = `timings.prompt_ms`**（= `t_prompt_processing`，`server-context.cpp:342/571`；llama-server 无独立 TTFT 字段） | driver 行 / timings |
| 输出 | `tokens`（数组）+ `content_sha256` + `finish_reason` | driver/runner 提取（e15 模式） |
| 分支质量 | `task_success`、canary 泄漏标志、决策点合法率、回收断言 | workload gate |
| rep 级状态（v16） | `status`（`OK\|INVALID_DECISION\|ERROR`，状态机 §5.1 + 逻辑约束：`INVALID_DECISION ⇔ decision_fallback=true`；`OK` 必须 `decision_fallback=false`；`ERROR` 必须 `decision_fallback=false` 且 `error_type` 为固定 error code（可选 `error_stage`/`error_bucket`；**不保存原始异常/路径/URL/prompt**））、`decision_fallback`（bool）、`error_type`/`error_stage`/`error_bucket` | runner 写入；**G-M0-7 对 formal 每 rep 校验这些必需键与逻辑约束** |

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
| `benchmark/framework/fanout_prompts.py` | 新增 | 纯函数：决策点 prompt（枚举约束）、分支扩展、canary 注入/检测、`ACTION: branch(bX)` 解析、**token 精确计数客户端（/apply-template + /tokenize，§4.2；端点不可用 → 报 preflight 失败，不降级继续）**、预算函数 `budget(P,B,N)` 与 fail-fast、**preflight 结果落盘助手（五键 schema，§5.1）** |
| `benchmark/runner/m0_fanout_runner.py` | 新增 | CLI + 矩阵编排 + server 生命周期（复用 e15 骨架）+ barrier 并发 + gate（G-M0-1..7）+ 唯一顶层 schema 落盘（§5.1） |
| `benchmark/framework/driver.py` | 修改 | **chat() keyword-only 扩展（temperature/seed/max_tokens/extra_body，§3.3）**，默认 None 保持旧行为 |
| `benchmark/tests/test_fanout_prompts.py` / `test_m0_fanout_runner.py` | 新增 | 纯函数（决策解析确定性/canary/**预算函数合法/非法组合表单测**/tokenize 客户端 mock/fail-fast/gate 判定/CLI）+ mock server e2e |
| `benchmark/tests/test_driver.py` | 修改 | 新增 chat 扩展用例（默认 None 旧行为、透传生效、keyword-only 兼容、extra_body 合并） |
| 文档 | 更新 | 本文件补实测结果 + AGENTS.md 状态更新 |

**不改**：`config.py`、`runner.py`、`workload/`。

CLI 合同（v12 收紧）：`--server-bin --model [--port-base N] --ctx-size 4096 --fanout {2,4,8} --prefix-len {short,medium,long} --branch-len {short,medium,long} --ctk q8_0 --ctv q8_0 --decision-n-predict 16 --branch-n-predict 64 --tool-rounds M --warmup 2 --reps 5 --out <path> [--tmp-dir <dir>]`；`--parallel` 由 runner 按 `fanout+2` 计算（不接受手工覆盖）。**端口合同（v12，任务 7）**：`--port-base` **可选**；未给时由 OS/端口探测分配（socket bind 探测，同 §3.7）；给了则 runner 每次启动从 base 起向上寻找可用端口；**校准 server、G-M0-1 验证 ×2、formal server 各自独立端口/进程**，每次 `finally` 停止——删除含糊的必传 `--port`。**临时目录合同（落盘/顶层键习惯对齐 E15；gates 子结构为 M0 自有）**：`--tmp-dir` 为**可选用户参数**（缺省时 runner 用 `tempfile.mkdtemp()` 创建、退出时 `shutil.rmtree` 清理——**此"创建并主动清理"是 M0 相对 E15 的增强**，E15 runner 接受 `--tmp-dir` 但清理语义未承诺）；`--slot-save-path` **不作为独立用户参数**，由 runner 内部绑定为同一临时目录（`--slot-save-path <tmp-dir>`）传给 server——保证 `POST /slots/:id?action=erase` 可用（slot erase 依赖该路径，`e15_branch_concurrent.py:548-560` 先例）且生命周期随 runner 清理。**"对齐 E15"仅指**：结果落盘位置（`results/`）与顶层键集 `{meta, modes, gates, verdict, notes}`（`e15_branch_concurrent.py:795-831`）；`gates` 子结构（§5.1 三值 status 与 G-M0-1..7 定义）为 M0 自有，不沿用 E15 的 gate 字段。**预算逐 unit（v12）**：`budget(P,B,N) > 3481` 的 unit → 记入 `meta.preflight_rejections[]` 并排除；**其余合法 unit 继续执行**（formal 只运行剩余 unit，`executed_units = planned_units − |rejections|`）；**存在任何 rejection → 顶层 HOLD_NOT_VALIDATED（PARTIAL_REJECTION）**；**全部被拒 → 落盘 phase=preflight HOLD_NOT_VALIDATED（ALL_BUDGET_REJECTED）**——不再有"CLI 直接退出/不落盘"行为。

**测试计划（v16：以 §5.1 测试要求 ①–㉖ 为唯一权威来源，本段仅摘要，避免双轨漂移）**：纯函数 pytest（决策点解析/确定性/canary/预算函数与逐 unit 排除/rep 状态机/transient retry 分层/decision_validation 落盘/schema validator 与 phase 约束/aggregate_verdict 优先级/Driver retry 与递归透传/端口生命周期/parity_progress/formal server 失败落盘）+ mock OpenAI server e2e（现有 `tests/mock_server.py` 模式）+ 可选 TinyLlama CPU smoke（仅冒烟，不宣称 4B 结论）；**完整用例编号与断言见 §5.1 ①–㉖，本段不再重复列项**。

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
| G-M0-1 决策确定性（v8：专用验证协议，与矩阵分离） | **专用稳定性验证，与正式矩阵数据完全分离**：**2 次独立 server 会话，每次 ≥10 请求（总数 ≥20）**，temp=0/seed=42 下 4B 决策点合法 `ACTION: branch(bX)` 输出率 **100%**；**INVALID_DECISION 谓词（三处同一）**：`(finish_reason=="length") OR (输出不含合法 ACTION)` → 任一即该 rep `status=INVALID_DECISION`（rep 级，非顶层 verdict；length 截断即使含 ACTION 也不可信）；**合法率 <100% → G-M0-1 = FAIL → 顶层 `HOLD_NOT_VALIDATED`**；**正式矩阵单 rep 决策失败仅记录 `status=INVALID_DECISION` 并走固定路由 fallback 继续内存压力实验，不计入 G-M0-1，不宣称真实决策 PASS**；fallback 触发 → `decision_fallback: true` + verdict 标记（§5.1） |
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
2. **4B 决策点提示工程**：需 0.8B 离线校准（CPU，可跑）→ 4B 验证；若枚举约束下 4B 仍不稳定，fallback = 固定决策映射且判定 **`HOLD_NOT_VALIDATED`**（规则 `DECISION_FALLBACK`/`G1_FAIL`）。
3. **temp=0 下分支"选择/回收"轮语义**：模型回收/选择轮的稳定性需实测（**决策/分支协议统一为 `ACTION: branch(bX)`**；回收走 `erase` 固定路径）；失败则降级为固定回收（`erase`），不阻塞内存归因目标。
4. **parallel 10 + long 桶的 decode 吞吐**：单线程调度下 latency 可能高，M0 只报告、不优化（且 long×大 fanout 已被预算公式排除）。
5. **build-cuda 二进制版本**：探针用 8569/afbf375c6（核心代码与 HEAD 等价，因 4a699aaad 仅新增测试文件）；正式 M0 实验前重建到 4a699aaad（version 8570）保证版本号一致。

## 10. 风险

- Laptop GPU 抖动（E3 门禁先例）→ warmup + 中位数 + 同硬件 off/on paired。
- 0.8B 指令遵循不稳定 → 决策点枚举约束 + 离线校准（§9.2 fallback → `HOLD_NOT_VALIDATED`）。
- 模型可用性：4B GGUF 已就位（2.7 GB，`models/qwen3-5-4B-Q4_K_M.gguf`）；0.8B 需 `download_models.sh 0.8b`（M0 实现时按需拉取）。

## 11. 实施清单（下一步，不在本阶段执行）

1. 新增 `benchmark/framework/fanout_prompts.py` + 纯函数测试（决策点解析/确定性/canary/预算公式 fail-fast）；
2. 新增 `benchmark/runner/m0_fanout_runner.py`（复用 e15 生命周期/gate 骨架）+ mock e2e 测试；
3. `cmake --build build-cuda` 重建 4B 实验二进制到 HEAD（4a699aaad）；
4. 0.8B 决策点校准 → 4B 决策确定性验证（G-M0-1，<100% → `HOLD_NOT_VALIDATED`）；
5. 跑合法矩阵（§4.2，6 组合 × 2 ctk × 2 对照 = 24 单元）→ 落盘 → 归因报告（§8 输出）→ 对照 G-M0-2..7；
6. 结果归档 `benchmark/baseline/` + 报告文档 + AGENTS.md 状态更新。
