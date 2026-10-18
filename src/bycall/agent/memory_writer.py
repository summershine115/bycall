"""agent 保守可写记忆路径（S2 Phase 2）。

spec §4.1 + §6.2：执行任务后，LLM 从对话中提炼**高置信新事实**，写入该用户的长期记忆，
让数字员工跨会话「记住」用户。红线 #7 不变：agent 记忆不可信，写入时标 ``source="agent"``，
召回时由 ``load_agent_memory`` 降权进 **user 角色**（绝不进 system）；admin 记忆是权威，
agent 不覆盖。

成本约束（硬指标）：不能每轮都调提取 LLM。本模块只做「给定一轮值得提取时，如何提取+闸门+
去重+写入」；**触发判定在调用方**（``runner.run_agent`` / ``run_agent_stream``：仅 answered 且
本轮有工具调用、或任务执行路径才调本模块），纯闲聊/denied/error 不进入此处。

保守闸门（代码层判定，LLM 只是提议）：
- confidence ≥ 阈值（默认 0.85，``SystemSetting.memory.agent_confidence_threshold`` 可调）；
- ``volatile=false``（价格/临时状态等时间敏感信息不写）；
- category ∈ {preference/fact/note/relationship/goal}；
- 解析失败/格式错 → 静默跳过，绝不崩主流程。

去重（归一化文本相等）：同一用户已有任意来源/状态的记忆与待写事实归一化后相等 → 不写。
- 已有 **admin** 记忆覆盖同事实 → 不写（admin 权威，留给 admin 纠正语义）。
- 已有 **agent** 记忆（含被 admin 标 superseded 的）→ 不重复写、不复活已作废记忆。
归一化 = 去首尾空白 + 折叠内部空白 + 小写。选精确相等而非语义相似：零额外成本、无假阳性，
过度去重比过度写入更安全（保守优先）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from sqlmodel import Session, select

from bycall.db import session_scope
from bycall.governance.models import UserMemory
from bycall.governance.settings_store import get_setting_bool, get_setting_float

logger = logging.getLogger(__name__)

# 提取出的 category 白名单（与 UserMemory.category 注释对齐）。
_ALLOWED_CATEGORIES = ("preference", "fact", "note", "relationship", "goal")
# 单条事实长度上限：记忆应简洁自包含，过长多半是 LLM 把整段对话当事实。
_MAX_FACT_LEN = 300


@dataclass
class _ExtractSettings:
    """提取的可运行时调参数（SystemSetting）。"""

    enabled: bool
    confidence_threshold: float


def _read_extract_settings(session: Session) -> _ExtractSettings:
    """从 SystemSetting 读提取开关与置信阈值（DB > 环境变量 > 代码默认）。

    阈值走 SystemSetting 而非 AppConfig：admin 可在控制台不开代码、不重启即调，
    适配「线上观察提取质量后微调阈值」的运维节奏（与解析引擎/IM 配置同机制）。
    """
    return _ExtractSettings(
        enabled=get_setting_bool(
            "memory.agent_extract_enabled",
            default=True,
            env_var="BYCALL_MEMORY_AGENT_EXTRACT_ENABLED",
            session=session,
        ),
        confidence_threshold=get_setting_float(
            "memory.agent_confidence_threshold",
            default=0.85,
            env_var="BYCALL_MEMORY_AGENT_CONFIDENCE_THRESHOLD",
            session=session,
        ),
    )


def maybe_extract_and_save_memory(
    *,
    llm: BaseChatModel,
    user_id: int,
    question: str,
    answer: str,
) -> int:
    """从一轮对话提取高置信事实并写入 agent 记忆，返回实际写入条数。

    用独立 session（不与调用方事务耦合）；任何失败（LLM 异常、解析错、闸门拒、写库失败）
    静默跳过、只记日志，**绝不向上抛**——这是主流程之外的增强，不能拖垮对话本身。
    """
    written = 0
    try:
        with session_scope() as session:
            settings = _read_extract_settings(session)
        if not settings.enabled:
            return 0
        candidates = _extract_candidates(llm, question, answer)
        facts = [gated for cand in candidates if (gated := _gate(cand, settings)) is not None]
        if not facts:
            return 0
        with session_scope() as session:
            existing = {
                _normalize(content)
                for content in session.exec(
                    select(UserMemory.content)
                    .where(UserMemory.user_id == user_id)
                    .execution_options(yield_per=128)
                )
            }
            for fact, category, confidence in facts:
                norm = _normalize(fact)
                if norm in existing:
                    continue
                session.add(
                    UserMemory(
                        user_id=user_id,
                        content=fact,
                        category=category,
                        source="agent",
                        confidence=confidence,
                    )
                )
                existing.add(norm)
                written += 1
            if written:
                logger.info("agent 记忆提取写入 %d 条 user_id=%s", written, user_id)
    except Exception:
        logger.exception("agent 记忆提取/写入失败（忽略，不影响主流程）")
    return written


def _extract_candidates(llm: BaseChatModel, question: str, answer: str) -> list[dict]:
    """调一次 LLM 做结构化提取，返回候选事实列表。LLM 异常/解析失败 → 返回 []。"""
    from langchain_core.messages import HumanMessage

    from bycall.agent.graph import _content_to_text

    prompt = _build_extract_prompt(question, answer)
    try:
        resp = llm.invoke([HumanMessage(content=prompt)])
        raw = _content_to_text(resp.content).strip()
    except Exception:
        logger.debug("记忆提取 LLM 调用失败（跳过）", exc_info=True)
        return []
    return _parse_json_facts(raw)


def _build_extract_prompt(question: str, answer: str) -> str:
    """构造防幻觉提取 prompt：只提取明确出现的高置信持久事实。"""
    return (
        "你是用户记忆提取器。从下面这轮对话中提取关于该用户的**明确、高置信、持久**的事实，"
        "用于跨会话记住用户。\n\n"
        "## 严格规则\n"
        "- 只提取对话里**明确出现**的事实：用户亲口说的偏好/身份/关系/目标，或工具执行确认的事实。\n"
        "- **绝不推断、脑补、猜测**或用常识补充；模棱两可或不确定 → 不提取。\n"
        "- 只提取**长期有效**的持久事实。时间敏感信息（当前价格、临时状态、今天的日期/天气、"
        "一次性查询结果）必须标 volatile=true（这类不会被记录）。\n"
        "- 每条事实简洁、自包含（例：「偏好用中文回复」「负责华东区销售」「目标：下半年拿 PMP」）。\n\n"
        "## category 取值\n"
        "- preference：用户偏好（语言/格式/沟通风格等）\n"
        "- fact：稳定的客观事实（身份/职责/所属团队等）\n"
        "- relationship：与他人/部门的关系\n"
        "- goal：用户提到的目标/计划\n"
        "- note：其他值得记住的持久信息\n\n"
        "## 输出格式\n"
        "严格输出一个 JSON 数组，不要任何解释或前后缀文字。无可提取事实时输出 []：\n"
        '[{"fact": "事实内容", "category": "preference", "confidence": 0.9, "volatile": false}]\n\n'
        "## 本轮对话\n"
        f"用户：{question}\n"
        f"助手：{answer}"
    )


def _parse_json_facts(text: str) -> list[dict]:
    """从 LLM 输出解析事实数组，容错围栏/多余文字。非法 → []。"""
    text = text.strip()
    # 去掉 ```json ... ``` / ``` ... ``` 围栏。
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _gate(cand: dict, settings: _ExtractSettings) -> tuple[str, str, float] | None:
    """保守闸门（代码层判定）：通过返回 (fact, category, confidence)，否则 None。

    LLM 只是提议，是否落库由代码层按阈值/volatile/category/长度裁定。
    """
    if cand.get("volatile") is True:
        return None  # 时间敏感信息不写
    try:
        confidence = float(cand.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if confidence < settings.confidence_threshold:
        return None
    category = str(cand.get("category", "")).strip()
    if category not in _ALLOWED_CATEGORIES:
        return None
    fact = str(cand.get("fact", "")).strip()
    if not fact or len(fact) > _MAX_FACT_LEN:
        return None
    return fact, category, confidence


def _normalize(text: str) -> str:
    """归一化：小写 + 去除全部空白。去重匹配用，消除大小写/空格/换行差异。

    去除全部空白（而非折叠为单空格）：中文事实原本无词间空格，折叠会把「负责华东区销售」
    与「负责 华东 区销售」判为不同——去空白才能正确去重。仅用于匹配，不影响落库原文。
    """
    return "".join(text.lower().split())


__all__ = ["maybe_extract_and_save_memory"]
