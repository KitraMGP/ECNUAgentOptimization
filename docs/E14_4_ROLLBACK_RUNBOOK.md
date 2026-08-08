# E14.4：回滚演练（Rollback Drill）

- 复核时间：2026-08-08
- 类型：非生产 dry-run（无真实生产流量）
- raw：`raw/e14_rollback_drill.json`

## 1. 演练步骤与结果

| 步骤 | 结果 |
|---|---|
| 1. q8_0 实例启动 + 健康 | True（per-cell 17408）|
| 2. 停止 q8_0（SIGTERM 排空）| **exit 0** |
| 3. 启动 F16 fallback（无 --cache-type-k/v）| True（**per-cell 32768**）|
| 4. health/canary 验证 | F16 健康、canary 无泄漏 |
| 5. 流量切回（模拟）| rollback_ok=True |
| 6. 命令退出码 | [0, 0] |

## 2. 要求核对

| 要求 | 结果 |
|---|---|
| 回滚不依赖重新编译 | **是**（仅配置参数差异：--cache-type-k/v）|
| q8_0 与 F16 profile 可通过配置切换 | **是**（同一二进制）|
| 不删除历史发布制品 | 是（E6-E14 制品保留）|
| 无 destructive git | 是 |
| 恢复时间 | q8_0 停止 → F16 健康 ≈ 30s（模型加载）|
| 命令退出码 | 全 0 |

## 3. 说明

- 输出 hash 差异（q8_0 `e969b733` vs F16 `000465e3`）：不同实例 + 不同请求历史（canary prompt 非 E11.1 paired workload）；**同输入同配置的 q8_0/F16 一致性已由 E11.1 paired 验证**（token-exact）
- F16 fallback profile = 默认 KV type（无 --cache-type 参数）

## 4. 结论

**ROLLBACK_DRILL: PASS**（q8_0 ↔ F16 配置切换可回滚，健康/输出/退出码全通过）
