r"""共享知识库目录端点测试：^\d{3}- 部门库命名约定分类、按 X-Client-User 的
部门可见性过滤、X-Client-Token 鉴权、WeKnora 不可达降级为空列表、WeKnora 库
id 一律加 weknora: 命名空间前缀下发（上游 id 是 uuidv4，客户端契约要求共享库
wire id 必非 uuidv4）。"""
import httpx
import respx
from fastapi.testclient import TestClient

import gateway.config as _cfg_module
from gateway import enterprise
from gateway.app import app
from gateway.config import settings

client = TestClient(app)
CLIENT = {"X-Client-Token": "test-token"}
settings.gateway_token = "test-token"


def _mock_kbs(payload: list[dict]) -> None:
    settings.weknora_base_url = "http://wk.test"
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(
        return_value=httpx.Response(200, json={"data": payload}))


@respx.mock
def test_kbs_classified_by_dept_prefix():
    _mock_kbs([
        {"id": "1", "name": "101-结构所规范库", "description": "部门规范"},
        {"id": "2", "name": "通用设计资料", "description": ""},
        {"id": "3", "name": "021-岩一院图集"},
    ])
    setattr(_cfg_module.settings, "user_departments", {"51-zhang-kj": ["101", "021"]})
    try:
        resp = client.get("/knowledge/api/kbs", headers={**CLIENT, "X-Client-User": "51-zhang-kj"})
    finally:
        setattr(_cfg_module.settings, "user_departments", {})
    assert resp.status_code == 200
    body = resp.json()["kbs"]
    # 虚拟库 ekp 恒在目录首位；真实库按部门/公共分类在其后。
    assert body[0]["id"] == "ekp" and body[0]["scope"] == "public"
    assert body[1:] == [
        {"id": "weknora:1", "name": "101-结构所规范库", "description": "部门规范", "scope": "dept"},
        {"id": "weknora:2", "name": "通用设计资料", "description": "", "scope": "public"},
        {"id": "weknora:3", "name": "021-岩一院图集", "description": "", "scope": "dept"},
    ]


@respx.mock
def test_kbs_dept_filtered_by_user_mapping():
    _mock_kbs([
        {"id": "1", "name": "202-工程技术研究院"},
        {"id": "2", "name": "101-市场运营部"},
        {"id": "3", "name": "公共制度汇编"},
    ])
    setattr(_cfg_module.settings, "user_departments", {"51-zhang-kj": "202"})
    try:
        # 登记用户：部门分区只剩本部门；公共库不受影响。
        resp = client.get("/knowledge/api/kbs", headers={**CLIENT, "X-Client-User": "51-zhang-kj"})
        assert resp.json()["kbs"][1:] == [
            {"id": "weknora:1", "name": "202-工程技术研究院", "description": "", "scope": "dept"},
            {"id": "weknora:3", "name": "公共制度汇编", "description": "", "scope": "public"},
        ]
        # 未登记用户：看不到任何部门库（首位虚拟库除外）。
        resp = client.get("/knowledge/api/kbs", headers={**CLIENT, "X-Client-User": "someone-else"})
        assert [kb["name"] for kb in resp.json()["kbs"] if kb["id"] != "ekp"] == ["公共制度汇编"]
        # 多部门支持（list 值）。
        setattr(_cfg_module.settings, "user_departments", {"51-zhang-kj": ["202", "101"]})
        resp = client.get("/knowledge/api/kbs", headers={**CLIENT, "X-Client-User": "51-zhang-kj"})
        assert [kb["name"] for kb in resp.json()["kbs"] if kb["scope"] == "dept"] == [
            "202-工程技术研究院", "101-市场运营部"]
    finally:
        setattr(_cfg_module.settings, "user_departments", {})


@respx.mock
def test_kbs_weknora_uuid_ids_are_namespaced():
    # 上游 WeKnora 库 id 本身是 uuidv4；下发必须带 weknora: 前缀，否则客户端按
    # id 形状会把它误判成本地库。
    _mock_kbs([
        {"id": "6f9619ff-8b86-4d01-b42d-00cf4fc964ff", "name": "公共规范库"},
    ])
    resp = client.get("/knowledge/api/kbs", headers=CLIENT)
    assert resp.status_code == 200
    assert resp.json()["kbs"][1:] == [
        {"id": "weknora:6f9619ff-8b86-4d01-b42d-00cf4fc964ff", "name": "公共规范库",
         "description": "", "scope": "public"},
    ]


@respx.mock
def test_kbs_empty_upstream():
    _mock_kbs([])
    resp = client.get("/knowledge/api/kbs", headers=CLIENT)
    assert resp.status_code == 200
    # 上游空列表时目录只剩虚拟库 ekp。
    assert [kb["id"] for kb in resp.json()["kbs"] if kb["id"] != "ekp"] == []


@respx.mock
def test_kbs_weknora_unreachable_returns_empty():
    settings.weknora_base_url = "http://wk.test"
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(
        side_effect=httpx.ConnectError("down"))
    resp = client.get("/knowledge/api/kbs", headers=CLIENT)
    assert resp.status_code == 200
    assert resp.json() == {"kbs": []}


@respx.mock
def test_kbs_malformed_upstream_json_returns_empty():
    settings.weknora_base_url = "http://wk.test"
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(
        return_value=httpx.Response(200, text="<html>"))
    resp = client.get("/knowledge/api/kbs", headers=CLIENT)
    assert resp.status_code == 200
    assert resp.json() == {"kbs": []}


def test_kbs_requires_client_token():
    assert client.get("/knowledge/api/kbs").status_code == 401
    assert client.get("/knowledge/api/kbs", headers={"X-Client-Token": "wrong"}).status_code == 401
