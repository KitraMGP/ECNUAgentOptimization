# KV Cache 分层存储与热度淘汰：可开展实现方法报告

- 日期：2026-08-21
- 范围：赛题“分层内存与异构存储”+ “KV Cache 生命周期管理”中的热度统计与按热度淘汰
- 约束：主模型 Qwen3.5-4B 为 hybrid（8 attention + 24 recurrent）；KV buffer 启动时预分配；不得把 metadata sharing 称为物理 COW
- 结论：可开展的是 **server 层 sequence-state 三级迁移** 与 **slot/session 级热度淘汰**；不可开展的是 PagedAttention/vAttention 式 GPU 内部分页，以及 H2O/SnapKV 原样 per-head 有损淘汰

**当前状态（2026-08-25）**：阶段 A/B/C 已实现并完成定向回归；新增 restore 热度元数据贯通、tier transition metrics，以及基于 recurrent-memory capability 的 hybrid restore gate。TinyLlama 分层 restore 与 Qwen3.5-4B L0→L1 offload、L1→L2 spill、安全回退均通过。Qwen3.5-4B hybrid over-long state 仍拒绝 restore 并全量 prefill，不宣称 suffix-only 命中。Qwen realistic_agent 7 轮 smoke 正确性通过但频繁迁移导致 p50/p95 变慢，尚无该场景稳定收益。Token 级有损淘汰与跨重启 L2 仍后置。

---

## 0. 现有底座（必须复用，不能另起一套 KV）

当前 fork 已经有三层“整状态”原语，粒度都是 **一个 slot / 一条 sequence 的完整 attention+recurrent state**，不是 token block。

| 层 | 现有机制 | 粒度 | 位置 | 能否降低 GPU 峰值 |
|---|---|---|---|---|
| L0 GPU | `llama_kv_cache` + `llama_memory_recurrent` 预分配 buffer | 全 ctx attention cells + `n_seq_max` recurrent rows | `llama-kv-cache.cpp` / `llama-memory-recurrent.cpp` | **否**。capacity 由 `--ctx-size` 与 `--parallel` 决定 |
| L1 Host RAM | `--cache-ram` `server_prompt_cache` | 整条 prompt 的 `llama_state_seq_get/set_data_ext` 字节向量 | `server-task.cpp:1670-1866` | **条件可以**。unified 下 `--cache-idle-slots` 会把 idle slot 序列化到 RAM 后清 GPU cell |
| L2 Disk | `--slot-save-path` `POST /slots/{id}?action=save\|restore\|erase` | 整 slot 文件 | `server-context.cpp:5463-6142` | **条件可以**。但目前是显式 API，不是自动冷热迁移 |

热度相关现有字段：

- `server_slot.last_used_tick`：单调 tick，不是 wall clock。
- `--unified-idle-slot-policy lru`：按 `last_used_tick` 升序、`prompt.n_tokens()` 降序、`slot.id` 升序。
- `prompt_cache` 淘汰：`states.pop_front()`，FIFO/oldest entry，**不是 LRU**。
- `/metrics/kv`：attention used/shared cells + 本轮新增 recurrent capacity bytes；**没有 per-token 热度**。

已验证不能直接抄的论文方案：

| 方案 | 项目判定 | 原因 |
|---|---|---|
| PagedAttention | `REJECT_INCOMPATIBLE_ARCHITECTURE` | 需要非连续 KV 读 kernel；llama.cpp attention 吃连续 view |
| vAttention | `REJECT_UNSUPPORTED_HARDWARE` | CUDA VMM + 驱动页大小；本机 RTX 4060 不支持 |
| InfiniGen | 长期候选 | 面向 CPU offload + 按 token 子集预取；当前图是整段 KV view |
| H2O 原样 | `REJECT_INCOMPATIBLE_ARCHITECTURE` | per-head 独立淘汰 vs llama.cpp token-shared cell |
| SnapKV | P1 有损候选 | 需要物化 attention 分数；FA 下不物化；hybrid recurrent 不能按 attention 前缀丢状态 |
| Quest | 现阶段 `REJECT_UNSUPPORTED_HARDWARE` | 选择逻辑可拼 ggml，加速需要稀疏页 kernel |
| MemServe | `REJECT_INCOMPATIBLE_ARCHITECTURE` | 分布式 disaggregated serving |

因此本报告只给 **能在当前 llama.cpp 数据面上落地** 的方法。

---

## 1. 分层存储：三级 sequence-state 迁移

### 1.1 目标语义

不要做 GPU 内部分页。做：

```text
L0 GPU resident  <->  L1 host RAM prompt-cache  <->  L2 disk slot file
```

对象是 **idle sequence 的完整 hybrid state**（attention KV + recurrent R/S + prompt tokens + LoRA identity）。  
active sequence 永远留在 L0。

这能降低的是 **used cells / 并发占用 / 池满时的 recompute**，不是启动时的 `capacity_bytes`。文档必须写死这条口径，否则会把 GPU 总量差误当成 KV 容量收益。

### 1.2 对象与 identity

每个可迁移对象至少包含：

```text
tier_entry {
  id
  slot_generation
  prompt_tokens
  lora_identity        // ptr + scale，E9.1 已有
  kv_types             // type_k/v/r/s
  ctx_size, parallel, kv_unified
  n_tokens, pos_min, pos_max
  state_bytes          // llama_state_seq_get_data_ext
  last_used_tick
  recency_score
  reuse_score
  recompute_cost
  location             // L0 / L1 / L2
}
```

恢复前必须全等匹配：token 前缀、LoRA、KV type、ctx/parallel/unified。缺失即拒绝，不能静默当 cache hit。hybrid 上 checkpoint-reuse 已证明“近似 restore 后全量 prefill”，所以分层 restore 必须走 **精确 sequence state**，不能走 E12 那条 prefix-aligned checkpoint 生产路径。

### 1.3 L0 → L1：自动 RAM offload

现有挂钩已经写在 `cache_idle_slots`：

- unified：idle slot 保存到 prompt cache **并清 GPU KV**。
- 非 unified：只复制到 RAM，**GPU 不清**，因为清了也不释放可复用容量。

可开展实现：

1. 把 `cache_idle_slots` 从“新任务到来时保存”升级为 **idle 超时 + 池压双重触发**。
2. 触发条件：
   - `used_cells / capacity_cells >= θ_pressure`（默认 0.90）
   - 或 idle 超过 `N` 个 lifecycle tick
3. victim 必须是 idle、非 processing、`shared_cells==0`（共享源保护已存在）。
4. 保存用现有 `llama_state_seq_get_data_ext`；成功后再 `seq_rm(slot.id, -1, -1)`。
5. 失败（alloc/identity/size 超限）保持 GPU 原状，记 `tier_offload_failed`，不得半保存半清除。

容量策略：`--cache-ram` 已按 MiB 限制。当前淘汰是 oldest FIFO。第一期可保持 FIFO；第二期改成与热度分数同一套 LRU/LFU。

### 1.4 L1 → L2：磁盘冷层

现有 `POST /slots/{id}?action=save` 已经能把 slot 写成 `slot_save_path/filename`。缺的是自动调度。

可开展实现：

1. 新增内部目录 `{slot_save_path}/tier2/`，文件名不暴露给用户 prompt。
2. 当 L1 `size() + incoming > cache_ram` 时，选 L1 中分数最低且 `f_keep` 低的 entry 落盘。
3. 落盘内容 = L1 已有字节向量 + identity 头，不再从 GPU 重新序列化。
4. L1 删除该 entry；GPU 不受影响。
5. restore 路径：请求到来 → 先比 L0 prefix → 再搜 L1 LCP → 再搜 L2 索引。命中后 `llama_state_seq_set_data_ext`，失败回滚到空 slot + 全量 prefill。

磁盘层必须有独立上限，例如 `--kv-tier2-max-mib`，默认 0=关闭。避免把用户磁盘写成无限 KV 仓库。

### 1.5 L2/L1 → L0：按需召回

召回只发生在 **新请求与冷状态有足够公共前缀** 时：

```text
lcp = common_prefix(cached_tokens, request_tokens)
accept if lcp >= min_lcp and lcp / cached_tokens >= 0.25
```

`0.25` 已是 prompt-cache `load()` 的现成阈值。召回后：

- 命中前缀计入 `n_past`
- 只 prefill `[lcp, n)`
- 更新 `last_used_tick` 并升回 L0
- 原 L1/L2 副本可删可留；第一期删，避免双份 identity 漂移

hybrid 限制：restore 必须是完整 sequence state。不能只召回 attention 前缀而丢掉 recurrent row，否则会重复 E12 的全量 prefill。

### 1.6 明确不做的分层变体

1. **GPU 内部 paged KV**。attention kernel 需要连续 K/V view；PagedAttention 已被判定架构不兼容。
2. **CUDA VMM demand paging**。本机硬件/驱动不支持。
3. **按 token 从 CPU 预取子集**（InfiniGen）。当前 `get_k/get_v` 是连续区间；子集预取要新算子。
4. **把 `--no-kv-offload` 当成分层**。它只是把 KQV 算子留在 CPU，不是冷热迁移，而且会显著降速。
5. **hybrid checkpoint-reuse 当 L2**。E12/E13 已 `NO_GO_WITH_EVIDENCE`。

### 1.7 建议分阶段

**P0 自动 L0↔L1（推荐先做）**

- 配置：`--kv-tiering ram`，默认 off。
- 行为：池压或 idle 超时 → 序列化 idle hybrid state 到 `--cache-ram` → 清 GPU cells。
- 复用：`server_prompt_cache` + `cache_idle_slots` + LoRA identity。
- 验证：Qwen3.5-4B parallel=4，制造第 5 个长请求；应看到 used_cells 下降、召回命中后 `prompt_n` 只算后缀、输出与不分层 greedy 一致。

**P1 增加 L2 磁盘**

- 配置：`--kv-tiering ram,disk --kv-tier2-max-mib N`。
- 只迁移 L1 溢出对象，不在请求路径同步写盘。
- 验证：L1 打满后文件数/字节上限、restore hash、进程重启后 L2 默认作废（第一期不跨重启，避免 identity 文件协议不完整）。

**P2 跨重启持久化（后置）**

- 需要把 identity 头写成稳定文件格式，并校验模型 SHA256 / KV type / LoRA。
- 在 P0/P1 的正确性和收益都成立前不要做。

---

## 2. 热度统计与按热度淘汰

### 2.1 两层热度，不能混用

| 层 | 对象 | 热度含义 | 用途 | 当前缺口 |
|---|---|---|---|---|
| Session/slot 热度 | idle slot 或 L1/L2 entry | 会不会被再次请求 | 决定 GPU 里留谁、RAM/磁盘放谁 | 只有 `last_used_tick` 和 prompt 长度 |
| Token 热度 | 一条序列内部的历史 token | 对后续 decode 是否重要 | 有损压缩 KV | 无 attention 分数通道；FA 不物化；hybrid recurrent 不能按 token 丢状态 |

第一期只做 **session/slot 热度**。Token 热度单独作为有损候选，默认关，且不得在 4B hybrid 上作为无损优化。

### 2.2 Session 热度分数

对每个 idle slot / cache entry 维护：

```text
recency     = last_used_tick
frequency   = hit_count
reuse_prob  = lcp_hits / lookups
recompute   = estimated_prefill_ms(n_tokens)   // 用历史 prompt_ms/token
size        = n_tokens 或 state_bytes
protect     = processing || shared_cells>0 || ckpt_source
```

推荐默认分数（全部用已有或可加的计数，不读未来信息、不读 prompt 文本）：

```text
score = wR * recency_rank
      + wF * log1p(frequency)
      + wC * recompute
      - wS * size_rank
```

建议初值：`wR=1.0, wF=0.5, wC=0.3, wS=0.2`。无历史时 `frequency=0, reuse_prob=0`，分数退化为 **LRU + 大 prompt 优先释放**，与现有 `lru` 策略兼容。

禁止进入分数的量：branch 名、用户标签、未来 round 数、模型输出文本、oracle needle。

### 2.3 统计采集点

全部 additive、默认关，走 `--lifecycle-stats` / `--kv-hotness`。

1. **launch_slot_with_task**：`last_used_tick = ++lifecycle_tick`，`hit_count++`（已有 tick）。
2. **prefix match**：记录 `lookups` 与 `lcp_hits`。LCP 扫描已在 C1 路径存在，即使 hybrid 不共享，也可以只统计不 `seq_cp`。
3. **prefill 完成**：用 `timings.prompt_ms / prompt_n` 更新该 slot 的 `recompute_cost` EMA。
4. **purge / offload / restore**：写 lifecycle event：victim id、score、pre/post used_cells、layer（L0/L1/L2）。
5. **`/metrics/kv` 扩展（可选）**：`hotness{idle_slots, mean_tick, victim_id}`；不得把缺失填 0。

Recurrent 专属统计第一期只做 **row 占用**：`n_rs_slots`、active sequences、是否可清。不要声称 recurrent token 热度，因为 R/S 是每 seq 一行，不是每 token 一格。

### 2.4 按热度淘汰：三处 victim 选择统一

今天有三套互不一致的淘汰：

- GPU unified idle：`default=first idle` 或 `lru=oldest tick`
- RAM prompt-cache：oldest deque front
- Disk slot files：无自动淘汰

应改成同一函数：

```text
pick_victim(candidates):
  filter out processing, shared_cells>0, protected
  sort by score ascending, then n_tokens descending, then id
  return front
```

接入点：

1. `try_clear_idle_slots()`：替换 default/lru 两个分支，保留 active 保护与共享源 skip。
2. `server_prompt_cache::alloc/update()`：`pop_front()` 改为按 score 选 victim。
3. L2 目录 GC：超过 `--kv-tier2-max-mib` 时删最低分文件。

回退：`--kv-hotness off` 必须逐字节回到当前 default/FIFO。`lru` 作为 control 保留。

### 2.5 Token 级热度（后置、有损、默认关）

若要做序列内部淘汰，只能走简化版，不能抄 H2O/SnapKV 原样：

1. 仅 attention 层。recurrent 层禁止按 token 丢 R/S。
2. 粒度必须是 **token/cell**，不是 per-head。llama.cpp 一个 cell 被所有 head 共享。
3. 分数来源二选一：
   - **非 FA 路径**：用物化 attention 权重做累计分（H2O 简化）。
   - **FA 路径**：只在 prefill 结束用 observation window 对 prefix 做一次 voting（SnapKV 简化），然后把保留 cell **compact 成连续区间**，因为 `get_k/get_v` 要连续 view。
4. 必须保留 recent window + sink tokens；禁止只留 heavy hitter。
5. 质量门禁走 12.12：tool JSON、system retention、禁止指令、needle。4B greedy 不一致就保持 `HOLD`，不得称无损。

这一层不降低启动 capacity，只降低长上下文的有效 used cells；对 hybrid 的 201 MiB recurrent 没帮助。

---

## 3. 推荐落地顺序

### 阶段 A：热度可观测 + 统一 victim（风险最低）

改动面：`server-context.cpp`、`server-task.cpp`、`/metrics/kv`。  
不改 KV tensor 布局，不改 attention kernel。

交付：

- `--kv-hotness recency|lfu|cost|off`
- idle purge / RAM cache 共用 score
- lifecycle event 含 victim score
- 单测：active 不清、shared 不清、off 时与 default 一致、lru control 可复现

验收：Qwen3.5-4B 多会话压力下，正确性不低于 default；若 latency/processed 改善仍 <5%，保持 experimental，不设默认。这与 E3.4 的结论兼容：均匀多会话下纯 LRU 收益会被稀释，所以必须先把分数和归属测出来，再谈收益。

### 阶段 B：L0↔L1 自动分层（真正对应“主存”）

依赖阶段 A 的 victim。  
交付：`--kv-tiering ram` + idle/pressure offload + prefix restore。

验收：

- 分层开关前后 greedy 输出 token 一致
- offload 后 `used_cells` 下降
- restore 命中时 `prompt_n` ≈ 请求长度 − lcp
- restore 失败走全量 prefill，不污染其他 slot
- GPU peak 若下降，必须同时报告 attention/recurrent capacity **未变**

### 阶段 C：L2 磁盘冷层

只在 B 的 RAM 上限成为瓶颈后做。  
验收增加：文件上限、非法 identity 拒绝、进程内 restore 成功、默认不跨重启。

### 阶段 D：token 级有损压缩（可选）

仅 attention-only 或明确标注有损的 4B 实验。不进入生产 profile。

---

## 4. 接口草案（实现时保持 additive）

```text
--kv-tiering none|ram|ram,disk     default none
--kv-tier-idle-ticks N             default 8
--kv-tier-pressure 0.90
--kv-tier2-max-mib N               default 0
--kv-hotness off|recency|lfu|cost  default off
--kv-hotness-protect-shared true
```

`/metrics/kv` 增加可选对象：

```json
"tiering": {
  "l0_used_cells": ...,
  "l1_entries": ...,
  "l1_bytes": ...,
  "l2_entries": ...,
  "l2_bytes": ...,
  "last_victim": {"id": 2, "score": 0.13, "reason": "pressure"}
}
```

缺失必须 `null`。

---

## 5. 验证矩阵

固定：Qwen3.5-4B Q4_K_M、CUDA、`--kv-unified`、temp=0、seed=42、`--cache-type-r/s f32`。

| 实验 | 目的 | 通过标准 |
|---|---|---|
| A1 parallel=4 无压力 | 无负收益 | 输出 hash 一致，latency 中位偏差 ≤3% |
| A2 第 5 会话挤池 | L0 淘汰正确 | active 不被清；victim 是最低分 idle |
| B1 offload+recall | RAM 分层正确 | used_cells 降；召回只算后缀；hash 一致 |
| B2 offload 失败 | 失败原子性 | GPU state 不变，error 可观测 |
| C1 L2 溢出 | 磁盘上限 | 不超过 `--kv-tier2-max-mib` |
| H1 hybrid 保护 | 不走死路径 | 不启用 checkpoint-reuse；restore 用 seq state |
| Q 质量 | 有损才测 | token 级淘汰必须单独开关，失败则 HOLD |

Control 永远是 `--kv-tiering none --kv-hotness off`。

---

## 6. 风险与边界

1. **不能降预分配峰值**。分层只影响 idle state 是否占用 used cells；attention 68/128 MiB 与 recurrent 50.25 MiB/slot 仍在启动时分配。
2. **hybrid restore 成本高**。整段 state 可能几十 MB；save+restore 必须小于重复 prefill 才有收益。E11.5 已证明单 target checkpoint 可能更慢。
3. **RAM cache 目前 FIFO**。若不改成热度，分层会把刚用过的大 prompt 挤掉。
4. **非 unified 清 GPU 无容量收益**。`cache_idle_slots` 已注明。分层默认要求 `--kv-unified`。
5. **共享源不能 offload**。`shared_cells>0` 必须 protect，否则破坏 C1 引用。
6. **Token 热度不是生命周期管理的第一刀**。它对 recurrent 无效，且与 FA/连续 view 冲突。

---

## 7. 建议的第一刀实现

只做两件事，足以覆盖赛题表述且不踩已否决架构：

1. **Session 热度分数接入 `try_clear_idle_slots` 和 `server_prompt_cache` 淘汰。**
2. **压力/idle 触发的 L0→L1 自动 offload + 精确 state restore。**

磁盘层、token 级压缩、paged/VMM 都列为后续。这样报告里的“可开展”是当前代码路径上能编译、能测、能回滚的工作，而不是重写 attention kernel。
