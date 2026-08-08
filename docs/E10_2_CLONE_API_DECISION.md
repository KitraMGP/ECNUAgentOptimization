# E10.2：clone_from API 决策（Clone API Decision）

- 复核时间：2026-08-08
- 对象：`/slots/{dst}?action=clone_from`（E9.6 实验 API）

## 1. 审计

| 维度 | 发现 |
|---|---|
| 授权 | **无鉴权**：任意客户端可触发跨 slot 状态复制（无 API key/身份检查）|
| source/target ownership | 无显式 ownership/refcount 模型（复制后 source/target 各自独立持有数据拷贝）|
| busy slot | 已处理（is_processing → defer）|
| 并发 | 单线程事件循环，无数据竞态；但无并发 clone 的语义定义 |
| cancel | 无 cancel 语义（与 server 整体一致，NOT_SUPPORTED）|
| restore/错误返回 | 错误返回已实现（无效 source/无缓存/空间不足 → error）|
| 跨请求污染 | hybrid 已拒绝（E9.6 保护）；attention-only 上复制精确 → 无污染 |
| 已验证用途 | TinyLlama 无损 clone（prompt_n 259→14、hash 一致）——与 C1 自动前缀共享**功能重叠**（显式 API vs 自动扫描）|

## 2. 决策：`INTERNALIZE_FOR_CHECKPOINT_PROTOTYPE`

理由：
1. **默认可访问 + 无明确产品用途**：当前是无需开关的公共 HTTP action，违反"不得保留默认可访问但不具备明确产品用途的跨 slot 状态操作 API"
2. 已验证用途与 C1 重叠（TinyLlama 上显式 clone ≈ C1 自动共享的效果），独立 API 价值低
3. hybrid 默认拒绝 → 该 API 在主模型上无用途
4. **E10.4 checkpoint 原型需要"状态恢复到 target"的内部机制**（state_seq_get/set_data 恢复路径）——底层 API 保留，公共 HTTP 层移除

## 3. 实施（已提交）

- **移除**：`post_slots` 的 `clone_from` action 注册、`handle_slots_clone_from` handler、`SERVER_TASK_TYPE_SLOT_CLONE` 任务类型、`slot_action.source_id` 字段、server-context.h 声明（3 文件 0 残留）
- **保留**：底层 `llama_state_seq_get_data/set_data`（E10.4 checkpoint 原型复用）
- 构建通过（CPU + CUDA）；无自动化测试依赖该 API（E9.6 为手动验证）

## 4. 结论

```text
CLONE_API_STATUS: INTERNALIZED_FOR_CHECKPOINT_PROTOTYPE（公共 HTTP action 已移除）
```
- 不再存在默认可访问的跨 slot 状态操作 API
- E10.4 在内部代码路径实现 checkpoint 对齐恢复（不暴露公共 action）
