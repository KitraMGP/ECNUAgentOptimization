# E4 报告：内存容量与并发承载验证（核心优化继续）

- 阶段：E4（核心内存管理优化；**暂缓 E3.5 性能门禁**）
- 日期：2026-08-06
- **状态：验证完成（探索性——GPU wall-time 不作正式性能证据）**

---

## 1. 背景与边界

- 用户指令：暂缓 E3.5 性能测试（Laptop GPU boost 抖动使 wall-time 门槛
  不可测）；转回核心大模型内存管理优化——验证内存容量/显存峰值/KV cache
  使用/slot 复用/淘汰/并发承载/OOM 边界；
- 约束（遵守）：不修改 E3.5.1 已闭环的 attribution 契约；所有 GPU 性能
  结果标记 exploratory；E3.5.3 保持 `HOLD_FOR_PERFORMANCE_EVIDENCE`；
  不执行 E3.6（lifecycle 性能门禁 + field trace validator 未满足）；
  未修改 llama.cpp（`aba4b26a` 保持）；未运行 A1/A4 收益矩阵。

---

## 2. 内存管理现状（验证基线）

| 机制 | 状态 | 内存维度 |
|---|---|---|
| unified KV 池（`--kv-unified`） | 启用 | 池共享容量 8192 cells（capacity_cells） |
| 淘汰（`--unified-idle-slot-policy lru`） | experimental（KEEP，E3.3/3.4） | 池满时价值感知 purge |
| KV 预分配 | 固定 | capacity_bytes 固定（268MB）——降显存峰值不在 scope（E2.0 结论） |
| 指标 | /metrics/kv + lifecycle trace | used_cells/capacity/active/shared |

---

## 3. 基准设计（E4，exploratory）

- `benchmark/scripts/e4_memory_capacity_gate.py`：
  - **A_并发承载**：N=2/4/6/8 并发 session × 4 轮 multi_turn → 可承载数/
    KV 利用率/显存峰值/失败率；
  - **B_OOM边界**：6 session × 轮数 4/8/12 → 池利用率增长至接近边界 +
    失败/400/截断；
  - **C_策略对比**：6×8 轮 → default vs lru（探索性）；
- 容量指标（不受 boost 抖动影响）：sessions_ok、failures、kv_peak_
  utilization、gpu_mem_peak_mb、rss_peak_mb、task_success；
- 每 replicate 全新 server（unified p4、ctx 8192、cache-ram 0、temp 0、
  seed 42、policy default/lru）。

---

## 4. 场景 A：并发承载（N=2/4/6/8，全 0 失败）

| N | policy | sessions_ok | KV 利用率（中位） | GPU 显存峰值 | failures |
|---|---|---|---|---|---|
| 2 | default / lru | 2/2 | 22.65% | 3270MB | 0 |
| 4 | default / lru | 4/4 | 24.0-24.7% | 3270MB | 0 |
| 6 | default / lru | 6/6 | 36.3-37.6% | 3386MB | 0 |
| 8 | default / lru | 8/8 | 46.4-48.2% | 3491MB | 0 |

- **unified 池共享承载 ≥8 并发 session 0 失败**（KV 利用率随并发增长
  23%→48%）；
- 显存峰值 3270→3491MB（模型 ~3.2GB + KV 增长 ~220MB）——KV 预分配
  固定（capacity 268MB 已含），显存增长来自模型上下文 buffer。

---

## 5. 场景 B：OOM 边界（6 session，轮数递增）

| rounds | policy | sessions_ok | KV 利用率 | 失败/400 |
|---|---|---|---|---|
| 4 | default / lru | 6/6 | 34.3-37.6% | 0/0 |
| 8 | default / lru | 6/6 | 70.2-72.5% | 0/0 |
| 12 | default / lru | 6/6 | **92.8-96.3%** | 0/0 |

- **接近边界（96.3%）仍 0 失败、0 400**——6×12 轮总 KV ~7800 < 8192
  （未超池；near_pressure 状态，E3.1 分类）；
- **已验证至 96.3%，饱和边界未触达**（E4.1 将验证真正跨越 8192 cells 的行为）；
  E3.1 特定超池场景（A 5109 + B 5094 > 8192）成功 purge 恢复（无 400）；
- trunc 日志为误报（"truncated = 0" 字段匹配），真实截断 0。

---

## 6. 场景 C：策略对比（探索性）

- 6×8 轮：default 与 lru 的 KV 利用率 **70.12% 相同**、0 失败——无压力
  时两策略等价（符合 E3.3/3.4 结论）；
- B 的 rounds=12：lru 峰值利用率 **92.84%** vs default **96.28%**
  （lru 淘汰更积极，used_cells 峰值略低）——探索性观察，非正式门槛。

---

## 7. 结论（内存容量维度）

1. **并发承载**：当前 unified 配置（p4/ctx 8192）可承载至少 8 个并发
   session（0 失败、KV 利用率 48%）；
2. **KV cache 使用**：利用率随并发/轮数增长（23%→96%），容量指标可精确
   观测（/metrics/kv）；
3. **OOM 边界**：已验证至 96.3%（6×12 未超池，饱和边界未触达）；E3.1
   特定超池场景成功 purge 恢复——**淘汰机制在已验证的超池场景恢复成功**，
   一般意义上的"池满不 OOM"待 E4.1 系统验证；
4. **显存峰值**：3.27-3.49GB（模型 3.2GB + KV）——KV 预分配固定，降显存
   峰值不在 scope（E2.0 既定结论）；
5. **slot 复用/淘汰**：lru 在压力下峰值利用率略低（更积极淘汰）——
   探索性；
6. **wall-time**：本阶段所有 GPU 性能数据为 **exploratory**（不作为
   <0.5%/<1% 正式性能证据）。

---

## 8. 后续（保持约束）

- **E3.5.3 保持 `HOLD_FOR_PERFORMANCE_EVIDENCE`**（性能门禁未解除）；
- **不执行 E3.6**（需：① lifecycle 性能门禁满足；② 用户提供且 validator
  通过的真实匿名 field trace）；
- attribution 契约（E3.5.1）未修改；
- 下一项可验证方向：真实 field trace 接入后的生命周期分布验证（E3.6）；
  或内存容量维度的更深验证（如更大 ctx 档的池利用率）。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e4/`（manifest/summary/csv + 34 per-replicate）；
- llama.cpp：**无新提交**（`aba4b26a` 保持）；
- 根仓库提交：`2a6ba48`（test: validate memory capacity and concurrency
  gate，5 文件 +595 行）；llama.cpp 无新提交（`aba4b26a` 保持）；
- 测试：根 pytest **167 passed**（含 test_e4_capacity 6 例）；llama.cpp
  22 + E1 5/5（复用）；
- 提交后两仓库 `git status --short` 均为空。
