"""kb_hybrid_search 必须透传 WeKnora chunk 的 content 字段（agent RAG 需要正文，不能只有标题）。"""
import httpx
import pytest

from gateway import weknora


def _fake_transport(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_hybrid_search_passes_content_through(monkeypatch):
    payload = {"data": [{"knowledge_id": "k1", "knowledge_title": "通知A",
                         "knowledge_filename": "a.md", "score": 0.9,
                         "content": "第一条正文段落"}]}
    transport = _fake_transport(payload)

    async def fake_post(self, url, **kwargs):
        request = httpx.Request("POST", url, **kwargs)
        response = transport.handle_request(request)
        response.request = request
        return response

    monkeypatch.setattr(weknora.httpx.AsyncClient, "post", fake_post)
    async with httpx.AsyncClient() as client:
        out = await weknora.kb_hybrid_search(client, "kb-1", "资质", 3)
    assert out[0]["content"] == "第一条正文段落"


@pytest.mark.asyncio
async def test_hybrid_search_content_defaults_empty(monkeypatch):
    payload = {"data": [{"knowledge_id": "k1", "knowledge_title": "通知A",
                         "knowledge_filename": "a.md", "score": 0.9}]}
    transport = _fake_transport(payload)

    async def fake_post(self, url, **kwargs):
        request = httpx.Request("POST", url, **kwargs)
        response = transport.handle_request(request)
        response.request = request
        return response

    monkeypatch.setattr(weknora.httpx.AsyncClient, "post", fake_post)
    async with httpx.AsyncClient() as client:
        out = await weknora.kb_hybrid_search(client, "kb-1", "资质", 3)
    assert out[0]["content"] == ""
