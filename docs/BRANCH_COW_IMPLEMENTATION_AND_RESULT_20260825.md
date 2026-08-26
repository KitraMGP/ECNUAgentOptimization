# 对话分支序列 Fork/COW 实现与多分支内存结果

- 日期：2026-08-25
- 状态：**逻辑分支 COW 与 attention paged/block COW 已实现；Qwen3.5-4B hybrid paged 仍由 capability gate 拒绝，正式 paged 显存矩阵待完成**
- 代码：`llama.cpp` 独立工作树，构建目标 `build-cuda/bin/llama-server`

## 实现

### Memory API

新增：

- `llama_memory_supports_branch_fork()`
- `llama_memory_seq_fork()`
- `llama_memory_i::supports_branch_fork()`
- `llama_memory_i::seq_fork()`
- `common_memory::seq_fork()`

标准 unified attention KV、recurrent、hybrid memory 声明支持完整 sequence fork；其它未审计 memory 仍 fail-closed。

### Server API

新增：

```text
POST /slots/{source_slot}?action=fork
{"target_slot": 1}
```

约束：source/target 必须是不同的 idle slot；source 必须包含纯文本 prompt；LoRA identity 不得冲突。该操作不依赖 `--slot-save-path`。

响应包含：

- `source_slot`
- `target_slot`
- `n_tokens`
- `unique_cells`
- `shared_cells`

`GET /metrics/kv` 增加：

```json
{
  "branch_fork": {
    "count": 4,
    "tokens": 1036,
    "semantics": "sequence snapshot fork with write isolation; physical block COW is not available in the contiguous KV allocator"
  }
}
```

### Hybrid 安全路径

Hybrid fork 首先共享 attention sequence metadata；随后通过 `LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY` 将 recurrent-only state 物化到目标 slot。这样：

- attention 公共前缀保持共享；
- recurrent target 拥有独立可写状态；
- 不要求 recurrent cache 回滚长生成后缀；
- sequential fan-out 与 batched fan-out 均走安全路径。

## 验证

### 单元/集成

```text
llama.cpp/tools/server/tests/unit/test_kv_branch_fork.py  4 passed
llama.cpp/tools/server/tests/unit/test_qwen35_branch_fork.py  1 passed（`SLOW_TESTS=1`）
最终重建后两文件合计 5 passed
benchmark/tests/test_branch_cow_memory_eval.py  2 passed
benchmark 全量 pytest  764 passed in 403.54s
```

TinyLlama 覆盖：

- fork 响应与 shared cells；
- 无 `--slot-save-path` 的 fork；
- 2 条分支独立续写；
- 删除一个分支后另一个分支继续可用；
- source==target 拒绝。

Qwen3.5-4B 覆盖：

- hybrid/recurrent metrics 可用；
- 真实 CUDA server fork；
- 两条 divergent continuation 均成功。

### Qwen3.5-4B 多分支正式矩阵

命令：

```bash
cd benchmark
uv run python scripts/branch_cow_memory_eval.py \
  --out-dir results/branch_cow_qwen35_20260825_final \
  --runs 3 --fanout 4 --ctx 2048
```

实验协议：

- Qwen3.5-4B Q4_K_M；CUDA；`--kv-unified`；`parallel=5`；`ctx=2048`；
- control：保留 parent，但每个 branch 独立完整 prefill；
- fork：parent 完成后 fork 到 4 个 target，仅处理分支 suffix；
- `n_predict=1`，避免 Qwen completion endpoint 的长 reasoning 输出引入非内存相关的 sampling 波动；
- 3 次独立 control/fork；每次另运行 4 个独立 full-prefill control 作为 hash 参考。

结果：

| 指标 | Control | Fork | 变化 |
|---|---:|---:|---:|
| formal runs | 3/3 | 3/3 | 全通过 |
| branch output hash | 全部一致 | 全部一致 | paired hash 全匹配 |
| `used_cells` | 1335 | 299 | **-77.60%** |
| `used_bytes` | 43,745,280 B | 9,797,632 B | **-77.60%** |
| `shared_cells` | 0 | 259 | 公共 parent state 共享 |
| attention `capacity_bytes` | 67,108,864 B | 67,108,864 B | 不变 |
| recurrent `capacity_bytes` | 263,454,720 B | 263,454,720 B | 不变 |
| GPU process memory sample | 3086 MiB | 3086 MiB | 不变 |
| `branch_fork.count` | 0 | 4 | 预期 |

原始证据：

```text
benchmark/results/branch_cow_qwen35_20260825_final/report.json
```

## 结论

### 已完成

1. 对话 slot 的 sequence snapshot fork API 已实现；
2. attention 公共 KV 状态实现跨分支逻辑共享；
3. 分支首次写入保持 source/target 写隔离；
4. hybrid recurrent 状态采用安全目标物化，避免长后缀回滚错误；
5. TinyLlama 与 Qwen3.5-4B 均通过分支 fork 集成测试；
6. Qwen4B 四分支正式 paired 矩阵中，有效 KV live occupancy 降低 77.60%，输出 hash 100% 一致。

### 未完成边界

连续 allocator 仍是固定连续池。因此：

- `capacity_bytes` 没有下降；
- GPU process memory sample 没有下降；
- 当前实现不能声称降低了启动时预分配显存；
- contiguous profile 的 `physical_sharing` 继续为 `false`；
- paged profile 已有 page table、动态 physical block allocator、按 block sequence 引用、写前 physical block COW、state save/load 物理行重建；
- Qwen3.5-4B 主模型是 hybrid，当前 paged profile 按 capability gate 启动拒绝；因此尚无 Qwen4B paged-vs-contiguous 显存收益结论。

所以本目标的准确判定是：

```text
逻辑/有效 KV 占用优化：PASS（Qwen3.5-4B 四分支，-77.60%）
分支正确性与生命周期：PASS
固定 KV 池预分配显存降低：NOT_COMPLETED / NOT_APPLICABLE
物理 page/block COW（paged attention profile）：IMPLEMENTED，并由 `test-kv-block`、`test_kv_paged.py` 三条 smoke 覆盖
```

### Paged 实现验证边界

直接证据：

- `build-cuda/bin/test-kv-block` 通过，覆盖 8/16/32/64 block size、generation、OOM、COW commit/rollback；
- `tools/server/tests/unit/test_kv_paged.py` 通过 3 条：paged attention/metrics、fork divergent write、state save/restore；
- TinyLlama attention-only paged fan-out 正式探针已通过 3/3 control 与 3/3 fork；每轮另有 4 个 isolated full-prefill hash 对照，`all_pass=true`、`hashes_match=true`。

### TinyLlama paged fan-out 正式探针

命令：

```bash
cd benchmark
KV_EVAL_MODEL=../llama.cpp/tools/server/tests/tmp/models--ggml-org--test-model-stories260K/snapshots/479896ec924af6d40fd419ab8f4d1eb2101de00d/stories260K-f32.gguf \
KV_EVAL_PORT=8092 \
uv run python scripts/branch_cow_memory_eval.py \
  --out-dir results/branch_cow_tinyllama_paged_final_v3 \
  --runs 3 --fanout 4 --ctx 1024 \
  --allocator paged --block-size 16 --root-tokens 10
```

| 指标 | Control | Fork | 变化 |
|---|---:|---:|---:|
| runs | 3/3 | 3/3 | 全通过 |
| isolated branch output hash | 全部一致 | 全部一致 | `hashes_match=true` |
| `used_cells` | 583 | 215 | **-63.12%** |
| physical `allocated_blocks` | 38 | 15 | **-60.53%** |
| physical `allocated_bytes` | 389,120 B | 153,600 B | **-60.53%** |
| `shared_cells` | 0 | 92 | 公共前缀共享 |
| `shared_blocks` | 0 | 6 | 物理 block 共享 |
| logical `capacity_bytes` | 655,360 B | 655,360 B | 不变，符合口径 |
| RSS sample mean | 759.86 MiB | 648.21 MiB | **-14.69%** |
| GPU process sample | 130 MiB | 130 MiB | 不变 |
| branch latency mean | 34.92 ms | 12.95 ms | -62.91%（仅本探针） |

原始证据：

```text
benchmark/results/branch_cow_tinyllama_paged_final_v3/report.json
```

准确结论：paged allocator 已在 attention-only CUDA 模型上证明四分支物理 persistent KV bytes 下降 60.53%，同时输出 hash 全匹配。GPU process sample 不变，因此不能声称本探针降低了 GPU 显存峰值；模型/compute/staging 固定开销主导该口径。Qwen3.5-4B 是 hybrid，当前 capability gate 拒绝 paged，故 Qwen4B paged 收益仍为 `NOT_APPLICABLE`，不能用 TinyLlama 结果替代。
