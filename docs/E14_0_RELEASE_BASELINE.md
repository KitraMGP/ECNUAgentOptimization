# E14.0：冻结最终发布基线（Release Baseline）

- 发布基线时间：2026-08-08

## 1. 仓库与源码 commit

| 项 | 值 |
|---|---|
| 根仓库最终 HEAD | `1f2493a`（benchmark-enhance，clean）|
| llama.cpp 最终 HEAD | `afbf375c6`（llama.cpp，clean）|
| **最终发布源码 commit** | llama.cpp **`afbf375c6`**（E13.2 hybrid 保护后；E14 无源码修改）|
| 差异处理 | E13.1 validated build `f54930492` vs E13 最终 `afbf375c`：E14 基于 **afbf375c** 重建并重跑回归（通过）|

## 2. 构建环境

| 项 | 值 |
|---|---|
| 编译器 | gcc 16.1.1（2026-07-28）|
| CUDA | 13.3（nvcc V13.3.73）|
| 驱动 | 610.43.03 |
| GPU | NVIDIA GeForce RTX 4060 Laptop（8GB）|
| 构建命令 | `cmake -B build-cuda -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89 && cmake --build build-cuda -j $(nproc)`（目标 llama-server）|

## 3. 制品 SHA256

| 制品 | SHA256 |
|---|---|
| 最终二进制 `llama.cpp/build-cuda/bin/llama-server` | `74a6b18bf2af8fc55a35cc0fc445f8722faa8c4400c5e2dd3cb8665af5dd6336`（ninja 重建后稳定）|
| 模型 `models/qwen3-5-4B-Q4_K_M.gguf` | `de8e96cd0d0c358487091aaaed1346bc02e61da3d4b412c833662702e233e78c`（完整）|
| q8_0 配置 `benchmark/configs/qwen35_4b_q8_validated.yaml` | `d860ca4e959596ceef0cb1a6d40009c366957f6d1ba09fa09d72facd10048ae1` |

## 4. 最终 build 回归（E13.1 轻量 q8_0 重跑，基于 afbf375c）

| 检查 | 结果 |
|---|---|
| server 启动 | OK |
| KV per-cell | **17408 B**（q8_0 生效）|
| canary 请求 | OK（输出正常）|

**RELEASE_STATUS: 构建通过（非 BLOCKED）**——E14.2 完整发布前验收继续。

## 5. 测试退出码（E13.3 记录，E14 基线沿用）

llama.cpp 50 passed + 3 skipped、E1 5/5、根 177、C++ identity 18 断言——全部 0。

## 6. 发布日期与制品目录

- 日期：2026-08-08
- 制品目录：`llama.cpp/build-cuda/bin/`（二进制）、`models/`（模型）、`benchmark/configs/`（配置）、`docs/E14_*`（发布文档）、`benchmark/results/kv_optimization/raw/e14_*`（发布 raw）
