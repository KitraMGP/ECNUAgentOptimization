# E3.5.2 报告：Lifecycle Diagnostics 默认关闭性能复核与 Field Trace 最终接入门禁

- 阶段：E3.5.2（独立 binary ABBA 测量；不修改 schema/策略/阈值）
- 日期：2026-08-06
- **最终状态：`HOLD_FOR_PERFORMANCE_EVIDENCE`**（依据见第 6-8 节）

---

## 1. 目标与边界

- 目标：验证 `--lifecycle-trace` **默认关闭**时，`aba4b26a` 相对
  `049872f59` 无可检测生产路径开销；
- 禁止（未违反）：改 schema/归属字段/parser、default/LRU/tick/seq/KV/
  decode/batch/响应、A1/A4 收益矩阵、把 trace-on 开销当默认关闭开销、
  改阈值或追加 replicate、伪造 field trace、提交 binary/模型/日志/build。

---

## 2. A/B 独立 binary

| 项 | A（baseline） | B（candidate） |
|---|---|---|
| commit | `049872f59` | `aba4b26a` |
| 构建 | 独立 worktree `llama-baseline/`（不污染当前工作树） | 现有 `llama.cpp/build-cuda` |
| binary sha256 | `408c7fd9…` | `74a6b18b…`（主可执行薄壳；逻辑在 .so，差异 22KB） |
| 编译配置 | Release、GGML_CUDA=ON、arch 89 | 相同（CMakeCache 核对一致） |
| 模型/GPU | Qwen3.5-4B（de8e96cd）、RTX 4060 Laptop 8GB、驱动 610.43.03 | 相同 |
| B 的 `--lifecycle-trace` | — | **明确关闭**（默认） |
| 其余 server 参数 | 完全相同（unified p4、ctx 8192、cache-ram 0、temp 0、seed 42、policy default） | 相同 |

---

## 3. 静态关闭路径审计（任务五）

| 审计项 | 结果 |
|---|---|
| trace 参数默认 false | ✓（common.h `lifecycle_trace=false`） |
| trace_event 在构造字符串/遍历 candidates/读 cell stats **前**立即返回 | ✓（函数首行 `if (!lifecycle_trace) return;`） |
| request trace 解析不进入 prompt/response | ✓（task_params 仅存 id，受限校验，E3.5.1 已验证） |
| unique/shared 遍历只在 trace 开启且 purge 事件时执行 | ✓（位于 trace_event 内，关闭即不执行） |
| 默认关闭零事件 | ✓（E3.5.1 + 本阶段 B 的 `trace_events_b=0`） |
| 调度候选/排序/victim/响应与 baseline 一致 | ✓（trace 代码不触碰决策路径） |
| 残余：trace_id 解析（每请求 JSON 键检查+字符串校验） | 存在但非"cell 遍历/格式化/日志构造"（任务五允许范围外）；测量证明不可检测（见第 6 节） |

**结论：无需最小性能修复，llama.cpp 不建新提交**（B 保持 `aba4b26a`）。

---

## 4. 预注册协议（manifest 冻结）

- 30 个 ABBA blocks（`A B B A` / `B A A B` 交替）+ 10 个 A/A placebo
  blocks；每 server：全新进程 → health → warmup（1 短请求，不计入）→
  固定批次（4 client × 6 轮 multi_turn = 24 并发请求）→ SIGTERM；
- 排除启动/加载时间，只统计批次 wall；记录吞吐/p50/p95/CPU process
  time/GPU 温度功耗/失败/响应 hash；
- 统计：`delta = (mean(B)-mean(A))/mean(A)`（块内）、median/p95、
  bootstrap 95% CI、正负方向、placebo median/p95；
- 严格门槛：median <0.5%、p95 <1%；**噪声校正规则 7 条**（预注册写入
  manifest，见第 6 节）。

---

## 5. 测量执行

- 30 ABBA + 10 placebo 全部完成（含第一次 runner 多线程 out 字典 bug
  修复后的完整重跑；placebo 命名 bug 修复后补跑）；
- 每 block 全新 server、A/B 交替、无中途调整。

---

## 6. 统计结果与门槛判定

| 指标 | 值 |
|---|---|
| ABBA blocks 有效 | 30 |
| **delta median** | **-1.967%**（B 快 1.97%） |
| delta p95 | **+15.279%** |
| delta bootstrap 95% CI | (-5.376, +2.201)（含 0） |
| 正负方向 | 11 正 / 19 负（positive_ratio 0.367） |
| placebo median / p95 | **-1.009% / +1.055%** |
| CPU process-time median diff（B-A） | +3.006% |
| 失败 | A=0、B=0 |
| B 默认关闭 trace 事件 | **0** |
| 响应 hash 交集（A/B） | 62.7% |
| 响应 hash 交集（A/A placebo 对照） | 70.5%（4B 采样非确定，与 trace 无关） |

**严格门槛**：median -1.967% < 0.5% ✓（B 不慢）；p95 15.28% ≥ 1% ✗ →
启用噪声校正。

**噪声校正逐项（预注册 7 条）**：

| # | 规则 | 结果 |
|---|---|---|
| 1 | placebo p95 >1% | ✓（1.055%——环境噪声确认） |
| 2 | ABBA median <0.5% | ✓（-1.967%——B 不慢） |
| 3 | ABBA 与 placebo p95 差 ≤0.25pp | ✗（|15.28-1.06|=14.2pp） |
| 4 | 方向对称，单侧 ≤60% | ✗（37% 正——B 偏快） |
| 5 | CPU process-time median <0.5% | ✗（+3.0%） |
| 6 | 吞吐无稳定负方向 | ✓ |
| 7 | 无失败/响应/行为差异 | 部分（失败 0 ✓；hash 62.7% vs placebo 70.5%——相当，4B 非确定非 trace；字面不满足） |

**噪声校正不通过（规则 3/4/5）**。

---

## 7. 结果解释

- **无证据表明 candidate 有真实默认关闭开销**：delta median -1.97%（B
  反而快）、bootstrap CI 含 0、B 零事件、失败 0——**若 trace 关闭有
  系统性开销，delta 应单侧为正**；
- **但环境噪声使"证明无开销"不可靠**：ABBA p95 15.3% 远大于 placebo p95
  1.06%——**消费级 GPU 长时（~2 小时）测量的温度/boost 频率漂移使块内
  A1/A2 跨度（~4 分钟/server）引入 >1% 漂移**——ABBA 块内配对被温度
  非线性破坏；
- placebo（A/A 相邻）p95 仅 1.06% → **相邻测量的环境噪声 ~1%**；ABBA
  块内漂移（4 个 server 跨度）更大；
- hash 差异（62.7%）与 A/A placebo（70.5%）相当 → **4B 并发采样非确定**，
  非 trace 导致；
- CPU +3.0%：进程级 CPU 时间受采样/调度影响，72 次/批次的 if 检查
  理论开销 <0.1%——**噪声**。

---

## 8. 门禁判定

**`HOLD_FOR_PERFORMANCE_EVIDENCE`**

- attribution 契约（E3.5.1）：**全过**（request mapping 100%、链 100%、
  错配 0、完整性 0）；
- 默认关闭零事件、失败 0、无 trace 系统性开销证据（B 快 2% 反证）；
- **但**：预注册噪声校正规则 3/4/5 不满足（p95 差 14pp、方向 37%、CPU
  3%）——**环境噪声（消费级 GPU 长时测量温度漂移）无法使 candidate 与
  baseline 在 <1% 精度上区分**；
- 不将 HOLD 命名为 attribution gap（attribution 已闭环；是性能证据不足）；
- 无 REJECT 触发（无默认关闭开销复现、无事件、无行为/响应/隐私变化）。

---

## 9. 下一步精确输入（解除 HOLD）

1. **更稳定测量环境**（推荐）：数据中心 GPU / 恒温机房 / 缩短块跨度
   （块内交替顺序微调）重测——目标让 ABBA p95 与 placebo p95 收敛
   （差 ≤0.25pp）；
2. **或用户决策**：接受"B 快/无系统性正 delta + 零事件 + 静态审计
   （trace_event 首行 return）+ placebo 噪声 1%"作为默认关闭无开销证据，
   放行 field trace 接入（需用户明确确认放宽规则 3/4/5 的理由）；
3. **field trace**：未提供（`field_trace_not_provided`）；用户提供且
   validator 通过后，方可生成 E3.6 指令；
4. 本阶段不追加 replicate（禁止范围）；不自动开始 E3.6。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e352/`（manifest/summary/abba_results.csv/
  placebo_results.csv + 140 per-block JSON）；
- llama.cpp：**无新提交**（B 保持 `aba4b26a`；静态审计无必要修复）；
- 根仓库提交：`4455799`（test: verify disabled lifecycle diagnostics
  overhead，6 文件 +722 行）；llama.cpp 无新提交（B 保持 `aba4b26a`）；
- 测试：根 pytest **154 passed**（含 test_e352_overhead 6 例）；llama.cpp
  lifecycle trace 7/7 + unified lifecycle 5/5 + slot routing 10/10 + E1 5/5
  （复用，无改动）；
- 提交后两仓库 `git status --short` 均为空。
