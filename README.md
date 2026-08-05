# 面向智能体的内存管理系统（高校赛题 14）

基于 llama.cpp 扩展，针对智能体长生命周期推理（多轮对话、工具调用、多路径决策）的
**KV Cache 生命周期管理**与**分支共享**优化，配套可复现的 Agent 工作流 Benchmark。

## 目录结构

```
├── llama.cpp/        # 上游框架（独立 git 仓库，优化改动在其中管理，不入根仓库）
├── benchmark/        # Agent 工作流 Benchmark（Python，OpenAI 兼容 API）
│   ├── agent_bench.py
│   └── results/      # 基线/对比实验结果（JSON）
├── models/           # GGUF 模型目录（模型文件不入库，用 download_models.sh 拉取）
│   └── download_models.sh
└── docs/             # （规划）技术方案与测试报告
```

## 环境要求

- Linux 发行版（openEuler 等均可）
- CMake ≥ 3.14、g++ ≥ 11（编译 llama.cpp）
- Python ≥ 3.13 + [uv](https://docs.astral.sh/uv/)（运行 Benchmark）
- 硬件：CPU 可开发验证；正式对比建议 NVIDIA GPU（本项目用 RTX 4060 8GB）

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

### 3. 启动推理服务

```bash
./llama.cpp/build-cuda/bin/llama-server \
  -m models/Qwen3.5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 8192
```

### 4. 运行 Benchmark

```bash
cd benchmark
uv sync
uv run python agent_bench.py --scenario all           # 常规三场景（多轮/工具/分支）
uv run python agent_bench.py --scenario long_life --long-rounds 40
# ↑ 长生命周期场景：需 server 以小 ctx 启动（如 --ctx-size 2048）以触发 KV 回收
```

结果输出到 `benchmark/results/bench_<时间戳>.json`，含 token 数、延迟、内存/显存峰值、前缀缓存命中率等指标，用于优化前后对比。

## 模型清单

| 模型 | GGUF 文件 | 用途 |
|---|---|---|
| Qwen2.5-0.5B | `qwen2.5-0.5b-instruct-q4_k_m.gguf` | 最小功能验证 |
| Qwen3.5-0.8B | `Qwen3.5-0.8B-Q4_K_M.gguf` | CPU 开发验证 |
| Qwen3.5-4B | `Qwen3.5-4B-Q4_K_M.gguf` | GPU 正式基线 / 优化对比 |

## Git 说明

- `models/*.gguf`、构建产物（`build*/`）、虚拟环境（`.venv/`）不入库，见根 `.gitignore`
- `llama.cpp/` 为独立 git 仓库（上游代码），根仓库不跟踪；对其改动在其内部提交
- 实验数据 `benchmark/results/` 保留入库，作为测试报告证据
