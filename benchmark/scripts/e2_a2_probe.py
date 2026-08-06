#!/usr/bin/env python3
"""E2.1-A2：受控 branch probe（机制验证，不替代正式 workload）。

序列（parallel=2, ctx=2048, --slot-routing-stats）：
  - 自动路由：A+X → A+Y → A+X 回访
  - 显式 id_slot：slot0=A+X, slot1=A+Y, slot0=A+X 回访
每 replicate 前 erase 两 slot + /metrics/kv 断言清洁（independent 协议）。

记录：selected_slot、routing_reason、cache_n、cached_tokens、prompt_n、
used_cells、active_sequences、shared_cells。

用法：uv run python scripts/e2_a2_probe.py --port 8080 --policy default|prefix-branch
      --output results/e21/branch_probe_<policy>.json --reps 5
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List, Optional
from urllib import request

A = "The quick brown fox jumps over the lazy dog near the river bank."
X = " It then runs into the dark forest chasing a small rabbit."
Y = " It stops to drink fresh water from the clear mountain stream."


def _norm(text: str) -> str:
    import hashlib
    import re
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower(), flags=re.UNICODE)
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


def post(url: str, body: dict) -> Dict[str, Any]:
    req = request.Request(url, data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str) -> Dict[str, Any]:
    with request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Probe:
    def __init__(self, base: str) -> None:
        self.base = base
        self.log: List[Dict[str, Any]] = []

    def clean(self) -> bool:
        """erase 所有 slot 并断言 KV 清洁（independent 协议）。"""
        slots = get(f"{self.base}/slots")
        for s in slots:
            post(f"{self.base}/slots/{s['id']}?action=erase", {})
        time.sleep(0.3)
        kv = get(f"{self.base}/metrics/kv")
        return kv.get("used_cells") == 0 and kv.get("active_sequences") == 0

    def completion(self, label: str, prompt: str, id_slot: Optional[int] = None) -> Dict[str, Any]:
        """用 OpenAI 兼容 /v1/chat/completions（usage.cached_tokens 是准确的总命中数）。

        注意：原生 /completion 的 timings.cache_n 是"最后批次"语义（分批处理时
        只反映最后一批的缓存计数），不可用作总命中数对比。
        """
        before = len(get(f"{self.base}/routing/events?limit=10000")["events"])
        body = {
            "model": "bench",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0,
            # 与 benchmark driver 一致：chat_template_kwargs 控制 Qwen3.5 thinking
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if id_slot is not None:
            body["id_slot"] = id_slot
        r = post(f"{self.base}/v1/chat/completions", body)
        usage = r.get("usage", {})
        kv = get(f"{self.base}/metrics/kv")
        # response 规范化 hash（输出等价性/串扰检查）
        content = ""
        choices = r.get("choices") or []
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content") or ""
        rec = {
            "label": label, "id_slot": id_slot,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens"),
            "used_cells": kv.get("used_cells"),
            "active_sequences": kv.get("active_sequences"),
            "shared_cells": kv.get("shared_cells"),
            "response_hash": _norm(content),
        }
        # 关联本请求产生的 routing 事件（取请求前后事件数差中的最后一条）
        all_ev = get(f"{self.base}/routing/events?limit=10000")["events"]
        new_ev = all_ev[before:]
        if new_ev:
            e = new_ev[-1]
            rec.update({
                "selected_slot": e.get("selected_slot"),
                "routing_reason": e.get("routing_reason"),
                "selected_prefix_tokens": e.get("selected_prefix_tokens"),
                "selected_prompt_tokens": e.get("selected_prompt_tokens"),
                "is_branch_request": e.get("is_branch_request"),
                "preserved_branch": e.get("preserved_branch"),
                "fallback_to_default": e.get("fallback_to_default"),
            })
        self.log.append(rec)
        print(f"  [{label:18s}] slot={rec.get('selected_slot')} reason={rec.get('routing_reason','?')} "
              f"cached={rec['cached_tokens']} used={rec['used_cells']} active={rec['active_sequences']}")
        return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--policy", choices=["default", "prefix-branch"], required=True)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"
    pr = Probe(base)
    results: Dict[str, Any] = {"policy": args.policy, "replicates": []}

    for rep in range(args.reps):
        assert pr.clean(), f"replicate {rep}: KV clean assertion failed"
        rep_rec: Dict[str, Any] = {"rep": rep, "auto": [], "explicit": []}
        # 自动路由
        pr.completion("A+X (auto)", A + X)
        pr.completion("A+Y (auto)", A + Y)
        r_ax = pr.completion("A+X again (auto)", A + X)
        rep_rec["auto"] = {"branch_preserved": pr.log[-2].get("preserved_branch"),
                           "revisit_reason": pr.log[-1].get("routing_reason"),
                           "revisit_cached_tokens": r_ax.get("cached_tokens"),
                           "revisit_selected_slot": r_ax.get("selected_slot"),
                           "active_sequences": r_ax.get("active_sequences")}
        # 显式 id_slot
        assert pr.clean()
        pr.completion("A+X (slot0)", A + X, id_slot=0)
        pr.completion("A+Y (slot1)", A + Y, id_slot=1)
        r_ax2 = pr.completion("A+X again (slot0)", A + X, id_slot=0)
        rep_rec["explicit"] = {"cached_first": pr.log[-3].get("cached_tokens"),
                               "cached_revisit": r_ax2.get("cached_tokens"),
                               "hash_first": pr.log[-3].get("response_hash"),
                               "hash_revisit": r_ax2.get("response_hash")}
        results["replicates"].append(rep_rec)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
