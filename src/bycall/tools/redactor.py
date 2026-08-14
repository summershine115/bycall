"""工具结果统一脱敏 + 大小限制（支柱 4 第二条泄漏面 + 治理缝隙 #5）。

把所有工具返回值的「脱敏 + 截断」收敛到一处，而非散落在各工具的 redactor 或审批执行路径：
- per-tool ``redactor``（可选）先跑，产出脱敏值（``redactor(raw, cap) -> 值``）；
- 然后统一截断到 ``MAX_TOOL_RESULT_CHARS``（防工具返回超大字符串撑爆 LLM 上下文 / 费用 DoS）；
- 非 str 结果转 str（ToolMessage 对 str 原样透传的契约）。

这是工具网关与审批执行共用的默认后处理——无论工具是否声明 redactor，结果都经此管道进
LLM 上下文与审计。无 redactor 的工具也受截断保护（此前原样透传、无上限）。
"""

from __future__ import annotations

# 工具结果统一长度上限：与数据面 MAX_QUESTION_LEN 对齐，防上下文撑爆 / 费用 DoS。
MAX_TOOL_RESULT_CHARS = 4000


def sanitize_tool_result(raw, cap, redactor=None, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """工具返回值 → 脱敏 + 截断后的字符串（进 LLM 上下文 / 审计）。"""
    if redactor is not None:
        raw = redactor(raw, cap)
    text = raw if isinstance(raw, str) else str(raw)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（结果已截断，原始 {len(text)} 字符）"
    return text
