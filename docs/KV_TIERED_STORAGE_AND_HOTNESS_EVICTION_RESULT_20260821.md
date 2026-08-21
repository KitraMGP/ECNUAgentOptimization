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

## 不做的边界（本轮保持）

- token 级 SnapKV/H2O
- GPU 内部分页 / CUDA VMM
- 把 `--checkpoint-reuse` 当 hybrid restore
- 默认打开 hotness / ram / ram,disk
- 跨重启 L2 持久化
