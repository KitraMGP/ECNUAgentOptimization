#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E15.3：B1 deterministic structured-lossless preprocessor paired 门禁 runner。

验证（E15.0 §3.1 lossless 门禁 + 用户 E15 指令）：
- 同硬件、同原始 prompt、seed=42 / temperature=0（greedy），**仅 preprocessor
  off/on 不同**（off 发原文，on 发 structured_lossless_compress 产物）；
- 记录：输入 token 数（prompt_tokens / timings.prompt_n）、输出 content 与
  finish_reason（停止原因）、timings、/metrics/kv 快照、压缩率（manifest 摘要）；
- 门禁判定（gate_verdict，纯函数）：
  - M0 数据完整性：off/on 均 HTTP 200、输出非空、timings 非空；
  - M1 输出一致：on 与 off 的 content 逐字节一致 **且 finish_reason 一致**
    （文本一致 ⟺ token ids 一致：同一模型同一 tokenizer 确定性编码；logprobs
    开启时额外记录 token ids）；
  - M2 实际 token 收益：on 的 prompt_tokens < off 的 prompt_tokens（至少一次）；
  - M3 manifest 可逆/确定性 + protected 字节保留：由压缩器单元测试保证，此处
    记录 on 侧 manifest 摘要（hash/压缩率/replaced/protected 计数）作为证据；
- 判定：M0+M1+M2 → PASS_LOSSLESS；M0+M1 但 M2 不达 → HOLD_NO_MEASURABLE_GAIN；
  M1 不达（输出不一致）→ HOLD_NOT_VALIDATED（无损不成立，降级有损候选走 E15.5）；
  数据缺失/异常 → HOLD_NOT_VALIDATED。**不伪称 PASS**。

场景：
- --scenario single：一组固定 messages（含完全重复块），off/on 各一次；
- --scenario multi_turn：20 轮累积历史 off/on 逐轮对照（最强验证：历史冗余
  逐轮累积，任何偏差都会在后续轮输出中暴露）。

用法（benchmark/ 目录下，server 需已启动，如）：
  ../llama.cpp/build-cuda/bin/llama-server -m ../models/qwen3-5-4B-Q4_K_M.gguf \
    --host 127.0.0.1 --port 8080 -ngl 99 --ctx-size 4096 --parallel 1 &
  python runner/e15_3_b1_paired.py --scenario single --output results/e15_3_b1_paired.json

输出：results/e15_3_b1_paired_<时间戳>.json（原始证据 + 门禁判定 + verdict）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional

# sys.path 注入（直接 `python runner/e15_3_b1_paired.py` 运行时可用）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI

from framework.prompt_preprocessor import (
    Preprocessor,
    structured_lossless_compress,
)

SEED = 42
TEMPERATURE = 0.0
MULTI_TURN_ROUNDS = 20

# 单请求对照的消息（含完全重复块：LONG_Q 出现两次；system 为 protected）
LONG_Q = ("请解释一下什么是 KV Cache，以及它在长上下文智能体推理中的内存累积问题，"
          "并说明它与传统缓存机制的区别。" * 2)
SINGLE_SYSTEM = ("你是乐于助人的中文助手。请基于给定的对话历史回答问题，"
                 "回答要求准确、简洁、完整，不要重复用户的话。")
SINGLE_MESSAGES = [
    {"role": "system", "content": SINGLE_SYSTEM},
    {"role": "user", "content": LONG_Q},
    {"role": "assistant", "content": "KV Cache 是推理时缓存中间键值对以加速生成的技术。下面给出详细解释。"},
    {"role": "user", "content": LONG_Q},          # 完全重复块（可压缩）
    {"role": "user", "content": "基于以上解释，KV Cache 的主要优点是什么？请用一句话总结。"},
]


# ---- 消息序列（multi_turn：POOL 循环 → 历史中出现完全重复的 user 消息）------------
POOL = [
    "解释一下什么是 KV Cache。",
    "写一句押韵的中文诗。",
    "把'今天天气很好'翻译成英文。",
    "3.14 乘以 2 等于多少？",
    "用一句话介绍长沙。",
    "什么是 Copy-on-Write？",
    "推荐一本编程入门书。",
    "计算 15 + 27 并解释过程。",
]
MT_SYSTEM = "你是乐于助人的中文助手。回答简洁准确。"


def build_multi_turn_history(rounds: int) -> List[Dict[str, Any]]:
    """确定性构造 rounds 轮累积历史（user 问题循环 POOL，assistant 为固定模板）。

    assistant 回复为固定文本（含轮次占位），保证 off/on 两侧的**原始历史逐字节
    相同**；重复块来自 POOL 循环的 user 消息。
    """
    history: List[Dict[str, Any]] = [{"role": "system", "content": MT_SYSTEM}]
    for i in range(rounds):
        history.append({"role": "user", "content": POOL[i % len(POOL)]})
        history.append({"role": "assistant",
                        "content": f"（第 {i + 1} 轮的回答：根据问题给出简洁准确的中文答复。）" * 2})
    return history


# ---- OpenAI 兼容请求 -----------------------------------------------------------


def complete(client: OpenAI, messages: List[Dict[str, Any]], model: str) -> Dict[str, Any]:
    """greedy（temp=0 / seed 固定）请求；返回 content / finish_reason / timings 等。"""
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=TEMPERATURE,
        seed=SEED,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    choice = resp.choices[0]
    timings = getattr(resp, "timings", None)
    if not isinstance(timings, dict):
        extra = getattr(resp, "model_extra", None) or {}
        timings = extra.get("timings")
    row: Dict[str, Any] = {
        "content": choice.message.content or "",
        "finish_reason": choice.finish_reason,
        "prompt_tokens": resp.usage.prompt_tokens,
        "completion_tokens": resp.usage.completion_tokens,
        "timings": timings,
        "logprobs_tokens": None,
    }
    lp = getattr(choice, "logprobs", None)
    if lp is not None and getattr(lp, "content", None):
        row["logprobs_tokens"] = [t.token for t in lp.content]
    return row


def kv_snapshot(base_url: str) -> Optional[Dict[str, Any]]:
    """GET /metrics/kv 快照（不可用时返回 None，不阻塞）。"""
    try:
        with urllib.request.urlopen(
            base_url.replace("/v1", "") + "/metrics/kv", timeout=5
        ) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ---- 门禁判定（纯函数）----------------------------------------------------------


def gate_verdict(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """按 M0/M1/M2 判定（纯函数，可单测）。

    - M0 数据完整性：off/on 均 HTTP 200、输出非空、timings 非空；
    - M1 输出一致：content 逐字节一致且 finish_reason 一致（全部对照点）；
    - M2 实际 token 收益：on 的 prompt_tokens 严格小于 off（至少一个对照点，
      multi_turn 取所有轮次；single 取唯一对照点）。
    """
    off_rows = evidence["off_rows"]
    on_rows = evidence["on_rows"]
    if len(off_rows) != len(on_rows) or not off_rows:
        return {
            "verdict": "HOLD_NOT_VALIDATED",
            "reason": "对照点数量不一致或为空（数据缺失）",
        }
    m0 = all(
        r.get("http_ok") and r.get("content") and r.get("timings") is not None
        for r in off_rows + on_rows
    )
    m1 = all(
        o["content"] == n["content"] and o["finish_reason"] == n["finish_reason"]
        for o, n in zip(off_rows, on_rows)
    )
    m2 = any(n["prompt_tokens"] < o["prompt_tokens"] for o, n in zip(off_rows, on_rows))
    if not m0:
        return {"verdict": "HOLD_NOT_VALIDATED", "reason": "数据完整性失败（M0）"}
    if not m1:
        return {
            "verdict": "HOLD_NOT_VALIDATED",
            "reason": "输出不一致（M1）：off/on content 或 finish_reason 不同 → "
                      "无损不成立，降级为有损候选（E15.5）",
        }
    if not m2:
        return {
            "verdict": "HOLD_NO_MEASURABLE_GAIN",
            "reason": "输出一致但无实际 token 收益（M2 不达）",
        }
    return {
        "verdict": "PASS_LOSSLESS",
        "reason": "M0+M1+M2 全部满足：输出逐字节一致 + 停止原因一致 + 实际 token 收益",
    }


# ---- 场景执行 -------------------------------------------------------------


def run_single(client: OpenAI, model: str, base_url: str) -> Dict[str, Any]:
    pp = Preprocessor()
    work, audit = pp.process(SINGLE_MESSAGES)
    off_row = complete(client, SINGLE_MESSAGES, model)
    off_row["http_ok"] = True
    on_row = complete(client, work, model)
    on_row["http_ok"] = True
    evidence: Dict[str, Any] = {
        "scenario": "single",
        "off_rows": [off_row],
        "on_rows": [on_row],
        "on_manifest": audit,
        "input_messages": SINGLE_MESSAGES,
        "compressed_messages": work,
        "kv": {"off": kv_snapshot(base_url), "on": kv_snapshot(base_url)},
    }
    verdict = gate_verdict(evidence)
    return {"evidence": evidence, "verdict": verdict}


def run_multi_turn(client: OpenAI, model: str, base_url: str, rounds: int) -> Dict[str, Any]:
    history_off = build_multi_turn_history(rounds)
    off_rows: List[Dict[str, Any]] = []
    on_rows: List[Dict[str, Any]] = []
    on_manifests: List[Dict[str, Any]] = []
    pp = Preprocessor()
    off_hist: List[Dict[str, Any]] = [dict(history_off[0])]
    on_hist_raw: List[Dict[str, Any]] = [dict(history_off[0])]   # off/on 共享同一原始历史
    for i in range(rounds):
        # 追加本轮 user（原始历史逐字节相同）
        off_hist.append(dict(history_off[2 * i + 1]))
        on_hist_raw.append(dict(history_off[2 * i + 1]))
        # off：发送原文；on：发送压缩产物（仅发送形态不同）
        off_row = complete(client, off_hist, model)
        off_row["http_ok"] = True
        work, audit = pp.process(on_hist_raw)
        on_row = complete(client, work, model)
        on_row["http_ok"] = True
        off_rows.append(off_row)
        on_rows.append(on_row)
        on_manifests.append(audit)
        # 两则历史追加相同 assistant 输出（保持 off/on 原始历史逐字节一致）
        off_hist.append({"role": "assistant", "content": off_row["content"]})
        on_hist_raw.append({"role": "assistant", "content": off_row["content"]})
    evidence: Dict[str, Any] = {
        "scenario": "multi_turn",
        "rounds": rounds,
        "off_rows": off_rows,
        "on_rows": on_rows,
        "on_manifests": on_manifests,
        "kv": {"off": kv_snapshot(base_url), "on": kv_snapshot(base_url)},
    }
    verdict = gate_verdict(evidence)
    return {"evidence": evidence, "verdict": verdict}


# ---- CLI -------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="E15.3 B1 paired 门禁 runner")
    ap.add_argument("--server-url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default="bench")
    ap.add_argument("--scenario", choices=["single", "multi_turn"], default="single")
    ap.add_argument("--rounds", type=int, default=MULTI_TURN_ROUNDS)
    ap.add_argument("--output", default=None, help="结果 JSON 路径（默认 results/e15_3_b1_paired_<ts>.json）")
    args = ap.parse_args(argv)

    client = OpenAI(base_url=args.server_url, api_key="EMPTY")
    base_url = args.server_url
    if args.scenario == "single":
        result = run_single(client, args.model, base_url)
    else:
        result = run_multi_turn(client, args.model, base_url, args.rounds)

    out_path = args.output or (
        "results/e15_3_b1_paired_"
        + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    )
    import os
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    v = result["verdict"]
    print(f"== verdict: {v['verdict']} ==")
    print(f"reason: {v['reason']}")
    ev = result["evidence"]
    for i, (o, n) in enumerate(zip(ev["off_rows"], ev["on_rows"])):
        same = o["content"] == n["content"] and o["finish_reason"] == n["finish_reason"]
        print(
            f"  [{i}] off prompt={o['prompt_tokens']} on prompt={n['prompt_tokens']} "
            f"out_same={same} finish={o['finish_reason']}/{n['finish_reason']}"
        )
    print(f"结果已保存: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
