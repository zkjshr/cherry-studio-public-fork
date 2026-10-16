"""/knowledge/api/search：调用时鉴权（用户×库交集）+ 虚拟库 ekp 展开。"""
import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import knowledge as knowledge_mod
from gateway.app import app

client = TestClient(app)

_H = {"X-Client-Token": "tok"}
# 用户 51-zhang-kj 属部门 202；库里 202-建筑一院=可见、101-市场运营部=不可见、公告栏=公共
_FAKE_KBS = [
    {"id": "kb-202", "name": "202-建筑一院", "description": ""},
    {"id": "kb-101", "name": "101-市场运营部", "description": ""},
    {"id": "kb-pub", "name": "公告栏", "description": ""},
]


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(knowledge_mod, "_token_matches", lambda t: t == "tok")
    monkeypatch.setattr(knowledge_mod, "get_settings",
                        lambda: {"user_departments": {"51-zhang-kj": "202"}})
    async def fake_list_kbs(c):
        return _FAKE_KBS
    monkeypatch.setattr(knowledge_mod.weknora, "list_kbs", fake_list_kbs)
    async def fake_retrieve(c, q, kb_ids, recent=False):
        # 每个 kb_id 都回显一条 chunk，交集/展开若混入不可见 id 立即暴露
        return "ok", [{"id": f"c-{k}", "kb_id": k, "kb_name": "", "title": "T",
                       "file_name": "", "score": 0.5, "content": "正文"} for k in kb_ids], \
               [{"id": f"c-{k}", "kb_id": k, "kb_name": "", "title": "T"} for k in kb_ids]
    monkeypatch.setattr(knowledge_mod.kb_search, "retrieve", fake_retrieve)


def test_search_requires_token():
    assert client.post("/knowledge/api/search",
                       json={"kb_ids": ["kb-202"], "query": "资质"}).status_code == 401


def test_search_filters_kb_ids_by_user_scope():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "51-zhang-kj"},
                    json={"kb_ids": ["kb-202", "kb-101"], "query": "资质"})
    assert r.status_code == 200
    # kb-101 不在该用户可见集，被静默剔除——扇出只打 kb-202（fake_retrieve 回显 kb_ids[0]）
    assert r.json()["chunks"][0]["kb_id"] == "kb-202"


def test_virtual_ekp_expands_within_user_scope():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "51-zhang-kj"},
                    json={"kb_ids": ["ekp"], "query": "资质"})
    body = r.json()
    # ekp = 该用户可见的部门库（202-建筑一院）。公共库不进 ekp；kb-101 不可见不进。
    hit_kb = {c["kb_id"] for c in body["chunks"]}
    # fake_retrieve 用 kb_ids[0] 回显一次；验证展开列表首项是 kb-202 而非 kb-101
    assert hit_kb == {"kb-202"}


def test_virtual_ekp_unregistered_user_no_result():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "nobody"},
                    json={"kb_ids": ["ekp"], "query": "资质"})
    assert r.json()["mode"] == "no_result"


def test_empty_query_rejected():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "51-zhang-kj"},
                    json={"kb_ids": ["kb-202"], "query": "  "})
    assert r.status_code == 422 or r.json()["mode"] == "no_result"


def test_recent_requires_some_visible_kb():
    # 未登记用户：即便 recent=true，可见集为空也绝不能进入 retrieve 扇出
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "nobody"},
                    json={"kb_ids": ["ekp"], "query": "x", "recent": True})
    assert r.json()["mode"] == "no_result"


def test_recent_with_visible_kb_reaches_retrieve():
    # 登记用户 + 可见真实库：recent=true 正常放行
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "51-zhang-kj"},
                    json={"kb_ids": ["kb-202"], "query": "x", "recent": True})
    assert r.json()["mode"] == "ok"
