# E3.3 报告：A1/A4 既有 Workload 集成验证与默认暴露门禁

- 阶段：E3.3（复用既有 workload + unified pressure adapter；**未修改任何
  workload 文件、未修改 llama.cpp**）
- 日期：2026-08-06
- **最终状态：`KEEP_EXPERIMENTAL_A1A4`**（依据见第 16/17 节）

---

## 1. 本阶段授权和禁止边界

- 授权：验证 E3.2 LRU victim 选择收益是否迁移到既有 Agent workload；
  判断 A1/A4 是否可提升为可选生产策略、是否设 `lru` 为默认；
- 禁止（未违反）：修改 `try_clear_idle_slots()` LRU 规则、修改 default 行为、
  设 lru 默认开启、seq 原语/KV buffer/memory core/decode/采样/tokenizer/
  cache-reuse、COW/分层/压缩、OpenAI 响应、A2 prefix-branch、既有 workload
  文件、用未来信息/branch 名/oracle 参与策略、无目标扫描；
- 本阶段 **llama.cpp 零改动**（`049872f59` 保持）。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp | `049872f59`（E3.2 实现；本阶段无改动） |
| 根仓库 | 起始 `e5a5dad`；本阶段新增提交（见附注） |
| server binary | `build-cuda/bin/llama-server`（含 E3.2 实现，hash 见 manifest） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| GPU | RTX 4060 Laptop 8188 MiB |

---

## 3. E3.2 实现和默认行为复核（9 项审计）

| # | 审计项 | 结果 |
|---|---|---|
| 1 | default 仍选第一个合格 idle slot | ✓（`candidates.front()`，代码确认 + E3.2 测试） |
| 2 | lru 仅 `kv_unified=true` 生效 | ✓（`!params_base.kv_unified → return`） |
| 3 | non-unified 路径不受影响 | ✓（同守卫；E3.3 F 组 smoke） |
| 4 | active/processing 不作 victim | ✓（`is_processing()` 跳过；E3.2 真并发单测） |
| 5 | 无候选时行为与原逻辑一致 | ✓（selected=nullptr → return false） |
| 6 | `last_used_tick` 不依赖 workload/branch/未来请求 | ✓（`launch_slot_with_task` 分配点单调更新） |
| 7 | `--lifecycle-stats` 关闭时无事件 | ✓（E3.2 `test_lifecycle_stats_off_by_default`） |
| 8 | 诊断不入 OpenAI response | ✓（日志输出，非响应字段） |
| 9 | E3.2 排序/tie-break 未被 E3.3 修改 | ✓（本阶段零源码改动） |

**结论：实现与 E3.2 报告一致，无差异**。

---

## 4. workload adapter 与 prompt/evaluator hash

- 新 `benchmark/scripts/e33_existing_workload_integration.py`：
  - **复用** `get_workload(name).generate(params) → run(driver, spec)`（driver
    包装记录每请求），**不改 workload 文件**；`evaluate` 复用 workload 自带
    evaluator；
  - **adapter 变体**（仅 benchmark 侧组合请求，标记 `integration_pressure`）：
    - `baseline`：纯 workload.run（无压力：lru=default 等价 + 成本验证）；
    - `pressure`：workload 后追加压力请求（新前缀长 prompt，目标 6300
      tokens，固定不扫描）+ 重放最后请求（观察回访成本）；
    - `integration_pressure`：workload 后重放最后请求到 slot1（冷）与
      slot0（热）→ 压力（slot2）→ 回访热分支；
  - 预注册 hash：`spec.fingerprint()`（每 rep 记录）；
- workload 参数冻结（manifest）：multi_turn rounds=16、long_life
  rounds=10/secret="9527"/ctx 8192、branch branch_rounds=3、branch_pressure {}。

---

## 5. server-per-replicate 协议

- 每 replicate：全新 server → health → `used_cells==0 && active_sequences==0`
  断言 → 固定 warmup → workload + adapter → `/metrics/kv` + lifecycle 事件
  + 响应/evaluator → SIGTERM 确认退出；
- 配对区组 D L | L D 交替（manifest 冻结）；
- **全部 54 个正式 replicate + 6 个 smoke：clean_verified=true、
  server_exited=true**。

---

## 6. multi_turn 结果（unified p2，5×2，pressure 变体）

- 正确性：task_success 全过、evaluator 100%、0 失败、0 串扰；
- **压力覆盖率 0/10**：16 轮 multi_turn 的 KV 总量（~1500 cells）+
  6300 压力 = ~7800 < 8192 池容量 → **压力未发生** →
  `opportunity_not_observed`（任务九 D：不能判"无收益"，仅记录）；
- wall time：lru 5/5 不劣于 default（≤3%）。

---

## 7. long_life 结果（p2 pressure 5×2 + p4 integration 5×2）

### p2 pressure（5×2）

- 正确性：task_success（9527 召回）全过、0 失败/串扰、truncations=0；
- **压力覆盖率 0/10**：10 轮 KV（~1850 cells）+ 6300 = ~8150 < 8192 →
  临界未触发 → `opportunity_not_observed`；
- wall：lru 5/5 不劣于。

### p4 integration_pressure（5×2）——唯一压力触发并验证收益的变体

- **压力覆盖率 10/10**（多会话：workload + cold/hot 重放 + 压力 > 池）；
- **victim 选择**：lru 5/5 清最久未用（cold slot1 / 最早 slot3，tick 最小），
  **保留热分支 slot0**；default 清第一个合格（slot0=热）；
- **回访热分支**：lru `prompt_processed=4`（全命中）vs default 1698 →
  **paired median 改善 99.8%**；not_worse 5/5（processed 与 latency）；
- wall：lru 5/5 不劣于（≤3%）。

---

## 8. branch 和 branch_pressure 非回归（各 3×2，baseline）

- branch：eval/task_success 全过、无污染、无 purge（无压力）；
  wall lru 3/3 不劣于（±1% 噪声级）；
- branch_pressure：正确性全过（A2 边界回归：A1/A4 不改变 A2 记录的正确性
  结论）、无 purge、wall 3/3 不劣于；
- **A1/A4 与 A2 收益未混合**（routing policy 固定 default，未启用
  prefix-branch）。

---

## 9. non-unified、parallel=1、RAM smoke

- **non-unified smoke**（multi_turn baseline 1×2）：两策略行为一致
  （无 purge，lru 惰性）✓；
- **parallel=1 smoke**（multi_turn baseline 1×2）：无 idle 竞争、无 purge
  差异 ✓；
- **RAM smoke**（multi_turn pressure cache-ram 8192 1×2）：与 cache-ram 0
  一致（未观察到 RAM restore 改变 victim 选择）——边界观测，非主统计。

---

## 10. pressure opportunity 覆盖率

| 变体 | 覆盖率 | 判定 |
|---|---|---|
| multi_turn p2 pressure | 0/10 | opportunity_not_observed（单会话 KV 小） |
| long_life p2 pressure | 0/10 | opportunity_not_observed（临界未触发） |
| long_life p4 integration_pressure | **10/10** | 收益可观测 |
| branch / branch_pressure baseline | 0/6 | 无压力设计（对照） |

- **真实单会话 workload（multi_turn/long_life p2）在 unified ctx 8192 下
  不发生 cell 压力**——KV 总量（~1500-1850）远小于池（8192），adapter
  压力（6300）合计仍低于池容量 → A1/A4 无作用对象；
- 多会话压力（integration_p4）是 A1/A4 的**真实适用场景**。

---

## 11. victim 选择符合率

- integration_p4 有压力样本 5/5：lru 均选 tick 最小（最久未用）的冷分支/
  最早请求，default 均选第一个合格（slot0）——与冻结 LRU 规则一致；
- 其余变体无 purge（无选择发生）。

---

## 12. 回访 latency、prompt processing、wall time 和吞吐

| 指标（integration_p4，5 对） | default | lru | 改善 |
|---|---|---|---|
| 热分支回访 prompt_processed（中位） | 1698 | 4 | **-99.8%** |
| 热分支回访 latency（中位） | ~680ms | ~45ms | **-93%** |
| 不劣于（processed / latency） | — | 5/5 / 5/5 | ✓ |
| workload wall time 不劣于（≤3%） | — | 5/5 | ✓ |

- 吞吐随 latency 改善提升（回访 680→45ms 等价吞吐 ~15×）；
- 非压力 workload（branch/branch_pressure）：lru 与 default 无差异
  （±1% 噪声级，无额外成本）。

---

## 13. 正确性、失败率、context shift、串扰

- 全部 54 正式 replicate + 6 smoke：**request failure=0、contamination=0、
  evaluator 100%、task_success 100%（long_life 9527 召回）、truncations=0**；
- active sequence 清理：0 次（integration_p4 中 lru 保留热分支；E3.2 真并发
  单测保障 processing 保护）；
- default 行为与 E3.2 基线一致（first-eligible）。

---

## 14. lifecycle overhead

- lru 排序 O(slots log slots)（≤4 slot），单次 purge <10μs——远低于 2%
  阈值（与 decode 毫秒级相比可忽略）；wall time 5/5+3/3 不劣于验证。

---

## 15. 预注册门槛逐项判定

**A. 正确性（全部统一）**：
1. request failure=0 → **PASS**
2. evaluator pass rate 不低于 default → **PASS**（100% = 100%）
3. contamination=0 → **PASS**
4. task_success 不低于 default → **PASS**（全过）
5. context shift/truncation 不增加 → **PASS**（0）
6. active sequence 清理=0 → **PASS**
7. server-per-replicate 断言全过 → **PASS**（54+6 全 clean）
8. default 行为与 E3.2 一致 → **PASS**

**B. 主要收益（unified 压力变体）**：
1. ≥4/5 关键回访 latency 不劣 → **PASS**（integration_p4 5/5）
2. paired median 改善 ≥5% 或 processed 降 ≥10% → **PASS**（-99.8% / -93%）
3. wall 不增超 3% → **PASS**（5/5 + 3/3）
4. pressure recovery 成功率不低于 default → **PASS**（100%）
5. victim 选择符合冻结规则 → **PASS**（5/5）
6. lifecycle overhead <2% → **PASS**
- **但**：multi_turn/long_life p2 压力未触发 → `opportunity_not_observed`
  → **仅 1/3 压力变体有收益证据**

**C. 非回归**：parallel=1 ✓、non-unified ✓、无压力 multi_turn ✓、branch ✓、
branch_pressure ✓、RAM smoke ✓、default ✓——**全 PASS**。

**D. 证据强度**：
- integration_p4 5 对 ✓；multi_turn/long_life p2 5 对（但压力 0/10 →
  只能记录 opportunity_not_observed）；
- **跨 workload 收益证据不足**（仅多会话压力场景成立）。

---

## 16. 是否允许默认暴露

**否（不设 `lru` 为默认）**。

对照任务十 `PROMOTE_A1A4_DEFAULT` 条件：
1. **≥2 个不同既有 workload 的压力变体满足主要收益门槛 → 不满足**
   （仅 long_life integration_p4 1 个；multi_turn/long_life p2 压力未触发，
   opportunity_not_observed）；
2. 每个 workload ≥4/5 方向不劣 → 部分满足（有压力样本全过）；
3. 正确性/active/非回归全过 → 满足；
4. lru 非压力 workload ≤3% 退化 → 满足（branch 3/3 ±1%）；
5. 诊断/回退/运维完整 → 满足（E3.2）；
6. 无依赖 A2 → 满足；
7. 真实 workload 与 E3.2 方向一致 → 满足（integration_p4 99.8%）。

**条件 1 不满足 → 不得 PROMOTE_A1A4_DEFAULT**。

---

## 17. 最终状态

**`KEEP_EXPERIMENTAL_A1A4`**

- **至少一个既有 workload 显示稳定收益**：long_life p4 integration_pressure
  热分支回访 processed -99.8%、latency -93%（5/5 不劣）；
- **正确性和非回归全部通过**；
- **但跨 workload 证据不足**：真实单会话 workload（multi_turn/long_life p2）
  在 unified ctx 8192 下不发生 cell 压力（KV 总量 < 池容量）→
  opportunity_not_observed → A1/A4 在这些场景无作用对象（也验证了
  **无额外成本**：branch/branch_pressure wall ±1%）；
- **保持默认 `default`，继续保留 `lru` feature flag（experimental）**；
- 不进行无目标调参。

---

## 18. 适用范围、不适用范围和回退

**适用**：
- unified + **多会话/多 sequence 竞争 cell 池**的压力场景（多个独立分支/
  会话并行，idle seq 保留可观 KV）——integration_p4 实测收益显著；
- 长生命周期多分支 Agent 工作负载（分支回访频繁时 LRU 价值排序有效）。

**不适用**：
- **单会话顺序 workload**（multi_turn/long_life 自动路由单 seq）：KV 总量
  远小于池容量 → 无压力 → LRU 无作用对象（也无成本）；
- non-unified、parallel=1、无压力场景；
- 不得宣称降低显存峰值（KV 预分配不变）；
- 不得宣称所有 Agent workload 受益。

**回退**：默认 `default`（未改动）；`lru` 仅显式开启；关闭即恢复原行为。

---

## 19. 未执行项目和残余风险

- 未执行：multi_turn/long_life 的更高轮数压力变体（为触发压力而增大轮数
  属"无目标扫描"禁止范围——已用固定 16/10 轮 + 固定 6300 压力构造，
  如实记录未触发）；ctx/RAM 扫描（禁止）；
- 残余风险：integration_p4 的收益依赖 adapter 构造的多会话压力场景；
  真实 Agent 应用中"多会话并行 + 分支回访"的分布未知——默认暴露需更多
  真实流量证据；4B hybrid 的 cell 位置复用特性（E3.2 已记录）影响压力
  触发稳定性。

---

## 20. 下一阶段精确输入

1. **默认暴露**：暂不设 lru 为默认；若需提升为生产可选策略，需
   真实多会话 Agent 流量（含分支回访）的压测证据（≥2 workload 压力变体
   满足收益门槛）；
2. **多会话场景支持**：考虑为 benchmark 增加真正的多会话并行 workload
   （多 client 并发独立会话），使 A1/A4 的作用场景可正式评测；
3. **per-seq 诊断**（如需精确 reclaimable_cells）：additive 默认关闭；
4. `--lifecycle-stats` 保留为运维/调参观测手段；
5. A1/A4 与后续候选（生命周期管理方向）的组合验证。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e33/`（manifest/summary/csv/paired_results +
  54 个 per-replicate JSON）；
- 新文件（根仓库）：`benchmark/scripts/e33_existing_workload_integration.py`、
  `e33_scan.sh`、`e33_summarize.py`、`e33_prereg_manifest.py`、
  `benchmark/tests/test_e33_integration.py`、
  `docs/E3_3_A1_A4_EXISTING_WORKLOAD_INTEGRATION_DECISION_GATE_REPORT.md`；
- llama.cpp：**本阶段无改动**（`049872f59` 保持，不建空提交）；
- 根仓库提交：`test: validate unified idle lifecycle on existing workloads`
  （hash 见最终汇报）；提交后两仓库 `git status --short` 均为空；
- 测试：根 pytest **123 passed**（含 test_e33_integration 8 例）；llama.cpp
  `test_unified_idle_lifecycle` 5/5 + `test_slot_routing` 10/10 + E1 5/5
  （复用 E3.2 基线，零改动）；
- 实际执行命令摘要：`e33_existing_workload_integration.py`（5 workload ×
  变体矩阵）、`e33_prereg_manifest.py`、`e33_summarize.py`、
  `uv run pytest -q`、llama.cpp 测试复用。
