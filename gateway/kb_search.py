"""WeKnora 检索共享核心：按库扇出 hybrid-search + 跨库轮转融合。

openai_compat（/v1/kb/chat/completions）与 mcp_weknora（MCP 检索工具）共用同一份实现，
避免检索语义复制粘贴后漂移。语义（沿用原 openai_compat._retrieve）：
- 选库 select_kb_ids：显式 kb_ids 优先；否则全部部门库（^\\d{3}- 命名规范）；无部门库回退前 12
- 并发 ≤ FANOUT_CONCURRENCY，单库 KB_TIMEOUT 秒预算；失败/超时的库记为失败，其余库照常返回
- 全部库失败 → "unavailable"，与 0 命中 "no_result" 区分（供上层降级文案，避免误读为"没有资料"）
- 跨库 score 不可比（实测同查询下小库噪声 0.48 vs 相关文档 0.016），按库轮转取优：
  各库先出库内最优 1 条，再按库内分数补足至 MAX_SOURCES
"""
import asyncio
import logging

import httpx

from gateway import weknora
from gateway.chat import select_kb_ids
from gateway.config import settings

# 单库预算与并发上限（与 app.py 的 DEEP_PER_KB_TIMEOUT/FANOUT_CONCURRENCY 同值）
KB_TIMEOUT = 12.0
FANOUT_CONCURRENCY = 8
MAX_SOURCES = 8
# 改写调用预算：改写是增益不是依赖，超时/失败一律退回原始 query
REWRITE_TIMEOUT = 4.0
RERANK_TIMEOUT = 25.0  # CPU 批量重排 250 条×512token 需要十秒级

logger = logging.getLogger(__name__)
# 最近一次 retrieve 的诊断（排障用，临时）
LAST_DEBUG: dict = {}


async def rewrite_query(client: httpx.AsyncClient, q: str) -> str:
    """Best-effort 查询改写（WeKnora default_rewrite 的简化版）：
    多轮指代消解（"它怎么申请"→"停车怎么申请"）+ 关键词化。
    任何失败/超时原样返回 q —— 检索可用性绝不依赖改写服务的可用性。
    模型用本地 4B（qwen3.5:4b，think:false 原生通道），预算 4s。"""
    if not settings.search_rewrite_enabled or not q.strip():
        return q
    try:
        resp = await asyncio.wait_for(
            client.post(
                f"{settings.search_ollama_url}/api/chat",
                json={
                    "model": settings.search_rewrite_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是企业知识库的查询改写器。把用户问题改写成一句适合检索的查询："
                                "补全指代（它/这个/那个→明确名词），补全省略的主语，保留原意。"
                                "只输出改写后的一句查询（不超过 30 字），不要解释，不要加引号。"
                            ),
                        },
                        {"role": "user", "content": q},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.1, "num_predict": 60},
                },
            ),
            timeout=REWRITE_TIMEOUT,
        )
        raw = str((resp.json().get("message") or {}).get("content") or "")
        # 4B 小模型不守"只输出一句"的指令（会输出 markdown 长文）：跳过标题/空行取
        # 首个内容行、剥掉残留修饰；超长视为改写失败回退原 query。
        first_line = next(
            (ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")),
            "",
        )
        cleaned = first_line.lstrip("*-> ` ").strip().strip('"').strip("“”").rstrip("。")
        if not cleaned or len(cleaned) > 40:
            return q
        return cleaned
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError):
        logger.warning("query rewrite failed; falling back to raw query", exc_info=True)
        return q


def _query_terms(q: str) -> list[str]:
    """中文无空格时退化为 2-gram：如何申请停车 → [如何, 何申, 申请, 停车]。"""
    parts = [t for t in q.split() if t]
    if len(parts) > 1:
        return parts
    text = q.strip()
    if len(text) <= 2:
        return [text] if text else []
    return [text[i : i + 2] for i in range(len(text) - 1)]


def _keyword_boost(q: str, chunk: dict) -> float:
    """查询词元在 标题+正文 的重叠率（0~1）：无 rerank 时唯一的相关性再排序信号。
    标题命中权重加倍——标题是 EKP 文档最强的主题信号。"""
    terms = _query_terms(q)
    if not terms:
        return 0.0
    title = chunk.get("title") or ""
    body = (chunk.get("content") or "")[:500]
    hit = sum(1 for t in terms if t in title) * 2 + sum(1 for t in terms if t in body)
    return min(1.0, hit / (len(terms) * 2))


async def rerank_via_local(client: httpx.AsyncClient, q: str, chunks: list[dict]) -> list[dict] | None:
    """自建重排服务（2080S bge-reranker-v2-m3，TEI/Cohere 兼容 /rerank）。不可达返回 None。"""
    try:
        resp = await asyncio.wait_for(
            client.post(
                f"{settings.rerank_base_url}/rerank",
                json={"query": q, "texts": [(c.get("content") or c.get("title") or "")[:1500] for c in chunks]},
            ),
            timeout=RERANK_TIMEOUT,
        )
        results = resp.json().get("results") or []
        ranked: list[dict] = []
        for item in sorted(results, key=lambda r: r.get("relevance_score") or 0, reverse=True):
            idx = item.get("index")
            if not isinstance(idx, int) or not 0 <= idx < len(chunks):
                continue
            score = item.get("relevance_score")
            # 对齐 WeKnora rerank_threshold：低于阈值 = 跨主题噪声，直接丢弃
            if not isinstance(score, (int, float)) or score < settings.rerank_threshold:
                continue
            c = dict(chunks[idx])
            c["score"] = max(0.0, min(1.0, score))
            ranked.append(c)
        return ranked or None
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
        logger.warning("local rerank unreachable; keeping rotation merge", exc_info=True)
        return None


async def retrieve(
    client: httpx.AsyncClient, q: str, kb_ids: list[str], recent: bool = False
) -> tuple[str, list[dict], list[dict]]:
    """返回 (mode, chunks, sources)。mode: "ok"|"no_result"|"unavailable"。

    recent=True 走 WeKnora 全局最近更新检索（忽略 q 与 kb_ids 范围，供"最新文档"类问题）。
    """
    if recent:
        return await _retrieve_recent(client)
    try:
        all_kbs = await weknora.list_kbs(client)
    except httpx.HTTPError:
        return "unavailable", [], []
    kb_names = {k["id"]: k["name"] for k in all_kbs}
    ids = select_kb_ids(all_kbs, kb_ids)
    if not ids:
        return "unavailable", [], []

    # 智能检索第一步：查询改写（指代消解/关键词化，best-effort）。recent 路径
    # 没有 q，跳过。
    q_effective = await rewrite_query(client, q) if q.strip() else q
    LAST_DEBUG.clear()
    LAST_DEBUG.update({"q_in": q, "q_effective": q_effective, "kbs": len(ids), "per_kb": {}})

    sem = asyncio.Semaphore(settings.fanout_concurrency)

    async def one(kb_id: str):
        async with sem:
            try:
                rs = await asyncio.wait_for(
                    weknora.kb_hybrid_search(client, kb_id, q_effective, settings.search_top_k_per_kb),
                    timeout=KB_TIMEOUT,
                )
                for r in rs:
                    r["kb_name"] = kb_names.get(kb_id, r.get("kb_name") or "")
                LAST_DEBUG["per_kb"][kb_id] = len(rs)
                return True, rs
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                LAST_DEBUG["per_kb"][kb_id] = f"FAIL {type(exc).__name__}"
                return False, []

    pairs = await asyncio.gather(*(one(k) for k in ids))
    if not any(ok for ok, _ in pairs):
        return "unavailable", [], []

    chunks: list[dict] = [r for _, rs in pairs for r in rs]

    # 智能检索第二步：优先自建重排服务（2080S bge-reranker），不可用时退回
    # 不可达则跨库轮转融合（同分不可比，按库轮转取优）。
    import time as _time
    _t0 = _time.monotonic()
    reranked = await rerank_via_local(client, q_effective, chunks) if settings.rerank_base_url else None
    LAST_DEBUG["rerank"] = {"applied": reranked is not None, "ms": int((_time.monotonic() - _t0) * 1000), "in": len(chunks)}
    if reranked is not None:
        chunks = reranked
        merged = chunks[:MAX_SOURCES]
        sources: list[dict] = []
        seen: set[str] = set()
        for c in merged:
            if c["id"] not in seen:
                seen.add(c["id"])
                sources.append({
                    "id": c["id"],
                    "kb_id": c["kb_id"],
                    "kb_name": c.get("kb_name") or "",
                    "title": c.get("title") or "",
                })
        mode = "ok" if sources else "no_result"
        return mode, merged, sources

    by_kb: dict[str, list[dict]] = {}
    for c in chunks:
        c["_boost"] = _keyword_boost(q, c)
        by_kb.setdefault(c["kb_id"], []).append(c)
    for rs in by_kb.values():
        rs.sort(key=lambda c: (c.get("_boost") or 0, c.get("score") or 0), reverse=True)
    q_terms = [t for t in q.split() if t] or [q]
    pools = sorted(by_kb.values(), key=lambda rs: rs[0].get("_boost") or 0, reverse=True)
    merged: list[dict] = []
    while any(pools) and len(merged) < MAX_SOURCES:
        before = len(merged)
        for pool in pools:
            if pool and len(merged) < MAX_SOURCES:
                merged.append(pool.pop(0))
        if len(merged) == before:
            break
    sources: list[dict] = []
    seen: set[str] = set()
    for c in merged:
        if c["id"] not in seen:
            seen.add(c["id"])
            sources.append({
                "id": c["id"],
                "kb_id": c["kb_id"],
                "kb_name": c.get("kb_name") or "",
                "title": c.get("title") or "",
            })
    mode = "ok" if sources else "no_result"
    return mode, merged, sources


async def _retrieve_recent(client: httpx.AsyncClient) -> tuple[str, list[dict], list[dict]]:
    """全库最近更新检索（WeKnora 全局 recent 接口，非按库扇出）。"""
    try:
        items = await weknora.recent_knowledge(client, MAX_SOURCES)
    except httpx.HTTPError:
        return "unavailable", [], []
    if not items:
        return "no_result", [], []
    return "ok", items, items
