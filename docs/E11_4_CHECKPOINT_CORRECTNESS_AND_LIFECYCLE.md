# E11.4：checkpoint 正确性与生命周期验收（Correctness & Lifecycle）

- 复核时间：2026-08-08
- 代码：llama.cpp `8d1b2b1e1`（E11.3 集成后）同一 build；TinyLlama（集成生效路径）+ 4B 记录
- raw：`raw/e11_lifecycle.json`

## 1. 矩阵结果（TinyLlama，前缀稳定 P）

### P 长度 × target 数（同一 checkpoint 分叉）

| 场景 | save | restore prompt_n | baseline 一致性 | kv |
|---|---|---|---|---|
| P~110 × t1 | 200 | 24 | **match=True** | used=649 |
| P~110 × t2 | 200 | 24, 24 | **match=True** | used=1298 |
| P~110 × t4 | 200 | 24, 24, 24, 24 | **match=True** | used=724, active=4 |
| P~440 × t1 | 200 | 24 | **match=True** | — |
| P~440 × t2 | 200 | 24, 24 | **match=True** | used=1298 |
| P~440 × t4 | 200 | 24, 24, 24, 24 | **match=True** | used=2596 |

- **restore 后每 target 只 prefill 后缀（24 tokens），P 部分（110/440）免算**
- **每 target 输出 hash 与独立 full-prefill baseline 完全一致**（P+X/P+Y 独立验证）

### 生命周期

| 场景 | 结果 | 判定 |
|---|---|---|
| source 删除后 target restore | 200、prompt_n=16（checkpoint 仍可用）| **PASS**（refcount 保护：source erase 不破坏 checkpoint）|
| prefix mismatch（不同前缀 restore）| 200、prompt_n=35（**拒绝 → 全量 prefill**）| **PASS**（安全拒绝，无错误复用）|
| restart 后池清空 | prompt_n=166（全量——池已清，restore 拒绝）| **PASS**（无跨重启持久化，符合范围）|
| target 删除/交替/save-restore/cleanup | E11.2 池逻辑 + E9.2 生命周期（复用）| PASS |
| refcount 归零 | E11.2 C++ 测试（release→refcount==0→失效）| PASS |
| crash/hang/timeout | 全程无（server alive）| PASS |

## 2. 4B（hybrid）补充记录

- restore 执行成功（E11.3 已验证）但 cache_prompt 未命中（全量 prefill、输出与 baseline 一致）→ **正确性 PASS（无损）、收益未达成**（recurrent 位置语义待适配）

## 3. 正确性门禁汇总

| 门禁 | 结果 |
|---|---|
| 每 target token ID 与独立 full-prefill baseline 一致 | **PASS**（6 场景 match=True）|
| P+X / P+Y 独立验证 | PASS（不同后缀各验）|
| checkpoint roundtrip 状态一致 | PASS（E11.2 往返测试）|
| 单 target 失败不污染其他 | PASS（池字节拷贝 + 回滚）|
| refcount 最终归零 | PASS（C++ 测试）|
| used/shared/active 回基线 | PASS（kv 无泄漏）|
| 无 crash/hang/timeout/OOM | PASS |
| source/target 删除、slot reuse 无错误恢复 | PASS（source 删除后 restore 生效）|

## 4. 结论

```text
CHECKPOINT_LIFECYCLE_STATUS: PASS（TinyLlama 完整；4B 正确性 PASS、收益未达成）
CANCEL_STATUS: NOT_SUPPORTED（无真实 cancel API——E11.0 保持；erase/cleanup 不得视为 cancel）
```

- 集成正确性（恢复/一致性/回滚/释放/拒绝路径）全部通过
- BPE 前缀边界（E11.3）与 hybrid 位置语义（E11.3/11.4）为两个真实限制，均安全降级
