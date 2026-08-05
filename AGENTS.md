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

- `benchmark/agent_bench.py`：三场景（多轮对话/工具调用/分支推理）+ `long_life` 长生命周期场景；OpenAI 兼容 API；psutil 采样 RSS、pynvml 采样 GPU 显存；`chat()` 内置 400 兜底（超 ctx 自动丢最早消息重试）。
- `benchmark/baseline/`：正式基线归档（可读命名 `<模型>_<环境>_<场景>_<说明>.json`）；`benchmark/results/` 为运行期临时输出（不入库）。
- `llama.cpp/`：上游框架（独立 git 仓库，根仓库不跟踪）；KV 优化改动在其中实施并内部提交。
- `models/download_models.sh`：可复现模型拉取。

## Conventions

- **沙盒内禁止自动运行 llama-server / benchmark**：当前开发沙盒无 NVIDIA 驱动（无法调用 GPU），且跑 benchmark 耗时极长。需要运行验证时，给出确切命令让用户亲手执行，不要自己启动 server 或跑脚本。
- **GPU 显存采样仅宿主机有效**：容器内 pynvml 报 `NVMLError_DriverNotLoaded`，`peak_gpu_mb` 返回 null 属正常降级，不是 bug；别据此判定脚本异常。
- **实验结果归档**：正式结果移入 `benchmark/baseline/` 并改可读文件名；`results/` 只留临时输出（.gitignore 已忽略）。
- **git**：`llama.cpp/` 独立仓库不纳入根仓库；`models/*.gguf`、`build*/`、`.venv/` 不入库；根仓库提交需用户授权（用户常要求"先不要提交"）。
- **commit message 格式**：标题 `<feat|fix|chore|docs|refactor，可多个用 & 连接如 feat&fix>: <摘要>`，空一行后分点（`- ` 开头）详细描述改动内容。
- **模型能力限制**：Qwen3.5-0.8B 指令遵循不稳定（工具参数可能填错）；默认 `--no-think`/`enable_thinking:false` 防思考循环。
- **AGENTS.md 必须及时更新**：当命令、目录结构、约定或架构发生变化时，本文件应在该变更落地后立即同步更新，保持准确——这是每个 agent 与协作者的职责，不要等到项目结束时才补。

## Notes

（后续补充：阶段 2 优化实现细节、4B 正式基线数据、技术方案文档位置等）
