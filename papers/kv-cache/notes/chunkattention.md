# ChunkAttention: Efficient Self-Attention with Prefix-Aware KV Cache and Two-Phase Partition

- **arXiv**: 2402.15220 | **Venue**: arXiv preprint（VENUE_UNVERIFIED）| 代码 https://github.com/microsoft/chunk-attention
- **作者**: Lu Ye, Ze Tao, Yong Huang, Yang Li（Microsoft）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 7201aa42e0f0020c

## 核心思想
多租户 LLM serving 中大量请求共享长 system prompt（ChatGPT 插件可达 1766 tokens）。把整体 KV tensor 沿序列维切成 chunk，组织为前缀树（prefix tree），运行时自动检测共享前缀并去冗余（零内存浪费）；two-phase partition（TPP）自注意力 kernel 提升数据局部性。

## 关键数据结构与算法
- PAKV：每个树节点 = 一个 chunk C（c 个 token 的 key/value 切片）；insert/delete/append 三操作
- pool-based 分配器（used/free chunk 列表，不释放回 OS）；对齐浪费上界 (c−1)/n
- TPP：chunk-first 阶段对共享 chunk 批量 partial_attn（在线 softmax）；sequence-first 阶段对独有 chunk 聚合
- lazy copy + kernel 重叠隐藏开销

## 论文关键数据
- 默认 chunk size c=64、head dim d=128、batch 32
- ChunkAttn 比 PagedAttn* 快 2.8–3.2×（ns=1024~4096）；ns=0 无回归
- 端到端：KV cache 内存减 70%–90%；peak batch 减 20%–40%；吞吐提升 1.6×–2.3×

## 限制
- 共享 prompt 必须序列开头（否则 PAKV 无效）；需 iteration-based batching；TPP 手写 CUDA 需逐个配置调优

## 训练/CUDA
无需训练；需要自定义 CUDA kernel（TPP 两阶段 attention + 在线 softmax）

## llama.cpp 映射
- cell 已具备一 cell 多 seq 元数据共享（bitset）；`find_slot` 前缀匹配被注释禁用 → 相同前缀重复写入
- 无树/hash 结构做共享前缀检索；共享只由 seq_cp 显式产生从不自动合并
- **候选判断**: 简化版（前缀感知 KV 去重：恢复 find_slot 前缀匹配 + token-hash/前缀表）可实现；完整版（含 TPP kernel）REJECT_INCOMPATIBLE_ARCHITECTURE
