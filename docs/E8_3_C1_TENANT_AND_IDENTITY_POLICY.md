# E8.3：C1 tenant 与 cache identity 策略（Tenant & Identity Policy）

- 复核时间：2026-08-08

## 1. tenant identity 审计结论

llama-server 的认证机制：`--api-key`（`common/common.h:636 api_keys`，一个或多个**全局** key）。**不存在 per-request tenant 概念、无 tenant header 校验、无角色/租户分隔**。任何自定义 HTTP header（如 `X-Tenant-Id`）均可由客户端任意伪造 → 不可作为可信 tenant identity。

**结论：server API 无可信、不可由普通 prompt/header 伪造的 tenant identity。**

## 2. C1 支持范围声明

```text
C1_SCOPE: TRUSTED_SINGLE_TENANT_DEPLOYMENT
```

- 同一可信服务（单租户部署，所有请求来自同一信任域）内的跨 session 前缀共享：**允许**（默认）
- 多租户服务中的跨 tenant 共享：**NOT_VERIFIED / 不支持**（无 tenant identity 可检查，无法实现 same_tenant_only 或 cross_tenant_opt_in）
- **不得**从用户文本、system prompt 文本或任意 HTTP header 猜测 tenant 身份（无此实现，且不应实现——不可信）
- 不将"canary 输出无泄漏"等同于"多 tenant 安全"（canary 验证的是 KV correctness 与输出内容隔离，不是 tenant 认证边界）

## 3. 代码修改（声明限制）

1. `common/arg.cpp --kv-prefix-share` 帮助文本：
   "RESTRICTED TO TRUSTED SINGLE-TENANT DEPLOYMENT: no tenant identity is checked (server has no per-request tenant auth); multi-tenant isolation is NOT_VERIFIED. Requires standard attention-only unified KV model (hybrid/recurrent/SWA rejected)."
2. `server-context.cpp` capability accepted 日志追加 tenant scope：
   "tenant scope = TRUSTED_SINGLE_TENANT_DEPLOYMENT (no tenant identity checked; multi-tenant isolation NOT_VERIFIED)"

## 4. 隔离测试（同 tenant 语义下的内容隔离）

| 测试 | 验证 | 状态 |
|---|---|---|
| M08_canary（e7 矩阵）| 相同前缀 + 不同 canary 的请求输出不泄漏对方 canary | PASS（5 reps）|
| test_canary_no_cross_session_leak（audit）| session 特有 canary 不跨 session 泄漏 | PASS |
| cache hit/miss 指标 | `usage.cached_tokens`/`prompt_n` 为数值，不含任何请求内容 | PASS（不暴露内容）|
| 同 tenant 相同前缀共享 | M01-M07（同服务内请求）| PASS |
| 身份不同租户不共享 | **N/A**（无 tenant identity 可实现）| NOT_VERIFIED |

## 5. 结论

- C1 明确限定 **trusted_single_tenant_deployment**；多租户隔离 **NOT_VERIFIED**
- 参数帮助、server 日志、文档三处声明限制
- 内容隔离（canary）已由测试证明；tenant 认证边界诚实标注为不存在
- 不构成项目 PASS 的 tenant 证据（PASS 需在支持范围内即可，此处如实声明范围）
