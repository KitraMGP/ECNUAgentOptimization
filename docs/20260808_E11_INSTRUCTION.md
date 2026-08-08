# E11 指令文件（2026-08-08，浓缩版）

用户于 2026-08-08 启动 E11：Qwen3.5-4B prefix-aligned checkpoint reuse server 集成与最终生产化决策。E10 历史报告不得改写；E10 的 PASS 过强，需用新报告区分 API prototype / server 集成 / validated profile / production-ready。

## 冻结状态（E11 前）
```
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION（E10 的 PASS 被判定过强，回退）
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS（API 层探针）
PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 阶段与交付
- E11.0 E10 状态勘误 → docs/E11_0_E10_STATUS_CORRECTION.md：逐项核对 10 项（E10.4 仅 API 探针无 server 集成；P/X/Y 长度与 raw；save/restore/prefill 精确时间；"26% vs 节省74%"表述；无并发/slot reuse/source 删除/checkpoint eviction/restart 测试；E10.3 每 workload 精确百分比；decode 是否 >3%（精确 1.06-2.10% 全 <3%）；GPU 显存单次 vs 多次 peak；q8_0 仅 temp=0/seed=42；identity 仅代码级）；明确 `E10.4 = API_LEVEL_PROTOTYPE`、`E10.3 = VALIDATED_DEPLOYMENT_PROFILE`；除非 E11.2-E11.5 全过不得写 production-ready
- E11.1 q8_0 profile 最终边界 → docs/E11_1_QWEN35_Q8_VALIDATED_PROFILE.md：F16/q8_0（q4_0 独立观察 NOT_VALIDATED 若无 F16 对照）；workload 9 类（short QA/长代码/长摘要/tool JSON/system retention/canary/多轮/save-restore/容量）+ prompt 256/1024/近 ctx + parallel 1/2/4 + ctx 2048/4096/8192（设备允许）；warmup≥5 + formal≥20 paired；每 run 完整 raw（token ID/hash/JSON parse/schema/失败）；记录 KV capacity/per-cell/GPU reserved-used-peak/prefill-decode-e2e p50-p95/最大并发/failed/OOM/save-restore 成本/canary；验收：不得"全局无损"只能"本矩阵内 token-exact"；decode 退化 >3% 须精确 raw 重判；q8_0 只有显存收益也如实保留
- E11.2 checkpoint 数据结构+所有权设计 → docs/E11_2_CHECKPOINT_OWNERSHIP_AND_IDENTITY_DESIGN.md：结构（id/model identity/adapter/scale/prefix hash/token count/KV type/context/RoPE/tenant/state bytes/generation/last use/refcount/validity）；所有权（source 删除不破坏 target 使用的 checkpoint、restore 失败回滚、target 恢复后独立拥有、多 target 分叉、refcount=0 才释放、shutdown 释放、不跨 model/adapter/KV type）；精确边界（只允许 prompt token 边界创建、P+X 末尾不得当 P、restore 前检查 prefix 一致否则拒绝、不从末尾状态推断 prefix）；默认关闭、仅内部路径、不新增无鉴权 API；模块级+C++ 单测（空/相同/不同 adapter pointer/不同 scale/缺失 identity/不同模型/KV type/prefix/refcount/source 删除/restore 失败回滚）
- E11.3 server 集成最小路径 → docs/E11_3_QWEN35_CHECKPOINT_SERVER_INTEGRATION.md：默认关闭内部路径；范围限制（单模型/trusted single-tenant/单 server/单 source/≤4 target/无 LRU/无跨重启持久化/无分布式/无跨模型/无 recurrent 近似/无 end-position clone）；流程 9 步（请求 A prefill P → P 边界存 checkpoint → B/C/D 同前缀 P → 各 target 恢复 → 追加不同后缀 → 对比 full-prefill baseline → target 写入不改变 checkpoint → source 删除后 target 不损坏 → 全清理后 checkpoint 释放）；同时跑 4B hybrid + TinyLlama 对照；C1 off/checkpoint off/checkpoint on；记录 bytes/save/restore/target 数/prefill/decode/e2e p50-p95/token ID/hash/cells/GPU/失败/refcount/generation/cleanup
- E11.4 server 正确性+生命周期验收 → docs/E11_4_CHECKPOINT_CORRECTNESS_AND_LIFECYCLE.md：真实 4B（非仅探针）；P 116/512/1024/近 ctx × target 1/2/4 × 后缀不同 × source/target 删除/交替 × on/off × save/restore/retry/cleanup/shutdown-restart/capacity pressure/adapter 拒绝/prefix mismatch/KV type mismatch/generation mismatch；门禁（每 target token ID 与独立 baseline 一致、P+X/P+Y 独立验证、roundtrip 状态一致、单 target 失败不污染、refcount 归零、used/shared/active 回基线、无 crash/hang/timeout/OOM、source/target 删除与 slot reuse 不错误恢复）；无真实 cancel API → CANCEL_STATUS: NOT_SUPPORTED（erase/cleanup/shutdown 不得写成 cancel 通过）
- E11.5 端到端收益验收 → docs/E11_5_CHECKPOINT_END_TO_END_PERFORMANCE.md：仅 E11.3/11.4 过后测；warmup 5 + formal 30 paired；on/off × target 1/2/4 × P 116/512/1024/近 ctx × X/Y 不同后缀；记录 full-prefill baseline/save/restore/target prefill/decode/e2e/p50-p95/CPU-GPU memory/checkpoint bytes/capacity/failure rate；收益区分（单 target: save+restore < 重复 prefill？多 target: 保存成本摊薄？长 prefix: 收益随 P 增？资源: checkpoint 常驻内存？物理容量: 最大成功 workload 增？）；不得把 restore 快于 prefill 写成服务整体收益（须纳入 save/索引/调度/checkpoint memory）
- E11.6 prompt-cache identity 最终 → docs/E11_6_PROMPT_CACHE_IDENTITY_FINAL.md：无真 LoRA 时 C++ 结构化测试（空/相同 pointer+scale/同 pointer 不同 scale/不同 pointer 同 scale/不同 pointer 不同 scale/缺失 identity/save-load/cache_ram on-off/C1 on-off/checkpoint on-off）；不依赖 Python 实例化 C++；缺失/不确定拒绝复用；不用 prompt/header 代替 identity；仍无法真实 adapter → LORA_RUNTIME_STATUS: NOT_VERIFIED + PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED（不得隐藏 RUNTIME_NOT_VERIFIED 在 PASS 后）
- E11_FINAL_DECISION.md：情况 A（server 集成无损+生命周期过+性能显著 → PASS_KV_CACHE_OPTIMIZATION + NOVEL: PRODUCTION_CANDIDATE + CHECKPOINT: SERVER_INTEGRATED_BETA）/ 情况 B（仅 API prototype + q8_0 profile 完成 → PASS_OPERATIONAL_KV_OPTIMIZATION + NOT_ACHIEVED + PROTOTYPE_PASS）/ 情况 C（集成失败无错误复用 → PARTIAL + NO_GO_WITH_EVIDENCE ×2）/ 情况 D（错误 restore/污染/越界/refcount 泄漏/raw 不可重现 → REJECT）；不得因 API 探针/单短 prefix/q8_0 temp=0 hash 一致/restore 快于 prefill 直接设生产 PASS

## 第一条工作更新（已完成）
根 06dd71c clean、llama cb39a8126 clean；E10 build 对应 E10.1（clone 移除后未重建——E11 重建）；探针未纳入源码（根忽略）；server 无 checkpoint reuse 代码（仅既有 prompt checkpoint）；无 refcount；q8_0 raw 完整（20 样本/workload）；q8_0 decode 精确退化 max 2.10%（全 <3%）；identity 仅代码级验证。

## 硬性约束
不改写 E6-E10 历史报告；每阶段检查工作树+记录退出码+新 commit；不提交模型/build/完整日志/临时二进制；不做 destructive git；E11 后必须做出"集成/保留 prototype/停止"唯一工程决策，不再启动纯复核。
