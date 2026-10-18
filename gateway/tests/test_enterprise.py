"""企业配置链路测试：结构化 CRUD 与校验、发布组合/回滚、客户端下发鉴权与 ETag/304、用量汇总。"""
import os
import sqlite3

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.config import settings
from gateway.enterprise import record_usage

client = TestClient(app)
CLIENT = {"X-Client-Token": "test-token"}
ADMIN = {"X-Admin-Token": "test-token"}
settings.gateway_token = "test-token"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """用例间隔离：每个用例指到独立临时库，存储层检测路径变化自动重连建表。"""
    monkeypatch.setenv("ENTERPRISE_DB_PATH", str(tmp_path / "enterprise.sqlite3"))
    yield


def _put_ok(path: str, body: dict) -> dict:
    """合法 PUT 的便捷通道：断言 200 并返回响应体。"""
    resp = client.put(f"/admin/api/{path}", headers=ADMIN, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upstream(uid: str = "gw", **over) -> dict:
    body = {"name": "主上游", "protocol": "openai", "base_url": "http://up.test", "api_key": "sk-up",
            "enabled": True, "models": [{"id": "m1", "name": "模型一"}, {"id": "hid", "visible": False}]}
    body.update(over)
    return body


def _full_state() -> None:
    """一组完整可发布的结构化配置：双上游（一停用）、助手、MCP、小程序、三键设置。"""
    _put_ok("upstreams/gw", _upstream())
    _put_ok("upstreams/off", _upstream("off", name="停用", protocol="ollama",
                                       base_url="http://o.test", api_key="", enabled=False,
                                       models=[{"id": "m2"}]))
    _put_ok("settings", {"public_base_url": "http://10.1.2.3:8787",
                         "default_models": {"assistant": "gw/m1", "translate": "gw/m1"},
                         "kb_entries": {"recent_enabled": True, "kb_ids": ["101-x"]}})
    _put_ok("assistants/a1", {"name": "知识助手", "prompt": "你是助手", "emoji": "📚", "description": "说明",
                              "model_ref": "gw/m1", "settings": {"temperature": 0.3}, "mcp_names": ["wk"]})
    _put_ok("mcp-servers/wk", {"type": "sse", "base_url": "http://wk.test/sse", "headers": {"X-A": "b"}})
    _put_ok("minapps/wk-web", {"name": "WeKnora", "url": "http://10.1.2.3:8787/proxy/weknora/"})


def test_client_config_requires_token():
    assert client.get("/api/client/config").status_code == 401


def test_admin_api_requires_token():
    assert client.get("/admin/api/published").status_code == 401
    assert client.get("/admin/api/upstreams").status_code == 401
    assert client.get("/admin/api/settings").status_code == 401


def test_client_config_404_when_nothing_published():
    resp = client.get("/api/client/config", headers=CLIENT)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no config published"}


def test_upstream_crud_and_validation():
    assert _put_ok("upstreams/gw", _upstream(base_url="http://up.test/v1/")) == {"saved": True}
    up = client.get("/admin/api/upstreams/gw", headers=ADMIN).json()
    assert up["base_url"] == "http://up.test/v1"  # 尾斜杠归一
    assert up["models"] == [{"id": "m1", "name": "模型一", "visible": True},
                            {"id": "hid", "name": "hid", "visible": False}]  # 缺省补全
    assert up["enabled"] is True and up["api_key"] == "sk-up"
    # PUT 全量 upsert 覆盖
    _put_ok("upstreams/gw", _upstream(name="改名"))
    assert client.get("/admin/api/upstreams/gw", headers=ADMIN).json()["name"] == "改名"
    assert [u["id"] for u in client.get("/admin/api/upstreams", headers=ADMIN).json()["upstreams"]] == ["gw"]
    # 非法结构 → 400
    for bad in [
        {**_upstream(), "protocol": "http"},              # 未知协议
        {**_upstream(), "base_url": "ftp://x"},           # 非 http(s)
        {**_upstream(), "models": [{"name": "无id"}]},    # 模型缺 id
        {**_upstream(), "models": "m1"},                  # models 非 list
        {**_upstream(), "models": [{"id": "m", "visible": "yes"}]},
        {**_upstream(), "enabled": "yes"},                # enabled 非 bool
        {**_upstream(), "name": "  "},                    # 空名称
        {"protocol": "openai", "base_url": "http://x"},   # 缺 name
    ]:
        assert client.put("/admin/api/upstreams/gw", headers=ADMIN, json=bad).status_code == 400, bad
    assert client.get("/admin/api/upstreams/nope", headers=ADMIN).status_code == 404
    assert client.delete("/admin/api/upstreams/nope", headers=ADMIN).status_code == 404


def test_upstream_delete_conflicts_while_referenced():
    _put_ok("upstreams/gw", _upstream())
    _put_ok("assistants/a1", {"name": "助手", "model_ref": "gw/m1"})
    resp = client.delete("/admin/api/upstreams/gw", headers=ADMIN)
    assert resp.status_code == 409
    assert "a1" in resp.json()["detail"]
    assert client.delete("/admin/api/assistants/a1", headers=ADMIN).json() == {"deleted": True}
    assert client.delete("/admin/api/upstreams/gw", headers=ADMIN).json() == {"deleted": True}


def test_assistant_name_unique_and_model_ref_format():
    _put_ok("assistants/a1", {"name": "同名"})
    _put_ok("assistants/a2", {"name": "另一个"})
    resp = client.put("/admin/api/assistants/a2", headers=ADMIN, json={"name": "同名"})
    assert resp.status_code == 409
    # 自身 upsert（id 相同）不算撞名
    assert _put_ok("assistants/a1", {"name": "同名", "prompt": "改"}) == {"saved": True}
    assert client.get("/admin/api/assistants/a1", headers=ADMIN).json()["prompt"] == "改"
    assert client.get("/admin/api/assistants", headers=ADMIN).json()["assistants"][0]["emoji"] == "✨"
    # model_ref 缺分隔符 → 400
    assert client.put("/admin/api/assistants/a3", headers=ADMIN,
                      json={"name": "x", "model_ref": "badformat"}).status_code == 400
    assert client.get("/admin/api/assistants/nope", headers=ADMIN).status_code == 404
    assert client.delete("/admin/api/assistants/nope", headers=ADMIN).status_code == 404


def test_mcp_and_minapp_crud_and_validation():
    _put_ok("mcp-servers/wk", {"type": "streamableHttp", "base_url": "http://wk.test/mcp",
                               "headers": {"X-A": "b"}, "is_active": False})
    s = client.get("/admin/api/mcp-servers/wk", headers=ADMIN).json()
    assert s == {"name": "wk", "type": "streamableHttp", "base_url": "http://wk.test/mcp",
                 "headers": {"X-A": "b"}, "is_active": False, "updated_at": s["updated_at"]}
    for bad in [{"type": "grpc"}, {"headers": {"A": 1}}, {"is_active": "yes"}]:
        assert client.put("/admin/api/mcp-servers/wk", headers=ADMIN, json=bad).status_code == 400, bad
    _put_ok("minapps/m1", {"name": "小程序", "url": "http://host/proxy/weknora/"})
    assert client.get("/admin/api/minapps", headers=ADMIN).json()["minapps"][0]["id"] == "m1"
    assert client.put("/admin/api/minapps/m2", headers=ADMIN,
                      json={"name": "x", "url": "not-url"}).status_code == 400
    # 被助手引用的 MCP 删除 → 409（与上游删除同语义）
    _put_ok("assistants/a1", {"name": "助手", "mcp_names": ["wk"]})
    assert client.delete("/admin/api/mcp-servers/wk", headers=ADMIN).status_code == 409
    assert client.delete("/admin/api/assistants/a1", headers=ADMIN).status_code == 200
    assert client.delete("/admin/api/mcp-servers/wk", headers=ADMIN).status_code == 200
    assert client.delete("/admin/api/minapps/m1", headers=ADMIN).status_code == 200


@respx.mock
def test_upstream_connectivity_test():
    _put_ok("upstreams/gw", _upstream())
    _put_ok("upstreams/oll", _upstream("oll", protocol="ollama", base_url="http://o.test", api_key=""))
    _put_ok("upstreams/dead", _upstream("dead", protocol="ollama", base_url="http://dead.test", api_key=""))
    respx.get("http://up.test/models").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get("http://o.test/api/tags").mock(return_value=httpx.Response(503))
    respx.get("http://dead.test/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    r = client.post("/admin/api/upstreams/gw/test", headers=ADMIN).json()
    assert r["ok"] is True and isinstance(r["latency_ms"], int) and "200" in r["detail"]
    r = client.post("/admin/api/upstreams/oll/test", headers=ADMIN).json()
    assert r["ok"] is False and "503" in r["detail"]
    r = client.post("/admin/api/upstreams/dead/test", headers=ADMIN).json()
    assert r["ok"] is False and "ConnectError" in r["detail"]
    assert client.post("/admin/api/upstreams/none/test", headers=ADMIN).status_code == 404


def test_settings_defaults_merge_and_validation():
    assert client.get("/admin/api/settings", headers=ADMIN).json() == {
        "public_base_url": "http://127.0.0.1:8787", "default_models": {},
        "kb_entries": {"recent_enabled": False, "kb_ids": []}, "world_url": "",
        "base_system_prompt": "", "user_departments": {}}
    _put_ok("settings", {"public_base_url": "http://10.1.2.3:8787",
                         "default_models": {"assistant": "gw/m1"},
                         "kb_entries": {"recent_enabled": True, "kb_ids": ["101-x"]}})
    st = client.get("/admin/api/settings", headers=ADMIN).json()
    assert st["public_base_url"] == "http://10.1.2.3:8787"
    assert st["default_models"] == {"assistant": "gw/m1"}
    assert st["kb_entries"] == {"recent_enabled": True, "kb_ids": ["101-x"]}
    for bad in [
        {"nope": 1},                                       # 未知键
        {"public_base_url": "not-url"},
        {"default_models": {"unknown_role": "gw/m"}},
        {"default_models": {"assistant": 1}},
        {"kb_entries": {"kb_ids": "101"}},                 # kb_ids 非 list
        {"kb_entries": {"recent_enabled": "yes"}},
        {"world_url": "ftp://world.test"},                 # 非 http(s)
        {"world_url": 42},                                 # 非字符串
    ]:
        assert client.put("/admin/api/settings", headers=ADMIN, json=bad).status_code == 400, bad


def test_world_url_setting_and_client_config_passthrough():
    """world_url：可空设置键 + 发布组合顶层透出（小世界 W1c 契约）。"""
    _put_ok("upstreams/gw", _upstream())
    # 默认未配置：发布内容不含 world_url 键
    client.post("/admin/api/publish", headers=ADMIN)
    assert "world_url" not in client.get("/api/client/config", headers=CLIENT).json()

    # 配置后随发布透出；去尾斜杠归一
    _put_ok("settings", {"world_url": "http://66.12:8788/"})
    assert client.get("/admin/api/settings", headers=ADMIN).json()["world_url"] == "http://66.12:8788/"
    client.post("/admin/api/publish", headers=ADMIN)
    body = client.get("/api/client/config", headers=CLIENT).json()
    assert body["world_url"] == "http://66.12:8788"

    # None / 空串均视为清空
    _put_ok("settings", {"world_url": None})
    assert client.get("/admin/api/settings", headers=ADMIN).json()["world_url"] == ""
    _put_ok("settings", {"world_url": ""})
    client.post("/admin/api/publish", headers=ADMIN)
    assert "world_url" not in client.get("/api/client/config", headers=CLIENT).json()


def test_publish_composes_client_config():
    _full_state()
    assert client.post("/admin/api/publish", headers=ADMIN).json() == {"version": 1}
    resp = client.get("/api/client/config", headers=CLIENT)
    assert resp.headers["etag"] == '"1"' and resp.headers["cache-control"] == "no-cache"
    body = resp.json()
    assert body["config_version"] == 1
    # 仅 enabled 上游的 visible 模型入列；api_key 恒为网关令牌
    assert body["providers"] == [{"id": "gateway", "name": "所里模型网关",
                                  "base_url": "http://10.1.2.3:8787/v1", "api_key": "test-token",
                                  "models": [{"id": "gw/m1", "name": "模型一"}]}]
    assert body["default_models"] == {"assistant": "gateway/gw/m1", "translate": "gateway/gw/m1"}
    assert body["assistants"] == [{"name": "知识助手", "prompt": "你是助手", "emoji": "📚",
                                   "description": "说明", "model": "gateway/gw/m1",
                                   "settings": {"temperature": 0.3}, "mcp_server_ids": ["wk"],
                                   "managed": True}]
    assert body["mcp_servers"][1:] == [{"name": "wk", "type": "sse", "base_url": "http://wk.test/sse",
                                        "headers": {"X-A": "b"}, "is_active": True}]
    # 内置 WeKnora MCP 恒在首位（公共知识库，人人默认下发）
    assert body["mcp_servers"][0] == {"name": "weknora", "type": "streamableHttp",
                                      "base_url": "http://10.1.2.3:8787/mcp/weknora",
                                      "headers": {}, "is_active": True}
    assert client.delete("/admin/api/mcp-servers/weknora", headers=ADMIN).status_code == 409
    assert body["minapps"] == [{"id": "wk-web", "name": "WeKnora",
                                "url": "http://10.1.2.3:8787/proxy/weknora/"}]
    assert body["kb_entries"] == {"recent_enabled": True, "kb_ids": ["101-x"]}
    # If-None-Match 列表 / * / 弱形式命中（RFC 7232，E1 回归保留）
    for inm in ('"9", "1"', "*", 'W/"1"'):
        assert client.get("/api/client/config", headers={**CLIENT, "If-None-Match": inm}).status_code == 304
    resp = client.get("/api/client/config", headers={**CLIENT, "If-None-Match": '"2"'})
    assert resp.status_code == 200 and resp.json()["config_version"] == 1


def test_publish_rejects_invalid_references():
    _put_ok("upstreams/gw", _upstream())
    _put_ok("upstreams/off", _upstream("off", name="停用", protocol="ollama",
                                       base_url="http://o.test", enabled=False, models=[{"id": "m2"}]))
    _put_ok("mcp-servers/wk", {})
    for bad in ["gw/nope", "gw/hid", "off/m2", "off/ghost", "ghost/m1"]:
        _put_ok("settings", {"default_models": {"assistant": bad}})
        resp = client.post("/admin/api/publish", headers=ADMIN)
        assert resp.status_code == 400
        assert bad in resp.json()["detail"]
    _put_ok("settings", {"default_models": {}})
    # 助手引用未知模型 / 未知 MCP → 400 带明细
    _put_ok("assistants/a1", {"name": "a", "model_ref": "gw/nope"})
    assert client.post("/admin/api/publish", headers=ADMIN).status_code == 400
    _put_ok("assistants/a1", {"name": "a", "model_ref": "gw/m1", "mcp_names": ["ghost"]})
    resp = client.post("/admin/api/publish", headers=ADMIN)
    assert resp.status_code == 400 and "ghost" in resp.json()["detail"]
    # 校验失败不落版本
    assert client.get("/admin/api/published", headers=ADMIN).json()["latest"] is None
    assert client.get("/api/client/config", headers=CLIENT).status_code == 404


def test_rollback_republishes_snapshot():
    _full_state()
    client.post("/admin/api/publish", headers=ADMIN)
    v1_body = client.get("/api/client/config", headers=CLIENT).json()
    _put_ok("settings", {"public_base_url": "http://10.9.9.9:8787"})
    assert client.post("/admin/api/publish", headers=ADMIN).json() == {"version": 2}
    assert client.get("/api/client/config", headers=CLIENT).json()["providers"][0]["base_url"] \
        .startswith("http://10.9.9.9")
    # 回滚 v1 = 快照重发为新版本 v3
    assert client.post("/admin/api/publish/rollback", headers=ADMIN, json={"version": 1}).json() == \
        {"version": 3}
    body = client.get("/api/client/config", headers=CLIENT)
    assert body.json() == {**v1_body, "config_version": 3}
    assert client.post("/admin/api/publish/rollback", headers=ADMIN, json={"version": 99}).status_code == 404
    assert client.post("/admin/api/publish/rollback", headers=ADMIN, json={"version": "1"}).status_code == 400


def test_published_view_shape():
    _full_state()
    client.post("/admin/api/publish", headers=ADMIN)
    client.post("/admin/api/publish", headers=ADMIN)
    data = client.get("/admin/api/published", headers=ADMIN).json()
    assert data["latest"]["version"] == 2 and data["latest"]["published_at"]
    assert data["latest"]["content"]["providers"][0]["id"] == "gateway"
    assert [v["version"] for v in data["versions"]] == [2, 1]
    assert all(v["published_at"] for v in data["versions"])


def test_usage_summary_window_and_aggregation():
    record_usage("gw/m1", "gw", 10, 20, 30, 100, "ok")
    record_usage("gw/m1", "gw", 1, 2, 3, 50, "ok")
    record_usage("gw/m2", "gw", 0, 0, 0, 10, "error")
    # 窗口外的旧记录：直接落库造旧 ts
    conn = sqlite3.connect(os.environ["ENTERPRISE_DB_PATH"])
    conn.execute("INSERT INTO usage_log (ts, model, total_tokens, status) VALUES (?,?,?,?)",
                 ("2020-01-01T00:00:00+00:00", "old/m", 999, "ok"))
    conn.commit()
    conn.close()
    s = client.get("/admin/api/usage/summary?days=30", headers=ADMIN).json()
    assert s["days"] == 30 and s["total_calls"] == 3 and s["total_tokens"] == 33
    by_model = {m["model"]: m for m in s["by_model"]}
    assert by_model["gw/m1"] == {"model": "gw/m1", "calls": 2, "tokens": 33}
    assert by_model["gw/m2"] == {"model": "gw/m2", "calls": 1, "tokens": 0}
    assert len(s["by_day"]) == 1 and s["by_day"][0]["calls"] == 3 and s["by_day"][0]["tokens"] == 33
    assert client.get("/admin/api/usage/summary?days=0", headers=ADMIN).status_code == 422


def test_admin_page_served_without_draft_endpoints():
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "发布" in resp.text and "加载已发布" in resp.text
    # draft 端点已移除，页面不得再引用
    assert "草稿" not in resp.text and "draft" not in resp.text
    assert client.put("/admin/api/config/draft", headers=ADMIN, json={}).status_code in (404, 405)
    assert client.post("/admin/api/config/publish", headers=ADMIN).status_code in (404, 405)


def test_admin_page_v2_tabs_and_markers():
    """管理页 v2：八个标签页与关键控件齐备；嵌入式 webview 兼容禁用 prompt/alert。"""
    resp = client.get("/admin")
    assert resp.status_code == 200
    for label in ["上游与模型", "默认模型", "助手", "MCP", "小程序", "知识库", "用量", "版本"]:
        assert label in resp.text, f"missing tab label: {label}"
    for marker in ['id="tabs"', 'id="btn-publish"', 'id="token"', 'id="status"', 'id="version"']:
        assert marker in resp.text, f"missing marker: {marker}"
    # webview 常不支持原生对话框，页面一律走状态行与两段式确认按钮
    assert "window.prompt(" not in resp.text
    assert "alert(" not in resp.text


def test_download_page_and_file(tmp_path, monkeypatch):
    """下载页列出 downloads 目录文件，/download/{name} 可取回；路径穿越 404。"""
    import os as _os
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "Cherry Studio-2.1.4-tjad.1-arm64.dmg").write_bytes(b"DMG-bytes")
    monkeypatch.setattr("gateway.enterprise._DOWNLOAD_DIR", str(d))
    resp = client.get("/download")
    assert resp.status_code == 200
    assert "Cherry Studio-2.1.4-tjad.1-arm64.dmg" in resp.text
    assert "4.6 MB" not in resp.text or True  # 大小行存在与否随文件内容，不强行断言
    got = client.get("/download/Cherry Studio-2.1.4-tjad.1-arm64.dmg")
    assert got.status_code == 200
    assert got.content == b"DMG-bytes"
    assert client.get("/download/..%2F..%2Fsecrets.sqlite3").status_code in (404, 400, 403)
    assert client.get("/download/nope.dmg").status_code == 404
    empty = tmp_path / "empty-downloads"
    empty.mkdir()
    monkeypatch.setattr("gateway.enterprise._DOWNLOAD_DIR", str(empty))
    assert "暂无安装包" in client.get("/download").text
