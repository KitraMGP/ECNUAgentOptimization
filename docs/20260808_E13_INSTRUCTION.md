# E13 指令文件（2026-08-08，浓缩版）

用户于 2026-08-08 启动 E13（最终阶段）：项目最终收口、q8_0 部署固化与 checkpoint 原型隔离。不得修改 E6-E12 历史报告/raw/结论；不得继续 hybrid checkpoint hit-path 修改；不得用 TinyLlama 证明主模型优化；不得把 q8_0 当新算法。

## 冻结状态
```
PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
QWEN35_CHECKPOINT_REUSE_STATUS: NO_GO_WITH_EVIDENCE
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 阶段与交付
- E13.0 最终事实冻结 → docs/E13_0_PROJECT_CLOSURE_AND_STATUS.md：核对 HEAD/工作树/E12 build commit/E12.2 两次修复失败原因/4B restore 成功但全量 prefill 证据/save+restore 额外成本/checkpoint host memory 64.66MB/q8_0 验证范围和限制/C1/LoRA/identity 最终状态；明确：E12.4 p95 门槛未被命中路径实测不能写成通过、E12.4 结论 NO_OPTIMIZATION_GAIN、4B checkpoint 正确性来自安全 fallback、E10.4 探针仍 API_LEVEL_PROTOTYPE；发现 E12 不一致只能新增 E13 勘误不改历史
- E13.1 q8_0 profile 固化 → docs/E13_1_QWEN35_Q8_DEPLOYMENT_PROFILE.md：唯一推荐 `--cache-type-k q8_0 --cache-type-v q8_0`；记录模型 SHA256（de8e96cd...）/CUDA build commit/ctx-parallel-kv-unified/seed-temp/已验证 workload/token-exact 仅限本矩阵/decode max 2.10%/KV -47%/GPU -58MB/q4_0 NOT_VALIDATED/8192 ctx 未验证/不得声称全局无损；新增 benchmark/configs/qwen35_4b_q8_validated.yaml（model path/cache types/ctx-parallel/generation params/validation scope/expected status）+ 轻量回归命令（server 启动、q8_0 生效、canary 输出、KV per-cell、无失败退出）
- E13.2 checkpoint 原型隔离 → docs/E13_2_CHECKPOINT_PROTOTYPE_DISPOSITION.md：--checkpoint-reuse 默认关；不进推荐 profile；不作主模型优化成果；不新增公共 clone API；不实现 LRU/索引/分布式/持久化/生产调度；不允许 hybrid 无提示产生 save/restore 开销；4B hybrid 提前标记 unsupported/no-hit 或安全降级；attention-only 原型保留但标 experimental；实验日志明确 hit/fallback/额外成本；**代码/配置层至少一个保护**（hybrid 启用 checkpoint reuse 直接拒绝给错误；或允许但请求前安全降级并记录 checkpoint_reuse_unsupported_hybrid；不得执行无收益的 64MB save + 8ms restore 后再全量 prefill）
- E13.3 默认路径回归 → raw/e13_default_path_regression.json：默认配置不启用 checkpoint reuse；4B 默认请求输出与 E12 baseline 一致；q8_0 profile 正常启动；C1 hybrid capability 仍拒绝；TinyLlama C1 测试不回归；checkpoint off 时无 save/restore；checkpoint 代码不改变普通请求显存/prompt_n/输出；clone_from 公共 action 0 残留；identity C++ 单测过；根测试/llama.cpp 测试退出码全 0
- E13.4 文档和状态命名收口 → docs/E13_FINAL_DECISION.md：状态字段含 CHECKPOINT_PROTOTYPE_STATUS: RETAINED_EXPERIMENTAL_ATTENTION_ONLY；工程决策（生产化 q8_0、保留 C1+checkpoint attention-only 原型+identity 测试、停止 4B hybrid hit-path+end-position clone+C2 扩展+TinyLlama 外推、不宣称新主模型算法）
- E13.5 提交和工作树收口：两仓库 clean；不提交模型/build/日志/临时二进制；探针保持未追踪或忽略；新增代码/测试/配置/文档分别提交；记录 commit hash；无 destructive git

## 最终回复（10 项）
两仓库 HEAD+工作树 / 最终 build commit / q8_0 推荐配置 / q8_0 验证边界 / checkpoint 为什么停止 / checkpoint 原型是否保留 / hybrid 是否加保护 / 默认路径回归结果 / 测试退出码 / 最终状态字段

## 硬性约束
不改 E6-E12 历史；E13 后项目正式收口，不再启动新的 4B checkpoint 纯复核阶段。
