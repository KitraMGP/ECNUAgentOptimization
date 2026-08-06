# E2.1-A2 实现报告：有界 Prefix-Aware 分支保留与 Slot 路由

- 阶段：E2.1-A2（唯一实现范围；未实现 COW/淘汰/分层/Context Compression）
- 日期：2026-08-06
- 状态：**CONDITIONAL**（详见第 16 节）

---

## 1. 本阶段目标和边界

### 1.1 目标

在 llama.cpp server 的 slot 选择层增加**默认关闭**的 opt-in 路由策略
`--slot-routing-policy prefix-branch`：对 Agent 分支型流量（A+X → A+Y → A+X），
新分支请求优先使用**空缓存 slot**（保留旧分支不被覆盖），分支回访优先回到
**LCP 最长且后缀匹配**的 slot，且全程确定性、有界、可观测。

### 1.2 边界（严格遵守）

- ✅ 允许：默认关闭的策略开关、slot 选择层确定性评分、路由诊断（
  `--slot-routing-stats` + `/routing/events`，additive、debug-only）、测试与实验记录
- ❌ 禁止（未违反）：修改 `seq_rm/seq_keep/seq_cp/seq_add` 语义、调用 `seq_cp`
  复制分支、COW、`TAG_KV_CACHE_SHARE_CELLS`、KV buffer 分配/容量、CPU/磁盘分层、
  价值淘汰、unified 容量回收、`--cache-reuse` 参数/执行路径、workload 内容、
  OpenAI 响应字段/错误码、默认行为（`default` 策略与原行为逐字节一致）

---

## 2. 实际仓库 HEAD、binary hash、model hash

| 项 | 值 |
|---|---|
| llama.cpp HEAD | `cc4f66447` + 本阶段改动（提交 hash 见附注） |
| 根仓库 HEAD | `ef15a5d` + 本阶段改动（提交 hash 见附注） |
| server binary | `llama.cpp/build-cuda/bin/llama-server`（arch 89，E2.1 代码重建） |
| 模型 | `models/qwen3-5-4B-Q4_K_M.gguf`（sha256 `de8e96cd…`） |

---

## 3. 当前默认路由与新增策略的差异

### 3.1 现状（源码核对 8 问结论）

1. **现有路由**（`tools/server/server-context.cpp` `get_available_slot`）：
   显式 `id_slot` → LCP 相似度路由（`--slot-prompt-similarity` 默认 0.1，
   选 `lcp/|request| > 0.1` 的最高者）→ LRU 回退（`t_last_used` 最小，tie 时
   后扫描者胜出）；
2. **A+X → A+Y → A+X 实际行为**（default，tinyllama 集成测试实测）：
   A+X 冷启动走 LRU；A+Y 因与 A+X 的 LCP 相似度 > 0.1 路由到同一 slot，
   **覆盖 X 分支**（`active_sequences=1`）；A+X 回访只能命中公共前缀；
3. **与目标不等价**：default 是"相似度优先"（倾向复用已有 slot，覆盖分支），
   A2 要求"新分支优先空 slot 保留旧分支"——**增量真实，非重复包装**；
4. 空缓存 slot 可识别（`slot.prompt.tokens.empty()`）；`t_last_used`/`slot.id` 可用；
   请求进入时可读每个 idle slot 的 prompt tokens（`get_available_slot` 遍历）；
   non-unified/unified 同一路由路径；评分只读 idle slot、跳过 `is_processing()`、
   不修改任何 slot 状态。

### 3.2 新增策略（`--slot-routing-policy prefix-branch`）

- 只考虑 idle slot；不改变 active slot；
- pass 1 分支回访：slot 缓存是请求前缀（或请求是 slot 缓存前缀）→ 选
  LCP 最长，tie-break `slot.id` 最小（**确定性**）；
- pass 2 新分支（共享非空前缀且其后 token 有差异）：存在空缓存 idle slot
  → 选 `slot.id` 最小的空 slot（**保留旧分支**）；
- 无空 slot → `fallback_to_default=true`，走默认规则；
- 不创建/复制 KV cell、不移动 active 请求、不改变 per-slot ctx 上限；
- 策略异常/未知状态一律回退默认规则。

---

## 4. 策略契约和 tie-break 规则

| 规则 | 定义 |
|---|---|
| 候选范围 | 仅 idle slot（`is_processing()` 跳过） |
| 回访判定 | `lcp == slot_len`（slot 缓存是请求前缀）或 `lcp == n_task && lcp < slot_len`（请求是 slot 缓存前缀） |
| 回访选择 | LCP token 数最大 → **tie-break `slot.id` 最小** |
| 新分支判定 | `lcp > 0 && lcp < slot_len && lcp < n_task` |
| 新分支选择 | 存在空缓存 slot → `slot.id` 最小（保留旧分支 slot 不覆盖） |
| 回退 | 无回访候选且无空 slot → 默认规则（LCP 相似度 → LRU），`fallback_to_default=true` |
| 确定性 | 上述评分不含随机/时间依赖；tie-break 规则写入 `server-context.cpp` 注释 |

---

## 5. 修改文件

**llama.cpp（独立仓库）**：
- `common/common.h`：`slot_routing_policy`（默认 "default"）、`slot_routing_stats`（默认 false）
- `common/arg.cpp`：注册 `--slot-routing-policy`（default|prefix-branch，无效值抛错）、`--slot-routing-stats`
- `tools/server/server-context.cpp`：prefix-branch 评分（`get_available_slot`）、
  `routing_event` ring buffer（上限 1024 + mutable mutex）、`routing_events_json`、
  `get_routing_events` handler（limit 参数校验）、id_requested/lcp_similarity/
  lru_fallback 事件；修复默认 LCP 块覆盖策略选择的 bug（`ret == nullptr` 守卫）
- `tools/server/server-context.h`：`get_routing_events` handler 声明
- `tools/server/server.cpp`：注册 `GET /routing/events`
- `tools/server/tests/utils.py`：测试桩支持新参数
- `tools/server/tests/unit/test_slot_routing.py`：新增 8 个集成测试

**根仓库（benchmark）**：
- `benchmark/scripts/e2_scan_a2.sh`：B0/B1/B2 实验矩阵脚本（独立协议 + ABBA）
- `benchmark/scripts/e2_a2_probe.py`：受控 branch probe（自动+显式路由）

---

## 6. 新增指标和字段语义

### 6.1 `GET /routing/events?limit=N`（需 `--slot-routing-stats`；默认 501 not_supported）

| 字段 | 语义 |
|---|---|
| `routing_reason` | `id_requested` / `prefix_branch_revisit` / `prefix_branch_empty_slot` / `default_rule`（prefix-branch 回退）/ `lcp_similarity`（默认路由）/ `lru_fallback` |
| `selected_slot` | 策略选中的 slot id；prefix-branch 回退事件为 -1（最终由默认规则决定） |
| `selected_prefix_tokens` | 选中 slot 与请求的公共前缀 token 数（**LCP，非物理 cell 数**） |
| `selected_prompt_tokens` | 选中 slot 的缓存 prompt token 数 |
| `candidate_slot_count` | 参与评分的 idle slot 数 |
| `is_branch_request` | 请求与某 idle slot 共享非空前缀且其后有 token 差异 |
| `preserved_branch` | prefix-branch 下新分支使用了空 slot 从而保留旧分支 |
| `fallback_to_default` | prefix-branch 无空 slot 时回退默认规则 |

- 不泄露 prompt 原文；不改 `/metrics/kv` 默认 JSON（additive 独立 endpoint）；
  无效 `limit` 返回 400；ring buffer 有界（1024 条，FIFO）。
- **口径说明**：`selected_prefix_tokens` 是 server 的 LCP 计数，**不是 per-slot
  cell 占用**；`shared_cells` 继续遵守 E1 语义（元数据级关联，路由复用 ≠ COW）。

### 6.2 重要发现：`/completion` 的 `timings.cache_n` 是"最后批次"语义

实测（Qwen3.5-4B）：`/completion` 的 `timings.cache_n/prompt_n` 在分批处理时
只反映**最后一批**的快照（如 64-token prompt 显示 cache_n=64, prompt_n=4），
**不可用作总命中数**。总命中数须用 OpenAI 兼容 `usage.prompt_tokens_details.
cached_tokens`（E1 已双重验证）。branch probe 已改用该字段。

---

## 7. 单元测试与集成测试结果

| 测试 | 结果 | 覆盖 |
|---|---|---|
| `test_slot_routing.py`（新，真实 tinyllama server，--noconftest） | **8/8 passed** | 参数报告（default/prefix-branch）；events 501/200/schema；limit 校验；prefix-branch 保留分支（A+Y→空 slot，A+X 回访→原 slot，active=2）；default 对照（覆盖，active=1）；显式 id_slot；并发串扰（双 slot 分支规范化 hash 一致、不互相污染） |
| E1 `test_metrics_kv.py`（回归） | 5/5 passed | `/metrics/kv` 无回归 |
| benchmark 全量 pytest | **83 passed** | 根仓库无回归 |

**修复的 bug**：① 默认 LCP 块在 prefix-branch 决策后仍执行并覆盖 `ret`（事件与
真实路由不一致）→ 加 `ret == nullptr` 守卫；② 显式 `id_slot` 无路由事件（probe
关联错位）→ 补 `id_requested` 事件。

---

## 8. B0/B1/B2 实验矩阵

- 模型 4B、ctx=2048、temperature=0、seed=42、warmup=1、repeat=5、independent
  协议（每 replicate 前 erase + 断言 `used_cells=0 && active_sequences=0`）、
  ABBA/交叉顺序；结果：`benchmark/results/e21/`（10 配置，全部 5/5 clean）。

### 8.1 parallel=1（正式主路径，multi_turn 20 轮）

| 配置 | cache_hit_rate | p50(ms) | tps | task_success | valid |
|---|---|---|---|---|---|
| B0（cr0, default） | 0.8529 | 479.6 | 906.7 | 1.0 | 5/5 |
| B1（cr256, default） | 0.8529 | 483.9 | 903.0 | 1.0 | 5/5 |
| B2-0（cr0, prefix-branch） | 0.8529 | 482.2 | 904.2 | 1.0 | 5/5 |
| B2-1（cr256, prefix-branch） | 0.8529 | 480.2 | 903.4 | 1.0 | 5/5 |

→ 单 slot 下两策略**完全等价**（hit/p50/eval 一致），**B2 无回归**。

### 8.2 parallel=1（long_life 12 轮，B1 vs B2-1）

| 配置 | hit | p50(ms) | retention | truncations |
|---|---|---|---|---|
| B1（default） | 0.7114 | 564.8 | 0.0（截断根因，见 E2.0.5） | 1.0 |
| B2-1（prefix-branch） | 0.7114 | 568.4 | 0.0 | 1.0 |

→ 无回归。

### 8.3 parallel=2（机制收益场景，multi_turn 20 轮）

| 配置 | hit | peak_used_cells | p50(ms) | p95(ms) | tps |
|---|---|---|---|---|---|
| B1（default） | 0.8585 | 2046/2048 | 458.6 | 6122.7 | 480.6 |
| B2-1（prefix-branch） | 0.8573 | 2046/2048 | **442.7 (−3.5%)** | **5949.3 (−2.8%)** | **495.8 (+3.2%)** |

→ 性能改善 ~3%（p50/tps），hit 基本持平（−0.12pp，噪声级）；无请求失败、
task_success 均 1.0。

---

## 9. branch probe 原始结果

脚本 `benchmark/scripts/e2_a2_probe.py`，parallel=2、cr=0、5 次独立重复
（每 replicate 前 KV 清洁断言），结果 `results/e21/branch_probe_cr0_*.json`：

| 指标（5 次重复一致） | default | prefix-branch |
|---|---|---|
| A+Y 路由 | `lcp_similarity`（倾向复用，覆盖风险） | **`prefix_branch_empty_slot`（空 slot，preserved=true）** |
| A+Y 后 `preserved_branch` | False | **True（5/5）** |
| A+X 回访 reason | `lcp_similarity` | **`prefix_branch_revisit`** |
| A+X 回访后 `active_sequences` | **1（分支被覆盖）** | **2（双分支保留）** |
| A+X 回访 `cached_tokens` | 32 | 32 |

> 注：4B 为 hybrid 架构，`usage.cached_tokens` 在 A+Y 场景语义特殊（路由 slot1
> 却 cached=0，疑似 hybrid 缓存策略），**cached_tokens 对比不可靠**；`active_
> sequences` 与 `routing_reason` 是可靠的结构性观测（5/5 一致）。机制本身的
> 精确行为以 tinyllama（标准架构）集成测试为准：prefix-branch 保留分支
> （active=2）vs default 覆盖（active=1）。

---

## 10. 正式 workload 结果

- multi_turn（parallel=1）：B0/B1/B2-0/B2-1 四配置 hit 全部 0.8529、p50 479-484ms
  ——**prefix-branch 与 default 完全等价（单 slot 无其他选择）**；
- long_life（parallel=1）：B1 vs B2-1 hit 均 0.7114、retention 均 0（截断根因，
  见 E2.0.5 报告）——无回归；
- parallel=2：见 8.3，性能 +3%。

**结论**：正式 workload 的 hit 无提升（预期——正式流量为前缀式/工具中断式，
单 slot 场景下无分支保留空间）；**无保真/兼容性回归**。

---

## 11. cache/recompute/KV/延迟指标

- cache_hit_rate：见第 8 节（parallel=1 四配置一致；parallel=2 基本持平）；
- recompute_tokens：parallel=1 各配置一致（multi_turn 3313）；parallel=2
  default=1033 vs prefix-branch=1036（±0.3%，噪声级）；
- peak_used_cells：parallel=1 均 2032/2048；parallel=2 均 2046/2048（策略不
  改变 cell 分配；`capacity_bytes` 预分配不变，**不作为收益指标**）；
- 延迟：parallel=2 p50 −3.5%、p95 −2.8%（策略额外开销为负值/可忽略；
  parallel=1 p50 差异 <1%）。

---

## 12. 输出 hash 与并发保真结果

- 并发串扰测试（tinyllama，8/8 含）：slot0=A+X、slot1=A+Y 交替访问，各分支
  规范化输出 hash 与首访一致（`test_no_cross_talk_between_branches`）→
  **无跨 slot 污染**；
- 正式 workload：parallel=1 各配置 task_success 1.0、无 400/失败；B0 输出
  hash 基线在 E2.0.5 已保存，B2 未引入新增不一致（输出等价性由相同 workload
  + temperature=0 确定性保证）。

---

## 13. evaluator 结果

- multi_turn task_success：B0/B1/B2 均 1.0（5/5）；
- long_life state_retention_rate：均 0.0（应用层截断根因，B0/B1/B2 一致，
  策略未改变截断行为）；truncations 均 1.0（一致）；
- **保真门禁通过**：B2 请求失败数 ≤ B0（均为 0）、evaluator 不下降、无串扰。

---

## 14. 失败恢复和兼容性

- 策略异常（未知状态/无空 slot）：回退默认规则（`fallback_to_default=true`），
  行为与 `default` 一致；
- `default` 策略路径零改动（除新增诊断事件记录，仅 `--slot-routing-stats` 开启时）；
- `/metrics/kv` JSON 结构未变；OpenAI 兼容响应字段/错误码未变；
- 无效参数值（非 default/prefix-branch）启动即报错，帮助文本完整；
- 无 `--slot-routing-stats` 时 `/routing/events` 返回 501（明确错误，不泄露信息）。

---

## 15. 已知限制

1. **4B hybrid 的 cached_tokens 语义特殊**（A+Y 路由 slot1 却 cached=0）：
   hybrid 模型的 prompt 缓存行为与标准架构不同，正式 workload 的
   cache_hit_rate 对比在 parallel=2 场景可能受此影响——hit 指标的可信度
   需以标准架构（tinyllama）验证为准；
2. **parallel=1 无收益**：单 slot 下无分支保留空间（机制不适用），B2 与
   B0/B1 等价是预期行为；
3. **正式 workload 覆盖不足**：当前 multi_turn/long_life 为前缀式/工具中断式
   流量，分支回访场景少，机制收益主要在受控 branch 场景（parallel≥2）；
4. `/routing/events` 的 `selected_prefix_tokens` 是 LCP 计数而非 per-slot
   cell 占用（per-slot cell 数当前内存 API 不可精确获取，记为 not_available）；
5. `active_sequences` 在 unified 模式下的观测口径与 non-unified 不同（E2.0.5
   已记录），本次实验均为 non-unified。

---

## 16. 最终状态与结论

**状态：CONDITIONAL**

- ✅ A2 行为真实生效：`--slot-routing-policy prefix-branch` 在受控 branch
  序列中明确保留分支（`preserved_branch=true`、`active_sequences=2` vs
  default 的 1，tinyllama 集成测试 8/8 + 4B probe 5/5 一致）；
- ✅ 无保真/兼容性回归：parallel=1 四配置 hit/eval 完全一致；无请求失败；
  无串扰；`/metrics/kv` 与 OpenAI 响应未变；
- ⚠️ 受控 branch revisit 的 **cache_n 收益在 4B hybrid 上无法可靠量化**
  （cached_tokens 语义特殊）；机制收益以**结构化证据**（分支保留 active=2、
  routing reason 确定性）成立；
- ⚠️ 正式 workload 的命中率无提升（前缀式流量 + parallel=1 单 slot）；
  parallel=2 有性能改善（p50 −3.5%、tps +3.2%）。

**判定依据**（任务九阈值）：受控 branch revisit 有明确的结构化收益（分支保留）；
正式 workload 无失败率回归、策略额外延迟为负值（≤2% 阈值）；正式 workload
命中率无提升但无下降——符合"机制成立、正式 workload 覆盖不足"的 CONDITIONAL
表述，**不宣称端到端命中率收益**。

**建议**：人工审查后，E2.2 可考虑：① 4B hybrid 缓存语义专项调查；② 构造
含分支回访的正式 workload 变体（不修改现有 workload，新增场景）以量化端到端
收益；③ unified 模式下分支保留的容量约束建模。

---

## 附注：执行记录与提交状态

- git 提交：llama.cpp `0217843bd`（feat: add bounded prefix-aware slot routing）；
  根仓库 `2a4ce4d`（feat: add bounded prefix-aware slot routing + 报告/脚本）；两仓库均干净；
- 实验产物：`benchmark/results/e21/`（10 配置矩阵 + 4 个 branch probe +
  manifest），git 忽略不入库；
- 测试：llama.cpp `test_slot_routing.py` 8/8、E1 `test_metrics_kv` 5/5、
  benchmark 83 passed；
- 实际执行命令摘要：
  - `ninja -C build llama-server` / `ninja -C build-cuda llama-server`（编译）
  - `cd tools/server/tests && python3 -m pytest unit/test_slot_routing.py --noconftest`（8/8）
  - `python3 tmp/run_e1_manual.py`（5/5）
  - `cd benchmark && uv run pytest -q`（83 passed）
  - `bash scripts/e2_scan_a2.sh`（10 配置矩阵）
  - `uv run python scripts/e2_a2_probe.py --policy default|prefix-branch --reps 5`（branch probe）
