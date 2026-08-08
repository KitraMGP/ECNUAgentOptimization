# E15.1：分支推理内存共享——不变量加固与真实 fan-out 验证报告

> 阶段：E15.1（A1：既有 bitset 引用与共享源保护的不变量加固 + 分支并发 paired 验证）
> 日期：2026-08-08
> 关联计划：`docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` §2.3（A1）、§7（E15.1/E15.2）
> **最终判定：`PASS`**（10/10 门禁 G0–G9 全过；**零 llama.cpp 核心代码改动**，纯测试 + benchmark 扩展）
> 约束遵守：不重开 E2.5 prefix-branch 路由；未新增 refcount/guard 状态；未将 metadata
> sharing 称为 paged COW；未改变 `physical_sharing=false`；未实现 radix tree。

---

## 0. 执行摘要

在真实 TinyLlama stories260K（attention-only，CPU build）上验证：现有 C1
`--kv-prefix-share` / `seq_cp`（bitset 元数据共享，零数据拷贝）在**真实分支 fan-out**
（共同前缀预热于 source slot 并 idle，3 个分支 target 在独立 slots **并发**发出，
ThreadPoolExecutor + barrier）中**确定生效**：

- **共享生效**：`shared_cells` 每 replicate = **454**（= 共享前缀 token 数，4 个 seq
  引用同一物理 cell 集）；server 日志每 replicate 恰好 **3 次** `E6-C1: shared` 事件；
- **收益**：on 模式 target recompute（`timings.prompt_n`）中位数 **45–49** vs off 模式
  **499–503**，**降幅 90.3%–91.0%**（门槛 ≥25%，12.11 exact-prefix recompute 指标）；
- **正确性**：on/off 输出 **token ids 逐位一致**（5 paired replicates × 3 targets = 0
  mismatch，`return_tokens=true` 真实 ids）；无跨分支 canary/state 污染；
- **不变量**：12.13.8（删除一个 target 只释放其引用）/12.13.9（最后引用删除后资源
  回收）cell 计数中间态显式断言通过；每 replicate 清空后 `used_cells==0 &&
  active_sequences==0` 回基线；`physical_sharing` 恒 `false` 且
  `shared_cells_semantics` 正确（metadata-level，非 COW）；
- **保护**：非完整前缀请求不得覆盖共享源（1951-1958 skip 保护），probe 后
  `shared_cells` 仍 = 454；
- **shutdown clean**：off/on 两模式 SIGTERM 退出码 0。

审计确认：**无需新增任何 refcount/guard 状态**——cell 的 seq bitset 即等价引用计数
（`llama-kv-cells.h` `seq_add/seq_rm/seq_has`）；**共享源覆盖/clear 保护已存在**
（`server-context.cpp:1951-1958` 非完整前缀 skip、`:2103-2108` idle 清理 skip）。

---

## 1. 审计结果（先审计后修改）

### 1.1 实现现状（代码级核验）

| 机制 | 位置 | 行为 |
|---|---|---|
| C1 跨 slot 前缀共享 | `tools/server/server-context.cpp:3851-3907` | LCP 扫描 idle sources → `seq_rm` 清自身旧 KV → `seq_cp` 共享前缀 → 只算剩余 |
| 能力 gate | `server-context.cpp:1497-1515` | min-lcp 负值拒绝；memory null / 不支持共享 / hybrid / recurrent / SWA 拒绝 |
| `seq_cp` 同 stream | `src/llama-kv-cache.cpp:447-474` | **bitset 元数据共享，零数据拷贝**；cell 追加 dst seq |
| bitset 即引用计数 | `src/llama-kv-cells.h:238/301/309` | `seq_rm`（删引用，空 cell 才回收）/`seq_has`/`seq_add` |
| 共享源保护 | `server-context.cpp:1951-1958`（分配 skip）、`:2103-2108`（清理 skip） | 非完整前缀不得占用共享源；idle 清理不清共享源 |
| `/metrics/kv` 契约 | `server-context.cpp:3115-3133` | `shared_cells` = 被 >1 seq 引用的 cell 数；`physical_sharing=false`；`shared_cells_semantics="multi-sequence cell association (metadata-level, not COW)"` |
| 物理共享统计 | `src/llama-kv-cache.cpp:797` | `stats.physical_sharing = false`（既有契约，本阶段未改） |

### 1.2 现有测试缺口（E15.1 补足依据）

| 缺口 | 现有覆盖 | E15.1 补充 |
|---|---|---|
| **真实并发**分支 fan-out | `test_kv_prefix_share*.py` 全部为顺序请求 | `test_e15_branch_shared.py::test_concurrent_fanout_paired_off_on`（barrier+TPE 并发 3 targets） |
| 12.13.8 删除**中间态** cell 计数 | `test_target_removed_source_still_works` 只验功能不验计数 | `test_erase_middle_target_releases_only_its_refs`（erase 一个 target → used 降、shared 保持 >0；全删 → 0/0） |
| metrics 契约在分支场景断言 | `test_metrics_kv.py` 无分支场景 | `test_metrics_kv_contract_on_branch_scenario` |
| 并发 canary 泄漏检测 | `test_canary_no_cross_session_leak`（顺序） | `test_concurrent_canary_no_cross_branch_leak` |
| benchmark 侧分支并发 paired runner | 无（`workload/branch.py` 顺序、无 off/on 对照、无 recompute 度量） | `benchmark/runner/e15_branch_concurrent.py` |

### 1.3 12.13 十五条不变量映射（本阶段可测项）

| 12.13 项 | 验证方式 | 结果 |
|---|---|---|
| 1 refcount=引用数 | shared_cells=454 实证（4 seq 引用同一前缀 cell 集）；代码审计 seq bitset 即等价引用计数 | PASS |
| 8 删除只释放本 seq 引用 | `test_erase_middle_target_releases_only_its_refs`：erase 1 target → used 下降、shared 保持 >0 | PASS |
| 9 最后引用删除后回收 | 同上：全部 erase → `used_cells==0 && active_sequences==0` | PASS |
| 14 shutdown 后资源可回收 | off/on 均 SIGTERM exit_code=0；每测试后 `server.stop()` | PASS |
| 7 partial 追加前唯一所有权 | 共享后 target 在分支点后生成，与 source 分支 cell 无冲突（输出一致实证） | PASS（行为验证） |
| 15 溢出/上限 | `seq` bitset 定长 `LLAMA_MAX_SEQ`，`seq_count` 不可能溢出（代码审计）；无新增计数器 | N/A（代码级） |

---

## 2. 改动文件清单

**新增（根仓库 benchmark/，已提交 f357a42）**：
- `benchmark/runner/e15_branch_concurrent.py` —— E15 branch concurrent paired runner
  （纯函数：prompt 构造/输出提取/recompute/canary 检测/gate_verdict；server 生命周期；
  replicate 协议；并发执行；结果 JSON 落盘）
- `benchmark/tests/test_e15_branch_concurrent.py` —— 35 个纯函数 pytest
  （不依赖 server/GPU；gate_verdict 全部分支判定 + G0 数据完整性 + CLI 参数校验）

**新增（llama.cpp 独立 E15 test 文件，已提交 4a699aaad，未改任何核心代码）**：
- `llama.cpp/tools/server/tests/unit/test_e15_branch_shared.py` —— 4 个真实 server
  集成测试（并发 fan-out off/on paired、12.13.8/9 中间态、metrics 契约、并发 canary）

**未改动**：llama.cpp 任何核心源码；E6–E14 历史报告/raw；`physical_sharing=false` 契约。

---

## 3. 实验协议与配置

- **模型**：`stories260K-f32.gguf`（TinyLlama stories260K，attention-only，
  非 hybrid/recurrent/SWA——C1 能力 gate 允许的唯一路径；Qwen3.5-4B hybrid 永久禁用不变）
- **server**（off/on 两模式，**仅共享开关不同**）：`llama-server -ngl 0 --ctx-size 2048
  --kv-unified --parallel 5 --cache-ram 0 --slot-save-path <tmp> --no-webui`
  （on 追加 `--kv-prefix-share --kv-prefix-share-min-lcp 64`）
- **请求**（全部请求一致）：`temperature=0, seed=42, n_predict=8, cache_prompt=true,
  return_tokens=true`；source 预热 `id_slot=0`；分支 targets `id_slot=1/2/3`
- **workload**：共同前缀（tokenized 454 tokens）+ 分支后缀（首 token 互异 → LCP 精确
  终止于前缀末尾；各含唯一 canary）；每 replicate：清空 slots 并断言
  `used_cells==0 && active_sequences==0` → source 预热 → 3 分支 target **并发**
  （ThreadPoolExecutor + barrier）→ 采样 `/metrics/kv` → 非前缀保护探针（自由路由）
  → 记录日志 `E6-C1: shared` 事件数 → 清空回基线
- **统计**：warmup=2（丢弃）+ formal reps=5（配对比较）；recompute = `timings.prompt_n`

---

## 4. 原始结果（正式 reps，reps=5 全列）

### 4.1 off 模式（`--kv-prefix-share` 关闭，对照）

| rep | source prompt_n | target b1/b2/b3 prompt_n (cache_n) | used_cells | shared_cells | active | probe 后 shared | log shared |
|---|---|---|---|---|---|---|---|
| 1 | 492 | 500/499/503 (0) | 2022 | **0** | 4 | 0 | 0 |
| 2 | 492 | 500/499/503 (0) | 2022 | **0** | 4 | 0 | 0 |
| 3 | 492 | 500/499/503 (0) | 2022 | **0** | 4 | 0 | 0 |
| 4 | 492 | 500/499/503 (0) | 2022 | **0** | 4 | 0 | 0 |
| 5 | 492 | 500/499/503 (0) | 2022 | **0** | 4 | 0 | 0 |

### 4.2 on 模式（`--kv-prefix-share` 开启）

| rep | source prompt_n | target b1/b2/b3 prompt_n (cache_n) | used_cells | shared_cells | active | probe 后 shared | log shared |
|---|---|---|---|---|---|---|---|
| 1 | 492 | 46/45/49 (**454**) | **660** | **454** | 4 | 454 | 3 |
| 2 | 492 | 46/45/49 (**454**) | **660** | **454** | 4 | 454 | 3 |
| 3 | 492 | 46/45/49 (**454**) | **660** | **454** | 4 | 454 | 3 |
| 4 | 492 | 46/45/49 (**454**) | **660** | **454** | 4 | 454 | 3 |
| 5 | 492 | 46/45/49 (**454**) | **660** | **454** | 4 | 454 | 3 |

### 4.3 关键解读

- **共享前缀 454 tokens 被 source + 3 targets 共 4 个 seq 引用** → `shared_cells=454`
  （= 前缀 token 数，物理 cell 只占一份：on 模式 `used_cells=660` vs off 模式 `2022`，
  物理 cell 占用 **-67.4%**——metadata 共享的直接容量证据，但按 E15 契约不称 COW）；
- **recompute 降幅**：b1 90.8%、b2 91.0%、b3 90.3%（off median 499-503 → on 45-49）；
- **输出一致性**（`return_tokens=true` 真实 token ids，5×3=15 对全一致，0 mismatch）：
  - b1: `[416,416,412,432,269,410,460,412]`
  - b2: `[411,295,432,410,460,412,264,422]`
  - b3: `[432,410,460,412,276,361,419,410]`
  - 内容 sha256（on/off 逐 target 相同）：`a7760575…` / `22a5621f…` / `bf45cb64…`
- **每 replicate**：清空后 `used_cells==0 && active_sequences==0`（12.13.9 e2e 版）；
  probe 后 `shared_cells` 保持 454（12.13 共享源保护实证）；
- **G6 可判别证据（第二轮修复后重跑）**：protect probe（自由路由）全部落到
  `id_slot=4`（≠0，未占用 source slot 0——1951-1958 分配 skip 保护直接实证）；
  probe 后 `shared_cells=454` 保持；probe 后重验 target（自由路由）`cache_n=499 /
  recompute=1` —— source 前缀仍可被共享（功能级判别，非仅状态计数）；
- **server 日志**：on 模式每 replicate 恰好 3 次 `E6-C1: shared 454-token prefix from slot 0`；
  off 模式 0 次；capability 日志 `E8-C1: capability accepted`（on 模式）。

### 4.4 门禁逐项（gate_verdict 原始输出）

| 门禁 | 结果 | 证据 |
|---|---|---|
| G0 数据完整性（source/每 target：HTTP=200、无 error、tokens/content 非空、recompute 非 None） | PASS | 全部 formal reps source+targets 完整 |
| G1 on/off 输出逐 target 一致（token ids + content_sha256 双比较；branch 集相同） | PASS | 5 paired reps 输出 token ids 与内容 sha256 逐位一致（0 mismatch） |
| G2 on shared_cells > 0 | PASS | 每 rep 454 |
| G3 on recompute 相对 off 降 ≥25% | PASS | 90.3%–91.0%（门槛 25%） |
| G4 off shared_cells == 0 | PASS | 每 rep 0 |
| G5 并发 target 无跨分支 canary 污染（targets + source + protect probe 文本） | PASS | 输出/源码/probe 文本均不含他分支 canary（仅检查 content） |
| G6 source 非完整前缀覆盖被拒/skip（可判别协议，按每个 on rep 独立判断） | PASS | probe id_slot!=0 + probe 后 shared_cells 仍 454 + 重验 target cache_n>0 |
| G7 每 replicate 清空后回基线 | PASS | 全部 reps used_cells==0 && active==0 |
| G8 shutdown clean | PASS | off/on 均 exit_code=0 |
| G9 physical_sharing 恒 false 且 semantics 正确 | PASS | 每采样 `physical_sharing==false`、`shared_cells_semantics` 正确 |

### 4.5 代码审查修复（2026-08-08，`e15_branch_concurrent.py` + 对应测试）

针对代码审查问题修复（不改变实验协议与原始结果，纯函数判定层加固）：

1. `/completion` 请求带 `return_tokens: true`；`output_ids_of` 只读真实 `tokens` list，
   绝不读整数 `tokens_predicted`（n_decoded 计数）；`content_sha256` 保留为冗余一致性门禁。
2. 新增 **G0 数据完整性**：每 formal rep source/每个 target 要求 HTTP=200、无 error、
   tokens 非空、content 非空、recompute 非 None；任一失败直接 `REJECT_CORRECTNESS_OR_ISOLATION`
   （不能落入 HOLD）。
3. `gate_verdict` 对 off/on 空数据、rep count<1 → `INVALID`（不进入门禁判定）；
   branch 集不完整 → G1 FAIL → REJECT；CLI 前置校验 `warmup>=0`、`reps>=1`、
   `branches` 仅 2/3、`parallel>=branches+2`（source+targets+protect probe spare slot）。
4. G1 同时比较 token ids 与 content_sha256；off/on branch key 集必须相同（全局 + 每配对 rep）。
5. canary 检测只检查解码文本 content，不拼 token ids（整数 ids 中不可能出现字符串标记）。
6. G6 按**每个 on rep 独立**判断该 rep 是否发生共享（不能用全局 `on_any_shared` 掩盖单 rep
   覆盖）；protect probe 缺 spare slot 由 CLI 前置校验避免。
7. shutdown/clean 失败保持 REJECT（G7/G8 仍在 correctness 门禁集）。
8. 单测补齐至 35 个：真实响应形态（tokens_predicted int / tokens list）、全/单 target 失败、
   source 失败、recompute missing、空 reps → INVALID、rep count mismatch、branch missing、
   content hash mismatch、单 rep no share → 该 rep G6 N/A、CLI 参数校验。

测试：`uv run pytest tests/test_e15_branch_concurrent.py -q` → `35 passed`（仅该文件，未跑全量）。

**（第一轮修复后状态）**：真实 server 实验当时未重跑，判定逻辑由纯函数单测覆盖；
第二轮修复后已用当前 runner 真实重跑 TinyLlama CPU paired smoke —— 结果见 **§4.7**
（verdict `PASS`，10/10 门禁，含可判别 G6）。

### 4.6 第二轮代码审查修复（2026-08-08，独立 reviewer 结论）

1. **G6 恒真盲区修复（可判别协议）**：核实 `/completion` 非 OAI 响应
   （`server-task.cpp` `server_task_result_cmpl_final::to_json_non_oaicompat`）含真实
   `id_slot` 字段。protect probe 自由路由时记录响应实际 `id_slot`，要求 **!= 0**
   （不得占用 source slot 0，直接验证 server 侧共享源分配 skip 保护 1951-1958）；
   并新增 **probe 后重验请求**（on 模式，probe 后重新请求 target 自由路由，
   `cache_n > 0` 证明 source 前缀仍可被共享——功能级判别）。probe 前后
   `shared_cells` 作为状态证据保留但**不单独作判据**（禁止只断言 shared_cells>0）。
   off 模式无共享语义，跳过重验以保持其 KV 容量。
2. **生命周期 try/finally**：每个 mode server 生命周期（start → replicate × N → stop）
   以 try/finally 包裹；`ready=False` 与 replicate 异常均 stop server 不泄漏进程；
   异常时保留结构化 `fatal_error`（stage/type/message）并确保部分原始结果落盘
   （含已完成的 replicates 与 shutdown 证据）。
3. **CLI**：`--ctx-size` 默认 512 → **2048**；校验正值，并新增容量前置估算
   （off 模式全量 KV 峰值 = source+Σ(target+predicted) < ctx，`est_tokens` 保守 2.5
   chars/token），避免开箱必败（unified KV 池溢出 Context exceeded）。
4. **fail-safe 与畸形数据**：`output_ids_of` 对异常 token 元素（None/字符串/嵌套）
   跳过不崩溃；G0 对 branch missing/None/**重复**（dict 覆盖会掩盖 G1 比较）判违规
   REJECT；G1/G3 用 str key 排序保证畸形 branch 也不崩溃。
5. **G5 扩展**：除 targets 外，还检查 source（不得含 b1/b2/b3 canary）与 protect
   probe（自由路由非前缀请求，不得含任何分支 canary）的可观测文本。
6. **source 失败清理**：source 请求失败提前返回时仍执行 post-clean（erase +
   回基线断言）并如实记录 `clean_post`/`clean_ok`。
7. 文档统一 **G0–G9 = 10/10**，删除 9/9 遗留。
8. 单测补齐至 **48 个**：G6 非恒真（probe id_slot=0 / recheck cache_n=0 / recheck
   HTTP fail）、G0 branch missing/None/重复、G5 source/probe canary、output_ids_of
   fail-safe、CLI ctx 校验（非正/过小）、runner not-ready / replicate 异常清理与
   部分落盘。

### 4.7 第二轮修复后真实重跑（2026-08-08，覆盖 `results/e15_branch_smoke.json`）

- **命令**：`uv run python runner/e15_branch_concurrent.py --server-bin
  ../llama.cpp/build/bin/llama-server --model <stories260K-f32.gguf> --port 8091
  --ctx-size 2048 --parallel 5 --cache-ram 0 --min-lcp 64 --warmup 2 --reps 5
  --branches 3 --out results/e15_branch_smoke.json`（真实 TinyLlama CPU，off/on ×
  2 warmup + 5 formal reps）；
- **纯测试先行**：`uv run pytest tests/test_e15_branch_concurrent.py -q` →
  `48 passed`（仅该文件）；
- **新 verdict：`PASS`（10/10 门禁 G0–G9 全过）**；新增 G0 出现在 gate 输出；
  G6 为可判别输出：probe `id_slot=4`（≠0）×5、probe 后 `shared_cells=454`、
  重验 `cache_n=499 / recompute=1` ×5（5/5 reps protected, 0 N/A）；
- **server clean**：off/on 均 SIGTERM `exit_code=0`；重跑后无 llama-server 进程残留
  （`pgrep -x llama-server` 为空）；
- 关键数值与首版一致：off prompt_n 500/499/503、on 46/45/49、shared_cells=454、
  recompute 降幅 90.3%–91.0%、off shared=0、输出 5×3 对 token ids + sha256 全一致；
  `results/e15_branch_smoke.json` 已覆盖更新（旧文件备份于会话内 /tmp）。

**剩余风险**：① 并发语义——llama-server 请求处理为单线程队列，HTTP 层 barrier 并发下
target 实际串行进入处理循环（首版已注明，不影响共享链证据）；② 容量——off 模式 4 并发
全量 KV 2022/2048，更大并发/更长 prompt 需按 §7.3 约束调参；③ 模型域——仅 TinyLlama
attention-only，Qwen3.5-4B hybrid 由 capability gate 永久禁用共享，不在本阶段范围；
④ 本阶段所有改动（根仓库 + llama.cpp）均未提交，等待用户授权（2026-08-08 收口：已提交，根仓库 f357a42 / llama.cpp 4a699aaad）。

---

## 5. 复现命令

```bash
# 0) 前置：CPU build 已存在（llama.cpp/build/bin/llama-server）；TinyLlama 已缓存
#    （llama.cpp/tmp/models--ggml-org--test-model-stories260K/.../stories260K-f32.gguf）

# 1) llama.cpp 侧 E15 单测（真实并发 fan-out / 12.13 中间态 / metrics 契约 / canary）
cd llama.cpp/tools/server/tests
LLAMA_SERVER_BIN_PATH=$PWD/../../build/bin/llama-server \
LLAMA_CACHE=$PWD/../../tmp \
python3 -m pytest unit/test_e15_branch_shared.py --noconftest -v

# 2) benchmark 侧纯函数测试（不依赖 server/GPU）
cd benchmark
.venv/bin/python -m pytest tests/test_e15_branch_concurrent.py -q

# 3) 分支并发 paired smoke（off/on × warmup=2 + reps=5；seed=42/temp=0）
cd benchmark
.venv/bin/python runner/e15_branch_concurrent.py \
  --server-bin ../llama.cpp/build/bin/llama-server \
  --model ../llama.cpp/tmp/models--ggml-org--test-model-stories260K/snapshots/479896ec924af6d40fd419ab8f4d1eb2101de00d/stories260K-f32.gguf \
  --port 8091 --ctx-size 2048 --parallel 5 --cache-ram 0 --min-lcp 64 \
  --warmup 2 --reps 5 --branches 3 --out results/e15_branch_smoke.json
```

原始结果：`benchmark/results/e15_branch_smoke.json`（每 replicate 完整记录：
HTTP/错误、prompt_n/cache_n、输出 token ids + 内容 sha256、latency、/metrics/kv 全字段、
日志 shared 事件 delta、清空前后基线断言）。

---

## 6. 判定：PASS

- **审计无缺口需代码修复** → 纯测试 + benchmark 扩展完成（A1 计划原判路径）；
- 10/10 门禁全过（G0–G9）；12.13 可测项不变量全绿；无崩溃/污染/悬空；
- **结论表述（严格语义）**：现有 C1 `--kv-prefix-share` / `seq_cp` 在 attention-only
  真实分支并发 fan-out 中**生效**：metadata 前缀共享（`shared_cells=454`）、recompute
  降幅 90%+、输出 token 级一致。**不宣称物理共享/COW**（`physical_sharing=false` 契约
  保持）；不宣称 Qwen3.5-4B hybrid 上任何收益（hybrid 由 capability gate 永久禁用）。

## 7. 剩余风险与限制

1. **模型域**：证据仅限 TinyLlama stories260K attention-only（CPU）。Qwen3.5-4B
   hybrid 因 capability gate（`llama-memory.h:122` 默认 false + `server-context.cpp:1497-1515`）
   永久禁用共享，**不在本阶段范围内**（与 E12/E13 结论一致）。
2. **并发语义**：llama-server 请求处理为单线程队列，HTTP 层并发（barrier）下 target
   实际串行进入处理循环；共享链（source idle → 每 target 独立 seq_cp）不受影响，日志
   与 metrics 实证共享对每个 target 均发生。若未来引入 server 内真正的多线程 slot 并行，
   需重验（本阶段不承诺）。
3. **容量配置**：unified KV cell 池 = ctx（2048）；off 模式 4 并发全量 KV 需 2022 cells
   （余量 26）。更大的并发/更长的 prompt 需按 `off_cells = Σ(prompt_n_i+predicted_n) < ctx`
   约束调参，否则 off 模式会触发 batch 重试/Context exceeded（初版 smoke 在 ctx=1024 时
   实测复现，已通过短分支后缀 + ctx=2048 消除——见下）。
4. **实验过程中发现并已修正的问题**（非 llama.cpp 缺陷）：
   - ctx=1024 时 off 模式 4 并发超池（2022>1024）→ 缩分支后缀 + ctx=2048；
   - `/completion` 输出 token ids 需 `return_tokens=true`（默认 false 时 `tokens` 为空，
     首版 G1 为空比较，已修正为真实 ids 比较）；
   - `/slots/{id}?action=erase` 需 `--slot-save-path`。
5. **性能数值**：latency（off ~98ms vs on ~18-20ms/target）为 CPU f32 TinyLlama 数据，
   仅作共享行为的辅助证据，不作主模型性能声明；正式收益门槛按 12.11 recompute 指标判定。
6. **未提交**：本阶段所有改动（根仓库 + llama.cpp）均未提交，等待用户授权（E6 §12.20
   提交规则；2026-08-08 收口：已提交，根仓库 f357a42 / llama.cpp 4a699aaad）。
