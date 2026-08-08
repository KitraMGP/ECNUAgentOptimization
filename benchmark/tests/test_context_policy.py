"""context_policy 单元测试（E15.4 C 线核心，纯函数，无 GPU/真实 server 依赖）。

覆盖（对应 E15.4 验收）：
- full identity
- trim 保护 system/tool schema/关键事实/最新 user、角色序列、预算
- extractive_summary 确定性 / 去重 / 不编造 / needle early/middle/late 保留 / 工具投影
- 跨 thread 隔离、snapshot/restore/purge
- invalid fail-fast（非法消息/预算/跨 thread restore/错误预算超限）
- DuplicateBlockCompressor round-trip（deterministic reversible preprocessor，与 summary 分开）

注意：本文件**不**mock 模型输出证明质量——真实模型 paired 与 12.12 门禁未验证，
见 `docs/E15_4_C_CONTEXT_POLICY_CORE_DRAFT.md`。
"""
from __future__ import annotations

import json

import pytest

from framework.context_policy import (
    BudgetError,
    CompressionManifest,
    ContextPolicy,
    ContextPolicyConfig,
    CrossThreadRestoreError,
    DuplicateBlockCompressor,
    ErrorBudgetExceeded,
    Fact,
    IllegalMessageError,
    Message,
    MessageCategory,
    ThreadState,
    classify_message,
    estimate_tokens,
    stable_hash,
    validate_role_sequence,
)

SYSTEM = "你是乐于助人的中文助手。回答简洁准确。"
TOOL_SYSTEM = """你是智能客服助手，可调用以下工具：
- search_orders(customer_id)：查询客户订单列表
- get_order_detail(order_id)：查询单个订单的完整详情
需要调用工具时，只输出一行：ACTION: 工具名(参数)。"""
POOL = ["问题A", "问题B", "问题C", "问题D"]


# ---- 辅助构造 ---------------------------------------------------------------


def build_long_thread(thread_id: str = "t1", rounds: int = 8, secret_round: int | None = None,
                      secret: str = "9527", policy: str = "full", keep: int = 4,
                      budget: int | None = None, max_facts: int = 64) -> ThreadState:
    st = ThreadState(thread_id, ContextPolicyConfig(
        policy=ContextPolicy(policy), keep_recent_rounds=keep,
        budget_chars=budget, max_facts=max_facts))
    st.append("system", SYSTEM)
    for i in range(1, rounds + 1):
        if i == secret_round:
            q = f"请记住这个秘密数字：{secret}。只回答两个字：记住了。"
        else:
            q = POOL[i % len(POOL)]
        st.append("user", q)
        st.append("assistant", f"回答{i}")
    return st


def build_tool_thread(thread_id: str = "t-tool", rounds: int = 4,
                      policy: str = "full", keep: int = 2) -> ThreadState:
    st = ThreadState(thread_id, ContextPolicyConfig(policy=ContextPolicy(policy),
                                                    keep_recent_rounds=keep))
    st.append("system", TOOL_SYSTEM)
    for i in range(1, rounds + 1):
        st.append("user", f"第{i}次查询客户 C10086 的订单。")
        st.append("assistant", f"ACTION: search_orders(customer_id=C10086) 第{i}步")
        st.append("user", f"<tool_response>\n{{\"order_id\": \"SO{i:05d}\"}}\n</tool_response>")
        st.append("assistant", f"第{i}轮汇总：订单 SO{i:05d} 金额正常。")
    return st


def all_contents(msgs) -> str:
    return "\n".join(m.content for m in msgs)


# ---- 1) full identity -------------------------------------------------------


def test_full_identity_returns_original_messages():
    st = build_long_thread(rounds=5, policy="full")
    orig = st.messages
    res = st.apply_policy()
    assert [m.as_dict() for m in res.messages] == [m.as_dict() for m in orig]
    assert res.manifest.policy == "full"
    assert res.manifest.lossless is True
    assert res.manifest.trimmed_ids == []
    assert res.manifest.kept_ids == [m.id for m in orig]
    assert res.manifest.stats.compression_ratio == 0.0
    assert res.manifest.stats.input_chars == res.manifest.stats.output_chars


def test_full_policy_never_mutates_state():
    st = build_long_thread(rounds=4)
    before = [m.as_dict() for m in st.messages]
    st.apply_policy()
    st.apply_policy()
    assert [m.as_dict() for m in st.messages] == before


def test_apply_policy_idempotent_repeat_call():
    """apply_policy 幂等：不修改 ThreadState 的消息/seq/facts；重复调用输出 hash/ids 稳定；
    summary 消息 id 由内容 hash 派生且不推进 seq。"""
    st = build_long_thread(rounds=8, secret_round=2, policy="extractive_summary", keep=2)
    before_msgs = [m.as_dict() for m in st.messages]
    before_facts = {f.id: (f.text, f.kind) for f in st.facts.values()}
    before_seq = st._seq
    r1 = st.apply_policy()
    r2 = st.apply_policy()
    # 状态不变：消息 / facts / seq
    assert [m.as_dict() for m in st.messages] == before_msgs
    assert {f.id: (f.text, f.kind) for f in st.facts.values()} == before_facts
    assert st._seq == before_seq
    # 重复调用输出稳定：hash 与消息 ids 逐位一致
    assert r1.manifest.output_hash == r2.manifest.output_hash
    assert r1.manifest.input_hash == r2.manifest.input_hash
    assert [m.id for m in r1.messages] == [m.id for m in r2.messages]
    # summary 消息 id 由内容 hash 派生（非 seq）：两次相同、前缀 summary-
    s1 = [m for m in r1.messages if m.category == MessageCategory.SUMMARY]
    s2 = [m for m in r2.messages if m.category == MessageCategory.SUMMARY]
    assert s1 and [m.id for m in s1] == [m.id for m in s2]
    assert all(m.id.startswith("summary-") for m in s1)
    # seq 未被推进 → 后续 append 不产生 id 冲突
    st.append("user", "幂等后的新提问")
    ids = [m.id for m in st.messages]
    assert len(ids) == len(set(ids))


# ---- 2) trim：保护 system / tool_schema / fact / 最新 user ---------------------


def test_trim_keeps_system_and_tool_schema():
    st = build_tool_thread(rounds=6, policy="trim", keep=2)
    res = st.apply_policy()
    contents = all_contents(res.messages)
    assert TOOL_SYSTEM in contents  # L2 tool schema 永不删
    assert "system" in [m.role for m in res.messages]
    assert res.manifest.stats.protected_preserved is True


def test_trim_keeps_registered_fact_message():
    st = build_long_thread(rounds=8, policy="trim", keep=2)
    # 注册关键事实，绑定第 1 轮 user 消息
    fact_msg = st.messages[1]
    assert fact_msg.role == "user"
    fact = st.register_fact("请记住这个秘密数字：9527。只回答两个字：记住了。",
                            source_message_id=fact_msg.id)
    res = st.apply_policy()
    ids = {m.id for m in res.messages}
    assert fact_msg.id in ids  # fact 消息受保护
    assert fact_msg.content in all_contents(res.messages)  # 原文保留
    assert fact.id in st.facts


def test_trim_keeps_latest_user_query():
    st = build_long_thread(rounds=10, policy="trim", keep=1, budget=50)
    res = st.apply_policy()
    # 至少保留一条真实 user query（最新轮）
    users = [m for m in res.messages if m.role == "user"]
    assert users, "trim 后必须仍有真实 user 提问"
    assert st.messages[-2].role == "user"  # 原最新轮 user 消息
    assert st.messages[-2].id in {m.id for m in res.messages}


def test_trim_never_removes_all_user_queries_under_tiny_budget():
    st = build_long_thread(rounds=12, policy="trim", keep=1, budget=10)
    res = st.apply_policy()
    users = [m for m in res.messages if m.role == "user"]
    assert len(users) >= 1
    # 预算极小也无法满足时，注明下限（stats 仍如实）
    assert any("min_user_queries" in n for n in res.manifest.notes)


# ---- 3) 角色序列 ---------------------------------------------------------------


def test_trim_output_role_sequence_valid():
    st = build_long_thread(rounds=10, policy="trim", keep=3, budget=300)
    res = st.apply_policy()
    ok, reason = validate_role_sequence(res.messages)
    assert ok, reason


def test_trim_tool_thread_role_sequence_valid():
    st = build_tool_thread(rounds=5, policy="trim", keep=2)
    res = st.apply_policy()
    ok, reason = validate_role_sequence(res.messages)
    assert ok, reason


def test_trim_keeps_message_relative_order():
    st = build_long_thread(rounds=8, policy="trim", keep=3)
    res = st.apply_policy()
    kept = [m.id for m in res.messages]
    orig = [m.id for m in st.messages]
    # 输出是原消息序列的子序列（保持相对顺序）
    idx = 0
    for kid in kept:
        assert kid in orig[idx:], "输出必须保持原消息相对顺序"
        idx = orig.index(kid) + 1


# ---- 4) 预算 ------------------------------------------------------------------


def test_trim_budget_reduces_output():
    st = build_long_thread(rounds=12, policy="trim", keep=4)
    res = st.apply_policy()
    out_chars = res.manifest.stats.output_chars
    in_chars = res.manifest.stats.input_chars
    assert out_chars < in_chars
    assert res.manifest.stats.compression_ratio > 0
    assert res.manifest.stats.input_messages > res.manifest.stats.output_messages


def test_trim_token_budget_converted_to_chars():
    st = build_long_thread(rounds=10, policy="trim", keep=2, budget=None)
    cfg = ContextPolicyConfig(policy="trim", budget_tokens=200, chars_per_token=3.0)
    st.config = cfg
    res = st.apply_policy()
    # token 预算折算为字符预算（200 tokens * 3 chars = 600 chars），应比无预算更激进
    assert res.manifest.stats.output_chars < res.manifest.stats.input_chars


def test_summary_budget_unmet_note_like_trim():
    """extractive_summary 预算无法满足时与 trim 一致写明 note（含 protected+min user 超限）。"""
    st = build_long_thread(rounds=10, policy="extractive_summary", keep=2, budget=10)
    res = st.apply_policy()
    # budget=10 远小于 protected（system）内容 → 无法严格满足，必须明确记录
    assert any("无法严格满足" in n and "min_user_queries" in n for n in res.manifest.notes)


def test_trim_no_budget_only_trims_by_rounds():
    st = build_long_thread(rounds=10, policy="trim", keep=3)
    res = st.apply_policy()
    # keep=3 轮 → 保留 system + 3*(user+assistant) = 7 条；被裁 7 轮 = 14 条
    assert res.manifest.stats.output_messages == 7
    assert len(res.manifest.trimmed_ids) == 14
    assert res.manifest.stats.input_messages == 21


def test_estimate_tokens_basic():
    assert estimate_tokens("abcdef", chars_per_token=3.0) == 2
    assert estimate_tokens("", chars_per_token=3.0) == 1  # 非零保证
    with pytest.raises(BudgetError):
        estimate_tokens("x", chars_per_token=0)


def test_budget_validation():
    with pytest.raises(BudgetError):
        ContextPolicyConfig(budget_chars=-1)
    with pytest.raises(BudgetError):
        ContextPolicyConfig(budget_tokens=-5)
    with pytest.raises(BudgetError):
        ContextPolicyConfig(chars_per_token=0)
    with pytest.raises(BudgetError):
        ContextPolicyConfig(keep_recent_rounds=0)
    with pytest.raises(BudgetError):
        ContextPolicyConfig(min_user_queries=0)
    with pytest.raises(BudgetError):
        ContextPolicyConfig(error_budget=-1)
    with pytest.raises(BudgetError):
        ContextPolicyConfig(max_facts=-1)
    with pytest.raises(BudgetError):
        ContextPolicyConfig(summary_block_chars=-1)


def test_budget_conflicting_min_queries_over_keep_rounds_fail_fast():
    """min_user_queries > keep_recent_rounds 是矛盾配置：即使保留全部轮次也无法满足
    最少真实 user 提问数（每轮以真实 user 提问开头）→ 构造即 BudgetError（fail-fast）。"""
    with pytest.raises(BudgetError, match="矛盾配置|无法满足最少真实 user"):
        ContextPolicyConfig(min_user_queries=5, keep_recent_rounds=2)
    # 相等与 min < keep 均合法
    ContextPolicyConfig(min_user_queries=2, keep_recent_rounds=2)
    ContextPolicyConfig(min_user_queries=1, keep_recent_rounds=4)
    # 合法配置下 trim 能真正保证 min_user_queries 下限（不会出现保留轮次不足下限）
    st = build_long_thread(rounds=12, policy="trim", keep=3, budget=40)
    st.config = ContextPolicyConfig(policy="trim", keep_recent_rounds=3,
                                    min_user_queries=2, budget_chars=40)
    res = st.apply_policy()
    users = [m for m in res.messages if m.role == "user"]
    assert len(users) >= 2


# ---- 5) extractive_summary：确定性 / 去重 / 不编造 -----------------------------


def test_summary_deterministic():
    st1 = build_long_thread(rounds=8, secret_round=2, policy="extractive_summary", keep=2)
    st2 = build_long_thread(rounds=8, secret_round=2, policy="extractive_summary", keep=2)
    r1 = st1.apply_policy()
    r2 = st2.apply_policy()
    assert r1.manifest.output_hash == r2.manifest.output_hash
    assert r1.manifest.input_hash == r2.manifest.input_hash
    b1 = r1.manifest.summary_blocks[0]
    b2 = r2.manifest.summary_blocks[0]
    assert b1.id == b2.id
    assert b1.rendered == b2.rendered
    assert [f.text for f in b1.facts] == [f.text for f in b2.facts]


def test_summary_is_lossy_candidate_never_lossless():
    st = build_long_thread(rounds=8, secret_round=2, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    assert res.manifest.lossless is False
    assert res.manifest.summary_blocks[0].lossless is False
    assert any("不伪称可从摘要恢复全部原文" in n for n in res.manifest.notes)


def test_summary_does_not_invent_facts():
    st = build_long_thread(rounds=8, secret_round=3, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    trimmed_contents = all_contents([m for m in st.messages if m.id in res.manifest.trimmed_ids])
    for f in block.facts:
        # 每个 fact.text 必须是某被裁消息原文的精确子串（不编造）
        assert trimmed_contents.find(f.text) != -1, f"fact 编造：{f.text!r}"
        assert f.kind in ("registered", "secret_declaration", "notable_statement")


def test_summary_deduplicates_identical_facts():
    # 两轮出现相同"记住"声明 → facts 中只保留一次
    st = ThreadState("t1", ContextPolicyConfig(
        policy="extractive_summary", keep_recent_rounds=1))
    st.append("system", SYSTEM)
    st.append("user", "请记住这个秘密数字：7777。只回答两个字：记住了。")
    st.append("assistant", "记住了")
    st.append("user", "请记住这个秘密数字：7777。只回答两个字：记住了。")
    st.append("assistant", "记住了")
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    texts = [f.text for f in block.facts]
    assert len(texts) == len(set(texts)), "同 text 事实必须去重"
    assert any("7777" in t for t in texts)


def test_summary_registered_fact_projected_when_source_trimmed():
    st = build_long_thread(rounds=8, policy="extractive_summary", keep=2)
    fact_msg = st.messages[1]
    fact = st.register_fact("请记住这个秘密数字：9527。只回答两个字：记住了。",
                            source_message_id=fact_msg.id)
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    assert fact_msg.id in res.manifest.trimmed_ids  # fact 消息被裁
    texts = [f.text for f in block.facts]
    assert fact.text in texts  # 但事实投影进 summary（needle 保留）
    assert "9527" in all_contents(res.messages)


def test_summary_block_bounded():
    # 10 轮，前 8 轮各带不同秘密数字 → 8 个事实，max_facts=3 截断；rendered 有界
    st = ThreadState("t1", ContextPolicyConfig(
        policy="extractive_summary", keep_recent_rounds=2,
        max_facts=3, summary_block_chars=400))
    st.append("system", SYSTEM)
    for i in range(1, 11):
        if i <= 8:
            q = f"请记住这个秘密数字：{1000 + i}。只回答两个字：记住了。"
        else:
            q = POOL[i % len(POOL)]
        st.append("user", q)
        st.append("assistant", f"回答{i}")
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    assert len(block.facts) <= 3  # max_facts 截断生效
    assert len(block.rendered) <= 400  # 有界
    assert block.rendered.endswith("</summary_block>")  # 闭合标签必须保留
    assert any("截断" in n or "max_facts" in n for n in res.manifest.notes)


def test_summary_block_tiny_budget_raises_budget_error():
    # 极小预算连 header + 全部 fact + 闭合标签都放不下 → 显式 BudgetError，绝不静默截断 needle
    st = ThreadState("t1", ContextPolicyConfig(
        policy="extractive_summary", keep_recent_rounds=2,
        summary_block_chars=50))
    st.append("system", SYSTEM)
    st.append("user", "请记住这个秘密数字：9527。只回答两个字：记住了。")
    st.append("assistant", "记住了")
    for i in range(2, 9):
        st.append("user", POOL[i % len(POOL)])
        st.append("assistant", f"回答{i}")
    with pytest.raises(BudgetError, match="不允许截断|无法容纳"):
        st.apply_policy()


def test_summary_block_needle_never_truncated_under_tight_budget():
    # 预算恰好容纳必需（fact 行 + 闭合标签 ≈ 240 字符）但放不下 tool projections：
    # fact（needle）完整保留、闭合标签保留、仅 projections 被丢弃并标记 truncated
    st = ThreadState("t1", ContextPolicyConfig(
        policy="extractive_summary", keep_recent_rounds=1,
        summary_block_chars=262))
    st.append("system", TOOL_SYSTEM)
    # 第 1 轮含秘密数字（needle），第 2-3 轮为普通工具轮
    st.append("user", "请记住这个秘密数字：6666。只回答两个字：记住了。")
    st.append("assistant", "记住了")
    for i in range(2, 4):
        st.append("user", f"第{i}次查询客户 C10086 的订单。")
        st.append("assistant", f"ACTION: search_orders(customer_id=C10086) 第{i}步")
        st.append("user", f"<tool_response>\n{{\"order_id\": \"SO{i:05d}\"}}\n</tool_response>")
        st.append("assistant", f"第{i}轮汇总：订单 SO{i:05d} 金额正常。")
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    # needle 完整性：被裁 secret 事实完整保留在渲染文本中（绝不截断）
    assert "6666" in block.rendered
    for f in block.facts:
        assert f.text in block.rendered
    assert block.rendered.endswith("</summary_block>")  # 闭合标签必须保留
    assert len(block.rendered) <= 262  # 有界
    assert block.truncated is True  # projections 被截断（needle 不放）→ 显式标记


def test_summary_records_covered_message_ids_and_hash():
    st = build_long_thread(rounds=8, secret_round=2, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    assert sorted(block.covered_message_ids) == sorted(res.manifest.trimmed_ids)
    assert block.covered_hash  # 覆盖消息的规范化 hash（可审计）
    assert len(block.covered_message_ids) >= 1


def test_summary_projection_lines_are_continuous_prefix():
    """projection 行按**连续前缀**容纳：一旦某行放不下即停止，绝不出现中间空洞
    （后面的短行不会越过前面的长行被单独保留）。"""
    def build(limit: int) -> ThreadState:
        st = ThreadState("t1", ContextPolicyConfig(
            policy="extractive_summary", keep_recent_rounds=1,
            summary_block_chars=limit))
        st.append("system", TOOL_SYSTEM)
        # 第 1 轮：长 args（长 projection 行）；第 2 轮：短 args（短 projection 行）；
        # 第 3 轮：普通轮（keep=1 的保留窗口）
        st.append("user", "第1次查询客户 C10086 的订单。")
        st.append("assistant", "ACTION: search_orders(customer_id=C10086, order_status=SHIPPED, "
                               "include_items=true, page_size=50, offset=0, page_token=abc) 第1步")
        st.append("user", "<tool_response>\n{\"order_id\": \"SO00001\"}\n</tool_response>")
        st.append("assistant", "第1轮汇总：订单 SO00001 金额正常。")
        st.append("user", "第2次查询客户 C10086 的订单。")
        st.append("assistant", "ACTION: search_orders(customer_id=C10086) 第2步")
        st.append("user", "<tool_response>\n{\"order_id\": \"SO00002\"}\n</tool_response>")
        st.append("assistant", "第2轮汇总：订单 SO00002 金额正常。")
        st.append("user", "第3次查询客户 C10086 的订单。")
        st.append("assistant", "第3轮汇总：订单 SO00003 金额正常。")
        return st

    # 先用不设限预算拿到全部 projection 行的真实渲染文本（确定性）
    full = build(10 ** 9)
    full_block = full.apply_policy().manifest.summary_blocks[0]
    all_proj_lines = [l for l in full_block.rendered.splitlines() if l.startswith("- [tool]")]
    assert len(all_proj_lines) == 2  # 第 1、2 轮（被裁）各一个 ACTION 投影
    # 第 1 行长行、第 2 行短行
    long_line, short_line = all_proj_lines
    assert len(long_line) > len(short_line)
    head = "# tool projections"
    head_idx = full_block.rendered.index(head)
    prefix = full_block.rendered[:head_idx]  # 头之前的内容（含换行）
    # 预算 = 恰好容纳"头 + 短行"、放不下"头 + 长行"（长行在前 → 长行放不下即停止）
    tight_limit = len(prefix) + len(head) + 1 + len(short_line) + 1 + len("</summary_block>")
    tight = build(tight_limit)
    tight_block = tight.apply_policy().manifest.summary_blocks[0]
    rendered_lines = [l for l in tight_block.rendered.splitlines() if l.startswith("- [tool]")]
    # 连续前缀：长行（第 1 个）放不下 → 后续短行绝不越过它被单独保留（无中间空洞）
    assert rendered_lines == []
    assert "# tool projections" in tight_block.rendered  # 头本身保留
    assert short_line not in tight_block.rendered
    assert long_line not in tight_block.rendered
    assert tight_block.truncated is True
    assert len(tight_block.rendered) <= tight_limit


def test_summary_fact_with_newlines_normalized_and_bounded():
    """fact 文本含换行时渲染单行化（换行 → 空格），行语义与 summary_block_chars 严格成立；
    fact.text 对象本身保持原文（精确子串不变式）。"""
    st = ThreadState("t1", ContextPolicyConfig(
        policy="extractive_summary", keep_recent_rounds=1,
        summary_block_chars=400))
    st.append("system", SYSTEM)
    st.append("user", "第一行提问")
    st.append("assistant", "回答")
    # 注册含换行的事实（绑定第 1 轮 user 消息，keep=1 时该轮被裁 → 事实投影进块）
    fact_msg = st.messages[1]
    fact = st.register_fact("第一行\n秘密数字：8888\n第二行",
                            source_message_id=fact_msg.id)
    assert "\n" in fact.text  # 事实原文含换行（子串不变式要求原文保留）
    for i in range(2, 6):
        st.append("user", POOL[i % len(POOL)])
        st.append("assistant", f"回答{i}")
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    assert len(block.rendered) <= 400  # summary_block_chars 约束严格成立
    # 渲染单行化：fact 以单行呈现（换行被替换为空格），不再以多行形式出现在 rendered 中
    assert "第一行 秘密数字：8888 第二行" in block.rendered
    assert "- [fact:registered] 第一行\n秘密数字" not in block.rendered
    assert block.rendered.endswith("</summary_block>")
    # 渲染中每个 fact 恰好占一行（fact 行内不含 \n）
    for line in block.rendered.splitlines():
        if line.startswith("- [fact:"):
            assert "\n" not in line


def test_summary_tool_projection():
    st = build_tool_thread(rounds=6, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    block = res.manifest.summary_blocks[0]
    tools = {p.tool for p in block.tool_projections}
    assert "search_orders" in tools
    proj = [p for p in block.tool_projections if p.tool == "search_orders"][0]
    assert proj.args == "customer_id=C10086"
    assert proj.result_hash is not None  # tool_response 只记录引用 hash，不复制全文
    assert proj.message_id  # ACTION 消息 id 可审计
    # 工具投影同样不编造：args 必须是原文子串
    trimmed = [m for m in st.messages if m.id in res.manifest.trimmed_ids]
    assert any(p.args in m.content for p in block.tool_projections for m in trimmed)


def test_summary_block_injected_after_system_tool_schema():
    """summary block 注入位置：所有 L1 system / L2 tool_schema 之后、第一个历史消息之前；
    且注入后的 role sequence 再次验证通过（否则抛 IllegalMessageError）。"""
    st = ThreadState("t1", ContextPolicyConfig(
        policy="extractive_summary", keep_recent_rounds=1))
    st.append("system", SYSTEM)          # L1
    st.append("system", TOOL_SYSTEM)     # L2（role=system）
    for i in range(1, 4):
        st.append("user", f"第{i}次查询客户 C10086 的订单。")
        st.append("assistant", f"ACTION: search_orders(customer_id=C10086) 第{i}步")
        st.append("user", f"<tool_response>\n{{\"order_id\": \"SO{i:05d}\"}}\n</tool_response>")
        st.append("assistant", f"第{i}轮汇总：订单 SO{i:05d} 金额正常。")
    res = st.apply_policy()
    cats = [m.category for m in res.messages]
    # 位置契约：L1 → L2 → summary → 历史
    assert cats[0] == MessageCategory.SYSTEM
    assert cats[1] == MessageCategory.TOOL_SCHEMA
    assert cats[2] == MessageCategory.SUMMARY
    summary_idx = cats.index(MessageCategory.SUMMARY)
    # summary 之前只有 system/tool_schema（不允许插到 system 前）
    assert all(
        c in (MessageCategory.SYSTEM, MessageCategory.TOOL_SCHEMA, MessageCategory.SUMMARY)
        for c in cats[: summary_idx + 1]
    )
    # summary 之后紧跟历史消息（role ∈ user/assistant/tool），且不再出现 system/schema
    assert res.messages[summary_idx + 1].role in ("user", "assistant", "tool")
    assert all(
        c not in (MessageCategory.SYSTEM, MessageCategory.TOOL_SCHEMA)
        for c in cats[summary_idx + 1:]
    )
    # 注入后的 role sequence 再验证（reviewer 需求 8）
    ok, reason = validate_role_sequence(res.messages)
    assert ok, reason


# ---- 6) needle early / middle / late 保留 --------------------------------------


def test_needle_early_kept_in_summary():
    st = build_long_thread(rounds=8, secret_round=1, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    assert "9527" in all_contents(res.messages)


def test_needle_middle_kept_in_summary():
    st = build_long_thread(rounds=10, secret_round=5, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    assert "9527" in all_contents(res.messages)


def test_needle_late_kept_in_summary():
    # secret 在倒数第 2 轮，keep=2 时该轮被裁，事实仍投影
    st = build_long_thread(rounds=8, secret_round=7, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    assert "9527" in all_contents(res.messages)


def test_needle_in_kept_window_kept_verbatim():
    # secret 在最新轮（保留窗口内）→ 原文保留，无需 summary
    st = build_long_thread(rounds=8, secret_round=8, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    users = [m for m in res.messages if m.role == "user"]
    assert any("9527" in m.content for m in users)


# ---- 7) 跨 thread 隔离 ----------------------------------------------------------


def test_thread_state_isolation_no_leak():
    a = build_long_thread(thread_id="A", rounds=8, secret_round=1,
                          policy="extractive_summary", keep=2)
    b = build_long_thread(thread_id="B", rounds=8, secret_round=None,
                          policy="extractive_summary", keep=2)
    a.apply_policy()
    b.apply_policy()
    b_contents = all_contents(b.messages)
    assert "9527" not in b_contents  # A 的事实不出现在 B
    assert b.manifest.summary_blocks[0].facts == []
    # 消息对象彼此独立（非同一引用）
    assert a.messages[1] is not b.messages[1]
    assert a.messages[1].id != b.messages[1].id


def test_thread_state_facts_scoped_per_thread():
    a = build_long_thread(thread_id="A", rounds=3)
    b = build_long_thread(thread_id="B", rounds=3)
    a.register_fact("只属于 A 的事实")
    assert "只属于 A 的事实" in [f.text for f in a.facts.values()]
    assert b.facts == {}


# ---- 8) snapshot / restore / purge ----------------------------------------------


def test_snapshot_restore_roundtrip():
    st = build_long_thread(rounds=4)
    snap = st.snapshot()
    st.append("user", "额外的提问")
    st.append("assistant", "额外的回答")
    assert len(st.messages) == len(snap.messages) + 2
    st.restore(snap)
    assert [m.as_dict() for m in st.messages] == [m.as_dict() for m in snap.messages]
    assert st.error_count == len(snap.errors)


def test_restore_then_append_ids_stay_unique():
    st = build_long_thread(rounds=3)
    snap = st.snapshot()
    st.restore(snap)
    st.append("user", "恢复后的新提问")
    ids = [m.id for m in st.messages]
    assert len(ids) == len(set(ids))  # 无 id 冲突


def test_restore_facts_and_summary_blocks():
    st = build_long_thread(rounds=6, policy="extractive_summary", keep=2)
    st.register_fact("快照前的事实")
    st.apply_policy()
    snap = st.snapshot()
    st.purge()
    st.restore(snap)
    assert "快照前的事实" in [f.text for f in st.facts.values()]
    assert st.summary_blocks  # summary 块一并恢复


def test_snapshot_restore_fact_message_ids_trim_protects():
    """ThreadSnapshot 保存并 restore ``_fact_message_ids``：source_message_id 绑定的 fact
    消息经 snapshot → purge → restore 后，trim 仍保护该消息原文（保护索引不丢失）。"""
    st = build_long_thread(rounds=8, policy="trim", keep=2)
    fact_msg = st.messages[1]
    st.register_fact("请记住这个秘密数字：9527。只回答两个字：记住了。",
                     source_message_id=fact_msg.id)
    assert fact_msg.id in st._fact_message_ids
    snap = st.snapshot()
    st.purge()
    assert st._fact_message_ids == []  # purge 清空保护索引
    st.restore(snap)
    # 恢复后保护索引同步恢复 → trim 仍保护 fact 消息（原文保留）
    assert st._fact_message_ids == [fact_msg.id]
    res = st.apply_policy()
    assert res.manifest.policy == "trim"
    ids = {m.id for m in res.messages}
    assert fact_msg.id in ids
    assert fact_msg.content in all_contents(res.messages)
    assert res.manifest.stats.protected_preserved is True


def test_snapshot_restore_restores_manifest_errors_seq():
    """完整 round-trip：manifest（含 summary_blocks 审计视图）、完整 errors 列表、
    seq 全部恢复——purge 后 restore 回到快照时刻（包括审计状态）。"""
    st = build_long_thread(rounds=6, secret_round=2, policy="extractive_summary", keep=2)
    st.apply_policy()
    # 制造一次计数错误（append 永远 fail-fast 并计数）
    with pytest.raises(IllegalMessageError):
        st.append("bogus", "x")
    snap = st.snapshot()
    assert snap.errors  # 快照保存完整错误列表
    assert snap.manifest is not None
    snap_manifest_hash = snap.manifest.output_hash
    snap_seq = snap.seq
    st.purge()
    st.restore(snap)
    # manifest 恢复：purge 后 restore 回到快照时刻的审计视图（output_hash 一致）
    assert st.manifest is not None
    assert st.manifest.output_hash == snap_manifest_hash
    assert st.manifest.summary_blocks  # manifest 内的 summary 审计视图也恢复
    # errors 完整恢复（不再是"只保留计数"）
    assert st._errors == list(snap.errors)
    assert st.error_count == len(snap.errors) == 1
    # seq 恢复：后续 append 不产生 id 冲突
    assert st._seq == snap_seq
    st.append("user", "恢复后的新提问")
    ids = [m.id for m in st.messages]
    assert len(ids) == len(set(ids))


def test_snapshot_manifest_deepcopy_isolation():
    """快照对 manifest（含可变 SummaryBlock）深拷贝：修改 thread 当前状态 / 快照任一方
    互不影响；restore 深拷贝回快照。"""
    st = build_long_thread(rounds=6, secret_round=2, policy="extractive_summary", keep=2)
    st.apply_policy()
    snap = st.snapshot()
    snap_rendered = snap.manifest.summary_blocks[0].rendered
    snap_fact_texts = [f.text for f in snap.manifest.summary_blocks[0].facts]
    # 修改 thread 当前 manifest 中的 summary block（可变结构）
    st.manifest.summary_blocks[0].rendered = "被外部修改"
    st.manifest.summary_blocks[0].facts.append(Fact.create("外部塞入的事实", "registered", []))
    # 快照不受污染
    assert snap.manifest.summary_blocks[0].rendered == snap_rendered
    assert [f.text for f in snap.manifest.summary_blocks[0].facts] == snap_fact_texts
    # restore 后回到快照时刻（外部修改被丢弃）
    st.restore(snap)
    assert st.manifest.summary_blocks[0].rendered == snap_rendered
    assert [f.text for f in st.manifest.summary_blocks[0].facts] == snap_fact_texts
    # 反向：restore 是深拷贝快照 → 恢复后再改 state 不影响快照
    st.manifest.summary_blocks[0].rendered = "恢复后再改"
    assert snap.manifest.summary_blocks[0].rendered == snap_rendered


def test_snapshot_summary_block_isolation():
    """快照对可变 SummaryBlock 深拷贝：修改 thread 当前状态 / 快照任一方互不影响。"""
    st = build_long_thread(rounds=6, secret_round=2, policy="extractive_summary", keep=2)
    st.apply_policy()
    snap = st.snapshot()
    snap_rendered = snap.summary_blocks[0].rendered
    snap_fact_texts = [f.text for f in snap.summary_blocks[0].facts]
    snap_covered = list(snap.summary_blocks[0].covered_message_ids)
    # 修改 thread 当前状态的 summary block（可变结构：rendered / facts / covered）
    st.summary_blocks[0].rendered = "被外部修改"
    st.summary_blocks[0].facts.append(Fact.create("外部塞入的事实", "registered", []))
    st.summary_blocks[0].covered_message_ids.append("xxx")
    # 快照不受污染（snapshot 时已深拷贝）
    assert snap.summary_blocks[0].rendered == snap_rendered
    assert [f.text for f in snap.summary_blocks[0].facts] == snap_fact_texts
    assert snap.summary_blocks[0].covered_message_ids == snap_covered
    # restore 后回到快照时刻状态（外部修改被丢弃）
    st.restore(snap)
    assert st.summary_blocks[0].rendered == snap_rendered
    assert [f.text for f in st.summary_blocks[0].facts] == snap_fact_texts
    assert st.summary_blocks[0].covered_message_ids == snap_covered
    # 反向：restore 是深拷贝快照 → 恢复后再改 state 不影响快照
    st.summary_blocks[0].rendered = "恢复后再改"
    assert snap.summary_blocks[0].rendered == snap_rendered


def test_cross_thread_restore_raises():
    a = build_long_thread(thread_id="A", rounds=3)
    b = build_long_thread(thread_id="B", rounds=3)
    snap = a.snapshot()
    with pytest.raises(CrossThreadRestoreError):
        b.restore(snap)


def test_purge_resets_state_keeps_identity():
    st = build_long_thread(rounds=5, policy="extractive_summary", keep=2)
    st.register_fact("要被清空的事实")
    st.apply_policy()
    st.purge()
    assert st.messages == []
    assert st.facts == {}
    assert st.summary_blocks == []
    assert st.manifest is None
    assert st.thread_id == "t1"  # thread 身份保留
    assert st.config.policy == ContextPolicy.EXTRACTIVE_SUMMARY


# ---- 9) invalid fail-fast -------------------------------------------------------


def test_append_unknown_role_raises():
    st = ThreadState("t1")
    with pytest.raises(IllegalMessageError):
        st.append("bogus", "内容")


def test_append_empty_content_raises():
    st = ThreadState("t1")
    with pytest.raises(IllegalMessageError):
        st.append("user", "   ")


def test_append_message_validates_like_append():
    """append_message 复用 append 的全部校验：role 合法 / content 非空 / system 位置。"""
    st = ThreadState("t1")
    m = Message.build("user", "你好", 0)
    got = st.append_message(m)
    assert got.role == "user" and got.content == "你好"
    assert got.id == m.id  # 同 index 同内容 → id/hash 派生一致
    with pytest.raises(IllegalMessageError):
        st.append_message(Message.build("bogus", "非法 role", 1))
    with pytest.raises(IllegalMessageError):
        st.append_message(Message.build("user", "   ", 2))
    with pytest.raises(IllegalMessageError):
        st.append_message(Message.build("system", "非头部的 system", 3))


def test_append_message_advances_seq_no_id_collision():
    """append_message 正确推进 seq：后续 append 的消息 id 不与已追加消息冲突。"""
    st = ThreadState("t1")
    st.append_message(Message.build("user", "问题A", 0))
    st.append_message(Message.build("assistant", "回答A", 1))
    assert st._seq == 2  # seq 随 append_message 推进
    st.append("user", "后续提问")  # 若 seq 未推进会与 m0000-* 冲突
    ids = [m.id for m in st.messages]
    assert len(ids) == len(set(ids))


def test_append_message_id_rederived_from_thread_seq():
    """append_message 的 id 语义：id 由 ``(thread 内当前序号, role, content)`` 重新派生，
    **不继承** 传入 msg.id（外部序号/其他 thread 的 id 不进入本 thread）。"""
    st = ThreadState("t1")
    m = Message.build("user", "你好", 99)  # 外部序号 99 与 thread 当前 seq=0 不一致
    got = st.append_message(m)
    expected_id = f"m0000-{stable_hash('你好')[:8]}"
    assert got.id == expected_id  # 按 thread 内 seq=0 重新派生
    assert got.id != m.id  # 不继承传入 id
    assert got.content == m.content  # 内容一致（仅 id 重派生）
    # 继续追加：seq 推进，id 全部唯一
    st.append_message(Message.build("assistant", "回答", 99))
    st.append("user", "普通提问")
    ids = [x.id for x in st.messages]
    assert len(ids) == len(set(ids))
    assert ids[0] == expected_id


def test_system_after_non_system_raises():
    st = ThreadState("t1")
    st.append("user", "你好")
    with pytest.raises(IllegalMessageError):
        st.append("system", "你是助手")


def test_error_budget_zero_is_strict_fail_fast():
    st = ThreadState("t1", ContextPolicyConfig(error_budget=0))
    with pytest.raises(IllegalMessageError):
        st.append("bogus", "x")
    assert st.error_count == 1


def test_error_budget_exceeded_raises():
    # 批量导入：error_budget=2 容忍前 2 条坏消息，第 3 条抛 ErrorBudgetExceeded
    st = ThreadState("t1", ContextPolicyConfig(error_budget=2))
    with pytest.raises(ErrorBudgetExceeded):
        st.load_messages([{"role": "bogus", "content": "x"}] * 3)
    assert st.error_count == 3


def test_load_messages_tolerates_bad_ones_within_budget():
    st = ThreadState("t1", ContextPolicyConfig(error_budget=1))
    n = st.load_messages([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "好"},
        {"role": "bogus", "content": "坏消息"},
        {"role": "assistant", "content": "回复"},
    ])
    assert n == 3  # 坏消息被跳过，其余导入
    assert st.error_count == 1


def test_validate_role_sequence_rules():
    msgs = [
        Message.build("user", "问题", 0),
        Message.build("assistant", "回答", 1),
    ]
    ok, _ = validate_role_sequence(msgs)
    assert ok
    bad = [Message.build("assistant", "回答", 0)]
    ok, reason = validate_role_sequence(bad)
    assert not ok and "首条" in reason
    double_a = [
        Message.build("user", "问题", 0),
        Message.build("assistant", "回答1", 1),
        Message.build("assistant", "回答2", 2),
    ]
    ok, reason = validate_role_sequence(double_a)
    assert not ok and "相邻" in reason


# ---- 10) DuplicateBlockCompressor round-trip ------------------------------------


def _sample_messages():
    return [
        Message.build("system", SYSTEM, 0),
        Message.build("user", "问题A", 1),
        Message.build("assistant", "回答1", 2),
        Message.build("user", "问题A", 3),          # 完全重复块
        Message.build("assistant", "回答1", 4),     # 完全重复块
        Message.build("user", "问题B", 5),
    ]


def test_duplicate_compressor_roundtrip():
    orig = _sample_messages()
    compressed, dictionary = DuplicateBlockCompressor.compress(orig)
    assert len(dictionary) == 4  # system/问题A/回答1/问题B 四类唯一块
    ref_roles = [m.role for m in compressed if "\x00REF:" in m.content]
    assert ref_roles == ["user", "assistant"]  # 重复块被替换为引用
    restored = DuplicateBlockCompressor.restore(compressed, dictionary)
    assert [m.as_dict() for m in restored] == [m.as_dict() for m in orig]


def test_duplicate_compressor_deterministic():
    o1 = _sample_messages()
    o2 = _sample_messages()
    c1, d1 = DuplicateBlockCompressor.compress(o1)
    c2, d2 = DuplicateBlockCompressor.compress(o2)
    assert [m.content for m in c1] == [m.content for m in c2]
    assert d1 == d2


def test_duplicate_compressor_escapes_control_chars():
    orig = [Message.build("user", "含\x00控制符的内容", 0),
            Message.build("user", "含\x00控制符的内容", 1)]
    compressed, dictionary = DuplicateBlockCompressor.compress(orig)
    restored = DuplicateBlockCompressor.restore(compressed, dictionary)
    assert [m.content for m in restored] == [m.content for m in orig]


def test_duplicate_compressor_missing_dict_entry_raises():
    compressed, dictionary = DuplicateBlockCompressor.compress(_sample_messages())
    with pytest.raises(Exception):
        DuplicateBlockCompressor.restore(compressed, {})  # 字典缺失 → 显式异常


def test_duplicate_compressor_separate_from_summary():
    """reversible preprocessor 与 summary 严格分开：不产生 SummaryBlock、不注入消息。"""
    st = build_long_thread(rounds=6)
    before = [m.as_dict() for m in st.messages]
    # compress 是独立调用，不修改 ThreadState 的消息内容
    compressed, dictionary = DuplicateBlockCompressor.compress(st.messages)
    assert [m.as_dict() for m in st.messages] == before
    assert compressed != st.messages  # 压缩产物是新的消息列表（REF 替换）
    # 与 summary 分开：compress 不产生 SummaryBlock，ThreadState.apply_policy(full) 也不注入
    res = st.apply_policy()
    assert res.manifest.summary_blocks == []
    assert res.manifest.policy == "full"


# ---- 分类与统计补充 --------------------------------------------------------------


def test_classify_message_categories():
    assert classify_message("system", SYSTEM) == MessageCategory.SYSTEM
    assert classify_message("system", TOOL_SYSTEM) == MessageCategory.TOOL_SCHEMA
    assert classify_message("user", "你好") == MessageCategory.HISTORY
    assert classify_message("user", "<tool_response>\n{}\n</tool_response>") == MessageCategory.HISTORY
    assert classify_message("assistant", "ACTION: search_orders(x)") == MessageCategory.HISTORY


def test_message_id_and_hash_stable():
    m1 = Message.build("user", "同样内容", 3)
    m2 = Message.build("user", "同样内容", 3)
    assert m1.id == m2.id
    assert m1.hash == m2.hash == stable_hash("同样内容")
    m3 = Message.build("user", "同样内容", 4)
    assert m1.id != m3.id  # 不同序号 → 不同 id（同内容 hash 相同）


def test_stats_fields_complete():
    # 长历史 + 少数事实：extractive_summary 有净收益（大量无关轮次被丢弃，事实投影进块）
    st = ThreadState("t1", ContextPolicyConfig(policy="extractive_summary",
                                               keep_recent_rounds=2, max_facts=16))
    st.append("system", SYSTEM)
    for i in range(1, 51):
        if i in (3, 9):
            q = f"请记住这个秘密数字：{900 + i}。只回答两个字：记住了。"
        else:
            q = POOL[i % len(POOL)]
        st.append("user", q)
        st.append("assistant", f"回答{i}")
    res = st.apply_policy()
    s = res.manifest.stats
    assert s.input_messages == 101  # system + 50*(user+assistant)
    assert s.input_chars > s.output_chars
    assert s.input_tokens_est > 0 and s.output_tokens_est > 0
    assert s.protected_preserved is True
    assert s.summary_facts >= 1
    assert s.output_messages == len(res.messages)
    assert 0.0 < s.compression_ratio < 1.0
    # needle（两条 secret 事实）都保留在输出中
    assert "903" in all_contents(res.messages)
    assert "909" in all_contents(res.messages)


def test_summary_expansion_reported_honestly():
    # 短历史：summary 块开销 > 被裁内容 → 压缩率如实为负 + 明确 note（不美化）
    st = build_long_thread(rounds=4, secret_round=2, policy="extractive_summary", keep=1)
    res = st.apply_policy()
    assert res.manifest.stats.compression_ratio < 0
    assert any("膨胀" in n and "压缩率如实为负" in n for n in res.manifest.notes)


def test_protected_byte_spans_auditable():
    st = build_long_thread(rounds=6, policy="trim", keep=2)
    res = st.apply_policy()
    spans = res.manifest.protected_byte_spans
    assert spans, "protected 内容必须有 byte span 记录"
    for span in spans:
        assert span.label in ("system", "tool_schema", "fact", "summary")
        assert span.start == 0 and span.end > 0  # 整条消息保护（UTF-8 字节）
    # 每个 span 对应的消息确实在输出中
    ids = {m.id for m in res.messages}
    assert all(s.message_id in ids for s in spans)


def test_manifest_to_dict_serializable():
    st = build_long_thread(rounds=6, secret_round=2, policy="extractive_summary", keep=2)
    res = st.apply_policy()
    d = res.manifest.to_dict()
    assert d["policy"] == "extractive_summary"
    assert d["version"]
    assert d["input_hash"] and d["output_hash"]
    assert d["kept_ids"] and d["trimmed_ids"]
    assert d["protected_byte_spans"]
    assert d["summary_blocks"]
    assert d["lossless"] is False
    assert "stats" in d


def test_manifest_to_dict_never_leaks_fact_plaintext():
    """to_dict 不输出 fact 明文或任何原始消息内容：fake secret 不得出现在 manifest JSON。"""
    st = build_long_thread(rounds=8, secret_round=1, policy="extractive_summary", keep=2)
    st.register_fact("秘密内容-外部注册-不要外泄", source_message_id=st.messages[1].id)
    res = st.apply_policy()
    d = res.manifest.to_dict()
    blob = json.dumps(d, ensure_ascii=False)
    # fake secret 明文 / 注册事实明文 / 原始消息特征均不得出现
    assert "9527" not in blob
    assert "请记住这个秘密数字" not in blob
    assert "秘密内容-外部注册-不要外泄" not in blob
    assert "只回答两个字" not in blob
    # facts 仅以 id/hash/kind/source_ids 呈现（可审计、不可还原原文）
    for fb in d["summary_blocks"][0]["facts"]:
        assert set(fb) == {"id", "hash", "kind", "source_ids"}
        assert "text" not in fb
    # kept/trimmed/protected 均为消息 id，不含 content
    for ids_field in ("kept_ids", "trimmed_ids", "protected_ids"):
        assert all(isinstance(i, str) and not i.endswith("-content") for i in d[ids_field])


def test_manifest_serialization_exit_only_to_dict():
    """manifest 的唯一序列化出口是 to_dict()：slots 阻止 vars()/__dict__ 直接 dump，
    repr 隐藏明文，直接 json.dumps 失败——防止公开对象直接 dump 泄漏原文。"""
    st = build_long_thread(rounds=8, secret_round=1, policy="extractive_summary", keep=2)
    st.register_fact("秘密内容-外部注册-不要外泄", source_message_id=st.messages[1].id)
    res = st.apply_policy()
    m = res.manifest
    # 内存态字段含明文（供审计）——这是受控访问，不属于序列化契约
    assert any("9527" in f.text for b in m.summary_blocks for f in b.facts)
    # repr 只显示概要：不含 fact 明文 / rendered 全文 / 原始消息特征
    r = repr(m)
    assert "9527" not in r
    assert "秘密内容-外部注册-不要外泄" not in r
    assert "请记住这个秘密数字" not in r
    assert "summary_blocks=1" in r  # 只显示数量
    # SummaryBlock 的 repr 同样隐藏明文（调试日志路径不泄露）
    br = repr(m.summary_blocks[0])
    assert "9527" not in br and "秘密内容" not in br
    assert "facts=" in br and "rendered_chars=" in br
    # slots 阻止 vars() / __dict__ 直接 dump；json.dumps 直接失败（无 __dict__ 契约）
    with pytest.raises(TypeError):
        vars(m)
    with pytest.raises(AttributeError):
        m.__dict__
    with pytest.raises(TypeError):
        json.dumps(m)
    # 受支持出口：to_dict()（无明文）
    d = m.to_dict()
    assert "9527" not in json.dumps(d, ensure_ascii=False)
