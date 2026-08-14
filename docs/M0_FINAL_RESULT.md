# M0 正式矩阵最终结果（run6/run7，2026-08-09）

> 状态：**完成**——wrapper（v66-v68）启用后两次完整 24-unit 4B formal 矩阵均
> `verdict=PASS`、120/120 reps OK、0 ERROR；G-M0-6 复现性 PASS。
> HEAD：根仓库 `b6b060f`（v68）｜ llama.cpp `4a699aaad`（8570，零改动）。

## 1. 运行命令与环境（两轮完全一致，唯一差异 out/tmp run 标识）

```bash
cd benchmark && uv run python -m runner.m0_fanout_runner \
  --server-bin ../llama.cpp/build-cuda/bin/llama-server \
  --model ../models/qwen3-5-4B-Q4_K_M.gguf --ctx-size 4096 \
  --out results/m0_formal_4b_run{6,7}_20260809.json \
  --tmp-dir results --port-base 8080 --ngl 99 --cleanup-tmp-age 3600
```

- 环境：RTX 4060 Laptop 8188 MiB / driver 610.43.03 / server b8570-4a699aaad /
  Qwen3.5-4B Q4_K_M.gguf / ctx 4096 / parallel=fanout+2（4/6/10）
- 每轮完整 15 个 server 生命周期（校准 1 + dv 2 + formal 12 groups）、
  24 units × 5 formal reps（warmup 2/unit）、2 cache profile × 3 fanout × 2 control
- wrapper 生效：`TRANSIENT_MAX_ATTEMPTS=3`、退避 0.25/0.5、SDK max_retries=0
- 顺序执行：run6 完全 cleanup（server 无泄漏、GPU 回落 40 MiB）后 run7

## 2. 两轮结果对账（每轮独立 canonical validator）

| 对账项 | run6 | run7 |
|---|---|---|
| canonical validator | OK | OK |
| groups（off+on） | 12（6+6） | 12（6+6） |
| reps / units | 120 / 24 | 120 / 24 |
| 5 reps/unit | 全 True | 全 True |
| (unit_id, rep_index) 重复 | 无 | 无 |
| rep status | 120 OK / 0 ERROR | 120 OK / 0 ERROR |
| matrix_complete | True | True |
| verdict | **PASS** | **PASS** |
| notes | 0 | 0 |
| 运行耗时 | ~11 min（23:08→23:19） | ~11 min（23:20→23:32） |

**gates（两轮一致）**：G-M0-1/2/3a/4/5/7 = **PASS**；G-M0-3b（smoke 观测，恒
N/A）、G-M0-6 = **NOT_APPLICABLE**（单轮内无法自证，见 §4 跨轮计算）。

与 run5 对比：run5（wrapper 未启用）36/120 connection_error → verdict
INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE（**INVALID_INTERMEDIATE**，保持降级）；
run6/run7（wrapper 启用）**0 connection_error**——transient retry 是 run5 高
失败率的根因修复，非掩盖。

## 3. 决策验证（G-M0-1 数据源）

| session | run6 | run7 |
|---|---|---|
| off（10 requests） | 10 valid / 0 invalid / 0 err | 10 valid / 0 invalid / 0 err |
| on（10 requests） | 10 valid / 0 invalid / 0 err | 10 valid / 0 invalid / 0 err |

G-M0-1（决策合法输出率 100%）= **PASS**（正式矩阵无 fallback：
`decision_fallback` 全 False）。

## 4. run6-vs-run7 配对分析与 G-M0-6 复现性

G-M0-6 定义：同配置两次独立 run，TTFT/latency 中位数偏差 ≤10%，决策分支归属一致。

| 指标 | run6 | run7 | 偏差 |
|---|---|---|---|
| latency 中位数 | 467.0 ms | 451.7 ms | **3.29%** ≤10% |
| ttft 中位数 | 261.3 ms | 267.0 ms | **2.15%** ≤10% |
| decision_fallback | 全 False | 全 False | 一致 |

**分支归属一致量化（按实际复算字段）**：
- 跨轮：同 unit+rep 120 对 `decision_fallback` mismatch = **0/120**（全 False）；
- off-on 严格配对：run6/run7 各 60 对 `decision_fallback` mismatch = **0/60**（全 False）。

→ **G-M0-6 = PASS**（**归档后跨轮计算**——G-M0-6 为跨两次独立 run 的复现性
判定，非单 run 机器 gate；单轮 gates 报 NOT_APPLICABLE 是单轮自证限制，
正式结论取 run6-vs-run7 跨轮计算）。

同 unit+rep 120 对逐对比较：
- status 一致性：120/120（0 mismatch）
- latency 差异：median +0.36%、max|.| 19.38%（个别长桶 rep 噪音）
- ttft 差异：median +0.57%、max|.| 52.77%（个别 rep 首 token 噪音）
- peak_gpu_mb：median 0.0 MiB、max 2.0 MiB（两轮一致）
- peak_rss_mb：median -0.5 MiB（系统/进程级观测不稳定，个别 rep 跨轮差异大）
- kv peak used_cells：median 0、max 4（两轮一致）

资源峰值（两轮一致）：
- 组级 baseline gpu_used_mb：3026–3414（mean 3202，两轮逐 group 相同）
- rep peak_gpu_mb：max 3424 MiB / median ~3120 MiB（8GB 卡内，无 OOM）
- rep peak_rss_mb：max 2767/2866 MiB
- rep kv peak used_cells：max 3192（attention 池 4096 cells 内）

## 5. 严格 paired off/on（结论 B 数据）

**定义**：同一 run 内、同 (fanout, bucket, ctk, ctv, rep_index) 的 off/on 对，
**两边 status 均 OK** 才入样（120/120 OK → 每 run 60 对全入）。不用不均样本均值。

| 维度 | run6（n） | run6 latency on-off | run6 ttft on-off | run7（n） | run7 latency on-off | run7 ttft on-off |
|---|---|---|---|---|---|---|
| all | 60 | **+0.24%** | -0.07% | 60 | **+0.62%** | +0.99% |
| profile=q8_0 | 30 | -0.09% | -0.00% | 30 | +0.23% | -0.15% |
| profile=f16 | 30 | +1.68% | -0.23% | 30 | +1.01% | +1.28% |
| fanout=f2 | 30 | +0.03% | +0.01% | 30 | +0.32% | +0.07% |
| fanout=f4 | 20 | +1.12% | +1.03% | 20 | +1.32% | +1.67% |
| fanout=f8 | 10 | +0.88% | -1.88% | 10 | +6.88% | +10.27% |
| bucket=short | 30 | +0.24% | -1.13% | 30 | +0.46% | +0.07% |
| bucket=medium | 20 | -0.18% | -0.07% | 20 | +0.89% | +1.48% |
| bucket=long | 10 | +2.61% | +2.85% | 10 | +0.39% | +0.99% |

结论：**run7 fanout=f8 同时 latency +6.88%、ttft +10.27% 为 n=10 小样本
（parallel 10 高并发）下的噪音例外**；其余全部维度 ±≤3%。**无系统性 on
方向收益**；两轮方向/量级不一致（run6 f8 ttft -1.88% vs run7 +10.27%）
→ 纯噪音。

## 6. 两层结论（M0 最终判定）

**结论 A（benchmark/归因门禁）**：**PASS**——run6/run7 双轮 24/24 units、
12 groups、120/120 reps OK、matrix_complete、canonical validator 通过、
gates G-M0-1/2/3a/4/5/6/7 全 PASS（G-M0-3b 恒 N/A）。M0 benchmark 基础设施
（workload、生命周期、观测、归因、复现性）验证完备。

**结论 B（on 机制优化收益）**：**NOT_APPLICABLE / NO_GAIN**——4B hybrid 模型
下 `--kv-prefix-share` 被 capability gate 拒绝（key-lines 证据：
`E8-C1: capability rejected: memory implementation does not support cross-slot
prefix metadata sharing (only standard unified attention KV is audited)`，
G-M0-5 PASS 验证）。**主证据是 on control 的 g6–g11 六个 group 启动日志中的
capability rejection 行——共享机制未建立**；两轮 24 个 group baseline
`/metrics/kv` 快照 `shared_cells` 全 0 仅作为**一致性佐证**（不称全部周期
样本——基线快照不等于运行期周期样本全集）。**不存在共享发生**；严格 paired
off/on 差异为噪音级（§5）且两轮不一致。**不得把 benchmark PASS 称为共享
优化 PASS**——on 与 off 在本模型上运行同一未共享路径，差异仅开关本身的开
销/噪音。

## 7. 归档（benchmark/baseline/）

| 文件 | 说明 |
|---|---|
| `qwen35-4b_gpu_m0_formal_run6_20260809.json` | run6 主结果 |
| `qwen35-4b_gpu_m0_formal_run7_20260809.json` | run7 主结果 |
| `qwen35-4b_gpu_m0_formal_run6_evidence_20260809.json` | run6 命令/环境/矩阵/gates |
| `qwen35-4b_gpu_m0_formal_run7_evidence_20260809.json` | run7 命令/环境/矩阵/gates |
| `qwen35-4b_gpu_m0_formal_run6_logevidence_20260809.json` | run6 15-tag key lines（RS/KV/capability） |
| `qwen35-4b_gpu_m0_formal_run7_logevidence_20260809.json` | run7 15-tag key lines |
| `qwen35-4b_gpu_m0_formal_run6v7_paired_20260809.json` | run6-vs-run7 配对分析 + G-M0-6 + paired off/on 分维度 |
| `qwen35-4b_gpu_m0_formal_run5_*.json`（保持） | run5 **INVALID_INTERMEDIATE**（不撤销降级） |

完整 -lv5 日志留 `results/m0_fanout_*` 专属目录（gitignore 不入库）。

## 8. 限制（如实声明）

- G-M0-6 为**归档后跨轮计算**（run6-vs-run7 复现性判定，非单 run 机器 gate）；
  单轮 gates 报 NOT_APPLICABLE（单轮内无法自证复现性）。
- **dv 决策多样性受限**：decision validation 仅固定 short 桶 + fanout=8 的
  决策 prompt，模型输出均为 `ACTION: branch(b1)`——**不覆盖决策输出多样性**
  （b1-b8 多分支路由未在 dv 中实证；正式矩阵 120/120 决策 OK 亦全为 b1 固定
  路由，fallback 未触发）。
- rep 级 RSS 差异属**系统/进程级观测不稳定**（进程 RSS 采样受系统调度/内存
  状态影响，个别 rep 跨轮差异大），非采样抖动断言；GPU/RSS 峰值跨轮 median
  一致、max 差异 ≤100 MiB。
- 无代码改动（本轮纯运行+归档）——全量 pytest 沿用 v68 HEAD 实测
  **758 passed**（`cd benchmark && uv run pytest -q`，2026-08-09）。
- run5 保持 INVALID_INTERMEDIATE（wrapper 未启用的中间结果，不作为正式结论）。
