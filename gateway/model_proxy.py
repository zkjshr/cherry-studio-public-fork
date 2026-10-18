"""模型路由代理：/v1/chat/completions 按 model="{upstream_id}/{model_id}"（首个 "/" 分隔，
model_id 本身可含 "/"）路由到注册上游。openai 协议原样透传（SSE 流式 / JSON）；ollama 协议
把 /api/chat 的 NDJSON 转为 OpenAI chunk 流（复用 chat.extract_ollama_delta），非 stream 聚合
为一份 completion；weknora 协议把 WeKnora 智能体会话（/api/v1/agent-chat，SSE event:message
帧）转为 OpenAI chunk 流（thinking→reasoning_content、answer→content），会话按对话指纹懒建并
在 weknora_sessions 表中映射，服务端 404 时自动重建重试一次。未知模型/上游返回 OpenAI 错误
格式 404。每次调用（含上游错误与路由失败）写一条 usage_log。RAG 编排在 openai_compat 的
/v1/kb/chat/completions，不走本模块。
"""
import hashlib
import json
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.auth import require_token
from gateway.chat import extract_ollama_parts
from gateway.enterprise import (
    delete_weknora_session,
    get_base_system_prompt,
    get_weknora_session,
    list_upstreams,
    record_usage,
    save_weknora_session
)

router = APIRouter()

# 首包 10s / 生成读 300s，与 RAG 编排的 ollama 预算一致
UPSTREAM_TIMEOUT = httpx.Timeout(10.0, read=300.0)


def _error(status: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    """OpenAI 错误格式。"""
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": err_type}})


def _resolve(model: str) -> tuple[dict | None, str, JSONResponse | None]:
    """解析路由 → (upstream, model_id, 404响应)。仅 enabled 上游可路由；模型须在其清单内
    （visible 只影响 /v1/models 展示，不限制直接调用）。"""
    upstream_id, sep, model_id = model.partition("/")
    if not sep or not upstream_id or not model_id:
        return None, "", _error(404, f"model not found: {model}")
    for up in list_upstreams():
        if up["id"] == upstream_id:
            if up["enabled"] and any(m["id"] == model_id for m in up["models"]):
                return up, model_id, None
            break
    return None, "", _error(404, f"model not found: {model}")


def _record(model: str, upstream_id: str | None, t0: float, usage: dict | None, status: str) -> None:
    """一次调用结束记 usage_log：tokens 抽不到记 0，duration 恒记。"""
    u = usage or {}
    record_usage(model, upstream_id, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0),
                 int(u.get("total_tokens") or 0), int((time.monotonic() - t0) * 1000), status)


def _ollama_counts(d: dict) -> dict:
    """ollama 末行/单对象计数 → OpenAI usage 形状。"""
    p, c = int(d.get("prompt_eval_count") or 0), int(d.get("eval_count") or 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _passthrough(resp: httpx.Response) -> Response:
    """上游响应原样回传（状态码与 content-type 保留）；非流式与上游 4xx/5xx 共用。"""
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


def _chunk(compl_id: str, model: str, delta: dict, finish: str | None = None,
           usage: dict | None = None) -> str:
    """ollama NDJSON → OpenAI chunk 单帧。"""
    body: dict = {
        "id": compl_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _send_stream(url: str, payload: dict, headers: dict) -> tuple[httpx.AsyncClient, httpx.Response]:
    """打开上游流；连接失败向上抛（调用方记 error），由调用方负责关闭。"""
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
    try:
        resp = await client.send(client.build_request("POST", url, json=payload, headers=headers), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise
    return client, resp


async def _close_stream(client: httpx.AsyncClient, resp: httpx.Response) -> None:
    await resp.aclose()
    await client.aclose()


async def _proxy_openai(upstream: dict, model_id: str, model: str, body: dict, t0: float):
    """openai 协议：POST {base}/chat/completions（model 换裸 model_id）；SSE 逐行透传，
    流中末个非空 usage 记账；非 stream/上游错误 JSON 透传。"""
    headers = {"Authorization": f"Bearer {upstream['api_key']}"} if upstream["api_key"] else {}
    url = f"{upstream['base_url']}/chat/completions"
    payload = {**body, "model": model_id}
    if "messages" in payload:
        # 同 ollama 路径：纯文本数组形 content 压平为字符串（字符串形是所有
        # OpenAI 兼容服务的基线格式，链式网关/严格上游都更稳妥）。
        payload["messages"] = _flatten_text_content(payload["messages"])
    if not body.get("stream"):
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            _record(model, upstream["id"], t0, None, "error")
            return _passthrough(resp)
        try:
            usage = resp.json().get("usage")
        except ValueError:  # 非 JSON 响应体：token 记 0，正文仍透传
            usage = None
        _record(model, upstream["id"], t0, usage, "ok")
        return _passthrough(resp)

    client, resp = await _send_stream(url, payload, headers)
    if resp.status_code >= 400:
        await resp.aread()  # 缓存 body 供 passthrough
        out = _passthrough(resp)
        await _close_stream(client, resp)
        _record(model, upstream["id"], t0, None, "error")
        return out

    async def gen():
        usage = None
        ok = True
        try:
            async for line in resp.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    try:
                        chunk_usage = json.loads(line[5:]).get("usage")
                    except ValueError:
                        chunk_usage = None
                    if chunk_usage:
                        usage = chunk_usage
                yield f"{line}\n"  # aiter_lines 已去换行，逐行补回还原原始 SSE 分帧
        except httpx.HTTPError:
            ok = False
            raise
        except BaseException:  # 客户端断流(GatewayExit/CancelledError)等也按失败记账
            ok = False
            raise
        finally:
            await _close_stream(client, resp)
            _record(model, upstream["id"], t0, usage, "ok" if ok else "error")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _flatten_text_content(messages):
    """OpenAI 数组形 content（全部为 text 部件时）压平为字符串。

    ollama 的 Go 结构体只收 string content，而智能体/多段文本消息常以
    `[{"type":"text","text":...}]` 数组下发，直接透传会被 400 拒绝
    （cannot unmarshal array into ... messages.content of type string）。
    含非 text 部件（图片等）的消息原样保留，交由上游自行取舍。
    """
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            if len(texts) == len(content):
                m = {**m, "content": "\n\n".join(t for t in texts if t)}
        out.append(m)
    return out


def _openai_tool_calls(ollama_tool_calls) -> list[dict]:
    """ollama message.tool_calls（arguments 为对象）→ OpenAI tool_calls（arguments 为 JSON 串）。"""
    out = []
    for i, t in enumerate(ollama_tool_calls or []):
        fn = t.get("function") or {}
        args = fn.get("arguments")
        out.append({
            "index": i,
            "id": t.get("id") or f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": fn.get("name") or "",
                         "arguments": args if isinstance(args, str)
                         else json.dumps(args or {}, ensure_ascii=False)},
        })
    return out


def _to_ollama_messages(messages: list) -> list:
    """OpenAI messages → ollama 形状：assistant.tool_calls 的 arguments 必须是对象
    （OpenAI 标准是 JSON 串，原样透传会被 ollama 400：Value looks like object...），
    id/type/index 等 ollama 不认的字段一并剥掉。"""
    out = []
    for m in _flatten_text_content(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tcs = []
            for t in m["tool_calls"]:
                fn = t.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                tcs.append({"function": {"name": fn.get("name") or "", "arguments": args or {}}})
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tcs})
        else:
            out.append(m)
    return out


async def _proxy_ollama(upstream: dict, model_id: str, model: str, body: dict, t0: float):
    """ollama 协议：POST {base}/api/chat（{model, messages, stream}）；NDJSON→OpenAI chunk 流
    （首块带 role，done 行抽 prompt_eval_count/eval_count 记账），非 stream 聚合为一份 completion。
    messages 先过 `_flatten_text_content`（数组形 content → 字符串）再转发。
    tools 原样透传（ollama 原生支持 OpenAI 形状），返回的 tool_calls 映射回 OpenAI 格式，
    finish_reason 相应置 tool_calls——否则客户端 Agent 拿不到结构化工具调用，对话即断。
    max_tokens/temperature 映射到 options。"""
    headers = {"Authorization": f"Bearer {upstream['api_key']}"} if upstream["api_key"] else {}
    url = f"{upstream['base_url']}/api/chat"
    payload = {
        "model": model_id,
        "messages": _to_ollama_messages(body.get("messages") or []),
        "stream": bool(body.get("stream")),
        # 显式开启思考：qwen3.5 等推理模型把过程放进 message.thinking，
        # 不开启时可能内联 <think> 进正文。不支持思考的模型会忽略该参数。
        "think": True,
    }
    if body.get("tools"):
        payload["tools"] = body["tools"]
    options: dict = {}
    if body.get("max_tokens") is not None:
        options["num_predict"] = body["max_tokens"]
    if body.get("temperature") is not None:
        options["temperature"] = body["temperature"]
    if options:
        payload["options"] = options
    compl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if payload["stream"]:
        client, resp = await _send_stream(url, payload, headers)
        if resp.status_code >= 400:
            await resp.aread()
            out = _passthrough(resp)
            await _close_stream(client, resp)
            _record(model, upstream["id"], t0, None, "error")
            return out

        async def gen():
            usage = None
            ok = True
            first = True
            finish = "stop"
            try:
                async for line in resp.aiter_lines():
                    parts = extract_ollama_parts(line)
                    if parts:
                        if first:
                            first = False
                            yield _chunk(compl_id, model, {"role": "assistant", "content": ""})
                        if "thinking" in parts:
                            yield _chunk(compl_id, model, {"reasoning_content": parts["thinking"]})
                        if "content" in parts:
                            yield _chunk(compl_id, model, {"content": parts["content"]})
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    tool_calls = _openai_tool_calls((d.get("message") or {}).get("tool_calls"))
                    if tool_calls:
                        finish = "tool_calls"
                        yield _chunk(compl_id, model, {"tool_calls": tool_calls})
                    if d.get("done"):
                        usage = _ollama_counts(d)
            except httpx.HTTPError:
                ok = False
                raise
            except BaseException:  # 客户端断流等非 HTTP 异常同样按失败记账
                ok = False
                raise
            finally:
                await _close_stream(client, resp)
                _record(model, upstream["id"], t0, usage, "ok" if ok else "error")
            yield _chunk(compl_id, model, {}, finish, usage)
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        _record(model, upstream["id"], t0, None, "error")
        return _passthrough(resp)
    try:
        d = resp.json()
    except ValueError:
        _record(model, upstream["id"], t0, None, "error")
        return _passthrough(resp)
    usage = _ollama_counts(d)
    _record(model, upstream["id"], t0, usage, "ok")
    message: dict = {
        "role": "assistant",
        "content": (d.get("message") or {}).get("content") or "",
        **({"reasoning_content": d["message"]["thinking"]}
           if (d.get("message") or {}).get("thinking") else {}),
    }
    tool_calls = _openai_tool_calls((d.get("message") or {}).get("tool_calls"))
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": compl_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": usage,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WeKnora 协议（智能体会话代理）
# ─────────────────────────────────────────────────────────────────────────────

def _weknora_headers(upstream: dict) -> dict:
    """WeKnora 开放 API 用 X-API-Key（Bearer 仅用户 token，API key 会 401）。"""
    return {"X-API-Key": upstream["api_key"]} if upstream["api_key"] else {}


def _weknora_agent_id(upstream: dict, model_id: str) -> str:
    """模型条目显式 agent_id 优先；缺省以模型 id 充当智能体 id。"""
    for m in upstream["models"]:
        if m["id"] == model_id:
            return m.get("agent_id") or model_id
    return model_id


def _message_text(m: dict) -> str:
    content = m.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _weknora_query_and_key(messages: list[dict]) -> tuple[str, str]:
    """query=最后一条 user 消息；对话指纹=首条 user 消息哈希。

    WeKnora 会话在服务端持有历史，网关每轮只发增量 query；指纹让同一客户端话题跨轮次
    复用同一 WeKnora 会话。指纹漂移（首条消息被编辑/删除）仅意味着开新会话，可接受。
    """
    query, first_user = "", ""
    for m in messages:
        if m.get("role") == "user":
            text = _message_text(m).strip()
            if not text:
                continue
            if not first_user:
                first_user = text
            query = text
    seed = first_user or query
    conv_key = hashlib.sha1(seed[:512].encode("utf-8", "ignore")).hexdigest()[:24]
    return query, conv_key


def _weknora_data(line: str) -> dict | None:
    """WeKnora SSE `data:{json}` 帧 → dict；event:/空行/坏帧返回 None。"""
    if not line.startswith("data:"):
        return None
    try:
        d = json.loads(line[5:])
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


async def _weknora_create_session(client: httpx.AsyncClient, upstream: dict, agent_id: str) -> str:
    resp = await client.post(f"{upstream['base_url']}/api/v1/sessions",
                             json={"agent_id": agent_id}, headers=_weknora_headers(upstream))
    resp.raise_for_status()
    return resp.json()["data"]["id"]


async def _proxy_weknora(upstream: dict, model_id: str, model: str, body: dict, t0: float):
    """weknora 协议：会话懒建+映射，POST {base}/api/v1/agent-chat/{sid} {"query"}。

    SSE event:message 帧按 response_type 转译：thinking→reasoning_content、answer→content，
    agent_query/session_title/complete 等忽略。会话在 WeKnora 侧失效（404）时重建重试一次。
    WeKnora 不回 token 计数，usage 记 0（duration/status 照记）。
    """
    messages = _flatten_text_content(body.get("messages") or [])
    query, conv_key = _weknora_query_and_key(messages)
    if not query:
        return _error(400, "weknora upstream requires at least one non-empty user message")
    agent_id = _weknora_agent_id(upstream, model_id)
    compl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # client 手动管理生命周期：流式生成器在函数返回后才消费 resp，不能挂在 async with 上
    # （否则返回即关池，生成器首读就是 ReadError——与 _send_stream 路径同一约束）
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
    try:
        mapping = get_weknora_session(conv_key)
        if mapping is None or mapping["agent_id"] != agent_id:
            session_id = await _weknora_create_session(client, upstream, agent_id)
            save_weknora_session(conv_key, session_id, agent_id)
        else:
            session_id = mapping["session_id"]

        def chat_request(sid: str):
            return client.build_request(
                "POST", f"{upstream['base_url']}/api/v1/agent-chat/{sid}",
                json={"query": query}, headers=_weknora_headers(upstream))

        resp = await client.send(chat_request(session_id), stream=True)
        if resp.status_code == 404:
            # 映射指向的会话已被服务端删除：清映射、重建、重试一次
            await resp.aclose()
            delete_weknora_session(conv_key)
            session_id = await _weknora_create_session(client, upstream, agent_id)
            save_weknora_session(conv_key, session_id, agent_id)
            resp = await client.send(chat_request(session_id), stream=True)

        if resp.status_code >= 400:
            await resp.aread()
            out = _passthrough(resp)
            await resp.aclose()
            await client.aclose()
            _record(model, upstream["id"], t0, None, "error")
            return out

        if not body.get("stream"):
            await resp.aread()
            try:
                text = resp.text
            finally:
                await resp.aclose()
                await client.aclose()
            content, reasoning = "", ""
            for line in text.splitlines():
                d = _weknora_data(line)
                if d is None:
                    continue
                rt = d.get("response_type")
                if rt == "answer":
                    content += d.get("content") or ""
                elif rt == "thinking":
                    reasoning += d.get("content") or ""
            _record(model, upstream["id"], t0, None, "ok")
            message: dict = {"role": "assistant", "content": content}
            if reasoning:
                message["reasoning_content"] = reasoning
            return {
                "id": compl_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
    except BaseException:
        await client.aclose()
        raise

    async def gen():
        ok = True
        try:
            yield _chunk(compl_id, model, {"role": "assistant", "content": ""})
            async for line in resp.aiter_lines():
                d = _weknora_data(line)
                if d is None:
                    continue
                rt = d.get("response_type")
                if rt == "thinking":
                    yield _chunk(compl_id, model, {"reasoning_content": d.get("content") or ""})
                elif rt == "answer":
                    yield _chunk(compl_id, model, {"content": d.get("content") or ""})
        except httpx.HTTPError:
            ok = False
            raise
        except BaseException:  # 客户端断流等非 HTTP 异常同样按失败记账
            ok = False
            raise
        finally:
            await resp.aclose()
            await client.aclose()
            _record(model, upstream["id"], t0, None, "ok" if ok else "error")
        yield _chunk(compl_id, model, {}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/chat/completions")
async def chat_completions(body: dict, _token: None = Depends(require_token)):
    """模型路由代理入口；上游不可达回 502（OpenAI 错误格式），上游 4xx/5xx 原样透传。

    基座系统提示词（admin_settings.base_system_prompt）在 openai/ollama 路径上强制注入
    到 messages 首位（客户端改不了；已有相同基座时不重复加）；weknora 路径不注入——
    其基座由 WeKnora 智能体侧的 prompt 承载，网关只发增量 query。
    """
    model = str(body.get("model") or "")
    upstream, model_id, miss = _resolve(model)
    t0 = time.monotonic()
    if miss is not None:
        _record(model, None, t0, None, "error")
        return miss
    if upstream["protocol"] in ("openai", "ollama"):
        base_prompt = get_base_system_prompt()
        msgs = body.get("messages")
        if base_prompt and isinstance(msgs, list):
            # 部分模型（如 qwen3.5）的 chat 模板要求 system 必须在首位且只能有一条：
            # 首条已是 system 时合并进它，而不是再插一条。
            if msgs and msgs[0].get("role") == "system":
                if msgs[0].get("content") != base_prompt:
                    first = dict(msgs[0])
                    first["content"] = base_prompt + "\n\n" + str(first.get("content") or "")
                    body = {**body, "messages": [first, *msgs[1:]]}
            elif not any(m.get("role") == "system" for m in msgs):
                body = {**body, "messages": [{"role": "system", "content": base_prompt}, *msgs]}
    try:
        if upstream["protocol"] == "ollama":
            return await _proxy_ollama(upstream, model_id, model, body, t0)
        if upstream["protocol"] == "weknora":
            return await _proxy_weknora(upstream, model_id, model, body, t0)
        return await _proxy_openai(upstream, model_id, model, body, t0)
    except httpx.HTTPError as e:
        _record(model, upstream["id"], t0, None, "error")
        return _error(502, f"upstream unreachable: {e}", "api_error")


@router.get("/v1/models")
def list_models(_token: None = Depends(require_token)) -> dict:
    """可见模型清单：enabled 上游的 visible 模型，id 为路由格式 "{upstream_id}/{model_id}"。"""
    data = [{"id": f'{up["id"]}/{m["id"]}', "object": "model"}
            for up in list_upstreams() if up["enabled"]
            for m in up["models"] if m.get("visible", True)]
    return {"object": "list", "data": data}
