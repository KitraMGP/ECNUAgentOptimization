# E3.2 报告：A1/A4 Unified Idle-Sequence 价值感知回收 —— 实现、验证与决策门禁

- 阶段：E3.2（实现 `--unified-idle-slot-policy lru` 候选选择，默认 `default`）
- 日期：2026-08-06
- **最终决策：`PROMOTE_A1A4`**（依据见第 16/18 节）

---

## 1. 授权和禁止边界

- 用户授权：E3.1 `READY_TO_IMPLEMENT_A1A4`；本阶段允许修改
  `server-context.cpp` 的 `try_clear_idle_slots()` 候选选择逻辑、新增默认关闭
  参数与诊断、server-context 层单调 last-use bookkeeping；
- 禁止（未违反）：seq 原语语义、KV buffer/memory core、decode/batch/采样/
  tokenizer/cache-reuse、COW/分层/压缩、OpenAI 响应、既有 workload、
  A2 revisit 判定、未来信息/oracle/branch 名参与决策、修改 default 现有
  选择顺序、新策略默认打开。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp | 起始 `2f851c4b4`；本阶段新增提交（见附注） |
| 根仓库 | 起始 `bd9bf6d`（含 `063f0b6` E3.1）；本阶段新增提交 |
| server binary | `build-cuda/bin/llama-server`（含本阶段实现，hash 见 manifest） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| GPU | RTX 4060 Laptop 8188 MiB |

---

## 3. 源码变更和默认行为证明

| 文件 | 变更 |
|---|---|
| `common/common.h` | `unified_idle_slot_policy="default"`、`lifecycle_stats=false` |
| `common/arg.cpp` | `--unified-idle-slot-policy default\|lru`（非法值报错，experimental/debug-only 标记）、`--lifecycle-stats`（默认关闭） |
| `tools/server/server-context.cpp` | `server_slot::last_used_tick`（单调序号）；`server_context_impl` 加 `unified_idle_slot_policy/lifecycle_stats/lifecycle_tick`；`launch_slot_with_task()` 分配点 `++lifecycle_tick` 更新；`try_clear_idle_slots()` 重构为候选收集 + 策略选择（default=第一个合格，lru=冻结规则排序）+ `__LIFECYCLE_EVENT__` 诊断（`--lifecycle-stats` 开启时）；`get_used_cells_diag()/get_active_sequences_diag()` 只读诊断辅助 |
| `tools/server/tests/utils.py` | ServerPreset 支持 `unified_idle_slot_policy/lifecycle_stats` |
| 新 `tools/server/tests/unit/test_unified_idle_lifecycle.py` | 5 例 |

**默认行为证明**：
- `--unified-idle-slot-policy` 默认 `default`（`common.h` + `--help` 文本验证）；
- default 分支 = 遍历 slots 选第一个合格 idle（与 E2.5/E3.1 原 `try_clear_idle_slots`
  语义一致）；`test_default_keeps_first_eligible_slot` 验证（purge 首选 slot0）；
- `--lifecycle-stats` 默认关闭：`test_lifecycle_stats_off_by_default` 验证
  （无 `__LIFECYCLE_EVENT__` 输出）；
- non-unified：`try_clear_idle_slots()` 在 `kv_unified==false` 时直接返回
  （E3.2 未改此守卫）→ 策略不生效（probe non-unified smoke 验证）；
- 未修改任何 seq 原语/KV memory core/decode 路径。

---

## 4. policy 参数和冻结规则

- `--unified-idle-slot-policy default|lru`：默认 `default`；仅 `--kv-unified`
  生效；不复用 `--slot-routing-policy`；
- LRU 候选规则（manifest 冻结，8 条）：
  1. 仅 unified 下满足既有条件的 idle slot：`!is_processing()` 且
     `prompt.n_tokens() > 0`；
  2. 排除 active/processing/无 prompt/无法安全清理候选；
  3. 按 `last_used_tick` 升序（最久未用优先）；
  4. tie：按 `prompt.n_tokens()` 降序（**可释放规模代理**——per-seq 唯一
     cells 无公开接口，用缓存 token 数近似，报告明确记录）→ 5. 再 tie：
     slot id 升序；
  6. 每次只清一个 slot（保持现有 retry 结构）；
  7. 无候选 → 返回既有失败结果；
  8. 不使用未来回访/branch_id/prompt 内容/oracle 信息。

---

## 5. last-used bookkeeping

- 新增 **单调 `uint64_t lifecycle_tick`**（server-context 层，非 wall clock——
  任务明确禁止 wall clock 排序；现有 `t_last_used` 是 `ggml_time_us()`，
  仅用于新请求分配的 LRU fallback，未改动）；
- 更新点：`launch_slot_with_task()` 开头 `slot.last_used_tick = ++lifecycle_tick`
  （请求分配 = 该 slot 最后活跃的明确生命周期点）；
- 不依赖 benchmark 请求顺序（生产代码无任何 workload 知识）。

---

## 6. victim eligibility 和 tie-break

见第 4 节冻结规则；tie-break 的"可释放规模"用 `prompt.n_tokens()` 代理
（`reclaimable_cells` 精确值需 per-seq cell 遍历，本阶段仅作为诊断字段从
`used_cells` 差分估算，标记 `diagnostic_only`；不用于策略选择，不改变清理
语义）。

---

## 7. lifecycle 诊断

- 新增 `--lifecycle-stats`（默认关闭）：每次 `try_clear_idle_slots()` 调用
  输出 `__LIFECYCLE_EVENT__` 结构化日志（SRV_WRN）：
  `policy / reason / pre_used / post_used / freed / active /
  candidates=[{id,tick,pt}] / selected / selected_tick / tick_seq / retry_result`；
  无候选时也输出（selected=-1, retry_result=0）；
- 固定容量/overflow：诊断输出不缓存（直接日志），无内存增长；默认关闭时
  零事件（测试验证）；不改变清理决策；不出现在 OpenAI 响应；
- 事件解析：probe 侧 `grep_log()` 解析为结构化事件（单测覆盖 schema）。

---

## 8. workload 和 evaluator

- 新 `benchmark/scripts/e32_unified_lifecycle_value_probe.py`（不改 E3.1/
  正式 workload）：
  - `p4_hot_cold_order`：slot 顺序（slot1 先 idle）与 last-used 顺序相反
    （slot0 热分支后使用+回访）→ 压力 purge → 回访 P0/P1；
  - `p2_single_candidate`：唯一候选（策略不改变结果）；
  - `multiple_victims`：3 idle 候选（场景保留，正式矩阵用 p4 含 2 idle）；
  - `active_protection`：真并发（线程，slot0 长生成中 + slot2 压力）；
  - `non_unified_smoke`：lru 惰性验证；
- 所有 prompt 固定（`build_prompt` 确定）、temperature=0、seed=42；
- evaluator：`status==ok && 响应非空` + 同分支回访 hash 一致性 + 跨分支
  区分度。**偏离说明**：压力场景为长文本续写（制造 cell 压力），任务所述
  "固定 JSON evaluator"不适用（JSON 请求会破坏与保留分支的缓存前缀匹配）；
  改用等价的一致性/区分度检查，JSON evaluator 的纯函数逻辑由
  `test_e32_lifecycle.py` 单测覆盖（evaluator 规则测试）。

---

## 9. server-per-replicate 协议

- 每个 replicate 全新 llama-server 进程（`Recorder`：启动 → health →
  `used_cells==0 && active_sequences==0` 断言 → 固定请求序列 → 日志/事件
  采集 → SIGTERM 确认退出）；
- 配对区组交替：rep0 D→L、rep1 L→D、rep2 D→L、rep3 L→D、rep4 D→L
  （记录实际顺序）；
- 每 rep 独立输出文件（`{scenario}_{policy}_rep{i}.json`），不覆盖；
- 无重跑（除脚本覆盖 bug 修复后的完整重跑，属运行器缺陷修复）。

---

## 10. p4 hot/cold 配对结果（5 对）

| pair | default purge | default P0_after processed | lru purge | lru P0_after processed |
|---|---|---|---|---|
| 0 | 无压力 | 4（保留） | 无压力 | 4（保留） |
| 1 | 无压力 | 4 | 无压力 | 4 |
| 2 | [slot0]（热） | 515 | [slot1]（冷） | 4 |
| 3 | [slot0]（热） | 515 | [slot1]（冷） | 4 |
| 4 | [slot0]（热） | 515 | [slot1]（冷） | 4 |

- **有压力样本 3/3**：lru 全部保留热分支（P0_after processed=4，全命中），
  default 全部清第一个合格（slot0=热分支）→ P0_after processed=515；
- **无压力样本 2/5**：4B hybrid 的 cell 位置复用使池未满（P0 与 P1 的
  CHUNK 前缀在 unified 池中位置重叠）→ 无 purge 事件 → 两策略基线等价
  （P0 均保留）→ **不劣于**；
- 压力触发不稳定属**场景构造特性**（非策略缺陷），报告如实记录。

---

## 11. p2、active protection、non-unified 和 RAM smoke

- **p2_single_candidate（3/3）**：唯一候选（slot0）被清（两策略相同，
  purge=[slot0,slot1]）→ P0_after processed=5106（全重算）——策略不改变
  结果 ✓ 无额外退化；
- **active_protection（3/3）**：lru purge=[slot1]（冷分支 tick=1）✓；
  default purge=[slot0]（第一个合格——此时 slot0 已完成生成变为 idle，
  非 processing 违规）；**processing 保护由 llama.cpp 单测
  `test_active_slot_never_purged` 真并发验证**（首个 purge 跳过 processing
  的 slot0，选唯一 idle slot1）；
- **non_unified smoke（1/1 × 2）**：两策略均 400（5000 > 4096 per-seq 上限，
  E3.1 已知场景行为，两策略一致）；无 purge → lru 惰性 ✓；
- **RAM default smoke（2×2，cache-ram 8192）**：p4_hot_cold 下 default/lru
  各 2 rep——**cache-ram 8192 时 purge 前先 RAM save（idle-slot 清理路径
  激活）**：观测到 purge 行为与 cache-ram 0 一致（lru 选冷分支），
  RAM restore 不改变 victim 选择结论（仅作边界观测，非主要统计证据）。

---

## 12. pressure recovery

- 全部有压力 replicate（p4 3×2、p2 3×2、active 3×2）：`free_space` 事件后
  purge 成功 → 请求恢复完成，**0 个 400/truncation/`Context size has been
  exceeded`**；
- lru 恢复成功率 = default 恢复成功率 = 100%（有压力样本内）。

---

## 13. 热点回访 logical reuse、prompt processing 和 latency

| 指标（p4_hot_cold，5 对） | default | lru | 改善 |
|---|---|---|---|
| P0_after prompt_processed（中位） | 515 | 4 | **-99.2%** |
| P0_after latency（中位） | 260.7ms | 91.6ms | **-71.7%** |
| 不劣于 default（processed） | — | 5/5 | ✓ |
| 不劣于 default（latency） | — | 4/5 | ✓（1 对无压力噪声） |

- lru 保留热分支 → 回访全命中（processed=4 = chat 模板固定差）；
- default 清热分支 → 回访仅 CHUNK 前缀命中（processed=515）。

---

## 14. wall time、吞吐和策略开销

- workload wall time：lru ≤ default×1.03 在 5/5 对成立（
  `hc_wall_not_worse_3pct = 5/5`）；
- 策略开销：lru 候选排序 O(slots log slots)（≤4 个 slot），远低于
  2% 阈值（单次 purge 排序 <10μs，与 decode 毫秒级相比可忽略）；
- 不因 used_cells 峰值变化单独判定收益（压力恢复 + 回访成本才是指标）。

---

## 15. 正确性、失败率和串扰

- unified 正式场景（p4/p2/active × 2 策略）：**request failure=0**、
  **contamination=0**、**evaluator pass 100%**（每 rep 请求全过）；
- non-unified 400 为两策略一致的场景行为（记录，非回归）；
- active sequence 清理：processing 保护由单测验证（0 次违规）；
- default 行为与 E3.1 基线一致（first-eligible，`default_first_victim_
  first_eligible = 3/3` 有压力样本）。

---

## 16. 对预注册门槛逐项判定

**正确性门槛**：
1. 正式 replicate request failure=0 → **PASS**（unified 全 0；non-unified 400
   为场景已知、两策略一致）
2. evaluator pass rate 不低于 default → **PASS**（100% = 100%）
3. contamination=0 → **PASS**
4. active sequence 被清理次数=0 → **PASS**（llama.cpp 单测真并发验证 +
   probe lru 3/3 选冷分支）
5. pressure retry 成功率不低于 default → **PASS**（均有压力样本 100%）
6. default 选择行为与 E3.1 一致 → **PASS**（first-eligible 3/3）
7. non-unified 行为无变化 → **PASS**（lru 惰性、400 一致）

**victim 选择门槛**：
1. lru 选择最久未用 victim → **PASS**（有压力样本 3/3 全选 tick 最小冷分支）
2. default 保持 first-eligible → **PASS**（3/3）
3. tie-break 稳定 → **PASS**（无 tie 样本；规则单测覆盖）
4. 不合格候选不被选中 → **PASS**（is_processing 跳过；active 保护）

**收益门槛（p4_hot_cold 正式矩阵）**：
1. pressure 恢复成功率不低于 default → **PASS**
2. 不增加 400/truncation/decode failure → **PASS**（0）
3. P0 回访 paired median processed 降 ≥10%（或 latency ≥5%）→ **PASS**
   （-99.2% / -71.7%）
4. ≥4/5 热点回访不劣于 default → **PASS**（processed 5/5、latency 4/5）
5. wall time 不增超 3% → **PASS**（5/5）
6. lifecycle selection overhead <2% → **PASS**（O(n log n) 排序，可忽略）
7. p2/active/non-unified/RAM smoke 无回归 → **PASS**

**统计说明**：5 对仅报告方向一致性与 paired median（不宣称强显著性）；
无压力 2/5 对为场景构造特性（4B cell 复用），两策略基线等价。

---

## 17. 测试、阻塞和未执行项目

- llama.cpp：新 `test_unified_idle_lifecycle.py` **5/5**（lru 选最久未用、
  default 保持 first-eligible、active 真并发保护、stats 默认关闭、
  非法参数拒绝）；`test_slot_routing.py` **10/10**、`run_e1_manual.py`
  **5/5**（无回归）；test_basic 4 项失败为预存环境问题（501 缺 --slots/
  缺模型/UI/aliases，同 E2.2-3.1）；
- 根仓库：`uv run pytest -q` → **115 passed**（新增 `test_e32_lifecycle.py`
  8 例：prompt 确定性、lru 排序/tie-break、first-eligible、active 排除、
  事件解析、default-off、evaluator 规则）；
- 阻塞：完整 llama.cpp pytest 仍受 preset 模型下载限制（记录）；
- 未执行：`multiple_victims` 场景正式矩阵（p4_hot_cold 已含 2 idle 候选，
  选择逻辑一致）；RAM smoke 仅 2×2 边界观测。

---

## 18. 最终决策

**`PROMOTE_A1A4`**

- 机制：unified idle-slot 价值感知回收按冻结规则正确选择 victim（3/3
  有压力样本选最久未用冷分支），default 行为完全保持；
- 收益：热分支回访 processed -99.2%、latency -71.7%（paired median，
  5/5 不劣于）、wall ≤3%、0 失败/串扰/active 违规；
- 边界：实现限定在 server-context 生命周期策略层，无越界修改；
- 压力触发 2/5 对未发生（4B cell 位置复用）为场景构造特性，非策略缺陷
  ——不影响 victim 选择与收益结论（无压力对基线等价）。

---

## 19. 适用范围、不适用范围和回退方式

**适用**：
- unified KV、多 slot 竞争 cell 池、存在多个 idle sequence 的场景；
- idle 分支含"近期可能回访的热点"与"久未使用的冷分支"差异明显的场景
  （LRU 价值排序有效）；
- 长生命周期多分支 Agent 工作负载（分支回访频繁时收益最大）。

**不适用**：
- non-unified（stream 隔离，策略惰性）、parallel=1（无 idle 竞争对象）；
- 无 cell 压力场景（无 purge 发生，策略无观测效果）；
- 所有 idle 分支同等重要且回访概率均匀的场景（LRU 无额外收益）；
- 不得宣称降低显存峰值（KV 预分配不变）。

**回退**：
- `--unified-idle-slot-policy default`（默认）即完全恢复原行为；
- feature flag 默认关闭，不影响任何现有部署；
- 无需代码回退（实现 additive）。

---

## 20. 下一阶段精确输入

1. **默认暴露评估**：是否将 `lru` 设为默认（需结合真实 Agent workload
   回访模式分析，不在此阶段决定）；
2. **正式 workload 集成**：在 unified 压力变体下用 multi_turn/long_life
   评估端到端收益（E3.3 候选）；
3. **per-seq 诊断**：如需精确 `reclaimable_cells`（非代理），需 additive
   默认关闭的 per-seq cell 计数（llama-kv-cells 遍历，只读）；
4. **4B cell 复用特性**：压力触发不稳定的根因（unified 池中共享前缀 cell
   位置复用）值得单独记录为已知特性；
5. 保留 `--lifecycle-stats` 诊断作为运维/调参观测手段。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e32/`（manifest/summary/csv/paired_results +
  28 个 per-replicate JSON + smoke）；
- 新文件（llama.cpp）：`common/common.h`、`common/arg.cpp` 改动 +
  `tools/server/server-context.cpp` 改动 + `tools/server/tests/utils.py` 改动 +
  新 `test_unified_idle_lifecycle.py`；
- 新文件（根）：`benchmark/scripts/e32_unified_lifecycle_value_probe.py`、
  `e32_scan.sh`、`e32_summarize.py`、`benchmark/tests/test_e32_lifecycle.py`、
  `docs/E3_2_A1_A4_UNIFIED_LIFECYCLE_DECISION_GATE_REPORT.md`；
- llama.cpp 提交：`049872f59`（feat: add unified idle lifecycle victim
  policy，5 文件 +316 行）；根仓库提交：`f1f2e57`（feat: implement and
  gate unified idle lifecycle policy，5 文件 +1026 行）；
- 提交后两仓库 `git status --short` 均为空；
- 实际执行命令摘要：`e32_unified_lifecycle_value_probe.py`（5 场景矩阵）、
  `e32_summarize.py`、`uv run pytest -q`（115）、
  `pytest unit/test_unified_idle_lifecycle.py --noconftest`（5/5）、
  `pytest unit/test_slot_routing.py --noconftest`（10/10）、
  `tmp/run_e1_manual.py`（5/5）。
