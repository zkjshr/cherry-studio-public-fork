# -*- coding: utf-8 -*-
"""智能检索管线：查询改写（4B ollama）+ rerank（siliconflow，key 门控）+ retrieve 集成。"""
import httpx
import pytest

from gateway import kb_search, weknora
from gateway.config import settings


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """隔离全局 settings（openai_compat 等既有测试会改 weknora_base_url 不还原）。"""
    monkeypatch.setattr(settings, "weknora_base_url", "http://wk.test")
    monkeypatch.setattr(settings, "search_rewrite_enabled", True)
    monkeypatch.setattr(settings, "search_rewrite_model", "qwen3.5:4b")
    monkeypatch.setattr(settings, "search_ollama_url", "http://10.121.16.5:11434")
    monkeypatch.setattr(settings, "siliconflow_rerank_key", "sk-test")


def _transport(handler):
    return httpx.MockTransport(handler)


def _make_client(handler):
    return httpx.AsyncClient(transport=_transport(handler))


def _route(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    body = request.read().decode()
    if "10.121.16.5:11434/api/chat" in url:
        return httpx.Response(200, json={"message": {"content": "停车 申请 流程"}})
    if "siliconflow.cn/v1/rerank" in url:
        docs = httpx.Request("POST", url, content=body)  # noqa: F841
        import json as _json
        payload = _json.loads(body)
        n = len(payload["documents"])
        # index 越大越相关（重排逻辑自己负责排序）
        return httpx.Response(200, json={"results": [
            {"index": i, "relevance_score": i} for i in range(n)
        ]})
    if "hybrid-search" in url:
        kb = url.split("/knowledge-bases/")[1].split("/")[0]
        return httpx.Response(200, json={"data": [
            {"id": f"{kb}-c1", "knowledge_id": f"{kb}-d1", "knowledge_title": "t",
             "knowledge_filename": "f", "score": 0.5, "content": f"{kb} 正文", "chunk_index": 0}
        ]})
    if "/api/v1/knowledge-bases" in url:
        return httpx.Response(200, json={"data": [
            {"id": "kb-a", "name": "103-科技质量部"},
            {"id": "kb-b", "name": "102-综合管理部"},
        ]})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_rewrite_success():
    async with _make_client(_route) as client:
        out = await kb_search.rewrite_query(client, "它怎么申请")
    assert out == "停车 申请 流程"


@pytest.mark.asyncio
async def test_rewrite_failure_falls_back():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))
    )) as client:
        out = await kb_search.rewrite_query(client, "原始查询")
    assert out == "原始查询"


@pytest.mark.asyncio
async def test_rewrite_verbose_model_output_extracts_first_line(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "### 1. 通用精准型\n停车 申请 流程\n更多说明文字"}})
    async with _make_client(handler) as client:
        out = await kb_search.rewrite_query(client, "它怎么申请")
    assert out == "停车 申请 流程"


@pytest.mark.asyncio
async def test_rewrite_garbage_falls_back():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"message": {"content": "a" * 80}})
    )) as client:
        assert await kb_search.rewrite_query(client, "原始查询") == "原始查询"


@pytest.mark.asyncio
async def test_rewrite_disabled(monkeypatch):
    monkeypatch.setattr(settings, "search_rewrite_enabled", False)
    async with _make_client(_route) as client:
        assert await kb_search.rewrite_query(client, "保持原样") == "保持原样"


@pytest.mark.asyncio
async def test_rerank_reorders_and_rescores(monkeypatch):
    monkeypatch.setattr(settings, "siliconflow_rerank_key", "sk-test")
    chunks = [{"id": f"c{i}", "kb_id": "kb-a", "content": f"doc{i}", "title": "t", "score": 0.1} for i in range(3)]
    async with _make_client(_route) as client:
        out = await kb_search.rerank_chunks(client, "停车", chunks)
    assert [c["id"] for c in out] == ["c2", "c1", "c0"]
    assert out[0]["score"] >= out[-1]["score"]


@pytest.mark.asyncio
async def test_rerank_no_key_passthrough(monkeypatch):
    monkeypatch.setattr(settings, "siliconflow_rerank_key", "")
    chunks = [{"id": "c0", "kb_id": "kb-a", "content": "x", "title": "t", "score": 0.3}]
    async with _make_client(_route) as client:
        assert await kb_search.rerank_chunks(client, "q", chunks) is chunks


@pytest.mark.asyncio
async def test_retrieve_uses_rewritten_query_and_rerank(monkeypatch):
    monkeypatch.setattr(settings, "siliconflow_rerank_key", "sk-test")
    seen_queries = []

    async def fake_hybrid(client, kb_id, q, limit):
        seen_queries.append(q)
        return [{"id": f"{kb_id}-1", "kb_id": kb_id, "kb_name": "", "title": "t",
                 "file_name": "f", "score": 0.5, "content": f"{kb_id} 正文"}]

    monkeypatch.setattr(weknora, "kb_hybrid_search", fake_hybrid)
    monkeypatch.setattr(kb_search, "FANOUT_CONCURRENCY", 4)

    async with _make_client(_route) as client:
        mode, chunks, sources = await kb_search.retrieve(client, "它怎么申请", ["kb-a", "kb-b"])
    assert mode == "ok"
    assert seen_queries and all(q == "停车 申请 流程" for q in seen_queries)
    # rerank 开着：分数被重写为倒序，第一名是 kb-b（index 最大）
    assert chunks[0]["kb_id"] == "kb-b"
    assert sources[0]["kb_id"] == "kb-b"


@pytest.mark.asyncio
async def test_retrieve_without_rerank_keeps_rotation(monkeypatch):
    monkeypatch.setattr(settings, "siliconflow_rerank_key", "")

    async def fake_hybrid(client, kb_id, q, limit):
        return [{"id": f"{kb_id}-1", "kb_id": kb_id, "kb_name": kb_id, "title": kb_id,
                 "file_name": "f", "score": 0.5, "content": f"{kb_id} 正文"}]

    monkeypatch.setattr(weknora, "kb_hybrid_search", fake_hybrid)
    async with _make_client(_route) as client:
        mode, chunks, sources = await kb_search.retrieve(client, "q", ["kb-a", "kb-b"])
    assert mode == "ok"
    # 轮转融合：不同库轮流占位
    assert {c["kb_id"] for c in chunks} == {"kb-a", "kb-b"}
