# SGLang: RadixAttention（Efficient Execution of Structured Language Model Programs）

- **arXiv**: 2312.07104 | **Venue**: SOSP 2024 | 代码 https://github.com/sgl-project/sglang
- **作者**: Lianmin Zheng, Ying Sheng 等（Stanford/UC Berkeley/SJTU/Texas A&M）
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: 161e0a0123a89cd8a57d68734ae704680b0e577fee41e1e7e1029c37a9d99b57

## 核心思想
LLM 推理视为语言模型程序（多个 LLM 调用+控制流+结构化输入输出）。RadixAttention：请求结束后不再丢弃 KV cache，把 "token 序列 → KV cache 张量" 映射放进 radix tree（压缩前缀树），支持前缀匹配/插入/分裂/淘汰。KV cache 用非连续分页布局，每页 1 token。LRU 淘汰：先淘汰最久未用叶子（ref=0 才可淘汰）。cache-aware scheduling：按已匹配前缀长度排序请求（最长共享前缀优先），离线最优定理（Theorem 3.1：DFS 顺序 + cache ≥ 最大请求长度 → 最优命中率）。

## 论文关键数据
- 吞吐最高 6.4×；延迟最高降低 3.7×；缓存命中率 50%–99%；cache-aware scheduling 达最优命中率 96%
- RadixAttention overhead < 0.3%（ShareGPT 100 请求树管理 0.2s/74.3s）
- 生产部署：LLaVA-NeXT-34B 命中率 52.4%、Vicuna-33B 74.1%、TTFT 平均降 1.7×

## 限制
- cache-aware scheduling 会饥饿（公平调度留作未来）；仅精确 token 前缀（无模糊匹配）；多轮长输出时几乎无加速；离线最优定理因输出长度不可预测被打破

## 训练/CUDA
不需要训练；RadixAttention 本身不需要改 kernel（树逻辑在 CPU）

## llama.cpp 映射
- 当前前缀匹配：server 层 `get_common_prefix`（token 级 LCP）逐 slot 扫描，无 trie/radix tree、无节点分裂/合并
- slot 与 seq 固定绑定（每 slot 一个 seq）；unified pool 所有 seq 共享 1 stream，靠 k_idxs/v_idxs gather/scatter 写入各自 cell
- cell 元数据已有 `seq` bitset（多 seq 关联同一 cell，metadata-level 非 COW）
- **gap**: 缺 token→cell trie 索引与分裂/合并；slot 生命周期按 slot 释放不按 token；无 token 级 LRU；无批内 DFS 排序
- **有利**: k_idxs/v_idxs 天然支持非连续 cell 写入；prompt cache（cache_ram）已是近似复用机制；radix 结构在 CPU 维护无需改 kernel
- **候选判断**: 可实现（工程量在 server/context 层，无需新 CUDA kernel）；风险：slot 与 seq 绑定粒度改造、LLAMA_MAX_SEQ bitset 上限、cache-aware 排序触碰 fairness
