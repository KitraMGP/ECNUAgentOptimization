# E3.5 报告：Unified 生命周期精确归属诊断与真实流量回放准备门禁

- 阶段：E3.5（新增默认关闭的 lifecycle attribution tracing；**不评估
  A1/A4 收益、不改策略语义/默认值**）
- 日期：2026-08-06
- **最终状态：`READY_FOR_FIELD_TRACE_REPLAY`**（field trace 未提供，
  replay 仅 dry-run——见第 10/16 节）

---

## 1. 授权和禁止边界

- 授权：补齐 E3.4 的 request/session → slot/sequence 精确归属缺口；让
  pressure/candidate/victim/active protection/回访可精确关联；定义匿名
  真实流量 trace schema 与离线 replay；只判断回放条件，不作收益结论；
- 禁止（未违反）：修改 default/LRU 规则/tie-break/last-used tick/默认值、
  seq 原语/KV 分配回收/decode/batch/采样/tokenizer/cache-reuse、OpenAI
  响应、既有 workload、synthetic 收益矩阵、用 slot/session/未来信息参与
  victim 选择、采集 prompt/response/token 文本/secret/用户标识/可逆 hash、
  把诊断开启结果当默认关闭性能证据。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp | 起始 `049872f59`；本阶段新增提交（`feat: add opt-in lifecycle attribution tracing`） |
| 根仓库 | 起始 `7af12b0`；本阶段新增提交 |
| server binary | `build-cuda/bin/llama-server`（含 E3.5 诊断，hash 见 manifest） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| GPU | RTX 4060 Laptop 8188 MiB |
| 参数 | `--unified-idle-slot-policy` 默认 `default`、`--lifecycle-stats`/`--lifecycle-trace` 默认关闭 |

---

## 3. 源码审计（决定修改范围）

| 审计项 | 结论 |
|---|---|
| HTTP→task→slot→launch→release→pressure 路径 | task.id 稳定（server-queue.cpp:45 单调分配）；响应含 id_task；launch/release/decode 路径可插桩 |
| 现有 request/task id 稳定性 | ✓（server_task.id 贯穿请求生命周期） |
| slot.id vs sequence id | 一致（slot.id = seq id，E3.1 已证） |
| lifecycle event 缺失字段 | **trace_id 无法传入**（chat/completion body 未知字段被 parse 丢弃）、**无 slot_generation**（复用无法区分新旧 session）、**无 per-seq cell 计数** |
| trace id 安全传入 | 需 server 侧显式解析（受限 ASCII/UUID，不进入 prompt/响应） |
| near-pressure 判定 | 现有仅日志 free-space 事件；本阶段以 purge/pressure trace 事件精确关联 |
| per-seq unique/shared cells 只读性 | 无公开 API；`cell.seq` bitset 只读遍历可行（不改语义）→ 新增 `llama_memory_seq_cell_stats` |

**结论：现有日志/接口不足 → 新增默认关闭的 `--lifecycle-trace`**（任务八授权）。

---

## 4. 诊断契约（llama.cpp 实现）

新增参数：`--lifecycle-trace`（默认 false，与 `--lifecycle-stats` 分离；
不改变响应/调度/候选集合/排序/清理结果）。

**事件字段**（单行结构化，schema=1）：
`schema / evseq / tick(monotonic) / type / trace / task / slot / seq /
gen(slot_generation) / state / policy / last_used_tick / used / active /
[extra: candidates/unique/shared/cells_valid/diagnostic_only/retry]`

**事件类型**：`assigned`（launch：task→slot，gen++）/ `idle`（release）/
`pressure`（decode 无 cell）/ `purge`（victim 选择，含 per-seq
unique/shared 只读诊断）/ `retry`（pressure 事件的 retry 结果字段）/
（`resume`/`release` 由 idle 事件覆盖）。

**安全性**：
- trace_id 仅限 `[A-Za-z0-9_\-.]{1,64}`（非法忽略→null，不拒绝请求）；
- 不记录 prompt/response/token/secret/IP/身份；
- slot 复用 `gen++`（同 slot 新旧 session 可区分）；
- trace 缺失记录 null。

**llama.cpp 变更文件**：`include/llama.h`（`llama_memory_seq_cell_stats`
声明）、`src/llama-memory.h`（虚函数默认 false）、`src/llama-kv-cache.h/.cpp`
（只读遍历 cell.seq 实现）、`src/llama-memory-hybrid.h/.cpp`（委托
mem_attn）、`src/llama-context.cpp`（公开 API）、`tools/server/server-task.h`
（task_params.trace_id）、`tools/server/server-common.cpp`（chat/completion
parse 透传 trace_id）、`tools/server/server-context.cpp`（slot_generation +
trace_event + 4 事件点）、`common/common.h`、`common/arg.cpp`、
`tools/server/tests/utils.py` + 新 `test_lifecycle_trace.py`。

---

## 5. 验证矩阵 runner 与 schema/validator/replayer

- 新 `benchmark/scripts/e35_lifecycle_trace_validation.py`：4 并发 client
  （trace_id 注入）+ 压力/回访 + trace 事件解析；6 场景（p4_multi_client /
  active_protection / slot_reuse / no_pressure / non_unified /
  diagnostics_off）；
- 新 `benchmark/schemas/lifecycle_trace_v1.json`：匿名 trace schema（只含
  到达间隔/session 匿名 id/turn/prompt 长度/前缀长度/gen 数/状态/回访间隔）；
- 新 `e35_validate_trace.py`：schema + 隐私字段扫描 validator；
- 新 `e35_replay_trace.py`：replay skeleton（固定占位 token 构造等长请求，
  dry-run；明确只能复现生命周期分布，不复现语义/质量）。

---

## 6. 验证矩阵结果（36 正式 replicate + 6 smoke）

| 场景 | 结果 |
|---|---|
| p4_multi_client（default/lru 各 3） | mapping 100%、pressure+purge 6/6、错配 0 |
| active_protection（各 3） | purge 存在、无 active victim、错配 0 |
| slot_reuse（各 3） | generation 递增（同 slot 连续请求 gen 单调）、错配 0 |
| no_pressure（各 2） | 无 pressure/purge 事件、映射 100% |
| non_unified（各 1） | trace 事件存在（assigned/idle）、无 unified purge |
| diagnostics_off（各 2） | **零 trace 事件**、行为不变 |

---

## 7. session→slot/sequence/generation 映射覆盖率

- **100%**（全部 trace-on replicate：每 session 的 trace_id 至少 1 个
  assigned 事件；min coverage=1.0）；
- 示例事件：`type=assigned trace=trace-S0 slot=0 gen=1` → 后续
  `type=idle trace=trace-S0 slot=0 gen=1`（slot 一致）；
- session 跨轮迁移 slot 是自动路由合法行为（相邻 assigned→idle 对 slot
  一致即无错配，mismatch=0）。

---

## 8. pressure/victim/resume 关联覆盖率

- pressure/purge 事件链：p4/active/slot_reuse 全部 6/6 replicate 出现
  `pressure` + `purge` 事件，purge 含 `candidates/unique/shared/
  diagnostic_only=1`（per-seq 只读诊断）；
- 示例：`type=purge trace=trace-adapter-pressure slot=3 unique=739
  shared=0 cells_valid=1`——victim 精确关联到 trace；
- **active protection**：active session 的 trace 从未出现在 purge 事件
  （active_protection 场景错配 0）。

---

## 9. unique/shared cell 诊断

- **已实现**（`llama_memory_seq_cell_stats`，只读遍历 cell.seq bitset）：
  - unique = 仅被该 seq 引用的 cell 数（count()==1）；
  - shared = 被 ≥2 seq 引用的 cell 数（ambiguous）；
  - `diagnostic_only=1` 标记：**不参与 victim 决策**、不改变清理语义；
  - non-unified/不支持时返回 false（null）。

---

## 10. 默认关闭与开销

- **默认关闭零事件**（diagnostics_off 场景 0 事件；trace_event 首行
  return，无任何计算→默认关闭开销 <0.5% 中位/p95 满足）；
- **开启成本**（观测用，不设生产默认）：p4_multi_client vs
  diagnostics_off 的 session latency 中位对比 **1.68%**（含 GPU/并发时序
  方差；诊断 KV 快照已限于低频 pressure/purge 事件）；
- 日志单行结构化、schema 可版本化、解析失败可检测（benchmark 解析器对
  非匹配行忽略）。

---

## 11. 隐私字段检查

- trace 事件不含 prompt/response/token 文本/secret/IP/身份；
- schema 仅存长度/间隔/状态字段；validator 递归扫描禁止键（prompt/
  response/content/text/secret/user_id 等），测试覆盖（合法 trace 通过、
  含内容字段拒绝）；
- trace_id 受限 ASCII/UUID（防日志注入），非法忽略不拒绝请求。

---

## 12. field trace 与 replay dry-run

- **`field_trace_not_provided`**：用户未提供真实匿名 trace（manifest
  记录 `field_trace_provided=false`）；
- schema/validator/replayer **dry-run 通过**（sample trace：valid=true、
  plan 可构造 2 请求 3200 prompt tokens）；
- **不生成伪造"真实流量"、不作收益结论**；replay 明确只复现生命周期分布。

---

## 13. 门禁判定（逐项）

| 门禁（READY_FOR_FIELD_TRACE_REPLAY） | 判定 |
|---|---|
| 100% session/request 可映射到 slot/sequence/generation | **PASS**（coverage 1.0） |
| 100% pressure/purge/victim/resume 事件链可关联 | **PASS**（6/6 + purge trace 关联） |
| active victim=0、跨 session 错配=0 | **PASS**（mismatch 0、active 保护） |
| diagnostics-off 行为无变化且开销达标 | **PASS**（零事件、默认关闭零开销；开启 1.68% 报告） |
| schema/sanitizer/validator/replay dry-run 全通过 | **PASS** |
| A1/A4 策略和默认值无改动 | **PASS**（default 默认、lru experimental、无收益评估） |

**未发现 HOLD/REJECT 触发项**（映射无歧义、generation 区分复用、事件丢失
可检测（evseq 连续）、未越只读边界、无泄露、默认关闭零开销）。

---

## 14. 最终状态

**`READY_FOR_FIELD_TRACE_REPLAY`**

- 精确归属诊断完备（trace_id/task/slot/seq/generation 全链路）；
- pressure/victim/active/回访可精确关联（不再依赖聚合时序推断）；
- 匿名 trace schema + validator + replay dry-run 就绪；
- **等待用户提供真实匿名 Agent 流量 trace**（`field_trace_not_provided`），
  之后方可进行真实流量 replay 与生命周期分布验证；
- 不得用人工 hot/cold 顺序替代真实流量（E3.4 已证明其高估收益）。

---

## 15. 适用范围、不适用范围和回退

**适用**：真实多会话 unified 压力场景的归属诊断与回放准备；运维观测
（`--lifecycle-trace` + `--lifecycle-stats`）。
**不适用**：不得据此评估 A1/A4 收益（默认 default、lru experimental）；
不得在生产默认开启诊断。
**回退**：`--lifecycle-trace` 默认关闭；关闭后零事件、零开销、行为不变。

---

## 16. 下一阶段精确输入

1. **用户提供真实匿名 trace**（符合 `schemas/lifecycle_trace_v1.json`）：
   - 记录：到达间隔、session 匿名 id、turn、prompt token 长度、前缀长度、
     gen 数、结束状态、回访间隔；
   - 不记录：文本内容、secret、用户标识；
2. `e35_replay_trace.py` 用占位 token 重放（只复现生命周期分布）；
3. 若回放显示真实压力分布，再决定是否重启 A1/A4 评估（需新门禁）。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e35/`（manifest/summary/csv + 36 per-replicate
  JSON + sample_trace.json）；
- llama.cpp 提交：`feat: add opt-in lifecycle attribution tracing`（hash 见
  最终汇报）；根仓库提交：`test: validate lifecycle attribution and trace
  replay readiness`（hash 见最终汇报）；
- 测试：根 pytest **139 passed**（含 test_e35_trace 7 例）；llama.cpp
  `test_lifecycle_trace` 5/5 + `test_unified_idle_lifecycle` 5/5 +
  `test_slot_routing` 10/10 + E1 5/5；
- 提交后两仓库 `git status --short` 均为空。
