# E7.5：C2 范围修正报告（Scope Correction）

- 复核时间：2026-08-08
- 复核对象：E6.3 的 "C2 SnapKV 式 prompt KV 压缩离线 oracle"（`benchmark/scripts/e6_c2_oracle.py`）

## 1. 实际能力 vs 声称能力

| 能力 | E6.3 声称 | 实际（代码审计） | 判定 |
|---|---|---|---|
| prompt token 计数 | ✓ | ✓ | 一致 |
| budget keep 数量估算 | ✓ | ✓ | 一致 |
| 理论 attention KV bytes 计算 | ✓ | ✓ | 一致 |
| 真实 attention score / importance score | SnapKV oracle | **无**（脚本不读任何 attention 输出）| **不符** |
| observation window 投票 | SnapKV oracle | **无** | **不符** |
| prefix token 选择 / pooling | SnapKV oracle | **无** | **不符** |
| 压缩 KV 生成 | SnapKV oracle | **无** | **不符** |
| 用压缩 KV 执行 decode | SnapKV oracle | **无** | **不符** |
| 输出质量测量 | SnapKV oracle | **无** | **不符** |

## 2. 修正后状态

```text
C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR
```

- 保留脚本（离线容量估算有参考价值：1024 budget 下 attention KV 理论上界 -68.6%）
- **不代表**在线压缩可用、不代表 hybrid 端到端收益、不包含质量结论、不作为项目 PASS 依据
- 不称 "SnapKV oracle"；SnapKV 归属为"未来方向"（方向 B 需真实 score 通道 + 压缩 KV + decode）

## 3. 已修正文件

- `benchmark/scripts/e6_c2_oracle.py`：docstring 更新为 KV_BUDGET_FEASIBILITY_ESTIMATOR 范围说明
- `benchmark/results/kv_optimization/optimization_hypotheses.json`：C2_SNAPKV_PROMPT_COMPRESSION → C2_KV_BUDGET_ESTIMATOR（status: NOT_IMPLEMENTED_OFFLINE_ESTIMATOR_ONLY）

## 4. 为什么不补成真 oracle（方向 B）

1. **主模型 hybrid 不可行**：recurrent state 无法按 token 压缩（E6.3 已记录），attention-only 压缩对主模型无端到端收益
2. 真 oracle 需 FA 下 attention score 输出通道 + KV 紧凑重排 + decode 评估（中等-大量工程），且成果仅适用于 attention-only
3. 指令方向 B 允许"attention-only future prototype"——标记为未来工作，不在本复核周期实现

## 5. 结论

- C2 从 "SnapKV oracle" 降级为 **KV_BUDGET_FEASIBILITY_ESTIMATOR**（诚实范围）
- 不构成项目 PASS 依据
