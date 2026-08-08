"""tool_payload —— E15.2 B 线核心：ToolPayloadStore（工具返回 payload 的进程内内容寻址存储）。

设计定位（对应 `docs/E15_0_TECHNICAL_PLAN_AND_ACCEPTANCE.md`）：核心 + workload 最小端到端
集成已交付（E15.2.1，接入 `tool_call` / `long_life`，见 `docs/E15_2_TOOL_PAYLOAD_STORE.md`
§6；**未跑真实模型 paired**，token/KV 收益待 4B greedy paired 对比）：
- 目标：工具调用返回的大段 JSON（如订单详情）以**内容寻址**方式入库，resolve 时按
  ``ref + expected_hash`` 显式取回原文，避免把整段 payload 反复回填进模型上下文。
- thread-scoped：store 绑定创建线程（``threading.get_ident``），任何跨线程访问 fail-fast
  （``ToolPayloadCrossThreadError``）。
- 不可变 Entry/Ref：``put`` 返回 ``Ref``（ref = sha256 hex，内容寻址，重复内容 dedupe）；
  ``Entry`` 为 frozen dataclass，只读审计视图（含确定性 projection，projection **递归只读**：
  dict → 只读映射（MappingProxyType）、list → tuple，外部任何层级修改 fail-fast）。
- canonical JSON：键排序 + 紧凑分隔符（``sort_keys`` + ``separators=(",", ":")``），
  语义相同而书写不同的 JSON 得到同一 ref；纯文本 payload 按原样 utf-8 字节处理。
- 确定性 structured projection：订单 payload 提取 ``order_ids`` / ``item_count`` /
  ``total_amount``（items 的 qty*price 求和，非有限值 fail-fast）/ ``logistics_status``
  （去重保序）；未知 JSON 使用**有界确定性**摘要（深度/条目/字符串长度上限，见模块
  常量），文本摘要只含 ``chars`` + 不含原文的确定性指纹（head_sha256），**绝不写入
  payload 原文片段**。
- 安全：denylist 字段名 + 敏感值模式，命中即 fail-closed（``ToolPayloadSecretError``）；
  检测覆盖**顶层 list 及嵌套 dict/list** 的全部键名，复合敏感字段（db_password /
  auth_token / user_secret 等）按安全边界（敏感词后缀）拒绝，但不误伤 author /
  monkey 等正常字段；敏感 payload **不得进入** store / projection / ref / snapshot。
- 淘汰：统一 LRU，取 ``min (last_used_tick, inserted_tick)``（inserted_tick 仅 tie-break）；
  ``max_entries`` / ``max_bytes`` 生效；``resolve`` / ``get`` 成功刷新 last_used_tick。
- snapshot/restore 仅进程内可用；快照**可审计**（只含元数据 + projection，不含 payload 原文，
  因此天然不含敏感值）；``purge`` 后所有对应 ref fail-fast；快照不携带原文，被 purge 的
  payload 在 restore 后仍 fail-fast（不可复活）。
- ``resolve`` 是纯数据读取（**不注入任何 prompt**）；上层拦截协议：
  ``ACTION: resolve_tool_payload(payload_ref=<ref>, expected_hash=<hash>)``（见文档 §5）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

TOOL_PAYLOAD_VERSION = "0.1.0"

# ---- 有界 projection 常量（未知 JSON / 文本摘要的上限，保证确定性 + 有界）--------
MAX_PROJECTION_DEPTH = 3          # 嵌套深度上限（root 为 0）
MAX_PROJECTION_ITEMS = 8          # 单容器遍历条目上限
MAX_PROJECTION_STRING = 32        # 摘要中单个字符串（如键名）截断长度
MAX_SUMMARY_KEYS = 16             # summary 中收集的键名数量上限

# ---- 异常（fail-fast 默认）------------------------------------------------------
class ToolPayloadError(Exception):
    """tool_payload 全部异常的基类。"""


class ToolPayloadSecretError(ToolPayloadError):
    """检测到敏感字段名 / 敏感值模式：fail-closed，payload 不入库。"""


class ToolPayloadMissingError(ToolPayloadError):
    """ref 不存在、已被 purge，或原文 blob 不可用。"""


class ToolPayloadHashMismatchError(ToolPayloadError):
    """resolve 的 expected_hash 与存储内容 hash 不一致（fail-fast，不刷新 LRU）。"""


class ToolPayloadCrossThreadError(ToolPayloadError):
    """跨线程访问 thread-scoped store / 跨线程 restore（fail-fast）。"""


class ToolPayloadSnapshotError(ToolPayloadError):
    """snapshot 跨进程 restore 或与当前 store 不兼容。"""


class ToolPayloadCapacityError(ToolPayloadError):
    """超出容量约束（单条超 max_bytes、容量参数非法）。"""


# ---- 安全 denylist（字段名 + 敏感值模式）-----------------------------------------
# 字段名 denylist：命中即拒绝（与值无关，fail-closed）。
DENYLIST_FIELDS: Tuple[str, ...] = (
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_token", "authorization", "authorization_code", "auth", "credential",
    "client_secret",
    "private_key", "secret_key", "credit_card", "card_number", "cardno",
    "cvv", "cvc", "ssn", "cookie", "session_id", "csrf",
    "密码", "密钥", "口令", "验证码",
)

# 敏感值模式：匹配 payload 文本中的常见密钥/凭据形态（命中即拒绝）。
_SECRET_VALUE_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),                     # OpenAI API key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                           # AWS access key
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),                        # GitHub token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),          # PEM 私钥
    re.compile(r"\bBearer [A-Za-z0-9._~+/=,-]{8,}\b"),             # Authorization header
)

# 敏感键值模式：纯文本中 ``key: value`` / ``key=value`` 形态（字段名 denylist 覆盖 JSON 键）。
_SECRET_KV_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|"
        r"client[_-]?secret)\b\s*[:=]\s*\S+"
    ),
)


def _normalize_key(key: Any) -> str:
    """字段名归一化：小写并去除所有非字母数字字符（``api_key``/``API-Key`` 视为同一）。"""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


_DENYLIST_FIELDS_NORM = frozenset(_normalize_key(k) for k in DENYLIST_FIELDS)

# 复合敏感字段后缀：归一化后以这些词结尾即拒绝（如 db_password → dbpassword、
# auth_token → authtoken、user_secret → usersecret）。只取不可能误伤正常字段的
# 敏感词作后缀——刻意不含 "key" / "auth" / "id" 等短词（monkey / author / grid 类
# 正常字段不得被误伤）；精确命中仍由 _DENYLIST_FIELDS_NORM 负责。
_DENYLIST_FIELD_SUFFIXES: Tuple[str, ...] = (
    "password", "passwd", "pwd", "secret", "token", "apikey",
    "credential", "cardnumber", "cardno", "cvv", "cvc", "ssn",
    "csrf", "sessionid",
)


def _is_secret_field(key: Any) -> bool:
    """字段名是否敏感：精确命中 denylist，或为复合敏感字段（安全边界 = 敏感词后缀）。"""
    norm = _normalize_key(key)
    if norm in _DENYLIST_FIELDS_NORM:
        return True
    return any(norm.endswith(suffix) for suffix in _DENYLIST_FIELD_SUFFIXES)


def _iter_keys(node: Any) -> Any:
    """深度优先遍历 JSON 结构中的所有 dict 键名（用于 denylist 检查）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _iter_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_keys(v)


# ---- canonical JSON / sha256 ------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """canonical JSON：键排序 + 紧凑分隔符 + 非 ASCII 原样输出。

    ``allow_nan=False``：NaN/Infinity 不是合法 JSON 值；混合类型键（如 int 与 str
    混排）无法稳定排序。两类异常统一包装为 ``ToolPayloadError``，**不得把非标准
    JSON 写入 store / blob / projection**。
    """
    try:
        return json.dumps(
            obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ToolPayloadError(
            f"payload 含非标准 JSON 值（NaN/Infinity 或混合类型键，fail-fast）：{exc}"
        ) from exc


def sha256_hex(data: bytes) -> str:
    """sha256 hex digest（内容寻址 ref 的基础）。"""
    return hashlib.sha256(data).hexdigest()


def stable_sha256(text: str) -> str:
    """字符串便捷版 sha256。"""
    return sha256_hex(text.encode("utf-8"))


# ---- 确定性 structured projection ------------------------------------------------
def _bounded_walk(node: Any, depth: int, state: Dict[str, Any]) -> None:
    """有界确定性遍历：深度 / 条目 / 键名长度全部受限，越界置 truncated 并停止下钻。"""
    if depth > MAX_PROJECTION_DEPTH:
        state["truncated"] = True
        return
    state["max_depth"] = max(state["max_depth"], depth)
    if isinstance(node, dict):
        state["containers"] += 1
        for i, (k, v) in enumerate(sorted(node.items())):
            if i >= MAX_PROJECTION_ITEMS:
                state["truncated"] = True
                return
            state["keys"].add(str(k)[:MAX_PROJECTION_STRING])
            _bounded_walk(v, depth + 1, state)
    elif isinstance(node, list):
        state["containers"] += 1
        for i, v in enumerate(node):
            if i >= MAX_PROJECTION_ITEMS:
                state["truncated"] = True
                return
            _bounded_walk(v, depth + 1, state)
    else:
        state["scalars"] += 1


def _bounded_json_summary(node: Any) -> str:
    """未知/通用 JSON 的有界确定性摘要（紧凑 JSON 字符串，可直接审计）。"""
    state: Dict[str, Any] = {
        "max_depth": 0, "containers": 0, "scalars": 0,
        "keys": set(), "truncated": False,
    }
    _bounded_walk(node, 0, state)
    return json.dumps(
        {
            "depth": state["max_depth"],
            "containers": state["containers"],
            "scalars": state["scalars"],
            "keys": sorted(state["keys"])[:MAX_SUMMARY_KEYS],
            "truncated": state["truncated"],
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _bounded_text_summary(text: str) -> str:
    """文本 payload 的有界确定性摘要：总字符数 + 不含原文的确定性指纹。

    reviewer 修复：head 截断会把 payload 原文片段写进 projection/snapshot，
    改为 ``chars`` + ``head_sha256``（文本 sha256 前缀，确定性且不含原文）。
    """
    return json.dumps(
        {"chars": len(text), "head_sha256": sha256_hex(text.encode("utf-8"))[:16]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _project_order_ids(parsed: Any) -> Tuple[Optional[List[str]], Optional[int]]:
    if not (isinstance(parsed, dict) and isinstance(parsed.get("order_ids"), list)):
        return None, None
    ids = [str(v) for v in parsed["order_ids"] if isinstance(v, str)]
    return ids[:MAX_PROJECTION_ITEMS], len(ids)


def _project_items(parsed: Any) -> Tuple[Optional[int], Optional[float]]:
    if not (isinstance(parsed, dict) and isinstance(parsed.get("items"), list)):
        return None, None
    total = 0.0
    for item in parsed["items"]:
        if not isinstance(item, dict):
            continue
        try:
            qty = float(item.get("qty", 0))
            price = float(item.get("price", 0))
        except OverflowError as exc:
            # 超大整数超出 float 范围：不是"非数值"，是数值但越界——明确 fail-fast，
            # 不得静默跳过导致 total_amount 算错。
            raise ToolPayloadError(
                f"items[].qty/price 超出有限数值范围（fail-fast）：{exc}"
            ) from exc
        except (TypeError, ValueError):
            continue
        product = qty * price
        if not (math.isfinite(qty) and math.isfinite(price) and math.isfinite(product)):
            # NaN/Infinity 不是合法 JSON 数值（reviewer：非有限值不得进入 projection）
            raise ToolPayloadError(
                f"items[].qty/price 必须为有限数值（qty={qty!r} price={price!r}，fail-fast）"
            )
        total += product
    if not math.isfinite(total):
        raise ToolPayloadError("items 金额求和溢出为无穷（fail-fast）")
    return len(parsed["items"]), round(total, 2)


def _project_logistics(parsed: Any) -> Optional[List[str]]:
    if not (isinstance(parsed, dict) and isinstance(parsed.get("logistics"), list)):
        return None
    seen: set = set()
    out: List[str] = []
    for item in parsed["logistics"]:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str) and status not in seen:
            seen.add(status)
            out.append(status)
            if len(out) >= MAX_PROJECTION_ITEMS:
                break
    return out


def project_payload(parsed: Any, content_type: str) -> Dict[str, Any]:
    """确定性 structured projection（纯函数）。

    - ``content_type="json"``：识别订单 payload（顶层含 ``order_ids`` / ``items`` /
      ``logistics`` 任一）→ ``type="order"`` 并提取四项字段；否则 ``type="generic_json"``，
      summary 为有界确定性摘要。projection 不提取任意值原文（不泄漏 payload 内容）。
    - ``content_type="text"``：``type="text"``，summary 为字符数 + 不含原文的指纹。
    """
    base: Dict[str, Any] = {
        "type": "text", "order_ids": None, "order_ids_total": None,
        "item_count": None, "total_amount": None, "logistics_status": None,
    }
    if content_type == "text":
        base["summary"] = _bounded_text_summary(str(parsed))
        return base
    order_ids, order_ids_total = _project_order_ids(parsed)
    item_count, total_amount = _project_items(parsed)
    logistics_status = _project_logistics(parsed)
    recognized = any(
        v is not None for v in (order_ids, item_count, logistics_status, total_amount)
    )
    base.update({
        "type": "order" if recognized else "generic_json",
        "order_ids": order_ids,
        "order_ids_total": order_ids_total,
        "item_count": item_count,
        "total_amount": total_amount,
        "logistics_status": logistics_status,
        "summary": _bounded_json_summary(parsed),
    })
    return base


# ---- 不可变值对象 ----------------------------------------------------------------
@dataclass(frozen=True)
class Ref:
    """内容寻址引用（不可变）。``ref`` 与 ``expected_hash`` 均为 sha256 hex（内容哈希）。"""

    ref: str
    expected_hash: str


@dataclass(frozen=True)
class Entry:
    """payload 审计视图（不可变 frozen）：元数据 + 确定性 projection，不含原文。

    ``projection`` **递归不可变**：dict → MappingProxyType、list → tuple，
    外部对任何层级的修改（``append`` / 下标赋值 / 新增键）一律 fail-fast。
    """

    ref: str
    content_type: str           # "json" | "text"
    size_bytes: int             # canonical 原文 utf-8 字节数
    sha256: str
    projection: Mapping[str, Any]   # 递归只读映射（dict→MPT、list→tuple）
    inserted_tick: int
    last_used_tick: int


def _deep_freeze(value: Any) -> Any:
    """递归冻结：dict → MappingProxyType（只读映射）、list → tuple（不可变序列）。"""
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_unfreeze(value: Any) -> Any:
    """递归解冻：MappingProxyType → dict、tuple → list（供 entry_to_dict 序列化）。"""
    if isinstance(value, MappingProxyType):
        return {k: _deep_unfreeze(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_deep_unfreeze(v) for v in value]
    return value


def entry_to_dict(entry: Entry) -> Dict[str, Any]:
    """Entry → 可序列化 dict（供 snapshot 审计 / 测试断言；深解冻 projection）。"""
    return {
        "ref": entry.ref,
        "content_type": entry.content_type,
        "size_bytes": entry.size_bytes,
        "sha256": entry.sha256,
        "projection": _deep_unfreeze(entry.projection),
        "inserted_tick": entry.inserted_tick,
        "last_used_tick": entry.last_used_tick,
    }


def eviction_key(entry: Entry) -> Tuple[int, int]:
    """LRU 淘汰键：``min last_used_tick`` 优先淘汰；``inserted_tick`` 仅 tie-break。"""
    return (entry.last_used_tick, entry.inserted_tick)


@dataclass(frozen=True)
class ToolPayloadSnapshot:
    """进程内可审计快照（不可变）：只含元数据 + projection，**不含 payload 原文**。

    因此快照天然不含敏感值（敏感 payload 本就被 put 拒之门外），可安全打印/序列化。
    """

    pid: int
    owner_ident: int
    created_tick: int
    entries: Tuple[Entry, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "owner_ident": self.owner_ident,
            "created_tick": self.created_tick,
            "entries": [entry_to_dict(e) for e in self.entries],
        }


# ---- ToolPayloadStore --------------------------------------------------------------
class ToolPayloadStore:
    """thread-scoped 内容寻址 payload 存储（详见模块 docstring）。"""

    def __init__(
        self, max_entries: Optional[int] = None, max_bytes: Optional[int] = None
    ) -> None:
        if max_entries is not None and max_entries < 1:
            raise ToolPayloadCapacityError(
                f"max_entries 必须 >= 1，当前 {max_entries}（fail-fast）"
            )
        if max_bytes is not None and max_bytes < 1:
            raise ToolPayloadCapacityError(
                f"max_bytes 必须 >= 1，当前 {max_bytes}（fail-fast）"
            )
        self._owner_ident = threading.get_ident()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: Dict[str, Entry] = {}
        self._blobs: Dict[str, bytes] = {}
        self._tick = 0

    # ---- thread 作用域 -------------------------------------------------------
    def _check_thread(self) -> None:
        if threading.get_ident() != self._owner_ident:
            raise ToolPayloadCrossThreadError(
                f"store 绑定线程 {self._owner_ident}，当前线程 "
                f"{threading.get_ident()}（thread-scoped，fail-fast）"
            )

    def _next_tick(self) -> int:
        self._tick += 1
        return self._tick

    # ---- 规范化 / 敏感检测 -----------------------------------------------------
    def _normalize(self, payload: Union[Dict[str, Any], List[Any], str, bytes]):
        """归一化：返回 (canonical_text, content_type, parsed)。

        parsed 为 dict/list（JSON 结构，供字段检测与 projection）或 str（纯文本，
        即 payload 原文，供文本 summary 统计真实字符数）；不再使用 None 表示纯文本
        （此前 ``str(None)`` 会让文本 projection 的 chars 恒为 4，reviewer 修复）。
        """
        if isinstance(payload, (dict, list)):
            text = canonical_json(payload)
            return text, "json", payload
        if isinstance(payload, bytes):
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolPayloadError("bytes payload 必须是 utf-8 编码（fail-fast）") from exc
        elif isinstance(payload, str):
            text = payload
        else:
            raise ToolPayloadError(
                f"不支持的 payload 类型 {type(payload).__name__}"
                f"（支持 dict/list/str/bytes，fail-fast）"
            )
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text, "text", text
        if isinstance(parsed, (dict, list)):
            return canonical_json(parsed), "json", parsed
        return text, "text", text

    def _check_secret(self, text: str, parsed: Any) -> None:
        """敏感检测（fail-closed）：命中任一 denylist 字段名或敏感值模式即抛错。

        reviewer 修复：顶层为 list 时同样遍历全部嵌套 dict 键（此前仅顶层 dict 才
        检查字段名，顶层 list 内的嵌套敏感键会漏网）；复合敏感字段（db_password /
        auth_token / user_secret 等）按安全边界（敏感词后缀）拒绝。
        """
        if isinstance(parsed, (dict, list)):
            for key in _iter_keys(parsed):
                if _is_secret_field(key):
                    raise ToolPayloadSecretError(
                        f"payload 含敏感字段 {key!r}（fail-closed，不入库）"
                    )
        for pat in _SECRET_VALUE_PATTERNS:
            if pat.search(text):
                raise ToolPayloadSecretError(
                    f"payload 命中敏感值模式 {pat.pattern!r}（fail-closed，不入库）"
                )
        for pat in _SECRET_KV_PATTERNS:
            if pat.search(text):
                raise ToolPayloadSecretError(
                    f"payload 命中敏感键值模式 {pat.pattern!r}（fail-closed，不入库）"
                )

    # ---- 写入 ---------------------------------------------------------------
    def put(self, payload: Union[Dict[str, Any], List[Any], str, bytes]) -> Ref:
        """入库并返回内容寻址 ``Ref``（不可变）。重复内容 dedupe（刷新 last_used_tick）。

        敏感 payload fail-closed（``ToolPayloadSecretError``）；单条超过 max_bytes
        抛 ``ToolPayloadCapacityError``；超容量后按 LRU 淘汰至满足约束。
        """
        self._check_thread()
        text, content_type, parsed = self._normalize(payload)
        self._check_secret(text, parsed)
        blob = text.encode("utf-8")
        if self._max_bytes is not None and len(blob) > self._max_bytes:
            raise ToolPayloadCapacityError(
                f"payload 大小 {len(blob)}B 超过 max_bytes={self._max_bytes}（fail-fast）"
            )
        digest = sha256_hex(blob)
        if digest in self._entries:
            self._touch(digest)
        else:
            tick = self._next_tick()
            entry = Entry(
                ref=digest,
                content_type=content_type,
                size_bytes=len(blob),
                sha256=digest,
                projection=_deep_freeze(project_payload(parsed, content_type)),
                inserted_tick=tick,
                last_used_tick=tick,
            )
            self._entries[digest] = entry
            self._blobs[digest] = blob
            self._evict_if_needed()
        return Ref(ref=digest, expected_hash=digest)

    # ---- 读取 ---------------------------------------------------------------
    def _touch(self, ref: str) -> None:
        """读刷新：last_used_tick 更新为当前 tick（LRU 语义）。"""
        tick = self._next_tick()
        self._entries[ref] = replace(self._entries[ref], last_used_tick=tick)

    def resolve(self, ref: str, expected_hash: str) -> str:
        """按 ``ref + expected_hash`` 显式取回 payload 原文（纯数据读取，不注入任何 prompt）。

        - ref 不存在 / 原文已被 purge → ``ToolPayloadMissingError``（fail-fast）；
        - expected_hash 不匹配 → ``ToolPayloadHashMismatchError``（fail-fast，不刷新 LRU）；
        - 成功 → 刷新 last_used_tick 并返回 canonical 文本。
        """
        self._check_thread()
        entry = self._entries.get(ref)
        if entry is None:
            raise ToolPayloadMissingError(f"ref {ref} 不存在或已被 purge（fail-fast）")
        if expected_hash != entry.sha256:
            raise ToolPayloadHashMismatchError(
                f"expected_hash {expected_hash} != 存储 hash {entry.sha256}（fail-fast）"
            )
        if ref not in self._blobs:
            raise ToolPayloadMissingError(f"ref {ref} 的原文已被 purge（fail-fast）")
        self._touch(ref)
        return self._blobs[ref].decode("utf-8")

    def get(self, ref: str) -> Entry:
        """返回不可变 ``Entry``（审计视图，含 projection）。读操作刷新 last_used_tick。"""
        self._check_thread()
        if ref not in self._entries:
            raise ToolPayloadMissingError(f"ref {ref} 不存在或已被 purge（fail-fast）")
        if ref not in self._blobs:
            raise ToolPayloadMissingError(f"ref {ref} 的原文已被 purge（fail-fast）")
        self._touch(ref)
        return self._entries[ref]

    # ---- 淘汰（统一 LRU） ------------------------------------------------------
    def _evict_if_needed(self) -> None:
        while True:
            if self._max_entries is not None and len(self._entries) > self._max_entries:
                self._evict_one()
            elif self._max_bytes is not None and self.total_bytes > self._max_bytes:
                self._evict_one()
            else:
                break

    def _evict_one(self) -> None:
        victim = min(self._entries.values(), key=eviction_key)
        del self._entries[victim.ref]
        del self._blobs[victim.ref]

    # ---- 快照 / 恢复 / 清除 -----------------------------------------------------
    def snapshot(self) -> ToolPayloadSnapshot:
        """进程内可审计快照：只含元数据 + projection，不含 payload 原文（天然无敏感值）。"""
        self._check_thread()
        return ToolPayloadSnapshot(
            pid=os.getpid(),
            owner_ident=self._owner_ident,
            created_tick=self._tick,
            entries=tuple(self._entries.values()),
        )

    def restore(self, snap: ToolPayloadSnapshot) -> None:
        """恢复快照时刻的视图（entries / LRU tick / 后续 tick 计数）。

        - 仅进程内可用：``snap.pid != os.getpid()`` → ``ToolPayloadSnapshotError``；
        - 仅同 store 线程可用：owner 不匹配 → ``ToolPayloadCrossThreadError``；
        - 快照不携带原文：已被 purge 的 payload 在 restore 后仍 fail-fast（不可复活），
          restore 同时丢弃快照视图之外的 blob。
        """
        self._check_thread()
        if snap.pid != os.getpid():
            raise ToolPayloadSnapshotError(
                f"snapshot 属于进程 {snap.pid}，当前进程 {os.getpid()}（仅进程内可用）"
            )
        if snap.owner_ident != self._owner_ident:
            raise ToolPayloadCrossThreadError(
                f"snapshot 属于线程 {snap.owner_ident}，不能恢复到线程 {self._owner_ident}"
            )
        refs = {e.ref for e in snap.entries}
        self._entries = {e.ref: e for e in snap.entries}
        self._blobs = {r: b for r, b in self._blobs.items() if r in refs}
        self._tick = snap.created_tick

    def purge(self, ref: Optional[str] = None) -> None:
        """清除全部（默认）或单个 ref。purge 后对应 ref 一律 fail-fast
        （resolve/get → ``ToolPayloadMissingError``；snapshot 不再包含）。"""
        self._check_thread()
        if ref is None:
            self._entries.clear()
            self._blobs.clear()
        else:
            self._entries.pop(ref, None)
            self._blobs.pop(ref, None)

    # ---- 审计视图（公开只读接口同样 thread fail-fast，与文档 §3.4 一致）--------------
    def __len__(self) -> int:
        self._check_thread()
        return len(self._entries)

    @property
    def total_bytes(self) -> int:
        self._check_thread()
        return sum(e.size_bytes for e in self._entries.values())

    @property
    def tick(self) -> int:
        self._check_thread()
        return self._tick

    @property
    def owner_ident(self) -> int:
        self._check_thread()
        return self._owner_ident

    def stats(self) -> Dict[str, Any]:
        """审计统计（refs 列表（排序）/ 字节 / tick；数量见 ``__len__``）。"""
        self._check_thread()
        return {"entries": sorted(self._entries), "bytes": self.total_bytes, "tick": self._tick}
