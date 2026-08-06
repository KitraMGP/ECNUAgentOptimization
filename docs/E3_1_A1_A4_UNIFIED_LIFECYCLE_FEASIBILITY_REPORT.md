# E3.1 报告：A1/A4 Unified Idle-Sequence 生命周期候选 —— 可行性与可观测性门禁

- 阶段：E3.1（仅可行性/可观测性/实验门禁；**未实现任何回收策略**）
- 日期：2026-08-06
- **最终状态：`READY_TO_IMPLEMENT_A1A4`**（依据见第 15/16 节）

---

## 1. 授权和范围

- 用户授权：正式选择候选 **A1/A4**（unified 容量压力下的 idle-sequence
  价值感知回收）；本阶段仅批准可行性、可观测性和实验门禁设计；
- 本阶段未实现生产策略；未修改 `try_clear_idle_slots` 行为；未新增
  seq_rm/seq_keep/seq_cp 调用；未修改 seq 原语/KV memory core/清理语义；
- 允许的 additive 诊断：未新增（复用日志 + `/metrics/kv` 足够，见第 5 节）。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp HEAD | `2f851c4b4`（本阶段无改动） |
| 根仓库 HEAD | `bcaab3b`（含 `3f6867c` E3.0） |
| server binary | `build-cuda/bin/llama-server` sha256 `74a6b18b…`（对应 `2f851c4b4`） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| GPU | RTX 4060 Laptop 8188 MiB（空闲 40 MiB 起步，4B 加载后 ~3.2 GB） |
| 参数 | `--kv-unified`、`--parallel 2/4`、`--ctx-size 8192`、`--cache-ram 0`（避免 restore 掩盖） |
| e21-e25 | manifest 完整（E3.1 复用协议与基线） |

---

## 3. 生命周期源码语义（审计证据）

| # | 事实 | 证据 |
|---|---|---|
| 1 | slot 的 seq id = slot.id（unified 下 n_seq_max=LLAMA_MAX_SEQ=256） | `llama-context.cpp:1727`；`server-context.cpp:2436`（batch.seq_id vs slot.id） |
| 2 | processing→idle：`is_processing() = state != SLOT_STATE_IDLE`；请求完成后 release 置 IDLE，**KV 保留** | `server-context.cpp:448-451`；E2.0 审计（release 不清 KV） |
| 3 | unified 下 per-seq 上限 = 总 ctx（非平分）；所有 seq 共享同一 cell 池 | `llama-context.cpp:287-290`；实测 n_ctx_slot=8192（ctx 8192 p2 unified） |
| 4 | cell 所有权：`cell.seq[i]` bitset（记录引用该 cell 的 seq id 集合）；删除 seq 只释放"仅被该 seq 引用"的 cell | `llama-kv-cells.h:32-240` |
| 5 | `try_clear_idle_slots()`：仅 `kv_unified`；遍历 slots 跳过 `is_processing()`；选第一个 `prompt.n_tokens()>0` 的 idle slot → `prompt_clear()`；**一次清 1 个** | `server-context.cpp:1932-1951` |
| 6 | `prompt_clear()` = `mem.seq_rm(id,-1,-1)`（删除该 seq 全部 KV）+ `prompt.clear()` —— **整 seq 删除，非区间** | `server-context.cpp:293-298` |
| 7 | 压力检测：`llama_decode` ret≠0 → n_batch==1&&ret==1 → "Context size has been exceeded."（报错所有 processing slots）；否则 `try_clear_idle_slots()`（成功重试）→ 失败 n_batch/=2 重试 | `server-context.cpp:3922-3964` |
| 8 | RAM cache 与清理先后：get_available_slot 中 prompt_save→prompt_load→update（1907-1921）；idle-slot 清理（2686-2704）先 save 再 `prompt_clear`（仅 `kv_unified && policy!="prefix-branch"`，E2.3 修复）；`cache_ram==0 → cache_idle_slots=false`（1542）→ **本阶段 cache-ram 0 下 idle slot 不被自动清，KV 保留** | `server-context.cpp:1542, 1902-1921, 2686-2704` |
| 9 | truncation/ctx shift（slot 自身 n_past 超上限时的 seq 头部清理）与 unified cell 池满（decode ret=1）是**两条不同压力路径**；本阶段构造的是后者 | 上表 3+7 |
| 10 | **hybrid 前缀部分匹配会清空 slot 缓存**：4B 下新请求与缓存公共前缀 < 全长时 n_past=0 并 seq_rm 全清（smoke 实测 B_base 后 used 3225→155）→ idle 缓存的保留窗口受"无前缀竞争请求"约束 | E2.2 结论 + 本阶段 smoke |

**E3.0 草案假设校验**：草案 H（LRU 淘汰可降 truncation/400 且保真不降）的
**机会前提成立**（idle seq 保留 cells、压力触发、现有 try_clear_idle_slots
已能清 idle 解除压力）；但"LRU 价值排序是否优于清第一个"的**收益假设未在
本阶段验证**（属实现阶段 E3.2 的证伪对象）。

---

## 4. idle/active/reclaimable 定义（冻结）

| 概念 | 定义 | 本阶段判定依据 |
|---|---|---|
| `idle_slot` | 当前不处理请求（state==SLOT_STATE_IDLE）且无排队任务 | `is_processing()` 取反 |
| `idle_sequence` | 仍占用 unified cells、当前不参与 decode 的 seq（slot.id 标识） | seq 与 slot 一一对应（事实 1） |
| `reclaimable_cells` | 删除该 idle seq 可释放的**唯一归属** cells（cell.seq 仅含该 seq） | purge 日志 "with %zu tokens" + used_cells 差分；**非全部 used_cells** |
| `shared_or_ambiguous_cells` | 被 ≥2 seq 引用的 cell（seq.count()>1）——不可安全归属 | `cell.seq[i]` bitset count（源码）；本阶段场景无共享（各 seq 独立内容） |
| `pressure_event` | server 因无可用 cell 进入 retry/purge 分支 | 日志 "failed to find free space in the KV cache" / "retrying with smaller batch size" / "purging slot" |
| `victim_age_ms` | seq 最后使用至压力事件的时间 | 本阶段用请求顺序近似（早期请求 = 更久未用） |
| `restore_available` | RAM prompt cache 是否可恢复被清理状态 | `--cache-ram 0` → 不可恢复（本阶段正式条件） |
| `oracle_reclaimable` | 离线分析认为压力时存在安全 idle victim 且释放量足够 | e31_summarize.py oracle（used_cells 差分） |

**observability gap 记录**：HTTP 层无 per-seq cell 精确计数接口（只有
`/metrics/kv` 聚合值）；本阶段用**日志 purge token 数 + used_cells 差分**
推断归属（机会判定足够）；若 E3.2 需要 per-seq 精确 cell 计数，需 additive
默认关闭诊断（任务五允许），届时按 `HOLD_FOR_OBSERVABILITY` 流程补。

---

## 5. 可观测性评估

- 复用：`GET /metrics/kv`（used_cells/active_sequences/capacity_cells）+
  server 日志（SLT_WRN 级：purge/retry/free-space 事件）；
- **现有数据足以回答任务核心问题 1-4**（压力事件、idle victim、回收充分性、
  active 保护）——**未新增任何 llama.cpp 诊断**；
- 无需新增 `/lifecycle/events`（本阶段无此需求；E3.2 实现价值感知时再评估
  per-seq 计数诊断）。

---

## 6. probe 和校准

- 新增 `benchmark/scripts/e31_unified_pressure_probe.py`（不改正式 workload）：
  - 固定 prompt（CHUNK 重复 + END STATE 标记），temperature=0、seed=42；
  - 场景：`p2_single`（slot0 长状态 idle + slot1 长请求）、`p4_multi`
    （3 idle + 最后大请求）、`active_protection`（2 长 + 1 短 idle +
    最后大请求）、`non_unified`（p2 同序列对照）；
  - 每请求记录：slot/status/prompt_tokens/cached/latency/used_cells/
    active_sequences/response_hash/fidelity；每 replicate 记录 server_pid、
    clean 断言、日志事件计数、server 退出确认；
- 校准（不计入正式）：prompt 长度系数 3.9→6.2 chars/token（smoke 实测
  prompt 3210→5094 tokens 精确）；ctx 固定 8192（唯一正式 ctx，unified
  per-seq 上限 = 8192 确认）；去掉会摧毁 idle 缓存的中介请求（hybrid
  前缀部分匹配 seq_rm 问题）。

---

## 7. parallel=2/4 结果（正式矩阵，每场景 3 个 server-per-replicate）

### p2_single（unified, 3/3）

| rep | clean | A_long | B_long | free_space | purge | retry | exceed |
|---|---|---|---|---|---|---|---|
| 0 | ✓ | ok/5094→5109 cells | ok/5094→5109 cells | 1 | 1 | 1 | 0 |
| 1 | ✓ | 同上 | 同上 | 1 | 1 | 1 | 0 |
| 2 | ✓ | 同上 | 同上 | 1 | 1 | 1 | 0 |

- A（slot0）idle 保留 5109 cells；B（slot1）请求 5094 → 池满
  （5109+5094 > 8192）→ **pressure event → try_clear_idle_slots 清 A →
  B 重试成功**（3/3 可复现）。

### p4_multi（unified, 3/3）

| rep | P0 | P1 | P2 | P3 | free_space | purge | retry |
|---|---|---|---|---|---|---|---|
| 0-2 | ok/2063 | ok/4126 | ok/6189 | ok/7173 | 2 | 2 | 2 |

- 3 个 idle（P0/P1/P2 各 ~2063 cells 累积）；P3（5000）遇 **2 次压力 →
  清 2 个 idle victim → 成功**（used 7173）——**多个可选 idle victim 存在**。

### active_protection（unified, 3/3）

| rep | AP0 | AP1 | AP3_short | AP2_long | free_space | purge |
|---|---|---|---|---|---|---|
| 0-2 | ok/2063 | ok/4126 | ok/4358 | ok/7405 | 1 | 1 |

- 3 个 idle + 最后 AP2（5000）；压力时 **purge 只清 idle（非 active）slot**
  （源码 is_processing 跳过 + 实测 AP2 完成后 active 请求无串扰）。

---

## 8. pressure event 证据

- 3 场景 × 3 reps 全部出现：
  `failed to find free space in the KV cache`（9 次总事件中 p2 3 + p4 6 +
  active 3）、`retrying with smaller batch size`、`purging slot N with NNNN
  tokens`；
- `Context size has been exceeded` = **0**（全部经 purge 恢复，无 400/报错）；
- 事件时序（日志）符合源码路径 7：decode ret≠0 → try_clear_idle_slots →
  n_batch/=2 重试 → 成功。

---

## 9. idle victim 和 active protection

- **idle victim 存在且可安全识别**：p2（A）、p4（P0/P1/P2 中清 2 个）、
  active_protection（3 idle 中清 1 个）；victim = 非 processing 且有
  prompt 的 slot（源码事实 5）；
- **active 保护**：`is_processing()` 跳过（源码）+ active_protection 场景
  实测无 active 被清；任务"现有清理路径不会选择 active sequence"确认。

---

## 10. oracle 收益上限（离线，未执行清理伪装 treatment）

| 场景 | pressure 时刻已处理 | 剩余需求 | reclaimable（idle victim） | sufficient |
|---|---|---|---|---|
| p2_single | ~3090/5094 | ~2004 | A 的 ~5109 cells | ✓ |
| p4_multi（×2 次） | ~2100 + ~2100 | ~2900 总 | P0+P1 ~4126 cells | ✓ |
| active_protection | ~3842/5094 | ~1252 | idle ~4358 cells | ✓ |

- **reclaimable 均足以解除压力（3/3）**；被清分支未来回访需重算
  （A 回访重算 ~5094 tokens vs 保留时全命中）——"解除当前压力收益"（B
  完成 vs 400）与"未来回访损失"（A 重算）的权衡是 E3.2 价值感知策略的
  核心对象；**oracle 只证明候选上限，不作为实现收益**。

---

## 11. non-unified 对照

- non-unified p2 smoke：A_long（5094 tokens）直接 **http_400**
  （per-seq 上限 4096 = ctx/2），无 purge/retry/free-space 事件；
- **结论：机会为 unified 特有**（共享 cell 池 + per-seq 上限=总 ctx）；
  non-unified 下无跨 slot 回收路径（try_clear_idle_slots 直接 return）。

---

## 12. 正确性和性能基线

- 全部 12 个正式 replicate（p2/p4/active ×3）+ smoke：`clean_verified=true`
  （used_cells==0 && active_sequences==0 启动断言）、server_exited=true、
  fidelity_ok=true（全部请求 status=ok 且响应非空）、无 contamination、
  无 400/截断/报错；
- 性能基线（default 现有行为）：A_long prefill ~2.0s/5094 tokens（~2.5k
  tps prefill）、B_long 经 purge 后完成 ~2.0s——**现有 try_clear_idle_slots
  的"清第一个"基线**，E3.2 的价值感知策略以此为对照。

---

## 13. 指标限制

- `used_cells` 仅表 attention KV cell（hybrid recurrent state 不可观测）；
- `cached_tokens` 在 4B hybrid 下不可靠（E2.2 结论），本阶段不用于
  机会判定（用日志事件 + used_cells 差分）；
- per-seq 精确 cell 计数无 HTTP 接口（observability gap，见第 4 节）；
- 本阶段不将 any used_cells 下降直接宣称端到端收益。

---

## 14. 测试和阻塞

- 根仓库：`uv run pytest -q` → **107 passed**（新增 `test_e31_probe.py`
  7 例：prompt 确定性/长度、grep_log 解析、oracle 充分/不足/null、
  active protection 跳过逻辑）；
- llama.cpp：`test_slot_routing` **10/10**、`tmp/run_e1_manual.py` **5/5**
  （复用 E2.5 基线，本阶段无 llama.cpp 改动，不建空提交）；可运行子集
  43 passed（8 failed 预存环境问题）；
- 阻塞：无新增阻塞；完整 llama.cpp pytest 仍受 preset 模型下载限制
  （记录，不伪造通过）。

---

## 15. 对门槛逐项判定（预注册 9 条）

| # | 门槛 | 判定 |
|---|---|---|
| 1 | unified 正式 probe ≥3/3 真实 pressure event | ✓（p2/p4/active 各 3/3） |
| 2 | pressure 时存在非 active idle sequence | ✓（p2: A；p4: 3 个；active: 3 个） |
| 3 | idle victim cell 所有权可可靠识别 | ✓（cell.seq[id] bitset + 日志 purge token 数 + used_cells 差分；共享 cell 本阶段无） |
| 4 | oracle reclaimable 足以解除压力，3/3 | ✓（p2 ~5109≥2004；p4 ~4126≥2900；active ~4358≥1252） |
| 5 | active sequence 可明确排除 | ✓（is_processing 跳过 + active_protection 实测） |
| 6 | baseline evaluator 通过、contamination=0 | ✓（fidelity 全过、无 400/截断） |
| 7 | non-unified 无同类跨 slot 机会 | ✓（400 超限、无 purge/free-space） |
| 8 | 实现可限制在 server-context 生命周期策略层 | ✓（try_clear_idle_slots 即该层，1932-1951） |
| 9 | 不需改 seq 原语或 KV memory core | ✓（现有 seq_rm/prompt_clear 已够） |

**全部满足 → `READY_TO_IMPLEMENT_A1A4`**（非仅凭 used_cells 高：真实
pressure 事件 + purge 恢复 + oracle 充分性 + active 保护 + non-unified
对照，证据链完整）。

---

## 16. 最终状态

**`READY_TO_IMPLEMENT_A1A4`**

- 机会实证成立：unified 下 idle sequence 保留可观 cells、active 请求
  遭遇真实 cell 压力、idle cells 在关键路径上构成可回收对象、回收足以
  解除压力（oracle 3/3）、active 可保护；
- 未实现任何回收策略（边界遵守）；
- 下一阶段（E3.2）范围：在 server-context 生命周期策略层实现
  `try_clear_idle_slots` 的价值感知选择（LRU/最小价值），以现有
  "清第一个"行为为 baseline 对照，证伪/证实 H 假设（价值排序优于
  顺序清除的收益 + 保真不降）。

---

## 17. 下一阶段精确边界（E3.2 输入）

- **允许**：修改 `server-context.cpp` 的 `try_clear_idle_slots`（价值感知
  victim 选择）；新增 additive 默认关闭的 per-seq 诊断（如需精确计数）；
  benchmark 侧统计与测试；
- **禁止**：seq 原语语义、KV buffer/memory core、decode/batch/采样/
  tokenizer、cache-reuse、OpenAI 响应、既有 workload、COW/分层/压缩；
- **协议**：复用本阶段 server-per-replicate + 配对区组 + 预注册阈值
  （E3.2 需先冻结：victim 选择正确性、回访损失、保真、400/truncation
  发生率）；
- **回退**：feature flag 默认关闭，关闭后恢复现有 `try_clear_idle_slots`
  原行为。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e31/`（manifest/summary/csv + p2/p4/active/
  non_unified JSON + smoke）；
- 新文件：`benchmark/scripts/e31_unified_pressure_probe.py`、
  `benchmark/scripts/e31_summarize.py`、`benchmark/tests/test_e31_probe.py`、
  `docs/E3_1_A1_A4_UNIFIED_LIFECYCLE_FEASIBILITY_REPORT.md`；
- llama.cpp：本阶段无改动（`2f851c4b4` 保持）；
- 根仓库提交：`docs: gate unified idle sequence lifecycle candidate`
  （hash 见最终汇报）；提交后两仓库 `git status --short` 均为空；
- 实际执行命令摘要：`e31_unified_pressure_probe.py`（4 场景）、
  `e31_summarize.py`、`uv run pytest -q`（107 passed）、
  `pytest unit/test_slot_routing.py --noconftest`（10/10）、
  `tmp/run_e1_manual.py`（5/5）。
