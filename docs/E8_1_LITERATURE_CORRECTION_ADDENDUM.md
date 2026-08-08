# E8.1：文献 metadata 修正增补（Literature Correction Addendum）

- 复核时间：2026-08-08
- 方法：正式来源优先（会议 proceedings / ACM DL / ACL Anthology / USENIX / NeurIPS virtual / DOI），arXiv 仅作版本补充；不确定 → VENUE_UNVERIFIED
- 历史 E7.2 保留；本报告逐项记录 E7 声称 → E8 核验

## 1. 逐项核验（12 篇重点）

| id | E7 声称 | E8 核验结果 | 证据来源类型 | 修正内容 | 影响路线？ |
|---|---|---|---|---|---|
| radixattention_sglang | SOSP 2024 | **NeurIPS 2024**（NeurIPS 37, Vancouver）| ACM DL 10.5555/3737916.3739916（38th NeurIPS）；stanford theory 页；NeurIPS virtual poster 94872 | venue SOSP→**NeurIPS 2024**；bib 同步 | 否（C1 受其启发的事实不变，仅引用 venue 修正）|
| chunkattention | VENUE_UNVERIFIED | **ACL 2024 Long Papers** | ACL Anthology 2024.acl-long.623；DOI 10.18653/v1/2024.acl-long.623；arXiv comments "ACL 2024" | venue→**ACL 2024**；@misc→@inproceedings | 否 |
| infinigen | VENUE_UNVERIFIED | **OSDI 2024** | USENIX OSDI 2024 官方 PDF（usenix.org/system/files/osdi24-lee.pdf）| venue→**OSDI 2024**；@misc→@inproceedings | 否 |
| lminfinite | VENUE_UNVERIFIED | **保持 VENUE_UNVERIFIED**（ICLR 2024 OpenReview forum pOujzgHIRY 存在，decision 不可读；arXiv v7 无 journal-ref）| OpenReview（验证墙）、arXiv | 无（加 evidence 说明）| 否 |
| h2o | NeurIPS 2023 | **确认** NeurIPS 2023（NeurIPS 36 pp.34661-34710）| ACM DL 10.5555/3666122.3667628 | 无 | 否 |
| kivi | ICML 2024 | **确认** ICML 2024（PMLR 235）| 论文正文页脚（一级印刷源）| 无 | 否 |
| snapkv | NeurIPS 2024 | **确认** NeurIPS 2024 poster；正文为 preprint 版 | NeurIPS poster 93531；arXiv 正文标注 | 保持 + preprint 备注 | 否 |
| scissorhands | NeurIPS 2023 | **确认**（papers.nips.cc hash a452a7c6；NeurIPS 36）| NeurIPS 官方页面 | 无 | 否 |
| cacheblend | EuroSys 2025 | **确认** | DOI（ACM EuroSys '25）| 无 | 否 |
| duoattention | ICLR 2025 | **确认** | ICLR 2025 proceedings | 无 | 否 |
| streamingllm | ICLR 2024 | **确认** | 论文正文标注 + OpenReview | 无 | 否 |
| quest | ICML 2024 | **确认** | PMLR 235 | 无 | 否 |

## 2. 关键修正详情

**SGLang/RadixAttention（重要）**：E7.2 曾标 SOSP 2024——错误。SGLang 主论文发表于 **NeurIPS 2024**（poster 94872，ACM DL 10.5555/3737916.3739916，stanford theory 页 NeurIPS 37）。此前无一级来源支持 SOSP 标注。已修正 manifest + references.bib。

**ChunkAttention**：ACL Anthology 2024.acl-long.623（DOI 10.18653/v1/2024.acl-long.623）确认 ACL 2024 Long Papers。

**InfiniGen**：USENIX OSDI 2024 官方 PDF（osdi24-lee.pdf）确认。

**LM-Infinite**：OpenReview forum pOujzgHIRY（ICLR 2024 submission）存在但被验证墙挡住无法读 decision；arXiv 无 journal-ref → 诚实保持 VENUE_UNVERIFIED。

## 3. 综述中"C1/C2 受论文启发"描述与代码范围一致性

- C1 ↔ RadixAttention：一致（C1 是"受 RadixAttention 启发的跨 slot 前缀共享，线性 LCP + seq_cp 元数据，非 radix tree 实现"——报告已按 E7.6 名称修正；RadixAttention 引用 venue 已修正）
- C2 ↔ SnapKV：一致（C2 已降级为容量估算，SnapKV 仅为未来方向参考，无过度引用）
- 其余论文为 route matrix 评估，与实现无直接启发关系描述

## 4. 已修正文件

- `papers/kv-cache/manifest.json`：4 处（sglang venue + evidence + correction；chunkattention/infinigen venue；lminfinite evidence）
- `papers/kv-cache/references.bib`：3 处（sglang booktitle/note；chunkattention/infinigen @misc→@inproceedings）
- 论文笔记与综述：随 manifest 一致（未改动历史文本，新增修正说明）

## 5. 结论

- 12 篇重点论文：10 篇确认正式 venue（含本次新确认 3 篇）、1 篇保持 UNVERIFIED（LM-Infinite）、1 篇修正（SGLang）
- 无"已确认发表写成 preprint"或"preprint 冒充正式"；无被证伪 venue 残留
- 不改变 C1/C2 路线选择
