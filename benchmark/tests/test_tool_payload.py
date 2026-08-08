"""test_tool_payload —— ToolPayloadStore 核心纯 pytest（无 GPU / 无 llama-server / 无 workload 依赖）。

覆盖（对应 `docs/E15_2_TOOL_PAYLOAD_STORE.md` §7）：
projection 确定性、canonical hash/ref、读刷新 LRU、LRU tie-break、跨线程 fail-fast、
missing、hash mismatch、secret fail-closed、max_entries/max_bytes 淘汰、单条超容量、
snapshot/restore（含跨进程拒绝）、purge、订单 payload projection、未知 JSON 有界 projection、
文本 payload、Entry/Ref 不可变、put dedupe、resolve 纯数据语义。
"""
from __future__ import annotations

import json
import os
import threading
from types import MappingProxyType

import pytest

from framework.tool_payload import (
    MAX_PROJECTION_DEPTH,
    MAX_PROJECTION_ITEMS,
    Entry,
    Ref,
    ToolPayloadCapacityError,
    ToolPayloadCrossThreadError,
    ToolPayloadError,
    ToolPayloadHashMismatchError,
    ToolPayloadMissingError,
    ToolPayloadSecretError,
    ToolPayloadSnapshotError,
    ToolPayloadStore,
    canonical_json,
    entry_to_dict,
    eviction_key,
    project_payload,
    sha256_hex,
)

# ---- 现有 tool_call workload 的 mock 订单 payload 形态（只读样例，不 import workload）----

SEARCH_ORDERS = {
    "customer": "C10086",
    "order_ids": [f"SO{i:05d}" for i in range(8)],
}

GET_ORDER_DETAIL = {
    "order_id": "SO00000",
    "items": [{"name": f"SKU{j}", "qty": j + 1, "price": round(25.5 * j, 2)}
              for j in range(6)],
    "address": "湖南省长沙市开福区三一大道 500 号 8 栋 1203 室",
    "logistics": [{"time": f"2026-03-0{i+1} 10:0{i}", "location": "长沙转运中心",
                   "status": "已揽收"} for i in range(5)],
}


def _refs(store: ToolPayloadStore) -> set:
    return set(store.stats()["entries"])


# ---- 1) projection 确定性 / 未知 JSON 有界 projection --------------------------------


def test_projection_deterministic():
    p1 = project_payload({"b": [3, 1, 2], "a": {"z": "x", "y": 1}}, "json")
    p2 = project_payload({"a": {"y": 1, "z": "x"}, "b": [3, 1, 2]}, "json")
    assert p1 == p2
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)
    # 同一 payload 两次入库 → 同一 ref（canonical 归一化）
    store = ToolPayloadStore()
    assert store.put({"a": 1, "b": 2}).ref == store.put({"b": 2, "a": 1}).ref


def test_unknown_json_bounded_projection_deterministic():
    unknown = {"foo": "bar", "meta": {"n": 42, "flag": True, "tags": ["x", "y"]}}
    p1 = project_payload(unknown, "json")
    p2 = project_payload(json.loads(json.dumps(unknown)), "json")  # 等价但键序一致
    assert p1 == p2
    assert p1["type"] == "generic_json"
    assert p1["order_ids"] is None and p1["item_count"] is None
    assert p1["logistics_status"] is None and p1["total_amount"] is None


def test_unknown_json_bounded_projection_limits():
    deep = {"a": {"b": {"c": {"d": {"e": list(range(100))}}}}}
    p = project_payload(deep, "json")
    s = json.loads(p["summary"])
    assert s["depth"] <= MAX_PROJECTION_DEPTH
    assert s["truncated"] is True
    wide = {"xs": list(range(100))}
    s2 = json.loads(project_payload(wide, "json")["summary"])
    assert s2["scalars"] <= MAX_PROJECTION_ITEMS
    assert s2["truncated"] is True
    # 字符串键名被截断到 MAX_PROJECTION_STRING
    long_key = {"k" * 100: 1}
    keys = json.loads(project_payload(long_key, "json")["summary"])["keys"]
    assert all(len(k) <= 32 for k in keys)


# ---- 2) canonical hash / ref ---------------------------------------------------------


def test_canonical_hash_and_ref():
    store = ToolPayloadStore()
    r1 = store.put({"b": 2, "a": 1})
    expected = sha256_hex(canonical_json({"a": 1, "b": 2}).encode("utf-8"))
    assert r1.ref == expected
    assert r1.expected_hash == r1.ref
    # dict 与等价 str 同 ref；内容不同 → 不同 ref
    assert store.put('{"a": 1, "b": 2}').ref == r1.ref
    assert store.put({"a": 1, "b": 3}).ref != r1.ref


def test_text_payload_hash_stable():
    store = ToolPayloadStore()
    text = "这是一段纯文本工具输出\n第二行"
    r = store.put(text)
    assert r.ref == sha256_hex(text.encode("utf-8"))
    assert store.resolve(r.ref, r.expected_hash) == text


# ---- 3) 读刷新 LRU / 淘汰 -------------------------------------------------------------


def test_resolve_refreshes_lru():
    store = ToolPayloadStore(max_entries=2)
    r1 = store.put({"a": 1})
    r2 = store.put({"b": 2})
    store.resolve(r1.ref, r1.expected_hash)  # r1 变最新
    r3 = store.put({"c": 3})                  # 淘汰 r2（最久未用）
    assert _refs(store) == {r1.ref, r3.ref}
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(r2.ref, r2.expected_hash)


def test_get_refreshes_lru():
    store = ToolPayloadStore(max_entries=2)
    r1 = store.put({"a": 1})
    r2 = store.put({"b": 2})
    store.get(r1.ref)
    r3 = store.put({"c": 3})
    assert _refs(store) == {r1.ref, r3.ref}
    assert r2.ref not in _refs(store)


def test_lru_eviction_min_last_used():
    store = ToolPayloadStore(max_entries=2)
    r1 = store.put({"a": 1})
    r2 = store.put({"b": 2})
    r3 = store.put({"c": 3})
    assert _refs(store) == {r2.ref, r3.ref}   # r1（last_used 最旧）被淘汰
    r4 = store.put({"d": 4})
    assert _refs(store) == {r3.ref, r4.ref}   # r2 被淘汰


def test_lru_tie_break_inserted_tick():
    # last_used_tick 相同 → inserted_tick 更小者先淘汰（纯函数语义）
    e_old = Entry(ref="r1", content_type="json", size_bytes=1, sha256="h1",
                  projection=MappingProxyType({}), inserted_tick=1, last_used_tick=5)
    e_new = Entry(ref="r2", content_type="json", size_bytes=1, sha256="h2",
                  projection=MappingProxyType({}), inserted_tick=2, last_used_tick=5)
    assert eviction_key(e_old) < eviction_key(e_new)
    # last_used_tick 不同 → last_used 主导
    e_later = Entry(ref="r3", content_type="json", size_bytes=1, sha256="h3",
                    projection=MappingProxyType({}), inserted_tick=0, last_used_tick=9)
    assert eviction_key(e_new) < eviction_key(e_later)


# ---- 4) 跨线程 fail-fast ---------------------------------------------------------------


def test_cross_thread_operation_fail_fast():
    store = ToolPayloadStore()
    errors = []

    def worker():
        try:
            store.put({"a": 1})
        except ToolPayloadCrossThreadError as exc:  # noqa: BLE001 - 精确断言
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(errors) == 1


def test_ref_cross_thread_fail_fast():
    box = {}

    def worker():
        s = ToolPayloadStore()
        box["store"] = s
        box["ref"] = s.put({"a": 1})
        # 子线程内正常使用（证明 thread-scoped 而非全局禁用）
        box["text"] = s.resolve(box["ref"].ref, box["ref"].expected_hash)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert box["text"] == canonical_json({"a": 1})
    # 主线程访问子线程创建的 store → fail-fast
    with pytest.raises(ToolPayloadCrossThreadError):
        box["store"].resolve(box["ref"].ref, box["ref"].expected_hash)
    with pytest.raises(ToolPayloadCrossThreadError):
        box["store"].snapshot()


def test_snapshot_restore_cross_thread_fail_fast():
    box = {}

    def worker():
        s = ToolPayloadStore()
        s.put({"a": 1})
        box["snap"] = s.snapshot()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    main_store = ToolPayloadStore()
    with pytest.raises(ToolPayloadCrossThreadError):
        main_store.restore(box["snap"])


# ---- 5) missing / 6) hash mismatch ------------------------------------------------------


def test_missing_fail_fast():
    store = ToolPayloadStore()
    with pytest.raises(ToolPayloadMissingError):
        store.resolve("0" * 64, "0" * 64)
    with pytest.raises(ToolPayloadMissingError):
        store.get("0" * 64)


def test_hash_mismatch_fail_fast_and_no_refresh():
    store = ToolPayloadStore()
    r = store.put({"a": 1})
    with pytest.raises(ToolPayloadHashMismatchError):
        store.resolve(r.ref, "0" * 64)
    # mismatch 不刷新 last_used_tick（tick 不变）
    tick_before = store.tick
    with pytest.raises(ToolPayloadHashMismatchError):
        store.resolve(r.ref, "0" * 64)
    assert store.tick == tick_before
    # 正确 hash 仍可解析
    assert store.resolve(r.ref, r.expected_hash) == canonical_json({"a": 1})


# ---- 7) secret fail-closed ---------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"username": "alice", "password": "fake_secret_123"},
    {"token": "fake_token_value"},
    {"api_key": "sk-0123456789abcdef0123"},
    {"nested": {"client_secret": "xyz"}},
    {"jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"},
    {"x": "sk-0123456789abcdef0123"},          # 键名干净但值命中敏感模式
    {"data": "Bearer abcdefghijklmnopqrstuvwxyz"},
])
def test_secret_json_fail_closed(payload):
    store = ToolPayloadStore()
    with pytest.raises(ToolPayloadSecretError):
        store.put(payload)
    assert len(store) == 0
    assert store.snapshot().to_dict()["entries"] == []  # 不进 snapshot


def test_secret_text_fail_closed():
    store = ToolPayloadStore()
    with pytest.raises(ToolPayloadSecretError):
        store.put("token: fake_secret_123")
    with pytest.raises(ToolPayloadSecretError):
        store.put("api key = sk-0123456789abcdef0123")
    assert len(store) == 0
    # 无敏感信息的文本不受影响
    store.put("订单已发货，请查收。")
    assert len(store) == 1


# ---- 8) max_entries / max_bytes ----------------------------------------------------------


def test_max_entries_eviction():
    store = ToolPayloadStore(max_entries=2)
    r1 = store.put({"a": 1})
    r2 = store.put({"b": 2})
    r3 = store.put({"c": 3})
    assert len(store) == 2
    assert _refs(store) == {r2.ref, r3.ref}
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(r1.ref, r1.expected_hash)


def test_max_bytes_eviction():
    store = ToolPayloadStore(max_bytes=200)
    r1 = store.put({"a": "x" * 80})
    r2 = store.put({"b": "y" * 80})
    assert len(store) == 2 and store.total_bytes <= 200
    r3 = store.put({"c": "z" * 80})  # 超出 → 淘汰 r1（最旧）
    assert len(store) == 2 and store.total_bytes <= 200
    assert _refs(store) == {r2.ref, r3.ref}
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(r1.ref, r1.expected_hash)


def test_single_payload_over_max_bytes():
    store = ToolPayloadStore(max_bytes=100)
    with pytest.raises(ToolPayloadCapacityError):
        store.put({"big": "x" * 500})
    assert len(store) == 0
    with pytest.raises(ToolPayloadCapacityError):
        ToolPayloadStore(max_entries=0)


# ---- 9) snapshot / restore ---------------------------------------------------------------


def test_snapshot_auditable_no_payload():
    store = ToolPayloadStore()
    store.put(SEARCH_ORDERS)
    store.put(GET_ORDER_DETAIL)
    snap = store.snapshot()
    d = snap.to_dict()
    assert d["pid"] == os.getpid()
    assert len(d["entries"]) == 2
    for e in d["entries"]:
        assert "ref" in e and "size_bytes" in e and "projection" in e
        assert "inserted_tick" in e and "last_used_tick" in e
        # 快照只含元数据 + projection，不含 payload 原文
        assert "payload" not in e and "blob" not in e and "text" not in e
    # projection 中不包含原文值（地址 / customer 值不出现在 projection；
    # summary 里的键名可审计，但不携带任何值内容）
    for e in d["entries"]:
        proj_json = json.dumps(e["projection"])
        assert "湖南省长沙市" not in proj_json
        assert "C10086" not in proj_json


def test_snapshot_restore_restores_view():
    store = ToolPayloadStore()
    a = store.put(SEARCH_ORDERS)
    b = store.put(GET_ORDER_DETAIL)
    snap = store.snapshot()
    c = store.put({"other": 1})
    assert len(store) == 3
    store.restore(snap)
    assert len(store) == 2
    assert _refs(store) == {a.ref, b.ref}
    # 快照后新 put 的 ref 不可见（fail-fast）
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(c.ref, c.expected_hash)
    # 快照内 ref 仍可正常 resolve
    assert store.resolve(a.ref, a.expected_hash) == canonical_json(SEARCH_ORDERS)
    assert json.loads(store.resolve(b.ref, b.expected_hash))["order_id"] == "SO00000"


def test_snapshot_restore_after_purge_no_resurrection():
    store = ToolPayloadStore()
    a = store.put({"a": 1})
    snap = store.snapshot()
    store.purge()
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(a.ref, a.expected_hash)
    store.restore(snap)
    # 快照不携带原文：被 purge 的 payload 不可复活（仍 fail-fast）
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(a.ref, a.expected_hash)


def test_snapshot_cross_process_restore_rejected(monkeypatch):
    store = ToolPayloadStore()
    store.put({"a": 1})
    snap = store.snapshot()
    monkeypatch.setattr(os, "getpid", lambda: 999999)
    with pytest.raises(ToolPayloadSnapshotError):
        store.restore(snap)


# ---- 10) purge ---------------------------------------------------------------------------


def test_purge_single_and_all():
    store = ToolPayloadStore()
    r1 = store.put({"a": 1})
    r2 = store.put({"b": 2})
    store.purge(r1.ref)
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(r1.ref, r1.expected_hash)
    assert r2.ref in _refs(store)
    assert store.snapshot().to_dict()["entries"][0]["ref"] == r2.ref
    store.purge()
    assert len(store) == 0
    with pytest.raises(ToolPayloadMissingError):
        store.resolve(r2.ref, r2.expected_hash)


# ---- 11) 订单 payload projection ---------------------------------------------------------


def test_search_orders_projection():
    store = ToolPayloadStore()
    r = store.put(SEARCH_ORDERS)
    proj = store.get(r.ref).projection
    assert proj["type"] == "order"
    # projection 深冻结：list → tuple（外部修改 fail-fast，见 reviewer 修复测试）
    assert isinstance(proj["order_ids"], tuple)
    assert list(proj["order_ids"]) == [f"SO{i:05d}" for i in range(8)]
    assert proj["order_ids_total"] == 8
    assert proj["item_count"] is None
    assert proj["total_amount"] is None
    assert proj["logistics_status"] is None


def test_order_detail_projection():
    store = ToolPayloadStore()
    r = store.put(GET_ORDER_DETAIL)
    proj = store.get(r.ref).projection
    assert proj["type"] == "order"
    assert proj["item_count"] == 6
    assert proj["total_amount"] == 1785.0  # sum((j+1) * 25.5j, j=0..5)
    assert list(proj["logistics_status"]) == ["已揽收"]  # 5 条同状态 → 去重保序
    assert isinstance(proj["logistics_status"], tuple)  # 深冻结
    assert proj["order_ids"] is None
    # projection 不提取地址等原文值内容（summary 仅含键名统计，不含值）
    assert "湖南省长沙市" not in json.dumps(dict(proj))


# ---- 7.2) reviewer 修复：secret 覆盖顶层 list / 嵌套 / 复合敏感字段 --------------------


@pytest.mark.parametrize("payload", [
    [{"username": "alice", "password": "fake"}],   # 顶层 list
    [{"a": [{"token": "t"}]}],                      # list → dict → list 嵌套
    [[{"secret": "s"}]],                              # 双层 list 嵌套
    {"db_password": "x"},                             # 复合敏感字段（后缀 password）
    {"auth_token": "y"},                              # 复合敏感字段（后缀 token）
    {"user_secret": "z"},                             # 复合敏感字段（后缀 secret）
    {"nested": {"master_password": "w"}},
    {"authorization_code": "c"},
    {"list": [{"client_secret": "s"}]},
])
def test_secret_top_level_list_and_composite_fields_fail_closed(payload):
    store = ToolPayloadStore()
    with pytest.raises(ToolPayloadSecretError):
        store.put(payload)
    assert len(store) == 0
    assert store.snapshot().to_dict()["entries"] == []  # 无任何痕迹


@pytest.mark.parametrize("payload", [
    {"author": "张三"},          # auth 前缀但不误伤
    {"username": "alice"},
    {"total": 3},
    {"monkey": "banana"},        # key 后缀但不误伤（后缀列表刻意不含 "key"）
    {"secretary": "王五"},        # secret 前缀但不误伤（后缀列表刻意不含 "auth"/"key"）
    {"tokenize": 1},              # token 前缀但不误伤
    {"user_profile": {"nickname": "a"}},
    [{"username": "alice", "orders": ["SO1"]}],   # 顶层 list 正常字段放行
])
def test_secret_no_false_positive_on_normal_fields(payload):
    store = ToolPayloadStore()
    store.put(payload)
    assert len(store) == 1
    assert len(store.snapshot().to_dict()["entries"]) == 1


# ---- 7.3) reviewer 修复：projection 递归不可变（外部修改 fail-fast + snapshot 不污染）----


def test_projection_recursively_immutable():
    store = ToolPayloadStore()
    r = store.put(SEARCH_ORDERS)
    proj = store.get(r.ref).projection
    assert isinstance(proj["order_ids"], tuple)
    with pytest.raises(AttributeError):
        proj["order_ids"].append("SO99999")       # tuple 无 append
    with pytest.raises(TypeError):
        proj["order_ids"][0] = "SO00001"          # tuple 不支持下标赋值
    with pytest.raises(TypeError):
        proj["total_amount"] = 999                 # MappingProxyType 只读
    # 嵌套 dict 深度冻结（构造含嵌套只读结构的 Entry，语义与 store 生成一致）
    frozen = MappingProxyType({"a": MappingProxyType({"b": (1, 2)}), "xs": (3,)})
    e = Entry(ref="r", content_type="json", size_bytes=1, sha256="h",
              projection=frozen, inserted_tick=1, last_used_tick=1)
    with pytest.raises(TypeError):
        e.projection["a"]["c"] = 1
    with pytest.raises(AttributeError):
        e.projection["a"]["b"].append(3)


def test_projection_mutation_attempt_does_not_pollute_snapshot():
    store = ToolPayloadStore()
    r = store.put(SEARCH_ORDERS)
    proj = store.get(r.ref).projection          # get 的读刷新发生在快照之前
    snap_before = store.snapshot().to_dict()
    for mut in (lambda: proj["order_ids"].append("X"),
                lambda: proj["order_ids"].__setitem__(0, "X"),
                lambda: proj.__setitem__("type", "hacked")):
        try:
            mut()
        except (AttributeError, TypeError):
            pass
    snap_after = store.snapshot().to_dict()
    assert snap_before == snap_after          # snapshot 视图不被污染
    # 原 entry 视图同样未被污染（snapshot 与 entry 共享同一不可变结构）
    assert proj["order_ids"] == tuple(f"SO{i:05d}" for i in range(8))


def test_entry_to_dict_deep_unfreezes_projection():
    store = ToolPayloadStore()
    r = store.put(SEARCH_ORDERS)
    d = entry_to_dict(store.get(r.ref))
    assert isinstance(d["projection"]["order_ids"], list)
    json.dumps(d)                             # 可序列化（无 MappingProxyType / tuple 残留）


# ---- 7.4) reviewer 修复：文本 projection 不泄漏 payload 原文 -----------------------------


def test_text_projection_never_leaks_raw_payload():
    store = ToolPayloadStore()
    text = "这是秘密订单文本内容，" + "长文本" * 50   # > 64 字符，原文可辨识
    r = store.put(text)
    proj = store.get(r.ref).projection
    sm = json.loads(proj["summary"])
    assert sm["chars"] == len(text)          # 真实字符数（reviewer 连带修复：chars 不再恒为 4）
    assert "head" not in sm                   # 无 head 原文
    assert sm["head_sha256"] == sha256_hex(text.encode("utf-8"))[:16]  # 确定性指纹
    proj_text = json.dumps(dict(proj))
    assert "这是秘密" not in proj_text and "长文本" not in proj_text
    snap_text = json.dumps(store.snapshot().to_dict())
    assert "这是秘密" not in snap_text and "长文本" not in snap_text
    # 短文本同样不泄漏
    r2 = store.put("订单已发货")
    p2 = store.get(r2.ref).projection
    assert json.loads(p2["summary"])["chars"] == 5
    assert "订单已发货" not in json.dumps(dict(p2))


# ---- 7.5) reviewer 修复：混合类型键 / NaN / Infinity / 非有限 qty/price 统一 fail-fast ----


@pytest.mark.parametrize("payload", [
    {"qty": float("nan")},
    {"a": float("inf")},
    {"a": float("-inf")},
    {1: "x", "b": 2},                        # 混合类型键无法稳定排序
    '{"qty": NaN}',
    '{"a": Infinity}',
])
def test_non_standard_json_unified_fail_fast(payload):
    store = ToolPayloadStore()
    with pytest.raises(ToolPayloadError):
        store.put(payload)
    assert len(store) == 0                      # 非标准 JSON 不进入 store/blob/projection


def test_non_finite_qty_price_fail_fast():
    with pytest.raises(ToolPayloadError):
        project_payload({"items": [{"qty": float("nan"), "price": 1}]}, "json")
    with pytest.raises(ToolPayloadError):
        project_payload({"items": [{"qty": 1e308, "price": 1e308}]}, "json")  # 乘积 inf
    with pytest.raises(ToolPayloadError):
        project_payload({"items": [{"qty": 10 ** 400, "price": 1}]}, "json")  # int 溢出 float
    with pytest.raises(ToolPayloadError):
        project_payload({"items": [{"qty": 1e308, "price": 1}] * 2}, "json")  # 求和溢出 inf


# ---- 7.6) reviewer 修复：只读接口同样 thread fail-fast（与文档 §3.4 一致）---------------


def test_readonly_accessors_cross_thread_fail_fast():
    box = {}

    def worker():
        s = ToolPayloadStore()
        s.put({"a": 1})
        box["store"] = s

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    st = box["store"]
    with pytest.raises(ToolPayloadCrossThreadError):
        len(st)
    with pytest.raises(ToolPayloadCrossThreadError):
        st.total_bytes
    with pytest.raises(ToolPayloadCrossThreadError):
        st.tick
    with pytest.raises(ToolPayloadCrossThreadError):
        st.owner_ident
    with pytest.raises(ToolPayloadCrossThreadError):
        st.stats()


# ---- 12) 补充：不可变 / dedupe / resolve 纯数据 ------------------------------------------


def test_entry_and_ref_immutable():
    store = ToolPayloadStore()
    r = store.put({"a": 1})
    e = store.get(r.ref)
    assert isinstance(r, Ref) and isinstance(e, Entry)
    with pytest.raises(AttributeError):
        r.ref = "xxx"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        e.ref = "xxx"  # type: ignore[misc]
    with pytest.raises(TypeError):
        e.projection["x"] = 1  # MappingProxyType 只读


def test_put_dedupe_same_content():
    store = ToolPayloadStore()
    r1 = store.put({"a": 1})
    tick = store.tick
    r2 = store.put({"a": 1})
    assert r1.ref == r2.ref
    assert len(store) == 1
    assert store.tick == tick + 1  # dedupe 刷新 last_used（一次 touch）


def test_resolve_is_pure_data_no_prompt_injection():
    store = ToolPayloadStore()
    r = store.put({"b": 2, "a": 1})
    out = store.resolve(r.ref, r.expected_hash)
    assert out == canonical_json({"a": 1, "b": 2})  # canonical 文本，非用户原始串
    assert "ACTION" not in out
    assert "prompt" not in out


def test_unsupported_payload_type():
    store = ToolPayloadStore()
    with pytest.raises(ToolPayloadError):
        store.put(12345)  # type: ignore[arg-type]
