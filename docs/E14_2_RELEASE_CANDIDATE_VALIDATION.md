# E14.2：发布前验收（Release Candidate Validation）

- 复核时间：2026-08-08
- 二进制：llama.cpp `afbf375c6`（build-cuda，SHA256 `74a6b18b...`）；模型 `de8e96cd...`
- raw：`raw/e14_release_candidate_validation.json`

## 1. 15 项验收结果

| # | 检查项 | 结果 |
|---|---|---|
| 1 | 启动 + 健康检查 | **True** |
| 2+3 | q8_0 参数生效 / KV per-cell | **17408 B**（q8_0）|
| 4 | short QA | 200 |
| 5 | tool JSON（chat/completions + tools）| **parse True、tool_calls True**（参数 JSON 解析通过）|
| 6 | system retention | 200 |
| 7 | canary | **无泄漏** |
| 8 | multi-turn（2 轮）| [200, 200] |
| 9 | long ~1000 tokens | 200 |
| 10 | parallel=4 并发 | [200, 200, 200, 200] |
| 11 | graceful shutdown（SIGTERM）| **exit 0** |
| 12 | 异常请求（空 prompt / 超长）| 空=200（受控）；超长 20000 字符 → **2500 tokens < slot ctx，合法接受**（未超边界）|
| 13 | checkpoint off 时 save/restore 日志 | **0** |
| 14 | clone_from 路由 | **501**（明确拒绝/不存在）|
| 15 | 重启后健康恢复 | **True** |

## 2. 发布门禁判定

| 门禁 | 结果 |
|---|---|
| 0 crash/hang/OOM | **PASS**（全程无）|
| 健康检查全部通过 | **PASS** |
| tool JSON schema 全部通过 | **PASS**（chat 接口真实工具调用 + JSON parse）|
| canary 无泄漏 | **PASS** |
| q8_0 参数和 per-cell 符合预期 | **PASS**（17408 B）|
| 默认路径无 checkpoint 操作 | **PASS**（日志 0）|
| 输出与 E13 回归基线一致 | **PASS**（short QA hash 与 E12/E13 baseline 一致）|
| 测试退出码全部 0 | **PASS**（llama 50+3、E1 5/5、根 177、C++ 18）|

## 3. 说明

- tool JSON 验收使用 `/v1/chat/completions + tools`（OpenAI 兼容，真实触发工具调用）；completion endpoint 的 tool 文本一致性已在 E11.1 验证（q8 vs f16 hash 一致）
- 超长请求在 slot ctx 内合法接受（2500 tokens < 4096）；超过 slot ctx 的请求由 server 返回 400（E4.2 已验证的 ctx-limit 路径）

## 4. 结论

**RELEASE_CANDIDATE_VALIDATION: PASS**——发布候选通过全部 15 项验收。
