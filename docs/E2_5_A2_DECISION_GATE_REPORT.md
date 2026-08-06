# E2.5 决策门禁报告：A2 正式分支压力 Workload、独立 RAM Cache 协议

- 阶段：E2.5（未实现任何新 KV 生命周期策略；未修改 slot routing 层）
- 日期：2026-08-06
- **最终决策：`REJECT`**（依据见第 21 节；不自动回退已有提交）

---

## 1. 阶段目标与禁止边界

- 目标：在 Qwen3.5-4B hybrid 上建立正式 branch-pressure workload、使用
  server-per-replicate 协议（解决 E2.4 RAM-on replicate 的独立性缺陷）、
  判定 A2 是否达到 `PROMOTE / HOLD / REJECT` 之一；
- 禁止（未违反）：无新 KV 策略、无淘汰/回收/COW/seq_cp/分层/压缩、未修改
  seq/KV buffer/memory core/decode/采样/tokenizer/cache-reuse/OpenAI 响应/
  既有 workload；**未修改 slot routing 层**（发现的 revisit 判定缺陷因越过
  禁止边界而未修复，见第 7/21 节）；无 per-part 指标接口；无目标扫描。

---

## 2. 两仓库提交和 binary/model hash

| 项 | 值 |
|---|---|
| llama.cpp HEAD | `2f851c4b4`（E2.3 unified 修复；本阶段无源码修改） |
| 根仓库起始 HEAD | `9b5e351`（含 `0a83014` E2.4） |
| server binary | `build-cuda/bin/llama-server`（E2.3 构建，sha256 见 manifest） |
| 模型 | `qwen3-5-4B-Q4_K_M.gguf` sha256 `de8e96cd…` |
| GPU | NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB |

---

## 3. E2.4 独立性问题及本阶段修正

- E2.4 缺陷：RAM-on replicate 仅 erase slot KV，**RAM prompt cache 跨
  replicate 保留**（server 进程内 LRU）→ RAM-on 数值非严格独立统计证据；
- 本阶段修正：**server-per-replicate 协议**——每个 replicate 启动全新
  llama-server 进程、health 就绪、`/metrics/kv` 断言 `used_cells==0 &&
  active_sequences==0`、独立 warmup（无关短请求）、erase 后正式执行、
  SIGTERM 退出并确认进程结束；记录 `server_restarted/fresh_process_verified/
  server_pid/binary/model/commit hash/ram_cache_mib/routing_policy`；
- E2.4 报告已同步最小修正（第 11 节补明确声明）。

---

## 4. server-per-replicate 协议

- 实现：`benchmark/scripts/e2_scan_e25.py`（`--pairs N` 配对区组模式）：
  replicate 1: default→prefix-branch；replicate 2: 反向；交替；
- 22 个正式 replicate + 4 个 smoke 全部 `server_restarted=true`、
  `fresh_process_verified=true`（启动断言通过）、`server_exited=true`；
- 未在同一个 server 进程上执行两个 independent replicate。

---

## 5. branch_pressure workload 和 evaluator

- 新 workload：`benchmark/workload/branch_pressure.py`（注册名
  `branch_pressure`，独立于 multi_turn/long_life/branch）；
- 8 请求固定序列：X1 → Y1 → X2 → Y2 → X3 → Y3 → X_revisit → Y_revisit
  （≥4 次可计量回访：X2/Y2/X3/Y3 前缀推进 + X/Y_revisit 完整回访）；
- 双分支 X/Y 各含 3 轮工具观察（X_STATE_17 / Y_STATE_42）、长共享前缀
  （SYSTEM + TASK + TOOL_HISTORY + PROTOCOL + DATA_DICTIONARY）；
- 输出固定 JSON `{"branch","state","answer"}`；evaluator 检查 JSON 可解析、
  branch/state 正确、answer ≥10 字符、无另一分支 state、同分支回访一致、
  跨分支有区分度。

---

## 6. workload/evaluator 冻结 hash

- 正式实验前完成一次固定调整（加长共享前缀至 40% ctx，见下）；
- 冻结后不再修改；manifest 记录：
  - workload 文件 sha256：见 `results/e25/manifest.json`
  - prompts 序列 sha256（`build_prompts()` 的 sort_keys JSON）：同 manifest
  - evaluator 随 workload 文件冻结；
- prompt 长度实测：X1=3281 / X3=3387 tokens（40-42% of ctx=8192），满足
  任务"40%-70%"目标；ctx=8192 选择依据：parallel=2 下每 slot 4096，prompt
  + 生成预算需 >4096（目标 40-70% 需 3300-5700）。

---

## 7. RAM pressure 校准

- 4B per-token KV state ≈ 105 MB/3281-token 状态（实测口径，见下）；
- 校准 smoke（default 策略，128/256/512 MiB，各 1 个 server-per-replicate）：
  三个容量下回访 `cached_tokens` **完全相同**（2765-2871，仅共享前缀命中），
  且 RAM=8192 下也相同 → **4B 长 prompt 下 RAM prompt restore 从未生效**
  （`prompt_load` 对超长 state 无成功恢复；128=512=8192 无差异）；
- **结论**：无法观测 eviction/miss 差异 → 按任务规则记录
  `ram_pressure_capacity_unresolved`；正式压力条件冻结为
  **RAM_PRESSURE = 128 MiB**（`--cache-ram 128`），其观测与 RAM off 等价；
- 校准 smoke 不纳入正式统计（manifest 区分）。

---

## 8. 正式实验矩阵

| 条件 | 策略 | replicates | 协议 |
|---|---|---|---|
| RAM off（0 MiB） | default / prefix-branch | 5 / 5 | server-per-replicate，配对区组 |
| RAM pressure（128 MiB） | default / prefix-branch | 5 / 5 | 同上 |
| RAM default（8192 MiB） | default / prefix-branch | 2 / 1 | smoke（边界验证） |
| unified + pressure | prefix-branch | 1 | smoke（E2.3 回归验证） |

- 每 replicate：全新 server、warmup、erase、8 请求、停 server；
- 配对区组顺序已执行（D P | P D | D P | P D | D P），manifest 记录；
- 无因结果负向而重跑；无 server 启动失败或协议断言失败（除早期端口
  占用与 `/health` JSON 格式修复，属运行器缺陷，正式矩阵重跑干净）。

---

## 9. 每个 replicate 的配对结果

完整数据见 `results/e25/paired_results.csv`（每对 default/prefix-branch
的 revisit p50、wall time、prompt_processed diff）。摘要：

| 指标（paired median，off） | default | prefix-branch | diff |
|---|---|---|---|
| branch revisit latency p50 | 890 ms | 1828 ms | **+105%** |
| workload wall time | 10.7 s | 13.0 s | **+21.5%** |
| prompt_processed_tokens 总和 | 6716 | 13424 | **+99.8%** |
| 方向一致性（5/5 对同向） | — | 更差 5/5 | 稳定 |

pressure 条件结果与 off 几乎相同（+105.6% / +20.4% / +99.8%）。

---

## 10. RAM off 结果

- 5/5 对 prefix-branch 的 revisit p50 均更高（+938ms 中位差，0/5 更低）；
- 5/5 对 wall time 均更长（0/5 不劣）；
- prompt_processed 总和 prefix-branch ≈ 2× default（X_revisit 全重算
  3227 tokens vs default 409）；
- **未达到任何性能收益门槛**。

---

## 11. RAM pressure 结果

- 与 RAM off 等价（restore 不生效）；5/5 对同样全面更差；
- 说明 prefix-branch 的退化与 RAM cache 容量无关（8192 下同样退化）。

---

## 12. RAM default smoke

- default（2 reps）：revisit p50≈921ms、wall≈10.7s、eval 1.0；
- prefix-branch（1 rep）：revisit p50≈2013ms（+119%）、wall≈14.0s（+31%）；
- 默认 8192 MiB 部署下 prefix-branch 同样显著更慢。

---

## 13. unified 回归

- unified + pressure + prefix-branch smoke：`prefix_branch_empty_slot` 正常
  （Y1 去空 slot，无 idle-slot 清理回归）→ **E2.3 unified 修复无回归**；
- 但 X2/X3 及 X_revisit 均 `default_rule`（长 prompt revisit 判定失效），
  X_revisit cached=53——退化与 non-unified 一致，非 unified 特定问题。

---

## 14. logical prefix reuse 和 prompt processing

- default：回访 cached=2765-2871（共享前缀命中），X_revisit
  prompt_processed=409；
- prefix-branch：X2/X3 cached=3222/3275（分支推进时命中 X1 大部分，96%），
  但 X_revisit cached=53（**几乎全重算**，prompt_processed=3227）；
- **根因（E2.1 实现，非 E2.3 回归）**：revisit 判定
  `lcp == slot_len || lcp == n_task`（server-context.cpp:1718-1719）只覆盖
  "一方是另一方完整前缀"；分支推进序列（X1→X2→X3）后回访 X1 时，与缓存
  X3 的 lcp 是共享前缀+X1obs（≠ 任何全长）→ 判定失败 → fallback 默认 →
  LCP 覆盖最近分支 → 回访丢失全部分支内容（cached=53）。

---

## 15. latency、吞吐和总 wall time

- revisit p50：prefix-branch 全面更高（+105% off / +105.6% pressure /
  +119% default smoke）；
- wall time：+21.5%（off）/ +20.4%（pressure）；
- routing overhead 本身 <2%（决策计算 O(slots)），但**端到端退化远超
  overhead**（覆盖策略导致的重算成本）；
- throughput：随 wall time 反比下降（约 -18%）。

---

## 16. 输出正确性、失败率和串扰

- 全部 22 正式 replicate + 4 smoke：request failure=0、contamination=0、
  evaluator pass rate=1.0（JSON/branch/state/answer/no_foreign_state 全过）、
  路由确定性（同条件同序列结果一致）；
- **无正确性回归**：A2 不改变输出，仅在缓存路径上退化。

---

## 17. hybrid 指标限制

- 4B hybrid 无 per-part attention/recurrent 统计接口 →
  `attention_cached_tokens=null`；`cached_tokens` 仅作逻辑公共前缀复用数；
- `used_cells` 仅表 attention KV cell；`active_sequences` 为分支保留结构
  观测，不等同 cache hit。

---

## 18. 复用的既有正式 workload 结果

- 复用 E2.1/2.2/2.3/2.4（未重跑）：multi_turn/long_life × B0/B1/B2
  （parallel=1，5 reps）hit=0.8529、task_success=1.0、无输出回归；
  TinyLlama RAM off 短 prompt 的 prefix-branch 收益（cached 111 vs 56、
  延迟 -25%）、bc4 有界回退、E2.3 unified 修复——**均为短 prompt 边界结论**；
- 本阶段唯一新增验证：新 binary hash 与 E2.3 一致、default 行为未变
  （default 数据与 E2.4 一致）、unified 无 idle-slot 回归、branch_pressure
  输出无保真回归。

---

## 19. 测试、阻塞和未执行项目

- 根仓库：`uv run pytest -q` → **100 passed**（含 branch_pressure 11 例、
  long_branch_probe 6 例、replicate/evaluator 等既有测试）；
- llama.cpp：`test_slot_routing` **10/10**、E1 `run_e1_manual` **5/5**、
  可运行 completion/basic 子集 43 passed（8 failed 预存环境问题，
  同 E2.2/2.3，非本阶段回归）；完整 pytest 因 preset 模型下载阻塞
  （记录，不伪造通过）；
- 未执行：long_prefix 变体（E2.4 已有 bc2 数据）；4B 多长度扫描（禁止）；
  RAM eviction 观测（无法构造，见第 7 节）。

---

## 20. 对预注册门槛逐项判定

预注册（manifest `pre_registered_thresholds`）逐项结果：

| 门槛 | off | pressure | 判定 |
|---|---|---|---|
| 1) 5/5 wall time 不劣于 default | 0/5 | 0/5 | **FAIL** |
| 2) ≥4/5 revisit p50 更低 | 0/5 | 0/5 | **FAIL** |
| 3) paired median revisit p50 改善 ≥5% | -105% | -105.6% | **FAIL** |
| 4) wall ≥3% 或 prompt_processed 降 ≥10% | -21.5% / -99.8% | -20.4% / -99.8% | **FAIL** |
| 5) request failure 不增加 | 0=0 | 0=0 | PASS |
| 6) evaluator 不下降 | 1.0=1.0 | 1.0=1.0 | PASS |
| 7) contamination=0 | 0 | 0 | PASS |
| 8) default RAM-on 无回归 | 与 E2.4 一致 | — | PASS |
| 9) routing overhead ≤2% | 决策计算 <2% | — | PASS（但端到端退化远超） |

**正式收益门槛未达到（性能 gate 1-4 全败，0/5 达标）**。

---

## 21. `PROMOTE / HOLD / REJECT` 最终决策

**`REJECT`**

依据（对照任务十二的 REJECT 定义）：
1. **4B 正式条件持续变慢**：RAM off 与 RAM pressure 均 5/5 对更慢
   （revisit p50 +105%、wall +21%、prompt_processed +99.8%），方向一致、
   可复现（独立 server-per-replicate 协议）；
2. **成本超过收益**：任何条件（off/pressure/default/unified）下 prefix-
   branch 均无性能收益，成本（近全量重算）明确超过收益（0）；
3. **必须修改禁止范围才能继续**：根因是 E2.1 `get_available_slot` 的
   revisit 判定缺陷（不覆盖"分支推进+回访"混合序列），修复需修改
   slot routing 层，本阶段禁止 → 按任务停止实现并记录 REJECT；
4. 收益仅在过窄条件成立（TinyLlama 短 prompt + RAM off），4B 正式条件
   无任何收益；
5. 正确性无回归（failure=0、contamination=0、eval 1.0、default 无回归、
   unified 修复无回归）——REJECT 不因正确性，而因收益/成本与禁止边界。

不判定 HOLD 的原因：HOLD 的前提是"机制有效但正式收益低于门槛"；本阶段
实测为**正式条件持续显著变慢**（非"收益不足"），且退化根因明确位于
A2 自身的路由判定（需越界修复），符合 REJECT 的"持续变慢 + 必须修改
禁止范围"触发项。不判定 PROMOTE（性能 gate 0/5）。

---

## 22. A2 的适用范围与不适用范围

**适用（边界内有效，已有证据）**：
- 多分支**完整回访**（请求 == 缓存或缓存 == 请求）短 prompt（<100 tokens）
  场景（E2.3/2.4 TinyLlama：分支保留、回访全命中、延迟 -25%）；
- slot 数 ≥ 活跃分支数的平行分支保留（unified/non-unified 均可）；
- 无 RAM prompt restore 可用的小模型环境。

**不适用（本阶段实测）**：
- **长 prompt 分支推进 + 回访混合序列**（X1→X2→X3→X_revisit）：revisit
  判定失效 → 覆盖 → 回访近全量重算（4B 实测 +105% 延迟）；
- 4B hybrid 长上下文（prompt >3000 tokens）：无任何收益，成本为负；
- 依赖 RAM prompt cache 的默认部署（8192 MiB）：无增量（restore 本身
  对长 state 不生效）；
- 不得宣称对所有 Agent workload 受益；不得将路由复用/分支保留描述为
  COW 或物理共享。

---

## 23. 下一阶段建议

1. **A2 处置**：保留 feature flag（`--slot-routing-policy prefix-branch`
   默认关闭、debug-only），**不建议默认暴露**；不自动回退已有提交；
   报告本判定为决策记录；
2. **根因修复方向（需单独授权，属 E2.1 实现缺陷）**：扩展 revisit 判定
   支持"分支推进+回访"（如维护分支链/以共享前缀为键的 LCP 缓存映射），
   修复后需重跑本报告矩阵；
3. **RAM prompt cache 长 state 失效**：`prompt_load` 对 4B 长 prompt 从不
   成功恢复——值得单独调查（容量单位/state 大小/load 匹配条件），
   但超出本阶段边界；
4. **路线图**：转向其他已批准候选（如生命周期管理方向），A2 不再调参；
5. 后续阶段若启用 A2，需先重验本报告 20 节门槛。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e25/`（manifest.json / summary.json / summary.csv /
  paired_results.csv / off_full / pressure_full / 4 个 smoke JSON）；
- 新文件：`benchmark/workload/branch_pressure.py`、`scripts/e2_scan_e25.py`、
  `scripts/e2_scan_e25_full.sh`、`scripts/e25_summarize.py`、
  `scripts/e25_make_manifest.py`、`tests/test_branch_pressure.py`；
- 修改：`docs/E2_4_LONG_BRANCH_RAM_CACHE_VALIDATION_REPORT.md`（第 11 节
  独立性声明修正）；
- llama.cpp：本阶段无源码修改（`2f851c4b4` 保持），不建空提交；
- 测试：根 100 passed；llama.cpp test_slot_routing 10/10、E1 5/5；
- 实际执行命令摘要：`e2_scan_e25.py --pairs 5`（off/pressure）、
  `--ram default/unified` smoke、`e25_summarize.py`、`e25_make_manifest.py`、
  `uv run pytest -q`、`pytest unit/test_slot_routing.py --noconftest`、
  `tmp/run_e1_manual.py`。
