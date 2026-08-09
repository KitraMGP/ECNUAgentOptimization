# E15.4（CORE 草稿）：C 线上下文策略独立核心与纯函数测试

> 状态：**DRAFT**（本阶段仅交付独立核心 + 纯函数测试；未集成 workload、未跑真实模型 paired）
> 日期：2026-08-08 ｜ 对应计划：`docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` 技术线 C（E15.4）
> 提交状态（2026-08-08 收口）：已提交，根仓库 commit `c9c26ab`（核心 + 测试 + 本文档）。
> 约束执行：只新增 `benchmark/framework/context_policy.py`、`benchmark/tests/test_context_policy.py` 与本文档；
> 未修改 tool_call.py / long_life.py / config.py / runner.py / driver.py / workload.py / 现有测试 / llama.cpp；
> 未安装依赖、未跑全量测试/格式化。
> 并行任务隔离：本核心不触碰 ToolPayloadStore（并行任务产物）涉及的任何文件与命名空间。
> **reviewer 修订（2026-08-08）**：本版按 reviewer 9 项意见修订——summary 注入位置、manifest
> 明文泄露、summary 块截断语义（完整行/闭合标签/needle 不截断）、apply_policy 幂等、snapshot
> 深拷贝、append_message 校验与 seq、预算 note、注入后序列验证；见 §2.3/§2.5/§2.6 与 §4。
> **reviewer 二修（2026-08-08）**：E15.4 reviewer 8 项发现全部修复——ThreadSnapshot 保存并
> restore `_fact_message_ids`（trim 保护经 snapshot→purge→restore 后仍生效）；`keep_recent_rounds`
> 与 `min_user_queries` 矛盾配置构造即 `BudgetError`；projection 行按**连续前缀**容纳（无中间
> 空洞）；fact 文本换行在渲染层**单行化**（`summary_block_chars` 严格成立）；snapshot round-trip
> 完整恢复 manifest/errors/seq；`error_budget` 负值 fail-fast；`append_message` id 语义明确；
> manifest 唯一序列化出口 `to_dict()`（slots + 隐藏明文 repr）；见 §2.1/§2.3/§2.5/§2.6 与 §4。

---

## 1. 改动清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `benchmark/framework/context_policy.py` | 新增 | C 线上下文策略核心（纯 Python stdlib，无外部依赖） |
| `benchmark/tests/test_context_policy.py` | 新增 | 68 个纯函数单元测试（无 GPU/真实 server） |
| `docs/E15_4_C_CONTEXT_POLICY_CORE_DRAFT.md` | 新增 | 本报告草稿（含两轮 reviewer 修订记录） |

## 2. 公共接口

### 2.1 策略与配置

- `ContextPolicy`（`str, Enum`）：`full`（默认，= 现状）｜`trim`｜`extractive_summary`。
- `ContextPolicyConfig`：独立配置对象（**本阶段不改 config.py**），字段：
  `budget_chars` / `budget_tokens`（token 预算经 `chars_per_token` 折算）/ `keep_recent_rounds`
  （history 保留最近 N 轮，默认 4）/ `min_user_queries`（≥1 真实 user 提问，默认 1）/
  `max_facts` / `summary_block_chars` / `error_budget`；
- **预算 fail-fast（reviewer 二修）**：非法预算（负数）抛 `BudgetError`；`error_budget` 为负
  同样抛 `BudgetError`；**矛盾配置** `min_user_queries > keep_recent_rounds`（每轮以真实 user
  提问开头，保留全部轮次也最多只有 `keep_recent_rounds` 个真实 user 提问，无法满足下限）
  构造即抛 `BudgetError`——trim/summary 永远不会静默违背 min_user_queries。

### 2.2 消息模型与四级分类

- `Message`：不可变；`id` 由（序号, role, content）确定性派生，`hash` = sha256(content)。
- `MessageCategory`：`system`（L1）/ `tool_schema`（L2）/ `fact`（L3）/ `history`（L4）/
  `summary`（应用层注入块）。`classify_message(role, content)` 纯函数：system 内容含工具协议
  特征（`ACTION:` 等，即 TOOL_SYSTEM 形态）→ L2，其余 system → L1。
- `Fact`：稳定 `id`（`fact-` + text hash 前 16 位）、`hash`、`kind`
  （`registered` / `secret_declaration` / `notable_statement`）、`source_message_ids`；同 text 去重。
- `ToolProjection`：被裁历史中的 ACTION 调用（工具名/参数原文子串）+ 最近 `tool_response`
  的引用 hash（不复制结果全文）。

### 2.3 ThreadState（thread-scoped，需求 2/6）

- `ThreadState(thread_id, config)`；`append(role, content)` / `append_message(msg)`（**复用
  append 的全部校验**：role 合法 / content 非空 / system 位置，并正确推进 seq 防 id 冲突；
  **id 语义（reviewer 二修）**：返回消息的 id 由 `(thread 内当前序号, role, content)` 确定性
  **重新派生**、**不继承** 传入 `msg.id`——外部序号/其他 thread 的 id 不进入本 thread，仅当
  传入 index 恰等于当前 `_seq` 时派生 id 才与 `msg.id` 相同）/
  `load_messages(dicts)`（批量导入，不改写 workload 代码）；`register_fact(text, source_message_id)`；
  `apply_policy()` → `PolicyResult(messages, manifest)`（**幂等**：不修改消息/seq/facts，
  重复调用输出 hash 与 ids 稳定）；`snapshot()` / `restore(snapshot)` / `purge()`；`describe()`。
- **snapshot 完整 round-trip（reviewer 二修）**：`ThreadSnapshot` 保存 messages / facts /
  summary_blocks / **`fact_message_ids`**（trim 的 fact 消息保护索引）/ **errors（完整错误列表，
  不再只存计数）** / **seq** / **manifest**，`restore` 全部恢复（含 purge 之后）——例如
  注册了 `source_message_id` 的 fact 消息，经 snapshot → purge → restore 后 trim 仍保护原文；
  可变结构（SummaryBlock / manifest）两侧均深拷贝，快照与 thread 当前状态互不共享。
- 隔离与异常：`IllegalMessageError`（未知 role / 空 content / system 非头部，append 永远
  fail-fast 并计数）；`CrossThreadRestoreError`（跨 thread restore）；`ErrorBudgetExceeded`
  （批量导入错误数超 `error_budget`）；`BudgetError`（预算非法，含矛盾配置与负值）。

### 2.4 trim（需求 3）

- 只裁 L4 history 中最旧轮次；L1/L2 与 L3 fact 消息永不删；latest 真实 user 提问保留；
  预算（字符/token）不足时继续裁更旧轮，下限为 `min_user_queries` 个真实 user 轮；
  在 turn 边界裁剪 → 角色序列合法（`validate_role_sequence` 校验）。
- **lossless=False**：输入 token 改变，greedy 输出一致性未验证（不伪称）。

### 2.5 extractive_summary（需求 4，不调用 LLM）

- 被裁历史 → 明确事实投影（注册 Fact + 内置"秘密数字/记住类"模式，提取文本必须是原文
  **精确子串**，非子串一律丢弃 → 不编造）+ 工具投影（ACTION 原文 + tool_response 引用 hash）；
- 稳定顺序、同 text 去重、`max_facts` / `summary_block_chars` 有界；`SummaryBlock` 记录
  `covered_message_ids` 与 `covered_hash`（可审计），渲染为带 `<summary_block>` 标记的
  system 消息注入输出；
- **注入位置（reviewer 修订）**：summary 消息插入到**所有** L1 system / L2 tool_schema
  （及既有 SUMMARY）之后、第一个历史消息之前——绝不放在 system 前；注入后的 role sequence
  再次验证，失败抛 `IllegalMessageError`；
- **截断语义（reviewer 修订）**：`summary_block_chars` 按**完整行**截断（绝不拆分 fact/
  projection 行），`</summary_block>` 闭合标签**必须保留**；facts（needle 载体）为 protected，
  **全部必须完整保留**——若预算连 header + 全部 fact 行 + 闭合标签都无法容纳 → 抛显式
  `BudgetError`（不静默截断 needle）；仅 tool projections 行可被丢弃并置 `truncated=True`；
- **projection 连续前缀（reviewer 二修）**：tool projections 行按**连续前缀**从前往后容纳，
  一旦某行放不下立即停止（break）——后面的短行绝不越过前面的长行被单独保留（无中间空洞）；
  放不下的行整体丢弃并置 `truncated=True`；
- **渲染单行化（reviewer 二修）**：fact 文本 / 工具名 / 参数含换行时，在渲染层替换为单个空格
  （`_one_line`，长度不增）——每个 fact/projection 在渲染中恰好占一行，"完整行截断"语义与
  `summary_block_chars` 字符预算严格成立（`_lines_len_with_closing` 对任意行内容计费精确）；
  `fact.text` 对象本身保持原文（精确子串不变式不被破坏），单行化只作用于渲染视图；
- **lossless=False 恒成立**：有损候选，不伪称可从摘要恢复全部原文；块开销大于被裁内容时
  压缩率如实为负并在 notes 说明（不美化）。

### 2.6 CompressionManifest（需求 5）

- 字段：`policy` / `version` / `input_hash` / `output_hash` / `kept_ids` / `trimmed_ids` /
  `protected_ids` / `protected_byte_spans`（输出消息内 UTF-8 字节区间）/ `summary_blocks` /
  `lossless` / `reversible` / `notes` / `stats`；`to_dict()` 可序列化审计。
- 原文内容不进 manifest；summary 无 restore 伪称。
- **`to_dict()` 明文控制（reviewer 修订）**：**不输出任何原文内容**（含 fact 明文）——
  summary_blocks 中的 facts 仅以 `{id, hash, kind, source_ids}` 呈现（可审计、不可从
  manifest 还原原始消息）；kept/trimmed/protected 均为消息 id。
- **唯一序列化出口（reviewer 二修）**：`to_dict()` 是 manifest **唯一受支持的序列化出口**；
  manifest 为 `slots` dataclass（无 `__dict__`，`vars()` / `__dict__` 访问直接抛错），
  `__repr__`（manifest 与 SummaryBlock）只显示概要、隐藏 fact 明文与 rendered 全文——
  调试日志/print 路径不泄露原文；内存态字段（如 `summary_blocks[].facts[].text`）含明文
  事实供审计（受控访问），`vars()` / `dataclasses.asdict()` / `json.dumps(obj)` 等直接
  dump 路径**不在契约内**（可能泄漏明文），一律使用 `to_dict()`。

### 2.7 DuplicateBlockCompressor（需求 5 后半，与 summary 严格分开）

- deterministic reversible preprocessor：只对**完全重复块**做 dictionary/ref 去重
  （第二次出现 → `\x00REF:<block_id>\x00`），`restore` round-trip 还原原文；
  原文 `\x00` 转义保证严格可逆；缺字典显式异常。
- 独立类/独立 manifest 语义：可 round-trip 还原，但**不声称** greedy 输出一致
  （BPE 边界风险，E12 教训），因此也不标 E15.0 lossless 门禁。

### 2.8 统计（需求 7）

`CompressionStats`：input/output chars、消息数、估算 tokens（`estimate_tokens`，确定性
chars/token 折算，非 BPE）、压缩率（可负）、`protected_preserved`、`summary_facts`。

## 3. 测试结果

```bash
cd benchmark && uv run pytest tests/test_context_policy.py -q
# 68 passed in 0.11s
```

覆盖（对照需求 8 + reviewer 9 项修订 + reviewer 二修 8 项发现）：

| 组 | 用例数 | 覆盖点 |
|---|---|---|
| full identity | 2 | 原样返回、lossless=True、ratio=0、不修改 state |
| 幂等（reviewer） | 1 | apply_policy 重复调用：消息/seq/facts 不变、输出 hash/ids 稳定、summary id 内容 hash 派生不推进 seq |
| trim 保护 | 4 | system/tool_schema/fact 消息/最新 user、tiny budget 下限 |
| 角色序列 | 3 | 多轮与 tool 场景裁剪后 `validate_role_sequence` 通过、相对顺序保持 |
| 预算 | 7 | 字符预算、token 预算折算、按轮裁剪、预算参数校验（含 error_budget 负值 / max_facts / summary_block_chars）、**矛盾配置 fail-fast（min_user_queries > keep_recent_rounds）**、summary 预算无法满足 note（与 trim 一致） |
| summary | 13 | 确定性、恒有损、不编造（fact 为原文子串）、去重、注册事实投影、有界、覆盖记录、工具投影、膨胀如实报告、**注入位置（L1/L2 之后）**、**极小预算 BudgetError / needle 完整性与闭合标签**、**projection 连续前缀（无中间空洞）**、**fact 含换行渲染单行化** |
| needle | 4 | early / middle / late 保留、窗口内原文保留 |
| 跨 thread 隔离 | 2 | 事实/消息不泄漏、facts 按 thread 作用域 |
| snapshot/restore/purge | 9 | round-trip、id 唯一、facts/summary 恢复、**fact_message_ids 恢复（trim 保护经 snapshot→purge→restore 后仍生效）**、**manifest/errors/seq 完整恢复**、**manifest 深拷贝隔离**、可变 SummaryBlock 深拷贝隔离、跨 thread restore 异常、purge 保留身份 |
| invalid fail-fast | 10 | 未知 role/空 content/system 位置、**append_message 复用校验**、**append_message 推进 seq 防 id 冲突**、**append_message id 重派生（不继承外部 id）**、error_budget=0 严格、预算超限、批量容忍、序列校验 |
| DuplicateBlockCompressor | 5 | round-trip、确定性、\x00 转义、缺字典异常、与 summary 分开 |
| 分类/统计/审计 | 8 | 分类、id/hash 稳定、stats 完整、protected byte spans、manifest 序列化、**manifest 不泄露 fact 明文/原文**、**manifest 唯一序列化出口（slots 阻止 vars/__dict__，repr 隐藏明文）** |

## 4. 设计决策记录

1. **trim 与 extractive_summary 的 L3 差异**：trim 保护 fact 消息原文；extractive_summary
   允许裁 fact 消息但把事实投影进 summary block（needle 保留）——符合"trim=删除语义、
   summary=替换语义"（LangGraph RemoveMessage vs summarize）。
2. **不编造约束落地**：事实提取器产出必须通过 `content.find(text) != -1` 子串校验，否则丢弃；
   工具投影只记录 ACTION 原文与结果引用 hash，不复制/改写 tool_response 内容。
3. **膨胀如实报告**：summary 块渲染大于被裁内容时 `compression_ratio` 为负并在 notes 声明，
   不美化收益（E15 原则）。
4. **错误预算语义**：`append()` 永远 fail-fast 抛原始异常并计数；`load_messages()` 批量导入
   容忍 `error_budget` 次错误，超限抛 `ErrorBudgetExceeded`——两者均为显式异常边界。
5. **估算 token 不冒充精确**：`estimate_tokens` 为确定性 chars/token 折算，注释与文档均声明
   非 BPE，避免 E12"文本看起来一样 ≠ token 一致"的教训在统计口径上重演。
6. **summary 注入位置**（reviewer）：注入在**所有** L1/L2（及既有 SUMMARY）之后、第一个历史
   消息之前——summary 是"历史替换产物"，不是 system 合同的一部分，绝不能插到 system 前。
7. **manifest 零明文**（reviewer）：`to_dict()` 的 facts 只输出 `{id, hash, kind, source_ids}`，
   保证审计可追踪（hash 对照）但不可从 manifest 还原原文；docstring 与行为一致。
8. **summary 块截断语义**（reviewer）：完整行截断 + 闭合标签必须保留；facts（needle）为
   protected 永不截断，预算不足时抛 `BudgetError` 显式信号（fail-fast，绝不静默丢 needle）；
   仅 tool projections 可被丢弃并标记 `truncated=True`。
9. **apply_policy 幂等**（reviewer）：不修改消息/`_seq`/facts；summary 消息 id 由渲染内容
   hash 派生（`summary-<hash16>`），重复调用输出 hash 与 ids 逐位稳定。
10. **snapshot 深拷贝**（reviewer）：`SummaryBlock` 为可变结构，snapshot/restore 两侧均深拷贝，
    快照与 thread 当前状态互不共享（Message/Fact 不可变，直接引用安全）。
11. **append_message 复用 append**（reviewer）：不绕过校验（role/content/system 位置），并正确
    推进 `_seq`，杜绝"绕过 seq 导致后续消息 id 冲突"的路径。
12. **矛盾配置 fail-fast**（reviewer 二修）：`min_user_queries > keep_recent_rounds` 在
    `ContextPolicyConfig.__post_init__` 抛 `BudgetError`——每轮（turn）以真实 user 提问开头，
    保留全部轮次也最多只有 `keep_recent_rounds` 个真实 user 提问，配置本身不可满足；与其在
    trim/summary 运行时静默违背下限，不如构造即显式失败。`error_budget` 负值同理。
13. **snapshot 完整 round-trip**（reviewer 二修）：快照保存并恢复 `_fact_message_ids`（trim 的
    fact 消息保护索引）、**完整 errors 列表**（不再"只存计数、restore 截断"）、`seq` 与
    `manifest`（含 summary 审计视图，深拷贝隔离）——"完整 round-trip"声明与实现一致；
    fact 消息保护经 snapshot → purge → restore 后仍生效。
14. **projection 连续前缀 + 渲染单行化**（reviewer 二修）：projection 行一旦某行放不下即 break，
    后面的短行绝不越过前面的长行被单独保留（无中间空洞）；fact/参数含换行时渲染层单行化
    （`_one_line`，长度不增），"完整行截断"与 `summary_block_chars` 字符预算严格成立，
    `fact.text` 对象保持原文（精确子串不变式不受影响）。
15. **manifest 唯一序列化出口**（reviewer 二修）：`CompressionManifest` 为 `slots` dataclass
    （无 `__dict__`，`vars()`/`__dict__` 直接抛错），`__repr__`（manifest 与 SummaryBlock）
    只显示概要——默认调试路径不泄露 fact 明文；`to_dict()` 是唯一受支持序列化出口（零明文）；
    `asdict()`/`json.dumps(obj)` 等直接 dump 不在契约内（内存态字段含明文供审计）。

## 5. 未验证边界（明确声明，不虚构）

- **真实模型 paired 未执行**：greedy 输出 token 一致性（12.10）、E15.0 §3.1 lossless 三条件
  均未用真实模型（Qwen3.5-4B / 0.8B）验证；本核心不 mock 模型输出证明质量。
- **E6 §12.12 有损门禁未执行**：tool JSON 100%、needle 每桶 ≤2pp、system retention 100%、
  bytes -25% 或 ctx/并发 +25% 等**全部未验证**——需要集成 workload 后跑 paired/离线 oracle。
- **long_life 对照未执行**：`--context-policy` CLI 接入、long_life full vs trim/summary 对照、
  跨 thread 隔离 needle 实测均属后续集成阶段。
- **prompt-cache identity 未触碰**：沿用应用层 thread-scoped state 方案，未修改 server cache
  identity（与 E15.0 §4.3 一致）。
- **与 ToolPayloadStore 的关系**：两者为并行独立核心；本模块的工具投影只做"描述/调用"层面
  的确定性记录，不做中间数据外置（中间数据优化在 E15.0 §5 明确排除/单独立项）。
- **非目标**：`full` 之外的策略默认关闭；4B hybrid 不因本模块产生任何 KV 层改动。

## 6. 后续步骤（非本阶段）

1. E15.4 集成：`long_life` / `tool_call` 接入 `ThreadState` + `--context-policy`（改 workload
   与 runner，需单独授权，本阶段不动）；
2. 真实模型 paired（同 seed/temp=0）与 12.12 门禁；
3. E15.5 有损摘要离线 oracle 对照（R-KV / PartPrompt / SnapKV 思想参考）。
