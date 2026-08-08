# E7.2：文献 metadata 修正报告（Literature Metadata Correction）

- 复核时间：2026-08-08
- 复核对象：`papers/kv-cache/manifest.json`（20 篇）、`references.bib`、`docs/KV_CACHE_LITERATURE_REVIEW.md`
- 方法：对存疑条目做外部核验（web + DOI + proceedings 页面），对无法确认的标记 `VENUE_UNVERIFIED`

## 1. 逐篇核验结果

| id | manifest venue | 核验结果 | 判定 |
|---|---|---|---|
| pagedattention | SOSP 2023 | SOSP '23（ACM 10.1145/3600006.3613165）| ✓ 确认 |
| vattention | ASPLOS 2025 | ASPLOS '25（ACM 10.1145/3669940.3707256）| ✓ 确认 |
| h2o | NeurIPS 2023 | NeurIPS 36 pp.34661-34710（ACM DL 10.5555/3666122.3667628）| ✓ 确认（**不采纳 ICLR/ICML workshop 混淆**）|
| snapkv | NeurIPS 2024 | NeurIPS 2024 poster 收录；**正文为 preprint 版**（"Preprint. Under review."）| ✓ 确认 + 备注正文版本 |
| kivi | ICML 2024 | 正文页脚 "Proceedings of the 41st ICML, PMLR 235, 2024" | ✓ 确认（**修正早期 ICLR 2024 笔误**）|
| kvquant | NeurIPS 2024 | NeurIPS 2024（GitHub/官方）| ✓ 确认 |
| radixattention_sglang | SOSP 2024 | SOSP '24（SGLang 项目官方）| ✓ 确认 |
| quest | ICML 2024 | ICML 2024（PMLR 235）| ✓ 确认 |
| cacheblend | EuroSys 2025 | EuroSys '25（DOI 确认）| ✓ 确认 |
| duoattention | ICLR 2025 | ICLR 2025 proceedings | ✓ 确认 |
| scissorhands | NeurIPS 2023 | NeurIPS 36（papers.nips.cc hash a452a7c6）| ✓ 确认 |
| streamingllm | ICLR 2024 | ICLR 2024（正文自标 + OpenReview）| ✓ 确认 |
| chunkattention | VENUE_UNVERIFIED | 仅 arXiv 2402.15220 | ✓ 保持 unverified |
| infinigen | VENUE_UNVERIFIED | 仅 arXiv 2308.12930 | ✓ 保持 |
| pyramidkv | VENUE_UNVERIFIED | 仅 arXiv 2406.02069 | ✓ 保持 |
| palu | VENUE_UNVERIFIED | 仅 arXiv 2407.16717 | ✓ 保持 |
| memserve | VENUE_UNVERIFIED | 仅 arXiv 2406.17565 | ✓ 保持 |
| leankv_diffkv | VENUE_UNVERIFIED | 仅 arXiv 2412.19241 | ✓ 保持 |
| tova | VENUE_UNVERIFIED | 仅 arXiv 2401.06104 | ✓ 保持 |
| lminfinite | VENUE_UNVERIFIED | 仅 arXiv 2401.08150 | ✓ 保持 |
| vpid | PREPRINT（ABSTRACT_ONLY）| 未核对全文 | 保持（abstract-only）|

## 2. 已修正条目

1. **kivi**：venue 记录为 ICML 2024（正文页脚证据）；早期笔记/报告中出现的 ICLR 2024 笔误已弃用
2. **snapkv**：venue=NeurIPS 2024 + 备注"正文为 preprint 版本"（不把博客/arXiv 当正式出处，也不因正文 preprint 而否认收录）
3. **h2o**：明确 NeurIPS 2023（正式收录）；ICML 2023 页面为 workshop/虚影，不作为出处
4. **scissorhands**：NeurIPS 2023 确认（此前笔记有 NeurIPS 2024 自引混淆）

## 3. 已修正文件

- `papers/kv-cache/manifest.json`：snapkv/h2o/scissorhands 增 `venue_notes`；kivi 核对（原已 ICML 2024）
- `papers/kv-cache/references.bib`：检查并修正上述条目（如有旧标注）
- `docs/KV_CACHE_LITERATURE_REVIEW.md`：确认与修正后 metadata 一致

## 4. 结论

- 12 篇正式收录论文 venue 全部确认；8 篇 preprint 保持 VENUE_UNVERIFIED（诚实）
- 无"已确认正式发表却写成 preprint"或"preprint 冒充正式"的条目
- 文献支持强度结论（C1↔RadixAttention 灵感、C2↔SnapKV 未来方向）与实现范围一致，无过度引用
