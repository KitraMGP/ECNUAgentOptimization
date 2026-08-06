# E0.6 实现报告：Benchmark 实验可靠性增强

> 阶段：Phase E0.6（实验可复现性与科研可信度增强，无新 workload / 无 KV 指标改动）
> 目标：提升 Benchmark 实验可复现性与统计可信度。
> 范围：仅实现重复机制 / 统计增强 / metadata 记录 / ctx 语义检查 / long_life evaluator 优化；
> 未修改 llama.cpp、未修改 workload 设计（generate/run 未动，仅优化 evaluate）、未修改 KV 指标设计。

---

## 1. 实现内容

### 1.1 实验重复机制（--warmup / --repeat）

| 参数 | 语义 | 实现 |
|---|---|---|
| `--warmup N` | 预热 N 次场景运行，**不计入统计** | `runner.run_scenario` 中 warmup 循环结果丢弃（已有，E0.6 验证） |
| `--repeat N` | 正式执行 N 次 | `scenarios[name]` 展开为 `{"runs": [...], "aggregate": ...}`；summary 数值键跨 run 聚合（已有，E0.6 增强 evaluation 聚合） |

### 1.2 统计增强（mean/std/p50/p95）

对 **latency / throughput / cache_hit_rate** 三类指标输出统计：

- latency：`avg/p50/p95/std/max_latency_ms`（单次运行即含）；
- throughput（新增）：`throughput_tps`（端到端 = total_tokens/总耗时）、`decode_tps`（`timings.predicted_per_second` 均值）；
- cache_hit_rate / cached_tokens / recompute_tokens（单次值）。
- `repeat>1` 时所有数值键（含上述）聚合为 `{mean, std, p50, p95}`；evaluation 判据（如 `state_retention_rate`）同样跨 run 聚合，0/1 判据的均值即统计意义上的"率"。

### 1.3 实验 metadata 记录（`framework/metadata.py` 新增）

每次实验在结果 JSON 增加 `metadata` 顶层键：

| 组 | 字段 |
|---|---|
| model | path（config 或 `/props` 探测）、**sha256**、size_bytes |
| llama.cpp | **git commit**（含 dirty 标记）、build_info（`/props` 探测） |
| gpu | name、memory_total_mb、driver_version（nvidia-smi，降级 pynvml） |
| server | base_url、**total_slots（parallel）**、**slot_n_ctx**、ctx_size_config |
| experiment | temperature、seed、repeat、warmup |
| warnings | ctx 语义检查等 warning 列表 |

所有探测容错降级（server 不可达 / 文件不存在时不阻塞，记 warning）。

### 1.4 ctx-size 语义检查

llama-server 的 `--ctx-size` 会被 `--parallel` 平分到每个 slot（如 2048/4=512）。
`framework/metadata.py::check_ctx_semantics` 通过 `/props`（total_slots）与 `/slots`（n_ctx）
探测实际 slot 上下文，与配置 `ctx_size` 不一致时打印 `[WARNING]` 并记入 `metadata.warnings`。

### 1.5 long_life evaluator 优化（`state_retention_rate`）

- **主指标**：`state_retention_rate` —— 会话注入的关键状态（secret）在最终询问中被召回的比例；
  单次运行取 0/1，跨 repeat 由 runner 聚合为均值。
- `task_success` 降为保真约束参考（不再作为主指标，语义等价但明确标注）。

## 2. 验证结果

### 2.1 单元测试（58 passed）

| 测试 | 覆盖 |
|---|---|
| `test_metadata.py`（新增 8 例） | ctx 检查匹配/不匹配/不可探测、parallel 不一致、文件哈希、llama commit 结构、探测降级、collect_metadata、mock server 探测 |
| `test_metrics.py`（+3 例） | throughput_tps / decode_tps / 无 timings 降级 |
| `test_workloads.py`（+1 例） | state_retention_rate 成功/失败 |
| `test_runner.py`（+1 例） | repeat 时 evaluation 跨 run 聚合 |
| `test_e2e_smoke.py` | cli_main 结果含 metadata 键 + ctx warning；mock server 提供 /props、/slots |

### 2.2 真实 llama-server 实验（RTX 4060 8GB，Qwen3.5-4B-Q4_K_M）

**实验 A（ctx 语义检查 + metadata，server `--ctx-size 2048 --parallel 4`）**：
- 输出 `[WARNING] ctx-size 语义不匹配：配置 ctx_size=2048，但 server 实际 slot n_ctx=512（total_slots=4，总 ctx 被 --parallel 平分）`；
- metadata 完整：model sha256 `de8e96cd…`（2.7GB 哈希 ~1s）、llama.cpp commit `b06aa774c…`（dirty=False，与 build_info `b3-b06aa774c` 一致）、GPU `RTX 4060 Laptop 8188MB driver 610.43.03`。

**实验 B（repeat/warmup 聚合，server `--ctx-size 2048` 无 parallel；`--long-rounds 20 --warmup 1 --repeat 3`）**：

```
long_life repeat=3（warmup 1 次不计入）：
  prompt_tokens      mean=16467.3  std=1734.4  p50=15653   p95=18459
  total_tokens       mean=18606.3  std=1891.6  p50=17839   p95=20761
  avg_latency_ms     mean=1913.1   std=324.7   p50=1833.2  p95=2270.2
  throughput_tps     mean=495.2    std=90.8    p50=526.3   p95=566.3
  decode_tps         mean=74.5     std=2.3     p50=75.3    p95=76.3
  cache_hit_rate     mean=0.6648   std=0.0297  p50=0.6728  p95=0.6897
  state_retention_rate mean=0.0（3 次运行均未召回 secret；truncations mean=2.0）
```

验证要点：
- **warmup 生效**：日志共 80 轮 = 1×20（warmup）+ 3×20（repeat），runs 仅 3；
- **repeat 聚合生效**：全部数值键 mean/std/p50/p95；
- **evaluation 聚合生效**：state_retention_rate/task_success/truncations 跨 run 聚合；
- **ctx 检查**：slot 2048 == config 2048 → 无 warning（实验 A 的 512≠2048 → warning）；
- **metadata 完整**：model hash / commit / GPU / server slot ctx 全部记录；
- **报告**：markdown 含"## 3. 实验 metadata"表与 throughput/state_retention_rate 行。

## 3. 发现并修复的问题

| # | 问题 | 修复 |
|---|---|---|
| 1 | `model.path=None`：server `/props` 返回相对路径 `models/…`，而路径解析只查 `benchmark/` 与 cwd，找不到 workspace 根下的模型 | `framework/metadata.py` 增加 `_PROJECT_ROOT`（workspace 根）作为候选 base |
| 2 | `llama.cpp.commit=None`：worktree 的 `.git` 是 **gitfile（文件）** 而非目录，`os.path.isdir` 判断失败 | 放宽为 `os.path.exists`（兼容目录与 gitfile） |

## 4. 限制遵守

- ✅ 未修改 llama.cpp；
- ✅ 未修改 workload 设计（long_life 仅优化 evaluate 方法，generate/run 未动）；
- ✅ 未修改 KV 指标设计（cached_tokens/hit_rate/recompute 语义未变，仅新增吞吐与判据聚合）；
- ✅ 既有测试全部保留并通过（58 passed，其中 5 个为新增/扩展）。

## 5. 结论与下一步

E0.6 全部目标达成：实验现在具备 **warmup 预热、repeat 重复、mean/std/p50/p95 统计、完整 metadata（模型哈希/llama.cpp commit/GPU）、ctx-size 语义告警**，以及 long_life 的 **state_retention_rate** 主指标。可进入下一阶段（E1：KV Cache 生命周期管理评测；E0.6 的 metadata 与统计机制可直接支撑正式实验的 warmup≥1、repeat≥5、同硬件复现要求）。

未提交 git（等待人工确认）。
