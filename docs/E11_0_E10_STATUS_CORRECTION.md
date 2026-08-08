# E11.0：E10 状态勘误与冻结（Status Correction）

- 复核时间：2026-08-08
- 目的：逐项核对 E10 结论，明确各证据的层级；E10 历史报告不改写

## 1. 逐项核对

| # | 核对项 | 结果 |
|---|---|---|
| 1 | E10.4 是否只有 API 探针，无 server 集成 | **是**：`llama.cpp/tmp/e10_checkpoint_probe.cpp`（独立 C++ 文件，根仓库忽略未入库）；server 侧无任何 checkpoint reuse 代码（仅既有 prompt checkpoint 机制）→ **API_LEVEL_PROTOTYPE** |
| 2 | checkpoint 测试真实 P/X/Y 长度、target 数、raw | P=116 tokens、X/Y=14 tokens、target=2（同一 checkpoint 分叉）；**raw 仅为探针 stderr 输出，无结构化 JSON**（缺口，E11.3/11.4 补结构化 raw）|
| 3 | save/restore/prefill 精确时间 | save（get_data）=41.99ms、restore（set_data）=7.86ms、prefill(P)=29.9ms（E10.4 报告值，来自探针计时）|
| 4 | "restore 26%" vs "节省 74%" | restore/prefill = 7.86/29.9 = **26.3%**；"节省 74%" 为 1-26.3%——**但未计入 save 成本（41.99ms，单 target 时 save+restore=49.9ms > prefill 29.9ms；多 target 才摊薄）**→ E11.5 必须纳入 save/索引成本，E10.4 的"显著收益"表述仅对多 target/长 P 成立 |
| 5 | 并发/slot reuse/source 删除/checkpoint eviction/restart 测试 | **均无**（探针为单 ctx 单 seq 顺序执行）→ E11.4 补 |
| 6 | E10.3 每 workload 精确百分比 | decode 退化（p50 精确）：short_qa **2.10%**、long_code 1.28%、long_summary 1.45%、tool_json 1.06%、system_retention 1.79%、canary 1.63% |
| 7 | 是否有 decode >3% | **无**（精确 max 2.10%；E10.3 报告写 "+1.5~3.3%" 中 3.3% 不精确——精确数据无超 3% 项）|
| 8 | GPU 显存单次 vs 多次 peak | **单次观测**（每 workload server 启动时 gpu_baseline/after 各 1 次 nvidia-smi）非多次 peak measurement → E11.1 补多次 |
| 9 | q8_0 输出一致性覆盖 | 仅 **temp=0/seed=42**（单采样配置）→ E11.1 保持该范围并明确标注（不声称其他采样配置）|
| 10 | prompt-cache LoRA identity | **仅代码级验证**（alloc/load 门禁逻辑 + 空-空回归）；无 C++ 结构化单测 → E11.6 补 |

## 2. 明确层级

```text
E10.4 = API_LEVEL_PROTOTYPE（llama API 探针，真实模型路径验证无损；无 server 集成）
E10.3 = VALIDATED_DEPLOYMENT_PROFILE（q8_0：本矩阵内 token-exact、decode 退化 ≤2.10%、
        KV -47%、GPU -30MB；temp=0/seed=42 范围）
```

**除非 E11.2-E11.5 全部通过，不得写成 production-ready。**

## 3. E11 起点状态（修正 E10 的过强 PASS）

```text
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION（回退；E10 的 PASS_KV_CACHE_OPTIMIZATION 因
  仅 API 层 prototype + 单次观测 + 未纳入 save/索引成本而判定过强，E11 重新验收）
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION（C1 语义保持）
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS（API 层）
PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 4. E11 关键缺口清单（关闭项）

1. checkpoint server 集成（E11.2/11.3）+ refcount/ownership
2. 结构化 raw（探针无 raw JSON）
3. 生命周期矩阵（并发/slot reuse/source 删除/restart/eviction）
4. GPU 多次 peak 观测
5. identity C++ 结构化测试
6. 收益验收纳入 save/索引/checkpoint memory 成本
