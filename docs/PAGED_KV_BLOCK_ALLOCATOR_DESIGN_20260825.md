# Paged KV / Block Allocator 与物理 COW 设计

- 日期：2026-08-25
- 状态：核心实现完成，CUDA Qwen3.5 paired 显存矩阵待执行
- 目标：在保持 token/状态正确性的前提下，将连续预分配 KV 改为按需 block 分配；公共完整 block 在 conversation fork 后共享，部分 block 在首次写入时执行 block-level COW。

## 1. 当前架构约束

当前 `llama_kv_cache`：

- 每个 layer 使用连续 tensor：`[embedding, kv_size, stream]`；
- `v_cells` 是逻辑 cell 元数据，cell index 同时是 K/V tensor 行号；
- `seq_cp()` 同 stream 只共享 sequence bitset；
- `get_k/get_v()` 直接返回连续 KV view；
- `cpy_k/cpy_v()` 通过 `ggml_set_rows()` 写入连续 tensor；
- attention mask 遍历逻辑 cell index。

因此，真正 paged KV 不能只增加一个 refcount：必须同时改变

1. 逻辑 cell → 物理 block/offset 映射；
2. K/V 读视图构造；
3. K/V 写入构造；
4. block 分配、释放、COW；
5. `/metrics/kv` 容量与物理占用统计。

第一版不修改 recurrent memory 的内部状态布局；Qwen3.5 hybrid 的 recurrent state 继续使用 sequence-level snapshot materialization，paged allocator 首先覆盖 attention KV。hybrid 的 attention page savings 与 recurrent capacity 分开报告。

## 2. 选择的设计

### 2.1 block 粒度

- 默认 `block_size = 16` logical KV cells；
- 支持配置值 `8|16|32|64`，默认 16；
- block 内 cell position 保留原始 RoPE position，不重编号；
- block 是 K/V 两个 tensor 的同一逻辑物理单元，refcount 统一管理；
- 当前实现只允许 non-SWA、standard attention KV、Flash Attention/V-transposed disabled 的 paged profile；未支持的 layout 启动时 fail-closed。

### 2.2 两级映射

```text
logical cell index
        ↓
cell_location { block_id, offset }
        ↓
physical block { refcount, generation, live_cells, K tensor, V tensor }
```

逻辑 cell 数仍受 `kv_size` 上限约束，避免改动 mask 的公共维度；物理 block 数按需增长，不能超过 logical capacity 的 block 上限。

`v_cells` 仍记录 position/sequence bitset，但不再把 cell index 当作 tensor row。新增 `cell_locations` 保存物理映射。

### 2.3 按需物理分配

- 创建 cache 时只创建 block metadata，不分配完整 K/V buffer；
- 首次写入 logical cell 时分配对应物理 block；
- 每个 layer 的 block K/V tensor 独立拥有 backend buffer；
- block tensor shape：
  - K：`[n_embd_k_gqa, block_size]`；
  - V（paged profile）：`[n_embd_v_gqa, block_size]`；
- buffer 使用 `ggml_backend_buft_alloc_buffer()` 按 block 分配；
- 释放 block 时先同步 context，再释放 backend buffer；
- `memory_breakdown()` 和 `/metrics/kv` 统计实际 allocated block bytes，而不是 logical capacity。

为避免一开始创建大量 backend allocation，block metadata 支持 free-list；后续可把多个 block 合并为 slab，但 slab 不属于本阶段合同。

### 2.4 attention 读路径

连续 cache 直接返回 view；paged cache 改为构造 staging tensor：

1. 根据当前 logical cell 顺序收集 `{block_id, offset}`；
2. 将每个 block 的 K/V 行读入临时连续 tensor；
3. 用 `ggml_get_rows()` + `ggml_set_rows()` 将物理行按 logical cell 顺序重排；
4. reshape 成现有 attention graph 需要的 K/V 形状；
5. 现有 mask 仍按 logical cell index 工作，不改 causal/SWA position 语义。

同一 block 被多个 sequence 引用时只读一次；staging tensor 是请求/graph 生命周期内的临时内存，不计入 persistent KV block bytes。

### 2.5 attention 写路径

`cpy_k/cpy_v` 不再直接将 logical cell index 当 tensor row：

1. 解析每个 batch token 的 logical cell；
2. 按 block 分组；
3. 从 `k_cur/v_cur` 提取对应 batch rows；
4. 对每个 block 执行 `ggml_set_rows(block_tensor, rows, local_offsets)`。

写入前执行 `ensure_writable(seq_id, logical_cells)`：

- block 只被当前 sequence 引用：直接写；
- block 被多个 sequence 引用且写入会改变已有 cell：
  1. 分配新 block；
  2. 复制整个 block 的 K/V 数据；
  3. 创建目标 sequence 对应的 logical cell 副本；
  4. 将目标 sequence 的 bitset 引用从旧 cell 移到副本；
  5. 更新副本的 `cell_locations`；
  6. 旧 block refcount 减一，新 block refcount 加一；
  7. 继续写入。

公共完整 block 不复制；最后一个 partial block 在追加前必须唯一化或执行 COW。

### 2.6 fork 语义

`seq_fork(src, dst, p0, p1)`：

1. 验证 source/destination identity、generation、range；
2. 对完整 block：destination 增加同一 logical cell 的 sequence 引用和 block refcount；
3. 对 partial block：不允许两个 branch 共享将要写入的 partial cell；fork 时复制 partial block，或者将其标记为 `cow_pending`，首次写入时复制；第一版采用 fork 时复制，减少写路径复杂度；
4. 记录 `fork_count`、`shared_blocks`、`cow_operations`、`cow_bytes`；
5. 任意 block 分配失败时回滚全部新增引用和 block。

### 2.7 删除与回收

`seq_rm()`：

- 仅删除该 sequence 的 cell 引用；
- block `refcount` 只在该 sequence 不再引用 block 的任何 cell 时递减；
- `refcount==0 && live_cells==0` 才进入 free-list；
- active/shared block 不得进入 free-list。

### 2.8 state save/load

第一版 paged profile：

- state save/load 继续序列化 logical cell metadata 与实际 token state；
- restore 时重新分配物理 blocks，禁止复用保存文件中的 block id；
- generation 重新生成；
- block refcount 从 restore 后的 sequence table 重建；
- save/load 失败不得留下半提交 block table。

## 3. API 与参数

新增 common 参数：

```text
--kv-allocator contiguous|paged       (default contiguous)
--kv-block-size 8|16|32|64           (default 16)
```

兼容规则：

- 默认 `contiguous` 行为不变；
- `paged` 与 `--kv-unified`、non-SWA、标准 attention KV、支持的 V layout 不满足时启动失败；
- hybrid 可以启用 attention paged，但 recurrent capacity 单独统计；
- MSA/DSA/DSV4/ISWA 暂不启用 paged profile。

新增 metrics：

```json
{
  "allocator": {
    "mode": "paged",
    "block_size": 16,
    "logical_capacity_cells": 2048,
    "allocated_blocks": 21,
    "allocated_bytes": 9797632,
    "shared_blocks": 16,
    "cow_operations": 1,
    "cow_bytes": 49152,
    "free_blocks": 3,
    "fragmentation_cells": 7
  }
}
```

现有 `capacity_bytes` 继续表示 logical capacity；新增 `allocated_bytes` 表示实际 persistent physical block bytes，防止两种口径混淆。

## 4. 实现文件

### Core

- `src/llama-kv-block.h/.cpp`：block metadata、free-list、refcount、generation、COW transaction；
- `src/llama-kv-cache.h/.cpp`：paged mode、cell mapping、read staging、write grouping、fork/erase；
- `src/llama-memory.h`：allocator capability/metrics 扩展；
- `include/llama.h`：allocator stats 扩展。

### Model/context

- `src/llama-model.cpp`：根据 `kv_allocator` 创建/拒绝 paged profile；
- `common/common.h/.cpp`、`common/arg.cpp`：参数解析；
- `src/llama-context.cpp`：stats API。

### Server

- `tools/server/server-context.cpp`：启动 capability、metrics、fork COW 诊断；
- `tools/server/README.md`：参数与 metrics 合同。

## 5. 测试门禁

必须覆盖：

1. block refcount 等于 logical sequence 引用；
2. free block refcount 为 0；
3. active block 不在 free-list；
4. model/adapter identity mismatch 禁止共享；
5. partial block fork COW；
6. source/target divergent output hash 一致；
7. 删除中间 branch 只释放自身引用；
8. 最后引用删除后 block 回收；
9. generation 变化拒绝旧 table；
10. state save/load 重建 refcount；
11. allocator OOM 原子失败；
12. 8/16/32/64 block size；
13. CPU paged smoke；
14. CUDA Qwen3.5-4B paired memory matrix；
15. contiguous profile 全量回归不变。

## 6. 验收指标

### Correctness

- output token hash 100% 与 contiguous control 一致；
- zero crash/hang/NaN；
- block invariant 全部通过；
- erase/shutdown 无 block leak。

### Memory

- `allocated_bytes` 随实际 live blocks 增长，不随 `capacity_cells` 固定预分配；
- Qwen4B fanout 4 的 persistent attention allocated bytes 相比 contiguous control 至少下降 25%；
- GPU sample 必须同时记录，若 allocator bytes 下降但 GPU sample 不变，结论只能标记 staging/model overhead 主导，不得宣称显存峰值下降；
- capacity 与 allocated 两种口径分别报告。

### 性能

- full prefill/recompute 下降；
- paged staging overhead 单独记录；
- 若 p95 延迟增加超过 10% 且无 allocated-memory 价值，保持实验性关闭。

## 7. 主要风险与回滚

- `ggml_get_rows/set_rows` 对量化 K/V 与 CUDA backend 的 layout 支持必须逐类型验证；
- 每 block backend buffer 会增加 allocation overhead；
- staging graph 可能抵消部分显存收益；
- paged 仅通过显式 `--kv-allocator paged` 开启，任何 capability/allocator 失败立即回退为启动错误，不静默切换；
- 删除 paged mode 参数和 block files即可回滚，contiguous 默认路径不改变。

## 8. 当前实现与验证

已落地：

- `llama-kv-block.*`：有界 block free-list、generation、按 block sequence 引用、COW transaction、OOM 原子失败；
- `llama-kv-cache.*`：paged K/V 物理 tensor、logical-cell 映射、按需分配/回收、fork 共享、写前物理 block COW、state save/load 物理行重建；
- `prepare()` 保存并恢复 allocator checkpoint 与 block→sequence metadata，避免 speculative slot probing 永久泄漏 logical→physical 映射；backend block tensors 保守延迟释放，防止 scheduler graph 悬挂引用；
- server：`--kv-allocator paged`、`--kv-block-size 8|16|32|64` 与 allocator metrics；
- 测试：`tests/test-kv-block.cpp`、`tools/server/tests/unit/test_kv_paged.py`。

当前直接证据：`test-kv-block` 通过；paged attention、branch fork/divergent write、四 slot 独立请求、state save/restore 四条 server smoke 通过。修复 graph 跨 block-table 复用和“同 block 不同 logical cell”误触发 COW 后，TinyLlama attention-only CUDA fanout=4 × 3 轮正式探针全部通过：output hash 全匹配，persistent `allocated_bytes` 389,120→153,600 B（-60.53%），RSS mean 759.86→648.21 MiB（-14.69%），GPU process sample 130→130 MiB（不变）。证据为 `benchmark/results/branch_cow_tinyllama_paged_final_v3/report.json`。Qwen3.5-4B hybrid 仍由 capability gate 拒绝 paged，结果为 `NOT_APPLICABLE`，不可宣称主模型 paged 收益。
