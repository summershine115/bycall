"""发布状态机（T2.4）：发布门结果落库 + 灰度路由 + 一键回滚。

与 ``evals/gate.py`` 的分工：
- 发布门负责「评不评得过」；本模块负责「通过之后怎么切流量、怎么退回去」。
- 灰度路由是**代码层判定**（稳定哈希分流），不是提示词/概率配置——策略仍在代码层。
- 回滚复用既有 ``version + is_active`` 模式：切换 is_active 即完成回滚，无新版本迁移。

灰度语义：
- ``canary_percent ∈ (0, 100)`` → status=canary：保留旧版 is_active，运行时按
  ``scope_id``（用户/会话）稳定哈希分流；
- ``0`` / ``100`` → 直接原子切换 is_active（全量），status=active。
"""

from __future__ import annotations

import hashlib

from sqlmodel import Session, select

from bycall.governance.models import Model, ReleaseState, SystemPrompt


class ReleaseTargetMissing(ValueError):
    """发布/回滚的目标不可解析（版本或模型已不存在，或压根没有上一版本）。

    切换 ``is_active`` 的前提是目标**真的存在**：目标缺失时必须 fail-closed 抛错且
    **不改动任何 is_active**。否则「先全部置 False、再激活目标」会退化成「全部置 False、
    没有目标可激活」——系统提示词静默变成空串、会话无模型可用，比直接报错危险得多。
    """


def in_canary(scope_id: int | None, percent: int) -> bool:
    """稳定哈希灰度：同一 scope 在比例不变时始终落在同一侧（不跳变）。"""
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.md5(str(scope_id).encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < percent


def get_release(session: Session, kind: str) -> ReleaseState | None:
    """按 kind 查当前发布状态（prompt / model）。"""
    return session.get(ReleaseState, kind)


def _get_prompt_by_version(session: Session, version: int | None):
    if version is None:
        return None
    return session.exec(select(SystemPrompt).where(SystemPrompt.version == version)).first()


def _get_active_prompt(session: Session):
    return session.exec(SystemPrompt.select_active()).first()


def _get_chat_model_by_id(session: Session, model_id: int | None):
    if model_id is None:
        return None
    entity = session.get(Model, model_id)
    if entity is None or entity.model_type != "chat":
        return None
    return entity


def _get_active_chat_model(session: Session):
    return session.exec(Model.select_active_by_type("chat")).first()


def resolve_prompt_version_for_user(session: Session, user_id: int | None) -> int | None:
    """当前用户应使用的提示词版本（canary 分流优先，否则当前激活版本）。

    返回 None 表示 DB 中无激活提示词（调用方按空 system_prompt 兜底）。
    """
    active = _get_active_prompt(session)
    release = get_release(session, "prompt")
    if (
        release is not None
        and release.status == "canary"
        and in_canary(user_id, release.canary_percent)
    ):
        target = _get_prompt_by_version(session, release.target_prompt_version)
        if target is not None:
            return target.version
    return active.version if active is not None else None


def resolve_prompt_content_for_user(session: Session, user_id: int | None) -> str:
    """当前用户应使用的提示词正文（canary 分流优先，否则当前激活版本）。"""
    active = _get_active_prompt(session)
    release = get_release(session, "prompt")
    if (
        release is not None
        and release.status == "canary"
        and in_canary(user_id, release.canary_percent)
    ):
        target = _get_prompt_by_version(session, release.target_prompt_version)
        if target is not None:
            return target.content
    return active.content if active is not None else ""


def resolve_canary_chat_model(session: Session, conversation_id: int | None):
    """canary 阶段的候选 chat Model（未命中灰度返回 None，调用方继续全局激活）。"""
    if conversation_id is None:
        return None
    release = get_release(session, "model")
    if (
        release is None
        or release.status != "canary"
        or not in_canary(conversation_id, release.canary_percent)
    ):
        return None
    return _get_chat_model_by_id(session, release.target_model_id)


def upsert_release(
    session: Session,
    *,
    kind: str,
    canary_percent: int,
    gate_report_json: str,
    target_prompt_version: int | None = None,
    previous_prompt_version: int | None = None,
    target_model_id: int | None = None,
    previous_model_id: int | None = None,
) -> ReleaseState:
    """创建/更新一次发布状态；canary_percent∈(0,100) 时置 canary，否则 active。"""
    release = get_release(session, kind)
    if release is None:
        release = ReleaseState(kind=kind)
        session.add(release)
    release.target_prompt_version = target_prompt_version
    release.previous_prompt_version = previous_prompt_version
    release.target_model_id = target_model_id
    release.previous_model_id = previous_model_id
    release.canary_percent = canary_percent
    release.status = "canary" if 0 < canary_percent < 100 else "active"
    release.gate_report_json = gate_report_json
    session.add(release)
    return release


def _deactivate_all_prompts(session: Session) -> None:
    for prompt in session.exec(select(SystemPrompt)).all():
        if prompt.is_active:
            prompt.is_active = False
            session.add(prompt)


def _deactivate_all_chat_models(session: Session) -> None:
    for model in session.exec(Model.select_by_type("chat")).all():
        if model.is_active:
            model.is_active = False
            session.add(model)


def activate_target(session: Session, release: ReleaseState) -> None:
    """全量激活 release.target（prompt / model），由调用方统一 commit。

    目标不存在（版本被删 / 模型被删或已非 chat）→ :class:`ReleaseTargetMissing`，
    且**不改动任何 is_active**（先解析目标，再动 is_active——空目标不得清空全部激活）。
    """
    if release.kind == "prompt":
        target = _get_prompt_by_version(session, release.target_prompt_version)
        if target is None:
            raise ReleaseTargetMissing(
                f"发布目标提示词 v{release.target_prompt_version} 已不存在，"
                "is_active 保持原状（请先恢复该版本或改选目标）"
            )
        _deactivate_all_prompts(session)
        target.is_active = True
        session.add(target)
        return
    target = _get_chat_model_by_id(session, release.target_model_id)
    if target is None:
        raise ReleaseTargetMissing(
            f"发布目标 chat 模型 id={release.target_model_id} 已不存在（或已非 chat 类型），"
            "is_active 保持原状"
        )
    _deactivate_all_chat_models(session)
    target.is_active = True
    session.add(target)


def activate_previous(session: Session, release: ReleaseState) -> None:
    """回滚：把 is_active 切回 previous（prompt 版本 / chat 模型），由调用方统一 commit。

    没有上一版本（首次发布）或上一版本已被删除 → :class:`ReleaseTargetMissing`，
    且**不改动任何 is_active**——回滚不是「清空」，没有回退目标就必须失败。
    """
    if release.kind == "prompt":
        if release.previous_prompt_version is None:
            raise ReleaseTargetMissing("该发布没有上一版本（首次发布），无法回滚")
        previous = _get_prompt_by_version(session, release.previous_prompt_version)
        if previous is None:
            raise ReleaseTargetMissing(
                f"上一版本提示词 v{release.previous_prompt_version} 已被删除，无法回滚"
            )
        _deactivate_all_prompts(session)
        previous.is_active = True
        session.add(previous)
        return
    if release.previous_model_id is None:
        raise ReleaseTargetMissing("该发布没有上一版本（首次发布），无法回滚")
    previous = _get_chat_model_by_id(session, release.previous_model_id)
    if previous is None:
        raise ReleaseTargetMissing(
            f"上一版本 chat 模型 id={release.previous_model_id} 已被删除或已非 chat 类型，无法回滚"
        )
    _deactivate_all_chat_models(session)
    previous.is_active = True
    session.add(previous)


def promote_release(session: Session, kind: str) -> ReleaseState | None:
    """灰度转全量：status=canary → active，并把 is_active 切到 target。"""
    release = get_release(session, kind)
    if release is None:
        return None
    if release.status not in ("canary", "active"):
        raise ValueError(f"发布状态 {release.status} 不可转全量")
    activate_target(session, release)
    release.status = "active"
    release.canary_percent = 100
    session.add(release)
    return release


def rollback_release(session: Session, kind: str) -> ReleaseState | None:
    """一键回滚：把 is_active 切回 previous，状态置 rolled_back。"""
    release = get_release(session, kind)
    if release is None:
        return None
    activate_previous(session, release)
    release.status = "rolled_back"
    session.add(release)
    return release


__all__ = [
    "ReleaseState",
    "ReleaseTargetMissing",
    "activate_previous",
    "activate_target",
    "get_release",
    "in_canary",
    "promote_release",
    "resolve_canary_chat_model",
    "resolve_prompt_content_for_user",
    "resolve_prompt_version_for_user",
    "rollback_release",
    "upsert_release",
]
