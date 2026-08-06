# E2.3 验证报告：A2 Unified 回访修复、标准架构逻辑收益量化与分支 workload 验证

- 阶段：E2.3（未实现任何新 KV 生命周期策略）
- 日期：2026-08-06
- 状态：**CONDITIONAL**（详见第 17 节）

---

## 1. 阶段目标和禁止边界

- 目标：unified 回访根因诊断与修复、tinyllama 标准架构逻辑收益量化、
  branch workload 验证、正式 workload 复用、测试/报告/提交一次闭环；
- 禁止（未违反）：seq 原语语义、seq_cp、COW、KV buffer、淘汰/回收/分层/
  Context Compression、cache-reuse、decode/batch/采样/tokenizer/memory core、
  multi_turn/long_life workload、OpenAI 响应、unified 共享 pool 宣称为物理
  sharing、active_sequences 宣称为 cache hit、无重复证据宣称稳定性能收益。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp HEAD（本阶段提交前） | `0217843bd`（A2 实现）；本阶段提交见附注 |
| 根仓库 HEAD | `a029ff6`（E2.2）；本阶段提交见附注 |
| server binary | `build-cuda/bin/llama-server`（E2.3 修复后重建） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| tinyllama | `stories260K-f32.gguf`（CPU server，标准架构对照） |
| 工作区 | 两仓库均干净（无未提交用户修改） |

---

## 3. Unified 根因分析（诊断优先，未先改代码）

### 3.1 复现（tinyllama + `--kv-unified` + prefix-branch）

| 步骤 | 现象 |
|---|---|
| A+X | slot1 缓存 75 tokens（LRU 后扫描者胜出） |
| A+Y | 路由到空 slot0（`prefix_branch_empty_slot`），**但 slot1 的 n_prompt_tokens 从 75 变 0** |
| A+X 回访 | 无缓存可命中（cache_n=0、reason=`prefix_branch_empty_slot`、全重算） |

### 3.2 根因（源码 + 对照实验）

- **源码**：`server-context.cpp:2686-2698` `[TAG_IDLE_SLOT_CLEAR]`——`cache_idle_slots`
  （`common.h:611` **默认 true**）在 **unified** 模式下，每个新任务启动时对
  **所有空闲 slot** 执行 `slot.prompt_clear()`（含 `mem.seq_rm(id,-1,-1)` 清 KV）；
- **对照实验（决定性）**：

| 配置（unified + prefix-branch） | AY 后 slot1 | A+X 回访 cache_n | 回访事件 |
|---|---|---|---|
| 默认（cache_idle_slots=true） | **0（被清）** | 0（全重算） | `prefix_branch_empty_slot` |
| `--no-cache-idle-slots` | **75（保留）** | **71（全命中）** | **`prefix_branch_revisit`** |

- **结论**：**根因是 `cache_idle_slots`（unified 分支）与 A2"保留分支"语义冲突**，
  不是路由谓词错误、不是 unified slot 状态语义差异、不是 KV memory 问题；
  prefix-branch 特意保留的分支被 idle-slot 清理机制清除。

---

## 4. 是否修复及修改边界

- **已修复**（修改边界 = slot 保留/选择层，`server-context.cpp` TAG_IDLE_SLOT_CLEAR）：
  ```cpp
  // E2.3: prefix-branch 策略的语义是"保留已有分支"，
  // unified 下清空 idle slot 会破坏分支保留（回访全重算）。
  // 仅在非 prefix-branch 模式下清理，保持 default 行为不变。
  if (params_base.kv_unified && slot_routing_policy != "prefix-branch") {
      slot.prompt_clear();
  }
  ```
- **不越界**：未触碰 seq 原语、KV buffer、memory core、decode、cache-reuse；
  `default` 策略行为完全不变（条件为 `!= "prefix-branch"`）；non-unified 不受影响
  （`kv_unified` 分支）；RAM prompt cache 保存（`prompt_save`）仍执行（无功能损失）。

### 4.1 修复验证（真实 server）

| 环境 | 修复前（E2.2） | 修复后 |
|---|---|---|
| tinyllama unified | 回访 cache_n=0、empty_slot | **回访 cache_n=71、prefix_branch_revisit、slot1 保留** |
| 4B unified | 回访 cached=0、empty_slot、active=1 | **回访 cached=111、prefix_branch_revisit、active=2** |
| 4B non-unified | 正常（8/8） | 正常（无回归） |
| default（4B/tinyllama） | 正常 | 正常（清理行为保留，回归测试覆盖） |

---

## 5. non-unified 结果

- 复用 E2.1（5 reps）+ E2.2（3 reps）：prefix-branch 保留分支 **8/8**、
  default 覆盖 8/8、回访 `prefix_branch_revisit` 回到原分支 slot、无串扰、
  输出 hash 一致（`e30f9baa…`）；
- 本轮 tinyllama 3 reps 复现一致（见第 7 节）。

---

## 6. unified 结果

| 指标（修复后，4B smoke 2 reps / tinyllama 1+1 reps） | default | prefix-branch |
|---|---|---|
| A+Y 路由 | `lcp_similarity`（覆盖） | `prefix_branch_empty_slot`（空 slot，保留） |
| A+X 回访 reason | `lcp_similarity` | **`prefix_branch_revisit`** |
| A+X 回访 cached_tokens | 111 | **111（原为 0）** |
| active_sequences | 1 | **2** |
| used_cells | 双分支 cell 共享（不翻倍，unified 语义） | 同左 |
| 400/错误 | 0 | 0 |
| 输出 hash | 一致 | 一致（跨策略一致） |

**修复后 unified 行为与 non-unified 对齐**（回访命中、分支保留）。

---

## 7. TinyLlama 标准架构量化（本轮新增，3 配置 × 3 reps）

配置：tinyllama（stories260K f32，CPU）、ctx=2048、cr=0、temp=0、seed=42、
non-unified、独立协议（每 replicate 前 erase + 断言）；结果 `results/e23/`。

### 7.1 序列与关键指标

| 配置 | A+Y（自动）cached | A+X 回访 reason | A+X 回访 cached | A+X 回访 prompt_processed | 回访延迟(ms) | active |
|---|---|---|---|---|---|---|
| p1 default | 56（前缀命中+覆盖） | lcp_similarity | 111 | 1 | — | 1 |
| p2 default | 56（前缀命中+覆盖） | lcp_similarity | **111** | **1** | **4.9** | 1 |
| p2 prefix-branch | **0（新分支全重算）** | **prefix_branch_revisit** | **111** | **1** | **4.1** | **2** |

（3 reps 逐 rep 一致：default 延迟 4.9/4.9/4.9ms，prefix-branch 4.1/4.1/4.1ms；
cached=111 为 A+X 全量 token 数。）

### 7.2 关键机制发现：RAM prompt cache 掩盖逻辑差异

- **default 的 A+X 回访 cached=111 并非 KV 直接命中**：A+Y 已覆盖原 slot（active=1），
  回访经 **RAM prompt cache 恢复**（`get_available_slot` 的 `prompt_save`/`prompt_load`，
  `server-context.cpp:1905-1917`）得到相同 token 数；
- **prefix-branch 的 cached=111 是 KV 直接命中**（原分支保留，无恢复开销）；
- **差异体现于延迟**：p2 回访延迟 4.9 → 4.1ms（**-16%，3/3 可复现**）——KV 直接
  命中优于 RAM 状态恢复；
- **cached_tokens 数量相同** → 按任务判定规则，**逻辑缓存收益（token 数层面）
  未证实**；延迟收益为**访问路径成本收益**（性能层面）。

---

## 8. branch workload 结果

- **复用**（任务七：已有 branch probe 满足全部条件——固定 A/X/Y、自动+显式路由、
  X/Y 回访、输出 hash、routing events、independent clean assertion）→ **不新增
  `branch_agent.py`**；
- 汇总（E2.1 5 + E2.2 3 + E2.3 3 reps）：prefix-branch 保留 **11/11**、default
  覆盖 11/11；回访 reason/selected_slot 确定；hash 无 mismatch；无 400。

---

## 9. 正式 workload 复用结果

- **复用** E2.1/E2.2 的 10 配置 × 5 independent replicates（B0/B1/B2-0/B2-1 ×
  multi_turn/long_life，parallel=1；B1/B2-1 × multi_turn parallel=2）；
- parallel=1：四配置 hit 全 0.8529、task_success 1.0（无回归）；
- long_life：hit 0.7114、retention 0（截断根因，一致）；
- parallel=2：B1 0.8585 vs B2-1 0.8573（持平）、p50 458.6 vs 442.7（E2.1 单次观测，
  E2.2/E2.3 未复现，不作为稳定收益）；
- **本阶段未补跑正式 workload**（字段已在 summary 派生，无缺失）。

---

## 10. logical prefix reuse

- 口径：`logical_prefix_reuse_tokens` = OAI `cached_tokens`（= `n_past`，server
  确认的公共前缀；benchmark 侧派生，零生产代码改动）；
- **TinyLlama（标准架构，cached_tokens 可靠）**：default 与 prefix-branch 的
  A+X 回访 `logical_prefix_reuse_tokens` **相同（111）**、`prompt_processed_tokens`
  相同（1）→ **数量层面无差异**（default 经 RAM prompt cache 恢复达到同值）；
- **收益体现**：回访延迟 -16%（KV 直接命中 vs RAM 恢复，3/3 可复现）；
- 4B hybrid：`attention_cached_tokens` = **null**（不可分离 recurrent）。

---

## 11. hybrid 指标限制

- `cached_tokens` = `n_past`（公共前缀），仅可作**逻辑前缀复用数**；
- attention/recurrent 分离：**not_available**（llama.cpp 无 per-part 接口）；
- `/completion` `timings.cache_n`：**最后批次语义**，不作总命中数；
- `used_cells`：仅 attention KV cell 观测；`active_sequences`：不等同 cache hit；
- 路由复用 ≠ COW（shared_cells 保持 E1 语义）。

---

## 12. cache/recompute/KV/延迟

- 复用正式数据：parallel=1 recompute 各配置一致（multi_turn 3313）；parallel=2
  default 1033 vs prefix-branch 1036（±0.3%）；peak_used_cells p1=2032/2048、
  p2=2046/2048；`capacity_bytes` 预分配不变（不作为收益）；
- 本轮 tinyllama：回访延迟 4.9 vs 4.1ms（-16%，可复现）；路由 overhead 为
  负值/可忽略（<2% 阈值）。

---

## 13. 输出 hash/evaluator/串扰

- hash：E2.3 全部 probe 同策略内 first/revisit 一致、跨策略（default ==
  prefix-branch）一致、跨 unified/non-unified 一致（`e30f9baa…`）→ 无串扰、
  策略不改输出；
- evaluator：复用 task_success 1.0（multi_turn）、retention 0（long_life 截断）；
- 请求失败：0（全配置）。

---

## 14. 测试结果

| 测试 | 结果 |
|---|---|
| llama.cpp `test_slot_routing.py`（新增 2 例 unified） | **10/10 passed** |
| llama.cpp E1 `test_metrics_kv.py` | 5/5 passed |
| 可运行子集（completion/basic/ignore_eos/tokenize，--noconftest） | 43 passed / 8 failed（预存测试桩/环境问题，与 E2.3 改动无关，记录） |
| 根仓库 benchmark pytest | **83 passed** |

---

## 15. 未执行项和环境阻塞

- 未执行：8192 扫描、完整 B0/B1 五次基线、完整 llama.cpp pytest（preset 模型
  下载阻塞，记录）；
- 4B 正式 workload 未补跑（复用数据字段已齐）；
- `branch_agent.py` 未新增（branch probe 已满足条件，直接复用）。

---

## 16. 机制、逻辑、端到端三层结论

| 层 | 结论 | 证据 |
|---|---|---|
| 1. 机制 | ✅ **成立** | prefix-branch 保留分支 11/11、default 覆盖 11/11、回访回原 slot（unified 修复后 4B+tinyllama 均 `prefix_branch_revisit`）、无串扰、输出等价 |
| 2. 逻辑收益 | ⚠️ **未证实（数量层面）** | TinyLlama 回访 `logical_prefix_reuse_tokens` 相同（111）、`prompt_processed` 相同（1）——default 经 RAM prompt cache 恢复达到同值；**延迟收益成立**（KV 直接命中 vs RAM 恢复，-16%，3/3 可复现） |
| 3. 端到端 | ❌ **不成立（正式 workload）** | 复用数据：formal hit 无提升（parallel=1 四配置 0.8529 一致）；parallel=2 性能差异单次未复现 |

**判定规则符合性**：unified 已修复且无回归（READY 条件之一满足）；但 TinyLlama
逻辑收益（token 数）未证实、正式 workload 无收益 → 按规则状态为 **CONDITIONAL**
（"non-unified 机制成立，但正式 workload 无收益或 hybrid 指标仍有限"）。

---

## 17. 最终状态

**CONDITIONAL**

- ✅ unified 回访已修复（根因 = cache_idle_slots 的 unified idle-slot 清理与 A2
  保留语义冲突；修复限定 slot 保留层，default/non-unified 零回归）；
- ✅ 机制成立（分支保留 11/11、回访回原 slot、无串扰、输出等价）；
- ⚠️ TinyLlama 逻辑缓存收益（token 数）未证实——RAM prompt cache 恢复使
  default 达到相同 cached_tokens；**访问路径收益**（回访延迟 -16%）可复现；
- ⚠️ 正式 workload 无端到端命中收益（复用数据）；
- 无 BLOCKED 触发项（无 hash 回归、无串扰、无失败回归、策略确定、未越界）。

---

## 18. 后续建议

1. **RAM prompt cache 与 A2 的关系**：量化"KV 直接命中 vs RAM 状态恢复"在
   4B 大模型上的延迟/带宽差异（tinyllama 已示 -16%）——这是 A2 真实价值的
   候选来源；
2. **长会话下 RAM cache 失效**：prompt cache 容量有限（`--cache-ram`），
   长 Agent 会话下 default 的恢复可能失效而 prefix-branch 的 KV 保留不受限——
   构造长分支会话验证；
3. **branch 型正式 workload**：新增含分支回访的正式场景（不改现有 workload），
   使端到端收益可测；
4. **hybrid per-part 统计**：扩展 E1 区分 attention/recurrent（additive 默认关闭）。

---

## 附注：执行记录、结果路径与提交状态

- 结果：`benchmark/results/e23/`（summary.json/csv + manifest.json + 4B unified
  smoke + tinyllama 7 个 probe JSON + server 日志）；复用 `e21/`、`e22/`；
- 提交：llama.cpp `fix: validate prefix-aware routing across memory modes`（修复
  + 测试，hash 见最终汇报）；根仓库 `docs: validate A2 unified and standard
  architecture results`（报告 + 脚本，hash 见最终汇报）；提交后两仓库干净；
- 实际执行命令摘要：
  - `ninja -C build llama-server` / `ninja -C build-cuda llama-server`
  - `python3 -m pytest unit/test_slot_routing.py --noconftest`（10/10）
  - `python3 tmp/run_e1_manual.py`（5/5）
  - `cd benchmark && uv run pytest -q`（83 passed）
  - `bash scripts/e2_scan_tl.sh` + 手动补跑（tinyllama 3 配置）
  - `uv run python scripts/e2_a2_probe.py`（4B unified smoke 2 reps）
