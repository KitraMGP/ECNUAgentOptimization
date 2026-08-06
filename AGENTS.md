# 面向智能体的内存管理系统（高校赛题 14）

基于 llama.cpp 扩展的 Agent 长生命周期推理内存优化项目：KV Cache 生命周期管理、分支共享（COW）、Prompt 压缩，配套可复现的 Agent 工作流 Benchmark。

## Project

- 目标：在保证推理效果前提下降低智能体推理的显存/内存占用与延迟（赛题要求优化前后同硬件对比）。
- 技术栈：llama.cpp（C++ 推理框架，qwen35 架构）+ Python 3.13 / uv（benchmark）+ OpenAI 兼容 API。
- 入口：`benchmark/agent_bench.py`（评测脚本）；核心优化代码位于 `llama.cpp/src/`（阶段 2 待实施）。
- 模型：Qwen3.5-4B（GPU 正式）/ Qwen3.5-0.8B（CPU 开发）/ Qwen2.5-0.5B（最小验证），GGUF 在 `models/`（不入库）。

## Commands

```bash
# 编译 llama.cpp（在 llama.cpp/ 内）
cmake -B build -DGGML_CUDA=OFF && cmake --build build -j $(nproc)        # CPU
cmake -B build-cuda -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 && cmake --build build-cuda -j $(nproc)  # GPU

# 下载模型（models/ 内；模型文件不入 git，必须用脚本拉取）
./download_models.sh          # 全部（0.5b/0.8b/4b）
./download_models.sh 4b       # 单个

# 启动 llama-server（GPU 正式对比；ctx 小则触发 KV 回收）
./llama.cpp/build-cuda/bin/llama-server -m models/Qwen3.5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 2048

# 运行 benchmark（benchmark/ 内，uv 管理依赖）
uv sync
uv run python agent_bench.py --scenario all            # 三场景：multi_turn/tool_call/branch
uv run python agent_bench.py --scenario long_life --long-rounds 40   # 长生命周期（需 --ctx-size 与 server 一致）
```

## Architecture

- `benchmark/agent_bench.py`：兼容入口（E0 重构后为薄壳，委托 `runner.cli_main`；CLI 参数与输出格式向后兼容）。
- `benchmark/framework/`：核心框架——`config.py`（JSON/YAML/dict 配置系统）、`driver.py`（OpenAI 兼容 API 封装，400 兜底 + timings 提取）、`sampler.py`（RSS/GPU 采样）、`workload.py`（Workload 抽象：`generate/run/evaluate` + 注册表）。
- `benchmark/workload/`：四场景实现（multi_turn / tool_call / branch / long_life），导入即注册；结果结构与旧脚本一致（行内追加 `timings` 键）。
- `benchmark/metrics/`：p50/p95/mean/std/cache_hit_rate/summarize（保留旧字段）。
- `benchmark/runner/`：场景 × repeat × warmup 编排、结果落盘 `results/bench_<ts>.json`（`{config, summary, scenarios}`）。
- `benchmark/report/`：markdown 实验报告。
- `benchmark/configs/`：示例配置（example.json / example.yaml）。
- `benchmark/tests/`：pytest（不依赖 GPU/真实 server，含 mock OpenAI server e2e 冒烟）。
- `benchmark/baseline/`：正式基线归档（可读命名 `<模型>_<环境>_<场景>_<说明>.json`）；`benchmark/results/` 为运行期临时输出（不入库）。
- `llama.cpp/`：上游框架（独立 git 仓库，根仓库不跟踪）；KV 优化改动在其中实施并内部提交。
- `models/download_models.sh`：可复现模型拉取。

## Conventions

- **运行 llama-server / benchmark 需用户明确授权**：沙盒历史上一度无 GPU 驱动，现已确认宿主机可访问 GPU（RTX 4060 8GB，驱动 610.43.03）；用户授权后可直接运行。注意耗时（all 场景约 5–10 分钟、long_life 40 轮约 10–20 分钟）与显存（4B 模型约占 3.2GB，8GB 卡需留意并行 slots 与 ctx 大小）。未经授权不要自动启动。
- **pkill/pgrep -f 会匹配 bash 作业自身命令行**：`bash -c` 把整条命令文本（含启动 llama-server 的命令）放入进程 cmdline，`pkill -f "llama-server ..."` 或 `pgrep -af "build-cuda/bin/llama-server"` 会匹配到执行该命令的 bash 作业自身，导致"自杀"（把自己杀掉，新 server 也随之未启动；2026-08-06 bash-4 事故）。避免方法：① 杀进程用 `pkill -x llama-server`（精确进程名，不匹配命令行文本）；② 查询进程用 `pgrep -x llama-server`；③ "先杀 → 确认无残留 → 再单独启动"必须拆成独立命令/作业，绝不在同一 bash 命令行里既 pkill 又启动同一模式的服务。
- **GPU 显存采样仅宿主机有效**：容器内 pynvml 报 `NVMLError_DriverNotLoaded`，`peak_gpu_mb` 返回 null 属正常降级，不是 bug；别据此判定脚本异常。
- **实验结果归档**：正式结果移入 `benchmark/baseline/` 并改可读文件名；`results/` 只留临时输出（.gitignore 已忽略）。
- **git**：`llama.cpp/` 独立仓库不纳入根仓库；`models/*.gguf`、`build*/`、`.venv/` 不入库；根仓库提交需用户授权（用户常要求"先不要提交"）。
- **commit message 格式**：标题 `<feat|fix|chore|docs|refactor，可多个用 & 连接如 feat&fix>: <摘要>`，空一行后分点（`- ` 开头）详细描述改动内容。
- **模型能力限制**：Qwen3.5-0.8B 指令遵循不稳定（工具参数可能填错）；默认 `--no-think`/`enable_thinking:false` 防思考循环。
- **AGENTS.md 必须及时更新**：当命令、目录结构、约定或架构发生变化时，本文件应在该变更落地后立即同步更新，保持准确——这是每个 agent 与协作者的职责，不要等到项目结束时才补。

## Notes

- E0（Benchmark 基础设施重构）已完成：`framework/ workload/ metrics/ runner/ report/ configs/` + 42 个 pytest（不依赖 GPU/server）；详见 `docs/E0_IMPLEMENTATION_REPORT.md`。
- 阶段 1（llama.cpp 可观测性）、阶段 2（KV 优化）未开始；详见 `docs/benchmark_implementation_plan.md`。
