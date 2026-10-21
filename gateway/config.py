import json
import os


class Settings:
    # 实测实例（LAN 部署默认值）；检索客户端与 /proxy/weknora 反向代理共用此地址
    weknora_base_url: str = os.environ.get("WEKNORA_BASE_URL", "http://10.137.200.58:8091")
    weknora_api_key: str = os.environ.get("WEKNORA_API_KEY", "")
    # 智能检索管线：查询改写（本地 4B ollama）+ 自建重排（2080S bge-reranker ONNX）
    # 查询改写只对多轮指代消解有价值；/knowledge/api/search 无状态（无历史），
    # 4B 改写在单轮关键词查询上会漂移（编造年份/语义偏移）——默认关闭。
    search_rewrite_enabled: bool = os.environ.get("SEARCH_REWRITE_ENABLED", "0") == "1"
    search_rewrite_model: str = os.environ.get("SEARCH_REWRITE_MODEL", "qwen3.5:4b")
    search_ollama_url: str = os.environ.get("SEARCH_OLLAMA_URL", "http://10.121.16.5:11434")
    search_top_k_per_kb: int = int(os.environ.get("SEARCH_TOP_K_PER_KB", "5"))
    # 扇出并发：WeKnora hybrid-search 的向量路依赖单卡 bge-m3（串行处理），
    # 并发 ≥2 就会让 WeKnora 内部 embedding 超时 → 大面积空命中（实测并发 3
    # 时 20 库仅 1 库命中；串行全命中）。默认 1 = 串行，全目录 ~20-40s。
    fanout_concurrency: int = int(os.environ.get("FANOUT_CONCURRENCY", "1"))
    # 自建 rerank 服务（2080S 上的 bge-reranker-v2-m3，TEI 兼容形状，无需 key）
    rerank_base_url: str = os.environ.get("RERANK_BASE_URL", "http://192.168.66.24:8088")
    # 对齐 WeKnora 快速问答管线的 rerank_threshold 0.3：低于阈值的相关性直接丢弃（跨主题噪声过滤）
    rerank_threshold: float = float(os.environ.get("RERANK_THRESHOLD", "0.3"))
    # 账号 → 部门库前缀映射（共享库按人授权）。放环境变量而非 admin_settings：
    # 该键曾被 /admin 表单整包保存覆盖清空（审计日志 e01b9afb62 之后仍复现一次），
    # start.sh 显式导出 USER_DEPARTMENTS_JSON 管理它。
    user_departments: dict = json.loads(os.environ.get("USER_DEPARTMENTS_JSON", "{}"))
    gateway_token: str = os.environ.get("GATEWAY_TOKEN", "")


settings = Settings()
