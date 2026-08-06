"""Metadata 采集与 ctx-size 语义检查测试（E0.6）。"""
from __future__ import annotations

import json
import os

import pytest

from framework.config import BenchmarkConfig
from framework.metadata import (
    check_ctx_semantics,
    collect_metadata,
    get_llama_commit,
    hash_file,
    probe_server,
)


def test_check_ctx_semantics_mismatch_warns():
    cfg = BenchmarkConfig(ctx_size=2048)
    info = {"probed": True, "slot_n_ctx": 512, "total_slots": 4}
    warns = check_ctx_semantics(cfg, info)
    assert len(warns) == 1
    assert "ctx_size=2048" in warns[0]
    assert "slot n_ctx=512" in warns[0]
    assert "total_slots" in warns[0]


def test_check_ctx_semantics_match_no_warning():
    cfg = BenchmarkConfig(ctx_size=2048)
    info = {"probed": True, "slot_n_ctx": 2048, "total_slots": 1}
    assert check_ctx_semantics(cfg, info) == []


def test_check_ctx_semantics_unprobed_warns():
    cfg = BenchmarkConfig(ctx_size=2048)
    warns = check_ctx_semantics(cfg, {"probed": False})
    assert len(warns) == 1
    assert "无法探测" in warns[0]


def test_parallel_mismatch_warns():
    cfg = BenchmarkConfig(ctx_size=2048, parallel=2)
    info = {"probed": True, "slot_n_ctx": 2048, "total_slots": 4}
    warns = check_ctx_semantics(cfg, info)
    assert any("parallel=2" in w for w in warns)


def test_hash_file(tmp_path):
    p = tmp_path / "model.bin"
    p.write_bytes(b"hello" * 1000)
    h = hash_file(str(p))
    assert h["size_bytes"] == 5000
    assert len(h["sha256"]) == 64


def test_hash_file_missing():
    assert hash_file("/nonexistent/x.gguf") == {}


def test_get_llama_commit_structure():
    info = get_llama_commit()
    # 结构完整即可；commit 值取决于运行环境是否存在 llama.cpp/.git
    assert set(info) == {"commit", "dirty"}


def test_probe_server_down_degrades():
    # 连不存在的端口：探测失败应降级为空 dict（不抛异常）
    info = probe_server("http://127.0.0.1:1/v1")
    assert isinstance(info, dict)
    assert info.get("probed") is False


def test_collect_metadata_with_server_info(tmp_path):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"gguf-data")
    cfg = BenchmarkConfig(ctx_size=2048, parallel=0, model_path=str(model))
    server_info = {"probed": True, "model_path": str(model),
                   "total_slots": 4, "slot_n_ctx": 512, "build_info": "b3-x"}
    meta = collect_metadata(cfg, server_info)
    assert meta["model"]["path"] == str(model)
    assert meta["model"]["sha256"]
    assert meta["server"]["slot_n_ctx"] == 512
    assert meta["server"]["ctx_size_config"] == 2048
    assert meta["experiment"]["temperature"] == 0.0
    assert meta["experiment"]["seed"] == 42
    assert len(meta["warnings"]) >= 1  # ctx 不匹配 → warning


def test_probe_server_mock(mock_openai_server):
    """对 mock server 探测：model_path / total_slots / slot n_ctx 正确读取。"""
    base_url = f"http://127.0.0.1:{mock_openai_server.port}/v1"
    info = probe_server(base_url)
    assert info["model_path"] == "models/mock-model.gguf"
    assert info["total_slots"] == 4
    assert info["slot_n_ctx"] == 512
    assert info["probed"] is True
