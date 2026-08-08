# E10 指令文件（2026-08-08，浓缩版）

用户于 2026-08-08 启动 E10：Qwen3.5-4B 主模型优化定案与生产化验收。不得继续用 TinyLlama 恢复 PASS、不得再大范围文献综述。

## 冻结状态（E10 前）
```
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
C1_ATTENTION_ONLY_STATUS: PROVISIONAL_C1_ATTENTION_ONLY_PASS（E9 的 PASS 为 provisional，E10.0/10.1 完成前不得确认）
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_Q8_KV_STATUS: CANDIDATE_SINGLE_CASE_VERIFIED
QWEN35_CHECKPOINT_REUSE_STATUS: NOT_IMPLEMENTED
PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE_RUNTIME_NOT_VERIFIED
```

## 阶段与交付
- E10.0 E9 勘误与证据关闭 → docs/E10_0_E9_ERRATA_AND_CLOSURE.md：
  - 修正 E9.4 "KV type 不可用 vs q8_0 可用"矛盾（参数名笔误已修，但 E9.4 报告残留需勘误）
  - 4B strict C1 off/on paired raw 仍未完成 → 同一 commit/二进制/模型/配置补测（capability rejected、shared=0、输出 hash、timings、资源状态一致）
  - cleanup 近似 ≠ cancel 测试（E9.2 的 cancel 场景修正）
  - LoRA runtime skipped ≠ runtime identity 通过
  - H4 NO_GO 仅限 end-position clone，不适用于 prefix-aligned checkpoint restore
  - C1_ATTENTION_ONLY_PASS 标 provisional
- E10.1 C1 最后验收缺口 → docs/E10_1_C1_FINAL_ACCEPTANCE.md：
  - 直接 source-scan elapsed 和 token-comparison 计数（不靠 e2e 差值推断）——需要代码改动（扫描计时/计数日志或统计）
  - N=0/1/4/16/32/64 的 no-match/first-match/last-match
  - 实际 cancel；server 无 cancel API → 标 NOT_SUPPORTED（不得用 erase 代替）
  - save/restore 后输出 hash 与独立 baseline 比较
  - prompt cache identity 结构化单元测试（直接构造空/相同/不同 adapter/不同 scale）
  - 审计 adapter pointer 生命周期；若可卸载/重载改用稳定 identity
  - 重新运行 30-cycle endurance
  - 全部完成才可确认 C1_ATTENTION_ONLY_PASS；LoRA 仍无 → LORA_RUNTIME_STATUS: NOT_VERIFIED
- E10.2 clone_from API 决策 → docs/E10_2_CLONE_API_DECISION.md：审计授权/source+target ownership/busy slot/并发/cancel/restore/错误返回/跨请求污染；三选一：
  - REMOVE_EXPERIMENTAL_CLONE_API（唯一用途与 C1 重复且 hybrid 默认拒绝）
  - RETAIN_ATTENTION_ONLY_EXPERIMENTAL_API（默认关、可信单租户、显式开关、完整测试）
  - INTERNALIZE_FOR_CHECKPOINT_PROTOTYPE（移除公共 HTTP action，仅保留内部实验路径）
  不得保留默认可访问且无明确产品用途的跨 slot 状态操作 API
- E10.3 q8_0 生产化验收 → docs/E10_3_QWEN35_Q8_KV_VALIDATION.md：F16 vs q8_0（+q4_0 稳定时），warmup≥5 + formal≥20 paired；覆盖短问答/长代码/长摘要/tool JSON/system retention/canary × prompt 256/1024/近 ctx 上限 × parallel 1/2/4 × ctx 2048/4096/8192 × 多轮+slot save/restore；记录 KV capacity bytes、GPU peak、最大成功并发、prefill/decode/e2e p50/p95、failed/rejected/OOM、token hash、JSON parse、canary、质量；q8_0 相对 F16 的实际总 GPU memory（不只 per-cell）；q8_0 不预设无损；状态：PASS_DEPLOYABLE / PASS_WITH_OUTPUT_VARIATION / HOLD_NO_CAPACITY_OR_PERFORMANCE_GAIN / REJECT_QUALITY_OR_STABILITY；通过则新增可复现运行配置+自动化回归（不改上游功能，标注 "validated deployment profile"）
- E10.4 prefix-aligned hybrid checkpoint 最小原型 → docs/E10_4_HYBRID_CHECKPOINT_PROTOTYPE.md：只验证"保存 P 末尾完整 attn+recr → 恢复到 target → 分别执行 P+X/P+Y"；禁止从 P+X 末尾恢复 P+Y；记录 checkpoint token pos/bytes/save+restore 时间、ownership/refcount、baseline vs restore token hash、prefill/e2e/GPU、1 checkpoint→1/2/4 target、cleanup/cancel/slot reuse/server restart；严格停止条件（任一 token mismatch / checkpoint 不能精确对齐 P / restore 成本 ≥80% 重复 prefill / 状态不能安全跨 target 分叉 / 生命周期或所有权不明确）→ QWEN35_CHECKPOINT_REUSE_STATUS: NO_GO_WITH_EVIDENCE；不得扩展为索引/LRU/并发调度/生产系统
- docs/E10_FINAL_DECISION.md

## 项目状态规则
- q8_0 profile 过完整验收但无新主模型算法实现 → PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION + NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
- checkpoint-aligned reuse 无损+显著收益 → PASS_KV_CACHE_OPTIMIZATION + NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: PROTOTYPE_PASS
- q8_0 未过且 checkpoint NO_GO → PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
- 错误 restore/跨 slot 污染/adapter 错误复用/资源泄漏 → REJECT_KV_CACHE_OPTIMIZATION

## 最终回复
PROJECT_STATUS / NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS / C1_ATTENTION_ONLY_STATUS / QWEN3.5_4B_STATUS / QWEN35_Q8_KV_STATUS / QWEN35_CHECKPOINT_REUSE_STATUS / PROMPT_CACHE_IDENTITY_STATUS / LORA_RUNTIME_STATUS / CLONE_API_STATUS

## 硬性约束
不等待用户确认；不改写 E6-E9 历史报告；E10 后必须作出保留/生产化/停止决策，不再启动纯复核阶段。
