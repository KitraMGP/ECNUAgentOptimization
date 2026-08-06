# E1 收尾验证报告（E1_VALIDATION_REPORT）

> 阶段：E1 集成测试收尾 + 指标语义/结果结构最终检查 + E2 前置审查
> 前提：E1 已实现（llama.cpp `b69773a1e` + 根仓库 `15658d4`/`5f51e11`/`a0c77cc`/`f9d3e3d`）
> 本阶段**未实现** E2 任何优化，仅测试收尾与审查。

---

## 1. E1 集成测试是否真实执行

**是。** `llama.cpp/tools/server/tests/unit/test_metrics_kv.py` 已在真实 llama-server（tinyllama stories260K，CPU build）上执行并通过（5/5）。

关键点：
- 测试模型 `ggml-org/test-model-stories260K`（stories260K-f32.gguf，1.18MB）已通过 hf-mirror 下载并放入 llama.cpp HF 缓存（`tmp/models--ggml-org--test-model-stories260K/`，含 `refs/main` 与 `snapshots/<commit>/` 标准结构）；
- `conftest.py` 的 module-scope fixture 会 `ServerPreset.load_all()` 下载全部 preset 模型（含 tinygemma3 ~3GB 等），本环境网络受限导致**完整 pytest 运行阻塞** → 采用手动驱动脚本复现 `create_server` fixture 逐个执行 5 个测试（`tmp/run_e1_manual.py`，不改仓库文件）；
- 修复：测试 fixture 增加 `server.slot_save_path = "./tmp"`（启用 `/slots/:id?action=erase`，`test_metrics_kv_multi_sequence_and_delete` 必需）。

## 2. 测试模型与环境

| 项 | 值 |
|---|---|
| llama.cpp commit | `b69773a1e`（E1 已提交），工作区含 1 处测试 fixture 修改（dirty） |
| 根仓库 commit | `f9d3e3d`（基线），工作区含 kv_probe/runner 修复（未提交） |
| 构建 | `cmake -B build -DGGML_CUDA=OFF -G Ninja && cmake --build build -j16 --target llama-server`（CPU） |
| 测试模型 | `ggml-org/test-model-stories260K`（stories260K-f32.gguf，1.18MB，HF 缓存） |
| 模型获取 | `HF_ENDPOINT=https://hf-mirror.com`（huggingface.co 直连不可达） |
| server 参数 | `llama-server --hf-repo ggml-org/test-model-stories260K --offline --temp 0.0 --seed 42 --ctx-size 512 --parallel 2 --batch-size 32 --n-predict 16 --slot-save-path ./tmp` |
| ctx-size / parallel | 512 / 2（每 slot n_ctx=256） |
| 补充验证模型 | Qwen3.5-4B-Q4_K_M（GPU，`build-cuda`，`-ngl 99 -c 2048 --parallel 2`） |

## 3. Python 与 llama.cpp 测试结果

| 测试 | 命令 | 结果 |
|---|---|---|
| llama.cpp E1 集成测试 | `python3 tmp/run_e1_manual.py`（绕过 load_all） | **5/5 passed**：schema 校验、空 cache（used=0）、completion 后 used_cells 增长、多 sequence（active≥1）与 slot erase、只读性 |
| benchmark Python 全量 | `uv run pytest -q` | **71 passed**（68 原 + 3 新增 repeat/warmup 用例） |
| llama.cpp 宽回归（test_completion.py） | `python3 -m pytest tools/server/tests/unit/test_completion.py` | **未完整执行**（39 errors）：conftest `load_all()` 需下载全部 8 个 preset 模型，本环境仅缓存 4 个（stories260K/260K-infill/stories15M_MOE/models 仓），tinygemma3（~3GB）等下载失败/超时。**阻塞原因为环境模型获取，非 E1 代码问题**。 |

## 4. endpoint 正常、错误和降级行为

**正常（HTTP 200）**：
```json
{
  "schema_version": 1,
  "capacity_bytes": 67108864,
  "used_bytes": 393216,
  "used_bytes_valid": true,
  "capacity_cells": 2048,
  "used_cells": 12,
  "active_sequences": 1,
  "shared_cells": 0,
  "physical_sharing": false,
  "shared_cells_semantics": "multi-sequence cell association (metadata-level, not COW)"
}
```

**错误行为**：
- server 未加载模型：middleware 返回 `503 {"error":{"type":"unavailable_error"}}`（与现有端点一致，未新增逻辑）；
- memory 类型不支持 KV stats（如 recurrent）：`500 "KV stats not available for the current memory type"`；
- llama_context 不可用：`500 "llama_context not available"`。

**Benchmark 降级**（endpoint 不可用时）：
- `--kv-probe` 下 endpoint 不可达 → `kv_observations.failures>0`、`last_error` 记录、`samples` 为空（不伪造 0），**workload 正常完成**（mock server 测试验证）；
- 未启用 `--kv-probe` 时结果结构与 E0.6 完全一致（`{config, metadata, summary, scenarios}`，无 kv_observations）。

## 5. used_bytes 的有效范围

- 定义：`used_cells × (size_k_bytes()+size_v_bytes()) / capacity_cells`；
- **精确条件**：`n_swa == 0 && capacity_cells > 0`。llama.cpp 的 K/V stream 是主 tensor 的等大小 view（`ggml_view_2d(k, ..., kv_size, ..., s*k->nb[2])`），无 SWA 时每 cell 每层字节跨 stream 一致 → `kv_bytes/capacity_cells` 即 per-cell 字节，公式**等价于按 stream 求和**，多 stream 下同样精确（非平均估算）；
- 实测验证：`capacity_bytes=64MB、capacity_cells=2048 → per_cell=32768B；used_cells=12 → 12×32768=393216 = 报告 used_bytes` ✓；
- SWA 或空 cache 时：`used_bytes_valid=false`、`used_bytes=null`（不伪造）。

## 6. active_sequences 与 shared_cells 验证

- **active_sequences**：遍历 `seq_id ∈ [0, LLAMA_MAX_SEQ)`（256），`seq_pos_min(seq_id) >= 0` 判断活跃（`seq_pos` 仅含有效位置的 seq；空闲/保留无位置的 seq 不计入）。实测：空 cache=0，单 slot 请求=1，双 slot=2 ✓；
- **shared_cells**：`seq[i].count() > 1` 的 **cell 数**（非 sequence 关联总数）；`physical_sharing=false` 在 endpoint 与文档一致。实测双 slot 独立 seq 时 shared_cells=0（无 seq 复制）；同 stream seq_cp（如 n_cmpl）会产生 >0，本环境 n_cmpl 被 server 限制为 1 未实测（tinyllama 单请求路径），已由集成测试覆盖元数据语义断言（`shared_cells==0` 与 `physical_sharing is False`）。

## 7. repeat/warmup 下 KV observations 保存语义（本轮修复）

**发现的问题**：原实现 KVProbe 为单一全局实例、samples 平铺、无 run 维度；warmup 请求也会采集（违反"warmup 不进入 kv_observations"）；repeat 时无法区分 run（违反"不得只保存最后一次/无法区分"）。

**修复**（`benchmark/framework/kv_probe.py` + `runner/runner.py`）：
- `KVProbe.begin_run(run_id)/end_run()`：正式 run 上下文，snapshot 记录 `run_id`；
- `KVProbe.set_collecting(False)`：warmup 阶段暂停采集；
- `to_dict()` 增加 `runs` 聚合（每 run 的 `samples/first_used_cells/last_used_cells/peak_used_cells`），raw samples（带 run_id/ts/tag）完整保留；
- `Runner.run_scenario`：warmup 前 `set_collecting(False)`，每个正式 run `begin_run(f"{workload.name}_{i}")`…`end_run()`。

**真实 server 验证**（`--kv-probe --warmup 1 --repeat 2`，multi_turn 3 轮）：
```
run_ids: ['multi_turn_0', 'multi_turn_1']          # 两 run 独立
samples 总数: 14 = start(1) + run0(6) + run1(6) + end(1)   # warmup 3 轮未采集
runs: {"multi_turn_0": {"samples":6, "first":998, "last":784, "peak":998},
       "multi_turn_1": {"samples":6, "first":784, "last":466, "peak":784}}
failures: 0
```
- start/end 为全局快照（run_id=None）；请求前后为 run 内快照（run_id=run 名）；periodic 采样带当前 run_id；
- 未启用 `--kv-probe` 时 E0.6 结构不变（测试验证）。

## 8. 已知限制

1. llama.cpp 宽回归（test_completion.py 等）依赖全部 preset 模型，本环境仅能获取部分 → 未完整执行（环境阻塞，非代码问题）；
2. `n_cmpl>1` 在当前 server 配置被限制为 1，`shared_cells>0` 的元数据场景未在真实 server 实测（集成测试断言其 `==0` 语义与 `physical_sharing=false`）；
3. `used_bytes` 仅无 SWA 时精确；SWA 模型返回 null；
4. 统计范围为当前主 context（`ctx_tgt`）的 attention KV；MTP 等辅助 context 与 recurrent state 不计入；
5. `periodic` 样本的 run_id 随采样时上下文变化（可能为 run 名或 None），语义为"采样时刻所属 run"。

## 9. E1 最终状态

**CONDITIONAL PASS。**

- ✅ E1 新增集成测试（test_metrics_kv.py）**真实执行并 5/5 通过**（tinyllama 真实 server）；
- ✅ benchmark Python 71 passed（含 repeat/warmup 新语义 3 例）；
- ✅ endpoint 正常/错误/降级行为全部验证；used_bytes 精确性实测确认；active_sequences 与多 slot 一致；shared_cells 语义无 COW 混淆；
- ✅ repeat/warmup 的 kv_observations 保存语义已修复并真实验证（run_id 区分、warmup 不采集、runs 聚合、raw samples 保留）；
- ⚠️ 条件：llama.cpp 宽回归（test_completion.py 等）因环境模型下载阻塞未完整执行，**不代表代码问题**，需在有完整模型缓存的 CI/环境补跑；
- ⚠️ 结论：**在补跑宽回归前，E2 实现不应开始**（任务要求"只有在 llama.cpp 集成测试真实通过，或者明确记录无法执行并获得人工确认后，才允许进入 E2"——本报告即为明确记录，等待人工确认）。

---

## E2 前置审查结论（本次不实现 E2）

### 1. KV buffer 分配方式
- llama.cpp 当前**启动时预分配**：`llama_kv_cache::total_size()` 在初始化时按 `n_ctx × n_layers × n_heads × head_dim × type` 全量分配（`ctxs_bufs`），运行期固定，**不支持运行时扩展/收缩**；
- 隐含结论：在**纯预分配**下，任何生命周期策略都不会改变 `capacity_bytes`，也无法降低**进程级显存峰值**（buffer 已全量占用）。

### 2. 现有机制对 cell 生命周期的影响
| 机制 | 对 cell 的影响 |
|---|---|
| `seq_rm(seq, p0, p1)` | 释放指定 range 的 cell（`seq[i]` 清位、`used` 移除，`seq_pos` 递减）；cell 仅变为"空闲"，buffer 不释放 |
| `seq_keep(seq)` | 删除其它所有 seq 的 cell（保留目标 seq） |
| `seq_cp(src, dst, ...)` | 目标 seq 关联到 src 的 cell（`seq[i]` 加位）→ **多 sequence 共享 cell 的唯一来源**（`shared_cells` 统计依据） |
| `--cache-reuse N` | 非前缀 chunk 的 KV 通过 `seq_rm + seq_add` 移位复用（slot 内复用已有 cell，不改 buffer） |
| slot reuse（find_slot/update） | 每批处理时按 slot 复用：旧 seq 的 cell 被 `seq_rm`/`seq_keep` 清理或覆盖 |

### 3. slot erase vs sequence 删除 vs KV 实际清理
- `POST /slots/:id?action=erase`：仅清 server_slot 的任务状态（prompt/tokens），**不立即清理 KV**；KV 内容在下次 slot 复用/update 时按前缀缓存语义保留或覆盖（实测 erase 后 used_cells/active_sequences 不变）；
- `seq_rm`/`seq_keep`：真正修改 cell 元数据（E1 统计可观测）；
- 结论：**slot erase 不是 KV 清理的观测点**，E2 评估必须以 `used_cells`/`active_sequences` 为准，不能以 slot 状态近似。

### 4. E1 指标是否足以测量生命周期策略
- `used_cells`（跨 stream 求和）与 `active_sequences` 可反映回收/淘汰效果 ✓；
- `used_bytes`（无 SWA 精确）可换算实际有效 KV 字节 ✓；
- `capacity_bytes/capacity_cells` 恒定 → 生命周期策略的收益只能体现为 **used_cells 下降、缓存命中率上升、重算延迟下降**，**不是显存下降**；
- 缺口：无 per-sequence 粒度（无法观测单 seq 的 cell 占用）、无"回收事件"计数。E2 若需可考虑扩展，但**不得伪造**。

### 5. 固定预分配下 E2 能否声称"降低显存"
- **不能**直接声称降低进程级显存（预分配 buffer 不变）；
- 可声称的收益：① 相同显存下服务**更长有效会话**（used_cells 利用率提高/回收后命中率维持）；② 降低**重算延迟**（更智能保留高价值 cell）；③ 若实现**按需分配/分层存储**（改变分配策略本身，属 E2 设计决策），则 `capacity_bytes` 才可能下降——这需要明确作为 E2 的一个**设计选项**而非默认假设。

### 6. E2 应采用的 Baseline
| Baseline | 配置 | 用途 |
|---|---|---|
| B0 | `--cache-reuse 0`（默认）+ 原始 server | 系统原始下限 |
| B1 | 现有 `--cache-reuse N`（如 256）+ 原始 server | **本项目所有优化的比较对象**（已有机制上限） |
| B2 | 新生命周期策略 | 收益 = B2 − B1 |

对照规则（沿用 E0.6 报告）：已有机制收益 B0 vs B1；本项目收益 B1 vs B2；端到端 B0 vs B2(all)。

### 7. E2 的主指标、代理指标与保真约束
| 类型 | 指标 | 说明 |
|---|---|---|
| 主指标 | `used_cells` 曲线、`cache_hit_rate` 衰减曲线、`recompute_ratio`、`kv_peak_used_bytes`（= used_bytes 峰值） | 生命周期策略直接效果 |
| 代理指标 | p50/p95 延迟、`throughput_tps`、长会话可持续轮数（不触发 400 的轮数） | 用户可感知收益 |
| 保真约束 | `task_success`/`state_retention_rate`（long_life）、各场景 evaluator | 优化不得损害任务效果 |

### 8. E2 必须明确排除的功能
- COW / `TAG_KV_CACHE_SHARE_CELLS`（属 E3）；
- Context Compression（属 E3）；
- 修改 workload 的 generate/run/prompt/轮数/秘密数字/截断（冻结）；
- 删除或覆盖已有 baseline；
- 未经授权提交 git。

---

## 附：本轮修改清单（未提交，等待授权）
| 仓库 | 文件 | 改动 |
|---|---|---|
| 根 | `benchmark/framework/kv_probe.py` | run 作用域（begin/end_run、set_collecting）、samples 带 run_id、to_dict 增 runs 聚合、peak/first/last 支持 run 过滤 |
| 根 | `benchmark/runner/runner.py` | run_scenario 中 warmup 暂停采集 + 正式 run 打 run_id 边界 |
| 根 | `benchmark/tests/test_kv_probe_repeat.py` | 新增 3 例（repeat run_id 区分、warmup 不采集、start/end 全局） |
| llama.cpp | `tools/server/tests/unit/test_metrics_kv.py` | fixture 增加 `slot_save_path`（启用 slot erase） |
| 环境 | `llama.cpp/tmp/` | 测试模型缓存（stories260K 等）+ `run_e1_manual.py`（临时脚本，不入库） |
