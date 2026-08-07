# E3.5.1 报告：Lifecycle Attribution 诊断契约闭环与 Field Trace 接入门禁

- 阶段：E3.5.1（独立复核 E3.5 + 契约闭环；additive 默认关闭只读诊断修正）
- 日期：2026-08-06
- **最终状态：`HOLD_FOR_ATTRIBUTION_GAPS`**（attribution 全部满足；
  唯一未达预注册门槛：p95 开销——归因环境噪声，见第 13/15 节）

---

## 1. 目标与边界

- 目标：独立复核 E3.5（不继承其 READY）；证明**每 request** 可映射到
  trace/slot/sequence/generation；闭合 assigned/active/idle/pressure/purge/
  retry/resume/release 事件链；事件丢失/截断/重复/乱序检测；diagnostics-off
  配对开销；field trace 安全接入确认；
- 禁止（未违反）：改 default/LRU/tie-break/tick/默认值、seq/KV/decode 语义、
  重跑 A1/A4 收益矩阵、改 OpenAI 响应、采集内容/secret/身份、用 trace 参与
  调度/victim 选择、伪造真实流量、无目标扫描。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp | `40948c938` → 本阶段 `aba4b26a`（fix: complete lifecycle attribution event contract） |
| 根仓库 | `0b1d28d` → 本阶段新增提交 |
| server binary | `build-cuda/bin/llama-server`（含 schema=2 契约，hash 见 manifest） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| GPU | RTX 4060 Laptop 8188 MiB |

---

## 3. E3.5 声明审计（10 项）

| # | E3.5 声明 | E3.5.1 独立审计 |
|---|---|---|
| 1 | 独立 active/resume/release 事件 | **缺口**：只有 assigned/idle/pressure/purge → 本阶段补 active/retry/resume |
| 2 | idle 当 release，所有 release 路径发出 | ✓（17 处 slot.release() 调用点全覆盖；代码核对无遗漏路径） |
| 3 | retry 独立事件可关联 pressure | **缺口**：retry 仅 pressure 事件字段 → 补独立 retry 事件 + pseq 关联 |
| 4 | purge 同时标识发起者与 victim | **部分**：补 rtrace/strace 分离（发起者）+ victim 由 slot/seq/gen 标识 |
| 5 | candidate 含 slot/seq/gen/trace/tick | **缺口**：仅 candidates 数量 → 补每候选完整字段 |
| 6 | 每请求唯一 request trace id | **缺口**：E3.5 每 session 一个 → 补 request/session trace 分离（每请求唯一 rtrace） |
| 7 | task id 贯穿 assigned→release | **部分**：idle 的 task=-1（release 释放）→ 用 rtrace 关联（request 级） |
| 8 | 缺失字段显式 null | **部分**：used/active 用 -1 → 改 null 字符串 |
| 9 | evseq 全局严格递增 | ✓（trace_evseq 每进程 ++） |
| 10 | drop counter / 丢失检测 | **缺口**：direct_log 无缓冲 → 补 transport=direct_log 声明 + parser gap/duplicate/乱序/malformed 检测 |

**结论：E3.5 有实质契约缺口 → 本阶段 llama.cpp 修正（`aba4b26a`，additive 默认关闭只读）**。

---

## 4. 冻结事件契约（schema=2）

每个事件：`schema=2 transport=direct_log evseq pseq tick type rtrace
strace task slot seq gen state policy last_used_tick used active [extra]`

- 事件类型：`assigned` → `active` → `idle/release`；
  `pressure` → `purge` → `retry`/`resume` → `active/resume` → `idle/release`；
- `rtrace`（request trace，每请求唯一）/ `strace`（session trace）；
- pressure/purge/retry/resume 含 `pseq`（pressure 事件序号，关联链）；
- purge 事件 candidates 每项含 `{s:slot,g:gen,rt:rtrace,st:strace,
  t:tick,u:unique,sh:shared}`（diagnostic_only）；
- null 语义：无快照/无值输出 `null`；禁止靠时间邻近猜测关联。

---

## 5. 验证矩阵（36 正式 replicate + 6 smoke + 40 overhead）

| 场景 | reps | 验证点 |
|---|---|---|
| request_level_mapping | default/lru 各 3 | 每请求 rtrace 的 assigned 映射 |
| chain_pressure | 各 3 | pressure→purge→retry/resume→idle 完整链 |
| active_protection | 各 3 | active 永不成为 victim |
| slot_reuse | 各 3 | generation 递增 |
| malformed_trace | 各 2 | 非法 trace 忽略（rtrace=null） |
| diagnostics_off | 各 2 | 零事件 + 行为不变 |
| non_unified | 各 1 | lru 惰性 |
| overhead 配对 | 20 对（on/off 交替） | 中位/p95 开销 |

---

## 6. request-level 映射覆盖率

- **100%**（request_level_mapping 全部 replicate：每请求 rtrace 至少 1 个
  assigned 事件；min=1.0）——**request 级（非 session 级）**；
- 事件示例：`type=assigned rtrace=trace-S0-0001 strace=sess-S0 slot=0
  gen=1` → `active` → `idle`（同 rtrace 同 slot）。

---

## 7. 完整事件链覆盖率

- **100%**（每请求 assigned→active→idle 全链；chain_pressure 场景
  pressure→purge→retry/resume→idle 全链 6/6）；
- pressure 发起者（rtrace=trace-adp-pres-02）与 victim（purge 事件
  slot/gen + 候选 rtrace）**均可精确归属**。

---

## 8. pressure/victim 归属

- purge 事件 `pseq` 关联到对应 pressure 事件；
- victim 由 slot/seq/gen 标识，其 session 归属由候选 rtrace/strace 给出；
- active protection：active session 的 rtrace 从未出现在 purge 事件
  （active_protection 场景错配 0）。

---

## 9. active protection 与 generation 错配

- **active victim=0**；**generation 错配=0**（相邻 assigned→idle 对 slot
  一致；同 slot 连续请求 gen 单调递增；mismatch=0 全场景）。

---

## 10. 事件完整性计数（integrity parser）

- `transport=direct_log`（无应用层缓冲，evseq 每进程严格递增）；
- parser 检测：**gap=0、duplicate=0、out_of_order=0、malformed=0**
  （全部 trace-on replicate）；
- 无未解释 gap → 满足"任一正式 replicate 存在未解释 gap 不得 READY"
  的反向条件。

---

## 11. diagnostics-off 配对开销（20 对，on/off 交替）

| 指标 | 值 | 判定 |
|---|---|---|
| 对级中位差 | **+0.05%** | ✓（<0.5%） |
| 对级 p95 | +9.34% | ✗（>1%） |
| 请求级中位差（480 对） | **-0.1%** | ✓ |
| 请求级正负分布 | 236 慢 / 244 快（对称） | 无系统性开销 |

- **中位证据充分**：trace 开启无真实开销（对级 +0.05%、请求级 -0.1%）；
- **p95 高为环境噪声**：20 对分布对称（-9.2% 至 +11.7%，正负各半），
  480 请求级差对称（236/244）——**若 trace 有系统性开销，差应单侧为正**；
  p95 受消费级 GPU + 4 并发 session 的时序波动支配（单请求 latency 波动
  可达 ±60%）；
- **门槛结论**：中位满足（<0.5%）；**p95 <1% 在 8GB 消费级 GPU + 并发
  场景无法稳定验证**（测量噪声非 trace 开销）。

---

## 12. 隐私与安全

- schema=2 事件与 schema v1 均无 prompt/response/token 文本/secret/IP/
  身份；validator 递归扫描（测试覆盖）；
- rtrace/strace 受限 ASCII/UUID（非法忽略→null 不拒绝，防日志注入）；
- trace 不参与调度/victim 选择；`diagnostic_only=1` 标记。

---

## 13. 门禁判定（逐项）

| READY_TO_ACCEPT_FIELD_TRACE 门槛 | 判定 |
|---|---|
| request-level mapping coverage=100% | **PASS**（1.0） |
| 完整 lifecycle chain coverage=100% | **PASS**（1.0） |
| pressure initiator 与 victim 可精确归属 | **PASS**（pseq + 候选 rtrace/strace） |
| generation 错配=0、active victim=0 | **PASS** |
| gap/duplicate/乱序/malformed/未解释 drop 全 0 | **PASS** |
| diagnostics-off 配对 median <0.5% | **PASS**（+0.05%） |
| diagnostics-off 配对 **p95 <1%** | **FAIL**（+9.34%，环境噪声） |
| 默认关闭零事件 | **PASS** |
| response/策略/候选/victim 行为不变 | **PASS** |
| schema/validator/replay dry-run 全通过 | **PASS**（E3.5） |
| field trace 未提供时记录 not_provided | **PASS** |

**9/10 门槛满足；唯一未达：p95 开销（环境噪声归因，非诊断缺陷）**。

---

## 14. 最终状态

**`HOLD_FOR_ATTRIBUTION_GAPS`**

- **attribution 全部闭环**：request 级映射 100%、事件链 100%、pressure/
  victim 精确归属、generation 错配 0、active victim 0、完整性计数全 0、
  diagnostics-off 零事件、中位开销 0.05%；
- **唯一未达预注册门槛**：p95 开销 +9.34%（对称分布证明为 GPU/并发
  噪声，非 trace 系统性开销；中位 -0.1% 请求级证据充分）；
- HOLD 依据：严格按任务七门槛（p95 <1% 为 READY 必要条件），p95 未达 →
  不 READY；无 REJECT 触发（无行为/泄露/默认事件问题）。

---

## 15. 下一步精确输入（解除 HOLD 的选项）

1. **用户决策**（二选一）：
   - **接受中位证据**（+0.05%，请求级 -0.1%）放行 field trace 接入
     （需用户明确确认放宽 p95 门槛的理由：对称分布证明无系统性开销）；
   - **更稳定环境复核**（非消费级 GPU / 更少并发）重新测量 p95；
2. 用户提供真实匿名 trace（符合 `schemas/lifecycle_trace_v1.json`）后，
   validator 通过方可进入 E3.6 replay；
3. **不得自动开始 E3.6**（硬前置：用户提供且 validator 通过的真实 trace）。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e351/`（manifest/summary/csv + 36 per-replicate
  JSON + 40 overhead JSON）；
- llama.cpp 提交：`aba4b26a`（fix: complete lifecycle attribution event
  contract，4 文件 +142 行）；根仓库提交：`test: close lifecycle attribution
  replay gate`（hash 见最终汇报）；
- 测试：根 pytest **146 passed**（含 test_e351_contract 9 例）；llama.cpp
  `test_lifecycle_trace` 7/7 + `test_unified_idle_lifecycle` 5/5 +
  `test_slot_routing` 10/10 + E1 5/5；
- 提交后两仓库 `git status --short` 均为空。
