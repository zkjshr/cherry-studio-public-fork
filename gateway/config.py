import os


class Settings:
    # 实测实例（LAN 部署默认值）；检索客户端与 /proxy/weknora 反向代理共用此地址
    weknora_base_url: str = os.environ.get("WEKNORA_BASE_URL", "http://10.137.200.58:8091")
    weknora_api_key: str = os.environ.get("WEKNORA_API_KEY", "")
    # 智能检索管线：查询改写（本地 4B ollama）+ 可选 rerank（siliconflow，key 空则关闭）
    search_rewrite_enabled: bool = os.environ.get("SEARCH_REWRITE_ENABLED", "1") == "1"
    search_rewrite_model: str = os.environ.get("SEARCH_REWRITE_MODEL", "qwen3.5:4b")
    search_ollama_url: str = os.environ.get("SEARCH_OLLAMA_URL", "http://10.121.16.5:11434")
    search_top_k_per_kb: int = int(os.environ.get("SEARCH_TOP_K_PER_KB", "5"))
    siliconflow_rerank_key: str = os.environ.get("SILICONFLOW_RERANK_KEY", "")
    # 自建 rerank 服务（2080S 上的 bge-reranker-v2-m3，TEI 兼容形状，无需 key）
    rerank_base_url: str = os.environ.get("RERANK_BASE_URL", "http://192.168.66.24:8088")
    gateway_token: str = os.environ.get("GATEWAY_TOKEN", "")


settings = Settings()
