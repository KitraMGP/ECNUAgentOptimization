# E14 指令文件（2026-08-08，浓缩版）

用户于 2026-08-08 启动 E14（部署发布阶段）：Qwen3.5-4B q8_0 operational profile 发布、灰度和运维交接。E14 不是优化研究阶段。

## 禁止
修改 E6-E13 历史报告/raw；继续 4B hybrid checkpoint hit-path；修改 recurrent/cache_prompt/checkpoint save 时机；checkpoint experimental 进生产配置；q8_0 描述为新算法；矩阵内 token-exact 扩展为全局无损；因部署好而改 NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS。

## 冻结状态
```
PROJECT_STATUS: PASS_OPERATIONAL_KV_OPTIMIZATION
NOVEL_MAIN_MODEL_OPTIMIZATION_STATUS: NOT_ACHIEVED
C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
QWEN35_Q8_KV_STATUS: PASS_VALIDATED_DEPLOYMENT_PROFILE
QWEN35_CHECKPOINT_REUSE_STATUS: NO_GO_WITH_EVIDENCE
CHECKPOINT_PROTOTYPE_STATUS: RETAINED_EXPERIMENTAL_ATTENTION_ONLY
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
LORA_RUNTIME_STATUS: NOT_VERIFIED
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE
```

## 阶段与交付
- E14.0 冻结发布基线 → docs/E14_0_RELEASE_BASELINE.md：根/llama 最终 HEAD+工作树、最终发布源码 commit、编译器/CUDA/驱动/GPU/OS、构建命令、**最终二进制 SHA256**、**模型完整 SHA256**、q8_0 配置 SHA256、发布日期和制品目录、测试退出码；处理差异（E13.1 validated build f54930492 vs E13 最终 afbf375c）→ **基于 afbf375c 重建 + 重跑 E13.1 轻量 q8_0 回归**；失败 → RELEASE_STATUS: BLOCKED_FINAL_BUILD_REGRESSION
- E14.1 不可歧义生产配置 → benchmark/configs/qwen35_4b_q8_production.yaml + docs/E14_1_PRODUCTION_CONFIGURATION.md：model path/完整 SHA256/cache-type-k/v=q8_0/ctx4096/parallel4/kv-unified/GPU offload/host-port/API authorization/metrics/日志路径+轮转/request timeout/shutdown timeout/最大并发和请求大小边界/checkpoint-reuse=false/无 checkpoint_save-restore/无 clone_from/q4_0 不进；启动日志断言 K/V type=q8_0、per-cell=17408、checkpoint 未启用、无 save/restore 日志、无 unsupported hybrid 路径、模型 SHA256 一致；**temp=0/seed=42 只用于发布回归，不作业务策略**
- E14.2 发布前验收 → docs/E14_2_RELEASE_CANDIDATE_VALIDATION.md + raw/e14_release_candidate_validation.json：15 项（启动健康/q8_0 生效/per-cell/short QA/tool JSON parse-schema/system retention/canary/multi-turn/long ~1000 tokens/parallel4 并发/graceful shutdown/异常+超长受控拒绝/checkpoint off 日志 0/clone_from 不存在或拒绝/重启健康恢复）；每项记录 HTTP status/token IDs/hash/parse/schema/prompt-decode 数/prefill-decode-e2e 时间/GPU used-peak/KV used-capacity/failed-rejected/exit code/关键启动日志；门禁：0 crash-hang-OOM、健康全过、tool schema 全过、canary 无泄漏、q8_0+per-cell 符合、默认路径无 checkpoint、输出与 E13 基线一致或可解释、测试退出码全 0
- E14.3 灰度 → docs/E14_3_CANARY_ROLLOUT.md + raw/e14_canary_rollout.json：真实生产则 5%/30min → 25%/60min → 100%；**无真实生产环境 → 不得虚构，本机/预发布 dry-run + RELEASE_STATUS: READY_FOR_DEPLOYMENT + PRODUCTION_ROLLOUT_STATUS: NOT_EXECUTED**；灰度记录请求数/成功率/4xx-5xx/rejected-OOM/p50-p95-p99/tps/GPU/KV occupancy/队列/server restart/schema 失败/canary 异常/资源差异；立即回滚 8 条件
- E14.4 回滚演练 → docs/E14_4_ROLLBACK_RUNBOOK.md + raw/e14_rollback_drill.json：上一个稳定配置/F16 KV fallback profile/校验方式/停新实例/排空/启回滚实例/health-canary-tool JSON 验证/流量切回/GPU-KV 确认；至少一次非生产回滚演练；回滚不依赖重编译；q8_0 与 F16 配置可切换；不删历史制品；无 destructive git；记录恢复时间和命令退出码
- E14.5 运维交接 → docs/E14_5_OPERATIONS_HANDOFF.md：启动/停止/重启命令、health/metrics endpoint、日志位置、SHA256、正常 GPU/KV 指标范围、告警阈值、常见启动失败、OOM-timeout-JSON 失败处理、q8_0→F16 回滚步骤、已验证/未验证范围、q4_0 NOT_VALIDATED、ctx8192 NOT_VALIDATED、LoRA NOT_VERIFIED、checkpoint hybrid NO_GO、checkpoint attention-only experimental 默认关；**不得要求值班启用 checkpoint-reuse 解决生产性能**
- E14.6 发布决策 → docs/E14_FINAL_RELEASE_DECISION.md：情况 A（真实灰度 → RELEASE_STATUS: DEPLOYED + ROLLOUT: COMPLETE + Q8: DEPLOYED_VALIDATED_PROFILE）/ 情况 B（候选过但无真实环境/窗口 → READY_FOR_DEPLOYMENT + NOT_EXECUTED + PASS_VALIDATED_DEPLOYMENT_PROFILE）/ 情况 C（失败 → BLOCKED + NOT_EXECUTED_OR_ROLLED_BACK + ..._NOT_DEPLOYED）；NOVEL/CHECKPOINT/CHECKPOINT_PROTOTYPE 三字段不得改变

## 提交收口
两仓库最终 clean；不提交模型/build/生产日志/密钥/临时二进制；API key 不写入配置或报告；配置/脚本/文档/raw 分开提交；记录 commit；保存制品 SHA256；不改 E6-E13 历史。

## 最终回复（12 项）
最终源码+build commit / 二进制-模型-配置 SHA256 / q8_0 参数生效 / checkpoint 关闭 / 发布前验收 / 灰度是否真实 / 回滚演练 / 监控告警边界 / 测试退出码 / RELEASE_STATUS / PRODUCTION_ROLLOUT_STATUS / 冻结状态

## 硬性约束
E14 后不再进入优化复核阶段；后续只允许正常运维、缺陷修复、或新模型/新上游/真 LoRA 的新独立项目。
