# E10.4：prefix-aligned hybrid checkpoint 最小原型（Checkpoint Prototype）

- 复核时间：2026-08-08
- 严格场景：**保存 P 末尾完整 attention + recurrent state → 恢复到 target → 分别执行 P+X 和 P+Y**；禁止从 P+X 末尾恢复 P+Y（E9.6 已证 end-position clone 错误）
- 实现：llama API 探针（`llama.cpp/tmp/e10_checkpoint_probe.cpp`，链接真实 libllama，运行在真实 Qwen3.5-4B 路径）
- 探针调试记录：batch logits 数组未初始化（llama_batch_init 的 logits 是未初始化 malloc）导致 output_ids 错乱——已修复（memset + 手动 pos）；该问题为探针自身 bug，非机制限制

## 1. 验证流程（4B，P=116 tokens）

```
1. baseline：全量 prefill P+X → decode 8 tokens（greedy）
2. prefill 仅 P → get_data(seq0) 保存 P 末尾完整状态（attn + recurrent）
3. seq_rm 清空 → set_data 恢复到 target seq
4. 追加 X（pos 从 P.size() 起）→ decode 8 tokens
5. 同一 checkpoint buffer 再次恢复到第二个 target → 追加 Y → decode
6. 对比：restore 路径 vs baseline 的 token ID
```

## 2. 结果（Qwen3.5-4B hybrid）

| 项 | 值 |
|---|---|
| checkpoint bytes（P=116 tokens）| **59,741,176 B（59.7MB）**（recurrent state 大头 + attention 前缀）|
| save（get_data）| 41.99 ms |
| **restore（set_data）** | **7.86 ms**（59.7MB @ 7600 MB/s）|
| prefill(P)（重复计算的替代成本）| 29.9 ms |
| restore 成本 / 重复 prefill | **26%**（< 80% 停止阈值）|
| baseline P+X tokens | `271 248068 198 90700 8340 25 271 16` |
| restore P->X tokens | **完全一致** |
| restore P->Y tokens（同一 checkpoint 分叉）| **与 baseline P+Y 完全一致** |
| VERDICT | **PREFIX_ALIGNED_CHECKPOINT_LOSSLESS** |

**同一 checkpoint 分叉到 2 个 target（P->X、P->Y）均无损。**

## 3. 严格停止条件检查

| 停止条件 | 结果 |
|---|---|
| 任一 token mismatch | **未触发**（P+X/P+Y 全 MATCH）|
| checkpoint 不能精确对齐 P | **未触发**（roundtrip set->get 逐字节一致 + 输出一致）|
| restore 成本 ≥80% 重复 prefill | **未触发**（26%）|
| 状态不能安全跨 target 分叉 | **未触发**（同一 checkpoint → 2 target 无损）|
| 生命周期/所有权不明确 | 探针层明确（set_data 到目标 seq 可重复）；server 层多 slot 所有权为生产化工程（E10.4 不扩展，如实说明）|

**全部未触发 → 非 NO_GO。**

## 4. 收益量化（显著）

- 长 P 场景（agent 多轮/分支）：restore（~8ms + attention 部分随 P 增长）vs 重复 prefill（P 越大越贵：116 tokens=30ms、1000 tokens≈300ms）→ **5-10× prefill 节省潜力**
- 与 E9.6 end-position clone 对比：**prefix-aligned 是关键**（P 末尾状态 vs X 末尾状态）；本验证证明 P 末尾 checkpoint 可精确保存/恢复/分叉

## 5. 结论

```text
QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS（无损 + 显著收益，llama API 层原型）
```

- 真实 Qwen3.5-4B（hybrid）路径验证；attention-only（TinyLlama）同样通过
- 探针为最小原型（未扩展索引/LRU/并发调度/生产系统——按指令边界）
- server 集成（slot 所有权、checkpoint 存储管理）为后续生产化工程，本阶段不承诺

## 6. 复现

```bash
# 探针源码 llama.cpp/tmp/e10_checkpoint_probe.cpp（git 忽略目录）
# 编译（链接 build-cuda/bin/libllama-server-impl + libllama.so.0）
# 运行（4B 示例见上）
```
