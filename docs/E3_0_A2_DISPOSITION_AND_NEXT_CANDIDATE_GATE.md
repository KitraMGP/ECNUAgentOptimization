# E3.0 报告：A2 REJECT 处置闭环、路线图复核与下一候选可行性门禁

- 阶段：E3.0（不实现任何新生产策略；仅决策记录、路线图复核、候选门禁）
- 日期：2026-08-06
- **最终状态：`BLOCKED_NEEDS_SELECTION`**（依据见第 7/13 节）

---

## 1. A2 最终处置

**决策记录（正式冻结）**：

| 项 | 值 |
|---|---|
| A2 状态 | **REJECT**（E2.5 决策门禁，`docs/E2_5_A2_DECISION_GATE_REPORT.md` 第 21 节） |
| feature flag | `--slot-routing-policy prefix-branch`：**保留、默认关闭、debug-only/experimental** |
| 默认部署 | **不建议**（4B 长分支 workload 下 revisit p50 +105%、prompt_processed +99.8%） |
| 后续投入 | **冻结**：不再追加同类 benchmark、参数扫描、补 probe 或修复 revisit 判定 |
| 测试保留 | 现有 `test_slot_routing.py`（10 例）保留——防止 default/unified 正确性回归 |
| 实现保留 | **不删除**——仍提供短 prompt（<100 tokens）分支保留的边界证据与诊断价值（E2.3/2.4 TinyLlama） |
| 重新开启条件 | ① 用户单独授权新候选版本（如 A2b）；② 预先批准修改 slot routing 层；③ 用**新候选 ID**（A2b），不覆盖 A2 的 REJECT 历史 |

**措辞边界（严格区分）**：
- 成立："当前 A2 实现未通过正式门禁（E2.5，4B 正式条件性能 gate 0/5）"；
- 不成立："所有 prefix-aware routing 思路都不可能有效"——A2 的失败原因是**性能和适用性**（长 prompt 分支推进+回访混合序列的 revisit 判定失效），**不是输出污染或正确性失败**（E2.5 实测 failure=0、contamination=0、evaluator pass 1.0）。

---

## 2. 两仓库 HEAD 和工作区状态

| 项 | 值 |
|---|---|
| 根仓库 HEAD | `30d0781`（含 `0ac0e14` E2.5；`0ac0e14` 为本 HEAD 祖先 ✓） |
| llama.cpp HEAD | `2f851c4b4`（`2f851c4b4` 为本 HEAD 祖先 ✓） |
| 工作区 | 两仓库均干净（`git status --short` 空），`git diff --check` 通过 |
| binary | `build-cuda/bin/llama-server` 仍对应 llama.cpp `2f851c4b4`（hash 记录于 E2.5 manifest，未重建） |
| 用户修改 | 无未提交用户修改；未回退/覆盖任何文件 |

**默认参数确认**：
- `--slot-routing-policy` 默认值 = `default`（`llama.cpp/common/common.h:679`），
  help 文本明确 "default (existing LCP-similarity + LRU, unchanged behavior)"；
- prefix-branch **未默认启用**（显式 `--slot-routing-policy prefix-branch` 才生效）；
- `test_slot_routing.py` 10 例含 default 路径断言，持续防护。

---

## 3. E2.5 证据复核

- `benchmark/results/e25/manifest.json`：llama_commit=`2f851c4b4`、
  root_commit=`9b5e351`（E2.5 执行时 HEAD），与报告第 2 节一致；12 个结果文件
  sha256 已记录；
- `summary.json` gates：off 与 pressure 均为 gate1-4（性能）=False、
  gate5-7（正确性）=True——与 E2.5 报告第 20 节逐项判定一致；
- `paired_results.csv`：5/5 对 prefix-branch revisit p50/wall/processed 更差
  （+105%/+21.5%/+99.8%）——与报告第 9-11 节一致；
- **复核结论**：E2.5 REJECT 判定依据（性能门槛未达 + 需越界修复）在
  结果文件中可完整复现，无证据与报告冲突。

---

## 4. A2 保留/禁用边界

| 边界 | 内容 |
|---|---|
| 禁用（默认） | prefix-branch 保持默认关闭；任何正式 baseline/报告不得在未显式开启时声称使用了 A2 |
| 保留（代码） | `0217843bd`（实现）+ `2f851c4b4`（unified 修复）提交保留，不 squash/回退/删除 |
| 保留（证据） | 短 prompt 分支保留（TinyLlama bc2 cached 111 vs 56、延迟 -25%）、bc4 有界回退、unified 修复——作为边界证据归档 |
| 防护（测试） | test_slot_routing（含 default 行为、unified、prefix-branch 路径）持续运行，防正确性回归 |
| 禁止 | 不调参、不补 probe、不修 revisit 判定、不改 prefix-branch 语义、不改写 REJECT 为 HOLD/PROMOTE |

---

## 5. 路线图搜索结果

搜索范围：`docs/`、`benchmark/`、`README.md`（rg：A3/E3/candidate/roadmap/
lifecycle/生命周期/淘汰/回收/compression/分层）。

**A. 总实施计划**（`docs/benchmark_implementation_plan.md` Part 3.3）——三阶段优化验证：

| 计划实验 | 优化 | 实际对应 | 批准状态 |
|---|---|---|---|
| E1 | KV 生命周期管理（`--enable-kv-lifecycle`） | 实际 E1 重定义为可观测性（已完成）；生命周期候选实际在 E2 阶段探索（A1-A4） | A2 REJECT；A1/A4 需另行立项 |
| E2 | 分支 CoW（`--enable-cow`） | 未实施 | **未批准**（历阶段禁止列表均有"不实现 COW"） |
| E3 | Context 压缩（compression OFF vs ON） | 未实施 | **未批准**（历阶段禁止列表均有"不实现 Context Compression"） |

**B. E2 候选矩阵**（`docs/E2_FEASIBILITY_AND_EVALUATION_GATE_REPORT.md` 第 8 节）：

| 候选 | 方向 | 判定 | 备注 |
|---|---|---|---|
| A1 | 空闲 slot 价值感知淘汰 | E2.0.5 否定（parallel=1 无对象；仅 unified 有意义） | **"需在 unified 场景另行立项"** |
| A2 | prefix-aware 路由（分支保留） | 已实现；**E2.5 REJECT** | 本阶段处置对象 |
| A3 | request 尾部回收 | E2.0.5 方向性否定（"回收必然降低潜在命中"） | 未立项 |
| A4 | unified 容量压力下 idle-sequence 回收 | E2.0.5 否定（仅 unified） | 与 A1 合并待立项 |

**C. E2.1 候选**（`docs/E2_DESIGN_AND_BASELINE_REPORT.md` 第 12 节）：

| 候选 | 方向 | 判定 |
|---|---|---|
| B | 按需 KV buffer 分配/收缩 | "在 A 验证后再评估"；需动 memory 核心 → E3.0 禁止修改 KV memory |
| C | CPU/磁盘分层 | "无基线数据前不实施"；属分层 KV → E3.0 禁止 |

**D. E2.5 建议**（`docs/E2_5_A2_DECISION_GATE_REPORT.md` 第 23 节）：
- A2 处置（本阶段执行）；A2b（revisit 修复，需用户单独授权 + 批准改 slot
  routing）；RAM prompt cache 长 state 失效调查（超出策略范围）；
  "转向其他已批准候选（如生命周期管理方向）"——但生命周期方向候选
  （A1/A3/A4）均已否定/待立项。

**E. 其他**：README 无独立 roadmap/issue；无已批准 A3/E3 策略。

---

## 6. 候选比较表

| 候选 | 原始文档 | 目标问题 | 已批准 | 修改边界 | 与 A2 依赖 | 正确性风险 | 可观测性 | 现有 workload 可评 | 最小实现/回退 |
|---|---|---|---|---|---|---|---|---|---|
| **A1/A4（unified 淘汰）** | E2_FEASIBILITY 8.1 | unified 下 KV 压力、idle seq 占 cell | ❌ 需另行立项 | server-context 策略层（`try_clear_idle_slots` 价值化） | 无 | 中（误淘汰活跃 seq） | 足（/metrics/kv + /routing/events） | 部分（需 unified p≥2 变体） | feature flag + 回退 try_clear_idle_slots 原行为 |
| **A2b（revisit 判定修复）** | E2.5 23.2 | A2 长 prompt 分支推进+回访失效 | ❌ 需用户单独授权 | slot routing 层（E2.1 代码） | A2 延续 | 低-中 | 足 | 是（branch_pressure） | flag + 重跑 E2.5 矩阵 |
| A3（尾部回收） | E2_FEASIBILITY 8.1 | 请求结束尾部无效 cell | ❌ 方向性否定 | server-context（seq_rm p0,-1 已有） | 无 | 低 | 足 | 是 | flag + 默认关闭 |
| B（按需 KV 分配） | E2_DESIGN 12 | 显存峰值下降 | ❌ 需 A 验证后评估 | **memory 核心**（E3.0 禁止） | 无 | 高 | 部分（capacity_bytes 语义会变） | 是（峰值显存） | 复杂（buffer 生命周期重构） |
| C（CPU/磁盘分层） | E2_DESIGN 12 | 显存下降 | ❌ 无基线不实施 | 大范围（含分层存储） | 无 | 高 | 不足 | 部分 | 复杂 |
| COW（分支共享） | impl_plan 3.3 | 分支存储共享 | ❌ 未批准 | llama-kv-cache 核心 | 独立 | 高 | 部分（shared_cells 已有） | branch/并发 | 复杂 |
| Context Compression | impl_plan 3.3 | prompt 压缩 | ❌ 未批准 | 大范围 | 独立 | 高（保真） | 部分 | tool_call/long_life | 复杂 |

**关键事实**：**没有任何候选同时满足"已批准 + 排在下一位 + 不越 E3.0 禁止边界"**。
最近的两个方向（A1/A4 unified 淘汰、A2b revisit 修复）均明确记录"需另行立项/
需用户单独授权"，且 A2b 修复还要求预先批准修改 slot routing 层。

---

## 7. 选定候选及依据（或选择阻塞原因）

**未选定——`BLOCKED_NEEDS_SELECTION`**。

依据：
1. 实施计划中的三个优化方向（生命周期/COW/压缩）中，生命周期方向候选
   （A1/A3/A4）已全部否定或标记"需另行立项"，COW 与 Context Compression
   从未获批准且被 E3.0 禁止；
2. E2.0.5 对 A1/A4 的结论是"需在 unified 场景**另行立项**"——明确要求
   单独授权，非"已批准排在下一位"；
3. 候选 B/C 需越 E3.0 禁止边界（memory 核心/分层 KV）；
4. A2b 需用户单独授权（E2.5 第 23 节明确"需单独授权，属 E2.1 实现缺陷"）；
5. 无其他文档（README/issue/计划）批准过新候选。

按任务四要求：**不实现任何策略**；本报告列出候选差异表（第 6 节）供用户选择。

---

## 8. 机制假设（为最近候选提供门禁草案，供选择时参考——未批准、不实现）

> 以下为**门禁设计草案**，仅作为用户选择候选时的输入；未经用户批准不进入实现。

**候选 A1/A4（unified idle-sequence 价值淘汰）的证伪假设（H）**：
> H：在 unified 模式下，当 active slot 逼近 per-slot ctx 上限时，按
> `t_last_used`（LRU）淘汰其他 idle sequence 的尾部 cell，可降低 active
> 请求的 truncation/400 发生率和重算延迟，且不降低任务保真。

- 适用 workload：unified + parallel≥2 的长生命周期/分支压力场景；
- 不适用：parallel=1（无 idle seq，E2.0.5 已证）、non-unified（stream 隔离）；
- 可证伪：若 5/5 独立 replicate 中淘汰策略的 truncation/400 发生率不下降
  或保真下降 → 假设证伪。

**候选 A2b（revisit 判定扩展）的证伪假设（H'）**：
> H'：将 revisit 判定从"一方是另一方完整前缀"扩展为"共享前缀 ≥ 分支阈值"
> （以共享前缀为键的 LCP 缓存映射），可使 branch_pressure 的 X_revisit
> cached 从 53 恢复到 ≥3227（共享前缀+X1obs），revisit p50 不劣于 default。

- 适用：branch_pressure（X1→X2→X3→X_revisit 混合序列）；
- 可证伪：若修复后 X_revisit cached 仍 < 90% prompt 或 p50 仍 ≥ default
  ×1.05 → 证伪。

---

## 9. 实现允许/禁止边界（草案，未批准）

**允许（若用户选择对应候选并授权）**：
- A1/A4：`tools/server/server-context.cpp` 的 slot 生命周期策略层
  （`try_clear_idle_slots` 价值化）；新增 benchmark 侧只读统计；
- A2b：`get_available_slot` 的 prefix-branch 块（需用户预先批准修改
  slot routing 层）。

**禁止（所有候选，维持历阶段边界）**：
- seq 原语语义、KV buffer 分配/容量、memory core、decode/batch/采样/
  tokenizer、cache-reuse、OpenAI 响应、既有 workload、COW、
  Context Compression、分层 KV。

---

## 10. 可观测性和实验设计（草案）

- A1/A4：复用 `GET /metrics/kv`（used_cells/active_sequences/shared_cells）
  + `GET /routing/events`（路由决策）+ 新增"eviction event"计数（additive、
  默认关闭）；server-per-replicate 协议（E2.5 已建立）；
- A2b：复用 branch_pressure workload + `cached_tokens`/`prompt_processed`
  + routing reason（`prefix_branch_revisit` 恢复）；E2.5 配对协议与预注册
  门槛直接复用；
- 两者均：independent replicate 为唯一独立样本、warmup + erase +
  server-per-replicate、配对区组（D P | P D 交替）。

---

## 11. 预注册门槛（草案）

- A1/A4（unified 淘汰）：
  - 正确性：failure/400 不增加、task_success/evaluator 不下降、
    contamination=0、active slot 保真无串扰；
  - 性能：truncation/400 发生率下降 ≥50%（5/5）、p50 不劣于 baseline、
    used_cells 峰值下降 ≥10%（5/5）；
- A2b（revisit 修复）：
  - 直接复用 E2.5 预注册收益门槛（revisit p50 ≥5% 改善、wall ≥3% 或
    prompt_processed ≥10% 下降，5/5 方向一致）；
- 未达到 → STOP（不进入实现后的推广）。

---

## 12. 风险、STOP 条件和回退（草案）

- A1/A4：误淘汰活跃 seq → 保真下降（STOP 条件：任意 replicate 保真低于
  baseline）；回退 = 关闭 flag 恢复 `try_clear_idle_slots` 原行为；
- A2b：判定扩展引入误路由（把新分支当回访）→ 串扰（STOP：contamination>0）；
  回退 = flag 默认关闭 + 代码保留 E2.1 原判定路径；
- 通用 STOP：任何要求越过第 9 节禁止边界的修复方向 → 立即停止并记录
  `BLOCKED_BY_SCOPE`，不扩大实现。

---

## 13. 最终状态

**`BLOCKED_NEEDS_SELECTION`**

- A2 已正式冻结（REJECT 闭环完成，第 1 节）；
- 默认策略仍为 `default`，prefix-branch 未默认启用（第 2 节）；
- 路线图无唯一、已批准的下一候选（第 5-7 节）；
- 候选差异表已提供（第 6 节），供用户选择；
- 未实现任何新策略（符合边界）。

---

## 14. 下一阶段的精确输入

用户需从第 6 节候选表中选择并授权其一（或指定全新候选）：

1. **选择候选 ID**：A1/A4（unified 淘汰）/ A2b（revisit 修复）/ 其他；
2. **授权边界**：是否允许修改 slot routing 层（A2b 必需）/ server-context
   策略层（A1/A4）；
3. **批准状态**：明确"已批准进入门禁设计+实现"或"仅批准门禁设计"；
4. 选定后，下一阶段从第 8-12 节草案冻结门禁（预注册阈值、指标、协议、
   STOP/回退），再进入实现；
5. 若用户选择"不选任何候选"，本路线图暂停，等待人工决策。

---

## 附注：执行记录与提交状态

- 本阶段无源码改动（llama.cpp 未修改、未建空提交；根仓库仅新增本报告）；
- 检查命令：`git status/rev-parse/log/merge-base/diff --check`（两仓库）、
  `--help` 参数确认、E2.5 manifest/summary/paired 复核、`rg` 路线图搜索；
- 测试：本阶段无代码变更，无新测试；E2.5 测试状态（根 100 passed、
  llama.cpp 10/10 + E1 5/5）作为基线继续有效；
- 根仓库提交：`docs: close A2 and gate the next optimization candidate`
  （本报告，hash 见最终汇报）；
- 提交后两仓库 `git status --short` 均为空。
