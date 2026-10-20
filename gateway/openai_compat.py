"""知识库对话端点：WeKnora 检索 + ollama 生成的 RAG 编排包装为 /v1/kb/chat/completions
（OpenAI 兼容格式）。流式为 OpenAI chunk 格式；来源在回答流结束后以附注形式追加
（OpenAI 协议无 sources 概念）。不经知识库的模型直连走 model_proxy 的 /v1/chat/completions。
"""
import json
import os
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from gateway import kb_search
from gateway.chat import build_prompt, extract_ollama_delta
from gateway.auth import require_token
from gateway.kb_search import MAX_SOURCES

router = APIRouter()


def _ollama_base() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://192.168.66.25:11434")


def _ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")


def _last_user_and_history(messages: list[dict]) -> tuple[str, list[dict]]:
    """取最后一条 user 消息为 query，其余（截尾 4 条）为 history。"""
    query = ""
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            query = str(m["content"])
            break
    history = [
        {"role": m["role"], "content": str(m.get("content", ""))}
        for m in messages[:-1]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not query and messages:
        query = str(messages[-1].get("content", ""))
    return query, history[-4:]


def _sources_tail(sources: list[dict]) -> str:
    if not sources:
        return ""
    lines = [f"{i}. {s['title']}（{s['kb_name']}）" for i, s in enumerate(sources[:MAX_SOURCES], 1)]
    return "\n\n——\n来源：\n" + "\n".join(lines)


async def _retrieve(client: httpx.AsyncClient, q: str, kb_ids: list[str]) -> tuple[str, list[dict], list[dict]]:
    """检索委托共享核心 kb_search.retrieve（与 MCP 检索工具同一实现，语义见该模块 docstring）。"""
    return await kb_search.retrieve(client, q, kb_ids)


def _chunk(compl_id: str, model: str, content: str | None = None, finish: str | None = None) -> str:
    delta = {} if finish else ({"content": content} if content is not None else {})
    body = {
        "id": compl_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _ollama_answer_stream(messages: list[dict], client: httpx.AsyncClient):
    """逐段产出 ollama 生成文本；失败时产出降级提示。"""
    try:
        async with client.stream(
            "POST",
            f"{_ollama_base()}/api/chat",
            # think:false 必须显式带：qwen3.5 思考内容走 reasoning 字段，不关会得到空 content
            json={"model": _ollama_model(), "stream": True, "messages": messages, "think": False},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                delta = extract_ollama_delta(line)
                if delta:
                    yield delta
    except (httpx.HTTPError, TimeoutError):
        yield "\n\n（生成服务暂不可用，可参考以下来源）"


async def _run_chat(query: str, history: list[dict], kb_ids: list[str]):
    """统一执行：产出 (文本片段迭代器, sources)。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0)) as client:
        mode, chunks, sources = await _retrieve(client, query, kb_ids)
        if mode == "unavailable":
            yield "知识库暂时无法访问，请稍后重试。", sources
            return
        if mode == "no_result":
            yield "知识库中没有检索到相关内容，换个说法或稍后再试。", sources
            return
        messages = build_prompt(query, chunks, history)
        async for piece in _ollama_answer_stream(messages, client):
            yield piece, sources
        # 尾部来源附注单独产出一次（用哨兵区分？这里直接由调用方拼）——见下


@router.post("/v1/kb/chat/completions")
async def kb_chat_completions(
    body: dict,
    _token: None = Depends(require_token),
    kb_ids: str = Query(default=""),
):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages required")
    model = str(body.get("model") or "tjad-knowledge")
    stream = bool(body.get("stream", False))
    query, history = _last_user_and_history(messages)
    # 客户端目录下发的是命名空间 wire id（ekp / weknora:<uuid>）：
    # weknora: 前缀直接剥；虚拟库 ekp 展开为全部部门库（管理端视角）。
    from gateway.chat import DEPT_KB_RE
    from gateway.knowledge import _strip_kb_id_prefix
    raw_ids = [s for s in kb_ids.split(",") if s]
    dept_ids: list[str] = []
    if "ekp" in raw_ids:
        from gateway import weknora
        async with httpx.AsyncClient(timeout=10) as c:
            try:
                kbs = await weknora.list_kbs(c)
            except httpx.HTTPError:
                kbs = []
        dept_ids = [k["id"] for k in kbs if DEPT_KB_RE.match(k.get("name") or "")]
    ids = list(dict.fromkeys(dept_ids + [_strip_kb_id_prefix(s) for s in raw_ids if s != "ekp"]))
    compl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if not stream:
        pieces: list[str] = []
        sources: list[dict] = []
        async for piece, srcs in _run_chat(query, history, ids):
            pieces.append(piece)
            sources = srcs
        return {
            "id": compl_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "".join(pieces) + _sources_tail(sources)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def gen():
        yield _chunk(compl_id, model, "")
        sources: list[dict] = []
        async for piece, srcs in _run_chat(query, history, ids):
            sources = srcs
            yield _chunk(compl_id, model, piece)
        yield _chunk(compl_id, model, _sources_tail(sources))
        yield _chunk(compl_id, model, None, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
