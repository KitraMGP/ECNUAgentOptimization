# E2.2 验证报告：A2 hybrid 指标校准、branch workload 验证与边界检查

- 阶段：E2.2（快速验证闭环；未实现任何新策略）
- 日期：2026-08-06
- 状态：**CONDITIONAL**（详见第 15 节）
- 前置：E2.0/E2.0.5/E2.1 报告；A2 实现（llama.cpp `0217843bd` / 根仓库 `2a4ce4d`）

---

## 1. 阶段目标和禁止边界

- 目标：一次执行完成 A2 的 hybrid 指标校准、branch workload 验证、
  unified/non-unified 边界检查、正式 workload 最小回归、报告/测试/提交；
- 禁止（未违反）：新生命周期策略、淘汰、COW、seq_cp 分支复制、KV buffer
  调整、分层存储、Context Compression；未修改生产代码（仅 benchmark 侧脚本
  与 probe 修正，见第 3 节说明）。

---

## 2. 两仓库 HEAD、binary/model hash

| 项 | 值 |
|---|---|
| 根仓库 HEAD | `1ff04b6`（E2.1 状态说明 commit；本轮提交见附注） |
| llama.cpp HEAD | `0217843bd`（A2 实现） |
| server binary | `build-cuda/bin/llama-server` sha256 `74a6b18b…`（含 A2 代码） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| 工作区 | 两仓库均干净（无用户未提交修改） |

---

## 3. 复用的 E2.1 结果和本轮新增实验

**复用（不重跑）**：
- E2.1 正式 workload 10 配置 × 5 independent replicates（`results/e21/`）；
- E2.1 branch probe 5 reps × 2 策略（`results/e21/branch_probe_cr0_*.json`）。

**本轮新增（快速验证，非完整统计）**：
- non-unified branch probe：default/prefix-branch 各 3 reps（`results/e22/`）；
- unified 边界 smoke：default/prefix-branch 各 1 rep（`--kv-unified`）；
- 统一 summary：`results/e22/summary.json / summary.csv / manifest.json`。

**生产代码改动**：无。仅修正 benchmark 侧 probe 脚本（记录 response hash；
`chat_template_kwargs.enable_thinking` 与 driver 一致）与新增
`e2_scan_e22.sh` / `e2_summarize_e22.py`。

---

## 4. independent clean assertion

- 每个 replicate 前：erase 所有 slot → GET `/metrics/kv` → 断言
  `used_cells == 0 && active_sequences == 0`；probe 中失败即中止该配置；
- 本轮 4 个 probe（3+3+1+1 reps）**全部通过 clean 断言**；E2.1 10 配置
  5/5 clean 复用。

---

## 5. hybrid 指标源码结论

| 指标 | 来源（源码） | 可靠性 |
|---|---|---|
| OAI `usage.prompt_tokens_details.cached_tokens` | `server-task.cpp:398` = `n_prompt_tokens_cache` = `n_past`（公共前缀） | ✅ 可靠（=逻辑前缀命中数） |
| `/completion` `timings.cache_n/prompt_n` | `get_timings()` 读同一字段 | ❌ **最后批次语义**（分批处理时只反映最后一批），不可作总命中数 |
| `/metrics/kv` used_cells | `llama_memory_hybrid::get_kv_stats` 委托 `mem_attn`（`llama-memory-hybrid.cpp:34`） | ✅ 仅 attention KV cell；recurrent 状态不在统计范围 |
| hybrid attention/recurrent 分离 | 无 per-part 接口 | ❌ `attention_cached_tokens` = **not_available（null）** |
| routing event `selected_prefix_tokens` | `get_common_prefix`（E2.1 实现） | ✅ LCP token 数（非物理 cell） |

**结论**：`cached_tokens`（OAI）= `n_past` 是可靠的**逻辑前缀命中数**，即
`logical_prefix_reuse_tokens`；无法分离 attention/recurrent；`cached_tokens`
**不得单独作为 hybrid 总缓存命中证据**（E2.1 已发现的 A+Y 路由 slot1 却
cached=0 现象印证）。

---

## 6. logical prefix reuse 的定义

- `logical_prefix_reuse_tokens` = OAI `cached_tokens`（server 确认的公共前缀
  `n_past`，benchmark 侧直接派生，零生产代码改动）；
- `logical_prefix_reuse_ratio` = `cached_tokens / prompt_tokens`；
- `prompt_processed_tokens` = `prompt_tokens - cached_tokens`；
- `full_recompute` = `cached_tokens == 0` 的请求数；
- `attention_cached_tokens` = `null`（hybrid 无法分离，not_available）。

---

## 7. branch workload 配置和结果

配置：parallel=2、ctx=2048、cr=0、temperature=0、seed=42；序列
A+X → A+Y → A+X（自动路由）+ 显式 slot0/slot1；独立协议（每 replicate 前
clean 断言）。

| probe | reps | preserved | overwritten | revisit reason | active_sequences |
|---|---|---|---|---|---|
| non-unified default（E2.1） | 5 | 0/5 | 5 | lcp_similarity | 1（5/5） |
| non-unified prefix-branch（E2.1） | 5 | 5/5 | 0 | prefix_branch_revisit | 2（5/5） |
| non-unified default（E2.2 新增） | 3 | 0/3 | 3 | lcp_similarity | 1（3/3） |
| non-unified prefix-branch（E2.2 新增） | 3 | 3/3 | 0 | prefix_branch_revisit | 2（3/3） |
| unified default（smoke） | 1 | 0/1 | 1 | lcp_similarity | 1 |
| **unified prefix-branch（smoke）** | 1 | 1/1 | 0 | **prefix_branch_empty_slot（回访失败）** | **1** |

**合并（non-unified）**：prefix-branch 保留分支 8/8、default 覆盖 8/8 —— 机制稳定复现。

---

## 8. default/prefix-branch 对照（逻辑指标）

| 指标（non-unified，A+X 回访） | default | prefix-branch |
|---|---|---|
| `logical_prefix_reuse_tokens`（cached_tokens） | 32 | 32 |
| `logical_prefix_reuse_ratio` | 0.89 | 0.89 |
| `prompt_processed_tokens` | 4 | 4 |
| `full_recompute`（A+Y 轮） | 1（cached=0） | 1（cached=0） |
| `active_sequences` | 1 | **2** |

**结论**：逻辑前缀复用指标在两种策略下**无差异**（仅 `active_sequences` 增加）——
按判定规则，只能称为**结构性分支保留**，**不能称为缓存命中收益**。
（4B hybrid 的 cached_tokens 语义使 default 覆盖后回访仍显示 cached=32，
逻辑收益在 hybrid 上不可证实；tinyllama 标准架构的行为差异见 E2.1 集成测试。）

---

## 9. non-unified/unified 边界

| 维度 | non-unified | unified（--kv-unified） |
|---|---|---|
| prefix-branch 保留分支 | ✅ preserved 8/8 | ⚠️ preserved=1/1（A+Y 去空 slot） |
| prefix-branch 回访 | ✅ `prefix_branch_revisit`（命中原 slot） | ❌ **回访失败**：A+X again 被选到空 slot（reason=`prefix_branch_empty_slot`），cached=0 全重算 |
| 双分支 used_cells | 86（翻倍） | **43（不翻倍，cell 共享）** |
| active_sequences | 2 | **1（unified 口径，E2.0.5 已记录）** |
| 输出 hash | 一致 | 一致 |

**边界结论**：**prefix-branch 在 unified 模式下回访不可靠**（cached=0 全重算、
reason 不触发 revisit）——策略的 non-unified 机制不能直接推广到 unified；
unified 下 cell 共享（used 不翻倍）是既有共享语义，**不等于 COW**。

---

## 10. 正式 workload 回归（复用 E2.1 5 reps）

| 配置 | hit | p50(ms) | eval | valid |
|---|---|---|---|---|
| multi_turn p1 B0/B1/B2-0/B2-1 | 全 0.8529 | 479-484 | 1.0 | 5/5 |
| multi_turn p2 B1 vs B2-1 | 0.8585 vs 0.8573 | 458.6 vs 442.7 | 1.0 | 5/5 |
| long_life p1 B1 vs B2-1 | 0.7114 vs 0.7114 | 564.8 vs 568.4 | retention 0 | 5/5 |

→ 正式 workload 无回归（hit/eval 一致）；**端到端命中收益不成立**。

---

## 11. latency、throughput、recompute、KV 指标

- latency/throughput：parallel=1 各配置 <1% 差异；parallel=2 B2-1 vs B1
  p50 −3.5%、tps +3.2%（**E2.1 单次观测，E2.2 未复现，按任务规则不作为
  稳定收益结论**）；
- recompute：parallel=1 各配置 recompute_tokens 一致（multi_turn 3313）；
  parallel=2 default 1033 vs prefix-branch 1036（±0.3% 噪声级）；
- KV：peak_used_cells p1=2032/2048、p2=2046/2048（策略不改变 cell 分配）；
  `capacity_bytes` 预分配不变，**不作为收益指标**；
- routing overhead：parallel=1 无额外延迟；parallel=2 为负值/可忽略（<2% 阈值）。

---

## 12. 输出 hash、evaluator 和串扰结果

- **hash 串扰**：4 个 probe 同策略内 first/revisit hash 全部一致
  （`e30f9baa…`）→ **无跨 slot 污染**；
- **跨策略输出等价**：default 与 prefix-branch 的 A+X 输出 hash 一致 →
  **策略只改路由、不改生成**；
- **跨 unified/non-unified 输出等价**：hash 一致；
- evaluator：multi_turn task_success 1.0（B0/B1/B2）；long_life retention 0
  （截断根因，各配置一致）；
- 请求失败：0（全配置）；无 400/超时。

---

## 13. 机制收益、逻辑收益、端到端收益三层结论

| 层 | 结论 | 证据 |
|---|---|---|
| 1. 机制收益 | ✅ **成立（结构性分支保留）** | prefix-branch preserved 8/8、default 覆盖 8/8、revisit 选原 slot、无串扰、输出等价 |
| 2. 逻辑收益 | ⚠️ **未证实** | logical_prefix_reuse/prompt_processed/full_recompute 在两种策略间无差异；仅 active_sequences 增加 → 只能称结构性保留 |
| 3. 端到端收益 | ❌ **不成立** | 正式 workload hit 无提升；parallel=2 性能差异单次未复现；不宣称端到端收益 |

**判定规则符合性**：正式 workload 无收益 → 状态 CONDITIONAL；hybrid
cached_tokens 不可解释 → 不阻塞机制判定但状态至少 CONDITIONAL；
logical prefix reuse 作为主指标（两者间无差异，故不宣称逻辑收益）。

**BLOCKED 触发项检查**：无（hash 无回归、无串扰、无请求失败、策略确定、
未越过禁止边界、分支保留成立、逻辑指标可计算）→ 不 BLOCKED。

---

## 14. 未执行项目和环境阻塞

- 未执行 8192 扫描、未重复完整 B0/B1 五次基线（按任务原则复用）；
- llama.cpp 完整 pytest 仍受 preset 模型下载限制；可运行子集
  `test_completion/basic/ignore_eos/tokenize`（--noconftest）43 passed /
  **8 failed——判定为预存测试桩/环境问题**（`test_server_slots` 501=缺
  `--slots` 桩参数、`test_load_split_model` 缺模型文件、stream/UI/aliases
  为 --noconftest 环境差异），**与 E2.1 路由改动无逻辑关联**（
  `test_slot_routing.py` 8/8 + `test_metrics_kv` 5/5 均过）；
- 本轮新增实验为 3 reps 快速验证 + 1 smoke，**标注为快速验证，不据此
  宣称完整统计显著性**。

---

## 15. 最终状态

**CONDITIONAL**

- 机制成立：prefix-branch 在 non-unified branch workload 中稳定保留分支
  （8/8），default 覆盖（8/8），回访命中原分支 slot，无串扰、输出等价；
- 逻辑收益未证实、端到端收益不成立：正式 workload 无命中提升；hybrid
  `cached_tokens` 语义限制使逻辑收益不可在 4B 上量化（以
  `logical_prefix_reuse` 为口径两者无差异）；
- **新边界发现**：unified 模式下 prefix-branch 回访失败（cached=0 全重算），
  机制不可直接推广到 unified；
- 无 BLOCKED 触发项。

---

## 16. 下一步建议

1. **unified 回访异常专项**：调查 unified 下 `prefix_branch_revisit` 判定
   失败根因（疑似 unified 单 stream 下 slot.prompt 状态/共享语义差异）；
2. **标准架构量化**：用 tinyllama（非 hybrid）补 default vs prefix-branch
   的 `logical_prefix_reuse_tokens` 精确对比，量化机制的真实缓存收益；
3. **分支型正式 workload**：新增含分支回访的正式场景（不改现有 workload），
   使端到端收益可测；
4. **hybrid per-part 统计**：扩展 E1 可观测性区分 attention/recurrent
   cached 量（additive，默认关闭）。

---

## 附注：执行记录、结果路径与提交状态

- 结果：`benchmark/results/e22/`（summary.json/csv + manifest.json + 4 个
  probe JSON + server 日志）；复用 `results/e21/`（10 配置 + 2 个 5-rep probe）；
  manifest 含命令、参数、模型/binary hash、commit、时间与协议；
- 提交：根仓库 `22f5348`（docs: validate A2 hybrid and branch workload
  results）；llama.cpp 本阶段**无生产代码改动**（无空提交），HEAD 仍为
  `0217843bd`；两仓库提交后均干净；
- 测试：根 pytest 83 passed；llama.cpp `test_slot_routing` 8/8、
  `test_metrics_kv` 5/5；可运行子集 43 passed（8 failed 预存环境问题，记录）。
