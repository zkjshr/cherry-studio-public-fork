"""知识库对话端点测试：respx 模拟 WeKnora 与 ollama，验证 chunk 流与降级（/v1/kb/chat/completions）。"""
import json
import httpx
import respx
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.config import settings

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}
settings.gateway_token = "test-token"


def _setup_wk():
    settings.weknora_base_url = "http://wk.test"
    # 本文件测流式/降级行为；查询改写（智能管线）不在覆盖范围，关闭以保持请求面不变
    settings.search_rewrite_enabled = False
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "kb1", "name": "101-市场运营部"}]}))
    respx.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"knowledge_id": "d1", "knowledge_title": "关于资质的通知", "knowledge_filename": "x.md", "score": 0.8}]}))


def _setup_ollama_stream(lines: list[str]):
    from gateway.config import settings as s
    s.weknora_base_url = "http://wk.test"
    payload = "".join(f"{ln}\n" for ln in lines).encode()
    respx.post("http://192.168.66.25:11434/api/chat").mock(return_value=httpx.Response(
        200, content=payload, headers={"content-type": "application/x-ndjson"}))


@respx.mock
def test_stream_chunks_openai_format():
    _setup_wk()
    _setup_ollama_stream(['{"message":{"content":"你好"},"done":false}', '{"message":{"content":"！"},"done":false}', '{"done":true}'])
    resp = client.post("/v1/kb/chat/completions?kb_ids=kb1", headers=AUTH, json={
        "model": "tjad-knowledge", "stream": True,
        "messages": [{"role": "user", "content": "资质通知"}]})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
    assert events[-1] == "data: [DONE]"
    contents = []
    for ev in events[:-1]:
        body = json.loads(ev[6:])
        assert body["object"] == "chat.completion.chunk"
        contents.append(body["choices"][0]["delta"].get("content") or "")
    joined = "".join(contents)
    assert "你好" in joined and "！" in joined
    assert "来源：" in joined and "关于资质的通知" in joined


@respx.mock
def test_stream_unavailable_kb():
    settings.weknora_base_url = "http://wk.test"
    respx.get("http://wk.test/api/v1/knowledge-bases").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "kb1", "name": "101-市场运营部"}]}))
    respx.post("http://wk.test/api/v1/knowledge-bases/kb1/hybrid-search").mock(
        return_value=httpx.Response(500))
    resp = client.post("/v1/kb/chat/completions", headers=AUTH, json={
        "stream": True, "messages": [{"role": "user", "content": "资质通知"}]})
    assert resp.status_code == 200
    assert "暂时无法访问" in resp.text and "data: [DONE]" in resp.text


def test_requires_auth():
    settings.gateway_token = "test-token"
    assert client.post("/v1/kb/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401


@respx.mock
def test_non_stream_returns_completion():
    _setup_wk()
    settings.weknora_base_url = "http://wk.test"
    respx.post("http://192.168.66.25:11434/api/chat").mock(return_value=httpx.Response(200, json={
        "message": {"content": "答案正文"}, "done": False}))
    resp = client.post("/v1/kb/chat/completions?kb_ids=kb1", headers=AUTH, json={
        "stream": False, "messages": [{"role": "user", "content": "资质通知"}]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert "答案正文" in body["choices"][0]["message"]["content"]
