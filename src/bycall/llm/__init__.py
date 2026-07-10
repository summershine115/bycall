"""模型无关 LLM 抽象（拆分版：对话与向量化解耦）。

- ``ChatModel`` / ``EmbedModel``（``base.py``）：对话与向量化两个独立接口，
  可各自挂不同 provider（参考 WeKnora）。
- ``LLMClient``（``base.py``）：chat+embed 复合兼容接口（历史遗留），保留为
  ``ChatModel``+``EmbedModel`` 子类，不破坏旧调用点与 ``MagicMock(spec=LLMClient)``。
- ``OpenAIChatModel`` / ``OpenAIEmbedModel``（``openai_compat.py``）：基于 openai SDK，
  ``base_url`` 可指向官方 / 代理 / 本地 vLLM / Ollama 等 OpenAI 兼容端点。
- ``GLMClient``（``glm.py``）：薄兼容层，``OpenAIChatModel`` + 智谱默认 URL。
- ``resolver``（``resolver.py``）：DB 激活 ``Model`` 实体 → ChatModel / EmbedModel。
- ``create_chat_model`` / ``create_embed_model``（``factory.py``）：新代码入口。
- ``create_llm_client``（``factory.py``）：兼容入口，返回对话模型。

新增非兼容 provider：在 ``resolver.resolve_chat_model`` /
``resolve_embed_model`` 按 ``model.provider`` 加分支即可。
"""

from bycall.llm.base import ChatModel, EmbedModel, LLMClient, Message

__all__ = ["ChatModel", "EmbedModel", "LLMClient", "Message"]
