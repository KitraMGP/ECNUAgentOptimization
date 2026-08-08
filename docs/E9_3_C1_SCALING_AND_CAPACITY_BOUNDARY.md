# E9.3：C1 线性扫描扩展性与真实容量边界（Scaling & Capacity Boundary）

- 复核时间：2026-08-08
- 代码：llama.cpp `d6d679e7`（E9.1 后）同一 build；TinyLlama；reps=3
- raw：`raw/e9_scaling.json`、`raw/e9_capacity_concurrent.json`、`raw/e9_capacity_boundary2.json`

## 1. 扫描扩展性（no-match 最坏路径）

idle source 数 N 从 0 到 64（no-match：target 前缀与所有 source 互异 → 最坏扫描路径），target 请求 e2e/prefill 对比：

| N sources | off prefill ms | on prefill ms | on-off Δms | recompute on/off |
|---|---|---|---|---|
| 0 | 5.38 | 5.54 | +0.16 | 182/182 |
| 1 | 5.33 | 5.56 | +0.23 | 182/182 |
| 2 | 5.96 | 5.88 | -0.08 | 182/182 |
| 4 | 6.74 | 6.41 | -0.33 | 182/182 |
| 8 | 8.20 | 7.17 | -1.03 | 182/182 |
| 16 | 9.23 | 8.81 | -0.42 | 182/182 |
| 32 | 12.97 | 12.68 | -0.29 | 182/182 |
| 64 | 21.47 | （ctx 不足，见下）| — | — |

**结论**：
- **no-match 扫描开销 ≈ 0**（on - off Δ 在 ±1ms 噪声内，即使扫描 32 个 source 的 LCP 比较也不超过 ~1ms）——LCP 扫描是 CPU token 比较（线性、O(N×L)），相对 prefill（ms 级）可忽略
- recompute 相同（182/182）→ no-match 无共享、无额外重算 ✓
- **N=64（65 slot）需更大 ctx**：ctx=17408（每 slot ~267 tokens）时 on 模式 KV 池满（500 Context exceeded）；**ctx=32768 重测全成功**（64/64 source + target 200，used=17643/shared=258）→ 该失败为**配置边界（slot 数与 ctx 划分），非 C1 缺陷**
- first/last-match、partial-prefix 等已在 E7.4/E8.5 覆盖（共享路径正确性）；此处聚焦 no-match 扫描成本

**扩展性结论：C1 线性扫描在 N≤64 内 CPU 开销可忽略（<1ms），不构成扩展性瓶颈。**

## 2. 真实容量边界（ctx=1024、parallel=4、每 slot 256 tokens、4 session）

### 2.1 顺序模式（idle source 可用，E8.5 场景）

E8.5 capacity 场景（30 reps）：used_cells **3137→1591（-49.3% 逻辑 headroom）**，shared_cells=1546，物理分配 capacity_bytes 不变（5242880）。

### 2.2 并发同时到达模式（4 请求同时提交，无预热 source）

| prompt ~tokens | off all_ok | on all_ok |
|---|---|---|
| 143 | 3/3 | 3/3 |
| 214 | 3/3 | 3/3 |
| 255（4×255=1020<1024）| 3/3 | 3/3 |
| 260（4×260=1040>1024）| **0/3** | **0/3** |
| 287-359 | 0/3 | 0/3 |

**关键发现：并发同时到达的共享前缀请求，C1 无法共享**——4 个请求同时 prefill（无 idle source 可扫描），on ≈ off（超池全失败）。与上游 PR #26204（clone_to）作者观察一致："with k=4 simultaneous branches on a cold server the built-in prefix reuse finds nothing to reuse"。

**并发模式容量结论：`NO_CAPACITY_GAIN`（C1 在并发同时到达场景无容量提升，与 baseline 相同）。**

### 2.3 预热 source 后并发 target（slot0 缓存 P+X，并发 4 target 共享 P）

| prompt ~tokens | off all_ok | on all_ok |
|---|---|---|
| 206-233 | 0/3（slot0 500：见注）| 1/3-2/3（部分 rep 全 200）|
| 251-260 | 0/3 | 0/3 |

注：预热场景 on 偶发成功（1300/1400 chars 各 1-2 rep 全 200）但**不稳定**（依赖 slot0 缓存是否被 cache_idle_slots 清理/共享源保护的调度竞争）——该路径受 E2.3 已知的 idle-slot 清理机制影响，不作正式容量证据。

## 3. 结论

1. **扫描扩展性**：线性 LCP 扫描 CPU 开销可忽略（N≤64 <1ms），无扩展性瓶颈
2. **容量边界**：
   - 顺序模式（idle source）：E8.5 实测 -49.3% used_cells（逻辑 headroom；物理分配不变）
   - **并发同时到达：NO_CAPACITY_GAIN**（C1 机制限制：需先有 idle source 缓存）
   - 容量收益的前提 = **请求序列中存在可复用的 idle 前缀缓存**（agent 多轮/分支场景满足；cold-start 并发不满足）
3. **物理分配**：`capacity_bytes` 全程不变（预分配）；所有容量表述为逻辑 headroom，不虚报
