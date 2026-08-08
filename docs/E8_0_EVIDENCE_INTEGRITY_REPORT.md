# E8.0：证据完整性复核报告（Evidence Integrity）

- 复核时间：2026-08-08
- 方法：从 `benchmark/results/kv_optimization/raw/` 独立重新生成汇总，与 E6/E7 报告逐项比对；重跑测试确认退出码；校验 commit 内容。**未修改任何 raw 数据。**

## 1. E7 扩展 benchmark 矩阵（raw → 独立重算）

| 场景 | off median | on median | delta | reduction% | 报告值一致 | 无损 | reps |
|---|---|---|---|---|---|---|---|
| M01_2slot_shared_prefix | 209.0 | 14.0 | -195 | 93.30% | OK | True | 5 |
| M02_3slot_multi_target | 208.5 | 13.0 | -196 | 93.76% | OK | True | 5 |
| M03_partial_prefix | 125.0 | 31.0 | -94 | 75.20% | OK | True | 5 |
| M04_no_shared_prefix | 135.0 | 135.0 | 0 | 0.00% | OK | True | 5 |
| M05_long_code | 227.0 | 75.0 | -152 | 66.96% | OK | True | 5 |
| M06_long_summary | 425.0 | 43.0 | -382 | 89.88% | OK | True | 5 |
| M07_tool_call_json | 319.0 | 52.0 | -267 | 83.70% | OK | True | 5 |
| M08_canary | 235.0 | 24.0 | -211 | 89.79% | OK | True | 5 |

**结论：E7_4 报告全部 8 场景的 median/delta/reduction/lossless 与 raw 完全一致 → `VERIFIED`**（样本数：M01 5、M02 10、其余 5）。

## 2. E6.4 paired bench（e6_c1_bench_summary.json）

- off median = 229（5/5 reps 全 229）、on median = 24（5/5 全 24）、reduction = **89.52%**、shared_cells(on) = 205×5 → **与 E6_4 报告一致 → `VERIFIED`**
- 输出 hash：off/on 均为 `e5bfbea7...`（一致）→ 无损声明 `VERIFIED`
- **注意**：E6.4 报告依赖 `timings.prompt_n`（prefill 处理 token）作 recompute 代理；raw 中同时记录了 `req1_prompt_n=227`（首次请求全量）→ 代理语义自洽

## 3. E6.5 endurance（e6_c1_churn12.json）

- cycles = 12（12 个 cycle_samples 全 status=ok）、`no_errors=True`、`errors=[]`、erase 后 `used_cells=0/active_sequences=0/shared_cells=0`、`same_logic_state_drift`/`reclaims_to_baseline`/`verdict` 字段存在 → **与 E6_5 报告一致 → `VERIFIED`**

## 4. C2 oracle（e6_c2_oracle.json）

- prompt_tokens=3250、used_cells=3265、budget=1024、attn_kv_bytes_after_estimate=33554432（-68.6%）→ 与 E6_3 报告一致 → `VERIFIED`（作为**容量估算**；其 "SnapKV oracle" 命名已被 E7.5 降级，raw 保留历史名）

## 5. 4B probe（e6_c1_probe_off/on.json）

- off/on 输出 hash 均为 `8a85763b...`（req1）/`bd505e40...`（req2）→ 一致 → 安全禁用声明 `VERIFIED`
- **瑕疵**：off 记录 `git_commit=69711a2d6`（E4.2 后、E7.2 前），on 记录 `git_commit=f5837427b`（E7.2 后）→ **非同一 commit 下的严格 paired** → `PARTIALLY_VERIFIED`（hybrid 禁用条件在 E7.2 前后未变，输出一致结论仍成立，但严格 paired 要求未满足；E8.5 将按同一 commit 重新验证）

## 6. 测试数量与退出码（独立重跑）

| 套件 | 结果 | 退出码 |
|---|---|---|
| llama.cpp server 单测（6 文件）| 41 passed | 0 |
| E1 手动（run_e1_manual.py）| 5/5 passed | 0 |
| 根仓库 pytest | 177 passed | 0 |

**报告声称的测试数量/名称与实测一致 → `VERIFIED`**

## 7. commit 内容校验

| commit | 声称 | 实际内容 | 判定 |
|---|---|---|---|
| llama `568b8733` | C1 主实现 | common/arg.cpp +16、common.h +4、server-context.cpp +68、test_kv_prefix_share.py +155、utils.py +8 | `VERIFIED` |
| llama `02541f72` | 共享源保护测试 | test_kv_prefix_share.py +28（6 例 → 与 E6.2 声称 6 例一致）| `VERIFIED` |
| llama `e7e08552` | E7.1 审计测试 | test_kv_prefix_share_audit.py 262 行（10 例）| `VERIFIED` |
| llama `f5837427` | E7.2 identity 加固 | server-context.cpp +10/-1 | `VERIFIED` |
| 根 `d620c13` | E7.4 矩阵 | e7_c1_bench_matrix.py + 报告 + raw | `VERIFIED` |

## 8. raw 数据字段完整性缺口（`PARTIALLY_VERIFIED`）

| 字段 | e6_c1_bench_summary | e7_c1_matrix |
|---|---|---|
| seed（固定 42）| **未显式记录**（脚本硬编码）| **未显式记录** |
| temperature（0）| 未记录 | 未记录 |
| 时间戳 | 未记录 | 未记录 |
| on/off 开关 | variant 字段（off/on）| 脚本参数推断（off 无 --kv-prefix-share）|
| 失败状态 | 无失败 | status/exit_code/failed 字段存在 |

**影响**：可复现性 OK（脚本参数固定、git_commit 记录），但 raw 未显式记录 seed/temp/时间戳 → 标注 `PARTIALLY_VERIFIED`；E8.5 新实验将补齐这些字段。

## 9. 历史命名残留（非矛盾，需说明）

- `e6_c1_bench_summary.json` 的 `candidate_id: "C1_RADIX_PREFIX_SHARING"`、`e6_c2_oracle.json` 的 `candidate_id: "C2_SNAPKV_PROMPT_COMPRESSION"` 为 E6 历史命名；E7.5 已在 hypotheses.json 与文档修正（C1_CROSS_SLOT_PREFIX_SHARING / C2_KV_BUDGET_ESTIMATOR）。raw 数据保留历史事实，不修改。

## 10. 结论汇总

| 结论 | 状态 |
|---|---|
| E7 矩阵报告与 raw 一致（8 场景）| `VERIFIED` |
| E6.4 recompute 收益 229→24（-89.52%）| `VERIFIED` |
| E6.5 endurance 12 周期 | `VERIFIED` |
| C2 容量估算数据 | `VERIFIED`（命名已降级）|
| 4B 安全禁用输出一致 | `PARTIALLY_VERIFIED`（off/on 非同一 commit）|
| raw 字段完整性（seed/temp/时间戳）| `PARTIALLY_VERIFIED`（缺口记录于第 8 节）|
| 测试数量与退出码 | `VERIFIED` |
| commit 内容与声称一致 | `VERIFIED` |
| E6 历史命名残留 | `CONTRADICTED`（E7.5 已修正文档层，raw 保留历史）|

**无 raw 数据被修改；无伪造迹象；E6/E7 报告主体结论经独立重算成立。**
