# E10.3：Qwen3.5-4B q8_0 KV 生产化验收（q8_0 Production Validation）

- 复核时间：2026-08-08
- 环境：RTX 4060 8GB；Qwen3.5-4B-Q4_K_M.gguf（de8e96cd...）；CUDA build；同 seed 42/temp 0
- 协议：F16 vs q8_0 **严格 paired**（相同输入/seed/配置，仅 `--cache-type-k/v` 不同）；每 workload warmup 5 + **formal 20 reps**
- raw：`raw/e10_q8_validation.json`（全部 20 rep 记录）

## 1. 无损验证（输出 hash 与 F16 完全一致）

| workload | f16 hash 集合 | q8 hash 集合 | 一致 |
|---|---|---|---|
| short_qa | 1 | 1 | **True** |
| long_code | 1 | 1 | **True** |
| long_summary | 1 | 1 | **True** |
| tool_json | 1 | 1 | **True** |
| system_retention | 1 | 1 | **True** |
| canary | 1 | 1 | **True**（单值=无泄漏）|

**20 reps × 6 workload：q8_0 输出 token 与 F16 完全一致（0 差异）**——本模型/配置/生成长度下 q8_0 无损（不预设全局无损，仅本次验收范围）。

## 2. 容量与显存（实际总 GPU memory）

| 指标 | F16 | q8_0 | 变化 |
|---|---|---|---|
| KV capacity bytes（ctx2048/p2）| 67,108,864 | 35,651,584 | **-47%** |
| GPU 总显存 baseline（ctx2048/p2）| 2966 MB | 2936 MB | **-30 MB**（实际总显存下降）|
| per-cell | 32768 B | 17408 B | -46.9% |

## 3. 性能（prefill/decode/e2e p50/p95，formal 20）

| workload | f16 prefill p50 | q8 prefill p50 | f16 decode p50/p95 | q8 decode p50/p95 |
|---|---|---|---|---|
| short_qa | 36.6 | 36.0 | 101.5/103.9 | 103.6/106.0 |
| long_code | 36.9 | 35.2 | 214.9/218.1 | 217.7/221.3 |
| long_summary | 37.5 | 36.4 | 215.1/216.3 | 218.3/223.6 |
| tool_json | 34.9 | 35.7 | 214.5/217.7 | 216.8/220.1 |
| system_retention | 36.5 | 37.1 | 213.5/216.5 | 217.4/222.1 |
| canary | 37.1 | 36.2 | 212.8/217.5 | 216.3/219.3 |

**时延差异 ≤3%（decode +1.5~3.3%）**——q8_0 解码微增（量化 KV 解压），远低于退化门槛；prefill 持平。

## 4. 稳定性与失败

- failed/rejected：**0**（6 workload + 容量档 × 20 reps 全 200）
- JSON parse/tool 输出：tool_json workload 输出与 F16 一致（无 schema 破坏）
- canary：无泄漏（输出单值、与 F16 一致）
- 容量档（cap_long_summary，ctx4096/p4）：20/20 成功（f16 与 q8_0）

## 5. q4_0 次级观察

- server 稳定启动、10 reps 输出自洽（单 hash c5e89e3b）——**稳定运行确认**
- 与 F16 输出一致性**未对比**（量化更深，需独立质量门禁）→ 标注为次级 profile，不进入本验收主结论

## 6. 验收状态

```text
QWEN35_Q8_KV_STATUS: PASS_DEPLOYABLE
```

依据：无损（20×6 全一致）、KV capacity -47%、实际总 GPU memory -30MB、时延 ±3%、0 失败、canary 无泄漏。

## 7. 可复现部署 profile（validated deployment profile，不改上游功能）

```bash
llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  -ngl 99 --ctx-size 4096 --parallel 4 --kv-unified \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 0 --seed 42
```

- 标注：`validated deployment profile`（上游既有参数组合 + 本验收数据；非新代码）
- 回归：`uv run python benchmark/scripts/e10_3_q8_validation.py --reps 20`（可复现）

## 8. 说明

- q8_0 收益 = 同容量 KV 翻倍潜力 + 总显存下降 30MB（8GB 卡上有意义）；**为上游既有开关（非新算法实现）** → 项目状态规则见 E10_FINAL
