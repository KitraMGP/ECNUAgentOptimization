# Qwen3.5-4B Hybrid KV Optimization: Round Result

- 记录日期：2026-08-21
- 模型：`models/Qwen3.5-4B-Q4_K_M.gguf`
- 模型 SHA256：`de8e96cd0d0c358487091aaaed1346bc02e61da3d4b412c833662702e233e78c`
- llama.cpp commit：`4a699aaad2ae95fc0968c6bd823b9a46f3abc3e0`
- 构建：CUDA `b8570-4a699aaad`，工作树含本轮改动
- GPU：RTX 4060 Laptop 8 GB
- 生成：`temperature=0`、`seed=42`、`enable_thinking=false`

## 1. 结论

本轮优化已证明 **attention KV 的分组件容量可观测，且 q8_0/q8_0 相比 F16/F16 降低 attention 预分配容量；recurrent R/S 仍为 F32，容量不变**。这属于局部容量收益，不是完整 hybrid state 优化。

- Q8 主候选：`PASS_PROBE_ONLY`。8K exact-token needle 单轮 paired 探针通过，但尚未完成计划要求的两轮、parallel=1/4/10、32K formal paired matrix，因此不升级既有 validated deployment profile。
- Q4 次级候选：`NOT_VALIDATED`。启动和短 completion smoke 通过，尚未完成质量 paired 门禁。
- recurrent F16/BF16：`REJECTED_FOR_QWEN35`。当前 CUDA recurrent graph 的 `scale` 路径要求 F32；代码现在在 context 创建阶段明确拒绝，不隐式回退、不允许 backend assertion。
- `n_rs_slots < n_seq_max`：`REJECTED_UNTIL_ROW_ROUTING`。当前 recurrent graph 以 `seq_id` 直接寻址物理 row，缩小容量而不做逻辑到物理 row 映射会产生状态覆盖风险。

## 2. 分组件容量对比（ctx=4096, parallel=4, kv-unified）

启动配置保持模型、build、ctx、parallel、offload 和 recurrent F32 不变；只改变 attention K/V 类型。

| 指标 | F16/F16 baseline | Q8/Q8 本轮 | Q8 相对 baseline | Q4/Q4 探针 | Q4 相对 baseline |
|---|---:|---:|---:|---:|---:|
| attention capacity | 128 MiB | 68 MiB | **-60 MiB / -46.88%** | 36 MiB | **-92 MiB / -71.88%** |
| recurrent total | 201 MiB | 201 MiB | **0 / 0%** | 201 MiB | **0 / 0%** |
| recurrent R | 9 MiB | 9 MiB | 0% | 9 MiB | 0% |
| recurrent S | 192 MiB | 192 MiB | 0% | 192 MiB | 0% |
| memory components total (attention + recurrent) | 329 MiB | 269 MiB | **-60 MiB / -18.24%** | 237 MiB | **-92 MiB / -27.96%** |
| recurrent rows | 4 | 4 | unchanged | 4 | unchanged |

Q8 live `/metrics/kv` evidence：

```json
{
  "attention": {"capacity_bytes": 71303168, "capacity_cells": 4096},
  "recurrent": {
    "capacity_bytes": 210763776,
    "r_bytes": 9437184,
    "s_bytes": 201326592,
    "n_rows": 4,
    "n_seq_max": 4,
    "n_rs_seq": 0,
    "n_rs_slots": 4
  }
}
```

F16 baseline 同配置的 attention capacity 为 `134217728` bytes；recurrent capacity 同为 `210763776` bytes。Q4 探针 attention 为 `37748736` bytes，recurrent 仍为 `210763776` bytes。

> 口径：上表是 memory component capacity，不是完整进程 GPU 占用。完整 GPU 占用还包含模型权重、compute buffer、CUDA allocator 和桌面环境，不能把总显存差直接等同于 KV capacity 收益。

## 3. 8K exact-token needle paired 探针

固定输入由新增 `needle` workload 通过 `/apply-template` + `/tokenize` 二分构造，目标 8192 tokens；实际 prompt token 数按位置分别为 8187/8189/8188，Q8 与 F16 完全一致。两次运行均为 `parallel=1`、`ctx=32768`、Qwen3.5-4B、F32 recurrent。

| 指标 | F16/F16 baseline | Q8/Q8 本轮 | 差异 |
|---|---:|---:|---:|
| prompt tokens | 24564 | 24564 | 0 |
| completion tokens | 39 | 39 | 0 |
| total tokens | 24603 | 24603 | 0 |
| early/middle/late recall | 3/3 | 3/3 | 无下降 |
| average latency | 3278.8 ms | 3237.7 ms | **-41.1 ms / -1.25%** |
| p50 latency | 3272.4 ms | 3248.2 ms | **-24.2 ms / -0.74%** |
| p95 latency | 3296.5 ms | 3256.7 ms | **-39.8 ms / -1.21%** |
| decode throughput | 69.03 tok/s | 69.02 tok/s | -0.01 tok/s / -0.01% |
| peak GPU sample | 3866 MB | 3500 MB | **-366 MB / -9.47%** |
| peak RSS sample | 2254.0 MB | 2017.1 MB | **-236.9 MB / -10.51%** |

逐位置结果：

| needle | prompt tokens F16/Q8 | latency F16/Q8 | recall |
|---|---:|---:|---|
| early | 8187 / 8187 | 3272.4 / 3256.7 ms | F16/Q8 均通过 |
| middle | 8189 / 8189 | 3267.6 / 3208.2 ms | F16/Q8 均通过 |
| late | 8188 / 8188 | 3296.5 / 3248.2 ms | F16/Q8 均通过 |

证据文件：

- Q8：`benchmark/results/bench_20260821_130843.json`
- F16：`benchmark/results/bench_20260821_130915.json`

> 该 paired 探针只有每个 profile 一次、三个位置各一次请求。GPU/RSS 是绝对峰值采样，且不是两轮独立实验；因此只能作为本轮探针指标，不能替代正式复现性门禁。

## 4. Q4 次级探针

Q4/Q4 在 ctx=4096、parallel=4 下启动成功：attention capacity `36 MiB`，recurrent capacity `201 MiB`。固定短 completion smoke 返回 `QWEN_HYBRID_OK`，但没有完成 F16↔Q4 的 token-level paired matrix、needle 三位置质量门禁和两轮复现。因此 Q4 保持 `NOT_VALIDATED`，不能作为部署建议。

## 5. 代码与观测交付

- `/metrics/kv` 新增 `attention` 与 `recurrent` 分组件对象；不适用字段为 `null`。
- recurrent stats 新增 R/S bytes、rows、`n_seq_max`、`n_rs_slots`。
- CLI 新增 `--cache-type-r`、`--cache-type-s`，允许 `f32/f16/bf16`；默认 F32。
- CLI 新增 `--n-rs-slots`；当前只接受与 `--parallel` 相等的值，其他值启动阶段明确拒绝。
- benchmark 新增 `needle` 场景和精确 token 计数；修复 `/apply-template`、`/tokenize` 从 `/v1` base URL 错误拼接的问题。

## 6. 验证状态

- CUDA build：通过 `cmake --build build-cuda -j$(nproc)`。
- benchmark 全量：`762 passed`。
- 定向 workload/driver/config/runner：`39 passed`。
- checkpoint/参数相关 C++ 测试目标已定位；无独立 `test-kv-prefix-share-capability` 或 `test-slot-save` target。
- 计划中的完整 32K formal matrix（F16/Q8 × p1/p4/p10 × 多负载 × 两轮）尚未执行。
- 本轮所有 llama-server 进程已停止；未提交、未 push。

## 7. 复现命令

```bash
# Q8 exact-token needle
cd benchmark
uv run python agent_bench.py \
  --host 127.0.0.1 --port 18085 \
  --scenario needle --needle-target-tokens 8192 \
  --ctx-size 32768 --parallel 1 --output-dir results

# F16 paired baseline：仅 cache type 改为 f16/f16
uv run python agent_bench.py \
  --host 127.0.0.1 --port 18086 \
  --scenario needle --needle-target-tokens 8192 \
  --ctx-size 32768 --parallel 1 --output-dir results
```

服务启动时分别使用：

```bash
--ctx-size 32768 --parallel 1 --n-rs-slots 1 --kv-unified \
--cache-type-r f32 --cache-type-s f32 --cache-type-k q8_0 --cache-type-v q8_0
```

或将最后一段 K/V 改为 `f16/f16`。
