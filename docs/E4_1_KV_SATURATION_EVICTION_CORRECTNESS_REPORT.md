# E4.1 报告：Unified KV 饱和、淘汰正确性与持续并发门禁

- 阶段：E4.1（真正跨越 8192 cells 容量需求的系统验证；不依赖 wall-time）
- 日期：2026-08-06
- **最终状态：`PASS_KV_SATURATION_CORRECTNESS`**

---

## 1. 目标与边界

- 目标：在 RTX 4060 Laptop 环境，不依赖微小 wall-time 差异，验证 unified KV
  池跨越容量需求时的行为：victim 选择、活跃 session 保护、释放与复用、恢复
  语义、持续 churn、错误归属、不可恢复压力的明确失败；
- 边界（遵守）：不改 E3.5.1 attribution 契约、请求/响应 schema、采样/默认
  策略；wall-time 仅 exploratory；不放宽 failure 门槛；不合并不同 ctx/模型/
  并发配置；不用宿主机 OOM/强制 crash 证明淘汰正确；不重复 E3.5 placebo；
  不开始 E3.6。

## 2. 前置修正（E4 口径 + 解析）

- **E4 报告口径修正**（`docs/E4_MEMORY_CAPACITY_CONCURRENCY_GATE_REPORT.md`）：
  "真实 OOM 边界 >96%" → "已验证至 96.3%，饱和边界未触达"；"A1/A4 保证池满
  不 OOM" → "E3.1 特定超池场景成功恢复"；"容量随池共享扩展" → "当前配置可
  承载至少 8 session"；
- **trunc 解析修复**：禁止宽泛匹配（`log.count("truncat")` 误匹配
  `truncated = 0`）→ 结构化正则 `truncated\s*=\s*1\b`；新增单测
  （truncated=0/1/缺字段/格式错误，`test_e4_capacity.py` 10 例）。

## 3. 预注册压力预算

- 基于 token/cell 预计算（每 token 1 cell；不同前缀 session 不共享 cell）：
  - 实测校准：`build_prompt(1200)` 实际 ~1600-1700 tokens/session
    （CHUNKS 前缀），4 个串行 session 累积 `used_cells = 6305-6705`
    （**77-82% 容量**，未满不触发互相淘汰）；
  - **场景 A/D 理论总需求**：4×~1680 + S4 ~1700 ≈ **8400 > 8192** ✓；
  - 场景 E 累计需求 > 6×~8000 = 48000（多轮 churn，远超一个池容量）；
- 预注册 manifest（`results/e41/prereg_manifest.json`）记录预算、场景顺序、
  停止条件、双模式协议。

## 4. 场景 A：可淘汰 idle victim 的确定性饱和

| 项 | 结果（trace on，default/lru × 2 reps） |
|---|---|
| 4 session 后 used_cells | 6305-6705（77-82%） |
| purge 发生 | **是**（default 1 次；lru 3 次，tick 升序 4→6→7） |
| S4 压力请求 | **ok（完成）** |
| active（S0/S3） | **ok（未被清，生成完成）** |
| 400/truncation | 0/0（结构化解析） |
| 压力后 used_cells | 6573（回落） |

**理论需求 8400 > 8192 确实触发 purge/eviction**；active 从未被选为 victim；
S4 与 active 均完成。

## 5. 场景 B：victim 顺序与策略语义

- 构造 3 个不同 last-used tick 的 idle session + 压力；
- **lru 的 purge 事件 tick 全部升序**（最久未用优先，符合冻结规则
  `last_used_tick 升序 → prompt 规模降序 → slot id 升序`）——2/2 reps 契约
  一致、可复现（相同输入+seed）；
- default 走 first-eligible（slot id 顺序）——两种策略各自契约成立（不比较
  两者优劣，仅验证契约）。

## 6. 场景 C：全 active、无合法 victim 的不可恢复压力

- 4 session 全 active（长生成中）+ S4 新需求超池 → **无 idle victim 可清**；
- **结果**：S4 完成（ok）；**active 请求返回既定失败（http_500 decode error
  "Context size has been exceeded"）**——server 不 crash、不 hang、不串线、
  无静默截断、无错误 2xx；错误为 OpenAI 兼容 schema 响应；
- purges=0（无 victim 时不强行淘汰 active——**active 保护契约成立**）。

## 7. 场景 D：淘汰后恢复与 slot 复用

- lru：S1（最久未用）被清 → 回访 S1 **cached=0（正确重算）**、kv_final 回落；
- default：清的 slot 非 S1 → 回访 S1 **cached=1918（前缀命中）**；
- 两种结果均符合产品契约（重算或命中，无跨 session/generation 串线；
  trace 事件验证 slot/seq/gen 归属一致）；
- 释放的 cells 被后续请求复用，used_cells 回落（6391 < 峰值）。

## 8. 场景 E：持续 churn 稳定性

- 6 轮 create/grow/idle/pressure/revisit 序列，累计需求远超多个池容量；
- default timeline used：7785→7939（**波动无单调增长**）；lru 恒定 7785；
- kv_final ≤ capacity、400=0、无悬挂 session、无负计数/超 capacity 计数、
  无死锁（server_exited 全 true）。

## 9. 双模式验证

| 模式 | 结果 |
|---|---|
| correctness（trace on） | purge/归属/事件链完整可核对（30-110 事件） |
| production（trace off） | **lifecycle trace 事件=0**；purge 行为一致（WRN 独立信号：
  default purge_wrn=1、lru=3，与 trace on 一致）；容量/失败行为一致 |

## 10. 门禁判定（11 项全过）

| 门槛 | 结果 |
|---|---|
| 至少一个场景理论需求 > 8192 cells | ✓（A/D：8400） |
| 可淘汰场景实际 purge 且请求完成 | ✓ |
| active session 从未被错误淘汰 | ✓（A active ok；C purges=0 不淘汰 active） |
| victim 选择符合策略/tie-break 契约 | ✓（lru tick 升序 2/2） |
| 无合法 victim 时既定 schema 合法失败 | ✓（C：active 500 decode error，不 crash） |
| 淘汰后恢复/重算符合契约 | ✓（D：cached=0 重算或命中） |
| 无跨 session/generation/slot 错误归属 | ✓（trace 事件核对） |
| churn 后资源回收、无持续增长 | ✓（E：~7800 稳定波动） |
| failure/truncation/事件结构化解析 | ✓（truncated=1 正则 + 单测） |
| production 模式 lifecycle 事件=0 | ✓ |
| 回归测试全过 | ✓（见第 11 节） |

**无 REJECT 触发**（无 active 误淘汰、无跨 session 复用、无静默截断/crash/
死锁/不回收、victim 契约符合、production 零事件）→ **`PASS_KV_SATURATION_CORRECTNESS`**。

## 11. 测试

- 根 pytest：**177 passed**（新增 `test_e41_saturation` 6 例 + `test_e4_capacity`
  补 trunc 4 例）；
- llama.cpp：lifecycle trace 7/7 + unified lifecycle 5/5 + slot routing 10/10
  + E1 5/5（复用，无改动）；
- `git diff --check` 通过。

## 12. 说明与后续

- **E3.5.3 保持 `HOLD_FOR_PERFORMANCE_EVIDENCE`**（性能门禁未解除）；
- wall-time 本阶段全部 exploratory；容量与正确性指标为门禁依据；
- **不执行 E3.6**（field trace 未提供）；
- E4 报告的"饱和边界未触达"结论已被本阶段**真正跨越 8192 cells** 的系统验证
  取代（本报告为准）。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e41/`（prereg_manifest/summary/saturation_results.csv/
  eviction_events.jsonl + 24 per-scenario JSON）；
- llama.cpp：**无新提交**（`aba4b26a` 保持）；
- 根仓库提交：`99b9a91`（test: validate KV saturation and eviction
  correctness，9 文件 +784 行）；llama.cpp 无新提交（`aba4b26a` 保持）；
- 提交后两仓库 `git status --short` 均为空。
