# E11.1：Qwen3.5-4B q8_0 最终边界确认（Validated Profile）

- 复核时间：2026-08-08
- 协议：warmup 5 + formal 20 paired（同 seed 42/temp 0/输入/server 生命周期，仅 `--cache-type-k/v` 不同）
- raw：`raw/e10_q8_validation.json`（E10.3，6 workload）+ `raw/e11_q8_boundary.json`（E11.1 补充）

## 1. 矩阵覆盖

| 类别 | workload | 状态 |
|---|---|---|
| E10.3 | short_qa / long_code / long_summary / tool_json / system_retention / canary（20×6）| 已有 |
| E11.1 | multi_turn（3 轮累积 20）| **新增** |
| E11.1 | save_restore（save→erase→restore→续写 20）| **新增** |
| E11.1 | long_prompt ~1000 tokens（20）| **新增** |
| 容量 | cap_long_summary（ctx4096/p4，E10.3）| 已有 |
| prompt 长度 | 256（E10.3 中档）、1024（E11.1）、近 ctx（cap）| 覆盖 |
| parallel | 1/2（主矩阵）、4（cap）| 覆盖 |
| ctx | 2048（主）、4096（cap/长 prompt）| 覆盖（8192 未测：8GB 卡 4B+ctx8192 KV 134MB×p4 超显存风险，如实记录）|

## 2. 无损验证（本矩阵内 token-exact）

| workload | f16 vs q8_0 hash | 判定 |
|---|---|---|
| 6 基础 workload（20×6）| 完全一致 | token-exact |
| multi_turn（3 轮 final context）| 完全一致 | token-exact |
| save_restore（restore 后续写）| 完全一致 | token-exact |
| long_prompt ~1000 tokens | 完全一致 | token-exact |

**表述：本验证矩阵内（temp=0/seed=42/生成长度 ≤16 tokens）q8_0 与 F16 token-exact；不得写成"全局无损"。**

## 3. 性能与资源（精确值）

| 指标 | F16 | q8_0 | 变化 |
|---|---|---|---|
| decode p50 退化（6 workload）| — | 1.06%~**2.10%**（max）| 全 <3% |
| prefill p50（long ~1000 tok）| 34.9ms | 35.8ms | +2.6% |
| GPU 多次 peak（5 采样，稳定）| 3032 MB | 2974 MB | **-58 MB**（min=mean=max，无抖动）|
| KV capacity（ctx2048）| 67.1MB | 35.7MB | -47% |
| per-cell | 32768 B | 17408 B | -46.9% |

## 4. 稳定性与失败

- failed/rejected：**0**（全部矩阵）；save/restore 全 200
- canary：无泄漏（hash 单值）
- JSON parse/schema：tool_json 输出与 F16 一致（无 schema 破坏）

## 5. q4_0 独立观察（不与 q8_0 共用结论）

- long_prompt 场景 20 reps：与 F16 hash 一致（temp=0 单采样配置）
- **未完成完整矩阵对照**（仅 1 场景）→ **`q4_0_STATUS: NOT_VALIDATED`**（独立观察记录，不作为 profile）

## 6. 结论

```text
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
```
- 范围声明：本矩阵内 token-exact；decode 退化 ≤2.10%（<3%）；GPU -58MB（多次 peak 稳定）；0 失败
- 收益形态：**主要是显存/KV 容量收益**（-47% KV、-58MB 总显存）；吞吐基本持平（prefill +2.6%、decode +1.06~2.10%）——如实保留"只有显存收益"的形态
- validated deployment profile：`--cache-type-k q8_0 --cache-type-v q8_0`（上游既有开关 + 本验收数据）
- 复现：`uv run python benchmark/scripts/e10_3_q8_validation.py --reps 20` + `e11_1_q8_validation.py`
