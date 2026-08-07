# llama.cpp KV Cache 当前架构审计（KV Cache Current Architecture）

- 阶段：E6.0
- 审计日期：2026-08-07
- 代码基线：llama.cpp HEAD `69711a2d6`（E4.2 修复后）

## 1. KV cell 结构、分配、查找、移动、释放、清空

- **cell 结构**：`llama_kv_cells`（`src/llama-kv-cells.h:32-533`）。每 stream 一个 `llama_kv_cells`（`src/llama-kv-cache.h:280-282`）。cell 元数据 = `pos`（llama-kv-cells.h:464，-1=空）、`ext`（2D M-RoPE）、`shift`（位移累积）、`seq` bitset `std::bitset<LLAMA_MAX_SEQ>`（486-489，一 cell 可被多 seq 共享）、`seq_pos[s]` map。cell 不含数据指针——数据在连续 tensor，cell 只是位置/归属元数据。
- **查找**：`find_slot`（`src/llama-kv-cache.cpp:998-1195`）。从 `v_heads[stream]` 起环回搜索（1101-1107）；`can_use` = 空 cell 或单 seq cell 且 SWA mask 允许覆盖（1142-1161）；非连续模式逐 token 填洞（1118-1184），`cont` 模式要求连续。
- **分配**：`apply_ubatch`（1197-1273）：覆盖旧 cell 时 `cells.rm` 再 `pos_set`+`seq_add`（1220-1243）；维护 `[pos_min,pos_max]` 连续不变量，purge 被覆盖位置之前旧 token（1250-1265）。
- **移动**：`mv()` 被注释禁用（llama-kv-cells.h:100-117）——**无 defrag**。
- **释放**：`seq_rm`（379-445）；`clear`（366-377）。
- **与优化方向兼容性**：flat array + bitset 结构；paged 需改 block table；radix 需改节点共享。

## 2. unified KV pool 容量计算与固定预分配

- `n_ctx` 先 PAD 256（`src/llama-context.cpp:285`）；`kv_unified=true` 时 `n_ctx_seq = n_ctx`（287-288），否则 `n_ctx_seq = n_ctx/n_seq_max` 再 PAD（290-291）。
- `kv_size = cparams.n_ctx_seq`（`src/llama-model.cpp:2090` 等）；`GGML_ASSERT(kv_size % n_pad == 0)`（llama-kv-cache.cpp:98）。
- `n_stream = unified ? 1 : n_seq_max`（llama-kv-cache.cpp:82）；K/V tensor 为 3D `[n_embd_gqa, kv_size, n_stream]`（231-232）。
- **结论**：一次性固定预分配，运行时不可扩展。→ E2 起已确认：生命周期策略不能降显存峰值，只能提利用率/防 OOM/降重算。

## 3. slot / sequence / session / generation 所有权

- `server_slot`（`tools/server/server-context.cpp:198-262`）：`id` = KV cache seq id（server-context.cpp:166 渲染时 `common_batch_add(..., {t.id_slot}, ...)`）；slots 初始化 `slot.id = i`（1513-1533）。
- KV 内所有权：cell `seq` bitset 表达归属（llama-kv-cells.h:488-489）；seq→stream 由 `seq_to_stream` 决定（llama-kv-cache.cpp:146-153）。
- session 是 server 层概念（server-context.cpp:151-196），KV 本身无 session 标记。
- generation：`slot_generation` 每任务递增（server-context.cpp:230-231, 2167），仅 trace 关联。

## 4. active / idle / protected / cancelled 状态

- `slot_state` 枚举 7 态（server-context.cpp:60-66）：IDLE、WAIT_OTHER、STARTED、PROCESSING_PROMPT、DONE_PROMPT、GENERATING。**无 CANCELLED、无 protected 状态**。
- 取消是任务/流级（server-queue.h:173、server-stream.cpp:108）。
- purge 只允许选 idle slot（`!is_processing && prompt.n_tokens()>0`，server-context.cpp:2029-2037）。

## 5. prefix / cache reuse

- 命中粒度 = token 级 LCP：`n_past = slot.prompt.tokens.get_common_prefix(input_tokens)`（server-context.cpp:3711-3713）。无 hash/radix/内容寻址。
- `n_cache_reuse`（默认 256）：分块匹配后 `seq_rm + seq_add(shift)` 位移 KV（3721-3779）。
- 失效规则：prompt 被清、lora 变更（2173-2187）、checkpoint 擦除（3883-3889）、apply_ubatch 覆盖 purge（llama-kv-cache.cpp:1250-1265）。
- **与 radix 兼容性**：LCP + 位移复用可被 radix tree 子树共享替换。

## 6. purge / defrag / LRU / victim selection

- 无 defrag（mv 注释掉）；空洞由 find_slot 环回 + head 前移利用。
- `try_clear_idle_slots`（server-context.cpp:2021-2147）：仅 `kv_unified` 生效；`unified_idle_slot_policy` 默认 "default"（第一候选，2060-2065）、"lru"（last_used_tick 升序 → prompt tokens 降序 → id 升序，2041-2059）；`last_used_tick = ++lifecycle_tick` 任务分配时更新（2164）。purge = `prompt_clear()`（2117）。
- 调用点：decode 失败 retry 路径（4222-4252）。
- **radix 兼容性**：victim 应改为逐节点（当前粒度整 slot）。

## 7. seq_cp / seq_keep / seq_rm 与 COW

- `seq_cp`（llama-kv-cache.cpp:447-537）：同流 = 纯元数据（459-487，零数据复制）；跨流（非 unified）才 `ggml_backend_tensor_copy`（490-537）。
- `seq_keep`（539-564）：删其他 seq 关联；`seq_rm`（379-445）按 pos 范围删关联、空 cell 回收。
- **无物理 COW**：`get_kv_stats` 显式 `physical_sharing = false`（796-797）；共享 cell 被任一 seq 覆盖时 `cells.rm` 清全部关联（1220-1229）。
- **radix 兼容性**：metadata 共享已具备；radix 共享需相同"覆盖即失效"语义。

## 8. RoPE position / KV type / adapter identity

- `kv_layer` 只有 `il/k/v/k_stream/v_stream`（llama-kv-cache.h:229-239），无 pos/type 字段；pos 存 cell（llama-kv-cells.h:464）。
- 量化 KV 与 RoPE 冲突由 Hadamard 旋转解决：`attn_rot_k/v`（319-336，`LLAMA_ATTN_ROT_DISABLE`）、`build_rope_shift` 对量化 dequant→rotate→quant（1940-1990）。
- **adapter 不参与 cache identity**：lora 变更时 `lora_should_clear_cache` 决定清空（2173-2187）。

## 9. CPU/GPU buffer 分配与实际 KV bytes

- 按层设备选 buft（llama-kv-cache.cpp:210-219）；构造末尾 `ggml_backend_alloc_ctx_tensors_from_buft` + clear（275-293）。
- 统计：`total_size()`（1910-1918）、`size_k/v_bytes()`（1920-1938）、`used_bytes = used_cells*(size_k+size_v)/capacity_cells`（仅 n_swa==0 && capacity>0 有效，784-794）。
- **paged 兼容性**：一次性整 buffer 预分配需改为按 block 分配。

## 10. attention kernel 的物理连续性要求

- `get_k/get_v` 返回覆盖 `[0, n_kv)` 的连续 4D view（1353-1403）；空洞由 kq_mask 屏蔽（build_attn_inp_kq_mask，llama-graph.cpp:2786）。
- FA 分支（`ggml_flash_attn_ext`，llama-graph.cpp:2522-2544）直接吃连续 view；非连续只在写入端（`cpy_k/cpy_v` 用 `ggml_set_rows` scatter，1405-1493）。
- **无 paged 支持**：无 block table / gather 路径。

## 11. KV 类型与量化粒度

- 支持：F32/F16/BF16/Q8_0/Q4_0/Q4_1/IQ4_NL/Q5_0/Q5_1（`common/arg.cpp:304-314`，`-ctk/-ctv`）。
- 约束：量化 V 必须开 FA（llama-context.cpp:459-463, 3565）；`blck_size` 整除 `n_embd_head_k/v`（3576-3592）；MLA/DEEPSEEK4 要求 type_k==type_v（3560）。
- 量化粒度 = per-token 行内 block（沿 embd 维，QK 块不跨 token）：K 布局 `[n_embd_k_gqa, kv_size, n_stream]`。
- **per-channel 兼容性**：需改布局+kernel（量化方向沿 head 维分组）。

## 12. state save/load、retry、resume、cancel、shutdown

- `state_write/read`（llama-kv-cache.cpp:2067-2183）：写 n_stream + 每流 cell meta + data；`state_read_data`（2436-2496）连续 fast path / 非连续 scatter，校验 v_trans/层数/type/rowsize。
- 多层缓存：RAM prompt cache（server-task.h:629-667）+ checkpoint 快照（2784-2830）+ 磁盘 slot save/restore（3046-3131）。
- retry/resume：decode 失败 → purge + n_batch/=2 重试（4222-4252）；`is_resume` 恢复加载。
- **radix 兼容性**：共享节点 state_write 需去重/refcount。

## 13. 指标缺口

- 现有（`include/llama.h:727-751` + `/metrics/kv`）：capacity_bytes / used_bytes(+valid) / capacity_cells / used_cells / active_sequences / shared_cells / physical_sharing。
- 扩展：per-seq `seq_cell_stats`（llama-memory.h:111-116）、lifecycle trace（server-context.cpp:1050-1095）、routing events、purge 事件。
- **缺口**：
  - internal fragmentation：无空洞 cell 计数
  - recompute tokens / cache miss：无命中率、recompute token 数
  - physical blocks/refcount：无 paged 概念，`physical_sharing` 恒 false
  - SWA 时 used_bytes 无效；无逐层/逐 seq 字节统计、无 eviction 统计

## 结论（对 E6 候选的影响）

1. **P0 无损主候选（RadixAttention 式）**：llama.cpp 已具备 cell 级多 seq 元数据共享（bitset）、非连续 cell 写入（`cpy_k/cpy_v`）、LCP 前缀匹配、RAM prompt cache——缺 radix tree 索引与节点分裂/合并、token 级 LRU、批内 DFS 排序。实现集中在 server/context 层，**无需改 attention kernel**。
2. **P1 有损第二候选（SnapKV 式）**：需要 prefill 注意力分数输出通道（FA 下不物化）+ KV 紧凑重排；cell 布局与"保留原始 pos"天然兼容。
3. 无 defrag → internal fragmentation 可测；无 physical COW → physical_sharing 恒 false（metrics 已声明）。

## 附带说明

- `src/llama-kv-cache-dsv4.cpp`（75KB，MLA 定制 cache）与 `src/llama-memory-recurrent.cpp`（非 KV 内存，get_kv_stats 返回 false）不在本次审计主路径（Qwen3.5-4B 为 hybrid 架构，走 llama-memory-hybrid 委托 mem_attn）。
- 全库 grep 确认：无 defrag 实现（mv 注释）、无 paged/radix/COW 代码路径、slot_state 无 CANCELLED。
