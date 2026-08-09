"""M0 fan-out 决策/分支 prompt 构造 —— 纯函数模块（不依赖 GPU / server）。

单一架构（docs/M0_BRANCH_MEMORY_BASELINE_DESIGN.md §3.2/§4.2）：
- 决策点协议：公共上下文末尾要求模型输出唯一合法分支指令
  ``ACTION: branch(bX)``（枚举约束，temp=0/seed=42 下确定性复现）；
- 分支请求：公共上下文 + 决策输出 + 分支专属任务 + 分支专属 canary；
- 工具注入：**不使用 OpenAI ``tools`` schema**（避免 apply-template 与 chat
  两条路径渲染差异与精确计数遗漏），以固定无敏感文本块 ``<tool_response>``
  注入（复用 tool_call.py 协议风格）；
- 所有长度/身份/矩阵组合由 ``MATRIX_PLAN`` 单一来源派生。
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Tuple

# ---- 矩阵单一来源（§4.2/§6，validator/runner/测试均引用，不得另造第二份） ----

# fanout -> 合法长度桶（v47 权威映射）
MATRIX_PLAN: Dict[int, List[str]] = {
    2: ["short", "medium", "long"],
    4: ["short", "medium"],
    8: ["short"],
}
FANOUTS: Tuple[int, ...] = (2, 4, 8)
BUCKETS: Tuple[str, ...] = ("short", "medium", "long")
# 长度桶目标 token（prefix P, branch B）（§4.2 实测校准表）
BUCKET_TARGETS: Dict[str, Tuple[int, int]] = {
    "short": (150, 150),
    "medium": (280, 480),
    "long": (400, 600),
}
# unified KV 严格预算：budget(P,B,N) = P + N*(P+B) ≤ 4096*0.85
BUDGET_THRESHOLD = 3481

CACHE_PROFILES: Tuple[Tuple[str, str], ...] = (("q8_0", "q8_0"), ("f16", "f16"))
CONTROLS: Tuple[str, ...] = ("off", "on")


def budget(prefix_tokens: int, branch_tokens: int, fanout: int) -> int:
    """unified KV 预算函数（§4.2）：P + N×(P+B)。"""
    if prefix_tokens < 0 or branch_tokens < 0 or fanout < 1:
        raise ValueError("budget 参数必须非负且 fanout>=1")
    return prefix_tokens + fanout * (prefix_tokens + branch_tokens)


def is_budget_legal(prefix_tokens: int, branch_tokens: int, fanout: int) -> bool:
    """预算合法性（严格 ≤ 阈值，fail-fast 门禁）。"""
    return budget(prefix_tokens, branch_tokens, fanout) <= BUDGET_THRESHOLD


def legal_matrix_plan() -> Dict[int, List[str]]:
    """由 MATRIX_PLAN × BUCKET_TARGETS × budget 重建的**理论合法组合**。

    返回 fanout -> [通过预算的 buckets]；被预算函数排除的组合（如 4×long、
    8×medium/8×long）不出现——它们不进入 planned_units、也不写
    preflight_rejections（v36 静态预筛语义）。
    """
    plan: Dict[int, List[str]] = {}
    for fanout, buckets in MATRIX_PLAN.items():
        ok: List[str] = []
        for bucket in buckets:
            p, b = BUCKET_TARGETS[bucket]
            if is_budget_legal(p, b, fanout):
                ok.append(bucket)
        if ok:
            plan[fanout] = ok
    return plan


def expected_unit_ids() -> List[str]:
    """EXPECTED_UNIT_IDS 单一来源（v47）：MATRIX_PLAN 合法组合 × 2 profile × 2 control。

    ID 格式（v42）：``{control}:{ctk}-{ctv}:f{fanout}:{bucket}``。
    顺序：control(off,on) → profile(q8_0-q8_0, f16-f16) → fanout 升序 → bucket 按
    MATRIX_PLAN 顺序。返回 24 个（validator 断言集合精确相等而非仅长度）。
    """
    ids: List[str] = []
    for control in CONTROLS:
        for (ctk, ctv) in CACHE_PROFILES:
            for fanout, buckets in legal_matrix_plan().items():
                for bucket in buckets:
                    ids.append(f"{control}:{ctk}-{ctv}:f{fanout}:{bucket}")
    return ids


# ---- 决策点协议（§3.1 确定性） ----

_DECISION_SYSTEM = (
    "你是多分支任务调度器。你只负责在给定公共上下文后选择一个执行分支。"
    "分支编号从 b1 开始。"
)

_DECISION_TASK_TMPL = (
    "【公共上下文】\n{context}\n\n"
    "【决策点】请根据公共上下文选择唯一执行分支。"
    "只能输出以下 ACTION 之一，不得输出任何其他内容（不得解释、不得加标点、"
    "不得输出 JSON）：\n{action_enum}"
)

# 分支枚举（fanout 决定 b1..bN；唯一合法输出）
def _action_enum(fanout: int) -> str:
    return " | ".join(f"ACTION: branch(b{i})" for i in range(1, fanout + 1))


def decision_messages(context: str, fanout: int) -> List[Dict[str, str]]:
    """构造决策点消息列表（system + user），确定性。

    ``fanout`` 必须是 MATRIX_PLAN 键（2/4/8），否则 ValueError（fail-fast）。
    """
    if fanout not in MATRIX_PLAN:
        raise ValueError(f"fanout 必须是 {sorted(MATRIX_PLAN)} 之一，got {fanout}")
    return [
        {"role": "system", "content": _DECISION_SYSTEM},
        {"role": "user", "content": _DECISION_TASK_TMPL.format(
            context=context, action_enum=_action_enum(fanout))},
    ]


# ---- 分支语义（§3.1：分支任务不依赖模型继续决策） ----

_BRANCH_TASK_TMPL = (
    "【分支任务】你被选中执行分支 {branch}。请完成以下固定任务：{task}\n"
    "任务完成后直接输出 CANARY {canary} 并结束。"
)

_TOOL_RESPONSE_TMPL = (
    "<tool_response>"
    '{{"tool": "m0_fanout_probe", "round": {round}, "branch": "{branch}",'
    ' "payload_ref": "p{round}", "status": "ok"}}'
    "</tool_response>"
)


def branch_messages(
    base_messages: List[Dict[str, str]],
    action_text: str,
    branch: str,
    task: str,
    canary: str,
) -> List[Dict[str, str]]:
    """分支消息 = 公共上下文 + 决策输出 + 分支任务 + canary。

    ``base_messages`` 是决策点消息（或渲染后的公共上下文），``action_text``
    是模型输出的 ACTION 行（作为 assistant 消息注入）。
    """
    return base_messages + [
        {"role": "assistant", "content": action_text},
        {"role": "user", "content": _BRANCH_TASK_TMPL.format(
            branch=branch, task=task, canary=canary)},
    ]


def tool_round_message(branch: str, tool_round: int) -> Dict[str, str]:
    """工具观测注入（v5 定稿：固定文本块，不走 OpenAI tools schema）。"""
    return {"role": "user", "content": _TOOL_RESPONSE_TMPL.format(
        round=tool_round, branch=branch)}


def canary_for(fanout: int, branch: str) -> str:
    """分支专属 canary：CANARY-B{fanout}-<8hex>，确定性派生。"""
    digest = hashlib.sha256(f"m0-fanout-{fanout}-{branch}".encode()).hexdigest()[:8]
    return f"CANARY-B{fanout}-{digest}"


# ---- ACTION 解析与 INVALID_DECISION 谓词（§3.1 v8 三处同一） ----

_ACTION_RE = re.compile(r"ACTION:\s*branch\(b([0-9]+)\)")


def parse_action(text: str) -> int:
    """解析输出中的 ``ACTION: branch(bX)``；找不到返回 0（非 1..fanout 均非法）。"""
    if not text:
        return 0
    m = _ACTION_RE.search(text)
    if not m:
        return 0
    return int(m.group(1))


def decision_invalid(text: str, finish_reason: str, fanout: int) -> bool:
    """INVALID_DECISION 谓词（v8 定稿，三处同一）：length 截断或输出不含合法 ACTION。"""
    if finish_reason == "length":
        return True
    action = parse_action(text)
    return not (1 <= action <= fanout)


def fallback_branch(fanout: int, rep_index: int) -> str:
    """固定路由 fallback（§3.1）：脚本按固定规则选分支，不中断、不重试。"""
    return f"b{(rep_index % fanout) + 1}"


# ---- 确定性上下文构造（长度桶目标由 BUCKET_TARGETS 派生） ----

_FILLER_TMPL = (
    "任务背景资料段落 {i}：{prefix} 目标 token 预算 {target}，用于构造确定性的"
    "公共上下文填充文本。该段内容与决策无关，仅用于控制上下文长度。"
)


def build_context(bucket: str, index: int = 0) -> str:
    """按长度桶构造确定性公共上下文（不精确对齐 token，精确计数靠 tokenize）。

    返回一个可重复的上下文文本；真实 token 数以 /tokenize 实测为准
    （P/B 必须来自渲染后 prompt 的真实计数，chars 估算仅对照）。
    """
    if bucket not in BUCKET_TARGETS:
        raise ValueError(f"bucket 必须是 {BUCKETS} 之一，got {bucket}")
    p_target, b_target = BUCKET_TARGETS[bucket]
    seg = _FILLER_TMPL.format(
        i=index + 1, prefix=bucket, target=p_target + b_target)
    # 重复若干次形成足够长的上下文（精确 token 由 tokenize 实测，此处仅构造）
    repeat = max(2, (p_target // 40) + 1)
    return "\n".join(seg for _ in range(repeat))


def build_branch_task(bucket: str, branch: str) -> str:
    """分支专属固定任务（确定性；与决策无关的后续推理）。"""
    return (
        f"对第 {branch} 号数据源执行固定归档流程：读取字段、计算摘要、"
        f"写入结果表（bucket={bucket}）。"
    )
