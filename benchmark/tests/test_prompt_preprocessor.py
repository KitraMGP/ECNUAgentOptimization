"""test_prompt_preprocessor —— E15.3 B1：deterministic structured-lossless preprocessor 测试。

覆盖（对照 E15.3 需求 4，无 GPU / 无真实 server 依赖）：
- identity / off（默认）：normalize 校验、Driver off 无 preprocessor 字段、workload
  默认路径行结构零改动、Runner 默认 off；
- 确定性：同输入 → 逐字节相同输出 + 相同 manifest（含 hash）；
- round-trip：compress → restore 逐消息/逐字节还原原输入；
- 确有重复时字符与估算 token 减少（真实 BPE token 减少由 4B greedy paired 门禁记录）；
- 无重复 / 无净收益（短重复块）→ identity（不膨胀）；
- protected byte compare：system（禁止指令/tool schema）/ 关键事实（秘密数字）/
  tool payload 引用协议 / 注册 fact 文本永不替换且逐字节保留；
- fail-fast：NUL、伪 ref、畸形 manifest、未知 ref、hash mismatch、跨 thread/session；
- Driver 400 重试不重复压缩 / 不污染调用者消息（幂等）；
- 审计不含原文（manifest.to_dict() 零明文）。
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from framework.prompt_preprocessor import (
    Preprocessor,
    PreprocessorCrossThreadError,
    PreprocessorDictionary,
    PreprocessorError,
    PreprocessorHashMismatchError,
    PreprocessorManifestError,
    PreprocessorNulError,
    PreprocessorUnknownRefError,
    block_id_for,
    is_protected_message,
    normalize_preprocessor_mode,
    ref_content_for,
    restore,
    structured_lossless_compress,
)
from tests.conftest import FakeDriver, default_row

# 长重复内容（≥49 字符，估算 token > 16 门槛，保证替换有收益）
LONG_A = "请解释一下什么是 KV Cache，以及它在长上下文推理中的内存累积问题和它与传统缓存机制的区别。" * 2
LONG_B = "写一句押韵的中文诗，主题是长沙的春天和橘子洲的夜景，以及湘江两岸的灯火与明月。" * 2
TOOL_SYSTEM = ("你是智能客服助手，可调用以下工具：\n"
               "- search_orders(customer_id)：查询客户订单列表（返回订单ID列表）\n"
               "处理流程（必须严格遵守）：\n"
               "1. 先调用 search_orders(customer_id) 获取订单列表；\n"
               "需要调用工具时，只输出一行：ACTION: 工具名(参数)。\n"
               "调用格式示例（参数必须填真实值）：\n"
               "- ACTION: search_orders(customer_id=C10086)")
SECRET_MSG = "请记住这个秘密数字：9527。只回答两个字：记住了。"


def dup_messages():
    """含重复长块的标准输入（system protected + 2 对重复历史块）。"""
    return [
        {"role": "system", "content": TOOL_SYSTEM},
        {"role": "user", "content": LONG_A},
        {"role": "assistant", "content": "回答 A 内容。"},
        {"role": "user", "content": LONG_A},            # 重复 user
        {"role": "user", "content": LONG_B},
        {"role": "assistant", "content": "回答 A 内容。"},  # 重复 assistant
        {"role": "user", "content": LONG_B},            # 重复 user
    ]


# ---- identity / off ------------------------------------------------------------


def test_normalize_mode_default_off_and_alias():
    assert normalize_preprocessor_mode(None) == "off"
    assert normalize_preprocessor_mode("off") == "off"
    assert normalize_preprocessor_mode("structured_lossless") == "structured_lossless"
    assert normalize_preprocessor_mode("structured-lossless") == "structured_lossless"  # 兼容 CLI 名
    with pytest.raises(ValueError):
        normalize_preprocessor_mode("bogus")
    with pytest.raises(ValueError):
        normalize_preprocessor_mode(123)


def test_no_duplicates_returns_identity():
    msgs = [
        {"role": "system", "content": TOOL_SYSTEM},
        {"role": "user", "content": LONG_A},
        {"role": "assistant", "content": "回答 B 内容。"},
    ]
    r = structured_lossless_compress(msgs)
    assert r.manifest.compressed is False
    assert r.messages == [dict(m) for m in msgs]          # 逐条逐字节相同
    assert r.manifest.input_hash == r.manifest.output_hash
    assert len(r.dictionary) == 0
    assert r.manifest.stats.compression_ratio == 0.0
    assert any("identity" in n for n in r.manifest.notes)


def test_short_duplicates_no_gain_identity():
    """重复但内容太短（≤ REF 23 字节）：替换反而膨胀 → 保留原文（identity）。"""
    msgs = [
        {"role": "user", "content": "好"},
        {"role": "user", "content": "好"},
        {"role": "user", "content": "好的"},
        {"role": "user", "content": "好的"},
    ]
    r = structured_lossless_compress(msgs)
    assert r.manifest.compressed is False
    assert [m["content"] for m in r.messages] == ["好", "好", "好的", "好的"]
    assert r.manifest.input_hash == r.manifest.output_hash
    assert any("净收益" in n for n in r.manifest.notes)


def test_chinese_short_block_no_gain_identity():
    """中文短块（字节多但 token 少，E12 教训）：REF 标记在 Qwen tokenizer 下约
    19 token，13 个中文字符仅约 13 token——替换会 token 膨胀 → 保留原文。"""
    q = "解释一下什么是 KV Cache。"
    assert len(q.encode("utf-8")) > 23                       # 字节口径满足
    msgs = [
        {"role": "user", "content": q},
        {"role": "user", "content": q},
    ]
    r = structured_lossless_compress(msgs)
    assert r.manifest.compressed is False                    # token 口径不满足 → identity
    assert [m["content"] for m in r.messages] == [q, q]
    assert any("净收益" in n for n in r.manifest.notes)


def test_off_driver_keeps_legacy_fields():
    """Driver preprocessor=None（默认 off）：发送原始消息、行内无 preprocessor 字段。"""
    fake_client = MagicMock()
    resp = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        choices=[SimpleNamespace(message=SimpleNamespace(content="答案"))],
    )
    fake_client.chat.completions.create.return_value = resp
    with patch("framework.driver.OpenAI", return_value=fake_client), \
         patch("framework.sampler.find_server_pid", return_value=None):
        from framework.driver import Driver
        drv = Driver(base_url="http://127.0.0.1:8080/v1")   # 未传 preprocessor
        msgs = [{"role": "user", "content": LONG_A}, {"role": "user", "content": LONG_A}]
        row = drv.chat(msgs)
    assert "preprocessor" not in row                        # off 无新字段
    called = fake_client.chat.completions.create.call_args.kwargs["messages"]
    assert called == msgs                                   # 发送原始消息


# ---- 确定性 ----------------------------------------------------------------------


def test_determinism_same_input_same_output_and_manifest():
    msgs = dup_messages()
    r1 = structured_lossless_compress(msgs)
    r2 = structured_lossless_compress(msgs)
    assert r1.messages == r2.messages
    assert r1.manifest.to_dict() == r2.manifest.to_dict()
    assert r1.dictionary.block_ids() == r2.dictionary.block_ids()
    # 跨线程压缩同样确定性（与压缩线程无关）
    with ThreadPoolExecutor(max_workers=2) as ex:
        outs = list(ex.map(lambda _: structured_lossless_compress(msgs).messages, range(2)))
    assert outs[0] == outs[1] == r1.messages


def test_manifest_slots_no_dict():
    r = structured_lossless_compress(dup_messages())
    with pytest.raises(TypeError):
        vars(r.manifest)                                   # slots dataclass 无 __dict__


# ---- round-trip ------------------------------------------------------------------


def test_round_trip_restores_exactly():
    msgs = dup_messages()
    orig = [dict(m) for m in msgs]
    r = structured_lossless_compress(msgs)
    assert r.manifest.compressed is True
    restored = restore(r.messages, r.manifest, r.dictionary)
    assert restored == orig                                # 逐消息/逐字节还原


def test_round_trip_preserves_extra_fields():
    """带额外字段（name/tool_call_id 等）的消息：压缩/还原均保留全部字段
    （逐消息/逐字段 round-trip，reviewer 修复）。"""
    msgs = [
        {"role": "system", "content": TOOL_SYSTEM},
        {"role": "user", "content": LONG_A, "name": "user_a"},
        {"role": "assistant", "content": "回答 A 内容。", "name": "asst_a"},
        {"role": "user", "content": LONG_A, "name": "user_a"},   # 重复（含额外字段）
    ]
    r = structured_lossless_compress(msgs)
    # 被替换消息保留额外字段
    replaced = [m for m in r.messages if "\x00REF:" in m["content"]]
    assert len(replaced) == 1
    assert replaced[0]["name"] == "user_a"
    restored = restore(r.messages, r.manifest, r.dictionary)
    assert restored == msgs                                 # 逐消息/逐字段还原


def test_restore_missing_key_fail_fast():
    """压缩输入缺 role/content 键 → PreprocessorError（而非 KeyError，reviewer 修复）。"""
    r = structured_lossless_compress(dup_messages())
    bad = [dict(m) for m in r.messages]
    del bad[1]["content"]
    with pytest.raises(PreprocessorError):
        restore(bad, r.manifest, r.dictionary)
    bad2 = [dict(m) for m in r.messages]
    del bad2[1]["role"]
    with pytest.raises(PreprocessorError):
        restore(bad2, r.manifest, r.dictionary)
    bad3 = [dict(m) for m in r.messages]
    bad3[1] = "不是 dict"
    with pytest.raises(PreprocessorError):
        restore(bad3, r.manifest, r.dictionary)


def test_round_trip_identity_case():
    msgs = [{"role": "system", "content": TOOL_SYSTEM}, {"role": "user", "content": LONG_A}]
    r = structured_lossless_compress(msgs)
    restored = restore(r.messages, r.manifest, r.dictionary)
    assert restored == msgs


# ---- 收益：确有重复时字符与 token 减少 ----------------------------------------------


def test_duplicates_reduce_chars_and_est_tokens():
    r = structured_lossless_compress(dup_messages())
    assert r.manifest.compressed is True
    assert r.manifest.stats.output_bytes < r.manifest.stats.input_bytes
    assert r.manifest.stats.output_chars < r.manifest.stats.input_chars
    assert r.manifest.stats.output_tokens_est < r.manifest.stats.input_tokens_est
    assert r.manifest.stats.replaced_messages == 2    # LONG_A@3、LONG_B@6（"回答 A 内容。" 8 字符 < 23 无收益保留）
    assert r.manifest.stats.compression_ratio > 0.0
    # 压缩输出里出现 REF 标记且不再出现被压缩的原文
    outs = [m["content"] for m in r.messages]
    assert any("\x00REF:b" in c for c in outs)
    assert LONG_A not in outs[3]                           # 第二条 LONG_A 被替换


# ---- protected byte compare -------------------------------------------------------


def test_system_message_never_replaced_even_if_duplicated():
    """禁止指令 / tool schema 本体（system）逐字节保留，重复也不替换。"""
    msgs = [
        {"role": "system", "content": TOOL_SYSTEM},
        {"role": "user", "content": "问题一"},
        {"role": "system", "content": TOOL_SYSTEM},        # 重复 system
        {"role": "user", "content": "问题二"},
    ]
    r = structured_lossless_compress(msgs)
    assert [m["content"] for m in r.messages if m["role"] == "system"] == \
        [TOOL_SYSTEM, TOOL_SYSTEM]                         # 两条都逐字节保留
    labels = [s.label for s in r.manifest.protected_spans if s.label == "system"]
    assert len(labels) == 2
    # system 全文不进入 manifest
    dump = json.dumps(r.manifest.to_dict(), ensure_ascii=False)
    assert TOOL_SYSTEM not in dump


def test_secret_declaration_never_replaced():
    """关键事实（秘密数字声明）逐字节保留，重复也不替换。"""
    m1 = {"role": "user", "content": SECRET_MSG}
    m2 = {"role": "user", "content": SECRET_MSG}
    msgs = [m1, m2]
    r = structured_lossless_compress(msgs)
    assert [m["content"] for m in r.messages] == [SECRET_MSG, SECRET_MSG]
    assert all(s.label == "fact" for s in r.manifest.protected_spans)


def test_tool_payload_protocol_never_replaced():
    """tool payload 引用协议消息逐字节保留（E15.2 H3 协议不得替换）。"""
    pp = ("<tool_response>\n[tool_payload externalized]\n"
          "payload_ref=abc123\nprojection: {...}\n</tool_response>")
    msgs = [
        {"role": "user", "content": pp},
        {"role": "user", "content": pp},
    ]
    r = structured_lossless_compress(msgs)
    assert [m["content"] for m in r.messages] == [pp, pp]
    assert all(s.label == "tool_payload_protocol" for s in r.manifest.protected_spans)


def test_registered_fact_text_protected():
    """显式注册的关键事实文本：含该文本的消息逐字节保留（重复也不替换）。"""
    fact = "常住城市长沙"
    fact_msg = f"用户登记信息：{fact}，工作单位位于岳麓区，通勤时间约四十分钟，每日通勤路线经过湘江大桥与橘子洲大桥。"
    msgs = [
        {"role": "user", "content": fact_msg},
        {"role": "user", "content": fact_msg},
    ]
    r = structured_lossless_compress(msgs, fact_texts=[fact])
    assert [m["content"] for m in r.messages] == [fact_msg, fact_msg]
    assert all(s.label == "fact" for s in r.manifest.protected_spans)
    # 未注册该 fact 时同内容可压缩（对照：protected 判定是注册驱动）
    r2 = structured_lossless_compress(msgs)
    assert r2.manifest.compressed is True


def test_is_protected_message_pure():
    assert is_protected_message("system", "任意内容") == (True, "system")
    assert is_protected_message("user", SECRET_MSG)[0] is True
    assert is_protected_message("user", "请记住这个数字 42")[0] is True
    assert is_protected_message("user", "payload_ref=abc") == (True, "tool_payload_protocol")
    assert is_protected_message("user", "ACTION: resolve_tool_payload(payload_ref=x)")[0] is True
    assert is_protected_message("user", LONG_A, fact_texts=["KV Cache"])[0] is True
    assert is_protected_message("user", "普通历史消息内容") == (False, None)


# ---- fail-fast -------------------------------------------------------------------


def test_nul_in_input_fail_fast():
    msgs = [{"role": "user", "content": "含 NUL \x00 的内容"}]
    with pytest.raises(PreprocessorNulError):
        structured_lossless_compress(msgs)


def test_fake_ref_handled_idempotently_fails_at_restore():
    """伪 ref（非本压缩器产出的 REF 格式输入）：compress 幂等保留（不报 NUL，
    因为自有 REF 产物也要容忍），restore 的 ref/hash 校验处 fail-fast。"""
    fake = ref_content_for("b" + "0" * 16)
    msgs = [{"role": "user", "content": fake}]
    r = structured_lossless_compress(msgs)
    assert [m["content"] for m in r.messages] == [fake]   # 幂等保留
    assert r.manifest.compressed is False
    with pytest.raises(PreprocessorUnknownRefError):
        restore(r.messages, r.manifest, r.dictionary)      # 伪 ref 无字典 → fail-fast
    # 非 REF 格式的 NUL 仍 fail-fast（原始内容含 NUL 是非法输入）
    with pytest.raises(PreprocessorNulError):
        structured_lossless_compress([{"role": "user", "content": "含 NUL \x00 的内容"}])


def test_unknown_role_fail_fast():
    msgs = [{"role": "narrator", "content": "内容"}]
    with pytest.raises(Exception):
        structured_lossless_compress(msgs)


def test_malformed_manifest_fail_fast():
    r = structured_lossless_compress(dup_messages())
    # 非 PreprocessorManifest 对象
    with pytest.raises(PreprocessorManifestError):
        restore(r.messages, {"not": "manifest"}, r.dictionary)
    # 模式错误
    bad = r.manifest
    from dataclasses import replace
    bad_mode = replace(bad, mode="summary")
    with pytest.raises(PreprocessorManifestError):
        restore(r.messages, bad_mode, r.dictionary)
    # 版本主版本不兼容
    bad_ver = replace(bad, version="99.0.0")
    with pytest.raises(PreprocessorManifestError):
        restore(r.messages, bad_ver, r.dictionary)


def test_unknown_ref_fail_fast():
    """restore 用空/错配 dictionary：REF 的 block_id 缺失 → UnknownRef（跨 session 错用）。"""
    r = structured_lossless_compress(dup_messages())
    empty_dict = PreprocessorDictionary()
    with pytest.raises(PreprocessorUnknownRefError):
        restore(r.messages, r.manifest, empty_dict)
    # 用另一输入的 dictionary（内容不同）→ 同样未知 ref fail-fast
    other = structured_lossless_compress(
        [{"role": "user", "content": LONG_B}, {"role": "user", "content": LONG_B}])
    with pytest.raises(PreprocessorUnknownRefError):
        restore(r.messages, r.manifest, other.dictionary)


def test_hash_mismatch_on_tampered_compressed_fail_fast():
    r = structured_lossless_compress(dup_messages())
    tampered = [dict(m) for m in r.messages]
    tampered[0]["content"] = "被篡改的内容"
    with pytest.raises(PreprocessorHashMismatchError):
        restore(tampered, r.manifest, r.dictionary)


def test_hash_mismatch_on_tampered_dictionary_fail_fast():
    """dictionary 内容被篡改（ref 与内容 hash 不符）→ restore 的 block_id 自验证 fail-fast。"""
    r = structured_lossless_compress(dup_messages())
    # 防御纵深：直接注入错误内容（正常 API 的 add() 会自验证拦截，这里绕过以触发
    # restore 的第二道校验；若 remove 该防线则此测试失败）
    ref = r.dictionary.block_ids()[0]
    r.dictionary._blocks[ref] = "与 ref hash 不符的伪造原文"
    with pytest.raises(PreprocessorHashMismatchError):
        restore(r.messages, r.manifest, r.dictionary)


def test_dictionary_add_self_validates():
    d = PreprocessorDictionary()
    good = "内容" * 10
    d.add(block_id_for(good), good)
    assert len(d) == 1
    with pytest.raises(PreprocessorHashMismatchError):
        d.add("b" + "0" * 16, "内容与 ref 不符")


def test_cross_thread_restore_fail_fast():
    r = structured_lossless_compress(dup_messages())

    def _restore():
        return restore(r.messages, r.manifest, r.dictionary)

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_restore)
        with pytest.raises(PreprocessorCrossThreadError):
            fut.result()
    # 同一线程 restore 正常
    assert restore(r.messages, r.manifest, r.dictionary) == dup_messages()


def test_dictionary_thread_bound_properties():
    d = PreprocessorDictionary()
    with pytest.raises(PreprocessorCrossThreadError):
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(lambda: d.session_id).result()
    assert d.owner_ident == threading.get_ident()


# ---- Driver 集成：off 默认 / on 不污染 / 400 重试幂等 ----------------------------------


def _make_driver(fake_client, preprocessor=None):
    with patch("framework.driver.OpenAI", return_value=fake_client), \
         patch("framework.sampler.find_server_pid", return_value=None):
        from framework.driver import Driver
        return Driver(base_url="http://127.0.0.1:8080/v1", preprocessor=preprocessor)


def _fake_resp(text="答案"):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
    )


def test_driver_on_compresses_before_send_and_does_not_pollute_caller():
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_resp()
    drv = _make_driver(fake_client, preprocessor=Preprocessor())
    msgs = dup_messages()
    orig = [dict(m) for m in msgs]
    row = drv.chat(msgs)
    assert msgs == orig                                       # 调用者消息零污染
    called = fake_client.chat.completions.create.call_args.kwargs["messages"]
    assert called != msgs                                     # 发送的是压缩后新列表
    outs = [m["content"] for m in called]
    assert any("\x00REF:b" in c for c in outs)                # 压缩形态
    assert row["preprocessor"]["compressed"] is True          # 审计统计进入结果行
    assert row["preprocessor"]["mode"] == "structured_lossless"
    # 幂等：对发送消息再压缩无变化
    r = structured_lossless_compress(called)
    assert r.messages == called
    assert r.manifest.compressed is False


def test_driver_400_retry_compresses_idempotently_without_pollution():
    """400 超出 ctx 重试：基于压缩后消息裁剪后递归，幂等且不污染调用者。"""
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        Exception("exceed_context_size_error ..."),           # 第一次触发 400 兜底
        _fake_resp(text="重试成功"),
    ]
    drv = _make_driver(fake_client, preprocessor=Preprocessor())
    msgs = [
        {"role": "system", "content": TOOL_SYSTEM},
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": LONG_A},
        {"role": "user", "content": LONG_A},                   # 重复（可压缩）
    ]
    orig = [dict(m) for m in msgs]
    row = drv.chat(msgs)
    assert msgs == orig                                        # 调用者消息零污染
    calls = fake_client.chat.completions.create.call_args_list
    assert len(calls) == 2
    first_msgs = calls[0].kwargs["messages"]
    second_msgs = calls[1].kwargs["messages"]
    # 第一次发送：压缩形态（含 REF）
    assert any("\x00REF:b" in m["content"] for m in first_msgs)
    # 400 兜底基于**原始消息**裁剪后递归 → 递归重新压缩，结果与直接压缩 candidate 一致（确定性幂等）
    candidate = [m for m in orig if m["role"] == "system"] + \
        [m for m in orig if m["role"] != "system"][1:]
    r = structured_lossless_compress(candidate)
    assert second_msgs == r.messages
    # 重试消息数减少（丢弃了最早消息）且 system 仍在最前
    assert len(second_msgs) < len(first_msgs)
    assert second_msgs[0]["role"] == "system"
    # 重试行审计存在且反映重新压缩（candidate 仍含重复 LONG_A）
    assert "preprocessor" in row
    assert row["preprocessor"]["compressed"] is True
    assert row["text"] == "重试成功"


def test_driver_preprocessor_fail_fast_propagates():
    """preprocessor 压缩抛错（NUL 输入）必须从 chat() 传播（fail-fast，不吞异常）。"""
    fake_client = MagicMock()
    drv = _make_driver(fake_client, preprocessor=Preprocessor())
    with pytest.raises(PreprocessorNulError):
        drv.chat([{"role": "user", "content": "含 NUL \x00"}])


# ---- 审计不含原文 ------------------------------------------------------------------


def test_manifest_contains_no_original_text():
    msgs = dup_messages()
    r = structured_lossless_compress(msgs)
    dump = json.dumps(r.manifest.to_dict(), ensure_ascii=False)
    for m in msgs:
        assert m["content"] not in dump                      # 全部原文均不在 manifest
    # 被压缩内容（LONG_A/LONG_B）不在 manifest
    assert LONG_A not in dump
    assert LONG_B not in dump
    assert TOOL_SYSTEM not in dump


# ---- 默认路径兼容（workload / Runner）----------------------------------------------


def test_workload_default_path_no_preprocessor_field():
    import workload  # noqa: F401
    from framework.workload import get_workload
    drv = FakeDriver(responses=[default_row(text="回答")] * 3)
    wl = get_workload("multi_turn")
    result = wl.run(drv, wl.generate({"rounds": 3}))
    assert all("preprocessor" not in r for r in result["rows"])   # 默认 off 无新字段


def test_runner_default_off_and_optin():
    from framework.config import BenchmarkConfig
    from runner.runner import Runner
    with patch("framework.driver.OpenAI", return_value=MagicMock()), \
         patch("framework.sampler.find_server_pid", return_value=None):
        cfg_off = BenchmarkConfig.from_dict({"scenario": "multi_turn"})
        assert Runner(cfg_off).driver.preprocessor is None           # 默认 off
        cfg_on = BenchmarkConfig.from_dict(
            {"scenario": "multi_turn", "preprocessor": "structured_lossless"})
        assert Runner(cfg_on).driver.preprocessor is not None        # opt-in on
    with patch("framework.driver.OpenAI", return_value=MagicMock()), \
         patch("framework.sampler.find_server_pid", return_value=None):
        with pytest.raises(ValueError):
            Runner(BenchmarkConfig.from_dict(
                {"scenario": "multi_turn", "preprocessor": "bogus"}))  # 非法值 fail-fast


def test_config_extra_passthrough_from_dict():
    from framework.config import BenchmarkConfig
    cfg = BenchmarkConfig.from_dict({"scenario": "multi_turn", "preprocessor": "structured_lossless"})
    assert cfg.extra["preprocessor"] == "structured_lossless"
    cfg2 = BenchmarkConfig.from_dict({"scenario": "multi_turn"})
    assert "preprocessor" not in cfg2.extra
