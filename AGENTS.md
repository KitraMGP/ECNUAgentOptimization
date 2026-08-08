# 面向智能体的内存管理系统（高校赛题 14）

基于 llama.cpp 扩展的 Agent 长生命周期推理内存优化项目：KV Cache 生命周期管理、分支共享（COW）、Prompt 压缩，配套可复现的 Agent 工作流 Benchmark。

## Project

- 目标：在保证推理效果前提下降低智能体推理的显存/内存占用与延迟（赛题要求优化前后同硬件对比）。
- 技术栈：llama.cpp（C++ 推理框架，qwen35 架构）+ Python 3.13 / uv（benchmark）+ OpenAI 兼容 API。
- 入口：`benchmark/agent_bench.py`（兼容入口，委托 `runner.cli_main`）；核心优化代码位于 `llama.cpp/tools/server/`（slot 生命周期策略层）与 `llama.cpp/src/`（KV 统计接口）。
- 模型：Qwen3.5-4B（GPU 正式；GGUF `qwen3-5-4B-Q4_K_M.gguf`，当前已下载）/ Qwen3.5-0.8B（CPU 开发；`Qwen3.5-0.8B-Q4_K_M.gguf`，可脚本拉取）/ Qwen2.5-0.5B（最小验证；`qwen2.5-0.5b-instruct-q4_k_m.gguf`，可脚本拉取），GGUF 在 `models/`（不入库）。

## Commands

```bash
# 编译 llama.cpp（在 llama.cpp/ 内）
cmake -B build -DGGML_CUDA=OFF && cmake --build build -j $(nproc)        # CPU
cmake -B build-cuda -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 && cmake --build build-cuda -j $(nproc)  # GPU

# 下载模型（models/ 内；模型文件不入 git，必须用脚本拉取）
./download_models.sh          # 全部（0.5b/0.8b/4b）
./download_models.sh 4b       # 单个

# 启动 llama-server（GPU 正式对比；优化机制开关）
./llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 8192 \
  --kv-unified --parallel 4 \
  --unified-idle-slot-policy default    # 或 lru（experimental：unified 价值感知淘汰）
  --lifecycle-trace                     # 可选：归属诊断（默认关闭，debug-only）

# 运行 benchmark（benchmark/ 内，uv 管理依赖）
uv sync
uv run python agent_bench.py --scenario all            # 三场景：multi_turn/tool_call/branch
uv run python agent_bench.py --scenario long_life --long-rounds 40   # 长生命周期（需 --ctx-size 与 server 一致）
```

## Architecture

- `benchmark/agent_bench.py`：兼容入口（E0 重构后为薄壳，委托 `runner.cli_main`；CLI 参数与输出格式向后兼容）。
- `benchmark/framework/`：核心框架——`config.py`（JSON/YAML/dict 配置系统）、`driver.py`（OpenAI 兼容 API 封装，400 兜底 + timings 提取；E15.3 起支持 `preprocessor=` 发送前结构化压缩）、`sampler.py`（RSS/GPU 采样）、`workload.py`（Workload 抽象：`generate/run/evaluate` + 注册表）、`prompt_preprocessor.py`（E15.3 B1：deterministic structured-lossless preprocessor，消息级完全重复去重 + protected 保留 + round-trip restore + fail-fast）、`tool_payload.py`（E15.2 ToolPayloadStore）、`context_policy.py`（E15.4 C 线核心）。
- `benchmark/workload/`：四场景实现（multi_turn / tool_call / branch / long_life），导入即注册；结果结构与旧脚本一致（行内追加 `timings` 键）。
- `benchmark/metrics/`：p50/p95/mean/std/cache_hit_rate/summarize（保留旧字段）。
- `benchmark/runner/`：场景 × repeat × warmup 编排、结果落盘 `results/bench_<ts>.json`（`{config, summary, scenarios}`）；`e15_branch_concurrent.py`（E15.1 分支并发 paired）、`e15_3_b1_paired.py`（E15.3 B1 4B greedy paired 门禁）。
- `benchmark/report/`：markdown 实验报告。
- `benchmark/configs/`：示例配置（example.json / example.yaml）+ q8_0 配置（`qwen35_4b_q8_validated.yaml` 为 E13.1 固化；`qwen35_4b_q8_production.yaml` 为 E14.1 固化）。
- `benchmark/tests/`：pytest（不依赖 GPU/真实 server，含 mock OpenAI server e2e 冒烟）。
- `benchmark/baseline/`：正式基线归档（可读命名 `<模型>_<环境>_<场景>_<说明>.json`）；`benchmark/results/` 为运行期临时输出（不入库）。
- `llama.cpp/`：上游框架（独立 git 仓库，根仓库不跟踪）；KV 优化改动在其中实施并内部提交。
- `models/download_models.sh`：可复现模型拉取。

## Conventions

- **可自动运行 llama-server / benchmark**：宿主机可访问 GPU（RTX 4060 8GB，驱动 610.43.03）；只要用户没有要求不能自动运行，都可直接运行。注意耗时（all 场景约 5–10 分钟、long_life 40 轮约 10–20 分钟）与显存（4B 模型约占 3.2GB，8GB 卡需留意并行 slots 与 ctx 大小）。
- **pkill/pgrep -f 会匹配 bash 作业自身命令行**：`bash -c` 把整条命令文本（含启动 llama-server 的命令）放入进程 cmdline，`pkill -f "llama-server ..."` 或 `pgrep -af "build-cuda/bin/llama-server"` 会匹配到执行该命令的 bash 作业自身，导致"自杀"（把自己杀掉，新 server 也随之未启动；2026-08-06 bash-4 事故）。避免方法：① 杀进程用 `pkill -x llama-server`（精确进程名，不匹配命令行文本）；② 查询进程用 `pgrep -x llama-server`；③ "先杀 → 确认无残留 → 再单独启动"必须拆成独立命令/作业，绝不在同一 bash 命令行里既 pkill 又启动同一模式的服务。
- **GPU 显存采样问题**：容器内 pynvml 报 `NVMLError_DriverNotLoaded`，`peak_gpu_mb` 返回 null 说明 GPU 未加载，需要暂停工作等待用户处理。
- **实验结果归档**：正式结果移入 `benchmark/baseline/` 并改可读文件名；`results/` 只留临时输出（.gitignore 已忽略）。
- **git**：`llama.cpp/` 独立仓库不纳入根仓库；`models/*.gguf`、`build*/`、`.venv/` 不入库；用户要求**每个阶段完成后都提交代码**（授权提交后执行提交，并同步更新相关文档中的 git 提交状态说明；未经授权不提交）。
- **commit message 格式**：标题 `<feat|fix|chore|docs|refactor，可多个用 & 连接如 feat&fix>: <摘要>`，空一行后分点（`- ` 开头）详细描述改动内容。
- **模型能力限制**：Qwen3.5-0.8B 指令遵循不稳定（工具参数可能填错）；默认 `--no-think`/`enable_thinking:false` 防思考循环。
- **不要自己尝试安装软件包**：如果需要安装非普通 uv Python 依赖的软件包，需要使用 root 权限安装软件，或者需要写入不可写目录，不要自己操作，请停下来让用户操作。
- **AGENTS.md 必须及时更新**：当命令、目录结构、约定或架构发生变化时，本文件应在该变更落地后立即同步更新，保持准确——这是每个 agent 与协作者的职责，不要等到项目结束时才补。

## Notes

- E0（Benchmark 基础设施重构）已完成：`framework/ workload/ metrics/ runner/ report/ configs/` + 42 个 pytest（E0 阶段实测，见 `docs/E0_IMPLEMENTATION_REPORT.md`）；**当前全仓累计 pytest = 413**（截至 2026-08-08 阶段 0 的实测数，后续新增测试以实测为准：`cd benchmark && uv run pytest -q` → `413 passed`；413 = 177 + 236，其中 **177 为 E15 之前的全仓累计（E1–E4 阶段实测，已含 E0 的 42，不再累加）**，E15 系列**新增** 236 个：E15.1 branch_concurrent 48、E15.2 tool_payload 65 + 集成 21、E15.3 preprocessor 34、E15.4 context_policy 68——`test_workloads.py` 10 为原有回归不计新增；均在 benchmark/ 内，不依赖 GPU/server；注意必须在 `benchmark/` 目录下运行，否则 pytest 会收集到 `llama.cpp/` 子仓库的测试）。
- E1（llama.cpp 可观测性）已完成：`GET /metrics/kv`（KV 统计）+ KVProbe；详见 `docs/E1_*`。
- E2（生命周期候选探索）已完成：A2 prefix-branch 路由实现后 REJECT 冻结；`--cache-ram` 默认 8192；详见 `docs/E2_*`。
- E3（A1/A4 统一 idle-sequence 价值感知回收）已完成：`--unified-idle-slot-policy default|lru`（lru experimental，默认 default）+ `--lifecycle-stats` + `--lifecycle-trace`（归属诊断）；真实场景收益稀释 → KEEP_EXPERIMENTAL；性能门禁因 Laptop GPU 抖动 HOLD；详见 `docs/E3_*`。
- E4（内存容量验证）已完成：并发承载 ≥8 session、OOM 边界 >96%（exploratory）；详见 `docs/E4_*`。
- E3.6（真实流量回放）**未执行**：硬前置 = 用户提供且 validator（`benchmark/schemas/lifecycle_trace_v1.json`）通过的真实匿名 trace。
- E6-E8（KV 优化实施与复核）已完成：C1 attention-only KV 优化真实且正确（TinyLlama 收益），但主模型 Qwen3.5-4B（hybrid）无收益 → E7 起 `PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION`；tenant 限定 TRUSTED_SINGLE_TENANT；详见 `docs/E6_* / E7_* / E8_*`。
- E9-E13（C1 关闭 + q8_0 生产化 + 主模型转向）已完成：C1_ATTENTION_ONLY_PASS；q8_0（`--cache-type-k/v q8_0`）部署 profile 固化 `PASS_VALIDATED_DEPLOYMENT_PROFILE`；checkpoint 主模型路径 E12 NO_GO_WITH_EVIDENCE（prototype 保留 experimental attention-only）；详见 `docs/E9_* / E10_* / E11_* / E12_* / E13_*`。
- E14（发布/灰度/运维交接）已完成：`RELEASE_STATUS: READY_FOR_DEPLOYMENT`（q8_0 production profile，15 项验收 + 回滚演练通过）；**真实生产灰度未执行**（`PRODUCTION_ROLLOUT_STATUS: NOT_EXECUTED`，无生产环境，不虚构）；运维交接见 `docs/E14_5_OPERATIONS_HANDOFF.md`；详见 `docs/E14_*`。
- E15（E14 后独立扩展，四技术线分阶段，方案见 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md`）：**E15.0** 方案已提交（根 `5dd6b79`）；**E15.1** 分支并发共享不变量加固 + 真实 fan-out 验证 **PASS**（根 `f357a42` / llama.cpp `4a699aaad`，零核心代码改动；未跑 4B——hybrid 由 capability gate 永久禁用共享）；**E15.2** ToolPayloadStore 外置存储 + workload 集成已提交（根 `a2e0558`；**未跑真实 4B paired**，收益待 §6 后续动作）；**E15.3** B1 deterministic structured-lossless preprocessor（消息级重复去重 + protected 保留 + restore/fail-fast，配置 `config.extra["preprocessor"]` 默认 off）已提交；**4B greedy paired 无损门禁不成立**（single 压缩生效但输出不一致 → `HOLD_NOT_VALIDATED` 降级有损转 E15.5；multi_turn 短中文块 token 无收益 → `HOLD_NO_MEASURABLE_GAIN`；restore 对照确认实验有效；llama.cpp 未改）；**E15.4** C 线上下文策略独立核心 **DRAFT** 已提交（根 `c9c26ab`；未集成 workload、未跑真实模型 paired）；**E15.5（有损摘要 oracle）/ E15.6（完整验证收口，含 4B 门禁）尚未完成**；详见 `docs/E15_*`。
- **M0（阶段 0 后首条路线）**：真实智能体多路径决策 workload + 分支内存基线——**调研完成 + 设计完成（DESIGN ONLY，v38 全桶 parity 执行语义与错误编码收口），未实现 workload 代码、未运行正式 benchmark**；详见 `docs/M0_BRANCH_MEMORY_BASELINE_DESIGN.md`（实施建议 GO）。关键探针结论（4B Q4_K_M + ctx4096 + unified，RTX 4060 8GB）：**32 层 = 8 attention + 24 recurrent**（qwen35.cpp fallback interval=4）；attention KV 固定池 68 MiB(q8_0)/128 MiB(f16) 与分支数无关（per-cell q8 17408 B = 8×8×128×2×34/32）；recurrent state **50.25 MiB/slot** 随 parallel 线性增长（= 24 层 × (n_embd_r 24576 + n_embd_s 524288) × 4B，实测闭合）；parallel 10 q8_0 峰值 3402/8188 MiB 无显存阻塞；合法矩阵 = fanout{2,4,8}→parallel{4,6,10} × 桶 short(150/150)/medium(280/480)/long(400/600)，预算函数 `budget=P+N×(P+B) ≤ 3481 cells` 逐 unit（**P/B 由 /apply-template + /tokenize 实测**；一致性门禁 = chat `usage.prompt_tokens` vs tokenize 偏差 0，**证据归档 `benchmark/baseline/m0_probe_thinking_token_parity_20260809.json`**，add_special 幂等归因 = 该 GGUF `add_bos_token=false`、bos_token_id 缺失、add_eos_token 字段缺失（无 BOS），换模型必须重验）；`/metrics/kv` 只统计 attention（recurrent 无运行期接口；回收门禁仅 attention，GPU 回落仅 smoke）；架构定稿独立 runner（不注册第五场景）；driver 扩展保留 `_retry` 位置参数（driver.py:132 递归兼容、_extra_body :135-142）+ `sdk_max_retries` 可选参数（M0 实例化 `Driver(max_retry=1, sdk_max_retries=0)`，6-wire 上限；context400 递归透传参数）；thinking 合同：server 默认 thinking 开启，必须显式 enable_thinking=false（server-common.cpp:1095-1102 + 证据）；**决策谓词 → rep `status=INVALID_DECISION`（⇔ decision_fallback=true）；rep 状态机 OK/INVALID_DECISION/ERROR；G-M0-1 专用验证 <100% → G-M0-1=FAIL 但 formal 仍请求模型；5b 三触发（启动失败/崩溃且 session 不完整/序列化失败）、验证崩溃/启动失败边界（v23-v30）、验证请求 retry 三路；formal server 启动失败保留完整证据、三类 code；**SCHEMA_INVALID 落盘合同（v34）：**唯一权威 canonical meta 表（27 键，ctk/ctv 拆两行逐项计数；model/model_id 去重只留 model_id；nullable 白名单 5 键（parity_compensation 必填但允许 null 恒 null）；decision_validated 仅 decision_validation 完整且 G-M0-1=PASS 时 true、decision_fallback 仅 formal 任一 rep fallback 时 true；**parity_ok/token_count_method 总规则（v35，不按 preflight_reason 硬编码）**：parity 全部桶完成且偏差=0 → true、全部完成但偏差≠0 → false、未完整完成 → null；token_count_method 完整执行 apply-template+tokenize 时 = 该字符串（无论 pass/mismatch）、未完整建立 → null；validation_incomplete、验证/formal server_crash/health_failed/endpoint_unavailable 若 parity 已通过均 true+apply-template+tokenize（validation_incomplete 差异字段含 parity_ok）；parity_progress 保留实际进度（validation_incomplete 不恒空）；SCHEMA_INVALID 专有值清单仅 canonical 表一处权威定义、全文不复制不内联不完整字段）**；**不可 null 默认值**（binary_version 未知 → unknown、整数运行配置来自 CLI/矩阵合同不得魔法 0、字符串不可空串）；**tmp kind 统一 {main, sidecar}**；**decision_validation 两者均 null**；**fanout_prompts.py 只构造 doc、写入委托 m0_schema.py atomic writer**；**source_phase：普通 PREFLIGHT_INFRA 即使 formal server 启动触发也 = phase=preflight，仅 schema_invalid 记录原触发 formal**；**SCHEMA_INVALID 为 parity 总规则固定错误报告例外（v35：parity_ok/token_count_method 恒 null、parity_progress 恒空，不信任原始对象）**；**CLI/config 与预算职责单一化（v35：CLI 仅校验语法/枚举/类型/结构非法 → EXIT_USAGE=64 不落盘；budget 超限统一 budget 函数 → preflight_rejections/ALL_BUDGET_REJECTED 落盘不由 CLI 拦截）**；**parity_progress.completed 权威全集 short-P,short-B,medium-P,medium-B,long-P,long-B（v35）**；**exit constants 统一 64/65/70/74（m0_schema.py）**；测试㉔ budget-only / ㉕ CLI-only / ⑯ canonical 表派生键集；**planned_units=24 语义（v36：静态预算预筛 6 组合×2ctk×2control；long×4 等预筛排除不写 preflight_rejections；rejections 仅记精确复核后进一步超限的 24 候选）**；**parity 交叉不变量（v36：parity_ok∈{true,false} → completed 严格等于权威有序 6 项；parity_ok=null → 真前缀；SCHEMA_INVALID 例外 null+空）**；**CANONICAL_META_KEYS 常量单一来源 + build_preflight_envelope 签名合同（v37 名称统一）**；CLI 不暴露 --parallel；**PARITY_MISMATCH 全桶统一判定（v37：完成 6 桶后判定、偏差不 fail-fast、parity_ok=false+completed 全集+errors 记 parity_mismatch；仅基础设施中断才 null+前缀）**；**completed 语义（v37：流程完成即计入即使 mismatch；基础设施失败桶不计只入 errors）**；**ALL_BUDGET_REJECTED 仅 24 候选精确复核全部被拒（v37/v38：preflight_rejections 永不含静态 3 组合）；parallel:=fanout+2 恒等派生**；**全桶 parity 执行语义（v38：数值 mismatch 不单桶 fail-fast、完成全部 6 项后统一判定；仅基础设施中断 → null+前缀；混合场景 mismatch 条目保留证据不丢）**；**parity_progress.errors 增 template∈{P,B}（v38），唯一性键 (code,stage??null,bucket??null,template??null)**；**测试⑯ CANONICAL_META_KEYS 常量驱动（v38）**；测试⑯ 逐字段覆盖 + phase 错配反例；**validation_incomplete 与 schema_invalid 差异字段**（schema_invalid parity_progress 恒空、schema 错误只在 sidecar；validation_incomplete 属 PREFLIGHT_INFRA、parity_progress 保留实际校准进度）；**source_phase validator**（普通结果必须 ==phase；SCHEMA_INVALID 例外 phase=preflight 且 source_phase∈{preflight,formal}）；**decision_validation 保守恒 null**（不信任子结构、formal 来源也不复制，与 PREFLIGHT_INFRA 保留独立验证证据路径不同）；**atomic writer 共用**（普通主结果也走 m0_schema.py atomic writer，无第二套非原子写入）；测试⑯ 逐字段覆盖；**SCHEMA_INVALID 落盘：专有值清单仅 canonical meta 表一处权威、全文不复制不内联（本处不展开）；段落仅保留特有说明（notes sidecar 路径、禁止递归、自检 70）；phase 合法组合加 SCHEMA_INVALID（测试⑯/映射表覆盖）；写入流程 5 步（validate→构造→自检 70 无文件→临时文件+fsync+先 rename sidecar 再主 envelope+fsync 目录→部分落盘语义）；⑬b-d validation_incomplete 用同一 canonical builder（exit 0）；sidecar 细化（RFC6901、类型白名单、归因 12 码不适用）；退出码 0 含所有合法落盘（65 除外）；⑬b-f 改「保留合法 envelope+结构化 sidecar 证据」；§6 分配 m0_schema.py + test_m0_schema.py；sidecar 独立 validator 错误码枚举（missing_key/extra_key/type_mismatch/enum_mismatch/invalid_value/invariant_violation，path JSON Pointer、类型名不保存值）；validator 拆分 validate_result_envelope/validate_schema_sidecar；退出码 0（合法结果成功落盘含 INVALID 但 65 除外）/64（EXIT_USAGE：CLI/config preflight 解析失败 → stderr INVALID_CONFIGURATION、不落盘；配置完整后构造结果对象 schema 非法才走 65）/65（EXIT_SCHEMA_INVALID 完整证据对）/70（EX_SOFTWARE INTERNAL_SCHEMA_ENVELOPE_BUG 不递归包装）/74（IO_WRITE_FAILED）；写入顺序（内存验证→临时文件+fsync→先 rename sidecar 再主 envelope；无主 envelope 时 sidecar 仅诊断）；⑬b-f 不落原始构造 JSON；source_phase 覆盖 preflight/formal；finally 不删 results 主/sidecar**；verdict 五值枚举 + 稳定规则名称 + 聚合签名；**planned_units 派生值；结构化错误码（12 枚举）；preflight_reason 映射（含 SCHEMA_INVALID→仅 schema_invalid）；token_count_method 彻底唯一；chars/2.0 正式诊断、chars/2.5 仅旧估算对照；ACTION 统一 branch(bX)；测试①–㉖（⑬a-f 六子例、⑰ 清理固定序列 + results 主/sidecar 保留断言、㉖ 5b rejections 正反）**；M0 不用 OpenAI tools schema；探针二进制 8569/afbf375c6（核心代码与 HEAD 等价）。
- 关键结论：KV 预分配固定 → 生命周期策略不能降显存峰值；lru 价值在"池满防 OOM + 压力下保热点"；q8_0 为上游既有参数组合（非新算法），不宣称主模型算法优化。
