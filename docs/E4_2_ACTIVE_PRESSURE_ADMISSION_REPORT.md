# E4.2：Unified KV 全活跃压力下的准入、背压与资源回收门禁

状态：**PASS_ACTIVE_PRESSURE_ADMISSION**

## 1. 背景与目标

E4.1 场景 C 中，新请求 S4 成功，但既有 active 请求返回 HTTP 500
"Context size has been exceeded"；其因果（单请求 ctx 上限 vs 共享 KV 饱和 +
S4 准入）未明，故 E4.1 不得无条件 `PASS_KV_SATURATION_CORRECTNESS`。

本阶段目标：明确全 active、无合法 victim 时的产品契约与实际行为；验证新请求
不能通过准入优先级反转破坏已接纳 active 请求；以完整时间序列验证 churn 后资源
是否真正回收。

## 2. 冻结的产品契约（benchmark/results/e42/contract.json）

- **admitted**：请求被分配到 slot 并进入 processing（`launch_slot_with_task` →
  state != IDLE）即视为已接纳；admitted 后应能在自身 per-seq ctx 上限内完成。
- **状态定义**：active = slot processing；idle = SLOT_STATE_IDLE（KV 可保留）；
  queued = 任务在 queue_tasks 等待空 slot；decoding = batch decode 中。
- **最小契约**：新请求不得仅因后到准入而使原本可完成的已接纳 active 请求失败。
- **单请求 ctx 上限**：400 `request (N tokens) exceeds the available context size (M tokens)`，
  产生于请求校验阶段（task 分配前）。
- **共享池饱和**：decode ret=1 → `Context size has been exceeded.`，产生于
  `update_slots` decode 失败分支（server-context.cpp）；原实现把错误发给**所有**
  processing slots（含已接纳 active）并清 KV——代码 TODO 注释明确
  "terminate only the largest active slot" 未实现，即准入优先级缺陷。
- **当前代码未承诺** active 优先于新到达请求；purge 只清 idle slot（active 不被选为 victim）。

## 3. 预注册（benchmark/results/e42/prereg_manifest.json）

- candidate/root commit、binary/model sha256；unified p4 ctx 8192 cache-ram 0
  temp 0 seed 42；prompt 预算（active 800→实测 ~1300-2100、S4 3000→实测 ~5300）；
  场景 A-E + churn 12 周期；判定规则（PASS/HOLD/REJECT）逐项冻结；wall-time 仅
  exploratory；E3.6 不执行、不伪造 field trace。

## 4. E4.1 场景 C 因果归因

复现（修复前，lru）：

| 场景 | S0 | S1 | S2 | S3 | S4 | kv_after | 说明 |
|---|---|---|---|---|---|---|---|
| A active-only | 500 | 500 | 500 | 500 | — | 0 | **无 S4 也全失败** |
| B +S4 | 500 | 500 | 500 | 500 | ok | 0 | active 全失败、S4 完成 |

**归因结论（对照）**：
1. active 无 S4 时**不能**完成——4 个并发 active 的 prompt 实测各 ~2137，
   总需求 8548 > 8192（**4 并发自身超池**），与 S4 无关；
2. 失败是**共享 KV 容量不足**（batch decode ret=1），非单请求 ctx 上限
   （active prompt 2137 << 8192）；
3. 原场景 C 的 active 失败**不是 S4 准入所致**（A 无 S4 也失败）；
4. 但暴露了**真缺陷**：batch decode 失败把错误连带发给**所有** processing slots
   （含原本可完成的 active）。

**校准后复现**（active 800→实测 ~1300-2100、S4 3000→实测 ~5300，
A 完成 / B 超池）：

| 场景 | S0 | S1 | S2 | S3 | S4 | kv_after |
|---|---|---|---|---|---|---|
| A active-only（校准） | ok | ok | ok | ok | — | 6099 |
| B +S4（修复前） | ok | ok | **500** | **500** | **500** | 0 |

**修复前缺陷确认**：active-only 完成、+S4 后 active 连带失败、S4 已 admitted、
失败由共享 KV 压力导致 → **准入优先级缺陷**（后到请求的 decode 失败连带终止
已接纳 active）。

## 5. 最小修复（llama.cpp `fix: preserve admitted requests under unified KV pressure`）

`tools/server/server-context.cpp` `update_slots` decode 失败分支：

- decode 失败（`!err.empty()`）时，**优先终止"仍在 prefill（
  `n_prompt_tokens_processed < task->n_tokens()`）"的 slot**——即容量不足的
  后到请求（S4）：send_error + release + `prompt_clear()`（只清被终止请求的 KV），
  然后 `return false`（不 throw，让已接纳 active 在下一轮 update_slots 继续）。
- 仅当**没有 prefill 中 slot**（全部在生成阶段）时保留既有"全部终止 + throw"
  行为（active 自身超池时的安全失败路径）。
- 不改变有 idle victim 时的 default/LRU 淘汰（E4.1 已验证行为不变）；
  不全局串行化、不降并发、不静默缩短输出、不改 HTTP schema/错误类型。

## 6. 对照场景结果（修复后，unified p4 ctx 8192）

| 场景 | S0 | S1 | S2 | S3 | S4 | kv_after | 结论 |
|---|---|---|---|---|---|---|---|
| A active-only | ok | ok | ok | ok | — | 6012 | active 全完成 ✓ |
| B late-arrival | ok | ok | ok | ok | **500** | 3756 | active 保留、S4 被拒 ✓ |
| C simultaneous | ok | ok | ok | ok | **500** | 5327 | admitted 后不失败、S4 拒 ✓ |
| D recovery | ok | ok | ok | ok | 500→**ok** | 5489 | 资源释放后 S4 重试成功 ✓ |
| E ctx-limit | — | — | — | — | **400** | 0 | 单请求 ctx 上限（校验阶段）|

- **B/C 的 S4 500**：`Context size has been exceeded.`（decode 失败，明确容量
  错误）——与 **E 的 400**（`request (N) exceeds the available context size (M)`）
  完成因果区分（任务四问题 3/4/5 全部回答）。
- **无 idle victim 时 active 无 purge/reset**（purge 只清 idle；修复后 active
  KV 保留，kv_after 3756-5489 > 0）。
- **D**：S4 首次 500 被拒 → active 完成后重试 → ok（不读已完成请求的错误 KV）。

## 7. churn 完整回收（12 周期，任务六）

- **固定 prompt（相同逻辑状态）**：12 周期 used_cells **全部 4440（drift=0）**；
  post-erase used=0/active=0；RSS 不增长；purge=0。
- **内容演进**：每周期 +200 tokens，used 4440→8032（增长可归因于内容增长，
  +358/周期 ≈ 预算）；第 11 周期超池触发 purge×3 后回落 6418。
- 门槛：used ≤ capacity ✓；无负计数/悬挂 ✓；相同逻辑状态无单调增长 ✓；
  清空后回空闲基线（0/0）✓。

## 8. 双模式验证

- trace-on（B）：active 的 assigned→active→idle 链完整（78 事件），S4 被拒，
  kv_after=3773，行为与 trace-off 一致。
- trace-off：lifecycle 事件 **0**；准入/完成/拒绝/回收行为一致。
- 不比较 wall-time（仅 exploratory）。

## 9. 测试

- 根仓库：`uv run pytest -q` → **177 passed**（含 E4/E4.1 契约与解析单测）。
- llama.cpp：`test_active_pressure.py`（新增 3：late-arrival/ctx-limit/recovery）
  + unified_idle_lifecycle 5 + slot_routing 10 + lifecycle_trace 7 → **25 passed**；
  E1 手动回归 **5/5**。
- `git diff --check` 通过。

## 10. 判定：PASS_ACTIVE_PRESSURE_ADMISSION

逐项（任务九）：

| 门槛 | 结果 |
|---|---|
| active-only control 全部完成 | ✓ |
| late-arrival 后到 S4 不使 active 失败 | ✓（修复后）|
| 无 idle victim 时无 active purge/reset/错误 KV 复用 | ✓ |
| 超容量请求明确拒绝（500 容量错误）| ✓ |
| simultaneous 符合准入契约 | ✓ |
| 资源释放后 S4 恢复 | ✓（D S4_retry ok）|
| 单请求 ctx 上限（400）与共享池压力（500）因果区分 | ✓ |
| 无错误 2xx/静默截断/串线/crash/hang/死锁 | ✓ |
| churn 相同逻辑状态无持续增长、最终回收 | ✓（drift=0、erase 回 0）|
| production trace-off event 数 0 | ✓ |
| 相关测试全过 | ✓（177 + 25 + E1 5/5）|

**E4.1 状态更新**：场景 C 因果已明（4 并发自身总需求超池 + 连带失败缺陷）；
idle-victim 淘汰结论继续有效，E4.1 的 PASS 仅限 idle-victim 部分；
active-pressure 契约由本阶段 PASS 覆盖。E3.5.3 继续 HOLD；E3.6 不执行。

## 11. 修改清单与提交状态

llama.cpp（独立仓库）：
- `tools/server/server-context.cpp`：decode 失败分支（优先终止 prefill 中 slot、
  保留 active、return false 不 throw）
- `tools/server/tests/unit/test_active_pressure.py`（新增 3 例）
- 提交：`fix: preserve admitted requests under unified KV pressure` + 状态说明

根仓库：
- `benchmark/scripts/e42_contract.py`、`e42_prereg_manifest.py`、
  `e42_admission_gate.py`、`e42_churn_reclamation.py`、`e42_summarize.py`
- `benchmark/results/e42/`：contract/prereg/summary/manifest（git 忽略部分）
- 提交：`test: validate active pressure admission and reclamation` + 状态说明
