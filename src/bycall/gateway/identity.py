"""IM 身份 → Bycall 用户映射。

数据面认证的入口：IM 渠道提供可信的 ``im_user_id``（企微是 body.from.userid，
由企微服务端签发，不可伪造），这里查 ``User.im_bindings`` 映射到 Bycall 用户记录。

映射规则：``User.im_bindings`` 是 ``{channel: im_user_id}``，按 (channel, im_user_id)
精确匹配。一个员工可能在企微和飞书各有 user_id，都指向同一条 User 记录。

边界：
- 未绑定（无匹配 User）→ 返回 None。调用方据此引导绑定或拒绝（邀请制）。
- 禁用用户（``is_active=False``）→ 不在 ``select_by_im`` 的候选集里，自然返回 None。
  （禁用优先于其他判定，与 ``capability.compute_capability_set`` 的禁用拒绝一致。）
"""

from __future__ import annotations

from sqlmodel import Session

from bycall.governance.models import User


def resolve_user_by_im(session: Session, channel: str, im_user_id: str) -> User | None:
    """按 (channel, im_user_id) 查 User.im_bindings，返回匹配的 active User，无则 None。

    ``User.select_by_im`` 当前只过滤 ``is_active=True``，IM 维度的匹配由本函数在
    Python 侧遍历 ``im_bindings`` 完成（SQLite 下 JSON 列查询受限，见 models.py 注释；
    迁 Postgres 后可下推 JSONB 查询）。

    :param session: 已打开的 DB 会话（由 ``session_scope`` / ``get_session`` 提供）。
    :param channel: 渠道名，如 "wecom" / "feishu"。
    :param im_user_id: IM 侧用户标识。
    :return: 匹配的 active ``User``；未绑定 / 禁用 / 不存在均返回 None。
    """
    if not im_user_id:
        # 空身份（frame 缺 from.userid）无法映射，直接拒绝，避免误匹配到空绑定。
        return None

    candidates = session.exec(User.select_by_im(channel, im_user_id)).all()
    for user in candidates:
        bindings = user.im_bindings or {}
        if bindings.get(channel) == im_user_id:
            return user
    return None
