"""模型无关 LLM 抽象。

- ``Message`` / ``EmbedModel``（``base.py``）：prompt 拼装中间类型 + 向量化抽象接口。
- ``OpenAIEmbedModel``（``openai_compat.py``）：基于 openai SDK 的向量化实现，
  ``base_url`` 可指向官方 / 代理 / 本地 vLLM / Ollama 等 OpenAI 兼容端点。
- ``resolver``（``resolver.py``）：DB 激活 ``Model`` 实体 → ChatOpenAI / EmbedModel。
- ``create_chat_model`` / ``create_embed_model``（``factory.py``）：调用方入口。

对话模型（M3）：统一切到 ``langchain-openai`` 的 ``ChatOpenAI``（``BaseChatModel``，
原生 ``bind_tools`` 供 LangGraph ``ToolNode`` 消费），不在本包定义自研 chat 抽象。

新增非兼容 provider：在 ``resolver.resolve_chat_model`` /
``resolve_embed_model`` 按 ``model.provider`` 加分支即可。
"""

from bycall.llm.base import EmbedModel, Message

__all__ = ["EmbedModel", "Message"]
