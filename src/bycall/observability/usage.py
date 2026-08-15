"""LLM token 用量与成本估算（阶段 2 · T2.5 五维指标之二）。

成本口径：``input_tokens * 单价 + output_tokens * 单价``（美元），单价分层：
DB ``SystemSetting``（``cost.chat_input_usd_per_1m`` / ``cost.chat_output_usd_per_1m``）
> 环境变量（``BYCALL_COST_CHAT_INPUT_USD_PER_1M`` / ``BYCALL_COST_CHAT_OUTPUT_USD_PER_1M``）
> 代码默认（gpt-4o-mini 公开价量级，仅作预算参考，不参与计费）。
单价经 TTL 缓存（60s），避免每条 LLM 回调都开 DB 会话。
"""

from __future__ import annotations

import logging
import os
import time

from langchain_core.callbacks import BaseCallbackHandler
from prometheus_client import Counter

logger = logging.getLogger(__name__)

DEFAULT_INPUT_USD_PER_1M = 0.15
DEFAULT_OUTPUT_USD_PER_1M = 0.60

LLM_TOKENS = Counter(
    "bycall_llm_tokens_total",
    "LLM token 用量（prompt/completion）",
    ["kind", "direction"],  # kind: chat；direction: input/output（低基数）
)
LLM_COST = Counter(
    "bycall_cost_total",
    "LLM 调用成本估算（美元）",
    ["kind"],  # kind: chat（低基数）
)

# TTL 缓存：单价是 admin 可改的运行时配置，但不能为此在每条回调里开 DB 会话。
_CACHE_TTL_SECONDS = 60.0
_cost_cache: dict = {"rates": None, "ts": 0.0}


def _default_rates() -> tuple[float, float]:
    """环境变量 → 代码默认（DB 层在 ``get_cost_rates`` 里叠加）。"""
    input_rate = float(
        os.environ.get("BYCALL_COST_CHAT_INPUT_USD_PER_1M", DEFAULT_INPUT_USD_PER_1M)
    )
    output_rate = float(
        os.environ.get("BYCALL_COST_CHAT_OUTPUT_USD_PER_1M", DEFAULT_OUTPUT_USD_PER_1M)
    )
    return input_rate, output_rate


def _rates_from_db() -> tuple[float, float] | None:
    """从 SystemSetting 读单价；DB 不可达 / 未配置时返回 None（不阻塞主链路）。"""
    try:
        from sqlmodel import select

        from bycall.db import session_scope
        from bycall.governance.models import SystemSetting

        with session_scope() as session:
            rows = {
                row.key: row.value
                for row in session.exec(
                    select(SystemSetting).where(
                        SystemSetting.key.in_(
                            ["cost.chat_input_usd_per_1m", "cost.chat_output_usd_per_1m"]
                        )
                    )
                ).all()
            }
        if not rows:
            return None
        input_rate = float(rows.get("cost.chat_input_usd_per_1m") or DEFAULT_INPUT_USD_PER_1M)
        output_rate = float(rows.get("cost.chat_output_usd_per_1m") or DEFAULT_OUTPUT_USD_PER_1M)
        return input_rate, output_rate
    except Exception:
        logger.debug("读取成本单价失败（用环境变量/默认值）", exc_info=True)
        return None


def get_cost_rates() -> tuple[float, float]:
    """当前生效的（输入、输出）每百万 token 美元单价。"""
    now = time.monotonic()
    if _cost_cache["rates"] is not None and now - _cost_cache["ts"] < _CACHE_TTL_SECONDS:
        return _cost_cache["rates"]
    rates = _rates_from_db() or _default_rates()
    _cost_cache["rates"] = rates
    _cost_cache["ts"] = now
    return rates


def clear_cost_cache() -> None:
    """清空单价缓存（测试 / 设置变更后调用）。"""
    _cost_cache["rates"] = None
    _cost_cache["ts"] = 0.0


def record_llm_usage(kind: str, input_tokens: int, output_tokens: int) -> None:
    """记录一次 LLM 调用的 token 用量 + 成本估算。"""
    if input_tokens < 0 or output_tokens < 0:
        return
    LLM_TOKENS.labels(kind=kind, direction="input").inc(input_tokens)
    LLM_TOKENS.labels(kind=kind, direction="output").inc(output_tokens)
    input_rate, output_rate = get_cost_rates()
    cost_usd = input_tokens / 1_000_000 * input_rate + output_tokens / 1_000_000 * output_rate
    if cost_usd > 0:
        LLM_COST.labels(kind=kind).inc(cost_usd)


class LLMUsageCallbackHandler(BaseCallbackHandler):
    """挂在 ChatOpenAI 上的回调：从 LLMResult.llm_output 取 token_usage 记指标。

    token_usage 是 OpenAI 兼容接口的标准返回；拿不到（旧 provider / 假模型）时静默
    跳过——用量指标是运营数据，不应因为缺元数据而让对话失败。
    """

    def on_llm_end(self, response, **kwargs) -> None:
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or {}
        try:
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            return
        record_llm_usage("chat", input_tokens, output_tokens)


__all__ = [
    "DEFAULT_INPUT_USD_PER_1M",
    "DEFAULT_OUTPUT_USD_PER_1M",
    "LLMUsageCallbackHandler",
    "clear_cost_cache",
    "get_cost_rates",
    "record_llm_usage",
]
