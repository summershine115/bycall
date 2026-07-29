"""M6 IM 处理器单测 —— 多模态输入 / 引用卡片 / 模板卡片点击。

覆盖三组契约：
1. 文件附件 → parse_document 以其字节被调用 → 解析文本并入问题；解析失败优雅降级不崩。
2. RAG 引用：结果携带检索来源时最终帧附 msg_item；无检索命中时不附。
3. card_action（模板卡片点击）按普通文本问题进入治理链路。

身份映射 / 限流 / session 均打桩，run_agent_stream 用假异步生成器替换，
聚焦 im_handler 自身的多模态拼装与回复组装逻辑。
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from bycall.agent.im_handler import handle_im_message
from bycall.agent.runner import AgentResult
from bycall.gateway.base import Attachment, InboundMessage, ReplySender

# ============ 公共桩 ============


@contextlib.contextmanager
def _handler_env(*, user_id: int = 7, allow: bool = True):
    """桩掉身份映射 / 限流 / session_scope，让 handle_im_message 一路走到流式。"""
    user = MagicMock(id=user_id)
    limiter = MagicMock()
    limiter.allow.return_value = allow
    with (
        patch("bycall.agent.im_handler.session_scope") as mock_scope,
        patch("bycall.agent.im_handler.resolve_user_by_im", return_value=user),
        patch("bycall.governance.ratelimit.get_limiter", return_value=limiter),
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_scope.return_value.__exit__ = MagicMock(return_value=None)
        yield limiter


def _make_streaming_sender(cap: list | None = None, *, with_card: bool = False):
    """流式 sender：可选模板卡片能力，所有发送内容写入 capture。"""
    cap = [] if cap is None else cap

    async def send_text(content):
        cap.append(("text", content))

    async def send_stream(content, stream_id, finish, msg_item=None):
        cap.append(("stream", content, stream_id, finish, msg_item))

    async def send_card(card):
        cap.append(("card", card))

    return ReplySender(
        send_text=send_text,
        send_stream=send_stream,
        send_card=send_card if with_card else None,
        channel="wecom",
    ), cap


def _make_legacy_streaming_sender(cap: list | None = None):
    """旧渠道 sender：仅支持 ``content/stream_id/finish`` 三参数。"""
    cap = [] if cap is None else cap

    async def send_text(content):
        cap.append(("text", content))

    async def send_stream(content, stream_id, finish):
        cap.append(("stream", content, stream_id, finish))

    return ReplySender(send_text=send_text, send_stream=send_stream, channel="legacy"), cap


def _make_text_sender(cap: list | None = None):
    """非流式 sender：send_stream=None，强制走 send_text 降级路径。"""
    cap = [] if cap is None else cap

    async def send_text(content):
        cap.append(("text", content))

    return ReplySender(send_text=send_text, send_stream=None, channel="wecom"), cap


def _answered_result(
    answer: str = "基于知识库的回答。", citations: list[dict] | None = None
) -> AgentResult:
    return AgentResult(
        answer=answer,
        decision="answered",
        knowledge_scope=["public"],
        retrieved_chunks=len(citations) if citations else 1,
        user_name="u",
        role="staff",
        tool_calls=[],
        citations=citations or [],
    )


def _run_stream_capturing(capture: dict, result: AgentResult):
    """构造假 _run_stream：记录传入的 question，吐一帧 final result。"""

    async def gen(user_id, question, channel, im_user_id=""):
        capture["question"] = question
        capture["user_id"] = user_id
        capture["channel"] = channel
        capture["im_user_id"] = im_user_id
        yield "", True, result

    return gen


def _run_stream_plain(result: AgentResult):
    async def gen(user_id, question, channel, im_user_id=""):
        yield "", True, result

    return gen


def _text_msg(text: str = "公司介绍", **kw) -> InboundMessage:
    return InboundMessage(channel="wecom", im_user_id="u1", text=text, raw_frame={}, **kw)


# ============ A. 多模态输入 ============


def test_file_attachment_parsed_and_appended():
    """文件附件 → parse_document 以其字节被调用 → 解析文本并入问题；原文保留。"""
    att = Attachment(kind="file", file_name="report.pdf", content=b"%PDF-FAKE")
    msg = _text_msg("总结一下", attachments=[att])
    sender, _ = _make_streaming_sender()

    parsed = MagicMock()
    parsed.text = "这是解析出的正文内容。"
    captured: dict = {}

    with (
        _handler_env(),
        patch("bycall.rag.parsers.engines.parse_document", return_value=parsed) as mock_parse,
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    # parse_document 以附件原始字节 + 文件名被调用
    mock_parse.assert_called_once_with(b"%PDF-FAKE", "report.pdf")
    # 解析文本作为带标注的上下文并入下游问题
    assert "[附件：report.pdf]" in captured["question"]
    assert "这是解析出的正文内容。" in captured["question"]
    # 原始问题保持完整
    assert "总结一下" in captured["question"]
    # attachment.text 回填
    assert att.text == "这是解析出的正文内容。"


def test_file_attachment_parse_failure_graceful():
    """parse_document 抛错 → 降级为失败提示并入问题，主消息流不中断（有最终回复）。"""
    att = Attachment(kind="file", file_name="bad.xlsx", content=b"\x00\x01")
    msg = _text_msg("看看这个", attachments=[att])
    sender, cap = _make_streaming_sender()

    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.rag.parsers.engines.parse_document",
            side_effect=RuntimeError("解析器崩溃"),
        ),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))  # 不应抛异常

    assert "[附件解析失败：bad.xlsx]" in captured["question"]
    assert "看看这个" in captured["question"]
    # 主流程照常走完——发出了最终流式帧
    finish_frames = [c for c in cap if c[0] == "stream" and c[3] is True]
    assert len(finish_frames) == 1


def test_image_attachment_injected_as_placeholder():
    """图片附件：视觉模型未保证 → 注入 [图片] 占位，不崩。"""
    att = Attachment(kind="image", file_name="pic.png", content=b"\x89PNG")
    msg = _text_msg("这是什么", attachments=[att])
    sender, _ = _make_streaming_sender()

    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert "[图片]" in captured["question"]
    assert "这是什么" in captured["question"]


def test_standalone_image_attachment_reaches_agent():
    """纯图片消息虽无文本，也必须进入附件增强与治理链路。"""
    msg = _text_msg("", attachments=[Attachment(kind="image", content=b"PNG")])
    sender, _ = _make_streaming_sender()
    captured: dict = {}

    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert captured["question"] == "[图片]"


def test_standalone_file_attachment_reaches_agent():
    """纯文件消息解析后进入治理链路，不因原始文本为空被丢弃。"""
    attachment = Attachment(kind="file", file_name="report.txt", content=b"report")
    msg = _text_msg("", attachments=[attachment])
    sender, _ = _make_streaming_sender()
    parsed = MagicMock(text="文件正文")
    captured: dict = {}

    with (
        _handler_env(),
        patch("bycall.rag.parsers.engines.parse_document", return_value=parsed),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert "[附件：report.txt]" in captured["question"]
    assert "文件正文" in captured["question"]


def test_attachment_truncated_when_overflow_question_limit(monkeypatch):
    """原文接近上限时：附件上下文被截断，原文一字不动。"""
    from bycall.agent import im_handler

    # 原文占满几乎全部预算，仅留极少空间给附件上下文。
    original = "问" * (im_handler.MAX_IM_QUESTION_LEN - 5)
    att = Attachment(kind="file", file_name="big.txt", content=b"DATA")
    msg = _text_msg(original, attachments=[att])
    sender, _ = _make_streaming_sender()

    parsed = MagicMock()
    parsed.text = "附件正文" * 500  # 远超预算
    captured: dict = {}
    with (
        _handler_env(),
        patch("bycall.rag.parsers.engines.parse_document", return_value=parsed),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    enriched = captured["question"]
    # 总长不超上限
    assert len(enriched) <= im_handler.MAX_IM_QUESTION_LEN
    # 原文完整保留（在开头）
    assert enriched.startswith(original)


# ============ B. 引用卡片 ============


def test_citations_attached_to_final_stream_frame():
    """有检索来源 → 最终流式帧带 msg_item（来源卡片列表）。"""
    msg = _text_msg("公司介绍")
    sender, cap = _make_streaming_sender()
    result = _answered_result(
        citations=[
            {"title": "文档A", "source": "public", "url": "http://a/doc", "snippet": "片段A内容"},
            {"title": "文档B", "source": "hr", "url": "", "snippet": "片段B内容"},
        ]
    )

    with (
        _handler_env(),
        patch("bycall.agent.im_handler._run_stream", _run_stream_plain(result)),
    ):
        asyncio.run(handle_im_message(msg, sender))

    finish_frames = [c for c in cap if c[0] == "stream" and c[3] is True]
    assert len(finish_frames) == 1
    msg_item = finish_frames[0][4]
    assert msg_item, "最终帧应携带来源卡片"
    assert msg_item[0]["title"] == "文档A"
    assert msg_item[0]["url"] == "http://a/doc"
    assert msg_item[0]["description"] == "片段A内容"
    assert len(msg_item) == 2


def test_no_citations_when_no_retrieval():
    """无检索命中（纯闲聊/工具）→ 最终帧不带 msg_item。"""
    msg = _text_msg("你好")
    sender, cap = _make_streaming_sender()
    result = _answered_result(answer="你好！有什么可以帮你？", citations=[])

    with (
        _handler_env(),
        patch("bycall.agent.im_handler._run_stream", _run_stream_plain(result)),
    ):
        asyncio.run(handle_im_message(msg, sender))

    finish_frames = [c for c in cap if c[0] == "stream" and c[3] is True]
    assert len(finish_frames) == 1
    assert finish_frames[0][4] is None


def test_legacy_stream_sender_without_citations_remains_compatible():
    """无引用时只传三参数，旧渠道 sender 不会因 M6 接口扩展失败。"""
    sender, cap = _make_legacy_streaming_sender()

    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_plain(_answered_result(answer="正常回答", citations=[])),
        ),
    ):
        asyncio.run(handle_im_message(_text_msg("你好"), sender))

    assert cap[-1][1] == "正常回答"
    assert cap[-1][3] is True


def test_legacy_stream_sender_with_citations_degrades_without_crashing():
    """有引用但旧 sender 不支持 msg_item 时，降级为三参数最终帧。"""
    sender, cap = _make_legacy_streaming_sender()
    result = _answered_result(
        answer="带来源回答",
        citations=[{"title": "文档", "source": "public", "url": "http://doc"}],
    )

    with (
        _handler_env(),
        patch("bycall.agent.im_handler._run_stream", _run_stream_plain(result)),
    ):
        asyncio.run(handle_im_message(_text_msg("查文档"), sender))

    assert cap[-1][1] == "带来源回答"
    assert cap[-1][3] is True


def test_citations_deduped_by_url_and_capped():
    """同 url 去重；无标题项跳过；最多 5 条。"""
    msg = _text_msg("查一下")
    sender, cap = _make_streaming_sender()
    result = _answered_result(
        citations=[
            {"title": "A", "source": "s", "url": "http://dup", "snippet": "x"},
            {
                "title": "A2",
                "source": "s",
                "url": "http://dup",
                "snippet": "dup",
            },  # url 重复 → 去掉
            {"url": "", "snippet": ""},  # 无标题也无来源 → 跳过
            *[
                {"title": f"T{i}", "source": "s", "url": f"http://u{i}", "snippet": f"s{i}"}
                for i in range(8)
            ],
        ]
    )

    with (
        _handler_env(),
        patch("bycall.agent.im_handler._run_stream", _run_stream_plain(result)),
    ):
        asyncio.run(handle_im_message(msg, sender))

    finish_frames = [c for c in cap if c[0] == "stream" and c[3] is True]
    msg_item = finish_frames[0][4]
    titles = [m["title"] for m in msg_item]
    assert "A2" not in titles  # url 去重
    assert len(msg_item) == 5  # 封顶 5


def test_citations_markdown_fallback_when_non_streaming():
    """非流式渠道 → 来源拼成 Markdown 参考列表附在答案末尾。"""
    msg = _text_msg("公司介绍")
    sender, cap = _make_text_sender()
    result = _answered_result(
        answer="答案是A。",
        citations=[
            {"title": "文档A", "source": "public", "url": "http://a/doc", "snippet": "片段A"}
        ],
    )

    with (
        _handler_env(),
        patch("bycall.agent.im_handler._run_stream", _run_stream_plain(result)),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert len(cap) == 1
    text = cap[0][1]
    assert text.startswith("答案是A。")
    assert "**参考来源：**" in text
    assert "[文档A](http://a/doc)" in text


# ============ C. 模板卡片点击 ============


def test_card_action_routed_as_normal_question():
    """card_action 消息 → 所选值作为普通问题进入治理链路。"""
    msg = InboundMessage(
        channel="wecom",
        im_user_id="u1",
        text="选项A：销售部",
        raw_frame={"event": "template_card_event"},
        chat_id="",
        message_type="card_action",
    )
    sender, _ = _make_streaming_sender()

    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    # 所选值原样进入治理链路
    assert captured["question"] == "选项A：销售部"
    assert captured["channel"] == "wecom"


# ============ D. 季度澄清卡片 ============


def test_ambiguous_quarter_sends_card_after_persisting_context():
    """模糊季度问题先保存问答上下文，再发 Q1-Q4 卡片，不调用 LLM。"""
    msg = _text_msg("请分析这个季度的销售情况")
    sender, cap = _make_streaming_sender(with_card=True)
    session = MagicMock()
    conversation = MagicMock(id=42)
    events: list[tuple] = []

    def save_message(_session, conversation_id, role, content):
        events.append(("save", conversation_id, role, content))

    async def send_card(card):
        events.append(("card", card))
        cap.append(("card", card))

    sender.send_card = send_card
    with (
        _handler_env(),
        patch("bycall.agent.im_handler.session_scope") as mock_scope,
        patch(
            "bycall.agent.session_store.get_or_create_conversation",
            return_value=conversation,
        ) as mock_conversation,
        patch("bycall.agent.session_store.save_message", side_effect=save_message),
        patch("bycall.agent.im_handler._run_stream") as mock_run,
    ):
        mock_scope.return_value.__enter__ = MagicMock(return_value=session)
        mock_scope.return_value.__exit__ = MagicMock(return_value=None)
        asyncio.run(handle_im_message(msg, sender))

    mock_conversation.assert_called_once_with(session, 7, "wecom")
    assert events[0] == ("save", 42, "user", "请分析这个季度的销售情况")
    assert events[1][0:3] == ("save", 42, "assistant")
    assert "季度" in events[1][3]
    assert events[2][0] == "card"
    session.commit.assert_called_once_with()
    mock_run.assert_not_called()
    assert len(cap) == 1
    card = cap[0][1]
    assert card["card_type"] == "button_interaction"
    assert card["task_id"].startswith("quarter_clarification_")
    assert card["main_title"] == {
        "title": "请选择季度",
        "desc": "请选择要查询的季度，我会继续处理原问题。",
    }
    assert card["button_list"] == [
        {"text": "Q1", "style": 1, "key": "Q1"},
        {"text": "Q2", "style": 1, "key": "Q2"},
        {"text": "Q3", "style": 1, "key": "Q3"},
        {"text": "Q4", "style": 1, "key": "Q4"},
    ]


@pytest.mark.parametrize(
    "text", ["分析 Q2 销售", "分析Q2季度销售", "分析第二季度销售", "quarter Q4 revenue"]
)
def test_explicit_quarter_uses_normal_path(text):
    """已明确季度时不发卡片，原文进入正常治理链路。"""
    msg = _text_msg(text)
    sender, cap = _make_streaming_sender(with_card=True)
    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert captured["question"] == text
    assert not [item for item in cap if item[0] == "card"]


def test_card_action_quarter_uses_normal_path_without_card():
    """卡片返回的 Q 值只进入正常路径，绝不递归生成澄清卡片。"""
    msg = _text_msg("Q3", message_type="card_action")
    sender, cap = _make_streaming_sender(with_card=True)
    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert captured["question"] == "Q3"
    assert not [item for item in cap if item[0] == "card"]


def test_ambiguous_quarter_without_card_support_uses_normal_path():
    """渠道不支持 send_card 时保持原有 LLM 路径。"""
    msg = _text_msg("show quarterly revenue")
    sender, cap = _make_streaming_sender()
    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert captured["question"] == "show quarterly revenue"
    assert not [item for item in cap if item[0] == "card"]


def test_quarter_question_with_attachment_uses_normal_path():
    """带附件的季度问题保留多模态路径，不发澄清卡片。"""
    attachment = Attachment(kind="image", file_name="chart.png", content=b"PNG")
    msg = _text_msg("分析季度表现", attachments=[attachment])
    sender, cap = _make_streaming_sender(with_card=True)
    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert "[图片]" in captured["question"]
    assert not [item for item in cap if item[0] == "card"]


@pytest.mark.parametrize("text", ["审批这个季度的申请", "删除这个季度的数据"])
def test_governed_quarter_message_never_uses_clarification_card(text):
    """审批或破坏性表述必须继续进入治理链路，不能借卡片交互。"""
    msg = _text_msg(text)
    sender, cap = _make_streaming_sender(with_card=True)
    captured: dict = {}
    with (
        _handler_env(),
        patch(
            "bycall.agent.im_handler._run_stream",
            _run_stream_capturing(captured, _answered_result()),
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert captured["question"] == text
    assert not [item for item in cap if item[0] == "card"]


# ============ D. IM UserID 治理链路传播 ============


def test_im_user_id_reaches_run_agent_stream():
    """IM 消息的 im_user_id 经 _run_stream 传入 run_agent_stream。

    验证整个真实路径：handle_im_message → 真实 _run_stream → run_agent_stream，
    确保企微用户标识最终到达 ApprovalContext（runner 内部用它构造审批请求）。
    仅桩掉 _run_stream 内部依赖（llm / session / conversation），
    run_agent_stream 自身用假生成器替换以捕获参数。
    """
    captured = {}

    async def fake_run_agent_stream(
        *,
        session,
        user,
        question,
        llm,
        channel="api",
        conversation_id=None,
        im_user_id="",
    ):
        captured["im_user_id"] = im_user_id
        yield "", True, _answered_result()

    msg = InboundMessage(
        channel="wecom",
        im_user_id="wecom_uid_abc123",
        text="公司介绍",
        raw_frame={},
    )
    sender, _cap = _make_streaming_sender()

    with (
        _handler_env(user_id=7),
        patch(
            "bycall.llm.resolver.get_chat_model",
            return_value=(MagicMock(), MagicMock()),
        ),
        patch(
            "bycall.agent.session_store.get_or_create_conversation",
            return_value=MagicMock(id=42),
        ),
        patch(
            "bycall.agent.runner.run_agent_stream",
            fake_run_agent_stream,
        ),
    ):
        asyncio.run(handle_im_message(msg, sender))

    assert captured["im_user_id"] == "wecom_uid_abc123"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
