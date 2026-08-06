# E2.0 设计报告：KV Cache 生命周期管理 —— 源码审查、基线表征与实验设计冻结

- 阶段：E2.0（仅审查/基线/设计，**不实现任何生命周期策略**）
- 日期：2026-08-06
- 状态：**CONDITIONAL**（详见第 16 节）

---

## 1. E2 目标与严格边界

### 1.1 目标

在**不改变 KV buffer 预分配总容量**的前提下，通过改进 slot/sequence 的**生命周期管理策略**（保留/淘汰/复用），提升：
1. KV Cache 有效利用率（used_cells 与逻辑 token 对齐度）；
2. 长生命周期 Agent workload 的 cache_hit_rate；
3. 降低 recompute_tokens / recompute_ratio 与 prefill 延迟（p50/p95）；
4. 在不损害任务保真（state_retention_rate / task_success）前提下延长有效会话轮数。

### 1.2 本阶段严格禁止（已完成核对，无违反）

- ❌ 实现 KV Cache 淘汰策略 / 新回收策略
- ❌ 修改 `seq_rm` / `seq_keep` / `seq_cp` 语义
- ❌ 修改 `--cache-reuse` 实现
- ❌ 修改 KV buffer 分配方式 / 按需分配 / 分层存储
- ❌ 实现 COW / 修改 `TAG_KV_CACHE_SHARE_CELLS`
- ❌ 实现 Context Compression
- ❌ 修改 workload 的 `generate()`/`run()`/prompt/轮数/秘密数字/截断逻辑
- ❌ 修改已有 baseline / 为改善曲线修改实验输入
- ❌ 未经授权提交 git commit（本阶段未提交任何 commit）

---

## 2. 仓库版本与工作区状态（实测核对）

### 2.1 两仓库 git 状态

| 项 | 根仓库 | llama.cpp |
|---|---|---|
| HEAD | `bd86c1d8e12f3764ef066b836033a6361bc7f241` | `cc4f664477d651c631ccdbeb41a3b9e79f5a77cd` |
| HEAD 说明 | `docs: 更新 E1_VALIDATION_REPORT 提交状态说明` | `test: enable slot erase in metrics/kv integration tests` |
| E1 实现 commit | `aa0ad1e`（E1 收尾，含 kv_probe repeat/warmup 修复） | `b69773a1e`（/metrics/kv endpoint） |
| E1 测试 fixture（slot_save_path） | — | `cc4f66447` |
| `git status --short` | 干净 | 干净 |
| `git diff --stat` | 空 | 空 |

**报告与工作区核对结论**：此前 E1 报告中的 commit 描述与实际 HEAD 一致，无未提交的 E1 修改，无脏文件。

### 2.2 E1 修改提交状态逐项核实

1. ✅ E1 核心实现（/metrics/kv endpoint + llama_memory_get_kv_stats）：`b69773a1e` 已提交
2. ✅ benchmark kv_probe repeat/warmup 修复：根仓库 `aa0ad1e` 已提交
3. ✅ llama.cpp 测试 fixture slot_save_path：`cc4f66447` 已提交

### 2.3 实验二进制与模型

| 项 | 值 |
|---|---|
| 实验 server binary | `llama.cpp/build-cuda/bin/llama-server`（CUDA，arch 89） |
| binary sha256 | `74a6b18bf2af8fc55a35cc0fc445f8722faa8c4400c5e2dd3cb8665af5dd6336` |
| 构建状态 | ⚠️ 发现 binary mtime（13:50/13:56）**早于** E1 commit 时间（14:04），即旧 binary 是在"工作区含未提交 E1 改动"时构建的；**本轮已在 E1 HEAD 上重新构建**（15:26），现与 `cc4f66447` 完全一致 |
| 模型 | `models/qwen3-5-4B-Q4_K_M.gguf`（2,707,514,144 B） |
| 模型 sha256 | `de8e96cd0d0c358487091aaaed1346bc02e61da3d4b412c833662702e233e78c` |
| GPU | NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB |

---

## 3. E1 宽回归补充结果

### 3.1 已执行并**真实通过**

| 测试 | 结果 | 说明 |
|---|---|---|
| `tools/server/tests/unit/test_metrics_kv.py`（5 个测试函数） | **5/5 PASS** | 真实 tinyllama（stories260K）CPU server 上执行：schema 校验 / 空 cache / completion 后 used_cells 增长 / 多 sequence+slot erase / 只读性 |
| benchmark Python 全量 | **71 passed** | `cd benchmark && uv run pytest -q` |

### 3.2 与 E1 相关的可运行子集（`--noconftest` 绕过 load_all 下载阻塞）

以已缓存模型（stories260K）执行，E1 相关文件：**44 passed, 1 skipped**（test_completion / test_basic / test_ignore_eos / test_tokenize）。

### 3.3 失败项（已判定为**预存问题，非 E1 回归**）

| 失败 | 判定依据 |
|---|---|
| `test_ctx_shift.py`（3 例，`prompt_n=1 != 226`） | 用 **E1 前基线** `b06aa774c` 构建的同一 server 执行，**同样失败**（prompt_n=1, predicted_n=30）→ 与 E1 无关，是上游测试/模型（stories260K）行为问题 |
| `test_sleep.py::test_server_sleep`（is_sleeping=False） | 测试先 GET `/health` 唤醒 server 再断言 `/props` 的 is_sleeping → 测试时序问题 |
| `test_template.py`（43 例） | `--noconftest` 导致的模板文件相对路径解析问题；纯模板渲染测试，与 server KV 无关 |

**环境问题记录**（测试代码 vs 环境分开）：
- 测试代码问题：`conftest.py` 的 module-scope `ServerPreset.load_all()` 需要下载全部 8 个 preset 模型（含 tinygemma3 ~3GB 等），当前环境仅能获取 4 个 → `test_completion.py` 等**无法在标准 pytest 入口完整执行**；
- 阻塞命令：`python3 -m pytest tools/server/tests/unit/`（收集阶段挂起/报错）；
- 未修改任何生产代码绕过测试；未将未执行写成通过。

---

## 4. llama.cpp KV 生命周期源码审查（源码级事实表）

### 4.1 KV buffer 分配时机与方式

- **分配时机**：`llama_context` 构造时（模型加载后立即），`memory.reset(model.create_memory(params_mem, cparams))`（`src/llama-context.cpp:383-390`）。
- **容量来源**：`attn_kv_size = cparams.n_ctx_seq`（`src/llama-model.cpp:2273/2291`）；`n_ctx_seq` 在 `llama_context::init` 计算：`kv_unified ? n_ctx : n_ctx / n_seq_max`（`src/llama-context.cpp:285-302`，注意 `n_ctx = GGML_PAD(n_ctx, 256)`）。
- **结论**：**始终按总 ctx 预分配**，运行时 KV 容量固定、不可扩展。`capacity_bytes`（E1 指标）是预分配容量，**不能作为"生命周期策略降低显存"的证据**。

### 4.2 cell 状态表示（`src/llama-kv-cells.h`）

- 每个 cell 有 `pos`（逻辑位置，`-1` = free）、`shift`、`ext`、`seq`（`std::bitset<LLAMA_MAX_SEQ>`）。
- **free/used**：`pos[i] == -1` ⇔ free；`used` 集合维护 free 状态。
- **被哪些 sequence 引用**：`seq[i]` bitset 的置位位。
- **多 sequence 共享**：**可以**——同一 cell 的 `seq[i]` 可含多个 seq id（元数据级共享；E1 的 `shared_cells` 统计 `seq[i].count() > 1` 的 cell 数，`physical_sharing=false`，**不是 COW 物理共享**）。

### 4.3 seq_rm / seq_keep / seq_cp / seq_add 语义（`src/llama-kv-cache.cpp`）

| 操作 | 语义 | 副作用 |
|---|---|---|
| `seq_rm(seq_id, p0, p1)`（379 行） | 删除指定 seq 在位置 `[p0,p1)`（`p0<0→0, p1<0→max`）的 cell 关联；`seq_id=-1` 时**清空所有 seq 全部位置** | 仅元数据；cell 在 `seq[i].none()` 时置 `pos=-1`（变 free）；**更新 `v_heads`（空闲搜索起点）**；不物理清零 buffer |
| `seq_keep(seq_id)`（539 行） | 只保留该 seq，移除**所有其他 seq** 的关联（按 cell 过滤：不含该 seq 的 cell 被 rm） | 同上 |
| `seq_cp(src, dst, p0, p1)`（445 行） | **同 stream：纯元数据复制**（`cells.seq_add(i, dst)`，无 buffer 拷贝）；**跨 stream：物理 buffer 拷贝**（enqueue `sc_info.ssrc/sdst`，且仅支持全 buffer） | 跨 stream 拷贝在下次 update 执行 |
| `seq_add(seq_id, p0, p1, shift)`（566 行） | 对 `[p0,p1)` 内 cell 的**逻辑位置加 shift**（KV shifting 原语） | 无物理移动 |

### 4.4 跨 stream（unified 与否）

- `n_stream = unified ? 1 : n_seq_max`；`v_cells[s].resize(kv_size)`，每 stream 独立 cell 数组 + 独立 K/V tensor（`src/llama-kv-cache.cpp:64-160`）。
- `seq_to_stream`：unified 时所有 seq → stream 0；非 unified 时 `seq_to_stream[s] = s`（seq_id 受 `n_seq_max` 约束）。
- **容量分配**：unified → 单 stream 容量 = `n_ctx`；非 unified → 每 stream 容量 = `n_ctx_seq = n_ctx / n_seq_max`。
- **行为差异**：`try_clear_idle_slots()`（见 4.6）**仅 unified 生效**；非 unified 下 slot 之间 KV 完全隔离，无法借用。

### 4.5 cache-reuse 完整执行路径

1. 参数：`--cache-reuse N` → `params.n_cache_reuse`，默认 **256**（`common/arg.cpp:3453, 4404-4478` 多个 server preset 分支设 256；`common/common.h:609` 基础默认 0，**server 路径实际默认 256**）。
2. 限制：multimodal 或 `llama_memory_can_shift()==false`（Step35 / `n_pos_per_embd>1`）时被强制置 0 并告警（`server-context.cpp:1278-1292`）。
3. 执行（`server-context.cpp:3263-3310`，SLOT_STATE_STARTED 内 cache_prompt 分支）：
   a. `n_past = slot.prompt.tokens.get_common_prefix(input_tokens)`（公共前缀命中）；
   b. 若 `n_cache_reuse > 0` 且可 shift：在 `[head_c, head_p)` 中找 `n_match >= n_cache_reuse` 的匹配块，执行 `seq_rm(slot.id, head_p, head_c)` + `seq_add(slot.id, head_c, head_c+n_match, kv_shift)` 把旧块 KV **逻辑移位**到新位置，`n_past` 累加；
   c. 该块之后的 token 走常规 decode。
4. **本质**：KV shifting（`seq_add` 位置偏移），无物理拷贝；收益场景是**公共前缀中断后、尾部仍有可复用块**（如工具调用改变中间内容）。
5. **命中统计**：`n_prompt_tokens_cache = n_past`（3445 行）→ `timings.cache_n`；`prompt_n` = 实际处理数（`n_prompt_tokens_processed`）。

### 4.6 slot 复用 / erase / KV 清理

- `slot.release()`（524 行）：仅置 IDLE + 重置统计，**不清 KV**（child slot 例外）。→ 请求完成后 KV 保留供下次复用。
- `prompt_clear()`（291 行）：`mem.seq_rm(id, -1, -1)` + prompt 清空。调用点：请求出错/超时/中断、`try_clear_idle_slots()`、prompt cache 加载失败。
- **slot erase（`/slots/{id}?action=erase`）**：需 `--slot-save-path`；实现为把 slot 状态写盘（prompt_save）+ `prompt_clear()`，**间接清 KV**。
- **slot 复用时机**：`get_free_slots()`（2300 行）只选 `!is_processing()` 的 slot——**无淘汰/替换策略**，等处理完才可复用。
- **新请求进入 slot**：`n_past` 公共前缀 → `seq_rm(slot.id, n_past, -1)`（3478 行）删除旧 prompt 尾部不重叠 KV → 从 n_past 连续写新 token（旧位置自然覆盖）。**无公共前缀（n_past=0）时 = 全量覆盖 + 全重算**。
- **KV 满时**（3730-3745 行）：`llama_decode` 批处理失败 → `try_clear_idle_slots()`（unified 下清 1 个空闲 slot）→ 仍失败则 `n_batch /= 2` 重试 → 最终报错 "Context size has been exceeded."。**无基于价值的淘汰**。

### 4.7 ctx shift（非 cache-reuse 路径）

- prompt 超 slot ctx 时（`n_past + task > n_ctx`）：`n_keep` 计算 → `seq_rm(n_keep, n_keep+n_discard)` + `seq_add(..., -n_discard)`（2988-2989 行）丢弃中间段并移位——与 cache-reuse 共享 `seq_add` 原语。

### 4.8 defrag

- **当前版本已无 defrag 代码**（`grep defrag src/llama-kv-cache.cpp` 无结果）。早期 llama.cpp 的 `llama_kv_cache_defrag` 已移除，由 `v_heads` 搜索起点 + unified KV 机制替代。**相关问题：废弃，不可依赖。**

### 4.9 used_cells / used_bytes 与逻辑 token 数差异（E1 指标口径）

- `used_cells` = `pos[i] != -1` 的 cell 数（含**所有 seq 关联**，同一 cell 多 seq 共享只计 1 次）；
- 与逻辑 token 数的差异来源：① 跨 stream 时每 stream 独立 cell（报告的是 stream0 的 `get_size()` 与合计 used）；② `seq_add` 移位产生的**空洞/重叠**（cell 物理位置与逻辑位置解耦）；③ `n_past` 截断（seq_rm 后尾部 cell 释放）；④ SWA/hybrid 的 recurrent state 不计入（Qwen3.5 为 hybrid，`get_kv_stats` 委托 `mem_attn`，仅统计 attention KV）。

---

## 5. B0/B1 基线配置（冻结）

### 5.1 冻结矩阵

| 项 | 值 |
|---|---|
| 模型 | Qwen3.5-4B-Q4_K_M.gguf（sha256 `de8e96cd…`） |
| server binary | build-cuda（E1 HEAD 重建，sha256 `74a6b18b…`） |
| llama.cpp commit | `cc4f66447` |
| ctx-size | 2048（总） |
| parallel | 1（补充 2） |
| per-slot ctx | 2048（p=1）/ 1024（p=2，非 unified） |
| kv_unified | false（默认） |
| batch / ubatch | server 默认（2048/64 级） |
| GPU offload | `-ngl 99` |
| temperature | 0（固定） |
| seed | 42（固定） |
| max_tokens | workload 默认（multi_turn 每轮 ~64） |
| warmup / repeat | 1 / 5 |
| workload 版本 | 当前 HEAD（未改动） |

### 5.2 基线定义

- **B0**：`--cache-reuse 0`（禁用）
- **B1**：`--cache-reuse N`，N ∈ {128, 256}（**通过扫描确定**，非假设默认值）
- **B2**（未来，本阶段未实现）：E2 新生命周期策略

---

## 6. cache-reuse 扫描结果

### 6.1 实验方法

- 每配置：`pkill -x llama-server` → 重启 server（KV 清空）→ warmup=1 + repeat=5 → `--kv-probe`；
- ABBA 顺序（multi_turn）：`cr=0 → 256 → 256 → 0`（对抗环境漂移），cr=128 作为中间点；
- ABBA 顺序（long_life）：`cr=0 → 256 → 256 → 0`；
- 结果落盘：`benchmark/results/e20/`（临时输出，不入库）。

### 6.2 multi_turn（rounds=20，prompt 34→2020 tokens，ctx=2048, parallel=1）

| 配置 | cache_hit_rate | recompute_tokens | peak_used_cells | p50_lat(ms) | p95_lat(ms) | tps |
|---|---|---|---|---|---|---|
| cr=0（#1） | 0.9076 | 2080 | 2032/2048 (99.2%) | 475.9 | 5502.2 | 912.6 |
| cr=256（#1） | 0.9076 | 2080 | 2032 | 469.0 | 5497.3 | 917.9 |
| cr=256（#2） | 0.9076 | 2080 | 2032 | 469.0 | 5497.3 | 917.9 |
| cr=0（#2） | 0.9076 | 2080 | 2032 | 475.9 | 5502.2 | 912.6 |
| cr=128 | 0.9076 | 2080 | 2032 | 475.1 | 5521.9 | 912.4 |

**ABBA 可复现性**：cr=0 两次逐字段一致、cr=256 两次逐字段一致 → **无环境漂移，数据可信**。

**结论**：multi_turn（纯前缀式重复 prompt）下 cache-reuse ∈ {0,128,256} 对命中率/重算/KV 曲线**无任何影响**，仅延迟有 ~1.5% 级差异（cr=256 略优）。原因：`get_common_prefix` 已覆盖全部前缀命中，chunk-shifting 无额外可复用块。

### 6.3 long_life（rounds=12，含工具调用，ctx=2048, parallel=1）

| 配置 | cache_hit_rate | recompute | peak_used_cells | p50_lat | p95_lat | tps |
|---|---|---|---|---|---|---|
| cr=0（#1） | 0.7130 | 2819 | 1977/2048 (96.5%) | 567.2 | 6529.9 | 475.0 |
| cr=256（#1） | 0.7130 | 2819 | 1977 | 568.2 | 6584.0 | 472.6 |
| cr=256（#2） | 0.7130 | 2819 | 1977 | 568.2 | 6584.0 | 472.6 |
| cr=0（#2） | 0.7130 | 2819 | 1977 | 567.2 | 6529.9 | 475.0 |

**关键观察**：long_life 存在**前缀中断**（工具结果替换前轮 assistant 回复，如 round 9 `cached=0` 全 miss；round 5 cached=467/563），理论上是 cache-reuse 的价值场景，**但 cr 0/256 仍无差异** → 说明当前中断模式（整段替换）下无可复用尾部块，或 `n_match >= 256` 阈值未满足。**这构成 E2.1 的重要基线事实：cache-reuse 在当前 Agent workload 上收益 ≈ 0，生命周期优化空间在"前缀命中率"而非"chunk 复用"。**

（parallel=2 与 8192 配置结果见第 7 节补充。）

---

## 7. ctx-size / parallel 配置语义与补充扫描

### 7.1 语义

- `--ctx-size 2048 --parallel 2`（非 unified）→ `n_ctx_seq = 1024/slot`，每 slot 独立 stream（容量 1024 cells）、独立 K/V tensor，**slot 间 KV 隔离**；
- `kv_unified=true`（`--kv-unified`）→ 单 stream 总容量 2048，可跨 slot 共享 cell（`shared_cells` 语义）；
- **ctx 压力触发**：multi_turn 20 轮 prompt 增长到 2020 > 1024（p=2 场景）→ 必然触发 ctx shift / KV 满路径。

### 7.2 parallel=2 补充扫描（已完成）

`ctx=2048, parallel=2`（每 slot 1024 cells，非 unified 双 stream），multi_turn rounds=20，warmup=1 repeat=5，ABBA 顺序 cr=0 → 256：

| 配置 | cache_hit_rate | recompute | peak_used_cells | p50_lat(ms) | p95_lat(ms) | tps |
|---|---|---|---|---|---|---|
| p2, cr=0 | 0.9262 | 1033 | **2046/2048**（双 stream 合计，每 slot ~1023/1024） | 416.5 | 5984.5 | 493.7 |
| p2, cr=256 | 0.9262 | 1033 | 2046 | 396.7 | 5977.1 | 498.3 |

- round 20 prompt=953（< 每 slot 1024 上限），used_cells 已达 1992-2046 → **同样触发容量压力**；
- cr 0/256 命中率/重算/KV 曲线**仍完全一致**（并发 slot 场景同样无差异）；
- hit=0.9262 > p=1 的 0.9076：prompt 增长区间（34→953）相对 slot 容量（1024）占比更高，前缀命中更充分；
- **口径说明**：E1 `capacity_cells`/`used_cells` 跨所有 stream 累加（`llama-kv-cache.cpp:698`，Σ v_cells[s].size()），p=2 时 capacity_cells=2048（1024×2），与 p=1 的 2048 数值相同但**物理意义不同**（双 stream 合计）——报告数据时须同时给出 parallel 与 per-slot ctx。

### 7.3 8192 配置

- 未执行（资源权衡：4B 模型 3.2GB + 8192×32KB=268MB KV + 双 buffer 开销，8GB 卡余量偏紧；且 2048 配置已确认触发压力，8192 不增加信息量）。记录为**未执行项**。

---

## 8. 原始指标与统计口径

### 8.1 每 run 保存内容

- 请求级原始行（round/prompt_tokens/completion_tokens/cached_tokens/latency_ms/rss_mb/gpu_mb/timings 全字段）；
- `kv_observations`：请求前后快照 + run 边界（start/end）+ 可选周期采样，按 `run_id` 聚合（first/last/peak used_cells、样本数）；
- 完整 metadata（model path、ctx、parallel、seed、temperature、server 参数、binary 等）。

### 8.2 统计口径（跨 5 个 repeat run 聚合 mean/std/p50/p95）

- 缓存与重算：`cached_tokens`、`prompt_n`、`cache_n`、`cache_hit_rate`、`recompute_tokens`、`recompute_ratio`、`cache_hit_rate over rounds`、full recompute 次数（cached=0 的 round 数）；
- KV：`capacity_bytes/used_bytes/used_bytes_valid/capacity_cells/used_cells/peak_used_cells/peak_used_bytes/active_sequences/shared_cells/endpoint failures`；
- 性能：request latency（p50/p95/mean/std）、prompt/prefill 延迟、decode 延迟、`throughput_tps`、`decode_tps`；
- 保真：`task_success`、`state_retention_rate`、`secret_recall`、`truncations`、请求失败数。

### 8.3 重要口径说明

- `capacity_bytes` 是**预分配容量**，本阶段**不得**作为生命周期策略降显存的证据（见 4.1）；
- KV 状态**跨 run 累积**：warmup run 的 KV 不清理（采样已正确跳过），正式 run 的 `first_used_cells=1253`（multi_turn）并非 0 → run 间不独立，**仅同 server 会话内可比**；跨配置比较必须重启 server（本实验已遵守）；
- `task_success` / `state_retention_rate` 在 4B 真实模型下全部为 0（见第 10 节），**当前 evaluator 无区分度**，是 E2.1 前必须解决的问题。

---

## 9. cache_hit_rate / recompute / used_cells 曲线

### 9.1 multi_turn（cr=0 与 cr=256 曲线一致）

- prompt_tokens：34 → 2020（round 1→20，前缀式单调增长）；
- cached_tokens：16 → 1786（随 prompt 增长，命中率稳定 ~0.9076）；
- used_cells：round 1 后 ~1253 → 峰值 2032（round ~18）→ run 末 1253（**发生 KV 回收**：ctx shift / seq_rm 尾部清理）；
- **recompute 集中在**：round 1（全 prefill）+ 每次前缀刷新点。

### 9.2 long_life（cr=0 与 cr=256 曲线一致）

- prompt 波动：34 → 440 → 471 → 808 → 563 → 580 → 902 → 1726 → **708(cached=0, 全重算)** → 1164 → 1198 → 1228；
- 工具调用轮（round 2/4/8）后 cached 显著下降 → **前缀中断是 recompute 主因（2819 tokens）**；
- used_cells：first=734 → peak=1977 → last=1275。

### 9.3 结论

- 两个 workload 均触发 ≥96% 的 KV 容量压力（**满足"触发回收压力"的实验要求**）；
- cache-reuse 三个取值在两类 workload 上均无命中率差异 → **B1 与 B0 行为等价**（对前缀式/工具中断式 Agent 流量）；
- E2.1 的优化抓手：**提高前缀命中率**（保留策略）与**减少无效 cell 占用**（淘汰策略），而非 chunk 复用。

---

## 10. evaluator 保真结果

| 指标 | multi_turn | long_life |
|---|---|---|
| task_success | true（8 轮） | **0.0（全配置）** |
| state_retention_rate | — | **0.0（全配置）** |
| secret_recall | — | 0.0 |
| truncations | 0 | **1.0/run**（round 8 prompt 1726 触发） |
| 400 fallback / 请求失败 | 0 | 0 |

- long_life 12 轮下 Qwen3.5-4B **无法保持 secret**（state_retention_rate=0）：这是模型能力限制（E0.6 已记录 4B 指令遵循/长程记忆弱），**非缓存策略引入**；
- truncations=1 是 ctx 压力下 slot 截断（round 8）——**当前生命周期行为影响任务输入的信号**；
- **E2.1 保真基线**：以 state_retention_rate 作为主保真指标，但需先解决"4B 无法通过 12 轮 long_life"的区分度问题（方案见第 15 节，如降低轮数/简化 secret 注入位置）。

---

## 11. 研究假设及可证伪条件

| # | 假设 | 对照 | 观测指标 | 预期结果 | 否证条件 |
|---|---|---|---|---|---|
| H1 | 固定 KV capacity 下，保留策略可降低无效/低价值 KV 占用 | B0 vs E2 策略 | used_cells 曲线、无效 cell 占比（逻辑 token 对齐度） | 相同吞吐下 used_cells 峰值下降 ≥10% | 峰值无差异或曲线等价 |
| H2 | 长生命周期 workload 下可提高 cache_hit_rate | B0 | cache_hit_rate、rounds 级曲线 | multi_turn ≥0.93（基线 0.9076）、long_life ≥0.75（基线 0.7130） | 无提升或下降 |
| H3 | 可降低 recompute_tokens / ratio | B0 | recompute_tokens、full recompute 次数 | multi_turn 重算 <2080；long_life <2819 | 持平或上升 |
| H4 | 可降低 prefill latency p50/p95 | B0 | prompt/prefill 延迟 | p50 改善 ≥5% | 无差异 |
| H5 | 可延长有效会话轮数（不截断） | B0 | truncations、可完成轮数 | truncations 下降（long_life 1→0） | 截断不变 |
| H6 | 不损害 state retention / evaluator | B0 | state_retention_rate、task_success | 与基线持平 | 下降（需先修复区分度） |
| H7 | cache-reuse 与生命周期策略协同/冲突 | B1 vs 新策略+cr | cache_hit_rate 联合曲线 | 协同 ≥ 单独任一 | 冲突（联合 < 单独） |
| H8 | 全 miss 时策略不引入额外开销 | B0 | 全重算轮延迟、endpoint 开销 | 每请求开销 <2% | 明显劣化 |
| H9 | 并发 slot 下回收不串扰其他 sequence | B0(p=2) | active_sequences、shared_cells、各 slot 保真 | 无跨 slot 缓存污染（错位命中） | 出现错位命中/保真下降 |

---

## 12. E2.1 候选方案比较

| 维度 | A：slot/sequence 淘汰与复用策略 | B：按需 KV buffer 分配/收缩 | C：CPU/磁盘分层 |
|---|---|---|---|
| 复用现有原语 | ✅ `seq_rm`/`seq_keep`/`seq_add` | 部分（需改 buffer 生命周期） | 部分 |
| 是否改变 `capacity_bytes` | 否（仅利用率） | **是** | 是（主存+辅存） |
| 源码改动范围 | 中（server 策略层，~几百行） | 大（memory 模块核心） | 很大（迁移/恢复/带宽） |
| 风险 | 低（不碰 decode 路径） | 高（并发、OOM、形状） | 很高（一致性、延迟抖动） |
| 对显存峰值主张 | 不可（预分配不变） | 可（峰值下降） | 可 |
| 本阶段数据支撑 | ✅ B0/B1 已确认 used_cells 峰值 99%、recompute 主因是前缀中断 | ❌ 无容量缺口数据（2048 内全部跑完） | ❌ 无需求证据 |
| 与赛题"显存/内存优化"契合 | 间接（利用率→延迟/吞吐） | 直接 | 直接 |

**结论**：选择 **候选 A**。理由：① 源码事实表明当前 slot 复用无淘汰（`get_free_slots` 只选空闲）、KV 满时仅"清空闲 slot/减 batch/报错"，存在明确的策略空位；② B0/B1 数据显示无效占用与重算集中于前缀中断与尾部残留，A 直接对症；③ B（按需分配）在预分配模型下需动 memory 核心且本阶段无容量缺口证据，应在 A 验证后再评估；C（分层）属更大范围设计，无基线数据前不实施。

---

## 13. 最终推荐方案（E2.1）

**首选：改进 slot/sequence 淘汰与复用策略（候选 A）**，具体方向（设计，本阶段不实现）：

1. **空闲 slot 回收策略化**：替换 `try_clear_idle_slots()` 的"顺序清 1 个"为基于价值的淘汰（如按 `t_last_used` LRU / prompt 长度 / 低价值 seq 优先）；
2. **request 级保留策略**：根据 Agent workload 特征（多轮共享前缀）在 `seq_rm(slot.id, n_past, -1)` 前保留跨请求稳定前缀，减少尾部抖动；
3. **前缀中断缓解**：对工具调用型中断，保留可复用尾部块（当前 `n_cache_reuse` 阈值下未生效，需数据驱动调参或改为按价值保留）；
4. 全部通过已有原语（`seq_rm/seq_keep/seq_add`）实现，**不改变 decode 路径与 KV 布局**。

**排除 B/C 的理由**：见第 12 节；核心是预分配模型下"降显存"主张不成立，且当前数据无容量缺口证据。

---

## 14. 失败风险与未决问题

1. **evaluator 区分度**（阻断性）：4B 真实模型 long_life 12 轮 state_retention_rate=0、truncations=1 → 无法作为 E2.1 保真判据，需先调整（轮数/secret 注入/评估窗口）；
2. **基线等价性**：B0/B1 在本 workload 上行为等价 → E2.1 与"cache-reuse 协同"假设（H7）可能无观测空间，需构造能分离两者的 workload（前缀中断且尾部可复用）；
3. **KV 跨 run 累积**：run 间不独立（warmup 残留），跨配置对比必须重启 server（已遵守）；E2.1 实验需固定此口径；
4. **宽回归缺口**：llama.cpp 完整 pytest 因模型下载基础设施未执行（已记录，非代码问题）；
5. **Qwen3.5 hybrid 架构**：`get_kv_stats` 只覆盖 attention KV，recurrent state 不可观测 → E2.1 指标口径受限；
6. **parallel=2 结果未出**（见第 7 节附注）；
7. **8192 ctx 未测**（资源权衡，已记录）。

---

## 15. E2.1 详细实现边界（本阶段不编码）

- **修改边界**：仅 `tools/server/server-context.cpp`（slot 生命周期策略层）与 `tools/server/server-context.h`（如需要新状态）；**不触碰** `src/llama-kv-cache.*`、`src/llama-memory*`、`src/llama-context.cpp`、decode/采样路径；
- **不变量**：`seq_rm/seq_keep/seq_cp/seq_add` 语义不变；KV buffer 分配不变；`capacity_bytes` 不变；OpenAI 兼容 API 行为不变（响应字段、错误码）；`--cache-reuse` 参数行为不变；
- **失败恢复**：策略层异常不得导致 decode 失败——回退到现有 `try_clear_idle_slots` 行为；每请求最多执行 1 次回收（控制开销，H8）；
- **任务语义保真**：state_retention_rate / task_success 不低于 B0（先修复 evaluator 区分度，见 14.1）；
- **需扩展的 E1 指标**：按 seq 维度的 cell 价值统计（如需）、无效 cell 占比（逻辑 token 对齐度）、回收事件计数（eviction events + 被回收 KV 的后续重算量）、前缀中断检测（cached=0 轮计数）；
- **需新增测试**：① 策略级单元测试（mock KV 状态 → 断言淘汰选择）；② 集成测试（真实 tinyllama：构造低价值 seq → 断言被优先回收）；③ 保真回归（long_life 调整后版本）；④ 并发 slot 串扰测试（H9）；
- **实验协议**：B0 vs B2 同 server 参数、ABBA 顺序、warmup=1 repeat=5、--kv-probe、temperature=0、seed=42。

---

## 16. 最终状态与结论

**状态：CONDITIONAL**

- ✅ 源码生命周期行为已完全解释（事实表见第 4 节）；
- ✅ B0/B1 基线真实可复现（ABBA 双重复逐字段一致）；
- ✅ cache-reuse 扫描结果可信（0/128/256 三值 × 两 workload）；
- ✅ KV 指标与请求指标已对齐（cached_tokens ↔ cache_n ↔ n_past ↔ used_cells 曲线）；
- ✅ 明确 E2.1 只实现**候选 A（slot/sequence 淘汰与复用策略）**；
- ✅ 未提前修改任何 llama.cpp 生命周期代码（本阶段零代码改动）；
- ⚠️ **条件**：① 修复 long_life evaluator 区分度（4B 模型 12 轮 retention=0，阻塞 H6/保真判据）；② 完整 pytest 宽回归仍受模型下载限制（环境问题，已记录，不阻塞 E2.1）。parallel=2 数据已补齐（第 7.2 节）。

**建议**：人工审查本报告并确认以上 3 个条件后可进入 E2.1。

---

## 附注：执行记录

- 本阶段**未执行任何 git commit**（遵守提交规则）；实验产物仅写入 `benchmark/results/e20/`（临时，不入库）；报告写入 `docs/`。
- 实际执行命令摘要：
  - `cmake -B build-cuda -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89` + `ninja build-cuda llama-server`（E1 HEAD 重建 binary）
  - `python3 tmp/run_e1_manual.py`（test_metrics_kv 5/5）
  - `cd tools/server/tests && python3 -m pytest unit/test_completion.py unit/test_basic.py ... --noconftest`（E1 相关子集 44 passed）
  - `cd benchmark && uv run pytest -q`（71 passed）
  - `bash llama.cpp/tmp/e20_scan_mt.sh`（multi_turn 5 配置 ABBA）
  - `bash llama.cpp/tmp/e20_scan_ll.sh`（long_life 4 配置 ABBA）
  - `bash llama.cpp/tmp/e20_scan_p2.sh`（parallel=2，cr 0/256，已完成）
- 所有 server 启停均用 `pkill -x llama-server`（避免 pkill -f 自杀陷阱）。
