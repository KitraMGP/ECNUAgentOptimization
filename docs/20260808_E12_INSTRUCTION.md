# E12 指令文件（2026-08-08，浓缩版）

用户于 2026-08-08 启动 E12：Qwen3.5-4B hybrid checkpoint 命中路径最终可行性验证与工程收口。E11 历史报告不改；不重新文献检索；不重复 TinyLlama 收益证明；不把 q8_0 当新算法。**E12 只能产生两种结果：1) 4B hybrid checkpoint 命中并通过最小生产候选验收；2) 明确证明 server 集成路径 NO_GO 并停止。不得无限延长。**

## 冻结状态
```
PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
QWEN35_CHECKPOINT_REUSE_STATUS: PROTOTYPE_PASS_SERVER_INTEGRATION_PARTIAL
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 阶段与交付
- E12.0 状态冻结+E11 事实核对 → docs/E12_0_E11_STATUS_FREEZE.md：核对 HEAD/工作树/build commit/checkpoint 三入口（--checkpoint-reuse、save、restore）/find_prefix、cache_prompt、n_past、pos_min、recurrent position 代码位置/4B restore 成功但 prompt_n=384 全量日志/TinyLlama 命中日志/E11 raw 是否记录 token vector、token_count、prompt_n、state position；明确：
  ```
  E11_CHECKPOINT_STATUS: SERVER_INTEGRATED_PARTIAL
  QWEN35_HYBRID_CACHE_HIT: FALSE
  QWEN35_HYBRID_CORRECTNESS: TRUE_BY_SAFE_FALLBACK
  QWEN35_HYBRID_OPTIMIZATION: NOT_ACHIEVED
  ```
- E12.1 定位 hybrid cache miss 真实原因（**先诊断不改逻辑**）→ docs/E12_1_QWEN35_HYBRID_CACHE_MISS_ROOT_CAUSE.md：对 4B 增加结构化诊断（checkpoint token_count/request token_count/token IDs/prefix hash/llama_memory_seq_pos_min/attention+recurrent position/restore 后 target seq position/prompt.tokens/n_past/n_tokens/cache_prompt keep-eval 区间/generation/state bytes/batch 起始 position/实际 token 数/used-shared-active）；三情况（P 单独 save+P+X 整体 tokenize；P 无尾空格前缀一致；P 有 BPE 合并风险前缀不一致）；区分 6 类原因（token prefix mismatch/identity mismatch/recurrent position mismatch/cache_prompt 误判/restore 后 state 被清/suffix 调度错误）
- E12.2 最小 hybrid hit-path 修复（**只有 E12.1 证明 restore state 在精确 P 边界才允许改**）→ 允许的 10 条最小路径（checkpoint 用实际已评估 token vector 保存；restore 前要求 request 前 token_count 个 token 与 checkpoint 完全一致；不用文本前缀/空格裁剪/hash 推断；restore 后 P 标记完成；直接调度 suffix；不让 cache_prompt 依据 recurrent pos_min 清空重算；target 独立 state copy；checkpoint 只读；默认关闭；不新增公共 clone API）；禁止 8 条（从 P+X 末尾推断 P/改 recurrent 数值/忽略 recurrent position/减少 token 数伪造命中/文本前缀代替 token vector/prefix mismatch 强制恢复/seq_cp 用于 recurrent）；**若无法证明 restore 后 recurrent position 正确 → 立即停止代码修改进入 NO_GO 判定**
- E12.3 4B 正确性矩阵 → docs/E12_3_QWEN35_HYBRID_CORRECTNESS_MATRIX.md：真实 4B；P 116/362/512/1024 × target 1/2/4 × suffix X/Y 不同长度 × P 末尾无空格/空格/标点/换行 × on/off × q8_0 与 F16 各一组 × C1 off/on（确认 hybrid 拒绝）；每 target 比较 baseline vs restore 的 token ID/hash/起始 position/实际 token 数/recurrent position/attention position/roundtrip hash；每场景≥5 次、正式性能≥20 次、P+X/P+Y 分别验证、同一 checkpoint 分叉 1/2/4、source 删除后 target 继续、target 删除不影响其他、prefix mismatch 安全拒绝全量 fallback、restore 失败回滚、refcount=0、无 crash/hang/OOM/跨 target 污染
- E12.4 4B 端到端收益 → docs/E12_4_QWEN35_HYBRID_END_TO_END_RESULT.md：仅 E12.3 全 token-exact 后；warmup 5 + formal 20 paired；on/off × target 1/2/4 × P 116/362/512/1024；X/Y 不同；save 成本算一次、restore 每 target 单独、含 checkpoint 常驻 host memory、不混 KV capacity gain 与 prefill reuse gain；记录 full prefill/save/restore/suffix prefill/decode/e2e p50-p95/checkpoint bytes/GPU-host memory/最大 target/failure/refcount/fallback 次数/cache hit rate；门槛：P=116 t1 不要求收益但不得错误；P≥512 或 target≥2 场景含 save/restore 总成本相对 full prefill ≥20% 降低；至少一个真实场景 ≥30% e2e 降低；p95 不增 >10%；fallback 不慢于 off >5%；checkpoint host memory 明确报告不得声称容量提升；若只恢复不减少 prefill → 无优化收益
- E12.5 最终工程决策：情况 A（4B 命中+无损+生命周期+性能 → PASS_KV_CACHE_OPTIMIZATION + NOVEL: SERVER_INTEGRATED_BETA + CHECKPOINT: SERVER_INTEGRATED_BETA + QWEN3.5_4B: OPTIMIZED_WITH_CHECKPOINT_REUSE）/ 情况 B（4B 正确但安全 fallback 无收益 → PASS_OPERATIONAL_KV_OPTIMIZATION + NOT_ACHIEVED + CHECKPOINT: NO_GO_WITH_EVIDENCE + QWEN3.5_4B: CORRECT_NO_OPTIMIZATION；保留 C1+q8_0+checkpoint 实验代码默认关；停止 4B checkpoint 索引/LRU/分布式/调度、TinyLlama 收益当主模型证据、q8_0 当新算法）/ 情况 C（错误恢复/token mismatch/污染/泄漏 → REJECT + 立即关闭 checkpoint 默认入口保留 q8_0）
- docs/E12_FINAL_DECISION.md
- raw：e12_hybrid_diagnostics.json / e12_hybrid_correctness.json / e12_hybrid_performance.json（不覆盖 E6-E11 raw）

## 最终回复格式
PROJECT_STATUS / NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS / C1_ATTENTION_ONLY_STATUS / QWEN3.5_4B_STATUS / QWEN35_Q8_KV_STATUS / QWEN35_CHECKPOINT_REUSE_STATUS / PROMPT_CACHE_IDENTITY_STATUS / LORA_RUNTIME_STATUS / CLONE_API_STATUS + 8 项报告（根因/是否改 hit-path/token-exact 结果/P×target 矩阵/save-restore-fallback 成本/checkpoint host memory/生命周期 refcount/最终决策）

## 硬性约束
不改 E6-E11 历史；E12 后不得再启动纯复核阶段；若 4B 无收益必须接受"q8_0 operational profile + novel checkpoint NO_GO_WITH_EVIDENCE"最终结论。
