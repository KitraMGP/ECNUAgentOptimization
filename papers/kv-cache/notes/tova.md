# TOVA: Token Omission Via Attention

- **arXiv**: 2401.06104 | **Venue**: arXiv preprint（VENUE_UNVERIFIED）
- **作者**: 子代理读取内容（TOVA 论文）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: fe234eea437d5893

## 核心思想
解码时每步基于当前步 attention 分数丢弃最不重要的 token（token omission）。用 1/8 cache（4096→512）即达 full model 相当性能，对应吞吐 +4.8×、batch 提升近 9×、cache 压缩最多 88%。

## 关键数据
- 1/8 cache 达 full 相当性能
- 吞吐 +4.8×；batch 提升近 9×；cache 压缩最多 88%

## 限制
- 需要 token 级驱逐与 cache 内位置重映射（可能需稀疏 attention）；依赖每步 attention 分数

## llama.cpp 映射
- 需要 token 级驱逐 + cache 内位置重映射 + 每步 attention 分数
- **候选判断**: 高难度（llama.cpp 连续 cell 布局需重大改动）
