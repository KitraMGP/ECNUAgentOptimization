# E13.2：checkpoint 原型隔离（Prototype Disposition）

- 复核时间：2026-08-08
- 目的：checkpoint 代码保留但明确工程隔离；hybrid 增加保护

## 1. 工程隔离清单

| 项 | 状态 |
|---|---|
| `--checkpoint-reuse` 默认关闭 | **是**（默认 off）|
| 进入 Qwen3.5-4B 推荐部署 profile | **否**（E13.1 profile 不含）|
| 作为主模型优化成果 | **否**（E12 NO_GO_WITH_EVIDENCE）|
| 公共 clone API | **无**（E10.2 已移除，0 残留）|
| LRU/索引/分布式/跨重启持久化/生产调度 | **不实现**（E12 停止）|
| **hybrid 无提示产生 save/restore 开销** | **已防护**（见 §2）|
| attention-only 原型 | 保留，标记 experimental（默认关）|
| 实验日志明确 hit/fallback/额外成本 | **是**（E11-CKPT saved/restored/restore failed + E12-DIAG）|

## 2. hybrid 保护（代码实现，E13.2）

**server 启动时**（load_model 后）：

```cpp
if (checkpoint_reuse && (llama_model_is_hybrid(model_tgt) || llama_model_is_recurrent(model_tgt))) {
    SRV_WRN("checkpoint_reuse_unsupported_hybrid: disabling --checkpoint-reuse on hybrid/recurrent model "
            "(E12: NO_GO_WITH_EVIDENCE; save/restore would be pure overhead)");
    checkpoint_reuse = false;
}
```

**行为**：hybrid/recurrent 模型上 `--checkpoint-reuse` **启动时安全降级（禁用）**并记录 `checkpoint_reuse_unsupported_hybrid`——**不执行无收益的 ~64MB save + ~8ms restore 后再全量 prefill**。

**实测**：
- TinyLlama（attention）：无 unsupported 日志（checkpoint-reuse 保留，experimental）
- Qwen3.5-4B（hybrid）：`checkpoint_reuse_unsupported_hybrid` 日志 1 条（降级禁用）

## 3. 保留与标记

- checkpoint 池/identity 测试（E11.2，C++ 18 断言）保留
- E10.4 API 探针（llama.cpp/tmp/，根忽略）保留为 API_LEVEL_PROTOTYPE 参考（非项目交付）
- attention-only（TinyLlama）原型保留：experimental、默认关、日志标 hit/fallback

## 4. 结论

```text
CHECKPOINT_PROTOTYPE_STATUS: RETAINED_EXPERIMENTAL_ATTENTION_ONLY
QWEN35_CHECKPOINT_REUSE_STATUS: NO_GO_WITH_EVIDENCE（Qwen3.5-4B server 路径）
```
- hybrid 保护已落地（代码 + 日志）；不产生无收益开销
