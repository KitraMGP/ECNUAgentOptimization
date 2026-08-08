# E11.6：prompt-cache identity 最终处理（Identity Finalization）

- 复核时间：2026-08-08
- 目标：无真实 LoRA 模型时，通过 C++ 结构化测试完成 identity 逻辑验证

## 1. C++ 结构化测试（E11.2，18 断言全过）

`tools/server/tests/unit/test_checkpoint_identity.cpp`（不依赖 Python 实例化 C++、不依赖 LoRA 资产）：

| identity 组合 | 断言 | 结果 |
|---|---|---|
| 空 adapter（无 lora）相等 | `checkpoint_lora_equal({}, {})` | PASS |
| 相同 pointer + 相同 scale | 相等 | PASS |
| 同 pointer + 不同 scale | 拒绝 | PASS |
| 不同 pointer + 同 scale | 拒绝 | PASS |
| 不同 pointer + 不同 scale | 拒绝 | PASS |
| 缺失 identity（空 vs 有）| 拒绝 | PASS |
| 不同 model identity | 拒绝 | PASS |
| 不同 KV type | 拒绝 | PASS |
| 不同 context config / rope / tenant | 拒绝 | PASS |
| 不同 prefix hash / token count | 拒绝 | PASS |
| refcount retain/release → 归零失效 | PASS | PASS |
| source 删除后（refcount 保护）| 仍可 retain | PASS |
| restore 失败回滚 | set_data 失败 → release + clear | PASS（代码路径）|
| state 拷贝独立（多 target 分叉）| 修改副本不影响池 | PASS |

**注**：`checkpoint_lora_equal` 与 server-common 的 `are_lora_equal` 同语义（size + ptr + scale）；E9.1 的 `server_prompt_cache` alloc/load 门禁使用 `are_lora_equal`（代码级验证 + E9.1 回归）。

## 2. 开关组合集成测试（无 lora 场景，8 组合）

`cache_ram {0,256} × kv-prefix-share {off,on} × checkpoint-reuse {off,on}`：

| 组合 | 结果 | prompt_n（target）|
|---|---|---|
| 全部 8 组合 | 200/200（无失败/crash）| ckpt=1 → 14（restore 生效）；ckpt=0 → 74（普通路径）|
| 开关独立性 | pn2 仅由 checkpoint 决定；cache_ram/share 不影响 | PASS |

raw：`raw/e11_switch_combos.json`

## 3. 状态

```text
LORA_RUNTIME_STATUS: NOT_VERIFIED（真实 adapter 资产不可得，保持）
PROMPT_CACHE_IDENTITY_STATUS: CODE_AND_UNIT_TEST_VERIFIED_RUNTIME_NOT_VERIFIED
```

- **不把 RUNTIME_NOT_VERIFIED 隐藏在 PASS 后**：identity 逻辑经 C++ 结构化测试 + 开关组合验证（CODE_AND_UNIT_TEST_VERIFIED）；真实 adapter 运行（不同 pointer/scale 的实际加载）仍 NOT_VERIFIED（网络不可达，moe_shakespeare15M.gguf 无法获取）
- 缺失/不确定 identity → 拒绝复用（E11.2 测试断言）；不使用 prompt 文本/HTTP header 代替 identity

## 4. 结论

- identity 逻辑（adapter ptr+scale、model、KV type、context、rope、tenant、prefix）C++ 结构化验证完成
- 真实 adapter 运行时验证受资产限制 → 如实 NOT_VERIFIED
