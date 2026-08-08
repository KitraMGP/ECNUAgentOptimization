# E11.2：server checkpoint 数据结构和所有权设计（Ownership & Identity Design）

- 复核时间：2026-08-08
- 代码：`llama.cpp/tools/server/server-checkpoint.h`（新，独立可测）+ `tools/server/tests/unit/test_checkpoint_identity.cpp`（C++ 单测）

## 1. checkpoint 数据结构（15 字段）

```cpp
struct server_checkpoint_entry {
    uint64_t id;                          // checkpoint id
    std::string model_id;                 // model identity/revision
    std::vector<common_adapter_lora_info> lora;  // adapter identity（ptr + scale）
    ggml_type type_k, type_v;             // KV type
    std::string context_config;           // ctx/parallel/kv_unified 摘要
    std::string rope_config;              // RoPE 配置
    std::string tenant_scope;             // trusted_single_tenant
    uint64_t prefix_hash;                 // token prefix 哈希
    size_t token_count;                   // 精确 token 数
    std::vector<uint8_t> state;           // attn + recurrent 完整状态
    size_t state_bytes;
    uint64_t generation;                  // 创建代次
    int64_t last_use_tick;
    int refcount;                         // 引用计数
    bool valid;                           // state validity（refcount==0 → false）
};
```

## 2. 所有权规则（实现于 `server_checkpoint_pool`）

| 规则 | 实现 |
|---|---|
| source slot 删除不破坏 target 使用的 checkpoint | refcount 保护：release 只减计数，refcount==0 才 `valid=false` |
| target restore 失败回滚 | 调用方在 `set_data` 失败时清 target 并释放引用（E11.3 集成）|
| target 恢复后独立拥有 | state 为字节拷贝（测试验证：修改副本不影响池内 state）|
| 多 target 分叉 | `retain()` 多次，各 target 从同一 buffer 恢复 |
| refcount==0 才释放 | `release()` 逻辑 |
| shutdown 释放全部 | `clear_all()` |
| 不跨 model/adapter/KV type/context/RoPE/tenant | `checkpoint_identity_matches` 全字段比较（缺失/不确定 → 拒绝）|

## 3. 精确边界

- 只允许在**精确 prompt token 边界**创建（`token_count` 精确记录）
- `P+X` 末尾状态不得当作 P（E10.4 探针已证 end-position 错误；本设计只存"P 末尾"checkpoint——由 E11.3 集成流程保证创建时机）
- restore 前必须 `checkpoint_prefix_matches`（prefix_hash + token_count 一致）否则拒绝
- 不从普通末尾 slot state 推断 prefix checkpoint

## 4. 默认行为

- **默认关闭**（`--checkpoint-reuse` 参数默认 off，E11.3 实现）
- 仅内部实验路径；**不新增无鉴权 HTTP action**（clone_from 已移除，E10.2）
- 若未来加 API：必须先实现可信 authorization + tenant scope

## 5. C++ 单元测试（18 断言，全部通过）

编译运行：`c++ -std=c++17 -I common -I tools/server -I include tools/server/tests/unit/test_checkpoint_identity.cpp common/jinja/*.cpp -L build-cuda/bin -lllama-server-impl -lllama -lllama-common -lggml-base ... && /tmp/test_ckpt` → **ALL CHECKPOINT IDENTITY TESTS PASSED**

覆盖：空 adapter 相等 / 同 pointer+scale 相等 / 同 pointer 不同 scale 拒绝 / 不同 pointer 同 scale 拒绝 / 不同 pointer 不同 scale 拒绝 / 缺失 identity 拒绝 / 不同 model 拒绝 / 不同 KV type 拒绝 / 不同 prefix hash 拒绝 / 不同 token count 拒绝 / 不同 context/rope/tenant 拒绝 / refcount retain-release / refcount==0 失效 / 失效后拒绝 / 多 target 分叉（2 retain）/ state 拷贝独立 / clear_all。

**不依赖 LoRA 模型资产、不依赖 Python 实例化 C++ 类型。**

## 6. 结论

- checkpoint 数据结构 + 所有权 + identity 边界设计完成并 C++ 测试验证
- 与 E11.3（server 集成最小路径）衔接：`server_checkpoint_pool` 为存储层，集成负责创建/恢复时机
