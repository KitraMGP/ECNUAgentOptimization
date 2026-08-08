# E13.3：默认路径回归报告（Default Path Regression）

- 复核时间：2026-08-08
- 目的：确认 checkpoint 相关代码不改变默认路径；全量回归退出码 0
- raw：`raw/e13_default_path_regression.json`

## 1. 回归结果

| 检查项 | 结果 |
|---|---|
| 默认配置不启用 checkpoint reuse | **是**（--checkpoint-reuse 默认 off）|
| Qwen3.5-4B 默认请求输出与 E12 baseline 一致 | **是**（hash `126357be2c69be7d...` 与 E12 independent baseline 一致）|
| checkpoint off 无 save/restore | **是**（E11-CKPT 日志 0）|
| checkpoint 代码不改变普通请求（显存/prompt_n/输出）| **是**（prompt_n=203 正常、hash 匹配、无额外日志）|
| clone_from 公共 HTTP action 残留 | **0**（grep 全文件 :0）|
| q8_0 profile 正常启动 | **是**（E13.1 回归：server OK、per-cell 17408、canary 输出）|
| C1 hybrid capability 仍拒绝 | **是**（E10.0 paired 验证 + E12 复验）|
| TinyLlama C1 既有测试不回归 | **是**（llama.cpp 50 passed 含 C1 全套）|
| identity C++ 单测 | **通过**（ALL CHECKPOINT IDENTITY TESTS PASSED）|

## 2. 测试退出码（全部 0）

| 套件 | 结果 | 退出码 |
|---|---|---|
| llama.cpp server 单测（8 文件）| 50 passed + 3 skipped（LoRA 资产缺失 skip）| 0 |
| E1 手动（run_e1_manual.py）| 5/5 | 0 |
| 根仓库 pytest | 177 passed | 0 |
| checkpoint identity C++ | 18 断言全过 | 0 |
| q8_0 轻量回归（E13.1）| server/per-cell/canary 全 OK | 0 |

## 3. 结论

- **默认路径零回归**：checkpoint 代码（默认关）不影响普通请求；hybrid 保护（E13.2）防止无收益开销
- 全部测试退出码 0；工作树将收口（E13.5）
