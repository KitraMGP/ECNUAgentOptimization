# Agent 工作流 Benchmark

面向智能体的内存管理系统赛题的评测框架：在 llama-server 的 OpenAI 兼容 API 上，
对多轮对话 / 工具调用 / 分支推理 / 长生命周期 / realistic agent / 分支压力六类 agent 工作流做
**KV 缓存、内存、延迟、并发承载**指标的量化评测。

E0 完成基础设施重构；E1-E4 逐步接入 KV 观测（`/metrics/kv`）、unified 价值感知
淘汰（`--unified-idle-slot-policy lru`）、精确归属诊断（`--lifecycle-trace`）
与内存容量/并发承载基准。各阶段结论见 `docs/E*` 报告。

## 目录结构

```
benchmark/
├── agent_bench.py       # 兼容入口（薄壳，委托 runner.cli_main；CLI/输出与旧脚本一致）
├── framework/           # 核心框架
│   ├── config.py        #   Config 系统（JSON/YAML/dict/CLI，字段向后兼容）
│   ├── driver.py        #   OpenAI 兼容 API 封装（400 兜底、timings 提取）
│   ├── kv_probe.py      #   KV 观测（/metrics/kv + slot erase 清洁协议）
│   ├── sampler.py       #   llama-server 进程采样（RSS + GPU 显存）
│   └── workload.py      #   Workload 抽象（generate / run / evaluate）+ 注册表
├── workload/            # 场景实现（导入即注册）
│   ├── multi_turn.py    #   多轮对话
│   ├── tool_call.py     #   工具调用（文本 ACTION 协议）
│   ├── branch.py        #   分支推理（公共前缀派生 A/B）
│   ├── long_life.py     #   长生命周期（秘密数字 + 应用层截断）
│   ├── realistic_agent.py # L2：规划/工具链/失败重试/状态更新/总结
│   └── branch_pressure.py # 分支压力（E2.5：双分支 JSON evaluator）
├── scripts/             # 各阶段实验 runner（e2-e4 系列 + 诊断/汇总）
├── schemas/             # 匿名 lifecycle trace schema（E3.5）
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
uv run python agent_bench.py --scenario realistic_agent --realistic-rounds 10 \
  --realistic-payload-chars 12000 --ctx-size 32768 --parallel 1
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
| `--scenario` | all | multi_turn / tool_call / branch / long_life / realistic_agent / all |
| `--repeat` | 1 | 正式重复次数（1 = 与旧脚本行为一致；>1 时结果含 `runs` 与聚合统计） |
| `--warmup` | 0 | 预热轮数（不计入统计） |
| `--seed` | 42 | 确定性种子（配合 temperature=0） |
| `--temperature` | 0.0 | 推理温度（正式实验默认 0） |
| `--repeat N` | 1 | 正式重复 N 次；>1 时 summary 数值键聚合为 mean/std/p50/p95，evaluation 判据（如 state_retention_rate）跨 run 取均值 |
| `--warmup N` | 0 | 预热 N 次场景运行（不计入统计） |
| `--model-path PATH` | 自动探测 | GGUF 模型文件路径（用于哈希；空则从 server `/props` 探测） |
| `--parallel N` | 自动探测 | server 并行 slot 数（与 `--ctx-size` 平分语义相关，用于 ctx 检查） |
| `--report PATH` | 无 | 生成 markdown 实验报告 |
| `--ctx-size` | 2048 | 需与 llama-server `--ctx-size` 一致（long_life 场景触发 KV 回收） |
| `--realistic-rounds` | 10 | realistic_agent 阶段数（至少 7） |
| `--realistic-payload-chars` | 12000 | realistic_agent 详情工具 payload 大小 |

## Workload 接口

新增场景需实现 `framework/workload.py` 的 `Workload` 抽象（`@register` 注册）：

- `generate(params)`：确定性生成场景规格（prompt 模板、规模参数、期望结果）；
- `run(driver, spec)`：执行场景，返回 `{"rows": [...], "meta": {...}}`；
- `evaluate(results, spec)`：确定性任务判定（保真约束，不依赖主观评价）。

场景参数从 `BenchmarkConfig` 经 `params_from_config(config)` 提取。

## 测试

测试分层：

- **L0 回归**：`multi_turn`、`tool_call`、基础 `branch`，验证 API 和基本结果结构；
- **L1 机制**：`long_life`、`branch_pressure`、E15 分支并发、M0 fanout，验证生命周期、分支和资源归因；
- **L2 真实 Agent 模拟**：`realistic_agent`，验证规划、工具链、一次 transient failure 重试、状态更新、校验和最终总结。

L0/L1/L2 目前均使用确定性 workload；L2 不声称真实互联网工具质量，工具延迟和返回值由 workload 固定，便于 baseline/candidate paired。

```bash
cd benchmark
uv run pytest -q        # 全量：分层 workload、driver、metrics、runner、report 与 e2e 冒烟
```

测试不依赖 GPU / 真实 llama-server：Driver 走 mock（单测）与本地 mock HTTP server（e2e，含
llama-server 风格 `timings` 字段的解析验证）。

## 实验结果结构（E0.6）

每次实验的 JSON 含四个顶层键：

```json
{
  "config":   {...},   // 实验配置（旧 CLI 键 + E0.6 新键的超集）
  "metadata": {...},   // E0.6：model 路径/sha256、llama.cpp commit、GPU、server slot ctx、warnings
  "summary":  {...},   // 每场景：tokens/延迟(p50/p95/std)/吞吐/缓存命中/任务判据
  "scenarios": {...}   // 每场景原始行（repeat>1 时为 {"runs": [...], "aggregate": ...}）
}
```

- **吞吐指标**：`throughput_tps`（端到端 = total_tokens/总耗时）、`decode_tps`（decode 吞吐均值）。
- **长期状态保持**：long_life 场景的 `state_retention_rate`（主指标，secret 召回率；跨 repeat 取均值）；`task_success` 仅作保真约束参考。
- **ctx-size 语义检查**：若 server 实际 slot n_ctx 与配置 `--ctx-size` 不一致（如 `--parallel 4` 把 2048 平分为 4×512），打印 `[WARNING]` 并记入 `metadata.warnings`。

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
