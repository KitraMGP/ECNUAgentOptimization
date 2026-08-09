"""context_policy —— E15.4 C 线：应用层上下文策略核心（纯 Python，无外部依赖）。

设计定位（对应 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` 技术线 C）：
- 本模块是**纯应用层**上下文策略核心，不触碰 llama.cpp / KV / server cache identity。
- 参照 LangGraph `trim_messages` / `RemoveMessage` / `summarize_conversation` 的 API 形态，
  落地为三级策略：``full``（现状，默认）/ ``trim``（只裁旧历史）/ ``extractive_summary``
  （把被裁历史中的明确事实/工具投影为有界结构化 summary block，**不调用 LLM**）。
- 消息四级分类（``MessageCategory``）：system（L1）/ tool_schema（L2）/ 关键事实（L3）/
  history（L4）。L1/L2 永不删除；L3 在 trim 下保护原文消息、在 extractive_summary 下
  投影进 summary block；L4 按最近 N 轮保留。
- **无损语义（如实声明）**：
  - ``full`` = identity，未压缩（lossless=True，平凡成立）。
  - ``trim`` / ``extractive_summary`` 都是**有损候选**（输入 token 改变、输出 token 大概率
    不一致；extractive_summary 尤其不伪称可从摘要恢复全部原文），``lossless`` 恒为 False。
    E15.0 定义的 lossless 硬门禁（manifest 可逆 + 字节级保留 + greedy 输出一致）本模块
    **未验证**——需要真实模型 paired 验证（后续集成后执行）。
  - 唯一的可逆变换是 ``DuplicateBlockCompressor``（deterministic reversible preprocessor）：
    仅对**完全重复块**做 dictionary/ref 去重，提供 round-trip restore，与 summary 严格分开。
- **审计**：每次策略应用产出 ``CompressionManifest``（策略版本、输入/输出 hash、保留/裁剪
  message ids、protected byte spans、统计），可独立核对。
- **隔离与异常**：``ThreadState`` thread-scoped；snapshot/restore/purge；跨 thread restore、
  非法消息、预算非法、错误预算超限均抛显式异常（fail-fast 默认）。
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 策略/数据结构版本（审计用；变更算法或格式时必须 bump）
CONTEXT_POLICY_VERSION = "0.1.0"

# ---- 异常 ------------------------------------------------------------------


class ContextPolicyError(Exception):
    """context_policy 全部异常的基类。"""


class IllegalMessageError(ContextPolicyError):
    """非法消息：未知 role / 空 content / system 出现在非头部等。"""


class BudgetError(ContextPolicyError):
    """预算参数非法（负数等）。"""


class CrossThreadRestoreError(ContextPolicyError):
    """把 snapshot 恢复到不同 thread_id 的 ThreadState 上。"""


class ErrorBudgetExceeded(ContextPolicyError):
    """错误计数超过配置的 error_budget。"""


# ---- 枚举与分类 --------------------------------------------------------------


class ContextPolicy(str, Enum):
    """上下文策略。默认 ``full`` = 现状（不做任何改写）。"""

    FULL = "full"
    TRIM = "trim"
    EXTRACTIVE_SUMMARY = "extractive_summary"


class MessageCategory(str, Enum):
    """消息四级分类（E15.0 §4.2）。"""

    SYSTEM = "system"            # L1：system prompt + 禁止指令，永不删除
    TOOL_SCHEMA = "tool_schema"  # L2：工具 schema/描述，schema 本体永不删除
    FACT = "fact"                # L3：关键事实（调用者注册），稳定 id/hash
    HISTORY = "history"          # L4：历史对话轮次（含工具结果）
    SUMMARY = "summary"          # 应用层注入的 summary block（属于 L4 历史，有损候选产物）


# tool_response 消息内容前缀（tool_call / long_life workload 的既有协议）
_TOOL_RESPONSE_PREFIX = "<tool_response>"

# 工具协议特征（tool_call.py TOOL_SYSTEM 的确定性特征，用于区分 L2 与 L1）
_TOOL_SCHEMA_MARKERS = ("ACTION:", "可调用以下工具", "调用格式示例")

# 内置"明确事实"声明模式（extractive_summary 专用，全部基于原文子串，禁止自由摘要）
_SECRET_RE = re.compile(r"请记住这个秘密数字[:：]\s*[0-9A-Za-z_\-]+")
_NOTABLE_RE = re.compile(r"(?:请记住|请注意|重要[:：])(?:(?!。|！|\?|？|\n).)+(?=。|！|\?|？|\n|$)")
_ACTION_RE = re.compile(r"ACTION:\s*([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)")

# REF 标记与转义（DuplicateBlockCompressor 专用，\x00 控制符几乎不会出现在真实文本）
_REF_PREFIX = "\x00REF:"
_REF_SUFFIX = "\x00"
_ESC = "\x00ESC\x00"


def stable_hash(text: str) -> str:
    """确定性的内容 hash（sha256 hex）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str, chars_per_token: float = 3.0) -> int:
    """确定性 token 估算（不精确，不做 BPE tokenize；仅用于预算与统计）。

    中文场景按每 token ~3 字符粗略折算；调用方可传显式 ``chars_per_token``。
    结果为 ``max(1, ceil(chars / chars_per_token))``，保证非零。
    """
    if chars_per_token <= 0:
        raise BudgetError(f"chars_per_token 必须 > 0，当前 {chars_per_token}")
    import math

    return max(1, math.ceil(len(text) / chars_per_token))


def classify_message(role: str, content: str) -> MessageCategory:
    """确定性四级分类（纯函数）。

    - role=system 且内容含工具协议特征 → TOOL_SCHEMA（L2）；
    - role=system 其余 → SYSTEM（L1）；
    - 内容以 ``<summary_block`` 开头的 system 消息 → SUMMARY（应用层注入，属 L4）；
    - 其余 user/assistant/tool → HISTORY（L4）。
    """
    if role == "system":
        if content.lstrip().startswith("<summary_block"):
            return MessageCategory.SUMMARY
        if any(m in content for m in _TOOL_SCHEMA_MARKERS):
            return MessageCategory.TOOL_SCHEMA
        return MessageCategory.SYSTEM
    return MessageCategory.HISTORY


def _is_real_user_query(msg: "Message") -> bool:
    """真实 user 提问（非 tool_response 回填）。"""
    return msg.role == "user" and not msg.content.lstrip().startswith(_TOOL_RESPONSE_PREFIX)


def _is_tool_result(msg: "Message") -> bool:
    """tool_response 回填消息。"""
    return msg.role == "user" and msg.content.lstrip().startswith(_TOOL_RESPONSE_PREFIX)


def validate_role_sequence(messages: Sequence["Message"]) -> Tuple[bool, str]:
    """角色序列合法性（宽松但明确）。

    规则：
    - role ∈ {system, user, assistant, tool}（构造时已校验）；
    - 首条不允许是 assistant 或 tool（须以 system/user 开头）；
    - 不允许相邻两条 assistant（模型输出不会连续出现两次 assistant）；
    - 其余放宽：连续 user 允许（tool_response 回填协议：assistant → user 工具结果 → user 提问）。
    返回 ``(ok, reason)``。trim/summary 只在 turn 边界裁剪，天然保持合法性。
    """
    if not messages:
        return True, "empty"
    first = messages[0].role
    if first in ("assistant", "tool"):
        return False, f"首条消息 role={first!r} 非法（须为 system/user）"
    prev = None
    for m in messages:
        if prev == "assistant" and m.role == "assistant":
            return False, "存在相邻两条 assistant 消息"
        prev = m.role
    return True, "ok"


# ---- 数据对象 ----------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """一条消息（不可变）。id 由 (序号, role, content) 确定性派生，hash 为内容 sha256。"""

    role: str
    content: str
    id: str
    hash: str
    category: MessageCategory

    @classmethod
    def build(cls, role: str, content: str, index: int) -> "Message":
        if role not in ("system", "user", "assistant", "tool"):
            raise IllegalMessageError(f"未知 role={role!r}（允许 system/user/assistant/tool）")
        if not content or not content.strip():
            raise IllegalMessageError("消息 content 不能为空")
        return cls(
            role=role,
            content=content,
            id=f"m{index:04d}-{stable_hash(content)[:8]}",
            hash=stable_hash(content),
            category=classify_message(role, content),
        )

    def as_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class Fact:
    """关键事实（L3）。id/hash 由 text 确定性派生；同一 text 全局去重。

    ``kind``: ``registered``（调用者注册）| ``secret_declaration`` | ``notable_statement``
    （内置确定性模式从原文提取）。fact.text 保证是某源消息 content 的精确子串（不编造）。
    """

    id: str
    text: str
    hash: str
    kind: str
    source_message_ids: Tuple[str, ...]

    @classmethod
    def create(cls, text: str, kind: str, source_message_ids: Sequence[str]) -> "Fact":
        if not text or not text.strip():
            raise IllegalMessageError("事实文本不能为空")
        return cls(
            id=f"fact-{stable_hash(text)[:16]}",
            text=text,
            hash=stable_hash(text),
            kind=kind,
            source_message_ids=tuple(source_message_ids),
        )


@dataclass(frozen=True)
class ToolProjection:
    """被裁历史中的工具调用投影（原文子串 + 结果引用 hash，不复制 tool_response 全文）。"""

    id: str
    tool: str
    args: str
    message_id: str
    hash: str
    result_message_id: Optional[str] = None
    result_hash: Optional[str] = None

    @classmethod
    def create(cls, tool: str, args: str, message_id: str,
               result_message_id: Optional[str] = None,
               result_hash: Optional[str] = None) -> "ToolProjection":
        return cls(
            id=f"tool-{stable_hash(f'{tool}({args})')[:16]}",
            tool=tool,
            args=args,
            message_id=message_id,
            hash=stable_hash(f"{tool}({args})"),
            result_message_id=result_message_id,
            result_hash=result_hash,
        )


@dataclass(repr=False)
class SummaryBlock:
    """extractive_summary 产物：有界、结构化、确定性。

    - ``facts`` / ``tool_projections`` 按被裁消息出现顺序稳定排序并去重；
    - ``covered_message_ids`` / ``covered_hash`` 记录覆盖的消息范围（可审计）；
    - ``rendered`` 为有界渲染文本，作为一条 system 消息（带 ``<summary_block`` 标记）注入；
    - ``lossless`` 恒 False：**有损候选，不伪称可从摘要恢复全部原文**；
    - ``__repr__`` 只显示概要（不含 fact 明文 / rendered 全文），防止调试日志误泄露原文；
      需要明文内容请直接访问字段（内存态，受控）。
    """

    id: str
    facts: List[Fact]
    tool_projections: List[ToolProjection]
    covered_message_ids: List[str]
    covered_hash: str
    rendered: str
    lossless: bool = False
    version: str = CONTEXT_POLICY_VERSION
    truncated: bool = False

    def __repr__(self) -> str:
        return (
            f"SummaryBlock(id={self.id!r}, facts={len(self.facts)}, "
            f"tool_projections={len(self.tool_projections)}, "
            f"covered={len(self.covered_message_ids)}, "
            f"rendered_chars={len(self.rendered)}, lossless={self.lossless}, "
            f"truncated={self.truncated})"
        )


@dataclass(frozen=True)
class ByteSpan:
    """protected 内容在输出消息中的字节区间（UTF-8，用于审计字节级保留）。"""

    message_id: str
    label: str          # system | tool_schema | fact
    start: int          # 相对该消息 content 的 UTF-8 字节起点
    end: int


@dataclass
class CompressionStats:
    """统计（需求 7）：chars / 消息数 / 估算 tokens / 压缩率 / 保护保持 / summary facts。"""

    input_chars: int = 0
    output_chars: int = 0
    input_messages: int = 0
    output_messages: int = 0
    input_tokens_est: int = 0
    output_tokens_est: int = 0
    compression_ratio: float = 0.0      # 1 - output/input（可负：膨胀时）
    protected_ids: List[str] = field(default_factory=list)
    protected_preserved: bool = True    # 所有 protected ids 均出现在输出
    summary_facts: int = 0


@dataclass(slots=True, repr=False)
class CompressionManifest:
    """策略应用的审计记录（需求 5）。

    - 记录策略版本、输入/输出 hash、保留/裁剪 ids、protected byte spans、统计；
    - ``lossless`` 语义如实声明（见模块 docstring）；summary 恒 False；
    - ``reversible`` 仅对提供 round-trip restore 的变换（DuplicateBlockCompressor）为 True；
    - 不包含任何从摘要恢复原文的伪称；原文内容不在 manifest 内。
    - **序列化契约**：唯一受支持的序列化出口是 ``to_dict()``——它**不输出任何原文内容**
      （含 fact 明文）：summary_blocks 中的 facts 仅以 ``{id, hash, kind, source_ids}``
      呈现，可审计但不可从 manifest 还原原始消息。
    - **防直接 dump 泄漏**：本对象为 ``slots`` dataclass（无 ``__dict__``，``vars()`` /
      ``__dict__`` 访问直接抛错），``__repr__`` 只显示概要——内存态字段（如
      ``summary_blocks`` 中的 ``facts[].text`` / ``rendered``）含明文事实供审计，
      **不属于序列化契约**；``vars()`` / ``dataclasses.asdict()`` / ``json.dumps(obj)``
      等直接 dump 路径不受支持（可能泄漏明文），一律使用 ``to_dict()``。
    """

    policy: str
    version: str
    input_hash: str
    output_hash: str
    kept_ids: List[str]
    trimmed_ids: List[str]
    protected_ids: List[str]
    protected_byte_spans: List[ByteSpan]
    summary_blocks: List[SummaryBlock]
    lossless: bool
    reversible: bool
    notes: List[str]
    stats: CompressionStats
    restore: Optional[Callable[[], List[Message]]] = None

    def __repr__(self) -> str:
        return (
            f"CompressionManifest(policy={self.policy!r}, version={self.version!r}, "
            f"input_hash={self.input_hash[:12]}…, output_hash={self.output_hash[:12]}…, "
            f"kept={len(self.kept_ids)}, trimmed={len(self.trimmed_ids)}, "
            f"protected={len(self.protected_ids)}, summary_blocks={len(self.summary_blocks)}, "
            f"lossless={self.lossless}, reversible={self.reversible}, stats={self.stats!r})"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "version": self.version,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "kept_ids": list(self.kept_ids),
            "trimmed_ids": list(self.trimmed_ids),
            "protected_ids": list(self.protected_ids),
            "protected_byte_spans": [vars(s) for s in self.protected_byte_spans],
            "summary_blocks": [
                {
                    "id": b.id,
                    "covered_message_ids": b.covered_message_ids,
                    "covered_hash": b.covered_hash,
                    "facts": [
                        {
                            "id": f.id,
                            "hash": f.hash,
                            "kind": f.kind,
                            "source_ids": list(f.source_message_ids),
                        }
                        for f in b.facts
                    ],
                    "tool_projections": [p.id for p in b.tool_projections],
                    "lossless": b.lossless,
                    "truncated": b.truncated,
                }
                for b in self.summary_blocks
            ],
            "lossless": self.lossless,
            "reversible": self.reversible,
            "notes": list(self.notes),
            "stats": vars(self.stats),
        }


@dataclass
class PolicyResult:
    """apply_policy 的结果。"""

    messages: List[Message]
    manifest: CompressionManifest


# ---- 配置 --------------------------------------------------------------------


@dataclass
class ContextPolicyConfig:
    """策略配置（独立于 BenchmarkConfig：本核心阶段不修改 config.py）。"""

    policy: ContextPolicy = ContextPolicy.FULL
    # 预算（字符口径为主；token 预算经 chars_per_token 折算）。None = 不设限。
    budget_chars: Optional[int] = None
    budget_tokens: Optional[int] = None
    chars_per_token: float = 3.0
    # history 保留最近 N 轮（turn = 以真实 user 提问开始的连续消息块）
    keep_recent_rounds: int = 4
    # 至少保留的真实 user 提问数（trim/summary 都保证 ≥1）
    min_user_queries: int = 1
    # extractive_summary 上限
    max_facts: int = 64
    summary_block_chars: int = 2000
    # 错误预算：非法消息/操作计数超过该值抛 ErrorBudgetExceeded（0 = 严格 fail-fast）
    error_budget: int = 0

    def __post_init__(self) -> None:
        self.policy = ContextPolicy(self.policy) if not isinstance(self.policy, ContextPolicy) else self.policy
        if self.budget_chars is not None and self.budget_chars < 0:
            raise BudgetError(f"budget_chars 必须 ≥ 0，当前 {self.budget_chars}")
        if self.budget_tokens is not None and self.budget_tokens < 0:
            raise BudgetError(f"budget_tokens 必须 ≥ 0，当前 {self.budget_tokens}")
        if self.chars_per_token <= 0:
            raise BudgetError(f"chars_per_token 必须 > 0，当前 {self.chars_per_token}")
        if self.keep_recent_rounds < 1:
            raise BudgetError(f"keep_recent_rounds 必须 ≥ 1，当前 {self.keep_recent_rounds}")
        if self.min_user_queries < 1:
            raise BudgetError(f"min_user_queries 必须 ≥ 1，当前 {self.min_user_queries}")
        if self.min_user_queries > self.keep_recent_rounds:
            # 矛盾配置：每轮（turn）以真实 user 提问开头，keep_recent_rounds 轮最多保留
            # keep_recent_rounds 个真实 user 提问；min_user_queries 超过它时即使保留全部
            # 轮次也无法满足下限 → 构造即 fail-fast，避免 trim/summary 静默违背下限。
            raise BudgetError(
                f"min_user_queries={self.min_user_queries} > keep_recent_rounds={self.keep_recent_rounds}："
                "矛盾配置，即使保留全部轮次也无法满足最少真实 user 提问数"
            )
        if self.max_facts < 0 or self.summary_block_chars < 0:
            raise BudgetError("max_facts / summary_block_chars 必须 ≥ 0")
        if self.error_budget < 0:
            raise BudgetError(f"error_budget 必须 ≥ 0，当前 {self.error_budget}")

    @property
    def effective_budget_chars(self) -> Optional[int]:
        """字符预算与 token 预算合并：取两者中更紧的一个（None = 不设限）。"""
        budgets: List[int] = []
        if self.budget_chars is not None:
            budgets.append(self.budget_chars)
        if self.budget_tokens is not None:
            budgets.append(int(self.budget_tokens * self.chars_per_token))
        return min(budgets) if budgets else None

    def from_dict(self, d: Dict[str, Any]) -> "ContextPolicyConfig":  # noqa: N802（复用既有框架风格）
        allowed = {
            "policy", "budget_chars", "budget_tokens", "chars_per_token",
            "keep_recent_rounds", "min_user_queries", "max_facts",
            "summary_block_chars", "error_budget",
        }
        kwargs = {k: v for k, v in d.items() if k in allowed}
        return ContextPolicyConfig(**kwargs)


# ---- 工具函数（纯函数，独立可测） ----------------------------------------------


def _normalized_hash(messages: Sequence[Message]) -> str:
    """规范化消息列表 hash（role+content，与消息 id 无关）。"""
    import json

    payload = json.dumps(
        [{"role": m.role, "content": m.content} for m in messages],
        ensure_ascii=False, sort_keys=True,
    )
    return stable_hash(payload)


def _group_turns(messages: Sequence[Message]) -> List[List[Message]]:
    """把历史消息按轮分组：turn = 以真实 user 提问开头的连续块（含其 assistant 回复与
    tool_response 回填）。summary/system/tool_schema 消息单独处理，不进入 turn 分组。"""
    turns: List[List[Message]] = []
    cur: List[Message] = []
    for m in messages:
        if _is_real_user_query(m):
            if cur:
                turns.append(cur)
            cur = [m]
        else:
            cur.append(m)
    if cur:
        turns.append(cur)
    return turns


def _is_protected(msg: Message, fact_message_ids: Sequence[str]) -> bool:
    """protected = L1 system + L2 tool_schema + L3 fact 消息（trim 模式）。"""
    return (
        msg.category in (MessageCategory.SYSTEM, MessageCategory.TOOL_SCHEMA, MessageCategory.SUMMARY)
        or msg.id in fact_message_ids
    )


def plan_trim(messages: Sequence[Message],
              fact_message_ids: Sequence[str],
              config: ContextPolicyConfig) -> Tuple[List[Message], List[Message], List[str]]:
    """trim 规划（纯函数，不修改输入）。

    返回 ``(kept, trimmed, notes)``：
    - 只裁 L4 history 中最旧的轮次（旧 history），L1/L2/L3 与最新 min_user_queries 个真实
      user 轮次保留；system/tool_schema 永不删；
    - 角色序列在 turn 边界裁剪，保持合法；
    - 预算不足时继续裁更旧轮，直到预算满足或只剩 min_user_queries 个真实 user 轮。
    """
    notes: List[str] = []
    budget = config.effective_budget_chars

    protected: List[Message] = []
    history: List[Message] = []
    for m in messages:
        if _is_protected(m, fact_message_ids):
            protected.append(m)
        else:
            history.append(m)

    turns = _group_turns(history)
    # 1) 先按轮数裁剪：保留最近 keep_recent_rounds 轮
    if len(turns) > config.keep_recent_rounds:
        dropped = len(turns) - config.keep_recent_rounds
        turns = turns[dropped:]
        notes.append(f"按轮数裁剪 {dropped} 个旧轮（keep_recent_rounds={config.keep_recent_rounds}）")
    # 2) 再按预算裁剪（只裁旧轮，至少保留 min_user_queries 个真实 user 轮）
    if budget is not None:
        while len(turns) > config.min_user_queries:
            kept_chars = sum(len(m.content) for m in protected)
            kept_chars += sum(len(m.content) for t in turns for m in t)
            if kept_chars <= budget:
                break
            turns = turns[1:]
            notes.append("按预算继续裁剪最旧轮")
        if budget is not None:
            final_chars = sum(len(m.content) for m in protected) + sum(
                len(m.content) for t in turns for m in t
            )
            if final_chars > budget:
                notes.append(
                    f"预算 {budget} 字符无法严格满足（最终 {final_chars}），"
                    "下限为 min_user_queries 个真实 user 轮"
                )

    kept_set = {id(m) for m in protected} | {id(m) for t in turns for m in t}
    kept = [m for m in messages if id(m) in kept_set]
    trimmed = [m for m in messages if id(m) not in kept_set]
    return kept, trimmed, notes


def extract_facts_from_messages(messages: Sequence[Message],
                                registered_facts: Sequence[Fact]) -> List[Fact]:
    """确定性事实提取（纯函数，禁止编造）。

    来源 1：调用者注册的 Fact（source_message_id 命中被裁消息）；
    来源 2：内置"明确事实"模式（秘密数字声明 / 记住类陈述），提取文本必须是源消息
    content 的**精确子串**（``content.find(text) != -1`` 校验，不满足则跳过）。
    返回按消息顺序稳定排序、同 text 去重后的 Fact 列表。
    """
    facts: List[Fact] = []
    seen: set = set()
    ids_of = {m.id: m for m in messages}
    covered = [m.id for m in messages]

    # 来源 1：注册事实（以 source_message_id 命中为准）
    for f in registered_facts:
        if any(sid in covered for sid in f.source_message_ids):
            if f.text not in seen:
                seen.add(f.text)
                facts.append(f)

    # 来源 2：内置模式（按消息顺序；提取文本必须为原文子串）
    for m in messages:
        content = m.content
        for pat, kind in ((_SECRET_RE, "secret_declaration"), (_NOTABLE_RE, "notable_statement")):
            for match in pat.finditer(content):
                text = match.group(0)
                if content.find(text) == -1:
                    continue  # 提取器防呆：非原文子串一律丢弃（不编造）
                if text in seen:
                    continue
                seen.add(text)
                facts.append(Fact.create(text=text, kind=kind, source_message_ids=[m.id]))
                if len(facts) >= 1000:  # 防御性上限
                    return facts
    return facts


def _extract_tool_projections(messages: Sequence[Message]) -> List[ToolProjection]:
    """确定性工具投影：ACTION 调用 → 工具名/参数原文子串 + 最近 tool_response 引用 hash。"""
    projections: List[ToolProjection] = []
    for m in messages:
        if m.role == "assistant":
            mt = _ACTION_RE.search(m.content)
            if mt:
                projections.append(ToolProjection.create(
                    tool=mt.group(1), args=mt.group(2), message_id=m.id,
                ))
        elif _is_tool_result(m) and projections:
            # 绑定最近一个 ACTION 投影的结果引用（重建 frozen 实例后替换）
            last = projections[-1]
            if last.result_message_id is None:
                projections[-1] = ToolProjection.create(
                    tool=last.tool, args=last.args, message_id=last.message_id,
                    result_message_id=m.id, result_hash=m.hash,
                )
    return projections


def _lines_len_with_closing(lines: Sequence[str], closing: str) -> int:
    """``"\\n".join(lines + [closing])`` 的字符数（含行间换行）。

    对任意行内容（含行内换行）该公式都与真实渲染长度一致：n 行 join 后有 n 个换行符
    （行间 n-1 个 + closing 前 1 个），``sum(len(line)+1)`` 恰好计入这 n 个换行。
    渲染前所有行都经 ``_one_line`` 单行化，行语义与计费严格一致。
    """
    return sum(len(line) + 1 for line in lines) + len(closing)


def _one_line(text: str) -> str:
    """渲染单行化：把换行/回车替换为单个空格（长度不增）。

    - 用于 summary block 渲染行（fact 文本 / 工具名 / 参数）：保证每个 fact/projection
      在渲染中恰好占**一行**，"完整行截断"语义与 ``summary_block_chars`` 字符预算严格成立；
    - ``fact.text`` 对象本身**保持原文**（精确子串不变式不被破坏）；单行化只作用于渲染视图。
    """
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _render_summary_block(block: SummaryBlock, config: ContextPolicyConfig) -> None:
    """渲染 summary block（有界）。调用方先填充 facts/tool_projections/covered 字段。

    有界语义（E15.4 reviewer 修订，与 docstring 一致）：
    - 截断以**完整行**为单位，绝不拆分 fact/projection 行；
    - ``</summary_block>`` 闭合标签**必须保留**在渲染文本末尾；
    - facts（needle 载体）是 protected：全部必须完整保留；若预算连
      header + 全部 fact 行 + 闭合标签都无法容纳 → 抛 ``BudgetError``
      （显式信号，绝不静默截断 needle）；
    - 仅 tool projections 行可被丢弃（置 ``block.truncated=True``，由调用方记 note）。
    """
    limit = config.summary_block_chars or None  # 0/None = 不设限（保持既有语义）
    header = (
        f'<summary_block id="{block.id}" covered_hash="{block.covered_hash}" '
        f'lossless="false" version="{CONTEXT_POLICY_VERSION}">'
    )
    closing = "</summary_block>"
    # 渲染行统一单行化（_one_line）：fact 文本/工具参数含换行时在渲染层替换为空格，
    # 保证每个 fact/projection 恰好占一行，行截断语义与字符预算严格一致。
    fact_lines = [f"- [fact:{f.kind}] {_one_line(f.text)}" for f in block.facts]
    proj_lines = []
    for p in block.tool_projections:
        ref = f" result_ref={p.result_hash[:8]}" if p.result_hash else ""
        proj_lines.append(
            f"- [tool] {_one_line(p.tool)}({_one_line(p.args)}) src={p.message_id}{ref}"
        )

    # 必需部分：header + facts 头 + 全部 fact 行 + 闭合标签（needle 完整性）
    lines = [header, "# facts（原文子串，稳定顺序，去重）", *fact_lines]
    if limit is not None and _lines_len_with_closing(lines, closing) > limit:
        need = _lines_len_with_closing(lines, closing)
        raise BudgetError(
            f"summary_block_chars={config.summary_block_chars} 无法容纳必需内容"
            f"（header + {len(fact_lines)} 个 fact + 闭合标签，至少需 {need} 字符）；"
            "protected facts/needle 不允许截断，请提高预算"
        )

    # 可选部分：tool projections 行按完整行、**连续前缀**从前往后容纳——
    # 一旦某行放不下立即停止（break），绝不出现"中间空洞"（后面的短行不会越过
    # 前面的长行被单独保留）；放不下的行整体丢弃并标记 truncated。
    truncated = False
    if proj_lines:
        proj_head = "# tool projections"
        if limit is None:
            lines.append(proj_head)
            lines.extend(proj_lines)
        else:
            for line in [proj_head, *proj_lines]:
                if _lines_len_with_closing(lines + [line], closing) <= limit:
                    lines.append(line)
                else:
                    truncated = True
                    break
    block.rendered = "\n".join([*lines, closing])
    block.truncated = truncated


def plan_extractive_summary(messages: Sequence[Message],
                            registered_facts: Sequence[Fact],
                            config: ContextPolicyConfig) -> Tuple[List[Message], List[Message],
                                                                  Optional[SummaryBlock], List[str]]:
    """extractive_summary 规划（纯函数，不修改输入；不调用 LLM）。

    与 trim 的差异：L3 fact 消息**允许被裁**，但其明确事实与工具投影进入 SummaryBlock
    （needle 仍以结构化形式保留在输出中）；L1/L2 永不删。
    返回 ``(kept, trimmed, summary_block, notes)``。
    """
    notes: List[str] = []
    budget = config.effective_budget_chars

    # L3 fact 消息在 extractive_summary 下不保护原文（投影进 summary），只保护 L1/L2/SUMMARY
    protected: List[Message] = []
    history: List[Message] = []
    for m in messages:
        if m.category in (MessageCategory.SYSTEM, MessageCategory.TOOL_SCHEMA, MessageCategory.SUMMARY):
            protected.append(m)
        else:
            history.append(m)

    turns = _group_turns(history)
    if len(turns) > config.keep_recent_rounds:
        turns = turns[len(turns) - config.keep_recent_rounds:]
        notes.append(f"按轮数保留最近 {config.keep_recent_rounds} 轮")
    if budget is not None:
        while len(turns) > config.min_user_queries:
            kept_chars = sum(len(m.content) for m in protected)
            kept_chars += sum(len(m.content) for t in turns for m in t)
            if kept_chars <= budget:
                break
            turns = turns[1:]
            notes.append("按预算继续裁剪最旧轮")
        # 与 trim 一致：预算无法严格满足时如实记录（不伪称满足）；
        # 若 protected + min_user_queries 个真实 user 轮已超限，同样明确记录。
        final_chars = sum(len(m.content) for m in protected) + sum(
            len(m.content) for t in turns for m in t
        )
        if final_chars > budget:
            notes.append(
                f"预算 {budget} 字符无法严格满足（最终 {final_chars}），"
                "下限为 min_user_queries 个真实 user 轮"
            )

    kept_set = {id(m) for m in protected} | {id(m) for t in turns for m in t}
    kept = [m for m in messages if id(m) in kept_set]
    trimmed = [m for m in messages if id(m) not in kept_set]
    if not trimmed:
        return kept, trimmed, None, notes

    # ---- 生成 summary block ----
    facts = extract_facts_from_messages(trimmed, registered_facts)
    if len(facts) > config.max_facts:
        notes.append(f"事实数 {len(facts)} 超 max_facts={config.max_facts}，截断保留前段")
        facts = facts[: config.max_facts]
    projections = _extract_tool_projections(trimmed)
    block = SummaryBlock(
        id=f"summary-{stable_hash(_normalized_hash(trimmed))[:16]}",
        facts=facts,
        tool_projections=projections,
        covered_message_ids=[m.id for m in trimmed],
        covered_hash=_normalized_hash(trimmed),
        rendered="",
    )
    _render_summary_block(block, config)
    # 膨胀如实报告：summary 块开销超过被裁内容（短历史/元信息密集时）→ 负压缩率，
    # 不美化（E15 原则：不为炫技虚报收益）。
    trimmed_chars = sum(len(m.content) for m in trimmed)
    if len(block.rendered) > trimmed_chars:
        notes.append(
            f"summary 块渲染 {len(block.rendered)} 字符 > 被裁内容 {trimmed_chars} 字符，"
            "无净收益（膨胀）；压缩率如实为负"
        )
    return kept, trimmed, block, notes


# ---- ThreadState ---------------------------------------------------------------


@dataclass(frozen=True)
class ThreadSnapshot:
    """ThreadState 快照（字段不可变；summary_blocks / manifest 为快照时刻的**深拷贝**）。

    完整 round-trip 语义：messages / facts / summary_blocks / fact_message_ids（trim 的
    fact 消息保护索引）/ errors（完整错误列表）/ seq（消息序号）/ manifest 全部保存并在
    ``restore`` 时恢复；仅可恢复到同 thread_id 的 state。
    """

    thread_id: str
    messages: Tuple[Message, ...]
    facts: Tuple[Fact, ...]
    summary_blocks: Tuple[SummaryBlock, ...]
    fact_message_ids: Tuple[str, ...]
    errors: Tuple[str, ...]
    seq: int
    manifest: Optional[CompressionManifest]


class ThreadState:
    """thread-scoped 上下文状态（需求 2/6）。

    - 每 thread 独立（thread_id 唯一标识）；system/tool schema 永不删；
    - ``register_fact`` 注册关键事实（稳定 id/hash，去重）；
    - ``apply_policy`` 按配置执行 full/trim/extractive_summary；
    - snapshot/restore/purge；跨 thread restore / 非法消息 / 预算非法 / 错误预算超限显式异常。
    """

    def __init__(self, thread_id: str, config: Optional[ContextPolicyConfig] = None) -> None:
        if not thread_id:
            raise IllegalMessageError("thread_id 不能为空")
        self.thread_id = thread_id
        self.config = config or ContextPolicyConfig()
        self._messages: List[Message] = []
        self._facts: Dict[str, Fact] = {}
        self._summary_blocks: List[SummaryBlock] = []
        self._seq = 0
        self._fact_message_ids: List[str] = []
        self._errors: List[str] = []
        self.manifest: Optional[CompressionManifest] = None

    # ---- 只读视图 ----
    @property
    def messages(self) -> List[Message]:
        return list(self._messages)

    @property
    def facts(self) -> Dict[str, Fact]:
        return dict(self._facts)

    @property
    def summary_blocks(self) -> List[SummaryBlock]:
        return list(self._summary_blocks)

    @property
    def error_count(self) -> int:
        return len(self._errors)

    # ---- 错误预算 ----
    # 语义：append() 永远 fail-fast 抛原始异常（IllegalMessageError）并计数；
    # load_messages() 批量导入容忍 error_budget 次错误，超过才抛 ErrorBudgetExceeded。
    # 这是"错误预算"的显式异常边界：预算=0 表示批量导入零容忍。

    # ---- 消息管理 ----
    def append(self, role: str, content: str) -> Message:
        """追加一条消息（fail-fast：role/content/system 位置非法立即抛 IllegalMessageError，
        并计入错误计数，供错误预算审计）。"""
        try:
            if role == "system" and self._messages and self._messages[0].role != "system":
                raise IllegalMessageError(
                    "system 消息必须位于消息序列头部（现有序列以非 system 开头）"
                )
            msg = Message.build(role, content, self._seq)
        except IllegalMessageError as exc:
            self._errors.append(str(exc))
            raise
        self._seq += 1
        self._messages.append(msg)
        return msg

    def append_message(self, msg: Message) -> Message:
        """追加一条已构造的 Message：复用 ``append`` 的全部校验（role 合法 / content 非空 /
        system 位置）并正确推进 seq（防止后续消息 id 冲突）。

        **id 语义**：返回消息的 id 由 ``(thread 内当前序号, role, content)`` 确定性**重新派生**，
        **不继承** 传入 ``msg.id``（传入 id 可能来自其他 thread 或外部序号；本方法保证 thread
        内 id 唯一且与内部 seq 一致）。仅当 ``msg`` 的原 index 恰好等于 thread 当前 ``_seq``
        时，重派生 id 才与 ``msg.id`` 相同。校验失败时与 ``append`` 一致：抛
        ``IllegalMessageError`` 并计入错误计数。
        """
        return self.append(msg.role, msg.content)

    def load_messages(self, messages: Sequence[Dict[str, str]]) -> int:
        """批量导入（错误预算语义）：坏消息计入错误计数并跳过；超过 error_budget
        抛 ErrorBudgetExceeded。返回成功导入条数。

        用于从 workload 现有 dict 消息初始化，不改写 workload 代码。
        """
        imported = 0
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            try:
                self.append(role or "", content)
                imported += 1
            except IllegalMessageError as exc:
                # append 已计数；此处只做预算边界判定
                if self.error_count > self.config.error_budget:
                    raise ErrorBudgetExceeded(
                        f"错误计数 {self.error_count} 超过 error_budget={self.config.error_budget}：{exc}"
                    ) from exc
                continue  # 预算内：跳过坏消息
        return imported

    # ---- 事实（L3） ----
    def register_fact(self, text: str, source_message_id: Optional[str] = None) -> Fact:
        """注册关键事实：稳定 id/hash，同 text 去重（返回已有 Fact）。"""
        fact = Fact.create(text=text, kind="registered",
                           source_message_ids=[source_message_id] if source_message_id else [])
        if fact.id in self._facts:
            return self._facts[fact.id]
        self._facts[fact.id] = fact
        if source_message_id:
            self._fact_message_ids.append(source_message_id)
        return fact

    # ---- 策略应用 ----
    def apply_policy(self) -> PolicyResult:
        """按 config.policy 执行上下文策略，产出新的消息列表 + CompressionManifest。

        幂等性（reviewer 修订）：本方法**不修改** ThreadState 的消息 / 序号（``_seq``）/
        facts；重复调用输出 hash 与消息 ids 稳定。唯一副作用是更新 ``manifest`` 与
        ``summary_blocks`` 缓存（策略应用结果的只读视图，供审计/快照使用）。
        """
        msgs = list(self._messages)
        input_hash = _normalized_hash(msgs)
        input_chars = sum(len(m.content) for m in msgs)
        input_tokens = sum(estimate_tokens(m.content, self.config.chars_per_token) for m in msgs)
        notes: List[str] = []
        summary_blocks: List[SummaryBlock] = []
        lossless: bool
        reversible = False
        restore: Optional[Callable[[], List[Message]]] = None

        if self.config.policy == ContextPolicy.FULL:
            out = msgs
            trimmed: List[Message] = []
            lossless = True  # identity：未做任何变换
            notes.append("policy=full：现状，不做任何压缩/改写")
        elif self.config.policy == ContextPolicy.TRIM:
            out, trimmed, notes_t = plan_trim(msgs, self._fact_message_ids, self.config)
            notes = notes_t
            lossless = False  # 有损候选：输入 token 改变，greedy 输出一致性未验证
            notes.append("policy=trim：有损候选（未验证 greedy 输出一致，需真实模型 paired）")
        elif self.config.policy == ContextPolicy.EXTRACTIVE_SUMMARY:
            out, trimmed, block, notes_e = plan_extractive_summary(msgs, list(self._facts.values()),
                                                                   self.config)
            notes = notes_e
            lossless = False
            notes.append(
                "policy=extractive_summary：有损候选，不伪称可从摘要恢复全部原文；"
                "不调用 LLM，仅原文子串事实投影"
            )
            if block is not None:
                summary_blocks.append(block)
                # summary block 渲染文本作为一条 system 消息注入，位置在**所有**
                # L1 system / L2 tool_schema（及既有 SUMMARY）之后、第一个历史消息之前。
                sblock_msg = Message(
                    role="system",
                    content=block.rendered,
                    # id 由内容 hash 确定性派生：重复调用 apply_policy 时稳定，
                    # 且**不推进** self._seq（apply_policy 无序号副作用，幂等）。
                    id=f"summary-{stable_hash(block.rendered)[:16]}",
                    hash=stable_hash(block.rendered),
                    category=MessageCategory.SUMMARY,
                )
                insert_at = 0
                while insert_at < len(out) and out[insert_at].category in (
                    MessageCategory.SYSTEM,
                    MessageCategory.TOOL_SCHEMA,
                    MessageCategory.SUMMARY,
                ):
                    insert_at += 1
                out.insert(insert_at, sblock_msg)
                notes.append(f"注入 summary block {block.id}（覆盖 {len(block.covered_message_ids)} 条消息）")
                # 注入后的 role sequence 再验证：失败必须显式异常，不得静默放行
                ok_seq, reason = validate_role_sequence(out)
                if not ok_seq:
                    raise IllegalMessageError(
                        f"summary 注入后角色序列非法：{reason}"
                    )
        else:  # pragma: no cover - 枚举兜底
            raise ContextPolicyError(f"未知策略 {self.config.policy!r}")

        kept_ids = [m.id for m in out]
        trimmed_ids = [m.id for m in trimmed]
        protected_ids = [m.id for m in msgs if _is_protected(m, self._fact_message_ids)]
        protected_preserved = all(pid in kept_ids for pid in protected_ids)

        # protected byte spans（输出消息内的 UTF-8 字节区间，整条保护）
        spans: List[ByteSpan] = []
        for m in out:
            if m.id in protected_ids:
                label = m.category.value if m.category != MessageCategory.SUMMARY else "summary"
                spans.append(ByteSpan(message_id=m.id, label=label,
                                      start=0, end=len(m.content.encode("utf-8"))))

        output_chars = sum(len(m.content) for m in out)
        output_tokens = sum(estimate_tokens(m.content, self.config.chars_per_token) for m in out)
        stats = CompressionStats(
            input_chars=input_chars,
            output_chars=output_chars,
            input_messages=len(msgs),
            output_messages=len(out),
            input_tokens_est=input_tokens,
            output_tokens_est=output_tokens,
            compression_ratio=(1.0 - output_chars / input_chars) if input_chars else 0.0,
            protected_ids=protected_ids,
            protected_preserved=protected_preserved,
            summary_facts=sum(len(b.facts) for b in summary_blocks),
        )

        self.manifest = CompressionManifest(
            policy=self.config.policy.value,
            version=CONTEXT_POLICY_VERSION,
            input_hash=input_hash,
            output_hash=_normalized_hash(out),
            kept_ids=kept_ids,
            trimmed_ids=trimmed_ids,
            protected_ids=protected_ids,
            protected_byte_spans=spans,
            summary_blocks=summary_blocks,
            lossless=lossless,
            reversible=reversible,
            notes=notes,
            stats=stats,
            restore=restore,
        )
        self._summary_blocks = list(summary_blocks)
        return PolicyResult(messages=out, manifest=self.manifest)

    # ---- snapshot / restore / purge ----
    def snapshot(self) -> ThreadSnapshot:
        """取快照。``SummaryBlock`` / ``CompressionManifest`` 为可变结构，快照中**深拷贝**，
        与 thread 当前状态完全隔离（后续修改任一方不影响另一方）；Message/Fact 为不可变
        对象，直接引用安全。``errors`` 保存**完整错误列表**、``seq`` 保存消息序号，
        ``fact_message_ids`` 保存 fact 消息保护索引——完整 round-trip。"""
        return ThreadSnapshot(
            thread_id=self.thread_id,
            messages=tuple(self._messages),
            facts=tuple(self._facts.values()),
            summary_blocks=tuple(copy.deepcopy(b) for b in self._summary_blocks),
            fact_message_ids=tuple(self._fact_message_ids),
            errors=tuple(self._errors),
            seq=self._seq,
            manifest=copy.deepcopy(self.manifest),
        )

    def restore(self, snapshot: ThreadSnapshot) -> None:
        if snapshot.thread_id != self.thread_id:
            raise CrossThreadRestoreError(
                f"snapshot 属于 thread={snapshot.thread_id!r}，不能恢复到 thread={self.thread_id!r}"
            )
        self._messages = list(snapshot.messages)
        self._facts = {f.id: f for f in snapshot.facts}
        # 深拷贝：restore 后 state 与快照中的可变 SummaryBlock 对象互不共享
        self._summary_blocks = [copy.deepcopy(b) for b in snapshot.summary_blocks]
        self._fact_message_ids = list(snapshot.fact_message_ids)
        self._errors = list(snapshot.errors)
        self._seq = snapshot.seq
        # manifest 一并恢复（purge 后 restore 同样回到快照时刻的审计视图）
        self.manifest = copy.deepcopy(snapshot.manifest)

    def purge(self) -> None:
        """清空 thread 状态（保留 thread_id 与 config）。"""
        self._messages = []
        self._facts = {}
        self._summary_blocks = []
        self._fact_message_ids = []
        self._errors = []
        self._seq = 0
        self.manifest = None

    # ---- 诊断 ----
    def describe(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "policy": self.config.policy.value,
            "messages": len(self._messages),
            "facts": len(self._facts),
            "summary_blocks": len(self._summary_blocks),
            "error_count": len(self._errors),
        }


# ---- deterministic reversible preprocessor（与 summary 严格分开） ------------------


class DuplicateBlockCompressor:
    """deterministic reversible preprocessor（需求 5 后半段）。

    - 只对**完全重复块**做 dictionary/ref 去重：同一 content 第二次出现 → 替换为
      ``\\x00REF:<block_id>\\x00`` 引用，block_id 由 content hash 确定性派生；
    - ``restore`` 提供 round-trip 还原（compress 后再 restore 与原文逐条相等）；
    - 与 extractive_summary **严格分开**（独立类、独立 manifest、独立测试）：
      本变换是信息论可逆的（round-trip lossless），但**不声称** greedy 输出一致
      （BPE 边界风险，E12 教训），因此也不标 E15.0 的 lossless 门禁；
    - 原文中的 ``\\x00`` 会被转义，保证 round-trip 严格成立。
    """

    @staticmethod
    def compress(messages: Sequence[Message]) -> Tuple[List[Message], Dict[str, str]]:
        """返回 ``(compressed, dictionary)``，dictionary 为 ``block_id -> 转义后原文``。"""
        dictionary: Dict[str, str] = {}   # block_id -> content（\x00 已转义）
        seen: Dict[str, str] = {}         # content -> block_id（去重判断）
        out: List[Message] = []
        for m in messages:
            content = m.content.replace("\x00", _ESC)
            block_id = seen.get(content)
            if block_id is None:
                block_id = f"b{stable_hash(content)[:16]}"
                seen[content] = block_id
                dictionary[block_id] = content
                out.append(m)
            else:
                out.append(Message(
                    role=m.role,
                    content=f"{_REF_PREFIX}{block_id}{_REF_SUFFIX}",
                    id=m.id,
                    hash=m.hash,
                    category=m.category,
                ))
        return out, dictionary

    @staticmethod
    def restore(compressed: Sequence[Message], dictionary: Dict[str, str]) -> List[Message]:
        ref_re = re.compile(re.escape(_REF_PREFIX) + r"([A-Za-z0-9]+)" + re.escape(_REF_SUFFIX))
        out: List[Message] = []
        for m in compressed:
            content = m.content
            if content.startswith(_REF_PREFIX) and content.endswith(_REF_SUFFIX):
                mt = ref_re.fullmatch(content)
                if mt is None or mt.group(1) not in dictionary:
                    raise ContextPolicyError(f"无法还原引用块 {content!r}：字典缺失")
                content = dictionary[mt.group(1)].replace(_ESC, "\x00")
            else:
                content = content.replace(_ESC, "\x00")
            out.append(Message(role=m.role, content=content, id=m.id,
                               hash=m.hash, category=m.category))
        return out
