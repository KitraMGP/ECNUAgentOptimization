# E3.4 报告：A1/A4 真实多会话 Workload 验证与生产暴露门禁

- 阶段：E3.4（真实多 client/多 sequence 并发验证；**未修改任何 workload
  文件、未修改 llama.cpp**）
- 日期：2026-08-06
- **最终状态：`KEEP_EXPERIMENTAL_A1A4`**（依据见第 15/17 节）

---

## 1. 授权和禁止边界

- 授权：验证 A1/A4 在真实多 client、多 sequence、统一 KV 池竞争场景下
  是否保留近期回访热点；至少两个 workload 家族稳定触发 unified pressure；
  收益是否来自 victim 选择；判断 `lru` 提升为生产可选/默认暴露；
- 禁止（未违反）：修改 `try_clear_idle_slots()` LRU 规则、default 行为、
  策略默认值、last-used tick 语义、seq 原语/KV buffer/memory core/decode/
  采样/tokenizer/cache-reuse、COW/分层/压缩、OpenAI 响应、A2 routing、
  既有 workload 文件、用 branch 名/未来信息参与策略、synthetic 结果当真实
  结果、无目标扫描、改 workload prompt 触发压力、同一 server 多 replicate、
  仅凭 used_cells/purge/cached 宣称收益；
- **本阶段 llama.cpp 零改动**（`049872f59` 保持）。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp | `049872f59`（E3.2 实现；本阶段无改动） |
| 根仓库 | 起始 `3dcdb9f`；本阶段新增提交（见附注） |
| server binary | `build-cuda/bin/llama-server`（含 E3.2 实现，hash 见 manifest） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| GPU | RTX 4060 Laptop 8188 MiB |

---

## 3. E3.2/E3.3 状态复核（10 项一致性审计）

| # | 审计项 | 结果 |
|---|---|---|
| 1 | default 仍选第一个合格 idle | ✓（`candidates.front()`） |
| 2 | lru 仅 `kv_unified=true` | ✓ |
| 3 | non-unified 保持原行为 | ✓（E3.4 场景 7 smoke） |
| 4 | active/processing 不作 victim | ✓（E3.2 真并发单测） |
| 5 | 无候选保持既有失败路径 | ✓ |
| 6 | last-used tick 不依赖 workload/branch/未来 | ✓（launch 分配点单调更新） |
| 7 | lifecycle stats 关闭无事件 | ✓（E3.2 测试） |
| 8 | 诊断不入 OpenAI response | ✓ |
| 9 | 本阶段不修改 llama.cpp | ✓（零改动） |
| 10 | E3.3 策略/tie-break/回退未漂移 | ✓（零改动保障） |

**结论：与 E3.2/E3.3 报告一致，无差异**。

---

## 4. 多 client 并发协议

- 新 `benchmark/scripts/e34_multi_session_workload_gate.py`：
  - 每 replicate 启动 4 个**独立 client 线程**，每线程独立 `Driver` +
  `RecordingDriver`（独立 history/evaluator）→ **真实并发多 sequence**；
  - 每 session 完整执行既有 workload（`get_workload().run()`，不改文件）；
  - **S0 热点**：重放 S0 最后请求（自动路由，tick 更新）；
  - 压力请求（新 active 会话，~6300 tokens，自动路由）；
  - 回访 S0（热点）与 S1（冷）；
  - **禁止 id_slot 强制 victim**（全部自动路由）；
- 并发性验证：`concurrency_verified`（4 session 全 ok）+ session 级
  `active_sequences_max=4`（4 个 seq 真实并存）——**并发协议可靠建立**。

---

## 5. session/slot/sequence 映射证据

- session 级 `active_sequences_max`：S0/S1/S2/S3 各 **4**（4 个独立 seq
  同时存在，统一池竞争）✓；
- 每请求记录 used_cells_before/after + active_sequences_after（聚合代理）；
- **gap 记录**：每请求的精确 `slot_id` 未记录（RecordingDriver 仅记聚合
  metrics）——任务八字段缺口；用 `active_sequences` 聚合推断多 seq 存在，
  精确 slot 归属需 E3.5 补 additive 只读诊断（若需要）。

---

## 6. multi_turn 多会话结果（pressure 5×2）

| 指标 | 结果 |
|---|---|
| pressure 机会分类 | **10/10 pressure_opportunity_observed** |
| 正确性 | 0 失败、eval 100%、task_success 100%、contamination 0、clean 全过 |
| lru 热点回访不劣于 default | **5/5**（processed 与 latency） |
| paired median 改善 | **processed 0.0% / latency 1.7%**（无显著收益） |
| wall time 不劣于（≤3%） | 5/5 |

**关键发现**：压力 100% 可复现、lru 无负收益、但**收益 ≈ 0**——真实并发
下 S0 的 slot 归属**均匀随机**（S0 落在 slot0 的概率约 1/4），default 清
"第一个合格 idle"（slot id 最小）在多数 rep 中清理的**不是 S0 的 slot** →
两策略都保留 S0 → 无差异。

---

## 7. long_life 多会话结果（pressure 5×2）

| 指标 | 结果 |
|---|---|
| pressure 机会分类 | **3/10 observed + 7/10 near_pressure**（coverage < 4/5） |
| 正确性 | 0 失败、eval 100%、9527 召回 100%、contamination 0 |
| 收益 | **opportunity_not_observed**（coverage 不足，任务十 D 规则） |
| wall time 不劣于（≤3%） | 5/5 |

- long_life 会话 KV 总量（~4×1900=7600）+ 压力 6300 已超池，但多数 rep
  池满触发的是 **near_pressure**（decode 未进 retry/purge 分支——4B cell
  位置复用特性，E3.2 已记录）→ 严格按预注册规则记为 not_observed。

---

## 8. pressure coverage 和 opportunity 分类

| 场景 | observed | near_pressure | not_observed |
|---|---|---|---|
| multi_turn pressure | 10/10 | 0 | 0 |
| long_life pressure | 3/10 | 7/10 | 0 |
| multi_turn no_pressure | 0 | 6/6 | 0 |
| long_life no_pressure | 0 | 6/6 | 0 |

- **仅 multi_turn 家族达到 coverage ≥4/5**；long_life 未达；
- no_pressure 变体为 near_pressure（无 adapter 压力，池接近但未满）——
  不计入收益分母，计入正确性/成本。

---

## 9. default/lru victim 选择

- mt_pressure 有压力样本：lru 选 tick 最小（最久未用）的 idle slot；
  default 选第一个合格（slot id 最小）——均符合冻结规则（lifecycle 事件
  证明）✓；
- **但**：真实并发下"tick 最小"与"slot id 最小"的 victim 与热点 S0 的
  关系均不确定（S0 位置随机）→ 两策略对 S0 的保留行为在多数 rep 相同。

---

## 10. 热点回访收益

- mt_pressure：revisit_S0 `prompt_processed` 两策略中位均为 4（全命中）→
  改善 **0%**；latency 改善 **1.7%**（噪声级）；
- 收益未达预注册阈值（≥5% latency 或 ≥10% processed）；
- **根因**：真实并发 slot 均匀分配 → default 也保留 S0 → **lru 的
  "价值排序"收益被场景方差稀释**（E3.3 synthetic integration 中 S0 固定
  在 slot0、default 必清它，高估了收益）。

---

## 11. wall time、吞吐和非压力成本

- pressure 变体：lru wall 5/5 不劣于 default（≤3%）；
- no_pressure 变体：**lru 3/3 不劣于**（wall bug 修正后；无压力成本）✓；
- 吞吐：随 latency 等价（无退化）。

---

## 12. 正确性、串扰、失败、truncation 和 active protection

- 全部 36 正式 replicate + 4 smoke：**0 失败、0 污染、eval 100%、
  task_success 100%（9527 召回）、truncations 0**；
- active protection：无 active sequence 被清理（E3.2 单测保障）；
- default 行为与 E3.2 first-eligible 一致。

---

## 13. branch/branch_pressure/non-unified/parallel=1 回归

- **branch/branch_pressure**：复用 E3.3 结果（3×2 baseline：正确性全过、
  wall ±1%、无 purge）——不重跑、不与主要收益合并 ✓；
- **non-unified smoke**（multi_session multi_turn no_pressure，parallel=2
  non-unified）：两策略行为一致（lru 惰性、无 purge）✓；
- **parallel=1 smoke**（unified p1）：无 idle 竞争、两策略一致 ✓。

---

## 14. 预注册门槛逐项判定

**A. 正确性（全过）**：
1. request failure=0 → **PASS**；2. evaluator 不低于 default → **PASS**；
3. task_success 不低于 default → **PASS**；4. contamination=0 → **PASS**；
5. truncation 不增 → **PASS**（0）；6. active 清理=0 → **PASS**；
7. clean 断言全过 → **PASS**（36+4）；8. default victim 与 E3.2 一致 →
   **PASS**；9. lifecycle stats 默认关 → **PASS**（E3.2 测试）；
10. branch/branch_pressure 无 A2 回归 → **PASS**（E3.3 复用）。

**B. OPTIONAL 收益（需至少一个家族）**：
- multi_turn：coverage 10/10 ✓、≥4/5 不劣 ✓（5/5）、**中位改善 0% ✗**
  （<5%/10%）→ **未达标**；
- long_life：**coverage 3/10 ✗**（<4/5）→ 未达标；
- **两个家族均未达到 OPTIONAL 收益门槛**。

**C. DEFAULT（需两个家族）**：未满足 B → 不适用。

**D. 证据强度**：每 workload 5 对 ✓；near_pressure 不计收益 ✓；
no-pressure 计入正确性/成本 ✓。

---

## 15. 是否允许 optional production exposure

**否（不提升为生产可选策略）**。

- `PROMOTE_A1A4_OPTIONAL` 需至少一个真实多会话家族达到收益门槛
  （coverage ≥4/5 + ≥4/5 不劣 + 中位改善 ≥5% 或 processed ≥10%）——
  **multi_turn coverage/不劣达标但改善 0%，long_life coverage 未达**；
- 收益未达阈值的根因是**真实并发 slot 均匀分配稀释了 lru 的价值排序
  收益**（E3.3 synthetic 高估）——这是真实场景的诚实结论；
- **保持 `lru` experimental/debug-only，不设默认、不推荐生产**。

---

## 16. 是否允许将 lru 设为默认

**否**。`PROMOTE_A1A4_DEFAULT` 需两个家族都满足收益门槛——均未达；
且收益证据（E3.3 synthetic）在真实多会话下被稀释。

---

## 17. 最终状态

**`KEEP_EXPERIMENTAL_A1A4`**

- **正确性、active protection、非回归全部通过**（36 正式 + 4 smoke）；
- **无负收益**：lru 全部 5/5+3/3 wall 不劣于 default，热点回访 5/5 不劣；
- **但收益证据不足**：真实多会话压力下（mt coverage 10/10）lru 与 default
  相当（改善 0%）；long_life coverage 3/10（near_pressure 特性）；
- **HOLD 触发条件不成立**：并发协议可靠（4 session、active=4）、压力可
  复现（mt 10/10）、热点可观测、无需修改策略解释结果；
- **REJECT 不成立**：无负收益、无回归、无越界；
- 保持默认 `default`、保留 `lru` feature flag；不调参、不扩大扫描。

---

## 18. 适用范围、不适用范围和回退

**适用（有限）**：
- unified 多会话竞争场景下**无负收益**（wall 5/5+3/3 不劣）；
- 热点集中在特定 slot（如按 slot 亲和分配会话）时 lru 可能保留热点
  （E3.2/E3.3 synthetic 证据）——需真实流量验证。

**不适用**：
- **真实均匀多会话分配**：lru 收益被稀释（≈0），与 default 相当；
- 单会话 workload（无压力机会，E3.3 已证）；
- non-unified、parallel=1、无压力场景；
- 不得宣称降显存、不得宣称所有 Agent workload 受益。

**回退**：默认 `default`；`lru` 仅显式开启；关闭即恢复原行为。

---

## 19. 未执行项目和残余风险

- 未执行：branch/branch_pressure 重跑（复用 E3.3）；ctx/RAM/轮数扫描
  （禁止）；精确 slot 归属诊断（需 E3.5 additive 只读接口，任务八 gap）；
- 残余风险：near_pressure 占比高（4B cell 复用特性）使 long_life 的
  真实压力机会难复现；slot 归属 gap 使"victim 是哪个 session"只能间接
  推断；收益 0% 可能受并发时序细节影响（4 线程启动顺序、server 调度），
  但方向一致（无负收益）已由 5/5 不劣覆盖。

---

## 20. 下一阶段精确输入

1. **不提升 lru**（保持 experimental）；若要重新评估，需：
   - 真实 Agent 流量的**会话-热点分布**数据（热点是否集中于特定 slot）；
   - additive 默认关闭的 per-session slot 归属诊断（补任务八 gap）；
2. **near_pressure 特性**：4B hybrid cell 位置复用导致池满但 decode 未
   进 purge 路径——记录为已知特性，供 E3.5 生命周期方向参考；
3. **A1/A4 处置**：冻结实现（`049872f59`），默认 `default`，`lru` 仅
   显式；`--lifecycle-stats` 保留运维/诊断；
4. 后续候选：生命周期管理方向（非 A2/A1-A4）或接受"当前机制在真实均匀
   多会话下收益有限"的结论，转向其他优化方向。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e34/`（manifest/summary/csv/paired_results +
  36 个 per-replicate JSON + smoke）；
- 新文件（根仓库）：`benchmark/scripts/e34_multi_session_workload_gate.py`、
  `e34_scan.sh`、`e34_summarize.py`、`e34_prereg_manifest.py`、
  `benchmark/tests/test_e34_gate.py`、
  `docs/E3_4_A1_A4_MULTI_SESSION_PRODUCTION_EXPOSURE_DECISION_GATE_REPORT.md`；
- llama.cpp：**本阶段无改动**（`049872f59` 保持，不建空提交）；
- 根仓库提交：`585c9fe`（test: validate unified idle lifecycle on
  multi-session workloads，6 文件 +1082 行）；提交后两仓库
  `git status --short` 均为空；
- 测试：根 pytest **132 passed**（含 test_e34_gate 9 例）；llama.cpp
  `test_unified_idle_lifecycle` 5/5 + `test_slot_routing` 10/10 + E1 5/5
  （复用基线，零改动）；
- 实际执行命令摘要：`e34_multi_session_workload_gate.py`（多会话矩阵）、
  `e34_prereg_manifest.py`、`e34_summarize.py`、`uv run pytest -q`、
  llama.cpp 测试复用。
