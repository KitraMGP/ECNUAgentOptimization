# E0.5 实现报告：Benchmark 真实环境验证

> 阶段：Phase E0.5（真实环境闭环验证，无新功能）
> 目标：验证 Benchmark 框架能否真实驱动 llama.cpp server（driver → OpenAI 兼容 API → llama-server 完整闭环），并确认各 workload 的行为是否符合设计预期。
> 范围：仅 bug 修复 / 参数修复 / 文档补充；未修改 benchmark 设计、未添加指标、未修改 llama.cpp。

---

## 1. 测试环境

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 4060 8GB（驱动 610.43.03，CUDA 13.3，arch 89） |
| 模型 | Qwen3.5-4B-Q4_K_M（`models/qwen3-5-4B-Q4_K_M.gguf`，2.7GB，ModelScope） |
| llama.cpp | HEAD `b06aa774c`，`build-cuda`（`-ngl 99`） |
| llama-server | `n_slots=4`；实验 1：`n_ctx_slot=2048`；实验 2：`n_ctx_slot=8192` |
| benchmark | E0 框架（framework/workload/metrics/runner/report），commit `7c524de` + 本次 driver 修复 |
| 推理参数 | `enable_thinking: false`，temperature=0（默认），`--ctx-size` 与 server 一致 |

## 2. 执行命令

```bash
# 启动 server（实验 1：ctx 2048 —— long_life 需触发 KV 回收压力）
./llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 2048

# 最小闭环冒烟
uv run python agent_bench.py --scenario multi_turn --rounds 3 --ctx-size 2048

# tool_call（ctx 2048 下会触发 400 兜底路径，验证修复）
uv run python agent_bench.py --scenario tool_call --tool-steps 6 --ctx-size 2048

# long_life（ctx 2048，40 轮）
uv run python agent_bench.py --scenario long_life --long-rounds 40 --ctx-size 2048

# 重启 server 为 ctx 8192 后跑 all（multi_turn 20 / tool_call 6 / branch 5）
pkill -x llama-server   # 单独执行，避免 pkill -f 自杀陷阱（AGENTS.md 约定）
./llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 8192
uv run python agent_bench.py --scenario all --ctx-size 8192

uv run pytest -q        # 43 passed（含本次新增的兜底保留真实 user 查询用例）
```

产物：`results/bench_20260806_122430.json`（冒烟）、`bench_20260806_122715.json`（tool_call 修复后）、
`bench_20260806_122831.json`（long_life）、`bench_20260806_123059.json`（all）。

## 3. 实验结果

### 3.1 闭环验证（1/3/2 项）

`benchmark driver → OpenAI 兼容 API → llama-server` 完整闭环跑通：
所有场景请求均返回有效 completion，`usage.prompt_tokens_details.cached_tokens` 与
`timings`（prompt_n/cache_n/predicted_ms 等）均被框架正确解析（E0 已验证
`prompt_n + cache_n == prompt_tokens`，本阶段再次成立）。GPU 采样正常（3028 MB，4B 模型占用）。

### 3.2 multi_turn（context 增长 + 前缀缓存复用）

| 轮次 | prompt_tokens | cached_tokens | latency(ms) |
|---|---|---|---|
| 1 | 34 | 0 | 6806.1（冷启动） |
| 2 | 519 | 0 | 473.2 |
| 5 | 610 | 575 | 385.4 |
| 10 | 1406 | 1077 | 356.2 |
| 15 | 1602 | 1529 | 620.4 |
| 20 | 2017 | 1984 | 325.4 |

summary：rounds=20，total=25887，cache_hit_rate=0.9123，task_success=True。
结论：context 随轮次增长，前缀缓存命中率稳定高位；冷启动首轮显著慢（预热必要性再证实）。

### 3.3 long_life（增长 context + 应用层截断 + KV 回收压力）

| 轮次 | kind | prompt_tokens | cached_tokens | total_tokens |
|---|---|---|---|---|
| 1 | chat | 34 | 0 | 411 |
| 4 | tool | 797 | 671 | 883 |
| 8 | tool | 1980 | 1447 | 2048（接近上限） |
| 12 | tool | 1523 | 1397 | 1603（截断后回落） |
| 16 | tool | 502 | 376 | 518（截断后回落） |
| 28 | chat | 1151 | **0** | 1168（前缀失效，触发重算） |
| 40 | chat | 759 | 705 | 788 |

summary：rounds=40，truncations=3，task_success=False，cached_tokens_total=25180。
结论：**增长 context 真实产生**（prompt 34→1980）；应用层截断 3 次，截断后 cached 骤降
（轮 28 cached=0 触发完整重算）——长生命周期 KV 回收压力场景行为符合设计。
task_success=False 与旧基线一致（秘密数字被截断丢弃，属该场景预期）。

### 3.4 tool_call（重复 context + 完整工具链）

| 步 | action | prompt_tokens | cached_tokens |
|---|---|---|---|
| 1 | search_orders | 214 | 0 |
| 2 | get_order_detail | 353 | 227 |
| 3 | get_order_detail | 901 | 368 |
| 4 | get_order_detail | 1449 | 916 |
| 5 | get_order_detail | 1997 | 1464 |
| 6 | get_order_detail | 2545 | 2012 |

summary：rounds=6，**total=7553（与旧基线 7459/94/7553 完全一致）**，cache_hit_rate=0.6686，
tool_calls=6，task_success=True。
结论：**重复 context 真实产生**（prompt 214→2545 逐步膨胀，工具 JSON 反复回填）；
token 数与旧基线逐 token 一致，再次证明迁移保真。

### 3.5 branch（多分支 + 公共前缀共享）

- branches = {A: 5 轮, B: 5 轮}，common 前缀 prompt=40；
- 分支 A prompt 311→4584（多轮分支展开），分支 B prompt 311→1201；
- summary：rounds=11，cache_hit_rate=0.6309（公共前缀缓存复用），branch_consistency=1.0，task_success=True。
结论：**多分支真实产生**，公共前缀被两个分支复用（命中 0.63），符合分支推理场景设计。

## 4. 发现的问题

| # | 问题 | 严重度 | 根因 |
|---|---|---|---|
| 1 | tool_call 在 ctx 2048 下第 6 步触发 400 兜底后，重试请求返回 **500 `Jinja Exception: No user query found in messages`**（openai.InternalServerError），整个场景崩溃 | 高（真实 bug） | Qwen3.5-4B chat template（`Qwen3.5-4B.jinja:67-80`）的 `multi_step_tool` 分支要求消息中至少存在一条**非 `<tool_response>` 的 user 消息**（从后往前找真实用户查询）。driver 的 400 兜底按"丢弃最早 1/3 非 system 消息"处理，把**唯一的真实用户查询**（首条 user）丢掉了，剩余全是 tool_response user → template 判定"无用户查询"→ 500 |
| 2 | `--ctx-size 2048 --parallel 4` 时每 slot 仅 512（总 ctx 被分割） | 低（参数语义） | 该 llama.cpp 版本 `--ctx-size` 为总上下文、按 `--parallel` 平分到 slot；与旧基线"ctx-size=每 slot"的语义不同，需明确文档 |
| 3 | tool_call 在 ctx 2048 下必然触发 400 兜底（第 6 步 prompt 1997+ 超限） | 低（预期行为） | 工具 JSON 膨胀后 prompt 超过小 ctx；属预期的回收压力，非 bug（修复后兜底可正常完成） |

## 5. 修复内容

| 修复 | 内容 | 文件 |
|---|---|---|
| driver 400 兜底保留真实用户查询 | 兜底丢弃消息后，若候选消息中已无"非 `<tool_response>` 的 user 消息"，则从被丢弃的早期消息中找回最早一条真实用户查询插入 system 之后，保证重试请求可被 Qwen3.5 chat template 渲染；找不到则抛出原异常（不发送非法请求） | `benchmark/framework/driver.py`（新增 `_is_real_user_query` + 兜底逻辑） |
| 回归测试 | 新增单测 `test_chat_retry_keeps_real_user_query`：构造"真实查询 + 全 tool_response 回填"的消息序列，断言 400 兜底后仍保留真实用户查询 | `benchmark/tests/test_driver.py` |

修复验证：`uv run pytest -q` → **43 passed**；真实 server 上 tool_call（ctx 2048）触发 2 次兜底后正常完成
（`tool_calls=6, task_success=true`，无 500）。

文档补充：`docs/E0_5_VALIDATION_REPORT.md`（本文件）。

## 6. 是否可以进入 E1

**结论：可以进入 E1（KV Cache 生命周期管理评测），附以下条件/建议：**

1. ✅ 框架可真实驱动 llama-server 闭环，四个 workload 行为均符合设计（增长 context / 截断重算 / 重复 context / 多分支共享）；
2. ✅ 阻塞性 bug（500 template 崩溃）已修复并有回归测试；
3. ⚠️ 建议 E1 前补充：`--parallel` 与 `--ctx-size` 的 per-slot 语义在 `benchmark/README.md` 与 run config 校验中明确（当前 `--ctx-size` 参数名与 server 语义对齐即可，E0 已采用"与 server 一致"约定）；
4. ⚠️ 建议 E1 正式实验统一使用 `warmup ≥ 1`（冷启动首轮 6806ms vs 稳态 ~400ms，差距显著）与 `repeat ≥ 3`（当前单次运行，未报告方差）；
5. ⚠️ long_life 的 `task_success` 判据在真实模型下稳定为 False（秘密数字被截断丢弃），E1 评估生命周期优化收益时建议以 `cache_hit_rate` 衰减曲线与 recompute 比例为代理指标（与实现计划一致），任务成功率仅作保真约束参考。

未提交 git（等待人工确认）。
