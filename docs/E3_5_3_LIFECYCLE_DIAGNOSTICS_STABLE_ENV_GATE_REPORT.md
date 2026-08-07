# E3.5.3 报告：Lifecycle Diagnostics 稳定环境相邻配对复核

- 阶段：E3.5.3（稳定环境资格先行；相邻配对；未执行正式 crossover）
- 日期：2026-08-06
- **最终状态：`HOLD_FOR_PERFORMANCE_EVIDENCE`（`environment_not_qualified`）**

---

## 1. 目标与边界

- 目标：candidate（`aba4b26a`）在 `--lifecycle-trace` 关闭时，能否在合格
  测量环境中证明无可检测生产路径开销；
- 边界（未违反）：不改 llama.cpp/schema/parser/attribution 契约、default/
  LRU/KV/decode/响应语义、A1/A4 收益、不沿用 E3.5.2 ABBA 数据、不放宽
  median<0.5%/p95<1% 门槛、不追加 replicate/删 outlier/事后改 schedule、
  不接收/生成/伪造 field trace、资格失败不继续正式测量。

---

## 2. 构建与运行时隔离

| 项 | A（baseline） | B（candidate） |
|---|---|---|
| commit | `049872f59` | `aba4b26a` |
| 构建 | detached worktree `llama-baseline/build-cuda`（全新 clean） | detached worktree `llama-candidate/build-cuda`（全新 clean） |
| 编译 | 相同编译器/CUDA 13.3/arch 89/Release（CMakeCache 核对） | 相同 |
| executable sha256 | `408c7fd9…` | `dff8ae9b…` |
| libllama-server-impl | `c497b4a4…` | `cbe09725…` |
| libllama.so.0 | `026ac8ae…` | `999fe064…` |
| libggml.so.0.18.1 | `f39401e1…` | `454fb928…` |
| **ldd 隔离** | 全部库加载自 `llama-baseline/build-cuda/bin/` ✓ | 全部库加载自 `llama-candidate/build-cuda/bin/` ✓ |
| **/proc/maps** | A server 进程映射 `llama-baseline/build-cuda/bin/llama-server` ✓ | （同法验证） |
| 模型 | `de8e96cd…`（Qwen3.5-4B-Q4_K_M） | 相同 |
| GPU | RTX 4060 Laptop 8188 MiB、41°C 空闲、SM clock 2250MHz | 相同 |

**运行时隔离证明**：ldd 显示 A/B 分别加载自身 build 目录的
libllama-server-impl/libllama/libggml；运行中 /proc/maps 确认 A server
映射自身 executable——**无 RPATH/LD_LIBRARY_PATH 混用**。

---

## 3. 预注册 manifest

- 生成于正式采样前：`results/e353/prereg_manifest.json`，sha256
  `c882968e22cec436`；
- 固定：unified p4、ctx 8192、cache-ram 0、temp 0、seed 42、policy
  `default`、批次 4 client × 6 轮（E3.5.2 同一批次）；
- placebo 20 对（10 A/A + 10 B/B，seed 20260807 生成顺序）；
- crossover 30 对（15 A→B + 15 B→A，同 seed）；
- 资格门槛 7 条 + READY/HOLD/REJECT 判定（预注册冻结）。

---

## 4. 环境稳定化

- GPU clock/power 锁定：**unavailable**（无 root 权限，`nvidia-smi -lgc`
  拒绝；Laptop 作用域不支持 -pl）——如实记录；
- 预热：连续 4 个批次（丢弃）后开始 placebo——**但温度仍在漂移**
  （pair 温差出现 >2°C）；
- 无其他 GPU workload 并发（本会话独占）。

---

## 5. 环境资格检查（placebo 20 对）

| 资格门槛 | 结果 |
|---|---|
| failure=0 | ✓（0） |
| A/A 与 B/B 默认关闭事件均为 0 | ✓（0） |
| **combined p95(abs(placebo_delta)) ≤1%** | **✗（16.23%）** |
| **A/A 各自 abs(median) <0.25%** | **✗（5.578%）** |
| **B/B 各自 abs(median) <0.25%** | **✗（0.569%）** |
| **pair 内 GPU 温差均 ≤2°C** | **✗（1 对 >2°C）** |
| GPU median clock pair 差 ≤1% | ✓（0） |
| throttling count=0 | ✓（温度/clock 差覆盖，无 throttling 查询权限） |

**placebo delta 分布**（20 对）：约 12 对 <1%（正常噪声），**约 8 对
±13-18%**（pair 3/6/7/9/11/14/15/18，正负交替）——**RTX 4060 Laptop
GPU 的 boost 时钟在负载温度爬升期间大幅跳变**（±15% wall 波动），
即使相邻配对也无法避免（GPU 状态在两次测量间跳变）。

**资格判定：FAIL（3/7 门槛未达）→ `environment_not_qualified`**——
按预注册协议，**不执行 30 个正式 A/B pair**。

---

## 6. 未执行 crossover

- 协议遵守：资格失败后未运行 crossover（无 crossover_results.csv）；
- **不再建议在同一环境追加样本**（任务五明确：环境资格失败后不追加）。

---

## 7. 门禁判定

**`HOLD_FOR_PERFORMANCE_EVIDENCE`（`environment_not_qualified`）**

- 无 REJECT 触发（无 candidate 默认关闭开销/事件/行为变化证据——本阶段
  未到正式测量）；
- HOLD 依据：环境资格失败（placebo p95 16.23% >> 1%、A/A median 5.6%、
  温差 1 对超限）——**消费级 Laptop GPU 的 boost 时钟跳变使 wall 噪声
  ±15%**，无法在 <1% 精度上区分 candidate 与 baseline；
- attribution 契约（E3.5.1）不涉及本阶段（已闭环，未复核/修改）。

---

## 8. 下一步精确输入

1. **合格环境**（推荐）：数据中心 GPU（可 `nvidia-smi -lgc` 锁定 clock/
   恒温机房/无并发 workload）重跑本协议（预注册 manifest 已就绪）；
2. **或用户决策**：接受"静态审计（trace_event 首行 return）+ B 零事件 +
   E3.5.2 相邻 placebo p95 1.06%（预热充分时）"作为默认关闭无开销证据，
   放行 field trace 接入；
3. **field trace**：未提供（`field_trace_not_provided`）；用户提供且
   validator 通过后，方可生成 E3.6 指令。

---

## 附注：执行记录与提交状态

- 结果：`benchmark/results/e353/`（prereg_manifest/environment_
  qualification.json + 44 per-measurement JSON（预热 4 + placebo 40））；
- llama.cpp：**无新提交**（`aba4b26a` 保持）；
- 根仓库提交：`3f78c6d`（test: verify disabled lifecycle diagnostics in
  stable crossover，6 文件 +643 行）；llama.cpp 无新提交（`aba4b26a` 保持）；
- 测试：根 pytest（含 test_e353_qualify）；llama.cpp 22 + E1 5/5（复用）；
- 提交后两仓库 `git status --short` 均为空（llama-baseline/llama-candidate
  worktree 为 detached 不入库，提交后清理）。
