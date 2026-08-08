# E9.7：文献 metadata 最终修正（Literature Final Correction）

- 复核时间：2026-08-08
- 焦点：LM-Infinite 正式发表状态（E8.1 保持 VENUE_UNVERIFIED 的最后残留）

## 1. LM-Infinite 核验结果

| 项 | 值 |
|---|---|
| arXiv 版（manifest 原有）| "LM-Infinite: Simple On-the-Fly Length Generalization for Large Language Models"（arXiv:2308.16137，2023-08-30）|
| **正式版（发现）** | "LM-Infinite: Zero-Shot Extreme Length Generalization for Large Language Models"——**NAACL 2024**，ACL Anthology `2024.naacl-long.222`（han-etal-2024-lm），DOI `10.18653/v1/2024.naacl-long.222` |
| 证据来源 | **ACL Anthology 正式 proceedings**（一级来源）+ .bib 官方条目 |
| 同一工作确认 | 同作者（Han, Chi; Wang, Qifan; Xiong, Wenhan; ...）+ 正文 "we propose LM-Infinite, a simple and effective method for enhancing LLMs' capabilities of handling long contexts" |

**修正：`VENUE_UNVERIFIED` → `NAACL 2024`**

## 2. E8 漏检原因（如实说明）

E8.1 只检索了 **arXiv 版标题**（"Simple On-the-Fly Length Generalization"）的 ICLR/OpenReview 路径（OpenReview decision 被验证墙挡住），**未检索 ACL Anthology**。正式版**标题不同**（"Zero-Shot Extreme Length Generalization"）→ 按 arXiv 标题搜索漏检。E9.7 改为按作者 + 方法名（LM-Infinite）在 ACL Anthology 检索 → 命中。

## 3. 修正文件

- `papers/kv-cache/manifest.json`：lminfinite venue → NAACL 2024 + evidence + correction 说明 + preprint 关系
- `papers/kv-cache/references.bib`：`@misc` → `@inproceedings`（NAACL 2024、DOI、正式标题）
- 综述/笔记：随 manifest 一致（历史文本保留，修正说明在 manifest venue_correction）

## 4. 影响评估

- **不改变路线选择**：LM-Infinite（λ-mask + distance ceiling）在 P2-G 路线矩阵中为 DEFERRED（C2 的 sibling 参考），正式 venue 确认不影响 C1/C2 实现与结论
- 文献 metadata 至此**无残留 UNVERIFIED 的"已发表但未确认"条目**（8 篇 preprint 保持 UNVERIFIED 是诚实标注——它们确实是 preprint）

## 5. 最终文献状态

```text
LITERATURE_METADATA_STATUS: CORRECTED（E9.7 关闭最后残留）
```
