"""KV observations 的 repeat/warmup 语义测试（E1 收尾）。

验证任务四要求：
- warmup 结果不进入 kv_observations；
- --repeat N 时每次正式 run 都有独立 run_id，samples 不覆盖；
- 顶层同时保存 raw samples（带 run_id）与 run-level aggregate（runs）；
- start/end 为全局快照（run_id=None）。
"""
from __future__ import annotations

import json
import os

import pytest

from tests.mock_server import MockOpenAIServer
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


def test_repeat_runs_have_distinct_run_id(tmp_path):
    """--repeat 2 时两个正式 run 的 samples 带不同 run_id，且均保留（不覆盖）。"""
    with MockOpenAIServer() as srv:
        result = _run_and_load(tmp_path, [
            "--port", str(srv.port), "--host", "127.0.0.1",
            "--repeat", "2",
        ])
    kv = result["kv_observations"]
    run_ids = sorted({s["run_id"] for s in kv["samples"] if s.get("run_id")})
    assert run_ids == ["multi_turn_0", "multi_turn_1"], f"run_ids={run_ids}"
    # 每个 run 有独立的请求前后样本（2 轮 × 2 = 4 条/run）
    n0 = sum(1 for s in kv["samples"] if s.get("run_id") == "multi_turn_0")
    n1 = sum(1 for s in kv["samples"] if s.get("run_id") == "multi_turn_1")
    assert n0 == 4 and n1 == 4
    # run-level aggregate 存在且两 run 独立
    assert set(kv["runs"]) == {"multi_turn_0", "multi_turn_1"}
    for r in kv["runs"].values():
        assert r["samples"] == 4
        assert r["first_used_cells"] is not None
        assert r["last_used_cells"] is not None


def test_warmup_not_in_observations(tmp_path):
    """--warmup 1 时 warmup 请求不产生 KV 样本（正式 run 才采集）。"""
    with MockOpenAIServer() as srv:
        result = _run_and_load(tmp_path, [
            "--port", str(srv.port), "--host", "127.0.0.1",
            "--warmup", "1", "--repeat", "1",
        ])
    kv = result["kv_observations"]
    # start + 2 轮请求前后(4) + end = 6；warmup 的请求（2 轮）不采集
    assert len(kv["samples"]) == 6, f"samples={len(kv['samples'])}"
    run_ids = {s.get("run_id") for s in kv["samples"]}
    assert run_ids == {"multi_turn_0", None}  # None = start/end 全局快照
    # 无 warmup 专属样本
    assert all("warmup" not in (s.get("tag") or "") for s in kv["samples"])


def test_global_start_end_run_id_none(tmp_path):
    """start/end 为全局快照（run_id=None），请求前后为 run 内快照。"""
    with MockOpenAIServer() as srv:
        result = _run_and_load(tmp_path, [
            "--port", str(srv.port), "--host", "127.0.0.1",
            "--repeat", "1",
        ])
    kv = result["kv_observations"]
    tags = [(s["tag"], s.get("run_id")) for s in kv["samples"]]
    assert tags[0] == ("start", None)
    assert tags[-1] == ("end", None)
    req_tags = [t for t in tags if t[0].startswith("req_")]
    assert req_tags and all(run_id == "multi_turn_0" for _, run_id in req_tags)
