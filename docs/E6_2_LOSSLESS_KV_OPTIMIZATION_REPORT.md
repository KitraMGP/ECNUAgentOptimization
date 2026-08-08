# E6.2：无损 KV 优化实现报告（C1：跨 slot 前缀 KV 共享）

状态：**E6.2 COMPLETE（标准架构上 PASS；hybrid 架构上架构限制）**

## 1. 实现概览（llama.cpp）

**C1（主 P0 候选）**：跨 idle slot 前缀 KV 共享，基于 RadixAttention/ChunkAttention 的"前缀复用"思想，用 llama.cpp 已有的 `seq_cp` 元数据共享机制实现（零数据复制、无损）。

### 新增参数
- `--kv-prefix-share`：启用跨 slot 前缀共享（experimental，**默认 off**，关闭即原始行为）
- `--kv-prefix-share-min-lcp N`：最小共享前缀 token 数（默认 64，防碎片）

### 核心机制（tools/server/server-context.cpp）
新请求在 `cache_prompt` 分支计算 `n_past` 后，若启用且满足条件：
1. 扫描所有 idle slot，找与请求 LCP 最长的缓存 prompt（> min_lcp 且 > 当前 n_past）
2. 用 `llama_memory_seq_cp(src.id, slot.id, 0, best_lcp)` 把前缀 cell **元数据共享**到当前 seq（unified pool 下同 stream 纯 bitset 操作，零数据复制）
3. 同步 `slot.prompt.tokens` 为输入前缀、清 checkpoints、`n_past = best_lcp`
4. 只计算 `[lcp, n)` 剩余部分

### 共享引用保护（无损契约）
- 共享源 slot（其 cell 被其他 seq 引用，`seq_cell_stats.shared > 0`）：
  - `get_available_slot` 只允许"完全命中其缓存前缀"的请求占用（防覆盖破坏共享者）
  - `try_clear_idle_slots` 跳过（防清理破坏共享者）
- 引用计数 = cell 的 seq bitset 关联数（论文 refcount 语义天然由 bitset 提供，`physical_sharing=false` 语义不变）

### hybrid 架构自动禁用（无损保护）
Qwen3.5-4B 是 hybrid（attention + recurrent）：recurrent state 是按 token 连续的不可分割状态，**无法按前缀分割共享**（`llama_memory_hybrid::seq_pos_min = max(attn, recr)` 被 recr 拉高触发全量重算；且复制 src 的 tail state 会引入跨 session 信息）。因此共享条件加 `!llama_model_is_hybrid(...)`，hybrid 上自动禁用（WRN 提示），**保证 hybrid 模型输出与基线完全一致**。

## 2. 机制验证（tinyllama，标准 attention-only 架构，真实 KV 路径）

`tools/server/tests/unit/test_kv_prefix_share.py`（5 例，全部通过）：

| 测试 | 验证点 | 结果 |
|---|---|---|
| test_kv_prefix_share_reported_in_log | 参数生效 | ✓ |
| test_kv_prefix_share_off_default | 默认 off | ✓ |
| test_prefix_shared_across_slots | A+X(slot0) 后 A+Y(slot1) 共享 A：`/metrics/kv shared_cells > 0` + 日志 "E6-C1: shared N-token" | ✓ |
| test_off_no_shared_cells | off 时 shared_cells=0 | ✓ |
| test_lossless_output_identical | on/off 输出 token 完全一致（greedy） | ✓ |

## 3. 4B GPU 验证（Qwen3.5-4B，build-cuda）

- `--kv-prefix-share` 启用时：日志确认共享被 hybrid 检测禁用（无共享日志），输出与 off 完全一致（无损保护生效）
- 4B 上 C1 状态：**REJECT_UNSUPPORTED（hybrid 架构）**——recurrent state 不可分割共享，已在实现层自动禁用；标准架构模型（tinyllama 等）可完整使用

## 4. 不变量（12.10/12.13 相关）

- active/protected 请求不被淘汰：共享源 slot 有引用时不可被覆盖/清理 ✓
- 无跨 session 错误复用：共享仅限"完全相同的 token 前缀"（LCP 匹配），cache identity 隐含（同 model/同 slot context）✓
- 无损：同输入同 seed 输出 token 完全一致（测试 5/5）✓
- 可配置关闭：`--kv-prefix-share` 默认 off，关闭后行为与基线完全一致 ✓
- 清空后资源回收：共享引用随 seq 释放消失（bitset），E6.1 W16 churn 已证 ✓

## 5. 测试汇总

- llama.cpp 集成测试（真实 tinyllama server）：kv_prefix_share 5 + active_pressure 3 + unified_idle 5 + slot_routing 10 + lifecycle_trace 7 = **30/30 passed**
- E1 手动回归：**5/5 passed**
- 根仓库 pytest：**177 passed**
- 构建：CPU + CUDA 均 exit 0

## 6. 收益定位（E6.4 正式测量）

- 机制收益：跨 slot 共享前缀时 `recompute_tokens` 下降（只算 [lcp, n)）、`used_cells` 不重复占用前缀
- 适用 workload：W07 变体（不同后缀共享长前缀）、多 slot 分支场景
- 已知限制：hybrid 模型（Qwen3.5-4B）不支持；需标准架构模型做正式 paired benchmark（E6.4）

## 7. 修改与提交

llama.cpp：
- `common/common.h`：kv_prefix_share / kv_prefix_share_min_lcp 参数
- `common/arg.cpp`：--kv-prefix-share / --kv-prefix-share-min-lcp 注册
- `tools/server/server-context.cpp`：共享逻辑 + 共享源保护 + hybrid 检测
- `tools/server/tests/utils.py`：ServerPreset 参数支持
- `tools/server/tests/unit/test_kv_prefix_share.py`：新增 5 例
- 提交：`perf: optimize unified KV cache utilization`（C1 跨 slot 前缀共享）

根仓库：
- `benchmark/scripts/e6_c1_probe.py`：4B 验证脚本
- 提交：E6.2 报告
