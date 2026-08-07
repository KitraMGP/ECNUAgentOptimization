# 面向智能体的内存管理系统（高校赛题 14）

基于 llama.cpp 扩展，针对智能体长生命周期推理（多轮对话、工具调用、多路径决策）的
**KV Cache 生命周期管理**与**分支共享**优化，配套可复现的 Agent 工作流 Benchmark。

> **当前阶段状态（2026-08）**：E0-E4 已完成。核心结论——
> ① KV buffer 启动时预分配、运行时不可扩展 → **生命周期策略不能降低显存峰值**，
> 优化空间在"池满防 OOM、压力下保热点、降低重算"；
> ② 已实现 unified idle-sequence 价值感知淘汰（`--unified-idle-slot-policy lru`，
> experimental）与精确归属诊断（`--lifecycle-trace`）；
> ③ A2 分支共享路由已 REJECT 冻结；④ 真实流量回放（E3.6）等待用户提供匿名 trace。

## 目录结构

```
├── llama.cpp/        # 上游框架（独立 git 仓库，优化改动在其中管理，不入根仓库）
├── benchmark/        # Agent 工作流 Benchmark（Python，OpenAI 兼容 API）
│   ├── framework/    # 核心框架：config/driver/sampler/workload/metrics
│   ├── workload/     # 场景：multi_turn / tool_call / branch / long_life / branch_pressure
│   ├── scripts/      # 各阶段实验 runner（e2-e4 系列）
│   ├── tests/        # pytest（不依赖 GPU/真实 server，含 mock server e2e）
│   ├── schemas/      # 匿名 lifecycle trace schema（E3.5）
│   ├── baseline/     # 正式基线归档（可读命名）
│   └── results/      # 运行期临时输出（不入库）
├── models/           # GGUF 模型目录（模型文件不入库，用 download_models.sh 拉取）
│   └── download_models.sh
└── docs/             # 各阶段报告（E0-E4，见下方"阶段报告索引"）
```

## 环境要求

- Linux 发行版（openEuler 等均可）
- CMake ≥ 3.14、g++ ≥ 11（编译 llama.cpp）
- Python ≥ 3.13 + [uv](https://docs.astral.sh/uv/)（运行 Benchmark）
- 硬件：CPU 可开发验证；正式对比建议 NVIDIA GPU（本项目用 RTX 4060 8GB）
- ⚠️ 注意：Laptop GPU 的 boost 时钟抖动使 wall-time 级性能门槛（<1% p95）
  不可稳定测量——本项目 wall-time 结果均标记 **exploratory**，容量类指标
  （KV 使用/显存/失败率/OOM 边界）不受影响。

## 快速开始

### 1. 编译 llama.cpp

```bash
cd llama.cpp
# CPU（开发验证）
cmake -B build -DGGML_CUDA=OFF
cmake --build build -j $(nproc)
# CUDA（GPU 正式对比；RTX 40 系架构号为 89）
cmake -B build-cuda -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89
cmake --build build-cuda -j $(nproc)
```

### 2. 下载模型

```bash
cd models
./download_models.sh          # 下载全部（0.5b / 0.8b / 4b）
./download_models.sh 4b       # 只下载指定模型
```

### 3. 启动推理服务（含优化机制开关）

```bash
./llama.cpp/build-cuda/bin/llama-server \
  -m models/Qwen3.5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 8192 \
  --kv-unified --parallel 4 \
  --unified-idle-slot-policy default   # 或 lru（experimental：unified 下价值感知淘汰）
  --lifecycle-trace                    # 可选：精确归属诊断（默认关闭，debug-only）
  --lifecycle-stats                    # 可选：purge 决策诊断（默认关闭）
```

### 4. 运行 Benchmark

```bash
cd benchmark
uv sync
uv run python agent_bench.py --scenario all           # 常规三场景（多轮/工具/分支）
uv run python agent_bench.py --scenario long_life --long-rounds 40
# ↑ 长生命周期场景：需 server 以小 ctx 启动（如 --ctx-size 2048）以触发 KV 回收
```

结果输出到 `benchmark/results/bench_<时间戳>.json`，含 token 数、延迟、内存/显存峰值、
前缀缓存命中率、KV 观测（`/metrics/kv`）等指标。

## 当前优化机制（llama.cpp 侧）

| 机制 | 参数 | 状态 |
|---|---|---|
| KV Cache 统计端点 | `GET /metrics/kv`（E1） | ✅ 默认启用 |
| unified idle-sequence 价值感知淘汰 | `--unified-idle-slot-policy default\|lru`（E3.2） | `lru` experimental，默认 `default` |
| 精确归属诊断（trace_id→slot/seq/generation + pressure/purge/retry/resume 事件链） | `--lifecycle-trace`（E3.5） | 默认关闭，debug-only |
| purge 决策诊断（candidates/unique/shared cells） | `--lifecycle-stats`（E3.2） | 默认关闭，debug-only |
| 已 REJECT 候选 | A2 prefix-branch 路由（E2.5） | 冻结，不重开 |

## 模型清单

| 模型 | GGUF 文件 | 用途 |
|---|---|---|
| Qwen2.5-0.5B | `qwen2.5-0.5b-instruct-q4_k_m.gguf` | 最小功能验证 |
| Qwen3.5-0.8B | `Qwen3.5-0.8B-Q4_K_M.gguf` | CPU 开发验证 |
| Qwen3.5-4B | `Qwen3.5-4B-Q4_K_M.gguf` | GPU 正式基线 / 优化对比 |

## 阶段报告索引（docs/）

| 阶段 | 报告 | 结论 |
|---|---|---|
| E0（基础设施） | E0_IMPLEMENTATION_REPORT / E0_5 / E0_6 | Benchmark 分层重构 + 独立协议 |
| E1（可观测性） | E1_IMPLEMENTATION / E1_VALIDATION | `/metrics/kv` + KVProbe，CONDITIONAL PASS |
| E2（候选探索） | E2_DESIGN / E2_FEASIBILITY / E2_1~E2_5 | A2 分支路由实现后 **REJECT**；基线/门禁冻结 |
| E3.0-3.1（A1/A4 门禁） | E3_0 / E3_1 | 候选选定 + 可行性 READY |
| E3.2-3.4（A1/A4 实现验证） | E3_2 / E3_3 / E3_4 | lru 实现 PROMOTE → 集成/多会话 KEEP_EXPERIMENTAL |
| E3.5-3.5.3（诊断与性能门禁） | E3_5 / E3_5_1 / E3_5_2 / E3_5_3 | 契约闭环；性能门禁 HOLD（GPU 环境噪声） |
| E4（内存容量） | E4_MEMORY_CAPACITY_CONCURRENCY_GATE | 并发承载 ≥8 session、OOM 边界 >96%（exploratory） |

## 关键结论（详见各报告）

1. **显存峰值**：KV buffer 预分配固定（E2.0 事实）→ 生命周期策略**不能**降显存峰值，
   只能提升 KV 利用率、降低重算、防止池满 OOM；
2. **A1/A4（lru 淘汰）**：unified 压力下保留热分支（回访 processed -99.2%）、
   池满不 OOM（purge 恢复）；但**真实均匀并发下收益被 slot 分配稀释**（E3.4），
   保持 experimental 默认关闭；
3. **A2（prefix-branch）**：4B 长分支混合序列下 revisit 判定失效 → REJECT 冻结；
4. **性能门禁**：Laptop GPU boost 抖动使 wall-time <1% 门槛不可测（E3.5.2/3.3
   HOLD）——容量指标不受影响（E4）；
5. **下一步（E3.6）**：真实匿名 trace 回放——**硬前置 = 用户提供且 validator
   （`benchmark/schemas/lifecycle_trace_v1.json`）通过的真实流量**。

## Git 说明

- `models/*.gguf`、构建产物（`build*/`）、虚拟环境（`.venv/`）、`llama-baseline/`
  `llama-candidate/`（实验 worktree）不入库，见根 `.gitignore`
- `llama.cpp/` 为独立 git 仓库（上游代码），根仓库不跟踪；对其改动在其内部提交
- 实验数据 `benchmark/results/` 不入库（.gitignore）；正式归档在
  `benchmark/results/e*` 的 manifest/summary 与 `docs/` 报告
