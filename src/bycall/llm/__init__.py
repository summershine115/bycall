"""模型无关 LLM 抽象。

- ``LLMClient``（``base.py``）：统一 ``chat()`` / ``embed()`` 接口，业务层只依赖它。
- ``OpenAICompatClient``（``openai_compat.py``）：基于 openai SDK，base_url 可配，
  指向官方 / 代理 / 本地 vLLM 均可。
- ``GLMClient``（``glm.py``）：智谱 GLM 走 OpenAI 兼容协议，复用 OpenAICompatClient，
  仅默认 base_url 不同。
- ``create_llm_client()``（``factory.py``）：按 ``config.llm.provider`` 实例化。

新增 provider（如本地 Ollama / Anthropic）：实现 ``LLMClient``，在 factory 注册分支即可。
"""

from bycall.llm.base import LLMClient, Message

__all__ = ["LLMClient", "Message"]
