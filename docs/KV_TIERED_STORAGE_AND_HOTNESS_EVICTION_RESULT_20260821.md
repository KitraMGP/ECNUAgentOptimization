# KV 分层存储与热度淘汰：第一刀实现结果（2026-08-21）

- 范围：计划 `docs/KV_TIERED_STORAGE_AND_HOTNESS_EVICTION_PLAN.md` 阶段 A+B
- 交付：`--kv-hotness off|recency|lfu|cost`（默认 off）+ `--kv-tiering none|ram|ram,disk`（默认 none）
- 状态：`IMPLEMENTED_PROBE`。TinyLlama 7 例 PASS；Qwen3.5-4B A1/A2/B1/C1 PASS。默认仍关闭。Token 级有损淘汰未做。
- 口径：不能降低启动预分配峰值；只降低 idle used cells / 池满 recompute。

## 实现

| 开关 | 默认 | 行为 |
|---|---|---|
| `--kv-hotness off` | yes | `try_clear_idle_slots` 保持 default/lru；prompt-cache 保持 FIFO `begin()` |
| `--kv-hotness recency\|lfu\|cost` | no | 统一 `kv_hotness_score()`：更低分更好 victim；active / `shared_cells>0` 永不入候选 |
| `--kv-tiering ram` | no | 需要 `--cache-ram` 且 `--kv-unified`。压力、全 idle、或 idle-generation≥N 时 `prompt_save` 成功后再 `prompt_clear` |
| `--kv-tiering ram,disk` | no | 在 ram 基础上，L1 溢出写入 `{slot-save-path}/tier2/`；需 `--slot-save-path` 且 `--kv-tier2-max-mib>0` |
| `--kv-tier-idle-ticks` | 8 | 全 idle 时 `update_slots` 也会 offload（否则 tick 永不涨） |
| `--kv-tier-pressure` | 0.90 | `used_cells/capacity_cells` |
| `--kv-tier2-max-mib` | 0 | 进程内 L2 字节上限；0=磁盘层关闭。不跨重启 |

失败原子性：`prompt_save` 失败（含 size 超限）保持 GPU 原状，记 `tier_offload_failed`。已在 cache 中的 identity 匹配视为保存成功。空 GPU slot 在 ram 模式下走 L1 `prompt_load`（显式 `id_slot` 也召回）。

`/metrics/kv` additive：`hotness.policy/tick`、`tiering.mode/l1_entries/l1_bytes/last_victim`。缺失仍为 null，不填 0。
`ram,disk` 另增 `l2_entries`/`l2_bytes`；关闭时为 null。

## TinyLlama 单测

`llama.cpp/tools/server/tests/unit/test_kv_hotness_tiering.py`：5 passed。

覆盖（原 5 例）：

- off 保持 default 第一个合格 idle（slot0）
- recency 选最久未用 idle（slot1），不清最近使用的 slot0
- 非法 `--kv-hotness` / `--kv-tiering` 启动失败
- ram offload：`pre_used > post_used`，召回 `prompt_n` 下降且 greedy 文本一致

本轮新增 2 例（合计 7）：

- 无 `--slot-save-path` 时 `ram,disk` 降级为 `ram`，`l2_entries=null`
- L2 spill+restore：`__TIER2_SPILL__` / `__TIER2_RESTORE__`；召回 `prompt_n` 下降且 greedy 文本一致；`l2_bytes` 不超过 cap

回归：`test_unified_idle_lifecycle.py` + 本文件共 10 passed。

本轮合计：hotness/tiering 7 + idle-lifecycle 5 = 12 passed。

## Qwen3.5-4B 评测（A1/A2/B1）

固定：`Qwen3.5-4B-Q4_K_M.gguf`、CUDA、`--kv-unified`、temp=0、seed=42、`--cache-type-r/s f32`。  
复现：`cd benchmark && uv run python scripts/kv_hotness_tiering_eval.py`  
归档：`benchmark/baseline/qwen35-4b_gpu_kv_hotness_tiering_eval_20260821.json`

| 实验 | 结果 | 数字 |
|---|---|---|
| A1 无压力 hash/latency | PASS | hash 全同；p50 207.2 vs 201.1 ms（-2.97% ≤3%） |
| A2 第 5 会话挤池 | PASS | purge=`[1]`（最久未用 idle）；slot3 active 未清；used 1574→1808 / cap 2048 |
| B1 offload+recall | PASS | hash 全同 `126357be…`；offload slot0 pressure 305→0 cells；recall `prompt_n` 298→4；attention capacity 仍 64 MiB（ctx2048），recurrent 仍 2 rows |

B1 同时确认 attention/recurrent **capacity 未变**。used_cells 降到 0 是 idle sequence 迁到 L1，不是预分配缩小。

## Qwen3.5-4B C1（L2 溢出）

`--kv-tiering ram,disk --cache-ram 128 --kv-tier2-max-mib 256`。归档 `benchmark/baseline/qwen35-4b_gpu_kv_hotness_tiering_c1_20260821.json`。

| 实验 | 结果 | 数字 |
|---|---|---|
| C1 L2 溢出 | PASS | `__TIER2_SPILL__` 1 次；hybrid over-long state 安全拒绝 restore 并全量 prefill；L2 57.9 MiB ≤ 256 MiB；greedy hash 同 `126357be…` |

**hybrid 限制（必须写进结论）**：Qwen3.5-4B restore 后不能 `seq_rm` 掉已生成后缀（recurrent 位置语义），否则 abort。当前 hybrid/recurrent prompt cache 在 state 长于请求时拒绝 restore，保留冷层文件并走全量 prefill；不宣称后缀-only `prompt_n`。TinyLlama（attention-only）仍允许后缀 prefill。B2 保存超限时验证 GPU state 保留、L1 不新增。

## 完整对照评测

归档：`benchmark/baseline/qwen35-4b_gpu_kv_hotness_tiering_final_20260821.json`。

固定 Qwen3.5-4B Q4_K_M、CUDA、`ctx=2048/4096`、`parallel=2/4`、greedy、seed=42、Qwen hybrid recurrent F32。Control 为 `--kv-hotness off --kv-tiering none`。

| 场景 | Control | Enabled | 对比结果 |
|---|---:|---:|---|
| A1 latency p50（recency vs off） | 200.92 ms | 205.61 ms | +2.34%，hash 一致 |
| B1 recall latency（ram vs none） | 261.37 ms | 234.11 ms | -10.43%，hash 一致；used cells 305→0 |
| C1 recall latency（ram,disk vs none） | 225.23 ms | 210.56 ms | -6.51%，hash 一致；L2 spill 1 次、57.9 MiB |

A2 压力场景：recency 选择 slot 1，active slot 未清理，`used_cells` 1574→1808 / capacity 2048。四个场景全部 PASS。A1 仅 3 个请求，延迟结果是单轮探针，不升级为稳定性能收益。

## 2026-08-25 实现收口与快速复测

本轮实现：

- L1/L2 restore 将 `last_used_tick`、`hit_count`、`recompute_ms` 回写 slot，保持冷热迁移后的热度连续性；
- `/metrics/kv` 增加 `lookup_count`、`lcp_hit_count`，以及 `offload_count`、`l1_restore_count`、`l2_spill_count`、`l2_restore_count`、`l2_gc_count`；
- TinyLlama/Qwen tiering 回归增加 transition-counter 断言。

验证：

```text
18 passed  # test_kv_hotness_tiering.py + test_unified_idle_lifecycle.py + test_metrics_kv.py
A1/A2/B1/C1: PASS
```

最新 Qwen 运行证据：`benchmark/baseline/qwen35-4b_gpu_kv_hotness_tiering_post_restore_metrics_20260825.json`。

| 场景 | 结果 | 观测 |
|---|---|---|
| A1 | PASS | recency p50 216.09 vs control 209.89 ms，+2.95%，hash 同 |
| A2 | PASS | victim=slot1；active 未清；used 1574→1808 / capacity 2048 |
| B1 | PASS | `offload_count=1`；used cells 305→0；attention capacity 64 MiB、recurrent capacity 100.66 MiB 不变；hash 同 |
| C1 | PASS | `l2_spill_count=1`；57.9 MiB ≤ 256 MiB；`l2_restore_count=0`，因为 hybrid over-long state 安全拒绝并全量 prefill；hash 同 |

B1/C1 的 Qwen hybrid 结果证明了真实 GPU used-cell 释放、L2 上限和安全回退；TinyLlama 测试证明 attention-only L1/L2 restore 命中。最终 capability-gate 源码的 B1/C1 复测仍为 PASS，归档 `benchmark/baseline/qwen35-4b_gpu_kv_hotness_tiering_capability_gate_final_20260825.json`。Qwen 本轮单次 A1/B1/C1 延迟分别为探针结果，不升级为稳定性能收益；后续长生命周期收益需要重复 paired 矩阵。

追加快速 paired probe（B1/C1，各 3 次，使用 restore metadata 修复后的最终源码；control 与 enabled 每次独立启动）：归档 `benchmark/baseline/qwen35-4b_gpu_kv_hotness_tiering_final_repeated_20260825.json`。

| 场景 | 平均延迟变化 | 范围 | 正确性/容量 |
|---|---:|---:|---|
| B1 RAM offload | -6.81% | -6.00%～-7.45% | 3/3 hash 同；每次 used cells 305→0 |
| C1 RAM+disk | -6.80% | -6.37%～-7.02% | 3/3 hash 同；每次 spill=1，L2≤256 MiB |

这组结果说明当前短场景中存在可重复的探针级延迟下降，同时满足 hash 和缓存占用门禁；仍不是 20～40 轮真实 Agent 工作流的稳定生产收益证明，后者需要后续长生命周期 paired 实验。

`realistic_agent` 7 轮 smoke 经过 recurrent-memory capability gate 修复后完成：Control 与 tiered 均 `task_success=true`。归档 `benchmark/baseline/qwen35-4b_gpu_realistic_agent_tiering_smoke_20260825.json`。tiered 自动 offload 保持正确性，但 p50 `2020.6 ms` vs control `1878.2 ms`（+7.58%），p95 `3624.7 ms` vs `2287.1 ms`（+58.48%），说明短 Agent 会话中频繁冷热迁移的重算成本超过收益；该场景不能宣称性能提升。性能收益证据仍来自 completion 级 B1/C1 paired probe，真实长生命周期 Agent 需提高复用率/压力触发阈值后再测。

## 不做的边界（本轮保持）

- token 级 SnapKV/H2O
- GPU 内部分页 / CUDA VMM
- 把 `--checkpoint-reuse` 当 hybrid restore
- 默认打开 hotness / ram / ram,disk
- 跨重启 L2 持久化

## Session 热度淘汰完整实测（2026-08-25）

新增 runner：`benchmark/scripts/kv_hotness_eviction_matrix.py`。固定 Qwen3.5-4B Q4_K_M、CUDA、`ctx=2048`、`parallel=3`、`--kv-unified`；每个策略独立启动 3 次，每次构造 20 次 hot session reuse、1 个 cold session 和 1 个 pressure session，记录 victim、KV metrics、延迟和输出 hash。Control 为 `--kv-hotness off`，比较 `recency`、`lfu`、`cost`。

归档：

- 原始矩阵：`benchmark/results/kv_hotness_eviction_matrix_repeated_20260825/report.json`
- 汇总证据：`benchmark/baseline/qwen35-4b_gpu_session_hotness_eviction_matrix_20260825.json`

| 策略 | 3 次 victim | 平均 hot latency | 平均 pressure latency | used cells（前→后） | hash |
|---|---|---:|---:|---:|---|
| off | 0,0,0 | 35.53 ms | 305.04 ms | 1324→1376 | 3/3 一致 |
| recency | 0,0,0 | 37.17 ms | 304.37 ms | 1324→1376 | 3/3 一致 |
| lfu | 1,1,1 | 40.54 ms | 307.51 ms | 1324→1504 | 3/3 一致 |
| cost | 0,0,0 | 37.09 ms | 303.86 ms | 1324→1376 | 3/3 一致 |

实测结论：

1. **LFU 价值保护有效**：20 次 hot session reuse 后，LFU 在 3/3 次实验中淘汰 cold slot 1；off/recency/cost 淘汰 slot 0。该结果与策略语义一致：recency 更重视 slot 的最近一次使用时间，而 LFU 保护高频 session。
2. **recency 与 off 在本构造中相同**：pressure 请求前 hot slot 是更早建立的 slot 0，且测试没有在 pressure 前再次访问它；因此 recency 选择 slot 0 并非实现失效，而是该访问时序下的预期结果。
3. **输出正确性稳定**：每个策略 3/3 次 pressure 输出 hash 均为 `75a11da4…`，无策略导致输出变化。
4. **本矩阵验证的是 victim 选择，不是性能收益**：单次 pressure latency 差异不足以宣称生产收益；LFU/recency/cost 的平均 pressure latency 与 off 接近，且该场景只迁移一次。要证明收益，需要冷热访问分布下的多轮 restore/offload 长生命周期实验。
