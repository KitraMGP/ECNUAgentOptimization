# Agent 工作流 Benchmark（E0 框架版）

面向智能体的内存管理系统赛题的评测框架：在 llama-server 的 OpenAI 兼容 API 上，
对多轮对话 / 工具调用 / 分支推理 / 长生命周期四类 agent 工作流做
**KV 缓存、内存、延迟**指标的量化评测。

E0 阶段完成 benchmark 基础设施重构（配置 / workload / driver / metrics / runner / report），
**不包含**任何 KV Cache 优化实现（见 `docs/benchmark_implementation_plan.md`）。

## 目录结构

```
benchmark/
├── agent_bench.py       # 兼容入口（薄壳，委托 runner.cli_main；CLI/输出与旧脚本一致）
├── framework/           # 核心框架
│   ├── config.py        #   Config 系统（JSON/YAML/dict/CLI，字段向后兼容）
│   ├── driver.py        #   OpenAI 兼容 API 封装（400 兜底、timings 提取）
│   ├── sampler.py       #   llama-server 进程采样（RSS + GPU 显存）
│   └── workload.py      #   Workload 抽象（generate / run / evaluate）+ 注册表
├── workload/            # 场景实现（导入即注册）
│   ├── multi_turn.py    #   多轮对话
│   ├── tool_call.py     #   工具调用（文本 ACTION 协议）
│   ├── branch.py        #   分支推理（公共前缀派生 A/B）
│   └── long_life.py     #   长生命周期（秘密数字 + 应用层截断）
├── metrics/             # 指标统计（mean/std/p50/p95/cache_hit_rate/summarize）
├── runner/              # 实验编排（场景 × repeat × warmup，结果落盘）
├── report/              # markdown 实验报告生成
├── configs/             # 示例配置（example.json / example.yaml）
├── tests/               # pytest（不依赖 GPU / 真实 server，含 mock server 冒烟）
├── baseline/            # 正式基线归档（只增不改）
└── results/             # 运行期临时输出（gitignore）
```

## 安装

```bash
cd benchmark
uv sync            # 安装依赖（含 dev: pytest）
```

## 快速开始

先启动 llama-server（见项目 README）：

```bash
./llama.cpp/build-cuda/bin/llama-server -m models/Qwen3.5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 2048
```

再运行 benchmark：

```bash
cd benchmark
uv run python agent_bench.py --scenario multi_turn --rounds 20      # 单场景
uv run python agent_bench.py --scenario all                          # 三场景（同旧脚本）
uv run python agent_bench.py --scenario long_life --long-rounds 40 --ctx-size 2048
uv run python agent_bench.py --config configs/example.yaml           # 配置文件驱动
```

输出：`results/bench_<时间戳>.json`（结构：`{config, summary, scenarios}`，与旧脚本一致）。

## 配置

配置文件（JSON / YAML）字段与 CLI 参数一一对应，CLI 显式参数优先：

```bash
uv run python agent_bench.py --config configs/example.json \
  --scenario branch --branch-rounds 5
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | 127.0.0.1 / 8080 | llama-server 地址 |
| `--server-url` | （空） | 显式 OpenAI base_url，如 `http://127.0.0.1:8080/v1` |
| `--scenario` | all | multi_turn / tool_call / branch / long_life / all |
| `--repeat` | 1 | 正式重复次数（1 = 与旧脚本行为一致；>1 时结果含 `runs` 与聚合统计） |
| `--warmup` | 0 | 预热轮数（不计入统计） |
| `--seed` | 42 | 确定性种子（配合 temperature=0） |
| `--temperature` | 0.0 | 推理温度（正式实验默认 0） |
| `--report PATH` | 无 | 生成 markdown 实验报告 |
| `--ctx-size` | 2048 | 需与 llama-server `--ctx-size` 一致（long_life 场景触发 KV 回收） |

## Workload 接口

新增场景需实现 `framework/workload.py` 的 `Workload` 抽象（`@register` 注册）：

- `generate(params)`：确定性生成场景规格（prompt 模板、规模参数、期望结果）；
- `run(driver, spec)`：执行场景，返回 `{"rows": [...], "meta": {...}}`；
- `evaluate(results, spec)`：确定性任务判定（保真约束，不依赖主观评价）。

场景参数从 `BenchmarkConfig` 经 `params_from_config(config)` 提取。

## 测试

```bash
cd benchmark
uv run pytest -q        # 42 个用例：config/driver/metrics/workloads/runner/report/e2e 冒烟
```

测试不依赖 GPU / 真实 llama-server：Driver 走 mock（单测）与本地 mock HTTP server（e2e，含
llama-server 风格 `timings` 字段的解析验证）。

## E0 与旧脚本的已知差异

| 差异 | 说明 |
|---|---|
| 结果行追加 `timings` 键 | 每行多出 `timings`（llama-server 的 prompt_n/cache_n/吞吐等），解析不到时为 None；其余键不变 |
| summary 追加 `evaluation` 子对象 | 每个场景的任务判据（task_success 等），旧字段全部保留 |
| config 键为旧 CLI 键的超集 | 新增 model/server_url/repeat/warmup/seed/temperature/base_url 等 |
| GPU 采样 bug 修复 | 旧 `find_server_gpu_mb` 误用未定义变量 `nvml`（恒返回 None），已修正为 `pynvml` |
| YAML 配置 | 新增，需要 pyyaml（已加入依赖） |

## 工程约束（E0）

- 不修改 llama.cpp；不实现 KV 优化 / COW / Context 压缩。
- workload 一旦冻结不因实验效果而修改；新增场景需记录原因并更新文档与 baseline。
- 正式实验：warmup ≥ 1、repeat ≥ 5、报告 mean/std/p50/p95（见实现计划 Part 4）。
