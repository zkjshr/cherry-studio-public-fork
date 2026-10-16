import json

import httpx
import respx
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.config import settings

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}
settings.gateway_token = "test-token"

# 以下 mock 均按真实实例（10.137.200.58:8091）实测响应形态：
# knowledge/search 与 hybrid-search 的字段结构、X-API-Key 鉴权头。


def test_health_no_auth():
    assert client.get("/api/health").json() == {"status": "ok"}


@respx.mock
def test_instant_proxies_keyword():
    respx.get("http://wk.test/api/v1/knowledge/search").mock(return_value=httpx.Response(200, json={
        "success": True, "data": [{"id": "k1", "knowledge_base_id": "kb1", "knowledge_base_name": "101-市场运营部", "title": "关于X的通知", "file_name": "x.md", "updated_at": "2026-09-28T10:00:00Z"}], "has_more": False, "total": 1}))
    from gateway.config import settings as s
    s.weknora_base_url = "http://wk.test"
    resp = client.get("/api/instant?q=X", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()["results"][0]
    assert body["kb_name"] == "101-市场运营部"
    assert body["updated_at"] == "2026-09-28T10:00:00Z"
    req = respx.get("http://wk.test/api/v1/knowledge/search").calls.last.request
    assert "keyword=X" in str(req.url)


def test_auth_required():
    settings.gateway_token = "test-token"
    assert client.get("/api/instant?q=X").status_code == 401


@respx.mock
def test_deep_flat_chunks_and_kb_name_fill():
    """hybrid-search 平铺 chunk 字段（无 kb_name）→ 网关映射并经库列表回填 kb_name。"""
    from gateway.config import settings as s
    s.weknora_base_url = "http://wk.test"
    respx.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"knowledge_id": "d1", "knowledge_base_id": "kb1", "knowledge_title": "关于X的通知", "knowledge_filename": "x.md", "score": 0.8},
            {"knowledge_id": "d2", "knowledge_base_id": "kb1", "knowledge_title": "另一篇", "knowledge_filename": "y.md", "score": 0.5},
        ]}))
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "kb1", "name": "103-科技质量部"}]}))
    resp = client.get("/api/deep?q=X&kb_ids=kb1", headers=AUTH)
    body = resp.json()
    assert body["partial"] is False
    top = body["results"][0]
    assert top["id"] == "d1:0" and top["kb_name"] == "103-科技质量部" and top["score"] == 1.0


@respx.mock
def test_deep_partial_on_kb_failure():
    from gateway.config import settings as s
    s.weknora_base_url = "http://wk.test"
    respx.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"knowledge_id": "d1", "knowledge_title": "t", "knowledge_filename": "f", "score": 0.8}]}))
    respx.post("http://wk.test/api/v1/knowledge-bases/kb2/hybrid-search").mock(
        return_value=httpx.Response(500))
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={"data": []}))
    resp = client.get("/api/deep?q=X&kb_ids=kb1,kb2", headers=AUTH)
    body = resp.json()
    assert body["partial"] is True and body["results"][0]["id"] == "d1:0"


@respx.mock
def test_recent_proxies_recent_mode():
    from gateway.config import settings as s
    s.weknora_base_url = "http://wk.test"
    route = respx.get("http://wk.test/api/v1/knowledge/search").mock(return_value=httpx.Response(200, json={
        "success": True, "data": [{"id": "r1", "knowledge_base_id": "kb1", "knowledge_base_name": "101-市场运营部", "title": "最新通知", "file_name": "n.md", "updated_at": None}], "has_more": False, "total": 1}))
    resp = client.get("/api/recent?limit=30", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["results"][0]["title"] == "最新通知"
    assert "recent=true" in str(route.calls.last.request.url) and "limit=30" in str(route.calls.last.request.url)


def test_deep_per_kb_timeout_keeps_partial(monkeypatch):
    """单库超过预算被取消，响应仍 200 且只含快库结果（partial=True）。

    预算经 monkeypatch 缩短：用例不断言、也不依赖 DEEP_PER_KB_TIMEOUT 的具体值。
    """
    import asyncio

    from gateway import app as app_mod
    from gateway import weknora

    monkeypatch.setattr(app_mod, "DEEP_PER_KB_TIMEOUT", 0.05)

    async def fake_kb_hybrid_search(client, kb_id, q, limit):
        if kb_id == "kb1":  # 慢库：sleep 可被 wait_for 超时
            await asyncio.sleep(5)
            return [{"id": "slow", "kb_id": kb_id, "kb_name": "", "title": "s", "file_name": "", "updated_at": None, "score": 0.9}]
        return [{"id": "fast", "kb_id": kb_id, "kb_name": "", "title": "f", "file_name": "", "updated_at": None, "score": 0.8}]

    monkeypatch.setattr(weknora, "kb_hybrid_search", fake_kb_hybrid_search)
    resp = client.get("/api/deep?q=X&kb_ids=kb1,kb2", headers=AUTH)
    body = resp.json()
    assert resp.status_code == 200
    assert body["partial"] is True
    assert [r["id"] for r in body["results"]] == ["fast"]


# ---------- /api/chat/stream（SSE，检索+ollama 编排）----------

@respx.mock
def test_chat_stream_default_fans_dept_kbs_and_streams(monkeypatch):
    """kb_ids 为空 → 按 ^\\d{3}- 部门库规范选库（非规范库不扇出），ollama 流式转发至 done。"""
    from gateway import app as app_mod
    from gateway.chat import parse_sse_chunk
    from gateway.config import settings as s

    s.weknora_base_url = "http://wk.test"
    monkeypatch.setattr(app_mod, "OLLAMA_BASE", "http://ollama.test")
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "kb1", "name": "101-市场运营部"}, {"id": "kbx", "name": "TJAD共建数据源"}]}))
    route_dept = respx.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"knowledge_id": "d1", "knowledge_title": "关于资质的通知", "knowledge_filename": "a.md", "score": 0.9}]}))
    route_ollama = respx.post("http://ollama.test/api/chat").mock(return_value=httpx.Response(200, content=(
        '{"message":{"content":"你好"},"done":false}\n{"done":true}\n')))
    # 非规范库故意不 mock：若被误扇出会打到真实网络触发 AllMockedAssertionError
    resp = client.get("/api/chat/stream?q=zizhi", headers=AUTH)
    assert resp.status_code == 200
    events = parse_sse_chunk(resp.text)
    assert route_dept.called is True
    assert json.loads(events[0][1]) == {"text": "正在检索 1 个部门知识库…"}
    assert any(e == "sources" and any(x["kb_name"] == "101-市场运营部" for x in json.loads(d)["sources"]) for e, d in events)
    assert any(e == "delta" and json.loads(d)["text"] == "你好" for e, d in events)
    assert events[-1][0] == "done" and json.loads(events[-1][1]) == {"partial": False}
    # build_prompt 经端点正确接线：system 开头、检索块+query 在 user 消息里
    body = json.loads(route_ollama.calls.last.request.content)
    assert body["messages"][0]["role"] == "system"
    assert "关于资质的通知" in body["messages"][-1]["content"] and "zizhi" in body["messages"][-1]["content"]


@respx.mock
def test_chat_stream_fallback_all_kbs_when_no_dept_match(monkeypatch):
    """无 ^\\d{3}- 部门库 → 回退全部库前 12（此处 2 库全扇出），0 命中走 partial 兜底。"""
    from gateway import app as app_mod
    from gateway.chat import parse_sse_chunk
    from gateway.config import settings as s

    s.weknora_base_url = "http://wk.test"
    monkeypatch.setattr(app_mod, "OLLAMA_BASE", "http://ollama.test")
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "kbx", "name": "TJAD共建数据源"}, {"id": "kby", "name": "TJAD既有数据源"}]}))
    respx.post("http://wk.test/api/v1/knowledge-bases/kbx/hybrid-search").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.post("http://wk.test/api/v1/knowledge-bases/kby/hybrid-search").mock(return_value=httpx.Response(200, json={"data": []}))
    resp = client.get("/api/chat/stream?q=x", headers=AUTH)
    assert resp.status_code == 200
    events = parse_sse_chunk(resp.text)
    assert json.loads(events[0][1]) == {"text": "正在检索 2 个部门知识库…"}
    assert any(e == "delta" and json.loads(d)["text"] == "知识库暂时没有检索到相关内容。" for e, d in events)
    assert events[-1][0] == "done" and json.loads(events[-1][1]) == {"partial": True}


@respx.mock
def test_chat_stream_all_kb_failures_reported_as_unreachable(monkeypatch):
    """检索全败 ≠ 0 命中：全部库失败时如实报"不可达"（spec §4），不发 sources。

    部分失败仍走常规 partial 语义；只有 ids 非空且全部库 ok=False 才走本分支。
    """
    from gateway import app as app_mod
    from gateway.chat import parse_sse_chunk
    from gateway.config import settings as s

    s.weknora_base_url = "http://wk.test"
    monkeypatch.setattr(app_mod, "OLLAMA_BASE", "http://ollama.test")
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "kb1", "name": "101-市场运营部"}, {"id": "kb2", "name": "103-科技质量部"}]}))
    respx.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(return_value=httpx.Response(500))
    respx.post("http://wk.test/api/v1/knowledge-bases/kb2/hybrid-search").mock(return_value=httpx.Response(500))
    resp = client.get("/api/chat/stream?q=x", headers=AUTH)
    assert resp.status_code == 200
    events = parse_sse_chunk(resp.text)
    assert any(e == "status" and json.loads(d)["text"] == "知识库暂时无法访问，请稍后重试" for e, d in events)
    assert any(e == "delta" and "知识库暂时无法访问" in json.loads(d)["text"] for e, d in events)
    assert not any(e == "sources" for e, d in events)
    assert events[-1][0] == "done" and json.loads(events[-1][1]) == {"partial": True}


@respx.mock
def test_chat_stream_sources_diverse_across_kbs(monkeypatch):
    """跨库 score 不可比：小库高分噪声不得淹没相关部门库命中（按库轮转取优）。"""
    from gateway import app as app_mod
    from gateway.chat import parse_sse_chunk
    from gateway.config import settings as s

    s.weknora_base_url = "http://wk.test"
    monkeypatch.setattr(app_mod, "OLLAMA_BASE", "http://ollama.test")
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "kbhi", "name": "004-团委"}, {"id": "kbrel", "name": "101-市场运营部"}]}))
    # 小库噪声：8 条高分 chunk（旧全局排序会独占 top8）
    respx.post("http://wk.test/api/v1/knowledge-bases/kbhi/hybrid-search").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"knowledge_id": f"n{i}", "knowledge_title": "摄影大赛征稿通知", "knowledge_filename": "p.md", "score": 0.48 - i * 0.001} for i in range(8)]}))
    # 部门库真相关：低分但相关
    respx.post("http://wk.test/api/v1/knowledge-bases/kbrel/hybrid-search").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"knowledge_id": "r1", "knowledge_title": "关于资质的通知", "knowledge_filename": "a.md", "score": 0.016}]}))
    respx.post("http://ollama.test/api/chat").mock(return_value=httpx.Response(200, content=(
        '{"message":{"content":"ok"},"done":false}\n{"done":true}\n')))
    resp = client.get("/api/chat/stream?q=zizhi", headers=AUTH)
    assert resp.status_code == 200
    events = parse_sse_chunk(resp.text)
    src_event = next(d for e, d in events if e == "sources")
    titles = [x["title"] for x in json.loads(src_event)["sources"]]
    assert "关于资质的通知" in titles  # 全局按分排序下该断言失败
    assert "摄影大赛征稿通知" in titles
    # 标题含查询词的库优先：q=资质 时 101 的相关文档排到首位（噪声库 0.48 仍第二）
    from urllib.parse import quote
    resp2 = client.get(f"/api/chat/stream?q={quote('资质')}", headers=AUTH)
    events2 = parse_sse_chunk(resp2.text)
    titles2 = [x["title"] for x in json.loads(next(d for e, d in events2 if e == "sources"))["sources"]]
    assert titles2[0] == "关于资质的通知"


@respx.mock
def test_chat_stream_bad_history_shape_is_reset(monkeypatch):
    """history 为合法 JSON 但形状非法（元素缺 role/content）→ 整体置空，流不 500。"""
    from urllib.parse import quote

    from gateway import app as app_mod
    from gateway.chat import parse_sse_chunk
    from gateway.config import settings as s

    s.weknora_base_url = "http://wk.test"
    monkeypatch.setattr(app_mod, "OLLAMA_BASE", "http://ollama.test")
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "kb1", "name": "101-市场运营部"}]}))
    respx.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"knowledge_id": "d1", "knowledge_title": "t", "knowledge_filename": "f", "score": 0.9}]}))
    route_ollama = respx.post("http://ollama.test/api/chat").mock(return_value=httpx.Response(200, content=(
        '{"message":{"content":"ok"},"done":false}\n{"done":true}\n')))
    bad = quote(json.dumps([{"role": "user"}]))  # 缺 content
    resp = client.get(f"/api/chat/stream?q=x&history={bad}", headers=AUTH)
    assert resp.status_code == 200
    events = parse_sse_chunk(resp.text)
    assert events[-1][0] == "done" and json.loads(events[-1][1]) == {"partial": False}
    body = json.loads(route_ollama.calls.last.request.content)
    assert len(body["messages"]) == 2  # system + 检索 user，坏 history 未混入


def test_chat_stream_ollama_first_token_timeout_degrades(monkeypatch):
    """ollama 首 token 超硬顶（测试缩为 0.05s）→ 降级 partial=true，流正常收尾不挂死。

    用 with 局部 router 且 assert_all_called=False：被取消的 side_effect 不会记入 calls。
    """
    import asyncio

    from gateway import app as app_mod
    from gateway.chat import parse_sse_chunk
    from gateway.config import settings as s

    s.weknora_base_url = "http://wk.test"
    monkeypatch.setattr(app_mod, "OLLAMA_BASE", "http://ollama.test")
    monkeypatch.setattr(app_mod, "OLLAMA_FIRST_TOKEN_TIMEOUT", 0.05)
    with respx.mock(assert_all_called=False) as rkm:
        rkm.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
            "data": [{"id": "kb1", "name": "101-市场运营部"}]}))
        rkm.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(
            return_value=httpx.Response(200, json={"success": True, "data": [
                {"knowledge_id": "d1", "knowledge_title": "t", "knowledge_filename": "f", "score": 0.9}]}))

        async def slow_ollama(request):
            await asyncio.sleep(2)
            return httpx.Response(200, content='{"message":{"content":"late"},"done":false}\n')

        rkm.post("http://ollama.test/api/chat").mock(side_effect=slow_ollama)
        resp = client.get("/api/chat/stream?q=x", headers=AUTH)
    assert resp.status_code == 200
    events = parse_sse_chunk(resp.text)
    assert any(e == "delta" and "生成服务暂不可用" in json.loads(d)["text"] for e, d in events)
    assert events[-1][0] == "done" and json.loads(events[-1][1]) == {"partial": True}
