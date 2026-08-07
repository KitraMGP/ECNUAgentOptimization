# MemServe: Context Caching for Disaggregated LLM Serving with Elastic Memory Pool

- **arXiv**: 2406.17565 | **Venue**: arXiv preprint（VENUE_UNVERIFIED）
- **作者**: Cunchen Hu, Heyang Huang, Junhao Hu, Jiang Xu, Xusheng Chen, Tao Xie, Chenxi Wang, Sa Wang, Yungang Bao, Ninghui Sun, Yizhou Shan（Huawei Cloud/UCAS/ICT CAS/PKU）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: a3023cef82e7ba13

## 核心思想
将 LLM serving 的 KV 缓存与模型执行解耦（disaggregated inference）。MemPool 统一内存池 + MemServe 结合 context caching 与 disaggregated inference。prompt trees 结构化缓存（prefix 树共享），全局内存调度（成本模型选择缓存位置）。

## 关键数据结构与算法
- MemPool API（分配/释放/映射/转移）；prompt trees（结构化前缀缓存）
- 全局调度：成本模型（compute-bound/memory-bound/constant 算子 profile）
- 指标：TTFT、JCT、TPOT

## 论文关键数据
（子代理部分完成；核心数据待补——JCT/TTFT 提升 10%–85%）

## 限制
- 面向分布式 serving 架构（非单机）；需要跨节点 KV 共享基础设施

## 训练/CUDA
无需训练；少量布局适配（非核心）

## llama.cpp 映射
- 面向 disaggregated 架构（prefill/decode 分离 + 跨节点缓存），与 llama.cpp 单机服务模型异构
- **候选判断**: REJECT_INCOMPATIBLE_ARCHITECTURE（完整方案）；prompt trees 思想可借鉴到 server 层 prompt cache
