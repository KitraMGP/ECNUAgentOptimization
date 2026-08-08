# E14.5：运维交接（Operations Handoff）

- 交接时间：2026-08-08
- 服务：Qwen3.5-4B q8_0（validated deployment profile）

## 1. 启动命令（生产）

```bash
llama.cpp/build-cuda/bin/llama-server \
  -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  -ngl 99 --ctx-size 4096 --parallel 4 --kv-unified \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --metrics --api-key <API_KEY> \
  --log-file /var/log/llama-server.log
```

## 2. 停止/重启

```bash
pkill -x llama-server            # 停止（SIGTERM → graceful，15s 超时）
# 重启 = 再次执行启动命令
```

## 3. Endpoint

| Endpoint | 用途 |
|---|---|
| `GET /health` | 健康检查（`{"status":"ok"}`）|
| `POST /completion`、`/v1/chat/completions` | 推理 |
| `GET /metrics` | Prometheus 指标（需 `--metrics`）|
| `GET /metrics/kv` | KV 统计（capacity/used/shared/per-cell）|

## 4. 日志

- 位置：启动参数 `--log-file` 指定（运维环境 `/var/log/llama-server.log`）
- 轮转：外部 logrotate（按大小/天）

## 5. SHA256（校验用）

| 制品 | SHA256 |
|---|---|
| 二进制 `llama.cpp/build-cuda/bin/llama-server` | `74a6b18bf2af8fc55a35cc0fc445f8722faa8c4400c5e2dd3cb8665af5dd6336` |
| 模型 `models/qwen3-5-4B-Q4_K_M.gguf` | `de8e96cd0d0c358487091aaaed1346bc02e61da3d4b412c833662702e233e78c` |
| 生产配置 `benchmark/configs/qwen35_4b_q8_production.yaml` | `d860ca4e959596ceef0cb1a6d40009c366957f6d1ba09fa09d72facd10048ae1` |

## 6. 正常指标范围（q8_0，8GB 卡）

| 指标 | 正常范围 |
|---|---|
| GPU memory | ~2.97 GB（q8_0；F16 ~3.03 GB）|
| KV per-cell | **17408 B**（q8_0）；32768 B（F16）|
| KV capacity（ctx2048/p1）| 35.7 MB（q8_0）|
| decode | ~76-77 tokens/s（Q4 模型单流）|
| p50/p95 e2e（8 tokens）| 631 / 685 ms（dry-run）|

## 7. 告警阈值（建议）

| 指标 | 阈值 |
|---|---|
| 5xx 率 | >1pp 相对基线 |
| p95 | 相对基线退化 >10% |
| GPU peak | >7.5 GB（设备 8GB 安全边界）|
| KV occupancy | >95% capacity 持续 |
| canary 泄漏 / tool schema 失败 | 任何发生即告警 |
| server crash/restart | 任何发生即告警 |

## 8. 常见启动失败处理

| 症状 | 处理 |
|---|---|
| `--api-key` 未设 + CORS * | WRN 日志（安全风险）——生产必须设 `--api-key` |
| `slot-save-path not a directory` | 预创建目录（未启用 slots action 可省略）|
| 显存不足 OOM | 降 `--parallel` 或 `--ctx-size`；检查其他 GPU 进程 |
| 端口占用 | `pkill -x llama-server` 后重试 |
| q8_0 参数未生效（per-cell≠17408）| 检查 `--cache-type-k/v` 拼写；4B 支持 q8_0/q4_0（E9.4 实测）|

## 9. OOM / timeout / JSON 失败处理

- **OOM**：检查 GPU 使用（nvidia-smi）；降并发/ctx；回滚 F16 或降 parallel
- **timeout**：单请求 300s 超时；长生成调大 `n_predict` 或分块
- **JSON 失败**：chat 接口需正确 `tools` 参数（E14.2 验证格式）；模型未调用工具时内容非 JSON（属正常模型行为，非故障）

## 10. q8_0 → F16 回滚步骤

```bash
# 1) 停止 q8_0 实例（排空在途请求后 SIGTERM）
# 2) 移除 --cache-type-k/v 参数启动（F16 默认）
# 3) 验证 /health、/metrics/kv per-cell=32768、canary
# 4) 流量切回
# （同一二进制，仅配置差异——E14.4 演练通过）
```

## 11. 已验证范围

- q8_0：9 类 workload 矩阵 token-exact（temp=0/seed=42/≤16 tokens）；KV -47%；GPU -58MB；decode ≤2.10%；per-cell 17408；15 项发布验收；回滚演练

## 12. 未验证范围（重要）

- **ctx=8192 NOT_VALIDATED**（8GB 卡限制）
- **q4_0 NOT_VALIDATED**（不得进入生产）
- **LoRA runtime NOT_VERIFIED**（资产不可得；identity 仅代码+C++ 单测）
- **checkpoint hybrid NO_GO_WITH_EVIDENCE**（不用于生产）
- **checkpoint attention-only experimental、默认关闭**——**不得要求值班人员启用 `--checkpoint-reuse` 解决生产性能问题**
- 真实生产流量灰度未执行（本机 dry-run 完成）

## 13. 值班提示

- 生产配置固定为 q8_0；**不要**为性能问题启用 `--checkpoint-reuse`（主模型上 NO_GO，纯开销）
- 业务采样参数（temperature/seed）由受控请求配置决定（temp=0/seed=42 仅发布回归用）
