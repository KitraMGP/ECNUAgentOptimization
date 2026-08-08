# E8.4：真实架构与 identity 运行时验证（Runtime Architecture Validation）

- 复核时间：2026-08-08

## 1. 资产盘点与验证矩阵

| 架构 | 可用模型 | 模型 SHA256 | 验证结果 |
|---|---|---|---|
| 标准 attention-only | tinyllama stories260K | （缓存，479896ec...）| **VERIFIED**：共享启用（E7.4 矩阵 + E8.2 测试）|
| attention MOE | stories15M_MOE | （缓存，b6dd7374...）| **VERIFIED**：capability accepted + 共享（E8.2 test_moe_attention_share）|
| hybrid（recurrent+attention）| Qwen3.5-4B | `de8e96cd0d0c3584...` | **VERIFIED**：capability rejected + 共享 0 + 独立运行（本节）|
| LoRA（adapter）| **缺失**（moe_shakespeare15M.gguf）| — | **NOT_VERIFIED**（网络不可达，见 §3）|
| SWA/iswa | **缺失**（Qwen3-SWA 等）| — | **NOT_VERIFIED**（无模型）|
| pure recurrent | **缺失**（Mamba）| — | **NOT_VERIFIED**（无模型）|

## 2. hybrid（Qwen3.5-4B）运行时验证（GPU，E8.2 修复后）

命令：`build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf -ngl 99 --ctx-size 2048 --parallel 2 --kv-unified --cache-ram 0 --kv-prefix-share --kv-prefix-share-min-lcp 4 --metrics`

| 检查项 | 结果 |
|---|---|
| capability 日志 | `E8-C1: capability rejected: memory implementation does not support cross-slot prefix metadata sharing`（llama_memory_hybrid 默认 false）|
| 共享日志（E6-C1: shared）| **0** |
| /metrics/kv | shared_cells=0、active_sequences=2（两 session 独立）、used_cells=518（正常 prefill）|
| 请求输出 | 正常生成（8 tokens），无错误、无 500 |
| 模型 SHA256 | de8e96cd0d0c3584...（与 E6 记录一致）|
| 配置 | ctx=2048、parallel=2、unified、cache-ram=0、kv-prefix-share on |

**结论：Qwen3.5-4B 上 C1 由正向能力判断正确拒绝，无错误共享、无 recurrent state 污染、无错误资源状态 → `QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION`（保持）**

## 3. LoRA 运行时验证：NOT_VERIFIED（资产缺失，如实记录）

**缺少的具体资产**：`moe_shakespeare15M.gguf`（ggml-org/stories15M_MOE 的 Shakespeare LoRA adapter，llama.cpp 官方测试 `test_lora.py` 使用的最小合法 adapter）。

**尝试过的来源**：
1. `https://huggingface.co/ggml-org/stories15M_MOE/resolve/main/moe_shakespeare15M.gguf` —— 原 URL 连接超时（网络不可达）
2. `https://hf-mirror.com/...` 镜像 —— 连接超时
3. pytest `download_file()`（HF_ENDPOINT=hf-mirror）—— URLError

**状态**：测试 `test_kv_prefix_share_lora.py`（3 例：不同 lora 不共享/不同 scale 不共享/相同 lora+scale 共享）**已写好并标记 skipif（网络不可达时 skip）**，当前 3 skipped。有模型资产即可运行。代码级隔离（`are_lora_equal` ptr+scale 检查 + `E8-C1: skip source slot %d: lora identity mismatch` 日志）已实现并经单元路径验证（E8.2）。

## 4. SWA / pure recurrent：NOT_VERIFIED（无模型资产）

- 无 SWA/iswa 模型（Qwen3-SWA 系列不在本机，需下载，网络不可达）
- 无 recurrent 模型（Mamba/RWKV GGUF）
- 代码级：`llama_model_n_swa(model) > 0` / `llama_model_is_recurrent(model)` 条件 + capability 默认 false 路径已实现；E8.2 报告记录

## 5. 结论

- **VERIFIED**：标准 attention-only（含 MOE）启用共享、hybrid 正确拒绝
- **NOT_VERIFIED**：LoRA（资产缺失，网络不可达，测试就绪）、SWA、recurrent（无模型）——**不因代码静态判断升级项目状态**
- 4B 主模型状态保持 `CORRECT_NO_OPTIMIZATION`
