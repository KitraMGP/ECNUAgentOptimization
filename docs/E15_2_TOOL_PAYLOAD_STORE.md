# E15.2（CORE + workload 集成）：ToolPayloadStore 工具返回 payload 进程内内容寻址存储

> 状态：**CORE（已交付）+ workload 最小端到端集成（已交付）**；独立核心 + 纯 pytest +
> tool_call / long_life 集成 + 集成 pytest；**未跑真实模型 paired**（上下文占位替换的
> token/KV 收益需 4B greedy paired 对比，见 §6，如实声明未做）。
> 日期：2026-08-08 ｜ 对应计划：`docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` 技术线 B（E15.2）
> 提交状态（2026-08-08 收口）：已提交，根仓库 commit `a2e0558`（核心 + 集成 + 测试 + 本文档）。
> 约束执行（CORE）：只新增 `benchmark/framework/tool_payload.py`、`benchmark/tests/test_tool_payload.py`
> 与本文档；未修改 tool_call.py / long_life.py / config.py / runner.py / driver.py / workload.py /
> 现有测试 / llama.cpp；未安装依赖、未跑全量测试/格式化（仅跑 `tests/test_tool_payload.py`）。
> 约束执行（集成）：只改 `benchmark/workload/tool_call.py`、`benchmark/workload/long_life.py`、
> 新增 `benchmark/tests/test_e15_2_tool_payload_integration.py` 与本文档；未改 llama.cpp / Driver /
> context_policy / runner / config.py / framework/tool_payload.py / 既有 E14 文档；未跑全量测试/格式化（仅跑集成测试 + test_workloads.py 回归）。
> 并行任务隔离：本实现不触碰 context_policy（并行任务产物）涉及的任何文件与命名空间。
> 核心 reviewer 修复（2026-08-08）：只改 `benchmark/framework/tool_payload.py`、
> `benchmark/tests/test_tool_payload.py` 与本文档；未改 workload / Driver / config / runner /
> llama.cpp；未格式化、未跑全量测试（仅跑 `tests/test_tool_payload.py`，65 passed）。

---

## 1. 改动清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `benchmark/framework/tool_payload.py` | 新增 | ToolPayloadStore 核心（纯 Python stdlib：hashlib/json/os/re/threading/dataclasses/types，无外部依赖） |
| `benchmark/tests/test_tool_payload.py` | 新增 | 36 个纯 pytest 单元测试（无 GPU / 真实 server / workload 依赖） |
| `docs/E15_2_TOOL_PAYLOAD_STORE.md` | 新增 | 本报告 |

## 2. 公共接口

| API | 签名 | 说明 |
|---|---|---|
| `put` | `put(payload: dict\|list\|str\|bytes) -> Ref` | 内容寻址入库；canonical 化；敏感 payload fail-closed（`ToolPayloadSecretError`）；单条超 `max_bytes` 抛 `ToolPayloadCapacityError`；重复内容 dedupe（刷新 last_used_tick） |
| `resolve` | `resolve(ref: str, expected_hash: str) -> str` | **显式**按 ref+expected_hash 取回 canonical 原文；成功刷新 last_used_tick；ref 缺失/已 purge → `ToolPayloadMissingError`；hash 不匹配 → `ToolPayloadHashMismatchError`（不刷新） |
| `get` | `get(ref: str) -> Entry` | 返回不可变 `Entry`（审计视图，含 projection）；读操作刷新 last_used_tick |
| `snapshot` | `snapshot() -> ToolPayloadSnapshot` | 进程内可审计快照：只含元数据 + projection，**不含 payload 原文**（天然无敏感值）；`to_dict()` 可序列化 |
| `restore` | `restore(snap)` | 恢复快照时刻视图（entries / LRU tick / tick 计数）；仅进程内（pid 校验）+ 同 store 线程（owner 校验）可用 |
| `purge` | `purge(ref: str \| None = None)` | 清除全部（默认）或单个 ref；purge 后对应 ref 一律 fail-fast |

构造：`ToolPayloadStore(max_entries=None, max_bytes=None)`（None = 无限制；`< 1` 抛 `ToolPayloadCapacityError`）。
辅助（均为 thread-scoped，跨线程访问 fail-fast，与写接口一致）：`__len__`（entry 数）、`total_bytes`、
`tick`、`owner_ident`、`stats() -> {entries, bytes, tick}`。
模块级纯函数：`canonical_json` / `sha256_hex` / `stable_sha256` / `project_payload` / `eviction_key` /
`entry_to_dict`。

### 不可变值对象

- `Ref`（frozen）：`ref`（sha256 hex 64 位，内容寻址）、`expected_hash`（= ref，内容哈希）。
- `Entry`（frozen）：`ref` / `content_type`（`"json"`\|`"text"`）/ `size_bytes` / `sha256` /
  `projection`（**递归只读**：dict → `MappingProxyType`、list → `tuple`，任何层级修改
  fail-fast）/ `inserted_tick` / `last_used_tick`。
- `ToolPayloadSnapshot`（frozen）：`pid` / `owner_ident` / `created_tick` / `entries` + `to_dict()`。

### 异常（fail-fast 默认，均继承 `ToolPayloadError`）

`ToolPayloadSecretError`（敏感，fail-closed）｜`ToolPayloadMissingError`（ref 缺失/已 purge/原文不可用）｜
`ToolPayloadHashMismatchError`（hash 不匹配）｜`ToolPayloadCrossThreadError`（跨线程访问/restore）｜
`ToolPayloadSnapshotError`（跨进程 restore）｜`ToolPayloadCapacityError`（容量超限/参数非法）。

## 3. 核心语义

### 3.1 canonical JSON / 文本与 sha256 ref

- `canonical_json`：`json.dumps(ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)`。
  键排序 + 紧凑分隔符 → 语义相同而书写不同的 JSON 得到同一 ref；NaN/Infinity 非法值与混合类型键
  （如 int 与 str 混排无法排序）统一包装为 `ToolPayloadError` fail-fast，**不得把非标准 JSON
  写入 store / blob / projection**。
- 文本 payload：utf-8 原样字节（不做 strip），`content_type="text"`。
- ref = `sha256(canonical 字节).hexdigest()`，内容寻址 → 重复内容 dedupe（不新增 entry，刷新 last_used）。

### 3.2 确定性 structured projection

`project_payload(parsed, content_type)` 纯函数，输出固定 schema：

```jsonc
{
  "type": "order" | "generic_json" | "text",
  "order_ids": [..] | null,          // 顶层 order_ids 的 str 项（截断到 8 条展示）
  "order_ids_total": int | null,     // 全量条数
  "item_count": int | null,          // 顶层 items 数组长度
  "total_amount": float | null,      // items 的 qty*price 求和（round 2）
  "logistics_status": [..] | null,   // logistics 的 status 去重保序（≤8 个）
  "summary": "紧凑 JSON 字符串"      // 有界确定性摘要
}
```

- 现有订单 payload 覆盖：`search_orders`（`order_ids`）与 `get_order_detail`
  （`items`→item_count/total_amount、`logistics`→logistics_status）均被识别为 `type="order"`。
- 未知 JSON：`type="generic_json"`，四个业务字段为 None，`summary` 为有界确定性遍历统计
  （`{depth, containers, scalars, keys, truncated}`，键排序；上限：深度 3 / 单容器条目 8 /
  键名长度 32 / 键数 16）。
- 文本：`type="text"`，`summary={chars, head_sha256}`——`chars` 为真实字符数，
  `head_sha256` 为文本 sha256 前 16 位（确定性指纹）。**summary 绝不写入 payload 原文片段**
  （reviewer 修复：head 截断会泄漏原文，已移除；`chars` 此前因 `str(None)` 恒为 4 的
  连带缺陷一并修复）。
- **projection 深冻结**：`project_payload` 输出经 `_deep_freeze` 递归转为只读结构
  （dict→`MappingProxyType`、list→`tuple`）；外部对 `entry.projection` 任何层级的
  `append` / 下标赋值 / 新增键一律 fail-fast，且**不会污染 snapshot 视图**。
  `entry_to_dict` 用 `_deep_unfreeze` 深解包回普通 dict/list，保证 snapshot 可 JSON 序列化。
- **projection 不携带 payload 值内容**：只提取订单号/数量/金额/状态等结构化字段与键名统计，
  address/customer 等原文值永不进入 projection（已用测试断言锁定）；order_ids 为 H3 定位字段
  保留于 projection。

### 3.3 淘汰（统一 LRU）

- `eviction_key(entry) = (last_used_tick, inserted_tick)`：`min last_used_tick` 优先淘汰，
  `inserted_tick` **仅** tie-break。
- `resolve` / `get` 成功刷新 `last_used_tick`（读刷新）；`put` dedupe 同样刷新。
- `max_entries` / `max_bytes` 在 put 后循环淘汰至满足；单条 payload 超过 `max_bytes` 直接
  fail-fast（`ToolPayloadCapacityError`），不存在"淘汰后塞入"的伪路径。

### 3.4 thread-scoped

- store 构造时记录 `threading.get_ident()`；**所有公开方法**（含只读访问器 `__len__` /
  `total_bytes` / `tick` / `owner_ident` / `stats`）首行 `_check_thread()`，跨线程访问一律
  fail-fast（`ToolPayloadCrossThreadError`），包括用子线程创建的 store/ref 在本线程操作、
  以及把快照 restore 到不同线程的 store。

### 3.5 snapshot / restore / purge

- `snapshot()`：只含元数据 + projection（不含原文）→ **可审计**（可打印/序列化），且因敏感
  payload 本就被 `put` 拒绝，快照**天然不含敏感值**。
- `restore(snap)`：仅进程内（`snap.pid != os.getpid()` → `ToolPayloadSnapshotError`）+ 同 store
  线程（owner 不匹配 → `ToolPayloadCrossThreadError`）。恢复快照时刻的 entries / LRU tick /
  tick 计数，并丢弃视图之外的 blob。**快照不携带原文**：被 purge 的 payload 在 restore 后仍
  fail-fast（不可复活）——语义诚实，无伪恢复。
- `purge(ref=None)`：清空全部或单个；之后对应 ref 的 `resolve`/`get` 一律
  `ToolPayloadMissingError`（fail-fast），snapshot 不再包含。

## 4. 安全模型（fail-closed）

- 字段名 denylist：`password/passwd/pwd/secret/token/api_key/apikey/access_token/authorization/
  authorization_code/auth/credential/client_secret/private_key/secret_key/credit_card/card_number/
  cardno/cvv/cvc/ssn/cookie/session_id/csrf/密码/密钥/口令/验证码`（归一化比较：小写 + 去非字母数字，
  `api_key` 与 `API-Key` 视为同一）。**检测覆盖顶层 dict 与顶层 list 的全部嵌套 dict/list 键名**
  （reviewer 修复：此前仅顶层 dict 检查，顶层 list 内的嵌套敏感键会漏网）。
- **复合敏感字段（安全边界 = 敏感词后缀）**：归一化后以 `password/passwd/pwd/secret/token/apikey/
  credential/cardnumber/cardno/cvv/cvc/ssn/csrf/sessionid` 结尾即拒绝——覆盖
  `db_password` / `auth_token` / `user_secret` / `master_password` 等组合字段；后缀集合刻意不含
  `key`/`auth`/`id` 等短词，`author` / `monkey` / `secretary` / `tokenize` 等正常字段不误伤
  （精确命中仍由 denylist 负责，`authorization_code` 已在精确词表）。
- 敏感值模式（匹配 payload 文本）：`sk-…`（OpenAI）/ `AKIA…`（AWS）/ `ghp_…`（GitHub）/
  JWT 三段式 / PEM 私钥头 / `Bearer …`；纯文本键值形态 `password|token|… : / = 值`。
- 检测在 `put` 存储之前：命中任一规则 → `ToolPayloadSecretError`，payload **不进入**
  store / projection / ref / snapshot（无任何痕迹，测试断言 `len==0` 且 snapshot 为空）。
- 默认 fail-closed：宁可拒绝可疑 payload，不猜测性放行。

## 5. 上层协议（已由 workload 拦截接入，E15.2.1）

工具结果读取协议（显式、可审计、不自动注入）：

```
ACTION: resolve_tool_payload(payload_ref=<ref>, expected_hash=<hash>)
```

- `resolve` 是**纯数据读取**：只校验 ref+expected_hash 并返回 canonical 原文，**不注入任何
  prompt**（测试 `test_resolve_is_pure_data_no_prompt_injection` 锁定）。
- 已接入 workload（tool_call / long_life）：把工具返回内容 `put` 入库 → 上下文只放
  projection 占位（含 payload_ref/expected_hash）→ 拦截模型输出的
  `ACTION: resolve_tool_payload(...)` 行 → 调用 `store.resolve(ref, expected_hash)` 校验
  并取回原文，**仅紧接的一次模型请求可见**，随后原位恢复为同 ref/hash 的 projection
  占位（one-shot restore，见 §6.1）——完整 payload 绝不反复拼进后续请求。

## 6. workload 集成（已实现：tool_call / long_life 最小端到端）

本阶段完成 ToolPayloadStore 到 benchmark workload 的最小端到端集成，改动仅限
`benchmark/workload/tool_call.py`（含共享组件）、`benchmark/workload/long_life.py`、
`benchmark/tests/test_e15_2_tool_payload_integration.py` 与本文档。

### 6.1 集成设计（共享组件位于 tool_call.py，long_life 复用）

- **`ToolPayloadRun`**：每个 workload run 创建一个实例，内含 thread-scoped
  `ToolPayloadStore`（绑定 run 所在线程）、可审计计数与 **pending one-shot restore 状态**。
  tool_call / long_life 的工具轮统一调用 `ToolPayloadRun.tool_response(action, args, text)`
  （返回 `(content, audit, projection_placeholder)`）。
- **配置**：`tool_payload_mode=off|externalized`（**默认 off**，可回滚），经
  `config.extra["tool_payload_mode"]`（配置文件未知键，沿用既有 extra 模式，不新增 CLI 参数）
  → `params_from_config` → `spec.params` → `run`。非法值 fail-fast（`ValueError`）。
- **externalized 工具轮**：工具返回 → `store.put` → tool_response 只注入
  `[tool_payload externalized] payload_ref=<ref> expected_hash=<hash> projection: <确定性 projection>`
  及"如需完整原文请输出 ACTION: resolve_tool_payload(...)"指引，**不含 payload 原文**
  （projection 由 ToolPayloadStore 生成，天然不含 address/customer 等原文值；
  search_orders 的 order_ids 保留于 projection，满足 H3 定位约束）。
- **resolve 拦截**：模型输出 `ACTION: resolve_tool_payload(payload_ref=<ref>,
  expected_hash=<hash>)` 时由 workload 拦截（**不交给 MOCK_TOOLS**，MOCK_TOOLS 无此项）：
  - 严格正则：ref / expected_hash 必须为 64 位小写十六进制，缺参/格式错 →
    `ToolPayloadError`（非法动作 fail-fast）；
  - `store.resolve(ref, expected_hash)`：ref 缺失/被淘汰 → `ToolPayloadMissingError`，
    hash 不匹配 → `ToolPayloadHashMismatchError`（均 fail-fast，不静默降级）；
  - 成功 → 本轮 tool_response 注入完整 payload，并登记 pending（`mark_pending` 记录
    消息位置 + 同 ref/hash 的 projection 占位）。
- **one-shot restore（pending 状态机）**：完整 payload 只在**紧接的一次模型请求**中可见——
  每次 `driver.chat` 返回后立即调用 `after_chat(messages)`，把 pending 位置上的完整
  `<tool_response>` **原位替换回相同 ref/hash 的 projection 占位**，随后才继续累积历史；
  因此后续所有 messages 历史均不含完整原文（测试断言 resolve 后第 2、3 个后续调用的
  整个 messages 均无 customer/address/完整 payload）。`after_chat` 是**严格校验**的：
  pending projection 缺失 / index 越界 / 位置不是 user 消息 / 内容与注入时不匹配
  （`mark_pending` 记录 expected_content 原文用于比对）→ 抛 `ToolPayloadError`
  （fail-fast），**绝不静默清空 pending**。异常/fail-fast 路径上 run 终止，局部
  messages 不再被任何后续请求使用，不会泄漏进历史。long_life 的局部工具消息
  （`tool_msgs`，不进入长期 history）同样调用同一 `after_chat` 恢复语义；且普通轮
  `driver.chat(history)` 返回后也调用 `after_chat(history)`——pending 不得跨轮残留
  （若有残留会在下一轮 fail-fast，而非静默带入）。
- **普通工具 ACTION**：保持原解析（参数原样传入 mock）；未知工具 KeyError fail-fast
  （无静默 fallback）。
- **审计（不改旧模式核心字段）**：externalized 模式
  - `meta["tool_payload"]`：`mode / externalized_puts / unique_refs / resolve_requests /
    resolve_successes / resolve_failures / projection_chars / full_payload_chars`；
  - 行级 `row["tool_payload"]`（tool_call 每工具步 / long_life 工具行）：
    `{"kind": "externalized_projection" | "resolve_full", "ref", "chars"}`；
  - evaluate 的 `tool_calls` / `tool_completion` 排除内部动作 `resolve_tool_payload`，
    只统计真实 MOCK_TOOLS 动作；
  - off 模式 meta/rows 零新增字段（完全旧行为）。

| 集成点 | 现状 | 后续动作 |
|---|---|---|
| `tool_call.py` 的 `<tool_response>` 回填 | **已接入**（externalized：put→projection 占位→resolve 拦截回填） | 真实模型 paired 对比（token/KV 收益） |
| `long_life.py` 工具轮 | **已接入**（同一 ToolPayloadRun 语义） | 同上 |
| 上下文占位替换与 token 收益 | **未实现（如实声明）** | 需要真实模型 paired（4B greedy，store on vs off）测量 prompt tokens / KV bytes（E15.0 §5.2 阈值 12.11/12.12） |
| 与 `context_policy.py`（E15.4 C 线）组合 | 未实现 | 两模块命名空间/文件完全隔离，后续可组合 |

## 7. 测试

### 7.1 核心纯 pytest（CORE）

命令：

```bash
cd benchmark && uv run pytest tests/test_tool_payload.py -q
```

结果：**65 passed in 0.06s**（2026-08-08 核心 reviewer 修复后，纯 pytest，无 GPU / 无真实
server / 无 workload 依赖；修复前基线 36 passed）。

### 7.2 workload 集成 pytest（E15.2.1）

命令：

```bash
cd benchmark && uv run pytest tests/test_e15_2_tool_payload_integration.py -q
cd benchmark && uv run pytest tests/test_workloads.py -q   # 回归（workload 兼容性）
```

结果：**21 passed in 0.06s**（集成，含 reviewer 两轮修复后的 one-shot restore 严格校验断言）+ **10 passed in 0.02s**（回归），
2026-08-08，纯 pytest，无 GPU / 无真实 server。

覆盖清单（集成）：

1. off 模式（默认）保持旧完整 payload 注入（含 customer/address 原文），meta/rows 零结构改动；
2. externalized projection 不含 address/customer 等敏感原文值（order_ids 保留，H3 定位约束）；
3. **one-shot restore**：resolve 完整 payload 仅在紧接的一次请求可见；resolve 后第 2、3 个
   后续调用的**整个 messages**（含历史）均不含 customer/address/完整 payload，且恢复的
   projection 占位与最初注入的 ref/hash/projection 完全一致（行级 kind 序列断言）；
4. hash mismatch（`ToolPayloadHashMismatchError`）/ missing（`ToolPayloadMissingError`）/
   非法动作（`ToolPayloadError`）/ 未知工具（KeyError，保持原语义）fail-fast；
5. thread / run 隔离：跨 run 引用 ref → missing fail-fast；4 线程并发 run 用不同 customer →
   各自 ref 不同且 resolve 正常（可区分独立 store 与误共享 store）；
6. 非法 `tool_payload_mode` 配置值 fail-fast（`ValueError`）；
7. long_life 工具轮同一语义：externalized projection 注入 + 审计、off 完整 payload、
   resolve 非法动作拦截 fail-fast、**resolve 成功路径（轮4 put → 轮8 resolve）完整 payload
   仅一次局部 tool_msgs 请求可见且不进入长期 history**；
8. evaluate 的 `tool_calls` / `tool_completion` 排除内部 `resolve_tool_payload`，只统计真实
   MOCK_TOOLS 动作；
9. **one-shot 状态机严格校验**：`after_chat` 对 pending index 越界 / 位置非 user / projection
   缺失 / 内容不匹配（expected_content 比对）→ `ToolPayloadError` fail-fast，且**不静默清空
   pending**（异常后 pending 保留，可审计）；正常路径原位恢复占位并消费 pending；
10. **pending 不跨轮**：long_life 普通轮 `after_chat(history)` 无残留 no-op；工具轮 resolve
    注入的 pending 在同一工具轮内即被消费，轮8 后普通轮（9-12）正常执行。

覆盖清单：

1. projection 确定性（同 payload 键序不同 → 同输出/同 ref；generic_json 字段为 None）
2. 未知 JSON 有界 projection（深度 ≤ 3、条目 ≤ 8、键名截断、truncated 标记、确定性）
3. canonical hash / ref（键序无关、dict 与等价 str 同 ref、内容不同不同 ref、ref == sha256）
4. 文本 payload（hash 稳定、resolve 原样返回）
5. 读刷新 LRU（resolve 刷新、get 刷新 → 淘汰最久未用者）
6. LRU tie-break（`eviction_key` 纯函数：last_used 相同 → inserted 小者先淘汰）
7. 跨线程 fail-fast（子线程操作主线程 store、主线程操作子线程 store/ref、跨线程 restore）
8. missing（resolve/get 未知 ref → `ToolPayloadMissingError`）
9. hash mismatch（`ToolPayloadHashMismatchError`，且不刷新 last_used/tick）
10. secret fail-closed（denylist 字段、`sk-`/JWT/`Bearer` 值、嵌套、文本键值；`len==0`、snapshot 空）
11. **reviewer：secret 覆盖顶层 list / 嵌套 dict/list / 复合敏感字段**（`db_password` / `auth_token` /
    `user_secret` / `master_password` / `authorization_code` 拒绝；`author` / `username` / `monkey` /
    `secretary` / `tokenize` 等正常字段放行，参数化）
12. **reviewer：projection 递归不可变**（order_ids/logistics_status 为 tuple；append / 下标赋值 /
    键赋值 fail-fast；嵌套 dict 冻结；mutation 尝试不污染 snapshot；entry_to_dict 深解包可序列化）
13. **reviewer：文本 projection 不泄漏原文**（summary 只含 chars + head_sha256，无 head；snapshot
    无原文片段；chars 为真实字符数）
14. **reviewer：非标准 JSON 统一 fail-fast**（NaN/Infinity/混合类型键/字符串 NaN → `ToolPayloadError`；
    非有限 qty/price、乘积/求和溢出、超大 int 转 float 溢出同样 fail-fast）
15. **reviewer：只读接口 thread fail-fast**（`__len__` / `total_bytes` / `tick` / `owner_ident` /
    `stats` 跨线程访问 → `ToolPayloadCrossThreadError`）
16. max_entries / max_bytes 淘汰（超限循环淘汰至满足；单条超 max_bytes → `ToolPayloadCapacityError`）
17. snapshot/restore（可审计不含原文值、恢复视图、快照后新 put 不可见、purge 后 restore 不可复活、
    跨进程 restore 拒绝）
18. purge（单个 + 全部，之后所有 ref fail-fast）
19. 订单 payload projection（search_orders：order_ids/order_ids_total；get_order_detail：
    item_count=6、total_amount=1785.0、logistics_status=["已揽收"]；projection 不含地址/customer 原文值）
20. Entry/Ref 不可变（frozen + projection 递归只读）、put dedupe、resolve 纯数据无 prompt 注入、
    不支持类型 fail-fast

## 8. 交付状态

- CORE：核心 + 纯 pytest 交付完成；**65/65 通过**（核心 reviewer 修复后）；无 TODO / 伪实现 / 占位逻辑。
- 核心 reviewer 修复要点（2026-08-08）：① secret 检测覆盖顶层 list 与嵌套 dict/list，复合敏感字段
  （db_password/auth_token/user_secret 等）按敏感词后缀安全边界拒绝，author/monkey 等正常字段不误伤；
  ② Entry.projection 递归不可变（dict→MPT、list→tuple），外部任何层级修改 fail-fast 且不污染
  snapshot；③ 文本 projection 移除 head 原文截断，summary 只含 chars + head_sha256（确定性指纹），
  并连带修复 chars 恒为 4 的 `str(None)` 缺陷；④ NaN/Infinity/混合类型键/非有限 qty/price 统一包装
  `ToolPayloadError`，不泄漏非标准 JSON；⑤ `__len__`/`total_bytes`/`tick`/`owner_ident`/`stats`
  只读接口与写接口一致 thread fail-fast；⑥ 文档与实现语义同步，API 与 workload 集成兼容
  （projection 占位文本格式不变，未改 workload/Driver/config/runner/llama.cpp）。
- 集成（E15.2.1，含 reviewer 两轮修复）：tool_call / long_life 最小端到端接入完成；
  21/21 集成测试 + 10/10 workload 回归通过；off 模式完全回滚（旧行为零改动）。
- 修复要点（第二轮）：① `after_chat` 严格校验（index 越界 / 非 user / projection 缺失 /
  内容不匹配 → `ToolPayloadError`，绝不静默清空 pending，`mark_pending` 记录
  expected_content 原文比对）；② long_life 普通轮 `chat(history)` 返回后也调用
  `after_chat(history)`（pending 不跨轮），新增对应测试；③ 删除 `run` 中无效死代码
  （`else: content = None`）；④ 保持 15 个原有集成测试与 off 兼容语义。
- 修复要点（第一轮）：① resolve 完整 payload one-shot 可见（pending restore 状态机，chat 返回后原位
  恢复同 ref/hash 的 projection 占位，历史不含原文；long_life 局部工具消息同一语义）；
  ② 测试改为检查 resolve 后第 2、3 个后续调用整个 messages；③ 并发隔离测试用不同
  customer → 不同 ref 区分独立/误共享 store；④ 新增 long_life resolve 成功路径测试；
  ⑤ evaluate 排除内部 resolve_tool_payload；⑥ 文档更新删除"未来集成"过时表述。
- 未安装依赖（仅用现有 `.venv`）；未跑全量测试与格式化（仅跑
  `tests/test_tool_payload.py`，65 passed）。
- **未做真实模型 paired**：externalized 模式的上下文占位替换是否带来 prompt tokens /
  KV bytes 收益（E15.0 §5.2 阈值 12.11/12.12）需 4B greedy paired 对比（store on vs off），
  本阶段未跑真实模型，如实声明（见 §6 表格"后续动作"）。
