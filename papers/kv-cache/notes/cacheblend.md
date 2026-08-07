# CacheBlend: Fast Large Language Model Serving for RAG with Cached Knowledge Fusion

- **arXiv**: 2405.16444 | **Venue**: EuroSys 2025（DOI 10.1145/3689031.3696098）| 代码 https://github.com/LMCache/LMCache
- **作者**: Jiayi Yao, Hanchen Li, Yuhan Liu, Siddhant Ray, Yihua Cheng, Qizheng Zhang, Kuntai Du, Shan Lu, Junchen Jiang（U. Chicago/CUHK-Shenzhen/Stanford/MSR）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 034b9f230930f20e

## 核心思想
RAG 输入含多个文本 chunk，只有第一个是前缀（prefix caching 只省第一个 chunk）。full KV reuse 忽略 chunk 间 cross-attention → 质量差。CacheBlend 提出 selective KV recompute：每层只重算一小部分 token（HKVD，high-KV-deviation tokens），其余复用预计算 KV，逐层流水化后以不增加 TTFT 的代价获得与 full prefill 相同质量。

## 关键数据结构与算法
- HKVD 选择：第 1 层比较"新算 vs 加载 KV"偏差选 top；后续层 gradual filtering（在子集内再过滤）
- partial prefill：mask 只保留选中 token 算 Q/K/V，用预计算 KV 扩展未选中 token
- RoPE 位置校正：每 chunk K 乘旋转矩阵（附录 A 证明 RoPE 只依赖相对位置）
- 加载控制器：选 r% 使 T_recompute ≈ T_load，取 max(r*, 15%)
- KV 存储：按 chunk 文本 hash，CPU RAM/SSD 单层存储，LRU 淘汰

## 论文关键数据
- TTFT 降低 2.2–3.3×（vs full recompute 与 prefix caching）；吞吐提升 2.8–5×
- 质量 vs full reuse 改善 0.15–0.35（F1/Rouge-L）；vs full recompute 损失 ≤0.01–0.03
- 敏感分析：5%–18% 重算比例质量损失 ≤0.002
- 默认 r*=15%；RAG chunk 512 tokens

## 限制
- 仅 transformer；需额外 KV 存储基础设施；依赖 KV deviation/注意力稀疏性；MapReduce 范式无此问题

## 训练/CUDA
无需训练、无需新 CUDA kernel（mask 实现 partial prefill，RoPE 校正为矩阵乘法）

## llama.cpp 映射
- **契合度高**：`cpy_k/cpy_v + idxs` 可在任意 cell 写入 KV；`seq_add(p0,p1,shift)`/K-shift 提供 RoPE 位置校正；`state_write/read` 可作为 chunk 级 KV 存储导出通道
- 缺口：无 chunk 级 KV 存储 + hash 索引；图构建需"仅算选中 token KV"路径；非连续 cell 的 attention 处理（主要障碍）
- **候选判断**: 可实现（中期候选，P1/P3 方向），工程可行性与收益/成本比最高
