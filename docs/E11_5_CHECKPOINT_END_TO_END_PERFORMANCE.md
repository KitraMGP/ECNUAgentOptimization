# E11.5：checkpoint 端到端收益验收（End-to-End Performance）

- 复核时间：2026-08-08
- 代码：llama.cpp `8d1b2b1e1`；TinyLlama（集成生效路径）；warmup 5 + formal 30 paired
- 协议：off = 每 rep erase + 全量 prefill P+X；on = save（P 缓存跨 rep 命中，反映 checkpoint 增量价值）+ restore + 后缀 prefill
- raw：`raw/e11_perf.json`（30 rep 全量记录）

## 1. 端到端结果（p50，30 formal reps）

| 场景 | off e2e | on e2e（含 save）| 变化 | off prompt_n | on prompt_n |
|---|---|---|---|---|---|
| P~110 × t1 | 7.12ms | 7.42ms | **+4%（持平）** | 179（全量）| 24（后缀）|
| P~110 × t4 | 27.7ms | 15.0ms | **-46%** | 179×4 | 24×4 |
| P~440 × t1 | 52.7ms | 10.0ms | **-81%** | 647 | 24 |
| P~440 × t4 | 229.6ms | 36.5ms | **-84%** | 647×4 | 24×4 |

失败率：全 0（30×4 场景）

## 2. 收益判定（E11.5 区分项）

| 维度 | 结论 |
|---|---|
| 单 target：save+restore vs 重复 prefill | P~110：**持平**（TinyLlama 上 save 开销 ≈ prefill 节省，±4%）；P~440：**-81%**（长 P 明显优）|
| 多 target：保存成本摊薄 | **成立**：t1→t4 时 on 增幅小（save 一次 + 4×restore）vs off 线性×4 → p1 -46%、p4 -84% |
| 长 prefix：收益随 P 增 | **成立**：P 179→647 tokens 时 on e2e 7.4→10.0ms（微增），off 7.1→52.7ms（线性）→ 收益随 P 增大 |
| 资源：checkpoint 常驻内存 | **state bytes**（host buffer）：TinyLlama P~110 = 73.8KB、P~440 ≈ 300KB；4B P~362 = **64.7MB/checkpoint**（recurrent 大头）——需计入 |
| 物理容量 | **KV 池不变**（state 存 host，不占 KV cells）→ 无物理容量提升（checkpoint 是"prefill 结果缓存"，非容量优化）|

## 3. 服务整体收益说明（诚实口径）

- 收益 = **避免重复 prefill**（P 的 prefill 结果缓存 + 后缀增量）——**非**容量/吞吐优化
- **已纳入**：save 成本（on e2e 含 save 请求）、checkpoint 常驻内存（state bytes）、restore 成本
- **未纳入**：索引/调度（第一版无 LRU/索引，find 为线性扫描——N 大时需评估）；4B 上未命中 cache → **主模型无收益**（E11.3/11.4）

## 4. 结论

```text
CHECKPOINT_E2E_STATUS: PARTIAL
- TinyLlama（attention，集成生效）：多 target/长 P 收益显著（-46%~-84%）、
  单 target 短 P 持平——真实端到端收益成立（在本实现范围内）
- Qwen3.5-4B（hybrid）：restore 未命中 cache → 无收益（正确性无损）
- 收益形态 = prefill 结果缓存（避免重复计算），非容量/物理显存优化
```
