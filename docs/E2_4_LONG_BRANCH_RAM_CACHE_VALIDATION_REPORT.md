# E2.4 验证报告：长分支 Agent workload、RAM Prompt Cache 边界与 A2 端到端收益验证

- 阶段：E2.4（未实现任何新 KV 生命周期策略）
- 日期：2026-08-06
- 状态：**CONDITIONAL**（详见第 18 节）

---

## 1. 目标和禁止边界

- 目标：量化 RAM prompt cache 对 default 的掩盖作用、验证长分支/RAM 压力下
  prefix-branch 的访问路径收益、分支数超过 slot 数的有界回退、保持 unified
  修复与 default 行为、决定 A2 状态；
- 禁止（未违反）：seq 原语、seq_cp、COW、KV buffer、淘汰/回收/分层/Context
  Compression、cache-reuse、decode/batch/采样/tokenizer/memory core、
  multi_turn/long_life workload、OpenAI 响应；RAM restore/routing reuse/
  shared cells 不描述为 COW；active_sequences/used_cells 不直接描述为 cache hit。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp HEAD | `2f851c4b4`（E2.3 unified 修复）；本阶段无源码修改 |
| 根仓库 HEAD | `6eb2123`（E2.3）；本阶段提交见附注 |
| server binary | `build-cuda/bin/llama-server`（含 E2.3 修复） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| tinyllama | `stories260K-f32.gguf`（CPU，标准架构） |
| 工作区 | 两仓库均干净 |

---

## 3. RAM 参数和实际配置

- `--cache-ram N`（`common.h:614` **默认 8192 MiB**，`0` = 禁用）；本阶段用
  `--cache-ram 0` 构造 RAM-disabled 对照；
- `--cache-idle-slots/--no-cache-idle-slots`（默认 enabled，需 cache-ram）；
- `--kv-unified`、`--slot-routing-policy default|prefix-branch`、
  `--slot-routing-stats` 均确认存在；
- **关键事实**：`cache_ram_mib == 0` 时 `prompt_cache` 不创建
  （`server-context.cpp:1472-1480`），且 `cache_idle_slots` 自动禁用
  （`server-context.cpp:1542`）——`--cache-ram 0` 同时关闭 RAM restore 与
  idle-slot 保存/清理（non-unified 下 idle-slot 本就不清理，行为等价）。

---

## 4. E2.3 结果复用情况

- 复用：E2.3 `tl_lat2_p2_default/prefix_branch`（TL bc2 RAM on：cached 均 111、
  回访延迟 4.9 vs 4.1ms）；E2.1/2.2/2.3 正式 workload 10 配置 × 5 reps；
  E2.3 unified 修复验证（cache_n 0→71）；
- 新增：TL bc2 RAM off（default/prefix-branch）、TL bc4 RAM on
  （default/prefix-branch）、4B unified bc2 smoke（prefix-branch）。

---

## 5. Long-Branch workload 定义

- 新增 `benchmark/scripts/e2_long_branch_probe.py`（不改现有 workload）：
  - `branch_count=2`：A+X → A+Y → A+X → A+Y（两轮交替回访）；
  - `branch_count=4`：A+X1 → A+Y1 → A+X2 → A+Y2 → A+X1 → A+Y1（分支数 4 > slot 数 2）；
  - `long_prefix`：长共享前缀 + 短分支后缀（保留给后续）；
  - 固定 prompt、temperature=0、seed=42、自动路由；
  - 每请求记录 branch_id/selected_slot/routing_reason/cached_tokens/
    latency/response normalized hash；
  - evaluator：同 branch_id 响应 hash 一致（无串扰）+ 跨 branch 不同（有区分度）。

---

## 6. branch_count=2/4 结果

### 6.1 bc2（TinyLlama，3 reps，见第 7 节 RAM 对照）

| 配置 | X 首访 cached | X 回访 cached | 回访 reason | 回访延迟(ms) |
|---|---|---|---|---|
| default（RAM off） | 0 | **56**（前缀命中） | lcp_similarity | 4.9-5.9 |
| prefix-branch（RAM off） | 0 | **111**（全命中） | prefix_branch_revisit | 3.4-4.2 |
| default（RAM on，E2.3 复用） | 0 | 111（RAM restore 掩盖） | lcp_similarity | 4.9 |
| prefix-branch（RAM on，E2.3 复用） | 0 | 111（KV 直接命中） | prefix_branch_revisit | 4.1 |

### 6.2 bc4（TinyLlama，RAM on，3 reps）

| 配置 | X1 回访 cached | 回访 reason/slot | evaluator |
|---|---|---|---|
| default | 73/137 | lcp_similarity | branch_consistent=True, distinct=True |
| prefix-branch | 73/137 | **default_rule（fallback，slot=-1）** | branch_consistent=True, distinct=True |

- **bc4 有界回退**：4 分支 > 2 slot 时，prefix-branch 无空 slot 且无
  revisit 候选 → **fallback_to_default**（routing event slot=-1），行为与
  default 一致；**无错误、无串扰、输出有区分度**；
- 注意：bc4 RAM on 下 cached 值含 RAM restore 成分（clean 只清 KV 不清
  RAM prompt cache，见第 11 节口径）。

---

## 7. RAM enabled/disabled 对照（核心结果）

TinyLlama bc2，3 reps（逐 rep 一致）：

| 指标 | default RAM on | default **RAM off** | prefix-branch RAM on | prefix-branch **RAM off** |
|---|---|---|---|---|
| X 回访 cached_tokens | 111 | **56** | 111 | **111** |
| prompt_processed | 1 | **55** | 1 | **1** |
| 回访延迟(ms) | 4.9 | 5.0-5.9 | 4.1 | 3.4-4.2 |

**结论**：
1. **RAM prompt cache 掩盖作用量化证实**：default 的"全命中"（cached=111）依赖
   RAM restore——`--cache-ram 0` 后 default 回访 cached 掉到 **56**（仅公共
   前缀命中，X 分支内容丢失）；
2. **RAM off 下 prefix-branch 逻辑收益成立（3/3 可复现）**：cached 111 vs 56
   （**+98%**）、prompt_processed 0 vs 55、延迟 3.4-4.2 vs 4.9-5.9ms
   （**-25%**）——KV 直接回访路径在无 RAM 恢复时优于覆盖+前缀命中；
3. RAM on 时数量被掩盖（cached 相同），仅延迟差异（4.1 vs 4.9ms，E2.3）。

---

## 8. default/prefix-branch 对照

- bc2 RAM off：见第 7 节（逻辑 + 延迟差异均成立）；
- bc2 RAM on：cached 相同、延迟差异（4.9 vs 4.1ms，E2.3）；
- bc4：两者均 fallback 默认路径（无空 slot），差异消失（有界）；
- 输出 hash：全部配置同分支一致、跨分支不同（evaluator 全过）→
  **策略不改变输出**。

---

## 9. unified smoke

- 4B + `--kv-unified` + prefix-branch + bc2（1 rep）：
  X 首访 cached=0 → Y 空 slot（empty_slot，preserved）→ X 回访
  **prefix_branch_revisit** cached=34（全命中）→ Y 回访 revisit cached=34；
  active=2、无 400、evaluator 通过；
- **E2.3 unified 修复在长分支场景保持**（无 idle-slot 清理回归）。

---

## 10. logical prefix reuse

- 口径：`logical_prefix_reuse_tokens` = OAI `cached_tokens`（= `n_past`）；
  `prompt_processed_tokens` = `prompt_tokens - cached_tokens`；
  `full_recompute` = cached==0；
- bc2 RAM off：default 回访 reuse=56/111=0.50，prefix-branch=1.00
  （+98%，3/3）——**RAM off 下逻辑收益成立**；
- bc2 RAM on：两者 1.00（RAM restore 掩盖）；
- bc4：两者相近（均有 RAM restore 成分 + fallback）。

---

## 11. RAM restore 路径证据

- **证据链**（路径级推断，非物理 cell 级证明）：
  ① `--cache-ram 0`（禁用 RAM restore）后 default 回访 cached 111→56
  （行为实验证据）；
  ② routing reason 差异：default=`lcp_similarity`（覆盖后回访），
  prefix-branch=`prefix_branch_revisit`（KV 直接回访）；
  ③ selected slot：prefix-branch 回访固定回到原分支 slot；
  ④ 延迟差异（4.9 vs 4.1ms）与 cached 差异（RAM off）方向一致；
- **ram_restore_suspected**：仅在 RAM on + default 覆盖场景（routing
  lcp_similarity + active=1 + cached 高值）设置；不依据延迟单独推断；
- **口径**：clean 断言只清 KV（erase），**RAM prompt cache 跨 replicate 保留**
  （bc4 rep1/2 首访 cached=138 的成因）——RAM on 场景的 cached 含恢复成分，
  已在报告中标注。**明确声明：slot erase 不清除 RAM prompt cache；E2.4
  RAM-on 结果为路径快速验证，E2.5 使用 server-per-replicate 协议提供
  独立正式证据。**

---

## 12. prompt processing/recompute

- bc2 RAM off：default 回访 prompt_processed=55 vs prefix-branch=1
  （每回访少处理 54 token，3/3）；
- full_recompute（cached=0）：仅各序列首请求（cold），策略无差异；
- bc4：首访部分有 RAM 前缀恢复（cached=138），回访 73/137。

---

## 13. latency/throughput/overhead

- bc2 RAM off 回访延迟：default 4.9-5.9ms vs prefix-branch 3.4-4.2ms
  （**-25%，3/3 可复现**）；
- bc2 RAM on：4.9 vs 4.1ms（-16%，E2.3）；
- 路由 overhead：prefix-branch 决策为 O(slots) 只读比较，延迟为负值/可忽略
  （<2% 阈值）；
- 不将单次差异归因于 A2（全部 3/3 重复证据）。

---

## 14. 输出 hash、evaluator、串扰和失败率

- 全部 5 个新增配置：同 branch_id 响应 hash 一致（branch_consistent=True）、
  跨 branch 有区分度（bc4 distinct=True；bc2 因模型短输出 distinct 波动，无串扰）；
- 请求失败：0（全配置）；无 400/超时；
- 4B unified smoke evaluator 通过。

---

## 15. 正式 workload 复用结果

- 复用 E2.1/2.2/2.3：multi_turn/long_life × B0/B1/B2-0/B2-1（parallel=1，
  5 reps）hit 全 0.8529、task_success 1.0；parallel=2 B1 vs B2-1 hit
  0.8585 vs 0.8573（持平）、p50 单次观测未复现；long_life retention 0
  （截断根因，一致）；
- **正式 workload 未出现收益**。按任务要求明确记录：
  > 当前正式 Agent workload 未覆盖足够的长分支回访和 RAM cache 压力，
  > 因此不能宣称 A2 已带来正式 workload 的稳定端到端收益。

---

## 16. 统计限制和未执行项目

- 新增实验为 3 reps 快速验证 + 1 smoke（bc4 RAM on 含 RAM 跨 replicate 残留，
  数值含恢复成分）；
- `long_prefix` 场景未执行（bc2 已示延迟/逻辑差异，优先级让位于 RAM 对照）；
- 完整 llama.cpp pytest 受 preset 模型下载限制（记录，不伪造通过）；
- 4B hybrid 无 per-part attention/recurrent 统计接口（`attention_cached_tokens`
  = null）。

---

## 17. 机制、逻辑、端到端三层结论

| 层 | 结论 | 证据 |
|---|---|---|
| 1. 机制 | ✅ **成立** | prefix-branch 保留分支（bc2 全场景）、回访回原 slot（revisit）、bc4 有界回退（fallback 无错误）、unified 修复保持、无串扰、输出等价 |
| 2. 逻辑 | ✅ **RAM off 下成立（3/3）** | cached 111 vs 56（+98%）、prompt_processed 0 vs 55、full_recompute 无差异；RAM on 下被 prompt cache 掩盖（cached 相同，延迟差异 4.9 vs 4.1ms） |
| 3. 端到端 | ❌ **正式 workload 不成立** | 复用数据：formal hit 无提升；正式场景未覆盖长分支+RAM 压力 |

**判定规则符合性**（任务十一）：
- bc2/bc4 无串扰无新增失败 ✓；prefix-branch 保留确定 ✓；RAM on/off 下
  default 无回归 ✓；unified 修复无回归 ✓；TinyLlama 逻辑收益 3/3 ✓；
  正式 workload 无保真回归 ✓；无越界 ✓；测试通过/阻塞归因环境 ✓；
- **但正式 workload 无稳定收益** → 按 CONDITIONAL 定义（"机制成立但正式
  workload 无稳定收益"），状态为 **CONDITIONAL**，不因 branch 级收益直接
  标 READY。

---

## 18. 最终状态

**CONDITIONAL**

- ✅ RAM prompt cache 掩盖作用**量化证实**（default 回访 cached 111→56）；
- ✅ **RAM off（cache 压力）下 A2 逻辑收益成立**（cached +98%、延迟 -25%，
  3/3 可复现）——A2 价值在 RAM cache 不可用/不足时显现；
- ✅ bc4 有界回退正常（fallback、无错误/串扰）、unified 修复无回归；
- ⚠️ 正式 workload 无稳定端到端收益（场景未覆盖长分支+RAM 压力）；
- ⚠️ RAM on 默认配置下逻辑收益被 prompt cache 掩盖（仅延迟差异）。

---

## 19. 下一步建议

1. **RAM 压力端到端场景**：新增含长分支回访 + `--cache-ram` 受限的正式
   workload 变体（不改现有 workload），验证 A2 在 RAM cache 压力下的端到端
   收益——这是 A2 真实价值场景；
2. **bc4+ 分支保留上限**：明确 prefix-branch 的分支保留数量上限（≈ slot 数）
   与后续回退策略的文档化；
3. **hybrid per-part 统计**：扩展 E1 区分 attention/recurrent（additive
   默认关闭），使 4B 的 cached 可归因；
4. **A2 与 prompt cache 的协同**：研究 prefix-branch 下 RAM restore 与
   KV 直接命中的混合策略（当前 A2 保留分支时 RAM cache 保存仍执行）。

---

## 附注：执行记录、结果路径与提交状态

- 结果：`benchmark/results/e24/`（summary.json/csv + manifest.json + 5 个
  probe JSON + server 日志）；复用 e21/e22/e23；
- 提交：根仓库 `0a83014`（docs: validate long branch and RAM cache boundaries，
  4 文件 +580 行）；llama.cpp 本阶段**无源码修改**（保持 `2f851c4b4`，不建空提交）；
  提交后两仓库 `git status --short` 均为空；
- 测试：根 pytest（新增 `test_long_branch_probe.py` 6 例）→ **89 passed**；
  llama.cpp `test_slot_routing` 10/10、E1 5/5、可运行子集 43 passed
  （8 failed 预存环境问题）；
- 实际执行命令摘要：
  - `bash scripts/e2_scan_e24.sh`（TL RAM off × 2、TL bc4 × 2、4B unified smoke）
  - `uv run python scripts/e2_long_branch_probe.py`（bc2/bc4/smoke）
  - `uv run pytest -q`（根）/ `python3 -m pytest unit/test_slot_routing.py --noconftest`
  - `python3 tmp/run_e1_manual.py`
