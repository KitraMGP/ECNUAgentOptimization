"""test_e15_2_tool_payload_integration —— ToolPayloadStore × workload 最小端到端集成测试。

覆盖（E15.2 集成验收，无 GPU / 无真实 server / 无 llama-server 依赖）：
- off 模式（默认）保持旧完整 payload 注入，结果结构零改动（可回滚）；
- externalized projection 不含 address/customer 等敏感原文值；
- resolve 成功仅下一轮注入完整 payload，随后恢复 projection；
- hash mismatch / missing / 非法动作 / 未知工具 fail-fast（无静默 fallback）；
- thread / run 隔离（跨 run 引用 ref fail-fast；并发 run 各自 store 互不干扰）；
- long_life 工具轮使用同一语义（put+projection / resolve 拦截 / 审计计数）。

只运行本文件（+ test_workloads.py 回归），不依赖 GPU / 真实模型。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import pytest

import workload  # noqa: F401  （触发 workload 注册）
from framework.tool_payload import (
    ToolPayloadError,
    ToolPayloadHashMismatchError,
    ToolPayloadMissingError,
)
from framework.workload import get_workload
from tests.conftest import FakeDriver, default_row
from workload.tool_call import ToolPayloadRun

RESOLVE_REF_RE = re.compile(r"payload_ref=([0-9a-f]{64})")
RESOLVE_HASH_RE = re.compile(r"expected_hash=([0-9a-f]{64})")

ZERO64 = "0" * 64


def _last_user_content(call) -> str:
    """chat 调用中最后一条 user 消息的内容（tool_response 所在位置）。"""
    return next(m["content"] for m in reversed(call) if m["role"] == "user")


def _tool_call_spec(steps: int = 3, mode: str = "off"):
    return get_workload("tool_call").generate(
        {"steps": steps, "tool_payload_mode": mode})


# ---- off 模式（默认，可回滚） ------------------------------------------------------

def test_off_mode_keeps_full_payload_in_tool_response():
    """off：完整 payload（含 customer/address 原文）直接回填，旧行为一致。"""
    drv = FakeDriver(responses=[
        default_row(text="ACTION: search_orders(customer_id=C10086)"),
        default_row(text="ACTION: get_order_detail(order_id=SO00000)"),
        default_row(text="完成"),
    ])
    result = get_workload("tool_call").run(drv, _tool_call_spec(steps=3, mode="off"))
    assert "C10086" in _last_user_content(drv.calls[1])       # search_orders 原文
    assert "SO00000" in _last_user_content(drv.calls[1])
    assert "湖南省长沙市" in _last_user_content(drv.calls[2])  # get_order_detail 原文
    # 结果结构零改动：无 tool_payload 审计字段
    assert "tool_payload" not in result["meta"]
    assert all("tool_payload" not in r for r in result["rows"])
    # 不传 mode（默认 off）同样保持旧行为
    drv2 = FakeDriver(responses=[default_row(text="完成")])
    result2 = get_workload("tool_call").run(
        drv2, get_workload("tool_call").generate({"steps": 1}))
    assert "tool_payload" not in result2["meta"]


# ---- externalized：projection 不含敏感原文 -------------------------------------------

def test_externalized_projection_excludes_sensitive_raw_values():
    drv = FakeDriver(responses=[
        default_row(text="ACTION: search_orders(customer_id=C10086)"),
        default_row(text="ACTION: get_order_detail(order_id=SO00000)"),
        default_row(text="完成"),
    ])
    result = get_workload("tool_call").run(
        drv, _tool_call_spec(steps=3, mode="externalized"))
    resp1 = _last_user_content(drv.calls[1])   # search_orders projection
    resp2 = _last_user_content(drv.calls[2])   # get_order_detail projection
    for resp in (resp1, resp2):
        assert "payload_ref=" in resp
        assert "expected_hash=" in resp
        assert "projection:" in resp
    # customer 原文值不进入 projection；order_ids 为定位字段（H3），projection 保留
    assert "C10086" not in resp1
    assert "SO00000" in resp1
    # address / items 内容不进入 projection
    assert "湖南省长沙市" not in resp2
    assert "三一大道" not in resp2
    assert "SKU0" not in resp2
    # 审计计数（meta）
    meta = result["meta"]["tool_payload"]
    assert meta["mode"] == "externalized"
    assert meta["externalized_puts"] == 2
    assert meta["unique_refs"] == 2
    assert meta["resolve_requests"] == 0
    assert meta["resolve_successes"] == 0
    assert meta["resolve_failures"] == 0
    assert meta["projection_chars"] > 0
    assert meta["full_payload_chars"] == 0
    # 行级审计（不改变核心行字段）
    assert result["rows"][0]["tool_payload"]["kind"] == "externalized_projection"
    assert result["rows"][1]["tool_payload"]["kind"] == "externalized_projection"


# ---- resolve 协议：仅下一轮注入完整 payload，随后恢复 projection ------------------------

class _ResolveFlowDriver(FakeDriver):
    """外部化流程：search(customer) → 收到 projection → resolve → 收到完整 payload →
    两个普通工具轮（get_order_detail ×2）→ 完成。

    用于验证 one-shot restore：resolve 后第 2、3 个后续调用（calls[3]/calls[4]）
    的**整个 messages**（含历史）都不含完整原文，且占位 ref/hash 与最初一致。
    """

    def __init__(self, customer: str = "C10086"):
        super().__init__()
        self.customer = customer
        self.resolved_ref = None
        self.resolved_hash = None
        self._resolved = False
        self._tool_rounds = 0

    def chat(self, messages, _retry=0):
        self.calls.append([dict(m) for m in messages])
        last_user = _last_user_content(messages)
        if "tool_response" not in last_user:
            text = f"ACTION: search_orders(customer_id={self.customer})"
        elif "projection:" in last_user:
            if not self._resolved:
                # 第一次收到 projection（search_orders）→ 请求完整原文
                self.resolved_ref = RESOLVE_REF_RE.search(last_user).group(1)
                self.resolved_hash = RESOLVE_HASH_RE.search(last_user).group(1)
                self._resolved = True
                text = (f"ACTION: resolve_tool_payload(payload_ref={self.resolved_ref}, "
                        f"expected_hash={self.resolved_hash})")
            elif self._tool_rounds < 2:
                # 收到 get_order_detail 的 projection → 继续普通工具轮
                self._tool_rounds += 1
                text = f"ACTION: get_order_detail(order_id=SO0000{self._tool_rounds})"
            else:
                text = "完成"
        else:   # 收到完整 search_orders payload（canonical，含 customer 原文值）
            text = "ACTION: get_order_detail(order_id=SO00000)"
        return dict(default_row(text=text))


def test_resolve_full_payload_visible_once_then_restored_to_projection():
    drv = _ResolveFlowDriver()
    result = get_workload("tool_call").run(drv, _tool_call_spec(steps=6, mode="externalized"))
    assert len(drv.calls) == 6
    # 紧接的一次请求（resolve 后第 1 个后续调用）：完整 payload 可见（含 customer 原文值）
    all_text_call2 = "".join(str(m.get("content", "")) for m in drv.calls[2])
    assert '"customer":"customer_id=C10086"' in all_text_call2
    # resolve 后第 2、3 个后续调用：整个 messages（不是最后 user）均不含完整原文
    # （注意：系统提示/模型输出中会出现 "ACTION: search_orders(customer_id=C10086)"
    #  查询参数文本，因此用 canonical 完整 payload 的独有键值对特征断言）
    for call in (drv.calls[3], drv.calls[4]):
        all_text = "".join(str(m.get("content", "")) for m in call)
        assert '"customer":"customer_id=C10086"' not in all_text   # search 完整 payload 独有
        assert "湖南省长沙市" not in all_text            # get_order_detail 完整 payload 独有值
        assert "三一大道" not in all_text
        assert "SKU0" not in all_text
    # 恢复的 projection 占位与最初注入完全一致（同 ref/hash 同 projection 文本）
    proj1 = _last_user_content(drv.calls[1])            # 最初 search_orders projection
    ref1 = RESOLVE_REF_RE.search(proj1).group(1)
    hash1 = RESOLVE_HASH_RE.search(proj1).group(1)
    restored = [m["content"] for m in drv.calls[3]
                if m["role"] == "user" and "payload_ref=" in m["content"]]
    assert proj1 in restored                            # 原位替换回相同 ref/hash 的占位
    assert drv.resolved_ref == ref1 and drv.resolved_hash == hash1
    # 行级审计：resolve 轮 = resolve_full，之后恢复 externalized_projection
    kinds = [r.get("tool_payload", {}).get("kind") for r in result["rows"]]
    assert kinds == ["externalized_projection", "resolve_full",
                     "externalized_projection", "externalized_projection",
                     "externalized_projection", None]
    meta = result["meta"]["tool_payload"]
    assert meta["externalized_puts"] == 4               # search + get_order_detail ×3
    assert meta["unique_refs"] == 4
    assert meta["resolve_requests"] == 1
    assert meta["resolve_successes"] == 1
    assert meta["resolve_failures"] == 0
    assert meta["full_payload_chars"] > 0
    # evaluate 排除内部动作：tool_calls 只统计真实 MOCK_TOOLS 动作
    ev = get_workload("tool_call").evaluate(result, _tool_call_spec(steps=6, mode="externalized"))
    assert ev["tool_calls"] == 4
    assert ev["task_success"] is True


# ---- fail-fast：hash mismatch / missing / 非法动作 / 未知工具 --------------------------

def test_resolve_hash_mismatch_fail_fast():
    class _HashMismatchDriver(FakeDriver):
        def chat(self, messages, _retry=0):
            self.calls.append([dict(m) for m in messages])
            last_user = _last_user_content(messages)
            if "tool_response" not in last_user:
                text = "ACTION: search_orders(customer_id=C10086)"
            elif "projection:" in last_user:
                ref = RESOLVE_REF_RE.search(last_user).group(1)
                text = f"ACTION: resolve_tool_payload(payload_ref={ref}, expected_hash={ZERO64})"
            else:
                text = "完成"
            return dict(default_row(text=text))

    drv = _HashMismatchDriver()
    with pytest.raises(ToolPayloadHashMismatchError):
        get_workload("tool_call").run(drv, _tool_call_spec(steps=4, mode="externalized"))


def test_resolve_missing_ref_fail_fast():
    """从未写入（或已淘汰）的 ref：missing fail-fast。"""
    drv = FakeDriver(responses=[
        default_row(text=f"ACTION: resolve_tool_payload(payload_ref={ZERO64}, "
                         f"expected_hash={ZERO64})")])
    with pytest.raises(ToolPayloadMissingError):
        get_workload("tool_call").run(drv, _tool_call_spec(steps=2, mode="externalized"))


def test_resolve_malformed_action_fail_fast():
    """ACTION 是 resolve_tool_payload 但格式非法（缺 expected_hash / ref 非 64 hex）。"""
    drv = FakeDriver(responses=[default_row(text="ACTION: resolve_tool_payload(payload_ref=abc)")])
    with pytest.raises(ToolPayloadError):
        get_workload("tool_call").run(drv, _tool_call_spec(steps=2, mode="externalized"))


def test_unknown_tool_action_fail_fast_preserved():
    """普通未知工具：保持原语义 KeyError fail-fast（无静默 fallback）。"""
    drv = FakeDriver(responses=[default_row(text="ACTION: unknown_tool(foo=1)")])
    with pytest.raises(KeyError):
        get_workload("tool_call").run(drv, _tool_call_spec(steps=2, mode="externalized"))


# ---- thread / run 隔离 -----------------------------------------------------------------

def test_run_isolation_ref_not_shared():
    """run1 的 ref 在 run2（新 store）中引用 → missing fail-fast（跨 run 隔离）。"""
    drv1 = FakeDriver(responses=[
        default_row(text="ACTION: search_orders(customer_id=C10086)"),
        default_row(text="完成"),
    ])
    get_workload("tool_call").run(drv1, _tool_call_spec(steps=2, mode="externalized"))
    proj = _last_user_content(drv1.calls[1])
    ref = RESOLVE_REF_RE.search(proj).group(1)
    h = RESOLVE_HASH_RE.search(proj).group(1)
    drv2 = FakeDriver(responses=[
        default_row(text=f"ACTION: resolve_tool_payload(payload_ref={ref}, expected_hash={h})")])
    with pytest.raises(ToolPayloadMissingError):
        get_workload("tool_call").run(drv2, _tool_call_spec(steps=2, mode="externalized"))


def test_thread_isolation_concurrent_runs():
    """并发 run（各线程独立 store）：不同 customer → 不同 ref，各自 resolve 正常。

    能区分独立 store 与误共享 store：若误共享同一 store 实例（错误实现），
    thread-scoped 检查会触发 ToolPayloadCrossThreadError fail-fast（本测试立即失败），
    或共享导致 ref 碰撞（断言 len(set(refs))==4 失败）。
    """

    def _run(customer):
        drv = _ResolveFlowDriver(customer=customer)
        get_workload("tool_call").run(drv, _tool_call_spec(steps=6, mode="externalized"))
        return drv.resolved_ref

    customers = [f"C{10000 + i}" for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        refs = list(ex.map(_run, customers))
    assert len(refs) == 4
    assert all(r for r in refs)           # 每个线程内 resolve 均成功（无跨线程污染）
    assert len(set(refs)) == 4            # 各自内容不同 → 各自 ref 不同（独立 store 证据）


# ---- 配置校验 ---------------------------------------------------------------------------

def test_invalid_tool_payload_mode_fail_fast():
    spec = get_workload("tool_call").generate(
        {"steps": 2, "tool_payload_mode": "bogus"})
    with pytest.raises(ValueError):
        get_workload("tool_call").run(
            FakeDriver(responses=[default_row(text="完成")]), spec)


# ---- long_life 工具轮同一语义 -----------------------------------------------------------

def _long_life_responses():
    return [default_row(text="回答")] * 3 + [
        default_row(text="ACTION: search_orders(customer_id=C10086)"),
        default_row(text="订单已汇总"),
        default_row(text="回答"),
        default_row(text="回答"),
        default_row(text="回答"),
        default_row(text="9527"),
    ]


def _long_life_spec(rounds=8, mode="off"):
    return get_workload("long_life").generate(
        {"rounds": rounds, "secret": "9527", "ctx_size": 2048,
         "tool_payload_mode": mode})


def test_long_life_tool_round_externalized_projection():
    """long_life 工具轮与 tool_call 同一语义：put + projection 注入 + 审计。"""
    drv = FakeDriver(responses=_long_life_responses())
    result = get_workload("long_life").run(drv, _long_life_spec(mode="externalized"))
    meta = result["meta"]
    assert meta["tool_payload"]["mode"] == "externalized"
    assert meta["tool_payload"]["externalized_puts"] == 1
    assert meta["tool_payload"]["resolve_requests"] == 0
    tool_rows = [r for r in result["rows"] if r.get("kind") == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["tool_payload"]["kind"] == "externalized_projection"
    # 工具轮 tool_response 是 projection（无 address 原文，含 payload_ref）
    tool_resp_msgs = [c for c in drv.calls if "tool_response" in _last_user_content(c)]
    assert len(tool_resp_msgs) == 1
    resp = _last_user_content(tool_resp_msgs[0])
    assert "payload_ref=" in resp and "projection:" in resp
    assert "湖南省长沙市" not in resp
    # evaluate 不受审计字段影响
    ev = get_workload("long_life").evaluate(result, _long_life_spec(mode="externalized"))
    assert ev["task_success"] is True
    assert ev["state_retention_rate"] == 1.0


def test_long_life_tool_round_off_keeps_full_payload():
    """long_life off（默认）：完整 payload 直接回填，meta 无审计字段。"""
    drv = FakeDriver(responses=_long_life_responses())
    result = get_workload("long_life").run(drv, _long_life_spec(mode="off"))
    assert "tool_payload" not in result["meta"]
    tool_resp_msgs = [c for c in drv.calls if "tool_response" in _last_user_content(c)]
    resp = _last_user_content(tool_resp_msgs[0])
    assert "customer_id=C10086" in resp
    assert "SO00000" in resp
    assert all("tool_payload" not in r for r in result["rows"])


def test_long_life_tool_round_resolve_interception_fail_fast():
    """long_life 工具轮同样拦截 resolve 动作：非法格式 → fail-fast（同一语义）。"""
    responses = [default_row(text="回答")] * 3 + [
        default_row(text="ACTION: resolve_tool_payload(payload_ref=bad)"),
    ]
    drv = FakeDriver(responses=responses)
    with pytest.raises(ToolPayloadError):
        get_workload("long_life").run(drv, _long_life_spec(mode="externalized"))


class _LongLifeResolveDriver(FakeDriver):
    """long_life 动态 driver：轮4 工具轮 search_orders（put），轮8 工具轮 resolve
    （引用轮4 的 ref/hash）。验证：完整 payload 仅一次局部 tool_msgs 请求可见，
    不进入长期 history。"""

    def __init__(self):
        super().__init__()
        self.tool_rounds = 0        # 已发生的工具轮数
        self.ref = None
        self.hash = None
        self.full_calls = []        # 含完整 payload 的调用索引

    def chat(self, messages, _retry=0):
        self.calls.append([dict(m) for m in messages])
        last_user = _last_user_content(messages)
        if last_user == "请查询客户 C10086 的最新一笔订单详情。":
            # 工具轮第一次调用（局部 tool_msgs 无 tool_response）
            if self.tool_rounds == 0:
                self.tool_rounds += 1
                text = "ACTION: search_orders(customer_id=C10086)"
            elif self.tool_rounds == 1:
                self.tool_rounds += 1
                # 引用轮4 的 ref/hash → resolve 成功
                text = (f"ACTION: resolve_tool_payload(payload_ref={self.ref}, "
                        f"expected_hash={self.hash})")
            else:
                text = "ACTION: search_orders(customer_id=C10086)"
        elif "tool_response" in last_user:
            # 工具轮第二次调用（收到 tool_response）
            if "projection:" in last_user and self.ref is None:
                self.ref = RESOLVE_REF_RE.search(last_user).group(1)
                self.hash = RESOLVE_HASH_RE.search(last_user).group(1)
            elif '"customer":"customer_id=C10086"' in last_user:
                self.full_calls.append(len(self.calls) - 1)
            text = "订单已汇总"
        elif "秘密数字" in last_user:
            text = "9527"
        else:
            text = "回答"
        return dict(default_row(text=text))


def test_long_life_resolve_success_one_shot_local():
    """long_life 工具轮 resolve 成功：完整 payload 仅一次局部请求可见，不进入长期 history。"""
    drv = _LongLifeResolveDriver()
    spec = get_workload("long_life").generate(
        {"rounds": 12, "secret": "9527", "ctx_size": 2048,
         "tool_payload_mode": "externalized"})
    result = get_workload("long_life").run(drv, spec)
    assert drv.ref and drv.hash
    # 完整 payload 恰好注入一次（轮8 的局部 r2 请求）
    assert len(drv.full_calls) == 1
    full_idx = drv.full_calls[0]
    assert '"customer":"customer_id=C10086"' in "".join(
        str(m.get("content", "")) for m in drv.calls[full_idx])
    # 除该局部请求外，其余所有调用（含轮9-12 的长期 history）整个 messages 不含完整原文
    # （系统提示/工具轮查询含 "customer_id=C10086" 查询参数，故用 canonical 独有特征断言）
    for idx, call in enumerate(drv.calls):
        if idx == full_idx:
            continue
        all_text = "".join(str(m.get("content", "")) for m in call)
        assert '"customer":"customer_id=C10086"' not in all_text
        assert "湖南省长沙市" not in all_text
    # 工具行审计：轮4 = externalized_projection，轮8 = resolve_full
    tool_rows = [r for r in result["rows"] if r.get("kind") == "tool"]
    assert [r["tool_payload"]["kind"] for r in tool_rows] == \
        ["externalized_projection", "resolve_full"]
    meta = result["meta"]["tool_payload"]
    assert meta["externalized_puts"] == 1       # 仅轮4 search put；轮8 resolve 不 put
    assert meta["unique_refs"] == 1
    assert meta["resolve_requests"] == 1
    assert meta["resolve_successes"] == 1
    assert meta["resolve_failures"] == 0
    # evaluate 正常（secret 召回不受影响）
    ev = get_workload("long_life").evaluate(result, spec)
    assert ev["task_success"] is True
    assert ev["state_retention_rate"] == 1.0


# ---- evaluate：排除内部 resolve_tool_payload 动作 -------------------------------------

def test_evaluate_excludes_internal_resolve_action():
    """evaluate 的 tool_calls/tool_completion 只统计真实 MOCK_TOOLS 动作，
    排除内部 resolve_tool_payload。"""
    wl = get_workload("tool_call")
    rows = [
        dict(default_row(text="ACTION: search_orders(customer_id=C10086)"),
             step=1, action="search_orders"),
        dict(default_row(text="ACTION: resolve_tool_payload(payload_ref=abc)"),
             step=2, action="resolve_tool_payload"),
        dict(default_row(text="ACTION: get_order_detail(order_id=SO00000)"),
             step=3, action="get_order_detail"),
        dict(default_row(text="完成"), step=4, action=None),
    ]
    spec = wl.generate({"steps": 4})
    ev = wl.evaluate({"rows": rows}, spec)
    assert ev["tool_calls"] == 2                    # 仅真实工具
    assert ev["task_success"] is True
    assert ev["tool_completion"] == round(2 / 4, 4)


# ---- one-shot 状态机严格校验（reviewer 修复）-----------------------------------------

def _pending_messages(full: str = "完整 payload 原文", projection: str = "projection 占位"):
    """构造注入完整 payload 后的消息列表，并登记 pending（含 expected_content）。"""
    tp = ToolPayloadRun("externalized")
    messages = [{"role": "user", "content": f"<tool_response>\n{full}\n</tool_response>"}]
    idx = len(messages) - 1
    tp.mark_pending(idx, f"<tool_response>\n{projection}\n</tool_response>",
                    expected_content=messages[idx]["content"])
    return tp, messages, idx


def test_after_chat_restores_pending_normally():
    """正常路径：pending 原位恢复为 projection 占位，且 pending 被消费。"""
    tp, messages, idx = _pending_messages()
    tp.after_chat(messages)
    assert messages[idx]["content"] == "<tool_response>\nprojection 占位\n</tool_response>"
    assert tp._pending_restore is None
    # 无 pending 时 after_chat 为 no-op
    tp.after_chat(messages)


def test_after_chat_index_out_of_range_fail_fast():
    """pending index 越界 → ToolPayloadError，且不静默清空 pending。"""
    tp, messages, idx = _pending_messages()
    del messages[idx]                       # 模拟消息被裁剪导致 index 越界
    with pytest.raises(ToolPayloadError):
        tp.after_chat(messages)
    assert tp._pending_restore is not None  # 绝不静默清空


def test_after_chat_non_user_role_fail_fast():
    """pending 位置不是 user 消息 → ToolPayloadError，不清空 pending。"""
    tp, messages, idx = _pending_messages()
    messages[idx]["role"] = "assistant"     # 模拟角色被改写
    with pytest.raises(ToolPayloadError):
        tp.after_chat(messages)
    assert tp._pending_restore is not None


def test_after_chat_missing_projection_fail_fast():
    """pending projection 缺失 → ToolPayloadError，不清空 pending。"""
    tp, messages, idx = _pending_messages()
    tp._pending_restore["projection"] = None
    with pytest.raises(ToolPayloadError):
        tp.after_chat(messages)
    assert tp._pending_restore is not None


def test_after_chat_content_mismatch_fail_fast():
    """pending 位置内容与注入时不匹配 → ToolPayloadError，不清空 pending。"""
    tp, messages, idx = _pending_messages()
    messages[idx]["content"] = "被其他逻辑改写的内容"
    with pytest.raises(ToolPayloadError):
        tp.after_chat(messages)
    assert tp._pending_restore is not None


def test_pending_does_not_cross_rounds_long_life():
    """long_life：工具轮 resolve 注入的 pending 在同一工具轮 r2 后即被消费，
    不得跨轮残留——轮8 后的普通轮 after_chat(history) 必须 no-op（不抛错）。"""
    drv = _LongLifeResolveDriver()
    spec = get_workload("long_life").generate(
        {"rounds": 12, "secret": "9527", "ctx_size": 2048,
         "tool_payload_mode": "externalized"})
    result = get_workload("long_life").run(drv, spec)   # 若 pending 残留跨轮，此处抛错
    assert result["meta"]["tool_payload"]["resolve_successes"] == 1
    # 轮8（resolve 轮）之后仍有 4 个普通轮（9-12）正常执行，证明 pending 未跨轮
    assert len(drv.calls) == 14    # 10 普通轮 chat + 2 个工具轮 ×2 次 = 14
