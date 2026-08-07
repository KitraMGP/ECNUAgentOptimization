# PyramidKV: Dynamic KV Cache Compression based on Pyramidal Information Funneling

- **arXiv**: 2406.02069 | **Venue**: arXiv preprint（VENUE_UNVERIFIED）| 代码 https://github.com/Zefan-Cai/PyramidKV
- **作者**: Zefan Cai, Yichi Zhang, Bofei Gao, Yuliang Liu, Yucheng Li, Tianyu Liu, Keming Lu, Wayne Xiong, Yue Dong, Junjie Hu
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 0bcd6c0fba461e76

## 核心思想
"Pyramidal Information Funneling"：低层注意力全局分散，中层局部化，高层集中于少数关键 token（massive attention 主要在高层）。据此按层金字塔式分配 KV 预算（低层多、高层少）；层内选 token 用 SnapKV 式 instruction token 注意力得分。

## 关键数据结构与算法
- 预算分配：保留最后 α 个 instruction token 在所有层；剩余预算 k_total 按算术序列分配（顶层 k_total/(β·m)，底层 2·k_total/m − 顶层）
- KV 选择：每层每 head 用 instruction token 注意力求和打分 + pooling 防 massive activation 误导，取 top-k_l
- 删 token 后不改 rotary embedding（保持原始位置）

## 论文关键数据
- 12% KV（2048）匹配 full KV 性能；0.7% KV（64）TREC 比基线高 20.5 绝对准确率
- 默认 β=20、α=8
- Table 2 内存：cache 512→428M（6.3%）；2048→1712M（25%）
- Needle：LLaMA-3-70B + 仅 128 KV + 8k ctx 达 100.0 Acc

## 限制
- 仅 3 个模型、仅英文；少量饱和任务略逊基线；vLLM naive 实现有内存碎片问题需 per-layer paging

## 训练/CUDA
不需要训练（post-hoc 压缩）；无自定义 CUDA kernel 要求（torch.gather）

## llama.cpp 映射
- **有利**: 论文保留原始 rotary 位置与 llama.cpp 连续 KV 天然兼容（pos 存 cell）
- 所需：导出 instruction-token 注意力分数 + 逐层 token 保留集寻址
- **候选判断**: 可大致实现（P2 有损方向），工作量中等偏大；可退化为各层统一保留集 + 层间预算分配简化
