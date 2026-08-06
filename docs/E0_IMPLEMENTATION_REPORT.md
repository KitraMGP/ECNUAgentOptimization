# E0 实现报告：Benchmark Harness 重构

> 阶段：Phase E0（Benchmark 基础设施，纯 Python）
> 前置文档：`docs/benchmark_design_review.md`、`docs/benchmark_implementation_plan.md`
> 范围约束：不修改 llama.cpp；不实现 KV Cache 优化 / COW / Context 压缩；只做 Benchmark 基础设施。

---

## 1. 目标

将 `benchmark/agent_bench.py` 从单文件脚本重构为可扩展科研 Benchmark 框架，
同时**保持旧入口、CLI 参数与结果格式向后兼容**，为后续阶段（llama.cpp 可观测性、
KV 生命周期 / CoW / Context 压缩优化验证）提供基础设施。

## 2. 完成内容

### 2.1 目录结构（按计划创建）

```
benchmark/
├── agent_bench.py       # 兼容入口（薄壳 → runner.cli_main）
├── framework/           # 核心框架
│   ├── config.py        #   Config 系统（JSON/YAML/dict/CLI，向后兼容字段）
│   ├── driver.py        #   OpenAI 兼容 API 封装（400 兜底 + timings 提取）
│   ├── sampler.py       #   llama-server 进程采样（RSS + GPU 显存）
│   └── workload.py      #   Workload 抽象（generate/run/evaluate）+ 注册表
├── workload/            # 四场景实现（导入即注册）
│   ├── multi_turn.py  tool_call.py  branch.py  long_life.py
├── metrics/             # mean/std/p50/p95/cache_hit_rate/summarize
├── runner/              # 场景 × repeat × warmup 编排、结果落盘
├── report/              # markdown 实验报告
├── configs/             # example.json / example.yaml
├── tests/               # 42 个 pytest（无 GPU/真实 server 依赖）
├── README.md            # 新框架文档
├── baseline/            # 未改动（正式基线，只增不改）
└── src/ pyproject.toml uv.lock  # 未删除；pyproject 追加 dev 依赖
```

### 2.2 各模块要点

| 模块 | 要点 |
|---|---|
| **Config** | `BenchmarkConfig` dataclass；字段名与旧 CLI dest 一致（host/port/scenario/rounds/tool_steps/branch_rounds/long_rounds/long_secret/ctx_size）；支持 JSON / YAML（pyyaml，已入依赖）/ dict / CLI 覆盖；未知键保留到 `extra`（向前兼容）；派生 `base_url` 与原始 `server_url` 分离（避免 merge 固化端口） |
| **Workload 接口** | 抽象 `generate()`（确定性规格+指纹）/ `run()`（执行，返回 `{"rows","meta"}`）/ `evaluate()`（任务判定，保真约束）；`@register` 注册表；`rows_for_summary` 支持非扁平结构（branch） |
| **Driver** | 迁移旧 `chat()`（返回键 text/prompt_tokens/completion_tokens/total_tokens/cached_tokens/latency_ms/rss_mb/gpu_mb + 400 兜底）；**新增 `timings` 键**（llama-server 的 prompt_n/cache_n/predicted_n/ms/吞吐），经 `model_extra`/属性/`__pydantic_extra__` 三重提取；`timings_per_token` 可配 |
| **Metrics** | `summarize()` 保留旧字段（avg/max latency、peak RSS/GPU 等）+ 追加 p50/p95/std latency、cached_tokens、cache_hit_rate、recompute_tokens |
| **Runner** | 场景选择与旧脚本一致（`all` 不含 long_life）；warmup ≥ 0、repeat ≥ 1（默认 1 = 旧行为；>1 输出 `runs` + 跨 run mean/std/p50/p95 聚合）；结果 `{config, summary, scenarios}` 落盘 `results/bench_<ts>.json` |
| **Report** | markdown 实验报告：运行配置表 + 每场景四层指标表 + 任务判据；`baseline` 对比参数预留（E0 未启用） |
| **兼容入口** | `agent_bench.py` 保留文件名与 CLI；委托 `runner.cli_main`；`uv run python agent_bench.py --scenario all` 与旧命令一致 |

## 3. 验证结果

### 3.1 测试套件（42 passed）

| 测试文件 | 覆盖 |
|---|---|
| `test_config.py` | 默认值、dict/JSON/YAML 加载、非法值校验、CLI merge、旧键兼容 |
| `test_driver.py` | 返回字段、timings 三种提取路径、400 兜底重试、max_retry 上限 |
| `test_metrics.py` | mean/std/p50/p95 数值、cache_hit_rate、summarize 旧字段+新字段 |
| `test_workloads.py` | 四场景结果结构、history 累积、工具链完整流程、evaluate 判据、spec 指纹确定性 |
| `test_runner.py` | `all` 场景选择、long_life 旧字段、repeat/warmup 计数、落盘 JSON 结构 |
| `test_report.py` | markdown 章节与指标表 |
| `test_e2e_smoke.py` | **真实 HTTP 链路**：mock OpenAI server → Driver timings 解析、cli_main 端到端、YAML 配置驱动 |

### 3.2 关键验证点（E0 开放问题的落地）

1. **timings 在 OAI 响应的可解析性**（实现计划附录 B 开放问题 1）：mock server 返回
   llama-server 风格顶层 `timings` 字段，openai SDK 2.53.0 经 `model_extra` 透出，
   `Driver._extract_timings` 解析成功；并验证 **`timings.prompt_n + cache_n == prompt_tokens`**
   （与 llama.cpp 单测语义一致，`tests/unit/test_completion.py:661`）。
2. **结果结构一致性**：`{config, summary, scenarios}` 与旧脚本一致；summary 旧字段
   （prompt/completion/total tokens、rounds、avg/max latency、peak RSS/GPU）逐项断言存在；
   config 含全部 9 个旧 CLI 键。

### 3.3 真实 GPU 环境运行（2026-08-06 已执行）

**环境**：RTX 4060 8GB（驱动 610.43.03，CUDA 13.3，arch 89）；llama.cpp HEAD `b06aa774c`
（CUDA 构建，`-ngl 99`）；模型 `Qwen3.5-4B-Q4_K_M`（GGUF 2.7GB，ModelScope）。

**运行命令与产物**：

```bash
# 编译（本机首次）
cmake -B build-cuda -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 -G Ninja
cmake --build build-cuda -j 16

# server 1（ctx 8192，对齐旧 all 基线规模）→ all 场景
./llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 8192
cd benchmark && uv run python agent_bench.py --scenario all --ctx-size 8192
# → results/bench_20260806_121357.json

# server 2（ctx 2048，对齐 longlife 基线）→ long_life 场景
uv run python agent_bench.py --scenario long_life --long-rounds 40 --ctx-size 2048
# → results/bench_20260806_121628.json

uv run pytest -q   # 42 passed（单元 + e2e 冒烟）
```

**all 场景与旧基线对比**（`baseline/qwen3.5-4b_gpu_all_baseline.json`）：

| 场景 | 字段 | 旧基线 | 新运行 | 说明 |
|---|---|---|---|---|
| tool_call | prompt/completion/total | 7459 / 94 / 7553 | **7459 / 94 / 7553** | **完全一致**：工具调用轨迹逐 token 复现，证明迁移保真 |
| tool_call | avg_latency_ms | 477.4 | 404.3 | 正常波动 |
| multi_turn | prompt/total | 15646 / 16741 | 17495 / 18752 | 差异源于模型输出随机性（旧基线无 seed 控制、server 版本不同） |
| multi_turn | rounds | 20 | 20 | 一致 |
| branch | rounds / peak_gpu_mb | 11 / 3226.0 | 11 / 3226.0 | 一致 |

**long_life 场景与旧基线对比**（`baseline/qwen3.5-4b_longlife_40r_baseline.json`）：

| 字段 | 旧基线 | 新运行 | 说明 |
|---|---|---|---|
| rounds | 40 | 40 | 一致 |
| task_success | False | False | 一致（该场景在真实模型下秘密数字召回本就不稳定） |
| peak_gpu_mb | **None** | **3028.0** | **GPU 采样 bug 修复的直接证据**（旧代码恒返回 None） |
| truncations | 7 | 3 | 模型生成长度随机性影响截断时机 |
| cached_tokens_total | 20964 | 27124 | 同批次运行内可比 |

**timings 字段真实解析（实现计划附录 B 开放问题 1 的结论）**：
非流式 OAI chat/completions 响应**默认包含** `timings`（无需 `timings_per_token`）；
openai SDK 经 `model_extra` 透出，`Driver._extract_timings` 解析成功，且
**`prompt_n + cache_n == prompt_tokens` 在真实 llama-server 上成立**（multi_turn:
30+249=279；tool_call: 227+126=353）。

**`--report` 功能**：真实数据生成 markdown 报告（`results/bench_report_demo.md`），
含运行配置表与场景指标表，工作正常。

## 4. 与旧脚本的差异（有意的、已记录的）

| 差异 | 原因 | 兼容性影响 |
|---|---|---|
| 结果行追加 `timings` 键 | 记录缓存/吞吐细粒度信号（E0 目标） | 追加键，不删旧键；对比脚本按旧键子集即可 |
| summary 追加 `evaluation` 子对象 | 任务判据（保真约束）统一入口 | 追加 |
| config 键为旧 CLI 键超集 | 新增 model/repeat/warmup/seed/temperature 等 | 追加 |
| `find_server_gpu_mb` 修复 `nvml`→`pynvml` 笔误 | 旧代码该函数恒返回 None（NameError 被 except 吞掉） | 修复后 GPU 采样在宿主机可正常工作；容器内仍按约定降级为 None |
| YAML 配置支持 | 用户要求 | 新增（pyyaml 入依赖） |

## 5. 约束遵守情况

- ✅ 未修改 llama.cpp（阶段约束）
- ✅ 未实现 KV Cache 优化 / COW / Context 压缩
- ✅ 未删除既有 baseline（`benchmark/baseline/` 原样保留）
- ✅ 未大规模重构已有代码：`agent_bench.py` 保留为兼容入口；场景逻辑按迁移方式移植（常量、打印、结果结构一致）
- ✅ 新增 README（`benchmark/README.md`）与测试（42 个，无 GPU/server 依赖）
- ✅ 根 `AGENTS.md` 已同步目录结构变化（按约定及时更新）
- ✅ 未提交 git（等待人工确认）

## 6. 未做事项（下一阶段，未开始）

按实现计划：第一阶段剩余项（后台资源采样线程、baseline diff/`--compare`、并发 driver 与
`n_cmpl` 对照场景、ctx 探测校验）→ 第二阶段 llama.cpp KV 可观测性（`/kv-stats`）→
第三阶段 E1/E2/E3 优化验证。等待人工确认后进入下一阶段。
