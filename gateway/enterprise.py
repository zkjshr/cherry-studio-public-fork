"""企业配置存储与下发：SQLite 保存上游/助手/MCP/小程序等结构化配置，发布时组合为
客户端配置快照写入 config_versions，客户端按版本拉取；/v1 模型代理每次调用另记
usage_log（见 model_proxy）。

客户端 GET /api/client/config 带 X-Client-Token 鉴权，If-None-Match 命中最新版本
返回 304；管理端经 /admin 页面或 /admin/api/* 维护结构化配置并发布（X-Admin-Token 鉴权）。
"""
import copy
import json
import os
import re
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from html import escape as html_escape
from urllib.parse import quote

from gateway.config import settings

router = APIRouter()

# 存储路径默认 gateway/data/enterprise.sqlite3（目录运行时建）；测试用 ENTERPRISE_DB_PATH 覆盖
_DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "enterprise.sqlite3")
_lock = threading.Lock()
logger = logging.getLogger(__name__)
_conn: sqlite3.Connection | None = None
_conn_path = ""

# admin_settings 四键的缺省值（GET 返回合并结果；PUT 只更新提供的键，按键合并）。
# world_url：小世界（Three.js 园区）服务地址，空串 = 未部署（客户端不下发该键）。
_DEFAULT_SETTINGS = {
    "public_base_url": "http://127.0.0.1:8787",
    "default_models": {},
    "kb_entries": {"recent_enabled": False, "kb_ids": []},
    "world_url": "",
    "base_system_prompt": "",
    # 账号 → 部门库名前缀（如 {"51-zhang-kj": "103"} 或 ["202","101"]）；共享库目录/检索
    # 的部门可见性映射（knowledge._dept_prefixes_for）。未登记用户部门分区为空。
    "user_departments": {},
}
_DEFAULT_MODEL_ROLES = ("assistant", "translate", "quick_model")
_MCP_TYPES = ("sse", "streamableHttp")
_BUILTIN_MCP_NAME = "weknora"  # 内置公共知识库 MCP：发布组合恒下发，管理行仅控 is_active，不可删


class NotFoundError(LookupError):
    """资源不存在（API 层转译为 404）。"""


class ConflictError(RuntimeError):
    """唯一性或引用冲突（API 层转译为 409）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    """惰性建连并建表（幂等 CREATE IF NOT EXISTS，老库自动补新表）；路径变化（如测试换临时库）时重开。须持有 _lock 调用。"""
    global _conn, _conn_path
    path = os.environ.get("ENTERPRISE_DB_PATH", _DEFAULT_DB_PATH)
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        _conn.close()
        _conn = None
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS config_versions ("
        "version INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, published_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS upstreams ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "protocol TEXT NOT NULL CHECK(protocol IN ('openai','ollama','weknora')), "
        "base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, "
        "models TEXT NOT NULL DEFAULT '[]', updated_at TEXT)"
    )
    # 老库 CHECK 不含 weknora：SQLite 无法 ALTER 约束，检测后整表重建（本库无外键引用，安全）
    up_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='upstreams'").fetchone()
    if up_sql and "weknora" not in up_sql[0]:
        conn.execute("ALTER TABLE upstreams RENAME TO upstreams_old")
        conn.execute(
            "CREATE TABLE upstreams ("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "protocol TEXT NOT NULL CHECK(protocol IN ('openai','ollama','weknora')), "
            "base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, "
            "models TEXT NOT NULL DEFAULT '[]', updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO upstreams SELECT id, name, protocol, base_url, api_key, enabled, models, updated_at"
            " FROM upstreams_old")
        conn.execute("DROP TABLE upstreams_old")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS admin_settings ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS admin_assistants ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, prompt TEXT DEFAULT '', emoji TEXT DEFAULT '✨', "
        "description TEXT DEFAULT '', model_ref TEXT DEFAULT '', settings TEXT DEFAULT '{}', "
        "mcp_names TEXT DEFAULT '[]', updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS admin_mcp_servers ("
        "name TEXT PRIMARY KEY, type TEXT NOT NULL DEFAULT 'sse', base_url TEXT DEFAULT '', "
        "headers TEXT DEFAULT '{}', is_active INTEGER DEFAULT 1, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS admin_minapps ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, model TEXT NOT NULL, upstream_id TEXT, "
        "prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, duration_ms INTEGER, status TEXT)"
    )
    # admin_assistants.pinned_topic：企业下发"置顶对话"（ALTER ADD COLUMN 需探测幂等）
    a_cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_assistants)").fetchall()}
    if "pinned_topic" not in a_cols:
        conn.execute("ALTER TABLE admin_assistants ADD COLUMN pinned_topic TEXT")
    # WeKnora 会话映射：客户端对话指纹 → WeKnora session_id（model_proxy 的 weknora 协议读写）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS weknora_sessions ("
        "conv_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent_id TEXT NOT NULL, created_at TEXT)"
    )
    conn.commit()
    _conn, _conn_path = conn, path
    return conn


def init_db() -> None:
    """显式预热（幂等）：部署时可先建库建表，不必等首个请求触发。"""
    with _lock:
        _connect()


# ---------- 上游注册表 ----------

def _upstream_row(row) -> dict:
    return {"id": row[0], "name": row[1], "protocol": row[2], "base_url": row[3], "api_key": row[4],
            "enabled": bool(row[5]), "models": json.loads(row[6]), "updated_at": row[7]}


def list_upstreams() -> list[dict]:
    with _lock:
        rows = _connect().execute("SELECT id, name, protocol, base_url, api_key, enabled, models, updated_at"
                                  " FROM upstreams ORDER BY id").fetchall()
    return [_upstream_row(r) for r in rows]


def get_upstream(uid: str) -> dict | None:
    with _lock:
        row = _connect().execute("SELECT id, name, protocol, base_url, api_key, enabled, models, updated_at"
                                 " FROM upstreams WHERE id=?", (uid,)).fetchone()
    return _upstream_row(row) if row else None


def upsert_upstream(uid: str, data: dict) -> None:
    """全量 upsert（data 为 API 层校验归一后的结果）。"""
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO upstreams (id, name, protocol, base_url, api_key, enabled, models, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (uid, data["name"], data["protocol"], data["base_url"], data["api_key"],
             int(data["enabled"]), json.dumps(data["models"], ensure_ascii=False), _now()),
        )
        conn.commit()


def delete_upstream(uid: str) -> None:
    """删除上游；被助手 model_ref 引用时抛 ConflictError（避免发布组合出现悬空引用）。"""
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM upstreams WHERE id=?", (uid,)).fetchone() is None:
            raise NotFoundError(f"upstream not found: {uid}")
        for aid, ref in conn.execute("SELECT id, model_ref FROM admin_assistants").fetchall():
            if ref and ref.partition("/")[0] == uid:
                raise ConflictError(f"upstream {uid} is referenced by assistant {aid} (model_ref={ref})")
        conn.execute("DELETE FROM upstreams WHERE id=?", (uid,))
        conn.commit()


# ---------- WeKnora 会话映射（model_proxy 的 weknora 协议读写） ----------

def get_weknora_session(conv_key: str) -> dict | None:
    """按对话指纹取映射；不存在返回 None（调用方按需懒建会话）。"""
    with _lock:
        row = _connect().execute(
            "SELECT conv_key, session_id, agent_id FROM weknora_sessions WHERE conv_key=?", (conv_key,)).fetchone()
    return {"conv_key": row[0], "session_id": row[1], "agent_id": row[2]} if row else None


def save_weknora_session(conv_key: str, session_id: str, agent_id: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO weknora_sessions (conv_key, session_id, agent_id, created_at)"
            " VALUES (?,?,?,?)", (conv_key, session_id, agent_id, _now()))
        conn.commit()


def delete_weknora_session(conv_key: str) -> None:
    """会话在 WeKnora 侧失效（404）后清除映射，供重建。"""
    with _lock:
        conn = _connect()
        conn.execute("DELETE FROM weknora_sessions WHERE conv_key=?", (conv_key,))
        conn.commit()


# ---------- 设置 ----------

def get_settings() -> dict:
    """三键设置（缺省值与已存键合并）。"""
    merged = copy.deepcopy(_DEFAULT_SETTINGS)
    with _lock:
        rows = _connect().execute("SELECT key, value FROM admin_settings").fetchall()
    for key, value in rows:
        merged[key] = json.loads(value)
    return merged


def save_settings(data: dict) -> None:
    """按提供的键写入；json.dumps 拒绝 NaN/Infinity（ValueError → API 层 400）。"""
    with _lock:
        conn = _connect()
        for key, value in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO admin_settings (key, value, updated_at) VALUES (?,?,?)",
                (key, json.dumps(value, ensure_ascii=False, allow_nan=False), _now()),
            )
        conn.commit()


# ---------- 助手 / MCP / 小程序 ----------

def _assistant_row(row) -> dict:
    return {"id": row[0], "name": row[1], "prompt": row[2], "emoji": row[3], "description": row[4],
            "model_ref": row[5], "settings": json.loads(row[6]), "mcp_names": json.loads(row[7]),
            "updated_at": row[8], "pinned_topic": json.loads(row[9]) if row[9] else None}


_ASSISTANT_COLS = "id, name, prompt, emoji, description, model_ref, settings, mcp_names, updated_at, pinned_topic"


def list_assistants() -> list[dict]:
    with _lock:
        rows = _connect().execute(f"SELECT {_ASSISTANT_COLS} FROM admin_assistants ORDER BY id").fetchall()
    return [_assistant_row(r) for r in rows]


def get_assistant(aid: str) -> dict | None:
    with _lock:
        row = _connect().execute(f"SELECT {_ASSISTANT_COLS} FROM admin_assistants WHERE id=?", (aid,)).fetchone()
    return _assistant_row(row) if row else None


def upsert_assistant(aid: str, data: dict) -> None:
    """name 全局唯一：与他条冲突抛 ConflictError（API 层 409）；settings 含 NaN 时 ValueError → 400。"""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT id FROM admin_assistants WHERE name=? AND id<>?", (data["name"], aid)).fetchone()
        if row is not None:
            raise ConflictError(f"assistant name '{data['name']}' already used by {row[0]}")
        conn.execute(
            f"INSERT OR REPLACE INTO admin_assistants ({_ASSISTANT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, data["name"], data["prompt"], data["emoji"], data["description"], data["model_ref"],
             json.dumps(data["settings"], ensure_ascii=False, allow_nan=False),
             json.dumps(data["mcp_names"], ensure_ascii=False), _now(),
             json.dumps(data["pinned_topic"], ensure_ascii=False) if data.get("pinned_topic") else None),
        )
        conn.commit()


def delete_assistant(aid: str) -> None:
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM admin_assistants WHERE id=?", (aid,)).fetchone() is None:
            raise NotFoundError(f"assistant not found: {aid}")
        conn.execute("DELETE FROM admin_assistants WHERE id=?", (aid,))
        conn.commit()


def _mcp_row(row) -> dict:
    return {"name": row[0], "type": row[1], "base_url": row[2], "headers": json.loads(row[3]),
            "is_active": bool(row[4]), "updated_at": row[5]}


_MCP_COLS = "name, type, base_url, headers, is_active, updated_at"


def list_mcp_servers() -> list[dict]:
    with _lock:
        rows = _connect().execute(f"SELECT {_MCP_COLS} FROM admin_mcp_servers ORDER BY name").fetchall()
    return [_mcp_row(r) for r in rows]


def get_mcp_server(name: str) -> dict | None:
    with _lock:
        row = _connect().execute(f"SELECT {_MCP_COLS} FROM admin_mcp_servers WHERE name=?", (name,)).fetchone()
    return _mcp_row(row) if row else None


def upsert_mcp_server(name: str, data: dict) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            f"INSERT OR REPLACE INTO admin_mcp_servers ({_MCP_COLS}) VALUES (?,?,?,?,?,?)",
            (name, data["type"], data["base_url"],
             json.dumps(data["headers"], ensure_ascii=False), int(data["is_active"]), _now()),
        )
        conn.commit()


def _builtin_weknora_mcp_entry(public_base_url: str) -> dict:
    """内置 WeKnora MCP（公共知识库，人人默认下发）：base_url 由 public_base_url 拼出，
    is_active 取同名管理行（可整体停用）；该行不可删除。"""
    row = get_mcp_server(_BUILTIN_MCP_NAME)
    return {"name": _BUILTIN_MCP_NAME, "type": "streamableHttp",
            "base_url": f'{public_base_url.rstrip("/")}/mcp/weknora',
            "headers": {}, "is_active": bool(row["is_active"]) if row else True}


def get_base_system_prompt() -> str:
    """服务端强制注入的基座系统提示词（空串 = 不注入）；仅服务端使用，不进发布组合。"""
    return str(get_settings().get("base_system_prompt") or "")


def delete_mcp_server(name: str) -> None:
    """删除 MCP 服务；被助手 mcp_names 引用时抛 ConflictError（与上游删除同语义）。"""
    if name == _BUILTIN_MCP_NAME:
        raise ConflictError("内置 WeKnora MCP 不可删除（只可停用）")
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM admin_mcp_servers WHERE name=?", (name,)).fetchone() is None:
            raise NotFoundError(f"mcp server not found: {name}")
        for aid, names in conn.execute("SELECT id, mcp_names FROM admin_assistants").fetchall():
            if name in json.loads(names):
                raise ConflictError(f"mcp server {name} is referenced by assistant {aid}")
        conn.execute("DELETE FROM admin_mcp_servers WHERE name=?", (name,))
        conn.commit()


def _minapp_row(row) -> dict:
    return {"id": row[0], "name": row[1], "url": row[2], "updated_at": row[3]}


_MINAPP_COLS = "id, name, url, updated_at"


def list_minapps() -> list[dict]:
    with _lock:
        rows = _connect().execute(f"SELECT {_MINAPP_COLS} FROM admin_minapps ORDER BY id").fetchall()
    return [_minapp_row(r) for r in rows]


def get_minapp(app_id: str) -> dict | None:
    with _lock:
        row = _connect().execute(f"SELECT {_MINAPP_COLS} FROM admin_minapps WHERE id=?", (app_id,)).fetchone()
    return _minapp_row(row) if row else None


def upsert_minapp(app_id: str, data: dict) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            f"INSERT OR REPLACE INTO admin_minapps ({_MINAPP_COLS}) VALUES (?,?,?,?)",
            (app_id, data["name"], data["url"], _now()),
        )
        conn.commit()


def delete_minapp(app_id: str) -> None:
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM admin_minapps WHERE id=?", (app_id,)).fetchone() is None:
            raise NotFoundError(f"minapp not found: {app_id}")
        conn.execute("DELETE FROM admin_minapps WHERE id=?", (app_id,))
        conn.commit()


# ---------- 发布组合 / 版本 ----------

def compose_client_config() -> dict:
    """结构化配置 → 客户端配置（契约见 docs/superpowers/plans/2026-10-03-e2e3-admin-client-full.md）。

    引用完整性校验收集全部明细后抛 ValueError（API 层转 400，不落版本）。
    仅 enabled 上游的 visible 模型进入 providers.models；api_key 恒为网关令牌，
    真实上游 key 只存 upstreams 表不下发。
    """
    errors: list[str] = []
    by_id = {u["id"]: u for u in list_upstreams()}

    def find_model(ref: str):
        """model_ref "{upstream_id}/{model_id}" → (upstream, model)，不存在返回 (None, None)。"""
        uid, _, mid = ref.partition("/")
        for m in by_id.get(uid, {}).get("models", []):
            if m["id"] == mid:
                return by_id[uid], m
        return None, None

    st = get_settings()
    default_models: dict[str, str] = {}
    for role, ref in st["default_models"].items():
        if not ref:
            continue
        up, m = find_model(ref)
        if up is None:
            errors.append(f"default_models.{role}: model not found: {ref}")
        elif not up["enabled"] or not m.get("visible", True):
            errors.append(f"default_models.{role}: model not visible: {ref}")
        else:
            default_models[role] = f"gateway/{ref}"

    providers_models = [
        {"id": f'{up["id"]}/{m["id"]}', "name": m.get("name") or m["id"]}
        for up in by_id.values() if up["enabled"]
        for m in up["models"] if m.get("visible", True)
    ]

    mcp_names = {s["name"] for s in list_mcp_servers()} | {_BUILTIN_MCP_NAME}
    assistants: list[dict] = []
    for a in list_assistants():
        model = None
        if a["model_ref"]:
            up, _m = find_model(a["model_ref"])
            if up is None:
                errors.append(f'assistants[{a["name"]}]: model not found: {a["model_ref"]}')
            else:
                model = f'gateway/{a["model_ref"]}'
        for n in a["mcp_names"]:
            if n not in mcp_names:
                errors.append(f'assistants[{a["name"]}]: mcp server not found: {n}')
        entry: dict = {"name": a["name"], "prompt": a["prompt"], "emoji": a["emoji"],
                       "description": a["description"], "settings": a["settings"],
                       "mcp_server_ids": a["mcp_names"], "managed": True}
        if model:
            entry["model"] = model
        if a.get("pinned_topic"):
            entry["pinned_topic"] = a["pinned_topic"]
        assistants.append(entry)

    if errors:
        raise ValueError("；".join(errors))
    return {
        "providers": [{"id": "gateway", "name": "所里模型网关",
                       "base_url": f'{st["public_base_url"].rstrip("/")}/v1',
                       "api_key": settings.gateway_token, "models": providers_models}],
        "default_models": default_models,
        "assistants": assistants,
        "mcp_servers": [_builtin_weknora_mcp_entry(st["public_base_url"])]
                        + [{"name": s["name"], "type": s["type"], "base_url": s["base_url"],
                            "headers": s["headers"], "is_active": s["is_active"]}
                           for s in list_mcp_servers() if s["name"] != _BUILTIN_MCP_NAME],
        "minapps": [{"id": a["id"], "name": a["name"], "url": a["url"]} for a in list_minapps()],
        "kb_entries": st["kb_entries"],
        # 小世界：仅在配置了地址时顶层透出（客户端未拿到该键即视为未部署）
        **({"world_url": st["world_url"].rstrip("/")} if st["world_url"].strip() else {}),
    }


def publish_composed() -> tuple[int, dict]:
    """组合当前结构化配置并落新版本；校验失败抛 ValueError。返回 (新版本号, 内容)。"""
    content = compose_client_config()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO config_versions (content, published_at) VALUES (?, ?)",
            (json.dumps(content, ensure_ascii=False, allow_nan=False), _now()),
        )
        version = int(cur.lastrowid)
        conn.commit()
    return version, content


def _latest_row() -> tuple[int, str, str] | None:
    """最新版本原始行 (version, content, published_at)，客户端下发与管理端视图共用。"""
    with _lock:
        row = _connect().execute(
            "SELECT version, content, published_at FROM config_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
    return (int(row[0]), row[1], row[2]) if row else None


def get_published() -> dict:
    """管理端发布视图：latest 全量 + versions 元数据（新→旧）。"""
    row = _latest_row()
    with _lock:
        versions = _connect().execute(
            "SELECT version, published_at FROM config_versions ORDER BY version DESC").fetchall()
    return {
        "latest": (
            {"version": row[0], "published_at": row[2], "content": json.loads(row[1])} if row else None
        ),
        "versions": [{"version": int(v), "published_at": p} for v, p in versions],
    }


def rollback_to(version: int) -> tuple[int, dict]:
    """把历史版本快照重发为新版本（客户端视角即回滚；不反解回结构化状态，由后台重新编辑覆盖）。"""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT content FROM config_versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise NotFoundError(f"version not found: {version}")
        cur = conn.execute(
            "INSERT INTO config_versions (content, published_at) VALUES (?, ?)", (row[0], _now())
        )
        new_version = int(cur.lastrowid)
        conn.commit()
    return new_version, json.loads(row[0])


# ---------- 用量 ----------

def record_usage(model: str, upstream_id: str | None, prompt_tokens: int, completion_tokens: int,
                 total_tokens: int, duration_ms: int, status: str) -> None:
    """追加一条调用流水；失败静默——代理主链路不因用量记录中断。"""
    try:
        with _lock:
            conn = _connect()
            conn.execute(
                "INSERT INTO usage_log (ts, model, upstream_id, prompt_tokens, completion_tokens,"
                " total_tokens, duration_ms, status) VALUES (?,?,?,?,?,?,?,?)",
                (_now(), model, upstream_id, int(prompt_tokens), int(completion_tokens),
                 int(total_tokens), int(duration_ms), status),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def usage_summary(days: int) -> dict:
    """近 N 天（按 UTC 日界）调用与 token 汇总：总量、按模型、按日。"""
    with _lock:
        rows = _connect().execute("SELECT ts, model, total_tokens FROM usage_log").fetchall()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    by_model: dict[str, list[int]] = {}
    by_day: dict[str, list[int]] = {}
    total_calls = total_tokens = 0
    for ts, model, tokens in rows:
        day = str(ts)[:10]
        if day < cutoff:
            continue
        tokens = int(tokens or 0)
        total_calls += 1
        total_tokens += tokens
        for bucket, key in ((by_model, model), (by_day, day)):
            cell = bucket.setdefault(key, [0, 0])
            cell[0] += 1
            cell[1] += tokens
    return {
        "days": days,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "by_model": [{"model": k, "calls": v[0], "tokens": v[1]}
                     for k, v in sorted(by_model.items(), key=lambda kv: (-kv[1][0], kv[0]))],
        "by_day": [{"day": k, "calls": v[0], "tokens": v[1]} for k, v in sorted(by_day.items())],
    }


# ---------- 鉴权与客户端下发 ----------

def _token_matches(supplied: str) -> bool:
    """timing-safe 比较；与 auth.py 一致：token 未配置时不启用鉴权。"""
    if not settings.gateway_token:
        return True
    return secrets.compare_digest(supplied.encode(), settings.gateway_token.encode())


def _require_client_token(x_client_token: str = Header(default="")) -> None:
    if not _token_matches(x_client_token):
        raise HTTPException(status_code=401, detail="unauthorized")


def _require_admin_token(x_admin_token: str = Header(default="")) -> None:
    # 管理端可向全员下发配置，token 未配置时直接拒绝，不随 auth.py 的空 token 放行
    if not settings.gateway_token or not secrets.compare_digest(
        x_admin_token.encode(), settings.gateway_token.encode()
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


@contextmanager
def _store_errors():
    """存储层错误统一转译：ValueError→400、NotFoundError→404、ConflictError→409。"""
    try:
        yield
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/api/client/config")
def client_config(
    if_none_match: str = Header(default=""),
    _token: None = Depends(_require_client_token),
) -> Response:
    """下发最新配置：body 追加 config_version，ETag 为带引号的版本号。"""
    row = _latest_row()
    if row is None:
        raise HTTPException(status_code=404, detail="no config published")
    etag = f'"{row[0]}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    # RFC 7232：If-None-Match 可为列表、* 或弱比较 W/"N"，命中即 304
    candidates = [t.strip().lstrip("W/").strip() for t in if_none_match.split(",")]
    if "*" in candidates or etag in candidates:
        return Response(status_code=304, headers=headers)
    return JSONResponse({**json.loads(row[1]), "config_version": row[0]}, headers=headers)


# ---------- 结构校验（API 层，400 直接带明细） ----------

def _http_url(value) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _require_name(data: dict, key: str = "name") -> str:
    name = data.get(key)
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail=f"{key} required")
    return name.strip()


def _validate_upstream(data: dict) -> dict:
    """校验并归一 upstream：base_url 去尾斜杠；models 补 name/visible 缺省。

    weknora 协议的模型可带 agent_id（WeKnora 智能体 UUID，会话创建时绑定）；
    省略时以模型 id 作为 agent_id。
    """
    name = _require_name(data)
    if data.get("protocol") not in ("openai", "ollama", "weknora"):
        raise HTTPException(status_code=400, detail="protocol must be 'openai', 'ollama' or 'weknora'")
    if not _http_url(data.get("base_url")):
        raise HTTPException(status_code=400, detail="base_url must be an http(s) URL")
    api_key = data.get("api_key") or ""
    if not isinstance(api_key, str):
        raise HTTPException(status_code=400, detail="api_key must be a string")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    models = data.get("models", [])
    if not isinstance(models, list):
        raise HTTPException(status_code=400, detail="models must be a list")
    norm: list[dict] = []
    for m in models:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
            raise HTTPException(status_code=400, detail="each model must have a non-empty string id")
        visible = m.get("visible", True)
        if not isinstance(visible, bool):
            raise HTTPException(status_code=400, detail="model.visible must be a boolean")
        agent_id = m.get("agent_id") or ""
        if not isinstance(agent_id, str):
            raise HTTPException(status_code=400, detail="model.agent_id must be a string")
        entry = {"id": m["id"], "name": m.get("name") or m["id"], "visible": visible}
        if agent_id:
            entry["agent_id"] = agent_id
        norm.append(entry)
    return {"name": name, "protocol": data["protocol"], "base_url": data["base_url"].rstrip("/"),
            "api_key": api_key, "enabled": enabled, "models": norm}


def _validate_assistant(data: dict) -> dict:
    name = _require_name(data)
    model_ref = data.get("model_ref") or ""
    if not isinstance(model_ref, str):
        raise HTTPException(status_code=400, detail="model_ref must be a string")
    if model_ref and "/" not in model_ref:
        raise HTTPException(status_code=400, detail='model_ref must look like "{upstream_id}/{model_id}"')
    for key in ("prompt", "emoji", "description"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise HTTPException(status_code=400, detail=f"{key} must be a string")
    assistant_settings = data.get("settings") or {}
    if not isinstance(assistant_settings, dict):
        raise HTTPException(status_code=400, detail="settings must be an object")
    mcp_names = data.get("mcp_names") or []
    if not isinstance(mcp_names, list) or not all(isinstance(n, str) and n for n in mcp_names):
        raise HTTPException(status_code=400, detail="mcp_names must be a list of non-empty strings")
    return {"name": name, "prompt": data.get("prompt") or "", "emoji": data.get("emoji") or "✨",
            "description": data.get("description") or "", "model_ref": model_ref,
            "settings": assistant_settings, "mcp_names": mcp_names,
            "pinned_topic": _validate_pinned_topic(data.get("pinned_topic"))}


def _validate_pinned_topic(value) -> dict | None:
    """置顶对话下发配置：{name, welcome?}；name 必填非空，welcome 可省。None/空 清除。"""
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="pinned_topic must be an object {name, welcome}")
    topic_name = value.get("name")
    if not isinstance(topic_name, str) or not topic_name.strip():
        raise HTTPException(status_code=400, detail="pinned_topic.name required")
    welcome = value.get("welcome") or ""
    if not isinstance(welcome, str):
        raise HTTPException(status_code=400, detail="pinned_topic.welcome must be a string")
    return {"name": topic_name.strip(), "welcome": welcome}


def _validate_mcp_server(data: dict) -> dict:
    mtype = data.get("type") or "sse"
    if mtype not in _MCP_TYPES:
        raise HTTPException(status_code=400, detail="type must be 'sse' or 'streamableHttp'")
    base_url = data.get("base_url") or ""
    if not isinstance(base_url, str):
        raise HTTPException(status_code=400, detail="base_url must be a string")
    headers = data.get("headers") or {}
    if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise HTTPException(status_code=400, detail="headers must be an object of string values")
    is_active = data.get("is_active", True)
    if not isinstance(is_active, bool):
        raise HTTPException(status_code=400, detail="is_active must be a boolean")
    return {"type": mtype, "base_url": base_url, "headers": headers, "is_active": is_active}


def _validate_minapp(data: dict) -> dict:
    if not _http_url(data.get("url")):
        raise HTTPException(status_code=400, detail="url must be an http(s) URL")
    return {"name": _require_name(data), "url": data["url"]}


def _validate_settings(data: dict) -> dict:
    """只接受已登记键（整体读写，按提供的键合并）；返回待存子集。"""
    unknown = set(data) - set(_DEFAULT_SETTINGS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown setting keys: {', '.join(sorted(unknown))}")
    if "public_base_url" in data and not _http_url(data["public_base_url"]):
        raise HTTPException(status_code=400, detail="public_base_url must be an http(s) URL")
    if "base_system_prompt" in data:
        # 服务端强制注入的基座系统提示词；可空。仅存服务端，不进客户端发布组合。
        if data["base_system_prompt"] is not None and not isinstance(data["base_system_prompt"], str):
            raise HTTPException(status_code=400, detail="base_system_prompt must be a string")
        data["base_system_prompt"] = data["base_system_prompt"] or ""
    if "world_url" in data:
        # 可空：空串/None 表示未部署；否则必须是 http(s) URL
        world_url = data["world_url"]
        if world_url is not None and world_url != "" and not _http_url(world_url):
            raise HTTPException(status_code=400, detail="world_url must be an http(s) URL or empty")
        data["world_url"] = (world_url or "").strip()
    if "default_models" in data:
        dm = data["default_models"]
        if not isinstance(dm, dict) or not all(
                role in _DEFAULT_MODEL_ROLES and isinstance(ref, str) for role, ref in dm.items()):
            raise HTTPException(
                status_code=400,
                detail=f"default_models must map {_DEFAULT_MODEL_ROLES} to model ref strings")
    if "kb_entries" in data:
        kb = data["kb_entries"]
        if (not isinstance(kb, dict) or not isinstance(kb.get("recent_enabled", False), bool)
                or not isinstance(kb.get("kb_ids", []), list)
                or not all(isinstance(i, str) for i in kb.get("kb_ids", []))):
            raise HTTPException(
                status_code=400, detail="kb_entries must be {recent_enabled: bool, kb_ids: [str, ...]}")
    return data


# ---------- 管理端路由 ----------

@router.get("/admin/api/upstreams", dependencies=[Depends(_require_admin_token)])
def admin_list_upstreams() -> dict:
    return {"upstreams": list_upstreams()}


@router.get("/admin/api/upstreams/{uid}", dependencies=[Depends(_require_admin_token)])
def admin_get_upstream(uid: str) -> dict:
    up = get_upstream(uid)
    if up is None:
        raise HTTPException(status_code=404, detail=f"upstream not found: {uid}")
    return up


@router.put("/admin/api/upstreams/{uid}", dependencies=[Depends(_require_admin_token)])
def admin_put_upstream(uid: str, body: dict) -> dict:
    with _store_errors():
        upsert_upstream(uid, _validate_upstream(body))
    return {"saved": True}


@router.delete("/admin/api/upstreams/{uid}", dependencies=[Depends(_require_admin_token)])
def admin_delete_upstream(uid: str) -> dict:
    with _store_errors():
        delete_upstream(uid)
    return {"deleted": True}


@router.post("/admin/api/upstreams/{uid}/test", dependencies=[Depends(_require_admin_token)])
async def admin_test_upstream(uid: str) -> dict:
    """连通性测试：openai 探 GET {base}/models，ollama 探 GET {base}/api/tags，
    weknora 探 GET {base}/api/v1/agents（X-API-Key）；5s 超时；结果不抛 5xx。"""
    up = get_upstream(uid)
    if up is None:
        raise HTTPException(status_code=404, detail=f"upstream not found: {uid}")
    if up["protocol"] == "openai":
        probe, headers = "/models", _bearer(up)
    elif up["protocol"] == "weknora":
        probe = "/api/v1/agents"
        headers = {"X-API-Key": up["api_key"]} if up["api_key"] else {}
    else:
        probe, headers = "/api/tags", _bearer(up)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{up['base_url']}{probe}", headers=headers)
        detail, ok = f"HTTP {resp.status_code}", resp.status_code < 400
    except httpx.HTTPError as e:
        detail, ok = f"{type(e).__name__}: {e}", False
    return {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": detail}


def _bearer(up: dict) -> dict:
    return {"Authorization": f"Bearer {up['api_key']}"} if up["api_key"] else {}


# ---------- Ollama 模型管理（列表 / 后台下载） ----------

_PULL_JOBS: dict[str, dict] = {}  # "{uid}:{model}" → 进度状态；同键重复拉取覆盖旧任务
_PULL_MODEL_RE = re.compile(r"^[\w][\w./:-]*$")  # ollama 模型名：字母数字开头 + . / : - _


def _pull_job_key(uid: str, model: str) -> str:
    return f"{uid}:{model}"


def _run_pull(job: dict, base_url: str, model: str) -> None:
    """后台线程：流式读 ollama /api/pull 的 NDJSON 进度行，写回 job 字典。"""
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, read=600.0)) as client:
            with client.stream("POST", f"{base_url}/api/pull",
                               json={"name": model, "stream": True}) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    evt = json.loads(line)
                    if evt.get("error"):
                        raise RuntimeError(evt["error"])
                    if evt.get("total"):
                        job["completed"] = int(evt.get("completed") or 0)
                        job["total"] = int(evt["total"])
                    if evt.get("status"):
                        job["status"] = str(evt["status"])
        job["state"], job["status"] = "done", "success"
    except Exception as e:  # 线程内失败落状态，不冒泡
        job["state"], job["error"] = "error", f"{type(e).__name__}: {e}"


@router.get("/admin/api/upstreams/{uid}/ollama/models", dependencies=[Depends(_require_admin_token)])
async def admin_ollama_models(uid: str) -> dict:
    """列出 ollama 上游已安装模型（GET {base}/api/tags），供前端勾选导入。"""
    up = get_upstream(uid)
    if up is None:
        raise HTTPException(status_code=404, detail=f"upstream not found: {uid}")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{up['base_url']}/api/tags")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ollama unreachable: {e}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ollama HTTP {resp.status_code}")
    models = resp.json().get("models", [])
    return {"models": [{"name": m.get("name", ""), "size": int(m.get("size") or 0)}
                       for m in models if m.get("name")]}


@router.post("/admin/api/upstreams/{uid}/ollama/pull", dependencies=[Depends(_require_admin_token)])
def admin_ollama_pull(uid: str, body: dict) -> dict:
    """启动后台下载；同模型进行中返回 409，完成/失败后可重发覆盖。"""
    up = get_upstream(uid)
    if up is None:
        raise HTTPException(status_code=404, detail=f"upstream not found: {uid}")
    model = str(body.get("model") or "").strip()
    if not _PULL_MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail="invalid model name")
    key = _pull_job_key(uid, model)
    if _PULL_JOBS.get(key, {}).get("state") == "running":
        raise HTTPException(status_code=409, detail=f"pull already running: {model}")
    job = {"state": "running", "status": "starting", "completed": 0, "total": 0,
           "error": "", "started_at": _now()}
    _PULL_JOBS[key] = job
    threading.Thread(target=_run_pull, args=(job, up["base_url"], model), daemon=True).start()
    return {"started": True, "model": model}


@router.get("/admin/api/upstreams/{uid}/ollama/pull", dependencies=[Depends(_require_admin_token)])
def admin_ollama_pull_status(uid: str, model: str = Query(...)) -> dict:
    job = _PULL_JOBS.get(_pull_job_key(uid, model))
    if job is None:
        raise HTTPException(status_code=404, detail="no pull job for this model")
    return {"model": model, **job}


# ---------- Ollama 模型库搜索（ollama.com，无公开 JSON API，解析搜索页/tags 页） ----------

_OLLAMA_WEB = "https://ollama.com"
_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _ollama_web_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=8.0, follow_redirects=True,
                             proxy=os.environ.get("OLLAMA_WEB_PROXY") or None)


@router.get("/admin/api/ollama/search", dependencies=[Depends(_require_admin_token)])
async def admin_ollama_search(q: str = Query(..., min_length=1, max_length=80)) -> dict:
    """按关键词搜索 ollama.com 模型库，返回仓库名列表（如 qwen3.5）。"""
    try:
        async with _ollama_web_client() as client:
            resp = await client.get(f"{_OLLAMA_WEB}/search", params={"q": q})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ollama.com unreachable: {e}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ollama.com HTTP {resp.status_code}")
    seen: set[str] = set()
    models: list[str] = []
    for name in re.findall(r'href="/library/([a-zA-Z0-9._-]+)"', resp.text):
        if name not in seen:
            seen.add(name)
            models.append(name)
    return {"models": models[:30]}


@router.get("/admin/api/ollama/search/tags", dependencies=[Depends(_require_admin_token)])
async def admin_ollama_search_tags(repo: str = Query(..., min_length=1, max_length=80)) -> dict:
    """列出模型库的版本标签（如 qwen3.5 的 4b/9b/latest…），供下载下拉选择。"""
    if not _REPO_NAME_RE.match(repo):
        raise HTTPException(status_code=400, detail="invalid repo name")
    try:
        async with _ollama_web_client() as client:
            resp = await client.get(f"{_OLLAMA_WEB}/library/{repo}/tags")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ollama.com unreachable: {e}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ollama.com HTTP {resp.status_code}")
    seen: set[str] = set()
    tags: list[str] = []
    for tag in re.findall(rf'href="/library/{repo}:([^"?]+)"', resp.text):
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return {"repo": repo, "tags": tags[:80]}


@router.get("/admin/api/settings", dependencies=[Depends(_require_admin_token)])
def admin_get_settings() -> dict:
    return get_settings()


@router.put("/admin/api/settings", dependencies=[Depends(_require_admin_token)])
def admin_put_settings(body: dict, request: Request) -> dict:
    # 审计：user_departments 曾被整包保存覆盖清空（2026-10-20），记录每次写入的
    # 键与来源以便追溯；uvicorn access log 不含 body。
    logger.warning("settings PUT keys=%s client=%s", sorted(body.keys()),
                   request.client.host if request.client else "?")
    with _store_errors():
        save_settings(_validate_settings(body))
    return {"saved": True}


@router.get("/admin/api/assistants", dependencies=[Depends(_require_admin_token)])
def admin_list_assistants() -> dict:
    return {"assistants": list_assistants()}


@router.get("/admin/api/assistants/{aid}", dependencies=[Depends(_require_admin_token)])
def admin_get_assistant(aid: str) -> dict:
    a = get_assistant(aid)
    if a is None:
        raise HTTPException(status_code=404, detail=f"assistant not found: {aid}")
    return a


@router.put("/admin/api/assistants/{aid}", dependencies=[Depends(_require_admin_token)])
def admin_put_assistant(aid: str, body: dict) -> dict:
    with _store_errors():
        upsert_assistant(aid, _validate_assistant(body))
    return {"saved": True}


@router.delete("/admin/api/assistants/{aid}", dependencies=[Depends(_require_admin_token)])
def admin_delete_assistant(aid: str) -> dict:
    with _store_errors():
        delete_assistant(aid)
    return {"deleted": True}


@router.get("/admin/api/mcp-servers", dependencies=[Depends(_require_admin_token)])
def admin_list_mcp_servers() -> dict:
    """管理行 + 内置 WeKnora MCP（builtin: true，只读展示；is_active 取管理行缺省 True）。"""
    builtin = _builtin_weknora_mcp_entry(get_settings()["public_base_url"])
    return {"mcp_servers": [{**builtin, "builtin": True}, *list_mcp_servers()]}


@router.get("/admin/api/mcp-servers/{name}", dependencies=[Depends(_require_admin_token)])
def admin_get_mcp_server(name: str) -> dict:
    s = get_mcp_server(name)
    if s is None:
        raise HTTPException(status_code=404, detail=f"mcp server not found: {name}")
    return s


@router.put("/admin/api/mcp-servers/{name}", dependencies=[Depends(_require_admin_token)])
def admin_put_mcp_server(name: str, body: dict) -> dict:
    with _store_errors():
        upsert_mcp_server(name, _validate_mcp_server(body))
    return {"saved": True}


@router.delete("/admin/api/mcp-servers/{name}", dependencies=[Depends(_require_admin_token)])
def admin_delete_mcp_server(name: str) -> dict:
    with _store_errors():
        delete_mcp_server(name)
    return {"deleted": True}


@router.get("/admin/api/minapps", dependencies=[Depends(_require_admin_token)])
def admin_list_minapps() -> dict:
    return {"minapps": list_minapps()}


@router.get("/admin/api/minapps/{app_id}", dependencies=[Depends(_require_admin_token)])
def admin_get_minapp(app_id: str) -> dict:
    a = get_minapp(app_id)
    if a is None:
        raise HTTPException(status_code=404, detail=f"minapp not found: {app_id}")
    return a


@router.put("/admin/api/minapps/{app_id}", dependencies=[Depends(_require_admin_token)])
def admin_put_minapp(app_id: str, body: dict) -> dict:
    with _store_errors():
        upsert_minapp(app_id, _validate_minapp(body))
    return {"saved": True}


@router.delete("/admin/api/minapps/{app_id}", dependencies=[Depends(_require_admin_token)])
def admin_delete_minapp(app_id: str) -> dict:
    with _store_errors():
        delete_minapp(app_id)
    return {"deleted": True}


@router.post("/admin/api/publish", dependencies=[Depends(_require_admin_token)])
def admin_publish() -> dict:
    """组合当前结构化配置落新版本；引用校验失败 400 带明细，不落版本。"""
    with _store_errors():
        version, _content = publish_composed()
    return {"version": version}


@router.post("/admin/api/publish/rollback", dependencies=[Depends(_require_admin_token)])
def admin_rollback(body: dict) -> dict:
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise HTTPException(status_code=400, detail="version (int) required")
    with _store_errors():
        new_version, _content = rollback_to(version)
    return {"version": new_version}


@router.get("/admin/api/published", dependencies=[Depends(_require_admin_token)])
def admin_published() -> dict:
    return get_published()


@router.get("/admin/api/usage/summary", dependencies=[Depends(_require_admin_token)])
def admin_usage_summary(days: int = Query(default=30, ge=1, le=365)) -> dict:
    return usage_summary(days)


_ADMIN_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>企业配置管理</title>
<style>
  :root { --line: #d0d7de; --muted: #57606a; --accent: #0b62d6; --danger: #c62828; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; color: #1f2328; background: #fff; }
  header { border-bottom: 1px solid var(--line); background: #f6f8fa; padding: .8rem 1.2rem; }
  .head-row { display: flex; align-items: center; gap: .8rem; flex-wrap: wrap; }
  h1 { font-size: 1.15rem; margin: 0; }
  h2 { font-size: 1.02rem; margin: 0; }
  h3, h4 { margin: .6rem 0 .4rem; font-size: .95rem; }
  .badge { background: #ddf4ff; color: var(--accent); border: 1px solid #b6e3ff;
           border-radius: 999px; padding: .12rem .7rem; font-size: .85rem; white-space: nowrap; }
  .grow { flex: 1; }
  .muted { color: var(--muted); font-size: .88rem; }
  .mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .85rem; }
  button { font: inherit; padding: .28rem .8rem; border: 1px solid var(--line); border-radius: 6px;
           background: #fff; cursor: pointer; }
  button:hover { background: #f3f4f6; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { background: #0a56bd; }
  button.danger { color: var(--danger); border-color: #efb8b8; }
  button.armed { background: var(--danger); border-color: var(--danger); color: #fff; }
  button:disabled { opacity: .55; cursor: default; }
  input, select, textarea { font: inherit; padding: .28rem .5rem; border: 1px solid var(--line);
                            border-radius: 6px; background: #fff; }
  input:focus, select:focus, textarea:focus { outline: 2px solid #b6e3ff; border-color: var(--accent); }
  label { display: inline-flex; align-items: center; gap: .35rem; }
  #tabs { display: flex; gap: .2rem; flex-wrap: wrap; padding: .45rem 1.2rem 0;
          border-bottom: 1px solid var(--line); }
  #tabs button { border: none; background: none; padding: .5rem .95rem; border-bottom: 2px solid transparent;
                 color: var(--muted); }
  #tabs button:hover { background: none; color: var(--accent); }
  #tabs button.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  main { padding: 1rem 1.2rem 3rem; max-width: 1080px; margin: 0 auto; }
  main > section { display: none; }
  main > section.active { display: block; }
  .card { border: 1px solid var(--line); border-radius: 8px; padding: .9rem 1rem; margin-bottom: 1rem; }
  .bar { display: flex; gap: .6rem; align-items: center; margin: .5rem 0; flex-wrap: wrap; }
  .bar input { flex: 1; min-width: 220px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: .6rem .9rem; }
  .grid label, .block { display: flex; flex-direction: column; gap: .25rem; font-size: .88rem; }
  .grid label.check { flex-direction: row; align-items: center; gap: .4rem; margin-top: 1.1rem; }
  .block { margin: .5rem 0; }
  textarea { resize: vertical; }
  table { border-collapse: collapse; width: 100%; margin: .4rem 0; }
  th, td { border: 1px solid var(--line); padding: .38rem .55rem; text-align: left;
           font-size: .88rem; vertical-align: middle; }
  th { background: #f6f8fa; font-weight: 600; white-space: nowrap; }
  td.ops { white-space: nowrap; }
  td.ops button { margin-right: .3rem; }
  .test-cell { max-width: 240px; word-break: break-all; }
  .test-cell.err { color: var(--danger); }
  .model-row { display: flex; gap: .5rem; align-items: center; margin: .3rem 0; flex-wrap: wrap; }
  .model-row input { flex: 1; min-width: 140px; }
  .checks { display: flex; flex-wrap: wrap; gap: .35rem 1.1rem; margin: .4rem 0; }
  .row { display: flex; gap: .6rem; align-items: center; margin-top: .7rem; flex-wrap: wrap; }
  .stats { display: flex; gap: 2.5rem; margin: .8rem 0; }
  .stat .num { font-size: 1.7rem; font-weight: 700; color: var(--accent); }
  .chart-row { display: flex; align-items: center; gap: .6rem; margin: .22rem 0; }
  .chart-label { width: 84px; font-size: .8rem; color: var(--muted);
                 font-family: ui-monospace, Menlo, monospace; }
  .chart-track { flex: 1; background: #eef1f4; border-radius: 4px; height: 14px; overflow: hidden; }
  .chart-bar { display: block; height: 100%; background: var(--accent); min-width: 2px; }
  .chart-num { width: 200px; font-size: .8rem; color: var(--muted); white-space: nowrap; }
  .json { background: #f6f8fa; border: 1px solid var(--line); border-radius: 6px;
          padding: .6rem; max-height: 320px; overflow: auto; font-size: .8rem; }
  #status { margin-top: .5rem; min-height: 1.25em; font-size: .9rem; }
  #status.ok { color: var(--accent); }
  #status.err { color: var(--danger); }
</style>
</head>
<body>
<header>
  <div class="head-row">
    <h1>企业配置管理</h1>
    <span id="version" class="badge">未加载</span>
    <span class="grow"></span>
    <button id="btn-publish" class="primary" data-action="publish">发布</button>
  </div>
  <div class="bar">
    <label for="token">管理 token</label>
    <input id="token" type="password" autocomplete="off" placeholder="GATEWAY_TOKEN">
    <button data-action="save-token">保存 token</button>
  </div>
  <div id="status"></div>
</header>
<nav id="tabs">
  <button data-tab="upstreams" class="active">上游与模型</button>
  <button data-tab="defaults">默认模型</button>
  <button data-tab="assistants">助手</button>
  <button data-tab="mcp">MCP</button>
  <button data-tab="minapps">小程序</button>
  <button data-tab="kbs">知识库</button>
  <button data-tab="market">插件市场</button>
  <button data-tab="usage">用量</button>
  <button data-tab="versions">版本</button>
</nav>
<main>
<section id="tab-upstreams" class="active">
  <div class="card">
    <div class="bar"><h2>上游服务</h2><span class="grow"></span>
      <button data-action="new-upstream">新建上游</button></div>
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>协议</th><th>base_url</th><th>启用</th><th>模型数</th><th>连通</th><th>操作</th></tr></thead>
      <tbody id="up-tbody"></tbody>
    </table>
  </div>
  <div class="card" id="upstream-form">
    <h3 id="up-form-title">新建上游</h3>
    <div class="grid">
      <label>ID<input id="up-id" placeholder="如 gw，创建后不可改"></label>
      <label>名称<input id="up-name" placeholder="显示名"></label>
      <label>协议<select id="up-protocol"><option value="openai">openai</option><option value="ollama">ollama</option><option value="weknora">weknora</option></select></label>
      <label>base_url<input id="up-base-url" placeholder="http://host:port"></label>
      <label>api_key<input id="up-api-key" type="password" autocomplete="off" placeholder="上游密钥，仅存服务端"></label>
      <label class="check"><input id="up-enabled" type="checkbox" checked>启用</label>
    </div>
    <div class="bar"><h4>模型</h4><span class="grow"></span>
      <button data-action="add-model-row">添加模型</button></div>
    <div id="up-models"></div>
    <div class="bar"><h4>Ollama 管理（需先保存上游）</h4><span class="grow"></span>
      <button data-action="fetch-remote-models">拉取服务器模型列表</button>
      <button data-action="import-remote-models">导入选中</button></div>
    <div id="up-remote-models" class="checks"><span class="muted">（ollama 上游可从服务器拉取已装模型，勾选后导入下方模型表）</span></div>
    <div class="row">
      <label>搜索模型库 <input id="pull-model-q" placeholder="输入关键词，如 qwen" style="min-width:150px"></label>
      <label>模型 <select id="pull-model-repo"><option value="">（先搜索）</option></select></label>
      <label>版本 <select id="pull-tag-select" disabled><option value="">—</option></select></label>
      <button data-action="pull-model">开始下载</button>
      <span class="muted mono" id="pull-status"></span>
    </div>
    <div class="row">
      <button class="primary" data-action="save-upstream">保存上游</button>
      <span class="muted" id="up-hint"></span>
    </div>
  </div>
</section>
<section id="tab-defaults">
  <div class="card">
    <h2>服务设置</h2>
    <div class="bar">
      <label for="set-base-url">public_base_url</label>
      <input id="set-base-url" placeholder="客户端访问网关的基础地址，如 http://10.1.2.3:8787">
      <button data-action="save-base-url">保存地址</button>
    </div>
    <p class="muted">发布组合里 provider base_url 以它为前缀。</p>
    <div class="bar">
      <label for="set-world-url">world_url</label>
      <input id="set-world-url" placeholder="小世界服务地址（可空），如 http://66.12:8788">
      <button data-action="save-world-url">保存小世界</button>
    </div>
    <p class="muted">非空时随发布透出，客户端侧栏「小世界」开放；留空即下线该入口。</p>
    <div class="bar"><h4>基座系统提示词（服务端强制注入，客户端不可查看与修改）</h4></div>
    <label class="block"><textarea id="set-base-prompt" rows="7"
      placeholder="留空 = 不注入。对所有经网关 /v1 的对话（openai/ollama 上游）生效，注入到 messages 首位；用户/助手提示词排在其后。weknora 智能体上游不受影响（基座在 WeKnora 侧）。建议写明公共知识库检索工具的调用规范。"></textarea></label>
    <div class="row"><button data-action="save-base-prompt">保存基座提示词</button></div>
  </div>
  <div class="card">
    <h2>默认模型</h2>
    <p class="muted">选项来自全部上游的可见模型（上游ID/模型ID）；选「不设置」即清除。发布时校验必须存在且可见。</p>
    <div class="grid">
      <label>助手模型<select id="dm-assistant"></select></label>
      <label>翻译模型<select id="dm-translate"></select></label>
      <label>快速模型<select id="dm-quick"></select></label>
    </div>
    <div class="row"><button class="primary" data-action="save-defaults">保存默认模型</button></div>
  </div>
</section>
<section id="tab-assistants">
  <div class="card">
    <div class="bar"><h2>助手</h2></div>
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>模型</th><th>MCP</th><th>操作</th></tr></thead>
      <tbody id="a-tbody"></tbody>
    </table>
  </div>
  <div class="card" id="assistant-form">
    <h3 id="a-form-title">新建助手</h3>
    <div class="grid">
      <label>ID<input id="a-id" placeholder="留空自动生成"></label>
      <label>名称<input id="a-name"></label>
      <label>Emoji<input id="a-emoji"></label>
      <label>模型<select id="a-model"></select></label>
    </div>
    <label class="block">描述<input id="a-desc"></label>
    <label class="block">提示词 prompt<textarea id="a-prompt" rows="5"></textarea></label>
    <h4>置顶对话下发（客户端聊天界面置顶，留空即不下发）</h4>
    <div class="grid">
      <label>置顶对话名称<input id="a-pt-name" placeholder="如 问问百事通"></label>
      <label>欢迎语（首条消息）<input id="a-pt-welcome" placeholder="进入对话时展示的说明文字，可空"></label>
    </div>
    <h4>MCP 服务（可多选）</h4>
    <div id="a-mcps" class="checks"></div>
    <div class="row">
      <button class="primary" data-action="save-assistant">保存助手</button>
      <span class="muted" id="a-hint"></span>
    </div>
  </div>
</section>
<section id="tab-mcp">
  <div class="card">
    <div class="bar"><h2>MCP 服务</h2></div>
    <table>
      <thead><tr><th>名称</th><th>类型</th><th>base_url</th><th>headers</th><th>启用</th><th>操作</th></tr></thead>
      <tbody id="m-tbody"></tbody>
    </table>
  </div>
  <div class="card" id="mcp-form">
    <h3 id="m-form-title">新建 MCP 服务</h3>
    <div class="grid">
      <label>名称<input id="m-name" placeholder="创建后不可改"></label>
      <label>类型<select id="m-type"><option value="sse">sse</option><option value="streamableHttp">streamableHttp</option></select></label>
      <label class="check"><input id="m-active" type="checkbox" checked>启用</label>
    </div>
    <label class="block">base_url<input id="m-base-url" placeholder="如 http://host:port/sse"></label>
    <div class="bar"><h4>Headers</h4><span class="grow"></span>
      <button data-action="add-header-row">添加 Header</button></div>
    <div id="m-headers"></div>
    <div class="row">
      <button class="primary" data-action="save-mcp">保存 MCP</button>
      <span class="muted" id="m-hint"></span>
    </div>
  </div>
</section>
<section id="tab-minapps">
  <div class="card">
    <div class="bar"><h2>小程序</h2></div>
    <table>
      <thead><tr><th>ID</th><th>名称</th><th>URL</th><th>操作</th></tr></thead>
      <tbody id="mi-tbody"></tbody>
    </table>
  </div>
  <div class="card" id="minapp-form">
    <h3 id="mi-form-title">新建小程序</h3>
    <div class="grid">
      <label>ID<input id="mi-id" placeholder="留空自动生成"></label>
      <label>名称<input id="mi-name"></label>
    </div>
    <label class="block">URL<input id="mi-url"
      placeholder="嵌入页建议填网关反代地址（如 http://host:8787/proxy/weknora/），直连会被 X-Frame-Options 拦截"></label>
    <div class="row">
      <button class="primary" data-action="save-minapp">保存小程序</button>
      <span class="muted" id="mi-hint"></span>
    </div>
  </div>
</section>
<section id="tab-kbs">
  <div class="card">
    <h2>知识库下发</h2>
    <p class="muted">库列表来自 WeKnora（GET /api/kbs，与管理 token 同值鉴权）；勾选结果写入 kb_entries.kb_ids，随发布原样下发客户端。</p>
    <label class="check"><input id="kb-recent" type="checkbox">启用「最近知识」入口（recent_enabled）</label>
    <div id="kb-list" class="checks"><span class="muted">尚未加载。</span></div>
    <div class="row"><button class="primary" data-action="save-kbs">保存知识库设置</button></div>
  </div>
</section>
<section id="tab-market">
  <div class="card">
    <div class="bar"><h2>插件市场目录</h2><span class="grow"></span>
      <button data-action="load-market">刷新</button></div>
    <p class="muted">目录每次请求现扫；上架/更新走 scp 放文件到下方目录。下载热度（plugin.json 的 downloads）仅此处可见，不下发客户端。</p>
    <p class="mono" id="market-dir">尚未加载。</p>
    <table><thead><tr>
      <th>插件</th><th>版本</th><th>部门</th><th>作者</th><th>下载次数</th><th>状态</th>
    </tr></thead><tbody id="market-tbody"></tbody></table>
  </div>
</section>
<section id="tab-usage">
  <div class="card">
    <div class="bar">
      <h2>用量统计</h2>
      <label>时间范围<select id="usage-days"><option value="7">近 7 天</option><option value="30" selected>近 30 天</option><option value="90">近 90 天</option></select></label>
      <button data-action="load-usage">刷新</button>
    </div>
    <div class="stats">
      <div class="stat"><div class="num" id="usage-calls">-</div><div class="muted">总调用</div></div>
      <div class="stat"><div class="num" id="usage-tokens">-</div><div class="muted">总 tokens</div></div>
    </div>
    <h4>按模型</h4>
    <table><thead><tr><th>模型</th><th>调用</th><th>tokens</th></tr></thead><tbody id="um-tbody"></tbody></table>
    <h4>按日（条长 = 调用数）</h4>
    <div id="usage-days-chart"></div>
  </div>
</section>
<section id="tab-versions">
  <div class="card">
    <div class="bar"><h2>发布版本</h2><span class="grow"></span>
      <button data-action="load-published">加载已发布</button></div>
    <div id="pub-latest" class="muted">尚未加载。</div>
    <details><summary>最新发布内容（JSON）</summary><pre id="pub-content" class="json"></pre></details>
    <table>
      <thead><tr><th>版本</th><th>发布时间</th><th>操作</th></tr></thead>
      <tbody id="v-tbody"></tbody>
    </table>
    <p class="muted">回滚 = 把历史版本快照重发为新版本（客户端视角即回滚；不反解回结构化状态）。</p>
  </div>
</section>
</main>
<script>
// 嵌入式 webview（企业内网窗口/内嵌浏览器）普遍不支持 prompt/alert/confirm：
// 全部反馈走状态行，删除与回滚用两段式确认按钮；token 沿用 localStorage。
const $ = (id) => document.getElementById(id);
let currentTab = "upstreams";
let UPSTREAMS = [];   // 上游缓存（默认模型/助手表单下拉复用）
let MCPS = [];        // MCP 缓存（助手表单多选复用）
let ASSISTANTS = [];
let MINAPPS = [];

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function show(msg, cls) {
  $("status").textContent = msg;
  $("status").className = cls || "";
}

function token() {
  const t = $("token").value.trim();
  if (!t) throw new Error("请先填写管理 token 并点「保存 token」");
  return t;
}

async function api(method, url, body, clientToken) {
  const t = token();
  const headers = clientToken
    ? { "Authorization": "Bearer " + t, "X-Client-Token": t }  // /api/kbs 走客户端鉴权，双头兼容
    : { "X-Admin-Token": t };
  if (body) headers["Content-Type"] = "application/json";
  const resp = await fetch(url, { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined });
  if (resp.status === 401) {
    localStorage.removeItem("admin_token");  // token 失效清缓存，提示重输
    throw new Error("token 无效（401），请重新填写并保存");
  }
  if (!resp.ok) {
    let msg = "HTTP " + resp.status;
    try { msg = (await resp.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return resp.json();
}

async function run(btn, fn) {
  if (btn) btn.disabled = true;
  try { await fn(); }
  catch (e) { show("请求失败：" + (e && e.message ? e.message : e), "err"); }
  finally { if (btn) btn.disabled = false; }
}

function newId() {
  // http 内网地址属非安全上下文，crypto.randomUUID 可能缺席，留兜底
  return (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : "id-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

function setBadge(latest) {
  $("version").textContent = latest ? "当前版本 v" + latest.version : "未发布";
}

function modelOptions(selected) {
  let html = '<option value="">不设置</option>';
  let found = false;
  for (const up of UPSTREAMS) {
    const tag = up.enabled ? "" : "（上游已停用）";
    for (const m of up.models || []) {
      if (m.visible === false) continue;
      const ref = up.id + "/" + m.id;
      const sel = ref === selected;
      if (sel) found = true;
      const extra = m.name && m.name !== m.id ? " · " + m.name : "";
      html += `<option value="${esc(ref)}"${sel ? " selected" : ""}>${esc(ref + extra + tag)}</option>`;
    }
  }
  if (selected && !found) {
    html += `<option value="${esc(selected)}" selected>${esc(selected)}（当前值，已不可见）</option>`;
  }
  return html;
}

// ---------- 上游与模型 ----------

function modelRow(m, options) {
  const names = options || [];
  const div = document.createElement("div");
  div.className = "model-row";
  const idField = names.length
    ? `<select class="m-id"><option value="">（选择模型）</option>${names.map((n) =>
        `<option value="${esc(n)}"${m && m.id === n ? " selected" : ""}>${esc(n)}</option>`).join("")}</select>`
    : '<input class="m-id" placeholder="模型 id（服务器列表不可用时才手输）">';
  div.innerHTML = idField
    + '<input class="m-name" placeholder="显示名（可空）">'
    + '<input class="m-agent" placeholder="agent_id（仅 weknora，可空）">'
    + '<label class="check"><input type="checkbox" class="m-visible" checked>可见</label>'
    + '<button type="button" data-action="del-row">删除行</button>';
  if (m) {
    if (!names.length) div.querySelector(".m-id").value = m.id || "";
    div.querySelector(".m-name").value = m.name || "";
    div.querySelector(".m-agent").value = m.agent_id || "";
    div.querySelector(".m-visible").checked = m.visible !== false;
  }
  return div;
}

function resetUpstreamForm() {
  $("up-form-title").textContent = "新建上游";
  $("up-id").value = ""; $("up-id").disabled = false;
  $("up-name").value = ""; $("up-base-url").value = ""; $("up-api-key").value = "";
  $("up-protocol").value = "openai"; $("up-enabled").checked = true;
  $("up-models").innerHTML = "";
  $("up-remote-models").innerHTML = '<span class="muted">（ollama 上游可从服务器拉取已装模型，勾选后导入下方模型表）</span>';
  REMOTE_MODELS = []; REMOTE_FOR = "";
  $("up-hint").textContent = "ID 创建后不可改；api_key 仅存服务端不下发；保存为 PUT 全量覆盖。";
}

function renderUpstreams() {
  const rows = UPSTREAMS.map((up) => {
    const id = esc(up.id);
    return `<tr><td class="mono">${id}</td><td>${esc(up.name)}</td><td>${esc(up.protocol)}</td>
      <td class="mono">${esc(up.base_url)}</td><td>${up.enabled ? "是" : "否"}</td>
      <td>${(up.models || []).length}</td><td class="test-cell muted"></td>
      <td class="ops">
        <button data-action="edit-upstream" data-id="${id}">编辑</button>
        <button data-action="test-upstream" data-id="${id}">测试连通</button>
        <button class="danger" data-action="del-upstream" data-id="${id}">删除</button>
      </td></tr>`;
  }).join("");
  $("up-tbody").innerHTML = rows || '<tr><td colspan="8" class="muted">（暂无上游，先在下方新建）</td></tr>';
}

async function loadUpstreams() {
  const data = await api("GET", "/admin/api/upstreams");
  UPSTREAMS = data.upstreams || [];
  renderUpstreams();
  resetUpstreamForm();
}

async function editUpstream(id) {
  const up = UPSTREAMS.find((u) => u.id === id);
  if (!up) return show("上游不存在，请重新加载", "err");
  $("up-form-title").textContent = "编辑上游：" + up.id;
  $("up-id").value = up.id; $("up-id").disabled = true;
  $("up-name").value = up.name || "";
  $("up-protocol").value = up.protocol || "openai";
  $("up-base-url").value = up.base_url || "";
  $("up-api-key").value = up.api_key || "";
  $("up-enabled").checked = !!up.enabled;
  $("up-models").innerHTML = "";
  $("up-hint").textContent = "正在编辑 " + up.id + "（api_key 留空保存即清除）。";
  $("upstream-form").scrollIntoView({ behavior: "smooth", block: "start" });
  // ollama 上游顺带拉已装模型，模型行走下拉选择
  const names = await ensureRemoteModels();
  $("up-models").innerHTML = "";
  (up.models || []).forEach((m) => $("up-models").appendChild(modelRow(m, names)));
}

async function saveUpstream() {
  const uid = $("up-id").value.trim();
  if (!uid) return show("上游 ID 必填", "err");
  const body = {
    name: $("up-name").value.trim(),
    protocol: $("up-protocol").value,
    base_url: $("up-base-url").value.trim(),
    api_key: $("up-api-key").value,
    enabled: $("up-enabled").checked,
    models: Array.from(document.querySelectorAll("#up-models .model-row")).map((r) => ({
      id: r.querySelector(".m-id").value.trim(),
      name: r.querySelector(".m-name").value.trim(),
      agent_id: r.querySelector(".m-agent").value.trim(),
      visible: r.querySelector(".m-visible").checked,
    })),
  };
  if (!body.name) return show("上游名称必填", "err");
  if (body.models.find((m) => !m.id)) return show("存在未填模型 id 的行，请补齐或删除该行", "err");
  await api("PUT", "/admin/api/upstreams/" + encodeURIComponent(uid), body);
  show(`上游 ${uid} 已保存`, "ok");
  await loadUpstreams();
}

async function testUpstream(el) {
  const cell = el.closest("tr").querySelector(".test-cell");
  cell.textContent = "测试中…"; cell.classList.remove("err");
  try {
    const r = await api("POST", "/admin/api/upstreams/" + encodeURIComponent(el.dataset.id) + "/test");
    cell.textContent = (r.ok ? "✓ " : "✗ ") + r.latency_ms + "ms · " + r.detail;
    cell.classList.toggle("err", !r.ok);
  } catch (e) {
    cell.textContent = "✗ " + e.message;
    cell.classList.add("err");
  }
}

async function deleteUpstream(id) {
  await api("DELETE", "/admin/api/upstreams/" + encodeURIComponent(id));
  show(`上游 ${id} 已删除`, "ok");
  await loadUpstreams();
}

// ---------- Ollama 模型管理 ----------

let pullPolling = false;  // 只保留一个轮询循环，多次点击下载复用

let REMOTE_MODELS = [];   // 当前上游从 ollama 拉到的已装模型名（模型行下拉数据源）
let REMOTE_FOR = "";      // 缓存对应的上游 ID

async function ensureRemoteModels() {
  const uid = $("up-id").value.trim();
  if (!uid) return [];
  if (REMOTE_FOR === uid) return REMOTE_MODELS;
  try {
    const r = await api("GET", "/admin/api/upstreams/" + encodeURIComponent(uid) + "/ollama/models");
    REMOTE_MODELS = (r.models || []).map((m) => m.name);
    REMOTE_FOR = uid;
  } catch (e) {
    REMOTE_MODELS = []; REMOTE_FOR = "";
  }
  return REMOTE_MODELS;
}

async function addModelRow() {
  const names = await ensureRemoteModels();
  $("up-models").appendChild(modelRow(null, names));
  if (!names.length) show("未能获取 ollama 模型列表（上游未保存或服务器不可达），本行退化为手动输入", "err");
}

function requireSavedUpstream() {
  const uid = $("up-id").value.trim();
  if (!uid) { show("请先填写上游 ID 并保存上游，再使用 Ollama 管理", "err"); return null; }
  return uid;
}

function formModelIds() {
  return new Set(Array.from(document.querySelectorAll("#up-models .m-id")).map((i) => i.value.trim()));
}

async function fetchRemoteModels() {
  const uid = requireSavedUpstream();
  if (!uid) return;
  const r = await api("GET", "/admin/api/upstreams/" + encodeURIComponent(uid) + "/ollama/models");
  REMOTE_MODELS = (r.models || []).map((m) => m.name);
  REMOTE_FOR = uid;
  const existing = formModelIds();
  $("up-remote-models").innerHTML = (r.models || []).map((m) => {
    const has = existing.has(m.name);
    const gb = m.size ? "（" + (m.size / 1073741824).toFixed(1) + "GB）" : "";
    return `<label class="check"><input type="checkbox" class="rm-item" value="${esc(m.name)}"${has ? " checked disabled" : ""}> ${esc(m.name)}${gb}${has ? " · 已在表" : ""}</label>`;
  }).join("") || '<span class="muted">（服务器上还没有已安装模型）</span>';
  show(`已拉取服务器模型列表（${(r.models || []).length} 个）`, "ok");
}

function importRemoteModels() {
  const existing = formModelIds();
  const picked = Array.from(document.querySelectorAll("#up-remote-models .rm-item:checked"))
    .map((c) => c.value).filter((n) => !existing.has(n));
  if (!picked.length) return show("没有可导入的模型（先拉取列表并勾选）", "err");
  picked.forEach((name) => $("up-models").appendChild(modelRow({ id: name }, REMOTE_MODELS)));
  show(`已导入 ${picked.length} 个模型到模型表，记得点「保存上游」`, "ok");
}

async function pollPull(uid, model) {
  if (pullPolling) return;
  pullPolling = true;
  try {
    for (;;) {
      const st = await api("GET", "/admin/api/upstreams/" + encodeURIComponent(uid)
        + "/ollama/pull?model=" + encodeURIComponent(model));
      const pct = st.total ? Math.round((st.completed / st.total) * 100) : 0;
      $("pull-status").textContent = `${model}: ${st.status || st.state}${pct ? " " + pct + "%" : ""}`;
      if (st.state === "done") { show(`${model} 下载完成，点「拉取服务器模型列表」可导入`, "ok"); return; }
      if (st.state === "error") { show(`${model} 下载失败：${st.error}`, "err"); return; }
      await new Promise((r) => setTimeout(r, 1500));
    }
  } finally { pullPolling = false; }
}

let pullSearchTimer = null;

async function searchRepos() {
  const q = $("pull-model-q").value.trim();
  if (!q) return;
  const r = await api("GET", "/admin/api/ollama/search?q=" + encodeURIComponent(q));
  const models = r.models || [];
  $("pull-model-repo").innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("")
    || '<option value="">（无结果）</option>';
  if (models.length) { $("pull-model-repo").value = models[0]; await loadPullTags(models[0]); }
  else { $("pull-tag-select").disabled = true; $("pull-tag-select").innerHTML = '<option value="">—</option>'; }
}

async function loadPullTags(repo) {
  $("pull-tag-select").disabled = true;
  $("pull-tag-select").innerHTML = '<option value="">加载中…</option>';
  const r = await api("GET", "/admin/api/ollama/search/tags?repo=" + encodeURIComponent(repo));
  const tags = r.tags || [];
  $("pull-tag-select").innerHTML = tags.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("")
    || '<option value="">（无标签）</option>';
  $("pull-tag-select").disabled = !tags.length;
}

async function pullModel() {
  const uid = requireSavedUpstream();
  if (!uid) return;
  const repo = $("pull-model-repo").value;
  if (!repo) return show("请先搜索并从下拉框选择模型", "err");
  const tag = $("pull-tag-select").value || "latest";
  const model = repo + ":" + tag;
  $("pull-status").textContent = model + ": 提交中…";
  await api("POST", "/admin/api/upstreams/" + encodeURIComponent(uid) + "/ollama/pull", { model: model });
  await pollPull(uid, model);
}

// ---------- 默认模型 / 服务设置 ----------

const DM_ROLES = [["dm-assistant", "assistant"], ["dm-translate", "translate"], ["dm-quick", "quick_model"]];

async function loadDefaults() {
  const [upData, st] = await Promise.all([
    api("GET", "/admin/api/upstreams"),
    api("GET", "/admin/api/settings"),
  ]);
  UPSTREAMS = upData.upstreams || [];
  $("set-base-url").value = st.public_base_url || "";
  $("set-base-prompt").value = st.base_system_prompt || "";
  $("set-world-url").value = st.world_url || "";
  const dm = st.default_models || {};
  for (const [sel, role] of DM_ROLES) $(sel).innerHTML = modelOptions(dm[role] || "");
}

async function saveBaseUrl() {
  const v = $("set-base-url").value.trim();
  if (!v.startsWith("http://") && !v.startsWith("https://")) {
    return show("public_base_url 必须是 http(s) 地址", "err");
  }
  await api("PUT", "/admin/api/settings", { public_base_url: v });
  show("public_base_url 已保存", "ok");
}

async function saveBasePrompt() {
  await api("PUT", "/admin/api/settings", { base_system_prompt: $("set-base-prompt").value });
  show("基座提示词已保存（服务端即时生效，无需发布）", "ok");
}

async function saveWorldUrl() {
  const v = $("set-world-url").value.trim();
  if (v && !v.startsWith("http://") && !v.startsWith("https://")) {
    return show("world_url 必须是 http(s) 地址或留空", "err");
  }
  await api("PUT", "/admin/api/settings", { world_url: v });
  show(v ? "world_url 已保存（下次发布透出）" : "world_url 已清空（下次发布后客户端隐藏小世界）", "ok");
}

async function saveDefaults() {
  const dm = {};
  for (const [sel, role] of DM_ROLES) if ($(sel).value) dm[role] = $(sel).value;
  await api("PUT", "/admin/api/settings", { default_models: dm });
  show("默认模型已保存", "ok");
}

// ---------- 助手 ----------

function renderAssistants() {
  const rows = ASSISTANTS.map((a) => {
    const id = esc(a.id);
    const pin = a.pinned_topic ? ` <span title="置顶对话：${esc(a.pinned_topic.name)}">📌</span>` : "";
    return `<tr><td class="mono">${id}</td><td>${esc(a.emoji)} ${esc(a.name)}${pin}</td>
      <td class="mono">${esc(a.model_ref || "—")}</td>
      <td>${esc((a.mcp_names || []).join("、")) || "—"}</td>
      <td class="ops">
        <button data-action="edit-assistant" data-id="${id}">编辑</button>
        <button class="danger" data-action="del-assistant" data-id="${id}">删除</button>
      </td></tr>`;
  }).join("");
  $("a-tbody").innerHTML = rows || '<tr><td colspan="5" class="muted">（暂无助手）</td></tr>';
}

function buildMcpChecks(checked) {
  const set = checked || [];
  $("a-mcps").innerHTML = MCPS.length
    ? MCPS.map((s) => `<label class="check"><input type="checkbox" value="${esc(s.name)}"${set.includes(s.name) ? " checked" : ""}> ${esc(s.name)}${s.is_active ? "" : "（停用）"}</label>`).join("")
    : '<span class="muted">（尚未配置 MCP 服务）</span>';
}

function resetAssistantForm() {
  $("a-form-title").textContent = "新建助手";
  $("a-id").value = ""; $("a-id").disabled = false;
  $("a-name").value = ""; $("a-emoji").value = "✨"; $("a-desc").value = ""; $("a-prompt").value = "";
  $("a-pt-name").value = ""; $("a-pt-welcome").value = "";
  $("a-model").innerHTML = modelOptions("");
  buildMcpChecks([]);
  $("a-hint").textContent = "ID 留空自动生成；模型与 MCP 可留空。";
}

async function loadAssistants() {
  const [aData, upData, mData] = await Promise.all([
    api("GET", "/admin/api/assistants"),
    api("GET", "/admin/api/upstreams"),
    api("GET", "/admin/api/mcp-servers"),
  ]);
  ASSISTANTS = aData.assistants || [];
  UPSTREAMS = upData.upstreams || [];
  MCPS = mData.mcp_servers || [];
  renderAssistants();
  resetAssistantForm();
}

function editAssistant(id) {
  const a = ASSISTANTS.find((x) => x.id === id);
  if (!a) return show("助手不存在，请重新加载", "err");
  $("a-form-title").textContent = "编辑助手：" + a.name;
  $("a-id").value = a.id; $("a-id").disabled = true;
  $("a-name").value = a.name || "";
  $("a-emoji").value = a.emoji || "✨";
  $("a-desc").value = a.description || "";
  $("a-prompt").value = a.prompt || "";
  $("a-model").innerHTML = modelOptions(a.model_ref || "");
  buildMcpChecks(a.mcp_names || []);
  const pt = a.pinned_topic || {};
  $("a-pt-name").value = pt.name || "";
  $("a-pt-welcome").value = pt.welcome || "";
  $("a-hint").textContent = "正在编辑 " + a.id;
  $("assistant-form").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function saveAssistant() {
  let aid = $("a-id").value.trim();
  if (!aid) aid = newId();
  const ptName = $("a-pt-name").value.trim();
  const body = {
    name: $("a-name").value.trim(),
    prompt: $("a-prompt").value,
    emoji: $("a-emoji").value.trim() || "✨",
    description: $("a-desc").value,
    model_ref: $("a-model").value,
    mcp_names: Array.from(document.querySelectorAll("#a-mcps input:checked")).map((c) => c.value),
    pinned_topic: ptName ? { name: ptName, welcome: $("a-pt-welcome").value } : null,
  };
  if (!body.name) return show("助手名称必填", "err");
  await api("PUT", "/admin/api/assistants/" + encodeURIComponent(aid), body);
  show(`助手 ${body.name} 已保存`, "ok");
  await loadAssistants();
}

async function deleteAssistant(id) {
  await api("DELETE", "/admin/api/assistants/" + encodeURIComponent(id));
  show(`助手 ${id} 已删除`, "ok");
  await loadAssistants();
}

// ---------- MCP ----------

function headerRow(kv) {
  const div = document.createElement("div");
  div.className = "model-row";
  div.innerHTML = '<input class="h-key" placeholder="Header 名，如 Authorization">'
    + '<input class="h-val" placeholder="值">'
    + '<button type="button" data-action="del-row">删除行</button>';
  if (kv) {
    div.querySelector(".h-key").value = kv[0] || "";
    div.querySelector(".h-val").value = kv[1] || "";
  }
  return div;
}

function resetMcpForm() {
  $("m-form-title").textContent = "新建 MCP 服务";
  $("m-name").value = ""; $("m-name").disabled = false;
  $("m-type").value = "sse"; $("m-base-url").value = ""; $("m-active").checked = true;
  $("m-headers").innerHTML = "";
  $("m-headers").appendChild(headerRow());
  $("m-hint").textContent = "名称创建后不可改；headers 逐行键值，空行忽略。";
}

function renderMcps() {
  const rows = MCPS.map((s) => {
    const name = esc(s.name);
    const hs = Object.keys(s.headers || {}).length
      ? Object.entries(s.headers).map((kv) => esc(kv[0]) + "=" + esc(kv[1])).join("、")
      : "—";
    if (s.builtin) {
      // 内置 WeKnora MCP：只读展示 + 启停（写同名管理行控制 is_active）
      return `<tr><td>${name} <span class="badge">内置·公共知识库</span></td><td class="mono">${esc(s.type)}</td>
        <td class="mono">${esc(s.base_url)}</td><td>—</td><td>${s.is_active ? "是" : "否"}</td>
        <td class="ops">
          <button data-action="toggle-builtin-mcp" data-name="${name}" data-active="${s.is_active ? 1 : 0}">${s.is_active ? "停用" : "启用"}</button>
        </td></tr>`;
    }
    return `<tr><td>${name}</td><td class="mono">${esc(s.type)}</td>
      <td class="mono">${esc(s.base_url)}</td><td>${hs}</td><td>${s.is_active ? "是" : "否"}</td>
      <td class="ops">
        <button data-action="edit-mcp" data-name="${name}">编辑</button>
        <button class="danger" data-action="del-mcp" data-name="${name}">删除</button>
      </td></tr>`;
  }).join("");
  $("m-tbody").innerHTML = rows || '<tr><td colspan="6" class="muted">（暂无 MCP 服务）</td></tr>';
}

async function toggleBuiltinMcp(el) {
  const active = el.dataset.active !== "1";
  await api("PUT", "/admin/api/mcp-servers/" + encodeURIComponent(el.dataset.name),
    { type: "streamableHttp", base_url: "", headers: {}, is_active: active });
  show(`内置 WeKnora MCP 已${active ? "启用" : "停用"}，发布后客户端生效`, "ok");
  await loadMcps();
}

async function loadMcps() {
  const data = await api("GET", "/admin/api/mcp-servers");
  MCPS = data.mcp_servers || [];
  renderMcps();
  resetMcpForm();
}

function editMcp(name) {
  const s = MCPS.find((x) => x.name === name);
  if (!s) return show("MCP 服务不存在，请重新加载", "err");
  $("m-form-title").textContent = "编辑 MCP：" + s.name;
  $("m-name").value = s.name; $("m-name").disabled = true;
  $("m-type").value = s.type || "sse";
  $("m-base-url").value = s.base_url || "";
  $("m-active").checked = !!s.is_active;
  $("m-headers").innerHTML = "";
  Object.entries(s.headers || {}).forEach((kv) => $("m-headers").appendChild(headerRow(kv)));
  $("m-hint").textContent = "正在编辑 " + s.name;
  $("mcp-form").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function saveMcp() {
  const name = $("m-name").value.trim();
  if (!name) return show("MCP 名称必填", "err");
  const headers = {};
  for (const r of document.querySelectorAll("#m-headers .model-row")) {
    const k = r.querySelector(".h-key").value.trim();
    if (k) headers[k] = r.querySelector(".h-val").value;
  }
  const body = { type: $("m-type").value, base_url: $("m-base-url").value.trim(),
                 headers: headers, is_active: $("m-active").checked };
  await api("PUT", "/admin/api/mcp-servers/" + encodeURIComponent(name), body);
  show(`MCP ${name} 已保存`, "ok");
  await loadMcps();
}

async function deleteMcp(name) {
  await api("DELETE", "/admin/api/mcp-servers/" + encodeURIComponent(name));
  show(`MCP ${name} 已删除`, "ok");
  await loadMcps();
}

// ---------- 小程序 ----------

function renderMinapps() {
  const rows = MINAPPS.map((a) => {
    const id = esc(a.id);
    return `<tr><td class="mono">${id}</td><td>${esc(a.name)}</td><td class="mono">${esc(a.url)}</td>
      <td class="ops">
        <button data-action="edit-minapp" data-id="${id}">编辑</button>
        <button class="danger" data-action="del-minapp" data-id="${id}">删除</button>
      </td></tr>`;
  }).join("");
  $("mi-tbody").innerHTML = rows || '<tr><td colspan="4" class="muted">（暂无小程序）</td></tr>';
}

function resetMinappForm() {
  $("mi-form-title").textContent = "新建小程序";
  $("mi-id").value = ""; $("mi-id").disabled = false;
  $("mi-name").value = ""; $("mi-url").value = "";
  $("mi-hint").textContent = "ID 留空自动生成；URL 创建后不可改。";
}

async function loadMinapps() {
  const data = await api("GET", "/admin/api/minapps");
  MINAPPS = data.minapps || [];
  renderMinapps();
  resetMinappForm();
}

function editMinapp(id) {
  const a = MINAPPS.find((x) => x.id === id);
  if (!a) return show("小程序不存在，请重新加载", "err");
  $("mi-form-title").textContent = "编辑小程序：" + a.name;
  $("mi-id").value = a.id; $("mi-id").disabled = true;
  $("mi-name").value = a.name || "";
  $("mi-url").value = a.url || "";
  $("mi-hint").textContent = "正在编辑 " + a.id;
  $("minapp-form").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function saveMinapp() {
  let appId = $("mi-id").value.trim();
  if (!appId) appId = newId();
  const body = { name: $("mi-name").value.trim(), url: $("mi-url").value.trim() };
  if (!body.name) return show("小程序名称必填", "err");
  if (!body.url.startsWith("http://") && !body.url.startsWith("https://")) {
    return show("url 必须是 http(s) 地址", "err");
  }
  await api("PUT", "/admin/api/minapps/" + encodeURIComponent(appId), body);
  show(`小程序 ${body.name} 已保存`, "ok");
  await loadMinapps();
}

async function deleteMinapp(id) {
  await api("DELETE", "/admin/api/minapps/" + encodeURIComponent(id));
  show(`小程序 ${id} 已删除`, "ok");
  await loadMinapps();
}

// ---------- 知识库 ----------

async function loadKbs() {
  const st = await api("GET", "/admin/api/settings");
  const kb = st.kb_entries || {};
  $("kb-recent").checked = !!kb.recent_enabled;
  const saved = kb.kb_ids || [];
  let kbs = null;
  let note = "";
  try {
    kbs = (await api("GET", "/api/kbs", null, true)).kbs || [];
  } catch (e) {
    note = "知识库列表获取失败（WeKnora 不可达？）：" + e.message + "；先展示已保存的库。";
  }
  const list = kbs || saved.map((id) => ({ id: id, name: id }));
  $("kb-list").innerHTML = list.length
    ? list.map((k) => `<label class="check"><input type="checkbox" class="kb-item" value="${esc(k.id)}"${saved.includes(k.id) ? " checked" : ""}> ${esc(k.name)}（${esc(k.id)}）</label>`).join("")
    : '<span class="muted">（没有可用知识库）</span>';
  if (note) show(note, "err");
}

async function saveKbs() {
  const kbIds = Array.from(document.querySelectorAll("#kb-list .kb-item:checked")).map((c) => c.value);
  await api("PUT", "/admin/api/settings",
    { kb_entries: { recent_enabled: $("kb-recent").checked, kb_ids: kbIds } });
  show(`知识库设置已保存（${kbIds.length} 个库）`, "ok");
  await loadKbs();
}

// ---------- 插件市场 ----------

async function loadMarket() {
  const data = await api("GET", "/admin/api/marketplace");
  $("market-dir").textContent = data.dir;
  $("market-tbody").innerHTML = data.plugins.length
    ? data.plugins.map((p) => `<tr>
        <td>${esc(p.name || p.id)}</td>
        <td class="mono">${esc(p.version || "-")}</td>
        <td>${esc(p.department || "-")}</td>
        <td>${esc(p.author || "-")}</td>
        <td class="mono">${p.downloads}</td>
        <td>${p.valid ? "✅ 有效" : "❌ " + esc(p.error || "无效")}</td>
      </tr>`).join("")
    : '<tr><td colspan="6" class="muted">（目录为空）</td></tr>';
}

// ---------- 用量 ----------

async function loadUsage() {
  const days = $("usage-days").value;
  const s = await api("GET", "/admin/api/usage/summary?days=" + encodeURIComponent(days));
  $("usage-calls").textContent = Number(s.total_calls || 0).toLocaleString();
  $("usage-tokens").textContent = Number(s.total_tokens || 0).toLocaleString();
  $("um-tbody").innerHTML = (s.by_model || []).map((m) =>
    `<tr><td class="mono">${esc(m.model)}</td><td>${m.calls}</td><td>${Number(m.tokens).toLocaleString()}</td></tr>`
  ).join("") || '<tr><td colspan="3" class="muted">（窗口内没有调用）</td></tr>';
  const max = Math.max(1, ...(s.by_day || []).map((d) => d.calls));
  $("usage-days-chart").innerHTML = (s.by_day || []).map((d) => {
    const pct = Math.round((d.calls / max) * 100);
    return `<div class="chart-row"><span class="chart-label">${esc(d.day)}</span>`
      + `<span class="chart-track"><span class="chart-bar" style="width:${pct}%"></span></span>`
      + `<span class="chart-num">${d.calls} 次 · ${Number(d.tokens).toLocaleString()} tokens</span></div>`;
  }).join("") || '<span class="muted">（窗口内没有调用）</span>';
}

// ---------- 版本 / 发布 ----------

function renderPublished(data) {
  const latest = data.latest;
  setBadge(latest);
  $("pub-latest").textContent = latest
    ? `当前最新 v${latest.version}（${latest.published_at}）`
    : "尚无发布：配置好上游/助手等后点顶部「发布」。";
  $("pub-content").textContent = latest ? JSON.stringify(latest.content, null, 2) : "";
  $("v-tbody").innerHTML = (data.versions || []).map((v) => {
    const cur = latest && v.version === latest.version;
    return `<tr><td class="mono">${v.version}${cur ? "（最新）" : ""}</td><td>${esc(v.published_at)}</td>
      <td class="ops"><button class="danger" data-action="rollback" data-version="${v.version}">回滚</button></td></tr>`;
  }).join("") || '<tr><td colspan="3" class="muted">（还没有版本）</td></tr>';
}

async function loadVersions() {
  renderPublished(await api("GET", "/admin/api/published"));
}

async function rollback(el) {
  const v = Number(el.dataset.version);
  const data = await api("POST", "/admin/api/publish/rollback", { version: v });
  show(`已回滚：v${v} 快照重发为新版本 v${data.version}`, "ok");
  await loadVersions();
}

async function refreshBadge() {
  const data = await api("GET", "/admin/api/published");
  setBadge(data.latest);
}

async function publishConfig() {
  const data = await api("POST", "/admin/api/publish");
  show(`已发布 v${data.version}，客户端下次同步生效`, "ok");
  setBadge({ version: data.version });
  if (currentTab === "versions") await loadVersions();
}

// ---------- 标签页与事件分发 ----------

const LOADERS = {
  upstreams: loadUpstreams, defaults: loadDefaults, assistants: loadAssistants,
  mcp: loadMcps, minapps: loadMinapps, kbs: loadKbs, market: loadMarket, usage: loadUsage, versions: loadVersions,
};

async function switchTab(name) {
  currentTab = name;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("main > section").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
  if (LOADERS[name]) await run(null, LOADERS[name]);
}

function armedConfirm(el, verb) {
  if (el.dataset.armed !== "1") {
    el.dataset.armed = "1";
    el.dataset.orig = el.textContent;
    el.textContent = "确认" + verb + "?";
    el.classList.add("armed");
    setTimeout(() => {
      if (el.isConnected && el.dataset.armed === "1") {
        el.dataset.armed = "";
        el.textContent = el.dataset.orig;
        el.classList.remove("armed");
      }
    }, 4000);
    return false;
  }
  el.dataset.armed = "";
  el.textContent = el.dataset.orig;
  el.classList.remove("armed");
  return true;
}

const ACTIONS = {
  "save-token": () => saveTokenAction(),
  "publish": (el) => run(el, publishConfig),
  "new-upstream": () => resetUpstreamForm(),
  "add-model-row": (el) => run(el, addModelRow),
  "del-row": (el) => el.closest(".model-row").remove(),
  "save-upstream": (el) => run(el, saveUpstream),
  "edit-upstream": (el) => run(el, () => editUpstream(el.dataset.id)),
  "test-upstream": (el) => run(el, () => testUpstream(el)),
  "del-upstream": (el) => { if (armedConfirm(el, "删除")) run(el, () => deleteUpstream(el.dataset.id)); },
  "fetch-remote-models": (el) => run(el, fetchRemoteModels),
  "import-remote-models": (el) => run(el, importRemoteModels),
  "pull-model": (el) => run(el, pullModel),
  "save-base-url": (el) => run(el, saveBaseUrl),
  "save-base-prompt": (el) => run(el, saveBasePrompt),
  "save-world-url": (el) => run(el, saveWorldUrl),
  "save-defaults": (el) => run(el, saveDefaults),
  "save-assistant": (el) => run(el, saveAssistant),
  "edit-assistant": (el) => editAssistant(el.dataset.id),
  "del-assistant": (el) => { if (armedConfirm(el, "删除")) run(el, () => deleteAssistant(el.dataset.id)); },
  "add-header-row": () => $("m-headers").appendChild(headerRow()),
  "save-mcp": (el) => run(el, saveMcp),
  "edit-mcp": (el) => editMcp(el.dataset.name),
  "del-mcp": (el) => { if (armedConfirm(el, "删除")) run(el, () => deleteMcp(el.dataset.name)); },
  "toggle-builtin-mcp": (el) => run(el, () => toggleBuiltinMcp(el)),
  "save-minapp": (el) => run(el, saveMinapp),
  "edit-minapp": (el) => editMinapp(el.dataset.id),
  "del-minapp": (el) => { if (armedConfirm(el, "删除")) run(el, () => deleteMinapp(el.dataset.id)); },
  "save-kbs": (el) => run(el, saveKbs),
  "load-usage": (el) => run(el, loadUsage),
  "load-market": (el) => run(el, loadMarket),
  "load-published": (el) => run(el, loadVersions),
  "rollback": (el) => { if (armedConfirm(el, "回滚")) run(el, () => rollback(el)); },
};

function saveTokenAction() {
  const t = $("token").value.trim();
  if (!t) return show("请先填写管理 token", "err");
  localStorage.setItem("admin_token", t);
  show("token 已保存（仅存本机 localStorage）", "ok");
  run(null, refreshBadge);
  switchTab(currentTab);
}

document.body.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const fn = ACTIONS[el.dataset.action];
  if (fn) fn(el);
});

$("tabs").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-tab]");
  if (b) switchTab(b.dataset.tab);
});

$("pull-model-q").addEventListener("input", () => {
  clearTimeout(pullSearchTimer);
  pullSearchTimer = setTimeout(() => run(null, searchRepos), 500);
});
$("pull-model-repo").addEventListener("change", () => {
  const repo = $("pull-model-repo").value;
  if (repo) run(null, () => loadPullTags(repo));
});

const storedToken = localStorage.getItem("admin_token") || "";
$("token").value = storedToken;
if (storedToken) {
  run(null, refreshBadge);
  switchTab("upstreams");
} else {
  show("首次使用：填写管理 token（与 GATEWAY_TOKEN 相同）后点「保存 token」");
}
</script>
</body>
</html>
"""


@router.get("/admin", response_class=HTMLResponse)
def admin_page() -> str:
    """单文件管理页 v2（无构建依赖）：八个标签页维护结构化配置并发布/回滚。

    token 存 localStorage（401 清除重提示）；嵌入式 webview 不支持 prompt/alert/confirm，
    故反馈全走状态行，删除与回滚用两段式确认按钮。
    """
    return _ADMIN_HTML


# ────────────────────────── 内测下载页（/download） ──────────────────────────

_DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "downloads")

_DOWNLOAD_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>TJADKnows Desktop 下载（内测）</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 680px; margin: 2rem auto; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: 1px solid #ddd; padding: .5rem .7rem; text-align: left; font-size: .95rem; }
  th { background: #f5f5f5; }
  .muted { color: #888; font-size: .85rem; }
</style>
</head>
<body>
<h1>TJADKnows Desktop · 客户端下载（内测）</h1>
<p class="muted">安装即用：已内置企业配置，连上所里网络后启动自动同步。macOS 首次打开若提示未公证，
右键 → 打开，或执行 <code>xattr -cr "/Applications/TJADKnows Desktop.app"</code>。</p>
<table>
<tr><th>文件</th><th>大小</th><th>上传时间</th></tr>
__ROWS__
</table>
<p class="muted">无文件时请管理员将安装包放入 gateway/data/downloads/ 目录。</p>
</body>
</html>
"""


@router.get("/download", response_class=HTMLResponse)
def download_page() -> str:
    """内测安装包列表（局域网开放，与 /proxy 同语义；文件由管理员放入 downloads 目录）。"""
    rows = []
    try:
        for name in sorted(os.listdir(_DOWNLOAD_DIR)):
            if name.startswith("."):
                continue
            fp = os.path.join(_DOWNLOAD_DIR, name)
            if not os.path.isfile(fp):
                continue
            size_mb = os.path.getsize(fp) / 1048576
            mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M")
            rows.append(f'<tr><td><a href="/download/{quote(name)}">{html_escape(name)}</a></td>'
                        f'<td>{size_mb:.0f} MB</td><td>{mtime}</td></tr>')
    except OSError:
        pass
    if not rows:
        rows = ['<tr><td colspan="3">暂无安装包</td></tr>']
    return _DOWNLOAD_HTML.replace("__ROWS__", "\n".join(rows))


@router.get("/download/{name}")
def download_file(name: str) -> FileResponse:
    # 只允许纯文件名，防路径穿越
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=404, detail="not found")
    fp = os.path.join(_DOWNLOAD_DIR, name)
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(fp, filename=name)
