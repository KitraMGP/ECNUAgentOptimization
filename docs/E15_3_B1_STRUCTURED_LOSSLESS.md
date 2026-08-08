# E15.3（B1）：deterministic structured-lossless prompt preprocessor

> 状态：**实现 + 单测 + 真实 4B greedy paired 门禁完成；无损门禁不成立 → `HOLD_NOT_VALIDATED`**，
> 按 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` §3.1 降级为**有损候选**，转 E15.5（B2 摘要
> oracle，12.12 门禁）。**不伪称 PASS**。
> 日期：2026-08-08 ｜ 对应计划：技术线 B 的 B1 候选（§3.1/§7/§8）。
> 提交状态：根仓库 commit（见本文件末）；llama.cpp **未改动、未提交**。
> 约束执行：只新增/修改 `benchmark/framework/prompt_preprocessor.py`、`benchmark/framework/driver.py`、
> `benchmark/runner/runner.py`（接入点）、`benchmark/tests/test_prompt_preprocessor.py`、
> `benchmark/runner/e15_3_b1_paired.py`（paired 门禁 runner）与本文档；未改 config.py /
> workload / context_policy / tool_payload / 既有 E14 文档 / llama.cpp；未安装依赖、未跑全量测试/格式化。
> 并行任务隔离：不触碰 context_policy（E15.4）与 tool_payload（E15.2）涉及的命名空间；preprocessor
> 为独立模块，仅 Driver/Runner 接入点与其交叉（最小改动）。

---

## 1. 需求映射（用户 E15.3 指令 → 实现）

| # | 需求 | 实现 |
|---|---|---|
| 1 | 独立可维护模块 + 消息模型/manifest/hash/区间/统计/restore + 确定性 | `benchmark/framework/prompt_preprocessor.py`（纯 stdlib）；`PreprocessorManifest`（slots，version=`1.0.0`，input/output hash，RefSpan/ProtectedSpan 替换/保留区间，PreprocessorStats，`to_dict()` 零明文）；同输入 → 逐字节相同输出 + 相同 manifest（测试验证） |
| 2 | 仅压缩完全重复的非安全非 protected 历史块；protected 逐字节保留；manifest 零明文；NUL/伪 ref/畸形 manifest/未知 ref/hash mismatch/跨 thread fail-fast；无净收益 identity | 消息级完全重复去重（块 = 单条消息 content）；`is_protected_message`（system/关键事实/tool payload 协议/注册 fact 文本）；manifest/dictionary 分离（manifest 零原文，原文仅存 thread-scoped `PreprocessorDictionary`）；异常体系全覆盖；**字节口径 + token 估算口径双净收益判定**（中文短块防膨胀，E12 教训）；`restore` 五步校验 |
| 3 | 显式 opt-in、默认 off 与旧行为一致；config.extra 惯例；发送前预处理；审计进结果不改 off 字段；Driver hook 安全性论证 | `config.extra["preprocessor"]`（默认 off，与 E15.2 `tool_payload_mode` 同一惯例；兼容 E15.0 CLI 名 `structured-lossless`）；Driver 单点（`driver.chat()` 发送前压缩，新列表零污染、幂等、400 重试安全）；`row["preprocessor"]` 仅 on 时出现；Driver hook 安全性见 §5 |
| 4 | 测试覆盖 identity/off、确定性、round-trip、字符与 token 减少、无收益 identity、protected byte compare、禁止指令/tool schema/关键事实不替换、NUL/ref 转义、非法 manifest/ref/hash fail-fast、跨 thread/session 隔离、默认路径兼容、Driver 400 重试、审计零明文 | `benchmark/tests/test_prompt_preprocessor.py` 32 例，全部通过（见 §6） |
| 5 | 先 pytest+smoke；GPU/4B 可用则 4B greedy paired；PASS 条件；不满足判 HOLD | 4B greedy paired 完成（single + multi_turn + restore 对照）；判定 `HOLD_NOT_VALIDATED`（single：输出不一致）/ `HOLD_NO_MEASURABLE_GAIN`（multi_turn：identity 无收益）——见 §7 |
| 6 | 论文：现有 E15 证据足够则不扩大综述 | **未检索**：B1 是 deterministic reversible preprocessing，E15 既有证据（E15_0 §3.1 门禁定义、E15_4 DuplicateBlockCompressor 语义、E12 BPE 教训）已覆盖实现决策；无新证据需求，不更新 E15_ARTICLE_MCP_LOG.json（该日志只记录实际检索，未检索不伪造条目） |
| 7 | docs/E15_3 文档 + AGENTS.md 同步 + 不改 E14 历史 | 本文件 + AGENTS.md 更新（§8） |
| 8 | 提交根仓库（llama.cpp 未改不提交）、clean 核对、最终报告 | 见 §9 |

---

## 2. 改动清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `benchmark/framework/prompt_preprocessor.py` | 新增 | B1 核心（纯 Python stdlib，无外部依赖；详见 §3） |
| `benchmark/framework/driver.py` | 修改 | `Driver.__init__` 增加 `preprocessor: Optional[Preprocessor]`；`chat()` 发送前压缩（新列表、审计字段、400 重试兼容） |
| `benchmark/runner/runner.py` | 修改 | `Runner.__init__` 按 `config.extra["preprocessor"]` 构造 Preprocessor（默认 off → None）；`cli_main` kv_probe 分支同步 |
| `benchmark/tests/test_prompt_preprocessor.py` | 新增 | 32 个 pytest（无 GPU/真实 server） |
| `benchmark/runner/e15_3_b1_paired.py` | 新增 | 4B greedy paired 门禁 runner（single / multi_turn / gate_verdict M0-M2） |
| `docs/E15_3_B1_STRUCTURED_LOSSLESS.md` | 新增 | 本报告 |
| `AGENTS.md` | 修改 | 架构/命令/E15 状态同步 |

未修改：`config.py`（preprocessor 走 `config.extra`，与 E15.2 同一惯例）、四个 workload、
`context_policy.py`、`tool_payload.py`、E6–E14 历史文档与原始数据、llama.cpp（独立仓库，未改）。

---

## 3. 设计 / API

### 3.1 压缩语义（H1 system 合同）

- **块 = 单条消息的完整 content**（消息级完全重复去重）。同 content 第二次出现（全局，
  与 role 无关）→ 替换为 `\x00REF:<block_id>\x00`；`block_id = "b" + sha256(content)[:16]`
  （内容寻址、确定性）。
- **protected（永不替换，逐字节保留）**：`role == "system"`（system prompt / 禁止指令 /
  安全约束 / tool schema 本体全部位于 system 消息）；关键事实声明模式（秘密数字 /
  `请记住` / `请注意` / `重要:`）；tool payload 引用协议特征（`payload_ref=` /
  `resolve_tool_payload` / `[tool_payload externalized]`）；显式注册 fact 文本
  （`Preprocessor(fact_texts=[...])`，content 包含即保护）。
- **净收益判定（双口径，防 E12 教训）**：替换仅当 ① 被替换 content 的 UTF-8 字节数 >
  `min_block_chars`（默认 23 = REF 标记字节数）**且** ② 估算 token（chars/3）严格大于
  `_gain_token_threshold()`（= REF 估算 token × 2 = 16，保守门槛）。**中文短块（如 13
  字符 = 39 字节 > 23）在 Qwen tokenizer 下仅 ~13 token，而 REF 标记 19 token——替换会
  token 膨胀 → 不替换（identity）**。已知估算窗口（reviewer）：48–76 字符的 ASCII 重复块
  估算判定可替换但真实 BPE 可能 < 19 token 而膨胀——由 paired 门禁 M2（真实
  prompt_tokens 收益）兜底判 HOLD；估算口径内绝不膨胀。全局
  `output_bytes < input_bytes` 不满足时整体返回 identity（绝不膨胀）。
- **无损语义（如实声明，与 E15.4 DuplicateBlockCompressor 一致）**：`reversible=True`
  （round-trip 逐字节还原，测试验证）；`lossless=False`（**不声称** greedy 输出一致——
  E15.0 门禁需 4B paired 验证，本模块不做伪称）。

### 3.2 公共接口

| API | 说明 |
|---|---|
| `structured_lossless_compress(messages, fact_texts=(), min_block_chars=23) -> PreprocessorResult` | 纯函数压缩；`PreprocessorResult{messages, manifest, dictionary}` |
| `restore(compressed, manifest, dictionary) -> List[dict]` | round-trip 还原；五步校验（线程绑定 → manifest 合法 → output_hash → 逐条 ref/hash 自验证 → input_hash），任一失败 fail-fast |
| `Preprocessor(fact_texts=(), min_block_chars=23)` | Driver 接入包装；`process(messages) -> (work, audit_dict)`，无跨请求状态 |
| `PreprocessorManifest`（slots） | 审计记录：mode/version/input_hash/output_hash/compressed/reversible/lossless/replaced_spans/protected_spans/notes/stats；`to_dict()` 零明文；`vars()` 直接抛错 |
| `PreprocessorDictionary` | restore 必需的内存字典（block_id → 原文），thread-scoped（owner_ident + session_id）；原文唯一载体，不进 manifest/日志/结果 |
| `is_protected_message(role, content, fact_texts) -> (bool, label)` | protected 纯函数判定 |
| `normalize_preprocessor_mode(value) -> str` | 配置归一化（off/structured_lossless，非法 fail-fast，兼容 `structured-lossless` 别名） |

### 3.3 fail-fast 体系

`PreprocessorNulError`（原始内容含 NUL）/ `PreprocessorManifestError`（畸形 manifest、
版本/模式不支持、压缩输入缺键/非法 role/非 str content——reviewer 修复：restore 结构
校验前置，杜绝 hash 阶段 KeyError）/ `PreprocessorUnknownRefError`（未知 ref）/ 
`PreprocessorHashMismatchError`（输出/输入 hash、block_id 自验证）/ 
`PreprocessorCrossThreadError`（dictionary 跨线程）。
自有 REF 产物（压缩输出再入，如 400 重试/嵌套压缩）**幂等识别并跳过**；伪 REF 在 restore
的 ref/hash 校验处 fail-fast（两层防御：output_hash 整体校验 + 逐条 ref/内容 hash 自验证）。
消息额外字段（`name`/`tool_call_id` 等）在压缩（`{**m, content: REF}`）与还原
（`{**m, content: 原文}`）两侧均保留——逐消息/逐字段 round-trip（reviewer 修复）。

---

## 4. 数据与身份边界

- **不触碰 llama.cpp / KV / server cache identity**：纯应用层，请求发送前改写，
  发送后即弃；`row["preprocessor"]` 审计不含原文。
- **manifest 零明文**：`to_dict()` 只含 hash/ref/label/字节区间/统计；测试断言全部原文
  （含 system 与被压缩内容）不在 manifest 序列化结果中。
- **原文唯一载体**：`PreprocessorDictionary`（进程内、thread-scoped、内容寻址自验证）；
  不得写入 manifest / 日志 / 结果 JSON（文档 + 测试双重约束）。
- **跨 thread/session**：dictionary 绑定创建线程（restore 跨线程 → fail-fast）；
  跨 session 错配 → 未知 ref / hash mismatch fail-fast（内容寻址 + 自验证兜底）。

---

## 5. Driver 单点接入与重试/消息生命周期安全性论证

**为什么选 Driver 单点**：四个 workload（multi_turn / tool_call / branch / long_life）
唯一请求入口均为 `driver.chat()`；单点接入 = 全部场景自动生效、零 workload 改动、
调用者消息零污染。

**为什么不破坏 400 重试/消息生命周期**：
1. **调用者零污染**：`process()` 返回新列表（`Preprocessor` 只读输入），workload 的
   history / tool_response 回填 / pending restore 校验 / 截断逻辑全部基于原始消息；
2. **400 重试安全**：兜底基于**原始消息**裁剪后递归 `chat()`，递归重新压缩同一输入 →
   确定性幂等（同输入同输出）；压缩产物（含 `\x00REF:`）若再入压缩器，幂等识别自有
   REF 并跳过（不触发 NUL fail-fast）——实测通过（test_driver_400_retry...）；
3. **审计字段**：`row["preprocessor"]` 仅 on 模式出现；off（默认）行为与旧版逐字节一致
   （159 个相关测试回归全绿）。

---

## 6. 测试与真实命令/结果

### 6.1 pytest（无 GPU/真实 server）

```bash
cd benchmark && uv run pytest tests/test_prompt_preprocessor.py -q
# 34 passed in 0.04s
```

> reviewer 修复（2026-08-08）：① restore 保留消息额外字段（name/tool_call_id 等），
> round-trip 逐字段还原；② 净收益 token 门槛提高到 REF 估算 ×2（16，保守），缩小 ASCII
> 短块估算膨胀窗口；③ restore 压缩输入结构校验前置（缺键 → PreprocessorError 而非
> KeyError）；新增对应测试（额外字段 round-trip / restore 缺键 fail-fast / 门槛更新）。

覆盖组（对照需求 4）：normalize/off/identity（3）｜确定性含跨线程（2）｜round-trip
（2）＋额外字段保留 round-trip（1）＋restore 缺键 fail-fast（1）｜重复时字符与估算
token 减少（1）｜无重复/中文短块/短重复无收益 identity（3）｜protected byte compare
（system 重复 / 秘密数字 / tool payload 协议 / 注册 fact / 纯函数，5）｜fail-fast
（NUL / 伪 ref 幂等+restore 失败 / 未知 role / 畸形 manifest / 未知 ref / 篡改压缩输入 /
篡改 dictionary / add 自验证 / 跨 thread restore / 跨线程属性，10）｜Driver 集成
（off 旧字段 / on 压缩不污染 / 400 重试幂等 / fail-fast 传播，4）｜审计零明文（1）｜
workload 默认路径 / Runner 默认 off/opt-in/非法值（2）。

回归（接入未破坏既有行为；同样从 `<repo-root>` 执行）：
```bash
cd <repo-root>/benchmark && uv run pytest tests/test_driver.py tests/test_runner.py tests/test_config.py \
  tests/test_workloads.py tests/test_e15_2_tool_payload_integration.py \
  tests/test_context_policy.py tests/test_e2e_smoke.py -q
# 127 passed in 2.01s
```

### 6.2 最小 smoke（真实 multi_turn 20 轮历史）

round-trip 逐字节还原 OK、manifest 零明文 OK；token 口径修复后 POOL 短块不压缩 →
identity 不膨胀（bytes 3990→3990）。

---

## 7. 门禁判定：Qwen3.5-4B greedy paired（正式）

### 7.1 环境与协议

- 硬件：RTX 4060 8GB（同硬件 off/on）；模型：`models/qwen3-5-4B-Q4_K_M.gguf`（hybrid）；
  server：`llama.cpp/build-cuda/bin/llama-server -ngl 99 --ctx-size 4096 --parallel 1`；
- 协议：同 seed=42 / temperature=0（greedy），同原始 prompt，**仅 preprocessor off/on
  不同**（off 发原文；on 发 `structured_lossless_compress` 产物）；
- runner：`benchmark/runner/e15_3_b1_paired.py`（gate_verdict M0 数据完整 / M1 输出一致
  + 停止原因 / M2 实际 token 收益）。

### 7.2 single 场景（长重复块，压缩生效）

| 项 | off | on |
|---|---|---|
| prompt_tokens | 204 | **168**（-36，-17.6%） |
| 压缩率（manifest） | — | ratio 0.297（字节口径），replaced=1，protected=1 |
| 输出 content | 逐字节不同（语义相近但文本不同） | — |
| finish_reason | stop | stop |
| KV（/metrics/kv） | used_cells=…（记录于原始 JSON） | 同 |

→ **M1 失败**：REF 压缩形态（`\x00REF:…` 在 Qwen tokenizer 下 19 token，被模型当
文本读）改变了 greedy 输出 → `HOLD_NOT_VALIDATED`（无损不成立）。

### 7.3 multi_turn 场景（12 轮累积历史）

- POOL 循环的重复 user 消息为**中文短块**（13 字符 ≈ 13 token < REF 19 token）→
  token 口径判定不压缩 → **identity**；
- 结果：12/12 轮 off/on 输出逐字节一致 + 停止原因一致（M1 通过），但 prompt_tokens
  无收益（M2 不达）→ `HOLD_NO_MEASURABLE_GAIN`；
- 备注：第 11 轮 on prompt 略高于 off（server cache 计数差异，`cache_n` 相同、
  `prompt_n` 33 vs 44），不影响判定（identity 场景，B1 无收益）。

### 7.4 restore 对照（实验有效性）

压缩 → restore 还原原文 → 重发：输出与 off **逐字节一致**（`output_same_off_vs_restored
= True`，`round_trip_exact = True`）→ **paired 输出不一致确由 REF 形态引起**（非 server
非确定性），实现正确、门禁判定有效。

### 7.5 原始证据

`benchmark/results/e15_3_b1_paired_single.json` / `e15_3_b1_paired_multiturn.json` /
`e15_3_b1_restore_control.json`（results/ 为临时输出，不入库；复现命令见 §10）。

### 7.6 最终判定

**`HOLD_NOT_VALIDATED`**（E15.0 §3.1：无损三条件中"greedy 输出一致 + 停止原因一致"
在真实 4B 上不成立）→ **降级为有损候选，转 E15.5（B2 摘要 oracle，12.12 门禁）**。
multi_turn 的 `HOLD_NO_MEASURABLE_GAIN` 补充说明：真实智能体历史中"完全重复块"多为
短中文块（模型输出每轮不同、tool payload 每轮不同），token 口径无压缩收益。

---

## 8. AGENTS.md 同步

- 架构：`benchmark/framework/prompt_preprocessor.py`（B1 deterministic structured-lossless
  preprocessor，消息级完全重复去重 + protected 保留 + round-trip restore + fail-fast）；
- 命令：无新 CLI（`config.extra["preprocessor"]`）；paired 复现见 §10；
- E15 状态：E15.3 完成，`HOLD_NOT_VALIDATED`（无损不成立，降级有损转 E15.5）；
  E15.2 之后的阶段进度更新。

---

## 9. 提交

- 根仓库：commit（见仓库 log；message 分点描述）；提交后 `git status --porcelain` 空；
- llama.cpp：**未改动，不提交**。

---

## 10. 复现命令

```bash
# 每条命令均从仓库根（<repo-root>）独立执行；server 与 benchmark 各自独立启动/运行

# 单元测试
cd <repo-root>/benchmark && uv run pytest tests/test_prompt_preprocessor.py -q

# paired 门禁（需 GPU + 4B 模型；server 以仓库根为 cwd 启动，-m 相对路径正确）
cd <repo-root>
./llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 4096 --parallel 1 &

# benchmark 侧（等待 server 就绪后独立执行）
cd <repo-root>/benchmark
uv run python runner/e15_3_b1_paired.py --scenario single   --output results/e15_3_b1_paired_single.json
uv run python runner/e15_3_b1_paired.py --scenario multi_turn --rounds 12 --output results/e15_3_b1_paired_multiturn.json
```

---

## 11. 回滚

`config.extra["preprocessor"]` 默认 off / 未设置 → `Preprocessor=None` → Driver 行为与
E15.2 及之前逐字节一致（159 个回归测试验证）。删除 preprocessor 配置即可完全回滚；
移除 `benchmark/framework/prompt_preprocessor.py` 与 Driver/Runner 接入点即恢复原状。

---

## 12. 风险与未完成项

- **无损不成立（已证）**：消息级 REF 去重改变模型输入 token 序列 → greedy 输出不一致；
  这是**本质限制**（非实现缺陷）——任何"以引用替代原文"的输入改写都会改变模型所见 token。
- **估算 token 口径**：`estimate_tokens`（chars/3）非 BPE；真实 token 收益必须由 paired
  门禁 prompt_tokens 验证（已做）。内部净收益判定用估算口径（保守，估算内不膨胀），
  中英文混合密度差异导致部分边缘块误判（不替换 = 错失收益，非膨胀）。
- **未完成**：B2 有损摘要 oracle（E15.5）——B1 降级后的 12.12 门禁；B3 在线决策
  （E15.0 §3.3 前置 = B1 无损 PASS 且 B2 达标，当前 B1 不成立 → 保持关闭）。
- **论文综述**：按需求 6 未扩大（E15 证据足够）；E15_ARTICLE_MCP_LOG.json 不新增条目。
