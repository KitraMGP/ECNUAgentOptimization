"""prompt_preprocessor —— E15.3 B1：deterministic structured-lossless prompt preprocessor。

设计定位（对应 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md` 技术线 B 的 B1 候选：
"应用层确定性结构化压缩"，默认关闭、可回滚）：
- **纯应用层**：在请求发送前对 OpenAI 兼容 ``messages`` 做确定性结构化压缩，
  不触碰 llama.cpp / KV / server cache identity。
- **压缩内容（H1 system 合同，永不违反）**：仅允许对**完全重复**的、非安全、非
  protected 的**历史消息块**做 dictionary/ref 去重；system prompt / 禁止指令 /
  安全约束 / tool schema 本体 / 已注册关键事实 / tool payload 引用协议消息在
  压缩输出中**逐字节保留且不得替换**。
- **块 = 单条消息的完整 content**（消息级完全重复去重）：同 content 第二次出现
  （任何 role，出现顺序全局判定）→ 替换为 ``\\x00REF:<block_id>\\x00``；
  block_id = ``b`` + sha256(content)[:16]（内容寻址，确定性）。
- **无损语义（如实声明，与 E15.4 DuplicateBlockCompressor 一致）**：
  - ``reversible=True``：信息论可逆——``restore`` 逐消息/逐字节还原原输入
    （round-trip，测试验证）；
  - ``lossless=False``：**不声称** greedy 输出一致（BPE 边界风险，E12 教训）——
    E15.0 门禁意义的 lossless 需 4B greedy paired 验证（同 seed/temp=0 输出 token
    完全一致 + 停止原因一致），本模块不做该伪称。
- **fail-fast（默认）**：原始内容含 NUL（破坏 REF 标记可逆性）、畸形 manifest、未知
  ref、hash mismatch、跨 thread/session 使用均抛显式异常（见异常体系）；自有 REF
  产物（压缩输出的幂等再入，如 400 重试/嵌套压缩）被识别并跳过，伪 REF 在 restore
  的 ref/hash 校验处 fail-fast。
- **无净收益 → identity**：替换收益逐条判定（被替换 content 字节数必须大于 REF
  标记长度）+ 全局判定（output_bytes >= input_bytes 时返回原始输入），绝不膨胀。
- **审计**：每次压缩产出 ``PreprocessorManifest``（模式/版本/输入输出 hash/替换
  与保留区间/统计），``to_dict()`` 是唯一受支持序列化出口且**不含任何原文**——
  replaced/protected 区间只含 ref/hash/label/字节区间，可审计但不可从 manifest
  还原原始消息；原文仅存在于 ``PreprocessorDictionary``（进程内内存对象，
  restore 必需，thread-scoped）。
- **接入**：Driver 单点（``framework/driver.py``），配置经
  ``BenchmarkConfig.extra["preprocessor"]``（与 E15.2 ``tool_payload_mode`` 同一
  惯例），默认 ``off`` = 旧行为零改动。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 策略/数据结构版本（审计用；变更压缩格式或 manifest 结构时必须 bump 主版本）
PREPROCESSOR_VERSION = "1.0.0"

# 模式
PREPROCESSOR_MODES: Tuple[str, ...] = ("off", "structured_lossless")
DEFAULT_PREPROCESSOR_MODE = "off"
# 兼容 E15.0 计划中 CLI 名 `--preprocessor structured-lossless` 的写法
_PREPROCESSOR_ALIASES: Dict[str, str] = {
    "structured-lossless": "structured_lossless",
}

# REF 标记（与 context_policy.DuplicateBlockCompressor 同一格式族）
_REF_PREFIX = "\x00REF:"
_REF_SUFFIX = "\x00"
# block_id = "b" + 16 位小写 hex；REF 标记总字节数（含两侧 \x00）
_REF_LEN = len(_REF_PREFIX.encode("utf-8")) + 1 + 16 + len(_REF_SUFFIX.encode("utf-8"))
assert _REF_LEN == 23, _REF_LEN

_REF_FULL_RE = re.compile(
    re.escape(_REF_PREFIX) + r"b[0-9a-f]{16}" + re.escape(_REF_SUFFIX)
)

# ---- 异常（fail-fast 默认）------------------------------------------------------


class PreprocessorError(Exception):
    """prompt_preprocessor 全部异常的基类。"""


class PreprocessorNulError(PreprocessorError):
    """输入消息 content 含 NUL（\\x00）：会与 REF 标记冲突，fail-fast（不做转义）。"""


class PreprocessorManifestError(PreprocessorError):
    """manifest 畸形：模式/版本不支持、字段缺失或类型错误（fail-fast）。"""


class PreprocessorUnknownRefError(PreprocessorError):
    """restore 时遇到 REF 但 dictionary 中无对应 block_id（fail-fast）。"""


class PreprocessorHashMismatchError(PreprocessorError):
    """hash 校验失败：restore 输入与 manifest.output_hash 不符 / 还原结果与
    manifest.input_hash 不符 / block_id 与 dictionary 内容 hash 不符（fail-fast）。"""


class PreprocessorCrossThreadError(PreprocessorError):
    """dictionary 跨线程使用（restore 线程与压缩线程不同，fail-fast）。"""


# ---- 纯函数 ----------------------------------------------------------------


def stable_hash(text: str) -> str:
    """确定性的内容 hash（sha256 hex）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str, chars_per_token: float = 3.0) -> int:
    """确定性 token 估算（不精确，非 BPE；仅用于统计口径，与 E15.4 同一语义）。"""
    if chars_per_token <= 0:
        raise PreprocessorError(f"chars_per_token 必须 > 0，当前 {chars_per_token}")
    import math

    return max(1, math.ceil(len(text) / chars_per_token))


def normalized_hash(messages: Sequence[Dict[str, Any]]) -> str:
    """规范化消息列表 hash（role+content，与 index/其他字段无关）。"""
    payload = json.dumps(
        [{"role": m["role"], "content": m["content"]} for m in messages],
        ensure_ascii=False, sort_keys=True,
    )
    return stable_hash(payload)


def message_id(index: int, content: str) -> str:
    """消息确定性 id（审计用；与 context_policy.Message.build 同风格）。"""
    return f"m{index:04d}-{stable_hash(content)[:8]}"


# 关键事实声明模式（与 context_policy.py 的 _SECRET_RE/_NOTABLE_RE 语义一致：
# 秘密数字声明 / 记住类陈述。B1 采用保守判定——命中前缀即 protected，宁可多保护）。
_SECRET_DECL_RE = re.compile(r"请记住这个秘密数字[:：]\s*\S+")
_NOTABLE_RE = re.compile(r"(?:请记住|请注意|重要[:：])")

# tool payload 引用协议特征（E15.2 H3 协议：projection 占位 / resolve 动作）
_TOOL_PAYLOAD_PROTOCOL_MARKERS = (
    "payload_ref=",
    "resolve_tool_payload",
    "[tool_payload externalized]",
)


def is_protected_message(
    role: str, content: str, fact_texts: Sequence[str] = ()
) -> Tuple[bool, Optional[str]]:
    """protected 判定（纯函数）。返回 ``(is_protected, label)``。

    protected（逐字节保留，永不替换）：
    - ``role == "system"`` → label ``"system"``（system prompt / 禁止指令 / 安全
      约束 / tool schema 本体全部位于 system 消息，H1 system 合同）；
    - 关键事实声明模式（秘密数字 / 记住类陈述）→ ``"fact"``；
    - tool payload 引用协议特征（payload_ref= / resolve_tool_payload /
      externalized 标记）→ ``"tool_payload_protocol"``；
    - 内容包含任一显式注册 fact 文本（``fact_texts``）→ ``"fact"``。
    其余（普通历史轮次消息）→ ``(False, None)``，可压缩。
    """
    if role == "system":
        return True, "system"
    if _SECRET_DECL_RE.search(content) or _NOTABLE_RE.search(content):
        return True, "fact"
    if any(marker in content for marker in _TOOL_PAYLOAD_PROTOCOL_MARKERS):
        return True, "tool_payload_protocol"
    for fact in fact_texts:
        if fact and fact in content:
            return True, "fact"
    return False, None


# ---- 数据对象 ----------------------------------------------------------------


@dataclass(frozen=True)
class RefSpan:
    """替换区间：输出消息列表中某条消息的 content 被替换为 REF。

    只含 ref / content_hash / 字节区间，**不含原文**（可审计、不可还原）。
    """

    message_index: int
    message_id: str
    ref: str                  # block_id（b + sha256(content)[:16]，内容寻址）
    content_hash: str         # 被替换原文的 sha256（全量 hex，审计对照用）
    start: int                # 相对该消息 content 的 UTF-8 字节起点（整条替换 = 0）
    end: int                  # 字节终点（整条替换 = REF 标记字节数）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_index": self.message_index,
            "message_id": self.message_id,
            "ref": self.ref,
            "content_hash": self.content_hash,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True)
class ProtectedSpan:
    """保留区间：protected 消息在输出中逐字节保留（字节级保留审计）。"""

    message_index: int
    message_id: str
    label: str                # system | fact | tool_payload_protocol
    start: int
    end: int                  # 整条消息 content 的 UTF-8 字节长度

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_index": self.message_index,
            "message_id": self.message_id,
            "label": self.label,
            "start": self.start,
            "end": self.end,
        }


@dataclass
class PreprocessorStats:
    """统计（字符/字节/消息数/估算 token/替换与保护数/压缩率）。"""

    input_chars: int = 0
    output_chars: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    input_messages: int = 0
    output_messages: int = 0
    input_tokens_est: int = 0
    output_tokens_est: int = 0
    replaced_messages: int = 0
    protected_messages: int = 0
    compression_ratio: float = 0.0     # 1 - output_bytes/input_bytes（identity 时 0）
    compressed: bool = False           # 是否实际发生压缩（有净收益）

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in vars(self).items()}


@dataclass(slots=True, repr=False)
class PreprocessorManifest:
    """压缩审计记录（需求 1：版本化 manifest / input/output hash / 替换/保留区间）。

    - 记录模式、版本、输入/输出 hash、替换区间（RefSpan）、保留区间
      （ProtectedSpan）、统计与说明；
    - ``reversible=True``：round-trip 可逆（restore 逐字节还原，测试验证）；
    - ``lossless=False``：**不声称** greedy 输出一致（E15.0 门禁需 4B paired）；
    - **序列化契约**：唯一受支持的序列化出口是 ``to_dict()``——**不输出任何原文
      内容**（替换/保留区间只含 ref/hash/label/字节区间）；本对象为 ``slots``
      dataclass（无 ``__dict__``），``__repr__`` 只显示概要。
    """

    mode: str
    version: str
    input_hash: str
    output_hash: str
    compressed: bool
    reversible: bool = True
    lossless: bool = False
    replaced_spans: List[RefSpan] = field(default_factory=list)
    protected_spans: List[ProtectedSpan] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    stats: PreprocessorStats = field(default_factory=PreprocessorStats)

    def __repr__(self) -> str:
        return (
            f"PreprocessorManifest(mode={self.mode!r}, version={self.version!r}, "
            f"input_hash={self.input_hash[:12]}…, output_hash={self.output_hash[:12]}…, "
            f"compressed={self.compressed}, replaced={len(self.replaced_spans)}, "
            f"protected={len(self.protected_spans)}, reversible={self.reversible}, "
            f"lossless={self.lossless}, stats={self.stats!r})"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "version": self.version,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "compressed": self.compressed,
            "reversible": self.reversible,
            "lossless": self.lossless,
            "replaced_spans": [s.to_dict() for s in self.replaced_spans],
            "protected_spans": [s.to_dict() for s in self.protected_spans],
            "notes": list(self.notes),
            "stats": self.stats.to_dict(),
        }


class PreprocessorDictionary:
    """restore 必需的内存字典（``block_id -> 原文 content``），thread-scoped。

    - 绑定创建线程（``threading.get_ident``）与随机 session_id；**任何跨线程使用
      fail-fast**（``PreprocessorCrossThreadError``）；
    - block_id = ``b`` + sha256(content)[:16]，内容寻址：即使跨 session 误配，
      内容不符会在 restore 的 hash 自验证处 fail-fast（``PreprocessorHashMismatchError``）；
    - **本对象是原文的唯一载体**，不得写入 manifest / 日志 / 结果 JSON。
    """

    def __init__(self) -> None:
        self._owner_ident = threading.get_ident()
        self._session_id = uuid.uuid4().hex
        self._blocks: Dict[str, str] = {}

    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner_ident:
            raise PreprocessorCrossThreadError(
                f"dictionary 绑定线程 {self._owner_ident}，当前线程 "
                f"{threading.get_ident()}（thread-scoped，fail-fast）"
            )

    def add(self, ref: str, content: str) -> None:
        """登记一个被引用的块（内容寻址自验证：ref 必须与 content hash 匹配）。"""
        expected = block_id_for(content)
        if ref != expected:
            raise PreprocessorHashMismatchError(
                f"ref {ref!r} 与内容 hash 派生 {expected!r} 不符（fail-fast）"
            )
        self._check_thread()
        self._blocks[ref] = content

    def resolve(self, ref: str) -> str:
        """取回原文；ref 缺失 → ``PreprocessorUnknownRefError``（fail-fast）。"""
        self._check_thread()
        content = self._blocks.get(ref)
        if content is None:
            raise PreprocessorUnknownRefError(
                f"dictionary 中无 block_id={ref!r}（未知 ref / 跨 session 错用，fail-fast）"
            )
        return content

    @property
    def owner_ident(self) -> int:
        self._check_thread()
        return self._owner_ident

    @property
    def session_id(self) -> str:
        self._check_thread()
        return self._session_id

    def block_ids(self) -> List[str]:
        self._check_thread()
        return sorted(self._blocks)

    def __len__(self) -> int:
        self._check_thread()
        return len(self._blocks)


def block_id_for(content: str) -> str:
    """block_id：``b`` + sha256(content)[:16]（确定性内容寻址）。"""
    return "b" + stable_hash(content)[:16]


def ref_content_for(ref: str) -> str:
    """把 block_id 渲染为 REF 标记文本（``\\x00REF:<block_id>\\x00``）。"""
    return f"{_REF_PREFIX}{ref}{_REF_SUFFIX}"


def ref_token_est() -> int:
    """REF 标记的估算 token（chars/3 口径）。

    REF 标记文本 23 字符 → 估算 8 token。**注意**：真实 BPE 下 REF 标记在 Qwen
    tokenizer 约 19 token（\x00 控制符与 ASCII 混排难以合并成更大 token）——估算
    对 ASCII 高估 token 数。因此净收益的 token 门槛取 ``ref_token_est() * 2``
    （保守，见 ``_gain_token_threshold``）：宁可在估算窗口内错失收益，也绝不
    （在估算口径下）膨胀（需求 2 硬约束）。
    """
    return estimate_tokens(ref_content_for("b" + "0" * 16))


def _gain_token_threshold() -> int:
    """净收益判定的 token 门槛（保守值 = REF 估算 token × 2，即 16）。

    - 中文/日文等密集编码（约 1 token/字符）：门槛 16 估算 token ≈ 48 字符 ≈ 48
      真实 token >> REF 真实 19 token，有真实收益 ✓；
    - 英文等稀疏编码（约 4 字符/token）：48 字符 ≈ 12 真实 token < REF 19 → 估算
      判定替换但真实可能膨胀（**已知窗口**：48–76 字符的 ASCII 重复块）——由
      paired 门禁 M2（真实 prompt_tokens 收益）兜底判 HOLD；估算口径内绝不膨胀。
    """
    return ref_token_est() * 2


@dataclass
class PreprocessorResult:
    """一次压缩的结果：压缩后消息 + manifest + restore 必需 dictionary。"""

    messages: List[Dict[str, Any]]
    manifest: PreprocessorManifest
    dictionary: PreprocessorDictionary


# ---- 压缩 / 还原（纯函数）--------------------------------------------------


def structured_lossless_compress(
    messages: Sequence[Dict[str, Any]],
    fact_texts: Sequence[str] = (),
    min_block_chars: int = _REF_LEN,
) -> PreprocessorResult:
    """deterministic structured-lossless 压缩（纯函数，不修改输入）。

    - 只对**完全重复**的非 protected 历史消息做 dictionary/ref 去重；
    - protected（system / 关键事实 / tool payload 协议 / 注册 fact 文本）逐字节保留；
    - 输入含 NUL → ``PreprocessorNulError``（fail-fast，REF 标记不可逆冲突）；
    - 替换逐条收益判定（被替换 content 字节数须 > ``min_block_chars``）+ 全局
      净收益判定（无净收益 → identity，绝不膨胀）；
    - 返回 ``PreprocessorResult(messages=压缩后消息, manifest, dictionary)``：
      同输入 → 逐字节相同输出 + 相同 ``manifest.to_dict()``（确定性）。
    """
    if min_block_chars < 1:
        raise PreprocessorError(f"min_block_chars 必须 >= 1，当前 {min_block_chars}")

    out: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}          # content -> block_id（首次出现登记）
    dictionary = PreprocessorDictionary()
    replaced_spans: List[RefSpan] = []
    protected_spans: List[ProtectedSpan] = []
    notes: List[str] = []
    short_duplicates = 0               # 重复但字节数 ≤ min_block_chars（无收益保留）的条数

    for idx, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content")
        if role not in ("system", "user", "assistant", "tool"):
            raise PreprocessorError(
                f"未知 role={role!r}（允许 system/user/assistant/tool，fail-fast）"
            )
        if not isinstance(content, str):
            raise PreprocessorError(
                f"消息 content 必须是 str，实际 {type(content).__name__}（fail-fast）"
            )
        if "\x00" in content:
            if _REF_FULL_RE.fullmatch(content):
                # 自有 REF 产物（幂等场景：压缩输出被再次送入，如 400 重试/嵌套压缩）：
                # 原样保留、跳过压缩与 seen 登记——压缩器对自有产物幂等。
                # 伪 REF（非本压缩器产出）在 restore 的 ref/hash 校验处 fail-fast。
                out.append(dict(m))
                continue
            raise PreprocessorNulError(
                f"消息 {idx} content 含 NUL（\\x00），与 REF 标记冲突（fail-fast）"
            )
        protected, label = is_protected_message(role, content, fact_texts)
        if protected:
            out.append(dict(m))
            protected_spans.append(ProtectedSpan(
                message_index=idx, message_id=message_id(idx, content),
                label=label or "system",
                start=0, end=len(content.encode("utf-8"))))
            continue
        ref = seen.get(content)
        if ref is None:
            # 首次出现：登记 block_id，保留原文（dictionary 待被引用时填充）
            seen[content] = block_id_for(content)
            out.append(dict(m))
        elif (
            len(content.encode("utf-8")) > min_block_chars
            and estimate_tokens(content) > _gain_token_threshold()
        ):
            # 完全重复且替换有收益（字节口径 + token 估算口径双满足，保守门槛）：
            # 替换为 REF（role 不变、顺序不变、额外字段保留）。token 口径防 E12
            # 教训：中文短块字节多但 token 少，替换 REF 反而膨胀 → 不替换（identity）。
            out.append({**m, "content": ref_content_for(ref)})
            replaced_spans.append(RefSpan(
                message_index=idx, message_id=message_id(idx, content),
                ref=ref, content_hash=stable_hash(content),
                start=0, end=_REF_LEN))
            dictionary.add(ref, content)
        else:
            # 重复但无收益（字节或 token 口径任一不满足）：保留原文（identity 语义）
            out.append(dict(m))
            short_duplicates += 1
            notes.append(
                f"消息 {idx} 重复但无净收益（字节 ≤ {min_block_chars} 或估算 token ≤ "
                f"{_gain_token_threshold()}），保留原文"
            )

    input_bytes = sum(len(m["content"].encode("utf-8")) for m in messages)
    output_bytes = sum(len(m["content"].encode("utf-8")) for m in out)
    input_chars = sum(len(m["content"]) for m in messages)
    output_chars = sum(len(m["content"]) for m in out)
    input_tokens = sum(estimate_tokens(m["content"]) for m in messages)
    output_tokens = sum(estimate_tokens(m["content"]) for m in out)
    compressed = output_bytes < input_bytes and len(replaced_spans) > 0

    if not compressed:
        # 无净收益：返回 identity（原始消息，逐字节相同），dictionary 清空
        out = [dict(m) for m in messages]
        replaced_spans = []
        dictionary = PreprocessorDictionary()
        if short_duplicates == 0:
            notes.append("无重复块可压缩，返回 identity")
        else:
            notes.append("压缩无净收益（output_bytes >= input_bytes），返回 identity，不膨胀")

    stats = PreprocessorStats(
        input_chars=input_chars,
        output_chars=sum(len(m["content"]) for m in out),
        input_bytes=input_bytes,
        output_bytes=sum(len(m["content"].encode("utf-8")) for m in out),
        input_messages=len(messages),
        output_messages=len(out),
        input_tokens_est=input_tokens,
        output_tokens_est=sum(estimate_tokens(m["content"]) for m in out),
        replaced_messages=len(replaced_spans),
        protected_messages=len(protected_spans),
        compression_ratio=(1.0 - output_bytes / input_bytes) if input_bytes else 0.0,
        compressed=compressed,
    )
    manifest = PreprocessorManifest(
        mode="structured_lossless",
        version=PREPROCESSOR_VERSION,
        input_hash=normalized_hash(messages),
        output_hash=normalized_hash(out),
        compressed=compressed,
        reversible=True,
        lossless=False,   # round-trip 可逆但不声称 greedy 输出一致（需 4B paired）
        replaced_spans=replaced_spans,
        protected_spans=protected_spans,
        notes=notes,
        stats=stats,
    )
    return PreprocessorResult(messages=out, manifest=manifest, dictionary=dictionary)


def restore(
    compressed: Sequence[Dict[str, Any]],
    manifest: PreprocessorManifest,
    dictionary: PreprocessorDictionary,
) -> List[Dict[str, Any]]:
    """round-trip 还原（逐消息/逐字节）。

    校验顺序（全部 fail-fast，任一失败立即抛错）：
    1. dictionary 线程绑定（跨 thread → ``PreprocessorCrossThreadError``）；
    2. manifest 合法（模式/版本 → ``PreprocessorManifestError``）；
    3. 压缩输入 hash == manifest.output_hash（错误输入 / 被篡改 →
       ``PreprocessorHashMismatchError``）；
    4. 逐条还原：REF 格式消息必须能在 dictionary 中解析（未知 ref →
       ``PreprocessorUnknownRefError``），且 block_id 与原文 hash 自验证一致
       （伪 dictionary / 跨 session 错用 → ``PreprocessorHashMismatchError``）；
    5. 还原结果 hash == manifest.input_hash（round-trip 完整性 → mismatch fail-fast）。
    """
    # 1) 线程绑定
    dictionary._check_thread()
    # 2) manifest 合法性
    if not isinstance(manifest, PreprocessorManifest):
        raise PreprocessorManifestError(
            f"manifest 必须是 PreprocessorManifest，实际 {type(manifest).__name__}（fail-fast）"
        )
    if manifest.mode != "structured_lossless":
        raise PreprocessorManifestError(
            f"manifest.mode={manifest.mode!r} 不是 structured_lossless（fail-fast）"
        )
    if not manifest.version or not manifest.version.startswith(PREPROCESSOR_VERSION.split(".")[0] + "."):
        raise PreprocessorManifestError(
            f"manifest.version={manifest.version!r} 与当前版本 {PREPROCESSOR_VERSION} 主版本不兼容（fail-fast）"
        )
    # 2.5) 压缩输入结构校验（缺键/非 dict 必须是 PreprocessorError，而非 hash 阶段的 KeyError）
    for idx, m in enumerate(compressed):
        if not isinstance(m, dict):
            raise PreprocessorError(
                f"压缩消息 {idx} 必须是 dict，实际 {type(m).__name__}（fail-fast）"
            )
        if m.get("role") not in ("system", "user", "assistant", "tool"):
            raise PreprocessorError(
                f"压缩消息 {idx} 的 role={m.get('role')!r} 非法或缺失（fail-fast）"
            )
        if not isinstance(m.get("content"), str):
            raise PreprocessorError(
                f"压缩消息 {idx} 的 content 缺失或不是 str（fail-fast）"
            )
    # 3) 压缩输入 hash
    if normalized_hash(compressed) != manifest.output_hash:
        raise PreprocessorHashMismatchError(
            "压缩输入 hash 与 manifest.output_hash 不符（输入被篡改或配对错误，fail-fast）"
        )
    # 4) 逐条还原（REF 消息的非 role/content 字段一并保留 → 逐消息/逐字段还原）
    out: List[Dict[str, Any]] = []
    for idx, m in enumerate(compressed):
        role = m.get("role")
        content = m.get("content")
        if _REF_FULL_RE.fullmatch(content):
            ref = content[len(_REF_PREFIX):-len(_REF_SUFFIX)]
            original = dictionary.resolve(ref)      # 未知 ref → PreprocessorUnknownRefError
            if block_id_for(original) != ref:
                raise PreprocessorHashMismatchError(
                    f"block_id {ref!r} 与 dictionary 内容 hash 不符（伪 dictionary / "
                    f"跨 session 错用，fail-fast）"
                )
            out.append({**m, "content": original})
        else:
            out.append(dict(m))
    # 5) round-trip 完整性
    if normalized_hash(out) != manifest.input_hash:
        raise PreprocessorHashMismatchError(
            "还原结果 hash 与 manifest.input_hash 不符（round-trip 失败，fail-fast）"
        )
    return out


# ---- 配置归一化 -------------------------------------------------------------


def normalize_preprocessor_mode(value: Any, default: str = DEFAULT_PREPROCESSOR_MODE) -> str:
    """归一化 ``preprocessor`` 配置值；非法值 fail-fast（ValueError）。

    配置来源：``config.extra["preprocessor"]``（配置文件未知键，与 E15.2
    ``tool_payload_mode`` 同一惯例）。off = 旧行为（可回滚）。
    """
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(
            f"preprocessor={value!r} 必须是字符串，可选: {', '.join(PREPROCESSOR_MODES)}（fail-fast）")
    value = _PREPROCESSOR_ALIASES.get(value, value)
    if value not in PREPROCESSOR_MODES:
        raise ValueError(
            f"preprocessor={value!r} 非法，可选: {', '.join(PREPROCESSOR_MODES)}（fail-fast）")
    return value


# ---- Driver 接入包装 ----------------------------------------------------------


class Preprocessor:
    """结构化无损预处理器的进程内实例（Driver 单点接入，无跨请求状态）。

    每个 ``process()`` 独立压缩（构建独立 dictionary，发送后即弃）；
    ``restore`` 仅用于审计/测试的 round-trip 验证（dictionary 在同一进程内持有）。
    """

    def __init__(
        self,
        fact_texts: Sequence[str] = (),
        min_block_chars: int = _REF_LEN,
    ) -> None:
        self.fact_texts = tuple(fact_texts)
        self.min_block_chars = min_block_chars

    def process(self, messages: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """请求发送前调用：返回 ``(work_messages, audit_dict)``。

        - ``work_messages``：压缩后的消息列表（新列表，**不修改调用者 messages**）；
        - ``audit_dict`` = ``manifest.to_dict()``（无原文，可进结果 JSON）。
        """
        result = structured_lossless_compress(
            messages, fact_texts=self.fact_texts, min_block_chars=self.min_block_chars)
        return result.messages, result.manifest.to_dict()
