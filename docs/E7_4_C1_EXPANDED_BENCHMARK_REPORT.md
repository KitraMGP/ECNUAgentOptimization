# E7.4：C1 扩展 paired benchmark 报告（Expanded Benchmark）

- 复核时间：2026-08-08
- 模型：TinyLlama stories260K（标准 attention-only，真实 KV 路径）+ Qwen3.5-4B（hybrid 禁用验证）
- 每场景 on/off × **5 reps**（每 rep slot erase 独立）；相同输入/seed 42/temp 0/budget，仅 `--kv-prefix-share` 不同

## 1. 矩阵结果（TinyLlama，recompute = target 本次 prefill 处理 token 数）

| 场景 | off recompute median | on recompute median | delta | 相对变化 | 无损 | 共享日志(on) | canary 泄漏 |
|---|---|---|---|---|---|---|---|
| M01_2slot_shared_prefix | 209 | 14 | -195 | **-93.3%** | ✓ | 5/5 | — |
| M02_3slot_multi_target | 208.5 | 13 | -195.5 | **-93.76%** | ✓ | 10/10 | — |
| M03_partial_prefix | 125 | 31 | -94 | **-75.2%** | ✓ | 5/5 | — |
| M04_no_shared_prefix | 135 | 135 | 0 | **0.0%**（无退化）| ✓ | 0/5 | — |
| M05_long_code | 227 | 75 | -152 | **-66.96%** | ✓ | 5/5 | — |
| M06_long_summary | 425 | 43 | -382 | **-89.88%** | ✓ | 5/5 | — |
| M07_tool_call_json | 319 | 52 | -267 | **-83.7%** | ✓ | 5/5 | — |
| M08_canary | 235 | 24 | -211 | **-89.79%** | ✓ | 5/5 | **无** |

（5 reps 全部原始值存于 `raw/e7_c1_matrix.json`；latency 记录但 TinyLlama 为 ms 级，wall-time 仅辅助）

## 2. 门槛判定（C1_ATTENTION_ONLY_PASS 相关项）

| 条件 | 结果 |
|---|---|
| 真实 KV 路径启用（attention-only）| ✓（seq_cp 元数据共享）|
| 输出 token 与 baseline 完全一致 | ✓（8 场景全无损）|
| exact-prefix recompute ≥25% 下降 | ✓（共享场景 67-94%）|
| 非共享 workload recompute 不增 >5% | ✓（M04 = 0%）|
| 不同 token prefix 不共享 | ✓（M04 共享日志 0）|
| 多 target 共享同一 source | ✓（M02）|
| partial prefix 安全 | ✓（M03 -75.2% 无损）|
| canary 无跨 session 泄漏 | ✓（M08）|
| source/target 删除、清理 | ✓（审计测试 10/10）|
| min-lcp 边界 | ✓（审计测试）|
| 长代码 / 长摘要 / tool JSON | ✓（M05/M06/M07 无损）|

**结论：C1 在 attention-only（TinyLlama）范围内满足 `C1_ATTENTION_ONLY_PASS` 的机制/隔离/收益条件**（性能退化门禁的 prefill/decode median 与 p95 依赖 wall-time，TinyLlama 上不可靠——仅记录原始值，不作为正式门槛证据；E6.4 已用 recompute 代理）。

## 3. Qwen3.5-4B（hybrid）验证

- C1 共享日志：**0**（`llama_model_is_hybrid` 禁用，E7.2 修复后保持）
- on/off 输出 hash 完全一致（8a8576... / bd505e...，与 off 基线相同）
- **QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION**（安全禁用、无收益、无错误共享）

## 4. 原始数据与复现

- `benchmark/results/kv_optimization/raw/e7_c1_matrix.json`（含 off/on 每 rep 原始值）
- 复现：`uv run python benchmark/scripts/e7_c1_bench_matrix.py --reps 5`

## 5. 提交

- 新增：`benchmark/scripts/e7_c1_bench_matrix.py`、`raw/e7_c1_matrix.json`
- 提交：`test: expand cross-slot KV benchmark matrix`（根仓库）
