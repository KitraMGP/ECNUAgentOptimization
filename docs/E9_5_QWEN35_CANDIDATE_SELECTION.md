# E9.5：Qwen3.5-4B 主模型候选选择（Candidate Selection）

- 复核时间：2026-08-08
- 评分公式：`score = 0.25 gain + 0.25 correctness + 0.20 compatibility + 0.15 feasibility + 0.15 engineering_cost`（每项 0-5，越高越好；cost 5 = 低成本）
- 依据：E9.4 内存组成实测（32KB/token、recurrent 固定、q8_0 可用）+ E8.7 审计 + 上游对比

## 1. 候选评分

### H1：hybrid attention-side KV type/precision 优化
| 维度 | 评分 | 依据 |
|---|---|---|
| gain | 5 | q8_0 实测 KV bytes -46.9%（32768→17408 B/cell），同容量 KV 翻倍潜力 |
| correctness | 5 | 实测输出 hash 与 F16 完全一致（39a0f0fc...）|
| compatibility | 4 | `--cache-type-k/v` 在 4B 上可用（server 启动 + 请求正常）|
| feasibility | 5 | 现有开关直接可用（零新代码）|
| engineering_cost | 5 | 零成本（配置级）|
| **score** | **4.85** | **SELECT（但为上游既有功能——作 comparator，不作新代码创新）** |

### H2：hybrid prefix checkpoint restore
| 维度 | 评分 | 依据 |
|---|---|---|
| gain | 4 | 避免重复 prefill（agent 多轮分支）|
| correctness | 5 | state_write/read 覆盖完整 attn+recr（E8.7 审计）→ 无损可行 |
| compatibility | 3 | server checkpoint 机制已有，需前缀对齐 + 跨 slot |
| feasibility | 2 | 4 个必要条件缺失（E8.7：对齐/所有权/并发/成本）|
| engineering_cost | 2 | 中等-大工程 |
| **score** | **3.45** | **HOLD（前置条件缺失，需独立工程）** |

### H3：recurrent state 精度/布局优化
| 维度 | 评分 | 依据 |
|---|---|---|
| gain | 1 | recurrent 固定且小（E9.4）|
| correctness | 3 | 数值敏感，需质量门禁 |
| compatibility | 3 | — |
| feasibility | 2 | 需确认数值敏感性 + 恢复精度 |
| engineering_cost | 2 | — |
| **score** | **2.20** | **REJECT（收益有限）** |

### H4：hybrid slot clone/fork（checkpoint reuse）
| 维度 | 评分 | 依据 |
|---|---|---|
| gain | 4 | 避免重复 prefill（同 H2）；copy 54MB vs prefill 280ms（E9.4 估算 copy 更优）|
| correctness | 4 | state 数据复制精确 → 无损（需处理 recurrent，state_read/write 覆盖）|
| compatibility | 3 | server slot 层可加（上游 clone_to 思路，PR #26204 未合并）|
| feasibility | 3 | 单 source/single target 最小 prototype 可行（state_seq_get/set_data）|
| engineering_cost | 3 | 最小原型中等 |
| **score** | **3.55** | **SELECT（新代码最小 prototype）** |

## 2. 选择结论

| 候选 | 状态 | 角色 |
|---|---|---|
| H1（KV 量化）| **SELECT（comparator）** | 4.85 分，但为上游既有开关；作为 baseline/comparator 与 H4 对比，不作新代码 |
| H4（slot clone/fork）| **SELECT（新代码 prototype）** | 3.55 分，真正的新代码最小原型（E9.7 实现）|
| H2（checkpoint restore）| HOLD | 前置条件缺失 |
| H3（recurrent 精度）| REJECT | 收益有限 |

## 3. E9.7 prototype 定义（H4）

- **功能**：跨 slot 完整 state clone（`/slots/{dst}?action=clone_from&source_id={src}` 或内部路径）——把 source slot 的完整状态（attention KV + recurrent state，经 `llama_state_seq_get_data/set_data`）复制到 target slot，target 从克隆状态继续（追加不同后缀）
- **无损契约**：克隆后生成与独立 prefill 完全一致（token ID 相同）；默认 off（实验参数）
- **测量**：copy bytes、copy 耗时 vs 重复 prefill 耗时、输出 hash、cancel/retry/cleanup
- **rollback**：参数关闭回到基线；任何输出不一致 → 关闭
