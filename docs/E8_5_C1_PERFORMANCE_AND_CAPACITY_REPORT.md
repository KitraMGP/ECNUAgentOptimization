# E8.5：C1 端到端性能与容量验收报告（Performance & Capacity）

- 复核时间：2026-08-08
- 环境：TinyLlama stories260K（标准 attention-only 真实 KV 路径），CPU build，ctx=8192/parallel=4，unified，cache-ram=0，seed=42/temp=0
- 协议：warmup=5 + 正式=30 次 paired（on/off 相同输入/seed/budget，仅 `--kv-prefix-share` 不同）；每 rep slot erase 独立
- 原始数据：`raw/e8_c1_perf_{shared,partial,noshare,capacity}.json`（含全部 35 rep 记录）

## 1. 性能原始值与汇总（正式 30 reps，tgt 请求）

| 场景 | off prefill med (ms) | on prefill med (ms) | off decode med (ms) | on decode med (ms) | off e2e med (ms) | on e2e med (ms) | recompute off→on | 输出 hash 一致 | 失败 |
|---|---|---|---|---|---|---|---|---|---|
| shared | 304.53 | 5.45 | 6.83 | 4.60 | 311.64 | 10.05 | 1560→14（**-99.1%**）| ✓ | 0/30 |
| partial | 88.18 | 8.06 | 5.07 | 3.83 | 93.47 | 11.91 | 814→41（**-94.96%**）| ✓ | 0/30 |
| noshare | 22.66 | 21.77 | 3.64 | 3.58 | 26.28 | 25.53 | 376→376（**0.0%**）| ✓ | 0/30 |
| capacity | 312.18 | 5.48 | 6.68 | 4.59 | 319.15 | 10.10 | 1560→14（**-99.1%**）| ✓ | 0/30 |

**CV（>5% 标 NOISY，不删离群值）**：

| 场景 | prefill CV off/on | 判定 |
|---|---|---|
| shared | 2.48% / 9.98% | on 模式 **NOISY**（5ms 级小值绝对噪声）|
| partial | 3.42% / 5.49% | on 边缘 NOISY |
| noshare | 6.15% / 6.91% | **NOISY**（CPU 22ms 级噪声）|
| capacity | 2.82% / 13.73% | on 模式 **NOISY** |

## 2. 门禁判定（C1_ATTENTION_ONLY_PASS 性能项）

| 条件 | 数值 | 判定 |
|---|---|---|
| recompute ≥25% 下降 | -99.1% / -94.96% | **PASS** |
| no-shared prefill median 不降 >5% | -3.92%（22.66→21.77ms）| **PASS**（但 CV>5% → NOISY）|
| no-shared decode median 不降 >5% | -1.70%（3.64→3.58ms）| **PASS** |
| no-shared p95 e2e 不增 >10% | +0.81%（29.07→29.31ms）| **PASS** |
| shared 场景统计可信受益 | prefill 304.5→5.45ms（-98.2%），on 模式 CV 9.98% | **PASS（标 NOISY）** |
| output token 完全一致 | 4 场景全 ✓（on=off hash）| **PASS** |
| failed/rejected | 0/30 全场景 | **PASS** |

**结论**：所有性能门槛**数值全部达标**；但 noshare 与 on 模式 CV>5%（CPU ms 级噪声）→ 按指令"任一性能门槛无法可靠测量时保持 CONDITIONAL"，**不升级 C1_ATTENTION_ONLY_PASS**，保持 `C1_CONDITIONAL_PASS_RECOMPUTE_ONLY`（诚实保守）。

## 3. 容量分析（capacity 场景，30 reps）

| 指标 | off | on | 差异 |
|---|---|---|---|
| capacity_bytes（预分配）| 524288（8192 cells × 64B）| 524288（不变）| **0（物理分配未降低）**|
| used_cells（实际）| 3137 | 1591 | **-1546（-49.3%，逻辑 headroom）**|
| shared_cells | 0 | 1546 | +1546 |
| active_sequences | 2 | 2 | 0 |

**容量结论**：
- C1 共享使**同容量下 used_cells 降低 49.3%**（共享前缀 cell 只计 1 次）→ 同 ctx 下可容纳更多共享前缀 session（逻辑 headroom 提升）
- **不声称物理分配降低**：`capacity_bytes` 预分配不变（E2.0 已确立：KV buffer 启动预分配、运行时不可扩展）；E8.5 明确区分"预分配 bytes"（不变）vs"used cells"（-49.3%）vs"共享 headroom"（+1546 cells）
- shared_cells=1546 是元数据级共享（multi-sequence cell association，非 COW），`physical_sharing=false`

## 4. endurance 重申

- E6.5 的 12 周期 churn 测试（raw/e6_c1_churn12.json：12 cycles、no_errors=True、erase 后 used=0/active=0/shared=0）在 E8 未重跑但原始数据与脚本仍在（`e6_c1_churn.py` 可复现）；E8.2 代码改动（capability/min-lcp/日志）不影响回收路径（共享与回收逻辑分离）→ 标注 `VERIFIED（E6.5 数据 + E8.2 无回收路径改动）`

## 5. 最终 C1 状态

```text
C1_ATTENTION_ONLY_STATUS: C1_CONDITIONAL_PASS_RECOMPUTE_ONLY
```
（recompute/无损/隔离/容量 headroom 全过；性能门禁数值全达标但测量 CV>5%（CPU ms 噪声）→ 按指令保守不升级；升级需 CV<5% 的可靠测量或稳定环境）
