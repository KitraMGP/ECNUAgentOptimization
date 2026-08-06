# E1 实现报告：llama.cpp KV Cache 可观测性

> 阶段：Phase E1（KV Cache 可观测性，只建立测量能力，**不实现任何 KV Cache 优化策略**）
> 前置：E0/E0.5/E0.6（Benchmark 框架与可靠性增强），根仓库基线 commit `b7e1d75`
> 本阶段未实现：KV 淘汰、生命周期回收、cache-reuse 修改、COW、Context Compression、TAG_KV_CACHE_SHARE_CELLS 改动。

---

## 1. E1 目标与边界

**目标**：为后续优化（E2 生命周期管理、E3 COW/压缩）建立**可观测的 KV Cache 统计能力**：
- llama.cpp 内部只读 KV stats 快照接口；
- llama-server HTTP endpoint `GET /metrics/kv`；
- Benchmark 自动采集并落盘 KV snapshots（`kv_observations`）。

**边界（本阶段明确不做）**：
- 不实现 KV Cache 淘汰 / 生命周期回收 / COW / Context Compression；
- 不修改 `--cache-reuse` 策略与 `TAG_KV_CACHE_SHARE_CELLS` TODO；
- 不修改 workload 的 `generate()`/`run()`，不为了结果调整 prompt/轮数/秘密数字/截断；
- 不删除或覆盖已有 baseline。

## 2. 修改文件

### llama.cpp（独立仓库，已提交 commit `b69773a1e`）
| 文件 | 改动 |
|---|---|
| `include/llama.h` | 新增 `struct llama_kv_stats` + `llama_memory_get_kv_stats()` 公开 API |
| `src/llama-memory.h` | `llama_memory_i` 增加非纯虚 `get_kv_stats()`（默认 false） |
| `src/llama-kv-cache.h/.cpp` | `llama_kv_cache::get_kv_stats()` 实现（快照遍历） |
| `src/llama-memory-hybrid.h/.cpp` | hybrid memory 委托内部 attention KV cache |
| `src/llama-context.cpp` | `llama_memory_get_kv_stats()` 实现 |
| `tools/server/server-task.h` | `server_task_result_metrics` 增加 `kv_stats` 字段 |
| `tools/server/server-context.h/.cpp` | metrics 任务分支采集 KV stats；新增 `get_metrics_kv` handler |
| `tools/server/server.cpp` | 注册 `GET /metrics/kv` 路由 |
| `tools/server/tests/unit/test_metrics_kv.py` | server 集成测试（新增，需模型环境/CI 执行） |

### benchmark（根仓库）
| 文件 | 改动 |
|---|---|
| `framework/kv_probe.py` | 新增独立 `KVProbe` 类（快照/周期采样/降级/聚合） |
| `framework/driver.py` | 可选 `kv_probe` 参数，每次请求前后快照 |
| `framework/config.py` | 新增 `kv_probe_enabled` / `kv_probe_interval` |
| `runner/runner.py` | CLI `--kv-probe`/`--kv-probe-interval`；结果写 `kv_observations` |
| `tests/mock_server.py` | mock server 增加 `/metrics/kv` 端点 |
| `tests/test_kv_probe.py` | 新增 10 例单元测试 |

## 3. KV 指标精确定义

| 字段 | 定义 | 数据来源 | 说明 |
|---|---|---|---|
| `capacity_bytes` | KV buffer **预分配容量**（不含模型权重） | `llama_kv_cache::total_size()`（全部 KV backend buffer 大小之和） | 启动时按 `n_ctx × layers × heads × dim × type` 预分配，运行期固定 |
| `used_bytes` | 有效 KV 内容占用字节 | `used_cells × (size_k_bytes()+size_v_bytes()) / capacity_cells` | **仅无 SWA 且 cache 非空时精确可算**；否则 `used_bytes_valid=false` 且值为 null。不使用比例估算伪造 |
| `capacity_cells` | 预分配 cell 总数 | `llama_kv_cells::size()`（跨 stream 求和） | Qwen3.5-4B 单 stream，等于 `n_ctx` |
| `used_cells` | 被有效 KV 内容占用的 cell 数 | `llama_kv_cells::get_used()`（跨 stream 求和） | 与逻辑 token 数不同（同一 cell 可被多 sequence 引用、可含 shift 状态） |
| `active_sequences` | cache 中存在的有效 sequence 数 | `llama_kv_cells::seq_pos_min(s) >= 0` 计数 | 统计来源为 cell 元数据中的 sequence 标记，**不是 server slot 数**（一个 slot 对应一个 seq，但 slot 空闲/复用时会变化） |
| `shared_cells` | 关联多个 sequence 的 cell 数 | `seq[i].count() > 1` 计数 | **元数据级多 sequence 关联**（如 seq_cp 复制）；**不等于 COW 物理内存共享收益** |
| `physical_sharing` | 是否物理共享 | 常量 `false` | E1 未实现 COW |

**统计范围**：`/metrics/kv` 统计的是**当前 llama_context 的 attention KV cache**（hybrid 模型统计其 attention 部分；recurrent state 不计入）。llama-server 单模型下即全 server 的 KV 状态。

## 4. endpoint JSON schema

`GET /metrics/kv`（成功，HTTP 200）：

```json
{
  "schema_version": 1,
  "capacity_bytes": 67108864,
  "used_bytes": 786432,
  "used_bytes_valid": true,
  "capacity_cells": 2048,
  "used_cells": 24,
  "active_sequences": 2,
  "shared_cells": 0,
  "physical_sharing": false,
  "shared_cells_semantics": "multi-sequence cell association (metadata-level, not COW)"
}
```

错误响应：
- server 未加载模型：middleware 自动返回 `503 {"error":{"message":"Loading model","type":"unavailable_error"}}`（与现有端点一致，不新增逻辑）；
- KV stats 对当前 memory 类型不可用（如 recurrent memory）：`500 {"error":{"code":500,"message":"KV stats not available for the current memory type","type":"server_error"}}`；
- llama_context 不可用：`500 ... "llama_context not available"`。

## 5. C API / 内部接口设计

```c
// include/llama.h
struct llama_kv_stats {
    uint64_t capacity_bytes;
    uint64_t used_bytes;
    bool     used_bytes_valid;
    uint64_t capacity_cells;
    uint64_t used_cells;
    uint64_t active_sequences;
    uint64_t shared_cells;
    bool     physical_sharing;
};
LLAMA_API bool llama_memory_get_kv_stats(llama_memory_t mem, struct llama_kv_stats * stats);
```

- `llama_memory_i::get_kv_stats()` 为**非纯虚**（默认 `false`），避免破坏 recurrent 等非 KV memory 类型的实现；
- `llama_kv_cache` / `llama_memory_hybrid` override 提供真实统计；
- 只读快照语义：不修改 KV 状态、不触发 defrag/update/seq 操作；结构体仅含标量，不暴露内部 vector/mutex 到 public header。

## 6. 并发与线程安全处理

- llama-server 采用**任务队列模型**：HTTP handler 把 `SERVER_TASK_TYPE_METRICS` 任务投递到 `queue_tasks`，**推理线程**（update_slots 主循环）执行并生成 KV stats，结果经 `queue_results` 返回；
- KV stats 的读取与 KV 修改**同一线程串行**，天然无数据竞争，无需新增锁；
- HTTP handler 不直接触碰 KV 状态（`get_llama_context()` 注释明确"not thread-safe, only from main thread"，本实现遵守）；
- 统计路径不改动任何推理行为（快照为纯遍历）。

## 7. Benchmark 采集方式

- 新增独立 `KVProbe` 类（`benchmark/framework/kv_probe.py`），与 RSS/GPU sampler、workload driver 职责分离；
- 启用：`--kv-probe`（默认关闭，不影响现有行为）；`--kv-probe-interval <秒>`（可选周期采样）；
- 采集点：实验开始（`start`）、每次请求前/后（`req_N_start`/`req_N_end`，经 Driver 可选 hook）、周期采样（`periodic`）、实验结束（`end`）；
- 结果 JSON 新增顶层 `kv_observations`（追加，不改旧字段）：`{enabled, schema_version, samples: [{ts, tag, data}], failures, last_error}`；
- **降级**：endpoint 不可达/非 200/含 error 时记录 `failures++` 与 `last_error`，不阻塞实验，KV 指标不写入样本（不伪造 0）。

## 8. 单元测试结果

### Python 侧（benchmark，无 GPU/真实模型依赖）：**68 passed**
新增 `tests/test_kv_probe.py`（10 例）：JSON 正常解析、缺失字段不崩溃、schema_version 跟踪、endpoint 不可达降级（failures/last_error）、禁用时无采集、peak/first/last 聚合、`--kv-probe` 结果写 `kv_observations`（start/请求前后/end 样本齐全）、未启用时无 `kv_observations`（E0.6 兼容）、FakeDriver 下 Runner 纯结构不变。mock server 提供 `/metrics/kv` 端点。

### llama.cpp 侧（server 集成测试，需模型环境/CI 执行）
新增 `tools/server/tests/unit/test_metrics_kv.py`（跟随项目现有 pytest + ServerPreset 风格）：schema 校验、空 cache（used=0）、completion 后 used_cells 增长、多 sequence 与删除后统计变化、只读性（连续快照一致）。本环境未执行（需 tinyllama 模型下载），由等价真实 server 验证覆盖（见 §9）。

## 9. 真实 server 验证结果（GPU，RTX 4060）

| 项 | 值 |
|---|---|
| 模型 | Qwen3.5-4B-Q4_K_M（hybrid 架构，attention + recurrent） |
| llama.cpp | commit `b06aa774c` + E1 本地改动（未提交） |
| server 参数 | `build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf -ngl 99 -c 2048 --parallel 2` |
| ctx-size / parallel | 2048 / 2（每 slot 1024） |

验证结果：

| 验证项 | 结果 |
|---|---|
| 空 cache | `capacity_bytes=64MB`、`capacity_cells=2048`、`used_cells=0`、`active_sequences=0`、`shared_cells=0`、`used_bytes_valid=true` |
| 单请求后 | `used_cells=15→24`、`active_sequences=1`、`used_bytes=491520→786432` |
| 双 sequence（slot0+slot1） | `active_sequences=2`、`used_cells=24`、`shared_cells=0` |
| 只读性 | 连续两次快照完全一致（24/2） |
| slot erase 后 | `used_cells`/`active_sequences` 不变——**符合预期**：server 的 slot erase 只清 slot 任务状态，KV 内容保留作前缀缓存，直到 slot 复用/回收时才被 `seq_rm` 清理 |
| benchmark `--kv-probe` | `kv_observations` 写入结果：20 个样本（start + 6×请求前后 + 12×periodic + end），`failures=0`，`schema_version=1`；multi_turn 3 轮 `used_cells 24→880`；旧字段（prompt/latency/cache_hit_rate）不变 |
| ctx 语义 warning | `--parallel 2` 平分总 ctx（2048→1024 slot），warning 正常输出并记入 metadata |

**工作负载行为不变**：multi_turn 3 轮与 E0.6 行为一致（token/延迟/cached 字段语义未变），验证 `--kv-probe` 不影响 workload 结果。

### 9.1 复现与验证命令（供独立复现）

```bash
# 1) 编译（llama.cpp/ 内；CPU 或 GPU 二选一）
cmake -B build -DGGML_CUDA=OFF -G Ninja && cmake --build build -j16 --target llama-server        # CPU
cmake -B build-cuda -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -G Ninja \
  && cmake --build build-cuda -j16 --target llama-server                                          # GPU

# 2) 启动 server（GPU 正式；--parallel 会平分总 ctx，触发 ctx 语义检查）
./llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 -c 2048 --parallel 2

# 3) 验证 endpoint（空 cache / 请求前后 / 多 sequence / 只读性）
curl -s http://127.0.0.1:8080/metrics/kv | python3 -m json.tool
curl -s http://127.0.0.1:8080/completion -d '{"prompt":"The capital of France is","n_predict":8,"temperature":0,"id_slot":0}'
curl -s http://127.0.0.1:8080/completion -d '{"prompt":"The capital of Japan is","n_predict":8,"temperature":0,"id_slot":1}'
curl -s http://127.0.0.1:8080/metrics/kv   # 期望 active_sequences=2

# 4) Benchmark 自动采集（benchmark/ 内；--kv-probe 启用 KV 快照）
cd benchmark && uv sync
uv run python agent_bench.py --scenario multi_turn --rounds 3 --ctx-size 2048 \
  --kv-probe --kv-probe-interval 0.5
# 结果 JSON 顶层含 kv_observations（samples/failures/schema_version）

# 5) Python 单元测试（无 GPU/真实模型依赖，mock server 提供 /metrics/kv）
uv run pytest -q          # 68 passed

# 6) llama.cpp server 集成测试（需 tinyllama 模型，由 CI 或本地模型环境执行）
#    （llama.cpp/ 内，tools/server/tests/unit 的现有 pytest 框架）
python -m pytest tools/server/tests/unit/test_metrics_kv.py
```

## 10. 与 E0.6 的兼容性

- 结果 JSON 仅**追加** `kv_observations` 顶层键；`config`/`metadata`/`summary`/`scenarios` 结构与语义不变；
- `--kv-probe` 默认关闭：不启用时结果与 E0.6 完全一致（测试验证 `set(result) == {config, metadata, summary, scenarios}`）；
- workload 的 `generate()`/`run()`、prompt/轮数/秘密数字/截断逻辑未改；
- 旧 server（无 `/metrics/kv`）下 `--kv-probe` 自动降级（`failures>0`、`last_error` 记录），不阻塞实验。

## 11. 已知限制

1. `shared_cells` 仅为元数据级多 sequence 关联计数，**不代表 COW 物理共享收益**（`physical_sharing=false`）；
2. `capacity_bytes` 为启动时**预分配容量**（KV buffer 全量分配），不代表实际有效内容；`used_bytes` 仅在无 SWA 且非空时精确（`used_cells × per-cell 字节`），否则为 null；
3. `used_cells` 与逻辑 token 数不同义（cell 可被多 sequence 引用、可含 shift/2D 状态）；
4. 统计范围为当前 context 的 attention KV cache（hybrid 的 recurrent state 不计入）；llama-server 多 context（如 MTP）场景未验证，单模型单 context 语义明确；
5. `llama_kv_cache_iswa/msa/dsa/dsv4` 等非本项目模型路径的 memory 类型未实现 stats（返回"not available"错误）；本项目模型 Qwen3.5-4B（hybrid，无 SWA）走 `llama_memory_hybrid`，已支持；
6. server 集成测试（`test_metrics_kv.py`）需模型环境/CI 执行，本环境以等价真实 GPU server 验证覆盖；
7. slot erase 后 KV 可能保留（前缀缓存语义），`/metrics/kv` 反映物理 KV 状态而非 slot 状态。

## 12. 是否满足进入 E2 的条件

**结论：满足。** 依据：

- ✅ `GET /metrics/kv` endpoint 可用（HTTP 200 合法 JSON，GPU 真实验证）；
- ✅ KV stats 来源于 llama.cpp 内部真实状态（`llama_kv_cache::get_kv_stats` 遍历 cell 元数据，非估算）；
- ✅ 指标语义有文档定义（§3/§4）；
- ✅ 不改变已有推理行为（只读快照，任务队列内串行执行，验证只读性与 workload 行为不变）；
- ✅ Benchmark 自动采集并落盘 KV snapshots（`kv_observations`，20 样本/实验）；
- ✅ endpoint 不可用时优雅降级（failures/last_error，不阻塞）；
- ✅ llama.cpp 编译通过 + Python 68 测试通过 + 真实 GPU server 验证通过；
- ✅ `E1_IMPLEMENTATION_REPORT.md` 已生成；
- ✅ 报告明确 `shared_cells` 非 COW 收益、`capacity_bytes` 为预分配、`used_bytes` 不伪造；
- ✅ 未提前进入 E2（生命周期优化在 E2 阶段设计与实现）。

---

## 附：执行过程中发现与处理的问题

| # | 问题 | 处理 |
|---|---|---|
| 1 | Qwen3.5 为 **hybrid 架构**，memory 类型是 `llama_memory_hybrid` 而非 `llama_kv_cache`，首版 `/metrics/kv` 返回 "not available" | 为 `llama_memory_hybrid` 增加 `get_kv_stats` 委托（报告其 attention KV 部分） |
| 2 | `n_cmpl>1` 被 server 限制为 1（当前配置），多 sequence 场景改用 `--parallel 2` 双 slot 验证 | 验证 `active_sequences=2` |
| 3 | 编译期 json 三元表达式类型不匹配、`ERROR_TYPE_INTERNAL` 枚举不存在 | 修正为 `json(stats.used_bytes)` 与 `ERROR_TYPE_SERVER` |

已提交 git：llama.cpp 内部 commit `b69773a1e`（E1 可观测性实现），根仓库 commit `15658d4`（benchmark 采集与报告）。
