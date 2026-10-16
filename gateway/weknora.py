"""WeKnora HTTP 客户端：标题检索、按库混合检索、列库。

适配实测实例（10.137.200.58:8091）的真实行为：
- API key 走 X-API-Key 头（Bearer 仅用户 token，API key 会 401）
- 标题/最近检索路由为 GET /api/v1/knowledge/search（?keyword=&recent=true&limit=&offset=）
- hybrid-search 为平铺 chunk 字段（knowledge_id/knowledge_title/score...，无嵌套 knowledge、
  无 kb_name），且忽略 limit 参数恒返 80 条——网关侧按 limit 截断
"""
import httpx

from gateway.config import settings

TIMEOUT = httpx.Timeout(5.0, read=8.0)


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.weknora_api_key}


def _doc_item(k: dict) -> dict:
    return {
        "id": k.get("id"),
        "kb_id": k.get("knowledge_base_id"),
        "kb_name": k.get("knowledge_base_name") or k.get("knowledgeBaseName") or "",
        "title": k.get("title") or k.get("file_name") or "",
        "file_name": k.get("file_name") or "",
        "updated_at": k.get("updated_at"),
        "score": None,
    }


async def instant_search(client: httpx.AsyncClient, q: str, limit: int) -> list[dict]:
    resp = await client.get(
        f"{settings.weknora_base_url}/api/v1/knowledge/search",
        params={"keyword": q, "limit": limit, "offset": 0},
        headers=_headers(),
    )
    resp.raise_for_status()
    return [_doc_item(k) for k in resp.json().get("data") or []]


async def recent_knowledge(client: httpx.AsyncClient, limit: int) -> list[dict]:
    resp = await client.get(
        f"{settings.weknora_base_url}/api/v1/knowledge/search",
        params={"recent": "true", "keyword": "", "limit": limit, "offset": 0},
        headers=_headers(),
    )
    resp.raise_for_status()
    return [_doc_item(k) for k in resp.json().get("data") or []]


async def kb_hybrid_search(client: httpx.AsyncClient, kb_id: str, q: str, limit: int) -> list[dict]:
    resp = await client.post(
        f"{settings.weknora_base_url}/api/v1/knowledge-bases/{kb_id}/hybrid-search",
        json={"query_text": q},
        headers=_headers(),
    )
    resp.raise_for_status()
    items = resp.json().get("data") or []
    out = []
    for item in items[:limit]:
        out.append({
            "id": item.get("knowledge_id"),
            "kb_id": kb_id,
            "kb_name": "",
            "title": item.get("knowledge_title") or item.get("knowledge_filename") or "",
            "file_name": item.get("knowledge_filename") or "",
            "updated_at": None,
            "score": item.get("score"),
            "content": item.get("content") or "",
        })
    return out


async def list_kbs(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(
        f"{settings.weknora_base_url}/api/v1/knowledge-bases",
        params={"page": 1, "page_size": 200},
        headers=_headers(),
    )
    resp.raise_for_status()
    items = resp.json().get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or []
    return [
        {"id": k["id"], "name": k.get("name", ""), "description": k.get("description") or ""}
        for k in items
        if k.get("id")
    ]
