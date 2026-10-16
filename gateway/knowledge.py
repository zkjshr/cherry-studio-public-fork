"""共享知识库目录端点：把 WeKnora 库列表按部门/公共分类下发给客户端。

部门/公共的划分沿用 chat.py 的命名约定（^\\d{3}- 前缀 = 部门库），与检索选库
(select_kb_ids) 同一语义——不在管理端另设配置，WeKnora 侧改名即时生效。

可见性模型（2026-10-14 定）：
- 部门库只对本部门员工可见——客户端请求带 X-Client-User（本地用户名，如
  51-zhang-kj），网关按 admin_settings.user_departments（账号 → 部门前缀，
  如 {"51-zhang-kj": "202"}）过滤；未登记的用户部门分区为空。
- 公共库全量下发，"是否出现在知识库页"由客户端按数据市场的接入状态决定。
鉴权同 marketplace client 端点（X-Client-Token，复用 enterprise._token_matches）；
WeKnora 不可达降级为空列表——客户端据此把两个共享分区留空，不报错。
"""
import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException

from gateway import kb_search, weknora
from gateway.chat import DEPT_KB_RE
from gateway.enterprise import _token_matches, get_settings

router = APIRouter()


def _require_client_token(x_client_token: str = Header(default="")) -> None:
    if not _token_matches(x_client_token):
        raise HTTPException(status_code=401, detail="unauthorized")


def _dept_prefixes_for(user: str) -> list[str]:
    """该用户的部门库名前缀列表（如 ["202"]）；未登记用户为空。"""
    mapping = get_settings().get("user_departments") or {}
    if not isinstance(mapping, dict):
        return []
    value = mapping.get(user)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


# gateway 保留的虚拟库 wire id——不是 WeKnora id；检索时在本用户可见范围内展开
VIRTUAL_KB_ID = "ekp"


def _virtual_kb_entry() -> dict | None:
    cfg = get_settings().get("virtual_kb") or {}
    if cfg.get("enabled") is False:
        return None
    return {"id": VIRTUAL_KB_ID, "name": cfg.get("name") or "ekp数据检索",
            "description": "聚合全所 EKP 部门知识库的统一检索入口", "scope": "public"}


def _visible_kb_ids(all_kbs: list[dict], user: str, *, include_public: bool) -> list[str]:
    """该用户可见的 WeKnora 库 id：公共库（非 ^\\d{3}- 命名）+ 本人部门的部门库。"""
    prefixes = tuple(p for p in _dept_prefixes_for(user) if p)
    out = []
    for kb in all_kbs:
        name = kb.get("name") or ""
        if DEPT_KB_RE.match(name):
            if prefixes and name.startswith(prefixes):
                out.append(kb["id"])
        elif include_public:
            out.append(kb["id"])
    return out


@router.get("/knowledge/api/kbs", dependencies=[Depends(_require_client_token)])
async def client_kbs(x_client_user: str = Header(default="")) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=60.0)) as client:
            kbs = await weknora.list_kbs(client)
    except (httpx.HTTPError, ValueError):
        # ValueError 覆盖 resp.json() 对非 JSON 响应体抛出的 JSONDecodeError。
        return {"kbs": []}

    prefixes = tuple(p for p in _dept_prefixes_for(x_client_user) if p)
    v = _virtual_kb_entry()
    out = [v] if v else []
    for kb in kbs:
        name = kb.get("name") or ""
        scope = "dept" if DEPT_KB_RE.match(name) else "public"
        # 部门库按用户可见性过滤（未登记用户看不到任何部门库）；公共库全量
        # 下发（接入与否由客户端决定）。
        if scope == "dept" and not name.startswith(prefixes):
            continue
        out.append({"id": kb["id"], "name": name, "description": kb.get("description") or "", "scope": scope})
    return {"kbs": out}


@router.post("/knowledge/api/search", dependencies=[Depends(_require_client_token)])
async def client_kb_search(
    body: dict = Body(...),
    x_client_user: str = Header(default=""),
) -> dict:
    """共享库检索（调用时鉴权）：kb_ids 与该用户可见集取交集后走 kb_search 扇出。

    虚拟库 "ekp" 展开为用户可见的全部部门库（不含非部门公共库）。交集为空返回
    no_result——不泄露库的存在性，与目录端点"未登记用户部门分区为空"同一姿态。
    """
    query = str(body.get("query") or "").strip()
    recent = bool(body.get("recent") or False)
    if not recent and not query:
        return {"mode": "no_result", "chunks": [], "sources": []}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=60.0)) as client:
            try:
                all_kbs = await weknora.list_kbs(client)
            except (httpx.HTTPError, ValueError):
                return {"mode": "unavailable", "chunks": [], "sources": []}
            visible = set(_visible_kb_ids(all_kbs, x_client_user, include_public=True))
            requested = body.get("kb_ids")
            requested = [str(k) for k in requested] if isinstance(requested, list) else []
            resolved: list[str] = []
            for kb_id in requested:
                if kb_id == VIRTUAL_KB_ID:
                    resolved.extend(_visible_kb_ids(all_kbs, x_client_user, include_public=False))
                elif kb_id in visible:
                    resolved.append(kb_id)
            # 去重保序；可见请求与 ekp 展开皆为空时一律 no_result——绝不以空集
            # 进入 retrieve（其空列表语义是扇出全部部门库），recent 分支同受此门约束。
            resolved = list(dict.fromkeys(resolved))
            if not resolved:
                return {"mode": "no_result", "chunks": [], "sources": []}
            mode, chunks, sources = await kb_search.retrieve(client, query, resolved, recent=recent)
    except (httpx.HTTPError, ValueError):
        return {"mode": "unavailable", "chunks": [], "sources": []}
    return {"mode": mode, "chunks": chunks, "sources": sources}
