# -*- coding: utf-8 -*-
"""智能检索管线：查询改写（4B ollama）+ 自建重排（2080S ONNX /rerank）+ retrieve 集成。"""
import json

import httpx
import pytest

from gateway import kb_search, weknora
from gateway.config import settings

LOCAL_RERANK = "http://192.168.66.24:8088/rerank"


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """隔离全局 settings（openai_compat 等既有测试会改 weknora_base_url 不还原）。"""
    monkeypatch.setattr(settings, "weknora_base_url", "http://wk.test")
    monkeypatch.setattr(settings, "search_rewrite_enabled", True)
    monkeypatch.setattr(settings, "search_rewrite_model", "qwen3.5:4b")
    monkeypatch.setattr(settings, "search_ollama_url", "http://10.121.16.5:11434")
    monkeypatch.setattr(settings, "rerank_base_url", LOCAL_RERANK.rsplit("/rerank", 1)[0])
    monkeypatch.setattr(settings, "rerank_threshold", 0.0)


def _route(scores_by_index=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = request.read().decode()
        if "8088/rerank" in url:
            import json as _json
            n = len(_json.loads(body).get("texts") or [])
            scores = scores_by_index if scores_by_index is not None else list(range(n))
            return httpx.Response(200, json={"results": [
                {"index": i, "relevance_score": scores[i]} for i in range(n)
            ]})
        if "11434/api/chat" in url:
            return httpx.Response(200, json={"message": {"content": "停车 申请 流程"}})
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
    return handler


@pytest.mark.asyncio
async def test_rewrite_success():
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route())) as client:
        out = await kb_search.rewrite_query(client, "它怎么申请")
    assert out == "停车 申请 流程"


@pytest.mark.asyncio
async def test_rewrite_failure_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await kb_search.rewrite_query(client, "原始查询")
    assert out == "原始查询"


@pytest.mark.asyncio
async def test_rewrite_disabled(monkeypatch):
    monkeypatch.setattr(settings, "search_rewrite_enabled", False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route())) as client:
        assert await kb_search.rewrite_query(client, "保持原样") == "保持原样"


@pytest.mark.asyncio
async def test_rewrite_verbose_model_output_extracts_first_line(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "### 1. 通用精准型\n停车 申请 流程\n更多说明文字"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await kb_search.rewrite_query(client, "它怎么申请")
    assert out == "停车 申请 流程"


@pytest.mark.asyncio
async def test_rewrite_garbage_falls_back():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"message": {"content": "a" * 80}})
    )) as client:
        assert await kb_search.rewrite_query(client, "原始查询") == "原始查询"


@pytest.mark.asyncio
async def test_rerank_reorders_and_rescores():
    chunks = [{"id": f"c{i}", "kb_id": "kb-a", "content": f"doc{i}", "title": "t", "score": 0.1} for i in range(3)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route())) as client:
        out = await kb_search.rerank_via_local(client, "停车", chunks)
    assert [c["id"] for c in out] == ["c2", "c1", "c0"]
    assert out[0]["score"] >= out[-1]["score"]


@pytest.mark.asyncio
async def test_rerank_drops_below_threshold(monkeypatch):
    """对齐 WeKnora：低相关 chunk（跨主题噪声）必须被丢弃而非混进回答。"""
    monkeypatch.setattr(settings, "rerank_threshold", 0.3)
    chunks = [{"id": f"c{i}", "kb_id": "kb-a", "content": f"doc{i}", "title": "t", "score": 0.1} for i in range(3)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route([0.9, 0.29, 0.05]))) as client:
        out = await kb_search.rerank_via_local(client, "q", chunks)
    assert [c["id"] for c in out] == ["c0"]


@pytest.mark.asyncio
async def test_rerank_unreachable_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    chunks = [{"id": "c0", "kb_id": "kb-a", "content": "x", "title": "t", "score": 0.3}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await kb_search.rerank_via_local(client, "q", chunks) is None


@pytest.mark.asyncio
async def test_retrieve_uses_rewritten_query_and_rerank(monkeypatch):
    seen_queries = []
    monkeypatch.setattr(settings, "fanout_concurrency", 4)

    async def fake_hybrid(client, kb_id, q, limit):
        seen_queries.append(q)
        return [{"id": f"{kb_id}-1", "kb_id": kb_id, "kb_name": "", "title": "t",
                 "file_name": "f", "score": 0.5, "content": f"{kb_id} 正文"}]

    monkeypatch.setattr(weknora, "kb_hybrid_search", fake_hybrid)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_route())) as client:
        mode, chunks, sources = await kb_search.retrieve(client, "它怎么申请", ["kb-a", "kb-b"])
    assert mode == "ok"
    assert seen_queries and all(q == "停车 申请 流程" for q in seen_queries)
    # 重排分数倒序：kb-b（index 大）居首
    assert chunks[0]["kb_id"] == "kb-b"
    assert sources[0]["kb_id"] == "kb-b"


@pytest.mark.asyncio
async def test_retrieve_rerank_unreachable_keeps_rotation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "8088/rerank" in url:
            raise httpx.ConnectError("down")
        if "11434/api/chat" in url:
            return httpx.Response(200, json={"message": {"content": "停车 申请 流程"}})
        if "hybrid-search" in url:
            kb = url.split("/knowledge-bases/")[1].split("/")[0]
            return httpx.Response(200, json={"data": [
                {"id": f"{kb}-c1", "knowledge_id": f"{kb}-d1", "knowledge_title": kb,
                 "knowledge_filename": "f", "score": 0.5, "content": f"{kb} 正文", "chunk_index": 0}
            ]})
        if "/api/v1/knowledge-bases" in url:
            return httpx.Response(200, json={"data": [
                {"id": "kb-a", "name": "103-科技质量部"},
                {"id": "kb-b", "name": "102-综合管理部"},
            ]})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mode, chunks, sources = await kb_search.retrieve(client, "如何申请停车", ["kb-a", "kb-b"])
    assert mode == "ok"
    # 重排不可达 → 轮转融合兜底：不同库轮流占位
    assert {c["kb_id"] for c in chunks} == {"kb-a", "kb-b"}
