# E12.3：Qwen3.5-4B hybrid 正确性矩阵（Correctness Matrix）

- 复核时间：2026-08-08
- 背景：E12.2 判定 server 层 hit-path NO_GO（save 边界 + recurrent pos_min 双重障碍）——**本矩阵验证的是 fallback 路径正确性**（restore 尝试 → 全量 prefill → 输出一致），非 hit 路径（未实现）
- 证据来源：E12.1 诊断（4B 真实运行）+ E11.4 生命周期矩阵 + 本阶段补充

## 1. 矩阵覆盖与结果（Qwen3.5-4B，真实运行）

| 维度 | 覆盖 | 结果 |
|---|---|---|
| P 长度 | 116（E10.4 探针）/ 362（E12.1）/ 440（E11.4 4B 档）| 全部输出与 baseline 一致 |
| target 数 | 1/2/4（E11.4 4B 档）| 全一致 |
| suffix X/Y 不同 | X/Y 分别验证 | P+X、P+Y 独立一致 |
| P 末尾类型 | 无空格（rstrip，前缀一致 → restore 执行 → fallback 全量）/ 空格/换行（BPE → find_prefix 拒绝 → 全量）| 全部安全 fallback，输出一致 |
| checkpoint reuse on/off | 两组 | on = restore 尝试（成功或拒绝）→ 全量 prefill；off = 全量；**输出一致** |
| q8_0 / F16 | F16（主矩阵）；q8_0（E11.1 验证 profile）| 一致 |
| C1 off/on | on 时 hybrid capability 拒绝（E10.0 paired 验证）| C1 不参与（hybrid 拒绝）|

## 2. 每 target 对比（fallback 路径）

| 对比项 | restore 执行（前缀一致）| restore 拒绝（BPE 边界）|
|---|---|---|
| 输出 hash vs 独立 baseline | **一致**（E12.1：restore=2d8dd8... baseline=2d8dd8...）| **一致**（find_prefix 拒绝 → 全量）|
| token ID 序列 | 一致（hash 相同）| 一致 |
| prompt_n | 384（全量——do_reset 后）| 384（全量）|
| 崩溃/hang/OOM | **无**（E12.1/E11.4 server alive）| 无 |
| 跨 target 污染 | 无（各 slot 独立）| 无 |
| refcount | E11.2 C++ 测试（release → 0）| 同 |

## 3. 门禁判定

| 门禁 | 结果 |
|---|---|
| token-exact（fallback 全量 = baseline）| **PASS**（所有场景 hash 一致）|
| 每场景 ≥5 次 | PASS（E11.4 6 场景 + E12.1 诊断多次）|
| prefix mismatch 安全拒绝全量 fallback | **PASS**（BPE 边界场景）|
| restore 失败回滚 | PASS（set_data 失败 → release + clear）|
| refcount 最终 0 | PASS（C++ 测试）|
| 无 crash/hang/OOM/污染 | **PASS**（E12.2 的 skip-do_reset 尝试崩溃已回退——当前默认路径无崩溃）|

## 4. 结论

```text
QWEN35_HYBRID_CORRECTNESS: TRUE（fallback 路径全部 token-exact、无崩溃、无污染）
QWEN35_HYBRID_CACHE_HIT: FALSE（hit-path 未实现——E12.2 NO_GO）
QWEN35_HYBRID_OPTIMIZATION: NOT_ACHIEVED
```
- **正确性成立（安全 fallback）**；**命中/收益未达成**（E12.2 证据：save 边界无法在 P 边界可靠获得 + recurrent pos_min 触发 do_reset + skip 后崩溃）
