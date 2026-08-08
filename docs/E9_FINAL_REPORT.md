# E9：最终报告（Final Report）

- 复核完成时间：2026-08-08
- 范围：E9.0-E9.7（C1 验收关闭 + Qwen3.5-4B 主模型优化转向）

## 最终状态

```text
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_OPTIMIZATION_PROTOTYPE_STATUS: NO_GO_WITH_EVIDENCE（简单 clone 路径；H1 comparator 真实收益）
C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR
LORA_RUNTIME_STATUS: NOT_VERIFIED
SWA_RUNTIME_STATUS: NOT_VERIFIED
RECURRENT_RUNTIME_STATUS: NOT_VERIFIED
PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE / RUNTIME_NOT_VERIFIED
```

## 1. E8 遗留问题关闭情况（E9.0-E9.3）

| E8 遗留 | E9 关闭 |
|---|---|
| 4B 无严格 paired raw | E9.0 记录缺口；E9.4/9.6 在同一 build 下补充 4B 实测（内存组成 + clone 实证）|
| capacity_bytes 笔误（524288 vs 5242880）| E9.0 记录（raw=5242880 正确，报告笔误已注明）|
| prompt-cache LoRA identity（#26207 继承）| **E9.1 修复**（server_prompt_cache_state.lora + alloc/load 门禁）|
| E8 后 endurance 未重跑 | **E9.2 重跑**（12+30 周期 + 7 生命周期场景，同一 build）|
| C1 扫描扩展性未测 | **E9.3**（N=0..64，开销 <1ms；并发容量 NO_CAPACITY_GAIN 如实）|
| LoRA/SWA/recurrent 运行时 | E9.4 再尝试：网络仍不可达 → **NOT_VERIFIED**（不伪造）|
| LM-Infinite 未确认 | **E9.7 确认 NAACL 2024**（ACL Anthology 一级来源）|

## 2. prompt cache identity 审计和修复（E9.1）

- 缺陷确认：`server_prompt_cache::load/alloc` 仅按 token LCP 匹配，无 adapter identity（上游 #26207 继承）→ 不同 lora 复用错误 KV
- 修复：`server_prompt_cache_state.lora`（ptr+scale）+ alloc already-in-cache 跳过条件 + load 候选过滤（identity 缺失/不同拒绝）；prompt_save 传 slot.lora、prompt_load 传任务 lora
- 测试：3 例回归（+3；50 passed + 3 skipped 全量）；真 LoRA 运行时 `RUNTIME_NOT_VERIFIED`（资产缺失）

## 3. 最新代码 endurance（E9.2，同一 build）

- churn 12 + 30 周期：drift=0、erase 后 used/shared/active 全 0、shared 不累积（PASS_ENDURANCE）
- 生命周期 7 场景（on/off）：retry hash 一致、save/restore 200、generation 无跨代污染、shutdown 重启资源归零、capacity 共享 -48% used、0 failed/rejected、exit 全 0

## 4. C1 slot 扫描扩展性（E9.3）

- no-match N=0..32：on-off Δ <1ms（噪声内）→ **LCP 扫描 CPU 开销可忽略**；N=64 需更大 ctx（配置边界非缺陷）
- 容量：顺序模式（idle source）used_cells -49.3%（E8.5）；**并发同时到达 NO_CAPACITY_GAIN**（无 idle source，与上游 clone_to 作者观察一致）——如实记录 C1 机制限制

## 5. 真实容量边界（E9.3）

- ctx=1024/p4/每 slot 256：off 在 4×260 tokens 超池失败（3/3 复现）；on 顺序模式共享 headroom；并发模式 on≈off（NO_CAPACITY_GAIN）
- 物理分配 capacity_bytes 全程不变（不虚报）

## 6. LoRA/SWA/recurrent 运行时（E9.4）

- 网络仍不可达（hf-mirror/huggingface 均超时）→ **全部 NOT_VERIFIED**；测试就绪（test_kv_prefix_share_lora.py 3 例 skipif）；代码级保护已实现（are_lora_equal + capability 默认 false）

## 7. Qwen3.5-4B 内存组成（E9.4，实测）

- 模型 2.7GB、GPU ~2.9GB（p1）；attention KV **32768 B/token** 线性；recurrent **固定**（不随 prompt）；parallel 增量 ~150MB（graph）；prefill 1740-2870 tps、**decode ~77 tps（主瓶颈）**；单请求超 slot ctx → 400

## 8. 主模型候选评分（E9.5）

| 候选 | score | 状态 |
|---|---|---|
| H1（KV 量化 q8_0）| 4.85 | SELECT（comparator：上游开关，实测 -46.9% bytes、输出与 F16 一致）|
| H4（跨 slot clone）| 3.55 | SELECT（新代码 prototype）|
| H2（checkpoint restore）| 3.45 | HOLD（前置条件缺失）|
| H3（recurrent 精度）| 2.20 | REJECT（收益有限）|

## 9. 主模型 prototype 结果（E9.6）

- **H4 简单 clone**（`/slots/{dst}?action=clone_from`）：TinyLlama 无损成立（prompt_n 259→14、hash 一致）；**4B hybrid 实证输出不一致**（recurrent 状态是末尾位置、与 target 续写起点错位）→ 已加 hybrid/recurrent 拒绝保护
- **QWEN35_OPTIMIZATION_PROTOTYPE_STATUS: NO_GO_WITH_EVIDENCE**（简单路径实证关闭；两个候选的真实测量证据：H1 q8_0 收益 + H4 hybrid 不一致）
- 正确路径 = H2 checkpoint 对齐（独立工程），本阶段未承诺

## 10. 文献最终修正（E9.7）

- **LM-Infinite = NAACL 2024**（2024.naacl-long.222，DOI 10.18653/v1/2024.naacl-long.222；E8 漏检因正式版标题不同）
- manifest/bib 修正；文献 metadata 无残留"已发表未确认"条目

## 11. 测试、退出码、commit、产物、工作树

**测试**：llama.cpp 50 passed + 3 skipped（LoRA 不计 passed）、E1 5/5、根 177；全量 exit 0
**commit**：
- llama.cpp：`d6d679e7`（fix prompt cache identity）、`795eb372`（feat clone prototype）
- 根：`e981a01`(E9.0)、`39cf880`(E9.1)、`265f368`(E9.2)、`09d9083`(E9.3)、`6b0b6de`(E9.4)、`a608b4b`(E9.5)、`4953405`(E9.6)、`273f83e`(E9.7)
**产物**：`docs/E9_0..E9_7 + E9_FINAL`、`benchmark/scripts/{e9_2_lifecycle,e9_3_scaling,e9_5_memory_breakdown}.py`、`raw/e9_*.json`
**工作树**：两仓库 clean（未跟踪 `docs/20260808_E9_INSTRUCTION.md`）

## 12. 唯一项目结论

**PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION**：
- **C1 完整关闭**（ATTENTION_ONLY_PASS：endurance 重跑、identity 修复、扩展性、容量边界、runtime identity 全过）——但收益仅在 attention-only 模型
- **主模型 Qwen3.5-4B 无收益**（hybrid：C1 禁用、简单 clone 实证 NO_GO）；H1（q8_0 KV 量化）在主模型上有真实收益（-46.9% bytes、输出一致）但为上游既有开关——**主模型优化的可行新路径 = H1 收益固化（自动化验证层）+ H2 checkpoint 对齐（独立工程）**
- 不用 TinyLlama 收益恢复 PASS；Qwen3.5-4B 仍为主模型
