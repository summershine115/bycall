"""模型服务商注册表（参考 WeKnora 的 GET /models/providers）。

后端维护一份 provider 元数据清单——每个 provider 的默认 URL、支持的模型类型、
推荐模型名。前端新建模型时从 ``GET /api/admin/models/providers`` 动态拉取，
用于填充 provider 下拉 + 自动填默认 URL + 推荐模型名。

新增 provider：在 ``PROVIDERS`` 列表里加一个 ``ProviderInfo`` 即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProviderInfo:
    """单个服务商的元数据。"""

    value: str  # 标识，如 "zhipu"
    label: str  # 显示名，如 "智谱 BigModel"
    # 各模型类型的默认 API 地址（用于新建模型时自动填充）。
    default_urls: dict[str, str] = field(default_factory=dict)
    # 该 provider 支持的模型类型。
    model_types: list[str] = field(default_factory=lambda: ["chat", "embedding"])
    # 推荐模型名（供前端下拉/占位提示）。
    sample_models: dict[str, list[str]] = field(default_factory=dict)
    description: str = ""


# 主流服务商清单（从 WeKnora 的 19 个里挑企业常用的先做）。
PROVIDERS: list[ProviderInfo] = [
    ProviderInfo(
        value="generic",
        label="自定义 (OpenAI 兼容)",
        default_urls={"chat": "", "embedding": "", "rerank": ""},
        model_types=["chat", "embedding", "rerank"],
        description="任意 OpenAI 兼容端点（代理 / 本地 vLLM / Ollama）",
    ),
    ProviderInfo(
        value="jina",
        label="Jina AI",
        default_urls={"rerank": "https://api.jina.ai/v1"},
        model_types=["rerank"],
        sample_models={"rerank": ["jina-reranker-v2-base-multilingual"]},
        description="专精 rerank，jina-reranker-v2 多语言支持",
    ),
    ProviderInfo(
        value="openai",
        label="OpenAI",
        default_urls={
            "chat": "https://api.openai.com/v1",
            "embedding": "https://api.openai.com/v1",
        },
        model_types=["chat", "embedding"],
        sample_models={
            "chat": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
            "embedding": ["text-embedding-3-small", "text-embedding-3-large"],
        },
        description="gpt-4o, text-embedding-3 等",
    ),
    ProviderInfo(
        value="zhipu",
        label="智谱 BigModel",
        default_urls={
            "chat": "https://open.bigmodel.cn/api/paas/v4",
            "embedding": "https://open.bigmodel.cn/api/paas/v4",
        },
        model_types=["chat", "embedding"],
        sample_models={
            "chat": ["glm-4-plus", "glm-4-flash", "glm-4-air"],
            "embedding": ["embedding-3", "embedding-2"],
        },
        description="glm-4-plus, embedding-3 等；国内合规友好",
    ),
    ProviderInfo(
        value="aliyun",
        label="阿里云 DashScope (通义千问)",
        default_urls={
            "chat": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "embedding": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
        model_types=["chat", "embedding"],
        sample_models={
            "chat": ["qwen-plus", "qwen-turbo", "qwen-max"],
            "embedding": ["text-embedding-v3", "text-embedding-v2"],
        },
        description="qwen-plus, text-embedding-v3 等",
    ),
    ProviderInfo(
        value="deepseek",
        label="DeepSeek",
        default_urls={"chat": "https://api.deepseek.com/v1"},
        model_types=["chat"],
        sample_models={"chat": ["deepseek-chat", "deepseek-reasoner"]},
        description="deepseek-chat / deepseek-reasoner（仅对话）",
    ),
    ProviderInfo(
        value="siliconflow",
        label="硅基流动 SiliconFlow",
        default_urls={
            "chat": "https://api.siliconflow.cn/v1",
            "embedding": "https://api.siliconflow.cn/v1",
            "rerank": "https://api.siliconflow.cn/v1",
        },
        model_types=["chat", "embedding", "rerank"],
        sample_models={
            "chat": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
            "embedding": ["BAAI/bge-m3", "BAAI/bge-large-zh-v1.5"],
            "rerank": ["BAAI/bge-reranker-v2-m3"],
        },
        description="聚合平台，支持多种开源模型；rerank 可用 bge-reranker-v2-m3",
    ),
    ProviderInfo(
        value="moonshot",
        label="月之暗面 Moonshot (Kimi)",
        default_urls={"chat": "https://api.moonshot.cn/v1"},
        model_types=["chat"],
        sample_models={"chat": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]},
        description="Kimi 系列长上下文模型",
    ),
    ProviderInfo(
        value="ollama",
        label="本地 Ollama",
        default_urls={
            "chat": "http://localhost:11434/v1",
            "embedding": "http://localhost:11434/v1",
        },
        model_types=["chat", "embedding"],
        sample_models={
            "chat": ["qwen2.5:7b", "llama3.1:8b"],
            "embedding": ["nomic-embed-text", "bge-m3"],
        },
        description="完全本地部署，数据不出域；需先安装 Ollama 并拉取模型",
    ),
]


def list_providers(model_type: str | None = None) -> list[ProviderInfo]:
    """返回 provider 列表。``model_type`` 过滤（如只看支持 embedding 的）。"""
    if model_type is None:
        return PROVIDERS
    return [p for p in PROVIDERS if model_type in p.model_types]


def get_provider(value: str) -> ProviderInfo | None:
    """按 value 查单个 provider。"""
    for p in PROVIDERS:
        if p.value == value:
            return p
    return None


def get_default_url(provider: str, model_type: str) -> str:
    """取某 provider 某 model_type 的默认 URL（无则空串）。"""
    info = get_provider(provider)
    if info is None:
        return ""
    return info.default_urls.get(model_type, "")


def to_dict_list(providers: list[ProviderInfo]) -> list[dict]:
    """序列化供 API 返回。"""
    return [
        {
            "value": p.value,
            "label": p.label,
            "default_urls": p.default_urls,
            "model_types": p.model_types,
            "sample_models": p.sample_models,
            "description": p.description,
        }
        for p in providers
    ]


__all__ = [
    "PROVIDERS",
    "ProviderInfo",
    "get_default_url",
    "get_provider",
    "list_providers",
    "to_dict_list",
]
