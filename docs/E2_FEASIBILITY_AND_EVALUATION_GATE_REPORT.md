# E2.0.5 报告：KV 生命周期基线有效性修正、策略可行性验证与保真门禁冻结

- 阶段：E2.0.5（仍不实现 E2.1 生命周期策略）
- 日期：2026-08-06
- 状态：**CONDITIONAL**（详见第 10 节）
- 前置：`docs/E2_DESIGN_AND_BASELINE_REPORT.md`（E2.0，commit `d946ddf`）

---

## 1. 独立重复与 Stateful soak 协议定义

本阶段为 benchmark 引入两套明确区分的重复协议（`--replicate-mode`），
禁止把状态化 cycle 误标为独立 repeat：

### 1.1 Independent replicate（独立重复）

- 每个正式 replicate 使用**清洁 KV 初态**：正式 run 前清除所有 slot KV
  （`POST /slots/{id}?action=erase`，需 `--slot-save-path`）并断言
  `GET /metrics/kv` 满足 `used_cells == 0 && active_sequences == 0`；
- 断言失败（clean_verified=False）的 replicate 标记 `valid=False`，
  **不计入独立统计聚合**（数据仍保留在结果 JSON 中）；
- server 启动后可做 GPU/model warmup（warmup 轮），但 warmup 的 KV 残留
  在第一个正式 replicate 前即被清除；
- 实现：`benchmark/runner/runner.py`（`_protocol_clean`）、
  `benchmark/framework/kv_probe.py`（`clean_all_slots/kv_state`）。

### 1.2 Stateful soak（状态累积）

- 多个 workload cycle 共享同一 KV 状态（不清理）；
- 每个 cycle 记录 `cycle_id` 与 `initial_used_cells`（run 开始前快照）；
- **不得**把 soak cycle 当作独立 replicate 进行统计推断；
- 用于评估长期缓存累积与淘汰行为。

### 1.3 自动验证

- 结果 JSON 顶层 `protocol.<workload>` 记录每个 replicate 的
  `independent / clean_verified / initial_used_cells / valid`；
- `valid_count` 为有效独立重复数；汇总仅聚合 `valid=True` 的 run；
- 测试：`benchmark/tests/test_replicate_protocol.py`（5 例，mock server）。

---

## 2. warmup 后 KV 清理断言（实测）

- 真实 llama-server（Qwen3.5-4B，`--slot-save-path`）验证：
  `POST /slots/0?action=erase` → `{"id_slot":0,"n_erased":9}` → 随后
  `/metrics/kv` 返回 `used_cells=0, active_sequences=0` ✓；
- 全部 8 个独立基线配置 × 5 replicates = **40 个 replicate 的
  clean_verified 全部为 True，initial_used_cells 全部为 0**。

---

## 3. 修正后的 B0/B1 独立基线

### 3.1 实验设置

- 模型 Qwen3.5-4B-Q4_K_M（sha256 `de8e96cd…`）、server binary
  `build-cuda/bin/llama-server`（sha256 `74a6b18b…`，llama.cpp `cc4f66447`）；
- temperature=0、seed=42、ctx=2048、parallel=1、warmup=1、repeat=5；
- ABBA 交叉顺序（cr=0 → 256 → 256 → 0），每个配置 2×5=10 个独立 replicate；
- 每 replicate 前清洁断言（见第 2 节）；
- 另各跑 1 个 stateful soak（repeat=5，共享 KV）。

### 3.2 multi_turn（rounds=20，prompt 34→2020）

| 配置 | 独立 replicates | cache_hit_rate (mean±std) | recompute_tokens (mean) | peak_used_cells | p50_lat(ms) | p95_lat(ms) | tps |
|---|---|---|---|---|---|---|---|
| B0 (cr=0) | 10（全部 clean） | **0.8529 ± 0.0000** | 3313 | 2032/2048 | 484.7 ± 9.0 | 5473.8 ± 26.0 | 903.5 |
| B1 (cr=256) | 10（全部 clean） | **0.8529 ± 0.0000** | 3313 | 2032/2048 | 480.4 ± 5.4 | 5459.3 ± 18.6 | 905.1 |

### 3.3 long_life（rounds=12，含工具调用）

| 配置 | 独立 replicates | cache_hit_rate (mean±std) | recompute_tokens | p50_lat(ms) | p95_lat(ms) | tps |
|---|---|---|---|---|---|---|
| B0 (cr=0) | 10（全部 clean） | **0.7114 ± 0.0000** | 2835 | 569.5 ± 8.9 | 6611.6 ± 48.4 | 472.5 |
| B1 (cr=256) | 10（全部 clean） | **0.7114 ± 0.0000** | 2835 | 564.2 ± 1.7 | 6589.2 ± 38.3 | 473.0 |

### 3.4 与 E2.0（未独立协议）的差异

| workload | E2.0 污染值（warmup 残留） | E2.0.5 独立值 | 差异 |
|---|---|---|---|
| multi_turn hit | 0.9076 | **0.8529** | −5.5pp（warmup 残留虚高） |
| multi_turn recompute | 2080 | **3313** | +59%（每 replicate 首轮全 prefill） |
| long_life hit | 0.7130 | 0.7114 | −0.2pp（工具调用已打散前缀，污染影响小） |

**结论**：E2.0 的 multi_turn 命中率被 warmup 残留显著虚高；独立协议下
**B0 ≈ B1 依然成立**（hit/recompute 逐字段一致，p50 差 <1%），且 10 个
replicate 内 std=0（确定性实验）。**修正后基线为：multi_turn hit=0.8529、
long_life hit=0.7114**。

### 3.5 Stateful soak 结果

- multi_turn soak（repeat=5 共享 KV）：`initial_used_cells` 从 0 起，
  cycle 间 used_cells 累积（第一 cycle 末 ~2032，后续 cycle 起点非 0）——
  与独立协议明确区分；
- long_life soak：同理，cycle 间残留工具调用 KV；
- 说明：soak 数据仅用于展示累积行为，**不用于 B0/B1 统计对比**。

---

## 4. 候选 A 机制可行性验证（受控 HTTP 序列）

实验脚本：`benchmark/scripts/e2_candidate_a_probe.py`；
结果：`benchmark/results/e205/ca_probe_p2_nonunified.json` /
`ca_probe_p2_unified.json`。

### 4.1 受控序列实测（parallel=2, ctx=2048）

| Case | 序列 | prompt_n / cache_n | 结论 |
|---|---|---|---|
| 1 | A → A+B（单 slot） | 14/0 → 13/10 | 前缀命中正常；B 追加只算增量 |
| 2 | A+X → A+Y → A+X（单 slot） | 14/10 → 15/10 → 14/10 | **分支回访只命中公共前缀 A（10），X 分支被 A+Y 覆盖 → 重算** |
| 3 | 双 slot 显式：slot0=A+X, slot1=A+Y, 回访 | 24/0 → 15/10 → 4/**20** → 4/**21** | **分支放不同 slot → 回访全命中（cache_n=20/21）** |
| 4 | 双 slot 自动路由：A+X→A+Y→A+X | 4/20 → 4/21 → 4/20 | 自动路由命中已有匹配缓存的 slot（前缀感知路由存在） |
| 5 | 填满双 slot 后对 slot0 施压（2001 tokens） | — | non-unified：**400 `exceed_context_size_error`**（slot1 空闲 cell 不可借用）；unified：容量共享，used 可达 2048 |

### 4.2 机制可行性逐项判定

| 问题 | 判定 | 依据 |
|---|---|---|
| 1. parallel=1 是否存在可淘汰的其他 idle sequence | **否** | 实测 active_sequences 恒为 1；单 slot 无其他 seq 可淘汰 |
| 2. non-unified p=2 清理 slot A 能否为 B 提供 cell | **否** | 源码 `try_clear_idle_slots` 对非 unified 直接 return；实测 slot0 满时 400（stream 隔离） |
| 3. unified p=2 清理 idle slot 能否缓解 active 压力 | **是** | unified 单 pool，used 可达 2048 总容量；`try_clear_idle_slots` 仅 unified 生效 |
| 4. slot 选择是否按前缀命中排序 | **部分** | Case 4 自动路由命中匹配 slot；但单 slot 下无选择余地 |
| 5. 分支回访能否从其他 idle slot 命中 | **是** | Case 3/4 cache_n=20/21 全命中（多 slot 天然保留分支） |
| 6. 仅 seq_rm 能否提高未来 cache hit | **否** | seq_rm 只释放 cell（元数据），不保留内容；提高命中需**保留分支**（seq_cp 或避免覆盖） |
| 7. 回收 cell 能否改变 per-slot ctx 上限 | **否** | `n_ctx_seq` 固定（1024/2048）；回收不改变上限，超限仍 400 |
| 8. try_clear_idle_slots 触发/选择/回退 | 已确认 | 仅 unified；按 slot 顺序清**第一个**空闲 slot；失败 `n_batch/=2` 重试，最终报错（server-context.cpp:3739） |

### 4.3 关键量化证据：分支保留的收益空间

- 单 slot 分支回访：cache_n=10（仅前缀 A）→ 重算 X 部分；
- 双 slot 分支回访：cache_n=20/21（全命中）；
- **同前缀内容的回访命中率相差 2×**——这是候选 A 中"分支保留"方向
  唯一有受控实验支撑的收益来源。

---

## 5. unified / non-unified 差异（实测汇总）

| 维度 | non-unified（默认） | unified（--kv-unified） |
|---|---|---|
| stream 数 | n_seq_max（每 slot 独立 cell 数组 + K/V tensor） | 1（共享 pool） |
| 每 stream 容量 | n_ctx_seq = n_ctx/n_seq_max（p=2 时 1024） | n_ctx（2048） |
| 跨 slot 借用 cell | **不可**（stream 隔离，实测 400） | **可**（共享 pool） |
| try_clear_idle_slots | 无效（直接 return） | 生效 |
| shared_cells>0 | 未观察到（并行请求各用独立 cell） | 未观察到（同前缀请求仍分 cell；共享需 seq 位置重叠） |
| 分支保留 | 每 slot 独立（Case 3 全命中） | 同左（容量共享不改变 seq 内容） |

---

## 6. Evaluator 正负控制结果

- 新增确定性测试：`benchmark/tests/test_evaluator_gate.py`（5 例）：
  - 正样本（最终回答含 secret）→ `state_retention_rate=1.0` ✓
  - 负样本（缺失 secret）→ `state_retention_rate=0.0` ✓
  - 区分度：0/1 两端互斥 ✓
  - 子串召回对大小写/标点鲁棒（"9527"/"数字是9527。"/" 9527 " 均命中；
    "9526"/"我忘了" 不命中）✓
  - meta 缺失按负样本处理 ✓
- **结论：evaluator 解析逻辑本身有完全区分度，不是 retention=0 的根因。**

---

## 7. 真实模型保真门禁（4B 诊断）

### 7.1 诊断矩阵（temperature=0, seed=42, repeat=3 独立）

| 配置 | truncations | state_retention_rate | 最终回答 |
|---|---|---|---|
| ctx=2048, rounds=12（正式 baseline） | 1.0 | **0.0** | "我没有记住任何秘密数字…" |
| ctx=8192, rounds=12（诊断） | 0.0 | **1.0** | "9527" |
| ctx=2048, rounds=4（诊断） | 0.0 | **1.0** | "9527" |

### 7.2 根因判定

- **retention=0 的根因是 long_life workload 的应用层截断**：
  `keep_msgs=6` + `threshold = ctx - 300`，ctx=2048 时第 8 轮后开始丢弃
  早期消息，secret 注入轮（round 5）的消息在第 12 轮询问前已被丢弃；
- **模型能力无问题**：无截断（ctx=8192 或短会话）下 4B 模型 100% 正确
  召回 secret；
- **evaluator 解析无问题**（第 6 节）；
- 修正 E2.0 报告中"4B 模型能力限制"的判断（E2_DESIGN 报告第 10 节已同步修正）。

### 7.3 E2.1 保真门禁（冻结）

正式 baseline 不修改 workload。E2.1 实现必须同时满足：

1. **确定性输出等价性**：B0 的逐请求 text 规范化 hash 已保存
   （`results/e205/text_hashes_*.json`，multi_turn 100 行 → 12 unique，
   证明 temperature=0 下输出确定性）；B2 的 hash 集合与 B0 一致
   （允许顺序不同，不允许内容缺失/变更）；
2. **请求成功率**：B2 请求失败数 ≤ B0；
3. **evaluator 不下降**：state_retention_rate / task_success 不低于 B0
   （B0 基线值 0，即 B2 不得引入新的截断/丢失导致 retention 进一步恶化——
   更严格地，B2 不得降低 ctx=8192 诊断配置下可达的 retention=1.0）；
4. **无跨 slot 串扰**：并发场景各 slot 输出与单 slot 一致（hash 对照）。

---

## 8. 唯一 E2.1 候选及修改边界

### 8.1 候选评估（四方向）

| 方向 | 触发条件 | 可用 KV/slot 状态 | 现有原语 | 能改善 | 无法改善 | 适用 | 最坏情况 |
|---|---|---|---|---|---|---|---|
| A1 空闲 slot 价值感知淘汰 | unified 下 KV 压力 | idle slot 列表、used_cells | seq_rm | 共享 cell 利用率 | 前缀命中（内容已丢） | 仅 unified | 淘汰高价值 slot → 回访重算 |
| A2 prefix-aware idle-slot 路由 | 多 slot 可用 | slot 缓存内容（不可读） | 路由（已有自动路由） | 分支回访命中（Case 4 已证） | — | p≥2 | 路由抖动 |
| A3 request 尾部回收 | 请求结束 | slot 尾部 KV | seq_rm（已有 p0,-1 路径） | 释放无效 cell | 命中率（回收即丢弃） | 全部 | 前缀被打断 |
| A4 unified 容量压力下 idle-sequence 回收 | unified + KV 满 | idle seq 状态 | seq_rm / try_clear_idle_slots 策略化 | 缓解 active slot 压力 | per-slot ctx 上限 | 仅 unified | 淘汰活跃 seq |

### 8.2 唯一推荐：A2（prefix-aware 保留/路由，落地为"分支保留"）

**依据（受控实验）**：Case 3/4 证明分支回访在"分支保留于不同 slot"时
cache_n=20/21 全命中，而单 slot 覆盖时仅 10——**这是全部候选方向中唯一
有 2× 实测收益证据的机制**。A1/A4 依赖 unified 且只释放 cell（不提高
命中，Q6 判定）；A3 方向相反（回收必然降低潜在命中）。

**修改边界（严格）**：
- 不修改 `seq_rm/seq_keep/seq_cp/seq_add` 语义（仅调用现有原语）；
- 不修改 KV buffer 分配、`--cache-reuse`、`capacity_bytes`；
- 实现范围限定 `tools/server/server-context.cpp` 的 slot 选择/保留策略层：
  请求进入时优先选择缓存前缀匹配度最高的空闲 slot（自动路由已有该行为，
  需显式化并保证确定性）；可选地在 slot 切换时用 `seq_cp`（同 stream
  元数据复制，现成原语）保留分支前缀；
- **不改变 per-slot context 上限**；超限行为（400）与 baseline 一致；
- 回退：策略层异常时回退到现有自动路由行为；
- 显式排除：不实现 COW（物理共享）、不实现淘汰（A1/A4 留待后续阶段）。

### 8.3 明确否定项

- **A1/A4（淘汰方向）本阶段否定**：parallel=1 无淘汰对象；non-unified
  stream 隔离下淘汰无效；unified 下仅释放 cell 不提高命中；
- **不得声称回收 cell 可降低 capacity_bytes 或突破 per-slot ctx 上限**
  （Q7：实测超限仍 400）。

---

## 9. 未决风险

1. 自动路由的 slot 选择细节未完全可观测（server 内部逻辑），A2 显式化
   需要先补充 slot 级前缀命中可观测性（E1 endpoint 目前只有聚合值）；
2. `seq_cp` 保留分支的内存开销（多分支占用 cell）在长会话下的累积需
   建模与上限约束（防止分支爆炸）；
3. 独立协议依赖 slot erase（`--slot-save-path`），E2.1 实验脚本已固化该
   参数；默认无该参数时 independent 模式自动标记 invalid（安全降级）；
4. unified 模式下 `active_sequences` 与 `shared_cells` 的观测口径与
   non-unified 不同（实测 active=1 现象），E2.1 需统一解释；
5. llama.cpp 完整 pytest 宽回归仍受模型下载限制（环境问题，不阻塞）。

---

## 10. 最终状态与结论

**状态：CONDITIONAL**

- ✅ 独立基线有效：40/40 replicate 清洁断言通过，ABBA 双重复逐字段一致，
  修正后基线（multi_turn hit=0.8529 / long_life hit=0.7114）真实可复现；
- ✅ 保真门禁可用：evaluator 正负控制 5/5、4B 诊断定位截断根因、
  B0 输出 hash 已保存（确定性输出等价性可判）；
- ✅ 候选机制实证：A2（分支保留）有 2× 命中收益的受控实验证据；
  A1/A4 淘汰方向在 non-unified/parallel=1 下不可行（已实证否定）；
- ⚠️ **条件**：① E2.1 实现范围限定为 A2（分支保留/路由显式化），
  淘汰方向（A1/A4）需在 unified 场景另行立项；② 需先补充 slot 级
  前缀命中可观测性（E1 扩展）；③ `seq_cp` 分支保留的容量上限策略需
  在 E2.1 设计时确定。

**建议**：人工审查本报告与 E2.0 报告修正后，可进入 E2.1（范围=A2）。

---

## 附注：执行记录

- git 提交：本阶段完成后提交根仓库（报告/脚本/测试）；llama.cpp 无改动。
- 实验产物：`benchmark/results/e205/`（独立基线 10 配置 + 候选 A probe +
  text hashes + summary CSV/JSON）、`benchmark/results/e205_diag/`（保真诊断）；
- 脚本：`benchmark/scripts/e2_scan_independent.sh` / `e2_candidate_a_probe.py`
  / `e2_diag_retention.sh` / `e2_summarize.py`；
- 测试：`benchmark/tests/test_replicate_protocol.py`（5 例）、
  `test_evaluator_gate.py`（5 例）；benchmark 全量 **83 passed**。
