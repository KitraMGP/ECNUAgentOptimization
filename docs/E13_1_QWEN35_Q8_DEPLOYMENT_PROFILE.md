# E13.1：Qwen3.5-4B q8_0 validated deployment profile 固化（Deployment Profile）

- 复核时间：2026-08-08
- 用途：唯一推荐部署配置（validated deployment profile；上游既有参数组合 + 本验收数据，非新算法）

## 1. 推荐配置（唯一）

```bash
llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  -ngl 99 --ctx-size 4096 --parallel 4 --kv-unified \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --temp 0 --seed 42
```

## 2. 配置要素

| 项 | 值 |
|---|---|
| 模型 | `models/qwen3-5-4B-Q4_K_M.gguf` |
| 模型 SHA256 | `de8e96cd0d0c3584...` |
| CUDA build commit | llama.cpp `f54930492` |
| KV type | `--cache-type-k q8_0 --cache-type-v q8_0`（唯一推荐）|
| ctx / parallel | 4096 / 4（kv-unified；8192 未验证）|
| generation | temp=0、seed=42（验证范围）|
| 已验证 workload | short_qa / long_code / long_summary / tool_json / system_retention / canary / multi_turn / save_restore / long_prompt ~1000 tokens |
| 协议 | warmup 5 + formal 20 paired（F16 vs q8_0，同输入/seed/生命周期）|

## 3. 验证边界（如实）

| 项 | 值 |
|---|---|
| token-exact | **仅限本验证矩阵**（temp=0/seed=42/生成长度 ≤16 tokens）——不声称全局无损 |
| decode 最大精确退化 | **2.10%**（short_qa；全 workload 1.06-2.10%）|
| KV capacity | **约 -47%**（67.1MB → 35.7MB @ctx2048）|
| GPU peak | **约 -58MB**（3032 → 2974 MB，5 次采样稳定）|
| q4_0 | **NOT_VALIDATED**（仅 1 场景对照；独立观察）|
| 8192 ctx | **未验证**（8GB 卡显存限制）|
| 收益形态 | **显存/KV 容量收益为主**；吞吐持平（decode +1.06~2.10%）——不声称普遍性能提升 |

## 4. 可复现配置文件

`benchmark/configs/qwen35_4b_q8_validated.yaml`（本阶段新增）：

```yaml
# Qwen3.5-4B q8_0 validated deployment profile（E13.1 固化）
# 状态：PASS_VALIDATED_DEPLOYMENT_PROFILE（非新算法；上游既有参数 + 验收数据）
model:
  path: models/qwen3-5-4B-Q4_K_M.gguf
  sha256_prefix: de8e96cd0d0c3584
kv:
  cache_type_k: q8_0
  cache_type_v: q8_0
  kv_unified: true
  ctx_size: 4096
  parallel: 4
generation:
  temperature: 0
  seed: 42
validation_scope:
  workloads: [short_qa, long_code, long_summary, tool_json, system_retention,
              canary, multi_turn, save_restore, long_prompt_1000]
  protocol: { warmup: 5, formal: 20, paired: f16_vs_q8_0 }
  boundaries:
    token_exact: "仅限本矩阵（temp=0/seed=42/≤16 tokens）"
    decode_max_regression_pct: 2.10
    kv_capacity_change_pct: -47
    gpu_peak_change_mb: -58
    q4_0_status: NOT_VALIDATED
    ctx_8192: NOT_VALIDATED
expected_status: PASS_VALIDATED_DEPLOYMENT_PROFILE
```

## 5. 轻量回归命令（验证配置生效）

```bash
# 1) server 可启动 + q8_0 参数生效
llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  -ngl 99 --ctx-size 2048 --parallel 1 --kv-unified --cache-ram 0 \
  --cache-type-k q8_0 --cache-type-v q8_0 --metrics \
  --log-file llama.cpp/tmp/e13_q8_reg.log &
sleep 20 && curl -s http://127.0.0.1:8080/health

# 2) canary 输出 + KV per-cell
curl -s http://127.0.0.1:8080/completion -d '{"prompt":"The secret is CANARY-ALPHA-3947. What should I remember?","n_predict":8,"temperature":0}'
curl -s http://127.0.0.1:8080/metrics/kv   # 期望 capacity_cells=2048, per-cell=17408B（q8_0）

# 3) 无失败退出（回归脚本见 benchmark/scripts/e13_1_q8_regression.py）
uv run python benchmark/scripts/e13_1_q8_regression.py
```
