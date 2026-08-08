# E9 指令文件（2026-08-08，浓缩版）

用户于 2026-08-08 启动 E9：C1 验收关闭与 Qwen3.5-4B 主模型优化转向。

## 冻结状态（不得提升，除非新真实代码+同 commit raw+完整验收）
```
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
C1_ATTENTION_ONLY_STATUS: C1_CONDITIONAL_PASS_RECOMPUTE_ONLY
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR
```

## 阶段与交付
- E9.0 仓库/E8 证据冻结 → docs/E9_0_E8_CLOSURE_BASELINE.md（核对：E8.4 4B 是否 strict paired raw；E8.5 capacity_bytes=524288 是否来自 /metrics/kv；shared/used/physical_sharing 语义；47/47 是否含 skip（passed/skipped 分开）；LoRA 3 例不得计 passed）
- E9.1 修复 server prompt-cache LoRA identity（cache_prompt/RAM cache/checkpoint/save-load/are_lora_equal）→ docs/E9_1_PROMPT_CACHE_IDENTITY_AUDIT.md。审计 6 问（不同 LoRA/不同 scale/无vs有 LoRA/restore 后 identity/off 时/on+ram 双路径）；风险成立则修复：cache entry 存结构化 adapter identity、lookup 比 ptr+scale、缺失拒绝、不用 prompt/header 替代；测试 no-LoRA→LoRA、A→B、scale、同 LoRA+scale、on/off、ram on/off、save/restore、hash+canary、资源回收；缺真 LoRA 时 mock/unit + `RUNTIME_NOT_VERIFIED`
- E9.2 E8 后 endurance 复验（E8 最新代码同一 build；不得引用 E6.5）：12 周期 + 30 周期 churn + 多 target/source 删/target 删/交替/cancel/retry/save-load/generation/capacity pressure/shutdown-restart/on-off/ram on-off；每周期记录 used/shared/active/source-target/gen/completed-failed/hash/canary/purge/timeout/exit；要求 drift=0、erase 回基线、无 active victim、无污染、无 identity 错误、无 crash → docs/E9_2_C1_POST_HARDENING_ENDURANCE_REPORT.md
- E9.3 C1 线性扫描扩展性 + 真实容量边界 → docs/E9_3_C1_SCALING_AND_CAPACITY_BOUNDARY.md。矩阵：idle source 0/1/2/4/8/16/32/64、no-match/first/last match、partial、全长前缀、lora 跳过、source 保护、off 对照；记录 scan elapsed/LCP 比较数/queue/prefill/decode/e2e p50/p95/CPU/recompute/hash；区分扫描 CPU 开销 vs prefill 收益 vs no-match 开销。容量：固定 ctx+prompt、逐步加并发、找 baseline 最大成功数与 C1 最大成功数、边界重复≥3、记录 failed/rejected/OOM、无提升写 NO_CAPACITY_GAIN
- E9.4 架构运行时验证（LoRA/SWA/recurrent）：优先 llama.cpp 官方小模型；记录来源/SHA256/许可/架构；下载失败记错误；不提交模型；无法获得 → LORA/SWA/RECURRENT_RUNTIME_STATUS: NOT_VERIFIED
- E9.5 主模型内存组成 → docs/E9_4_QWEN35_MEMORY_BREAKDOWN.md：Qwen3.5-4B 权重/attention KV/recurrent state/graph/CUDA reserved/used/每 slot 增量/prompt 增量/recurrent 是否与长度无关/parallel 1/2/4/ctx 1024-8192/KV type F16/Q8/Q4/prefill-decode tps/OOM 边界；用实际指标非公式
- E9.6 候选选择 → docs/E9_5_QWEN35_CANDIDATE_SELECTION.md：
  - H1 hybrid attention-side KV type/precision（现有 -ctk/-ctv 只能 baseline，不能冒充创新）
  - H2 hybrid prefix checkpoint restore（A 末尾存 attn+recr，从 checkpoint 恢复 target；单 source/single target 最小 prototype；量化 bytes/保存/恢复时间）
  - H3 recurrent state 精度/布局优化（先确认 type/生命周期/数值敏感性；禁止丢弃/近似）
  - H4 hybrid slot clone/fork 或 checkpoint reuse（对比上游 clone_to；禁止 attention-only seq_cp 用于 recurrent）
  - score = 0.25 gain + 0.25 correctness + 0.20 compatibility + 0.15 feasibility + 0.15 engineering_cost；SELECT/HOLD/REJECT + 瓶颈/代码位置/不变量/预期收益/最小 prototype/失败条件
- E9.7 实现一个主模型最小 prototype → docs/E9_6_QWEN35_PROTOTYPE_REPORT.md：真实 4B 路径、baseline/candidate 开关、默认 off、无损（除非标有损+质量门禁）、真实 bytes/prefill/decode/e2e/p95、cancel/retry/cleanup、rollback 条件；不满足安全实现 → `QWEN35_OPTIMIZATION_PROTOTYPE_STATUS: NO_GO_WITH_EVIDENCE`（须含≥2 候选真实测量或代码级 feasibility，不得只引用 E8.7）
- 文献最后修正 → docs/E9_7_LITERATURE_FINAL_CORRECTION.md：LM-Infinite 正式发表状态（ACL Anthology/proceedings/DOI 优先，非仅 OpenReview）；确认则修正 manifest/bib/综述并说明 E8 漏检原因；无法确认保留 VENUE_UNVERIFIED + 记录查找来源
- docs/E9_FINAL_REPORT.md

## 最终状态规则
- 仅主模型 4B 真实收益+完整门禁 → PASS_KV_CACHE_OPTIMIZATION
- C1 完整关闭但主模型无收益 → PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION（默认）
- C1_ATTENTION_ONLY_PASS 需：最新代码 endurance 重跑过、prompt cache identity 关闭或隔离、no-match 扫描开销门禁过、p95 门禁过、实际容量边界完成、支持范围文档完整、已获模型 runtime identity 测试过
- 错误缓存复用/adapter 污染/recurrent 污染/泄漏/raw 不可重现 → REJECT
- 外部资产/网络阻塞才 HOLD

## 最终回复格式
PROJECT_STATUS / C1_ATTENTION_ONLY_STATUS / QWEN3.5_4B_STATUS / QWEN35_OPTIMIZATION_PROTOTYPE_STATUS / C2_STATUS / LORA_RUNTIME_STATUS / SWA_RUNTIME_STATUS / RECURRENT_RUNTIME_STATUS / PROMPT_CACHE_IDENTITY_STATUS + 12 项报告

## 硬性约束
不等待用户确认范围变化；Qwen3.5-4B 仍是主模型；不用 TinyLlama 收益恢复 PASS；保留 E6-E8 历史报告不改写。
