# E12.4：Qwen3.5-4B hybrid 端到端收益（End-to-End Result）

- 复核时间：2026-08-08
- 前置：E12.3 正确性 PASS（fallback 路径）；**hit-path 未实现（E12.2 NO_GO）** → 本报告按指令判定"若只恢复但不减少 prefill，判定无优化收益"

## 1. 收益判定

| 门槛 | 结果 |
|---|---|
| P=116 单 target 不要求收益但不得错误 | **无错误**（E12.1：200/200、输出一致）|
| P≥512 或 target≥2 含 save/restore 总成本相对 full prefill ≥20% 降低 | **未达成**——restore 后仍全量 prefill（do_reset），prompt_n=384=全量；save/restore 为**额外成本**（64.66MB state 保存+恢复）|
| 至少一个真实场景 ≥30% e2e 降低 | **未达成**（无命中 → on ≈ off + save/restore 开销）|
| p95 不增 >10% | 无命中 → on 不慢于 off + 少量开销（未实测 p95，命中未发生）|
| fallback 不慢于 off >5% | **需确认**：restore 尝试（find_prefix + set_data 64.66MB ≈ 8ms）→ 全量 → **on 比 off 多 save+restore 开销**（TinyLlama 上 on e2e 含 save 已体现；4B 上 save 64.66MB ~42ms + restore ~8ms 为纯开销）|
| checkpoint host memory 明确报告 | **64.66MB/checkpoint**（4B P~362；recurrent state 大头）——**不声称容量提升**（state 存 host，KV 池不变）|
| 若只恢复但不减少 prefill → 无优化收益 | **成立**：restore 后 do_reset 全量 prefill → **无 prefill 减少 → 无优化收益** |

## 2. 关键数据（E12.1/12.2）

- restore 执行成功（set_data 64,659,376 bytes）→ 但 cache_prompt do_reset → **prompt_n=384（= 全量 prefill）**——**prefill 未减少**
- skip-do_reset 修复尝试 → 崩溃（save 边界错位：state 位置 364 vs prefix 362）
- save 边界修复（post_decode）→ 条件未触发（server prefill 完成检测时机不匹配）
- **net**：on 路径 = save（~42ms）+ restore（~8ms）+ 全量 prefill（= off）→ **on 比 off 更慢（+save/restore 开销）**

## 3. 结论

```text
QWEN35_HYBRID_E2E: NO_OPTIMIZATION_GAIN（hit-path 未实现；restore 未减少 prefill）
CHECKPOINT_HOST_MEMORY: 64.66MB/checkpoint（4B P~362；明确报告，不声称容量提升）
```

- **收益未达成**（E12.2 证据）；**正确性保持**（fallback 全量输出一致）
- 保留的可行路径：E10.4 llama API 探针（真实路径无损验证——但非 server 集成）；q8_0 profile（PASS_VALIDATED）
