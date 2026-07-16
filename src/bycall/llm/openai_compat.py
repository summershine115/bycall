"""OpenAI 兼容模型实现：对话与向量化各自独立。

基于官方 ``openai`` SDK，``base_url`` 可指向：
- OpenAI 官方：``https://api.openai.com/v1``
- 代理 / 网关：任意兼容地址
- 本地推理服务：vLLM / Ollama 的 OpenAI 兼容端点

智谱 GLM / 阿里通义 / DeepSeek 等 Provider 都提供 OpenAI 兼容端点，因此
``resolver`` 对所有 provider 统一用本模块的两个实现分发，provider 字段只用于
UI 展示默认 URL（见 ``llm/providers.py``），不参与分发逻辑。

拆分为两个类后，对话模型与向量模型可各自挂不同 endpoint/key（如 chat 用智谱、
embed 用本地 Ollama），这正是参考 WeKnora 的设计目标。

⚠️ M0 不强制真实 API key（冒烟测试用 mock）。生产用前需在 DB 配好 key。
"""

from __future__ import annotations

from openai import OpenAI

from bycall.llm.base import ChatModel, EmbedModel, Message

# ============ 对话模型 ============
#
# ⚠️ Deprecated（M3，见 ADR-0002）：对话模型已统一切到 ``langchain-openai`` 的
# ``ChatOpenAI``（原生 ``bind_tools``，供 LangGraph ``ToolNode`` 消费）。``OpenAIChatModel``
# 保留代码不删（降风险），但生产路径（``resolver`` / ``factory``）不再构造它。后续里程碑清理。


class OpenAIChatModel(ChatModel):
    """[Deprecated] OpenAI 兼容对话模型。一个实例 = 一组 base_url + key + model。

    M3 起对话路径改用 ``ChatOpenAI``（支持 function calling）。本类仅保留给历史
    import 与测试一个不崩的入口，不再被 ``resolver`` / ``factory`` 构造。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        # 显式持有 client，便于测试注入 mock / 录制。
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    def chat(self, messages: list[Message], *, temperature: float = 0.7) -> str:
        payload = [{"role": m.role, "content": m.content} for m in messages]
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=payload,
            temperature=temperature,
            max_tokens=2048,  # 费用控制：限制输出长度，防止恶意/异常输入导致天价账单
        )
        # 取首条 assistant 回复；空 choices 视为空回复而非报错（鲁棒性）。
        return resp.choices[0].message.content or "" if resp.choices else ""

    # ---- 测试友好：允许替换底层 client（mock 注入）----

    def _set_client(self, client: object) -> None:
        """仅供测试注入 mock client。"""
        self._client = client  # type: ignore[assignment]


# ============ 向量化模型 ============


class OpenAIEmbedModel(EmbedModel):
    """OpenAI 兼容向量化模型。一个实例 = 一组 base_url + key + model + dimension。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._dimension = dimension
        # 显式持有 client，便于测试注入 mock / 录制。
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)

    @property
    def dimension(self) -> int:
        """输出向量维度（构造时传入，供向量库 schema 对齐）。"""
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._model, input=texts)
        # 按 index 对齐，避免返回顺序不稳。
        return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]

    # ---- 测试友好：允许替换底层 client（mock 注入）----

    def _set_client(self, client: object) -> None:
        """仅供测试注入 mock client。"""
        self._client = client  # type: ignore[assignment]


__all__ = ["OpenAIChatModel", "OpenAIEmbedModel"]
