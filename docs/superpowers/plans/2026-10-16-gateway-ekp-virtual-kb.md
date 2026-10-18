# EKP 虚拟知识库「ekp数据检索」实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在网关层新增虚拟知识库「ekp数据检索」（聚合全部 EKP 部门库），客户端 agent 通过原生 `kb_search`/`kb_list` 工具像调本地知识库一样检索它；公共库全员可搜，部门库在**下发时**（已有 `/knowledge/api/kbs` 目录过滤）和**调用时**（新检索端点按用户过滤）双重鉴权。WeKnora 侧零改动，本地知识库结构零改动。

**Architecture:** 网关 `knowledge.py` 已有共享库目录端点（X-Client-Token + X-Client-User 鉴权，`user_departments` 映射部门可见性）。本方案：(1) 目录里插入一条虚拟库「ekp数据检索」（gateway 保留 id `ekp`，不是 WeKnora id）；(2) 新增 `POST /knowledge/api/search` 端点，调用时重新校验"用户 × 库"授权，虚拟 id 在用户可见范围内展开，检索复用 `kb_search.retrieve` 扇出核心；(3) 客户端主进程新增网关检索适配器，把共享库目录物化为伪 `KnowledgeBase` 对象（不落库、不走 SQLite 迁移），在 `knowledgeLookup.searchKnowledge` 按 baseId 分流——网关 id 走 HTTP，本地 id 走原 `KnowledgeService.search`。这样 agent 的 kb_* 工具、composer 选择器、引用格式全部原样复用。

**Tech Stack:** 网关 FastAPI + httpx + pytest（`gateway/pytest.ini`）；客户端 Electron main + zod + vitest。

## Global Constraints

- WeKnora 侧不做任何改动（不建库、不动 74 个库、不改权限模型）；网关持有唯一 admin API key（`gateway.config.settings.weknora_api_key`）。
- 客户端本地知识库（SQLite `knowledge_base` 表、`KnowledgeBaseEntitySchema`、索引管线）**不加字段、不迁移**。网关库是运行时伪对象，永不持久化到本地 KB 表。
- 虚拟库 wire id 固定为 `"ekp"`；显示名固定为 `ekp数据检索`（可被 settings 覆盖）。
- 部门库判定沿用 `gateway.chat.DEPT_KB_RE`（`^\d{3}-` 命名约定），与 `select_kb_ids`、目录端点同一语义。
- 调用时鉴权是安全边界：**服务端**对每次 search 请求重算用户可见库集合并取交集；客户端下发的目录仅是 UI 过滤，不作授权依据。
- `searchKnowledge` 契约不变：永不 throw，全失败返回 `{ error }`，0 命中返回 `[]`。
- 所有面向模型的 steer 文案用英文（与 `knowledgeLookup.ts` 现状一致）；面向用户的 UI 文案用中文。

---

## 背景事实（执行者必读）

- 网关目录端点：`gateway/knowledge.py` `GET /knowledge/api/kbs`，鉴权 `_require_client_token`（`enterprise._token_matches`），用户部门 `_dept_prefixes_for(user)`（settings 键 `user_departments`，如 `{"51-zhang-kj": "202"}`）。返回 `{"kbs": [{id, name, description, scope: "dept"|"public"}]}`。
- 检索核心：`gateway/kb_search.py` `retrieve(client, q, kb_ids, recent=False) -> (mode, chunks, sources)`，mode ∈ ok/no_result/unavailable；chunk 字段 `{id, kb_id, kb_name, title, file_name, updated_at, score}`（+ 可选 content，见 Task 1）。扇出并发 8、单库 6s、融合后 8 条。
- WeKnora 客户端：`gateway/weknora.py` `list_kbs / kb_hybrid_search(client, kb_id, q, limit)`。hybrid-search 忽略 limit 恒返 80 条，网关侧截断。
- 客户端目录获取：`src/main/features/knowledge/sharedKbs.ts` `listSharedKnowledgeBases()` → `{ kbs: SharedKnowledgeBase[], chatTopicId }`，带 `X-Client-Token` + `X-Client-User`（本地用户名 `app.user.name`），一切失败降级空目录。
- 客户端检索汇聚点：`src/main/ai/tools/knowledgeLookup.ts` `searchKnowledge(query, baseIds, allowedIds)`，逐库调 `application.get('KnowledgeService').search(baseId, query)`。
- `KnowledgeSearchResultSchema`（strictObject，`src/shared/data/types/knowledge.ts:536`）：`{ pageContent, score, scoreKind, rank, metadata: {itemId(uuidv7), itemType, source, chunkIndex, tokenCount}, chunkId, itemId?, conceptId?, title? }`。网关结果需合成合法 uuidv7 itemId。
- scope 机制：`allowedIds`（空 = 全部）是 agent 绑定的天花板；`searchKnowledge` 会把不在 allowedIds 里的 baseId 静默丢弃并 log。
- 网关测试：`gateway/tests/`，跑 `cd gateway && python3 -m pytest tests/ -x -q`。客户端测试：`pnpm vitest run <file>`。

---

### Task 0（前置验证）: hybrid-search 响应体确认 content 字段

**Files:** 无代码改动；只做一次线上验证，结论写进 Task 2 的实现依据。

- [ ] **Step 1: 从网关服务器 curl WeKnora hybrid-search，确认 chunk 里有没有正文**

```bash
# 在 66.12 网关服务器上执行（凭据见 TJAD server access）：
curl -s -X POST "http://10.137.200.58:8091/api/v1/knowledge-bases/482c52e7-$(true)/hybrid-search" 2>/dev/null || true
# 用真实 kb id（如 103-科技质量部 482c52e7-...，从 GET /api/v1/knowledge-bases 取）：
curl -s -X POST "http://10.137.200.58:8091/api/v1/knowledge-bases/<真实kb_id>/hybrid-search" \
  -H "X-API-Key: <weknora_api_key>" -H "Content-Type: application/json" \
  -d '{"query_text": "资质"}' | python3 -m json.tool | head -60
```

Expected: 响应 chunk 里有 `content`（或 `chunk_content`/`snippet`）字段且非空。若确实无正文（与 `mcp_weknora.py` 注释"平铺结果无正文"一致），则 Task 2 走 `_doc_item` 的 `instant_search`/标题模式兜底，Task 3 的 `pageContent` 用 title+kb_name 合成——**两条路都在 Task 2/3 里写好了，按验证结果选其一并删掉另一条**。

---

### Task 1: 网关——kb_hybrid_search 透传 chunk 正文

**Files:**
- Modify: `gateway/weknora.py:52-71`（`kb_hybrid_search`）
- Test: `gateway/tests/test_weknora_content.py`（新建）

**Interfaces:**
- Produces: `kb_hybrid_search` 返回的每个 chunk dict 新增 `"content": str` 键。Task 2 的端点与 Task 3 的客户端映射都依赖它。

- [ ] **Step 1: 写失败测试**

```python
# gateway/tests/test_weknora_content.py
"""kb_hybrid_search 必须透传 WeKnora chunk 的 content 字段（agent RAG 需要正文，不能只有标题）。"""
import httpx
import pytest

from gateway import weknora


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_transport(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_hybrid_search_passes_content_through(monkeypatch):
    payload = {"data": [{"knowledge_id": "k1", "knowledge_title": "通知A",
                         "knowledge_filename": "a.md", "score": 0.9,
                         "content": "第一条正文段落"}]}
    monkeypatch.setattr(weknora.httpx.AsyncClient, "post",
                        lambda self, *a, **k: _fake_transport(payload).handler.__self__.build_request("POST", "x") if False else None)
    # 直接断言映射逻辑：走 MockTransport 更稳
    transport = _fake_transport(payload)
    async def fake_post(self, url, **kwargs):
        request = transport.build_request("POST", url, **kwargs)
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
        request = transport.build_request("POST", url, **kwargs)
        response = transport.handle_request(request)
        response.request = request
        return response
    monkeypatch.setattr(weknora.httpx.AsyncClient, "post", fake_post)
    async with httpx.AsyncClient() as client:
        out = await weknora.kb_hybrid_search(client, "kb-1", "资质", 3)
    assert out[0]["content"] == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd gateway && python3 -m pytest tests/test_weknora_content.py -x -q`
Expected: FAIL，KeyError `'content'`

- [ ] **Step 3: 最小实现**（若 Task 0 验证出字段名不同，替换 `content` 的取值键）

```python
# gateway/weknora.py kb_hybrid_search 内，out.append({...}) 增加一项：
            "content": item.get("content") or item.get("chunk_content") or "",
```

- [ ] **Step 4: 跑测试确认通过 + 回归既有网关测试**

Run: `cd gateway && python3 -m pytest tests/ -x -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add gateway/weknora.py gateway/tests/test_weknora_content.py
git commit -m "feat(gateway): pass chunk content through kb_hybrid_search"
```

---

### Task 2: 网关——「ekp数据检索」虚拟库目录项 + 调用时鉴权的检索端点

**Files:**
- Modify: `gateway/knowledge.py`（目录插虚拟项 + 新增 `POST /knowledge/api/search`）
- Test: `gateway/tests/test_knowledge_search.py`（新建）

**Interfaces:**
- Produces:
  - 目录：`GET /knowledge/api/kbs` 的 `kbs` 数组首位多一条 `{"id": "ekp", "name": "ekp数据检索", "description": "...", "scope": "public"}`（settings `virtual_kb.enabled=false` 可关闭）。
  - 检索：`POST /knowledge/api/search`，请求体 `{"kb_ids": ["ekp"] 或真实 id 列表, "query": "关键词", "recent": false}`；响应 `{"mode": "ok"|"no_result"|"unavailable", "chunks": [...], "sources": [{id, kb_id, kb_name, title}]}`。鉴权同目录（token + user）。
- Consumes: Task 1 的 chunk `content` 字段；`kb_search.retrieve`；`_dept_prefixes_for`；`weknora.list_kbs`。

**鉴权语义（本任务的核心）:** 用户可见集 = 公共库（`^\d{3}-` 之外的库）+ 本人部门前缀的部门库。请求 kb_ids 里的虚拟 id `ekp` 展开为**该用户可见集内的全部部门库**（不含非部门公共库，EKP 检索语义即"全所制度/通知/项目资料"）；真实 id 与可见集取交集，交集外静默剔除（与 `searchKnowledge` 对 allowedIds 的处理同型），全部被剔除时返回 `no_result` 而非 403（不向客户端泄露库的存在性）。

- [ ] **Step 1: 写失败测试**

```python
# gateway/tests/test_knowledge_search.py
"""/knowledge/api/search：调用时鉴权（用户×库交集）+ 虚拟库 ekp 展开。"""
import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import knowledge as knowledge_mod
from gateway.app import app

client = TestClient(app)

_H = {"X-Client-Token": "tok"}
# 用户 51-zhang-kj 属部门 202；库里 202-建筑一院=可见、101-市场运营部=不可见、公告栏=公共
_FAKE_KBS = [
    {"id": "kb-202", "name": "202-建筑一院", "description": ""},
    {"id": "kb-101", "name": "101-市场运营部", "description": ""},
    {"id": "kb-pub", "name": "公告栏", "description": ""},
]


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(knowledge_mod, "_token_matches", lambda t: t == "tok")
    monkeypatch.setattr(knowledge_mod, "get_settings",
                        lambda: {"user_departments": {"51-zhang-kj": "202"}})
    async def fake_list_kbs(c):
        return _FAKE_KBS
    monkeypatch.setattr(knowledge_mod.weknora, "list_kbs", fake_list_kbs)
    async def fake_retrieve(c, q, kb_ids, recent=False):
        return "ok", [{"id": "c1", "kb_id": kb_ids[0], "kb_name": "", "title": "T",
                       "file_name": "", "score": 0.5, "content": "正文"}], \
               [{"id": "c1", "kb_id": kb_ids[0], "kb_name": "", "title": "T"}]
    monkeypatch.setattr(knowledge_mod.kb_search, "retrieve", fake_retrieve)


def test_search_requires_token():
    assert client.post("/knowledge/api/search",
                       json={"kb_ids": ["kb-202"], "query": "资质"}).status_code == 401


def test_search_filters_kb_ids_by_user_scope():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "51-zhang-kj"},
                    json={"kb_ids": ["kb-202", "kb-101"], "query": "资质"})
    assert r.status_code == 200
    # kb-101 不在该用户可见集，被静默剔除——扇出只打 kb-202（fake_retrieve 回显 kb_ids[0]）
    assert r.json()["chunks"][0]["kb_id"] == "kb-202"


def test_virtual_ekp_expands_within_user_scope():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "51-zhang-kj"},
                    json={"kb_ids": ["ekp"], "query": "资质"})
    body = r.json()
    # ekp = 该用户可见的部门库（202-建筑一院）。公共库不进 ekp；kb-101 不可见不进。
    hit_kb = {c["kb_id"] for c in body["chunks"]}
    # fake_retrieve 用 kb_ids[0] 回显一次；验证展开列表首项是 kb-202 而非 kb-101
    assert hit_kb == {"kb-202"}


def test_virtual_ekp_unregistered_user_no_result():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "nobody"},
                    json={"kb_ids": ["ekp"], "query": "资质"})
    assert r.json()["mode"] == "no_result"


def test_empty_query_rejected():
    r = client.post("/knowledge/api/search", headers={**_H, "X-Client-User": "51-zhang-kj"},
                    json={"kb_ids": ["kb-202"], "query": "  "})
    assert r.status_code == 422 or r.json()["mode"] == "no_result"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd gateway && python3 -m pytest tests/test_knowledge_search.py -x -q`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 实现**

`gateway/knowledge.py` 头部补 import：

```python
from fastapi import Body

from gateway import kb_search
```

模块级常量与虚拟库设置（放在 `_dept_prefixes_for` 之后）：

```python
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
```

目录端点 `client_kbs` 的 `out = []` 之前插一行（虚拟库恒在目录首位；置顶利于 UI 呈现）：

```python
    v = _virtual_kb_entry()
    out = [v] if v else []
```

（原 `out = []` 删除，后续 `out.append(...)` 不变。）

新端点（追加到文件尾）：

```python
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
            requested = [str(k) for k in body.get("kb_ids") or []]
            resolved: list[str] = []
            for kb_id in requested:
                if kb_id == VIRTUAL_KB_ID:
                    resolved.extend(_visible_kb_ids(all_kbs, x_client_user, include_public=False))
                elif kb_id in visible:
                    resolved.append(kb_id)
            # 去重保序；既无可见请求也无 ekp 展开时 no_result
            resolved = list(dict.fromkeys(resolved))
            if not recent and not resolved:
                return {"mode": "no_result", "chunks": [], "sources": []}
            mode, chunks, sources = await kb_search.retrieve(client, query, resolved, recent=recent)
    except (httpx.HTTPError, ValueError):
        return {"mode": "unavailable", "chunks": [], "sources": []}
    return {"mode": mode, "chunks": chunks, "sources": sources}
```

注意：`retrieve` 对 `kb_ids=[]` 的行为是"扇出全部部门库"（`select_kb_ids` 回退语义）——上面 resolved 为空时提前返回 no_result，**不会**触发该回退，权限边界由此保住。`recent=True` 走全局最近检索（WeKnora 全局接口本身无库级鉴权，公共语义可接受；若不接受，删掉 recent 分支即可，见 Step 4）。

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `cd gateway && python3 -m pytest tests/ -x -q`
Expected: 全 PASS。**决策点**：若决定 recent 不对无权限用户开放，把 `recent` 分支改为仅当 `resolved` 非空才放行，并加一条测试 `test_recent_requires_some_visible_kb`。

- [ ] **Step 5: Commit**

```bash
git add gateway/knowledge.py gateway/tests/test_knowledge_search.py
git commit -m "feat(gateway): ekp virtual KB catalog entry + per-user authorized /knowledge/api/search"
```

---

### Task 3: 客户端主进程——网关共享库检索适配器

**Files:**
- Create: `src/main/features/knowledge/gatewayKbSearch.ts`
- Modify: `src/main/features/knowledge/index.ts`（export 新模块，随该文件既有导出风格）
- Test: `src/main/features/knowledge/__tests__/gatewayKbSearch.test.ts`（随邻近目录的既有测试布局；若 main 侧无 `__tests__` 目录，放 `src/main/features/knowledge/gatewayKbSearch.test.ts`）

**Interfaces:**
- Produces:
  - `isGatewayKbId(id: string): boolean` —— 共享库目录里的 id（含 `ekp`）。
  - `listGatewayKbPseudoBases(): Promise<KnowledgeBase[]>` —— 把目录物化为伪 `KnowledgeBase`（status 'completed'，embeddingModelId/dimensions null，id 原样用 wire id）。
  - `gatewayKbSearch(kbId: string, query: string): Promise<KnowledgeSearchResult[]>` —— 调 `/knowledge/api/search`，映射为 `KnowledgeSearchResult[]`；网关 unavailable 抛 `Error`（由调用方归入 per-base failure 路径），no_result 返回 `[]`。
- Consumes: `loadEnterpriseSettings()`（`@main/enterprise/enterpriseSettings`，status==='enabled' 时有 `serverUrl`/`token`）；`readLocalUserName` 语义（同 `sharedKbs.ts`，偏好键 `app.user.name`）；`KnowledgeSearchResultSchema`。

- [ ] **Step 1: 写失败测试**（mock `net.fetch`）

```typescript
// src/main/features/knowledge/__tests__/gatewayKbSearch.test.ts
import { beforeEach, describe, expect, it, vi } from 'vitest'

const fetchMock = vi.fn()
vi.mock('electron', () => ({ net: { fetch: (...a: unknown[]) => fetchMock(...a) } }))
vi.mock('@main/enterprise/enterpriseSettings', () => ({
  loadEnterpriseSettings: () => ({ status: 'enabled', serverUrl: 'http://gw', token: 'tok' })
}))
// app.user.name 偏好读取走 DbService——mock application
const prefValue = { current: '51-zhang-kj' }
vi.mock('@application', () => ({
  application: {
    get: () => ({
      getDb: () => ({
        select: () => ({
          from: () => ({
            where: () => ({
              limit: () => ({
                all: () =>
                  prefValue.current
                    ? [{ value: prefValue.current }]
                    : []
              })
            })
          })
        })
      })
    })
  }
}))

import { gatewayKbSearch, listGatewayKbPseudoBases } from '../gatewayKbSearch'

const GW_RESPONSE = {
  mode: 'ok',
  chunks: [
    { id: 'k1', kb_id: 'kb-202', kb_name: '202-建筑一院', title: '通知A',
      file_name: 'a.md', score: 0.9, content: '第一条正文' },
    { id: 'k1', kb_id: 'kb-202', kb_name: '202-建筑一院', title: '通知A',
      file_name: 'a.md', score: 0.9, content: '第一条正文' }
  ],
  sources: []
}

describe('gatewayKbSearch', () => {
  beforeEach(() => fetchMock.mockReset())

  it('maps ok mode to KnowledgeSearchResult[] with deduped ranks and valid uuidv7 itemIds', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(GW_RESPONSE), { status: 200 }))
    const out = await gatewayKbSearch('ekp', '资质')
    expect(out).toHaveLength(1)
    expect(out[0].pageContent).toBe('第一条正文')
    expect(out[0].score).toBe(0.9)
    expect(out[0].scoreKind).toBe('relevance')
    expect(out[0].rank).toBe(1)
    expect(out[0].title).toBe('通知A')
    expect(out[0].chunkId).toBe('k1')
    // itemId 必须通过 uuidv7 校验（KnowledgeItemIdSchema）
    expect(out[0].itemId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
    )
    // 同一 chunk 的 itemId 稳定（同文档同 chunk 跨调用可关联）
    const again = await gatewayKbSearch('ekp', '资质')
    expect(again[0].itemId).toBe(out[0].itemId)
  })

  it('maps no_result mode to empty array', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ mode: 'no_result', chunks: [], sources: [] }), { status: 200 })
    )
    await expect(gatewayKbSearch('ekp', '资质')).resolves.toEqual([])
  })

  it('throws on unavailable mode so the caller treats it as infra failure', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ mode: 'unavailable', chunks: [], sources: [] }), { status: 200 })
    )
    await expect(gatewayKbSearch('ekp', '资质')).rejects.toThrow()
  })

  it('throws on non-2xx so the caller treats it as infra failure', async () => {
    fetchMock.mockResolvedValue(new Response('unauthorized', { status: 401 }))
    await expect(gatewayKbSearch('ekp', '资质')).rejects.toThrow()
  })

  it('lists pseudo bases from the shared catalog with the virtual kb first', async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          kbs: [
            { id: 'ekp', name: 'ekp数据检索', description: '', scope: 'public' },
            { id: 'kb-202', name: '202-建筑一院', description: '', scope: 'dept' }
          ]
        }),
        { status: 200 }
      )
    )
    const bases = await listGatewayKbPseudoBases()
    expect(bases.map((b) => b.id)).toEqual(['ekp', 'kb-202'])
    for (const b of bases) {
      expect(b.status).toBe('completed')
      expect(b.error).toBeNull()
      expect(b.embeddingModelId).toBeNull()
      expect(b.dimensions).toBeNull()
    }
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pnpm vitest run src/main/features/knowledge/__tests__/gatewayKbSearch.test.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```typescript
// src/main/features/knowledge/gatewayKbSearch.ts
/**
 * 网关共享库检索适配器：把 /knowledge/api/kbs 目录物化为伪 KnowledgeBase（不落库），
 * 把 /knowledge/api/search 映射为 KnowledgeSearchResult[]，供 knowledgeLookup 按
 * baseId 分流。鉴权头部语义与 sharedKbs.listSharedKnowledgeBases 完全一致
 * （X-Client-Token + X-Client-User）；授权判断在网关每次调用时执行，这里不做本地
 * 二次过滤（本地过滤只是 UI 体验，不是安全边界）。
 */
import { and, eq } from 'drizzle-orm'
import { net } from 'electron'

import { application } from '@application'
import { preferenceTable } from '@data/db/schemas/preference'
import { loggerService } from '@logger'
import { loadEnterpriseSettings } from '@main/enterprise/enterpriseSettings'
import {
  GatewaySharedKbsResponseSchema,
  type KnowledgeBase,
  type KnowledgeSearchResult
} from '@shared/data/types/knowledge'

const logger = loggerService.withContext('gatewayKbSearch')

const FETCH_TIMEOUT_MS = 15_000
const PREFERENCE_SCOPE_DEFAULT = 'default'

interface GatewayChunk {
  id: string
  kb_id: string
  kb_name: string
  title: string
  file_name: string
  score: number | null
  content?: string
}

interface GatewaySearchResponse {
  mode: 'ok' | 'no_result' | 'unavailable'
  chunks: GatewayChunk[]
  sources: { id: string; kb_id: string; kb_name: string; title: string }[]
}

export interface GatewayCatalogEntry {
  id: string
  name: string
  description: string
  scope: 'dept' | 'public'
}

export function isGatewayKbId(id: string): boolean {
  return gatewayKbIdCache.has(id)
}

export async function listGatewayKbPseudoBases(): Promise<KnowledgeBase[]> {
  const kbs = await fetchCatalog()
  return kbs.map((kb) => pseudoBase(kb.id, kb.name))
}

export async function gatewayKbSearch(kbId: string, query: string): Promise<KnowledgeSearchResult[]> {
  const settings = loadEnterpriseSettings()
  if (settings.status !== 'enabled') throw new Error('Enterprise gateway is not enabled.')
  const resp = await net.fetch(`${settings.serverUrl}/knowledge/api/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Client-Token': settings.token, 'X-Client-User': readLocalUserName() },
    body: JSON.stringify({ kb_ids: [kbId], query }),
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
  })
  if (!resp.ok) {
    throw new Error(`Gateway knowledge search failed with status ${resp.status}`)
  }
  const body = GatewaySearchResponseSchema.parse(await resp.json())
  if (body.mode !== 'ok') {
    // no_result 是正常空结果（返回 []）；unavailable 按基础设施失败抛出，
    // 让 searchKnowledge 的"全库失败→{error}"路径与本地向量库故障同型。
    if (body.mode === 'unavailable') throw new Error('Gateway knowledge search unavailable')
    return []
  }
  // chunkId 全局去重（WeKnora hybrid-search 可能跨库返回同一 chunk）
  const seen = new Map<string, KnowledgeSearchResult>()
  const results: KnowledgeSearchResult[] = []
  let rank = 1
  for (const chunk of body.chunks) {
    if (seen.has(chunk.id)) continue
    const mapped: KnowledgeSearchResult = {
      pageContent: (chunk.content || '').trim() || `${chunk.title}（${chunk.kb_name}）`,
      score: clampScore(chunk.score),
      scoreKind: 'relevance',
      rank,
      metadata: {
        itemId: deterministicItemId(chunk.kb_id, chunk.id),
        itemType: 'file',
        source: chunk.kb_name || 'ekp数据检索',
        chunkIndex: 0,
        // WeKnora 未给 token 数；按 ~1.6 字符/token 估——metadata 契约要求正整数
        tokenCount: Math.max(1, Math.ceil((chunk.content || chunk.title || '').length / 1.6)),
      },
      itemId: deterministicItemId(chunk.kb_id, chunk.id),
      chunkId: chunk.id,
      title: chunk.title
    }
    seen.set(chunk.id, mapped)
    results.push(mapped)
    rank += 1
  }
  return results
}

// ---------- internals ----------

const GatewaySearchResponseSchema: import('zod').ZodType<GatewaySearchResponse> = {
  parse(value: unknown): GatewaySearchResponse {
    // 网关响应是自有契约（本仓库 Python 侧定义），宽松校验即可：
    // mode 三值枚举 + chunks 数组；字段缺失在映射处兜底。
    const b = (value ?? {}) as Record<string, unknown>
    const mode = b.mode === 'unavailable' ? 'unavailable' : b.mode === 'ok' ? 'ok' : 'no_result'
    const chunks = Array.isArray(b.chunks) ? (b.chunks as GatewayChunk[]) : []
    const sources = Array.isArray(b.sources)
      ? (b.sources as GatewaySearchResponse['sources'])
      : []
    return { mode, chunks, sources }
  }
} as never

function pseudoBase(id: string, name: string): KnowledgeBase {
  const now = new Date().toISOString()
  return {
    id,
    name,
    groupId: null,
    dimensions: null,
    embeddingModelId: null,
    status: 'completed',
    error: null,
    chunkSize: 1024,
    chunkOverlap: 200,
    chunkStrategy: 'structured',
    chunkSeparator: '\\n\\n'
  } as KnowledgeBase
}

function clampScore(score: number | null): number {
  if (score === null || Number.isNaN(score)) return 0
  return Math.max(0, Math.min(1, score))
}

/**
 * WeKnora chunk id（知识库内稳定）→ 确定性 uuidv7 形态 id。
 * KnowledgeItemIdSchema 要求 uuidv7；同 chunk 跨调用必须得到同一 id（kb_read
 * 提示、引用展示都靠它关联）。v7 布局：48bit 时间戳固定值 + 12bit 随机区取 hash。
 */
function deterministicItemId(kbId: string, chunkId: string): string {
  const digest = sha256Hex(`${kbId}/${chunkId}`)
  const timeMs = 0x0190000000000 // 固定纪元，保证 7 开头的 version 位
  const hex = timeMs.toString(16).padStart(12, '0')
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    `7${digest.slice(0, 3)}`,
    `${'89ab'[parseInt(digest[4], 16) % 4]}${digest.slice(5, 8)}`,
    digest.slice(8, 20)
  ].join('-')
}

function sha256Hex(input: string): string {
  // Node 侧主进程直用 crypto（Electron main = Node 环境）
  // eslint 规则：该文件在 main 进程，允许 node:crypto
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { createHash } = require('node:crypto') as typeof import('node:crypto')
  return createHash('sha256').update(input).digest('hex')
}

let catalogCache: { at: number; kbs: GatewayCatalogEntry[] } | null = null
const CATALOG_TTL_MS = 5 * 60_000
const gatewayKbIdCache = new Set<string>()

async function fetchCatalog(): Promise<GatewayCatalogEntry[]> {
  if (catalogCache && Date.now() - catalogCache.at < CATALOG_TTL_MS) return catalogCache.kbs
  const settings = loadEnterpriseSettings()
  if (settings.status !== 'enabled') return []
  const resp = await net.fetch(`${settings.serverUrl}/knowledge/api/kbs`, {
    headers: { 'X-Client-Token': settings.token, 'X-Client-User': readLocalUserName() },
    redirect: 'follow',
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS)
  })
  if (!resp.ok) {
    logger.warn('Gateway KB catalog fetch failed', { status: resp.status })
    return []
  }
  const kbs = GatewaySharedKbsResponseSchema.parse(await resp.json()).kbs
  gatewayKbIdCache.clear()
  for (const kb of kbs) gatewayKbIdCache.add(kb.id)
  catalogCache = { at: Date.now(), kbs }
  return kbs
}

function readLocalUserName(): string {
  try {
    const db = application.get('DbService').getDb()
    const [row] = db
      .select({ value: preferenceTable.value })
      .from(preferenceTable)
      .where(and(eq(preferenceTable.scope, PREFERENCE_SCOPE_DEFAULT), eq(preferenceTable.key, 'app.user.name')))
      .limit(1)
      .all()
    return typeof row?.value === 'string' ? row.value : ''
  } catch (error) {
    logger.warn('Failed to read local user name', { error: String(error) })
    return ''
  }
}
```

并在 `src/main/features/knowledge/index.ts` 补导出（随既有导出风格）：`export * from './gatewayKbSearch'`。若 lint 拒绝 `require`，把 `sha256Hex` 改为文件顶部 `import { createHash } from 'node:crypto'`（main 进程文件允许 Node 内置，参考仓库内其他 main 文件的用法）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pnpm vitest run src/main/features/knowledge/__tests__/gatewayKbSearch.test.ts`
Expected: 5 个用例全 PASS（伪 KB 断言若因 `KnowledgeBase` 类型多必填字段报类型错，补齐字段为类型默认值——以 `KnowledgeBaseEntitySchema` strictObject 为准，绝不引入迁移）。

- [ ] **Step 5: Commit**

```bash
git add src/main/features/knowledge/gatewayKbSearch.ts src/main/features/knowledge/index.ts src/main/features/knowledge/__tests__/gatewayKbSearch.test.ts
git commit -m "feat(client): gateway shared-KB search adapter with deterministic uuidv7 item ids"
```

---

### Task 4: 客户端——knowledgeLookup 按 baseId 分流（agent 调用入口）

**Files:**
- Modify: `src/main/ai/tools/knowledgeLookup.ts:166-232`（`searchKnowledge`）、`src/main/ai/tools/knowledgeLookup.ts:452-460`（`listOrOutlineKnowledge`）、`src/main/ai/tools/knowledgeLookup.ts:620-654`（`listKnowledgeBases`）
- Test: `src/main/ai/tools/__tests__/knowledgeLookup.test.ts`（追加用例）

**Interfaces:**
- Consumes: Task 3 的 `isGatewayKbId` / `gatewayKbSearch` / `listGatewayKbPseudoBases`。
- Produces: `searchKnowledge(query, baseIds, allowedIds)` 与 `listOrOutlineKnowledge(input, allowedIds)` 对网关 id 透明——本地调用方（AI-SDK 工具、MCP bridge）零改动。

- [ ] **Step 1: 写失败测试**（追加到既有测试文件，mock 模块级 `gatewayKbSearch`）

```typescript
// 追加到 src/main/ai/tools/__tests__/knowledgeLookup.test.ts
import { gatewayKbSearch, listGatewayKbPseudoBases } from '@main/features/knowledge/gatewayKbSearch'

vi.mock('@main/features/knowledge/gatewayKbSearch', () => ({
  isGatewayKbId: vi.fn((id: string) => id === 'ekp' || id === 'kb-202'),
  gatewayKbSearch: vi.fn(),
  listGatewayKbPseudoBases: vi.fn()
}))

describe('searchKnowledge routes gateway kb ids', () => {
  it('routes a gateway id to gatewayKbSearch, not KnowledgeService', async () => {
    vi.mocked(gatewayKbSearch).mockResolvedValue([
      {
        pageContent: '正文', score: 0.9, scoreKind: 'relevance', rank: 1,
        chunkId: 'k1', itemId: '01900000-0000-7000-8000-000000000001',
        metadata: { itemId: '01900000-0000-7000-8000-000000000001', itemType: 'file',
                    source: '202-建筑一院', chunkIndex: 0, tokenCount: 10 },
        title: '通知A'
      }
    ])
    const localSearch = vi.fn()
    vi.doMock('@application', () => ({ application: { get: () => ({ search: localSearch }) } }))
    const { searchKnowledge } = await import('../knowledgeLookup')
    const out = await searchKnowledge('资质', ['ekp'], [])
    expect(gatewayKbSearch).toHaveBeenCalledWith('ekp', '资质')
    expect(localSearch).not.toHaveBeenCalled()
    expect(out).toHaveLength(1)
    expect(out[0].baseId).toBe('ekp')
  })

  it('mixed local+gateway ids each hit their own backend, merged and sorted by score', async () => {
    vi.mocked(gatewayKbSearch).mockResolvedValue([
      { pageContent: 'gw', score: 0.5, scoreKind: 'relevance', rank: 1,
        chunkId: 'k2', itemId: '01900000-0000-7000-8000-000000000002',
        metadata: { itemId: '01900000-0000-7000-8000-000000000002', itemType: 'file',
                    source: 's', chunkIndex: 0, tokenCount: 4 }, title: 'T2' }
    ])
    // 本地库走既有 mock 的 KnowledgeService（见文件顶部既有 fixture），返回 score 0.9 的一条
    const { searchKnowledge } = await import('../knowledgeLookup')
    const out = await searchKnowledge('q', ['<既有本地库id>', 'ekp'], [])
    expect(out[0].score).toBeGreaterThanOrEqual(out[out.length - 1].score)
    expect(out.map((r) => r.baseId)).toContain('ekp')
  })

  it('gateway failure alone yields { error } (all targeted bases failed)', async () => {
    vi.mocked(gatewayKbSearch).mockRejectedValue(new Error('unavailable'))
    const { searchKnowledge } = await import('../knowledgeLookup')
    const out = await searchKnowledge('q', ['ekp'], [])
    expect(out).toHaveProperty('error')
  })
})

describe('kb_list includes gateway pseudo bases', () => {
  it('appends shared catalog entries to the base list', async () => {
    vi.mocked(listGatewayKbPseudoBases).mockResolvedValue([
      { id: 'ekp', name: 'ekp数据检索', groupId: null, dimensions: null, embeddingModelId: null,
        status: 'completed', error: null, chunkSize: 1024, chunkOverlap: 200,
        chunkStrategy: 'structured', chunkSeparator: '\\n\\n' } as never
    ])
    const { listOrOutlineKnowledge } = await import('../knowledgeLookup')
    const out = await listOrOutlineKnowledge({ limit: 50 }, [])
    if (!('items' in out)) throw new Error('expected list mode')
    expect(out.items.map((i) => i.id)).toContain('ekp')
  })
})
```

（`<既有本地库id>` 用测试文件既有 fixture 里的本地库 id；混排用例依赖该 fixture 的 `KnowledgeService.search` mock 返回 score 0.9 结果——若既有 fixture 不同，按它的返回改断言下界为 `out.length === 2`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `pnpm vitest run src/main/ai/tools/__tests__/knowledgeLookup.test.ts`
Expected: 新用例 FAIL（现在网关 id 被当本地库查 NOT_FOUND）

- [ ] **Step 3: 实现**

`knowledgeLookup.ts` 顶部加：

```typescript
import { gatewayKbSearch, isGatewayKbId, listGatewayKbPseudoBases } from '@main/features/knowledge/gatewayKbSearch'
```

`searchKnowledge` 的逐库 `Promise.all` 内、`knowledgeService.search` 调用处分流（保持 per-base `{ok}` 包装与错误语义不变）：

```typescript
  const perBase = await Promise.all(
    targetIds.map(async (baseId) => {
      try {
        const results = isGatewayKbId(baseId)
          ? await gatewayKbSearch(baseId, query)
          : await knowledgeService.search(baseId, query)
        return { ok: true as const, results: results.map((result) => ({ result, baseId })) }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        logger.warn('Knowledge search failed', { baseId, query, gateway: isGatewayKbId(baseId), error: message })
        return { ok: false as const, error: message }
      }
    })
  )
```

（`const knowledgeService = application.get('KnowledgeService')` 移到 `if (targetIds.length === 0) return []` 之后、`Promise.all` 之前即可，本地/网关两路共用。）

`listKnowledgeBases`：在 `page.items.map(...)` 得到 `items` 后、return 前，合并目录（失败降级为不追加，与 catalog 的静默降级姿态一致）：

```typescript
    let gatewayItems: KbListOutputItem[] = []
    try {
      const gatewayBases = await listGatewayKbPseudoBases()
      const localIds = new Set(page.items.map((base) => base.id))
      gatewayItems = gatewayBases
        .filter((base) => !localIds.has(base.id))
        .map((base) => ({
          id: base.id,
          name: base.name,
          groupId: null,
          status: 'completed' as const,
          itemCount: 0,
          itemsUnavailable: true
        }))
    } catch (error) {
      logger.warn('Gateway KB catalog unavailable for kb_list; listing local bases only', {
        error: error instanceof Error ? error.message : String(error)
      })
    }
    return {
      items: [...items, ...gatewayItems],
      total: page.total + gatewayItems.length,
      ...(page.nextCursor ? { nextCursor: page.nextCursor } : {})
    }
```

（`itemsUnavailable: true` 让模型不把它当"空库"；目录里置顶的 `ekp` 排在前面正合适。`KbListOutputItem` 若无 `itemsUnavailable` 之外的字段冲突，按类型实际形状微调。）

**kb_read / kb_manage / kb_list(outline 模式) 对网关 id**：不加通——网关库只有检索语义。在 `readOrGrepConcept`、`manageKnowledge`、`readTree` 的 `allowedIds` 检查之后各加：

```typescript
  if (isGatewayKbId(baseId)) {
    return { error: `Knowledge base "${baseId}" is a shared (gateway) knowledge base — only kb_search is supported. Use kb_search with baseIds: ["${baseId}"].` }
  }
```

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `pnpm vitest run src/main/ai/tools/`
Expected: 全 PASS（既有用例不受影响——本地 id 的 `isGatewayKbId` 为 false 走原路径）

- [ ] **Step 5: Commit**

```bash
git add src/main/ai/tools/knowledgeLookup.ts src/main/ai/tools/__tests__/knowledgeLookup.test.ts
git commit -m "feat(client): route gateway shared KB ids to /knowledge/api/search inside kb_search/kb_list"
```

---

### Task 5: 客户端——composer 与 agent scope 收编网关库

**Files:**
- Modify: composer 侧 `allKnowledgeBases` 的来源（chat 与 agent 两个 composer 共用的 `useComposerKnowledgeBaseScope` 调用方——用 `grep -rn "useComposerKnowledgeBaseScope(" src/renderer` 定位调用点，通常在 `src/renderer/components/composer/variants/chat/` 与 `.../agent/` 各一处）
- Modify: `src/main/ai/agentSession/agentSessionRuntimeState.ts` 中 `resolveKnowledgeBaseScope`（allowedIds 计算处——先 `grep -n "resolveKnowledgeBaseScope" src/main -r` 确认实际所在文件）
- Test: 各改动文件邻近的既有测试文件追加用例

**Interfaces:**
- Consumes: Task 3 的 `listGatewayKbPseudoBases`（renderer 侧经 IPC 拿不到 main 模块——因此本任务先在 `src/main/ipc/handlers/knowledge.ts` 加一条 IPC 通道 `knowledge.list_gateway_pseudo_bases`，随既有 `knowledge.list_shared_kbs` 的模式注册，renderer 经 `ipcApi.request` 调用）。
- Produces: composer 的 `allKnowledgeBases`（可选列表）与 agent 的 `allowedIds` 天花板都包含网关库。

- [ ] **Step 1: IPC 通道**——在 `src/main/ipc/handlers/knowledge.ts` 依 `knowledge.list_shared_kbs` 的注册样式加：

```typescript
// 返回网关共享库的伪 KnowledgeBase 列表（含 ekp数据检索）；失败降级为 []。
// 与 list_shared_kbs 的区别：这里产出 composer/scope 可直接合并的 KnowledgeBase 形状。
ipcMain.handle('knowledge.list_gateway_pseudo_bases', async () => {
  try {
    return await listGatewayKbPseudoBases()
  } catch {
    return []
  }
})
```

（`listGatewayKbPseudoBases` 从 `@main/features/knowledge` import；类型声明随该文件既有 IPC 类型注册处同步补到 `ipcApi` 的 contract 类型里。）

- [ ] **Step 2: composer 合并**——两个 composer 调用点把伪库并入 `allKnowledgeBases`：

```typescript
  const { payload: gatewayBases } = useSWR<KnowledgeBase[]>(
    'knowledge.list_gateway_pseudo_bases',
    () => ipcApi.request('knowledge.list_gateway_pseudo_bases'),
    { shouldRetryOnError: false, revalidateOnFocus: false, revalidateIfStale: false }
  )
  const mergedKnowledgeBases = useMemo(
    () => [...(allKnowledgeBases ?? []), ...(gatewayBases ?? [])],
    [allKnowledgeBases, gatewayBases]
  )
  // 原 allKnowledgeBases 传给 useComposerKnowledgeBaseScope 的地方改传 mergedKnowledgeBases
```

- [ ] **Step 3: scope 收编**——在 `resolveKnowledgeBaseScope` 实现处，允许的 id 集合并入网关库 id。原则：**agent 绑定的天花板对网关库同样生效**——绑定里没有的网关库不进 allowedIds（`searchKnowledge` 会丢弃，防越权选择）。实现按该文件现有的"绑定 ∪ 本轮选择"语义把伪库 id 一并纳入（具体 diff 以现场代码为准，不改语义只加数据源）。

- [ ] **Step 4: 测试**——为 Step 2/3 各加一个用例：composer 渲染后知识库选择器出现「ekp数据检索」；agent 绑定含 `ekp` 时 `allowedIds` 含 `ekp`、不含未绑定的其他网关库。运行：

Run: `pnpm vitest run src/renderer/components/composer src/main/ai/agentSession`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A src/main/ipc/handlers/knowledge.ts src/renderer/components/composer src/main/ai/agentSession
git commit -m "feat(client): surface gateway shared KBs in composer selection and agent scope"
```

---

### Task 6: 端到端验证（网关服务器 + 真实客户端）

**Files:** 无新代码；部署 + 手工验证清单。

- [ ] **Step 1: 部署网关**——按仓库既有部署流程把 `gateway/` 同步到 66.12 并重启网关服务（沿用 `DEPLOY_BT.md`/`deploy_bot.sh` 的既有流程）。

- [ ] **Step 2: curl 验证三连**（在网关服务器或 Mac 上）：

```bash
# 1) 目录里有 ekp数据检索，且部门库按用户过滤
curl -s http://<gateway>/knowledge/api/kbs -H "X-Client-Token: <token>" -H "X-Client-User: 51-zhang-kj" | python3 -m json.tool
# 2) 虚拟库检索（应命中多个部门库的融合结果，chunk 带 content）
curl -s -X POST http://<gateway>/knowledge/api/search \
  -H "X-Client-Token: <token>" -H "X-Client-User: 51-zhang-kj" -H "Content-Type: application/json" \
  -d '{"kb_ids": ["ekp"], "query": "资质 动态核查"}' | python3 -m json.tool | head -40
# 3) 越权验证：换一个 user_departments 未登记的用户名，ekp 应返回 no_result（不是 500/403 泄露）
curl -s -X POST http://<gateway>/knowledge/api/search \
  -H "X-Client-Token: <token>" -H "X-Client-User: nobody" -H "Content-Type: application/json" \
  -d '{"kb_ids": ["ekp"], "query": "资质"}'
```

Expected: (1) `kbs` 首位是 `ekp`；(2) `mode:"ok"` 且 chunks 有非空 content；(3) `mode:"no_result"`。

- [ ] **Step 3: 客户端联调**——`pnpm dev` 启动客户端（企业配置指向网关，`CHERRY_ENTERPRISE_*` env，见 memory desktop-vision），验证：
  1. 知识库页与 composer 知识库选择器出现「ekp数据检索」；
  2. 新建对话选中「ekp数据检索」，问"资质动态核查的要求是什么"，agent 调 `kb_search(baseIds:["ekp"])` 并带 `[cite:id]` 引用；
  3. `kb_list` 能看到共享库；对 `ekp` 调 `kb_read` 得到"仅支持 kb_search"的 steer；
  4. 断网关（docker stop）后检索返回"检索失败"类文案，模型如实告知而非编造。

- [ ] **Step 4: Commit（如有联调修正）** 并在计划文件勾掉全部步骤。

---

## 自查记录（Self-Review）

- **规格覆盖**：公共库全员可搜（Task 2 include_public + 目录 scope=public）✓；下发鉴权（既有 `/knowledge/api/kbs` + user_departments，本方案复用）✓；调用鉴权（Task 2 服务端交集，ekp 在用户可见范围内展开）✓；调用方式与原生一致（Task 4/5 走同一 `kb_search`/composer 链路）✓；存储分层不改原结构（伪对象方案，零迁移）✓；个人库不受影响（`isGatewayKbId` false 走原路径，既有用例回归保障）✓。
- **占位符**：Task 5 的 Step 2/3 给了代码骨架 + 定位指令（composer 调用点与 `resolveKnowledgeBaseScope` 所在文件依赖现场代码，已给出 grep 定位命令与语义约束，非"以后再说"式占位）。
- **类型一致性**：`GatewayChunk.content`（Task 1 产出）→ `gatewayKbSearch` 映射（Task 3）→ `KnowledgeSearchResult`（既有 schema）→ `searchKnowledge` 分流（Task 4）名称一致；`ekp` wire id 在网关常量 `VIRTUAL_KB_ID` 与客户端 `isGatewayKbId` 两侧一致。
