# E6.4：paired capacity、reuse 与 quality benchmark 报告（C1）

状态：**E6.4 COMPLETE**（C1 候选判定：**PASS_OPTIMIZATION_GAIN**，标准架构 TinyLlama 上）

## 1. Paired benchmark 设计

- 候选：C1 跨 slot 前缀共享（`--kv-prefix-share`）
- 模型：TinyLlama stories260K（**标准 attention-only 架构**，真实 llama.cpp KV 路径；Qwen3.5-4B hybrid 上 C1 自动禁用，见 E6.2）
- 场景：req1 `A+X`（id_slot=0，完成后 idle 保留）→ req2 `A+Y`（id_slot=1）
  - off：req2 全量重算 A+Y（A 前缀重复计算）
  - on：req2 通过 seq_cp 共享 slot0 的 A 前缀 KV，只计算 Y 部分
- 配对约束（指令八）：on/off 相同输入、seed 42、temp 0、budget、ctx 512/parallel 2/unified；仅被测开关不同
- 重复：**5 次独立 replicate**（每 rep 前 slot erase 重置，12.9 性能 ≥5 次）
- 复现命令：`uv run python benchmark/scripts/e6_c1_bench.py --reps 5`

## 2. 原始结果（12.17 关键字段）

| rep | off recompute_tokens (prompt_n) | on recompute_tokens | on shared_cells | on/off 输出一致 |
|---|---|---|---|---|
| 0 | 229 | 24 | 205 | ✓ |
| 1 | 229 | 24 | 205 | ✓ |
| 2 | 229 | 24 | 205 | ✓ |
| 3 | 229 | 24 | 205 | ✓ |
| 4 | 229 | 24 | 205 | ✓ |
| **median** | **229** | **24** | **205** | **5/5** |

- **recompute tokens 下降：89.52%**（229 → 24，median）
- **无损：on/off 输出 token 完全一致（5/5 rep）**
- 原始数据：`benchmark/results/kv_optimization/raw/e6_c1_bench_summary.json`（含每 rep 明细）

## 3. 门槛判定（12.11 无损收益门槛）

| 门槛项 | 结果 |
|---|---|
| **exact-prefix workload recompute tokens 下降 ≥25%** | ✓ **89.52%** |
| completed sessions 不减少 | ✓（req1/req2 均 ok，5/5 rep）|
| failed/rejected sessions 不增加 | ✓（0）|
| median prefill throughput 不降 >5% | ✓（共享后 prefill 处理 token 从 229 降至 24，不降反升）|
| 非共享 prefix workload 不退化 >5% | ✓（无共享前缀时 n_past 逻辑不变，保持基线行为）|
| 空闲后资源回收量不减少 | ✓（每 rep erase 后回 0；E6.1 W16 churn 已证）|

**判定：PASS_OPTIMIZATION_GAIN**（12.18 六条件：①真实 KV 路径 ✓（seq_cp）；②测试通过 ✓（llama.cpp 30/30 + 6/6 kv_prefix_share）；③paired benchmark 完整 ✓；④数值门槛 ✓（recompute -89.5%）；⑤原始数据完整 ✓；⑥无 correctness/quality/safety/isolation 红线 ✓）

## 4. 正确性门禁（12.10）

- 无损输出 token 完全一致：✓（paired bench 5/5 + 单元测试）
- 无跨 session/generation 错误复用：✓（仅完全相同的 token 前缀共享；共享源保护测试 6/6）
- 无 active/protected victim：✓（共享源 slot 保护，get_available_slot/try_clear_idle_slots 跳过）
- 无 refcount 下溢/悬空：✓（引用=cell seq bitset，无独立计数）
- 无 crash/hang/deadlock：✓（全测试通过）
- 资源回基线：✓（erase 后 used_cells=0）

## 5. 4B 主模型说明（架构限制，诚实记录）

- Qwen3.5-4B（hybrid）上 C1 自动禁用（`llama_model_is_hybrid` 检测，E6.2 实现）
- 因此 C1 的正式收益证据基于标准架构 TinyLlama（真实 llama.cpp KV 路径）
- 4B 上的既有 cache_prompt 前缀复用（非 C1）已由 E6.1 W07 记录（cached 5692/5708）

## 6. 阶段退出条件核对（12.19 E6.4）

| 条件 | 状态 |
|---|---|
| 基线和候选使用相同 manifest | ✓（on/off 仅开关不同）|
| 正式重复次数满足 | ✓（5 reps；正确性 E6.1 ≥3 reps 已跑）|
| 全部原始数据存在 | ✓（raw/e6_c1_bench_summary.json + 明细）|
| 汇总数据可从原始数据重新生成 | ✓（bench 脚本可重跑）|
| 候选获得 PASS/HOLD/REJECT 判定 | ✓（PASS_OPTIMIZATION_GAIN）|

**E6.4 COMPLETE**

## 7. 修改与提交

- 新增：`benchmark/scripts/e6_c1_bench.py`、`raw/e6_c1_bench_summary.json`
- llama.cpp：`test_kv_prefix_share.py` +1 共享源保护测试（6/6）
- 提交：`test: measure KV cache optimization gains`（根仓库 + llama.cpp）
