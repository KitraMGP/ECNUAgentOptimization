# E8.7：Qwen3.5-4B hybrid 前缀复用技术路径决策（Hybrid Prefix Reuse Decision）

- 复核时间：2026-08-08
- 主模型：Qwen3.5-4B（hybrid：recurrent + attention 混合架构）
- 冻结状态：`QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION`（除非新实现通过无损/生命周期/性能验收，否则保持）

## 1. hybrid 状态语义审计（代码证据）

| 组件 | 实现 | 关键语义 |
|---|---|---|
| hybrid memory | `llama_memory_hybrid`（src/llama-memory-hybrid.h/.cpp）| 组合 `mem_attn`（llama_kv_cache）+ `mem_recr`（llama_memory_recurrent）|
| `seq_cp`（attention 侧）| 转发 `mem_attn->seq_cp` | cell bitset 元数据共享（同 stream 纯元数据）|
| `seq_cp`（recurrent 侧）| `mem_recr->seq_cp`（llama-memory-recurrent.cpp:235）| **只把 dst seq 加入 src 的 tail cell 的 seq_id 集合（`tail_dst.tail = tail_src.tail`）——共享尾部状态位置，非状态快照复制** |
| `state_write/read` | hybrid 同时序列化 attn + recr（llama-memory-hybrid.cpp:201-213）| **checkpoint / state save-restore 覆盖完整 recurrent state**（理论上可恢复任意 checkpoint 位置的完整状态）|

## 2. 核心障碍（C1 元数据共享路径在 hybrid 上不可行）

场景：source 完成 `A+X`（recurrent state 已推进到 X 末尾），target 请求 `A+Y`。

1. **recurrent state 不可回退**：target 若通过 seq_cp 共享 source 的 tail（A+X 末尾状态），将按 A+X 状态继续生成 Y —— **错误状态**（E2.0.5 已实测：hybrid 上 seq_cp 使 pos_min 被 recr 拉高 → n_past 清零，共享有损）
2. **C1 的"元数据共享"路径**只适用于 attention cell（可随机访问的 token 级位置）；recurrent convs/state 是**顺序推进的不可逆状态**，不存在"前缀位置共享"语义
3. **不存在 A 末尾的持久快照**：server prompt checkpoint（`slot.prompt.checkpoints`）是 per-slot 私有、prefill 完成后清理、无跨 slot 共享机制

## 3. 路线 A：正式缩小目标为 attention-only 模型优化

- 适用范围：标准 attention-only unified KV 模型（TinyLlama、MOE、Llama 系列等非 hybrid/SWA/recurrent）
- C1 在该范围：capability accepted、recompute -99.1%、无损、容量 headroom -49.3%（E8.5）
- **Qwen3.5-4B 明确不在支持范围**（hybrid，capability rejected）
- 项目级目标重定义**需用户明确接受**（当前项目以 Qwen3.5-4B 为主模型 → 项目级状态保持 PARTIAL）

## 4. 路线 B：hybrid exact-prefix reuse 可行性研究

### 4.1 理论可行路径（唯一）
`state_read/state_write` 覆盖完整 attn+recr → 若在 **A 末尾**保存 persistent checkpoint，target 从该 checkpoint 恢复（**数据恢复，非元数据共享**）→ 可做到无损（token ID 一致）。

### 4.2 必要条件（全部不满足，当前实现）
1. checkpoint 精确对齐 A 末尾（现 checkpoint 机制不对齐任意前缀边界）
2. checkpoint 跨 slot 共享/所有权（现 per-slot 私有）
3. 并发多 target 从同一 checkpoint 分叉的语义（现无）
4. recurrent state 恢复成本可接受（recurrent state 尺寸 = Σ n_layer_recr × conv/state 张量；4B 上需序列化+拷贝，非零成本）

### 4.3 成本量化（结构式估算）
- 每 target 从 checkpoint 恢复 = 完整 recurrent state 反序列化 + attention KV 前缀复制（数据复制，内存瞬时双份）
- 上游 PR #26204（clone_to）作者实测：hybrid SSM 模型上加速低（"On hybrid SSM models the speedup is lower"）——与本地审计一致
- 恢复成本随 recurrent 层数线性增长；并发 target 数 N 时成本 ×N

### 4.4 结论：**NO_GO（当前实现下）**

```text
ROUTE_B_STATUS: NO_GO_IN_CURRENT_IMPLEMENTATION
```

理由：
- C1 元数据共享路径在 hybrid 上**机制性不可行**（recurrent 顺序状态不可回退/不可按前缀共享）
- checkpoint 恢复路径存在但需 4 个不满足的必要条件（对齐/所有权/并发/成本），且上游 clone_to 实测 hybrid 加速低
- 无损验收（token ID 完全一致）可满足，但**无最小无损 prototype 可低成本构建**——需先实现 checkpoint 前缀对齐机制（独立中型工程），收益在 hybrid 上不确定

### 4.5 若未来继续路线 B（非本阶段承诺）
前置：① prompt checkpoint 前缀对齐；② 跨 slot checkpoint 引用；③ recurrent state 恢复成本基准（4B 实测）；④ 并发分叉语义。全部完成并过无损/生命周期/性能验收后才可改 `QWEN3.5_4B_STATUS`。

## 5. 决策

1. **保持 `QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION`**（C1 在 4B 上安全禁用、无收益、无错误共享——E8.4 实证）
2. **路线 B 当前 NO_GO**（机制性不可行 + checkpoint 路径前置条件缺失）
3. **路线 A 为务实路径**（attention-only 目标），但**范围变化需用户明确确认**——本报告不擅自重定义项目目标
4. 不将本可行性分析写成"已优化 Qwen3.5-4B"

## 6. 复现

```bash
# hybrid seq_cp tail 共享语义
sed -n '235,275p' src/llama-memory-recurrent.cpp
# hybrid state 序列化覆盖 attn+recr
sed -n '201,213p' src/llama-memory-hybrid.cpp
```
