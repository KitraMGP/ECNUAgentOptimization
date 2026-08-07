#!/usr/bin/env python3
"""E3.5：匿名 trace validator（schema 校验 + 字段检查 + 隐私字段扫描）。

usage: uv run python scripts/e35_validate_trace.py <trace.json>
"""
from __future__ import annotations

import json
import re
import sys

FORBIDDEN_KEYS = {"prompt", "response", "content", "text", "token_text", "secret",
                  "user_id", "user", "ip", "email", "password", "api_key"}
# 允许的长度/统计字段（不视为内容）
ALLOWED = {"prompt_tokens", "prompt_prefix_tokens", "gen_tokens", "token_count"}


def validate(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    errors = []
    if data.get("schema_version") != 1:
        errors.append("schema_version != 1")
    sessions = data.get("sessions", [])
    if not isinstance(sessions, list) or not sessions:
        errors.append("sessions 缺失或为空")
    # 隐私字段扫描（递归检查键名）
    def scan(obj, keypath=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ALLOWED:
                    scan(v, f"{keypath}.{k}")
                    continue
                if k.lower() in FORBIDDEN_KEYS or re.search(r"(response|content|token_text|secret|user_id|password)", k, re.I):
                    errors.append(f"隐私/内容字段: {keypath}.{k}")
                scan(v, f"{keypath}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                scan(v, f"{keypath}[{i}]")
    scan(data)
    # 字段类型检查
    for s in sessions:
        sid = s.get("session_id", "")
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", str(sid)):
            errors.append(f"session_id 非法: {sid!r}")
        for req in s.get("requests", []):
            for field in ("turn", "prompt_tokens", "prompt_prefix_tokens", "gen_tokens"):
                if not isinstance(req.get(field), int):
                    errors.append(f"{sid} 请求缺字段/类型错: {field}")
            if req.get("status") not in ("ok", "truncated", "error"):
                errors.append(f"{sid} 请求 status 非法: {req.get('status')}")
    return {"valid": len(errors) == 0, "errors": errors, "sessions": len(sessions),
            "requests": sum(len(s.get("requests", [])) for s in sessions)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: e35_validate_trace.py <trace.json>")
        sys.exit(1)
    res = validate(sys.argv[1])
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.exit(0 if res["valid"] else 1)
