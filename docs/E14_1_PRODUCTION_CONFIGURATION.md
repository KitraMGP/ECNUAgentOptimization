# E14.1：不可歧义生产配置（Production Configuration）

- 复核时间：2026-08-08
- 配置文件：`benchmark/configs/qwen35_4b_q8_production.yaml`（SHA256 见 E14.0）

## 1. 生产配置字段（全部明确）

| 字段 | 值 |
|---|---|
| model path | `models/qwen3-5-4B-Q4_K_M.gguf` |
| model SHA256（完整）| `de8e96cd0d0c358487091aaaed1346bc02e61da3d4b412c833662702e233e78c` |
| cache-type-k / cache-type-v | `q8_0` / `q8_0` |
| ctx-size | 4096 |
| parallel | 4 |
| kv-unified | true |
| GPU offload | `-ngl 99`（全部层）|
| host/port | 127.0.0.1 / 8080 |
| API authorization | 启动参数 `--api-key` 注入（值不写入配置/仓库/报告）|
| metrics | `--metrics`（/metrics、/metrics/kv）|
| 日志 | 路径/轮转由运维环境配置（外部 logrotate）|
| request timeout | 300s |
| shutdown timeout | 15s |
| 最大并发/请求大小 | parallel=4 决定并发上限；单请求 prompt 受 ctx 边界（超限 400 拒绝）|
| **checkpoint-reuse** | **false**（生产必须关闭）|
| checkpoint_save / checkpoint_restore | **不包含** |
| clone_from | **不包含**（公共 action 已移除，0 残留）|
| q4_0 | **不得进入生产配置**（NOT_VALIDATED）|

## 2. 启动日志断言（实测，`e14_1_prod_config_assert.py`）

| 断言 | 结果 |
|---|---|
| K/V cache type = q8_0 | 生效（per-cell 验证）|
| KV per-cell = 17408 B | **17408 B** ✓ |
| checkpoint reuse 未启用 | 日志 0 ✓ |
| 无 checkpoint save/restore 日志 | **0** ✓ |
| 无 unsupported hybrid checkpoint 开销路径 | 0 ✓ |
| 模型 SHA256 与配置一致 | E14.0 记录一致 ✓ |

## 3. 采样参数说明

- **`temp=0/seed=42` 仅用于发布回归**（E10/E11 验证协议），**不是业务采样策略**——实际请求采样参数由受控请求配置决定（生产请求可传任意 temperature/seed）

## 4. 复现

```bash
uv run python benchmark/scripts/e14_1_prod_config_assert.py
```
