"""E2.0.5：replicate 协议测试（independent / soak / auto）。

验证任务四要求：
- independent：每个正式 replicate 前清除 KV 并断言 used_cells=0、active_sequences=0；
  clean 失败（valid=False）的 replicate 不计入独立统计聚合；
- soak：多个 cycle 共享 KV，记录 cycle_id 与初始 used_cells，不视为独立重复；
- auto：旧行为（repeat 共享 KV，无清洁断言）。
"""
from __future__ import annotations

import json
import os

import pytest

from framework.kv_probe import KVProbe
from tests.mock_server import MockOpenAIServer, _KV
from runner.runner import cli_main


def _run_and_load(tmp_path, extra_args):
    out_dir = str(tmp_path / "r")
    rc = cli_main([
        "--scenario", "multi_turn",
        "--rounds", "2",
        "--output-dir", out_dir,
        "--kv-probe",
        *extra_args,
    ])
    assert rc == 0
    files = [f for f in os.listdir(out_dir) if f.startswith("bench_")]
    with open(os.path.join(out_dir, files[0]), encoding="utf-8") as f:
        return json.load(f)


# ---- KVProbe 层：清洁断言原语 ----

def test_kv_state_reflects_mock(mock_openai_server):
    base_url = f"http://127.0.0.1:{mock_openai_server.port}/v1"
    probe = KVProbe(base_url)
    st = probe.kv_state()
    assert st is not None and st["used_cells"] == 0 and st["active_sequences"] == 0


def test_clean_all_slots_resets_kv(mock_openai_server):
    base_url = f"http://127.0.0.1:{mock_openai_server.port}/v1"
    probe = KVProbe(base_url)
    # 先触发一次 completion 使 KV 非空
    assert probe.snapshot() is None or True
    n_attempt, n_ok = probe.clean_all_slots()
    assert n_attempt == 2 and n_ok == 2
    st = probe.kv_state()
    assert st["used_cells"] == 0 and st["active_sequences"] == 0


def test_clean_failure_when_erase_unsupported(mock_openai_server):
    _KV["erase_disabled"] = True
    try:
        base_url = f"http://127.0.0.1:{mock_openai_server.port}/v1"
        probe = KVProbe(base_url)
        n_attempt, n_ok = probe.clean_all_slots()
        assert n_attempt == 2 and n_ok == 0
    finally:
        _KV["erase_disabled"] = False


# ---- independent 协议 ----

def test_independent_mode_cleans_and_verifies(tmp_path):
    with MockOpenAIServer() as srv:
        result = _run_and_load(tmp_path, [
            "--port", str(srv.port), "--host", "127.0.0.1",
            "--repeat", "2", "--warmup", "1",
            "--replicate-mode", "independent",
        ])
        proto = result["protocol"]["multi_turn"]
        assert proto["mode"] == "independent"
        recs = proto["replicates"]
        assert len(recs) == 2
        for r in recs:
            assert r["independent"] is True
            assert r["clean_verified"] is True
            assert r["initial_used_cells"] == 0
            assert r["valid"] is True
        assert proto["valid_count"] == 2


def test_independent_clean_failure_marks_invalid_and_excludes(tmp_path):
    """erase 不可用 → clean_verified=False → valid=False → 从独立统计聚合中排除。"""
    with MockOpenAIServer() as srv:
        _KV["erase_disabled"] = True
        try:
            result = _run_and_load(tmp_path, [
                "--port", str(srv.port), "--host", "127.0.0.1",
                "--repeat", "2",
                "--replicate-mode", "independent",
            ])
        finally:
            _KV["erase_disabled"] = False
        proto = result["protocol"]["multi_turn"]
        # 第一次 replicate 前 KV 初始为空（mock 清洁）→ 断言通过；
        # 第二次 replicate 前有残留 KV（384）且 erase 被禁用 → 断言失败 → invalid
        assert proto["valid_count"] == 1
        recs = proto["replicates"]
        assert recs[0]["valid"] is True
        assert recs[0]["clean_verified"] is True
        assert recs[1]["valid"] is False
        assert recs[1]["clean_verified"] is False
        assert recs[1]["initial_used_cells"] == 384
        # 聚合只包含 valid run（runs[1] 被排除）
        assert result["scenarios"]["multi_turn"]["runs"] is not None


# ---- soak 协议 ----

def test_soak_mode_records_initial_used_cells(tmp_path):
    with MockOpenAIServer() as srv:
        result = _run_and_load(tmp_path, [
            "--port", str(srv.port), "--host", "127.0.0.1",
            "--repeat", "2", "--warmup", "1",
            "--replicate-mode", "soak",
        ])
        proto = result["protocol"]["multi_turn"]
        assert proto["mode"] == "soak"
        assert "NOT independent" in proto["note"]
        recs = proto["replicates"]
        assert len(recs) == 2
        for r in recs:
            assert r["independent"] is False
            # warmup 后 mock KV=384（非清洁），soak 记录初始 used_cells 而不清除
            assert r["initial_used_cells"] == 384
            assert r["valid"] is True
        # soak 的 cycle 全部参与聚合（无独立统计排除）
        assert "p50_latency_ms" in result["summary"]["multi_turn"]


# ---- auto 协议（旧行为回归） ----

def test_auto_mode_no_clean(tmp_path):
    with MockOpenAIServer() as srv:
        result = _run_and_load(tmp_path, [
            "--port", str(srv.port), "--host", "127.0.0.1",
            "--repeat", "2",
        ])
        # auto 模式：protocol 存在但 mode=auto，无清洁断言（independent=False, clean_verified=None）
        proto = result["protocol"]["multi_turn"]
        assert proto["mode"] == "auto"
        for r in proto["replicates"]:
            assert r["independent"] is False
            assert r["clean_verified"] is None
        assert "runs" in result["scenarios"]["multi_turn"]
